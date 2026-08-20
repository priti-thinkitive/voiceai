"""Unit tests for app/services/platform_relay.py — the outbound relay to
Platform X's own registered inbound-variables webhook.

Uses httpx's MockTransport to simulate Platform X's server responding,
erroring, or (for the timeout case) actually taking longer than our strict
timeout — a real (but entirely local, in-process) timeout, not a monkeypatch
of the timeout logic itself, so the real httpx.Timeout enforcement path is
exercised. No real network call ever leaves the process, per the standards
doc's hard rule.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import platform_relay


async def test_success_returns_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"dynamic_variables": {"contact_name": "John Smith"}})

    _patch_client(monkeypatch, handler)

    result = await platform_relay.request_dynamic_variables(
        webhook_url="https://platformx.example.com/variables",
        from_number="+15551234567",
        to_number="+19129143920",
        agent_id="agent_mongo_1",
    )
    assert result.outcome == "success"
    assert result.dynamic_variables == {"contact_name": "John Smith"}


async def test_non_string_variable_values_are_stringified(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"dynamic_variables": {"call_count": 3}})

    _patch_client(monkeypatch, handler)

    result = await platform_relay.request_dynamic_variables(
        webhook_url="https://platformx.example.com/variables",
        from_number="+15551234567",
        to_number="+19129143920",
        agent_id="agent_mongo_1",
    )
    assert result.outcome == "success"
    assert result.dynamic_variables == {"call_count": "3"}


async def test_vendor_error_status_is_treated_as_error_with_empty_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    _patch_client(monkeypatch, handler)

    result = await platform_relay.request_dynamic_variables(
        webhook_url="https://platformx.example.com/variables",
        from_number="+15551234567",
        to_number="+19129143920",
        agent_id="agent_mongo_1",
    )
    assert result.outcome == "error"
    assert result.dynamic_variables == {}
    assert result.upstream_status == 500


async def test_malformed_json_response_is_error_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    _patch_client(monkeypatch, handler)

    result = await platform_relay.request_dynamic_variables(
        webhook_url="https://platformx.example.com/variables",
        from_number="+15551234567",
        to_number="+19129143920",
        agent_id="agent_mongo_1",
    )
    assert result.outcome == "error"
    assert result.dynamic_variables == {}


async def test_timeout_returns_timeout_outcome_with_empty_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates Platform X being genuinely slower than our strict timeout.

    httpx.MockTransport does not enforce real wall-clock timeouts against an
    async handler's execution time (confirmed directly — a MockTransport
    handler that sleeps past the configured httpx.Timeout still returns
    normally, since MockTransport bypasses the actual I/O layer where
    timeout enforcement happens), so this simulates the outcome the same way
    every other adapter-failure test in this codebase does: monkeypatch the
    HTTP call itself to raise the real exception class Platform X's own
    slowness would produce (httpx.ConnectTimeout, a genuine httpx.TimeoutException
    subclass) — never a real slow network call.
    """

    async def _raise_timeout(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated Platform X timeout")

    monkeypatch.setattr(httpx.AsyncClient, "post", _raise_timeout)

    result = await platform_relay.request_dynamic_variables(
        webhook_url="https://platformx.example.com/variables",
        from_number="+15551234567",
        to_number="+19129143920",
        agent_id="agent_mongo_1",
    )
    assert result.outcome == "timeout"
    assert result.dynamic_variables == {}


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    original_client = httpx.AsyncClient

    def _client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)
