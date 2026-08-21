"""Repository for the `Calls` collection — the only layer touching Motor for
outbound-call data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.collections import CALLS
from app.database import MongoDB
from app.models.call import CallDirection, CallInDB, CallStatus


def _from_doc(doc: dict[str, Any]) -> CallInDB:
    return CallInDB(
        id=str(doc["_id"]),
        platform_id=doc["platform_id"],
        agent_id=doc["agent_id"],
        from_number=doc["from_number"],
        to_number=doc["to_number"],
        dynamic_variables=doc.get("dynamic_variables", {}),
        status=CallStatus(doc["status"]),
        # Pre-existing documents (before `direction` was added) default to
        # OUTBOUND — the only real path that has ever created a Calls
        # document (see CallDirection's docstring), same defensive
        # doc.get(...)-with-fallback pattern already established for
        # updated_at backfill below.
        direction=CallDirection(doc.get("direction", CallDirection.OUTBOUND)),
        vendor=doc["vendor"],
        vendor_ref=doc.get("vendor_ref"),
        # All default to None/False for any pre-existing document that
        # predates the post-call webhook feature (or, for in_voicemail/
        # disconnection_reason, predates the voicemail-detection feature) —
        # same defensive doc.get(...)-with-fallback pattern the standards
        # doc's Database section already establishes for updated_at backfill
        # (see _from_doc's updated_at line below), applied here to every
        # post-call field.
        recording_url=doc.get("recording_url"),
        transcript_url=doc.get("transcript_url"),
        summary=doc.get("summary"),
        sentiment=doc.get("sentiment"),
        extracted_data=doc.get("extracted_data"),
        recording_rehost_failed=doc.get("recording_rehost_failed", False),
        in_voicemail=doc.get("in_voicemail"),
        disconnection_reason=doc.get("disconnection_reason"),
        created_at=doc["created_at"],
        updated_at=doc.get("updated_at", doc["created_at"]),
    )


async def create(
    db: MongoDB,
    *,
    platform_id: str,
    agent_id: str,
    from_number: str,
    to_number: str,
    dynamic_variables: dict[str, str],
    status: CallStatus,
    vendor: str,
    vendor_ref: str | None,
    direction: CallDirection = CallDirection.OUTBOUND,
) -> CallInDB:
    now = datetime.now(UTC)
    doc = {
        "platform_id": platform_id,
        "agent_id": agent_id,
        "from_number": from_number,
        "to_number": to_number,
        "dynamic_variables": dynamic_variables,
        "status": status.value,
        "direction": direction.value,
        "vendor": vendor,
        "vendor_ref": vendor_ref,
        "created_at": now,
        "updated_at": now,
    }
    result = await db[CALLS].insert_one(doc)
    doc["_id"] = result.inserted_id
    return _from_doc(doc)


async def get_by_id(db: MongoDB, call_id: str, *, platform_id: str) -> CallInDB | None:
    """Tenancy-scoped lookup — always filters on platform_id, never trusts a
    caller-supplied identifier alone. Mirrors agent_repo/phone_number_repo's
    ObjectId guard. Used by the recording/transcript serving endpoints (see
    app/routers/calls.py) as the tenancy boundary — a platform can only ever
    fetch its OWN call's re-hosted files, never another platform's, exactly
    the same discipline as every other single-record get in this codebase.
    """
    if not ObjectId.is_valid(call_id):
        return None
    doc = await db[CALLS].find_one({"_id": ObjectId(call_id), "platform_id": platform_id})
    return _from_doc(doc) if doc else None


async def list_by_platform_id(
    db: MongoDB, *, platform_id: str, limit: int, offset: int
) -> tuple[list[CallInDB], int]:
    """Tenancy-scoped paginated list — `GET /calls`'s only real query.
    Same `limit`/`offset` + `total_count`, newest-first, no-filters-for-now
    convention as `agent_repo.list_by_platform_id` (see that function's own
    docstring for the full pagination-pattern reasoning shared by both).

    Deliberately no filters (`agent_id`/`status`/`direction`) in this first
    pass, even though `CallPublic` has fields that would support them —
    per the task's own explicit "lean toward the simpler version unless a
    filter is trivial to add" guidance. A plain paginated newest-first list
    is enough to close the real, documented gap ("Browsing call history
    today means knowing every individual call id in advance" — see
    vendor-docs/Phase1-Status-Report.html's Tier 1 table); add filters when
    a concrete caller need for narrowing shows up, same "don't build for
    hypothetical futures" bias this codebase applies elsewhere.

    A platform's own call history is exactly the kind of collection the
    standards doc's "No unbounded queries" rule anticipates growing large
    over time (unlike a single agent's bound phone numbers) — so, same as
    agent_repo.list_by_platform_id, this is a real indexed `.skip()`/
    `.limit()` Mongo query plus `count_documents`, never `.to_list(length=
    None)`. `platform_id` is already indexed (see app/database.py).
    """
    cursor = (
        db[CALLS]
        .find({"platform_id": platform_id})
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    total_count = await db[CALLS].count_documents({"platform_id": platform_id})
    return [_from_doc(doc) for doc in docs], total_count


async def get_by_vendor_ref(db: MongoDB, vendor_ref: str) -> CallInDB | None:
    """Lookup by the voice vendor's own call id, with NO platform_id
    filter — deliberately different from get_by_id above, and only ever used
    by the post-call webhook handler (app/routers/webhooks.py).

    That handler receives a webhook carrying only the vendor's own call_id —
    it doesn't know which platform's Calls document this correlates to until
    this lookup resolves it, the exact same "tenancy gets resolved BY this
    lookup, so it cannot itself be tenancy-scoped" reasoning already
    documented on phone_number_repo.get_by_phone_number_any_platform() for
    the inbound-call webhook. Safe for the same reason: the caller here is
    the voice vendor's own HMAC-signature-verified webhook, not an arbitrary
    HTTP caller, and this returns at most the one Calls document that
    genuinely has this vendor_ref (set only by our own POST /calls/outbound
    at creation time), never platform-scoped data belonging to someone else
    beyond that single already-correlated record.
    """
    doc = await db[CALLS].find_one({"vendor_ref": vendor_ref})
    return _from_doc(doc) if doc else None


async def update_post_call_outcome(
    db: MongoDB,
    call_id: str,
    *,
    status: CallStatus,
    recording_url: str | None,
    transcript_url: str | None,
    summary: str | None,
    sentiment: str | None,
    extracted_data: dict[str, Any] | None,
    recording_rehost_failed: bool,
    in_voicemail: bool | None = None,
    disconnection_reason: str | None = None,
) -> None:
    """Write the post-call webhook's outcome onto an existing Calls document,
    identified by OUR OWN Mongo `_id` (already resolved via get_by_vendor_ref
    above before this is ever called).

    Idempotent by construction: a Retell retry/re-delivery of the same
    finished-call event calls this again with the same (or a strictly more
    complete, e.g. call_ended then call_analyzed) set of values — a plain
    `$set` overwrite, no separate "already processed" bookkeeping needed,
    same reasoning as the S3 upload's own idempotent-overwrite design (see
    storage.py's module docstring) and confirmed as the real, working
    pattern in eCareVoiceAI's own `_process_post_call_payload` ("Idempotent
    on retell_call_id — re-deliveries just overwrite").

    `summary`/`sentiment`/`extracted_data`/`in_voicemail`/
    `disconnection_reason` are only passed non-None by the caller once the
    corresponding event has actually supplied them (see
    app/routers/webhooks.py) — passing None here would silently blank out
    values a prior event already set if an earlier, less-complete event were
    re-delivered after a later, more-complete one, so the caller is
    responsible for only including fields it actually has fresh data for on
    each specific event. `in_voicemail` needs the same explicit
    `is not None` guard as the others despite being a bool — a real `False`
    (confirmed human answered) must still be written, never treated as
    falsy-and-skipped. This function always sets `updated_at`, per the
    standards doc's baseline-bookkeeping-fields rule (every mutation bumps
    it, no exceptions).
    """
    if not ObjectId.is_valid(call_id):
        return
    update: dict[str, Any] = {
        "status": status.value,
        "recording_rehost_failed": recording_rehost_failed,
        "updated_at": datetime.now(UTC),
    }
    if recording_url is not None:
        update["recording_url"] = recording_url
    if transcript_url is not None:
        update["transcript_url"] = transcript_url
    if summary is not None:
        update["summary"] = summary
    if sentiment is not None:
        update["sentiment"] = sentiment
    if extracted_data is not None:
        update["extracted_data"] = extracted_data
    if in_voicemail is not None:
        update["in_voicemail"] = in_voicemail
    if disconnection_reason is not None:
        update["disconnection_reason"] = disconnection_reason
    await db[CALLS].update_one({"_id": ObjectId(call_id)}, {"$set": update})
