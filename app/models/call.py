"""Call — an outbound phone call Platform X triggers through us, per
vendor-docs/White-Label-Launch-Plan.html's Phase 1 API surface table
("Trigger an outbound call — from_number, to_number, which agent to use, and
dynamic variables").

Backed by Retell's real `POST /v2/create-phone-call` (see
app/services/retell_adapter.py's create_phone_call()), but that's an
implementation detail Platform X never sees — no vendor name, no Retell field
names, no vendor_ref on the public shape, same principle as AgentPublic.

**`from_number` ownership, the core security/correctness boundary of this
endpoint:** `from_number` must be one of Platform X's own numbers, looked up
in our `PhoneNumbers` collection scoped to `platform_id`. Retell itself has
no concept of OUR tenancy — it only knows a phone number is "owned/imported
in Retell" at all, not which of OUR platforms it belongs to. If we forwarded
whatever `from_number` a caller supplied straight to Retell without checking
it against our own `PhoneNumbers` records, Platform A could trigger a call
that appears to originate from a number actually provisioned (and billed) for
Platform B — a real cross-tenant boundary violation, not just a nice-to-have
validation. A number that doesn't resolve to one of the caller's own
`PhoneNumbers` records is a meaningful validation error (422/`invalid_request`
via CODE_VALIDATION), not a 404 — the E.164 value itself might be perfectly
well-formed and even a real Retell-owned number; it's just not one *this*
platform owns with us.

**`agent_id` is OUR OWN agent id** (from POST /agents' response), looked up
tenancy-scoped exactly like the phone-number endpoints (404 if missing/
cross-platform, 422 if `status == "failed"` — no real vendor-side agent to
call from). Its `vendor_ref` (Retell's real agent_id) is translated into
Retell's real `override_agent_id` field when placing the call. The request's
`agent_id` is deliberately NOT required to match the `agent_id` currently
bound to `from_number` in `PhoneNumbers` — Retell's own `override_agent_id`
field exists precisely to let a call use a different agent than whichever one
is bound to the originating number, without permanently rebinding that
number. Forcing them to match would take away a real, intentional Retell
capability for no benefit.

**`dynamic_variables` is our own name for Retell's real
`retell_llm_dynamic_variables`** — confirmed (WebFetch of Retell's live docs,
this session) to be `type: object` with `additionalProperties: type: string`,
i.e. a flat dict of string keys to *string* values only, no nested objects,
numbers, or booleans. We validate this at our own boundary (Pydantic
`dict[str, str]`) and reject a request with any non-string value as a 422
before ever calling the vendor, rather than silently stringifying it —
silently coercing `"call_count": 3` into `"call_count": "3"` would hide a
caller's likely mistake (e.g. accidentally passing a JSON number) behind a
successful-looking response, and vendor-docs/Retell.md's own dynamic-
variables section already documents that callers are expected to stringify
these values themselves ("numbers/booleans must be stringified... and cast
back explicitly where needed") — so rejecting a wrongly-typed value is
enforcing a constraint Platform X is already supposed to know about, not
inventing a new one.

**`vendor_ref` design decision, reasoned through explicitly (like Agent's,
not PhoneNumber's):** Retell's real `call_id` is a genuinely separate, opaque
value that Platform X could not otherwise construct or know in advance — this
is the AgentPublic pattern (a real internal correlation id), not the
PhoneNumberPublic pattern (where the E.164 number itself already was the
vendor's own identifier, so no separate id was needed). Platform X does need
*some* stable identifier back to reference this call later (e.g. a future
`GET /calls/{id}`), but per the "never leak vendor_ref" rule, that identifier
is OUR OWN Mongo `_id` (`CallPublic.id`), never Retell's raw `call_id`
string — `vendor_ref` stays internal-only on `CallInDB`, used only for
webhook correlation and support debugging against Retell's own dashboard.

**Persist-on-vendor-failure, decided the same way as POST /agents (not the
phone-number endpoints' no-persist pattern) — re-justified for this specific
case, not copied blindly:** Platform X's submitted from_number/to_number/
agent_id/dynamic_variables are valid and theirs the moment we've validated
ownership and accepted the request; a transient vendor outage or rate limit
on our side shouldn't force them to blindly resubmit the same call trigger.
We persist a `Calls` document regardless of whether the Retell call
succeeds: success -> `status="registered"` (mirroring Retell's own real
`call_status` starting value) with a real `vendor_ref`; vendor failure ->
`status="failed"`, `vendor_ref=None`, and the original AppError is still
raised (502/`upstream_failed`) so the caller sees the real failure — the
persisted "failed" record is a side effect for a future retry/audit trail,
not a silent success. This differs from the phone-number endpoints
(no-persist-on-failure) because a failed number *purchase* has no real
resource to keep a placeholder for (nothing was bought), whereas here the
call *attempt itself* is the real, meaningful event worth recording — Platform
X asked us to dial a specific number with a specific agent at a specific
time, and that fact is true and worth keeping a record of regardless of
whether Retell's own call placement succeeded.

**`status` mirrors Retell's own real `call_status` values where meaningful**
(`registered` on success, confirmed the literal starting value Retell
returns), plus our own `failed` for a vendor-call rejection that never
produced a real Retell call at all (no Retell call_status value describes
"we never got as far as talking to the vendor" so this is our own state, not
a copy of one of theirs). Later Retell-side states are picked up by the
post-call webhook (see below) — this endpoint's job is only to *trigger* the
call and record the outcome of that trigger attempt.

**`voicemail_detection` — VoiceAI's own name for Retell's real
`voicemail_option` field on `POST /v2/create-phone-call`** (confirmed via a
live WebFetch of `docs.retellai.com/api-references/create-phone-call` this
session — genuinely per-CALL, not per-agent: it sits at the same level as
`from_number`/`to_number`/`override_agent_id` on the create-phone-call
request body, with no equivalent field anywhere on `create-agent`/
`create-retell-llm`, confirmed by checking both of those schemas too before
committing to this placement). "If this option is set, the call will try to
detect voicemail in the first 3 minutes of the call."

`VoicemailAction` mirrors Retell's own real, documented enum values exactly
(`static_text`, `prompt`, `hangup`, `bridge_transfer`) — kept unrenamed
because, like `PronunciationEntry`'s `word`/`pronunciation` field names
(app/models/agent.py), these are generic, self-explanatory action words that
don't read as vendor-specific ("hang up," "speak static text," "transfer,"
"let the AI improvise a message" are meaningful on their own, not Retell
jargon) — the same "generic enough to not read as vendor-specific" exception
already established there, not a new one invented for this field.

`text` is required (and non-empty) ONLY when `action == 'static_text'` — the
real sibling field Retell's own schema requires alongside that one action
value specifically (confirmed via the same live WebFetch: `VoicemailAction
StaticText`'s own `required` array includes `text`, described as "The text
to be spoken when the call is detected to be in voicemail"), and rejected
(422) for every other action value. This is the exact same
required-for-one-enum-value, rejected-for-every-other-value validator shape
already established by `StructuredDataFieldDefinition.choices` (required
only when `type == 'enum'`) in app/models/agent.py — reused deliberately,
not reinvented, since it's the same shape of problem.

`detection_prompt` — Retell's real, optional, nullable field, confirmed
`max_length` 2000 in the same WebFetch ("Optionally describe what should be
treated as voicemail. Leave as null to use the default definition.") — an
edge-case override for callers whose own definition of "this is voicemail"
differs from Retell's default detection, not required for the feature to
work at all.

**Not restricted to `response_engine='builtin'`, and deliberately not
checked against the owning agent's mode at all** — unlike `welcome_message`/
`transfer_number`/`states`/`custom_tools` (app/models/agent.py), which are
genuinely restricted because their underlying vendor mechanism lives on a
`custom_llm`-mode-only LLM object that simply doesn't exist under `custom`
mode, `voicemail_option` lives entirely on the CALL-TRIGGER request
(`create-phone-call`), not on the agent/LLM object at all — it has no
dependency on `response_engine` mode to begin with, the same "agent-object
field, not LLM-object field" reasoning `agent_name`'s own placement note
(app/models/agent.py) already worked through for a different field. Retell's
own docs impose no restriction here, and this codebase's own established
principle — see `structured_data_fields`'/`agent_name`'s own explicit
warnings against this exact mistake — is to never invent a restriction the
vendor doesn't have "for consistency" with an unrelated field that's
restricted for a genuinely different reason. Whether a `custom`-mode agent
can hold a real enough conversation for voicemail detection to be
meaningfully USEFUL today is a separate, honest limitation (this codebase's
own documented gap: a `custom` mode call can't yet hold a real conversation
at all) — but that's a reason a caller might choose not to combine the two,
not a reason for this request-validation layer to forbid the combination.

**Post-call fields, added for `POST /webhooks/retell/post-call` (see
app/routers/webhooks.py's post-call handler docstring for the full,
sourced webhook event/payload research), NOT built by this endpoint above —
`POST /calls/outbound` only ever writes the fields already described above;
everything below this point is written later, by the post-call webhook,
once a call actually finishes.**

`status=COMPLETED` — added to CallStatus for the post-call webhook's
`call_ended`/`call_analyzed` events (confirmed via a live WebFetch of
Retell's own webhook-overview docs and eCareVoiceAI's own working
`_process_post_call_payload`, both agreeing these are the two "finished"
events). Retell's own `call_status` values (`registered`, `not_connected`,
`ongoing`, `ended`, `error`) are not copied verbatim onto our own enum —
same reasoning as `registered`/`failed` above: Platform X only needs to know
"is this call done and did it complete," not Retell's own internal state
machine.

`disconnection_reason` (confirmed real, one of 37 documented values
including `user_hangup`, `agent_hangup`, `voicemail_reached`) — a PRIOR
session note on this field said it was deliberately NOT stored ("no
caller-facing feature in this task's scope reads it yet, per the 'no
unnecessary fields' rule"). **That reasoning no longer holds and this field
is now stored** — the voicemail-detection feature (see
`CreateOutboundCallRequest.voicemail_detection` above) is exactly the real
caller-facing use the old note was waiting for: Platform X needs to know
*why* a call ended, specifically whether it ended because voicemail was
reached, not just *that* it ended. This is not a silent reversal of the old
rule — the rule itself ("don't add a field nothing real reads") is still
correct and still applied everywhere else in this module; this one field
simply now has a real reader. Stored as a plain `str | None` (Retell's own
raw enum string, e.g. `"voicemail_reached"`), not re-modeled as our own enum
the way `CallStatus`/`CallDirection` deliberately are — with 37 real values
and growing, and no caller-facing behavior in this task's scope that branches
on any value OTHER than `voicemail_reached` (which `in_voicemail` already
signals more directly), inventing and maintaining a parallel 37-value enum
here would be effort spent with no real payoff; Platform X gets the vendor's
own honest string value and can react to values this codebase hasn't
special-cased without needing a VoiceAI code change first.

`in_voicemail` (`bool | None`) — Retell's real `call_analysis.in_voicemail`
field (confirmed via a live WebFetch of Retell's own current get-call API
reference this session), the actual detection RESULT — was the voicemail
option's detector triggered on this call. Same "populated only once
call_analyzed/call_ended provides it, None until then" convention as
`summary`/`sentiment`/`extracted_data` above: `None` before analysis has
run, and Retell's own confirmed example payload shows a real, explicit
`false` (not an absent/omitted field) for a call a human answered — so once
analysis genuinely has run, `False` is a real, meaningful "confirmed not
voicemail" answer, never confused with "we don't know yet" (`None`).
Deliberately NOT gated on `voicemail_detection` having been set on the
triggering call — Retell's own detection can in principle still populate
this field even without the option explicitly configured (the option only
documented as controlling the ACTION taken, not whether detection itself
ever runs), so this codebase passes through whatever Retell actually sends
rather than assuming a caller-side precondition the vendor doesn't itself
enforce.

**Inbound-call record creation, and why there's no intermediate status for
it**: when the post-call webhook's `handle_post_call` (app/routers/
webhooks.py) sees a `call_id` with no existing Calls document, it creates
one right there rather than dropping the event (see CallDirection's own
comment above for the full resolution path). There is deliberately no
`REGISTERED`-equivalent phase for this new record the way there is for
`POST /calls/outbound`'s own created-before-the-call-happens record: by the
time a `call_ended`/`call_analyzed` webhook exists at all, the call is
already over — we only ever learn about a true inbound call in retrospect,
never while it's in progress. So the record is created with
`status=COMPLETED` directly (the honest state at the moment of creation, not
a placeholder immediately overwritten), and the SAME `_process_finished_call`
background flow every outbound call already goes through runs unchanged
right after — it re-sets `COMPLETED` again via
`call_repo.update_post_call_outcome` (a harmless, idempotent no-op repeat,
same as any other webhook re-delivery) while doing the real work: re-hosting,
summary/sentiment/extracted_data, and the call-completed notification. This
was the deciding factor over inventing a separate "just learned about this"
status: it would exist for a single line of code before being overwritten,
duplicating status-setting logic in two places for no real benefit.

`recording_url`/`transcript_url` — OUR OWN re-hosted, S3-backed serving
paths (`/calls/{id}/recording`, `/calls/{id}/transcript` — see
app/routers/calls.py), never Retell's raw recording_url/transcript fields.
This is the core "re-host, don't pass through" rule from the plan doc,
applied to the highest-stakes version of the vendor-URL-leak class this
project has now found and fixed twice already (GET /voices'
preview_audio_url, and Swagger documentation text) — real call recordings,
not voice previews. `None` until the post-call webhook has actually
finished re-hosting (see the partial-success design below) — a caller must
never be given a URL that 404s because re-hosting hasn't happened yet or
failed; `None` is the honest "not available (yet, or at all)" signal.

`summary`/`sentiment` — Retell's real `call_analysis.call_summary` /
`call_analysis.user_sentiment` fields (confirmed real, sourced from the same
webhook-overview WebFetch + eCareVoiceAI's working code, which reads these
exact nested paths). Only populated by `call_analyzed` (the event carrying
`call_analysis` at all, per Retell's own docs) — `call_ended` alone (fired
first, before analysis finishes) leaves these `None`, matching
eCareVoiceAI's own documented ordering (`call_ended` fires first with
analysis usually not ready; `call_analyzed` fires ~5s later with the full
extraction).

`extracted_data` — the real output of the structured-data-extraction
feature (see app/models/agent.py's `structured_data_fields` docstring for
the agent-level configuration that produces this). Populated only by
`call_analyzed` (same event that carries `call_analysis` at all, per the
`summary`/`sentiment` reasoning above) — `None` if the agent had no
`structured_data_fields` configured, or if analysis hasn't finished yet.
Keyed by each configured field's own `name` (e.g. `{"Caller Name": "Jane
Doe", "Call Outcome": "Appointment booked"}`) — VoiceAI's own vendor-neutral
name for the voice vendor's real `call_analysis.custom_analysis_data` field
(confirmed via the same live WebFetch as app/models/post_call.py's
`RetellCallAnalysis.custom_analysis_data`), never that field name itself,
per the plan doc's own stated rule ("if the vendor calls something
post_call_analysis_data, our API calls it something else").

`recording_rehost_failed` — the "partial success" state this task's
verification explicitly asked to be designed rather than left undefined:
Retell's own call-outcome fields (status/summary/sentiment/transcript) are
always written when a finished-call webhook arrives, regardless of whether
re-hosting the recording/transcript to our own S3 bucket succeeds — a real,
genuine S3 failure (e.g. today's empty AWS_S3_BUCKET placeholder) must never
prevent Platform X from learning a call actually completed, mirroring this
project's own persist-on-vendor-failure precedent (POST /agents, POST
/calls/outbound) of "the real, meaningful event is recorded regardless of a
downstream step's success." `True` means re-hosting was attempted and
failed (S3 unreachable/misconfigured) — `recording_url`/`transcript_url`
stay `None` in that case, this flag is what tells Platform X "this isn't
missing because nothing happened, it's missing because storage failed" as
opposed to the ordinary steady-state `False` meaning either "not attempted
yet" or "succeeded."
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CallStatus(StrEnum):
    REGISTERED = "registered"
    FAILED = "failed"
    COMPLETED = "completed"


# VoicemailAction mirrors the voice vendor's real `VoicemailAction` enum
# exactly — see this module's docstring, "voicemail_detection" section, for
# why these generic action words are kept unrenamed (the same exception
# already established for `PronunciationEntry`'s field names in
# app/models/agent.py). Kept as a plain `#` comment, not the class docstring
# below, since this note names the vendor and StrEnum class docstrings ARE
# rendered into Swagger's schema `description` — see the standards doc's
# "Swagger-visible text must never name the vendor" rule.
class VoicemailAction(StrEnum):
    """What to do once voicemail is detected on a call (within the first 3
    minutes). 'static_text' requires a `text` message alongside it; every
    other value must leave `text` unset.
    """

    STATIC_TEXT = "static_text"
    PROMPT = "prompt"
    HANGUP = "hangup"
    BRIDGE_TRANSFER = "bridge_transfer"


# VoicemailDetectionConfig is VoiceAI's own name for the voice vendor's real
# `voicemail_option` object on its real create-phone-call API — see this
# module's docstring, "voicemail_detection" section (a `#` comment here, not
# the class docstring below, for the same Swagger-vendor-name reason as
# VoicemailAction's comment above) for the full sourced reasoning: confirmed
# shape, placement decision, and why it's never restricted by the owning
# agent's response_engine mode.
class VoicemailDetectionConfig(BaseModel):
    """If set, try to detect voicemail in the first 3 minutes of the call
    and take the configured action once detected.

    `text` is required (and non-empty) ONLY when `action == 'static_text'`,
    rejected for every other action value — the same
    required-for-one-enum-value validator shape already established by
    `StructuredDataFieldDefinition.choices` (app/models/agent.py), reused
    here rather than reinvented.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "action": "static_text",
                "text": "Sorry we missed you. Please call us back at your convenience.",
                "detection_prompt": "Treat a generic carrier greeting (e.g. 'The person you "
                "are trying to reach is unavailable') as voicemail even without a beep tone.",
            }
        }
    )

    action: Annotated[
        VoicemailAction,
        Field(
            description="What to do once voicemail is detected (within the first 3 minutes "
            "of the call). 'static_text' requires `text`; every other value must leave "
            "`text` unset.",
        ),
    ]
    text: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=2000,
            description="The message to speak once voicemail is detected. Required (and "
            "non-empty) only when action='static_text' — rejected for every other action "
            "value.",
        ),
    ] = None
    detection_prompt: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=2000,
            description="Optional custom natural-language definition of what counts as "
            "voicemail for this call, for edge cases where the default detection isn't "
            "right. Leave unset to use the vendor's own default definition.",
        ),
    ] = None

    @model_validator(mode="after")
    def _validate_text_matches_action(self) -> VoicemailDetectionConfig:
        if self.action == VoicemailAction.STATIC_TEXT:
            if not self.text:
                raise ValueError(
                    "text must be a non-empty string when action='static_text' — provide "
                    "the message to speak once voicemail is detected."
                )
        elif self.text is not None:
            raise ValueError(
                f"text must be left unset when action='{self.action.value}' — text is only "
                "valid alongside action='static_text'."
            )
        return self


# Internal reasoning for CallDirection, deliberately kept in a plain comment
# (not rendered by FastAPI/Swagger — see the standards doc's "Swagger-visible
# text must never name the vendor" rule, which applies to a class docstring
# but not to a `#` comment above it) rather than in the class docstring
# below, since this note names an internal route.
#
# Two creation paths now, both real. OUTBOUND: `POST /calls/outbound` (see
# app/routers/calls.py's create_outbound_call) creates the Calls document
# up front, before the vendor call is even placed — the post-call webhook
# later finds it via `call_repo.get_by_vendor_ref` and updates it in place.
# INBOUND: nobody on our side ever called `POST /calls/outbound` for this
# call, so the post-call webhook's `get_by_vendor_ref` lookup
# (app/routers/webhooks.py's `handle_post_call`) finds nothing — instead of
# dropping the event (the old gap this comment used to describe), the
# handler now resolves the vendor's own `call.agent_id` to one of our own
# Agents documents (`agent_repo.get_by_vendor_ref`, the same lookup pattern
# `handle_custom_tool_call` already uses) to learn which Platform/agent owns
# this number, and creates the Calls document right there, at the moment we
# first learn the call happened at all — which is also already the moment it
# ended (see CallStatus's own docstring for why there's no "registered"
# phase for this path). If the agent_id doesn't resolve to any agent we
# recognize, no record is created (there's no Platform to own it) — logged
# and acknowledged gracefully, same as every other "unrecognized vendor
# identity" case in this module.
class CallDirection(StrEnum):
    """Was this call triggered by Platform X through us (OUTBOUND), or did it
    arrive on one of Platform X's provisioned numbers with no prior VoiceAI
    Calls record (INBOUND)?

    Added for the call-completed notification — Platform X, especially
    running an outbound calling campaign it triggered itself, needs to know
    which kind of call this notification is about; the same summary/
    sentiment payload means something different depending on whether
    Platform X initiated the call or a caller did.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallInDB(BaseModel):
    """Shape of a `Calls` document as stored in Mongo.

    `vendor`/`vendor_ref` are internal-only — correlate a document back to
    whichever vendor/adapter placed it and to that vendor's own call id, for
    webhook correlation and support debugging. Never exposed on CallPublic —
    see the Database rules in the standards doc: the vendor's own ID is
    stored, but only as a clearly-internal field.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    platform_id: str
    agent_id: str

    from_number: str
    to_number: str
    dynamic_variables: dict[str, str]

    status: CallStatus
    direction: CallDirection
    vendor: str
    vendor_ref: str | None

    # Post-call fields — None until the post-call webhook has processed a
    # finished-call event for this record (see this module's docstring for
    # the full field-by-field reasoning). recording_url/transcript_url are
    # OUR OWN re-hosted serving paths, never Retell's raw URLs — the
    # database-field-level version of the "never leak vendor_ref" rule
    # applied to re-hosted media, not just to an opaque id.
    recording_url: str | None = None
    transcript_url: str | None = None
    summary: str | None = None
    sentiment: str | None = None
    extracted_data: dict[str, Any] | None = None
    recording_rehost_failed: bool = False
    # in_voicemail/disconnection_reason — see this module's docstring,
    # "in_voicemail"/"disconnection_reason" sections, for the full sourced
    # reasoning (including why disconnection_reason is now stored, reversing
    # a prior session's deliberate decision not to).
    in_voicemail: bool | None = None
    disconnection_reason: str | None = None

    created_at: datetime
    updated_at: datetime


class CallPublic(BaseModel):
    """Safe-to-return shape — no `vendor`, no `vendor_ref` (the voice
    vendor's raw call id never reaches Platform X; `id` is our own Mongo id
    instead, the stable identifier Platform X should reference this call by
    later).

    `recording_url`/`transcript_url` are OUR OWN re-hosted serving paths
    (never the voice vendor's raw URLs — see CallInDB's docstring for the
    full "re-host, don't pass through" reasoning) and stay `None` until a
    call has actually finished and been successfully re-hosted.
    `recording_rehost_failed` distinguishes "not ready yet" (still `None`,
    flag `False`) from "re-hosting was attempted and failed" (still `None`,
    flag `True`) — real, useful signal for Platform X's own error handling,
    not an internal implementation detail (it names no vendor, no cloud
    provider, no infrastructure detail).

    `extracted_data` — the facts configured via the owning agent's
    `structured_data_fields` (see AgentPublic), automatically pulled from
    this call once it's analyzed. `None` until analysis finishes, or if the
    agent had no fields configured at all. See CallInDB's docstring for the
    full sourcing/naming reasoning.

    `in_voicemail`/`disconnection_reason` — the voicemail-detection RESULT
    (see `CreateOutboundCallRequest.voicemail_detection` for the request-side
    trigger). `in_voicemail` is `None` until analysis finishes, `True`/`False`
    once it has (a real `False` from the vendor means "confirmed a human
    answered," not "unknown" — see CallInDB's docstring for the sourced
    confirmation). `disconnection_reason` is the vendor's own raw reason
    string (e.g. `"voicemail_reached"`, `"user_hangup"`) once the call has
    ended, `None` before then.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6706f1a2b3c4d5e6f708abcd",
                "platform_id": "6706f1a2b3c4d5e6f7089aaa",
                "agent_id": "6706f1a2b3c4d5e6f7089abc",
                "from_number": "+19129143920",
                "to_number": "+15551234567",
                "dynamic_variables": {"contact_name": "John Smith"},
                "status": "completed",
                "direction": "outbound",
                "recording_url": "/calls/6706f1a2b3c4d5e6f708abcd/recording",
                "transcript_url": "/calls/6706f1a2b3c4d5e6f708abcd/transcript",
                "summary": "Caller asked about visiting hours and was transferred to staff.",
                "sentiment": "Positive",
                "extracted_data": {
                    "Caller Name": "Jane Doe",
                    "Call Outcome": "Appointment booked",
                },
                "recording_rehost_failed": False,
                "in_voicemail": False,
                "disconnection_reason": "user_hangup",
                "created_at": "2026-08-19T10:00:00Z",
                "updated_at": "2026-08-19T10:00:00Z",
            }
        }
    )

    id: str
    platform_id: str
    agent_id: str
    from_number: str
    to_number: str
    dynamic_variables: dict[str, str]
    status: CallStatus
    direction: CallDirection
    recording_url: str | None
    transcript_url: str | None
    summary: str | None
    sentiment: str | None
    extracted_data: Annotated[
        dict[str, Any] | None,
        Field(
            description="Facts automatically extracted from this call, as configured on the "
            "owning agent's structured_data_fields (see GET /agents' response for that "
            'agent). Keyed by each configured field\'s own name (e.g. {"Caller Name": "Jane '
            'Doe", "Call Outcome": "Appointment booked"}). null until analysis finishes, or '
            "if the agent has no structured_data_fields configured at all.",
        ),
    ]
    recording_rehost_failed: bool
    in_voicemail: Annotated[
        bool | None,
        Field(
            description="Whether this call's voicemail detector (see POST /calls/outbound's "
            "voicemail_detection) determined the call reached voicemail. null until analysis "
            "finishes; a real false means a human was confirmed to have answered, not that "
            "detection hasn't run yet.",
        ),
    ]
    disconnection_reason: Annotated[
        str | None,
        Field(
            description="Why the call ended (e.g. 'user_hangup', 'agent_hangup', "
            "'voicemail_reached'). null until the call has ended.",
        ),
    ]
    created_at: datetime
    updated_at: datetime


class CreateOutboundCallRequest(BaseModel):
    """POST /calls/outbound request body.

    `from_number` must be an E.164 number already provisioned through one of
    our own numbers endpoints (`POST /agents/{agent_id}/numbers` or its `/byo`
    sibling) for the calling platform — we look it up in our own records and
    reject (422) a number that isn't ours to call from, even if it might be a
    perfectly valid number on the voice vendor's side. `agent_id` is our own
    agent id (from `POST /agents`); it does not need to match whichever agent
    is currently bound to `from_number` — using a different agent for one
    call without rebinding the number is an intentional, supported case.

    `voicemail_detection` is optional — see this module's docstring,
    "voicemail_detection" section, for the full sourced field-shape reasoning
    and why it's never restricted by the owning agent's response_engine mode.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "from_number": "+19129143920",
                "to_number": "+15551234567",
                "agent_id": "6706f1a2b3c4d5e6f7089abc",
                "dynamic_variables": {"contact_name": "John Smith"},
                "voicemail_detection": {
                    "action": "static_text",
                    "text": "Sorry we missed you. Please call us back at your convenience.",
                },
            }
        }
    )

    from_number: Annotated[
        str,
        Field(
            description="E.164 number to call from. Must be a number already provisioned "
            "for the calling platform via POST /agents/{agent_id}/numbers or its /byo "
            "sibling — a number this platform hasn't provisioned with us is rejected, "
            "even if it looks well-formed.",
        ),
    ]
    to_number: Annotated[
        str,
        Field(
            description="E.164 destination number to dial (e.g. '+14155551234'). Note: if "
            "`from_number` was purchased through us rather than imported from your own SIP "
            "trunk, only US destination numbers are supported.",
        ),
    ]
    agent_id: Annotated[
        str,
        Field(
            description="Our agent id (from POST /agents' response) to run for this call. "
            "Does not need to match the agent currently bound to `from_number` — supplying "
            "a different one runs that agent for this call only, without rebinding the "
            "number.",
        ),
    ]
    dynamic_variables: Annotated[
        dict[str, str],
        Field(
            description="Optional {{variable}} values to inject into the agent's prompt for "
            'this call only (e.g. {"contact_name": "John Smith"}). Every value must be a '
            'string — stringify numbers/booleans yourself (e.g. "3" not 3, "true" not '
            "true) before sending. Empty by default.",
        ),
    ] = {}  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    voicemail_detection: Annotated[
        VoicemailDetectionConfig | None,
        Field(
            description="If set, try to detect voicemail in the first 3 minutes of the call "
            "and take the configured action once detected. Omit entirely for the vendor's "
            "own default behavior (no voicemail detection).",
        ),
    ] = None
