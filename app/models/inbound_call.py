"""Models for inbound dynamic-variable injection — the plan doc's `POST
/calls/:id/variables` idea, built as a synchronous vendor-webhook flow
instead of a REST endpoint Platform X calls, because that's how the real
mechanism actually works (see app/routers/webhooks.py's module docstring for
the full, confirmed real trace).

Two distinct wire shapes live here, deliberately kept separate even though
both are "the payload for an inbound call":

1. `RetellInboundCallWebhook`/`RetellInboundCallDetails` — what the voice
   vendor sends US. A vendor webhook body, not a Platform-X-facing request;
   FastAPI never renders these in Swagger (the router takes the raw request
   body for signature verification, not a parsed Pydantic model — see
   app/routers/webhooks.py), so vendor-neutral phrasing isn't load-bearing
   here, but is used anyway for consistency.
2. `PlatformVariablesRequest`/`PlatformVariablesResponse` — the contract WE
   define and Platform X must implement on their own
   `inbound_variables_webhook_url` server. This is genuinely a Platform-X-
   facing API contract (they write the handler, we write the client). No
   FastAPI router uses these as a request/response body either (we are the
   HTTP *client* here, not the server), so this docstring plus
   app/services/platform_relay.py's module docstring together ARE the
   documentation of this contract for Platform X's own implementers.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class RetellInboundCallDetails(BaseModel):
    """The `call_inbound` object nested inside the vendor's webhook body.

    Only fields this endpoint actually reads are modeled: `agent_id` here is
    the VOICE VENDOR's own agent id (not our Mongo id) — used to resolve
    which of our own PhoneNumbers/Agents records this inbound call concerns.
    """

    agent_id: str | None = None
    agent_version: int | None = None
    from_number: str | None = None
    to_number: str | None = None
    custom_sip_headers: dict[str, Any] | None = None


class RetellInboundCallWebhook(BaseModel):
    """Body of the `call_inbound` webhook the voice vendor POSTs to our own
    inbound webhook endpoint the moment a call rings in, before answering.

    Confirmed real shape (already researched this session, not guessed):

        {"event": "call_inbound",
         "call_inbound": {"agent_id": ..., "agent_version": ...,
                           "from_number": ..., "to_number": ...,
                           "custom_sip_headers": {...}},
         "event_timestamp": ...}
    """

    model_config = ConfigDict(populate_by_name=True)

    event: str
    call_inbound: RetellInboundCallDetails
    event_timestamp: int | None = None


class PlatformVariablesRequest(BaseModel):
    """What WE send to Platform X's own registered
    `inbound_variables_webhook_url` — the contract Platform X's developers
    must implement on their own server to receive this relay.

    POST body we send, `Content-Type: application/json`:

        {"from_number": "+15551234567",
         "to_number": "+19129143920",
         "agent_id": "6706f1a2b3c4d5e6f7089abc"}

    `agent_id` is deliberately OUR OWN Mongo agent id (from Platform X's own
    prior `POST /agents` call), never the voice vendor's internal id —
    matching the "never leak vendor_ref" rule applied to every other
    Platform-X-facing contract in this codebase.

    We enforce a strict, short timeout on this call (see
    app/services/platform_relay.py for the exact value and reasoning) — if
    Platform X's server does not respond in time, we proceed with a safe
    fallback rather than wait, so Platform X's own implementation should
    aim to respond quickly (well under our timeout) and avoid any blocking
    work (its own DB/network calls) in this handler.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "from_number": "+15551234567",
                "to_number": "+19129143920",
                "agent_id": "6706f1a2b3c4d5e6f7089abc",
            }
        }
    )

    from_number: Annotated[str, Field(description="E.164 number the caller is dialing from.")]
    to_number: Annotated[
        str, Field(description="E.164 number the caller is dialing — one of your own numbers.")
    ]
    agent_id: Annotated[
        str, Field(description="Your own agent id (from POST /agents) bound to this number.")
    ]


class PlatformVariablesResponse(BaseModel):
    """The JSON body Platform X's `inbound_variables_webhook_url` handler
    must return to us. Only `dynamic_variables` is read; any other field in
    the response body is ignored.

    Expected response body, `Content-Type: application/json`, 2xx status:

        {"dynamic_variables": {"contact_name": "John Smith"}}

    Every value must be a string (same `dict[str, str]` constraint as `POST
    /calls/outbound`'s `dynamic_variables` field) — stringify numbers/
    booleans on your side before responding. An empty object (`{}`) or an
    empty/missing `dynamic_variables` key is treated as "no personalization
    for this call," not an error — the call still proceeds using the agent's
    own already-configured base prompt.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"dynamic_variables": {"contact_name": "John Smith"}}}
    )

    dynamic_variables: Annotated[
        dict[str, str],
        Field(
            default_factory=dict,
            description="Per-call {{variable}} values to inject into the agent's prompt for "
            "this specific inbound call. Every value must already be a string.",
        ),
    ]
