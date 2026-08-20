"""Cross-cutting FastAPI dependencies: authentication and tenancy scoping.

Bootstrap steps 6/7. Every router that touches platform-scoped data depends
on `CurrentPlatform` (never re-implements key lookup) and uses
`get_platform_filter` as the base filter on every list query (never filters
on `caller.id` inline in a router).

`AdminCaller`/`get_admin_caller` (added alongside `POST /platforms`, see
app/routers/platform.py) is a SEPARATE, unrelated authentication mechanism —
a single shared internal-admin secret, not a platform's own key — for
endpoints VoiceAI's own team calls to administer platforms themselves (e.g.
creating one). See `get_admin_caller`'s own docstring for the full reasoning
on why this is deliberately not built on top of `get_current_platform`.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer

from app.config import get_settings
from app.database import MongoDB, get_db
from app.errors import AppError
from app.models.platform import PlatformInDB, PlatformStatus
from app.repositories import platform_repo
from app.security import hash_api_key

# auto_error=False so a missing header raises our own AppError (unauthenticated
# code + envelope) instead of FastAPI's default plain-text 403.
_bearer_scheme = HTTPBearer(auto_error=False)

DbDep = Annotated[MongoDB, Depends(get_db)]


async def get_current_platform(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: DbDep,
) -> PlatformInDB:
    """Resolve `Authorization: Bearer <key>` to a `PlatformInDB`.

    Missing/malformed header, unknown key, or a revoked key all return the
    same 401 unauthenticated shape — never distinguish "unknown key" from
    "revoked key" to a caller, since that would let a leaked key be probed
    for whether it was merely rotated away vs never valid at all.
    """
    if credentials is None or not credentials.credentials:
        raise AppError(
            code="unauthenticated",
            message="Missing or invalid Authorization header.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    key_hash = hash_api_key(credentials.credentials)
    platform = await platform_repo.get_by_api_key_hash(db, key_hash)
    if platform is None or platform.status != PlatformStatus.ACTIVE:
        raise AppError(
            code="unauthenticated",
            message="Invalid or revoked API key.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return platform


CurrentPlatform = Annotated[PlatformInDB, Depends(get_current_platform)]


async def get_admin_caller(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> None:
    """Authenticate an internal VoiceAI admin request via a single shared
    secret (`Settings.ADMIN_API_KEY`) — a completely separate credential and
    check from `get_current_platform` above, deliberately NOT reused or
    layered on top of it.

    **Why this is a genuinely different mechanism, not just "another
    platform key check":** `get_current_platform` resolves `Authorization:
    Bearer <key>` to a specific *platform's own* API key, by hashing the
    presented value and looking it up in the `Platforms` collection — that
    answers "which platform is this," and is the wrong question for an
    admin-only endpoint like `POST /platforms` (creating a platform), which
    must be callable by VoiceAI's own team and by NO platform's own key, no
    matter how legitimate that platform is otherwise. Reusing
    `get_current_platform` here would make every existing platform's own key
    also work as an admin credential, which is exactly backwards. This
    function instead does a single constant-time comparison against one
    configured value — no database lookup, no per-caller identity, no
    "which admin is this" question at all.

    **Why a single shared secret, not per-admin-user accounts (deliberate,
    documented scope decision, not an oversight):** there is currently no
    concept anywhere in this codebase of an "admin user" as a distinct
    identity with its own credentials, roles, or audit trail — building that
    (a real admin-accounts system, presumably with its own login/session
    flow) is a separate, later, bigger initiative, the same way a full
    customer-facing dashboard is. A single shared secret is the smallest
    correct building block for "only VoiceAI's own team can call this
    specific endpoint" and matches how `RETELL_API_KEY` already works in
    this codebase (one shared secret, configured via `.env`, no per-caller
    distinction) — the established "simplest correct call for the common
    case" pattern here, not a novel shortcut. If/when multiple admins with
    distinct identities, scoped permissions, or revocable-per-person access
    become a real requirement, that calls for a real accounts table and a
    new dependency — this function is deliberately not designed to grow into
    that by adding fields; it would be replaced, not extended.

    **Constant-time comparison**: `hmac.compare_digest`, never `==` — same
    discipline as `app/routers/webhooks.py`'s `_verify_signature`, since a
    naive `==` on a secret comparison leaks timing information proportional
    to how many leading characters matched, letting an attacker recover the
    secret byte-by-byte over many requests.

    **Failure handling — never distinguishes *why* a request was rejected**,
    the same principle `get_current_platform` documents above: missing
    header, malformed header, and a wrong secret all raise the identical 401
    unauthenticated shape. This matters slightly differently here than for a
    platform key (there's no "revoked vs never valid" distinction to hide,
    since there's only one admin secret, not per-admin records) but the same
    discipline is kept anyway — a caller probing this endpoint learns
    nothing about whether ADMIN_API_KEY is configured, malformed, or simply
    didn't match.

    **Dev/test posture**: if `ADMIN_API_KEY` is unset (the default in dev),
    every request is rejected — there is no "unverified passthrough" mode
    for this credential (unlike e.g. `webhooks.py`'s Retell-signature check,
    which deliberately allows unverified requests in dev/test so local
    tunnels work without a real vendor key). An admin endpoint with no
    working auth check at all in dev would be a worse default than requiring
    a real (test) value to be configured; `.env`/`.env.example` and the test
    suite's fixtures set a real value so this isn't friction in practice. In
    production, `Settings.assert_production_secrets()` independently refuses
    to boot at all if `ADMIN_API_KEY` is unset, so this path is effectively
    unreachable there — the runtime check below is kept anyway as a
    belt-and-suspenders backstop, matching this codebase's existing pattern
    for RETELL_API_KEY.

    Returns `None` on success (there is no "admin identity" object to return
    — unlike `get_current_platform`, which returns the resolved
    `PlatformInDB`, there is nothing more specific than "yes, this caller
    knew the admin secret" to hand back). Route functions depend on this via
    `AdminCaller` purely for its side effect of raising on failure.
    """
    settings = get_settings()
    if credentials is None or not credentials.credentials:
        raise AppError(
            code="unauthenticated",
            message="Missing or invalid Authorization header.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not settings.ADMIN_API_KEY or not hmac.compare_digest(
        credentials.credentials, settings.ADMIN_API_KEY
    ):
        raise AppError(
            code="unauthenticated",
            message="Invalid admin credentials.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


AdminCaller = Annotated[None, Depends(get_admin_caller)]


# auto_error=False for the same reason as `_bearer_scheme` above: a missing/
# malformed Basic header should fall through to our own AppError envelope
# (and, unlike Bearer, this one also needs a WWW-Authenticate response header
# on the 401 to make the browser actually pop its native login dialog — see
# get_admin_docs_caller below) rather than Starlette's default plain-text 401.
_basic_scheme = HTTPBasic(auto_error=False)


async def get_admin_docs_caller(
    basic_credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
) -> None:
    """Gate the ADMIN-ONLY Swagger docs page/schema (`/admin/docs`,
    `/admin/openapi.json` — see app/routers/admin_docs.py) with the same
    `Settings.ADMIN_API_KEY` shared secret `get_admin_caller` above already
    checks for the admin API *calls themselves* — but via HTTP Basic Auth
    instead of a Bearer header, and that difference is deliberate, not an
    inconsistency.

    **Why this exists as a second function instead of just reusing
    `get_admin_caller` directly:** `get_admin_caller` depends on
    `HTTPAuthorizationCredentials` (the `Authorization: Bearer <key>` shape),
    which is exactly right for a JSON API called by curl/Postman/an internal
    script — the audience `POST /platforms` itself is built for. A *browser*
    loading a docs page is a different kind of caller: there is no
    programmatic step where something sets an `Authorization: Bearer` header
    before the page loads, and Swagger UI's own "Authorize" button only
    attaches auth to API calls Swagger UI itself makes FROM WITHIN the
    already-loaded page — it cannot gate the page/schema load itself. So the
    docs page needs a mechanism a bare browser navigation can satisfy on its
    own, which rules out Bearer entirely for this specific use case.

    **Why HTTP Basic over a `?key=` query-string param (the other realistic
    option, deliberately rejected):** a query param is simpler to implement
    (no separate FastAPI security scheme needed) but a real, documented
    downside outweighs that: query strings routinely end up persisted in
    plaintext in places a Bearer header or Basic credential never would —
    browser history, `~/.bash_history` if fetched via curl, proxy/load-
    balancer/webserver access logs (which log the request line/URL by
    default far more often than headers), and Referer headers if the docs
    page ever links offsite. That turns a single shared admin secret (the
    same value protecting `POST /platforms` itself) into something that
    leaks by default through normal infrastructure logging, not just through
    misuse. HTTP Basic sends the credential in a request header
    (`Authorization: Basic <base64(user:pass)>`), which standard access-log
    configurations do not capture, and — the deciding factor for a *browser-
    facing* page specifically — is natively understood by every browser:
    returning a 401 with a `WWW-Authenticate: Basic` header makes the browser
    pop its own native username/password prompt with zero custom HTML/JS, the
    browser caches it for the session, and there is nothing to accidentally
    copy-paste into a shareable URL. The username field is meaningless here
    (there is only one admin identity, matching `get_admin_caller`'s single-
    shared-secret model — see that function's docstring) — only the password
    is checked, against `Settings.ADMIN_API_KEY`, via the same
    `hmac.compare_digest` constant-time comparison `get_admin_caller` uses,
    copied here rather than shared as a helper only because the two functions
    take structurally different credential objects (`HTTPAuthorizationCredentials`
    vs `HTTPBasicCredentials`) with different field names.

    **Scope reminder**: this function protects access to the DOCS PAGE only
    (viewing/browsing what admin endpoints exist and trying them out via
    Swagger UI's own separate Authorize button). It does not replace, weaken,
    or wrap `get_admin_caller` — `POST /platforms` itself still requires its
    own `Authorization: Bearer <ADMIN_API_KEY>` header on the actual API call,
    completely independently of whatever credential was used to load the docs
    page that describes it.
    """
    settings = get_settings()
    if basic_credentials is None or not settings.ADMIN_API_KEY or not hmac.compare_digest(
        basic_credentials.password, settings.ADMIN_API_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthenticated",
                "field": None,
                "message": "Admin credentials required to view this documentation.",
            },
            headers={"WWW-Authenticate": "Basic"},
        )


AdminDocsCaller = Annotated[None, Depends(get_admin_docs_caller)]


def get_platform_filter(caller: PlatformInDB) -> dict[str, Any]:
    """Base Mongo filter scoping a query to the calling platform's own data.

    Use this as the base filter on every list endpoint over platform-scoped
    collections (agents, calls, credentials, webhook config, ...) — never
    filter directly on `caller.id` inline in a router. Authorization always
    comes from the API key resolved via get_current_platform, never from a
    client-supplied header/query/body field.
    """
    return {"platform_id": caller.id}


def assert_owns_record(caller: PlatformInDB, record_platform_id: str) -> None:
    """Raise 404 (never 403) if a fetched record doesn't belong to the caller.

    404 rather than 403 so cross-platform probing on a single-record
    get/update/delete can't confirm whether the record exists at all.
    """
    if record_platform_id != caller.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
