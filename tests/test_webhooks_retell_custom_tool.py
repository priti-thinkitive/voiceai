"""Integration tests for POST /webhooks/retell/custom-tool — the mid-call
custom-tool proxy. See app/routers/webhooks.py's module docstring for the
full confirmed mechanism and routing/lookup design.

Two external-ish calls are involved, same discipline as
test_webhooks_retell_inbound.py:
  - "The voice vendor calls us" is simulated directly via the test HTTP
    client hitting our real endpoint with a real HMAC signature computed the
    same way the endpoint verifies it — no actual vendor account involved.
  - "We call Platform X" is simulated by monkeypatching
    custom_tool_relay.relay_tool_call directly (never a real httpx call to
    an external server).
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
from app.models.agent import AgentStatus, CustomToolDefinition
from app.models.call import CallStatus
from app.models.language import Language
from app.repositories import agent_repo, call_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import custom_tool_relay
from app.services.retell_adapter import VENDOR_NAME

_WEBHOOK_SECRET = "test-retell-api-key-for-custom-tool"

_TOOL = CustomToolDefinition(
    name="check_availability",
    description="Check whether a given date has an open appointment slot.",
    parameters_schema={
        "type": "object",
        "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
        "required": ["date"],
    },
    webhook_url="https://example.com/voiceai/tools/check-availability",
)


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET, *, timestamp_ms: int | None = None) -> str:
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    ts_str = str(ts)
    digest = hmac.new(
        secret.encode("utf-8"), body + ts_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v={ts_str},d={digest}"


def _tool_call_payload(
    *,
    name: str = "check_availability",
    agent_id: str = "agent_retell_xyz",
    call_id: str | None = "call_retell_abc",
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "args": args if args is not None else {"date": "2026-09-01"},
        "call": {"call_id": call_id, "agent_id": agent_id, "call_status": "ongoing"},
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


async def _seed_agent(
    db: MongoDB,
    *,
    platform_id: str,
    vendor_ref: str = "agent_retell_xyz",
    custom_tools: list[CustomToolDefinition] | None = None,
) -> str:
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
        custom_tools=custom_tools if custom_tools is not None else [_TOOL],
        status=AgentStatus.ACTIVE,
        vendor=VENDOR_NAME,
        vendor_ref=vendor_ref,
    )
    return agent.id


@pytest.fixture(autouse=True)
def _configure_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", _WEBHOOK_SECRET)


async def test_missing_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    body = json.dumps(_tool_call_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_invalid_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    body = json.dumps(_tool_call_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": "not-real"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_malformed_body_is_422(client: AsyncClient, db: MongoDB) -> None:
    body = b"not json at all"
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_unrecognized_agent_id_returns_clean_error_not_a_crash(
    client: AsyncClient, db: MongoDB
) -> None:
    """Design decision 2's failure handling: no matching Agents document ->
    graceful 200 with {"error": ...}, never a 4xx/5xx/crash.
    """
    payload = _tool_call_payload(agent_id="agent_does_not_exist")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


async def test_unrecognized_tool_name_returns_clean_error_not_a_crash(
    client: AsyncClient, db: MongoDB
) -> None:
    """Agent found, but the fired tool name isn't one of its configured
    custom_tools -> graceful fallback, same as an unrecognized agent_id.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    await _seed_agent(db, platform_id=platform_id, vendor_ref="agent_known")

    payload = _tool_call_payload(name="not_a_real_tool", agent_id="agent_known")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


async def test_routing_resolves_correct_platform_and_relays_successfully(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core routing/lookup test: call.agent_id resolves to the right
    Agents document (via vendor_ref), the matching custom_tools entry's own
    webhook_url is what gets relayed to, and Platform X's response is
    returned to the vendor verbatim.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id, vendor_ref="agent_known")
    await platform_repo.set_call_completed_webhook_url(
        db, platform_id, url="https://platformx.example.com/call-completed", new_secret="s3cr3t"
    )

    captured: dict[str, Any] = {}

    async def _fake_relay(
        *,
        webhook_url: str,
        method: str,
        tool_timeout_ms: int,
        body: dict[str, Any],
        secret: str | None,
    ) -> custom_tool_relay.CustomToolRelayResult:
        captured["webhook_url"] = webhook_url
        captured["method"] = method
        captured["body"] = body
        captured["secret"] = secret
        return custom_tool_relay.CustomToolRelayResult(
            outcome="success",
            response_body={"available": True},
            elapsed_ms=12.0,
            upstream_status=200,
        )

    monkeypatch.setattr(custom_tool_relay, "relay_tool_call", _fake_relay)

    payload = _tool_call_payload(agent_id="agent_known", call_id=None)
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )

    assert resp.status_code == 200
    assert resp.json() == {"available": True}
    assert captured["webhook_url"] == str(_TOOL.webhook_url)
    assert captured["body"]["tool_name"] == "check_availability"
    assert captured["body"]["args"] == {"date": "2026-09-01"}
    assert captured["body"]["agent_id"] == agent_id
    assert captured["body"]["call_id"] is None  # no matching Calls doc
    # The owning platform's signing secret is threaded through to the relay
    # call — proves the new platform_repo.get_by_id lookup in the router
    # actually resolves the right platform, not just that signing exists
    # somewhere in the codebase.
    assert captured["secret"] == "s3cr3t"


async def test_routing_enriches_with_our_own_call_id_when_resolvable(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When call.call_id resolves to a real Calls document (via
    call_repo.get_by_vendor_ref), the relay body carries OUR OWN call id —
    never the vendor's raw call_id — per the "never leak vendor_ref" rule.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_mongo_id = await _seed_agent(db, platform_id=platform_id, vendor_ref="agent_known")
    call = await call_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_mongo_id,
        from_number="+15551234567",
        to_number="+19129143920",
        dynamic_variables={},
        status=CallStatus.REGISTERED,
        vendor=VENDOR_NAME,
        vendor_ref="call_retell_abc",
    )

    captured: dict[str, Any] = {}

    async def _fake_relay(**kwargs: Any) -> custom_tool_relay.CustomToolRelayResult:
        captured["body"] = kwargs["body"]
        return custom_tool_relay.CustomToolRelayResult(
            outcome="success", response_body={"ok": True}, elapsed_ms=5.0, upstream_status=200
        )

    monkeypatch.setattr(custom_tool_relay, "relay_tool_call", _fake_relay)

    payload = _tool_call_payload(agent_id="agent_known", call_id="call_retell_abc")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )

    assert resp.status_code == 200
    assert captured["body"]["call_id"] == call.id
    assert captured["body"]["call_id"] != "call_retell_abc"


async def test_relay_timeout_returns_clean_error_shape(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    await _seed_agent(db, platform_id=platform_id, vendor_ref="agent_known")

    async def _fake_relay(**kwargs: Any) -> custom_tool_relay.CustomToolRelayResult:
        return custom_tool_relay.CustomToolRelayResult(
            outcome="timeout", response_body=None, elapsed_ms=8000.0, error_class="TimeoutException"
        )

    monkeypatch.setattr(custom_tool_relay, "relay_tool_call", _fake_relay)

    payload = _tool_call_payload(agent_id="agent_known")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )

    # Not a 4xx/5xx — see this module's docstring: the vendor's own
    # tool-calling contract expects a 200 with a JSON body the conversation
    # can react to, so a Platform X failure degrades gracefully instead of
    # erroring the whole call.
    assert resp.status_code == 200
    assert "error" in resp.json()


async def test_relay_upstream_error_returns_clean_error_shape(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    await _seed_agent(db, platform_id=platform_id, vendor_ref="agent_known")

    async def _fake_relay(**kwargs: Any) -> custom_tool_relay.CustomToolRelayResult:
        return custom_tool_relay.CustomToolRelayResult(
            outcome="error", response_body=None, elapsed_ms=50.0, upstream_status=500
        )

    monkeypatch.setattr(custom_tool_relay, "relay_tool_call", _fake_relay)

    payload = _tool_call_payload(agent_id="agent_known")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/custom-tool",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )

    assert resp.status_code == 200
    assert "error" in resp.json()
