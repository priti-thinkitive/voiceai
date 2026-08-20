from __future__ import annotations

from httpx import AsyncClient


async def test_health_returns_ok_and_db_connected(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "database": "connected"}
