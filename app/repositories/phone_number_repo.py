"""Repository for the `PhoneNumbers` collection — the only layer touching
Motor for phone-number data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.collections import PHONE_NUMBERS
from app.database import MongoDB
from app.models.phone_number import PhoneNumberInDB


def _from_doc(doc: dict[str, Any]) -> PhoneNumberInDB:
    return PhoneNumberInDB(
        id=str(doc["_id"]),
        platform_id=doc["platform_id"],
        agent_id=doc["agent_id"],
        phone_number=doc["phone_number"],
        area_code=doc.get("area_code"),
        nickname=doc.get("nickname"),
        vendor=doc["vendor"],
        created_at=doc["created_at"],
        updated_at=doc.get("updated_at", doc["created_at"]),
    )


async def create(
    db: MongoDB,
    *,
    platform_id: str,
    agent_id: str,
    phone_number: str,
    area_code: int | None,
    nickname: str | None,
    vendor: str,
) -> PhoneNumberInDB:
    now = datetime.now(UTC)
    doc = {
        "platform_id": platform_id,
        "agent_id": agent_id,
        "phone_number": phone_number,
        "area_code": area_code,
        "nickname": nickname,
        "vendor": vendor,
        "created_at": now,
        "updated_at": now,
    }
    result = await db[PHONE_NUMBERS].insert_one(doc)
    doc["_id"] = result.inserted_id
    return _from_doc(doc)


async def get_by_phone_number(
    db: MongoDB, phone_number: str, *, platform_id: str
) -> PhoneNumberInDB | None:
    """Tenancy-scoped lookup by the E.164 number itself, not our Mongo id —
    used by POST /calls/outbound to confirm a caller-supplied `from_number`
    is genuinely one this platform provisioned with us before ever placing a
    call from it. Always filters on platform_id, same tenancy discipline as
    get_by_id above: a well-formed number that belongs to a different
    platform (or no platform at all) must not resolve here.
    """
    doc = await db[PHONE_NUMBERS].find_one(
        {"phone_number": phone_number, "platform_id": platform_id}
    )
    return _from_doc(doc) if doc else None


async def list_by_agent_id(
    db: MongoDB, agent_id: str, *, platform_id: str
) -> list[PhoneNumberInDB]:
    """Tenancy-scoped list of every number currently bound to one agent —
    `GET /agents/{agent_id}/numbers`'s only real query.

    No pagination — deliberately, per the standards doc's own "lean toward
    the simpler version" bias, since the number of phone numbers bound to a
    single agent is realistically small (a handful at most; this is not a
    large, independently-growing collection the way a platform's full call
    history is) and there is no existing precedent in this codebase for a
    caller needing to page through a single agent's own numbers. Revisit
    only if a real need for it shows up. Sorted newest-first for
    consistency with `agent_repo.list_by_platform_id`'s own ordering
    convention.

    `platform_id` is required and enforced here (not left to the caller's
    own agent-ownership check alone) for the same defense-in-depth reason
    every other tenancy-scoped query in this codebase double-checks
    ownership at the query level rather than trusting an earlier check
    performed elsewhere in the call chain.
    """
    cursor = (
        db[PHONE_NUMBERS]
        .find({"agent_id": agent_id, "platform_id": platform_id})
        .sort("created_at", -1)
    )
    docs = await cursor.to_list(length=None)
    return [_from_doc(doc) for doc in docs]


async def delete(db: MongoDB, number_id: str, *, platform_id: str) -> bool:
    """Tenancy-scoped hard delete of our own PhoneNumbers document — always
    called AFTER the router has already released the real vendor-side
    number via retell_adapter.delete_phone_number() — see DELETE
    /agents/{agent_id}/numbers/{phone_number}'s own docstring in
    app/routers/agents.py for the full ordering/partial-failure contract.

    A genuine hard delete, not a soft-delete — same reasoning as
    agent_repo.delete() above: a released phone number has no ongoing need
    to be kept as a "deleted" record (unlike Platform.status, which keeps a
    revoked platform's own audit trail). Returns a bool (`deleted_count ==
    1`); `False` covers both a malformed id and "no matching document for
    this platform_id."
    """
    if not ObjectId.is_valid(number_id):
        return False
    result = await db[PHONE_NUMBERS].delete_one(
        {"_id": ObjectId(number_id), "platform_id": platform_id}
    )
    return result.deleted_count == 1


async def update(
    db: MongoDB,
    number_id: str,
    *,
    platform_id: str,
    nickname: str | None,
    agent_id: str,
) -> bool:
    """Tenancy-scoped update of `nickname`/`agent_id` on our own PhoneNumbers
    document — always called AFTER the router has already confirmed the real
    vendor-side rename/rebind succeeded (PATCH /agents/{agent_id}/numbers/
    {phone_number}, app/routers/agents.py), same "never update our own record
    ahead of a confirmed vendor-side success" ordering every other write path
    in this module already follows (see delete()'s own docstring for the same
    principle applied to a release instead of an update).

    Same `update_one` + bool-return + separate get_by_id-by-the-caller shape
    as agent_repo.update() — deliberately matched rather than inventing a
    `find_one_and_update`/return-the-fresh-doc pattern that has no other
    precedent anywhere in this codebase's repository layer.

    Unlike delete()/create(), this always receives a real `agent_id` — the
    caller (the router) has already resolved "leave the current binding
    alone" vs. "rebind to a new one" into one concrete value before calling
    this function, exactly the same "the router computes the merged intended
    state, the repository just writes it" division of labor PATCH
    /agents/{agent_id} already established for AgentInDB (see that endpoint's
    own module docstring, step 5). This repository layer has no independent
    concept of "unchanged" for agent_id — a plain, unconditional `$set`.

    `nickname` accepts `None` as a genuine value (clear it back to unset) —
    see UpdatePhoneNumberRequest's own docstring in app/models/phone_number.py
    for why nickname does not need a separate clear_* flag the way
    UpdateAgentRequest's welcome_message/transfer_number/agent_name fields
    do: the vendor's own `update-phone-number` accepts `nickname: null` to
    genuinely unset it (confirmed via the same live WebFetch this task's own
    docstring cites), so there is no omitted-vs-null ambiguity left to
    resolve once the router has already decided "the caller touched this
    field" — that decision already happened before this function is ever
    called (see UpdatePhoneNumberRequest.has_any_field_set()).

    `updated_at` is always bumped, per the standards doc's baseline-field
    rule — every mutation, not just create(), touches it.

    Returns a bool (`modified_count == 1` OR a matched-but-identical no-op
    write — see the `matched_count` check below, same reasoning
    agent_repo.update() itself doesn't need since every one of its own
    callers always changes at least one field); `False` covers both a
    malformed id and "no matching document for this platform_id."
    """
    if not ObjectId.is_valid(number_id):
        return False
    now = datetime.now(UTC)
    result = await db[PHONE_NUMBERS].update_one(
        {"_id": ObjectId(number_id), "platform_id": platform_id},
        {
            "$set": {
                "nickname": nickname,
                "agent_id": agent_id,
                "updated_at": now,
            }
        },
    )
    return result.matched_count == 1


async def get_by_phone_number_any_platform(
    db: MongoDB, phone_number: str
) -> PhoneNumberInDB | None:
    """Lookup by the E.164 number alone, with NO platform_id filter —
    deliberately different from get_by_phone_number above, and only ever
    used by the inbound-call webhook handler (app/routers/webhooks.py).

    That handler receives a webhook from the voice vendor carrying only
    `to_number` — it doesn't know which platform owns that number yet; this
    lookup is exactly how tenancy gets resolved for an inbound call, so it
    cannot itself be tenancy-scoped (there is no caller-supplied platform_id
    to trust even if there were one — see deps.py's rule that authorization
    never comes from a client-supplied field). This is safe specifically
    because the caller here is the voice vendor's own signed webhook (HMAC-
    verified before this is ever reached), not an arbitrary HTTP caller, and
    the only thing this lookup returns is "which platform/agent owns this
    number," never platform-scoped data belonging to someone else.

    `phone_number` is unique per vendor account in practice (the voice
    vendor would not let the same E.164 number be provisioned twice), but
    this returns at most one match either way via find_one.
    """
    doc = await db[PHONE_NUMBERS].find_one({"phone_number": phone_number})
    return _from_doc(doc) if doc else None
