"""Wire shape of the voice vendor's real `transcript_updated` webhook body —
what Retell sends US many times over the course of ONE call, per `POST
/webhooks/retell/transcript-updated` (see app/routers/webhooks.py's module
docstring for the full mechanism and app/services/live_transcript_registry.py
for what happens to each update once parsed).

**This is a genuinely different kind of webhook from every other one in this
codebase, confirmed via a fresh live WebFetch of
`docs.retellai.com/features/webhook-overview` this session** — every other
Retell webhook (`call_started`/`call_ended`/`call_analyzed`, the pre-call
`call_inbound`, a custom-tool call) fires ZERO OR ONE time for a given real-
world event. `transcript_updated` fires MANY TIMES per call — Retell's own
docs describe it as "triggered on turn-taking transcript updates, plus a
final update when the call ends" — so a single phone call produces a whole
SEQUENCE of these deliveries, each one a fuller/more-current snapshot of the
conversation so far, not a one-shot notification. Retell's docs explicitly
warn to treat each delivery as INCREMENTAL and NOT to dedupe by `call_id`
alone — many deliveries sharing the same `call_id` is expected, correct
behavior, not a sign of a broken/retried webhook the way it would be for
`call_ended`/`call_analyzed`.

**Not in Retell's default webhook event set — must be explicitly opted
into.** Confirmed via the same live WebFetch: a voice agent's default
webhook_events (if never configured) is exactly `call_started`, `call_ended`,
`call_analyzed` — `transcript_updated` is NOT among them. Opting in is a
real, separate per-agent configuration step (`webhook_events` array on the
vendor's own agent object, confirmed via live WebFetch of both
`create-agent` and `update-agent`'s current API references this session) —
see app/models/agent.py's module docstring, "live_transcript_enabled"
section, and app/services/retell_agent_adapter.py for exactly how VoiceAI's
own `live_transcript_enabled` field drives that subscription.

**Payload shape, confirmed via the same live WebFetch session plus Retell's
own current get-call API reference (the two share the same `call` object
shape, since this webhook's body IS a `call` object at a point in time):**

    {"event": "transcript_updated",
     "call": {
        "call_id": "...", "agent_id": "...",
        "transcript": "...",
        "transcript_object": [
            {"role": "agent" | "user" | "transfer_target",
             "content": "...",
             "words": [{"word": "...", "start": 0.7, "end": 1.3}, ...]}
        ],
        ...
     }}

Only `transcript_object` (the structured turn-by-turn array — `role` +
`content` per turn, confirmed via live WebFetch of Retell's current get-call
reference) is modeled here as `RetellTranscriptTurn`, not the plain
`transcript` string also present on the payload — a structured array of
`{role, content}` turns is genuinely more useful to relay to Platform X than
a flattened "Agent: ... Caller: ..." string, and is the same `transcript_
object` shape `RetellPostCallDetails` already carries (see
app/models/post_call.py) for the FINAL transcript, so reusing the identical
per-turn shape here keeps the incremental-vs-final transcript representation
consistent for anyone consuming both. `words` (word-level timestamps) is a
real, documented sub-field but deliberately NOT modeled — nothing in this
task's scope (a live relay of who-said-what) reads word-level timing, per
the "no unnecessary fields" rule; a caller only needing role/content per
turn is exactly what a live "what's being said right now" feed needs.

Retell's docs also mention `transcript_with_tool_calls` (a richer union type
covering tool-call/node-transition/DTMF/SMS entries, not just plain
utterances) is additionally included specifically on `transcript_updated`
deliveries — also deliberately NOT modeled here, same "no unnecessary
fields" reasoning: relaying plain conversational turns is the real, scoped
feature; a tool-call-aware transcript feed is a plausible FUTURE extension,
not something anything in this task's brief asks for today.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RetellTranscriptTurn(BaseModel):
    """One entry in `call.transcript_object` — a single conversational turn.
    Same `{role, content}` shape as the FINAL transcript's own turn objects
    (see app/models/post_call.py's docstring) — deliberately kept identical
    so a caller relaying both the live feed and the final transcript sees
    one consistent turn shape throughout a call's lifecycle, not two
    different ones.
    """

    role: str
    content: str


class RetellTranscriptUpdatedDetails(BaseModel):
    """The `call` object nested inside the vendor's real `transcript_updated`
    webhook body. Only the fields this endpoint actually reads are modeled —
    `agent_id` is NOT read here (unlike the post-call/custom-tool webhooks)
    because this endpoint resolves the owning Call/Platform purely from
    `call_id` via `call_repo.get_by_vendor_ref` (see app/routers/webhooks.py)
    — an agent-id fallback-creation path (the way `handle_post_call` creates
    a missing inbound-call record) is deliberately NOT replicated here: a
    live transcript update for a call VoiceAI has never heard of has nothing
    useful to relay to (no Calls document means no platform_id to resolve a
    WebSocket subscriber against), and creating a placeholder record purely
    to satisfy a live-relay-only feature would be a real, unwanted side
    effect for what is explicitly a best-effort, no-guaranteed-delivery
    mechanism (see this module's docstring and live_transcript_registry.py's
    module docstring for the full "relay-only, not archival" design).
    """

    call_id: str
    transcript_object: list[RetellTranscriptTurn] = []  # noqa: RUF012 — Pydantic field default.


class RetellTranscriptUpdatedWebhook(BaseModel):
    """Body of the voice vendor's real `transcript_updated` webhook — fires
    MANY TIMES per call (see this module's docstring). Every delivery is
    handled identically by `POST /webhooks/retell/transcript-updated`: parse,
    resolve `call_id` to one of our own Calls documents, relay it to any
    currently-connected WebSocket client(s) for that call. No accumulation/
    merging of successive deliveries happens on our side — see
    live_transcript_registry.py's module docstring for why each delivery is
    relayed as-is, not stitched into a running transcript.

    **Real bug found and fixed via a live phone-call test this session,
    correcting this module's original (wrong) assumption:** `call.
    transcript_object` is NOT populated on real `transcript_updated`
    deliveries — confirmed via a captured raw payload from an actual live
    call, `call.transcript_object` was absent entirely on every delivery.
    The real conversational content instead arrives on a SEPARATE top-level
    field, `transcript_with_tool_calls` (sibling to `call`, not nested
    inside it) — this module's docstring already anticipated this field's
    existence (Retell's docs mention it) but had deliberately NOT modeled it
    on the assumption `transcript_object` would still carry the plain
    turn-by-turn content too. That assumption was wrong in practice: for
    THIS event specifically, `transcript_with_tool_calls` is where the
    actual per-turn `role`/`content` data lives. Each entry in that array
    also carries `words` (word-level timing) and an optional `metadata`
    object (e.g. `response_id`) — both ignored here, same "no unnecessary
    fields" reasoning already applied to `transcript_object`'s own `words`
    field; only `role`/`content` are modeled, reusing `RetellTranscriptTurn`
    unchanged since the per-turn shape is otherwise identical.
    """

    model_config = ConfigDict(populate_by_name=True)

    event: str
    call: RetellTranscriptUpdatedDetails
    transcript_with_tool_calls: list[RetellTranscriptTurn] = []  # noqa: RUF012
