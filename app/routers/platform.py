"""PATCH /platform — let the calling platform set/clear its own registered
webhook URLs: the inbound dynamic-variable webhook, and the outbound
call-completed notification webhook.

POST /platforms — admin-only platform creation + API-key issuance, the HTTP
replacement for having to run `scripts/seed_platform.py` by hand. See that
endpoint's own section below for its full design.

Originally closed the one narrow gap left by feature 6 in backend-dev.md's
Feature status section (the inbound dynamic-variable webhook): `PlatformInDB.
inbound_variables_webhook_url` existed on the model/DB, but the only way to
set it was `platform_repo.set_inbound_variables_webhook_url()` called
directly, by hand — no HTTP endpoint. Later extended (same endpoint, same
narrow shape) to also set/clear `call_completed_webhook_url` (see
app/services/call_completed_webhook.py and app/routers/webhooks.py's
post-call handler) — reusing this endpoint rather than building a second
one, per the task's explicit instruction, since both are "the calling
platform's own webhook settings" and belong on the same narrow settings
surface. `PATCH /platform` is deliberately NOT a general platform-settings/
onboarding API — it only ever touches "my own record, identified by my own
key." Platform *creation*/API-key issuance used to be a separate, still-open
gap (`scripts/seed_platform.py` was the only way in); `POST /platforms`
below closes that gap without changing anything about `PATCH /platform`'s
own narrow scope.

**Path/naming decision: `PATCH /platform` (singular, no id, no `/me`
suffix) — not `PATCH /platforms/{id}`, not `PATCH /platforms/me`.**
`/platforms/{id}` was rejected outright: it would accept a client-supplied
id, tempting (or allowing) a caller to attempt updating a DIFFERENT
platform's record — the standards doc's tenancy rules are explicit that
authorization must only ever come from the resolved API key, never a
client-supplied identifier, and the surest way to guarantee that is to make
the shape structurally incapable of taking one. `/platforms/me` was also
considered and rejected: from a PLATFORM'S OWN point of view, this project
has no `GET /platforms` collection, no `GET /platforms/{id}`, and no general
platforms-CRUD resource at all — a platform can only ever act on itself,
never look up another platform. Introducing the plural noun `/platforms`
into a platform-facing endpoint would misleadingly imply a collection
resource exists when it doesn't, from that caller's perspective. The bare
singular `/platform` (never pluralized, never carrying an id) is the
clearest signal that this operates on "your own settings" specifically, not
a general resource collection — the same reasoning the standards doc already
applies to `health.py`'s bare `/health` (no resource collection either, so no
meaningless prefix).

**Why `POST /platforms` (plural) below does NOT contradict that reasoning —
it's answering a different question, from a different caller's point of
view.** The paragraph above is about what a PLATFORM sees: no collection
exists that a platform itself can list or address by id, so pluralizing
would lie to that caller. `POST /platforms` is never called by a platform at
all — it's called by an ADMIN (VoiceAI's own team, authenticated via
`AdminCaller`, see app/deps.py's `get_admin_caller`), and from an admin's
point of view a real collection of platforms genuinely does exist and is
exactly what's being created into — that's the whole point of the endpoint.
Using the plural here is the accurate, honest name for what this operation
actually is (administratively creating a new member of a real collection),
not a naming inconsistency with the singular-`/platform` reasoning above,
which was specifically about avoiding a misleading collection implication
for a caller who has no access to any such collection. Put another way: the
earlier decision rejected `/platforms` for being MISLEADING to a platform
caller; that concern doesn't transfer to a route no platform caller can ever
successfully call in the first place.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.deps import AdminCaller, CurrentPlatform, DbDep
from app.models.platform import (
    CreatePlatformRequest,
    CreatePlatformResponse,
    PlatformInDB,
    UpdatePlatformSettingsRequest,
    UpdatePlatformSettingsResponse,
)
from app.repositories import platform_repo
from app.security import generate_api_key, generate_webhook_secret, hash_api_key, key_display_prefix
from app.utils.ssrf_guard import reject_if_internal_url

router = APIRouter(prefix="/platform", tags=["platform"])

# Separate router: same underlying resource (a Platform document), but a
# structurally different path (plural, no analogue to "my own record") and a
# structurally different auth mechanism (AdminCaller, not CurrentPlatform) —
# see this module's docstring for the full plural-vs-singular reasoning.
# Kept in this same file rather than a new one because it's a small amount of
# code about the same underlying resource/model as everything else here, not
# because the two routers share a prefix (they deliberately don't).
admin_router = APIRouter(prefix="/platforms", tags=["admin"])


def _to_response(
    platform: PlatformInDB, *, newly_generated_secret: str | None
) -> UpdatePlatformSettingsResponse:
    return UpdatePlatformSettingsResponse(
        id=platform.id,
        name=platform.name,
        api_key_prefix=platform.api_key_prefix,
        status=platform.status,
        inbound_variables_webhook_url=platform.inbound_variables_webhook_url,
        call_completed_webhook_url=platform.call_completed_webhook_url,
        call_completed_webhook_secret=newly_generated_secret,
        created_at=platform.created_at,
        updated_at=platform.updated_at,
        revoked_at=platform.revoked_at,
    )


@router.patch(
    "",
    response_model=UpdatePlatformSettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Set or clear your own registered webhook URLs",
    description="""
Register (or clear) either of the two URLs on your own server that we call
on your behalf. Both are independent settings — set one, both, or neither.

- `inbound_variables_webhook_url`: we call this to ask "what dynamic
  variables should this specific inbound call use" the moment a call rings
  in for one of your provisioned numbers.
- `call_completed_webhook_url`: we call this once, the moment a call
  finishes processing — carries its status, summary, sentiment, and links to
  its recording/transcript on our domain. The first time you set this to a
  non-null value, we generate a signing secret for you and return it once in
  `call_completed_webhook_secret` — save it immediately, it is never shown
  again. Use it to verify the `X-VoiceAI-Signature` header (HMAC-SHA256 over
  the raw request body) on every notification we send you.

There is no id parameter — this always operates on the platform identified
by your API key, never on another platform's record.

Send a field to set it, or `null` to clear a previously-registered value.
Every URL must be a well-formed http(s) address and must not resolve to an
internal/private network address (e.g. localhost or a private IP range) — we
reject those up front since these are addresses our own server will later
make real outbound requests to on your behalf.

Returns your platform's current settings, including the values that were
just saved.
""",
    responses={
        422: {
            "description": "A URL is malformed, or resolves to an internal/private network "
            "address, which is not allowed for a webhook URL we call on your behalf.",
        },
    },
)
async def update_platform_settings(
    body: UpdatePlatformSettingsRequest,
    caller: CurrentPlatform,
    db: DbDep,
) -> UpdatePlatformSettingsResponse:
    """Set or clear the calling platform's own
    `inbound_variables_webhook_url` and/or `call_completed_webhook_url`.

    Identity comes entirely from `caller` (resolved via `get_current_platform`
    from the caller's API key) — no path parameter exists for a platform id,
    so there is no shape here that could tempt or allow updating a different
    platform's record. See this module's docstring for the full naming/path
    reasoning.
    """
    await reject_if_internal_url(
        body.inbound_variables_webhook_url, field="inbound_variables_webhook_url"
    )
    await reject_if_internal_url(
        body.call_completed_webhook_url, field="call_completed_webhook_url"
    )

    inbound_url_to_store = (
        str(body.inbound_variables_webhook_url)
        if body.inbound_variables_webhook_url is not None
        else None
    )
    await platform_repo.set_inbound_variables_webhook_url(db, caller.id, url=inbound_url_to_store)

    # call_completed_webhook_url and its secret are handled together: a
    # secret is generated ONLY the first time a non-null URL is registered
    # while none previously existed — see
    # platform_repo.set_call_completed_webhook_url's docstring for why that
    # decision lives here (the router) rather than in the repository layer.
    completed_url_to_store = (
        str(body.call_completed_webhook_url)
        if body.call_completed_webhook_url is not None
        else None
    )
    newly_generated_secret: str | None = None
    if completed_url_to_store is not None and caller.call_completed_webhook_secret is None:
        newly_generated_secret = generate_webhook_secret()
    await platform_repo.set_call_completed_webhook_url(
        db, caller.id, url=completed_url_to_store, new_secret=newly_generated_secret
    )

    updated = await platform_repo.get_by_id(db, caller.id)
    assert updated is not None  # the caller's own record, resolved moments ago via its API key
    return _to_response(updated, newly_generated_secret=newly_generated_secret)


# ── POST /platforms — admin-only platform creation ──────────────────────
#
# See this module's docstring for the full naming/path reasoning (why
# `/platforms`, plural, is correct here despite `/platform` being singular
# above) and the auth-mechanism reasoning (see app/deps.py's
# `get_admin_caller` docstring for why this is a wholly separate credential
# from a platform's own API key, not a variant of it).
#
# **Swagger visibility — deliberately HIDDEN (`include_in_schema=False`),
# decided explicitly, not a default left unconsidered.** Every genuinely
# customer-facing router in this codebase (`platform.py`'s own `PATCH
# /platform`, `agents.py`, `calls.py`, ...) is Swagger-visible because
# Platform X is the intended reader of `/docs` — that's who the public
# OpenAPI surface exists for. This endpoint has a different, narrower
# audience: only VoiceAI's own team, who already know it exists (it's
# documented here, in this file, and used directly) and hold a credential
# (`ADMIN_API_KEY`) no platform is ever given. Showing it in the public
# `/docs` a Platform X engineer might browse would mean:
#   (a) advertising an endpoint they can never successfully call (a
#       confusing, functionally-useless surface for that reader — the
#       Swagger equivalent of a solicitation they can't act on), and
#   (b) a very mild information-leak/trust-signal risk: seeing a raw "create
#       any platform" admin endpoint in a vendor-neutral customer-facing API
#       doc invites the reasonable question "who else can create platforms,
#       and how is that controlled" — a question this project would rather
#       not prompt in a surface meant to describe THEIR integration, not
#       VoiceAI's own internal operations.
# This mirrors `app/routers/webhooks.py`'s existing treatment of its own
# non-customer-facing routes (`include_in_schema=False` on every voice-
# vendor-facing webhook, with the real documentation living in that module's
# docstring instead of Swagger) — the same underlying principle applied here:
# a route whose real caller is not "a Platform X developer reading /docs"
# doesn't belong in the surface built for that reader, regardless of whether
# the route happens to also be reachable over plain HTTP. Unlike the webhook
# routes (which are literally uncallable without a vendor-signed request),
# this endpoint IS a normal bearer-auth JSON endpoint an admin calls directly
# (e.g. via curl/Postman/an internal script) — hiding it from Swagger has no
# effect on that; it only removes it from the PUBLIC-facing documentation
# surface Platform X reads.
_ADMIN_TAG = "admin"


@admin_router.post(
    "",
    response_model=CreatePlatformResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
    summary="[Internal admin only] Create a new platform + issue its API key",
    tags=[_ADMIN_TAG],
)
async def create_platform(
    body: CreatePlatformRequest,
    _admin: AdminCaller,
    db: DbDep,
) -> CreatePlatformResponse:
    """Create a new platform and issue its API key — the HTTP replacement
    for having to run `python scripts/seed_platform.py "<name>"` by hand on
    a machine with direct server/database access.

    Requires `Authorization: Bearer <ADMIN_API_KEY>` (see `AdminCaller` /
    `app.deps.get_admin_caller`) — a single internal shared secret, wholly
    separate from any platform's own API key. A regular platform's own valid
    key does NOT work here; only the configured admin secret does. This is
    NOT a public self-serve signup endpoint: a customer/platform never calls
    this themselves. VoiceAI's own team calls it on a platform's behalf and
    then manually delivers the returned plaintext key to that platform
    (email/Slack/call) — that delivery step is intentionally out of scope
    here and stays manual for now.

    Reuses exactly the same key-issuance mechanism the script already uses
    (`generate_api_key`, `hash_api_key`, `key_display_prefix` from
    app/security.py, and `platform_repo.create`) — no key-generation logic
    is reimplemented here.

    The plaintext API key is returned exactly ONCE, in this response. It is
    not recoverable afterwards — only its SHA-256 hash is stored. If it's
    lost, revoke the platform (not yet exposed via this admin surface — use
    `platform_repo.revoke` directly, same as today) and create a new one.

    `scripts/seed_platform.py` remains in place unchanged as an emergency/
    offline fallback (e.g. if the API itself is unreachable, or
    `ADMIN_API_KEY` is not yet configured in a fresh environment) — this
    endpoint does not deprecate or replace it structurally, it just becomes
    the normal path for day-to-day platform onboarding going forward.
    """
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=body.name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return CreatePlatformResponse(
        id=platform.id,
        name=platform.name,
        api_key=api_key,
        api_key_prefix=platform.api_key_prefix,
        status=platform.status,
        created_at=platform.created_at,
    )
