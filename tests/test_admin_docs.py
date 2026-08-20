"""Integration tests for the admin-only Swagger docs surface: GET
/admin/docs (HTML) and GET /admin/openapi.json (schema) — see
app/routers/admin_docs.py's module docstring for the full design (superset
schema, HTTP Basic Auth via app/deps.py's `get_admin_docs_caller`, why a
second FastAPI sub-app was not used).

These tests prove three things together:
  1. The admin routes genuinely require the admin credential (401 with none,
     401 with a wrong one) — proven with both a bare unauthenticated request
     and an httpx `auth=` (HTTP Basic) tuple carrying a wrong password.
  2. The admin schema, once unlocked, actually contains `/platforms` — the
     endpoint that's deliberately `include_in_schema=False` on the public
     app — proving this is genuinely a superset schema, not just an
     identical-to-public schema hidden behind an extra login.
  3. The EXISTING public `/openapi.json` (and, implicitly, `/docs`) are
     completely unaffected by any of this — still zero-auth, and still does
     NOT list `/platforms`. This is the regression guard for the hard
     constraint that customer-facing docs must not change at all.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.config import get_settings


def _admin_key() -> str:
    admin_key = get_settings().ADMIN_API_KEY
    assert admin_key, "ADMIN_API_KEY must be configured for these tests to be meaningful"
    return admin_key


async def test_admin_openapi_rejects_no_credential(client: AsyncClient) -> None:
    resp = await client.get("/admin/openapi.json")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"
    # The browser-popup mechanism depends on this header being present.
    assert resp.headers.get("www-authenticate") == "Basic"


async def test_admin_openapi_rejects_wrong_credential(client: AsyncClient) -> None:
    resp = await client.get("/admin/openapi.json", auth=("anything", "definitely-wrong"))
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_admin_openapi_accepts_correct_credential_and_contains_platforms(
    client: AsyncClient,
) -> None:
    """The core proof: with the right admin key as the Basic Auth password,
    the admin schema is returned AND it genuinely includes `/platforms` —
    the route that's hidden (`include_in_schema=False`) from the public
    schema. Username is irrelevant/ignored (see get_admin_docs_caller) —
    used here as "admin" only for readability, any value would work.
    """
    resp = await client.get("/admin/openapi.json", auth=("admin", _admin_key()))
    assert resp.status_code == 200
    schema = resp.json()
    assert "/platforms" in schema.get("paths", {})
    assert "post" in schema["paths"]["/platforms"]
    # Superset, not replacement: ordinary customer-facing routes are still
    # present too.
    assert "/platform" in schema.get("paths", {})
    assert "/health" in schema.get("paths", {})


async def test_admin_docs_html_rejects_no_credential(client: AsyncClient) -> None:
    resp = await client.get("/admin/docs")
    assert resp.status_code == 401


async def test_admin_docs_html_accepts_correct_credential(client: AsyncClient) -> None:
    resp = await client.get("/admin/docs", auth=("admin", _admin_key()))
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    # Points Swagger UI at the admin schema, not the public one.
    assert "/admin/openapi.json" in resp.text


async def test_public_openapi_json_unchanged_no_auth_and_no_platforms(
    client: AsyncClient,
) -> None:
    """Regression guard: the pre-existing public /openapi.json must still
    require zero auth and must still NOT list /platforms — proving the new
    admin docs surface didn't leak into or alter the public one.
    """
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/platforms" not in schema.get("paths", {})
    assert "/platform" in schema.get("paths", {})


async def test_public_docs_html_unchanged_no_auth(client: AsyncClient) -> None:
    resp = await client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
