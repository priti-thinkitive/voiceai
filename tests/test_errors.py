"""Verifies the error contract's shape and status codes for each handler."""

from __future__ import annotations

from httpx import AsyncClient


async def test_app_error_shape(client: AsyncClient) -> None:
    resp = await client.get("/_debug/error/app-error")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "platform_not_found"
    assert detail["field"] == "platform_id"
    assert detail["message"]
    assert detail["request_id"]


async def test_http_exception_normalised(client: AsyncClient) -> None:
    resp = await client.get("/_debug/error/http-exception")
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "forbidden"
    assert detail["message"] == "You cannot do that."
    assert detail["request_id"]


async def test_validation_error_humanised(client: AsyncClient) -> None:
    resp = await client.post("/_debug/error/validation", json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail["field"] == "name"
    assert detail["request_id"]


async def test_unhandled_exception_hides_details(client: AsyncClient) -> None:
    resp = await client.get("/_debug/error/unhandled")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "internal"
    assert "boom" not in detail["message"]
    assert detail["request_id"]
