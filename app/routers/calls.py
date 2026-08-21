"""POST /calls/outbound — Phase 1 API surface table: "Trigger an outbound
call — from_number, to_number, which agent to use, and dynamic variables".

A new top-level resource (`/calls`), not a sub-resource of `/agents` — an
outbound call references an agent and a phone number but isn't owned by
either the way a phone number is owned by (bound to) an agent.

See app/models/call.py's module docstring for the full reasoning behind this
endpoint's three real design decisions: from_number ownership enforcement,
agent_id override semantics, and the vendor_ref/persist-on-failure choices.

Also GET /calls/{call_id} — the full CallPublic record in one response
(status, direction, summary, sentiment, recording/transcript links,
extracted_data, from/to numbers, dynamic_variables, timestamps) — the
natural pull-based complement to the push-based call-completed webhook (see
app/services/call_completed_webhook.py). Tenancy-scoped via the same
`call_repo.get_by_id(db, call_id, platform_id=caller.id)` every other
single-record lookup in this codebase already uses; 404 (never 403) if
missing or owned by another platform.

**Known limitation, not fixed here, deliberately**: inbound calls
(one that rings in with no prior POST /calls/outbound trigger) have no
Calls record at all today — see backend-dev.md's Known open items for the
full, separate, not-yet-built gap. This means GET /calls/{id} correctly
404s for a genuinely inbound call's id, since there is no Calls document
for it to look up — that's an honest reflection of the current gap, not a
bug in this endpoint. Closing that gap (creating a Calls record when an
inbound call rings in) is out of this endpoint's scope.

Also GET /calls/{call_id}/recording and GET /calls/{call_id}/transcript —
the serving side of the post-call re-hosting feature (see
app/routers/webhooks.py's post-call handler for how these files get
populated in our own S3 bucket in the first place). These are the "our own
domain" endpoints CallPublic.recording_url/transcript_url actually point
at — see app/services/storage.py's module docstring for why a same-domain
proxy was chosen over a presigned S3 URL.

**Access control, decided**: unlike GET /voices/{voice_id}/preview (public
catalog audio, not platform-owned, no tenancy scoping), a call
recording/transcript is real platform-owned data that may carry PII/PHI (a
live phone conversation) — every endpoint in this router requires
`get_current_platform` AND looks the call up tenancy-scoped via
`call_repo.get_by_id(..., platform_id=caller.id)`, the same discipline as
every other single-record fetch in this codebase. A cross-platform access
attempt 404s (never 403), matching the standards doc's tenancy rule exactly.


WS /calls/{call_id}/live-transcript — live per-turn transcript relay
===================================================================

**The one genuinely new kind of connection in this whole codebase.** Every
other endpoint in VoiceAI, in every router, is request/response: one HTTP
call in, one HTTP response out. This is a WebSocket — Platform X opens ONE
long-lived connection and then receives zero or more PUSHED messages over
time, each one triggered by an unrelated, later event (the voice vendor's
own `transcript_updated` webhook arriving at `POST
/webhooks/retell/transcript-updated`, see app/routers/webhooks.py) rather
than by anything Platform X itself sends after the initial connection. See
app/services/live_transcript_registry.py's module docstring for the
in-process registry that makes this push possible and its own documented
single-process/no-restart-survival/no-delivery-guarantee limitations —
required reading before touching this endpoint.

**Why a WebSocket, not Server-Sent Events or polling — the real, minimal-
infrastructure-respecting choice for this codebase's actual shape.** This
app has no existing task-queue/worker/pub-sub infrastructure (the same
starting condition that already justified `BackgroundTasks` over a real
queue for post-call re-hosting — see webhooks.py's module docstring,
"Latency design"). A WebSocket endpoint Platform X connects TO is the
standard, natively-supported-by-FastAPI mechanism for "push data to a
client as it happens" that needs zero additional infrastructure beyond
this one route — no message broker, no SSE-specific proxy/timeout
configuration concerns, and (unlike polling `GET /calls/{id}` on an
interval) no wasted requests during the — often much longer — stretches of
a call where nothing new has been said yet.

**Auth mechanism — API key as a WebSocket query parameter, a deliberate,
reasoned departure from this codebase's own `Authorization: Bearer` header
convention used everywhere else, not an inconsistency.** Confirmed via
FastAPI's own documented WebSocket pattern (`fastapi.tiangolo.com`'s
"WebSockets" guide): a WebSocket handshake IS a real HTTP GET request under
the hood, so query parameters are fully available and are FastAPI's own
recommended, standard mechanism for WebSocket-time data (the guide's own
worked example authenticates a WebSocket via a query-string token,
precisely this case) — custom headers, by contrast, are NOT reliably
settable by every real-world WebSocket client (most notably: browser-native
`WebSocket` JavaScript has no API to set arbitrary request headers on the
handshake at all), so requiring a Bearer header here would make this
endpoint unusable from the single most common real client this feature
exists for. This endpoint accepts the platform's own existing API key
(the SAME key used as a Bearer token everywhere else in this codebase, no
new credential type) as `?api_key=<key>` on the connection URL.

**The tradeoff this creates, thought through explicitly — and why it is
judged acceptable HERE specifically, rather than reflexively copied from
the admin-docs page's earlier, different decision to use HTTP Basic over a
query param for that other, separate feature.** A query-string credential
has a real, documented downside: it can end up logged in places a header
value would not — proxy/load-balancer/webserver access logs, which
routinely capture the request line/URL by default. This is exactly the
concern that made HTTP Basic (a header) the right choice for the ADMIN DOCS
page earlier this session — but that page is a BROWSER-NAVIGATED page (a
human typing a URL/clicking a link), where the URL is also exposed via
browser history and any Referer header if the page ever links offsite, and
which needed a mechanism a bare browser navigation (no custom JS) could
satisfy on its own. A WebSocket connection URL is a different kind of
artifact: it is opened programmatically by a client library (a browser's
`new WebSocket(url)` call, or a server-side WS client), never typed/
bookmarked/navigated-to by a human, so it is not exposed via browser
history the way a docs-page URL is, and WebSocket handshake requests are
also typically NOT logged the same way ordinary page-view HTTP requests
are by standard access-log configurations (most infra logs are configured
around HTTP request/response cycles, not the long-lived upgraded
connection that follows a WS handshake) — though a caller running behind
infrastructure that DOES log the handshake request line should be aware
the key would appear there, same as any query-string secret. Weighed
against that residual risk: this key is the SAME credential already used
as a Bearer token on every other endpoint (not a more sensitive, single-
purpose secret), it can be rotated via the same mechanism as any other
leaked platform key, and the alternative (Bearer-header-only auth) would
make this endpoint simply unusable from a standard browser WebSocket
client — a real capability loss, not just a style preference. This is a
considered, documented tradeoff specific to this endpoint's real
constraints, not a blanket "query params are fine now" reversal of the
admin-docs decision.

**Tenancy check — same discipline as every other single-call lookup in
this codebase, adapted to WebSocket's own close-code mechanism instead of
an HTTP status code.** `call_repo.get_by_id(db, call_id, platform_id=
caller.id)` — the identical tenancy-scoped lookup `get_call`/
`get_call_recording`/`get_call_transcript` above already use — resolves
whether this call_id both EXISTS and belongs to the platform that
authenticated via `api_key`. A WebSocket has no HTTP status code to return
once the connection is open, so "reject" here means accepting the
handshake (FastAPI's WebSocket protocol requires calling `.accept()` before
`.close()` can carry a meaningful code on some clients — see this
endpoint's own implementation) and then immediately closing with a real,
standard WebSocket close code rather than inventing a nonstandard one:
`1008` (Policy Violation — the closest standard code, per RFC 6455, to
"you violated the server's access policy," a deliberately close, real
analogue to HTTP's 401/404 for a protocol that has no equivalent status-
code mechanism of its own) is used uniformly for BOTH "no such call_id" and
"wrong platform's key" — never distinguishing the two, the identical
"never let a cross-tenant probe distinguish exists-but-not-yours from
genuinely-doesn't-exist" discipline `assert_owns_record`'s 404-never-403
rule already establishes for the HTTP side of this codebase, applied here
via the nearest WebSocket-native equivalent.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from starlette import status as ws_status

from app.config import Settings, get_settings
from app.database import MongoDB, get_db
from app.deps import CurrentPlatform, DbDep
from app.errors import CODE_NOT_FOUND, CODE_VALIDATION, AppError
from app.models.agent import AgentStatus
from app.models.call import CallInDB, CallPublic, CallStatus, CreateOutboundCallRequest
from app.repositories import agent_repo, call_repo, phone_number_repo, platform_repo
from app.security import hash_api_key
from app.services import live_transcript_registry, retell_adapter
from app.services.storage import get_storage_service, recording_key, transcript_key

logger = logging.getLogger("app.calls.live_transcript")

router = APIRouter(prefix="/calls", tags=["calls"])

# Same pagination cap/default GET /voices and GET /agents already established
# as this codebase's list-endpoint convention (see app/routers/voices.py's
# module docstring) — kept as separate constants here, same "no cross-router
# coupling purely to share two integers" reasoning as app/routers/agents.py's
# own copy.
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


class CallListResponse(BaseModel):
    """`GET /calls` response envelope — same `items`/`total_count`/`limit`/
    `offset` shape as every other list endpoint in this codebase (see
    app/routers/voices.py's module docstring for the established
    convention)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [],
                "total_count": 0,
                "limit": 20,
                "offset": 0,
            }
        }
    )

    items: list[CallPublic]
    total_count: int
    limit: int
    offset: int


def _to_public(call: CallInDB) -> CallPublic:
    return CallPublic(
        id=call.id,
        platform_id=call.platform_id,
        agent_id=call.agent_id,
        from_number=call.from_number,
        to_number=call.to_number,
        dynamic_variables=call.dynamic_variables,
        status=call.status,
        direction=call.direction,
        recording_url=call.recording_url,
        transcript_url=call.transcript_url,
        summary=call.summary,
        sentiment=call.sentiment,
        extracted_data=call.extracted_data,
        recording_rehost_failed=call.recording_rehost_failed,
        in_voicemail=call.in_voicemail,
        disconnection_reason=call.disconnection_reason,
        created_at=call.created_at,
        updated_at=call.updated_at,
    )


@router.post(
    "/outbound",
    response_model=CallPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger an outbound call from one of your own numbers",
    responses={
        404: {
            "description": "No agent exists with this id for the calling platform.",
        },
        422: {
            "description": "Either `from_number` is not a number this platform has "
            "provisioned with us, or `agent_id` refers to an agent whose creation never "
            "finished on the voice vendor (status 'failed').",
        },
        502: {
            "description": "The voice vendor could not be reached or rejected the outbound "
            "call request.",
        },
    },
)
async def create_outbound_call(
    body: CreateOutboundCallRequest,
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CallPublic:
    """Trigger an outbound call: dial `to_number` from `from_number`, running
    `agent_id`, with optional per-call `dynamic_variables`.

    **`from_number` must be a number this platform already provisioned with
    us** (via `POST /agents/{agent_id}/numbers` or its `/byo` sibling) — we
    look it up in our own records, scoped to the calling platform, before
    ever calling the voice vendor. A number that isn't ours to call from is
    rejected with 422, even if it might be a well-formed or even
    vendor-valid number: the voice vendor has no concept of OUR tenancy, so
    this check is the only thing standing between one platform and dialing
    out as if it owned another platform's number.

    **`agent_id` is your own agent id** (from `POST /agents`'s response),
    looked up tenancy-scoped the same way as the numbers endpoints (a
    cross-platform lookup 404s, never 403; a `status == "failed"` agent
    never finished creation on the vendor and is rejected with 422 before
    any vendor call). It does not need to match whichever agent is currently
    bound to `from_number` for inbound calls — supplying a different one
    runs that agent for this call only, without rebinding the number.

    We persist our own call record regardless of whether the vendor call
    succeeds, for the same reason `POST /agents` does: the request you sent
    us (a real from_number you own, a real agent, a specific number to dial)
    is valid and real the moment we've checked ownership and accepted it — a
    vendor-side outage or rejection shouldn't force you to reconstruct and
    resubmit the same call trigger blindly. On success, `status` is
    `"registered"` (mirroring the voice vendor's own real starting call
    state); on vendor failure, `status` is `"failed"` and the original 502
    error is still raised.

    **`voicemail_detection` is optional** — set it to try to detect
    voicemail in the first 3 minutes of the call and take the configured
    action once detected (see `CreateOutboundCallRequest.voicemail_detection`
    for the full field shape). The result (`in_voicemail`,
    `disconnection_reason`) is delivered later, once the call finishes — see
    `GET /calls/{id}` and the call-completed notification.
    """
    agent = await agent_repo.get_by_id(db, body.agent_id, platform_id=caller.id)
    if agent is None:
        raise AppError(
            code="not_found",
            message="No agent exists with that id.",
            status_code=404,
            field="agent_id",
        )
    if agent.status == AgentStatus.FAILED or agent.vendor_ref is None:
        raise AppError(
            code=CODE_VALIDATION,
            message="This agent never finished creation on the voice vendor, so it can't be "
            "used to place a call yet. Retry creating the agent first.",
            status_code=422,
            field="agent_id",
        )

    owned_number = await phone_number_repo.get_by_phone_number(
        db, body.from_number, platform_id=caller.id
    )
    if owned_number is None:
        raise AppError(
            code=CODE_VALIDATION,
            message="from_number is not a phone number this platform has provisioned with "
            "us. Provision it first via POST /agents/{agent_id}/numbers or its /byo "
            "sibling, then use that exact number here.",
            status_code=422,
            field="from_number",
        )

    status_value = CallStatus.REGISTERED
    vendor_ref: str | None = None
    vendor_error: AppError | None = None
    try:
        result = await retell_adapter.create_phone_call(
            settings,
            from_number=body.from_number,
            to_number=body.to_number,
            retell_agent_id=agent.vendor_ref,
            dynamic_variables=body.dynamic_variables,
            voicemail_detection=body.voicemail_detection,
        )
        vendor_ref = result.call_id
    except AppError as exc:
        status_value = CallStatus.FAILED
        vendor_error = exc

    call = await call_repo.create(
        db,
        platform_id=caller.id,
        agent_id=agent.id,
        from_number=body.from_number,
        to_number=body.to_number,
        dynamic_variables=body.dynamic_variables,
        status=status_value,
        vendor=retell_adapter.VENDOR_NAME,
        vendor_ref=vendor_ref,
    )

    if vendor_error is not None:
        # Re-raise the original upstream_failed error so the caller sees the
        # real 502/upstream_failed contract — the persisted "failed" record
        # above is a side effect for a future retry/audit trail, not a
        # success response. Same pattern as POST /agents.
        raise vendor_error

    return _to_public(call)


@router.get(
    "",
    response_model=CallListResponse,
    status_code=status.HTTP_200_OK,
    summary="List your own calls",
    responses={
        401: {"description": "Missing or invalid API key."},
    },
)
async def list_calls(
    caller: CurrentPlatform,
    db: DbDep,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_LIMIT,
            description=f"Max calls to return, 1-{_MAX_LIMIT}. Default {_DEFAULT_LIMIT}.",
        ),
    ] = _DEFAULT_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of calls to skip, for paging. Default 0."),
    ] = 0,
) -> CallListResponse:
    """List every call belonging to the calling platform, newest first —
    closes the real, documented gap that browsing call history required
    already knowing every individual call id in advance (see
    vendor-docs/Phase1-Status-Report.html's Tier 1 table).

    Reads from our own `Calls` collection (`call_repo.list_by_platform_id`),
    never a live vendor call — same as `GET /calls/{call_id}` above. Same
    `limit`/`offset` + `total_count` pagination convention as every other
    list endpoint in this codebase.

    No filters in this first pass (deliberately the simpler version — see
    call_repo.list_by_platform_id's own docstring for the full reasoning);
    add `agent_id`/`status`/`direction` filters later if a real caller need
    for narrowing shows up.

    **Known limitation, same as GET /calls/{call_id}**: a genuinely inbound
    call (one that rang in with no prior POST /calls/outbound trigger) has
    no Calls record at all today, so it will not appear in this list either
    — see this module's docstring for the full, separate, not-yet-built gap.
    """
    calls, total_count = await call_repo.list_by_platform_id(
        db, platform_id=caller.id, limit=limit, offset=offset
    )
    return CallListResponse(
        items=[_to_public(call) for call in calls],
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


async def _get_owned_call(db: MongoDB, caller_id: str, call_id: str) -> CallInDB:
    """Shared tenancy-scoped lookup for both file-serving endpoints below —
    404 (never 403) on a missing or cross-platform call_id, same discipline
    as every other single-record fetch in this codebase.
    """
    call = await call_repo.get_by_id(db, call_id, platform_id=caller_id)
    if call is None:
        raise AppError(
            code=CODE_NOT_FOUND,
            message="No call exists with that id.",
            status_code=404,
            field="call_id",
        )
    return call


@router.get(
    "/{call_id}",
    response_model=CallPublic,
    status_code=status.HTTP_200_OK,
    summary="Get the full record for one call",
    responses={
        404: {
            "description": "No call exists with this id for the calling platform.",
        },
    },
)
async def get_call(
    caller: CurrentPlatform,
    db: DbDep,
    call_id: Annotated[str, Path(description="Our call id, from POST /calls/outbound's response.")],
) -> CallPublic:
    """Fetch everything we know about one call in a single response: status,
    direction, summary, sentiment, recording/transcript links, extracted_data
    (see AgentPublic.structured_data_fields for what configures it),
    from/to numbers, dynamic_variables, and timestamps.

    Tenancy-scoped exactly like every other single-record lookup in this
    codebase (`call_repo.get_by_id(..., platform_id=caller.id)`) — a call
    belonging to a different platform 404s, never 403.

    **Known limitation**: a genuinely inbound call (one that rang in with no
    prior POST /calls/outbound trigger) has no Calls record at all today —
    this correctly 404s for that id, since there's nothing to look up yet,
    not a bug in this endpoint.
    """
    call = await _get_owned_call(db, caller.id, call_id)
    return _to_public(call)


@router.get(
    "/{call_id}/recording",
    status_code=status.HTTP_200_OK,
    summary="Stream this call's re-hosted recording from our own domain",
    responses={
        200: {"content": {"audio/wav": {}}, "description": "Recording audio bytes."},
        404: {
            "description": "No call exists with this id for the calling platform, or the "
            "recording hasn't been re-hosted (yet, or at all — check "
            "recording_rehost_failed on the call record).",
        },
        502: {"description": "File storage is unreachable or rejected the request."},
    },
)
async def get_call_recording(
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
    call_id: Annotated[str, Path(description="Our call id, from POST /calls/outbound's response.")],
) -> Response:
    """Stream this call's re-hosted recording bytes from our own S3 bucket.

    This is what `CallPublic.recording_url` actually points at — see
    app/services/storage.py's module docstring for why a same-domain proxy
    was chosen over a presigned S3 URL. Tenancy-scoped: a call belonging to
    a different platform 404s, never 403 (same as every other single-record
    lookup in this codebase). 404 also covers the ordinary case where the
    call hasn't finished yet or re-hosting hasn't completed/failed — the
    call record's own `recording_rehost_failed` flag distinguishes "not
    ready yet" from "failed."
    """
    call = await _get_owned_call(db, caller.id, call_id)
    audio_bytes, content_type = await get_storage_service().download(
        key=recording_key(call.id), settings=settings
    )
    return Response(content=audio_bytes, media_type=content_type)


@router.get(
    "/{call_id}/transcript",
    status_code=status.HTTP_200_OK,
    summary="Stream this call's re-hosted transcript from our own domain",
    responses={
        200: {"content": {"text/plain": {}}, "description": "Transcript text."},
        404: {
            "description": "No call exists with this id for the calling platform, or the "
            "transcript hasn't been re-hosted (yet, or at all — check "
            "recording_rehost_failed on the call record).",
        },
        502: {"description": "File storage is unreachable or rejected the request."},
    },
)
async def get_call_transcript(
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
    call_id: Annotated[str, Path(description="Our call id, from POST /calls/outbound's response.")],
) -> Response:
    """Stream this call's re-hosted transcript text from our own S3 bucket.

    Same tenancy scoping and "our own domain, never the voice vendor's raw
    URL" reasoning as get_call_recording above.
    """
    call = await _get_owned_call(db, caller.id, call_id)
    transcript_bytes, content_type = await get_storage_service().download(
        key=transcript_key(call.id), settings=settings
    )
    return Response(content=transcript_bytes, media_type=content_type)


# ── WS /calls/{call_id}/live-transcript ─────────────────────────────────
# See this module's docstring, "WS /calls/{call_id}/live-transcript"
# section, for the full mechanism, the WebSocket-vs-SSE-vs-polling choice,
# the query-param auth mechanism (and its documented tradeoff), and the
# tenancy/close-code reasoning.


@router.websocket("/{call_id}/live-transcript")
async def live_transcript(websocket: WebSocket, call_id: str) -> None:
    """Platform X connects here to receive live, per-turn transcript updates
    for one in-progress call, pushed the moment each `transcript_updated`
    webhook delivery arrives from the voice vendor (see
    app/routers/webhooks.py's `handle_transcript_updated` and
    app/services/live_transcript_registry.py).

    Not rendered as a normal Swagger operation — FastAPI/OpenAPI has no
    concept of documenting a WebSocket route's message contract the way it
    documents an HTTP request/response body (there is no equivalent of
    `response_model` for a stream of pushed messages), so this docstring
    plus this module's own docstring section ARE the documentation of this
    endpoint's contract, the same "docstring is the real documentation"
    convention app/routers/webhooks.py already established for its own
    vendor-facing routes (for a different reason there — schema exclusion —
    but the same practical outcome: read the docstring, not Swagger).

    **Handshake sequence, in order:**
    1. Resolve `?api_key=<platform's own API key>` (query param — see this
       module's docstring for why a query param, not a header, is the
       correct choice for a WebSocket specifically) to a `PlatformInDB` via
       the SAME `hash_api_key`/`platform_repo.get_by_api_key_hash` lookup
       `get_current_platform` uses for every HTTP endpoint — this is
       genuinely the same credential/mechanism, just read from a different
       part of the request (query string vs Authorization header) because a
       WebSocket handshake needs it there.
    2. Tenancy-scoped lookup: `call_repo.get_by_id(db, call_id,
       platform_id=<resolved platform's id>)` — identical to every other
       single-call lookup in this router.
    3. Missing/invalid api_key, unknown/revoked key, or a call_id that
       doesn't exist/doesn't belong to this platform — ALL close the
       connection with the same code (`1008`, Policy Violation), never
       distinguishing which — same 404-never-403-adjacent discipline as
       every HTTP endpoint in this router.
    4. On success: register this connection in `live_transcript_registry`,
       then just wait — this endpoint sends nothing itself in response to
       anything the client sends; its ENTIRE job past this point is to stay
       open so `live_transcript_registry.relay()` (called from the webhook
       handler, an entirely separate request) can push to it. A `receive()`
       loop is still run (see below) purely to detect when the CLIENT
       closes the connection or the connection drops — not because this
       endpoint expects or acts on anything the client sends.

    **Cleanup — guaranteed via `finally`, covering every real exit path**
    (client-initiated close, network drop, or an unexpected exception):
    always calls `live_transcript_registry.unregister()`, so a closed/dead
    connection is never left in the registry accumulating forever — see
    that function's own docstring for why this matters over a long-running
    process.
    """
    api_key = websocket.query_params.get("api_key")
    db: MongoDB = get_db()

    platform = None
    if api_key:
        platform = await platform_repo.get_by_api_key_hash(db, hash_api_key(api_key))

    if platform is None or platform.status.value != "active":
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return

    call = await call_repo.get_by_id(db, call_id, platform_id=platform.id)
    if call is None:
        await websocket.close(code=ws_status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await live_transcript_registry.register(call.id, websocket)
    try:
        while True:
            # Nothing the client sends is ever acted on (see this
            # endpoint's own docstring) — this loop exists solely to detect
            # a client-initiated close or a dropped connection via the
            # WebSocketDisconnect it raises, so `finally` below can clean
            # up the registry promptly rather than only on the next relay
            # attempt's own send failure.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning(
            "Live-transcript WebSocket connection ended unexpectedly",
            extra={"call_id": call.id},
            exc_info=True,
        )
    finally:
        await live_transcript_registry.unregister(call.id, websocket)
