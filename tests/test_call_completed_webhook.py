"""Unit tests for app/services/call_completed_webhook.py — the outbound
"call completed" notification to Platform X.

Per the standards doc's hard rule: no real network call ever leaves the
process. httpx.AsyncClient.post is monkeypatched directly (same technique
already used by tests/test_platform_relay.py's timeout test) rather than
relying on a real MockTransport-driven timeout, since the retry/backoff loop
here needs deterministic, fast-running control over exactly how many times
the call "fails" before succeeding (or never succeeds).

`CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS` is monkeypatched down to 0 in every
retry test so the test suite doesn't actually sleep for several real
seconds per exhausted-retries test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.models.call import CallDirection, CallInDB, CallStatus
from app.services import call_completed_webhook as ccw


def _make_call(**overrides: Any) -> CallInDB:
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "id": "call_mongo_1",
        "platform_id": "platform_1",
        "agent_id": "agent_mongo_1",
        "from_number": "+19129143920",
        "to_number": "+15551234567",
        "dynamic_variables": {"contact_name": "John Smith"},
        "status": CallStatus.COMPLETED,
        "direction": CallDirection.OUTBOUND,
        "vendor": "retell",
        "vendor_ref": "call_retell_abc123",
        "recording_url": "/calls/call_mongo_1/recording",
        "transcript_url": "/calls/call_mongo_1/transcript",
        "summary": "Caller asked about visiting hours.",
        "sentiment": "Positive",
        "recording_rehost_failed": False,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return CallInDB(**defaults)


def test_build_payload_is_vendor_neutral_and_uses_own_domain_links() -> None:
    call = _make_call()
    payload = ccw.build_call_completed_payload(call)

    assert payload["call_id"] == "call_mongo_1"
    assert payload["agent_id"] == "agent_mongo_1"
    assert payload["direction"] == "outbound"
    assert payload["status"] == "completed"
    assert payload["from_number"] == "+19129143920"
    assert payload["to_number"] == "+15551234567"
    assert payload["summary"] == "Caller asked about visiting hours."
    assert payload["sentiment"] == "Positive"
    assert payload["recording_url"] == "/calls/call_mongo_1/recording"
    assert payload["transcript_url"] == "/calls/call_mongo_1/transcript"

    # Never leaks vendor identity or internal field names — same rule as
    # every *Public response model in this codebase (see CallPublic).
    serialized = str(payload)
    assert "retell" not in serialized.lower()
    assert "vendor" not in payload
    assert "vendor_ref" not in payload
    assert "platform_id" not in payload
    assert "dynamic_variables" not in payload


def test_sign_payload_is_correct_hmac_sha256() -> None:
    import hashlib
    import hmac

    body = b'{"call_id": "abc"}'
    secret = "test-secret"
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert ccw.sign_payload(raw_body=body, secret=secret) == expected


async def test_deliver_skips_cleanly_when_no_url_registered() -> None:
    call = _make_call()
    result = await ccw.deliver(call=call, webhook_url=None, webhook_secret=None)
    assert result.outcome == "not_registered"
    assert result.attempts == 0


async def test_deliver_succeeds_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["content"] = kwargs.get("content")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    call = _make_call()
    result = await ccw.deliver(
        call=call,
        webhook_url="https://platformx.example.com/call-completed",
        webhook_secret="s3cr3t",
    )

    assert result.outcome == "delivered"
    assert result.attempts == 1
    assert captured["url"] == "https://platformx.example.com/call-completed"
    assert captured["headers"]["X-VoiceAI-Signature"] == ccw.sign_payload(
        raw_body=captured["content"], secret="s3cr3t"
    )


async def test_deliver_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccw, "CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS", 0.0)

    attempt_count = {"n": 0}

    async def _flaky_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        attempt_count["n"] += 1
        if attempt_count["n"] < 3:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _flaky_post)

    call = _make_call()
    result = await ccw.deliver(
        call=call,
        webhook_url="https://platformx.example.com/call-completed",
        webhook_secret="s3cr3t",
    )

    assert result.outcome == "delivered"
    assert result.attempts == 3
    assert attempt_count["n"] == 3


async def test_deliver_exhausts_retries_and_reports_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ccw, "CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS", 0.0)

    attempt_count = {"n": 0}

    async def _always_fails(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        attempt_count["n"] += 1
        raise httpx.ConnectError("simulated Platform X unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", _always_fails)

    call = _make_call()
    result = await ccw.deliver(
        call=call,
        webhook_url="https://platformx.example.com/call-completed",
        webhook_secret="s3cr3t",
    )

    assert result.outcome == "exhausted"
    assert result.attempts == ccw.CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS
    assert attempt_count["n"] == ccw.CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS
    assert result.last_error_class == "ConnectError"
    # Never raises — the caller (the background task) never crashes because
    # of a fully-failed delivery.


async def test_deliver_refuses_to_send_unsigned_when_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive edge case: a URL registered with no secret (shouldn't
    happen via the real PATCH /platform path, but must not silently send an
    unsigned request if it ever does).
    """
    called = {"n": 0}

    async def _should_not_be_called(
        self: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _should_not_be_called)

    call = _make_call()
    result = await ccw.deliver(
        call=call, webhook_url="https://platformx.example.com/call-completed", webhook_secret=None
    )

    assert result.outcome == "exhausted"
    assert called["n"] == 0
