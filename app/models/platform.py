"""Platform — a SaaS company integrating with VoiceAI. VoiceAI's tenant.

Not to be confused with eCareVoiceAI's "Tenant"/"Facility" — VoiceAI's
domain is platforms/agents/calls, with exactly one tenancy level (the
integrating platform itself), not a nested tenant->facility hierarchy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints


class PlatformStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class PlatformInDB(BaseModel):
    """Shape of a `Platforms` document as stored in Mongo.

    The plaintext API key is never stored — only its SHA-256 hash. The
    plaintext is shown to the caller exactly once, at creation time — either
    via `POST /platforms` (admin-only, see app/routers/platform.py) or
    `scripts/seed_platform.py`, the emergency/offline fallback kept in place
    alongside it.

    `inbound_variables_webhook_url` — added for inbound dynamic-variable
    injection (see app/routers/webhooks.py's module docstring for the full
    mechanism). This is the URL on Platform X's own server that we call,
    with a strict short timeout, to ask "what dynamic variables should this
    specific inbound call use" the moment the voice vendor tells us a call is
    ringing in. Optional and `None` by default — most platforms have not
    registered one yet, and an unregistered platform is the fast, expected,
    majority-case path (immediate fallback, no wasted relay attempt), not an
    error state.

    Platform X can now set/clear this themselves via `PATCH /platform` (see
    app/routers/platform.py) — the one narrow gap this field originally
    shipped with. Platform *creation*/API-key issuance now has an HTTP path
    too (`POST /platforms`, admin-only — see app/routers/platform.py's
    module docstring), replacing `scripts/seed_platform.py` as the normal way
    in; the script itself remains in place as an emergency/offline fallback.

    `call_completed_webhook_url` — a SEPARATE registered URL from
    `inbound_variables_webhook_url` above, added for the outbound
    "call completed" notification (see app/routers/webhooks.py's post-call
    handler and app/services/call_completed_webhook.py). Different purpose
    (tells Platform X a call finished, with its summary/sentiment/re-hosted
    recording+transcript links, rather than asking Platform X a question
    before a call is answered), different trigger point (fired once, after a
    call's post-call webhook has been fully processed, not synchronously
    on every inbound ring), and a different payload shape entirely — kept as
    its own field rather than reusing `inbound_variables_webhook_url` for
    both purposes, since a platform may reasonably want one without the
    other (e.g. registered for inbound personalization but not yet wired up
    to receive completion notifications, or vice versa). Optional and `None`
    by default, same "unregistered is the fast, expected, majority-case
    path" reasoning as the inbound field — see PlatformInDB's docstring
    above.

    `call_completed_webhook_secret` — a PER-PLATFORM HMAC signing secret,
    generated once (via `secrets.token_urlsafe`, see
    `generate_webhook_secret()` in app/security.py) the first time a
    platform sets `call_completed_webhook_url` to a non-null value via
    `PATCH /platform`, and reused on every later notification to that same
    platform. Per-platform, not global, decided explicitly: this mirrors how
    each platform's own API key is unique to them (see api_key_hash above) —
    a single global signing secret shared across every platform would mean
    any one platform that ever saw a valid `X-VoiceAI-Signature` (e.g. by
    inspecting their own inbound request) would then be able to forge a
    convincing-looking webhook claiming to be FOR another platform, since
    the signature alone wouldn't distinguish which platform it was signed
    for. A per-platform secret closes that: verifying a signature and
    trusting "this genuinely came from VoiceAI for MY integration" are the
    same check for a platform holding only its own secret. Never returned on
    `PlatformPublic` — same internal-only treatment as `api_key_hash`, since
    unlike `call_completed_webhook_url` itself (which a platform legitimately
    needs to see back to confirm what's registered), the *secret* is
    write-once/verify-only from Platform X's side, exactly like their own
    API key is shown once at issuance and never displayed again. Currently
    shown back once in `PATCH /platform`'s response only at the moment it is
    first generated (see `UpdatePlatformSettingsResponse` in
    app/routers/platform.py) — the one deliberate exception to "never on
    PlatformPublic," matching how an API key's own plaintext is shown once
    at creation time and never again.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    api_key_hash: str
    api_key_prefix: str  # e.g. "voiceai_live_ab12cd34" — safe to display/log
    status: PlatformStatus = PlatformStatus.ACTIVE
    inbound_variables_webhook_url: str | None = None
    call_completed_webhook_url: str | None = None
    call_completed_webhook_secret: str | None = None
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None


class PlatformPublic(BaseModel):
    """Safe-to-return shape — never includes api_key_hash.

    `inbound_variables_webhook_url` is included so a platform can see its own
    currently-registered setting when it reads back its own record (e.g. the
    response of `PATCH /platform`) — same reasoning as every other field on
    this model: it's the caller's own data, not internal-only like
    `api_key_hash`.

    `call_completed_webhook_url` is included for the same reason — a platform
    can see its own current setting. `call_completed_webhook_secret` is
    deliberately NOT included here — see PlatformInDB's docstring for why it
    is write-once/shown-once instead (same treatment as an API key's own
    plaintext), surfaced only via the dedicated one-time response in
    `PATCH /platform` at the moment it's first generated, never on this
    general-purpose read-back shape.
    """

    id: str
    name: str
    api_key_prefix: str
    status: PlatformStatus
    inbound_variables_webhook_url: str | None = None
    call_completed_webhook_url: str | None = None
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None


class CreatePlatformRequest(BaseModel):
    """POST /platforms request body — admin-only platform creation, the HTTP
    replacement for `scripts/seed_platform.py "<name>"` (see that script's
    own docstring, still kept in place as a fallback/emergency-access tool).

    `name` mirrors exactly the constraint already enforced on
    `PlatformInDB.name` (min_length=1, max_length=200) — kept in sync
    deliberately rather than left unconstrained here and only caught later at
    the repository layer, so a bad name is rejected as an ordinary `422`
    before any database write is attempted.

    Deliberately NO other fields (e.g. no `contact_email`) for this first
    version. `PlatformInDB` has no existing field for "who to contact about
    this platform" today, and adding one now — even though it's a
    plausible, small, genuinely useful addition for whoever runs this admin
    tool — would be new product surface (a new stored field, a new
    migration concern, a new question of whether/where it's ever shown back)
    that nothing in this task's scope actually requires yet. This endpoint's
    job is narrowly "replace the script, reusing exactly what the script
    already does" (see app/routers/platform.py's module docstring for the
    full reasoning) — matching the script's own single-argument shape
    exactly is the smaller, safer choice for a first version. Add
    `contact_email` (or similar) later, if/when a real need for it shows up.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Acme Voice Co"}})

    name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=200),
        Field(description="The new platform's display name."),
    ]


class CreatePlatformResponse(BaseModel):
    """POST /platforms response — the created platform's public identity,
    plus its plaintext API key shown exactly ONCE.

    Same "shown once, only in the response that generates it" shape already
    established in this codebase by `UpdatePlatformSettingsResponse.
    call_completed_webhook_secret` (see that model's docstring) and, before
    that, by `scripts/seed_platform.py`'s own printed plaintext key — this is
    the third instance of the same pattern, not a new one invented here.
    Unlike the webhook secret (which is `None` on every response except the
    one that first generates it), `api_key` here is ALWAYS present — every
    call to this endpoint creates a brand-new platform and therefore a
    brand-new key, so there is no "already generated earlier" case to ever
    return `None` for.

    Deliberately does not reuse `PlatformPublic` as a base (unlike
    `UpdatePlatformSettingsResponse`, which does): `PlatformPublic` exposes
    `inbound_variables_webhook_url`/`call_completed_webhook_url`, both
    meaningless on a platform the instant it's created (nothing has been
    registered yet, always `None`) — a smaller, purpose-built shape here
    avoids advertising fields that can never be non-null in this response.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6706f1a2b3c4d5e6f7089aaa",
                "name": "Acme Voice Co",
                "api_key": "voiceai_live_9f8e7d6c5b4a3210zyxwvutsrqponmlkjihgfedcba",
                "api_key_prefix": "voiceai_live_9f8e7d6c",
                "status": "active",
                "created_at": "2026-08-19T10:00:00Z",
            }
        }
    )

    id: str
    name: str
    api_key: Annotated[
        str,
        Field(
            description="The new platform's plaintext API key — shown ONLY in this response. "
            "It cannot be retrieved again; only its hash is stored. If it's lost, revoke the "
            "platform and create a new one."
        ),
    ]
    api_key_prefix: str
    status: PlatformStatus
    created_at: datetime


class UpdatePlatformSettingsRequest(BaseModel):
    """PATCH /platform request body — set or clear the caller's own
    registered webhook URLs. See each field's own description below for what
    it's for; both are independent settings and either can be set/cleared
    without touching the other.

    Only well-formedness (`HttpUrl`) is validated at the Pydantic-model
    layer, deliberately — the SSRF-adjacent loopback/private-network check
    (see app/utils/ssrf_guard.py's `reject_if_internal_url`) needs a real DNS
    resolution, which is blocking I/O and must not run inside a synchronous
    Pydantic validator on the event loop (see the standards doc's async-
    discipline rule) — it's done in the router instead, wrapped in
    `asyncio.to_thread`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "inbound_variables_webhook_url": "https://platformx.example.com/voiceai/variables",
                "call_completed_webhook_url": "https://platformx.example.com/voiceai/call-completed",
            }
        }
    )

    inbound_variables_webhook_url: Annotated[
        HttpUrl | None,
        Field(
            description="The URL on your own server we call to ask 'what dynamic variables "
            "should this specific inbound call use' the moment a call rings in for one of "
            "your provisioned numbers. Must be a well-formed http(s) URL that does not "
            "resolve to an internal/private network address. Send null to clear a "
            "previously-registered URL — an unregistered platform simply skips "
            "personalization for inbound calls, which is the default state."
        ),
    ] = None
    call_completed_webhook_url: Annotated[
        HttpUrl | None,
        Field(
            description="The URL on your own server we call, once, the moment a call finishes "
            "processing — carries its status, summary, sentiment, and links to its recording/ "
            "transcript on our domain. Must be a well-formed http(s) URL that does not resolve "
            "to an internal/private network address. The first time you set this to a non-null "
            "value, we generate a signing secret for you and return it once in this same "
            "response (call_completed_webhook_secret) — save it immediately, it is never shown "
            "again; use it to verify the X-VoiceAI-Signature header on every notification we "
            "send you (HMAC-SHA256 over the raw request body). Send null to clear a "
            "previously-registered URL — an unregistered platform simply doesn't receive "
            "completion notifications, which is the default state."
        ),
    ] = None


class UpdatePlatformSettingsResponse(PlatformPublic):
    """PATCH /platform's actual response shape — everything PlatformPublic
    has, plus one extra field shown ONLY at the moment a
    call_completed_webhook_secret is first generated.

    `call_completed_webhook_secret` is `None` on every response except the
    exact call that causes a NEW secret to be generated (the first time
    `call_completed_webhook_url` is set to a non-null value while none was
    previously registered) — every other response (setting the URL again
    once a secret already exists, changing the URL, clearing it, or any
    unrelated PATCH) returns `None` here, since the real secret is never
    re-displayed once shown. Same one-time-reveal pattern as an API key's own
    plaintext at issuance (scripts/seed_platform.py) — the correct, proven
    precedent in this codebase for "a value shown exactly once, at the
    moment of creation, then never again."
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6706f1a2b3c4d5e6f7089aaa",
                "name": "Aspen Quality Care Platform",
                "api_key_prefix": "voiceai_live_ab12cd34",
                "status": "active",
                "inbound_variables_webhook_url": "https://platformx.example.com/voiceai/"
                "variables",
                "call_completed_webhook_url": "https://platformx.example.com/voiceai/"
                "call-completed",
                "call_completed_webhook_secret": "wh_sec_p8x2K9mQdE4vN7tR1yB6cW3zL0aH5jS",
                "created_at": "2026-08-19T10:00:00Z",
                "updated_at": "2026-08-20T14:30:00Z",
                # revoked_at deliberately omitted, not set to None — this example depicts
                # an active platform (status="active"), and per this file's own fix for
                # UpdatePlatformSettingsResponse's example (this exact model), a literal
                # None value here would be silently dropped from the rendered OpenAPI
                # example by FastAPI/Pydantic anyway (the same bug this whole task closes)
                # — omitting the key entirely is the honest, non-misleading way to show
                # "this field is absent/inapplicable for this example record."
            }
        }
    )

    call_completed_webhook_secret: Annotated[
        str | None,
        Field(
            description="Your new HMAC signing secret for verifying call-completed "
            "notifications — present ONLY in the response that generates it (the first time "
            "you register call_completed_webhook_url). Save it now; it is never shown again. "
            "null on every other response, including this same field being read back later."
        ),
    ] = None
