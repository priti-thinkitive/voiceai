"""GET /admin/docs and GET /admin/openapi.json — a SECOND, separate Swagger
UI surface that shows admin-only endpoints (e.g. `POST /platforms`) alongside
every ordinary customer-facing endpoint, gated behind the `ADMIN_API_KEY`
shared secret via HTTP Basic Auth.

**Why this exists as a second surface instead of just un-hiding `POST
/platforms` on the existing public `/docs`:** `POST /platforms` is
deliberately `include_in_schema=False` on the main app (see
app/routers/platform.py's docstring) specifically so Platform X, browsing the
public docs, never sees an admin-only "create any platform" endpoint they
have no credential for. That reasoning is still correct and unchanged. But
VoiceAI's own team legitimately wants a real, browsable, try-it-out Swagger
page that shows admin endpoints too — the two needs (hide from customers,
show to admins) aren't in tension, they just need two different pages behind
two different visibility rules. This module is that second page.

**Why a superset schema, not a replacement.** An admin doing platform
onboarding work also needs to see (and possibly try) the ordinary
customer-facing endpoints — e.g. to sanity-check what `PATCH /platform` looks
like from a platform's point of view while setting one up, or to cross-
reference `POST /platforms`' response shape against `GET`-style endpoints
elsewhere. A schema that showed ONLY admin routes would be a strictly worse
tool for that person than one that shows everything the public docs show PLUS
the admin-only routes layered on top. Concretely: the admin schema is built
by calling FastAPI's own `get_openapi()` (the same function `FastAPI.openapi()`
calls internally to build the public schema) against the SAME `app.routes`
list, but with every route's `include_in_schema` forced to `True` — see
`_build_admin_openapi_schema` below for exactly how and why a simple
"don't filter" pass over `get_openapi()` isn't enough on its own.

**Why a second `FastAPI` sub-app / mount was NOT used.** FastAPI supports
mounting a second `FastAPI()` instance at a sub-path (`app.mount("/admin",
sub_app)`) with its own independent `docs_url`/`openapi_url`, but that
approach requires either (a) registering every route TWICE — once on the main
app, once on the sub-app, which is exactly the kind of drift-prone
duplication this codebase avoids elsewhere (see e.g. how `PATCH /platform`
and `POST /platforms` share one file specifically to avoid duplicating
platform-model logic), or (b) some form of route-sharing between two distinct
`FastAPI` app instances, which FastAPI does not support directly (a route is
owned by one `APIRouter`/`FastAPI` app's `.routes` list). Reusing
`get_openapi()` against the *existing* `app.routes` — the routes are already
registered exactly once via `app.include_router(...)` in main.py — avoids
that duplication entirely: this module reads the already-built route list,
it never re-declares any endpoint.

**Auth mechanism — HTTP Basic, delegated entirely to
`app.deps.get_admin_docs_caller` / `AdminDocsCaller`.** See that dependency's
own docstring in app/deps.py for the full reasoning on why HTTP Basic was
chosen over a `?key=` query parameter (short version: Basic Auth credentials
travel in a request header, which standard web-server/proxy access-log
configurations do not capture by default, unlike a query string, which
routinely does end up in logs/browser history/Referer headers — a real
exposure difference for a value that is the same shared secret protecting
every actual admin API call). Both routes below depend on `AdminDocsCaller`;
neither is reachable without the correct `ADMIN_API_KEY` as the Basic Auth
password (the username is ignored/meaningless — see that docstring).

**Availability mirrors the public docs' own gating**: both routes below are
only registered when `settings.docs_enabled` (dev-only, same flag that
gates `/docs`/`/redoc`/`/openapi.json` in main.py) — there is no reason for
an interactive Swagger UI (admin or otherwise) to exist in a production
deployment; production admin work goes through curl/Postman/an internal
script directly against `POST /platforms`, same as always.
"""

from __future__ import annotations

import copy

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from app.deps import AdminDocsCaller

# Path choice: `/admin/docs` + `/admin/openapi.json` — mirrors the existing
# `/admin` naming already established by `admin_router = APIRouter(prefix=
# "/platforms", tags=["admin"])`'s "admin" tag in app/routers/platform.py,
# and reads unambiguously as "the internal side of this API," the same way
# `/_debug` already signals "not part of the public surface" for
# _error_check.py. `/admin/openapi.json` (not `/admin/docs.json` or similar)
# matches FastAPI's own convention of pairing an `openapi.json` schema with
# a same-directory `docs` HTML page, just one level deeper.
_ADMIN_OPENAPI_URL = "/admin/openapi.json"
_ADMIN_DOCS_URL = "/admin/docs"


def _build_admin_openapi_schema(app: FastAPI) -> dict:
    """Build the admin OpenAPI schema fresh on every request from the live
    `app.routes` list — deliberately NOT cached (unlike FastAPI's own
    `app.openapi()`, which memoises onto `app.openapi_schema`), because
    caching here would risk accidentally caching and returning this schema
    from the PUBLIC `/openapi.json` route if the two were ever wired up
    carelessly, and because this route is admin-only/low-traffic, so the
    (small) cost of rebuilding per-request buys a structurally simpler
    "always reflects the current route table" guarantee instead.

    The one difference from how `FastAPI.openapi()` builds the public schema:
    every `APIRoute` is passed through with `include_in_schema` forced to
    `True`. This is NOT just a matter of skipping a filter step before
    calling `get_openapi()` — `get_openapi()` itself has no such filter
    (confirmed by reading its source: it iterates `routes` unconditionally).
    The actual `include_in_schema` check lives one layer deeper, inside
    `get_openapi_path()` (`fastapi/openapi/utils.py`), which reads
    `route.include_in_schema` directly off each route object and skips
    building an operation for it entirely if that's `False` — so passing
    `app.routes` through unmodified (an earlier version of this function did
    exactly that) silently reproduces the SAME exclusion the public schema
    already applies, defeating the entire point of this module. Forcing the
    flag `True` on each route is what actually makes `POST /platforms`
    (hidden on the public schema via `include_in_schema=False`) appear here.

    Routes are shallow-copied (`copy.copy`) before the flag is flipped,
    NEVER mutated in place on `app.routes` — flipping the flag on the live
    route objects, even briefly, would create a window where a concurrent
    request rebuilding the cached PUBLIC schema (`FastAPI.openapi()`,
    memoised onto `app.openapi_schema` — see main.py's `docs_url`/
    `openapi_url` wiring) could observe `include_in_schema=True` and bake
    `/platforms` into the public schema's cache. Operating on copies makes
    that race structurally impossible rather than merely unlikely. Every
    ordinary customer-facing route is unaffected either way — forcing
    `include_in_schema=True` on a route that was already `True` is a no-op.
    """
    admin_routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            route_copy = copy.copy(route)
            route_copy.include_in_schema = True
            admin_routes.append(route_copy)
        else:
            admin_routes.append(route)
    return get_openapi(
        title=f"{app.title} — Admin",
        version=app.version,
        routes=admin_routes,
    )


def register_admin_docs(app: FastAPI) -> None:
    """Attach the admin-only OpenAPI schema + Swagger UI routes to `app`.

    Called from `create_app()` in main.py, only when `settings.docs_enabled`
    — same dev-only gate as the public `/docs`. Takes `app` directly (rather
    than being a plain `APIRouter` included the usual way) because building
    the admin schema requires reading back `app.routes` at request time,
    which an `APIRouter` has no handle on until it's already been merged into
    the app's own route table.

    **Builds a fresh `APIRouter` on every call, deliberately not a
    module-level singleton.** `create_app()` runs once per test (see
    tests/conftest.py's `client` fixture) as well as once at import time for
    the real running server — a module-level `router = APIRouter()` with
    routes attached via `@router.get(...)` inside this function would
    accumulate routes EVERY time this function ran, since decorating a
    shared router object is additive, not idempotent: a second `create_app()`
    call would redecorate the same router with a second pair of handlers,
    and `app.include_router(router)` would then pull in both the old and new
    routes into the new app. Constructing `router = APIRouter(...)` fresh,
    inside this function, avoids that entirely — each `create_app()` call
    gets its own router with exactly its own two routes.
    """
    router = APIRouter(tags=["admin"])

    @router.get(_ADMIN_OPENAPI_URL, include_in_schema=False)
    async def admin_openapi_json(request: Request, _admin: AdminDocsCaller) -> dict:
        """The admin-only OpenAPI document — a superset of the public
        `/openapi.json`, additionally including every `include_in_schema=
        False` route (currently: `POST /platforms`). See this module's
        docstring for why this is a superset rather than an admin-only-routes
        replacement, and app/deps.py's `get_admin_docs_caller` for the HTTP
        Basic Auth gate in front of it.
        """
        return _build_admin_openapi_schema(request.app)

    @router.get(_ADMIN_DOCS_URL, include_in_schema=False, response_class=HTMLResponse)
    async def admin_docs_html(_admin: AdminDocsCaller) -> HTMLResponse:
        """The admin Swagger UI page itself, pointed at `_ADMIN_OPENAPI_URL`
        (not the public `/openapi.json`) so it renders the superset schema.

        Gated by the SAME `AdminDocsCaller` dependency as the schema route
        above — both the HTML shell and the JSON it fetches independently
        require the admin Basic Auth credential, so there's no way to reach
        the admin schema's contents by loading only one of the two routes
        without the credential (a browser hitting this page without
        credentials gets its own 401 before any HTML is returned; even if it
        somehow obtained the HTML separately, the schema fetch the page makes
        from inside the browser would independently 401 too).
        """
        return get_swagger_ui_html(
            openapi_url=_ADMIN_OPENAPI_URL,
            title=f"{app.title} — Admin Docs",
        )

    app.include_router(router)
