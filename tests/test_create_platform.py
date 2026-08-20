"""Integration tests for POST /platforms — admin-only platform creation +
API-key issuance, the HTTP replacement for `scripts/seed_platform.py`.

Auth here is `AdminCaller` (app/deps.py's `get_admin_caller`), a wholly
separate mechanism from `CurrentPlatform` — a single shared
`Settings.ADMIN_API_KEY` compared via `hmac.compare_digest`, not a
per-platform key hash lookup. These tests prove that separation explicitly:
a regular platform's own valid API key must NOT work as the admin key (see
test_platform_key_does_not_work_as_admin_key below), and vice versa (the
admin key must not resolve as a platform identity — proven implicitly, since
no platform is ever seeded with the admin secret as its key).

`.env`'s ADMIN_API_KEY is real in this dev/test environment (see
get_admin_caller's own docstring: unlike the Retell-webhook-signature check,
there is deliberately no "unset admin secret -> unverified passthrough"
mode), so `get_settings().ADMIN_API_KEY` is used directly rather than a
hardcoded duplicate literal — keeps these tests correct even if the
configured value changes.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.config import get_settings
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


def _admin_headers() -> dict[str, str]:
    admin_key = get_settings().ADMIN_API_KEY
    assert admin_key, "ADMIN_API_KEY must be configured for this test to be meaningful"
    return {"Authorization": f"Bearer {admin_key}"}


async def test_create_platform_success(client: AsyncClient, db: MongoDB) -> None:
    resp = await client.post(
        "/platforms",
        json={"name": "Acme Voice Co"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Acme Voice Co"
    assert "id" in body and body["id"]
    assert "api_key" in body and body["api_key"]
    assert "api_key_prefix" in body and body["api_key_prefix"]
    # The full plaintext key must never equal the short display prefix.
    assert body["api_key"] != body["api_key_prefix"]
    assert body["api_key"].startswith("voiceai_live_")
    assert body["status"] == "active"

    # Actually persisted, not just echoed.
    persisted = await platform_repo.get_by_id(db, body["id"])
    assert persisted is not None
    assert persisted.name == "Acme Voice Co"
    assert persisted.api_key_prefix == body["api_key_prefix"]
    # Only the hash is stored — the returned plaintext key is never itself
    # persisted anywhere.
    assert persisted.api_key_hash == hash_api_key(body["api_key"])


async def test_missing_admin_auth_is_401(client: AsyncClient) -> None:
    resp = await client.post("/platforms", json={"name": "No Auth Co"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_wrong_admin_key_is_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/platforms",
        json={"name": "Wrong Key Co"},
        headers={"Authorization": "Bearer definitely-not-the-admin-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_platform_key_does_not_work_as_admin_key(client: AsyncClient, db: MongoDB) -> None:
    """A regular platform's own valid API key is a structurally different
    credential from the admin secret — it must be rejected here, proving the
    two are genuinely non-interchangeable, not just "usually" different.
    """
    platform_api_key, _ = await _seed_platform(db, "Regular Platform")
    resp = await client.post(
        "/platforms",
        json={"name": "Sneaky New Co"},
        headers={"Authorization": f"Bearer {platform_api_key}"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"

    # And confirm no platform named "Sneaky New Co" got created despite the attempt.
    all_platforms_by_name = await db["Platforms"].find_one({"name": "Sneaky New Co"})
    assert all_platforms_by_name is None


async def test_missing_name_is_422(client: AsyncClient) -> None:
    resp = await client.post("/platforms", json={}, headers=_admin_headers())
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_blank_name_is_422(client: AsyncClient) -> None:
    resp = await client.post("/platforms", json={"name": ""}, headers=_admin_headers())
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_issued_key_is_immediately_usable(client: AsyncClient, db: MongoDB) -> None:
    """The whole point of this endpoint: the returned api_key must actually
    authenticate as a real platform on a subsequent request, not just be a
    value that was persisted. PATCH /platform (an ordinary CurrentPlatform-
    gated endpoint) with an empty no-op body proves the freshly-issued key
    genuinely resolves through get_current_platform end to end.
    """
    create_resp = await client.post(
        "/platforms",
        json={"name": "Live Wire Co"},
        headers=_admin_headers(),
    )
    assert create_resp.status_code == 201
    issued_key = create_resp.json()["api_key"]
    platform_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        "/platform",
        json={},
        headers={"Authorization": f"Bearer {issued_key}"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["id"] == platform_id


async def test_response_not_in_public_schema(client: AsyncClient) -> None:
    """include_in_schema=False — POST /platforms must not appear in the
    public OpenAPI document Platform X reads at /docs, since it's an
    internal admin-only tool, not a customer-facing API surface.
    """
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/platforms" not in schema.get("paths", {})
