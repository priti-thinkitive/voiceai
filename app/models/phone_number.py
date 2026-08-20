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
