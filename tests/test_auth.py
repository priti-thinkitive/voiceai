"""Authentication + multi-tenancy: get_current_platform, get_platform_filter.

Uses the real /_debug/whoami endpoint (bootstrap step 7 verification
endpoint) rather than a real feature router, since no real platform-scoped
feature exists yet at this bootstrap stage.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.database import MongoDB
from app.repositories import platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix


async def _seed_platform(db: MongoDB, name: str) -> tuple[str, str]:
    """Returns (plaintext_api_key, platform_id)."""
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key, platform.id


async def test_missing_key_is_401(client: AsyncClient) -> None:
    resp = await client.get("/_debug/whoami")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_unknown_key_is_401(client: AsyncClient) -> None:
    resp = await client.get(
        "/_debug/whoami", headers={"Authorization": "Bearer voiceai_live_does_not_exist"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_valid_key_resolves_platform(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.get("/_debug/whoami", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_id"] == platform_id
    assert body["resolved_filter"] == {"platform_id": platform_id}


async def test_revoked_key_is_401(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform B")
    revoked = await platform_repo.revoke(db, platform_id)
    assert revoked is True

    resp = await client.get("/_debug/whoami", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_two_platforms_resolve_to_distinct_filters(client: AsyncClient, db: MongoDB) -> None:
    """Platform A cannot see/touch Platform B's records — the filter each
    caller resolves to is always its own platform_id, never another's."""
    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    assert id_a != id_b

    resp_a = await client.get("/_debug/whoami", headers={"Authorization": f"Bearer {key_a}"})
    resp_b = await client.get("/_debug/whoami", headers={"Authorization": f"Bearer {key_b}"})

    assert resp_a.json()["resolved_filter"] == {"platform_id": id_a}
    assert resp_b.json()["resolved_filter"] == {"platform_id": id_b}
    assert resp_a.json()["resolved_filter"] != resp_b.json()["resolved_filter"]
