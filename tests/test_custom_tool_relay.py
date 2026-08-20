"""Unit tests for app/services/custom_tool_relay.py — specifically the new
signing behavior (Task 3, closes former Known open item 3). Mirrors
tests/test_call_completed_webhook.py's exact pattern for testing a signed
outbound relay: monkeypatch httpx.AsyncClient.request/post directly, no real
network call.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services import custom_tool_relay
from app.utils.webhook_signing import sign_webhook_body


async def test_relay_tool_call_attaches_correct_signature_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real regression test: the signature IS present on the outbound
    request, and is independently recomputable/checkable by a receiver —
    build the expected HMAC ourselves here (via the same shared
    sign_webhook_body helper Platform X's own verification would use) and
    assert it matches the header the relay function actually sent.
    """
    captured: dict[str, Any] = {}

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["content"] = kwargs.get("content")
        return httpx.Response(200, json={"available": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await custom_tool_relay.relay_tool_call(
        webhook_url="https://platformx.example.com/voiceai/tools/check-availability",
        method="POST",
        tool_timeout_ms=10_000,
        body={"tool_name": "check_availability", "args": {"date": "2026-09-01"}},
        secret="test-relay-secret",
    )

    assert result.outcome == "success"
    assert result.response_body == {"available": True}

    assert captured["headers"]["X-VoiceAI-Signature"]
    expected_signature = sign_webhook_body(raw_body=captured["content"], secret="test-relay-secret")
    assert captured["headers"]["X-VoiceAI-Signature"] == expected_signature
    # Signed over the raw JSON body, not some other representation.
    assert json.loads(captured["content"]) == {
        "tool_name": "check_availability",
        "args": {"date": "2026-09-01"},
    }


async def test_relay_tool_call_refuses_to_send_unsigned_when_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive edge case, same hard-fail discipline as
    call_completed_webhook.deliver()'s missing-secret handling: a platform
    with no signing secret yet must never get an unsigned request sent on
    its behalf.
    """
    called = {"n": 0}

    async def _should_not_be_called(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", _should_not_be_called)

    result = await custom_tool_relay.relay_tool_call(
        webhook_url="https://platformx.example.com/voiceai/tools/check-availability",
        method="POST",
        tool_timeout_ms=10_000,
        body={"tool_name": "check_availability", "args": {}},
        secret=None,
    )

    assert result.outcome == "error"
    assert called["n"] == 0


async def test_relay_tool_call_different_secrets_produce_different_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check that the signature is genuinely derived from the
    platform's own secret, not a fixed/shared value — two different
    platforms' relays must produce two different signatures for the same
    body.
    """
    captured_signatures: list[str] = []

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        headers = kwargs.get("headers") or {}
        captured_signatures.append(headers["X-VoiceAI-Signature"])
        return httpx.Response(200, json={}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    for secret in ("secret-a", "secret-b"):
        await custom_tool_relay.relay_tool_call(
            webhook_url="https://platformx.example.com/tool",
            method="POST",
            tool_timeout_ms=10_000,
            body={"tool_name": "check_availability", "args": {}},
            secret=secret,
        )

    assert len(captured_signatures) == 2
    assert captured_signatures[0] != captured_signatures[1]
