"""In-process registry routing "this call_id just got a live transcript
update" (from `POST /webhooks/retell/transcript-updated`, see
app/routers/webhooks.py) to "which WebSocket connections are currently
listening for this call_id" (from `WS /calls/{call_id}/live-transcript`, see
app/routers/calls.py).

**Architecturally new territory for this codebase, called out explicitly.**
Every other feature in this codebase is request/response: one HTTP call in,
one HTTP response out, done — including every existing vendor webhook (a
POST arrives, we do some work, we return 200). A WebSocket connection is
long-lived and PUSH-based: Platform X opens it once and then receives zero or
more messages over time, pushed from OUR side whenever something relevant
happens, with no request from them driving each individual message. This
registry is the piece that makes that push possible: it is the one place in
the whole app that remembers "who is currently listening," so that an
unrelated, later HTTP request (the vendor's webhook delivery) can reach
across to a completely different, already-open connection and push data into
it.

**Design choice: a plain in-process dict, deliberately, matching this
codebase's own established precedent for "the simplest correct choice for a
single-process FastAPI app with no existing task-queue/worker/pub-sub
infrastructure"** — the exact same reasoning already used to justify
`BackgroundTasks` over a real task queue for the post-call re-hosting work
(see app/routers/webhooks.py's module docstring, "Latency design"). No Redis
pub/sub, no message broker, no external state store — just a
`dict[call_id, set[WebSocket]]` guarded by an `asyncio.Lock`, held in this
module's own process memory.

**Real, known limitations of this choice — documented here explicitly,
exactly as this codebase's own standards doc requires ("known limitation,
not fixed here, deliberately" — see e.g. GET /calls/{id}'s inbound-call
caveat, or CreateAgentRequest.transfer_show_original_caller_id's known
telephony-path gap), NOT an oversight:**

1. **Does not survive a process restart.** Every registered WebSocket
   connection lives only in this process's own memory — a deploy, a crash, or
   any restart of the VoiceAI process drops every currently-connected
   Platform X client. There is no reconnect-and-replay: a client that
   reconnects after a restart starts receiving updates again from whatever
   point it reconnects, with no way to retrieve whatever was pushed while it
   was disconnected (see this module's "relay-only, no persistence" note
   below for why that's also true even absent a restart).
2. **Does not work across multiple server processes/instances.** If this app
   is ever horizontally scaled (multiple uvicorn/gunicorn workers, multiple
   container replicas behind a load balancer), a `transcript_updated` webhook
   delivery landing on worker A has NO WAY to reach a WebSocket client
   connected to worker B — this registry is process-local, not shared. A
   real multi-process deployment of this specific feature would need a real
   pub/sub layer (Redis pub/sub, a message broker, or similar) so every
   worker can learn about every call's updates regardless of which worker the
   webhook happened to land on, and every worker can push to whichever
   worker(s) hold the relevant WebSocket connections. That is real,
   meaningful infrastructure this task deliberately does NOT build — this
   registry is the correct, minimal building block for TODAY's single-process
   deployment, not a scaled one. A future maintainer scaling this app
   horizontally must revisit this module before doing so, or live transcript
   relay will silently only work for whichever fraction of calls happen to
   have their webhook and their WebSocket land on the same worker.
3. **No delivery guarantee, by design, not by omission.** If nobody is
   connected when a `transcript_updated` delivery arrives, that update is
   simply dropped — there is no queue, no buffering, no "deliver when they
   reconnect." This is a deliberate, documented product decision (see this
   module's "relay-only, no persistence" note and app/routers/webhooks.py's
   transcript-updated handler): the FULL, complete transcript remains
   available after the fact via the existing `GET /calls/{id}/transcript`
   (populated once the call ends, via the unchanged post-call re-hosting
   flow) — this registry only ever serves the "watch it happen live" use
   case, which is inherently best-effort by nature (a viewer who wasn't
   watching didn't miss anything that isn't ALSO available afterward through
   the durable path).

**Relay-only, no persistence here — a deliberate decision, not the obvious
default a future maintainer might reflexively reach for.** This registry
holds ONLY live WebSocket connections, never any transcript content itself —
it is not a buffer, not a cache, not a partial-transcript store. Every
`transcript_updated` delivery is pushed to whoever is CURRENTLY connected and
then forgotten by this module entirely. A future maintainer might reasonably
ask "shouldn't we persist each incremental delta somewhere, so a client that
connects late can catch up?" — see app/routers/webhooks.py's
`handle_transcript_updated` docstring for the full reasoning on why that is
the wrong default here: real write amplification (Retell fires this once per
conversational turn, for every in-progress call, all day) for a feature
whose entire value proposition is "live," not "archived" — the durable
record already exists via the unchanged post-call transcript re-hosting
path.

**Concurrency**: `asyncio.Lock` guards mutation of the registry's dict/sets
(register/unregister/lookup-and-snapshot) — a single-process app still has
many concurrent asyncio tasks (one per in-flight request/connection), so two
WebSocket connections for the same call_id opening/closing concurrently with
an incoming webhook relay must not corrupt the shared dict. Reads for the
actual push (iterating the snapshotted set of connections) happen OUTSIDE the
lock, after a quick snapshot copy — holding the lock across the actual
`send_json` calls would serialize unrelated calls' pushes behind each other
and would deadlock-adjacent-risk if a `send_json` ever blocked on a slow/dead
client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("app.live_transcript_registry")

_lock = asyncio.Lock()
_connections: dict[str, set[WebSocket]] = {}


async def register(call_id: str, websocket: WebSocket) -> None:
    """Record `websocket` as listening for live transcript updates on
    `call_id`. Called once, right after the WebSocket handshake completes and
    tenancy has been verified (see app/routers/calls.py's live-transcript
    endpoint) — never before tenancy is confirmed, so an unauthorized
    connection is never registered at all.
    """
    async with _lock:
        _connections.setdefault(call_id, set()).add(websocket)


async def unregister(call_id: str, websocket: WebSocket) -> None:
    """Remove `websocket` from `call_id`'s listener set — called from a
    `finally` block covering the WebSocket connection's whole lifetime (see
    app/routers/calls.py), so this runs regardless of whether the connection
    ended via a clean client-initiated close, a network drop, or an
    exception. Cleans up the now-empty set entry for `call_id` too, so this
    dict never accumulates empty-set entries for calls that finished long
    ago — a real, if small, memory-leak risk over a long-running process
    otherwise.
    """
    async with _lock:
        listeners = _connections.get(call_id)
        if listeners is None:
            return
        listeners.discard(websocket)
        if not listeners:
            del _connections[call_id]


async def relay(call_id: str, message: dict[str, Any]) -> int:
    """Push `message` (already-JSON-serializable) to every WebSocket
    currently registered for `call_id`. Returns how many connections it was
    actually sent to (0 is a normal, expected outcome — see this module's
    docstring, "no delivery guarantee" — not an error).

    A dead/slow individual connection's send failure is caught and logged,
    never allowed to raise out of this function — one broken WebSocket must
    never prevent the SAME update from reaching every other still-healthy
    listener for the same call_id (e.g. two Platform X tabs watching the same
    call), and must never propagate back to crash the webhook handler that
    called this (see app/routers/webhooks.py's transcript-updated handler,
    "graceful no-op" requirement). A send failure does NOT immediately
    unregister the dead connection here — the WebSocket endpoint's own
    receive loop (app/routers/calls.py) is what detects disconnects and
    calls `unregister` from its `finally` block; this function's job is only
    "try to deliver now," not connection lifecycle management.
    """
    async with _lock:
        listeners = set(_connections.get(call_id, ()))

    delivered = 0
    for websocket in listeners:
        try:
            await websocket.send_json(message)
            delivered += 1
        except Exception:
            logger.warning(
                "Failed to relay live transcript update to a connected client",
                extra={"call_id": call_id},
                exc_info=True,
            )
    return delivered


def connection_count(call_id: str) -> int:
    """How many WebSocket clients are currently registered for `call_id` —
    used only by tests today (asserting a connection was registered/cleaned
    up), not by any request-handling code path.
    """
    return len(_connections.get(call_id, ()))
