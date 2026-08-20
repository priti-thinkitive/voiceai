"""Integration tests for POST /webhooks/retell/transcript-updated — the
live, per-turn transcript relay webhook.

Same signature-construction convention as
tests/test_webhooks_retell_post_call.py (`_sign`, same HMAC scheme —
RETELL_API_KEY is the webhook-signature secret). These tests exercise the
HTTP webhook handler's own resolve-then-relay logic in isolation from the
WebSocket side: `live_transcript_registry.relay` is asserted directly
(monkeypatched to record calls) rather than driving a real WebSocket
connection here — the full end-to-end webhook-triggers-a-real-WebSocket-
push path is covered separately in
tests/test_calls_live_transcript_ws.py, which needs a different (sync
TestClient-based) test harness for WebSocket support (see that file's own
module docstring for why).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.database import MongoDB
from app.models.agent import AgentStatus
from app.models.call import CallStatus
from app.models.language import Language
from app.repositories import agent_repo, call_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import live_transcript_registry
from app.services.retell_adapter import VENDOR_NAME

_WEBHOOK_SECRET = "test-retell-api-key-for-transcript-updated"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET, *, timestamp_ms: int | None = None) -> str:
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    ts_str = str(ts)
    digest = hmac.new(
        secret.encode("utf-8"), body + ts_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v={ts_str},d={digest}"


def _transcript_updated_payload(
    *,
    call_id: str = "call_retell_live_abc",
    turns: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "event": "transcript_updated",
        "call": {
            "call_id": call_id,
            "transcript_object": turns
            if turns is not None
            else [
                {"role": "agent", "content": "Hello, how can I help you today?"},
                {"role": "user", "content": "I'd like to book an appointment."},
            ],
        },
    }


async def _seed_platform(db: MongoDB, name: str) -> tuple[str, str]:
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key, platform.id


async def _seed_agent(db: MongoDB, *, platform_id: str) -> str:
    agent = await agent_repo.create(
        db,
        platform_id=platform_id,
        prompt="You are a friendly assistant.",
        voice_id="11labs-Adrian",
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[],
        status=AgentStatus.ACTIVE,
        vendor=VENDOR_NAME,
        vendor_ref="agent_retell_live_xyz",
    )
    return agent.id


async def _seed_registered_call(
    db: MongoDB, *, platform_id: str, agent_id: str, vendor_ref: str = "call_retell_live_abc"
) -> str:
    call = await call_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        from_number="+19129143920",
        to_number="+15551234567",
        dynamic_variables={},
        status=CallStatus.REGISTERED,
        vendor=VENDOR_NAME,
        vendor_ref=vendor_ref,
    )
    return call.id


@pytest.fixture(autouse=True)
def _configure_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", _WEBHOOK_SECRET)


async def test_valid_webhook_resolves_call_and_relays(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correctly-signed transcript_updated webhook for a known call_id
    resolves it via call_repo.get_by_vendor_ref and calls
    live_transcript_registry.relay with our own Mongo call id (never the
    vendor's call_id) and the parsed transcript turns."""
    relay_calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_relay(call_id: str, message: dict[str, Any]) -> int:
        relay_calls.append((call_id, message))
        return 1

    monkeypatch.setattr(live_transcript_registry, "relay", _fake_relay)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _transcript_updated_payload(call_id="call_retell_live_abc")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/transcript-updated",
        content=body,
        headers={"X-Retell-Signature": _sign(body), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert len(relay_calls) == 1
    relayed_call_id, message = relay_calls[0]
    assert relayed_call_id == call_mongo_id
    assert message["call_id"] == call_mongo_id
    assert message["transcript"] == [
        {"role": "agent", "content": "Hello, how can I help you today?"},
        {"role": "user", "content": "I'd like to book an appointment."},
    ]


async def test_unrecognized_call_id_is_dropped_gracefully(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call_id with no matching Calls document is acknowledged (200,
    unrecognized_call_id: true) and never relayed — no fallback-creation
    path the way /post-call has for inbound calls (see this endpoint's own
    docstring for why)."""
    relay_calls: list[Any] = []

    async def _fake_relay(call_id: str, message: dict[str, Any]) -> int:
        relay_calls.append((call_id, message))
        return 0

    monkeypatch.setattr(live_transcript_registry, "relay", _fake_relay)

    payload = _transcript_updated_payload(call_id="call_never_seen_before")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/transcript-updated",
        content=body,
        headers={"X-Retell-Signature": _sign(body), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "unrecognized_call_id": True}
    assert relay_calls == []


async def test_multiple_deliveries_for_same_call_id_are_all_relayed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression coverage for the explicit "do not dedupe by call_id"
    contract this webhook has, unlike /post-call — several deliveries
    sharing the same call_id are each relayed independently, none dropped
    as a would-be duplicate."""
    relay_calls: list[Any] = []

    async def _fake_relay(call_id: str, message: dict[str, Any]) -> int:
        relay_calls.append(message["transcript"])
        return 1

    monkeypatch.setattr(live_transcript_registry, "relay", _fake_relay)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    for turn_content in ("First turn.", "Second turn.", "Third turn, call ending."):
        payload = _transcript_updated_payload(
            turns=[{"role": "agent", "content": turn_content}]
        )
        body = json.dumps(payload).encode("utf-8")
        resp = await client.post(
            "/webhooks/retell/transcript-updated",
            content=body,
            headers={"X-Retell-Signature": _sign(body), "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    assert len(relay_calls) == 3
    assert relay_calls[0] == [{"role": "agent", "content": "First turn."}]
    assert relay_calls[2] == [{"role": "agent", "content": "Third turn, call ending."}]


async def test_invalid_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    payload = _transcript_updated_payload()
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/transcript-updated",
        content=body,
        headers={"X-Retell-Signature": "v=123,d=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_malformed_payload_is_422(client: AsyncClient) -> None:
    body = json.dumps({"event": "transcript_updated"}).encode("utf-8")  # missing "call"
    resp = await client.post(
        "/webhooks/retell/transcript-updated",
        content=body,
        headers={"X-Retell-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_no_connected_client_does_not_crash(
    client: AsyncClient, db: MongoDB
) -> None:
    """A real, unmocked live_transcript_registry.relay() call against a
    call_id nobody is currently listening for is a graceful no-op — proves
    the real (not monkeypatched) relay path never raises when the
    connections dict has nothing registered for this call_id."""
    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _transcript_updated_payload()
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/transcript-updated",
        content=body,
        headers={"X-Retell-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
