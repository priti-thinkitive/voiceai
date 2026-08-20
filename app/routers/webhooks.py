"""POST /webhooks/retell/inbound — inbound dynamic-variable injection.

This is a webhook WE RECEIVE from the voice vendor, not an endpoint Platform
X calls — the reverse direction from every other router in this codebase, so
it lives in its own `webhooks` module (singular-purpose file, not folded into
agents.py/calls.py) and is wired into Retell-facing config
(`inbound_webhook_url`, see retell_adapter.create_phone_number/
import_phone_number) rather than anything Platform X sees in Swagger.

**Confirmed real mechanism (already researched, not guessed this session):**
when a call rings in to a number we've provisioned, the voice vendor
synchronously POSTs to the `inbound_webhook_url` configured on that number
(now set on every purchase/import — see retell_adapter.py) and WAITS for our
response before answering:

    Vendor -> us:  POST <our inbound_webhook_url>
        {"event": "call_inbound",
         "call_inbound": {"agent_id": ..., "agent_version": ...,
                           "from_number": ..., "to_number": ...,
                           "custom_sip_headers": {...}},
         "event_timestamp": ...}

    Us -> vendor:  200 OK
        {"call_inbound": {"dynamic_variables": {...},
                           "reject": bool (optional),
                           "override_agent_id": "..." (optional),
                           "override_agent_version": ... (optional),
                           "metadata": {...} (optional),
                           "agent_override": {...} (optional)}}

Real ceiling: the vendor waits up to 10 seconds and retries up to 3 times on
a non-2xx response. This endpoint only ever sets `dynamic_variables` on the
response — `reject`/`override_agent_id`/`override_agent_version`/`metadata`/
`agent_override` are real, documented, optional fields but out of this
task's scope (no caller-facing feature needs them yet, per the "no
unnecessary fields" rule — add them when something real needs to).

**The "not our fault" design mandate, the whole point of this endpoint's
structure (explicit user requirement, not a nice-to-have):** our own
processing (steps 1-2 below) must be minimal, our relay to Platform X (step
3) carries its own strict short timeout
(`platform_relay.PLATFORM_RELAY_TIMEOUT_SECONDS`, currently 4s — well inside
the vendor's ~10s ceiling), and ANY failure/slowness on Platform X's side
still gets a fast, safe fallback response sent to the vendor — never a
timeout on OUR side of this exchange. See platform_relay.py's module
docstring for the full reasoning.

Every step is timestamped and logged with clearly separated segments (see
`_log_outcome` below) specifically so "was a delay Platform X's fault or
ours" is answerable from a single structured log line after the fact, not
just true in theory:

  1. `t_received`  — the instant this handler starts (after signature
     verification, before any DB/relay work).
  2. `t_before_relay` — right before the relay call to Platform X (if any).
     `t_before_relay - t_received` = OUR OWN processing time (DB lookups,
     platform/agent resolution) BEFORE we've even reached out to Platform X.
  3. `t_after_relay` — right after Platform X responds or times out.
     `t_after_relay - t_before_relay` = Platform X's own response time
     (or our own timeout duration, if they never answered).
  4. `t_responding` — right before we send our final response to the vendor.
     `t_responding - t_after_relay` = any final processing on our side after
     getting Platform X's answer (building the response body).

Logged as `our_processing_ms_before_relay`, `platform_x_response_ms`,
`our_processing_ms_after_relay`, `total_ms` — four separate numbers, not one
combined duration, so a slow DB lookup on OUR side is never hidden behind
"used fallback due to timeout" as if it were automatically Platform X's
fault. The "not our fault" framing only holds up if our own segments are
consistently fast AND that fact is provable from logs, not just asserted.

**Signature verification — real scheme, sourced from a fresh live WebFetch of
Retell's OWN CURRENT docs this session
(`docs.retellai.com/features/secure-webhook.md`), NOT copied from
eCareVoiceAI's code — a deliberate, explicit exception to this project's
usual "trust the working sibling-project code over docs" tie-breaker.**

eCareVoiceAI's `webhooks/retell.py:_verify_signature` (bare
`hexdigest()` over the raw body, keyed by a separate `RETELL_WEBHOOK_SECRET`)
was the original source for this endpoint's signature check, and is still a
real, working pattern — but it was written against an earlier/simpler
version of Retell's scheme. This session, Retell's own current documentation
was re-checked live (not assumed carried over) and turned out to describe a
meaningfully different, newer scheme:

    Header (unchanged):    X-Retell-Signature
    Value format (NEW):    "v={timestamp_ms},d={hex_digest}"
    Algorithm (unchanged): HMAC-SHA256
    Signed content (NEW):  raw_body + timestamp_string (string concat, NOT
                            raw_body alone)
    Secret (NEW):          RETELL_API_KEY (Retell's own Node/Python SDK
                            examples pass process.env.RETELL_API_KEY /
                            os.environ["RETELL_API_KEY"] into their verify()
                            helper) — NOT a separate webhook-only secret
    Replay protection (NEW): timestamp must be within 5 minutes of now

The user was shown this exact discrepancy and explicitly chose to follow
Retell's current docs over eCareVoiceAI's older working code for this one
mechanism — reasoning: eCareVoiceAI's simpler bare-hexdigest scheme plausibly
reflects a pre-deprecation version of Retell's own API, and a signature
scheme is exactly the kind of thing a vendor tightens over time (the 5-minute
replay window is a genuinely newer security property eCareVoiceAI's version
doesn't have at all). Following the vendor's own current docs is the safer
long-term choice here, even though it goes against this project's normal
tie-breaker rule. This is recorded as a deliberate, reasoned exception, not a
silent deviation from that rule.

**UNVERIFIED ASSUMPTION, flagged explicitly per the standards doc's
"say so plainly when a vendor detail is genuinely unconfirmed" rule — do
not silently rely on this in production without the user confirming it
directly first:** Retell's docs state "only the API key that has a webhook
badge next to it can be used to verify the webhook" — implying this is a
per-key, dashboard-side setting that this codebase has no way to inspect or
confirm. Whether this project's currently-configured `RETELL_API_KEY` in
`.env` actually has that badge is NOT verified here and CANNOT be verified
from code — it can only be confirmed by looking at the real Retell dashboard
directly. If it turns out the configured key lacks the badge, every webhook
signature will genuinely fail verification (a real, currently-open risk,
not a hypothetical one) even though the request came from the real vendor.

`hmac.compare_digest` (never `==`) is still used for the digest comparison,
still verified BEFORE parsing JSON — those two aspects of the original
pattern were already correct and carry over unchanged.

**Fail-closed/dev-permissive contract, re-derived for the new secret source
(RETELL_API_KEY), not blindly carried over from the old
RETELL_WEBHOOK_SECRET-keyed logic**: secret (RETELL_API_KEY) set -> full
verification required (format + digest + timestamp freshness) in any
environment, bad/missing/stale signature -> `401`; secret unset AND
`ENV == "production"` -> refuse the request (`500`) — this is already
consistent with `Settings.assert_production_secrets()`, which independently
requires `RETELL_API_KEY` to be set in production anyway (it's needed for
outbound Retell API calls to work at all, not just webhook verification), so
this path is effectively unreachable in a correctly configured production
deployment, but the runtime backstop is kept anyway per the standards doc's
belt-and-suspenders rule; secret unset AND dev/test -> allow unverified (so
local dev/tunnels work without a real key configured).

**`RETELL_WEBHOOK_SECRET` removed entirely** (from `Settings`, `.env`,
`.env.example`, `assert_production_secrets()`) — it is no longer read
anywhere; keeping an unused, misleadingly-named secret around would be more
confusing than helpful once it plays no role in verification.


POST /webhooks/retell/post-call — recording/transcript re-hosting
===================================================================

Per `vendor-docs/White-Label-Launch-Plan.html` Phase 1 item 15 ("Recording &
transcript delivery"): when a call ends, re-host the recording/transcript to
OUR OWN S3 bucket rather than ever handing Platform X a voice-vendor-hosted
URL ("re-host, don't pass through" — the plan doc's own words). This is the
highest-stakes instance of the vendor-URL-leak class already found and fixed
twice this session (GET /voices' preview_audio_url, and Swagger
documentation text): real call recordings, not voice previews.

**Confirmed real mechanism** (WebFetch of docs.retellai.com/features/
webhook-overview.md this session; cross-checked against eCareVoiceAI's own
real, working `webhooks/retell.py:_process_post_call_payload`, which reads
the exact same nested field paths off a real, working integration — see
app/models/post_call.py's module docstring for the full sourced payload
shape). Retell sends up to THREE separate webhook deliveries per call, in
order but not blocking each other:

  - `call_started`  — call begins (not sent at all if the dial never
    connected). Not handled by this endpoint — nothing to re-host yet, and
    POST /calls/outbound already records the call's initial "registered"
    state; a call_started event this handler receives is acknowledged and
    ignored, not an error.
  - `call_ended`    — call finishes (always sent once a call started,
    including on error/transfer). Carries `call.recording_url`,
    `call.transcript`, and `call.disconnection_reason` (confirmed real,
    directly on `call` rather than inside `call_analysis` — e.g.
    `"user_hangup"`, `"voicemail_reached"`, one of 37 documented values, see
    app/models/post_call.py's `RetellPostCallDetails`), but usually NOT
    `call.call_analysis` yet (analysis is often still processing at this
    point — confirmed via eCareVoiceAI's own working-code comment:
    "call_ended fires with no call_analysis block... when call_analyzed
    hasn't landed yet — normal mid-flight").
  - `call_analyzed` — fires ~5s later (per eCareVoiceAI's own observed
    timing) with the full `call.call_analysis` object (`call_summary`,
    `user_sentiment`, `custom_analysis_data`, `in_voicemail`, ...)
    populated. `custom_analysis_data` is the real output of the
    structured-data-extraction feature (see app/models/agent.py's
    `structured_data_fields` docstring for the request-side configuration)
    — read here and passed through to `call_repo.update_post_call_outcome`
    as `extracted_data`, same as `call_summary`/`user_sentiment` are passed
    through as `summary`/`sentiment`. `in_voicemail` is the real result of
    the voicemail-detection feature (see app/models/call.py's
    `CreateOutboundCallRequest.voicemail_detection` docstring for the
    request-side trigger) — passed through the same way, on `call_analyzed`
    only (same "only populated once analysis exists" reasoning as
    `summary`/`sentiment`), while `disconnection_reason` is read from
    `call_ended` (or `call_analyzed`, if that's the first event actually
    delivered) since it lives directly on `call`, not inside
    `call_analysis`.

Both `call_ended` and `call_analyzed` trigger the same re-hosting work below
— re-processing is safe and expected (see idempotency below), so there is no
need to special-case which of the two arrived first; whichever event this
handler sees, it re-hosts whatever's present.

**Registration point — per-agent, not per-phone-number, confirmed**: unlike
the INBOUND pre-call webhook above (`inbound_webhook_url`, set on each
phone-number purchase/import, since inbound calls are number-scoped), the
post-call webhook is tied to the call's AGENT, not the number that was
called — Retell's own webhook-overview docs describe both an account-level
default and an agent-level override ("If set, account level webhooks will
not be triggered for that agent"). This project registers it at
agent-creation time (`retell_adapter.create_agent()`'s `webhook_url` param,
wired from `app/routers/agents.py`), matching Retell's documented agent-
level registration point exactly, rather than relying on an
account-level/dashboard-configured default this codebase has no visibility
into or control over.

**Latency design — the exact inverse of the inbound webhook above, and
deliberately so.** The inbound webhook (`/inbound`, above) has a real,
tight latency budget because a live caller is on hold waiting for Retell to
answer — every millisecond of our own processing directly delays a real
phone call. This endpoint has the opposite shape: by the time `call_ended`/
`call_analyzed` fires, the call has ALREADY ended — nobody is waiting on the
phone, so there is no live-caller latency budget to protect. What DOES
matter here is Retell's own webhook delivery contract: non-2xx responses
are retried (per this module's docstring above, same "up to 3 retries"
mechanism as the inbound webhook), so a SLOW 200 risks nothing directly, but
an accidentally-timed-out or crashed request risks a spurious retry
re-attempting the same download-from-Retell + upload-to-S3 work.

So the design here is standard "acknowledge fast, do the real work async":
this handler verifies the signature, parses the payload, does the one fast
indexed lookup to resolve which Calls document this concerns
(`call_repo.get_by_vendor_ref` — see database.py's Calls.vendor_ref index
docstring), and returns `200` immediately — the actual download-from-Retell
+ upload-to-S3 + Calls-record-update sequence (which can genuinely take
several real seconds for an audio file) runs AFTER that response, via
FastAPI's `BackgroundTasks` (the simplest correct choice for a single-
process FastAPI app with no existing task-queue/worker infrastructure — see
`_process_finished_call` below). This means: Retell gets acknowledged fast
regardless of how long S3 takes; a genuinely slow/failing S3 step can never
cause Retell to see a timeout and retry a webhook that we, in fact, already
received and are processing; and "send Platform X their own webhook with
the re-hosted links" (see app/services/call_completed_webhook.py, wired in
at the end of `_process_finished_call`) fires only once this background
work actually finishes — naturally sequenced after re-hosting, not before,
since that's the first moment there's real, complete (or partially
complete — see the partial-success design below) data to send.

**Idempotency, decided**: Retell may retry a webhook delivery (non-2xx
response, up to 3 times) or, independently, send `call_ended` and
`call_analyzed` as two genuinely separate deliveries for the same call — both
cases must be safe to re-process, never duplicating an S3 upload or
corrupting the Calls record. Two mechanisms combine to guarantee this:
  1. **Deterministic S3 keys, keyed by our own Calls document's Mongo
     `_id`** (see storage.py's `recording_key`/`transcript_key`) — a
     re-upload to the same key is a plain S3 overwrite, not a duplicate
     object, with no separate "already uploaded" bookkeeping needed.
  2. **A plain Mongo `$set` overwrite on the Calls document**
     (`call_repo.update_post_call_outcome`) — re-processing the same
     call_id just re-writes the same fields, matching eCareVoiceAI's own
     confirmed real pattern ("Idempotent on retell_call_id — re-deliveries
     just overwrite").

**Partial-success design, decided explicitly (not left undefined)**: a
call's own outcome (status/summary/sentiment/transcript text) and the
re-hosting of its recording/transcript to S3 are treated as SEPARATE
concerns with separate failure semantics — mirroring this project's
existing persist-on-vendor-failure precedent (POST /agents, POST
/calls/outbound: "the real, meaningful event is recorded regardless of a
downstream step's success"). A finished-call webhook always updates
`status`/`summary`/`sentiment` on the Calls record even if the S3 upload
step genuinely fails (e.g. today's empty `AWS_S3_BUCKET` placeholder) — a
call that actually completed must never be left looking "still in
progress" just because our own storage integration isn't configured yet.
`CallInDB.recording_rehost_failed` (see app/models/call.py) is the explicit
signal for this partial state: re-hosting was attempted and failed, as
opposed to "not attempted yet" — see that field's docstring for the full
reasoning.

**Signature verification**: identical mechanism to `/inbound` above (same
`_verify_signature` function, reused rather than reimplemented — the
standards doc's Webhook security section documents this as one unified
mechanism for all of Retell's webhook types, and eCareVoiceAI's own working
code uses the exact same `_verify_signature` helper across every one of its
own Retell webhook routes, not a per-route reimplementation).

**Inbound-call gap, closed**: Retell's real post-call webhook fires for
EVERY finished call, inbound or outbound — Retell does not distinguish (see
app/models/post_call.py's docstring: the vendor's own sample payload even
carries its own `direction` field, deliberately not trusted here). But only
`POST /calls/outbound` (app/routers/calls.py) ever called `call_repo.create`
until now — a call that rang in on one of Platform X's provisioned numbers
with no prior outbound trigger had no Calls document for this handler's
`get_by_vendor_ref` lookup to find, so the event was silently dropped
(`{"ok": true, "unrecognized_call_id": true}`, with no record ever created).
That meant `GET /calls/{id}` could never find an inbound call, the
call-completed notification never fired for one, and there was no way for
Platform X to ever learn what happened on an inbound call through this API
at all — a real, user-facing gap, not a hypothetical one.

Closed by having `handle_post_call` create the missing Calls document
itself, at the exact moment it discovers one is missing, using data already
present on the very webhook payload that revealed the gap: `call.call_id`
becomes `vendor_ref` (so future re-deliveries for the same call resolve to
this same record — the ordinary idempotency path, unchanged), and
`call.agent_id` (the vendor's own agent id) is resolved to one of our own
Agents documents via `agent_repo.get_by_vendor_ref` — the exact same lookup
`handle_custom_tool_call` below already uses to turn a vendor `agent_id`
into `platform_id`/`agent_id` ownership. That resolved Agent's own
`platform_id` becomes the new record's `platform_id` — there is no other way
to learn which Platform owns an inbound call we were never told about in
advance. If the agent_id doesn't resolve (an inbound call for an agent we
genuinely don't recognize — should be rare, but not impossible, e.g. a stale
webhook registration from a deleted agent), there is no Platform to own the
record, so none is created — logged and acknowledged gracefully, the same
`{"ok": true, "unrecognized_call_id": true}` shape as any other unrecognized
case in this handler, never a crash.

Once created, the record is handed to the EXACT SAME `_process_finished_call`
background flow every outbound call already goes through — no inbound
special-casing inside that function at all. This is the actual point of
creating the record eagerly rather than writing a parallel "handle inbound
post-call" code path: once a Calls document exists, re-hosting, summary/
sentiment/extracted_data, and the call-completed notification already work
identically regardless of which direction the call came from, because
`_process_finished_call` was never written to assume `POST /calls/outbound`
was involved in the first place — it only ever needed `call_mongo_id` and
`platform_id`, both of which the inbound path now supplies exactly the same
way the outbound path always has. See CallDirection's own comment
(app/models/call.py) and CallStatus's "Inbound-call record creation" section
in the same file for `direction`/`status`'s own reasoning on the new record.


POST /webhooks/retell/custom-tool — mid-call custom-tool proxy
===================================================================

Closes the "custom mid-call tools beyond transfer" gap: a `builtin`
agent can now call out to an external URL mid-conversation (e.g. "check
appointment availability") and use the response to continue the
conversation. See app/models/agent.py's module docstring for the full
proxy-routing architecture decision — the short version: the voice vendor
itself calls whatever `url` we registered on the tool, directly,
mid-call, carrying a real `X-Retell-Signature` header. If that URL were
Platform X's own server, Platform X would see that header name — a direct
vendor-identity leak. So `url` ALWAYS points at THIS endpoint (our
own proxy), never Platform X's URL directly.

**Confirmed real mechanism** (live WebFetch of the voice vendor's own
current custom-function docs this session): the vendor POSTs (or whichever
method the tool was registered with)

    Vendor -> us:  <method> <our custom-tool url>
        {"name": "check_availability",
         "args": {"date": "2026-09-01"},
         "call": {"call_id": "...", "agent_id": "...", "transcript": "...", ...}}

and waits up to the tool's own configured `timeout_ms` (capped at 30s on our
side — see CUSTOM_TOOL_MAX_TIMEOUT_MS in app/models/agent.py) for our
response, which becomes the tool's result and is handed back into the live
conversation. We respond with any JSON object (`2xx`) — see
app/models/custom_tool_call.py's docstring for the full contract.

**The routing/lookup mechanism — the hardest real design problem this
endpoint solves, worked through explicitly, not a formality.** The payload's
own `name` field says WHICH tool fired, but not which agent/platform it
belongs to — and a tool name is only unique WITHIN one agent (see
CreateAgentRequest's own per-agent uniqueness check), not globally, so `name`
alone can never resolve the right platform. The real, confirmed payload
shape's `call` object DOES carry the vendor's own `agent_id` (confirmed via
the same live WebFetch this session — see the sourced JSON excerpt above),
which is exactly `AgentInDB.vendor_ref` — the same value every other
vendor-webhook-driven lookup in this codebase already resolves through
(`call_repo.get_by_vendor_ref`, `phone_number_repo.
get_by_phone_number_any_platform`). So the resolution path is:

  1. `agent_repo.get_by_vendor_ref(db, call.agent_id)` — ONE indexed lookup
     (new `Agents.vendor_ref` sparse index, see database.py) resolves the
     vendor's agent_id straight to our own Agents document, which carries
     both `platform_id` (who owns this) and `custom_tools` (where each
     tool's own registered `webhook_url` lives).
  2. Find the entry in that agent's `custom_tools` whose `name` matches the
     payload's `name` — this is where per-agent tool-name uniqueness
     actually matters: without it, this step would be ambiguous.
  3. (Best-effort, not required for correctness) `call_repo.
     get_by_vendor_ref(db, call.call_id)` — a SECOND lookup, only to enrich
     the relay payload with OUR OWN call id (never the vendor's), per the
     "never leak vendor_ref" rule applied to PlatformCustomToolRequest.
     `call.call_id` may not resolve to any Calls document we own yet (e.g.
     timing edge cases around when POST /calls/outbound's own record is
     written vs. when the first tool fires) — handled gracefully, `call_id`
     is simply null in that case, never a crash or a rejected request.

Every one of these lookups is either a single indexed `find_one` or an
in-memory list scan over one agent's own (capped at
`MAX_CUSTOM_TOOLS` = 20) `custom_tools` array — no loop over multiple DB
round trips, same "minimal own processing" discipline as `/inbound`'s
routing above.

**Failure handling — never a crash, always SOME clean response the voice
vendor's real tool-calling contract can use.** An unrecognized `call.
agent_id` (no matching Agents document), an unrecognized tool `name` (agent
found but no matching `custom_tools` entry), and a Platform X relay
failure/timeout are all THREE distinct, separately-logged outcomes, but all
resolve to the SAME caller-visible shape: `200` with a JSON body
`{"error": "..."}` — not a 4xx/5xx status. This is deliberate, not a
shortcut: per the task's own instruction to use "what's actually usable by
Retell's tool-calling mechanism per the real confirmed response contract,"
the vendor's documented contract is that our response body becomes the
tool's result for the LLM to react to — a non-2xx status is not documented
as having a defined "graceful degradation into the conversation" behavior,
whereas a 200 with a small JSON object the agent's own prompt can be told to
handle (e.g. "if a tool result contains an 'error' field, apologize and
offer to have someone follow up") is something the conversation can actually
act on instead of the call potentially erroring out. A genuinely malformed
webhook body (fails Pydantic validation) is the one exception — that's a
sign of a request that isn't really this mechanism at all, so it still gets
a real `422`, matching `/inbound`'s and `/post-call`'s existing malformed-
body handling.

**Timeout budget, "not our fault" framing, exactly the same principle as
`/inbound` above, re-applied to a different real ceiling.** The relevant
ceiling here isn't Retell's own generic inbound-webhook ~10s — it's
whatever `timeout_ms` THIS SPECIFIC tool was registered with (our own
capped default 10s, hard cap 30s — see CustomToolDefinition in
app/models/agent.py). Our relay to Platform X's own `webhook_url` (see
app/services/custom_tool_relay.py) derives its own timeout as a fraction of
that per-tool budget rather than one shared global constant, so it always
leaves real margin for our own processing plus the vendor's network round
trip, regardless of which tool (and therefore which timeout_ms) fired. If
Platform X doesn't respond in time (or errors, or returns something
malformed), we return the same clean `{"error": "..."}` 200 described
above — a slow/broken Platform X integration degrades the ONE tool call
gracefully, it never leaves the voice vendor hanging for its own full
budget nor crashes the live call.

**Signature verification (incoming, from the voice vendor)**: identical
mechanism to `/inbound`/`/post-call` above (same `_verify_signature`
function, reused unchanged).

**Signature verification (outgoing, to Platform X's own webhook_url)**: the
relay leg (app/services/custom_tool_relay.py) now signs every request with
`X-VoiceAI-Signature`, closing former Known open item 3 — see that module's
docstring for the full signing mechanism and the secret-reuse decision
(the platform's existing `call_completed_webhook_secret`, not a new field).
This handler looks up the owning Platform (`platform_repo.get_by_id`,
mirroring how `_process_finished_call` above already does the same lookup
before calling `call_completed_webhook.deliver()`) and threads its secret
through to `custom_tool_relay.relay_tool_call()`.


POST /webhooks/retell/transcript-updated — live per-turn transcript relay
===================================================================

**Architecturally different from every webhook above, called out
explicitly.** `/inbound`, `/post-call`, and `/custom-tool` are each, at
most, a small, bounded number of deliveries per call (one, or up to three).
This endpoint is not: it fires MANY TIMES over the course of ONE call —
Retell's own current docs (`docs.retellai.com/features/webhook-overview`,
confirmed via a live WebFetch this session, NOT assumed) describe
`transcript_updated` as "triggered on turn-taking transcript updates, plus a
final update when the call ends." A single 5-minute phone call might
generate dozens of these deliveries. Retell's docs explicitly warn NOT to
dedupe by `call_id` alone here — many deliveries sharing one `call_id` is
expected, correct behavior for this event, unlike every other webhook in
this module, where a second delivery for the same `call_id` means "a retry
or a later, more-complete event," not "an unrelated, equally-valid new
update."

**Not in Retell's default webhook event set.** Confirmed via the same live
WebFetch: a voice agent's default `webhook_events` (when never configured)
is exactly `call_started`/`call_ended`/`call_analyzed` — an agent must
explicitly opt in via `webhook_events` (a real field on the vendor's AGENT
object, confirmed via live WebFetch of both `create-agent` and
`update-agent`'s current API references) before this endpoint ever receives
anything for that agent's calls. See app/models/agent.py's module docstring,
"live_transcript_enabled" section, for VoiceAI's own opt-in field and
app/services/retell_agent_adapter.py's `_build_webhook_events`/
`build_webhook_events_for_update` for the exact vendor request shape this
drives.

**What this handler actually does — resolve, then relay, nothing else.**
Unlike `/post-call`, this handler does NOT create a missing Calls document
for an unrecognized `call_id` — see app/models/transcript_updated.py's
docstring for why that fallback-creation path (which `/post-call` has, for
genuinely inbound calls) is deliberately NOT replicated here: a live update
for a call this codebase has never heard of has no platform_id to relay
toward and no WebSocket subscriber could possibly exist for it yet, so there
is nothing useful to create a placeholder record FOR. `call_repo.
get_by_vendor_ref(db, call.call_id)` — the SAME lookup `/post-call` already
uses — resolves the vendor's `call_id` to one of our own Calls documents;
an unrecognized `call_id` is acknowledged and dropped
(`{"ok": true, "unrecognized_call_id": true}`, same shape as every other
unrecognized-webhook case in this module), never an error, since Retell's
own retry behavior on a non-2xx response would otherwise just re-deliver the
same unresolvable update forever.

Once resolved, the update is hand-carried straight to
`app/services/live_transcript_registry.py`'s `relay()` — no intermediate
processing, no accumulation of successive deliveries into a running
transcript on our side (see that module's docstring, "relay-only, no
persistence" section, for why NOT accumulating/persisting every delta is the
deliberate right default here, not an oversight a future maintainer should
"fix"). `relay()` pushes to zero or more currently-connected WebSocket
clients for that Calls document's own id (never the vendor's `call_id` —
see `WS /calls/{call_id}/live-transcript` in app/routers/calls.py for the
subscription side) and returns immediately regardless of how many clients
(if any) were actually listening.

**Latency/response design — fully synchronous, no BackgroundTasks, unlike
`/post-call`.** `/post-call`'s slow step (download-from-Retell +
upload-to-S3) genuinely takes real seconds and has no latency budget to
protect (the call has already ended by the time it fires) — deferring it to
a background task is what makes sense there. This handler's own work
(one indexed Mongo lookup, then an in-memory dict lookup + a handful of
in-process WebSocket sends) is fast by construction and has no comparable
slow step to defer — there is no reason to add BackgroundTasks' own
complexity here for work that already completes in low single-digit
milliseconds under normal conditions. A single slow/dead WebSocket send is
still bounded and non-blocking-for-others by `live_transcript_registry.
relay()`'s own per-connection try/except (see that function's docstring) —
this handler's own synchronous `await relay(...)` call can never be made
arbitrarily slow by one bad connection.

**No signature-verification exception** — same `_verify_signature` function,
reused unchanged, identical to every other Retell-facing route in this
module (see the standards doc's "one unified mechanism for all of Retell's
webhook types" rule, already cited above for `/post-call`/`/custom-tool`).

**Response contract**: always `200` (this is a Retell-initiated webhook,
Retell's own retry behavior on non-2xx applies exactly like every other
webhook in this module) — `{"ok": true}` on a successful relay attempt
(regardless of whether anyone was actually listening — see this section's
"what this handler does" above), `{"ok": true, "unrecognized_call_id":
true}` for a `call_id` this codebase doesn't recognize, matching this
module's established response shape for every other "nothing to do, not an
error" case.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, status
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.database import MongoDB, get_db
from app.errors import CODE_UNAUTHENTICATED, CODE_VALIDATION, AppError
from app.models.call import CallDirection, CallStatus
from app.models.custom_tool_call import RetellCustomToolWebhook
from app.models.inbound_call import RetellInboundCallWebhook
from app.models.post_call import RetellPostCallWebhook
from app.models.transcript_updated import RetellTranscriptUpdatedWebhook
from app.repositories import agent_repo, call_repo, phone_number_repo, platform_repo
from app.services import (
    call_completed_webhook,
    custom_tool_relay,
    live_transcript_registry,
    platform_relay,
    retell_adapter,
)
from app.services.storage import get_storage_service, recording_key, transcript_key

logger = logging.getLogger("app.webhooks.retell")

router = APIRouter(prefix="/webhooks/retell", tags=["webhooks"])

# No Swagger-visible summary/description/response_model on the route below —
# deliberately. This endpoint is never called through Swagger (the voice
# vendor calls it directly), never authenticated via API key (verified by
# HMAC signature instead), and its request/response shape is the vendor's
# own real wire format, not something worth documenting for Platform X. The
# module docstring above is the real documentation of this endpoint, same as
# retell_adapter.py's functions.


_SIGNATURE_PATTERN = re.compile(r"^v=(?P<timestamp>\d+),d=(?P<digest>[0-9a-f]+)$")

# Replay-protection window. 5 minutes (300s) backward is Retell's own
# documented requirement ("within 5 minutes of the current time"), taken
# literally. The forward-tolerance side (a timestamp slightly AHEAD of our
# own clock) is not documented by Retell at all — added here deliberately,
# not from a vendor spec, purely to absorb ordinary small clock skew between
# Retell's servers and ours (NTP drift, VM clock jitter) without ever
# widening the real replay-protection window in the direction that actually
# matters (backward/stale). Kept intentionally small (60s, well under the
# 300s backward window) so it can only ever mask trivial skew, never
# meaningfully extend how long a captured signature stays valid.
_MAX_TIMESTAMP_AGE_SECONDS = 300
_MAX_TIMESTAMP_SKEW_FORWARD_SECONDS = 60


def _verify_signature(*, raw_body: bytes, header_value: str, settings: Settings) -> None:
    """Fail-closed HMAC verification against Retell's real, current scheme —
    see this module's docstring (the section right above `_verify_signature`)
    for the full sourced scheme description, the deliberate "vendor docs over
    sibling-project code" exception this represents, and the unverified
    "webhook badge" assumption.

    Header value format: "v={timestamp_ms},d={hex_digest}" — rejected
    (401) if it doesn't match this pattern at all.

    Signed content: raw_body + timestamp (string concatenation), where
    `timestamp` is the exact literal digit string taken from the `v=`
    field of the header — never a re-formatted/re-parsed version of it.
    This is the most literal, direct reading of Retell's own documented
    "HMAC-SHA256(raw_body + timestamp, api_key)" and its Go sample
    (`mac.Write([]byte(rawBody + matches[1]))`, where `matches[1]` is the
    regex-captured timestamp substring, i.e. the raw string, not a
    round-tripped integer) — confirmed via a fresh live WebFetch of
    Retell's own docs this session, not assumed.

    Secret: `settings.RETELL_API_KEY` — NOT a separate webhook secret (see
    module docstring for why this changed and the unverified "webhook
    badge" caveat).

    Raises AppError(401) on: a malformed header value, a digest mismatch,
    or a timestamp outside the allowed window. Raises AppError(500) if no
    secret is configured in production.
    """
    if settings.RETELL_API_KEY:
        match = _SIGNATURE_PATTERN.match(header_value)
        if match is None:
            raise AppError(
                code=CODE_UNAUTHENTICATED,
                message="Invalid webhook signature.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        timestamp_str = match.group("timestamp")
        digest = match.group("digest")

        expected = hmac.new(
            settings.RETELL_API_KEY.encode("utf-8"),
            raw_body + timestamp_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise AppError(
                code=CODE_UNAUTHENTICATED,
                message="Invalid webhook signature.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        # Replay protection — Retell's own documented 5-minute window,
        # taken literally, plus a small forward-skew tolerance (see the
        # constants' own comments above for why).
        timestamp_seconds = int(timestamp_str) / 1000
        now_seconds = time.time()
        age_seconds = now_seconds - timestamp_seconds
        if (
            age_seconds > _MAX_TIMESTAMP_AGE_SECONDS
            or age_seconds < -_MAX_TIMESTAMP_SKEW_FORWARD_SECONDS
        ):
            raise AppError(
                code=CODE_UNAUTHENTICATED,
                message="Webhook signature timestamp is outside the allowed window.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return
    if settings.ENV == "production":
        # Belt-and-suspenders with Settings.assert_production_secrets(),
        # which already requires RETELL_API_KEY to be set in production
        # (for outbound Retell API calls, independent of this webhook
        # check) — this path is effectively unreachable in a correctly
        # configured production deployment, but the runtime backstop is
        # kept anyway rather than trusting the boot-time check alone.
        logger.error("RETELL_API_KEY not configured in production — refusing webhook request")
        raise AppError(
            code="internal",
            message="Webhook verification is not configured.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    # Secret unset, dev/test — allow unverified so local dev/tunnels work
    # without a real key configured, matching the standards doc's
    # dev-permissive rule exactly.


def _log_outcome(
    *,
    outcome: str,
    reason: str,
    t_received: float,
    t_before_relay: float,
    t_after_relay: float,
    t_responding: float,
    platform_id: str | None,
    agent_mongo_id: str | None,
    relay_upstream_status: int | None = None,
    relay_error_class: str | None = None,
) -> None:
    """Single structured log line per inbound webhook, carrying the four
    separately-measured timing segments described in this module's
    docstring — the evidence trail for "was this delay ours or Platform
    X's," not just a single combined duration.
    """
    our_before_ms = (t_before_relay - t_received) * 1000
    platform_x_ms = (t_after_relay - t_before_relay) * 1000
    our_after_ms = (t_responding - t_after_relay) * 1000
    total_ms = (t_responding - t_received) * 1000
    payload: dict[str, Any] = {
        "outcome": outcome,
        "reason": reason,
        "platform_id": platform_id,
        "agent_id": agent_mongo_id,
        "our_processing_ms_before_relay": round(our_before_ms, 1),
        "platform_x_response_ms": round(platform_x_ms, 1),
        "our_processing_ms_after_relay": round(our_after_ms, 1),
        "total_ms": round(total_ms, 1),
    }
    if relay_upstream_status is not None:
        payload["relay_upstream_status"] = relay_upstream_status
    if relay_error_class is not None:
        payload["relay_error_class"] = relay_error_class
    logger.info("Inbound call-variables webhook handled", extra=payload)


@router.post("/inbound", status_code=status.HTTP_200_OK, include_in_schema=False)
async def handle_inbound_call(request: Request) -> dict[str, Any]:
    """Receive the voice vendor's `call_inbound` webhook, relay to Platform
    X's registered variables webhook (if any) under a strict short timeout,
    and respond with `{"call_inbound": {"dynamic_variables": {...}}}` fast
    enough to stay well inside the vendor's real ~10s deadline regardless of
    Platform X's own responsiveness.

    `include_in_schema=False`: never rendered in Swagger — this is a voice-
    vendor-facing webhook endpoint, not a Platform-X-facing API surface (see
    this module's docstring for why). Kept out of /openapi.json entirely
    rather than merely given vendor-neutral text, since Platform X has no
    reason to ever see or call this endpoint.
    """
    t_received = time.monotonic()
    settings = get_settings()
    db: MongoDB = get_db()

    raw_body = await request.body()
    _verify_signature(
        raw_body=raw_body,
        header_value=request.headers.get("X-Retell-Signature", ""),
        settings=settings,
    )

    try:
        parsed = json.loads(raw_body or b"{}")
        webhook = RetellInboundCallWebhook.model_validate(parsed)
    except (ValueError, ValidationError) as exc:
        raise AppError(
            code=CODE_VALIDATION,
            message="Malformed inbound-call webhook payload.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc

    details = webhook.call_inbound
    to_number = details.to_number
    from_number = details.from_number

    # Resolve which platform/agent owns this inbound number. A single
    # indexed lookup (PhoneNumbers.phone_number, no platform_id filter here
    # since we don't know the caller's tenancy yet — that's exactly what
    # this lookup determines) — no loop, no N+1, per the standards doc's DB
    # rules and this task's "minimal own processing" mandate.
    owned_number = (
        await phone_number_repo.get_by_phone_number_any_platform(db, to_number)
        if to_number
        else None
    )

    t_before_relay = time.monotonic()

    if owned_number is None:
        # Unrecognized to_number — handle gracefully, never crash. No
        # platform to relay to, so respond immediately with the safe
        # fallback; this is functionally identical to "not registered" from
        # the vendor's point of view, just logged with its own reason so
        # it's distinguishable in an audit.
        t_after_relay = time.monotonic()
        response_body: dict[str, Any] = {"call_inbound": {"dynamic_variables": {}}}
        t_responding = time.monotonic()
        _log_outcome(
            outcome="fallback",
            reason="unrecognized_to_number",
            t_received=t_received,
            t_before_relay=t_before_relay,
            t_after_relay=t_after_relay,
            t_responding=t_responding,
            platform_id=None,
            agent_mongo_id=None,
        )
        return response_body

    platform = await platform_repo.get_by_id(db, owned_number.platform_id)
    webhook_url = platform.inbound_variables_webhook_url if platform else None

    if not webhook_url:
        # Fastest, most predictable path — the majority case until real
        # platforms register a URL (see PlatformInDB's docstring). No
        # wasted relay attempt to nothing.
        t_after_relay = time.monotonic()
        response_body = {"call_inbound": {"dynamic_variables": {}}}
        t_responding = time.monotonic()
        _log_outcome(
            outcome="fallback",
            reason="platform_not_registered",
            t_received=t_received,
            t_before_relay=t_before_relay,
            t_after_relay=t_after_relay,
            t_responding=t_responding,
            platform_id=owned_number.platform_id,
            agent_mongo_id=owned_number.agent_id,
        )
        return response_body

    relay_result = await platform_relay.request_dynamic_variables(
        webhook_url=webhook_url,
        from_number=from_number,
        to_number=to_number,
        agent_id=owned_number.agent_id,
    )
    t_after_relay = time.monotonic()

    response_body = {"call_inbound": {"dynamic_variables": relay_result.dynamic_variables}}
    t_responding = time.monotonic()

    _log_outcome(
        outcome="relay_success" if relay_result.outcome == "success" else "fallback",
        reason=relay_result.outcome,
        t_received=t_received,
        t_before_relay=t_before_relay,
        t_after_relay=t_after_relay,
        t_responding=t_responding,
        platform_id=owned_number.platform_id,
        agent_mongo_id=owned_number.agent_id,
        relay_upstream_status=relay_result.upstream_status,
        relay_error_class=relay_result.error_class,
    )
    return response_body


# ── POST /webhooks/retell/post-call ─────────────────────────────────────
# See this module's docstring, "POST /webhooks/retell/post-call" section,
# for the full confirmed-real mechanism, latency design, idempotency, and
# partial-success reasoning.

_FINISHED_EVENTS = frozenset({"call_ended", "call_analyzed"})


async def _rehost_recording(
    *, settings: Settings, call_mongo_id: str, recording_url: str
) -> tuple[str | None, bool]:
    """Download the recording from the voice vendor's real URL and re-upload
    it to our own S3 bucket. Returns (our_own_serving_path, rehost_failed).

    A genuine Retell-side failure (vendor unreachable/rejects the recording
    fetch) and a genuine S3-side failure (storage unreachable/misconfigured,
    e.g. today's empty AWS_S3_BUCKET placeholder) are both caught here and
    folded into the same `rehost_failed=True` outcome — from the Calls
    record's point of view, "the recording didn't get re-hosted" is one
    fact, regardless of which half of the pipeline failed; the underlying
    AppError (with its own distinct upstream_failed/storage_failed code) is
    still logged with full detail for our own debugging, just not
    propagated to raise/crash this background task.
    """
    try:
        audio_bytes, content_type = await retell_adapter.fetch_recording_bytes(
            settings, recording_url=recording_url
        )
        await get_storage_service().upload(
            key=recording_key(call_mongo_id),
            content=audio_bytes,
            content_type=content_type,
            settings=settings,
        )
    except AppError as exc:
        logger.warning(
            "Post-call recording re-hosting failed",
            extra={"call_id": call_mongo_id, "error_code": exc.code},
        )
        return None, True
    return f"/calls/{call_mongo_id}/recording", False


async def _rehost_transcript(
    *, settings: Settings, call_mongo_id: str, transcript: str
) -> tuple[str | None, bool]:
    """Re-host the transcript text to our own S3 bucket. Same fold-failures-
    into-a-flag reasoning as _rehost_recording above. An empty transcript
    string (Retell sent the field but it's blank) is treated as "nothing to
    re-host yet," not a failure — returns (None, False), same as if the
    field were absent entirely.
    """
    if not transcript:
        return None, False
    try:
        transcript_bytes = (
            await retell_adapter.fetch_transcript_text(settings, transcript=transcript)
        ).encode("utf-8")
        await get_storage_service().upload(
            key=transcript_key(call_mongo_id),
            content=transcript_bytes,
            content_type="text/plain; charset=utf-8",
            settings=settings,
        )
    except AppError as exc:
        logger.warning(
            "Post-call transcript re-hosting failed",
            extra={"call_id": call_mongo_id, "error_code": exc.code},
        )
        return None, True
    return f"/calls/{call_mongo_id}/transcript", False


async def _process_finished_call(
    *,
    settings: Settings,
    db: MongoDB,
    call_mongo_id: str,
    platform_id: str,
    event: str,
    recording_url: str | None,
    transcript: str | None,
    summary: str | None,
    sentiment: str | None,
    extracted_data: dict[str, Any] | None,
    in_voicemail: bool | None,
    disconnection_reason: str | None,
) -> None:
    """The actual re-hosting work — runs AFTER this module's post-call route
    has already responded 200 to Retell (see this module's docstring,
    "Latency design", for why this is a FastAPI BackgroundTasks callback
    rather than inline in the request/response cycle).

    Re-hosts the recording and transcript independently (a recording
    failure doesn't block a transcript succeeding, or vice versa) and always
    writes the call's own outcome (status/summary/sentiment) regardless of
    whether either re-hosting step succeeds — the partial-success design
    documented in this module's docstring. Then notifies Platform X that the
    call has finished (see the call-completed-webhook block below) — the
    reason `platform_id` is threaded through from the caller (already
    resolved there via `existing_call.platform_id`) is so this function can
    both re-fetch the just-updated Calls record and look up the owning
    Platform's registered notification settings, without a second,
    unscoped, tenancy-bypassing lookup.
    """
    recording_serving_path: str | None = None
    transcript_serving_path: str | None = None
    any_rehost_failed = False

    if recording_url:
        recording_serving_path, recording_failed = await _rehost_recording(
            settings=settings, call_mongo_id=call_mongo_id, recording_url=recording_url
        )
        any_rehost_failed = any_rehost_failed or recording_failed

    if transcript:
        transcript_serving_path, transcript_failed = await _rehost_transcript(
            settings=settings, call_mongo_id=call_mongo_id, transcript=transcript
        )
        any_rehost_failed = any_rehost_failed or transcript_failed

    await call_repo.update_post_call_outcome(
        db,
        call_mongo_id,
        status=CallStatus.COMPLETED,
        recording_url=recording_serving_path,
        transcript_url=transcript_serving_path,
        summary=summary,
        sentiment=sentiment,
        extracted_data=extracted_data,
        recording_rehost_failed=any_rehost_failed,
        in_voicemail=in_voicemail,
        disconnection_reason=disconnection_reason,
    )
    logger.info(
        "Post-call re-hosting finished",
        extra={
            "call_id": call_mongo_id,
            "event": event,
            "recording_rehosted": recording_serving_path is not None,
            "transcript_rehosted": transcript_serving_path is not None,
            "rehost_failed": any_rehost_failed,
        },
    )

    # Notify Platform X, now that there's real, complete (or partially
    # complete) data to send — see app/services/call_completed_webhook.py's
    # module docstring for the full design. Fires EVEN IF recording_rehost_
    # failed above (any_rehost_failed=True): the trigger-point decision,
    # made deliberately per this task's design brief, is that partial data
    # is better than no notification — the recording/transcript endpoints
    # can be retried/re-fetched later (the S3 key is deterministic, per the
    # idempotency design above), and a failed recording specifically must
    # never block notifying Platform X about everything else that DID
    # succeed (status, summary, sentiment). Re-reads the Calls record fresh
    # (rather than reusing the pre-update values already in this function's
    # own locals) so the payload reflects exactly what was just persisted,
    # including recording_rehost_failed itself.
    updated_call = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    if updated_call is None:
        # Should not happen — the record we just updated a moment ago,
        # looked up by its own id under the same platform_id that owns it.
        # Logged, not raised: this background task's job (re-hosting) has
        # already fully completed successfully; a notification we can't
        # even attempt is a separate, lesser failure that must not look
        # like the whole background task crashed.
        logger.error(
            "Could not re-fetch Calls record for call-completed notification",
            extra={"call_id": call_mongo_id},
        )
        return

    platform = await platform_repo.get_by_id(db, platform_id)
    await call_completed_webhook.deliver(
        call=updated_call,
        webhook_url=platform.call_completed_webhook_url if platform else None,
        webhook_secret=platform.call_completed_webhook_secret if platform else None,
    )


@router.post("/post-call", status_code=status.HTTP_200_OK, include_in_schema=False)
async def handle_post_call(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Receive the voice vendor's post-call webhook (`call_started`/
    `call_ended`/`call_analyzed`), acknowledge fast, and re-host the
    recording/transcript to our own S3 bucket in the background — see this
    module's docstring, "POST /webhooks/retell/post-call" section, for the
    full confirmed mechanism and the "acknowledge fast, do the real work
    async" latency reasoning.

    `include_in_schema=False`: same reasoning as `/inbound` above — this is
    a voice-vendor-facing webhook, never a Platform-X-facing Swagger
    surface.
    """
    settings = get_settings()
    db: MongoDB = get_db()

    raw_body = await request.body()
    _verify_signature(
        raw_body=raw_body,
        header_value=request.headers.get("X-Retell-Signature", ""),
        settings=settings,
    )

    try:
        parsed = json.loads(raw_body or b"{}")
        webhook = RetellPostCallWebhook.model_validate(parsed)
    except (ValueError, ValidationError) as exc:
        raise AppError(
            code=CODE_VALIDATION,
            message="Malformed post-call webhook payload.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc

    # transcript_updated is delivered to this SAME URL, not a separate one —
    # confirmed via a live WebFetch of the vendor's own current docs: an
    # agent has exactly ONE webhook_url for every subscribed event type
    # ("If set, will bind webhook events for this agent to the specified
    # url"), never a per-event-type URL. `webhook_events` only FILTERS which
    # events get sent, it does not route them elsewhere. This was a real bug
    # caught during live-call verification: a sibling `/transcript-updated`
    # route existed and worked correctly in isolation, but the vendor never
    # actually called it, since only THIS url was ever registered on the
    # agent (see `_build_webhook_events`/`build_webhook_events_for_update`
    # in retell_agent_adapter.py — they add "transcript_updated" to
    # `webhook_events`, but every webhook_url on this codebase's agents has
    # only ever pointed at `/post-call`). Delegating to the exact same
    # `handle_transcript_updated` relay logic here (re-parsing the same raw
    # body as the transcript-specific model) rather than duplicating it.
    if webhook.event == "transcript_updated":
        return await _relay_transcript_updated(raw_body)

    if webhook.event not in _FINISHED_EVENTS:
        # call_started (or any other/future event) — nothing to re-host yet.
        # Acknowledge and ignore, never an error; see this module's
        # docstring's event-by-event breakdown.
        logger.info("Post-call webhook received, no action needed", extra={"event": webhook.event})
        return {"ok": True, "ignored": webhook.event}

    call = webhook.call
    existing_call = await call_repo.get_by_vendor_ref(db, call.call_id)
    if existing_call is None:
        # No Calls document has this vendor_ref yet. Two real cases share
        # this branch: a genuinely inbound call (no prior POST
        # /calls/outbound ever ran for it — the gap this section closes, see
        # this module's docstring) and a genuine data inconsistency (e.g. a
        # Calls document that was somehow deleted). Both are handled the same
        # way: try to create the missing record from the vendor's agent_id;
        # if that doesn't resolve to an agent we recognize, there is nothing
        # for us to update, so acknowledge and move on gracefully — same
        # "unrecognized == graceful 200" pattern as /inbound's
        # unrecognized-to_number case above.
        agent = await agent_repo.get_by_vendor_ref(db, call.agent_id) if call.agent_id else None
        if agent is None:
            logger.warning(
                "Post-call webhook for unrecognized call_id and agent_id — "
                "no Calls document to update and no agent to create one under",
                extra={
                    "event": webhook.event,
                    "vendor_call_id": call.call_id,
                    "vendor_agent_id": call.agent_id,
                },
            )
            return {"ok": True, "unrecognized_call_id": True}

        # A real inbound call, discovered for the first time via this
        # webhook. Created with status=COMPLETED directly — see
        # CallInDB's "Inbound-call record creation" docstring section
        # (app/models/call.py) for why there's no intermediate "registered"
        # phase here: by the time call_ended/call_analyzed exists at all,
        # the call is already over. dynamic_variables has no equivalent for
        # an inbound call (nothing was ever sent) — empty dict, matching the
        # field's existing non-optional dict[str, str] type.
        existing_call = await call_repo.create(
            db,
            platform_id=agent.platform_id,
            agent_id=agent.id,
            from_number=call.from_number or "",
            to_number=call.to_number or "",
            dynamic_variables={},
            status=CallStatus.COMPLETED,
            vendor=agent.vendor,
            vendor_ref=call.call_id,
            direction=CallDirection.INBOUND,
        )
        logger.info(
            "Created Calls record for previously-unrecognized inbound call",
            extra={
                "event": webhook.event,
                "vendor_call_id": call.call_id,
                "call_id": existing_call.id,
                "platform_id": existing_call.platform_id,
                "agent_id": existing_call.agent_id,
            },
        )

    analysis = call.call_analysis
    background_tasks.add_task(
        _process_finished_call,
        settings=settings,
        db=db,
        call_mongo_id=existing_call.id,
        platform_id=existing_call.platform_id,
        event=webhook.event,
        recording_url=call.recording_url,
        transcript=call.transcript,
        summary=analysis.call_summary if analysis else None,
        sentiment=analysis.user_sentiment if analysis else None,
        extracted_data=analysis.custom_analysis_data if analysis else None,
        in_voicemail=analysis.in_voicemail if analysis else None,
        disconnection_reason=call.disconnection_reason,
    )
    return {"ok": True}


# ── POST /webhooks/retell/custom-tool ───────────────────────────────────
# See this module's docstring, "POST /webhooks/retell/custom-tool" section,
# for the full confirmed-real mechanism, the routing/lookup design, and the
# failure-handling/timeout reasoning.

_CUSTOM_TOOL_ERROR_TIMEOUT = "The connected service did not respond in time. Let the caller know there's a delay and offer to follow up."  # noqa: E501
_CUSTOM_TOOL_ERROR_UPSTREAM = "The connected service could not complete this request right now. Let the caller know and offer to follow up."  # noqa: E501
_CUSTOM_TOOL_ERROR_UNKNOWN_TOOL = "This tool is not configured. Do not attempt to use it."
_CUSTOM_TOOL_ERROR_UNKNOWN_AGENT = "This tool is not configured. Do not attempt to use it."


def _log_custom_tool_outcome(
    *,
    outcome: str,
    reason: str,
    tool_name: str | None,
    agent_mongo_id: str | None,
    platform_id: str | None,
    elapsed_ms: float,
    relay_upstream_status: int | None = None,
    relay_error_class: str | None = None,
) -> None:
    """Single structured log line per custom-tool call — same "one line,
    enough fields to answer 'whose fault was this' without cross-
    referencing anything else" discipline as `/inbound`'s `_log_outcome`.
    """
    logger.info(
        "Custom-tool proxy call handled",
        extra={
            "outcome": outcome,
            "reason": reason,
            "tool_name": tool_name,
            "agent_id": agent_mongo_id,
            "platform_id": platform_id,
            "elapsed_ms": round(elapsed_ms, 1),
            "relay_upstream_status": relay_upstream_status,
            "relay_error_class": relay_error_class,
        },
    )


@router.post("/custom-tool", status_code=status.HTTP_200_OK, include_in_schema=False)
async def handle_custom_tool_call(request: Request) -> dict[str, Any]:
    """Receive the voice vendor's real custom-tool webhook, resolve which
    agent/platform/tool it belongs to, relay to that tool's own registered
    `webhook_url` under a timeout derived from the tool's own configured
    budget, and return either Platform X's response or a clean
    `{"error": "..."}` the agent's own prompt can react to gracefully.

    `include_in_schema=False`: same reasoning as `/inbound`/`/post-call`
    above — voice-vendor-facing, never a Platform-X-facing Swagger surface.
    """
    t_start = time.monotonic()
    settings = get_settings()
    db: MongoDB = get_db()

    raw_body = await request.body()
    _verify_signature(
        raw_body=raw_body,
        header_value=request.headers.get("X-Retell-Signature", ""),
        settings=settings,
    )

    try:
        parsed = json.loads(raw_body or b"{}")
        webhook = RetellCustomToolWebhook.model_validate(parsed)
    except (ValueError, ValidationError) as exc:
        raise AppError(
            code=CODE_VALIDATION,
            message="Malformed custom-tool webhook payload.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc

    vendor_agent_id = webhook.call.agent_id
    agent = await agent_repo.get_by_vendor_ref(db, vendor_agent_id) if vendor_agent_id else None
    if agent is None:
        # Unrecognized agent_id — handle gracefully, never crash. No agent
        # to resolve a tool/platform through at all.
        _log_custom_tool_outcome(
            outcome="fallback",
            reason="unrecognized_agent_id",
            tool_name=webhook.name,
            agent_mongo_id=None,
            platform_id=None,
            elapsed_ms=(time.monotonic() - t_start) * 1000,
        )
        return {"error": _CUSTOM_TOOL_ERROR_UNKNOWN_AGENT}

    tool = next((t for t in agent.custom_tools if t.name == webhook.name), None)
    if tool is None:
        # Agent found, but this tool name isn't (or is no longer) one of its
        # configured custom_tools — handle gracefully, same reasoning.
        _log_custom_tool_outcome(
            outcome="fallback",
            reason="unrecognized_tool_name",
            tool_name=webhook.name,
            agent_mongo_id=agent.id,
            platform_id=agent.platform_id,
            elapsed_ms=(time.monotonic() - t_start) * 1000,
        )
        return {"error": _CUSTOM_TOOL_ERROR_UNKNOWN_TOOL}

    # Best-effort enrichment only — see this module's docstring, routing
    # step 3. A miss here does not block the relay; call_id is simply null.
    our_call_id: str | None = None
    if webhook.call.call_id:
        matched_call = await call_repo.get_by_vendor_ref(db, webhook.call.call_id)
        our_call_id = matched_call.id if matched_call else None

    # Look up the owning Platform to get its signing secret — mirrors how
    # _process_finished_call above already does `platform_repo.get_by_id`
    # before calling call_completed_webhook.deliver(). See
    # custom_tool_relay.py's module docstring for the secret-reuse decision
    # (PlatformInDB.call_completed_webhook_secret, not a new field) and the
    # hard-fail-if-unsigned handling of a platform with no secret yet.
    platform = await platform_repo.get_by_id(db, agent.platform_id)

    relay_body = {
        "tool_name": tool.name,
        "args": webhook.args,
        "call_id": our_call_id,
        "agent_id": agent.id,
    }
    relay_result = await custom_tool_relay.relay_tool_call(
        webhook_url=str(tool.webhook_url),
        method=tool.method.value,
        tool_timeout_ms=tool.timeout_ms,
        body=relay_body,
        secret=platform.call_completed_webhook_secret if platform else None,
    )
    elapsed_ms = (time.monotonic() - t_start) * 1000

    if relay_result.outcome == "success" and relay_result.response_body is not None:
        _log_custom_tool_outcome(
            outcome="relay_success",
            reason="success",
            tool_name=tool.name,
            agent_mongo_id=agent.id,
            platform_id=agent.platform_id,
            elapsed_ms=elapsed_ms,
            relay_upstream_status=relay_result.upstream_status,
        )
        return relay_result.response_body

    error_message = (
        _CUSTOM_TOOL_ERROR_TIMEOUT
        if relay_result.outcome == "timeout"
        else _CUSTOM_TOOL_ERROR_UPSTREAM
    )
    _log_custom_tool_outcome(
        outcome="fallback",
        reason=relay_result.outcome,
        tool_name=tool.name,
        agent_mongo_id=agent.id,
        platform_id=agent.platform_id,
        elapsed_ms=elapsed_ms,
        relay_upstream_status=relay_result.upstream_status,
        relay_error_class=relay_result.error_class,
    )
    return {"error": error_message}


# ── POST /webhooks/retell/transcript-updated ────────────────────────────
# See this module's docstring, "POST /webhooks/retell/transcript-updated"
# section, for the full confirmed-real mechanism, the resolve-then-relay
# design, and the latency/response reasoning.
#
# **Kept as a real, separate route below for backward-compatibility /
# directness, but NOT what the vendor actually calls in practice** — see the
# comment in `handle_post_call` above (the `webhook.event == "transcript_
# updated"` branch): the vendor sends every subscribed event type to the
# SAME single `webhook_url` registered on the agent, confirmed via a live
# WebFetch of its own current docs ("If set, will bind webhook events for
# this agent to the specified url"). There is no per-event-type URL. Both
# this route and `handle_post_call`'s inline branch call the same
# `_relay_transcript_updated` helper so the logic lives in exactly one
# place regardless of which URL a caller happens to hit.


async def _relay_transcript_updated(raw_body: bytes) -> dict[str, Any]:
    """Shared logic for one incremental `transcript_updated` delivery
    (fired MANY TIMES per call — see this module's docstring): resolve
    which Calls document it belongs to, and relay it live to any
    currently-connected WebSocket client(s) for that call via
    `live_transcript_registry.relay()`. Called from both `handle_post_call`
    (the URL the vendor actually uses) and `handle_transcript_updated`
    (kept as its own route below, in case a future vendor change or a
    differently-configured agent ever does route this event separately).
    """
    db: MongoDB = get_db()

    try:
        parsed = json.loads(raw_body or b"{}")
        webhook = RetellTranscriptUpdatedWebhook.model_validate(parsed)
    except (ValueError, ValidationError) as exc:
        raise AppError(
            code=CODE_VALIDATION,
            message="Malformed transcript-updated webhook payload.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc

    call = webhook.call
    existing_call = await call_repo.get_by_vendor_ref(db, call.call_id)
    if existing_call is None:
        # Unrecognized call_id — deliberately NOT a fallback-creation path
        # the way /post-call has for inbound calls (see this module's
        # docstring and app/models/transcript_updated.py's docstring for
        # why): there is nothing useful to relay TOWARD for a call we have
        # no record of, so acknowledge and drop, same graceful pattern as
        # every other unrecognized-webhook case in this module.
        logger.info(
            "Transcript-updated webhook for unrecognized call_id — dropped",
            extra={"vendor_call_id": call.call_id},
        )
        return {"ok": True, "unrecognized_call_id": True}

    # Prefer `transcript_with_tool_calls` (confirmed, via a live phone-call
    # test, to be where real content actually lands on this event — see
    # RetellTranscriptUpdatedWebhook's docstring for the full story). Fall
    # back to `call.transcript_object` only if a delivery ever carries that
    # instead and not the other — belt-and-suspenders, not the expected path.
    turns = webhook.transcript_with_tool_calls or call.transcript_object
    delivered_to = await live_transcript_registry.relay(
        existing_call.id,
        {
            "call_id": existing_call.id,
            "transcript": [turn.model_dump() for turn in turns],
        },
    )
    logger.info(
        "Transcript-updated webhook relayed",
        extra={
            "call_id": existing_call.id,
            "platform_id": existing_call.platform_id,
            "delivered_to_connections": delivered_to,
        },
    )
    return {"ok": True}


@router.post(
    "/transcript-updated", status_code=status.HTTP_200_OK, include_in_schema=False
)
async def handle_transcript_updated(request: Request) -> dict[str, Any]:
    """Receive one incremental delivery of the voice vendor's real
    `transcript_updated` webhook via its own dedicated URL — see the module
    comment immediately above for why `handle_post_call`'s inline branch,
    not this route, is what actually receives this event in practice today.

    `include_in_schema=False`: same reasoning as `/inbound`/`/post-call`/
    `/custom-tool` above — voice-vendor-facing, never a Platform-X-facing
    Swagger surface.
    """
    settings = get_settings()
    raw_body = await request.body()
    _verify_signature(
        raw_body=raw_body,
        header_value=request.headers.get("X-Retell-Signature", ""),
        settings=settings,
    )
    return await _relay_transcript_updated(raw_body)
