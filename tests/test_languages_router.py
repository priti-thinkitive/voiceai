"""Integration tests for GET /languages — the static reference list of
language/locale codes accepted by POST /agents' `language` field (see
app/models/language.py, app/routers/languages.py).

No vendor/network mocking needed here at all (unlike every other list
endpoint's test module in this project) — this is a fully static, hardcoded
list, not a live vendor call, so there's nothing to monkeypatch.

No cross-platform-isolation test here, deliberately — same reasoning as
GET /voices: every platform sees the identical static list, not
platform-scoped data.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.database import MongoDB
from app.models.language import LANGUAGE_NAMES
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
    resp = await client.get("/languages")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_list_languages_returns_every_code(client: AsyncClient, db: MongoDB) -> None:
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/languages", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == len(LANGUAGE_NAMES)
    assert len(body["items"]) == len(LANGUAGE_NAMES)

    codes = {item["code"] for item in body["items"]}
    assert codes == {lang.value for lang in LANGUAGE_NAMES}


async def test_response_shape_has_code_and_name(client: AsyncClient, db: MongoDB) -> None:
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/languages", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    for item in body["items"]:
        assert isinstance(item["code"], str)
        assert isinstance(item["name"], str)
        assert item["name"]  # never blank


async def test_cantonese_entry_flags_mainland_not_hong_kong(
    client: AsyncClient, db: MongoDB
) -> None:
    """Real, sourced trap this response is meant to prevent (see
    app/models/language.py's docstring and vendor-docs/Retell.md): the
    Cantonese entry must carry the Mainland-vs-Hong-Kong distinction
    directly in the response, visible even without reading Swagger prose.
    """
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/languages", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    cantonese = next(item for item in body["items"] if item["code"] == "yue-CN")
    assert "mainland" in cantonese["name"].lower()
    assert "hong kong" in cantonese["name"].lower()

    # And the (non-existent) Hong Kong code must never appear as a valid entry.
    codes = {item["code"] for item in body["items"]}
    assert "zh-HK" not in codes


async def test_no_pagination_params_needed_full_list_in_one_response(
    client: AsyncClient, db: MongoDB
) -> None:
    """Confirms the deliberate exception to this API's usual list-endpoint
    pagination rule actually holds: no limit/offset needed, the full static
    list comes back in one response every time.
    """
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/languages", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert "limit" not in body
    assert "offset" not in body
    assert len(body["items"]) == body["total_count"]
