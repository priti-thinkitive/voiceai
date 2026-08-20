"""Wire shape of the voice vendor's real post-call webhook body — what
Retell sends US when a call finishes, per `POST /webhooks/retell/post-call`
(see app/routers/webhooks.py's module docstring for the full, sourced
research trail: confirmed event names, payload shape, and signature
mechanism).

Kept in its own module, separate from `inbound_call.py`, on purpose — that
file's docstring already explains its own two-distinct-wire-shapes split;
this is a third, unrelated wire shape (a different real Retell webhook
mechanism entirely — fired after a call ends, not before one is answered)
and folding it into `inbound_call.py` would misname that file's scope.

This is a vendor webhook body, never rendered in Swagger (the router takes
the raw request body for signature verification before ever parsing it, and
the route itself is `include_in_schema=False` — see webhooks.py), so
vendor-neutral phrasing isn't load-bearing here the way it is for a
Platform-X-facing model, but is used anyway for consistency with the rest of
this codebase's models.

Confirmed real shape (WebFetch of docs.retellai.com/features/
webhook-overview.md this session, cross-checked against eCareVoiceAI's own
real, working `webhooks/retell.py:_process_post_call_payload`, which reads
these exact nested paths off a real, working integration):

    {"event": "call_ended" | "call_analyzed" | "call_started",
     "call": {
        "call_type": "phone_call",
        "call_id": "...", "agent_id": "...",
        "from_number": "...", "to_number": "...",
        "direction": "inbound" | "outbound",
        "call_status": "registered" | "not_connected" | "ongoing" |
                        "ended" | "error",
        "disconnection_reason": "user_hangup" | "agent_hangup" |
                                 "voicemail_reached" | ... (37 total values),
        "recording_url": "https://...s3...",
        "transcript": "...",
        "transcript_object": [...],
        "call_analysis": {
            "call_summary": "...",
            "user_sentiment": "Positive" | "Negative" | "Neutral" | "Unknown",
            "call_successful": bool,
            "in_voicemail": bool,
            "custom_analysis_data": {...}
        },
        ...
     }}

Only `call_analyzed` carries a populated `call_analysis` object in practice
(confirmed: `call_ended` fires first, before analysis finishes: "call_ended
fires with no call_analysis block... when call_analyzed hasn't landed yet —
normal mid-flight" per eCareVoiceAI's own working code comment) — modeled as
optional here, not assumed present on every finished-call event.

**`from_number`/`to_number` — re-confirmed via a fresh live WebFetch of
docs.retellai.com/features/webhook-overview this session, specifically for
THIS event's own payload (not carried over from the separate, already-
confirmed `call_inbound` pre-call webhook's payload, which is a genuinely
different wire shape — see inbound_call.py's docstring). The vendor's own
sample payload confirms both fields are present directly on `call`, spelled
exactly the same as the pre-call webhook's own `from_number`/`to_number`.
Added specifically so `POST /webhooks/retell/post-call`'s handler can build a
`Calls` record for a genuinely inbound call the first time it ever learns
about one (no prior `POST /calls/outbound` created a record) — see
app/routers/webhooks.py's `handle_post_call` for that flow.

Retell's own sample payload also includes a `direction` field
("inbound"/"outbound") directly on `call` — deliberately NOT modeled or
trusted here. This codebase's own `CallDirection` is derived structurally
(a `Calls` document already existing, created by `POST /calls/outbound`,
means OUTBOUND; none existing yet means INBOUND — see
app/routers/webhooks.py's `handle_post_call`), not by copying the vendor's
own self-reported label — the same "Platform X only needs to know our own
honest state, not the vendor's internal one" reasoning already applied to
`CallStatus` in app/models/call.py's docstring.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RetellCallAnalysis(BaseModel):
    """The `call.call_analysis` object — only populated on `call_analyzed`,
    per this module's docstring. Only the fields this handler actually reads
    are modeled (`call_successful` is a real Retell field too but nothing in
    this task's scope reads it yet, per the "no unnecessary fields" rule).

    `custom_analysis_data` — the real output of the structured-data-
    extraction feature (see app/models/agent.py's `structured_data_fields`
    docstring for the request-side field that configures this). Confirmed
    via a live WebFetch of the voice vendor's own current get-call API
    reference this session: `type: object` with no fixed properties — a
    dict keyed by each configured field's own `name`, values matching each
    field's own `type` (string/bool/number, or one of the configured
    `choices` for an enum field). This IS now read — see
    app/routers/webhooks.py's post-call handler, which passes it through to
    `call_repo.update_post_call_outcome` as `extracted_data`.

    `in_voicemail` — the real result of the voicemail-detection feature (see
    app/models/call.py's `CreateOutboundCallRequest.voicemail_detection`
    docstring for the request-side field that triggers detection). Confirmed
    real via a live WebFetch of the voice vendor's own current get-call API
    reference this session: a plain boolean, with the vendor's own sample
    payload showing an explicit `false` (not an absent field) for a call a
    human answered. `None` here only means "call_analyzed hasn't arrived
    yet," never "detection ran and found nothing" — once analysis exists,
    Retell always sends a real `true`/`false`.
    """

    call_summary: str | None = None
    user_sentiment: str | None = None
    custom_analysis_data: dict[str, Any] | None = None
    in_voicemail: bool | None = None


class RetellPostCallDetails(BaseModel):
    """The `call` object nested inside the vendor's post-call webhook body.

    Only fields this handler actually reads are modeled. `agent_id` here is
    the VOICE VENDOR's own agent id (Agent.vendor_ref), not our Mongo id —
    used for correlation in exactly one case: an unrecognized `call_id` (no
    existing Calls document — see call_repo.get_by_vendor_ref), where it's
    the ONLY way to resolve which of our own Agents (and therefore which
    Platform) a genuinely inbound call belongs to, via
    `agent_repo.get_by_vendor_ref` (see app/routers/webhooks.py's
    `handle_post_call`). When `call_id` DOES already resolve to an existing
    Calls document (the outbound path), `agent_id` here is redundant with
    that document's own `agent_id` and not currently re-checked — kept
    modeled either way since it's a real, always-present field on the
    vendor's payload.

    `from_number`/`to_number` — see this module's docstring for the fresh
    per-event confirmation. Used only in the same unrecognized-`call_id`
    (inbound-record-creation) path as `agent_id` above; the outbound path
    already has these values from `POST /calls/outbound`'s own request body
    and does not re-read them here.

    `disconnection_reason` — real, documented field directly on `call`
    (confirmed via a live WebFetch of the voice vendor's own current get-call
    API reference this session: one of 37 documented values, e.g.
    `"user_hangup"`, `"agent_hangup"`, `"voicemail_reached"`). A prior
    session note said this was deliberately not modeled at all ("no
    caller-facing feature in this task's scope reads it yet") — see
    app/models/call.py's CallInDB docstring for why that reasoning no longer
    holds now that the voicemail-detection feature is a real reader.
    """

    call_id: str
    agent_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    call_status: str | None = None
    recording_url: str | None = None
    transcript: str | None = None
    call_analysis: RetellCallAnalysis | None = None
    disconnection_reason: str | None = None


class RetellPostCallWebhook(BaseModel):
    """Body of the voice vendor's real post-call webhook — `call_started`,
    `call_ended`, or `call_analyzed`, confirmed as three SEPARATE webhook
    deliveries sent at different times (not one combined event) — see this
    module's docstring for the sourced confirmation.
    """

    model_config = ConfigDict(populate_by_name=True)

    event: str
    call: RetellPostCallDetails
    event_timestamp: int | None = None
