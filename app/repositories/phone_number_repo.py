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


async def get_by_id(db: MongoDB, number_id: str, *, platform_id: str) -> PhoneNumberInDB | None:
    """Tenancy-scoped lookup — always filters on platform_id, never trusts a
    caller-supplied identifier alone. Mirrors agent_repo's ObjectId guard.
    """
    if not ObjectId.is_valid(number_id):
        return None
    doc = await db[PHONE_NUMBERS].find_one({"_id": ObjectId(number_id), "platform_id": platform_id})
    return _from_doc(doc) if doc else None


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
