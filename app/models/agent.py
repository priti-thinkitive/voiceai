"""Agent — a voice agent Platform X creates for one of its own end-clients.

One base prompt, one voice, tuned via a handful of optional Retell-backed
fields (voice_speed, interruption_sensitivity, enable_backchannel,
pronunciation_dictionary) per the Phase 1 scope in
vendor-docs/White-Label-Launch-Plan.html (items 1, 2, 3, 15).

**`language` — single code OR array, wire-format decision, made explicitly
per a new product decision (see app/models/language.py's module docstring
for the sourcing).** Confirmed via a live WebFetch of the voice vendor's own
current `create-agent` OpenAPI schema: the vendor's real `language` field is
a `oneOf` — a single locale code, OR a non-empty array of locale codes for a
genuinely multilingual agent (the vendor auto-detects which language the
caller is speaking from the enabled set), OR the deprecated `"multi"`
shortcut string (still out of scope here, not supported).

- **`CreateAgentRequest.language` is typed `Language | list[Language]`**,
  matching the vendor's own `oneOf` shape exactly on the way in. Pydantic v2's
  smart-mode union tries the scalar `Language` member first for a bare
  string, so **any existing caller sending `"language": "en-US"` keeps
  working completely unchanged** — this was the primary backward-
  compatibility constraint, since `CreateAgentRequest` already shipped with
  single-string semantics and a real integration could already depend on it.
  A `field_validator` on the list form rejects an empty list and caps it at
  `MAX_LANGUAGES = 10` — both 422, both before any vendor call is attempted.
  `MAX_LANGUAGES` is **our own judgment call, not a vendor-documented
  limit** — the vendor's schema places no maximum on the array form at all
  (confirmed the same WebFetch session); 10 was picked as a sane, generous
  cap for a real multilingual-agent use case while still catching an
  obviously-malformed request (e.g. all 63 codes sent by mistake).
- **`AgentInDB.languages`/`AgentPublic.languages` normalize to always
  `list[Language]` (non-empty), regardless of which form the request used.**
  A single-string request becomes a 1-element list internally — chosen over
  keeping a `Language | list[Language]` union on the stored/response shape
  too, because a single consistent internal representation means every piece
  of code that reads a stored agent back out (repository, adapter, any
  future feature) only ever has to handle one shape, not two — exactly the
  "no duplicate fields/no ambiguous shape" spirit of the standards doc's
  database rules, applied to a field's *type* rather than its *presence*.
  The field is renamed `languages` (plural) on `AgentInDB`/`AgentPublic`
  specifically because it is now always a list — keeping the name `language`
  singular while the value is always a list would be misleading on every
  response body. `CreateAgentRequest.language` keeps its original singular
  name and accepts either shape; only the stored/returned shape is
  renamed+normalized. See `retell_agent_adapter.py` for how a 1-element
  `languages` list is sent back to the vendor as a bare string (matching the
  vendor's own "single code is the common case" framing) while a longer list
  is sent as a real JSON array — never the deprecated `"multi"` string.

Backed by Retell's real `/create-agent` endpoint (see
app/services/retell_agent_adapter.py), but that's an implementation detail
Platform X never sees: no vendor name, no Retell field names, no
`vendor_ref`/`llm_ref` on the public shape — see AgentPublic below.

**`response_engine` — the two real modes, closing the Phase 1 warm-transfer
gap.** Prior to this, every agent used one fixed mode (our own future AI
brain, "custom" below) that cannot support tool-based transfer at all under
any circumstance, and has no working conversational ability today since the
AI-brain server it depends on isn't built yet. Platform X can now pick
per-agent between:

  - `BUILTIN` ("the voice vendor's own built-in AI") — works TODAY: the
    voice vendor itself generates conversation responses, so a `builtin`
    agent can hold a real conversation the moment it's created, and it's
    the only mode that supports warm transfer to a human (`transfer_number`
    below). This is now the DEFAULT, deliberately, not `custom` — see
    CreateAgentRequest.response_engine's Field description for the full
    reasoning on why defaulting to the mode that actually works today is
    the correct "simplest correct call for the common case" choice.
    (Internal implementation note, not Swagger-visible: this is Retell's
    real `retell-llm` response_engine type — see
    app/services/retell_agent_adapter.py. The enum's own value is
    deliberately vendor-neutral, per the standards doc's "Swagger-visible
    text/response bodies must never name the vendor" rule — this value
    appears on a live AgentPublic response field, not just doc text, so it
    gets the same treatment as any other public-facing field value.)
  - `CUSTOM` ("our own AI") — VoiceAI's own future AI brain. Cannot hold a
    real conversation yet (the WebSocket server it depends on doesn't
    exist), and even once built, only supports COLD transfer, never warm,
    and only via a live decision our own AI-brain server would have to make
    mid-call — there is no agent-creation-time way to configure it. Kept
    only for forward compatibility with that future server; `transfer_*`
    fields are rejected (422) if set alongside this mode, since they would
    be silently meaningless under it. (Internal implementation note: this is
    Retell's `custom-llm` response_engine type.)

**`custom_tools` — closes the "custom mid-call tools beyond transfer" gap,
same `builtin`-only restriction as `transfer_number`, for the same
underlying reason (no vendor-side LLM/tool-registration mechanism exists
under `custom` mode).** Confirmed via a live WebFetch of the voice
vendor's own current custom-function docs this session: a `custom` entry on
the same `general_tools` array `end_call`/`transfer_call` already use (see
app/services/retell_agent_adapter.py's `_build_general_tools`) lets the
agent call out to an external URL mid-conversation and use the response to
continue the conversation.

**The core architecture decision, made explicitly by the user after being
shown the real mechanism — proxy through us, never register Platform X's
own URL with the vendor directly.** The vendor's real custom-tool mechanism
means the VENDOR ITSELF calls the tool's configured `url` field directly,
mid-call, with a real `X-Retell-Signature` header for the receiver to
verify. If that `url` were Platform X's own server directly, Platform X
would see that header name in the request — a direct vendor-identity leak,
the same class of defect as a leaked `retell_call_id` field or a raw
vendor-domain URL (see backend-dev.md's Feature status section, `GET
/voices`' `preview_audio_url` incident, for the precedent), just via an HTTP
header instead of a response field. So the `url` sent to the vendor
ALWAYS points at OUR OWN new proxy endpoint (`POST
/webhooks/retell/custom-tool`, see app/routers/webhooks.py), built from
`settings.BASE_URL` exactly like the existing inbound/post-call webhook URLs
already are (see app/routers/agents.py). We receive the vendor's real
tool-call webhook, verify its real signature (reusing `_verify_signature`
unchanged), relay to Platform X's OWN registered URL for that specific tool
(`CustomToolDefinition.webhook_url` below), and return Platform X's response
back to the vendor in the vendor's real expected shape. Structurally the
SAME pattern as the existing inbound pre-call webhook relay
(app/services/platform_relay.py) — reused, not reinvented.

**API shape decision: an array field on `CreateAgentRequest`, not a separate
resource/endpoint — same reasoning as `transfer_number`/`pronunciation_
dictionary` already being plain fields rather than sub-resources.** A custom
tool has no independent lifecycle apart from the agent it's attached to (it
is built at agent-creation time, alongside every other `general_tools`
entry, and never queried/managed independently of an agent) — the same
"only split into a separate collection/resource when the data has its own
independent lifecycle" test the standards doc applies to Mongo collections
applies equally here to API shape. `CustomToolDefinition.webhook_url` gets
the identical SSRF-adjacent check already built for `inbound_variables_
webhook_url`/`call_completed_webhook_url` (app/utils/ssrf_guard.py,
extracted from app/routers/platform.py into a shared module for this reuse)
— it is an address our own server will later make a real outbound POST to,
automatically, on Platform X's behalf, mid-call, exactly the same risk class
as those two fields.

**Timeout, VoiceAI's own capped default, deliberately far below the
vendor's real default (2 minutes) — a real design decision, not a detail.**
The vendor's own `timeout_ms` defaults to 120000ms if omitted; a caller
sitting on hold for up to 2 minutes while ONE tool call hangs is a terrible
experience regardless of what a Platform X integrator sets (or forgets to
set). `CustomToolDefinition.timeout_ms` defaults to `10_000` (10s) and is
capped at `30_000` (30s) — see the field's own Field description for the
full reasoning, mirroring `platform_relay.PLATFORM_RELAY_TIMEOUT_SECONDS`'s
"generous but bounded, never anywhere near the vendor's own ceiling"
principle. This is the timeout the VENDOR is told to use when calling OUR
proxy (`url`'s `timeout_ms`); our own relay from the proxy onward
to Platform X's `webhook_url` carries its own separate, stricter internal
timeout (see app/routers/webhooks.py's custom-tool handler) so a slow
Platform X integration can never consume the vendor-facing budget alone —
there is real margin between the two, not one shared number reused twice.

**Storage: embedded array on `AgentInDB`, not its own collection —
justified against the standards doc's own stated collection-splitting
test.** That test asks whether data "has its own independent lifecycle, is
queried independently at scale, or would make the parent document
unboundedly large." A custom tool fails all three: it's created/updated only
as part of an agent write, never queried on its own outside the context of
"build this agent's general_tools array" or "route this specific vendor
tool-call webhook back to a platform" (both already resolve via an agent
lookup — see app/routers/webhooks.py's custom-tool handler for the routing
mechanism), and the realistic count per agent (a handful of external
integrations) is nowhere near large enough to threaten Mongo's 16MB document
limit. Embedding is the correct default for genuinely agent-scoped data with
no independent lifecycle — the same reasoning `pronunciation_dictionary`
already established as a precedent on this exact model.

**`structured_data_fields` — lets Platform X define what facts should be
automatically pulled from every finished call on this agent (e.g. "caller's
name", "appointment time", "was an appointment booked"). Confirmed real via a
live WebFetch of the voice vendor's own current create-agent API reference
this session: an array field directly on the agent-creation request body
(same level as `voice_id`/`response_engine`), where each entry is
`{type, name, description, choices?}` — `type` is one of `string`/`enum`/
`boolean`/`number`, and `choices` (a non-empty array of allowed string
values) is required only when `type == "enum"`, rejected otherwise. The
extracted values land on the finished call's own record (`extracted_data` on
`CallPublic`, see app/models/call.py) and in the call-completed notification
sent to Platform X (see app/services/call_completed_webhook.py) — never
anywhere else.

**Placement decision, load-bearing and worth stating explicitly: this is an
AGENT-object field, NOT a response_engine/LLM-object field — confirmed by
checking the voice vendor's own create-retell-llm API reference in the same
session and finding no such field anywhere on that request body.**
`transfer_number`/`custom_tools` above are restricted to `builtin`
mode only because their underlying vendor mechanism (`general_tools`) lives
on the vendor's separate LLM object, which only exists under that mode.
`structured_data_fields` has no such dependency — it is sent directly on
every `POST /create-agent` call this codebase makes (both
`create_agent()`/`custom-llm` and `create_retell_llm_agent()`/`retell-llm`,
via the shared `_post_create_agent` helper in
app/services/retell_agent_adapter.py), so it is genuinely available under
BOTH `response_engine` modes. **Do NOT add it to
`_reject_transfer_fields_under_custom_mode`'s rejection list below** —
that would incorrectly restrict a feature that has no real reason to be
restricted, "for consistency" with fields that are restricted for an
unrelated, genuine reason. A future maintainer extending that validator
should re-read this paragraph before assuming every optional field on this
model needs the same treatment.

Same storage/API-shape reasoning as `custom_tools` above (no independent
lifecycle, no separate collection/resource) and the same duplicate-name
validation pattern (`_reject_duplicate_structured_data_field_names`, mirrors
`_reject_duplicate_tool_names`) — a field name must be unique within one
agent, since it becomes a dict key on the extracted-data object Platform X
reads back.

**`states`/`starting_state` — Single Prompt vs Multi Prompt, closing a real,
confirmed gap found via a feature-by-feature audit against a sibling
product.** Until now every agent was what the voice vendor's own dashboard
calls "Single Prompt": one flat `general_prompt` the conversation brain
follows for the whole call. The vendor's real `retell-llm` response engine
also supports "Multi Prompt": the SAME agent internally split into named
`states` (e.g. a root/triage state plus department states like Billing,
Scheduling) that the conversation brain automatically routes between
mid-call, based on natural-language `edges` attached to each state. This is
a genuinely different mechanism from `custom_tools`/`structured_data_fields`
above — it is not a tool the brain calls, it is a restructuring of the
brain's OWN prompt/routing, confirmed via a live check of the vendor's real
`create-retell-llm`/`update-retell-llm` request schemas this session:

```json
{
  "general_prompt": "...",
  "states": [
    {
      "name": "billing",
      "state_prompt": "You are now handling billing questions...",
      "edges": [
        {"destination_state_name": "triage", "description": "When the caller's billing question is resolved or they want something else."}
      ],
      "tools": []
    }
  ],
  "starting_state": "triage"
}
```

- `AgentState.state_prompt` is APPENDED to `general_prompt` at conversation
  time, not a replacement of it (confirmed: the vendor's own docs describe
  it as "will be appended to the system prompt of LLM") — `general_prompt`
  (VoiceAI's `prompt`) stays the shared, always-active base instructions
  every state inherits; a state's own `state_prompt` only adds the
  state-specific behavior on top. This is exactly why Single Prompt vs
  Multi Prompt is additive, not a fork: an agent with `states=[]` still has
  a genuinely complete, working prompt (`general_prompt` alone), and adding
  `states` later never invalidates or replaces it.
- `AgentState.tools` reuses the SAME per-tool shape `custom_tools` already
  validates (`CustomToolDefinition`) rather than inventing a parallel model —
  the vendor's own real `states[].tools` array is documented as the exact
  same shape as the top-level `general_tools` array this codebase already
  builds via `_build_general_tools`/`CustomToolDefinition`, so a per-state
  tool is genuinely the same kind of object, just scoped to fire only while
  that state is active. Only the `custom` tool type is supported here, same
  narrow scope as `custom_tools` above (see this module's `general_tools
  scope` note in retell_agent_adapter.py) — no new tool types are introduced
  by this feature.
- **Validation, same "loud, not silent" discipline as every other field on
  this model:** (a) `starting_state` is REQUIRED and must name one of this
  request's own `states[].name` values whenever `states` is non-empty — an
  agent with states but no valid entry point is meaningless and the vendor's
  own docs confirm `starting_state` is "required if states is not empty";
  (b) every `edges[].destination_state_name`, across every state, must
  resolve to either another state's own `name` in this same request OR to
  `starting_state` itself (routing back to the root is always valid, even
  though the root technically isn't itself one of the "department" states in
  a typical shape) — a dangling reference would silently fail on the
  vendor's own side or produce an agent that can route into a dead end, so
  it's rejected here with a clear 422 instead; (c) `AgentState.name` values
  must be unique within one agent (`_reject_duplicate_state_names`, mirrors
  `_reject_duplicate_tool_names` exactly — a state name is how edges/
  `starting_state` reference it, so a duplicate is genuinely ambiguous, not
  cosmetic); (d) `states`/`starting_state` are restricted to `builtin` mode
  only, extending the SAME `_reject_transfer_fields_under_custom_mode`
  validator `transfer_number`/`custom_tools` already use rather than
  duplicating the mechanism — confirmed via the same live schema check that
  `states`/`starting_state` exist ONLY on `create-retell-llm`/
  `update-retell-llm` (the vendor's separate LLM object, which only exists
  under `builtin` mode), with no equivalent concept anywhere under `custom`
  mode's API surface at all (unlike `structured_data_fields`, which
  genuinely is agent-object-level and available under both modes — see that
  field's own placement note above; `states` has no such independence, it is
  exactly as LLM-object-coupled as `general_tools` is).
- **`MAX_STATES = 15`, our own judgment call, not a vendor-documented
  limit** — the vendor's own schema places no maximum on the `states` array
  (confirmed the same session). Picked the same way `MAX_CUSTOM_TOOLS`/
  `MAX_STRUCTURED_DATA_FIELDS` were: generous enough for a real multi-
  department agent (a handful of departments plus a root is the realistic
  shape this feature targets — eCareVoiceAI's own live "Agent Team" usage
  tops out well below this), while still catching an obviously-malformed
  request (e.g. hundreds of auto-generated states sent by mistake) before it
  ever reaches the vendor.
- **General-purpose, not eCareVoiceAI-shaped.** This mechanism is built for
  ANY future customer's own department/routing structure — Platform X names
  its own states, writes its own `state_prompt`/edge `description` text, and
  decides its own `starting_state`; nothing here assumes a "triage" root or
  any particular department naming, even though eCareVoiceAI's own
  production usage of this exact vendor mechanism (states/edges/
  starting_state) was the reference used to confirm the shape actually
  works end-to-end.
- **`AgentPublic.multi_prompt_enabled`** — a derived `bool` (`len(states) >
  0`), same "at-a-glance answer" reasoning as `transfer_enabled` above:
  Platform X can already tell whether states are configured by checking
  `len(states) > 0` themselves, but `transfer_enabled` already established
  the precedent that this codebase surfaces that specific yes/no answer
  explicitly rather than making every caller re-derive it, so the same
  convenience is extended here for consistency across the two "is this
  optional capability actually on" questions this model answers.
- **Update semantics: whole-array-replace on `states`, same as
  `custom_tools`/`structured_data_fields` — NOT a true partial-merge field.**
  Confirmed via the same live schema check of `update-retell-llm`: while the
  endpoint itself is field-level partial-merge (an omitted field keeps its
  current value), there is no vendor-side mechanism to add/remove/rename a
  single state or edge independently — including `states` in an update body
  at all replaces the ENTIRE array, exactly the same "field-level
  partial-merge, but no element-level merge for this specific array" gap
  `general_tools` already has (see `UpdateAgentRequest`'s own docstring
  below for the full reasoning already established for that array, which
  applies identically here). Sending `states: []` on an update is the
  documented, correct way to collapse a Multi Prompt agent back to Single
  Prompt — confirmed real, not inferred.

**`welcome_message` — VoiceAI's own name for the voice vendor's real
`begin_message` field on `create-retell-llm`/`update-retell-llm`, closing a
real, confirmed customer gap.** Confirmed via a live WebFetch of the vendor's
own current `create-retell-llm` API reference this session: `begin_message`
(string) is "the first utterance said by the agent in the call." Three real,
documented vendor behaviors, all supported here:

  1. Omitted entirely -> the vendor's own conversation brain improvises an
     opening line from `general_prompt` on the fly. This is VoiceAI's
     EXISTING default behavior (see `create_retell_llm()` in
     app/services/retell_agent_adapter.py, which has deliberately never sent
     `begin_message` until this feature) and stays completely unchanged for
     any caller who doesn't use `welcome_message` at all.
  2. A specific non-empty string -> the agent says that exact string,
     verbatim, every single call — no improvisation. This is the real gap
     being closed: some businesses (compliance-driven, brand-consistency-
     driven, or a required disclosure on e.g. a healthcare line) need a
     word-for-word identical greeting every time, which "the AI improvises
     something reasonable" can never guarantee.
  3. An explicit empty string `""` -> the agent says nothing and waits for
     the caller to speak first — also a real, documented vendor behavior,
     distinct from both of the above.

**The three-state distinction is carried entirely by Pydantic's own
`str | None` semantics on the wire, deliberately, rather than inventing a
separate flag** — `None` (key omitted, or explicit JSON `null`) means "don't
send `begin_message` at all"; `""` (empty string) means "send
`begin_message: \"\"\"` "; any other string is sent verbatim. **This is a
genuine footgun for a future maintainer to get wrong**: `""` is falsy in
Python, so a careless `if welcome_message:` check in the adapter layer would
silently collapse case 3 into case 1 — sending nothing instead of sending an
explicit empty string — which would be a real, silent behavior change (an
agent configured to wait silently would instead start improvising a
greeting). The adapter functions that build the vendor request body
(`create_retell_llm()`/`update_retell_llm()` in
app/services/retell_agent_adapter.py) use an explicit `is not None` check for
exactly this reason — see those functions' own docstrings.

**Placement/mode-restriction: LLM-object-only, same as `transfer_number`/
`custom_tools`/`states`, for the identical underlying reason.** Confirmed via
the same live WebFetch: `begin_message` lives only on
`create-retell-llm`/`update-retell-llm` (the vendor's separate LLM object,
which only exists under `builtin` mode) — there is no equivalent concept
anywhere on `create-agent`/`update-agent` or anywhere in `custom` mode's API
surface. A `custom`-mode agent has no vendor-side LLM object to attach a
`begin_message` to; under that mode, a real greeting (if any) would have to
be VoiceAI's own future AI-brain server's responsibility, not something
forwarded to the vendor. So `welcome_message` is rejected with 422 if set
alongside `response_engine='custom'`, extending the SAME shared
`_reject_transfer_fields_under_custom_mode`/`_reject_transfer_fields_under_update`
validators `transfer_number`/`custom_tools`/`states` already use, rather than
adding a new, parallel mechanism.

**Update semantics: same omitted-vs-null-vs-clear-flag pattern as
`transfer_number`, applied identically.** `UpdateAgentRequest.welcome_message`
being `None` is already overloaded to mean "field omitted" at the Pydantic
layer for a partial update (exactly the same ambiguity `transfer_number`
already has), so it alone cannot represent "explicitly clear
`welcome_message` back to the default improvised-greeting behavior." Rather
than invent a new mechanism, this reuses the EXACT SAME established shape:
`clear_welcome_message: bool = False`, mirroring `clear_transfer_number`
field-for-field (same "ignored if a new value is also set in the same
request" precedence, same reasoning).

**A genuinely subtle wrinkle, confirmed live against a real vendor account
and gotten wrong on a first pass: clearing on the WIRE requires sending
`begin_message: null` EXPLICITLY, never simply omitting the key.**
`update-retell-llm` is a true field-level partial-merge endpoint (see
retell_agent_adapter.py's `update_retell_llm` docstring) — an OMITTED
`begin_message` key on an update leaves Retell's currently-stored value
untouched, exactly the same "omitted means don't touch" trap `states`
already had to work around with an explicit `states: []` (see this
module's docstring, "states/starting_state" section) applied here to a
scalar instead of an array. So `clear_welcome_message=true` does NOT
resend the update with `welcome_message`/`begin_message` left out — the
router (app/routers/agents.py's `update_agent`) always includes
`begin_message` explicitly whenever the LLM object is touched at all,
sending Python `None` (JSON `null`) specifically when the merged, intended
`welcome_message` is `None` — confirmed live this session: a real
`PATCH /update-retell-llm/{llm_id}` with `{"begin_message": null}` genuinely
removes the key from Retell's own stored object on the next GET, restoring
the improvised-greeting default. This is the one and only way to move an
agent that currently has a `welcome_message` (real string or `""`) back to
improvised-greeting behavior via PATCH.

**Persistence: added to `AgentInDB`/`AgentPublic` like every other tuning
field, for the same "PATCH merge needs the CURRENT value" reason
`transfer_ring_duration_ms`/`transfer_on_hold_music`/
`transfer_show_original_caller_id` became persisted fields.** A PATCH that
touches `prompt`/`transfer_number`/`custom_tools`/`states` (any field that
triggers an `update-retell-llm` call) rebuilds that call's ENTIRE body from
scratch (see `update_retell_llm()`'s own docstring) — omitting
`welcome_message` from that rebuild whenever the caller's PATCH didn't
mention it would otherwise silently reset an existing custom greeting back to
improvised, exactly the unintended-side-effect bug those three fields'
comment on `AgentInDB` already documents for the analogous case.

**`agent_name` — VoiceAI's own name for the voice vendor's real `agent_name`
field on `create-agent`/`update-agent`, closing a real, confirmed gap.**
Confirmed via a live WebFetch of the vendor's own current `create-agent` and
`update-agent` API references this session: `agent_name` (string, nullable,
example `"Jarvis"`) is "the name of the agent. Only used for your own
reference." Purely an internal/dashboard label, never surfaced to anyone
Platform X's agent talks to. Before this field existed, every agent Platform
X created showed up on the vendor's own dashboard/`get-agent` responses with
a generic, auto-filled, type-based label (e.g. "Single Prompt Agent"/"Custom
LLM Agent") — indistinguishable from every other agent Platform X has, since
nothing customer-chosen was ever sent. A real Platform X customer running
several agents (e.g. Front Desk, After Hours, Billing Overflow) had no way to
tell them apart on the vendor's own admin surface, only on ours. Kept as a
simple optional `str | None` label, no three-state complexity the way
`welcome_message` needs — the vendor's own docs describe no special meaning
for an empty string here (unlike `begin_message`'s documented "wait
silently" behavior), so this field only ever needs two states: unset, or a
real chosen name.

**Placement decision, load-bearing and worth stating explicitly, THE ONE
GENUINE DIFFERENCE FROM `welcome_message`/`transfer_number`/`custom_tools`/
`states` above: this is an AGENT-object field, NOT a response_engine/LLM-
object field — confirmed by checking the vendor's own `create-agent`/
`update-agent` API references, where `agent_name` sits at the same level as
`voice_id`/`response_engine`/`language`, with no equivalent field anywhere
on `create-retell-llm`/`update-retell-llm`.** Every agent this codebase
creates — `builtin` mode (retell_llm) or `custom` mode (custom_llm) — is
still, underneath, a real vendor `agent` object with a real `agent_id`; only
`builtin` mode ALSO has a second, separate LLM object attached to it. The
four fields restricted to `builtin`-only above are restricted because their
underlying vendor mechanism (`general_tools`, `begin_message`, `states`) all
live on that separate LLM object, which simply does not exist under `custom`
mode. `agent_name` has no such dependency — it is sent directly on every
`POST /create-agent` call this codebase makes (both `create_agent()`/
`custom-llm` and `create_retell_llm_agent()`/`retell-llm`, via the shared
`_post_create_agent` helper in app/services/retell_agent_adapter.py, exactly
the same dual-path plumbing `structured_data_fields` already uses), so it is
genuinely available under BOTH `response_engine` modes. **Do NOT add
`agent_name` to `_reject_transfer_fields_under_custom_mode`'s rejection list
below, and do NOT add it to `_reject_transfer_fields_under_update` in
app/routers/agents.py** — that would incorrectly restrict a feature that has
no real reason to be restricted, "for consistency" with fields that are
restricted for an unrelated, genuine reason (see `structured_data_fields`'
own placement note above for the identical precedent and the identical
warning to a future maintainer — `agent_name` is structurally the same kind
of exception, for the same underlying reason: agent-object field, not
LLM-object field).

**Type/limits: `str | None`, default `None`, no maximum length imposed by
this codebase** — the same live WebFetch that confirmed the field's shape
found no documented `max_length` on the vendor's own `agent_name` schema, so
none is invented here, consistent with how `MAX_LANGUAGES`/`MAX_CUSTOM_TOOLS`
/`MAX_STRUCTURED_DATA_FIELDS`/`MAX_STATES` are only ever added when this
codebase's own judgment calls for a sane cap, never as a reflexive default —
an agent's own reference label is exactly the kind of low-risk, low-volume
field that doesn't need one.

**Update semantics: same omitted-vs-null-vs-clear-flag pattern as
`transfer_number`/`welcome_message`, applied identically.**
`UpdateAgentRequest.agent_name` being `None` is already overloaded to mean
"field omitted" at the Pydantic layer for a partial update, so it alone
cannot represent "explicitly clear `agent_name` back to unset." Rather than
invent a new mechanism, this reuses the EXACT SAME established shape:
`clear_agent_name: bool = False`, mirroring `clear_transfer_number`/
`clear_welcome_message` field-for-field (same "ignored if a new value is
also set in the same request" precedence, same reasoning). Unlike
`welcome_message`, there is no "omitted vs. explicit null on the wire"
wrinkle to worry about on the Retell side here — `agent_name` is a genuine
field-level partial-merge field on both `update-agent` (confirmed the same
session), so the router only needs to include `agent_name` in the
`update-agent` body when this PATCH actually touches it (a new value or
`clear_agent_name=true`, sending explicit `None` in that clear case), and can
otherwise leave the key out entirely — there is no separate "omitted leaves
it untouched but we still needed to explicitly re-send null to actually
clear" trap the way `begin_message` has, since this codebase already handles
`agent_name` explicitly rather than relying on omission either way.

**Persistence: added to `AgentInDB`/`AgentPublic` like every other field**,
for the same "PATCH merge needs the CURRENT value" reason `welcome_message`/
the three transfer-tuning fields became persisted fields — a PATCH that
touches `agent_name` needs the agent's own current value to correctly no-op
when this PATCH doesn't mention it.

**`live_transcript_enabled` — opts an agent into the voice vendor's real
`transcript_updated` webhook event, closing the "live transcript" gap: until
now, Platform X could only ever learn what was said on a call AFTER it ended
(the post-call `call_ended`/`call_analyzed` events, re-hosted transcript).**
See app/routers/webhooks.py's module docstring, "POST
/webhooks/retell/transcript-updated" section, for the full confirmed
mechanism (fires many times per call, one per conversational turn, plus a
final update at call end) and app/routers/calls.py's `WS
/calls/{call_id}/live-transcript` for how Platform X actually consumes it in
real time.

**Placement decision, load-bearing and confirmed via a live WebFetch of the
vendor's own current `create-agent`/`update-agent` API references this
session — deliberately CONTRADICTING this task's own initial brief, which
assumed (by analogy with `welcome_message`/`custom_tools`/`states`) that this
would be an LLM-object-only, `builtin`-only field. It is not.** The real
vendor mechanism is a `webhook_events` array field (allowed values include
`call_started`/`call_ended`/`call_analyzed`/`transcript_updated`/four
`transfer_*` values — confirmed via the same live WebFetch), and that field
lives directly on the AGENT object (`create-agent`/`update-agent`), at the
same level as `voice_id`/`agent_name`/`structured_data_fields` — NOT on the
separate LLM object (`create-retell-llm`/`update-retell-llm`) the way
`general_tools`/`begin_message`/`states` do. This makes
`live_transcript_enabled` structurally identical to `agent_name`/
`structured_data_fields` above, not to `welcome_message`/`transfer_number`/
`custom_tools`/`states`: it is genuinely available and meaningful under BOTH
`response_engine` modes (a `custom`-mode agent still has a real vendor-side
agent object — just no LLM object — and `webhook_events` is a property of
THAT object), and it is sent on every `POST /create-agent` call this
codebase makes, in both `create_agent()`/`custom-llm` and
`create_retell_llm_agent()`/`retell-llm` (via the shared `_post_create_agent`
helper in app/services/retell_agent_adapter.py). **Do NOT add
`live_transcript_enabled` to `_reject_transfer_fields_under_custom_mode`
below, and do NOT add it to `_reject_transfer_fields_under_update` in
app/routers/agents.py** — same warning already given for
`agent_name`/`structured_data_fields`: restricting it "for consistency"
with fields that are restricted for the unrelated, genuine reason of
depending on the LLM object would be incorrect here.

**Still opt-in (`= False` default), even though it is NOT mode-restricted —
for a completely different reason than the mode question.** Most agents
don't need this: subscribing to `transcript_updated` means Retell delivers a
real HTTP webhook to this codebase for EVERY conversational turn of EVERY
call on that agent, all day — genuinely extra inbound traffic and, when a
Platform X client is actually connected, extra WebSocket connection-
management overhead, for a capability (watching a call happen live, turn by
turn) most integrations never need. Defaulting this to `True` would silently
opt every agent into that extra traffic the moment this field existed, for
no benefit to an integrator who never asked for it — exactly the kind of
"quietly change behavior for everyone" mistake the standards doc's opt-in
defaults elsewhere in this module (e.g. `welcome_message`'s unset default
preserving the original improvised-greeting behavior) are already careful to
avoid.

**Vendor call shape**: `webhook_events` is sent as `["call_started",
"call_ended", "call_analyzed", "transcript_updated"]` when
`live_transcript_enabled=true` — the vendor's own documented DEFAULT set
PLUS `transcript_updated`, never `transcript_updated` alone. Sending only
`["transcript_updated"]` would silently stop delivering
`call_started`/`call_ended`/`call_analyzed` too (confirmed via the same live
WebFetch: `webhook_events` REPLACES the default set entirely once present,
it does not add to it), which would break the existing post-call re-hosting
flow this codebase already depends on for every agent — a real, easy-to-miss
regression this explicit union avoids. When `live_transcript_enabled=false`
(the default), `webhook_events` is omitted from the vendor request entirely,
preserving today's exact existing behavior (Retell's own default set,
implicitly) for every agent that doesn't opt in — see
app/services/retell_agent_adapter.py's `_build_webhook_events` for the exact
mapping.

**Update semantics: a plain optional bool, NOT the omitted-vs-null-vs-clear-
flag pattern `transfer_number`/`welcome_message`/`agent_name` use.** Those
three need a three/four-state mechanism because `None` is genuinely ambiguous
for them (a real, meaningful value can itself be `None`/`""`). A bool has no
such ambiguity in the same way — `UpdateAgentRequest.live_transcript_enabled`
being `None` unambiguously means "omitted, leave unchanged" (matching every
plain scalar tuning field on that model, e.g. `voice_speed`/
`enable_backchannel`), and an explicit `true`/`false` always means exactly
that. No `clear_live_transcript_enabled` flag is needed or provided.

**Persistence: added to `AgentInDB`/`AgentPublic` like every other field**,
same "PATCH merge needs the CURRENT value" reason as every other field on
this model.

**The ~19-field tuning-knob batch below — `model`, `model_temperature`,
`voice_model`, `voice_temperature`, `stt_mode`, `denoising_mode`,
`ambient_sound`, `ambient_sound_volume`, `backchannel_frequency`,
`backchannel_words`, `responsiveness`, `reminder_trigger_ms`,
`reminder_max_count`, `end_call_after_silence_ms`, `max_call_duration_ms`,
`begin_message_delay_ms`, `allow_user_dtmf`, `allow_dtmf_interruption`,
`data_storage_setting`, `pii_config`, `post_call_analysis_model`,
`handbook_config` — closes a large, real gap found via a full audit against
the voice vendor's own current `create-agent`/`create-retell-llm` API
references: every one of these is a genuine, documented, currently-live
vendor field that this codebase never exposed, meaning every agent silently
got the vendor's own bare defaults with zero visibility or control. All
confirmed real and current via a fresh live WebFetch of
`docs.retellai.com/api-references/create-agent`,
`.../create-retell-llm`, `.../update-agent`, and `.../update-retell-llm`
this session — not carried over from an earlier, possibly-stale audit.
`kb_config`/knowledge-base support is explicitly OUT of scope (a separate,
not-yet-researched document-upload flow) and nothing here touches it.

**Placement — two on the LLM object, the rest on the agent object,
confirmed the same way `general_tools`/`begin_message`/`states` vs.
`agent_name`/`structured_data_fields` were confirmed above.** `model`/
`model_temperature` are real, documented fields on `create-retell-llm`/
`update-retell-llm` only (the LLM object `general_prompt`/`general_tools`/
`begin_message`/`states` already live on) — same `builtin`-only
availability as those fields, for the identical underlying reason (no LLM
object exists under `custom` mode). Every other field in this batch is a
real, documented field on `create-agent`/`update-agent` (the agent object
`agent_name`/`structured_data_fields`/`live_transcript_enabled` already live
on) — genuinely available under BOTH `response_engine` modes, so none of
them are added to `_reject_transfer_fields_under_custom_mode` below or to
`_reject_transfer_fields_under_update` in app/routers/agents.py.

**Every default below matches the vendor's own documented default exactly**
(confirmed via the same fresh WebFetch), so an agent that sets none of these
behaves EXACTLY as it did before this batch — no accidental behavior change
for any existing integration. Field descriptions here are deliberately
terser than e.g. `transfer_number`'s — most of these are simple, self-
explanatory tuning knobs (a sentence is enough), reserving the long-form
"explain the whole feature" treatment for genuinely complex mechanisms.

- **`model`/`model_temperature`** — which text LLM powers the conversation
  and how random its responses are. `model` is `str | None` (not a closed
  Python enum): the vendor adds new model names over time (its own schema
  already lists `gpt-4.1`, `gpt-4.1-mini`, `gpt-5`, `claude-4.5-sonnet`,
  `gemini-3.0-flash`, and many more as of this session, confirmed via the
  same WebFetch), and hardcoding today's exact list into a Python `StrEnum`
  would make this codebase reject a real, brand-new vendor-supported model
  name the day the vendor ships it — a worse failure mode than accepting a
  free-form string and letting the vendor's own API be the source of truth
  for whether a given model name is currently valid. `model_temperature`
  keeps this codebase's own pre-existing `_DEFAULT_MODEL_TEMPERATURE = 0.0`
  constant as the default, now genuinely configurable rather than hardcoded.
  Previously `create_retell_llm()` deliberately omitted `model` entirely,
  reasoning that the vendor's own real, documented default (`gpt-4.1`)
  meant this adapter didn't need to invent or hardcode a choice — that
  reasoning was correct as far as it went, but stopped short: "the vendor
  has a sane default" is a reason not to REQUIRE a value, not a reason to
  never let a caller choose one. This batch closes that gap for `model`
  and, for the identical reason, for its sibling `post_call_analysis_model`
  below (see that field's own note — the old "deliberately omitted" language
  on that field is now stale and has been rewritten, not left standing next
  to code that now sends it).
- **`voice_model`/`voice_temperature`** — separate from the existing
  `voice_id` (WHICH voice) and `voice_speed` (playback rate): `voice_model`
  picks the TTS engine/quality tier (e.g. `eleven_flash_v2_5`, `sonic-3`),
  `voice_temperature` controls how much expressive variation that engine
  applies. `voice_model` is likewise `str | None`, not a closed enum, for
  the identical "vendor adds new engines over time" reasoning as `model`
  above.
- **`stt_mode`/`denoising_mode`/`ambient_sound`/`ambient_sound_volume`** —
  speech-recognition and background-audio tuning. `stt_mode` and
  `denoising_mode` ARE closed, real `StrEnum`s (confirmed short, stable
  vendor-documented lists — `fast`/`accurate`/`custom` and
  `no-denoise`/`noise-cancellation`/`noise-and-background-speech-
  cancellation` respectively), unlike `model`/`voice_model` above, since
  these are structural mode choices, not an open-ended, frequently-growing
  model catalog. `custom_stt_config` (only meaningful alongside
  `stt_mode='custom'`) is a separate vendor field NOT built here — out of
  this batch's scope, same "don't gold-plate beyond the confirmed field
  list" discipline as everywhere else in this codebase; setting
  `stt_mode='custom'` without it is forwarded to the vendor as-is and is the
  vendor's own call to accept or reject. `ambient_sound` IS a closed enum
  too (a fixed, vendor-documented preset list — `coffee-shop`,
  `convention-hall`, `summer-outdoor`, `mountain-outdoor`, `static-noise`,
  `call-center` — plus `None`/off).
- **`backchannel_frequency`/`backchannel_words`** — only meaningful
  alongside the EXISTING `enable_backchannel=true` (this codebase's own
  pre-existing field); both are forwarded to the vendor unconditionally
  regardless of `enable_backchannel`'s value (the vendor's own docs
  attach no such conditional-inclusion rule to either — they are simply
  inert whenever `enable_backchannel=false`), matching how
  `transfer_ring_duration_ms`/`transfer_on_hold_music` are already
  unconditionally sent regardless of whether `transfer_number` is set.
- **`responsiveness`** — how quickly the agent replies after the caller
  stops talking, a genuinely different knob from the existing
  `interruption_sensitivity` (how readily it yields the floor while the
  caller is STILL talking).
- **`reminder_trigger_ms`/`reminder_max_count`** — how long the agent waits
  in silence before proactively prompting the caller again, and how many
  times it will do so per call.
- **`end_call_after_silence_ms`/`max_call_duration_ms`** — two independent
  hangup safety nets: the first ends a call after a stretch of pure silence
  (default 600000ms/10min, vendor-documented minimum 10000ms); the second
  caps a call's TOTAL duration regardless of activity (default
  3600000ms/1hr, vendor-documented range 60000-7200000ms). Both were
  already flagged as tracked Phase 1 "do first" gaps from an earlier audit
  this session — this batch is what actually closes them, not a new
  discovery.
- **`begin_message_delay_ms`** — a pause, in milliseconds, before the agent
  speaks its opening line (whether improvised or a configured
  `welcome_message`) — lets ringback/connection audio settle before the
  agent starts talking. Range 0-5000, default 0 (no delay, today's
  unchanged behavior).
- **`allow_user_dtmf`/`allow_dtmf_interruption`** — whether the caller's own
  keypad (DTMF) input is accepted at all, and whether pressing a key
  interrupts the agent mid-sentence the same way speaking over it does.
  `user_dtmf_options` (digit_limit/termination_key/timeout_ms, only
  meaningful alongside `allow_user_dtmf=true`) is a separate vendor field
  NOT built here — out of this batch's confirmed scope, same reasoning as
  `custom_stt_config` above.
- **`data_storage_setting`** — a real `StrEnum` (`everything` [default],
  `everything_except_pii`, `basic_attributes_only`) controlling how much of
  a call's own data the vendor retains after the call. Confirmed via the
  same WebFetch that the OLDER `opt_out_sensitive_data_storage` boolean
  field (referenced in earlier session research) is superseded/removed on
  the vendor's current schema — this codebase never used that old field
  name and does not start now; `data_storage_setting` is the real, current
  mechanism.
- **`pii_config`** — a real nested object, `{mode, categories}`. `mode` is a
  `StrEnum` with exactly ONE real vendor-documented value today
  (`post_call` — PII is redacted from stored data after the call ends, not
  live-masked during it); built as a genuine enum anyway, per this batch's
  own "build real enums, don't guess a plausible list" discipline, so a
  future vendor-added mode is a one-line addition here rather than a design
  change. `categories` is a non-empty-when-`pii_config`-is-set list of a
  real, confirmed 13-value `StrEnum` (`person_name`, `address`, `email`,
  `phone_number`, `ssn`, `passport`, `driver_license`, `credit_card`,
  `bank_account`, `password`, `pin`, `medical_id`, `date_of_birth`,
  `customer_account_number`). `pii_config` itself defaults to `None`
  (omitted — the vendor's own default is no PII redaction configured at
  all); when provided, `categories` must be non-empty (an empty category
  list configures redaction of nothing, which is indistinguishable from not
  configuring `pii_config` at all, so it's rejected as a likely mistake
  with a clear 422 rather than silently forwarded).
- **`post_call_analysis_model`** — see `model`'s own note above for why this
  field's addition specifically REVISES a previous, now-outdated design
  decision: this codebase's own `create_retell_llm_agent()` docstring in
  retell_agent_adapter.py used to say `post_call_analysis_model` was
  "deliberately OMITTED... this adapter doesn't need to invent or hardcode a
  model choice," reasoning from the vendor's own real, documented default
  (`gpt-4.1`) applying when unset. That reasoning is still TRUE, but the
  conclusion it was used to support has changed: `model` (the sibling,
  main-conversation field) just went through the identical reasoning and
  was promoted from "correctly omitted" to "real gap, expose it," because a
  real, customer-relevant choice existing with a sane default is a reason to
  make it CONFIGURABLE, not a reason to hide it. `post_call_analysis_model`
  gets the same treatment now, for consistency — the adapter's own
  docstring is updated to say so explicitly rather than leaving stale
  "we deliberately don't do this" text standing next to code that now does.
  Same `str | None`, not a closed enum, as `model`/`voice_model` above, and
  same reasoning (vendor's own model catalog grows over time).
- **`handbook_config`** — a real nested object of ten independent, optional
  booleans (`default_personality`, `conversational_personality`,
  `natural_filler_words`, `high_empathy`, `echo_verification`,
  `nato_phonetic_alphabet`, `speech_normalization`, `smart_matching`,
  `ai_disclosure`, `scope_boundaries`), each toggling one vendor-side
  conversation-quality behavior. **Note on the field name the user
  originally typed:** the original ask referenced a field called
  `normalize_for_speech` — that name does NOT exist on the vendor's real
  schema at all, on this object or anywhere else; the real, current field is
  `speech_normalization`, one of ten booleans NESTED inside `handbook_config`
  (confirmed via the same fresh WebFetch this session, matching earlier
  session research). This codebase builds the full real object (all ten
  booleans, each individually optional/`None`-meaning-"vendor default",
  matching this batch's own "expose what's genuinely there" discipline)
  rather than only wiring the one boolean the user happened to reference —
  the other nine are equally real, equally documented, and equally cheap to
  expose once the object itself is being modeled at all.

**Update semantics for this whole batch — plain omitted-means-unchanged for
every scalar, matching `voice_speed`'s existing precedent; no clear-flag
mechanism needed anywhere in this batch.** None of these ~19 fields has
`welcome_message`'s "empty string is a real, different value" wrinkle or
`transfer_number`'s "explicit null must mean something different from
omitted" wrinkle — every one of them is either a plain scalar (a real value
means change it, `None`/omitted means leave it alone — including `model`/
`voice_model`/`post_call_analysis_model`'s `str | None` type, where `None`
on `UpdateAgentRequest` means "omitted," not "explicitly clear back to the
vendor's own default"; there is no vendor-documented way to explicitly
un-set these three back to their bare default via PATCH once set, short of
creating a new agent, and none of this batch's use cases need that) or one
of the two array-touching fields confirmed below to need whole-object-
replace handling on update, same as `custom_tools`/`states` already do.
**Confirmed via the same fresh WebFetch of `update-agent`/`update-retell-llm`
this session: BOTH endpoints remain field-level partial-merge for every
field in this batch** — including `backchannel_words` (a plain array) and
`pii_config` (an object whose own `categories` is an array) — omitting the
key leaves the vendor's current value untouched, and including the key
sends a complete replacement value for that key, exactly like every other
optional field already on `UpdateAgentRequest` (e.g.
`pronunciation_dictionary`), NOT like `general_tools`/`states`' deeper
"the array is only ONE PIECE of a shared vendor-side mechanism this
codebase itself builds by combining several of our own fields together"
problem. `backchannel_words`/`pii_config` are each independently, directly
settable vendor fields with no such cross-field-assembly step on our side —
there is nothing here for this codebase's own adapter layer to merge or
reconstruct beyond simply including or omitting the key, so no
special-case handling analogous to `_build_general_tools`/`_build_states`
is needed for either.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.language import Language

# Our own judgment call, not a vendor-documented limit — the voice vendor's
# real `language` array form has no documented maximum size (confirmed via
# live WebFetch of its create-agent OpenAPI schema). Chosen as a generous
# cap for a real multilingual-agent use case while still catching an
# obviously-malformed request. See this module's docstring for the full
# language wire-format reasoning.
MAX_LANGUAGES = 10

# See this module's docstring, "Timeout" section, for the full reasoning —
# deliberately far below the voice vendor's own 120000ms (2 minute) default.
CUSTOM_TOOL_DEFAULT_TIMEOUT_MS = 10_000
CUSTOM_TOOL_MIN_TIMEOUT_MS = 1_000
CUSTOM_TOOL_MAX_TIMEOUT_MS = 30_000

# Our own judgment call, not a vendor-documented limit — a sane cap on how
# many custom tools one agent can carry, catching an obviously-malformed
# request the same way MAX_LANGUAGES does for the language array.
MAX_CUSTOM_TOOLS = 20

# Our own judgment call, not a vendor-documented limit — the voice vendor
# places no documented maximum on the post-call-analysis array (confirmed via
# the same live WebFetch session that confirmed the field's shape). Picked
# for the same reason MAX_CUSTOM_TOOLS/MAX_LANGUAGES were: a generous cap for
# a real use case (a handful of facts per agent) while still catching an
# obviously-malformed request.
MAX_STRUCTURED_DATA_FIELDS = 20

# Our own judgment call, not a vendor-documented limit — the voice vendor
# places no documented maximum on the states array (confirmed via the same
# live schema check that confirmed the field's shape). See this module's
# docstring, "states/starting_state" section, for the full reasoning.
MAX_STATES = 15

# Retell's own documented default for model_temperature/voice_temperature/
# responsiveness/backchannel_frequency/ambient_sound_volume/reminder_*/
# end_call_after_silence_ms/max_call_duration_ms/begin_message_delay_ms/
# allow_user_dtmf/allow_dtmf_interruption/data_storage_setting — confirmed
# via a fresh live WebFetch of create-agent/create-retell-llm this session.
# See this module's docstring, the "~19-field tuning-knob batch" section,
# for the full field-by-field sourcing.
DEFAULT_VOICE_TEMPERATURE = 1.0
DEFAULT_RESPONSIVENESS = 1.0
DEFAULT_BACKCHANNEL_FREQUENCY = 0.8
DEFAULT_AMBIENT_SOUND_VOLUME = 1.0
DEFAULT_REMINDER_TRIGGER_MS = 10_000
DEFAULT_REMINDER_MAX_COUNT = 1
DEFAULT_END_CALL_AFTER_SILENCE_MS = 600_000
MIN_END_CALL_AFTER_SILENCE_MS = 10_000
DEFAULT_MAX_CALL_DURATION_MS = 3_600_000
MIN_MAX_CALL_DURATION_MS = 60_000
MAX_MAX_CALL_DURATION_MS = 7_200_000
DEFAULT_BEGIN_MESSAGE_DELAY_MS = 0
MAX_BEGIN_MESSAGE_DELAY_MS = 5_000
DEFAULT_ALLOW_USER_DTMF = True
DEFAULT_ALLOW_DTMF_INTERRUPTION = False


class AgentStatus(StrEnum):
    ACTIVE = "active"
    FAILED = "failed"


class ResponseEngine(StrEnum):
    # Internal note (plain code comment, not the class docstring below —
    # this one IS rendered into Swagger's components.schemas description,
    # so it must stay vendor-neutral; see this module's docstring above for
    # which real vendor type each value maps to internally): values are
    # deliberately vendor-neutral rather than the underlying vendor's own
    # response_engine type names, since this enum backs a live AgentPublic
    # response field — vendor-name leakage here would be a real
    # response-body defect, not just a documentation-text one.
    """Which brain generates the agent's conversation responses.

    See this module's docstring above for the full reasoning behind the two
    modes and the default choice.
    """

    BUILTIN = "builtin"
    CUSTOM = "custom"


class OnHoldMusic(StrEnum):
    """Preset hold music played to the caller during a warm transfer while
    they wait for the human to pick up. Only the voice vendor's non-custom
    presets are supported for now — a caller-supplied custom audio asset is
    out of this task's scope (see CreateAgentRequest.transfer_on_hold_music's
    Field description).
    """

    NONE = "none"
    RELAXING_SOUND = "relaxing_sound"
    UPLIFTING_BEATS = "uplifting_beats"
    RINGTONE = "ringtone"


class SttMode(StrEnum):
    """Speech-to-text mode. 'custom' is accepted here (it's a real vendor
    value) but its companion `custom_stt_config` object is NOT built by this
    codebase — out of this batch's confirmed scope, same "don't gold-plate
    beyond the confirmed field list" discipline as the rest of this module.
    Confirmed real via a fresh live WebFetch of create-agent this session.
    """

    FAST = "fast"
    ACCURATE = "accurate"
    CUSTOM = "custom"


class DenoisingMode(StrEnum):
    """How aggressively background noise is filtered from the caller's
    audio. Confirmed real via a fresh live WebFetch of create-agent this
    session.
    """

    NO_DENOISE = "no-denoise"
    NOISE_CANCELLATION = "noise-cancellation"
    NOISE_AND_BACKGROUND_SPEECH_CANCELLATION = "noise-and-background-speech-cancellation"


class AmbientSound(StrEnum):
    """A background ambience preset played under the agent's voice, for a
    less sterile-sounding call (e.g. a faint call-center or coffee-shop
    ambience). Confirmed real, fixed, current preset list via a fresh live
    WebFetch of create-agent this session — `None`/omitted (the default)
    means no ambient sound at all.
    """

    COFFEE_SHOP = "coffee-shop"
    CONVENTION_HALL = "convention-hall"
    SUMMER_OUTDOOR = "summer-outdoor"
    MOUNTAIN_OUTDOOR = "mountain-outdoor"
    STATIC_NOISE = "static-noise"
    CALL_CENTER = "call-center"


class DataStorageSetting(StrEnum):
    """How much of a call's own data the voice vendor retains after the call
    ends. Confirmed real, current values via a fresh live WebFetch of
    create-agent/update-agent this session — supersedes the older
    `opt_out_sensitive_data_storage` boolean field referenced in earlier
    session research, which is not used anywhere in this codebase (this is
    the real, current mechanism).
    """

    EVERYTHING = "everything"
    EVERYTHING_EXCEPT_PII = "everything_except_pii"
    BASIC_ATTRIBUTES_ONLY = "basic_attributes_only"


class PiiMode(StrEnum):
    """When PII redaction happens. Confirmed via a fresh live WebFetch of
    create-agent this session: exactly ONE real vendor-documented value
    exists today (`post_call` — PII is redacted from stored data after the
    call ends, not live-masked during it). Still modeled as a real enum, not
    a hardcoded literal, per this batch's "build real enums, don't guess"
    discipline — a future vendor-added mode is then a one-line addition.
    """

    POST_CALL = "post_call"


class PiiCategory(StrEnum):
    """One category of personally-identifiable information to redact.
    Confirmed real, current 13-value list via a fresh live WebFetch of
    create-agent this session.
    """

    PERSON_NAME = "person_name"
    ADDRESS = "address"
    EMAIL = "email"
    PHONE_NUMBER = "phone_number"
    SSN = "ssn"
    PASSPORT = "passport"
    DRIVER_LICENSE = "driver_license"
    CREDIT_CARD = "credit_card"
    BANK_ACCOUNT = "bank_account"
    PASSWORD = "password"
    PIN = "pin"
    MEDICAL_ID = "medical_id"
    DATE_OF_BIRTH = "date_of_birth"
    CUSTOMER_ACCOUNT_NUMBER = "customer_account_number"


class PiiConfig(BaseModel):
    """PII redaction configuration for stored call data. See this module's
    docstring, "~19-field tuning-knob batch" section, for the full feature
    description. `categories` must be non-empty when `pii_config` is set at
    all — an empty list would configure redaction of nothing, indistinguishable
    from not setting `pii_config` at all, so it's rejected as a likely
    mistake rather than silently forwarded.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "mode": "post_call",
                "categories": ["person_name", "phone_number", "email"],
            }
        }
    )

    mode: Annotated[
        PiiMode,
        Field(description="When redaction happens. Currently only 'post_call' is supported."),
    ]
    categories: Annotated[
        list[PiiCategory],
        Field(
            min_length=1,
            description="Which categories of PII to redact from stored call data. Must be "
            "non-empty.",
        ),
    ]


class HandbookConfig(BaseModel):
    """Ten independent, optional conversation-quality toggles, all real,
    current vendor fields confirmed via a fresh live WebFetch of
    create-agent this session. See this module's docstring, "~19-field
    tuning-knob batch" section, for the full feature description and — this
    is the load-bearing part — the correction of the user's originally-typed
    (incorrect) field name `normalize_for_speech`, which does not exist:
    the real field is `speech_normalization`, nested here.

    Each field is `bool | None` — `None`/omitted means "use the vendor's own
    default for that specific toggle" (this codebase does not invent a
    stance on any of the ten), a real value explicitly turns that one
    behavior on or off.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "default_personality": False,
                "conversational_personality": True,
                "natural_filler_words": True,
                "high_empathy": True,
                "echo_verification": True,
                "nato_phonetic_alphabet": False,
                "speech_normalization": True,
                "smart_matching": True,
                "ai_disclosure": True,
                "scope_boundaries": True,
            }
        }
    )

    default_personality: Annotated[
        bool | None,
        Field(description="Use the vendor's default conversational personality tuning."),
    ] = None
    conversational_personality: Annotated[
        bool | None,
        Field(description="Favor a more casual, conversational speaking style."),
    ] = None
    natural_filler_words: Annotated[
        bool | None,
        Field(description="Allow natural filler words (e.g. 'um', 'well') for a less robotic "
        "cadence."),
    ] = None
    high_empathy: Annotated[
        bool | None,
        Field(description="Favor higher-empathy phrasing in responses."),
    ] = None
    echo_verification: Annotated[
        bool | None,
        Field(description="Have the agent echo back key details (e.g. a spelled name) to "
        "confirm it heard correctly."),
    ] = None
    nato_phonetic_alphabet: Annotated[
        bool | None,
        Field(description="Use the NATO phonetic alphabet (Alpha, Bravo, ...) when spelling "
        "things out loud."),
    ] = None
    speech_normalization: Annotated[
        bool | None,
        Field(description="Normalize spoken output (numbers, dates, etc.) for clearer speech. "
        "This is the real field behind what's sometimes informally called "
        "'normalize for speech'."),
    ] = None
    smart_matching: Annotated[
        bool | None,
        Field(description="Use fuzzy/smart matching when interpreting caller input against "
        "expected values."),
    ] = None
    ai_disclosure: Annotated[
        bool | None,
        Field(description="Have the agent proactively disclose that it is an AI."),
    ] = None
    scope_boundaries: Annotated[
        bool | None,
        Field(description="Have the agent proactively stay within its configured scope rather "
        "than improvising outside it."),
    ] = None


# Internal note (not Swagger-visible — this is a plain code comment, not
# the class docstring below): field names here match the active vendor's
# own `pronunciation_dictionary` entry shape, confirmed in eCareVoiceAI's
# working retell_client.py usage and Retell's create-agent docs. This one
# exception to "never use vendor field names" is fine because
# {word, pronunciation} is generic enough to not read as vendor-specific,
# and Platform X supplies these values directly anyway.
class PronunciationEntry(BaseModel):
    """One word/pronunciation override, passed through to the voice vendor
    verbatim.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"word": "Aspen Quality Care", "pronunciation": "AS-pen KWAL-i-tee kair"}
        }
    )

    word: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    pronunciation: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class CustomToolHttpMethod(StrEnum):
    """HTTP method the voice vendor uses when calling our proxy for this
    tool (mirrors what our proxy then uses calling Platform X's own
    webhook_url — same method both hops, kept simple rather than letting the
    two legs diverge for no real reason). Matches the vendor's own real,
    documented method choices for a custom tool.
    """

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class CustomToolDefinition(BaseModel):
    """One Platform-X-defined custom mid-call tool, attached to a
    `builtin` agent. See this module's docstring for the full
    proxy-routing architecture decision, storage decision, and timeout
    reasoning.

    `parameters_schema` — a JSON Schema object describing what arguments the
    conversation brain can/must fill in for this tool (properties with a
    `description` get LLM-filled at call time, properties with a `const` are
    fixed values) — Platform X defines this themselves per-tool, since it's
    specific to what their tool actually does (e.g. a `date` property for a
    calendar-availability tool). Only validated here as "a well-formed JSON
    Schema shape" (a dict with `type: object`, per the standards doc's "not a
    full JSON Schema validator" instruction) — not deep-validated against the
    full JSON Schema spec.

    `webhook_url` — Platform X's OWN server URL for THIS specific tool. This
    is genuinely Platform X's own data (not vendor-internal), so it's safe to
    include on `AgentPublic` (see AgentPublic's docstring) the same way
    `inbound_variables_webhook_url`/`call_completed_webhook_url` are safe on
    `PlatformPublic` — a platform seeing back a URL IT registered is not a
    leak. Still gets the identical SSRF-adjacent check those two fields
    already have (app/utils/ssrf_guard.py) — see this module's docstring for
    why: it's an address our own server later POSTs to automatically,
    mid-call, on Platform X's behalf.

    `method`/`timeout_ms` control how the voice vendor calls OUR OWN proxy
    (`url`, always ours — see this module's docstring) — NOT a
    separate configuration for the relay leg from our proxy to
    `webhook_url`, which carries its own fixed, stricter internal timeout
    (see app/routers/webhooks.py's custom-tool handler).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "check_availability",
                "description": "Check whether a given date/time has an open appointment slot.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "The date the caller wants to check, as YYYY-MM-DD.",
                        }
                    },
                    "required": ["date"],
                },
                "webhook_url": "https://platformx.example.com/voiceai/tools/check-availability",
                "method": "POST",
                "timeout_ms": 10000,
            }
        }
    )

    name: Annotated[
        str,
        Field(
            pattern=r"^[A-Za-z_]+$",
            min_length=1,
            max_length=100,
            description="Unique identifier for this tool, letters and underscores only "
            "(e.g. 'check_availability'). Must be unique among this agent's own custom "
            "tools. The conversation brain uses this name internally to decide which tool "
            "to call.",
        ),
    ]
    description: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description="Explains to the conversation brain WHEN and WHY to call this tool — "
            "this is what the agent actually reads to decide whether a given moment in the "
            "conversation warrants calling it. Be concrete (e.g. 'Check whether a given date "
            "has an open appointment slot before promising the caller a time.').",
        ),
    ]
    parameters_schema: Annotated[
        dict[str, Any],
        Field(
            description="A JSON Schema object describing the arguments the conversation brain "
            "can fill in for this tool call (e.g. {'type': 'object', 'properties': {'date': "
            "{'type': 'string', 'description': 'The requested date, YYYY-MM-DD'}}, "
            "'required': ['date']}). Only checked for well-formedness (a JSON object shaped "
            "like a schema), not deeply validated against the full JSON Schema specification.",
        ),
    ]
    webhook_url: Annotated[
        HttpUrl,
        Field(
            description="Your own server's URL for THIS specific tool — we relay the voice "
            "vendor's real tool-call to this URL with a strict short timeout and return your "
            "response back to the conversation. Must be a well-formed http(s) URL that does "
            "not resolve to an internal/private network address.",
        ),
    ]
    method: Annotated[
        CustomToolHttpMethod,
        Field(
            description="HTTP method used to call your webhook_url. Default 'POST'.",
        ),
    ] = CustomToolHttpMethod.POST
    timeout_ms: Annotated[
        int,
        Field(
            ge=CUSTOM_TOOL_MIN_TIMEOUT_MS,
            le=CUSTOM_TOOL_MAX_TIMEOUT_MS,
            description="How long (milliseconds) the voice vendor waits for our proxy before "
            f"giving up. Range {CUSTOM_TOOL_MIN_TIMEOUT_MS}-{CUSTOM_TOOL_MAX_TIMEOUT_MS}, "
            f"default {CUSTOM_TOOL_DEFAULT_TIMEOUT_MS} — deliberately far below the voice "
            "vendor's own 120000ms (2 minute) default, since a caller sitting on hold for "
            "up to 2 minutes on a single tool call is a poor experience. Our own relay to "
            "your webhook_url carries its own separate, stricter internal timeout, so make "
            "sure your own server responds well within it.",
        ),
    ] = CUSTOM_TOOL_DEFAULT_TIMEOUT_MS

    @field_validator("parameters_schema")
    @classmethod
    def _validate_schema_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Not a full JSON Schema validator (per the standards doc's explicit
        instruction) — just confirms this looks like a real JSON Schema
        object shape: a dict, with a `type` key present (every real JSON
        Schema object/property definition has one), so an obviously-wrong
        payload (e.g. an empty dict, a list, a bare string) is rejected with
        a clear 422 rather than silently forwarded to the voice vendor.
        """
        if not isinstance(value, dict) or not value:
            raise ValueError(
                "parameters_schema must be a non-empty JSON Schema object (e.g. "
                '{"type": "object", "properties": {...}}).'
            )
        if "type" not in value:
            raise ValueError(
                'parameters_schema must include a \'type\' key, e.g. "type": "object" — '
                "this doesn't need to be a fully valid JSON Schema, just recognizably "
                "shaped like one."
            )
        return value


class StructuredDataFieldType(StrEnum):
    """Which kind of value this extracted fact is. Matches the voice
    vendor's own real, documented type choices for this mechanism
    (confirmed via a live WebFetch this session) — see
    StructuredDataFieldDefinition's docstring for the full feature.
    """

    STRING = "string"
    ENUM = "enum"
    BOOLEAN = "boolean"
    NUMBER = "number"


class StructuredDataFieldDefinition(BaseModel):
    """One fact Platform X wants automatically pulled from every finished
    call on this agent (e.g. "caller's name", "appointment time", "was an
    appointment booked"). See this module's docstring, "structured_data_fields"
    section, for the full feature description and the agent-vs-LLM-object
    placement decision.

    `choices` is required (and must be non-empty) ONLY when `type == 'enum'`
    — a fixed set of allowed values the conversation brain must pick from
    (e.g. "Booked", "Declined", "No answer given"). For every other `type`,
    `choices` must be omitted/empty — set it alongside `string`/`boolean`/
    `number` and it's rejected with a clear 422, the same "loud, not silent"
    discipline this codebase already applies to `transfer_number` under the
    wrong response_engine mode.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "enum",
                "name": "Call Outcome",
                "description": "Categorize how the call ended.",
                "choices": ["Appointment booked", "Declined", "Follow-up requested"],
            }
        }
    )

    type: Annotated[
        StructuredDataFieldType,
        Field(
            description="The kind of value to extract. 'enum' requires a non-empty `choices` "
            "list; every other type must leave `choices` empty."
        ),
    ]
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            description="What this extracted fact is called (e.g. 'Caller Name', 'Appointment "
            "Time'). Must be unique among this agent's own structured_data_fields — this is "
            "the key the extracted value is returned under on the finished call's record and "
            "in the call-completed notification.",
        ),
    ]
    description: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description="Explains to the conversation brain WHAT to extract and HOW to "
            "recognize it in the conversation (e.g. 'The name the caller gives for "
            "themselves, not the agent's own name.'). Be concrete — this is what the "
            "extraction actually reads to decide what value to pull.",
        ),
    ]
    choices: Annotated[
        list[str],
        Field(
            description="Allowed values, required and non-empty ONLY when type='enum' — the "
            "conversation brain picks exactly one of these. Must be empty/omitted for every "
            "other type.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.

    @model_validator(mode="after")
    def _validate_choices_match_type(self) -> StructuredDataFieldDefinition:
        if self.type == StructuredDataFieldType.ENUM:
            if not self.choices:
                raise ValueError(
                    "choices must be a non-empty list when type='enum' — provide the allowed "
                    "values the conversation brain should pick from."
                )
        elif self.choices:
            raise ValueError(
                f"choices must be empty when type='{self.type.value}' — choices is only valid "
                "alongside type='enum'."
            )
        return self


class StateEdge(BaseModel):
    """One possible transition OUT of an `AgentState`, toward another state
    (or back to `starting_state`). See this module's docstring,
    "states/starting_state" section, for the full Single Prompt vs Multi
    Prompt feature description.

    `description` is the ONLY thing that actually drives routing — the
    conversation brain reads this natural-language text mid-call to decide
    WHEN to take this transition, there is no code-level condition/rule
    engine involved. Be concrete about the trigger condition, the same
    "be concrete, this is what the brain actually reads" guidance
    `CustomToolDefinition.description` and `StructuredDataFieldDefinition.
    description` already give (e.g. "When the caller's billing question is
    resolved or they want something else," not just "billing done").
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "destination_state_name": "triage",
                "description": "When the caller's billing question is resolved or they want "
                "something else.",
            }
        }
    )

    destination_state_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            description="The `name` of the state to transition to. Must match either another "
            "state's own `name` in this same request, or the agent's `starting_state` (routing "
            "back to the root is always valid). A reference that matches neither is rejected "
            "with 422 before any vendor call is attempted.",
        ),
    ]
    description: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description="Explains to the conversation brain WHEN to take this transition — "
            "this natural-language text is what actually drives routing, not any code-level "
            "rule. Be concrete about the trigger condition (e.g. 'When the caller's billing "
            "question is resolved or they want something else.').",
        ),
    ]


class AgentState(BaseModel):
    """One named section of a Multi Prompt agent (e.g. a root/triage state,
    or a department state like Billing/Scheduling) that the conversation
    brain automatically routes between mid-call. See this module's
    docstring, "states/starting_state" section, for the full feature
    description, the vendor schema this maps to, and the validation rules
    enforced across an entire `states` array (uniqueness of `name`, edge
    reference resolution, `starting_state` consistency) — those are
    cross-state rules and live on `CreateAgentRequest`/`UpdateAgentRequest`
    instead of here, since a single `AgentState` in isolation has no way to
    check them.

    `state_prompt` is APPENDED to the agent's own `general_prompt` (VoiceAI's
    `prompt`) at conversation time, never a replacement of it — see this
    module's docstring for the confirmed vendor behavior. Write it as the
    INCREMENTAL behavior/knowledge this state adds on top of the agent's
    always-active base prompt, not a full restatement of the agent's whole
    persona.

    `tools` reuses `CustomToolDefinition` unchanged — a per-state tool is the
    same kind of object as a top-level `custom_tools` entry, just only
    callable while this specific state is active. Empty by default; most
    states need no tools of their own beyond what the agent-level
    `custom_tools` already provides.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "billing",
                "state_prompt": "You are now handling billing questions. Be precise about "
                "amounts and dates, and never guess at a balance you're not sure of.",
                "edges": [
                    {
                        "destination_state_name": "triage",
                        "description": "When the caller's billing question is resolved or "
                        "they want something else.",
                    }
                ],
                "tools": [],
            }
        }
    )

    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            description="Unique identifier for this state within this agent (e.g. 'billing', "
            "'triage'). Referenced by `starting_state` and by other states' own `edges"
            "[].destination_state_name`. Must be unique among this agent's own states.",
        ),
    ]
    state_prompt: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10_000,
            description="Instructions specific to this state — APPENDED to the agent's own "
            "base `prompt` at conversation time, not a replacement of it. Describe only what's "
            "incremental about this state (the behavior/knowledge it adds), since the base "
            "prompt is already active underneath it.",
        ),
    ]
    edges: Annotated[
        list[StateEdge],
        Field(
            description="Possible transitions OUT of this state, toward other states. Empty "
            "means this state never hands off elsewhere once entered — valid for a genuine "
            "dead-end state, but most non-root states should route back to `starting_state` "
            "at minimum so a caller isn't stuck once their need is addressed.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    tools: Annotated[
        list[CustomToolDefinition],
        Field(
            description="Custom mid-call tools available only while this state is active — "
            "same shape/proxy-routing as the agent-level `custom_tools` (see that field's own "
            "docstring). Empty by default.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.


class AgentInDB(BaseModel):
    """Shape of an `Agents` document as stored in Mongo.

    `vendor`, `vendor_ref`, and `llm_ref` are internal-only — correlate a
    document back to whichever vendor/adapter created it and to that
    vendor's own record IDs (Retell's `agent_id` and, for `retell_llm`-mode
    agents only, Retell's separate `llm_id`), for webhook correlation and
    support debugging. Never exposed on AgentPublic — see the Database rules
    in the standards doc: the vendor's own ID is stored, but only as a
    clearly-internal field.

    `llm_ref` gets its own field rather than being folded into `vendor_ref`
    because it names a genuinely separate vendor-side resource — under
    `retell_llm` mode, the voice vendor creates TWO objects (an LLM engine,
    then an agent that references it by id), not one, and both ids are
    needed internally: `vendor_ref` to manage/webhook-correlate the agent,
    `llm_ref` to manage/clean up the LLM engine (e.g. the orphan-cleanup path
    in retell_agent_adapter.py's create_retell_llm_agent()). Always `None`
    for `custom_llm`-mode agents, which never create a vendor-side LLM
    object at all.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    platform_id: str

    prompt: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    voice_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    # Typed as a non-empty list of the validated Language enum, always —
    # never a bare Language and never a union — regardless of which form
    # (single string or array) CreateAgentRequest.language was given as. See
    # this module's docstring for the full wire-format/normalization
    # reasoning. The value only ever gets here already-validated (via
    # CreateAgentRequest.language), so this buys a small extra type-safety
    # guarantee for anything reading a stored Agent document back out later
    # (mypy catches a misuse), for free, at no extra validation cost.
    languages: list[Language]
    voice_speed: float
    interruption_sensitivity: float
    enable_backchannel: bool
    pronunciation_dictionary: list[PronunciationEntry]

    response_engine: ResponseEngine
    transfer_number: str | None
    # transfer_ring_duration_ms/transfer_on_hold_music/
    # transfer_show_original_caller_id — added alongside PATCH
    # /agents/{agent_id} (see UpdateAgentRequest's docstring). Before that
    # endpoint existed these three were CreateAgentRequest-only,
    # write-once inputs consumed directly by _build_general_tools at
    # creation time and then discarded — nothing downstream ever needed to
    # know an agent's CURRENT ring duration/hold music/caller-id setting,
    # since there was no update path that could need to "leave it
    # unchanged." A real PATCH changes that: any update that touches
    # transfer_number/custom_tools rebuilds the ENTIRE general_tools array
    # (see app/routers/agents.py's `update_agent` docstring for why that
    # whole-array rebuild is unavoidable even though the vendor endpoint
    # itself is partial-merge), and rebuilding it needs these three values
    # even when the caller's PATCH request didn't mention them — otherwise
    # a caller who only meant to change custom_tools would silently reset
    # an existing ring_duration_ms/hold_music/caller_id customization back
    # to CreateAgentRequest's own defaults as an unintended side effect.
    # Storing them (rather than re-deriving/guessing) is the only way to
    # genuinely support "leave this alone" semantics for these three
    # fields, consistent with every other tuning field on this model.
    transfer_ring_duration_ms: int
    transfer_on_hold_music: OnHoldMusic
    transfer_show_original_caller_id: bool
    custom_tools: list[CustomToolDefinition]
    structured_data_fields: list[StructuredDataFieldDefinition]
    # states/starting_state — Single Prompt vs Multi Prompt (see this
    # module's docstring). Persisted from the start, not create-only, for
    # the exact same "PATCH needs the CURRENT state to merge correctly"
    # reason transfer_ring_duration_ms/transfer_on_hold_music/
    # transfer_show_original_caller_id became persisted fields alongside
    # PATCH /agents/{agent_id} — see those three fields' own comment just
    # above for the full lesson this repeats.
    states: list[AgentState]
    starting_state: str | None
    # welcome_message — see this module's docstring, "welcome_message"
    # section, for the full three-state (None/""/real-string) semantics and
    # why this needs to be a persisted field (same "PATCH rebuild needs the
    # CURRENT value" reason as the three transfer-tuning fields just above).
    # None here means "no welcome_message configured" — genuinely
    # indistinguishable, once stored, from "the caller never set one," which
    # is exactly correct: both cases mean "don't send begin_message at all"
    # on the next vendor call.
    welcome_message: str | None
    # agent_name — see this module's docstring, "agent_name" section, for the
    # full feature description and the agent-object-vs-LLM-object placement
    # decision that makes this field, unlike welcome_message just above,
    # available under BOTH response_engine modes. Persisted for the same
    # "PATCH merge needs the CURRENT value" reason as every other field here.
    # None means "no agent_name configured" — genuinely indistinguishable,
    # once stored, from "the caller never set one."
    agent_name: str | None
    # live_transcript_enabled — see this module's docstring, "live_transcript_
    # enabled" section, for the full feature description and the agent-
    # object placement confirmation (available under BOTH response_engine
    # modes, unlike welcome_message/custom_tools/states). Persisted for the
    # same "PATCH merge needs the CURRENT value" reason as every other field
    # here. Defaults to False for any pre-existing document that predates
    # this field (see agent_repo.py's doc.get()-with-fallback pattern).
    live_transcript_enabled: bool

    # The ~19-field tuning-knob batch — see this module's docstring,
    # "~19-field tuning-knob batch" section, for the full feature
    # description of each. Persisted for the same "PATCH merge needs the
    # CURRENT value" reason as every other field here. `None` for every
    # optional one below means "not configured," genuinely indistinguishable
    # once stored from "the caller never set one" — same as
    # welcome_message/agent_name above.
    model: str | None
    model_temperature: float
    voice_model: str | None
    voice_temperature: float
    stt_mode: SttMode
    denoising_mode: DenoisingMode
    ambient_sound: AmbientSound | None
    ambient_sound_volume: float
    backchannel_frequency: float
    backchannel_words: list[str]
    responsiveness: float
    reminder_trigger_ms: int
    reminder_max_count: int
    end_call_after_silence_ms: int
    max_call_duration_ms: int
    begin_message_delay_ms: int
    allow_user_dtmf: bool
    allow_dtmf_interruption: bool
    data_storage_setting: DataStorageSetting
    pii_config: PiiConfig | None
    post_call_analysis_model: str | None
    handbook_config: HandbookConfig | None

    status: AgentStatus
    vendor: str
    vendor_ref: str | None
    llm_ref: str | None

    created_at: datetime
    updated_at: datetime


class AgentPublic(BaseModel):
    """Safe-to-return shape — no `vendor`, no `vendor_ref`, no `llm_ref`.
    Platform X never needs to know or care which voice vendor is underneath
    (see the standards doc's "design from the integrating platform's side"
    principle).

    `response_engine` and `transfer_enabled` are surfaced explicitly, per
    the standards doc's "how does Platform X know/use this" checklist — a
    caller must be able to tell which brain their agent uses and whether
    transfer is actually configured by reading this response, not by
    remembering what they set on the original CreateAgentRequest.
    `transfer_enabled` is deliberately a derived bool (`response_engine ==
    BUILTIN and transfer_number is not None`) rather than re-exposing
    `transfer_number` itself here — Platform X already has the number they
    submitted, and this field answers the one question they actually need
    answered at a glance ("is transfer live on this agent right now")
    without duplicating the same fact two ways (see the standards doc's
    "no duplicate fields carrying the same information" rule).

    `custom_tools` is included in full, including each tool's own
    `webhook_url` — this is genuinely Platform X's OWN data (the URL THEY
    registered for us to relay to), not vendor-internal, so it gets the same
    "safe to read back" treatment as `inbound_variables_webhook_url`/
    `call_completed_webhook_url` on `PlatformPublic`. See
    CustomToolDefinition's own docstring in this module for the full
    proxy-routing/storage/timeout reasoning.

    `structured_data_fields` is included in full — entirely Platform X's own
    data (what facts they asked us to extract), available under BOTH
    response_engine modes (see StructuredDataFieldDefinition's docstring and
    this module's docstring for the agent-vs-LLM-object placement decision
    that makes this different from `custom_tools`/`transfer_enabled` above).

    `states`/`starting_state` are included in full — same "customer's own
    data, safe to read back" reasoning as `custom_tools` — and
    `multi_prompt_enabled` is a derived `bool` (`len(states) > 0`), same
    at-a-glance convenience `transfer_enabled` already established: Platform
    X shouldn't have to re-derive "is Multi Prompt actually on for this
    agent" from an array length themselves. See this module's docstring,
    "states/starting_state" section, for the full Single Prompt vs Multi
    Prompt feature description.

    `welcome_message` is included as-is — `None` means no welcome_message is
    configured (the agent improvises an opening line), `""` means the agent
    waits silently for the caller to speak first, and any other string is the
    exact greeting the agent says every call. See this module's docstring,
    "welcome_message" section, for the full three-state semantics and why
    this distinction matters. Only meaningful when
    `response_engine='builtin'`, same restriction as `custom_tools`/
    `transfer_number`/`states` above.

    `agent_name` is included as-is — `None` means no agent_name is
    configured (the vendor's own dashboard falls back to a generic,
    type-based label). Unlike `welcome_message` just above, this is
    available under BOTH response_engine modes — see this module's
    docstring, "agent_name" section, for the full agent-object-vs-LLM-object
    placement reasoning behind that difference.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6706f1a2b3c4d5e6f7089abc",
                "platform_id": "6706f1a2b3c4d5e6f7089aaa",
                "prompt": "You are a friendly front-desk assistant for Aspen Quality Care...",
                "voice_id": "11labs-Adrian",
                "languages": ["en-US"],
                "voice_speed": 1.0,
                "interruption_sensitivity": 1.0,
                "enable_backchannel": True,
                "pronunciation_dictionary": [
                    {"word": "Aspen Quality Care", "pronunciation": "AS-pen KWAL-i-tee kair"}
                ],
                "response_engine": "builtin",
                "transfer_enabled": True,
                "transfer_ring_duration_ms": 30000,
                "transfer_on_hold_music": "ringtone",
                "transfer_show_original_caller_id": True,
                "custom_tools": [
                    {
                        "name": "check_availability",
                        "description": "Check whether a given date/time has an open "
                        "appointment slot.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "date": {
                                    "type": "string",
                                    "description": "The date to check, as YYYY-MM-DD.",
                                }
                            },
                            "required": ["date"],
                        },
                        "webhook_url": "https://platformx.example.com/voiceai/tools/"
                        "check-availability",
                        "method": "POST",
                        "timeout_ms": 10000,
                    }
                ],
                "structured_data_fields": [
                    {
                        "type": "string",
                        "name": "Caller Name",
                        "description": "The name the caller gives for themselves.",
                    },
                    {
                        "type": "enum",
                        "name": "Call Outcome",
                        "description": "Categorize how the call ended.",
                        "choices": ["Appointment booked", "Declined", "Follow-up requested"],
                    },
                ],
                "states": [
                    {
                        "name": "billing",
                        "state_prompt": "You are now handling billing questions. Be precise "
                        "about amounts and dates, and never guess at a balance you're not "
                        "sure of.",
                        "edges": [
                            {
                                "destination_state_name": "triage",
                                "description": "When the caller's billing question is "
                                "resolved or they want something else.",
                            }
                        ],
                        "tools": [],
                    },
                    {
                        "name": "triage",
                        "state_prompt": "Greet the caller and figure out whether they need "
                        "billing help or something else.",
                        "edges": [
                            {
                                "destination_state_name": "billing",
                                "description": "When the caller has a billing question.",
                            }
                        ],
                        "tools": [],
                    },
                ],
                "starting_state": "triage",
                "multi_prompt_enabled": True,
                "welcome_message": "Thank you for calling Aspen Quality Care. This call may "
                "be recorded for quality assurance.",
                "agent_name": "Front Desk — Aspen Quality Care",
                "live_transcript_enabled": False,
                "model": "claude-4.5-haiku",
                "model_temperature": 0.1,
                "voice_model": "eleven_turbo_v2_5",
                "voice_temperature": 1.0,
                "stt_mode": "accurate",
                "denoising_mode": "noise-cancellation",
                "ambient_sound": "call-center",
                "ambient_sound_volume": 0.85,
                "backchannel_frequency": 0.5,
                "backchannel_words": ["yeah", "uh-huh"],
                "responsiveness": 1.0,
                "reminder_trigger_ms": 10000,
                "reminder_max_count": 1,
                "end_call_after_silence_ms": 600000,
                "max_call_duration_ms": 3600000,
                "begin_message_delay_ms": 0,
                "allow_user_dtmf": True,
                "allow_dtmf_interruption": False,
                "data_storage_setting": "everything",
                "pii_config": {
                    "mode": "post_call",
                    "categories": ["ssn", "credit_card", "medical_id"],
                },
                "post_call_analysis_model": "gpt-4.1",
                "handbook_config": {
                    "default_personality": False,
                    "conversational_personality": True,
                    "natural_filler_words": True,
                    "high_empathy": True,
                    "echo_verification": True,
                    "nato_phonetic_alphabet": False,
                    "speech_normalization": True,
                    "smart_matching": True,
                    "ai_disclosure": True,
                    "scope_boundaries": True,
                },
                "status": "active",
                "created_at": "2026-08-19T10:00:00Z",
                "updated_at": "2026-08-19T10:00:00Z",
            }
        }
    )

    id: str
    platform_id: str
    prompt: str
    voice_id: str
    languages: Annotated[
        list[Language],
        Field(
            description="The language(s) enabled on this agent for speech recognition and "
            "voice output — always a list here, even when only one language was configured, "
            "for a single consistent response shape regardless of which form (single code or "
            "array) was used on creation. A list of exactly one entry means a single-language "
            "agent; more than one means the voice vendor auto-detects which language the "
            "caller is speaking from this enabled set."
        ),
    ]
    voice_speed: float
    interruption_sensitivity: float
    enable_backchannel: bool
    pronunciation_dictionary: list[PronunciationEntry]
    response_engine: Annotated[
        ResponseEngine,
        Field(
            description="Which brain generates this agent's conversation responses. "
            "'builtin' (the voice vendor's own built-in AI) can hold a real "
            "conversation today and is the only mode that supports transfer_enabled=true. "
            "'custom' (our own AI) cannot hold a conversation yet — the server it "
            "depends on is not built — and never supports transfer."
        ),
    ]
    transfer_enabled: Annotated[
        bool,
        Field(
            description="Whether this agent will actually transfer the caller to a human "
            "today. True only when response_engine='builtin' and a transfer_number "
            "was configured on creation."
        ),
    ]
    transfer_ring_duration_ms: Annotated[
        int,
        Field(
            description="How long (ms) the transfer destination rings before giving up. "
            "Meaningless while transfer_enabled=false — see CreateAgentRequest.transfer_"
            "ring_duration_ms for the default/range."
        ),
    ]
    transfer_on_hold_music: Annotated[
        OnHoldMusic,
        Field(
            description="What the caller hears on hold during a warm transfer. Meaningless "
            "while transfer_enabled=false."
        ),
    ]
    transfer_show_original_caller_id: Annotated[
        bool,
        Field(
            description="Whether the transfer recipient sees the original caller's own "
            "number as caller ID. Meaningless while transfer_enabled=false."
        ),
    ]
    custom_tools: Annotated[
        list[CustomToolDefinition],
        Field(
            description="Custom mid-call tools configured on this agent (e.g. checking "
            "appointment availability, looking up an order status). Empty by default. Only "
            "meaningful when response_engine='builtin' — see CreateAgentRequest."
        ),
    ]
    structured_data_fields: Annotated[
        list[StructuredDataFieldDefinition],
        Field(
            description="Facts automatically extracted from every finished call on this "
            "agent (e.g. caller's name, appointment time, whether an appointment was "
            "booked). Empty by default. Available under BOTH response_engine modes, unlike "
            "custom_tools/transfer_number above. The extracted values themselves show up on "
            "each finished call's own record (extracted_data on GET /calls/{id}'s response) "
            "and in the call-completed notification — not here, since this field only "
            "describes what to extract, not any one call's actual results."
        ),
    ]
    states: Annotated[
        list[AgentState],
        Field(
            description="Named sections ('Billing', 'Scheduling', a root/triage state, etc.) "
            "the conversation brain automatically routes between mid-call. Empty means Single "
            "Prompt (one flat prompt, no internal routing) — the default. A non-empty list "
            "means Multi Prompt is configured; see multi_prompt_enabled below for the "
            "at-a-glance answer. Only meaningful when response_engine='builtin', same "
            "restriction as custom_tools/transfer_number above."
        ),
    ]
    starting_state: Annotated[
        str | None,
        Field(
            description="Which state's `name` the conversation enters first. Always set "
            "(matching one of `states[].name`) when `states` is non-empty; null when "
            "`states` is empty (Single Prompt)."
        ),
    ]
    multi_prompt_enabled: Annotated[
        bool,
        Field(
            description="Whether this agent is actually configured as Multi Prompt today. "
            "True only when `states` is non-empty — a derived convenience field so callers "
            "don't have to check the array length themselves, same reasoning as "
            "transfer_enabled above."
        ),
    ]
    welcome_message: Annotated[
        str | None,
        Field(
            description="The agent's configured opening line, if any. Three distinct states: "
            "null means no welcome_message is configured, so the agent improvises an opening "
            "line from its prompt (the default, unchanged behavior); an empty string means the "
            "agent stays silent and waits for the caller to speak first; any other string is "
            "the exact greeting said verbatim on every call. Only meaningful when "
            "response_engine='builtin'."
        ),
    ]
    agent_name: Annotated[
        str | None,
        Field(
            description="Your own internal reference label for this agent (e.g. 'Front Desk — "
            "Aspen Clinic'), only used on the voice vendor's own dashboard/admin surface to "
            "tell your agents apart — never surfaced to a caller during a call. Null means no "
            "agent_name is configured. Unlike welcome_message above, this is available under "
            "BOTH response_engine values ('builtin' and 'custom')."
        ),
    ] = None
    live_transcript_enabled: Annotated[
        bool,
        Field(
            description="Whether this agent is subscribed to live, per-turn transcript "
            "updates during a call — see WS /calls/{call_id}/live-transcript for how to "
            "actually consume them in real time. Default false (opt-in): most integrations "
            "only need the transcript after a call ends (GET /calls/{id}/transcript), and "
            "enabling this means extra webhook traffic for every conversational turn on "
            "every call this agent handles. Available under BOTH response_engine values, "
            "same as agent_name/structured_data_fields above."
        ),
    ] = False
    model: Annotated[
        str | None,
        Field(
            description="Which text LLM powers this agent's conversation. Null means the "
            "voice vendor's own default ('gpt-4.1') applies. Only meaningful when "
            "response_engine='builtin'."
        ),
    ] = None
    model_temperature: Annotated[
        float,
        Field(description="Response randomness for the conversation model. Range 0.0-1.0."),
    ] = 0.0
    voice_model: Annotated[
        str | None,
        Field(description="Which TTS engine/quality tier renders this agent's voice, separate "
        "from voice_id (which voice) and voice_speed (playback rate). Null means the voice "
        "vendor's own default applies."),
    ] = None
    voice_temperature: Annotated[
        float,
        Field(description="How much expressive variation the TTS engine applies. Range 0.0-2.0."),
    ] = DEFAULT_VOICE_TEMPERATURE
    stt_mode: Annotated[
        SttMode,
        Field(description="Speech-to-text mode."),
    ] = SttMode.FAST
    denoising_mode: Annotated[
        DenoisingMode,
        Field(description="How aggressively background noise is filtered from the caller's "
        "audio."),
    ] = DenoisingMode.NOISE_CANCELLATION
    ambient_sound: Annotated[
        AmbientSound | None,
        Field(description="Background ambience preset played under the agent's voice. Null "
        "means off."),
    ] = None
    ambient_sound_volume: Annotated[
        float,
        Field(description="Volume of ambient_sound, if set. Range 0.0-2.0."),
    ] = DEFAULT_AMBIENT_SOUND_VOLUME
    backchannel_frequency: Annotated[
        float,
        Field(description="How often the agent makes backchannel sounds. Only meaningful when "
        "enable_backchannel=true. Range 0.0-1.0."),
    ] = DEFAULT_BACKCHANNEL_FREQUENCY
    backchannel_words: Annotated[
        list[str],
        Field(description="Custom backchannel words/sounds (e.g. 'mm-hmm', 'I see'). Only "
        "meaningful when enable_backchannel=true. Empty means the voice vendor's own default "
        "word set is used."),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    responsiveness: Annotated[
        float,
        Field(description="How quickly the agent replies after the caller stops talking. "
        "Range 0.0-1.0."),
    ] = DEFAULT_RESPONSIVENESS
    reminder_trigger_ms: Annotated[
        int,
        Field(description="How long (ms) the agent waits in silence before proactively "
        "prompting the caller again."),
    ] = DEFAULT_REMINDER_TRIGGER_MS
    reminder_max_count: Annotated[
        int,
        Field(description="How many times the agent will proactively re-prompt per call."),
    ] = DEFAULT_REMINDER_MAX_COUNT
    end_call_after_silence_ms: Annotated[
        int,
        Field(description="Ends the call after this many milliseconds of pure silence."),
    ] = DEFAULT_END_CALL_AFTER_SILENCE_MS
    max_call_duration_ms: Annotated[
        int,
        Field(description="Caps a call's total duration regardless of activity."),
    ] = DEFAULT_MAX_CALL_DURATION_MS
    begin_message_delay_ms: Annotated[
        int,
        Field(description="Pause (ms) before the agent speaks its opening line."),
    ] = DEFAULT_BEGIN_MESSAGE_DELAY_MS
    allow_user_dtmf: Annotated[
        bool,
        Field(description="Whether the caller's keypad (DTMF) input is accepted."),
    ] = DEFAULT_ALLOW_USER_DTMF
    allow_dtmf_interruption: Annotated[
        bool,
        Field(description="Whether pressing a key interrupts the agent mid-sentence."),
    ] = DEFAULT_ALLOW_DTMF_INTERRUPTION
    data_storage_setting: Annotated[
        DataStorageSetting,
        Field(description="How much of a call's own data the voice vendor retains after the "
        "call ends."),
    ] = DataStorageSetting.EVERYTHING
    pii_config: Annotated[
        PiiConfig | None,
        Field(description="PII redaction configuration for stored call data. Null means no "
        "redaction is configured."),
    ] = None
    post_call_analysis_model: Annotated[
        str | None,
        Field(description="Which model performs post-call structured-data extraction "
        "(structured_data_fields above). Null means the voice vendor's own default "
        "('gpt-4.1') applies."),
    ] = None
    handbook_config: Annotated[
        HandbookConfig | None,
        Field(description="Ten independent conversation-quality toggles (e.g. "
        "speech_normalization, high_empathy). Null means none configured — the voice "
        "vendor's own defaults apply to all ten."),
    ] = None
    status: AgentStatus
    created_at: datetime
    updated_at: datetime


class CreateAgentRequest(BaseModel):
    """POST /agents request body.

    Only `prompt` and `voice_id` are required; every tuning field has a
    sensible default so the common case is a two-field call (per the
    standards doc: don't force every caller to specify everything).
    Numeric ranges match the voice vendor's own documented ranges for
    these fields.

    **Transfer fields and `custom_tools` are only meaningful under
    `response_engine='builtin'` (the default) — see
    `transfer_number`'s and `custom_tools`' Field descriptions.** Setting any
    `transfer_*` field or a non-empty `custom_tools` while
    `response_engine='custom'` is rejected with 422 rather than
    silently ignored, since a silently-dropped field would look like it
    worked and only fail obviously once someone tries to use it
    on a real call.

    **`structured_data_fields` is the one exception to that restriction —
    it works under BOTH response_engine modes.** See
    StructuredDataFieldDefinition's docstring and this module's docstring
    for why: it's an agent-object field on the voice vendor's own API, not a
    general_tools/LLM-object mechanism the way transfer/custom tools are.

    **`states`/`starting_state` pick Single Prompt (default, `states=[]`) vs
    Multi Prompt (`states` non-empty).** Same `builtin`-only restriction as
    `transfer_number`/`custom_tools`, for the same reason (both depend on
    the vendor's separate LLM object, which only exists under `builtin`) —
    see this module's docstring, "states/starting_state" section, for the
    full feature description, the vendor schema, and every validation rule
    enforced below.

    **`welcome_message` picks a fixed, word-for-word greeting instead of the
    default improvised opening line.** Same `builtin`-only restriction as
    `transfer_number`/`custom_tools`/`states`, for the same reason (the
    underlying conversation-brain mechanism lives on the same LLM object) —
    see this module's docstring, "welcome_message" section, for the full
    three-state (omitted/empty-string/real-string) semantics, which is the
    genuinely subtle part of this field.

    **`agent_name` is a SECOND exception to the `builtin`-only restriction,
    alongside `structured_data_fields` — it works under BOTH response_engine
    modes.** Unlike `welcome_message` immediately above, `agent_name` lives
    on the vendor's own agent object, not its LLM object — see this module's
    docstring, "agent_name" section, for the full placement reasoning and
    the explicit warning against reflexively restricting it the way every
    other recently-added field on this model is restricted.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prompt": "You are a friendly front-desk assistant for Aspen Quality Care...",
                "voice_id": "11labs-Adrian",
                "language": "en-US",
                "voice_speed": 1.0,
                "interruption_sensitivity": 1.0,
                "enable_backchannel": True,
                "pronunciation_dictionary": [
                    {"word": "Aspen Quality Care", "pronunciation": "AS-pen KWAL-i-tee kair"}
                ],
                "response_engine": "builtin",
                "welcome_message": "Thank you for calling Aspen Quality Care. How can I "
                "help you today?",
                "agent_name": "Front Desk — Aspen Quality Care",
                "live_transcript_enabled": False,
                "model": "claude-4.5-haiku",
                "model_temperature": 0.1,
                "voice_model": "eleven_turbo_v2_5",
                "voice_temperature": 1.0,
                "stt_mode": "accurate",
                "denoising_mode": "noise-cancellation",
                "ambient_sound": "call-center",
                "ambient_sound_volume": 0.85,
                "backchannel_frequency": 0.5,
                "backchannel_words": ["yeah", "uh-huh"],
                "responsiveness": 1.0,
                "reminder_trigger_ms": 10000,
                "reminder_max_count": 1,
                "end_call_after_silence_ms": 600000,
                "max_call_duration_ms": 3600000,
                "begin_message_delay_ms": 0,
                "allow_user_dtmf": True,
                "allow_dtmf_interruption": False,
                "data_storage_setting": "everything",
                "pii_config": {
                    "mode": "post_call",
                    "categories": ["ssn", "credit_card", "medical_id"],
                },
                "post_call_analysis_model": "gpt-4.1",
                "handbook_config": {
                    "default_personality": False,
                    "conversational_personality": True,
                    "natural_filler_words": True,
                    "high_empathy": True,
                    "echo_verification": True,
                    "nato_phonetic_alphabet": False,
                    "speech_normalization": True,
                    "smart_matching": True,
                    "ai_disclosure": True,
                    "scope_boundaries": True,
                },
                "transfer_number": "+14155550100",
                "transfer_ring_duration_ms": 30000,
                "transfer_on_hold_music": "ringtone",
                "transfer_show_original_caller_id": True,
                "custom_tools": [
                    {
                        "name": "check_availability",
                        "description": "Check whether a given date/time has an open "
                        "appointment slot.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "date": {
                                    "type": "string",
                                    "description": "The date to check, as YYYY-MM-DD.",
                                }
                            },
                            "required": ["date"],
                        },
                        "webhook_url": "https://platformx.example.com/voiceai/tools/"
                        "check-availability",
                        "method": "POST",
                        "timeout_ms": 10000,
                    }
                ],
                "structured_data_fields": [
                    {
                        "type": "string",
                        "name": "Caller Name",
                        "description": "The name the caller gives for themselves.",
                    },
                    {
                        "type": "enum",
                        "name": "Call Outcome",
                        "description": "Categorize how the call ended.",
                        "choices": ["Appointment booked", "Declined", "Follow-up requested"],
                    },
                ],
                "states": [
                    {
                        "name": "billing",
                        "state_prompt": "You are now handling billing questions. Be precise "
                        "about amounts and dates, and never guess at a balance you're not "
                        "sure of.",
                        "edges": [
                            {
                                "destination_state_name": "triage",
                                "description": "When the caller's billing question is "
                                "resolved or they want something else.",
                            }
                        ],
                        "tools": [],
                    },
                    {
                        "name": "triage",
                        "state_prompt": "Greet the caller and figure out whether they need "
                        "billing help or something else.",
                        "edges": [
                            {
                                "destination_state_name": "billing",
                                "description": "When the caller has a billing question.",
                            }
                        ],
                        "tools": [],
                    },
                ],
                "starting_state": "triage",
            },
            "examples": [
                {
                    # Full, every-field example — listed FIRST, deliberately. Swagger
                    # UI's "Example Value" panel defaults to whichever entry is FIRST
                    # in `examples` (this list takes priority over the single `example`
                    # dict above for rendering purposes, per how Swagger UI resolves
                    # OpenAPI 3.1's example/examples precedence) — confirmed live: a
                    # minimal 2-3-field scenario was rendering by default before this
                    # change, making the request body look far smaller than it
                    # actually is. Putting the comprehensive example first means a
                    # developer opening this endpoint's docs sees every real field at
                    # a glance, not just two of eighteen.
                    "prompt": "You are a friendly front-desk assistant for Aspen Quality Care...",
                    "voice_id": "11labs-Adrian",
                    "language": "en-US",
                    "voice_speed": 1.0,
                    "interruption_sensitivity": 1.0,
                    "enable_backchannel": True,
                    "pronunciation_dictionary": [
                        {"word": "Aspen Quality Care", "pronunciation": "AS-pen KWAL-i-tee kair"}
                    ],
                    "response_engine": "builtin",
                    "welcome_message": "Thank you for calling Aspen Quality Care. How can I "
                    "help you today?",
                    "agent_name": "Front Desk — Aspen Quality Care",
                    "live_transcript_enabled": False,
                    "model": "claude-4.5-haiku",
                    "model_temperature": 0.1,
                    "voice_model": "eleven_turbo_v2_5",
                    "voice_temperature": 1.0,
                    "stt_mode": "accurate",
                    "denoising_mode": "noise-cancellation",
                    "ambient_sound": "call-center",
                    "ambient_sound_volume": 0.85,
                    "backchannel_frequency": 0.5,
                    "backchannel_words": ["yeah", "uh-huh"],
                    "responsiveness": 1.0,
                    "reminder_trigger_ms": 10000,
                    "reminder_max_count": 1,
                    "end_call_after_silence_ms": 600000,
                    "max_call_duration_ms": 3600000,
                    "begin_message_delay_ms": 0,
                    "allow_user_dtmf": True,
                    "allow_dtmf_interruption": False,
                    "data_storage_setting": "everything",
                    "pii_config": {
                        "mode": "post_call",
                        "categories": ["ssn", "credit_card", "medical_id"],
                    },
                    "post_call_analysis_model": "gpt-4.1",
                    "handbook_config": {"speech_normalization": True},
                    "transfer_number": "+14155550100",
                    "transfer_ring_duration_ms": 30000,
                    "transfer_on_hold_music": "ringtone",
                    "transfer_show_original_caller_id": True,
                    "custom_tools": [
                        {
                            "name": "check_availability",
                            "description": "Check whether a given date/time has an open "
                            "appointment slot.",
                            "parameters_schema": {
                                "type": "object",
                                "properties": {
                                    "date": {
                                        "type": "string",
                                        "description": "The date to check, as YYYY-MM-DD.",
                                    }
                                },
                                "required": ["date"],
                            },
                            "webhook_url": "https://platformx.example.com/voiceai/tools/"
                            "check-availability",
                            "method": "POST",
                            "timeout_ms": 10000,
                        }
                    ],
                    "structured_data_fields": [
                        {
                            "type": "string",
                            "name": "Caller Name",
                            "description": "The name the caller gives for themselves.",
                        },
                        {
                            "type": "enum",
                            "name": "Call Outcome",
                            "description": "Categorize how the call ended.",
                            "choices": ["Appointment booked", "Declined", "Follow-up requested"],
                        },
                    ],
                    "states": [],
                    # starting_state deliberately omitted, not set to None — this scenario
                    # depicts Single Prompt (states=[]), for which starting_state is
                    # genuinely null. Per this file's own fix for the identical landmine
                    # on UpdatePlatformSettingsResponse's revoked_at, a literal None value
                    # here would be silently dropped from the rendered OpenAPI example
                    # anyway — omitting the key is the honest way to show "absent/null
                    # for this example" without relying on a value FastAPI would drop.
                },
                {
                    "prompt": "You are a friendly front-desk assistant for Aspen Quality Care...",
                    "voice_id": "11labs-Adrian",
                    "language": "en-US",
                },
                {
                    "prompt": "You are a friendly front-desk assistant for a bilingual clinic...",
                    "voice_id": "11labs-Adrian",
                    "language": ["en-US", "es-ES"],
                },
                {
                    "prompt": "You are a friendly front-desk assistant for Aspen Quality Care.",
                    "voice_id": "11labs-Adrian",
                    "states": [
                        {
                            "name": "billing",
                            "state_prompt": "You are now handling billing questions.",
                            "edges": [
                                {
                                    "destination_state_name": "triage",
                                    "description": "When the caller's billing question is "
                                    "resolved or they want something else.",
                                }
                            ],
                        },
                        {
                            "name": "scheduling",
                            "state_prompt": "You are now handling appointment scheduling.",
                            "edges": [
                                {
                                    "destination_state_name": "triage",
                                    "description": "When the scheduling request is resolved "
                                    "or they want something else.",
                                }
                            ],
                        },
                    ],
                    "starting_state": "triage",
                },
            ],
        }
    )

    prompt: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10_000,
            description="The agent's base system prompt — the structural instructions the "
            "conversation brain follows for every call (see response_engine below for which "
            "brain that is). Personalize per-call on top of this via dynamic variables (a "
            "separate, future capability), not by editing this prompt per call.",
        ),
    ]
    voice_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="A voice ID (e.g. '11labs-Adrian'). Browse available voices via "
            "GET /voices.",
        ),
    ]
    language: Annotated[
        Language | list[Language],
        Field(
            description="BCP-47-style language/locale code(s) for speech recognition and "
            "voice output. The common case is a SINGLE code (e.g. 'en-US', 'zh-CN', "
            "'fr-CA') — send it as a plain string. For a genuinely multilingual agent, send "
            "a non-empty ARRAY of codes instead (e.g. ['en-US', 'es-ES']); the voice vendor "
            "auto-detects which language the caller is speaking from the enabled set and "
            "responds in kind. Arrays are capped at "
            f"{MAX_LANGUAGES} codes (our own sane limit, not a vendor-documented one). "
            "Browse all supported codes via GET /languages. Defaults to 'en-US' if omitted. "
            "Note: Cantonese is 'yue-CN' (Mainland China) only — there is no Hong Kong "
            "Cantonese code; do not assume 'yue-CN' covers Hong Kong callers, whether used "
            "alone or as one entry in a multilingual array."
        ),
    ] = Language.EN_US

    @field_validator("language")
    @classmethod
    def _validate_language_list(cls, value: Language | list[Language]) -> Language | list[Language]:
        """The single-code form (`Language`) is already fully validated by the
        enum itself — nothing further to check. The array form needs two
        extra checks the type annotation alone can't express: non-empty (an
        empty array is meaningless — no language would be configured at all)
        and capped at MAX_LANGUAGES (our own judgment call, not a vendor
        limit — see this module's docstring).
        """
        if isinstance(value, list):
            if len(value) == 0:
                raise ValueError(
                    "language array must not be empty — provide at least one language code, "
                    "or send a single code as a plain string instead of an array."
                )
            if len(value) > MAX_LANGUAGES:
                raise ValueError(
                    f"language array must not exceed {MAX_LANGUAGES} codes (got {len(value)}) "
                    "— this is our own sane limit, not a voice-vendor-documented maximum."
                )
        return value

    voice_speed: Annotated[
        float,
        Field(
            ge=0.5,
            le=2.0,
            description="Playback speed multiplier for the agent's voice. Range 0.5-2.0, "
            "default 1.0 (normal speed).",
        ),
    ] = 1.0
    interruption_sensitivity: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="How readily the agent yields the floor when the caller starts "
            "speaking over it. Range 0.0 (rarely interrupted) to 1.0 (interrupts easily), "
            "default 1.0.",
        ),
    ] = 1.0
    enable_backchannel: Annotated[
        bool,
        Field(
            description="Whether the agent makes small acknowledgement sounds ('mm-hmm', "
            "'right') while the caller is speaking, for a more natural feel. Default true."
        ),
    ] = True
    pronunciation_dictionary: Annotated[
        list[PronunciationEntry],
        Field(
            description="Word-level pronunciation overrides (e.g. a brand or clinical name "
            "the default TTS mispronounces). Empty by default."
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    response_engine: Annotated[
        ResponseEngine,
        Field(
            description="Which brain generates this agent's conversation responses. "
            "'builtin' (the voice vendor's own built-in AI) is the DEFAULT: it can "
            "hold a real conversation the moment the agent is created, and it's the only "
            "mode that supports transferring the caller to a human (transfer_number below). "
            "'custom' (our own AI) is reserved for future use — it cannot hold a "
            "real conversation today because the server that would generate its replies is "
            "not built yet, and it never supports transfer under any circumstance (only a "
            "same-call, AI-decided handoff would be possible once that server exists, with "
            "no way to configure it at agent-creation time). Choose 'custom' only "
            "if you specifically intend to wait for that future capability; for a working "
            "agent today, use the default."
        ),
    ] = ResponseEngine.BUILTIN
    transfer_number: Annotated[
        str | None,
        Field(
            pattern=r"^\+[1-9]\d{1,14}$",
            description="E.164 phone number (e.g. '+14155550100') to warm-transfer the "
            "caller to when the agent decides a human is needed. Only valid alongside "
            "response_engine='builtin' (the default) — REJECTED with 422 if set "
            "while response_engine='custom', since that mode cannot support "
            "transfer at all and silently dropping the field would hide a real integration "
            "mistake. Omit (or leave null) to create an agent with no transfer capability. "
            "A warm transfer means the receiving human hears a short handoff/context cue "
            "before being connected, rather than being dropped straight into the live call. "
            "Known limitation, not yet tested by us: what happens if the destination "
            "doesn't answer within transfer_ring_duration_ms (call returns to the agent vs. "
            "ends outright) is not fully documented by the voice vendor either — test this "
            "directly against your own number before relying on it in production.",
        ),
    ] = None
    transfer_ring_duration_ms: Annotated[
        int,
        Field(
            ge=5000,
            le=90000,
            description="How long (milliseconds) to ring the transfer_number before giving "
            "up. Range 5000-90000, default 30000 (30s, matching the voice vendor's own "
            "documented default). Ignored if transfer_number is not set. See "
            "transfer_number's description for a known gap in what's documented to happen "
            "if this timeout is reached.",
        ),
    ] = 30_000
    transfer_on_hold_music: Annotated[
        OnHoldMusic,
        Field(
            description="What the caller hears while on hold during a warm transfer. "
            "Default 'ringtone'. A caller-supplied custom audio track is not supported yet — "
            "only these built-in presets. Ignored if transfer_number is not set.",
        ),
    ] = OnHoldMusic.RINGTONE
    transfer_show_original_caller_id: Annotated[
        bool,
        Field(
            description="Whether the human receiving the transfer sees the original "
            "caller's phone number as the caller ID, instead of this agent's own number. "
            "Default true. Ignored if transfer_number is not set. Known limitation: whether "
            "this reliably works depends on which telephony path provisioned the number "
            "this agent receives calls on (buy-new through us via POST "
            "/agents/{agent_id}/numbers, vs. bring-your-own-SIP via the /numbers/byo "
            "sibling) — it is not guaranteed to behave identically across both paths, per "
            "the voice vendor's own documentation. If this matters for your use case, "
            "verify it against your specific number/trunk setup before relying on it.",
        ),
    ] = True
    custom_tools: Annotated[
        list[CustomToolDefinition],
        Field(
            max_length=MAX_CUSTOM_TOOLS,
            description="Custom mid-call tools this agent can call out to (e.g. 'check "
            "appointment availability', 'look up an order status') — the conversation brain "
            "decides when to call each one, based on its description. Only valid alongside "
            "response_engine='builtin' (the default) — REJECTED with 422 if set while "
            "response_engine='custom', same reasoning as transfer_number above. Each "
            "tool's own webhook_url is YOUR OWN server's URL for that tool — we relay the "
            f"real tool-call to it. Empty by default. Capped at {MAX_CUSTOM_TOOLS} tools per "
            "agent.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    structured_data_fields: Annotated[
        list[StructuredDataFieldDefinition],
        Field(
            max_length=MAX_STRUCTURED_DATA_FIELDS,
            description="Facts you want automatically pulled from every finished call on "
            "this agent — e.g. the caller's name, an appointment time, or whether an "
            "appointment was booked. Each entry names the fact, describes what to extract, "
            "and picks a type ('string'/'enum'/'boolean'/'number'; 'enum' requires a "
            "non-empty `choices` list). Unlike transfer_number/custom_tools, this works "
            "under BOTH response_engine values. The extracted values appear on each "
            "finished call's own record (extracted_data on GET /calls/{id}) and in the "
            "call-completed notification, keyed by each field's own `name`. Empty by "
            f"default. Capped at {MAX_STRUCTURED_DATA_FIELDS} fields per agent.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    states: Annotated[
        list[AgentState],
        Field(
            max_length=MAX_STATES,
            description="Split this agent into named sections ('Billing', 'Scheduling', a "
            "root/triage state, etc.) the conversation brain automatically routes between "
            "mid-call — this is Multi Prompt. Empty (the default) means Single Prompt: one "
            "flat prompt, no internal routing. Only valid alongside response_engine='builtin' "
            "(the default) — REJECTED with 422 if set while response_engine='custom', same "
            "reasoning as transfer_number/custom_tools above. Requires starting_state to be "
            f"set whenever non-empty. Capped at {MAX_STATES} states per agent.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    starting_state: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=100,
            description="Which state's `name` the conversation enters first. REQUIRED when "
            "`states` is non-empty (and must match one of states[].name); must be omitted "
            "when `states` is empty. Every state's own edges may also route back here.",
        ),
    ] = None
    welcome_message: Annotated[
        str | None,
        Field(
            max_length=2000,
            description="The agent's opening line — has THREE distinct states, not two. "
            "Omit (or send null) for the DEFAULT, unchanged behavior: the agent improvises an "
            "opening line from `prompt` on the fly, different every call. Set to a specific "
            "non-empty string to have the agent say EXACTLY that, verbatim, every single call "
            "— use this when you need a consistent, word-for-word greeting (e.g. a required "
            "disclosure, brand-consistent wording). Set to an explicit empty string \"\" to "
            "have the agent stay silent and wait for the caller to speak first, instead of "
            "saying anything. Only valid alongside response_engine='builtin' (the default) — "
            "REJECTED with 422 if set while response_engine='custom', same reasoning as "
            "transfer_number above.",
        ),
    ] = None
    agent_name: Annotated[
        str | None,
        Field(
            description="Your own internal reference label for this agent (e.g. 'Front Desk — "
            "Aspen Clinic'), used ONLY on the voice vendor's own dashboard/admin surface so "
            "your agents are distinguishable there instead of all showing a generic, "
            "type-based label — never surfaced to a caller during a call. Optional, no "
            "maximum length imposed. Unlike welcome_message/transfer_number/custom_tools/"
            "states above, this works under BOTH response_engine values — it lives on the "
            "agent object itself, not the conversation-brain object those four depend on, so "
            "it is never rejected under response_engine='custom'.",
        ),
    ] = None
    live_transcript_enabled: Annotated[
        bool,
        Field(
            description="Subscribe this agent to live, per-turn transcript updates during a "
            "call, relayed in real time over WS /calls/{call_id}/live-transcript. Default "
            "false (opt-in) — most integrations only need the transcript after a call ends "
            "(GET /calls/{id}/transcript); enabling this means the voice vendor sends us a "
            "real webhook for every conversational turn on every call this agent handles. "
            "Like agent_name/structured_data_fields (and unlike welcome_message/"
            "transfer_number/custom_tools/states), this works under BOTH response_engine "
            "values — it lives on the agent object itself, not the conversation-brain object "
            "those four depend on.",
        ),
    ] = False
    model: Annotated[
        str | None,
        Field(
            description="Which text LLM powers the conversation (e.g. 'gpt-4.1', "
            "'claude-4.5-sonnet', 'gemini-3.0-flash'). Not a fixed list here — the voice "
            "vendor adds new models over time; any name it currently supports is accepted, "
            "and an unsupported one is rejected by the vendor itself. Omit to use the voice "
            "vendor's own default ('gpt-4.1'). Only meaningful alongside "
            "response_engine='builtin' — REJECTED with 422 if set while "
            "response_engine='custom', same reasoning as transfer_number above.",
        ),
    ] = None
    model_temperature: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Randomness of the conversation model's responses. Range 0.0 "
            "(deterministic) to 1.0 (more random), default 0.0. Only meaningful alongside "
            "response_engine='builtin'.",
        ),
    ] = 0.0
    voice_model: Annotated[
        str | None,
        Field(
            description="Which TTS engine/quality tier renders the agent's voice — separate "
            "from voice_id (which voice) and voice_speed (playback rate). Not a fixed list "
            "here, same reasoning as model above (the vendor adds new engines over time). "
            "Omit to use the voice vendor's own default.",
        ),
    ] = None
    voice_temperature: Annotated[
        float,
        Field(
            ge=0.0,
            le=2.0,
            description="How much expressive variation the TTS engine applies. Range "
            "0.0-2.0, default 1.0.",
        ),
    ] = DEFAULT_VOICE_TEMPERATURE
    stt_mode: Annotated[
        SttMode,
        Field(
            description="Speech-to-text mode. Default 'fast'. 'custom' is accepted but its "
            "companion custom_stt_config is not supported by this API — omit unless you know "
            "the voice vendor's own default handling of an unconfigured 'custom' mode "
            "applies to your case.",
        ),
    ] = SttMode.FAST
    denoising_mode: Annotated[
        DenoisingMode,
        Field(
            description="How aggressively background noise is filtered from the caller's "
            "audio. Default 'noise-cancellation'.",
        ),
    ] = DenoisingMode.NOISE_CANCELLATION
    ambient_sound: Annotated[
        AmbientSound | None,
        Field(
            description="A background ambience preset played under the agent's voice (e.g. "
            "'call-center', 'coffee-shop'). Omit for no ambient sound (the default).",
        ),
    ] = None
    ambient_sound_volume: Annotated[
        float,
        Field(
            ge=0.0,
            le=2.0,
            description="Volume of ambient_sound, if set. Range 0.0-2.0, default 1.0. "
            "Ignored if ambient_sound is not set.",
        ),
    ] = DEFAULT_AMBIENT_SOUND_VOLUME
    backchannel_frequency: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="How often the agent makes backchannel acknowledgement sounds. "
            "Range 0.0-1.0, default 0.8. Only meaningful when enable_backchannel=true.",
        ),
    ] = DEFAULT_BACKCHANNEL_FREQUENCY
    backchannel_words: Annotated[
        list[str],
        Field(
            description="Custom backchannel words/sounds (e.g. 'mm-hmm', 'I see', 'right'). "
            "Empty (the default) uses the voice vendor's own default word set. Only "
            "meaningful when enable_backchannel=true.",
        ),
    ] = []  # noqa: RUF012 — Pydantic field default, not a mutable-class-attribute footgun.
    responsiveness: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="How quickly the agent replies after the caller stops talking — a "
            "different knob from interruption_sensitivity (how readily it yields the floor "
            "while the caller is STILL talking). Range 0.0-1.0, default 1.0.",
        ),
    ] = DEFAULT_RESPONSIVENESS
    reminder_trigger_ms: Annotated[
        int,
        Field(
            gt=0,
            description="How long (milliseconds) the agent waits in silence before "
            "proactively prompting the caller again. Default 10000 (10s).",
        ),
    ] = DEFAULT_REMINDER_TRIGGER_MS
    reminder_max_count: Annotated[
        int,
        Field(
            ge=0,
            description="How many times the agent will proactively re-prompt per call. "
            "Default 1.",
        ),
    ] = DEFAULT_REMINDER_MAX_COUNT
    end_call_after_silence_ms: Annotated[
        int,
        Field(
            ge=MIN_END_CALL_AFTER_SILENCE_MS,
            description="Ends the call after this many milliseconds of pure silence. "
            f"Minimum {MIN_END_CALL_AFTER_SILENCE_MS} (the voice vendor's own documented "
            f"floor), default {DEFAULT_END_CALL_AFTER_SILENCE_MS} (10 minutes).",
        ),
    ] = DEFAULT_END_CALL_AFTER_SILENCE_MS
    max_call_duration_ms: Annotated[
        int,
        Field(
            ge=MIN_MAX_CALL_DURATION_MS,
            le=MAX_MAX_CALL_DURATION_MS,
            description="Caps a call's total duration regardless of activity. Range "
            f"{MIN_MAX_CALL_DURATION_MS}-{MAX_MAX_CALL_DURATION_MS} (1 minute to 2 hours), "
            f"default {DEFAULT_MAX_CALL_DURATION_MS} (1 hour).",
        ),
    ] = DEFAULT_MAX_CALL_DURATION_MS
    begin_message_delay_ms: Annotated[
        int,
        Field(
            ge=0,
            le=MAX_BEGIN_MESSAGE_DELAY_MS,
            description="Pause (milliseconds) before the agent speaks its opening line "
            "(improvised or welcome_message), letting ringback/connection audio settle "
            f"first. Range 0-{MAX_BEGIN_MESSAGE_DELAY_MS}, default 0 (no delay).",
        ),
    ] = DEFAULT_BEGIN_MESSAGE_DELAY_MS
    allow_user_dtmf: Annotated[
        bool,
        Field(
            description="Whether the caller's keypad (DTMF) input is accepted at all. "
            "Default true.",
        ),
    ] = DEFAULT_ALLOW_USER_DTMF
    allow_dtmf_interruption: Annotated[
        bool,
        Field(
            description="Whether pressing a key interrupts the agent mid-sentence, the same "
            "way speaking over it does. Default false.",
        ),
    ] = DEFAULT_ALLOW_DTMF_INTERRUPTION
    data_storage_setting: Annotated[
        DataStorageSetting,
        Field(
            description="How much of a call's own data the voice vendor retains after the "
            "call ends. Default 'everything'.",
        ),
    ] = DataStorageSetting.EVERYTHING
    pii_config: Annotated[
        PiiConfig | None,
        Field(
            description="Configure PII redaction for stored call data. Omit for no "
            "redaction (the default).",
        ),
    ] = None
    post_call_analysis_model: Annotated[
        str | None,
        Field(
            description="Which model performs post-call structured-data extraction "
            "(structured_data_fields above) — same open-ended, not-a-fixed-list reasoning as "
            "model above. Omit to use the voice vendor's own default ('gpt-4.1').",
        ),
    ] = None
    handbook_config: Annotated[
        HandbookConfig | None,
        Field(
            description="Ten independent conversation-quality toggles (e.g. "
            "speech_normalization, high_empathy, ai_disclosure) — see HandbookConfig for the "
            "full list. Omit for none configured (the voice vendor's own defaults apply to "
            "all ten).",
        ),
    ] = None

    @field_validator("pii_config")
    @classmethod
    def _reject_empty_pii_categories(cls, value: PiiConfig | None) -> PiiConfig | None:
        """`categories` already enforces `min_length=1` on PiiConfig itself
        (see that model), so this is unreachable in practice via normal
        validation — kept as a defense-in-depth, explicit-error safety net
        consistent with this codebase's "loud, not silent" discipline,
        rather than relying solely on the nested model's own constraint.
        """
        if value is not None and not value.categories:
            raise ValueError(
                "pii_config.categories must be non-empty when pii_config is set — an empty "
                "list would configure redaction of nothing."
            )
        return value

    @field_validator("structured_data_fields")
    @classmethod
    def _reject_duplicate_structured_data_field_names(
        cls, value: list[StructuredDataFieldDefinition]
    ) -> list[StructuredDataFieldDefinition]:
        """Same duplicate-prevention reasoning as `_reject_duplicate_tool_names`
        below — each field's `name` becomes a key on the extracted-data
        object Platform X reads back, so a duplicate would be genuinely
        ambiguous, not just cosmetic.
        """
        names = [field.name for field in value]
        if len(names) != len(set(names)):
            raise ValueError(
                "structured_data_fields entries must have unique 'name' values — found a "
                "duplicate."
            )
        return value

    @field_validator("states")
    @classmethod
    def _reject_duplicate_state_names(cls, value: list[AgentState]) -> list[AgentState]:
        """Mirrors `_reject_duplicate_tool_names` exactly — a state's `name`
        is how `starting_state` and every edge's `destination_state_name`
        reference it, so a duplicate would make routing genuinely ambiguous
        on the vendor's own side, not just cosmetic.
        """
        names = [state.name for state in value]
        if len(names) != len(set(names)):
            raise ValueError("states entries must have unique 'name' values — found a duplicate.")
        return value

    @model_validator(mode="after")
    def _validate_states_routing(self) -> CreateAgentRequest:
        """Cross-field routing checks that a single `AgentState`/`StateEdge`
        can't validate on its own — see this module's docstring,
        "states/starting_state" section, for the full reasoning behind each
        check. Runs regardless of response_engine; the mode restriction
        itself is enforced separately by
        `_reject_transfer_fields_under_custom_mode` below, so an empty
        `states` array under 'custom' mode never reaches these checks with
        anything to validate anyway.
        """
        if not self.states:
            if self.starting_state is not None:
                raise ValueError(
                    "starting_state must be omitted when states is empty — there is nothing "
                    "for it to name. Set at least one entry in states first."
                )
            return self

        state_names = {state.name for state in self.states}
        if self.starting_state is None:
            raise ValueError(
                "starting_state is required when states is non-empty — it must name the "
                "state the conversation enters first."
            )
        if self.starting_state not in state_names:
            raise ValueError(
                f"starting_state '{self.starting_state}' does not match any state's own "
                f"'name' (got: {sorted(state_names)}). starting_state must be one of the "
                "states you're defining in this same request."
            )

        valid_destinations = state_names | {self.starting_state}
        for state in self.states:
            for edge in state.edges:
                if edge.destination_state_name not in valid_destinations:
                    raise ValueError(
                        f"state '{state.name}' has an edge pointing to "
                        f"'{edge.destination_state_name}', which is not the name of any "
                        "state in this request and is not starting_state either. Every edge's "
                        "destination_state_name must resolve to a real state (or back to "
                        "starting_state)."
                    )
        return self

    @model_validator(mode="after")
    def _reject_transfer_fields_under_custom_mode(self) -> CreateAgentRequest:
        """`custom` mode cannot support tool-based transfer, custom
        tools, Multi Prompt (states), OR a fixed welcome_message at all (see
        this model's docstring and response_engine's Field description) —
        any of these set alongside it would be silently meaningless if
        allowed through, which is exactly the "what happens when they get it
        wrong" gap the standards doc requires an explicit, loud answer for.
        Only the presence of a non-null `transfer_number`, a non-empty
        `custom_tools`, a non-empty `states`, a non-null `welcome_message`,
        or a non-null `model`/`post_call_analysis_model` signals real intent
        to configure any of these — the other tuning fields all carry real
        defaults and are meaningless-but-harmless on their own.

        `welcome_message` uses `is not None`, deliberately, NOT a truthy
        check — `welcome_message=""` is a real, meaningful configuration
        (wait silently for the caller), not "unset," so it must trigger this
        same rejection under 'custom' mode exactly like a real greeting
        string would. See this module's docstring, "welcome_message"
        section, for the full three-state reasoning this mirrors.

        `model` is rejected here too, for the identical LLM-object-only
        reason `welcome_message`/`states` already are — it is a real,
        documented `create-retell-llm`/`update-retell-llm`-only field (see
        this module's docstring, "~19-field tuning-knob batch" section),
        with no equivalent concept under `custom` mode's API surface.
        `model_temperature` deliberately is NOT checked here — unlike
        `model`, it has no `None`-means-"not set" state to signal real
        intent with (Retell's own default, `0.0`, and this codebase's own
        unset-default are the same value), so it is exactly as
        meaningless-but-harmless under 'custom' mode as `voice_speed`/
        `interruption_sensitivity` already are, and gets the same pass.
        `post_call_analysis_model` is deliberately NOT checked here either —
        it is an agent-object field (same placement as
        `structured_data_fields`/`agent_name`, confirmed via the same
        WebFetch — see this module's docstring), genuinely available under
        BOTH response_engine modes, so restricting it here would be the
        exact "for consistency" mistake this model's docstring already
        warns a future maintainer against for `structured_data_fields`/
        `agent_name`/`live_transcript_enabled`.
        """
        if self.response_engine == ResponseEngine.CUSTOM:
            if self.transfer_number is not None:
                raise ValueError(
                    "transfer_number can only be set when response_engine='builtin' — "
                    "'custom' cannot support transfer at all today. Either omit "
                    "transfer_number, or switch response_engine to 'builtin'."
                )
            if self.custom_tools:
                raise ValueError(
                    "custom_tools can only be set when response_engine='builtin' — "
                    "'custom' has no vendor-side tool-registration mechanism to "
                    "attach them to. Either omit custom_tools, or switch response_engine to "
                    "'builtin'."
                )
            if self.states:
                raise ValueError(
                    "states can only be set when response_engine='builtin' — 'custom' has "
                    "no vendor-side conversation-brain object to attach Multi Prompt states "
                    "to. Either omit states, or switch response_engine to 'builtin'."
                )
            if self.welcome_message is not None:
                raise ValueError(
                    "welcome_message can only be set when response_engine='builtin' — "
                    "'custom' has no vendor-side conversation-brain object to attach a fixed "
                    "welcome_message to. Either omit welcome_message, or switch "
                    "response_engine to 'builtin'."
                )
            if self.model is not None:
                raise ValueError(
                    "model can only be set when response_engine='builtin' — 'custom' has no "
                    "vendor-side conversation-brain object to attach a model choice to. "
                    "Either omit model, or switch response_engine to 'builtin'."
                )
        return self

    @field_validator("custom_tools")
    @classmethod
    def _reject_duplicate_tool_names(
        cls, value: list[CustomToolDefinition]
    ) -> list[CustomToolDefinition]:
        """Tool names must be unique per agent — the conversation brain uses
        `name` to identify which tool it's calling, so a duplicate would be
        genuinely ambiguous on the voice vendor's own side, not just a
        cosmetic issue.
        """
        names = [tool.name for tool in value]
        if len(names) != len(set(names)):
            raise ValueError(
                "custom_tools entries must have unique 'name' values — found a duplicate."
            )
        return value

    @property
    def languages_list(self) -> list[Language]:
        """Normalize `language` (single code or array, per the wire-format
        decision in this module's docstring) to always a non-empty list —
        the one shape every downstream consumer (agent_repo.create,
        AgentInDB/AgentPublic.languages) actually stores/returns. The
        single-string vs. array distinction only matters at the wire
        boundary; everything past this point uses this property, never
        `self.language` directly.
        """
        if isinstance(self.language, list):
            return self.language
        return [self.language]


class UpdateAgentRequest(BaseModel):
    """PATCH /agents/{agent_id} request body — partial update. Closes the gap
    documented in app/routers/agents.py's module docstring: before this,
    changing anything about an existing agent (prompt, a transfer number, a
    custom tool, any tuning field) had no path except creating an entirely
    new agent, discarding the old one's id/history.

    **Partial-update semantics, same pattern as
    `UpdatePlatformSettingsRequest` (app/models/platform.py) — every field is
    `Optional` with no forcing default, and an OMITTED field means "leave
    this alone," not "reset to CreateAgentRequest's own default."** This is
    the one place this model genuinely differs from that precedent, and it
    matters: `UpdatePlatformSettingsRequest` has no notion of "current value"
    beyond what's already stored (its two fields are simple scalars with an
    explicit null-to-clear convention), so forwarding exactly what the
    caller sent is enough. An agent's tuning fields, by contrast, have real
    defaults on `CreateAgentRequest` (e.g. `voice_speed=1.0`) — if this model
    used the same defaults instead of `None`, there would be no way to tell
    "the caller explicitly wants voice_speed reset to 1.0" apart from "the
    caller didn't mention voice_speed at all," and the second case would
    silently stomp a customized value on every partial update. So every
    field here defaults to `None` specifically to make "not mentioned"
    representable, and the router builds the vendor request body from the
    EXISTING stored agent's values merged with whichever fields are
    non-None here — never from this request body alone. See
    app/routers/agents.py's `update_agent` docstring for exactly how that
    merge happens and why it's necessary even though the voice vendor's own
    two real update endpoints are themselves true partial-merge APIs
    (confirmed via a live WebFetch of both current vendor docs pages this
    session — omitting a field on the vendor's side also means "leave it
    alone," the same convention this model uses) — merging is still
    required on our side for exactly one reason, described in the next
    paragraph.

    **Why merging is still required despite the vendor's endpoints already
    being partial-merge, for the general-tools mechanism specifically.**
    `transfer_number` and `custom_tools` both compile down to entries in ONE
    array field on the vendor's conversation-brain update request (see the
    vendor adapter service's `_build_general_tools` helper). The
    vendor's partial-merge behavior operates at the FIELD level, not the
    array-element level: omitting that array entirely leaves the whole
    array untouched, but INCLUDING it (to change just one of the two things
    it encodes) sends a complete replacement array — there is no vendor-side
    mechanism to say "add/remove just the transfer entry, leave any custom
    entries alone" or vice versa. So the moment either `transfer_number` or
    `custom_tools` is present in this PATCH request, our own adapter must
    rebuild the ENTIRE array from the agent's current values merged with
    whichever of the two the caller actually sent — otherwise changing just
    `custom_tools` would silently drop an existing `transfer_number` (or
    vice versa) purely as a side effect of which field happened to be
    included on the vendor's request, not because the caller asked for
    that. No other field on this model has this coupling; every other field
    (`prompt`, `voice_id`, tuning fields, `structured_data_fields`) is
    genuinely independent, so the router simply forwards the merged value
    for those without any cross-field reasoning.

    **`states` is a SEPARATE array field on the voice vendor's own
    conversation-brain update request (never merged into general_tools) but
    needs the identical whole-object-reconstruction treatment for its own
    reason:
    it is not a true partial-merge field at all (see this module's
    docstring, "states/starting_state" section) — including `states` in an
    update body replaces the entire array, so the router must always send
    the caller's new array (or the agent's existing one, if this PATCH
    didn't touch it) rather than relying on the vendor to merge
    element-by-element, since no such merge exists on the vendor's side for
    this field.

    **`custom_tools`/`structured_data_fields`/`states`: whole-array-replace,
    not merge/diff, when present in this request.** Standard,
    least-surprising PATCH semantics for an array-typed field — the same
    "the value you send is the value that's now stored" behavior any REST
    PATCH with an array body field has. A caller wanting to add one tool to
    an agent that already has three must send all four; there is no
    add/remove-by-name sub-operation. Same for `states` — adding a third
    department to a two-state agent means resending all three states, not
    just the new one. This mirrors how `CreateAgentRequest`'s own array
    fields already work (there is no create-time merge concept either,
    since nothing exists yet) and keeps this model's semantics uniform
    rather than inventing a diff protocol only PATCH would need. Same
    duplicate-name validation as create (`_reject_duplicate_tool_names`/
    `_reject_duplicate_structured_data_field_names`/
    `_reject_duplicate_state_names`, reused directly below rather than
    re-implemented) applies to whichever array the caller actually sends,
    and the same cross-field routing checks
    (`_validate_states_routing`) apply to `states`/`starting_state`
    whenever either is present. Sending `states: []` is the documented way
    to collapse a Multi Prompt agent back to Single Prompt.

    **`response_engine` is deliberately NOT a field on this model —
    switching modes via PATCH is out of scope for this first pass, not a
    silent gap.** The voice vendor's own agent-update docs list an
    equivalent field as technically mutable (confirmed via the same live
    WebFetch), but the docs give no account of what actually happens
    server-side when the underlying TYPE changes (e.g. vendor-hosted-brain
    -> our-own-brain) on an agent that already has calls/history against it,
    versus the narrower, clearly-intended case of pointing the same brain
    type at an updated internal version. Given VoiceAI's own two modes
    point at structurally different vendor objects (`builtin` has a real
    `llm_ref` alongside `vendor_ref`; `custom` has only `vendor_ref`, no
    LLM object at all — see AgentInDB's docstring), a mode switch would
    require either creating a brand-new `llm_ref` (custom
    -> builtin) or deleting the existing one (builtin -> custom) as a real,
    orchestrated side effect of this PATCH — exactly the kind of
    multi-vendor-call, partial-failure-prone operation this module's
    "partial failure" reasoning (see app/routers/agents.py's `update_agent`
    docstring) already has to account for once, for the fields that
    genuinely need it, without extending that same complexity to an
    operation whose vendor-side behavior isn't even confirmed. Rather than
    guess at undocumented vendor behavior and risk silently corrupting an
    agent's vendor-side state, this is a deliberate, defensible scope cut:
    `response_engine` cannot be changed via this endpoint at all. If a
    caller wants a different mode, they still create a new agent — exactly
    today's status quo for that one specific case, unchanged. The router
    does not accept a `response_engine` key in the request body — and
    `model_config` below sets `extra="forbid"` specifically so that sending
    one anyway (or any other unrecognized field name — e.g. a typo, or a
    caller assuming this model mirrors CreateAgentRequest exactly) is a
    loud 422 rather than Pydantic's own default `extra="ignore"` behavior,
    which every other model in this codebase otherwise relies on
    (confirmed: no other model in app/models/ sets `extra="forbid"` — this
    is a deliberate, narrow exception). A silently-ignored `response_engine`
    on a PATCH would be exactly the "looks like it worked, only fails
    obviously once someone tries to use it" trap this codebase's standards
    doc already calls out for `transfer_number` under the wrong mode — a
    caller who genuinely believes they just switched an agent's mode and
    gets back a 200 with `response_engine` unchanged is far worse off than
    a caller who gets an immediate, explicit 422 naming exactly why.

    **`welcome_message` — same omitted-vs-null-vs-clear-flag pattern as
    `transfer_number`, reusing the identical mechanism (`clear_welcome_message`,
    mirroring `clear_transfer_number` field-for-field) rather than inventing a
    new one.** See app/models/agent.py's module docstring, "welcome_message"
    section, for the full three-state (None/""/real-string) semantics this
    field carries on `CreateAgentRequest`, which apply identically here for
    whatever value IS sent; the only new wrinkle on this partial-update model
    is the fourth, orthogonal state — "omitted" — meaning "don't touch,"
    exactly the same layering `transfer_number`/`clear_transfer_number`
    already established.

    **`agent_name` — same omitted-vs-null-vs-clear-flag pattern as
    `welcome_message`/`transfer_number` (`clear_agent_name`, mirroring
    `clear_welcome_message`/`clear_transfer_number` field-for-field), but
    valid on BOTH a `builtin` and a `custom` agent** — see app/models/
    agent.py's module docstring, "agent_name" section, for the full
    agent-object-vs-LLM-object placement reasoning that makes this the one
    field on this model NOT restricted by `_reject_transfer_fields_under_
    update` in app/routers/agents.py.

    **Vendor call sequencing / partial-failure handling** lives in the
    router (app/routers/agents.py's `update_agent`), not here — this model's
    only job is validating and shaping the partial-update request itself.
    """

    model_config = ConfigDict(
        # See this model's docstring for why this is the one deliberate
        # exception to this codebase's otherwise-universal
        # extra="ignore" default.
        extra="forbid",
        json_schema_extra={
            # `example` stays deliberately the smallest realistic call — a
            # genuine, single-field partial PATCH — since that IS the most
            # common real-world shape for this endpoint (unlike
            # CreateAgentRequest, where a comprehensive example is the right
            # default because a create call plausibly sets many fields at
            # once). A giant "every field populated" example here would
            # actively misrepresent how this endpoint is normally used —
            # nobody sends all 39 fields in one PATCH.
            #
            # What a single minimal example does NOT show, though, is which
            # fields exist to change, or the non-obvious clear-flag pattern
            # (clear_transfer_number/clear_welcome_message/clear_agent_name)
            # a developer would otherwise have to read this whole docstring
            # to discover. `examples` below covers that gap with a small set
            # of realistic, narrow, individually-plausible partial-update
            # scenarios — still genuine partial PATCHes, not one everything-
            # at-once example, so the "how this endpoint is really used"
            # signal stays intact while still surfacing real capability.
            "example": {
                "prompt": "You are a friendly front-desk assistant for Aspen Quality Care. "
                "We are now open until 8pm on weekdays.",
            },
            "examples": [
                {
                    "prompt": "You are a friendly front-desk assistant for Aspen Quality "
                    "Care. We are now open until 8pm on weekdays.",
                },
                {
                    "voice_speed": 1.1,
                    "interruption_sensitivity": 0.7,
                    "responsiveness": 0.9,
                    "backchannel_frequency": 0.6,
                },
                {
                    "transfer_number": "+14155550199",
                    "transfer_ring_duration_ms": 45000,
                    "transfer_on_hold_music": "relaxing_sound",
                },
                {
                    "clear_transfer_number": True,
                },
                {
                    "welcome_message": "",
                },
                {
                    "clear_welcome_message": True,
                },
                {
                    "states": [
                        {
                            "name": "billing",
                            "state_prompt": "You are now handling billing questions. Be "
                            "precise about amounts and dates.",
                            "edges": [
                                {
                                    "destination_state_name": "triage",
                                    "description": "When the caller's billing question is "
                                    "resolved or they want something else.",
                                }
                            ],
                            "tools": [],
                        },
                        {
                            "name": "triage",
                            "state_prompt": "Greet the caller and figure out whether they "
                            "need billing help or something else.",
                            "edges": [
                                {
                                    "destination_state_name": "billing",
                                    "description": "When the caller has a billing question.",
                                }
                            ],
                            "tools": [],
                        },
                    ],
                    "starting_state": "triage",
                },
                {
                    "handbook_config": {"speech_normalization": True, "high_empathy": True},
                    "data_storage_setting": "everything_except_pii",
                },
            ],
        },
    )

    prompt: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=10_000,
            description="Replace the agent's base system prompt. Omit to leave the current "
            "prompt unchanged.",
        ),
    ] = None
    voice_id: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=200,
            description="Replace the agent's voice. Omit to leave the current voice unchanged. "
            "Browse available voices via GET /voices.",
        ),
    ] = None
    language: Annotated[
        Language | list[Language] | None,
        Field(
            description="Replace the agent's language/locale configuration — same single-code-"
            "or-array shape as CreateAgentRequest.language. Omit to leave the current "
            "language(s) unchanged."
        ),
    ] = None
    voice_speed: Annotated[
        float | None,
        Field(
            ge=0.5,
            le=2.0,
            description="Replace the playback speed multiplier (range 0.5-2.0). Omit to leave "
            "the current value unchanged.",
        ),
    ] = None
    interruption_sensitivity: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=1.0,
            description="Replace how readily the agent yields the floor when interrupted "
            "(range 0.0-1.0). Omit to leave the current value unchanged.",
        ),
    ] = None
    enable_backchannel: Annotated[
        bool | None,
        Field(
            description="Replace whether the agent makes backchannel acknowledgement sounds. "
            "Omit to leave the current value unchanged."
        ),
    ] = None
    pronunciation_dictionary: Annotated[
        list[PronunciationEntry] | None,
        Field(
            description="Replace the agent's full word-level pronunciation override list "
            "(whole-array-replace, not merged — see this model's docstring). Omit to leave "
            "the current list unchanged; send an empty array to clear it."
        ),
    ] = None
    transfer_number: Annotated[
        str | None,
        Field(
            pattern=r"^\+[1-9]\d{1,14}$",
            description="Replace the warm-transfer destination number. Only valid on a "
            "'builtin' agent (see this model's docstring for why response_engine itself can't "
            "be changed here) — REJECTED with 422 if set on a 'custom' agent, same "
            "reasoning as CreateAgentRequest.transfer_number. Omit to leave the current "
            "transfer configuration unchanged. Send null explicitly to REMOVE an existing "
            "transfer_number (disable transfer) without touching anything else — null and "
            "'omitted' are different here: omitted means 'don't touch', explicit null in a "
            "request that otherwise sets transfer_ring_duration_ms/transfer_on_hold_music/"
            "transfer_show_original_caller_id means 'turn transfer off'. Use "
            "clear_transfer_number below for the unambiguous way to express removal.",
        ),
    ] = None
    clear_transfer_number: Annotated[
        bool,
        Field(
            description="Set true to explicitly remove this agent's transfer_number (disable "
            "transfer) without providing a replacement. Since `transfer_number: null` is "
            "indistinguishable from 'omitted' in a JSON body once Pydantic sees it as the same "
            "None value either way, this separate boolean flag is the only unambiguous way to "
            "say 'clear it' versus 'I didn't mention it' — same reasoning JSON Merge Patch "
            "(RFC 7396) style APIs run into with nullable fields, resolved here with an "
            "explicit sibling flag rather than a sentinel value. Ignored (has no effect) if "
            "transfer_number is also set in the same request — an explicit new value always "
            "wins over a clear flag.",
        ),
    ] = False
    welcome_message: Annotated[
        str | None,
        Field(
            max_length=2000,
            description="Replace the agent's opening line — same three-state semantics as "
            "CreateAgentRequest.welcome_message (omit/null to leave unchanged here; a "
            "non-empty string for a fixed verbatim greeting; \"\" for the agent to wait "
            "silently for the caller to speak first). Only valid on a 'builtin' agent, same "
            "reasoning as transfer_number above. Omit to leave the current welcome_message "
            "unchanged — note that 'omitted' here means 'don't touch', not 'clear it', "
            "exactly the same omitted-vs-null ambiguity transfer_number already has, since "
            "both null and 'omitted' collapse to the same Python None once Pydantic parses "
            "this request. Use clear_welcome_message below for the unambiguous way to reset "
            "this agent back to the default improvised-greeting behavior.",
        ),
    ] = None
    clear_welcome_message: Annotated[
        bool,
        Field(
            description="Set true to explicitly reset this agent's welcome_message back to "
            "the default improvised-greeting behavior (equivalent to never having set "
            "welcome_message at all). Same clear-flag mechanism as clear_transfer_number "
            "above, for the identical reason: 'welcome_message: null' is indistinguishable "
            "from 'omitted' once Pydantic parses this request, so this separate boolean is "
            "the only unambiguous way to say 'clear it' versus 'I didn't mention it'. "
            "Ignored (has no effect) if welcome_message is also set in the same request — an "
            "explicit new value always wins over a clear flag.",
        ),
    ] = False
    agent_name: Annotated[
        str | None,
        Field(
            description="Replace your own internal reference label for this agent. Omit to "
            "leave the current agent_name unchanged — note that 'omitted' here means 'don't "
            "touch', not 'clear it', same omitted-vs-null ambiguity welcome_message/"
            "transfer_number already have. Use clear_agent_name below for the unambiguous way "
            "to reset this agent back to having no agent_name configured. Unlike "
            "welcome_message/transfer_number/custom_tools/states, valid on BOTH a 'builtin' "
            "and a 'custom' agent — see app/models/agent.py's module docstring, \"agent_name\" "
            "section, for why.",
        ),
    ] = None
    clear_agent_name: Annotated[
        bool,
        Field(
            description="Set true to explicitly reset this agent's agent_name back to unset. "
            "Same clear-flag mechanism as clear_welcome_message/clear_transfer_number above, "
            "for the identical reason: 'agent_name: null' is indistinguishable from 'omitted' "
            "once Pydantic parses this request, so this separate boolean is the only "
            "unambiguous way to say 'clear it' versus 'I didn't mention it'. Ignored (has no "
            "effect) if agent_name is also set in the same request — an explicit new value "
            "always wins over a clear flag.",
        ),
    ] = False
    live_transcript_enabled: Annotated[
        bool | None,
        Field(
            description="Replace whether this agent is subscribed to live, per-turn "
            "transcript updates (see CreateAgentRequest.live_transcript_enabled). Omit to "
            "leave the current setting unchanged — unlike transfer_number/welcome_message/"
            "agent_name, a plain bool has no omitted-vs-null ambiguity, so no separate clear "
            "flag is needed: send true/false to change it, or omit the field entirely to "
            "leave it as-is. Available on BOTH 'builtin' and 'custom' agents, same as "
            "agent_name.",
        ),
    ] = None
    transfer_ring_duration_ms: Annotated[
        int | None,
        Field(
            ge=5000,
            le=90000,
            description="Replace how long (ms) to ring transfer_number before giving up. Omit "
            "to leave the current value unchanged. Meaningless if the agent has no "
            "transfer_number configured (current or newly-set).",
        ),
    ] = None
    transfer_on_hold_music: Annotated[
        OnHoldMusic | None,
        Field(
            description="Replace what the caller hears on hold during a warm transfer. Omit "
            "to leave the current value unchanged."
        ),
    ] = None
    transfer_show_original_caller_id: Annotated[
        bool | None,
        Field(
            description="Replace whether the transfer recipient sees the original caller's "
            "number. Omit to leave the current value unchanged."
        ),
    ] = None
    custom_tools: Annotated[
        list[CustomToolDefinition] | None,
        Field(
            max_length=MAX_CUSTOM_TOOLS,
            description="Replace the agent's full custom_tools list (whole-array-replace, not "
            "merged/diffed — see this model's docstring). Only valid on a 'builtin' agent, "
            "same reasoning as CreateAgentRequest.custom_tools. Omit to leave the current "
            "list unchanged; send an empty array to remove all custom tools.",
        ),
    ] = None
    structured_data_fields: Annotated[
        list[StructuredDataFieldDefinition] | None,
        Field(
            max_length=MAX_STRUCTURED_DATA_FIELDS,
            description="Replace the agent's full structured_data_fields list "
            "(whole-array-replace, not merged/diffed — see this model's docstring). Available "
            "under BOTH response_engine modes, same as CreateAgentRequest.structured_data_"
            "fields. Omit to leave the current list unchanged; send an empty array to remove "
            "all fields.",
        ),
    ] = None
    states: Annotated[
        list[AgentState] | None,
        Field(
            max_length=MAX_STATES,
            description="Replace the agent's full Multi Prompt states list (whole-array-"
            "replace, not merged/diffed — see this model's docstring). Only valid on a "
            "'builtin' agent, same reasoning as CreateAgentRequest.states. Omit to leave the "
            "current states unchanged; send an empty array to collapse the agent back to "
            "Single Prompt. When present and non-empty, starting_state must also be present "
            "in this SAME request (a new states array with no accompanying starting_state is "
            "rejected — the agent's previous starting_state may not even name one of the new "
            "states, so it can never be silently reused).",
        ),
    ] = None
    starting_state: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=100,
            description="Replace which state's `name` the conversation enters first. Must be "
            "sent in the SAME request as a non-empty `states` (see states' own description "
            "above for why the previous starting_state can't be silently carried over). "
            "Meaningless — and rejected — if sent without a non-empty `states` in this same "
            "request; omit both together to leave the agent's Multi Prompt configuration "
            "unchanged.",
        ),
    ] = None
    model: Annotated[
        str | None,
        Field(
            description="Replace which text LLM powers the conversation. Only valid on a "
            "'builtin' agent, same reasoning as CreateAgentRequest.model. Omit to leave the "
            "current model unchanged — there is no vendor-documented way to explicitly clear "
            "this back to the bare vendor default via PATCH once set.",
        ),
    ] = None
    model_temperature: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=1.0,
            description="Replace the conversation model's response randomness (range "
            "0.0-1.0). Omit to leave the current value unchanged.",
        ),
    ] = None
    voice_model: Annotated[
        str | None,
        Field(
            description="Replace which TTS engine/quality tier renders the agent's voice. "
            "Omit to leave the current value unchanged.",
        ),
    ] = None
    voice_temperature: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=2.0,
            description="Replace how much expressive variation the TTS engine applies "
            "(range 0.0-2.0). Omit to leave the current value unchanged.",
        ),
    ] = None
    stt_mode: Annotated[
        SttMode | None,
        Field(description="Replace the speech-to-text mode. Omit to leave the current value "
        "unchanged."),
    ] = None
    denoising_mode: Annotated[
        DenoisingMode | None,
        Field(description="Replace how aggressively background noise is filtered. Omit to "
        "leave the current value unchanged."),
    ] = None
    ambient_sound: Annotated[
        AmbientSound | None,
        Field(description="Replace the background ambience preset. Omit to leave the current "
        "value unchanged; there is no separate clear flag — sending an explicit request with "
        "no ambient_sound field leaves the existing preset (if any) untouched, same as every "
        "other plain-scalar field in this batch."),
    ] = None
    ambient_sound_volume: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=2.0,
            description="Replace the volume of ambient_sound (range 0.0-2.0). Omit to leave "
            "the current value unchanged.",
        ),
    ] = None
    backchannel_frequency: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=1.0,
            description="Replace how often the agent makes backchannel sounds (range "
            "0.0-1.0). Omit to leave the current value unchanged.",
        ),
    ] = None
    backchannel_words: Annotated[
        list[str] | None,
        Field(description="Replace the agent's full backchannel word list (whole-array-"
        "replace, not merged — see this model's docstring). Omit to leave the current list "
        "unchanged; send an empty array to reset to the voice vendor's own default word set."),
    ] = None
    responsiveness: Annotated[
        float | None,
        Field(
            ge=0.0,
            le=1.0,
            description="Replace how quickly the agent replies after the caller stops "
            "talking (range 0.0-1.0). Omit to leave the current value unchanged.",
        ),
    ] = None
    reminder_trigger_ms: Annotated[
        int | None,
        Field(
            gt=0,
            description="Replace how long (ms) the agent waits in silence before "
            "proactively re-prompting. Omit to leave the current value unchanged.",
        ),
    ] = None
    reminder_max_count: Annotated[
        int | None,
        Field(
            ge=0,
            description="Replace how many times the agent will proactively re-prompt per "
            "call. Omit to leave the current value unchanged.",
        ),
    ] = None
    end_call_after_silence_ms: Annotated[
        int | None,
        Field(
            ge=MIN_END_CALL_AFTER_SILENCE_MS,
            description="Replace the pure-silence hangup threshold (ms). Omit to leave the "
            "current value unchanged.",
        ),
    ] = None
    max_call_duration_ms: Annotated[
        int | None,
        Field(
            ge=MIN_MAX_CALL_DURATION_MS,
            le=MAX_MAX_CALL_DURATION_MS,
            description="Replace the total call duration cap (ms). Omit to leave the "
            "current value unchanged.",
        ),
    ] = None
    begin_message_delay_ms: Annotated[
        int | None,
        Field(
            ge=0,
            le=MAX_BEGIN_MESSAGE_DELAY_MS,
            description="Replace the pause (ms) before the agent speaks its opening line. "
            "Omit to leave the current value unchanged.",
        ),
    ] = None
    allow_user_dtmf: Annotated[
        bool | None,
        Field(description="Replace whether the caller's keypad (DTMF) input is accepted. "
        "Omit to leave the current value unchanged."),
    ] = None
    allow_dtmf_interruption: Annotated[
        bool | None,
        Field(description="Replace whether pressing a key interrupts the agent mid-sentence. "
        "Omit to leave the current value unchanged."),
    ] = None
    data_storage_setting: Annotated[
        DataStorageSetting | None,
        Field(description="Replace how much of a call's own data the voice vendor retains "
        "after the call ends. Omit to leave the current value unchanged."),
    ] = None
    pii_config: Annotated[
        PiiConfig | None,
        Field(description="Replace the agent's PII redaction configuration (whole-object-"
        "replace, not merged — see this model's docstring). Omit to leave the current "
        "configuration unchanged. An empty categories list is rejected (same as on create) "
        "since it would configure redaction of nothing — there is no way to explicitly clear "
        "pii_config back to 'unconfigured' via this endpoint once set."),
    ] = None
    post_call_analysis_model: Annotated[
        str | None,
        Field(description="Replace which model performs post-call structured-data "
        "extraction. Omit to leave the current value unchanged."),
    ] = None
    handbook_config: Annotated[
        HandbookConfig | None,
        Field(description="Replace the agent's handbook_config toggles (whole-object-"
        "replace, not merged — see this model's docstring). Omit to leave the current "
        "configuration unchanged."),
    ] = None

    @field_validator("pii_config")
    @classmethod
    def _reject_empty_pii_categories(cls, value: PiiConfig | None) -> PiiConfig | None:
        """Same defense-in-depth check as CreateAgentRequest's own validator
        of the identical name — see that validator's docstring.
        """
        if value is not None and not value.categories:
            raise ValueError(
                "pii_config.categories must be non-empty when pii_config is set — an empty "
                "list would configure redaction of nothing."
            )
        return value

    @field_validator("language")
    @classmethod
    def _validate_language_list(
        cls, value: Language | list[Language] | None
    ) -> Language | list[Language] | None:
        """Identical check to CreateAgentRequest's own validator, just also
        tolerating None (omitted). See that validator's docstring for the
        full reasoning.
        """
        if isinstance(value, list):
            if len(value) == 0:
                raise ValueError(
                    "language array must not be empty — provide at least one language code, "
                    "or send a single code as a plain string instead of an array, or omit this "
                    "field entirely to leave the current language(s) unchanged."
                )
            if len(value) > MAX_LANGUAGES:
                raise ValueError(
                    f"language array must not exceed {MAX_LANGUAGES} codes (got {len(value)}) "
                    "— this is our own sane limit, not a voice-vendor-documented maximum."
                )
        return value

    @field_validator("structured_data_fields")
    @classmethod
    def _reject_duplicate_structured_data_field_names(
        cls, value: list[StructuredDataFieldDefinition] | None
    ) -> list[StructuredDataFieldDefinition] | None:
        if value is None:
            return None
        names = [field.name for field in value]
        if len(names) != len(set(names)):
            raise ValueError(
                "structured_data_fields entries must have unique 'name' values — found a "
                "duplicate."
            )
        return value

    @field_validator("custom_tools")
    @classmethod
    def _reject_duplicate_tool_names(
        cls, value: list[CustomToolDefinition] | None
    ) -> list[CustomToolDefinition] | None:
        if value is None:
            return None
        names = [tool.name for tool in value]
        if len(names) != len(set(names)):
            raise ValueError(
                "custom_tools entries must have unique 'name' values — found a duplicate."
            )
        return value

    @field_validator("states")
    @classmethod
    def _reject_duplicate_state_names(
        cls, value: list[AgentState] | None
    ) -> list[AgentState] | None:
        if value is None:
            return None
        names = [state.name for state in value]
        if len(names) != len(set(names)):
            raise ValueError("states entries must have unique 'name' values — found a duplicate.")
        return value

    @model_validator(mode="after")
    def _validate_states_routing(self) -> UpdateAgentRequest:
        """Same cross-field routing checks as
        CreateAgentRequest._validate_states_routing, adapted for this
        model's all-optional/omitted-means-unchanged semantics. Unlike
        create, this model has no access to the agent's EXISTING states —
        only the router (which fetches the current AgentInDB) does — so
        this validator deliberately only validates SELF-CONSISTENCY of
        whatever this one request sent, never a stale/omitted starting_state
        against a states array the caller didn't touch. Concretely:

        - `states` non-empty with no `starting_state` in this same request
          is rejected — see the `states`/`starting_state` Field
          descriptions above for why silently reusing the agent's previous
          starting_state would be unsound (it may not even name one of the
          NEW states).
        - `starting_state` set without a non-empty `states` in this same
          request is rejected — there is no new states array for it to
          apply to, and silently applying it to the agent's EXISTING states
          would be a surprising, un-requested side effect.
        - When both are present together, the identical uniqueness/
          resolution checks CreateAgentRequest enforces (starting_state
          matches a real state name; every edge resolves to a real state or
          back to starting_state) apply here too.
        """
        if self.states is None and self.starting_state is None:
            return self

        if self.states is not None and not self.states:
            if self.starting_state is not None:
                raise ValueError(
                    "starting_state must be omitted when states is set to an empty array — "
                    "there is nothing left for it to name."
                )
            return self

        if self.states is None:
            raise ValueError(
                "starting_state was set without states in the same request — starting_state "
                "only applies to a states array you're replacing in this same PATCH. Include "
                "the full new states list alongside it, or omit starting_state to leave the "
                "agent's Multi Prompt configuration unchanged."
            )
        if self.starting_state is None:
            raise ValueError(
                "starting_state is required in the same request whenever states is non-empty "
                "— it must name the state the conversation enters first. The agent's previous "
                "starting_state cannot be silently reused, since it may not even name one of "
                "the new states."
            )

        state_names = {state.name for state in self.states}
        if self.starting_state not in state_names:
            raise ValueError(
                f"starting_state '{self.starting_state}' does not match any state's own "
                f"'name' (got: {sorted(state_names)}). starting_state must be one of the "
                "states you're sending in this same request."
            )

        valid_destinations = state_names | {self.starting_state}
        for state in self.states:
            for edge in state.edges:
                if edge.destination_state_name not in valid_destinations:
                    raise ValueError(
                        f"state '{state.name}' has an edge pointing to "
                        f"'{edge.destination_state_name}', which is not the name of any "
                        "state in this request and is not starting_state either. Every edge's "
                        "destination_state_name must resolve to a real state (or back to "
                        "starting_state)."
                    )
        return self

    @property
    def languages_list(self) -> list[Language] | None:
        """Same normalization as CreateAgentRequest.languages_list, tolerant
        of None (field omitted -> no change).
        """
        if self.language is None:
            return None
        if isinstance(self.language, list):
            return self.language
        return [self.language]

    def has_any_field_set(self) -> bool:
        """True if this request actually changes something. An entirely-
        empty PATCH body (every field omitted, clear_transfer_number,
        clear_welcome_message, and clear_agent_name all left False) is
        accepted as a well-formed no-op by Pydantic (every field has a
        None/False default), but the router rejects it with a clear 422
        rather than doing a no-op round-trip to the vendor and back — see
        app/routers/agents.py's `update_agent` docstring for why a silent
        no-op is worse than an explicit "you didn't ask for anything" error
        here.
        """
        return (
            any(
                value is not None
                for field_name, value in self.model_dump(
                    exclude={
                        "clear_transfer_number",
                        "clear_welcome_message",
                        "clear_agent_name",
                    }
                ).items()
            )
            or self.clear_transfer_number
            or self.clear_welcome_message
            or self.clear_agent_name
        )
