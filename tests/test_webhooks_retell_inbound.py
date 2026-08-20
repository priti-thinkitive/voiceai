"""Integration tests for POST /webhooks/retell/inbound — inbound
dynamic-variable injection.

Two external-ish calls are involved (the voice vendor calling us, us calling
Platform X's relay) — both are handled without a real network call, per the
standards doc's hard rule:
  - "The voice vendor calls us" is simulated directly via the test HTTP
    client hitting our real endpoint with a real HMAC signature computed the
    same way the endpoint verifies it — no actual vendor account involved.
  - "We call Platform X" is simulated by monkeypatching
    platform_relay.request_dynamic_variables directly (never a real httpx
    call to an external server), following the same monkeypatch-the-adapter
    discipline already established for retell_adapter.* elsewhere in this
    suite.
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
from app.models.language import Language
from app.repositories import agent_repo, phone_number_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import platform_relay
from app.services.retell_adapter import VENDOR_NAME

# Standing in for RETELL_API_KEY, which is now the webhook-signature secret
# (Retell's own current scheme — see app/routers/webhooks.py's module
# docstring for the full sourced reasoning behind this change).
_WEBHOOK_SECRET = "test-retell-api-key-for-inbound-variables"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET, *, timestamp_ms: int | None = None) -> str:
    """Builds a real "v={timestamp},d={digest}" header value the same way
    _verify_signature computes/parses it: HMAC-SHA256 over
    raw_body + timestamp_string (string concat), keyed by the API key.
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    ts_str = str(ts)
    digest = hmac.new(
        secret.encode("utf-8"), body + ts_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v={ts_str},d={digest}"


def _sign_old_scheme(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    """The OLD (no-longer-valid) bare-hexdigest-over-raw-body-only scheme,
    for the regression test proving it's genuinely rejected now.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _inbound_payload(
    *, to_number: str = "+19129143920", from_number: str = "+15551234567"
) -> dict[str, Any]:
    return {
        "event": "call_inbound",
        "call_inbound": {
            "agent_id": "agent_retell_xyz",
            "agent_version": 1,
            "from_number": from_number,
            "to_number": to_number,
        },
        "event_timestamp": 1234567890,
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
        vendor_ref="agent_retell_xyz",
    )
    return agent.id


async def _seed_number(
    db: MongoDB, *, platform_id: str, agent_id: str, phone_number: str = "+19129143920"
) -> None:
    await phone_number_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        phone_number=phone_number,
        area_code=912,
        nickname=None,
        vendor=VENDOR_NAME,
    )


@pytest.fixture(autouse=True)
def _configure_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file exercises real signature verification, so a
    deterministic secret is configured for the duration of each test rather
    than relying on whatever (if anything) is in the real .env — matching
    the "never depend on ambient environment state" rule already established
    for vendor-failure tests elsewhere in this suite.

    RETELL_API_KEY is the secret now, not a separate RETELL_WEBHOOK_SECRET
    (removed entirely — see app/routers/webhooks.py's module docstring).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", _WEBHOOK_SECRET)


async def test_missing_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    body = json.dumps(_inbound_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_invalid_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    body = json.dumps(_inbound_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": "not-the-real-sig"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_malformed_signature_format_missing_v_and_d_is_401(
    client: AsyncClient, db: MongoDB
) -> None:
    """A header value that isn't shaped like "v=...,d=..." at all — e.g. a
    plain hex string with no prefixes — must be rejected as malformed before
    any HMAC comparison is even attempted.
    """
    body = json.dumps(_inbound_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Retell-Signature": "deadbeef1234",  # no v=/d= at all
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_old_bare_hexdigest_scheme_is_now_correctly_rejected(
    client: AsyncClient, db: MongoDB
) -> None:
    """Regression test: eCareVoiceAI's older scheme (bare HMAC-SHA256
    hexdigest over the raw body only, no v=/d=/timestamp) must NOT still be
    accepted now that the real, current Retell scheme has been implemented.
    Proves the old scheme genuinely no longer works, not just that the new
    one does.
    """
    body = json.dumps(_inbound_payload()).encode("utf-8")
    old_style_signature = _sign_old_scheme(body)
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": old_style_signature},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_valid_v_d_signature_with_fresh_timestamp_is_accepted(
    client: AsyncClient, db: MongoDB
) -> None:
    """A correctly-formed "v={timestamp_ms},d={digest}" signature with a
    timestamp from right now must be accepted — the core new-scheme
    happy path.
    """
    payload = _inbound_payload(to_number="+19998887766")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    # Unrecognized to_number -> graceful fallback, but the key assertion is
    # that signature verification itself did NOT reject the request (a
    # rejected signature would be 401, not 200).
    assert resp.status_code == 200


async def test_stale_timestamp_correct_digest_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    """A digest that's genuinely correct for its timestamp, but the
    timestamp itself is more than 5 minutes (300s) old, must still be
    rejected — the real replay-protection property the old scheme never
    had at all.
    """
    body = json.dumps(_inbound_payload()).encode("utf-8")
    stale_ts_ms = int((time.time() - 400) * 1000)  # 400s old > 300s window
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Retell-Signature": _sign(body, timestamp_ms=stale_ts_ms),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_far_future_timestamp_correct_digest_is_rejected(
    client: AsyncClient, db: MongoDB
) -> None:
    """A timestamp far enough in the future to exceed the small forward
    clock-skew tolerance must also be rejected, not just stale-past
    timestamps.
    """
    body = json.dumps(_inbound_payload()).encode("utf-8")
    future_ts_ms = int((time.time() + 120) * 1000)  # 120s ahead > 60s tolerance
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Retell-Signature": _sign(body, timestamp_ms=future_ts_ms),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_malformed_body_is_422_not_500(client: AsyncClient, db: MongoDB) -> None:
    body = b"{not valid json"
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_unrecognized_to_number_is_graceful_fallback(
    client: AsyncClient, db: MongoDB
) -> None:
    """No PhoneNumbers record for this to_number at all — must not crash,
    must respond with the safe empty-variables fallback.
    """
    payload = _inbound_payload(to_number="+19999999999")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_inbound": {"dynamic_variables": {}}}


async def test_platform_not_registered_is_immediate_fallback_no_relay_attempted(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platform exists and owns the number, but has not registered
    inbound_variables_webhook_url — the fast, expected, majority-case path.
    No relay attempt should even be made.
    """
    called = False

    async def _should_not_be_called(**kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("relay must not be attempted when no webhook URL is registered")

    monkeypatch.setattr(platform_relay, "request_dynamic_variables", _should_not_be_called)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_number(db, platform_id=platform_id, agent_id=agent_id)
    # Deliberately do NOT set inbound_variables_webhook_url.

    body = json.dumps(_inbound_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_inbound": {"dynamic_variables": {}}}
    assert not called


async def test_platform_x_responds_fast_variables_are_used(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fast_success(**kwargs: Any) -> platform_relay.PlatformRelayResult:
        return platform_relay.PlatformRelayResult(
            outcome="success",
            dynamic_variables={"contact_name": "John Smith"},
            elapsed_ms=42.0,
        )

    monkeypatch.setattr(platform_relay, "request_dynamic_variables", _fast_success)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_number(db, platform_id=platform_id, agent_id=agent_id)
    await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://platformx.example.com/voiceai/variables"
    )

    body = json.dumps(_inbound_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_inbound": {"dynamic_variables": {"contact_name": "John Smith"}}}


async def test_platform_x_times_out_fallback_used_and_response_still_fast(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core 'not our fault' guarantee: even when Platform X's relay
    itself reports a timeout (simulated here rather than a real slow network
    call, per the no-real-network-call rule), our own endpoint still
    responds immediately with a safe fallback — it never blocks waiting.
    """

    async def _timed_out(**kwargs: Any) -> platform_relay.PlatformRelayResult:
        return platform_relay.PlatformRelayResult(
            outcome="timeout",
            dynamic_variables={},
            elapsed_ms=platform_relay.PLATFORM_RELAY_TIMEOUT_SECONDS * 1000,
            error_class="TimeoutException",
        )

    monkeypatch.setattr(platform_relay, "request_dynamic_variables", _timed_out)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_number(db, platform_id=platform_id, agent_id=agent_id)
    await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://platformx.example.com/voiceai/variables"
    )

    body = json.dumps(_inbound_payload()).encode("utf-8")

    import time

    started = time.monotonic()
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert resp.json() == {"call_inbound": {"dynamic_variables": {}}}
    # The monkeypatched relay returns instantly (it never really sleeps), so
    # our own endpoint's wall-clock time here should be trivially fast —
    # this asserts our own handler doesn't add any additional blocking wait
    # on top of whatever the relay call itself took.
    assert elapsed < 1.0


async def test_platform_x_errors_fallback_used(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _errored(**kwargs: Any) -> platform_relay.PlatformRelayResult:
        return platform_relay.PlatformRelayResult(
            outcome="error",
            dynamic_variables={},
            elapsed_ms=10.0,
            upstream_status=500,
        )

    monkeypatch.setattr(platform_relay, "request_dynamic_variables", _errored)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_number(db, platform_id=platform_id, agent_id=agent_id)
    await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://platformx.example.com/voiceai/variables"
    )

    body = json.dumps(_inbound_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_inbound": {"dynamic_variables": {}}}


async def test_dev_permissive_when_no_secret_configured(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENV=test (not production) + RETELL_API_KEY unset -> allowed through
    unverified, matching the fail-closed-in-prod/dev-permissive-locally
    contract from the standards doc, re-derived for RETELL_API_KEY as the
    new secret source (see app/routers/webhooks.py's module docstring).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", "")
    assert settings.ENV != "production"

    body = json.dumps(_inbound_payload(to_number="+19998887777")).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"call_inbound": {"dynamic_variables": {}}}


async def test_openapi_excludes_this_endpoint(client: AsyncClient, db: MongoDB) -> None:
    """This is a voice-vendor-facing webhook, never a Platform-X-facing
    Swagger surface — include_in_schema=False must actually keep it out of
    /openapi.json, not just be set and unverified.
    """
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert "/webhooks/retell/inbound" not in spec.get("paths", {})
