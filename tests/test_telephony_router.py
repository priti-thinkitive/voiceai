"""Integration tests for GET /telephony/ip-ranges — the static reference
list of CIDR ranges a Platform X customer must whitelist on their own SIP
trunk provider for BYO SIP (see app/models/telephony.py,
app/routers/telephony.py).

No vendor/network mocking needed here at all (unlike every other list
endpoint's test module in this project) — this is a fully static, hardcoded
list, not a live vendor call, so there's nothing to monkeypatch.

No cross-platform-isolation test here, deliberately — same reasoning as
GET /languages and GET /voices: every platform sees the identical static
list, not platform-scoped data.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.database import MongoDB
from app.models.telephony import IP_RANGES, LAST_VERIFIED
from app.repositories import platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix


async def _seed_platform(db: MongoDB, name: str) -> str:
    """Returns the plaintext API key."""
    api_key = generate_api_key()
    await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/telephony/ip-ranges")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_list_ip_ranges_returns_all_five(client: AsyncClient, db: MongoDB) -> None:
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/telephony/ip-ranges", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == len(IP_RANGES)
    assert len(body["items"]) == len(IP_RANGES)

    cidrs = {item["cidr"] for item in body["items"]}
    assert cidrs == {entry.cidr for entry in IP_RANGES}


async def test_response_shape_has_cidr_and_label(client: AsyncClient, db: MongoDB) -> None:
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/telephony/ip-ranges", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        assert isinstance(item["cidr"], str)
        assert isinstance(item["label"], str)
        assert item["cidr"]  # never blank
        assert item["label"]  # never blank


async def test_last_verified_is_the_literal_hand_verified_date(
    client: AsyncClient, db: MongoDB
) -> None:
    """This must be a static, hand-set date string, not a dynamic
    "as of right now" timestamp — this data is hand-verified on an
    as-needed basis, not fetched live from the vendor on every request.
    """
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/telephony/ip-ranges", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_verified"] == LAST_VERIFIED
    assert body["last_verified"] == "2026-08-21"


async def test_known_real_cidr_values_present(client: AsyncClient, db: MongoDB) -> None:
    """Regression test for the actual, real values this endpoint exists to
    serve — confirmed live against the voice vendor's own current docs.
    """
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/telephony/ip-ranges", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    cidrs = {item["cidr"] for item in body["items"]}
    assert cidrs == {
        "18.98.16.120/30",
        "3.42.144.0/23",
        "153.57.128.0/18",
        "143.223.88.0/21",
        "161.115.160.0/19",
    }


async def test_no_pagination_params_needed_full_list_in_one_response(
    client: AsyncClient, db: MongoDB
) -> None:
    """Confirms the deliberate exception to this API's usual list-endpoint
    pagination rule actually holds: no limit/offset needed, the full static
    list comes back in one response every time.
    """
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/telephony/ip-ranges", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert "limit" not in body
    assert "offset" not in body
    assert len(body["items"]) == body["total_count"]
