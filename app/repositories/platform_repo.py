"""Repository for the `Platforms` collection — the only layer touching Motor
for platform data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.collections import PLATFORMS
from app.database import MongoDB
from app.models.platform import PlatformInDB, PlatformStatus


def _from_doc(doc: dict[str, Any]) -> PlatformInDB:
    return PlatformInDB(
        id=str(doc["_id"]),
        name=doc["name"],
        api_key_hash=doc["api_key_hash"],
        api_key_prefix=doc["api_key_prefix"],
        status=PlatformStatus(doc.get("status", PlatformStatus.ACTIVE)),
        inbound_variables_webhook_url=doc.get("inbound_variables_webhook_url"),
        call_completed_webhook_url=doc.get("call_completed_webhook_url"),
        call_completed_webhook_secret=doc.get("call_completed_webhook_secret"),
        created_at=doc["created_at"],
        updated_at=doc.get("updated_at", doc["created_at"]),
        revoked_at=doc.get("revoked_at"),
    )


async def create(db: MongoDB, *, name: str, api_key_hash: str, api_key_prefix: str) -> PlatformInDB:
    now = datetime.now(UTC)
    doc = {
        "name": name,
        "api_key_hash": api_key_hash,
        "api_key_prefix": api_key_prefix,
        "status": PlatformStatus.ACTIVE.value,
        "inbound_variables_webhook_url": None,
        "call_completed_webhook_url": None,
        "call_completed_webhook_secret": None,
        "created_at": now,
        "updated_at": now,
        "revoked_at": None,
    }
    result = await db[PLATFORMS].insert_one(doc)
    doc["_id"] = result.inserted_id
    return _from_doc(doc)


async def get_by_api_key_hash(db: MongoDB, api_key_hash: str) -> PlatformInDB | None:
    doc = await db[PLATFORMS].find_one({"api_key_hash": api_key_hash})
    return _from_doc(doc) if doc else None


async def get_by_id(db: MongoDB, platform_id: str) -> PlatformInDB | None:
    if not ObjectId.is_valid(platform_id):
        return None
    doc = await db[PLATFORMS].find_one({"_id": ObjectId(platform_id)})
    return _from_doc(doc) if doc else None


async def revoke(db: MongoDB, platform_id: str) -> bool:
    """Mark a platform's key revoked. Does not delete the row — rotation and
    revocation both work by flipping status, so audit history is preserved.
    """
    if not ObjectId.is_valid(platform_id):
        return False
    now = datetime.now(UTC)
    result = await db[PLATFORMS].update_one(
        {"_id": ObjectId(platform_id)},
        {"$set": {"status": PlatformStatus.REVOKED.value, "revoked_at": now, "updated_at": now}},
    )
    return result.modified_count == 1


async def set_inbound_variables_webhook_url(
    db: MongoDB, platform_id: str, *, url: str | None
) -> bool:
    """Set (or clear, with `url=None`) the platform's registered URL for the
    inbound dynamic-variable relay (see app/routers/webhooks.py).

    No HTTP endpoint calls this yet — set directly, by hand, until a real
    platform-settings API exists (same documented gap as platform onboarding
    itself; see PlatformInDB's docstring). Kept as its own narrow repository
    function rather than a generic "update platform" method so the one real
    write path this feature needs is explicit and easy to find.
    """
    if not ObjectId.is_valid(platform_id):
        return False
    now = datetime.now(UTC)
    result = await db[PLATFORMS].update_one(
        {"_id": ObjectId(platform_id)},
        {"$set": {"inbound_variables_webhook_url": url, "updated_at": now}},
    )
    return result.modified_count == 1


async def set_call_completed_webhook_url(
    db: MongoDB, platform_id: str, *, url: str | None, new_secret: str | None
) -> bool:
    """Set (or clear, with `url=None`) the platform's registered URL for the
    outbound call-completed notification (see
    app/services/call_completed_webhook.py).

    `new_secret`: pass a freshly-generated secret (see
    `security.generate_webhook_secret()`) ONLY when the router has decided
    this call must (re)generate one — i.e. `url` is being set to a non-null
    value and the platform had no secret yet. Pass `None` for every other
    case (clearing the URL, or setting it while a secret already exists) so
    an existing secret is never silently overwritten or wiped by a routine
    URL update — the secret's own lifecycle is independent of the URL's,
    once generated. The router (app/routers/platform.py) is responsible for
    deciding whether generation is needed by reading the platform's current
    `call_completed_webhook_secret` first; this repository function only
    executes whatever decision it's given, mirroring how
    `set_inbound_variables_webhook_url` above stays a thin, explicit write
    path rather than embedding business logic in the repository layer.
    """
    if not ObjectId.is_valid(platform_id):
        return False
    now = datetime.now(UTC)
    update: dict[str, Any] = {"call_completed_webhook_url": url, "updated_at": now}
    if new_secret is not None:
        update["call_completed_webhook_secret"] = new_secret
    result = await db[PLATFORMS].update_one(
        {"_id": ObjectId(platform_id)},
        {"$set": update},
    )
    return result.modified_count == 1
