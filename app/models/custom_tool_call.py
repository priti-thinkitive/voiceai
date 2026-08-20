"""Models for the custom-tool proxy — the real mid-call mechanism where the
voice vendor calls OUR OWN proxy endpoint (`POST /webhooks/retell/
custom-tool`, see app/routers/webhooks.py) when a `builtin` agent's
configured custom tool fires. See app/models/agent.py's module docstring for
the full proxy-routing architecture decision (why our own URL is registered
with the vendor instead of Platform X's).

Two distinct wire shapes live here, deliberately kept separate, same
convention as app/models/inbound_call.py:

1. `RetellCustomToolWebhook`/`RetellCustomToolCallContext` — what the voice
   vendor sends US. A vendor webhook body, not a Platform-X-facing request;
   the router takes the raw request body for signature verification, not a
   parsed Pydantic model directly off FastAPI's request parsing (see
   app/routers/webhooks.py).
2. `PlatformCustomToolRequest` — the contract WE define and Platform X must
   implement on their own per-tool `webhook_url`. This is genuinely a
   Platform-X-facing API contract (they write the handler, we write the
   client) — no FastAPI router uses this as a request body either (we are
   the HTTP *client* here), so this docstring plus each field's own
   description together ARE the documentation of this contract for Platform
   X's own implementers, the same pattern already established for
   PlatformVariablesRequest/PlatformVariablesResponse.

Confirmed real shape (live WebFetch of the voice vendor's current
custom-function docs this session, not guessed): the vendor POSTs (using
whichever method the tool's own `method` field configured)
`{"name": "<tool_name>", "args": {<LLM-filled + const params>}, "call":
{<full call context, including call_id, agent_id, transcript, ...>}}` —
unless the tool's `payload_args_only` was set, in which case the body is
just the args object directly. This proxy endpoint standardizes on the FULL
shape internally (never sets `payload_args_only` when registering a tool —
see retell_agent_adapter.py's `_build_general_tools`) specifically because
`call.agent_id` is what the routing/lookup mechanism (design decision 2,
see app/routers/webhooks.py's custom-tool handler docstring) depends on to
resolve which of our own Agents documents — and therefore which platform and
which tool's registered webhook_url — this call concerns. The args-only
shape would lose that entirely.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class RetellCustomToolCallContext(BaseModel):
    """The `call` object nested inside the vendor's real custom-tool webhook
    body — only the fields this endpoint actually reads are modeled.
    `agent_id` here is the VOICE VENDOR's own agent id (not our Mongo id) —
    the routing key used to resolve which of our own Agents/Platforms
    records this tool-call concerns (see agent_repo.get_by_vendor_ref).
    """

    call_id: str | None = None
    agent_id: str | None = None


class RetellCustomToolWebhook(BaseModel):
    """Body of the real custom-tool webhook the voice vendor POSTs to our
    own proxy endpoint the moment a `builtin` agent's configured
    custom tool fires mid-conversation.

    Confirmed real shape (see this module's docstring):

        {"name": "check_availability",
         "args": {"date": "2026-09-01"},
         "call": {"call_id": "...", "agent_id": "...", ...}}
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    args: dict[str, Any] = {}  # noqa: RUF012 — Pydantic field default.
    call: RetellCustomToolCallContext


class PlatformCustomToolRequest(BaseModel):
    """What WE send to Platform X's own registered per-tool `webhook_url` —
    the contract Platform X's developers must implement on their own server
    to receive this relay.

    POST body we send (method per the tool's own configured `method`),
    `Content-Type: application/json`:

        {"tool_name": "check_availability",
         "args": {"date": "2026-09-01"},
         "call_id": "6706f1a2b3c4d5e6f7089abc",
         "agent_id": "6706f1a2b3c4d5e6f7089aaa"}

    `call_id`/`agent_id` are deliberately OUR OWN Mongo ids where resolvable
    (never the voice vendor's internal ids) — matching the "never leak
    vendor_ref" rule applied to every other Platform-X-facing contract in
    this codebase. `call_id` is null if we could not resolve the vendor's
    call_id back to one of our own Calls documents (e.g. the tool fired
    before POST /calls/outbound's own webhook-driven record existed, or an
    inbound call whose Calls record is created by a different path) — this
    is expected to happen and Platform X's handler should treat a null
    call_id gracefully, not as an error.

    We enforce a strict, short timeout on this call — see
    app/routers/webhooks.py's custom-tool handler for the exact value and
    reasoning — if Platform X's server does not respond in time, we return a
    clean error to the voice vendor rather than wait, so Platform X's own
    implementation should aim to respond quickly and avoid blocking work.

    Expected response body, 2xx status, `Content-Type: application/json`:
    ANY JSON object — it is returned to the voice vendor verbatim as the
    tool's result and the conversation continues using it (optionally mapped
    into `{{dynamic_variables}}` via the tool's own `response_variables`
    configuration). There is no fixed required shape here — a real
    integration point where Platform X controls the payload their own tool
    logic needs the conversation to see next.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tool_name": "check_availability",
                "args": {"date": "2026-09-01"},
                "call_id": "6706f1a2b3c4d5e6f7089abc",
                "agent_id": "6706f1a2b3c4d5e6f7089aaa",
            }
        }
    )

    tool_name: Annotated[str, Field(description="Which of your registered tools fired.")]
    args: Annotated[
        dict[str, Any],
        Field(description="The arguments the conversation brain filled in for this call."),
    ]
    call_id: Annotated[
        str | None,
        Field(description="Your own call id (from a prior webhook/response), if resolvable."),
    ]
    agent_id: Annotated[str, Field(description="Your own agent id (from POST /agents).")]
