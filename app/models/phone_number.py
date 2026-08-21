"""PhoneNumber — a phone number Platform X bought through us for inbound
calls to one of its own agents, per vendor-docs/White-Label-Launch-Plan.html's
Phase 1 scope item 4 ("Phone number — buy new through Retell", Option 1 of
the "Telephony: two options" section; BYO SIP is Option 2, a separate future
endpoint).

Backed by Retell's real `POST /create-phone-number` (see
app/services/retell_adapter.py's create_phone_number()), but that's an
implementation detail Platform X never sees — no vendor name in the public
shape, same principle as AgentPublic.

Own collection (`PhoneNumbers`), not an embedded field on Agent — a phone
number has its own lifecycle independent of the agent it's currently bound
to (e.g. a future release/reassign flow), per the standards doc's "own
independent lifecycle" test for when a sub-resource earns its own
collection.

**`vendor_ref` design decision, reasoned through explicitly (not copied
blindly from AgentInDB):** on Agent, `vendor_ref` holds Retell's `agent_id`,
which is a genuinely separate, opaque, internal-only correlation ID — it is
never something Platform X needs back, only something we use to correlate
webhooks / debug against Retell's dashboard. A phone number is different:
Retell's `/create-phone-number` response confirms Retell does not issue any
separate opaque ID for a phone number at all — the E.164 `phone_number`
string IS Retell's own identifier for this resource (it's what every other
Retell phone-number endpoint, e.g. `GET /get-phone-number/{phone_number}`,
`DELETE /delete-phone-number/{phone_number}`, keys off). `phone_number` is
also exactly the value Platform X needs back in our own response (point 2 of
the task brief) to know what number was provisioned. Storing it twice, once
as `phone_number` and again as an internal `vendor_ref` holding the identical
string, would violate the standards doc's "no duplicate fields carrying the
same information" rule (two fields, one fact, risk of drift) for zero
benefit — there is no separate "public-safe number" vs "internal vendor
correlation id" the way there genuinely is for Agent's Retell `agent_id` vs
our own Mongo `_id`. So: no separate `vendor_ref` field on PhoneNumberInDB.
`phone_number` alone plays both roles (Retell's identifier for this record,
used internally for e.g. a future delete/release call; and the value
Platform X sees). `vendor` (`"retell"`) is still recorded, per the standards
doc's non-negotiable "any collection holding vendor-originated data records
which vendor produced it" rule — that rule is about which vendor, not about
a redundant per-record id.

**`PhoneNumberInDB`/`PhoneNumberPublic` are reused as-is for BYO-imported
numbers too (POST /agents/{agent_id}/numbers/byo, Option 2 telephony — see
ImportPhoneNumberRequest below), not forked into a separate model.** A
BYO-imported number and a buy-new number are the same kind of resource once
they exist in `PhoneNumbers` — both have `phone_number`/`area_code`/
`nickname`/`agent_id`/`vendor`/timestamps, and Retell's own response shape
for `/import-phone-number` is confirmed identical to `/create-phone-number`'s
(see retell_adapter.py). Considered adding an `acquisition_method` field
("buy_new" vs "byo") to distinguish how a number was acquired, per the
standards doc's "no unnecessary fields" rule this was deliberately NOT
added: nothing in either endpoint's scope today reads or filters on it —
there's no list/reporting endpoint yet that would need to group numbers by
acquisition method, and `area_code`/`nickname` genuinely apply to a
BYO-imported number just as much as a bought one, so there's no case where
the two flows produce structurally different-looking documents. If a real
future need to distinguish them shows up (e.g. a report or a
release/delete flow that behaves differently per acquisition path), add the
field then, justified by that concrete need — not speculatively now.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class PhoneNumberInDB(BaseModel):
    """Shape of a `PhoneNumbers` document as stored in Mongo."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    platform_id: str
    agent_id: str

    phone_number: str
    area_code: int | None
    nickname: str | None

    vendor: str

    created_at: datetime
    updated_at: datetime


class PhoneNumberPublic(BaseModel):
    """Safe-to-return shape.

    Deliberately minimal for now (explicit user decision, not an oversight):
    the voice vendor's phone-number-purchase response also includes several
    internal-looking or vendor-implementation fields that are not exposed
    here. Returning only what's confirmed useful today is additive-safe —
    more fields can be added later without breaking any existing caller,
    whereas internal/unused fields exposed now can't be cleanly taken back
    once a Platform X integration might start depending on them.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "phone_number": "+19129143920",
                "area_code": 912,
                "nickname": "Aspen Quality Care — main line",
                "agent_id": "6706f1a2b3c4d5e6f7089abc",
                "created_at": "2026-08-19T10:00:00Z",
            }
        }
    )

    phone_number: str
    area_code: int | None
    nickname: str | None
    agent_id: str
    created_at: datetime


class CreatePhoneNumberRequest(BaseModel):
    """POST /agents/{agent_id}/numbers request body.

    Every field is optional — the voice vendor picks sensible values (e.g.
    a random available US number) when omitted. Field names/semantics
    match the voice vendor's own real, documented phone-number-purchase
    request fields exactly — a deliberate exception to VoiceAI's usual
    narrower-field-set pattern (see POST /agents), because Platform X
    integrators reasonably expect control over area code / toll-free / a
    specific requested number when buying a phone number, and these are
    real vendor-documented fields, not guessed ones.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "area_code": 415,
                "toll_free": False,
                "country_code": "US",
                "phone_number": "+14155551234",
                "nickname": "Aspen Quality Care — main line",
            }
        }
    )

    area_code: Annotated[
        int | None,
        Field(
            description="Preferred US/Canada area code for the new number (e.g. 415). "
            "We pick a random available number if omitted. Ignored if `phone_number` "
            "is also given.",
        ),
    ] = None
    toll_free: Annotated[
        bool | None,
        Field(
            description="Request a toll-free number instead of a local one. Defaults to "
            "our own default (false / local number) if omitted.",
        ),
    ] = None
    country_code: Annotated[
        str | None,
        Field(
            description="ISO country code for the new number, e.g. 'US' or 'CA'. Defaults to "
            "our own default ('US') if omitted.",
        ),
    ] = None
    phone_number: Annotated[
        str | None,
        Field(
            description="Request this exact E.164 number (e.g. '+14155551234') instead of "
            "letting us pick one. Must be a number we can actually provision; omit to "
            "let us choose.",
        ),
    ] = None
    nickname: Annotated[
        str | None,
        Field(
            max_length=200,
            description="An internal display label for this number (e.g. 'Aspen Quality Care "
            "— main line'). Not shown to callers. Optional.",
        ),
    ] = None


# Internal sourcing note (plain comment, NOT the class docstring below — a
# Pydantic model's class docstring renders as that schema's `description` in
# Swagger's components.schemas, so vendor-identifying text must never live
# there; see this codebase's standards doc, "Swagger-visible text must never
# name the vendor" section). Confirmed via a fresh live WebFetch this
# session (docs.retellai.com/api-references/update-phone-number): `PATCH
# /update-phone-number/{phone_number}` — the phone number is a path
# parameter (matching this codebase's existing delete-phone-number/
# get-phone-number pattern, NOT a body field), every field on the request
# body is optional/nullable, and it is a genuine field-level partial-merge
# endpoint (confirmed the same way update-agent/update-retell-llm already
# were for PATCH /agents/{agent_id} — see that endpoint's own module
# docstring in app/routers/agents.py): an omitted field leaves the vendor's
# existing stored value untouched, only fields actually present in the
# request body are changed. The real field name for renaming is `nickname`
# (a plain string). The real field name for rebinding is `inbound_agents` —
# an array of `{agent_id, agent_version, weight}` objects, the SAME
# array-of-AgentWeight binding mechanism create_phone_number()/
# import_phone_number() already use (see retell_adapter.py's module
# docstring — the older singular `inbound_agent_id` field remains confirmed
# deprecated as of 2026-03-31 and is never sent by this codebase). This
# re-confirms, fresh, that `inbound_agents` is still the current, correct
# field for this task rather than trusting a cached assumption carried over
# from the create/import endpoints. The vendor's own `nickname` field is
# documented as "for your reference only" with no special vendor-side
# meaning for an empty string (unlike welcome_message's real, vendor-
# meaningful three-state distinction between omitted/null/empty-string on
# PATCH /agents/{agent_id}) — this is why `nickname` below does not need a
# separate clear_* flag; see its own Field description for the full
# reasoning kept vendor-neutral.
class UpdatePhoneNumberRequest(BaseModel):
    """PATCH /agents/{agent_id}/numbers/{phone_number} request body — closes
    a real, confirmed gap: until now there was no way to rename a number's
    `nickname` or rebind it to a different agent without deleting and
    recreating it. Deleting a *bought* number risks losing it permanently
    (per DELETE /agents/{agent_id}/numbers/{phone_number}'s own docstring:
    "not guaranteed to be available to re-provision later"), and for a BYO
    SIP number it means re-entering SIP trunk credentials all over again —
    a small, low-risk rename/rebind operation should not require that level
    of risk.

    **Both fields are optional — a caller can rename only, rebind only, or
    both in one call**, matching PATCH /agents/{agent_id}'s own "true partial
    update, omit what you don't want to change" convention. `omit ==
    unchanged` for BOTH fields here — there is no `clear_nickname`-style flag
    the way UpdateAgentRequest needs for welcome_message/transfer_number/
    agent_name, and that asymmetry is deliberate, not an inconsistency:
    those three fields on UpdateAgentRequest need a separate clear flag
    specifically because Pydantic parses an explicit `null` in the request
    JSON identically to an omitted field once the field type itself is
    `X | None` — there is no way, from the parsed body alone, to distinguish
    "the caller explicitly wants this cleared" from "the caller didn't
    mention this field at all." `nickname` here has the exact same
    theoretical ambiguity (its type is `str | None`) — but unlike those three
    fields, nothing in this endpoint's real scope needs a distinct
    THIRD state ("explicitly clear the nickname back to none," as opposed to
    "leave whatever nickname is currently set alone") to be reachable via a
    single PATCH call. A caller who wants to clear a nickname can simply send
    `nickname: ""` (empty string) — the voice vendor's own `nickname` field
    is "for your reference only" with no special vendor-side meaning for an
    empty string, unlike welcome_message's real, vendor-meaningful
    three-state distinction between omitted/null/empty-string — so an empty
    string is both a real, useful "no nickname" value on our side AND
    something `has_any_field_set()` below correctly still recognizes as "the
    caller touched this field" (since `""` is not `None`). This is the
    simplest-correct choice per the standards doc's own guidance to compare
    against `welcome_message`'s precedent and use judgment rather than copy
    a heavier pattern nothing here actually needs — revisit only if a real
    future need for a genuine three-state distinction on `nickname`
    specifically shows up.

    `agent_id` is VoiceAI's own Mongo agent id (never the voice vendor's raw
    agent id — same "never leak vendor identity" translation every other
    endpoint in this codebase already performs), tenancy-scoped and
    status-checked by the router exactly like every other agent lookup — see
    PATCH /agents/{agent_id}/numbers/{phone_number}'s own docstring in
    app/routers/agents.py for the full validation contract.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "nickname": "Aspen Quality Care — after-hours line",
                "agent_id": "6706f1a2b3c4d5e6f7089abc",
            }
        }
    )

    nickname: Annotated[
        str | None,
        Field(
            max_length=200,
            description="A new internal display label for this number. Omit to leave the "
            "current nickname unchanged. Send an empty string to clear it back to none — "
            "there is no separate clear flag, since (unlike welcome_message on PATCH "
            "/agents/{agent_id}) an empty string here is already a real, unambiguous "
            "'no nickname' value with no other special meaning.",
        ),
    ] = None
    agent_id: Annotated[
        str | None,
        Field(
            description="Rebind this number to a different agent of yours, by our own agent "
            "id (from POST /agents' response) — never the voice vendor's own agent id. Omit "
            "to leave the number bound to its current agent. The target agent must belong to "
            "you and must have finished creation on the voice vendor (status != 'failed').",
        ),
    ] = None

    def has_any_field_set(self) -> bool:
        """Same "an entirely-empty PATCH body is well-formed Pydantic but a
        meaningless no-op" check as UpdateAgentRequest.has_any_field_set() —
        deliberately NOT enforced as a model-level validator (matching that
        model's own precedent exactly, not just its spirit): the ROUTER is
        what rejects an empty request with a clear 422, by calling this
        method itself, same as PATCH /agents/{agent_id} already does — see
        that endpoint's own docstring in app/routers/agents.py, step 2, and
        `update_agent`'s "reject an empty request" reasoning, which applies
        identically here.
        """
        return self.nickname is not None or self.agent_id is not None


class ImportPhoneNumberRequest(BaseModel):
    """POST /agents/{agent_id}/numbers/byo request body — Option 2 of
    "Telephony: two options" (bring your own SIP trunk), per
    vendor-docs/White-Label-Launch-Plan.html's Phase 1 scope item 4.

    A separate model from CreatePhoneNumberRequest, deliberately — the two
    endpoints' fields genuinely differ (this one requires `phone_number` and
    `termination_uri` up front, since there's no "let us pick one" for a
    number Platform X already owns) and backs a real, separate vendor
    endpoint for importing an existing number over a SIP trunk (distinct
    from the buy-new endpoint). Field names/semantics match the voice
    vendor's real, confirmed fields exactly — notably `termination_uri`,
    not `sip_trunk_uri`, which was an earlier planning-doc illustrative
    guess that turned out wrong once actually checked against the vendor's
    live docs. There is no `sip_provider` field on the vendor's real API
    either; the planning doc invented it, and it is not included here.

    **`sip_trunk_auth_username`/`sip_trunk_auth_password` are forwarded to
    the voice vendor but are NEVER persisted on our own `PhoneNumbers`
    document and NEVER logged, at any level — explicit, reasoned design
    decision, not an oversight.** The vendor itself already
    holds these credentials operationally (it needs them for every
    outbound call placed through that trunk), so VoiceAI storing a second
    copy adds real breach-surface risk (a compromise of our DB would leak
    Platform X's own third-party telephony provider's credentials) with no
    debugging capability the vendor's own account doesn't already provide.
    This mirrors how the vendor API key itself is handled (held in config,
    never returned to a caller) — same principle, applied here to a third
    party's credentials passing through us. If Platform X ever needs to
    rotate these credentials, that is a future PATCH-style endpoint that
    re-submits fresh credentials directly to the vendor, not something read
    back from our own storage — there is nothing to read back, by design.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "phone_number": "+14155551234",
                "termination_uri": "platformx.pstn.twilio.com",
                "sip_trunk_auth_username": "platformx_trunk_user",
                "sip_trunk_auth_password": "correct-horse-battery-staple",
                "ignore_e164_validation": False,
                "transport": "TLS",
                "nickname": "Aspen Quality Care — BYO trunk",
            }
        }
    )

    phone_number: Annotated[
        str,
        Field(
            description="The E.164 phone number Platform X already owns and wants to import "
            "(e.g. '+14155551234'). This number must already be provisioned and routable on "
            "your own SIP trunk before calling this endpoint — we do not purchase or "
            "provision it.",
        ),
    ]
    termination_uri: Annotated[
        str,
        Field(
            description="The SIP termination URI identifying your SIP trunk (e.g. "
            "'platformx.pstn.twilio.com'). Your trunk must already be configured at your own "
            "telephony provider (Twilio, Telnyx, Vonage, etc. all have dedicated setup "
            "guides for connecting to a voice platform like ours) and must whitelist our "
            "IP ranges before this import can succeed: "
            "18.98.16.120/30, 3.42.144.0/23, 153.57.128.0/18, 143.223.88.0/21, "
            "161.115.160.0/19.",
        ),
    ]
    sip_trunk_auth_username: Annotated[
        str | None,
        Field(
            description="Auth username for your SIP trunk, if it requires authentication. "
            "Forwarded to the voice vendor; never stored on our side or returned in any "
            "response — see this model's docstring for why. Omit if your trunk uses "
            "IP-based auth only.",
        ),
    ] = None
    sip_trunk_auth_password: Annotated[
        str | None,
        Field(
            description="Auth password for your SIP trunk, if it requires authentication. "
            "Forwarded to the voice vendor; never stored on our side, never logged, and "
            "never returned in any response — see this model's docstring for why. Omit if "
            "your trunk uses IP-based auth only.",
        ),
    ] = None
    ignore_e164_validation: Annotated[
        bool | None,
        Field(
            description="Skip strict E.164 format validation on `phone_number`. Defaults to "
            "our own default (true) if omitted.",
        ),
    ] = None
    transport: Annotated[
        str | None,
        Field(
            description="SIP transport protocol for your trunk: 'TLS', 'TCP', or 'UDP'. "
            "Defaults to our own default ('TCP') if omitted.",
        ),
    ] = None
    nickname: Annotated[
        str | None,
        Field(
            max_length=200,
            description="An internal display label for this number (e.g. 'Aspen Quality Care "
            "— main line'). Not shown to callers. Optional.",
        ),
    ] = None
