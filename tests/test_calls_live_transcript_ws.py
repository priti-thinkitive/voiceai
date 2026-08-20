"""Integration tests for WS /calls/{call_id}/live-transcript — the WebSocket
endpoint Platform X connects to for a live, per-turn transcript feed, and its
end-to-end interaction with POST /webhooks/retell/transcript-updated.

**Why this file drives the ASGI WebSocket protocol directly, instead of
using `starlette.testclient.TestClient`'s usual `websocket_connect(...)`.**
FastAPI/Starlette's own documented mechanism for testing a WebSocket route
is `TestClient` used as a context manager (`with TestClient(app) as
client: ... client.websocket_connect(url) ...`), and that IS the right
choice in an otherwise-synchronous test file. This project's test suite is
NOT that shape, though: `tests/conftest.py`'s `db`/`client` fixtures are
async, driven by `pytest-asyncio` (`asyncio_mode = "auto"`, see
`pyproject.toml`), and `TestClient`'s context-manager form runs the app's
`lifespan` (and every `websocket_connect`) on a SEPARATE thread with its own
dedicated event loop (`anyio.from_thread.start_blocking_portal`) — a
genuinely different loop from pytest-asyncio's own.

**A real, reproducible bug found while building this file, root-caused (not
worked around blindly), worth a permanent record for the next person who
touches this:** mixing `anyio`'s blocking-portal thread with an
already-active `pytest-asyncio` session reliably breaks WebSocket message
delivery the moment ANY other test — even a trivial, fully synchronous one
with no event loop of its own — has already run earlier in the same pytest
process. Confirmed directly, step by step: `live_transcript_registry.
connection_count()` correctly showed the WebSocket's own connection
registered; `live_transcript_registry.relay()` (called from the webhook
POST, itself correctly routed to the SAME in-process registry) logged a
successful delivery with no exception; and yet the test-side
`WebSocketTestSession`'s receive queue never saw the message, so
`receive_json()` blocked forever. The same exact code, run as the FIRST and
ONLY test in a fresh pytest process, always worked. This points at
`pytest-asyncio`'s own event-loop-policy/session state interacting badly
with `anyio.from_thread.start_blocking_portal()`'s thread-and-loop
bootstrapping — a known troublesome combination in the wider Python async
ecosystem, not a bug in this endpoint or in `live_transcript_registry`
itself (both were independently verified correct via direct, non-WebSocket
calls during this investigation).

**The fix**: don't use a second thread/loop at all. This file drives the
ASGI WebSocket protocol by hand — a small `_ASGIWebSocketSession` helper
below that calls `app(scope, receive, send)` as a plain `asyncio.Task` on
THIS SAME event loop (whichever one `pytest-asyncio` is already running for
the current `async def test_...`), using `asyncio.Queue` for the ASGI
`receive`/`send` channels. This is a lower-level, more manual approach than
`TestClient.websocket_connect`, but it is a direct, faithful implementation
of the real ASGI WebSocket protocol (`websocket.connect` ->
`websocket.accept`/`websocket.close` -> `websocket.receive`/
`websocket.send` -> `websocket.disconnect`, per the ASGI spec) — not a
simplification that risks missing a real bug in the route under test. Every
piece of async work in these tests (seeding data via the ordinary `db`
fixture, connecting the WebSocket, posting the webhook, receiving the
relayed message) now runs on the exact same single event loop, exactly like
real production (one process, one loop) and exactly like every other test
file in this suite — no portal, no second thread, no cross-loop risk.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.database import MongoDB
from app.main import create_app
from app.models.agent import AgentStatus
from app.models.call import CallStatus
from app.models.language import Language
from app.repositories import agent_repo, call_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services.retell_adapter import VENDOR_NAME

_WEBHOOK_SECRET = "test-retell-api-key-for-live-transcript-ws"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    ts_str = str(int(time.time() * 1000))
    digest = hmac.new(
        secret.encode("utf-8"), body + ts_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v={ts_str},d={digest}"


class WebSocketRejectedError(Exception):
    """Raised by `_ASGIWebSocketSession.connect()` when the server closes
    (or denies) the connection instead of accepting it — the direct
    equivalent of the `WebSocketDisconnect` Starlette's own
    `WebSocketTestSession` raises in the same situation.
    """

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"WebSocket rejected with close code {code}")


class _ASGIWebSocketSession:
    """A minimal, direct ASGI WebSocket client — runs the target `app` as a
    plain `asyncio.Task` on the CURRENT event loop (no thread, no portal;
    see this module's docstring for why that matters here). Implements only
    the handful of ASGI WebSocket messages this test file's own scenarios
    need: connect/accept/close/send/receive — not a general-purpose test
    client, deliberately narrow to what this file actually exercises.
    """

    def __init__(self, app: Any, path: str) -> None:
        self._app = app
        self._scope: dict[str, Any] = {
            "type": "websocket",
            "path": path.split("?", 1)[0],
            "raw_path": path.split("?", 1)[0].encode("utf-8"),
            "query_string": (path.split("?", 1)[1].encode("utf-8") if "?" in path else b""),
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "scheme": "ws",
            "subprotocols": [],
        }
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._from_app.put(message)

    async def connect(self) -> None:
        """Sends `websocket.connect` and waits for the server's first
        response message (`websocket.accept` or `websocket.close`) —
        raises `WebSocketRejectedError` in the close case, mirroring
        `WebSocketTestSession.__enter__`'s own `_raise_on_close` behavior.
        """
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self._from_app.get(), timeout=5.0)
        if message["type"] == "websocket.close":
            raise WebSocketRejectedError(code=message.get("code", 1000))

    async def receive_json(self, *, timeout: float = 5.0) -> Any:
        message = await asyncio.wait_for(self._from_app.get(), timeout=timeout)
        if message["type"] == "websocket.close":
            raise WebSocketRejectedError(code=message.get("code", 1000))
        return json.loads(message["text"])

    async def close(self) -> None:
        if self._task is None or self._task.done():
            return
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()


class _websocket_connect:  # noqa: N801 — deliberately lowercase, mirrors contextlib-style helpers
    """Async context manager wrapping `_ASGIWebSocketSession` — mirrors
    `TestClient.websocket_connect(...)`'s own `with ... as websocket:` shape
    so the test bodies below read the same way they would with the real
    Starlette helper, just async (`async with`) since everything here runs
    on the current event loop rather than a separate thread.
    """

    def __init__(self, app: Any, path: str) -> None:
        self._session = _ASGIWebSocketSession(app, path)

    async def __aenter__(self) -> _ASGIWebSocketSession:
        await self._session.connect()
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        await self._session.close()


async def _seed_scenario(
    db: MongoDB, *, name: str, vendor_ref: str
) -> tuple[str, str, str]:
    """Seeds one platform/agent/call, all through the SAME `db` this test's
    own fixture already connected (pytest-asyncio's loop) — returns
    (api_key, platform_id, call_id).
    """
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    agent = await agent_repo.create(
        db,
        platform_id=platform.id,
        prompt="You are a friendly assistant.",
        voice_id="11labs-Adrian",
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[],
        status=AgentStatus.ACTIVE,
        vendor=VENDOR_NAME,
        vendor_ref=f"agent_retell_ws_{vendor_ref}",
    )
    call = await call_repo.create(
        db,
        platform_id=platform.id,
        agent_id=agent.id,
        from_number="+19129143920",
        to_number="+15551234567",
        dynamic_variables={},
        status=CallStatus.REGISTERED,
        vendor=VENDOR_NAME,
        vendor_ref=vendor_ref,
    )
    return api_key, platform.id, call.id


async def _seed_second_platform_key(db: MongoDB, *, name: str) -> str:
    """A second platform's own valid api_key — for the cross-tenant
    rejection test, which needs a REAL, resolvable-but-wrong key, not a
    garbage string."""
    api_key = generate_api_key()
    await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key


async def _post_signed_webhook(
    app: Any, call_vendor_ref: str, turns: list[dict[str, str]]
) -> dict[str, Any]:
    """POSTs the signed transcript_updated webhook against the SAME `app`
    instance the WebSocket connected to, via an ordinary ASGITransport call
    — same pattern tests/test_webhooks_retell_transcript_updated.py already
    uses, run here on the SAME event loop as everything else in this file
    (no thread/portal involved anywhere in this file — see module
    docstring).
    """
    payload = {
        "event": "transcript_updated",
        "call": {"call_id": call_vendor_ref, "transcript_object": turns},
    }
    body = json.dumps(payload).encode("utf-8")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/webhooks/retell/transcript-updated",
            content=body,
            headers={"X-Retell-Signature": _sign(body), "Content-Type": "application/json"},
        )
    return {"status_code": resp.status_code, "json": resp.json()}


@pytest.fixture(autouse=True)
def _configure_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", _WEBHOOK_SECRET)


@pytest.fixture()
async def ws_app(db: MongoDB) -> AsyncIterator[Any]:
    """A fresh app instance for this file's own direct-ASGI WebSocket
    driving — built the same way `tests/conftest.py`'s own `client` fixture
    builds its app, reusing the SAME already-connected `db` (the `db`
    fixture parameter here forces fixture ordering: Mongo is connected
    before this app is used for anything).
    """
    yield create_app()


async def test_authorized_client_receives_relayed_update(
    db: MongoDB, ws_app: Any
) -> None:
    """The core end-to-end test: connect a WebSocket with a valid key for
    the OWNING platform, POST a signed transcript_updated webhook for that
    call, and confirm the WS client receives the relayed update in real
    time.
    """
    api_key, _platform_id, call_id = await _seed_scenario(
        db, name="Platform Live WS", vendor_ref="call_retell_ws_abc"
    )

    async with _websocket_connect(
        ws_app, f"/calls/{call_id}/live-transcript?api_key={api_key}"
    ) as websocket:
        result = await _post_signed_webhook(
            ws_app,
            "call_retell_ws_abc",
            [
                {"role": "agent", "content": "Hello, how can I help?"},
                {"role": "user", "content": "I have a question."},
            ],
        )
        assert result["status_code"] == 200
        message = await websocket.receive_json()

    assert message["call_id"] == call_id
    assert message["transcript"] == [
        {"role": "agent", "content": "Hello, how can I help?"},
        {"role": "user", "content": "I have a question."},
    ]


async def test_wrong_platform_key_is_rejected(db: MongoDB, ws_app: Any) -> None:
    """A WebSocket client authenticating with a DIFFERENT platform's own
    valid API key cannot subscribe to a call it doesn't own — the
    connection is closed (1008 Policy Violation), matching the 404-never-403
    tenancy discipline every other single-call lookup in this codebase
    already has, adapted to WebSocket's own close-code mechanism."""
    _owner_api_key, owner_platform_id, call_id = await _seed_scenario(
        db, name="Platform Owner", vendor_ref="call_retell_ws_wrongkey"
    )
    other_api_key = await _seed_second_platform_key(db, name="Platform Other")

    with pytest.raises(WebSocketRejectedError) as exc_info:
        async with _websocket_connect(
            ws_app, f"/calls/{call_id}/live-transcript?api_key={other_api_key}"
        ) as websocket:
            await websocket.receive_json()
    assert exc_info.value.code == 1008


async def test_no_api_key_is_rejected(db: MongoDB, ws_app: Any) -> None:
    """A WebSocket connection with no api_key query param at all is
    rejected the same way as a wrong-platform key — never accepted."""
    _api_key, _platform_id, call_id = await _seed_scenario(
        db, name="Platform A", vendor_ref="call_retell_ws_nokey"
    )

    with pytest.raises(WebSocketRejectedError) as exc_info:
        async with _websocket_connect(
            ws_app, f"/calls/{call_id}/live-transcript"
        ) as websocket:
            await websocket.receive_json()
    assert exc_info.value.code == 1008


async def test_unknown_call_id_is_rejected(db: MongoDB, ws_app: Any) -> None:
    """A well-formed but non-existent call_id is rejected the same way —
    never distinguished from a wrong-platform call_id (same 404-never-403-
    adjacent reasoning)."""
    api_key, _platform_id, _call_id = await _seed_scenario(
        db, name="Platform A", vendor_ref="call_retell_ws_unused"
    )

    with pytest.raises(WebSocketRejectedError) as exc_info:
        async with _websocket_connect(
            ws_app, f"/calls/64b64b64b64b64b64b64b64/live-transcript?api_key={api_key}"
        ) as websocket:
            await websocket.receive_json()
    assert exc_info.value.code == 1008


async def test_no_connected_client_webhook_is_graceful_noop(
    db: MongoDB, ws_app: Any
) -> None:
    """A transcript_updated webhook for a call nobody is currently
    WebSocket-connected to must not crash — this is the ordinary, expected
    case for most calls most of the time (live-only, no guaranteed
    delivery)."""
    await _seed_scenario(db, name="Platform A", vendor_ref="call_retell_ws_lonely")

    result = await _post_signed_webhook(
        ws_app, "call_retell_ws_lonely", [{"role": "agent", "content": "Nobody is listening."}]
    )
    assert result["status_code"] == 200
    assert result["json"] == {"ok": True}
