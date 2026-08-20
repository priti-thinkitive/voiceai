"""Vendor adapter for Retell — the only place in this codebase that knows
Retell's actual request/response shape. Swappable per the adapter-layer seam
in vendor-docs/Full-System-Architecture.html: a future second vendor gets its
own adapter module with the same call shapes, never a branch inside this one.

**Agent/LLM creation (both `custom_llm` and `retell_llm` response_engine
modes) now lives in `app/services/retell_agent_adapter.py`, a sibling
module, not here** — split out once this file crossed the standards doc's
~800-1000 line size ceiling. See that module's docstring for the full
sourced evidence trail on both agent-creation modes; this module keeps
everything else (voices, phone numbers, calls, recording/transcript
fetching).

Confirmed against real, working evidence — not guessed (see the standards
doc's vendor-verification rule):
  - Auth: `Authorization: Bearer <RETELL_API_KEY>`, base URL
    `https://api.retellai.com` — eCareVoiceAI's retell_client.py `_client()`.

`list_voices()` — GET /voices support, closing the documented gap noted in
the standards doc's Feature status section ("Platform X has no way to
discover a valid voice_id"). Confirmed live against the same real Retell
account: `GET https://api.retellai.com/list-voices`,
`Authorization: Bearer <key>`, no query params, no pagination on Retell's
side — a flat JSON array of ~300 voice objects in one response. Real fields
confirmed via a live call: `voice_id`, `voice_type`, `standard_voice_type`,
`voice_name`, `provider` (enum incl. cartesia/elevenlabs/fish_audio/inworld/
minimax/openai/platform), `accent`, `gender`, `age`, `avatar_url`,
`preview_audio_url`, and `recommended` (bool — present on some entries,
absent on others; absence means not-recommended, never assume presence).
This is a read-through proxy, not a synced local collection — Retell's
catalog changes independently of us, so every call to our GET /voices hits
Retell live rather than a cached/persisted copy.

`get_voice()` — GET /voices/{voice_id}/preview support (the preview-audio
proxy fix; see app/routers/voices.py's module docstring for the full bug
history). Confirmed sourced in vendor-docs/Retell.md's "Discovering
available voices" section (docs.retellai.com/api-references/get-voice) and
re-verified live against the real account in this same session:
`GET https://api.retellai.com/get-voice/{voice_id}` returns the identical
per-voice schema as one entry of `/list-voices`, 200 for a real voice_id,
404 with `{"status":"error","message":"Not Found"}` for an unknown one.
Retell has no per-voice *audio* fetch API — only this metadata lookup, which
is what hands back `preview_audio_url` (a real Retell S3 URL) for the
router to then fetch server-side and stream back under our own domain.

`create_phone_number()` — POST /agents/{agent_id}/numbers support (Phase 1
item 4, "Phone number — buy new through Retell", Option 1 of the "Telephony:
two options" section; BYO SIP/Option 2 is separate future work). Confirmed
against eCareVoiceAI's real, working `buy_phone_number()`/`delete_phone_number()`
(`retell_client.py`) and Retell's live docs, both agreeing:
`POST https://api.retellai.com/create-phone-number`, same
`Authorization: Bearer <RETELL_API_KEY>` auth, success is 201. Binding to an
agent happens in the SAME create call, not a separate step — the
`inbound_agents` field, an array of `{"agent_id": ..., "weight": 1.0}`
objects. The older singular `inbound_agent_id` field is confirmed deprecated
across all Retell phone-number endpoints as of 2026-03-31 (eCareVoiceAI's
`update_phone_number()`/`detach_phone_number()` docstrings both note this
explicitly and already use the array form exclusively) — this adapter only
ever sends the array form. Optional request fields `area_code` (int),
`toll_free` (bool), `country_code` (str), `phone_number` (str, request a
specific E.164 number), `nickname` (str) are all real and documented; Retell
supplies its own sensible defaults for anything omitted. Response fields
confirmed real: `phone_number` (E.164), plus `phone_number_pretty`,
`area_code`, `nickname`, `inbound_agents`/`outbound_agents`,
`inbound_webhook_url`, `last_modification_timestamp`, and internal-looking
fields (`sip_outbound_trunk_config`, `phone_number_type`) that the router
does not expose to Platform X (see app/models/phone_number.py's
PhoneNumberPublic docstring). No documented failure mode for "requested
area code/number unavailable" — any 4xx from Retell is treated like any
other vendor rejection, same upstream_failed pattern as create_agent above.

`delete_phone_number()` — not called from any router yet (there is no
release/delete endpoint in this task's scope), but used for real manual
live-test cleanup (see backend-dev.md's Feature status entry for this
endpoint) so a real purchased number doesn't sit unreleased (and billed) in
the configured Retell account after a live verification run. Confirmed real:
`DELETE https://api.retellai.com/delete-phone-number/{phone_number}`
(eCareVoiceAI's working `delete_phone_number()`), success is 204/200 with no
body.

`import_phone_number()` — POST /agents/{agent_id}/numbers/byo support (Phase
1 item 4, Option 2 of the "Telephony: two options" section — "bring your own
SIP trunk"). Confirmed via live WebFetch of Retell's own current docs
(`docs.retellai.com/api-references/import-phone-number.md`,
`docs.retellai.com/deploy/custom-telephony.md`) — eCareVoiceAI does NOT
implement this flow at all (buy-new only), so Retell's docs are the sole
source here, not a working-code cross-check like the other adapter
functions above.

`POST https://api.retellai.com/import-phone-number` is a genuinely separate
endpoint from `/create-phone-number`, not the same endpoint with a flag.
Same `Authorization: Bearer <RETELL_API_KEY>` auth; success is 201. Real
request fields: `phone_number` (required, the E.164 number Platform X
already owns), `termination_uri` (required — identifies their SIP trunk;
this is the confirmed real field name, NOT `sip_trunk_uri`, which was an
earlier planning-doc guess that turned out wrong once actually checked
against Retell's docs), `sip_trunk_auth_username`/`sip_trunk_auth_password`
(optional — see the credential-non-persistence note below),
`ignore_e164_validation` (optional, Retell default true), `transport`
(optional: TLS/TCP/UDP, Retell default TCP), `inbound_agents`/
`outbound_agents` (same array-of-{agent_id, agent_version, weight} binding
form as create_phone_number above — confirmed still the current, non-
deprecated form), `nickname`, `inbound_webhook_url`,
`allowed_inbound_country_list`, `allowed_outbound_country_list` (all
optional, same semantics as create-phone-number). There is no
`sip_provider` field on Retell's real API — an earlier planning-doc example
invented that field name; it is not sent. Response shape is the identical
phone-number-resource shape `/create-phone-number` returns, so this
function returns the same `RetellCreatePhoneNumberResult` type as
create_phone_number above.

**Credentials are passed through to Retell in the request body and never
logged, at any level, by this function** — `sip_trunk_auth_username`/
`sip_trunk_auth_password` do not appear in any `logger.*` call below (the
warning/error logs only ever include `vendor`/`upstream_status`/
`upstream_body`/`error_class`, never the request body). See
app/models/phone_number.py's ImportPhoneNumberRequest docstring for the
full reasoning on why these are never persisted to our own DB either.

Real, documented prerequisites Platform X must handle themselves before
calling our endpoint (we cannot automate these — they happen on a
third-party SIP provider's own dashboard, confirmed via the same Retell
docs): (1) configure a SIP trunk at their own telephony provider first
(Twilio/Telnyx/Vonage and others have dedicated Retell guides), (2)
whitelist Retell's real IP blocks on their SIP provider's side:
`18.98.16.120/30`, `3.42.144.0/23`, `153.57.128.0/18`, `143.223.88.0/21`,
`161.115.160.0/19`. These are surfaced in the router's Swagger docs (see
app/routers/agents.py) so a Platform X developer reading only our docs
knows about them without needing to separately find Retell's own docs.

`create_phone_call()` — POST /calls/outbound support (Phase 1 API surface
table: "Trigger an outbound call — from_number, to_number, which agent to
use, and dynamic variables"). Confirmed both against vendor-docs/Retell.md's
already-researched "Outbound calls" section AND a fresh live WebFetch of
Retell's own current docs (docs.retellai.com/api-references/create-phone-call)
in this session, which agree exactly:
`POST https://api.retellai.com/v2/create-phone-call` — note the `/v2/`
prefix, genuinely different from create-agent/create-phone-number/
import-phone-number above, which have no version prefix; this was
specifically re-checked, not assumed to still be true. Same
`Authorization: Bearer <RETELL_API_KEY>` auth; success is 201. Required:
`from_number` (E.164, must already be owned/imported in Retell — enforced on
OUR side too, see app/models/call.py's module docstring for why this is a
tenancy boundary, not just a Retell-side rule), `to_number` (E.164; if
`from_number` is a Retell-purchased number specifically, only US destination
numbers are supported — surfaced to Platform X in this endpoint's Swagger
docs). This adapter always sends `override_agent_id` (Retell's real agent id,
i.e. Agent.vendor_ref) rather than binding via a separate mechanism — Retell
runs that agent for this one call without permanently rebinding
`from_number`. Optional fields confirmed real and sent when provided:
`retell_llm_dynamic_variables` (confirmed via the live WebFetch check to be
`type: object` with `additionalProperties: type: string` — a flat dict of
string-to-string pairs only, matching vendor-docs/Retell.md's "Custom
prompts & dynamic variables" section's existing note that values must be
pre-stringified), `metadata` (not currently exposed by our own request
model — nothing in this task's scope needs it yet, per the "no unnecessary
fields" rule; can be added later without breaking any caller).
`override_agent_version`, `agent_override`, `custom_sip_headers`,
`ignore_e164_validation` are real, documented, optional Retell fields but are
also not exposed on our own request model yet for the same reason — none of
them are in this task's brief, and Retell supplies its own defaults for
anything omitted. Response fields confirmed real: `call_id`, `agent_id`,
`call_status` (starts as the literal string `"registered"`), plus
`agent_version`, `from_number`, `to_number`, `call_type`, `direction`, and
other `V2CallBase` fields this adapter does not need and does not surface.
No documented rate-limit numbers (a generic "Account rate limited" error is
the only documented behavior) — treated as a normal vendor rejection, same
upstream_failed pattern as every other adapter function above, nothing
special needed.

`voicemail_option` — now sent when the caller supplies
`CreateOutboundCallRequest.voicemail_detection` (see app/models/call.py's
module docstring, "voicemail_detection" section, for the full sourced field
shape: `action` one of `static_text`/`prompt`/`hangup`/`bridge_transfer`,
`text` required only for `static_text`, optional `detection_prompt`).
Confirmed real and optional via the same live WebFetch of
docs.retellai.com/api-references/create-phone-call this session that
confirmed `retell_llm_dynamic_variables` above — omitted from the request
body entirely (never sent as `null`) when the caller didn't set
`voicemail_detection`, matching the same "send only what's actually
configured" discipline `retell_llm_dynamic_variables` already follows just
above.

`stop_call()` — Retell's real `POST /v2/stop-call/{call_id}` ("Stop an
ongoing call"), confirmed via live WebFetch of Retell's own current docs
(docs.retellai.com/api-references/stop-call) in this same session. Not
called from any router — there is no cancel/stop endpoint in this task's
scope. Exists purely so a real manual live-verification test call can be
stopped immediately after confirming registration, to minimize real billed
call duration, mirroring how delete_phone_number() above exists only for
live-test cleanup. Success is 204 with no body; a 422 ("Cannot find requested
call") is treated as already-stopped/not-found, not an error, mirroring
delete_phone_number's soft-fail-on-404 pattern — the end state ("call is not
running") is the same either way.

`fetch_recording_bytes()` — post-call re-hosting support (Phase 1 item 15,
"re-host, don't pass through": app/routers/webhooks.py's `/post-call`
handler). Retell's real `call_ended`/`call_analyzed` webhook payloads carry
`call.recording_url` — a real, vendor-hosted (S3) URL, confirmed via a live
WebFetch of Retell's own current docs
(docs.retellai.com/features/webhook-overview.md) in this session, and
cross-checked against eCareVoiceAI's own real, working
`webhooks/retell.py:_process_post_call_payload` (which reads
`call.get("recording_url")` directly off the same webhook payload — not a
separate GET call). This function is the exact same "fetch the vendor's URL
server-side, never hand it to the caller directly" pattern as
fetch_preview_audio() above, reused rather than duplicated logic-for-logic —
the only difference is the caller (the post-call webhook handler, re-hosting
to our own S3 bucket) rather than a live proxy response. No content-type
normalization here unlike fetch_preview_audio: Retell's `recording_url` is
consistently a WAV file per their own docs' example filenames and
eCareVoiceAI's working code has never needed to correct its type, so this
function returns the raw upstream Content-Type header (falling back to
`audio/wav` if absent) rather than asserting a normalization Retell hasn't
been observed to need.

`webhook_url` on retell_agent_adapter.py's `create_agent()`/
`create_retell_llm_agent()` — added for post-call re-hosting (Phase 1
item 15, see app/routers/webhooks.py's `/post-call` handler docstring for
the full confirmed mechanism). Real, documented Retell field, confirmed via
live WebFetch of docs.retellai.com/features/register-webhook.md this
session: set at the AGENT level (not per-phone-number, unlike the inbound
webhook below) since post-call events are tied to the call's agent. The
router now sends VoiceAI's own `{settings.BASE_URL}/webhooks/retell/
post-call` here on every agent creation, so Retell has somewhere real to
send call_started/call_ended/call_analyzed events for that agent's calls.

`inbound_webhook_url` on `create_phone_number()`/`import_phone_number()` —
added for inbound dynamic-variable injection (Phase 1 plan doc's `POST
/calls/:id/variables` idea, built as a synchronous vendor-webhook flow
instead — see app/routers/webhooks.py's module docstring for the full,
confirmed real mechanism: Retell POSTs `call_inbound` to this URL and waits
up to 10s for our response before answering). This is a real, documented
Retell field on both endpoints (eCareVoiceAI's own phone-number bodies
already reference it, and the router/model docstrings for both endpoints
above already listed it among "not currently exposed" fields before this
task); the router now sends VoiceAI's own `{settings.BASE_URL}/webhooks/
retell/inbound` here on every purchase/import so Retell has somewhere real to
send the webhook. Without this, the new inbound webhook endpoint this
module's docstring above describes would never actually receive a call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.errors import CODE_NOT_FOUND, CODE_UPSTREAM_FAILED, AppError
from app.models.call import VoicemailDetectionConfig

logger = logging.getLogger("app.retell_adapter")

VENDOR_NAME = "retell"


async def list_voices(settings: Settings) -> list[dict[str, Any]]:
    """Call Retell's real `GET /list-voices`.

    Returns the raw list of voice objects (dicts) exactly as Retell sends
    them — the router maps these onto our own `VoicePublic` shape, this
    adapter's only job is talking to Retell. Raises `AppError(code=
    "upstream_failed")` on any network error, timeout, or non-2xx response,
    same pattern as create_agent above: never leak Retell's raw exception
    text to the caller, never crash the server.

    No query params — Retell's own endpoint has none; pagination/filtering
    for GET /voices happens entirely on our side after fetching the full
    list (see app/routers/voices.py), since ~300 small JSON objects is not a
    real performance concern.
    """
    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.get("/list-voices")
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell list-voices request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to list voices. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell list-voices returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to list voices.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data = resp.json()
    if not isinstance(data, list):
        logger.error(
            "Retell list-voices succeeded but response was not a JSON array",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while listing voices.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return data


async def get_voice(settings: Settings, *, voice_id: str) -> dict[str, Any]:
    """Call Retell's real `GET /get-voice/{voice_id}`.

    Confirmed sourced (vendor-docs/Retell.md, "Discovering available
    voices") and re-verified live in this session: same per-voice schema as
    one entry of `/list-voices`, 200 for a real voice_id, 404 for an unknown
    one. Used by the `GET /voices/{voice_id}/preview` proxy to look up the
    real (Retell-hosted) `preview_audio_url` server-side before fetching and
    re-streaming the audio bytes — Platform X never sees this URL directly.

    Raises `AppError(code="not_found")` for an unknown voice_id (Retell's
    404), `AppError(code="upstream_failed")` for any other network error,
    timeout, or non-2xx/non-404 response — same never-leak-raw-response
    pattern as create_agent/list_voices above.
    """
    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.get(f"/get-voice/{voice_id}")
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell get-voice request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to look up this voice. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code == 404:
        raise AppError(
            code=CODE_NOT_FOUND,
            message="No voice exists with that voice_id.",
            status_code=404,
            field="voice_id",
            log_extra={"vendor": VENDOR_NAME},
        )

    if resp.status_code >= 400:
        logger.warning(
            "Retell get-voice returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to look up this voice.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data = resp.json()
    if not isinstance(data, dict):
        logger.error(
            "Retell get-voice succeeded but response was not a JSON object",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while looking up this "
            "voice.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return data


async def fetch_preview_audio(settings: Settings, *, audio_url: str) -> tuple[bytes, str]:
    """Fetch the actual audio bytes from Retell's real (vendor-hosted) audio
    URL, server-side, so we can re-stream them back to the caller under our
    own domain instead of ever handing out the vendor's URL directly.

    This is the same "download and re-store under our own domain" pattern
    vendor-docs/Full-System-Architecture.html already establishes for
    call recordings/transcripts ("We download and re-store the
    recording/transcript on our own domain... This is the second
    re-branding boundary"), applied here to voice preview audio instead of
    persisting it — since preview audio is small, static per voice_id, and
    fetched on demand, there is no need for our own storage/CDN, just a
    live pass-through fetch on every request (mirrors GET /voices' own
    read-through-not-cached decision, for the same reason: Retell's data,
    no local persistence needed yet).

    Returns (audio_bytes, content_type). Raises `AppError(code=
    "upstream_failed")` on any network error, timeout, or non-2xx response
    from the vendor's audio host — never leaks that raw URL or response to
    the caller.

    Content-Type note, confirmed live: Retell's own S3 bucket
    (retell-utils-public.s3.us-west-2.amazonaws.com) serves these `.mp3`
    files with a generic `Content-Type: binary/octet-stream`, not
    `audio/mpeg` — a real, confirmed quirk of their bucket config, not
    something to blindly pass through. Every real preview URL observed is a
    `.mp3` file (confirmed via a live fetch + `file` inspection: genuine
    MPEG layer III audio), so we normalise the content type to `audio/mpeg`
    ourselves rather than propagate S3's generic byte-stream type.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(audio_url)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell preview audio fetch failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to fetch preview audio. "
            "Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell preview audio fetch returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request for preview audio.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )

    upstream_content_type = resp.headers.get("content-type", "")
    # Retell's S3 bucket serves .mp3 previews as generic binary/octet-stream
    # (confirmed live) rather than a real audio type — normalise to
    # audio/mpeg unless the vendor ever sends back a genuine audio/* type.
    content_type = (
        upstream_content_type if upstream_content_type.startswith("audio/") else "audio/mpeg"
    )
    return resp.content, content_type


async def fetch_recording_bytes(settings: Settings, *, recording_url: str) -> tuple[bytes, str]:
    """Fetch a finished call's recording bytes from Retell's real
    `recording_url` (a vendor-hosted S3 URL, carried on the `call_ended`/
    `call_analyzed` webhook payload — see this module's docstring for the
    sourced evidence trail). Server-side fetch, never a redirect, same
    reasoning as fetch_preview_audio() above — the caller (the post-call
    webhook handler) re-uploads these bytes to our own S3 bucket so Platform
    X is never handed Retell's raw URL.

    Returns (audio_bytes, content_type). Raises `AppError(code=
    "upstream_failed")` on any network error, timeout, or non-2xx response —
    this is a genuine vendor-call failure (Retell's own recording host is
    unreachable/rejecting), not a storage failure, so it uses the same code
    as every other retell_adapter function, not storage.py's
    "storage_failed" (that code is reserved for OUR OWN S3 failing, a
    separate failure class — see errors.py's CODE_STORAGE_FAILED docstring).
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(recording_url)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell recording fetch failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to fetch the call recording. "
            "Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell recording fetch returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request for the call recording.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )

    content_type = resp.headers.get("content-type") or "audio/wav"
    return resp.content, content_type


async def fetch_transcript_text(settings: Settings, *, transcript: str) -> str:
    """Not a vendor HTTP fetch at all — Retell's real webhook payload
    already carries the full plain-text transcript inline as `call.
    transcript` (confirmed via the live WebFetch of Retell's webhook-overview
    docs and eCareVoiceAI's working code, both agreeing: `transcript` is a
    string field directly on the webhook payload, unlike `recording_url`
    which only carries a link Retell hosts elsewhere). This function exists
    only to give the post-call webhook handler one consistent "fetch the
    text to re-host" call shape alongside fetch_recording_bytes() above,
    rather than the router reading `call.transcript` directly and treating
    transcript re-hosting as a special case — kept intentionally trivial, no
    network call, `settings` unused (accepted only for call-shape
    consistency with the other fetch_* functions, in case a future Retell
    API version moves this behind a real URL the way recording_url already
    is).
    """
    del settings
    return transcript


class RetellCreatePhoneNumberResult:
    """Successful `/create-phone-number` outcome — only the fields
    PhoneNumberPublic actually needs (see app/models/phone_number.py); Retell
    returns more (phone_number_pretty, sip_outbound_trunk_config, ...) that
    are deliberately not surfaced past this adapter.
    """

    def __init__(self, *, phone_number: str, area_code: int | None, nickname: str | None) -> None:
        self.phone_number = phone_number
        self.area_code = area_code
        self.nickname = nickname


async def create_phone_number(
    settings: Settings,
    *,
    retell_agent_id: str,
    area_code: int | None,
    toll_free: bool | None,
    country_code: str | None,
    phone_number: str | None,
    nickname: str | None,
    inbound_webhook_url: str | None = None,
) -> RetellCreatePhoneNumberResult:
    """Call Retell's real `POST /create-phone-number`, binding the new number
    to `retell_agent_id` (Retell's own agent id, i.e. Agent.vendor_ref — NOT
    our own Mongo agent id) for inbound calls in the same request.

    `inbound_webhook_url` is Retell's real, documented per-phone-number field
    (confirmed in eCareVoiceAI's working create/update-phone-number bodies
    and Retell's own docs) — the URL Retell synchronously POSTs to and waits
    on (up to 10s, 3 retries on non-2xx) the moment a call rings in to this
    number, before answering. Without it set, Retell has nowhere to send the
    inbound-call webhook and answers with no way for us to inject
    dynamic_variables. The router passes our own `POST
    /webhooks/retell/inbound` URL (see app/routers/webhooks.py) here,
    built from `settings.BASE_URL` — never omitted for a real deployment,
    optional here (defaults to None, Retell's own default is presumably "no
    inbound webhook configured") only so existing call sites/tests that
    don't yet care about inbound calls aren't forced to pass it.

    Raises `AppError(code="upstream_failed")` on any network error, timeout,
    or non-2xx response — same never-leak-raw-response pattern as
    create_agent/list_voices/get_voice above. Retell has no documented
    failure mode specifically for "area code/number unavailable"; any 4xx is
    treated as a generic vendor rejection.
    """
    body: dict[str, Any] = {
        "inbound_agents": [{"agent_id": retell_agent_id, "weight": 1.0}],
    }
    if area_code is not None:
        body["area_code"] = area_code
    if toll_free is not None:
        body["toll_free"] = toll_free
    if country_code is not None:
        body["country_code"] = country_code
    if phone_number is not None:
        body["phone_number"] = phone_number
    if nickname is not None:
        body["nickname"] = nickname
    if inbound_webhook_url is not None:
        body["inbound_webhook_url"] = inbound_webhook_url

    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.post("/create-phone-number", json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell create-phone-number request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to buy a phone number. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell create-phone-number returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the phone number purchase request.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data = resp.json()
    returned_number = data.get("phone_number")
    if not returned_number:
        logger.error(
            "Retell create-phone-number succeeded but response is missing phone_number",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while buying a "
            "phone number.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return RetellCreatePhoneNumberResult(
        phone_number=returned_number,
        area_code=data.get("area_code"),
        nickname=data.get("nickname"),
    )


async def import_phone_number(
    settings: Settings,
    *,
    phone_number: str,
    termination_uri: str,
    retell_agent_id: str,
    sip_trunk_auth_username: str | None,
    sip_trunk_auth_password: str | None,
    ignore_e164_validation: bool | None,
    transport: str | None,
    nickname: str | None,
    inbound_webhook_url: str | None = None,
) -> RetellCreatePhoneNumberResult:
    """Call Retell's real `POST /import-phone-number` — Option 2 telephony,
    "bring your own SIP trunk" (a genuinely separate endpoint from
    `/create-phone-number`, not the same call with a flag; see this module's
    docstring for the full sourced evidence trail).

    Binds to `retell_agent_id` (Retell's own agent id, i.e. Agent.vendor_ref)
    in the same import call via `inbound_agents`, same mechanism as
    create_phone_number above.

    `sip_trunk_auth_username`/`sip_trunk_auth_password` are forwarded to
    Retell in the request body only — never included in any log line here
    (the warning logs below only ever carry `vendor`/`upstream_status`/
    `upstream_body`/`error_class`) and never returned to the caller. The
    router that calls this function must also never persist them to our own
    `PhoneNumbers` collection — see app/models/phone_number.py's
    ImportPhoneNumberRequest docstring for the full reasoning.

    Raises `AppError(code="upstream_failed")` on any network error, timeout,
    or non-2xx response — same never-leak-raw-response pattern as every
    other adapter function above. A real SIP trunk at a third-party provider
    is a genuine prerequisite Retell cannot verify without actually placing
    a call through it, so a "rejected" response here may reflect an
    unreachable/misconfigured trunk on Platform X's side rather than a
    malformed request — Retell's own error message (never leaked verbatim to
    the caller, but logged for our own debugging) is the only signal.
    """
    body: dict[str, Any] = {
        "phone_number": phone_number,
        "termination_uri": termination_uri,
        "inbound_agents": [{"agent_id": retell_agent_id, "weight": 1.0}],
    }
    if sip_trunk_auth_username is not None:
        body["sip_trunk_auth_username"] = sip_trunk_auth_username
    if sip_trunk_auth_password is not None:
        body["sip_trunk_auth_password"] = sip_trunk_auth_password
    if ignore_e164_validation is not None:
        body["ignore_e164_validation"] = ignore_e164_validation
    if transport is not None:
        body["transport"] = transport
    if nickname is not None:
        body["nickname"] = nickname
    if inbound_webhook_url is not None:
        body["inbound_webhook_url"] = inbound_webhook_url

    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.post("/import-phone-number", json=body)
    except httpx.HTTPError as exc:
        # Never log `body` here — it may carry sip_trunk_auth_username/
        # sip_trunk_auth_password. Only the exception class is logged.
        logger.warning(
            "Retell import-phone-number request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to import the phone number. "
            "Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        # upstream_body is Retell's own response text, not our request body —
        # it cannot contain the SIP credentials we sent, only what Retell
        # sends back (typically a validation/rejection message).
        logger.warning(
            "Retell import-phone-number returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the phone number import request. Confirm the "
            "SIP trunk is reachable and Retell's IP ranges are whitelisted on your "
            "provider's side.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data = resp.json()
    returned_number = data.get("phone_number")
    if not returned_number:
        logger.error(
            "Retell import-phone-number succeeded but response is missing phone_number",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while importing the "
            "phone number.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return RetellCreatePhoneNumberResult(
        phone_number=returned_number,
        area_code=data.get("area_code"),
        nickname=data.get("nickname"),
    )


async def delete_phone_number(settings: Settings, *, phone_number: str) -> None:
    """Call Retell's real `DELETE /delete-phone-number/{phone_number}`.

    Not called from any router in this task's scope — no release/delete
    endpoint exists yet. Exists so a real number purchased during manual live
    verification can be released afterward instead of sitting billed and
    unused in the configured Retell account (see eCareVoiceAI's own
    `delete_phone_number()`, which this mirrors).

    Raises `AppError(code="upstream_failed")` on any network error or
    non-2xx/non-404 response. A 404 (already gone) is treated as success,
    not an error — mirrors eCareVoiceAI's own soft-fail-on-404 delete
    behavior, since the end state ("number no longer ours") is the same
    either way.
    """
    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.delete(f"/delete-phone-number/{phone_number}")
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell delete-phone-number request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to release the phone number.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400 and resp.status_code != 404:
        logger.warning(
            "Retell delete-phone-number returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to release the phone number.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )


class RetellCreatePhoneCallResult:
    """Successful `/v2/create-phone-call` outcome — only the fields
    CallPublic actually needs. Retell's `call_id` is stored as an
    internal-only vendor_ref on our CallInDB, never surfaced to Platform X —
    see app/models/call.py's module docstring for the full reasoning.
    """

    def __init__(self, *, call_id: str, agent_id: str, call_status: str) -> None:
        self.call_id = call_id
        self.agent_id = agent_id
        self.call_status = call_status


async def create_phone_call(
    settings: Settings,
    *,
    from_number: str,
    to_number: str,
    retell_agent_id: str,
    dynamic_variables: dict[str, str],
    voicemail_detection: VoicemailDetectionConfig | None = None,
) -> RetellCreatePhoneCallResult:
    """Call Retell's real `POST /v2/create-phone-call` — note the `/v2/`
    prefix, confirmed different from create-agent/create-phone-number/
    import-phone-number above (see this module's docstring for the sourced
    evidence trail).

    `retell_agent_id` is Retell's own agent id (Agent.vendor_ref, NOT our own
    Mongo agent id) and is always sent as `override_agent_id` — this runs
    that agent for this one call without permanently rebinding
    `from_number`'s own inbound agent binding.

    `voicemail_option`, sent only when `voicemail_detection` is not None —
    see this module's docstring, "voicemail_option" section, for the full
    sourced shape. Built here (not passed through as a raw dict) so this
    adapter stays the one place that knows Retell's exact wire field names,
    same discipline as every other adapter function.

    Raises `AppError(code="upstream_failed")` on any network error, timeout,
    or non-2xx response — same never-leak-raw-response pattern as every
    other adapter function above. This genuinely dials a real phone once it
    succeeds; the caller (app/routers/calls.py) is responsible for all
    tenancy/ownership checks (from_number belongs to the caller, agent_id
    belongs to the caller and isn't status=failed) *before* calling this
    function — this adapter only knows how to talk to Retell, not which
    platform owns what.
    """
    body: dict[str, Any] = {
        "from_number": from_number,
        "to_number": to_number,
        "override_agent_id": retell_agent_id,
    }
    if dynamic_variables:
        body["retell_llm_dynamic_variables"] = dynamic_variables
    if voicemail_detection is not None:
        voicemail_option: dict[str, Any] = {"action": voicemail_detection.action.value}
        if voicemail_detection.text is not None:
            voicemail_option["text"] = voicemail_detection.text
        if voicemail_detection.detection_prompt is not None:
            voicemail_option["detection_prompt"] = voicemail_detection.detection_prompt
        body["voicemail_option"] = voicemail_option

    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.post("/v2/create-phone-call", json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell create-phone-call request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to place the call. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell create-phone-call returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the outbound call request.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data = resp.json()
    call_id = data.get("call_id")
    agent_id = data.get("agent_id")
    call_status = data.get("call_status")
    if not call_id or not agent_id or not call_status:
        logger.error(
            "Retell create-phone-call succeeded but response is missing required fields",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while placing the call.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return RetellCreatePhoneCallResult(call_id=call_id, agent_id=agent_id, call_status=call_status)


async def stop_call(settings: Settings, *, call_id: str) -> None:
    """Call Retell's real `POST /v2/stop-call/{call_id}` ("Stop an ongoing
    call").

    Not called from any router in this task's scope — no cancel/stop
    endpoint exists yet. Exists purely for real manual live-verification
    cleanup, so a just-triggered test call can be stopped immediately after
    confirming registration, minimizing real billed call duration (mirrors
    delete_phone_number()'s live-test-only purpose above).

    Raises `AppError(code="upstream_failed")` on any network error or
    non-2xx/non-422 response. A 422 ("Cannot find requested call", per
    Retell's documented error for this endpoint) is treated as
    already-stopped/not-found, not an error — same soft-fail-on-already-gone
    reasoning as delete_phone_number's 404 handling above.
    """
    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.post(f"/v2/stop-call/{call_id}")
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell stop-call request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to stop the call.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400 and resp.status_code != 422:
        logger.warning(
            "Retell stop-call returned an error",
            extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
            },
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to stop the call.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )
