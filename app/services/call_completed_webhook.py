"""Outbound "call completed" notification — the moment a call finishes and
its post-call data (summary, sentiment, re-hosted recording/transcript
links) is ready, automatically POST it to the URL Platform X registered with
us via `PATCH /platform`'s `call_completed_webhook_url`.

Per `vendor-docs/White-Label-Launch-Plan.html`'s "What we send Platform X,
unprompted" section — this was always the plan, and closes the single
highest-priority gap identified in a full Phase-1 status audit: the
recording/transcript/summary/sentiment are already correctly captured and
re-hosted (see app/routers/webhooks.py's post-call handler), but nothing
notified Platform X when that finished, so none of that captured data was
actually reaching them.

**Two proven patterns combined, per the task's explicit instruction, rather
than inventing a new scheme:**

1. **eCareVoiceAI's real, working pattern for "how a per-customer URL is
   stored and POSTed to"** — `push_lead_sync()` in eCareVoiceAI's
   `ecarelite_client.py`: `httpx.AsyncClient().post(url, json=payload,
   headers=headers)` where the endpoint URL comes from per-facility config
   in Mongo (not hardcoded), and every attempt is logged. This module
   follows the same shape: `call_completed_webhook_url` and
   `call_completed_webhook_secret` are read from the calling platform's own
   `Platforms` document (app/models/platform.py), never hardcoded or
   global.

2. **VoiceAI's own already-proven `platform_relay.py` pattern for "how an
   automatic, unattended outbound call to a third party is made
   reliably"** — a strict per-attempt `httpx.Timeout`, every failure mode
   (timeout, connection error, non-2xx, malformed response) caught and
   folded into a structured result, never a raised/uncaught exception, and
   full structured logging of the outcome.

**Why NOT eCareVoiceAI's retry story too**: eCareVoiceAI's `push_lead_sync`
is USER-INITIATED — a human clicks "Push to CRM" in their UI, so a failure
just means the human clicks again; there is no automatic retry built in
because none is needed. This module's trigger is the opposite: it fires
automatically, in a background task, with no human anywhere in the loop
(see app/routers/webhooks.py's post-call handler, which calls this after
S3 re-hosting finishes). If Platform X's server is briefly down/slow, there
is nobody to press retry — so THIS module adds real retry/backoff handling
that eCareVoiceAI's version genuinely doesn't need. See `deliver()`'s
docstring for the exact numbers and reasoning.

**Retry/backoff design — bounded synchronous loop, inside the same
background task, not a separate job queue.** Considered a real job-queue/
worker system (e.g. persisting a "pending notification" record and having a
separate process retry it on a schedule, potentially over minutes/hours) and
explicitly rejected FOR NOW: this project has no task-queue infrastructure
at all yet (the post-call webhook's own re-hosting step already established
FastAPI `BackgroundTasks` as the "simplest correct choice for a single-
process FastAPI app," see webhooks.py's module docstring) — building a
persistent retry-queue system for one notification type, before this
project has any other use for one, is exactly the "don't build for
hypothetical futures" trap the standards doc warns against. A bounded
synchronous retry loop (3 attempts, short backoff, all within the same
background task that already runs after the webhook response has been sent
to Retell) is simple, correct for the realistic failure mode (a brief
Platform X outage/slow deploy, not a multi-hour one), and honestly logs
"exhausted retries" as a real, visible, debuggable outcome rather than
silently losing the notification — not a permanent architecture decision,
just the right-sized one for where this project actually is today. If a
future need shows up for surviving a Platform X outage measured in hours
(not seconds), that's the trigger to revisit with a real persistent queue,
not a reason to build one now.

**Timeout value, reasoned separately from platform_relay.py's
`PLATFORM_RELAY_TIMEOUT_SECONDS` (4.0s), not copied blindly.** That value
exists because a live caller is on hold waiting for the voice vendor to
answer — it must leave real margin inside the vendor's ~10s ceiling. This
notification has no such caller-facing deadline at all: it fires from a
background task, well after the call already ended and after Retell has
already been acknowledged. `CALL_COMPLETED_WEBHOOK_TIMEOUT_SECONDS = 8.0` is
used instead — generous enough that a normal Platform X server (even one
doing real work synchronously per notification: writing to a DB, updating a
UI) isn't punished by a suspiciously tight deadline nothing requires, while
still bounded (never hangs a background task indefinitely) and small enough
that 3 attempts still complete in well under a minute even in the worst
case, keeping the background task's total runtime reasonable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from app.models.call import CallDirection, CallInDB
from app.utils.webhook_signing import sign_webhook_body

logger = logging.getLogger("app.call_completed_webhook")

# See this module's docstring, "Timeout value" section, for the full
# reasoning behind why this differs from platform_relay.py's stricter 4.0s.
CALL_COMPLETED_WEBHOOK_TIMEOUT_SECONDS = 8.0

# See this module's docstring, "Retry/backoff design" section. 3 total
# attempts (1 initial + 2 retries) with a short, fixed backoff between each
# — not exponential, deliberately: this is a bounded, short-lived retry
# inside one background task, not a long-running queue where exponential
# backoff earns its complexity by spreading load over minutes/hours. 2
# seconds is long enough to ride out a brief blip (a deploy in progress, a
# transient DNS hiccup) without making a failing background task run for an
# unreasonably long time (worst case: 3 attempts x 8s timeout + 2x2s backoff
# = ~28s, still well within what a background task can reasonably take).
CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS = 3
CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS = 2.0

_SIGNATURE_HEADER = "X-VoiceAI-Signature"


def _direction_for_payload(direction: CallDirection) -> str:
    return direction.value


def build_call_completed_payload(call: CallInDB) -> dict[str, Any]:
    """Build the vendor-neutral, "design from Platform X's side" payload —
    same principle already applied to `POST /calls/outbound`'s response and
    `CallPublic` (see app/models/call.py): our own call id, never Retell's;
    no vendor field names; recording/transcript links point at OUR OWN
    domain's existing `GET /calls/{id}/recording`/`/transcript` endpoints
    (already re-hosted, per the "re-host, don't pass through" rule), never
    Retell's raw URLs or our own S3 bucket.

    Deliberately excludes: `platform_id` (Platform X already knows who it
    is — this notification only ever goes to the platform that owns the
    call, so echoing their own id back adds nothing), `vendor`/`vendor_ref`
    (internal-only, never leaked past our own boundary, same rule as every
    other *Public model in this codebase), `dynamic_variables` (the
    per-call personalization Platform X itself supplied at trigger time —
    they already have it, no need to echo it back in a completion
    notification whose whole purpose is the NEW information: how the call
    went).

    `duration` is NOT included — checked CallInDB for a stored duration
    value and there isn't one (Retell's post-call webhook payload does
    carry per-call timing fields, but this codebase's post-call handler,
    app/routers/webhooks.py, does not currently parse or store one on the
    Calls record at all — see app/models/post_call.py). Per the "no
    unnecessary fields" rule, this payload doesn't invent a duration field
    that has no real backing data; a future task that adds duration
    capture to the post-call webhook can add it here too, once it exists.

    `extracted_data` IS included — the actual point of the structured-data-
    extraction feature (see app/models/agent.py's `structured_data_fields`
    docstring): Platform X learns the facts pulled from this call the
    moment it finishes, via this push notification, not only by separately
    polling `GET /calls/{id}`. `None` when the owning agent had no
    structured_data_fields configured, or analysis hasn't populated it yet —
    same honest-null convention as `summary`/`sentiment`/`recording_url`.

    `in_voicemail`/`disconnection_reason` ARE included, same reasoning as
    `extracted_data` above — the voicemail-detection RESULT (see
    `CreateOutboundCallRequest.voicemail_detection` in app/models/call.py for
    the request-side trigger) is exactly the kind of "how did the call go"
    fact this notification exists to push, not something Platform X should
    have to separately poll `GET /calls/{id}` to learn. Same honest-null
    convention: `None` until the call has actually ended/been analyzed.
    """
    return {
        "call_id": call.id,
        "agent_id": call.agent_id,
        "direction": _direction_for_payload(call.direction),
        "status": call.status.value,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "summary": call.summary,
        "sentiment": call.sentiment,
        "recording_url": call.recording_url,
        "transcript_url": call.transcript_url,
        "extracted_data": call.extracted_data,
        "in_voicemail": call.in_voicemail,
        "disconnection_reason": call.disconnection_reason,
    }


def sign_payload(*, raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256 over the raw JSON body, using the platform's own
    per-platform signing secret — the outgoing-direction half of the
    standards doc's "Webhook security — both directions" section, mirroring
    exactly how Retell signs webhooks to us (see app/routers/webhooks.py's
    `_verify_signature`), just with VoiceAI as the signer instead of the
    verifier. Platform X verifies this the same way we verify Retell's
    signature: recompute the HMAC over the raw body they received, using
    THEIR OWN `call_completed_webhook_secret` (shown to them once, at
    generation time — see app/models/platform.py's
    `UpdatePlatformSettingsResponse` docstring), and compare with
    `hmac.compare_digest`.

    Thin wrapper over the shared `sign_webhook_body` helper (see
    app/utils/webhook_signing.py's module docstring for why this was
    extracted once `app/services/custom_tool_relay.py` needed the identical
    computation) — kept as a named function here rather than inlined at
    every call site in this module, since `sign_payload` is also the name
    this module's own tests and docstrings already reference.
    """
    return sign_webhook_body(raw_body=raw_body, secret=secret)


class CallCompletedDeliveryResult:
    """Outcome of one full delivery attempt sequence (including retries) to
    Platform X's registered call-completed webhook.

    `outcome` is one of:
      - "delivered"       — a 2xx response was received (possibly after
                             one or more retries).
      - "not_registered"  — the platform has no call_completed_webhook_url
                             set; no attempt was made at all.
      - "exhausted"        — every attempt failed (timeout/error/non-2xx);
                             the notification was NOT delivered.
    """

    def __init__(
        self,
        *,
        outcome: str,
        attempts: int,
        elapsed_ms: float,
        last_error_class: str | None = None,
        last_upstream_status: int | None = None,
    ) -> None:
        self.outcome = outcome
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        self.last_error_class = last_error_class
        self.last_upstream_status = last_upstream_status


async def _post_once(
    *, webhook_url: str, raw_body: bytes, signature: str
) -> tuple[bool, int | None, str | None]:
    """One HTTP attempt. Returns (succeeded, upstream_status, error_class).

    Never raises — every httpx failure mode is caught here so the retry loop
    in `deliver()` never needs its own try/except around this call, mirroring
    platform_relay.request_dynamic_variables()'s "never raises" contract.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(CALL_COMPLETED_WEBHOOK_TIMEOUT_SECONDS)
        ) as client:
            resp = await client.post(
                webhook_url,
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    _SIGNATURE_HEADER: signature,
                },
            )
    except httpx.HTTPError as exc:
        return False, None, type(exc).__name__

    if resp.status_code >= 400:
        return False, resp.status_code, None
    return True, resp.status_code, None


async def deliver(
    *,
    call: CallInDB,
    webhook_url: str | None,
    webhook_secret: str | None,
) -> CallCompletedDeliveryResult:
    """Notify Platform X that `call` has finished, with bounded retries.

    Skips cleanly (outcome="not_registered") if `webhook_url` is falsy — the
    same "unregistered is the fast, expected, majority-case path, not an
    error" contract already established for the inbound-variables relay
    (see app/routers/webhooks.py's handling of a missing
    `inbound_variables_webhook_url`). This is expected to be the common case
    today: no real platform integration exists yet beyond the "Swagger
    testing" test platform.

    `webhook_secret` may legitimately be None even when `webhook_url` is
    set, ONLY as a defensive fallback for data predating this feature or a
    direct repository write that skipped the router's generation step (see
    platform_repo.set_call_completed_webhook_url's docstring) — the normal
    path (PATCH /platform) always generates a secret in the same call that
    first sets a non-null URL, so this should not happen for any platform
    onboarded through the real API. Rather than silently sending an
    unsigned request in that edge case (which would violate the "sign every
    outgoing webhook" rule silently), this is treated as a delivery failure
    and logged loudly — a misconfigured signing setup should be visible, not
    quietly downgraded to "sent unsigned."

    Retries up to `CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS` times with a fixed
    `CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS` delay between attempts (see
    this module's docstring for the full reasoning) — all within this one
    call, i.e. within the same background task the caller already runs this
    from (see app/routers/webhooks.py's `_process_finished_call`), never
    spanning multiple separate HTTP requests/retried webhook deliveries from
    Retell's side. Every outcome (delivered / not_registered / exhausted) is
    logged with full structured detail so a failed delivery is visible and
    debuggable, never silently lost.
    """
    if not webhook_url:
        logger.info(
            "Call-completed notification skipped — no URL registered",
            extra={"call_id": call.id, "outcome": "not_registered"},
        )
        return CallCompletedDeliveryResult(outcome="not_registered", attempts=0, elapsed_ms=0.0)

    payload = build_call_completed_payload(call)
    raw_body = json.dumps(payload).encode("utf-8")

    if not webhook_secret:
        # See this function's docstring — a genuinely unexpected state for
        # any platform onboarded through PATCH /platform, surfaced loudly
        # rather than silently sending an unsigned request.
        logger.error(
            "Call-completed webhook URL registered with no signing secret — "
            "refusing to send an unsigned notification",
            extra={"call_id": call.id, "outcome": "exhausted", "reason": "missing_secret"},
        )
        return CallCompletedDeliveryResult(outcome="exhausted", attempts=0, elapsed_ms=0.0)

    signature = sign_payload(raw_body=raw_body, secret=webhook_secret)

    started = time.monotonic()
    last_error_class: str | None = None
    last_upstream_status: int | None = None

    for attempt in range(1, CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS + 1):
        succeeded, upstream_status, error_class = await _post_once(
            webhook_url=webhook_url, raw_body=raw_body, signature=signature
        )
        last_upstream_status = upstream_status
        last_error_class = error_class

        if succeeded:
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.info(
                "Call-completed notification delivered",
                extra={
                    "call_id": call.id,
                    "outcome": "delivered",
                    "attempts": attempt,
                    "elapsed_ms": round(elapsed_ms, 1),
                    "upstream_status": upstream_status,
                },
            )
            return CallCompletedDeliveryResult(
                outcome="delivered",
                attempts=attempt,
                elapsed_ms=elapsed_ms,
                last_upstream_status=upstream_status,
            )

        logger.warning(
            "Call-completed notification attempt failed",
            extra={
                "call_id": call.id,
                "attempt": attempt,
                "max_attempts": CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS,
                "upstream_status": upstream_status,
                "error_class": error_class,
            },
        )
        if attempt < CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS:
            await asyncio.sleep(CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS)

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.error(
        "Call-completed notification exhausted all retries — not delivered",
        extra={
            "call_id": call.id,
            "outcome": "exhausted",
            "attempts": CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS,
            "elapsed_ms": round(elapsed_ms, 1),
            "last_upstream_status": last_upstream_status,
            "last_error_class": last_error_class,
        },
    )
    return CallCompletedDeliveryResult(
        outcome="exhausted",
        attempts=CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS,
        elapsed_ms=elapsed_ms,
        last_error_class=last_error_class,
        last_upstream_status=last_upstream_status,
    )
