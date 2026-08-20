"""MongoDB connection lifecycle (Motor, async).

Bootstrap step 4. Repositories are the only layer allowed to touch Motor
directly (see the layering rule in the standards doc) — this module owns the
client/connection lifecycle and exposes `get_db()` as the one FastAPI
dependency every repository is constructed from.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.collections import AGENTS, CALLS, PHONE_NUMBERS, PLATFORMS
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

MongoDB = AsyncIOMotorDatabase[dict[str, Any]]
MongoClient = AsyncIOMotorClient[dict[str, Any]]


class MongoState:
    client: MongoClient | None = None
    db: MongoDB | None = None


_state = MongoState()


async def connect_to_mongo(settings: Settings) -> MongoDB:
    if _state.db is not None:
        return _state.db
    logger.info(
        "Connecting to MongoDB",
        extra={"uri": settings.MONGODB_URI, "db": settings.MONGODB_DB_NAME},
    )
    # tz_aware=True makes Motor return timezone-aware (UTC) datetimes instead
    # of naive ones, so anything we serialize back out carries an explicit
    # offset rather than being silently misread as local time downstream.
    _state.client = AsyncIOMotorClient(
        settings.MONGODB_URI,
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
        tzinfo=UTC,
    )
    await _state.client.admin.command("ping")
    _state.db = _state.client[settings.MONGODB_DB_NAME]
    await _ensure_indexes(_state.db)
    logger.info("MongoDB connected and indexes ensured")
    return _state.db


async def disconnect_from_mongo() -> None:
    if _state.client is not None:
        _state.client.close()
        _state.client = None
        _state.db = None
        logger.info("MongoDB disconnected")


def get_db() -> MongoDB:
    """FastAPI dependency — every repository takes this as its `db` param."""
    if _state.db is None:
        raise RuntimeError("Database not initialized. connect_to_mongo() must run at startup.")
    return _state.db


async def ping(db: MongoDB) -> bool:
    try:
        await db.command("ping")
        return True
    except Exception:
        logger.warning("Mongo ping failed", exc_info=True)
        return False


async def _ensure_indexes(db: MongoDB) -> None:
    """Create all required indexes. Idempotent — safe to call on every boot.

    api_key_hash is looked up on every authenticated request
    (get_current_platform), so it must be indexed and unique — two platforms
    must never share a key hash.

    Agents.platform_id is indexed because every read (get_by_id, and any
    future list endpoint) is scoped through get_platform_filter, i.e.
    filtered on platform_id on every single query — an unindexed collection
    scan there would only get worse as agents accumulate.

    PhoneNumbers.platform_id is indexed for the same tenancy-scoping reason
    as Agents.platform_id above (every read on this collection is filtered
    by platform_id). PhoneNumbers.agent_id is also indexed: POST
    /agents/{agent_id}/numbers persists one PhoneNumbers doc per purchased
    number keyed to its owning agent, and a future "list numbers for this
    agent" / release-on-agent-delete lookup would filter on agent_id
    directly — indexing it now avoids a collection scan once a platform has
    bought numbers for many agents. PhoneNumbers.phone_number is indexed
    unique: it's already the natural key for this collection (see
    app/models/phone_number.py's vendor_ref docstring — the E.164 string IS
    the vendor's own identifier, no separate id exists), and
    phone_number_repo.get_by_phone_number_any_platform() now queries it with
    NO platform_id filter on every single inbound-call webhook from the
    voice vendor (app/routers/webhooks.py) — a hot, latency-sensitive path
    per that endpoint's "not our fault" timing mandate, so an unindexed scan
    here would be a direct, self-inflicted violation of "our own processing
    must be minimal." unique=True also enforces at the DB level that the
    same E.164 number can never be provisioned twice across platforms,
    matching the real-world constraint that a vendor-owned number belongs to
    exactly one of our platforms at a time.

    Calls.platform_id is indexed for the same tenancy-scoping reason as
    every other platform-scoped collection above — every read (get_by_id
    today, and any future GET /calls list endpoint, which per the standards
    doc's list-endpoint rules must be platform-scoped and paginated) filters
    on platform_id. Calls.agent_id is also indexed: POST /calls/outbound
    persists one Calls doc per triggered call keyed to the agent that ran
    it, and a future "list calls for this agent" lookup would filter on
    agent_id directly, same reasoning as PhoneNumbers.agent_id above. Unlike
    PhoneNumbers/Agents, Calls is expected to be append-heavy and to grow
    much larger over time (one document per call attempt, not per
    provisioned resource) — both indexes matter more here, not less, since
    an unindexed scan only gets worse as call volume grows.

    Calls.vendor_ref is indexed (sparse, since not every Calls document has
    one — a vendor-call-failure record persists with vendor_ref=None, see
    POST /calls/outbound's persist-on-vendor-failure design) for
    POST /webhooks/retell/post-call: every post-call webhook delivery
    (call_started/call_ended/call_analyzed) carries only the vendor's own
    call_id, not our Mongo _id, so call_repo.get_by_vendor_ref() is how that
    handler resolves which Calls document to update — an unindexed scan here
    would get worse as call volume grows, same reasoning as every other
    hot-path lookup index in this collection.
    """
    await db[PLATFORMS].create_index("api_key_hash", unique=True)
    await db[PLATFORMS].create_index("status")
    await db[AGENTS].create_index("platform_id")
    # Justified exactly like Calls.vendor_ref below: the custom-tool proxy
    # webhook (POST /webhooks/retell/custom-tool, see app/routers/webhooks.py)
    # receives a real vendor tool-call carrying only the voice vendor's own
    # agent_id (inside the payload's nested `call` object) and must resolve
    # it back to our own Agents document (and therefore platform + tool
    # webhook_url) via agent_repo.get_by_vendor_ref() on every single tool
    # call fired mid-conversation — an unindexed scan here directly adds to
    # live-call latency, same reasoning as every other hot-path lookup index
    # in this file.
    await db[AGENTS].create_index("vendor_ref", sparse=True)
    await db[PHONE_NUMBERS].create_index("platform_id")
    await db[PHONE_NUMBERS].create_index("agent_id")
    await db[PHONE_NUMBERS].create_index("phone_number", unique=True)
    await db[CALLS].create_index("platform_id")
    await db[CALLS].create_index("agent_id")
    await db[CALLS].create_index("vendor_ref", sparse=True)


@asynccontextmanager
async def lifespan_mongo(settings: Settings | None = None) -> AsyncIterator[MongoDB]:
    """Context manager form of the Mongo lifecycle — for scripts/tests."""
    s = settings or get_settings()
    db = await connect_to_mongo(s)
    try:
        yield db
    finally:
        await disconnect_from_mongo()
