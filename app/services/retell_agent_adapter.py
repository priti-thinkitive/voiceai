"""Vendor adapter for agent creation specifically — split out of
retell_adapter.py once that file crossed the standards doc's ~800-1000 line
size ceiling (it was 1165 lines before this split). Same rules apply: this is
the only place that knows Retell's real agent/LLM-creation request/response
shape, swappable per the adapter-layer seam in
vendor-docs/Full-System-Architecture.html, and every function here follows
the identical never-leak-raw-vendor-error pattern as every other adapter
function in this codebase (see retell_adapter.py's module docstring for the
full shared conventions: auth header, base URL, error handling).

Two response_engine modes are supported, both real, both matching exactly
what Retell itself offers — no invented abstraction (see
app/models/agent.py's module docstring for the full "which mode does what"
reasoning):

**`custom_llm` mode — `create_agent()`, unchanged from before this task.**
`response_engine: {"type": "custom-llm", "llm_websocket_url": ...}` — VoiceAI
runs its own LLM, per vendor-docs/White-Label-Launch-Plan.html Phase 1
decision 16. Single call, no separate LLM object on Retell's side. See the
function's own docstring for the full sourced evidence trail (carried over
unchanged from the pre-split module).

**`retell_llm` mode — NEW, `create_retell_llm()` + `create_retell_llm_agent()`.**
This is the mode that makes an agent actually able to hold a conversation
AND warm-transfer to a human TODAY, without waiting on VoiceAI's own
WebSocket server — closing the real Phase 1 "warm transfer to a human from
day one" gap the standards doc's Feature status section documents as never
having been built. Confirmed via a live WebFetch of Retell's own current
`create-retell-llm` and `create-agent` OpenAPI/docs pages this session (not
guessed, not carried over from eCareVoiceAI's code, though eCareVoiceAI's
production usage of this same `retell-llm` mode — see backend-dev.md's
"Known open items" #2 — is consistent corroborating evidence):

  - `POST /create-retell-llm` (a genuinely separate endpoint from
    `/create-agent`, confirmed real and distinct): `general_prompt` (string,
    the base prompt — unlike custom_llm mode, THIS one actually reaches
    Retell, since retell-llm mode's prompt lives on Retell's own LLM object,
    not on a WebSocket server we control), `general_tools` (array — end_call/
    transfer_call only, see below), `model` (omitted here — Retell defaults
    to "gpt-4.1" on its own side if not sent, a real Retell-side default, not
    something this adapter needs to invent or hardcode), `model_temperature`
    (0.0, Retell's own documented default, sent explicitly for clarity),
    `start_speaker` (required — "agent", so the agent greets the caller
    first, matching typical inbound/outbound voice-agent UX), `begin_message`
    (see the dedicated "welcome_message" paragraph below — OMITTED by
    default, letting Retell/the LLM generate an opening line from
    general_prompt on the fly, which Retell's docs confirm is valid behavior
    for an empty/unset begin_message even with start_speaker="agent"; ONLY
    included when VoiceAI's own `welcome_message` field is actually set, a
    feature added after this module was first built — see that paragraph
    for the full three-state reasoning). Response: `llm_id` (the id this
    adapter hands back for the subsequent create-agent call).
  - `POST /create-agent` with `response_engine: {"type": "retell-llm",
    "llm_id": "<from above>", "version": 0}` — confirmed exact shape via the
    same live WebFetch. All the other agent-level fields (voice_id, language,
    voice_speed, interruption_sensitivity, enable_backchannel,
    pronunciation_dictionary, webhook_url) are confirmed to apply identically
    regardless of response_engine type, so `create_retell_llm_agent()` below
    reuses the exact same body-building logic as `create_agent()`'s
    custom-llm path for those fields — only `response_engine` itself differs.
  - `DELETE /delete-retell-llm/{llm_id}` (`delete_retell_llm()`) — confirmed
    real via a live WebFetch of Retell's own current docs this session: 204
    on success. Not called from any router directly; used for (a) the
    orphan-cleanup path in `create_retell_llm_agent()` below when
    create-retell-llm succeeds but the subsequent create-agent call fails,
    and (b) real manual live-verification cleanup (mirrors
    retell_adapter.py's delete_phone_number()/stop_call() being kept around
    purely for live-test teardown).

**`welcome_message` -> `begin_message`, added after this module was first
built to close a genuine customer gap.** VoiceAI's own name for Retell's real
`begin_message` field on create-retell-llm/update-retell-llm — confirmed via
a live WebFetch of Retell's current `create-retell-llm` API reference:
"First utterance said by the agent in the call. If not set, LLM will
dynamically generate a message. If set to \"\", agent will wait for user to
speak first." Three real, distinct vendor behaviors: omitted/unset (agent
improvises an opening line — the original, unchanged default described
above), a specific non-empty string (agent says exactly that, verbatim,
every call — closes the real gap: some businesses need a consistent,
word-for-word greeting for compliance/brand-consistency/required-disclosure
reasons, which improvised-per-call can never guarantee), or an explicit
empty string `""` (agent stays silent and waits for the caller to speak
first). Confirmed present and settable on BOTH create-retell-llm and
update-retell-llm, not create-only.

**The one genuinely subtle implementation detail here, worth calling out
explicitly: distinguishing "don't send begin_message at all" from "send
begin_message: ''" requires an `is not None` check, NEVER a truthy
check.** `""` is falsy in Python, so `if welcome_message:` would silently
collapse the "wait silently" case into the "omit the key, let the AI
improvise" case — a real, silent behavior change, not a cosmetic one (an
agent a customer configured to wait for the caller would instead start
talking first). `create_retell_llm()` below uses `is not None` for exactly
this reason; so does the update path in app/routers/agents.py's
`update_agent` handler, which builds `update_retell_llm()`'s request body
(this adapter's `update_retell_llm()` itself is a thin, generic
whatever-the-caller-sends passthrough — see that function's own docstring —
so the `is not None` discipline has to live at the call site, same as every
other field on that whole-object-reconstruction body). See
app/models/agent.py's module docstring, "welcome_message" section, for the
full three-state semantics this mirrors at the model layer.

**CREATE vs UPDATE handle the "no value" case differently — this is not an
inconsistency, it is required by `update-retell-llm`'s own real semantics,
confirmed live against a real vendor account.** On CREATE
(`create_retell_llm()` below), omitting the `begin_message` key entirely is
correct and sufficient — there is no prior vendor-side value to worry about,
so "no key" and "improvise" are the same outcome. On UPDATE, they are NOT
the same: `update-retell-llm` is a genuine field-level partial-merge
endpoint, so omitting `begin_message` from an update body leaves Retell's
CURRENTLY-STORED value untouched rather than clearing it — confirmed by a
real `PATCH /update-retell-llm/{llm_id}` this session that left a
previously-set `begin_message` unchanged when the key was left out. To
genuinely clear a welcome_message back to improvised behavior via PATCH,
the caller must send `begin_message: null` EXPLICITLY (confirmed live: this
does remove the key from Retell's own stored object) — see
app/routers/agents.py's `update_agent` handler, which always includes
`begin_message` in its `update_retell_llm()` request body whenever the LLM
object is touched at all, sending explicit `None` rather than omitting the
key when the agent's merged, intended welcome_message is `None`.

**`general_tools` scope, explicitly narrow per product decision — do not
extend without a new decision.** Retell's real `general_tools` schema
supports 13 tool types total (end_call, transfer_call, check_availability_cal,
book_appointment_cal, agent_swap, press_digit, send_sms, custom, code,
extract_dynamic_variable, bridge_transfer, cancel_transfer, mcp, confirmed
via the same live WebFetch). Only `end_call`, `transfer_call`, and (as of
this task) `custom` are built here — nothing in Phase 1 scope needs calendar
booking, SMS, code execution, MCP, or agent-swap yet. `transfer_call` itself
is further narrowed: only the `predefined` form of `transfer_destination` (a
Platform-X-supplied fixed number — never the `inferred`/AI-guessed-number
form, since VoiceAI's `transfer_number` field is an explicit, caller-supplied
value, not something we want the AI inferring from conversation context) and
only the `warm_transfer` form of `transfer_option` (never `cold_transfer` or
`agentic_warm_transfer` — warm transfer specifically is the feature this
task exists to close the gap on).

**`custom` tool entries — one per `CreateAgentRequest.custom_tools` entry,
`url` ALWAYS our own proxy, never Platform X's `webhook_url`
directly.** See app/models/agent.py's module docstring for the full
proxy-routing architecture decision (vendor-identity-leak prevention) and
CustomToolDefinition's own docstring for what each field controls.
`_build_general_tools` below builds `url` from `settings.BASE_URL`
exactly like `webhook_url`/`inbound_webhook_url` already are elsewhere in
this codebase (see app/routers/agents.py) — one fixed URL,
`{BASE_URL}/webhooks/retell/custom-tool`, shared by every custom tool on
every agent, since the routing/lookup mechanism (app/routers/webhooks.py's
custom-tool handler) resolves which specific tool fired from the payload's
own `name`/`call.agent_id` fields, not from a per-tool URL path. `method`/
`timeout_ms`/`parameters_schema` map directly onto Retell's real, identically-
named fields — confirmed via the same live WebFetch of Retell's current
custom-function docs this session.

**`post_call_analysis_data` — structured data extraction, an AGENT-object
field, confirmed NOT nested under response_engine/the LLM object.** Confirmed
via a live WebFetch of the vendor's own current create-agent AND
create-retell-llm API references this session: `post_call_analysis_data` (and
its sibling `post_call_analysis_model`) appear only on the agent-creation
request body, at the same level as `voice_id`/`response_engine` — the
create-retell-llm request body has no such field at all. This is exactly why
`structured_data_fields` (VoiceAI's own name for it — see
app/models/agent.py's module docstring) is NOT restricted to `builtin`
mode the way `transfer_number`/`custom_tools` are: those two depend on the
vendor's separate LLM object (`general_tools`), which only exists under
`builtin`, but this field is sent on every `POST /create-agent` call
this codebase makes, in both `create_agent()` and `create_retell_llm_agent()`
below (both funnel through the shared `_post_create_agent()` helper), so it
is genuinely available under both modes. `post_call_analysis_model` —
**REVISED this session, superseding this paragraph's own earlier
reasoning**: it used to be deliberately OMITTED here, on the reasoning that
the vendor's own docs confirm a real, documented default (`gpt-4.1`)
applies when the field is left unset, so this adapter didn't need to invent
or hardcode a model choice. That premise is still true, but the conclusion
drawn from it was incomplete — "the vendor has a sane default" is a reason
not to REQUIRE a value, not a reason to never let a caller CHOOSE one. The
sibling `model` field on `create_retell_llm()` below went through the
identical reasoning this session and was promoted from "correctly omitted"
to "real, customer-relevant gap, expose it" — see app/models/agent.py's
module docstring, "~19-field tuning-knob batch" section, for the full
field-by-field sourcing. `post_call_analysis_model` gets the same treatment
now, for consistency: both `create_agent()` and `create_retell_llm_agent()`
below include it in their `POST /create-agent` body whenever
`CreateAgentRequest.post_call_analysis_model` is non-None, same "only
include an optional key when it has a real value" convention as
`webhook_url`/`agent_name` elsewhere in this module.

**The rest of the ~19-field tuning-knob batch — `voice_model`,
`voice_temperature`, `stt_mode`, `denoising_mode`, `ambient_sound`,
`ambient_sound_volume`, `backchannel_frequency`, `backchannel_words`,
`responsiveness`, `reminder_trigger_ms`, `reminder_max_count`,
`end_call_after_silence_ms`, `max_call_duration_ms`,
`begin_message_delay_ms`, `allow_user_dtmf`, `allow_dtmf_interruption`,
`data_storage_setting`, `pii_config`, `handbook_config` — are all AGENT-
object fields, confirmed via the same fresh WebFetch this session, so they
follow the identical dual-path placement as `agent_name`/
`structured_data_fields`/`post_call_analysis_model` above: built once by
the shared `_build_agent_tuning_fields()` helper below and merged into the
request body of BOTH `create_agent()` and `create_retell_llm_agent()`,
genuinely available under both response_engine modes. `model`/
`model_temperature` are the only two fields in this whole batch that are
LLM-object fields instead — see `create_retell_llm()`'s own docstring
below.**

**`states`/`starting_state` — Single Prompt vs Multi Prompt, sent on
`create-retell-llm`/`update-retell-llm` ONLY, never on `create-agent`/
`update-agent`.** Confirmed via the same live schema check referenced in
app/models/agent.py's module docstring: both fields live on the vendor's
separate LLM object (the same object `general_prompt`/`general_tools`
already live on), not the agent object — consistent with `transfer_number`/
`custom_tools` above being LLM-object-coupled, and the opposite placement
from `structured_data_fields`/`post_call_analysis_data`, which is
agent-object-only. `_build_states()` below maps `AgentState`/`StateEdge`
(app/models/agent.py) to the vendor's real `{name, state_prompt, edges,
tools}`/`{destination_state_name, description}` shapes; a state's own
`tools` reuses `_build_general_tools`'s `custom` entry-building logic (via
`_build_state_tools()`) so a per-state tool gets the identical our-own-proxy
`url` treatment as every agent-level `custom_tools` entry — no separate
proxy-routing story for state-scoped tools. Included in the request body
only when `states` is non-empty (same "only include an optional key when it
has a real value" convention as `webhook_url`/`post_call_analysis_data`
elsewhere in this module) — an empty `states` list is never sent as
`"states": []` on CREATE (there is nothing to configure yet), though
`update_retell_llm()`'s caller (the router) DOES send `"states": []`
explicitly on an UPDATE that's deliberately collapsing a Multi Prompt agent
back to Single Prompt, since omitting the key entirely there would leave
the vendor's existing states array untouched instead of clearing it — see
that function's own docstring.

**`update_agent()`/`update_retell_llm()` — NEW, back PATCH /agents/{agent_id}
(app/routers/agents.py).** Both confirmed real via a live WebFetch of
Retell's own current docs this session: `PATCH /update-agent/{agent_id}` and
`PATCH /update-retell-llm/{llm_id}`, both true partial-merge endpoints at the
field level (an omitted field keeps its current vendor-side value — neither
endpoint requires the full object on every call). Unlike the two `create_*`
functions above, these two do NOT build their own request bodies from
individual kwargs — the caller builds `body` itself and passes it through,
since deciding which fields belong in that body is inherently tied to
`UpdateAgentRequest`'s own merge semantics (see that model's docstring in
app/models/agent.py), not a concern that belongs duplicated into this
adapter layer. See each function's own docstring for the full reasoning,
especially `update_retell_llm()`'s on why `general_tools` specifically still
needs whole-array reconstruction on our side despite the endpoint itself
being partial-merge.

**`agent_name` — VoiceAI's own name for the vendor's real `agent_name` field,
confirmed on BOTH `create-agent` and `update-agent` this session, an
AGENT-object field, NOT an LLM-object field — the opposite placement from
`begin_message`/`general_tools`/`states` above.** Sent directly in the
request body built by `create_agent()` (custom-llm path) AND in
`create_retell_llm_agent()`'s own `POST /create-agent` call (the SECOND of
its two real vendor calls, the same one `post_call_analysis_data`/
`structured_data_fields` already lands on — never the `create-retell-llm`
call that precedes it, which has no `agent_name` concept at all). This is
why `agent_name` is genuinely available under BOTH response_engine modes,
unlike `begin_message`/`general_tools`/`states`, which only exist under
`retell_llm` mode's separate LLM object. Included in the request body only
when non-None, same "only include an optional key when it has a real value"
convention as `webhook_url`/`post_call_analysis_data` elsewhere in this
module — there is no three-state (None/""/string) distinction to preserve
here the way `welcome_message` has, since the vendor's own docs describe no
special meaning for an empty string on this field.

**`live_transcript_enabled` -> `webhook_events` — confirmed via a live
WebFetch of Retell's own current create-agent/update-agent API references
this session, an AGENT-object field like `agent_name`/
`structured_data_fields`, NOT an LLM-object field.** `_build_webhook_events`
maps VoiceAI's own `live_transcript_enabled` bool to the vendor's real
`webhook_events` array — Retell's own documented default set
(`call_started`/`call_ended`/`call_analyzed`) plus `transcript_updated` when
enabled, key omitted entirely when disabled (preserving today's existing
behavior unchanged for every agent that doesn't opt in). Sent on `POST
/create-agent` in both `create_agent()` and `create_retell_llm_agent()`
below, same dual-path availability as `agent_name`/`structured_data_fields` —
see app/models/agent.py's module docstring, "live_transcript_enabled"
section, for the full feature description and the explicit note that this
placement corrects an initial assumption (by analogy with
`welcome_message`/`states`) that it would be LLM-object-only.

**Orphan-cleanup decision on partial failure, reasoned through explicitly.**
If `create-retell-llm` succeeds but the subsequent `create-agent` call fails,
that LLM object is now a real, billed(-adjacent) orphan on Retell's account
with nothing referencing it — the same "never leave orphaned real vendor
resources" discipline already established elsewhere in this project (see
retell_adapter.py's create_phone_number()/delete_phone_number() live-test
cleanup discipline, and app/services/storage.py's partial-success handling
for post-call re-hosting). `create_retell_llm_agent()` attempts a real
`delete_retell_llm()` call immediately on this specific failure path. If that
cleanup call *also* fails (a genuinely unlucky double-failure), it is caught
and logged clearly at ERROR with the orphaned `llm_id` explicit in
`log_extra` — never silently swallowed, never allowed to mask or replace the
original create-agent failure that's still raised to the caller. The
original create-agent AppError is always what reaches the router/caller;
cleanup is a best-effort side effect, not something that can change the
caller-visible outcome.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.errors import CODE_UPSTREAM_FAILED, AppError
from app.models.agent import (
    AgentState,
    CreateAgentRequest,
    CustomToolDefinition,
    HandbookConfig,
    OnHoldMusic,
    PiiConfig,
    PronunciationEntry,
    StructuredDataFieldDefinition,
)
from app.models.language import Language

logger = logging.getLogger("app.retell_agent_adapter")

VENDOR_NAME = "retell"

# Placeholder for VoiceAI's own not-yet-built custom-LLM WebSocket server.
# Retell connects here per-call once real traffic flows; building that server
# is future work, out of scope for agent creation itself.
_CUSTOM_LLM_WEBSOCKET_URL = "wss://voiceai.example.com/llm-websocket"

# Retell's own documented default for model_temperature (see this module's
# docstring) — sent explicitly rather than omitted, so the request body is
# self-documenting even though it matches what Retell would default to
# anyway.
_DEFAULT_MODEL_TEMPERATURE = 0.0

# Retell's own documented DEFAULT webhook_events set — sent verbatim (plus
# "transcript_updated") when an agent opts into live_transcript_enabled, so
# that opting in ADDS transcript_updated rather than silently REPLACING the
# vendor's default set and breaking the existing post-call re-hosting flow
# this codebase already depends on for every agent. Confirmed via a live
# WebFetch of Retell's own current create-agent/update-agent API references
# this session: webhook_events, when present at all, REPLACES the default
# set entirely — there is no vendor-side "add to defaults" semantics, so this
# codebase must spell out the full intended set itself. See
# app/models/agent.py's module docstring, "live_transcript_enabled" section,
# for the full reasoning.
_DEFAULT_WEBHOOK_EVENTS = ("call_started", "call_ended", "call_analyzed")
_TRANSCRIPT_UPDATED_EVENT = "transcript_updated"


def _build_post_call_analysis_data(
    fields: list[StructuredDataFieldDefinition],
) -> list[dict[str, Any]]:
    """Map VoiceAI's own `structured_data_fields` (see app/models/agent.py's
    module docstring) to the vendor's real `post_call_analysis_data` array
    shape: `{type, name, description, choices?}` — `choices` only included
    when present, matching this codebase's existing "only include an
    optional key when it has a real value" convention (e.g. `webhook_url` on
    create_agent()'s body below).
    """
    entries: list[dict[str, Any]] = []
    for field in fields:
        entry: dict[str, Any] = {
            "type": field.type.value,
            "name": field.name,
            "description": field.description,
        }
        if field.choices:
            entry["choices"] = field.choices
        entries.append(entry)
    return entries


def _build_vendor_language(languages: list[Language]) -> str | list[str]:
    """Map VoiceAI's own normalized `languages` list (see
    app/models/agent.py's module docstring for the full wire-format
    decision) back to the shape the voice vendor's real `language` field
    expects: a bare string for the single-language case (the vendor's own
    "common case" framing, confirmed via live WebFetch of its create-agent
    OpenAPI schema), or a real JSON array for a genuinely multilingual
    agent. NEVER the deprecated `"multi"` shortcut string — that stays
    explicitly out of scope regardless of how many languages are configured.
    """
    if len(languages) == 1:
        return languages[0].value
    return [lang.value for lang in languages]


def _build_webhook_events(*, live_transcript_enabled: bool) -> list[str] | None:
    """Map VoiceAI's own `live_transcript_enabled` bool (see
    app/models/agent.py's module docstring) to the vendor's real
    `webhook_events` field. Returns `None` (meaning: omit the key entirely)
    when `live_transcript_enabled` is False — preserving today's exact
    existing behavior (Retell's own default event set, implicitly) for every
    agent that doesn't opt in, same "only include an optional key when it has
    a real value/real intent to change something" convention as
    `webhook_url`/`post_call_analysis_data` elsewhere in this module.

    When True, returns the vendor's own documented default set PLUS
    `transcript_updated` — NEVER `transcript_updated` alone, since
    `webhook_events` REPLACES the default set on the vendor's side rather
    than adding to it (confirmed via a live WebFetch this session) — see
    `_DEFAULT_WEBHOOK_EVENTS`'s own comment above for why silently dropping
    call_started/call_ended/call_analyzed would be a real regression to the
    existing post-call re-hosting flow.
    """
    if not live_transcript_enabled:
        return None
    return [*_DEFAULT_WEBHOOK_EVENTS, _TRANSCRIPT_UPDATED_EVENT]


def build_webhook_events_for_update(*, live_transcript_enabled: bool) -> list[str]:
    """Same mapping as `_build_webhook_events` above, but for `PATCH
    /update-agent/{agent_id}` specifically, where omitting the key has a
    genuinely different meaning than it does on CREATE — confirmed via a
    live WebFetch of Retell's own current update-agent docs this session.

    On CREATE, omitting `webhook_events` entirely is correct for the
    disabled case: there is no prior vendor-side value to worry about, so
    "no key" and "the vendor's own documented default set" are the same
    outcome (see `_build_webhook_events` above).

    On UPDATE, they are NOT the same once an agent may have previously had a
    non-default `webhook_events` value (e.g. `live_transcript_enabled` was
    turned on, then later turned back off): `update-agent` is a field-level
    partial-merge endpoint, so omitting `webhook_events` from an update body
    leaves Retell's CURRENTLY-STORED value untouched, not reset to the
    documented default — the exact same "omitted means don't touch" trap
    `begin_message`/`states` already have on `update-retell-llm` (see that
    function's own docstring), applied here to `webhook_events` on the
    SIBLING agent-object update endpoint instead. And an empty array `[]` is
    its OWN distinct, real value here too — confirmed via the same live
    WebFetch: it means "no webhook events at all," not "reset to defaults."
    So the caller (app/routers/agents.py's `update_agent`) must ALWAYS send
    an explicit, complete `webhook_events` array whenever this field is
    touched at all: this codebase's own documented default set when
    disabling (restoring exactly Retell's own documented default behavior,
    not silently turning off every webhook event this agent relies on — a
    real, easy-to-get-wrong regression this explicit choice avoids), or that
    same set plus `transcript_updated` when enabling. Never `None`/omitted,
    never bare `[]`, unlike the CREATE-time helper above.
    """
    if live_transcript_enabled:
        return [*_DEFAULT_WEBHOOK_EVENTS, _TRANSCRIPT_UPDATED_EVENT]
    return list(_DEFAULT_WEBHOOK_EVENTS)


def _build_agent_tuning_fields(
    *,
    voice_model: str | None,
    voice_temperature: float,
    stt_mode: Any,
    denoising_mode: Any,
    ambient_sound: Any,
    ambient_sound_volume: float,
    backchannel_frequency: float,
    backchannel_words: list[str],
    responsiveness: float,
    reminder_trigger_ms: int,
    reminder_max_count: int,
    end_call_after_silence_ms: int,
    max_call_duration_ms: int,
    begin_message_delay_ms: int,
    allow_user_dtmf: bool,
    allow_dtmf_interruption: bool,
    data_storage_setting: Any,
    pii_config: PiiConfig | None,
    post_call_analysis_model: str | None,
    handbook_config: HandbookConfig | None,
) -> dict[str, Any]:
    """Build the AGENT-object half of the ~19-field tuning-knob batch (see
    this module's docstring, "the rest of the ~19-field tuning-knob batch"
    section) — every field here is real and confirmed on BOTH
    `create-agent`/`update-agent`, so this single helper is shared by
    `create_agent()`, `create_retell_llm_agent()`, and (via the router)
    `PATCH /agents/{agent_id}`'s update-agent body, exactly the same
    dual/triple-path reuse `_build_post_call_analysis_data`/
    `_build_webhook_events` already get.

    Every field is unconditionally included — unlike `webhook_url`/
    `agent_name` elsewhere in this module (which are only included when
    non-None, since their ABSENCE has a real, different meaning worth
    preserving), these ~17 fields all carry real, vendor-documented
    defaults that this codebase's own Pydantic model defaults already
    match exactly (see app/models/agent.py's module docstring for the
    per-field default sourcing) — so sending them explicitly every time,
    even at their default value, is self-documenting and harmless, the
    same "sent explicitly for clarity" choice `model_temperature` already
    makes on `create_retell_llm()` below. `voice_model`/
    `post_call_analysis_model` are the two exceptions: both stay `None`-
    means-omit-the-key, matching `model`'s own convention on
    `create_retell_llm()`, since the vendor's own bare default for an open-
    ended model-catalog field is "whatever the vendor currently defaults
    to," not a fixed value this codebase should hardcode into every request.
    `ambient_sound`/`pii_config`/`handbook_config` are likewise only
    included when non-None — each is a genuinely optional feature with no
    "always send a default value" equivalent (there is no sensible default
    ambient sound preset, PII config, or handbook toggle set to send on an
    agent that hasn't opted into any of the three).
    """
    body: dict[str, Any] = {
        "voice_temperature": voice_temperature,
        "stt_mode": stt_mode.value if hasattr(stt_mode, "value") else stt_mode,
        "denoising_mode": denoising_mode.value
        if hasattr(denoising_mode, "value")
        else denoising_mode,
        "ambient_sound_volume": ambient_sound_volume,
        "backchannel_frequency": backchannel_frequency,
        "responsiveness": responsiveness,
        "reminder_trigger_ms": reminder_trigger_ms,
        "reminder_max_count": reminder_max_count,
        "end_call_after_silence_ms": end_call_after_silence_ms,
        "max_call_duration_ms": max_call_duration_ms,
        "begin_message_delay_ms": begin_message_delay_ms,
        "allow_user_dtmf": allow_user_dtmf,
        "allow_dtmf_interruption": allow_dtmf_interruption,
        "data_storage_setting": (
            data_storage_setting.value
            if hasattr(data_storage_setting, "value")
            else data_storage_setting
        ),
    }
    if backchannel_words:
        body["backchannel_words"] = backchannel_words
    if voice_model is not None:
        body["voice_model"] = voice_model
    if ambient_sound is not None:
        body["ambient_sound"] = (
            ambient_sound.value if hasattr(ambient_sound, "value") else ambient_sound
        )
    if post_call_analysis_model is not None:
        body["post_call_analysis_model"] = post_call_analysis_model
    if pii_config is not None:
        body["pii_config"] = pii_config.model_dump(mode="json")
    if handbook_config is not None:
        body["handbook_config"] = handbook_config.model_dump(mode="json", exclude_none=True)
    return body


class RetellCreateAgentResult:
    """Successful `/create-agent` outcome — just the one field we need."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


class RetellCreateRetellLlmAgentResult:
    """Successful retell_llm-mode creation outcome — both real vendor-side
    ids VoiceAI needs to store internally (see AgentInDB.vendor_ref/llm_ref).
    """

    def __init__(self, agent_id: str, llm_id: str) -> None:
        self.agent_id = agent_id
        self.llm_id = llm_id


async def create_agent(
    settings: Settings,
    *,
    prompt: str,
    voice_id: str,
    languages: list[Language],
    voice_speed: float,
    interruption_sensitivity: float,
    enable_backchannel: bool,
    pronunciation_dictionary: list[PronunciationEntry],
    webhook_url: str | None = None,
    structured_data_fields: list[StructuredDataFieldDefinition] | None = None,
    agent_name: str | None = None,
    live_transcript_enabled: bool = False,
    agent_tuning_fields: dict[str, Any] | None = None,
) -> RetellCreateAgentResult:
    """Call Retell's real `POST /create-agent` under `custom-llm` mode.

    Unchanged behavior from before this task's split — see this module's
    docstring for the full sourced evidence trail. Raises
    `AppError(code="upstream_failed")` on any network error, timeout, or
    non-2xx response — never leaks Retell's raw response/exception text to
    the caller, but logs it for debugging.

    `prompt` is not a create-agent field on Retell's side under this mode —
    it belongs to the LLM/response-engine, not the agent object. Under
    `custom-llm`, the prompt lives entirely on VoiceAI's own future WebSocket
    server (which receives live transcripts and generates replies itself),
    not on Retell at all. We still accept and store `prompt` on our own
    Agent record — Retell's agent object simply doesn't reference it under
    this response_engine mode. Contrast with `create_retell_llm()` below,
    where `general_prompt` genuinely does reach Retell — a real, important
    difference between the two modes.

    `webhook_url` — Retell's real, documented agent-level `webhook_url`
    field, set for post-call event delivery (see retell_adapter.py's
    docstring for the full mechanism). Optional here (defaults to None) only
    so existing call sites/tests that don't yet care about post-call events
    aren't forced to pass it.

    `languages` — always a non-empty list on our own side (see
    app/models/agent.py's module docstring for the wire-format decision).
    Mapped to the vendor's real `language` field via `_build_vendor_language`:
    a bare string for the single-language case, a real JSON array for a
    genuinely multilingual agent — never the deprecated `"multi"` string.

    `structured_data_fields` — mapped to the vendor's real
    `post_call_analysis_data` field via `_build_post_call_analysis_data`,
    included only when non-empty (same "only include an optional key when it
    has a real value" convention as `webhook_url` here). Confirmed available
    under this response_engine mode too — see this module's docstring for the
    agent-vs-LLM-object placement confirmation.

    `agent_name` — the vendor's own real `agent_name` field, an
    internal/dashboard-only reference label. Same agent-object placement as
    `structured_data_fields`, so also confirmed available under this
    response_engine mode — see this module's docstring, "agent_name"
    section. Included only when non-None, same convention as
    `webhook_url`/`structured_data_fields` above.

    `live_transcript_enabled` — mapped to the vendor's real `webhook_events`
    field via `_build_webhook_events`, same agent-object placement as
    `agent_name`/`structured_data_fields` (confirmed available under this
    response_engine mode too) — see app/models/agent.py's module docstring,
    "live_transcript_enabled" section. Included only when True, same "only
    include an optional key when it has a real value" convention as every
    other optional field in this body.

    `agent_tuning_fields` — the pre-built AGENT-object half of the
    ~19-field tuning-knob batch (see this module's docstring), from
    `_build_agent_tuning_fields()`. `None` (the default) omits the whole
    batch entirely, matching every existing test/call site that predates
    this batch and doesn't pass it — same "don't force every caller to
    reason about a concern outside their scope" default as `webhook_url`.
    """
    body: dict[str, Any] = {
        "response_engine": {
            "type": "custom-llm",
            "llm_websocket_url": _CUSTOM_LLM_WEBSOCKET_URL,
        },
        "voice_id": voice_id,
        "language": _build_vendor_language(languages),
        "voice_speed": voice_speed,
        "interruption_sensitivity": interruption_sensitivity,
        "enable_backchannel": enable_backchannel,
        "pronunciation_dictionary": [entry.model_dump() for entry in pronunciation_dictionary],
    }
    if webhook_url is not None:
        body["webhook_url"] = webhook_url
    if structured_data_fields:
        body["post_call_analysis_data"] = _build_post_call_analysis_data(structured_data_fields)
    if agent_name is not None:
        body["agent_name"] = agent_name
    webhook_events = _build_webhook_events(live_transcript_enabled=live_transcript_enabled)
    if webhook_events is not None:
        body["webhook_events"] = webhook_events
    if agent_tuning_fields:
        body.update(agent_tuning_fields)

    data = await _post_create_agent(settings, body)
    agent_id = data.get("agent_id")
    if not agent_id:
        logger.error(
            "Retell create-agent succeeded but response is missing agent_id",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while creating the agent.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return RetellCreateAgentResult(agent_id=agent_id)


def _build_custom_tool_entries(
    custom_tools: list[CustomToolDefinition], *, custom_tool_api_endpoint: str
) -> list[dict[str, Any]]:
    """Build one `custom` general_tools-shaped entry per `custom_tools`
    entry — factored out of `_build_general_tools` so `_build_state_tools`
    below (per-state `tools`, see this module's docstring) can build the
    IDENTICAL entry shape for a state-scoped tool without duplicating the
    field mapping. `custom_tool_api_endpoint` is always OUR OWN proxy URL,
    same for every tool regardless of whether it's agent-level or
    state-level — see this module's docstring for the proxy-routing
    architecture decision.
    """
    return [
        {
            "type": "custom",
            "name": tool.name,
            "description": tool.description,
            "url": custom_tool_api_endpoint,
            "method": tool.method.value,
            "timeout_ms": tool.timeout_ms,
            "parameters": tool.parameters_schema,
        }
        for tool in custom_tools
    ]


def _build_general_tools(
    *,
    transfer_number: str | None,
    transfer_ring_duration_ms: int,
    transfer_on_hold_music: OnHoldMusic,
    transfer_show_original_caller_id: bool,
    custom_tools: list[CustomToolDefinition],
    custom_tool_api_endpoint: str,
) -> list[dict[str, Any]]:
    """Build the `general_tools` array for `create-retell-llm` — end_call
    always included (every retell_llm agent should be able to hang up
    cleanly), transfer_call only when `transfer_number` is set, one `custom`
    entry per `custom_tools` entry. See this module's docstring for why only
    these three of Retell's 13 real tool types are supported, and only the
    predefined+warm_transfer forms of transfer_call.

    `custom_tool_api_endpoint` is the SAME fixed URL (our own proxy, built
    from settings.BASE_URL by the caller) for every custom tool on every
    agent — never Platform X's own `webhook_url`, per the proxy-routing
    architecture decision documented in app/models/agent.py's module
    docstring.
    """
    tools: list[dict[str, Any]] = [
        {
            "type": "end_call",
            "name": "end_call",
            "description": "End the call when the conversation is naturally complete or the "
            "caller asks to hang up.",
        }
    ]
    if transfer_number is not None:
        tools.append(
            {
                "type": "transfer_call",
                "name": "transfer_call",
                "description": "Transfer the caller to a human when the agent's own prompt "
                "determines a human needs to take over.",
                "transfer_destination": {
                    "type": "predefined",
                    "number": transfer_number,
                },
                "transfer_option": {
                    "type": "warm_transfer",
                    "show_transferee_as_caller": transfer_show_original_caller_id,
                    "transfer_ring_duration_ms": transfer_ring_duration_ms,
                    "on_hold_music": transfer_on_hold_music.value,
                    "opt_out_human_detection": False,
                },
            }
        )
    tools.extend(
        _build_custom_tool_entries(custom_tools, custom_tool_api_endpoint=custom_tool_api_endpoint)
    )
    return tools


def _build_states(
    states: list[AgentState], *, custom_tool_api_endpoint: str
) -> list[dict[str, Any]]:
    """Map VoiceAI's own `AgentState`/`StateEdge` (app/models/agent.py) to
    the vendor's real `states[]` shape: `{name, state_prompt, edges,
    tools}`, with `edges` as `{destination_state_name, description}`. See
    this module's docstring for the full states/starting_state placement
    confirmation (LLM-object-only, same as general_tools) and Single Prompt
    vs Multi Prompt feature description.

    A state's own `tools` reuses `_build_custom_tool_entries` — the exact
    same per-tool shape/our-own-proxy-url treatment as the agent-level
    `general_tools` custom entries, just scoped into this one state's own
    `tools` array instead of the top-level one.
    """
    return [
        {
            "name": state.name,
            "state_prompt": state.state_prompt,
            "edges": [
                {
                    "destination_state_name": edge.destination_state_name,
                    "description": edge.description,
                }
                for edge in state.edges
            ],
            "tools": _build_custom_tool_entries(
                state.tools, custom_tool_api_endpoint=custom_tool_api_endpoint
            ),
        }
        for state in states
    ]


async def create_retell_llm(
    settings: Settings,
    *,
    general_prompt: str,
    transfer_number: str | None,
    transfer_ring_duration_ms: int,
    transfer_on_hold_music: OnHoldMusic,
    transfer_show_original_caller_id: bool,
    custom_tools: list[CustomToolDefinition],
    states: list[AgentState] | None = None,
    starting_state: str | None = None,
    welcome_message: str | None = None,
    model: str | None = None,
    model_temperature: float = _DEFAULT_MODEL_TEMPERATURE,
) -> str:
    """Call Retell's real `POST /create-retell-llm` — the first of the two
    real vendor calls `retell_llm` mode requires (see this module's
    docstring). Returns the new `llm_id`.

    `model`/`model_temperature` — the two LLM-object fields of the
    ~19-field tuning-knob batch (see app/models/agent.py's module
    docstring). `model` stays `None`-means-omit-the-key (the vendor's own
    bare default, `gpt-4.1`, applies whenever omitted — this codebase
    doesn't hardcode that choice into every request, same reasoning
    `voice_model`/`post_call_analysis_model` use). `model_temperature`
    keeps this function's own pre-existing "sent explicitly, even at its
    default value, for a self-documenting request body" behavior —
    unchanged from before this batch, just now genuinely caller-
    configurable instead of hardcoded to `_DEFAULT_MODEL_TEMPERATURE`.

    `states`/`starting_state` — Single Prompt (default, both omitted/empty)
    vs Multi Prompt — see this module's docstring for the confirmed
    LLM-object placement. Included in the request body only when `states`
    is non-empty, same "only include an optional key when it has a real
    value" convention as `webhook_url`/`post_call_analysis_data` elsewhere
    in this module — there is nothing to configure on an empty/Single
    Prompt agent, so the key is simply left out rather than sent as an
    empty array (contrast with `update_retell_llm()`, whose caller DOES
    send `states: []` explicitly to collapse an EXISTING Multi Prompt agent
    back to Single Prompt — see that function's own docstring for why an
    update needs the explicit empty array where a create does not).

    `welcome_message` -> `begin_message` — see this module's docstring,
    "welcome_message" section, for the full three-state semantics. Included
    in the request body ONLY when `welcome_message is not None` — this is
    the exact `is not None`-not-truthy check that section warns about:
    `welcome_message=""` MUST still set `body["begin_message"] = ""`, since
    an empty string is a real, distinct, meaningful vendor-side value (wait
    silently for the caller), not the same as never having set
    welcome_message at all. `welcome_message=None` (the default — not set
    by the caller) leaves the key out entirely, preserving this codebase's
    original, unchanged default behavior: Retell/the LLM improvises an
    opening line from `general_prompt` on the fly.

    Raises `AppError(code="upstream_failed")` on any network error, timeout,
    non-2xx response, or a 2xx response missing `llm_id` — same
    never-leak-raw-response pattern as every other adapter function in this
    codebase.
    """
    custom_tool_api_endpoint = f"{settings.BASE_URL}/webhooks/retell/custom-tool"
    body: dict[str, Any] = {
        "general_prompt": general_prompt,
        "general_tools": _build_general_tools(
            transfer_number=transfer_number,
            transfer_ring_duration_ms=transfer_ring_duration_ms,
            transfer_on_hold_music=transfer_on_hold_music,
            transfer_show_original_caller_id=transfer_show_original_caller_id,
            custom_tools=custom_tools,
            custom_tool_api_endpoint=custom_tool_api_endpoint,
        ),
        "model_temperature": model_temperature,
        "start_speaker": "agent",
    }
    if model is not None:
        body["model"] = model
    if states:
        body["states"] = _build_states(states, custom_tool_api_endpoint=custom_tool_api_endpoint)
        body["starting_state"] = starting_state
    # is not None, deliberately — see this function's own docstring and this
    # module's docstring for why a truthy check here would be a real bug
    # (silently dropping the "wait silently for the caller" welcome_message
    # ="" case).
    if welcome_message is not None:
        body["begin_message"] = welcome_message

    try:
        async with httpx.AsyncClient(
            base_url=settings.RETELL_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            resp = await client.post("/create-retell-llm", json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell create-retell-llm request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to set up the conversation brain. "
            "Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell create-retell-llm returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to set up the conversation brain.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data = resp.json()
    llm_id = data.get("llm_id")
    if not llm_id:
        logger.error(
            "Retell create-retell-llm succeeded but response is missing llm_id",
            extra={"vendor": VENDOR_NAME},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while setting up the "
            "conversation brain.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME},
        )
    return str(llm_id)


async def delete_retell_llm(settings: Settings, *, llm_id: str) -> None:
    """Call Retell's real `DELETE /delete-retell-llm/{llm_id}`.

    Used for (a) orphan cleanup in `create_retell_llm_agent()` below when the
    LLM was created but the subsequent agent-creation call failed, and (b)
    real manual live-verification cleanup. Raises
    `AppError(code="upstream_failed")` on any network error or
    non-2xx/non-404 response — a 404 (already gone) is treated as success,
    same soft-fail-on-already-gone pattern as retell_adapter.py's
    delete_phone_number().
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
            resp = await client.delete(f"/delete-retell-llm/{llm_id}")
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell delete-retell-llm request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to clean up the conversation brain.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400 and resp.status_code != 404:
        logger.warning(
            "Retell delete-retell-llm returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to clean up the conversation brain.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )


async def delete_agent(settings: Settings, *, agent_id: str) -> None:
    """Call the voice vendor's real `DELETE /delete-agent/{agent_id}`.

    Confirmed via a fresh live WebFetch of Retell's own current docs this
    session (Tier 1 "delete an agent" work — see backend-dev.md's Feature
    status section): `DELETE /delete-agent/{agent_id}`, success is `204 No
    Content`, and the docs' own path-parameter description says plainly
    "Deletes all versions of the agent" — there is no separate per-version
    delete to worry about.

    This is the one delete function this codebase was actually missing
    before this task — `delete_phone_number()` (retell_adapter.py) and
    `delete_retell_llm()` (above) already existed, both built purely for
    manual live-verification cleanup with no router ever calling them, but
    nothing here ever deleted the AGENT object itself. `DELETE
    /agents/{agent_id}` (app/routers/agents.py) is this function's first
    real caller.

    **Retell's docs are silent on what happens to a phone number still bound
    to the agent being deleted — confirmed via the same live WebFetch (no
    mention either way).** This was resolved empirically instead of assumed:
    see `DELETE /agents/{agent_id}`'s own docstring in app/routers/agents.py
    for the real, live-observed vendor behavior and the resulting product
    decision (unbind numbers first, ourselves, before ever calling this
    function) — this function itself makes no assumption about that and
    simply deletes the agent object it's told to.

    Raises `AppError(code="upstream_failed")` on any network error or
    non-2xx/non-404 response — same never-leak-raw-response contract, and
    the same soft-fail-on-already-gone-is-success treatment of a 404, as
    every other delete function in this codebase (`delete_phone_number`,
    `delete_retell_llm`).
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
            resp = await client.delete(f"/delete-agent/{agent_id}")
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell delete-agent request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to delete the agent. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400 and resp.status_code != 404:
        logger.warning(
            "Retell delete-agent returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to delete the agent.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )


async def create_retell_llm_agent(
    settings: Settings,
    *,
    body: CreateAgentRequest,
    voice_id: str,
    languages: list[Language],
    voice_speed: float,
    interruption_sensitivity: float,
    enable_backchannel: bool,
    pronunciation_dictionary: list[PronunciationEntry],
    webhook_url: str | None = None,
    structured_data_fields: list[StructuredDataFieldDefinition] | None = None,
) -> RetellCreateRetellLlmAgentResult:
    """Two real HTTP calls, in order: `create_retell_llm()` first (gets back
    an `llm_id`), then `POST /create-agent` with `response_engine:
    {"type": "retell-llm", "llm_id": ..., "version": 0}` — confirmed exact
    shape via a live WebFetch this session (see this module's docstring).

    **Orphan-cleanup on partial failure**: if create-retell-llm succeeds but
    the create-agent call that follows fails, this function attempts a real
    `delete_retell_llm()` call before re-raising the original create-agent
    error, so a failed agent creation doesn't leave a real, unreferenced LLM
    object sitting on the vendor account. If the cleanup call itself also
    fails, that's logged at ERROR with the orphaned `llm_id` explicit — never
    silently dropped — but does not change what's raised to the caller; the
    original create-agent failure is always what the caller sees.

    `languages` — same normalized non-empty list as `create_agent()` above,
    mapped to the vendor's real `language` field via `_build_vendor_language`
    (single string vs. real array, never the deprecated `"multi"` string —
    see app/models/agent.py's module docstring for the wire-format decision).

    `structured_data_fields` — sent on the `POST /create-agent` call in this
    function (the second of the two calls, NOT on the create-retell-llm call
    that precedes it — see this module's docstring for the confirmed
    agent-vs-LLM-object placement), same `_build_post_call_analysis_data`
    mapping and "only include when non-empty" convention as `create_agent()`
    above.

    `body.states`/`body.starting_state` — the OPPOSITE placement from
    structured_data_fields: sent on the create-retell-llm call (the FIRST of
    the two, this function's first line below), never on the create-agent
    call that follows — see this module's docstring for the confirmed
    LLM-object-only placement, same as transfer_number/custom_tools above.

    `body.welcome_message` — same LLM-object-only placement as `states`/
    `transfer_number`/`custom_tools`, forwarded to `create_retell_llm()`
    unchanged (already `None`/`""`/a real string exactly as validated on
    `CreateAgentRequest` — no further transformation needed here; see
    `create_retell_llm()`'s own docstring for the `is not None` handling
    that actually builds `begin_message` into its request body).

    `body.agent_name` — the OPPOSITE placement from `states`/`welcome_message`
    just above, same agent-object placement as `structured_data_fields`: sent
    on the `POST /create-agent` call in THIS function (the second of the two
    calls), never on the create-retell-llm call that precedes it — see this
    module's docstring, "agent_name" section, for the confirmed
    agent-object placement that makes this field available under both
    response_engine modes.

    `body.live_transcript_enabled` — the SAME agent-object placement as
    `body.agent_name`/`structured_data_fields` just above (NOT the LLM-object
    placement `states`/`welcome_message` have) — sent on the `POST
    /create-agent` call in THIS function via `_build_webhook_events`, never
    on the create-retell-llm call that precedes it. See app/models/agent.py's
    module docstring, "live_transcript_enabled" section, for the full
    confirmed placement (a deliberate correction of this feature's own
    initial LLM-object-only assumption).

    `body.model`/`body.model_temperature` — the two LLM-object fields of the
    ~19-field tuning-knob batch, forwarded to `create_retell_llm()` (the
    FIRST of the two calls), same placement as `states`/`welcome_message`
    above.

    The rest of the ~19-field tuning-knob batch (`voice_model` through
    `handbook_config`) — all AGENT-object fields, built once via
    `_build_agent_tuning_fields()` and merged into `agent_body` below, same
    placement as `agent_name`/`structured_data_fields`/`live_transcript_
    enabled` above. See app/models/agent.py's module docstring, "~19-field
    tuning-knob batch" section, for the full per-field feature description.

    Raises `AppError(code="upstream_failed")` — from either step — same
    never-leak-raw-response contract as every other adapter function.
    """
    llm_id = await create_retell_llm(
        settings,
        general_prompt=body.prompt,
        transfer_number=body.transfer_number,
        transfer_ring_duration_ms=body.transfer_ring_duration_ms,
        transfer_on_hold_music=body.transfer_on_hold_music,
        transfer_show_original_caller_id=body.transfer_show_original_caller_id,
        custom_tools=body.custom_tools,
        states=body.states,
        starting_state=body.starting_state,
        welcome_message=body.welcome_message,
        model=body.model,
        model_temperature=body.model_temperature,
    )

    agent_body: dict[str, Any] = {
        "response_engine": {
            "type": "retell-llm",
            "llm_id": llm_id,
            "version": 0,
        },
        "voice_id": voice_id,
        "language": _build_vendor_language(languages),
        "voice_speed": voice_speed,
        "interruption_sensitivity": interruption_sensitivity,
        "enable_backchannel": enable_backchannel,
        "pronunciation_dictionary": [entry.model_dump() for entry in pronunciation_dictionary],
    }
    if webhook_url is not None:
        agent_body["webhook_url"] = webhook_url
    if structured_data_fields:
        agent_body["post_call_analysis_data"] = _build_post_call_analysis_data(
            structured_data_fields
        )
    if body.agent_name is not None:
        agent_body["agent_name"] = body.agent_name
    webhook_events = _build_webhook_events(live_transcript_enabled=body.live_transcript_enabled)
    if webhook_events is not None:
        agent_body["webhook_events"] = webhook_events
    agent_body.update(
        _build_agent_tuning_fields(
            voice_model=body.voice_model,
            voice_temperature=body.voice_temperature,
            stt_mode=body.stt_mode,
            denoising_mode=body.denoising_mode,
            ambient_sound=body.ambient_sound,
            ambient_sound_volume=body.ambient_sound_volume,
            backchannel_frequency=body.backchannel_frequency,
            backchannel_words=body.backchannel_words,
            responsiveness=body.responsiveness,
            reminder_trigger_ms=body.reminder_trigger_ms,
            reminder_max_count=body.reminder_max_count,
            end_call_after_silence_ms=body.end_call_after_silence_ms,
            max_call_duration_ms=body.max_call_duration_ms,
            begin_message_delay_ms=body.begin_message_delay_ms,
            allow_user_dtmf=body.allow_user_dtmf,
            allow_dtmf_interruption=body.allow_dtmf_interruption,
            data_storage_setting=body.data_storage_setting,
            pii_config=body.pii_config,
            post_call_analysis_model=body.post_call_analysis_model,
            handbook_config=body.handbook_config,
        )
    )

    try:
        data = await _post_create_agent(settings, agent_body)
    except AppError:
        logger.warning(
            "create-agent failed after create-retell-llm succeeded — cleaning up " "orphaned LLM",
            extra={"vendor": VENDOR_NAME, "llm_id": llm_id},
        )
        try:
            await delete_retell_llm(settings, llm_id=llm_id)
        except AppError as cleanup_exc:
            # Never let cleanup failure mask the real error the caller needs
            # to see — log loudly (this LLM is now a real orphan on the
            # vendor account requiring manual attention) and re-raise the
            # ORIGINAL create-agent failure below, unchanged.
            logger.error(
                "Orphaned Retell LLM cleanup also failed — manual cleanup needed",
                extra={
                    "vendor": VENDOR_NAME,
                    "llm_id": llm_id,
                    "cleanup_error_code": cleanup_exc.code,
                },
            )
        raise

    agent_id = data.get("agent_id")
    if not agent_id:
        logger.error(
            "Retell create-agent succeeded but response is missing agent_id",
            extra={"vendor": VENDOR_NAME, "llm_id": llm_id},
        )
        # The LLM exists but we have no agent to attach it to on our side
        # either — same orphan situation as the exception path above.
        try:
            await delete_retell_llm(settings, llm_id=llm_id)
        except AppError as cleanup_exc:
            logger.error(
                "Orphaned Retell LLM cleanup also failed — manual cleanup needed",
                extra={
                    "vendor": VENDOR_NAME,
                    "llm_id": llm_id,
                    "cleanup_error_code": cleanup_exc.code,
                },
            )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor returned an unexpected response while creating the agent.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "llm_id": llm_id},
        )

    return RetellCreateRetellLlmAgentResult(agent_id=agent_id, llm_id=llm_id)


async def update_agent(
    settings: Settings,
    *,
    agent_id: str,
    body: dict[str, Any],
) -> None:
    """Call Retell's real `PATCH /update-agent/{agent_id}`. Confirmed via a
    live WebFetch of Retell's own current docs this session: this is a true
    partial-merge endpoint — a field omitted from `body` keeps its current
    stored value on Retell's side, exactly like every other adapter function
    that hits an update endpoint would want. This function does NOT build
    `body` itself (contrast with `create_agent`/`create_retell_llm_agent`
    above, which build their own request bodies from individual kwargs) —
    the caller (app/routers/agents.py's `update_agent` handler) builds it,
    since which fields to include is itself part of the merge decision
    documented on `UpdateAgentRequest` (app/models/agent.py) and doesn't
    belong duplicated into this adapter layer.

    Deliberately returns nothing: unlike creation, an update has no new
    vendor-side id to hand back — the caller already knows this agent's
    `vendor_ref`, that's how it addressed this call in the first place. The
    only outcome that matters to the caller is "did it succeed," which is
    exactly the raise-on-failure/return-None contract every other void
    adapter function in this codebase already uses (see
    `delete_retell_llm` above).

    Raises `AppError(code="upstream_failed")` on any network error, timeout,
    or non-2xx response — same never-leak-raw-response contract as every
    other adapter function in this codebase.
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
            resp = await client.patch(f"/update-agent/{agent_id}", json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell update-agent request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to update the agent. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell update-agent returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the agent update request.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )


async def update_retell_llm(
    settings: Settings,
    *,
    llm_id: str,
    body: dict[str, Any],
) -> None:
    """Call Retell's real `PATCH /update-retell-llm/{llm_id}`. Confirmed via
    a live WebFetch of Retell's own current docs this session: also a true
    partial-merge endpoint at the FIELD level — a field omitted from `body`
    keeps its current stored value. **This does NOT mean array fields are
    merged at the element level** — seeing this module's docstring is
    required reading before calling this function: if `general_tools` is
    included in `body` at all, it REPLACES the entire array on Retell's
    side, since Retell has no per-element merge/diff mechanism for it. The
    caller (app/routers/agents.py's `update_agent` handler) is responsible
    for reconstructing the full `general_tools` array from the agent's
    current state before calling this function whenever `transfer_number`
    or `custom_tools` changes — see `UpdateAgentRequest`'s docstring
    (app/models/agent.py) for the full reasoning on why that reconstruction
    is required even though this endpoint is itself partial-merge.

    **`states` has the identical whole-array-replace behavior, for the same
    underlying reason** (no vendor-side per-element merge mechanism — see
    this module's docstring, "states/starting_state" section) — if `states`
    is included in `body` at all, it REPLACES the entire array. Unlike
    `general_tools`, the caller sends `states: []` EXPLICITLY (not simply
    omitting the key) whenever an update is meant to collapse a Multi
    Prompt agent back to Single Prompt — omitting the key here means
    "leave the vendor's existing states array untouched" (this endpoint's
    own field-level partial-merge behavior), which is the opposite of what
    "clear the states" needs, so the router must distinguish those two
    intents itself before building `body`.

    Only called for `builtin`-mode agents (the ones with a real `llm_ref` —
    see AgentInDB's docstring); a `custom`-mode agent has no vendor-side LLM
    object to update at all, so the router never calls this function for
    one.

    Deliberately returns nothing, same void-on-success contract as
    `update_agent` above.

    Raises `AppError(code="upstream_failed")` on any network error, timeout,
    or non-2xx response — same never-leak-raw-response contract as every
    other adapter function in this codebase.
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
            resp = await client.patch(f"/update-retell-llm/{llm_id}", json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell update-retell-llm request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to update the conversation brain. "
            "Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell update-retell-llm returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the conversation brain update request.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )


async def _post_create_agent(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """Shared `POST /create-agent` HTTP call + error handling, used by both
    `create_agent()` (custom-llm) and `create_retell_llm_agent()`
    (retell-llm) — only the request body differs between the two modes, the
    transport/error-handling logic is identical, so it's factored out once
    rather than duplicated (per the standards doc's "don't let a function do
    more than one clearly-named job" rule extended to duplicated logic
    across two functions).
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
            resp = await client.post("/create-agent", json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            "Retell create-agent request failed",
            extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="Could not reach the voice vendor to create the agent. Try again shortly.",
            status_code=502,
            log_extra={"vendor": VENDOR_NAME, "error_class": type(exc).__name__},
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "Retell create-agent returned an error",
            extra={"vendor": VENDOR_NAME, "upstream_status": resp.status_code},
        )
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the agent creation request.",
            status_code=502,
            log_extra={
                "vendor": VENDOR_NAME,
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:2000],
            },
        )

    data: dict[str, Any] = resp.json()
    return data
