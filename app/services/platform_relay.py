"""Relay client for asking Platform X for per-call dynamic variables — the
OUTBOUND half of inbound dynamic-variable injection (see
app/routers/webhooks.py's module docstring for the full mechanism, and
app/models/inbound_call.py for the request/response contract Platform X
implements).

**Design mandate, explicit and non-negotiable (from the user directly):**
"delay should not be from our [side] to [the voice vendor] — if delay [is]
from [Platform X's] side then that's not our fault." Concretely:

  1. Our OWN processing before this relay call (receiving the vendor's
     webhook, looking up which platform/agent owns the number) must be
     minimal — no N+1 queries, no unnecessary work.
  2. This relay call itself carries a strict, short timeout — see
     `PLATFORM_RELAY_TIMEOUT_SECONDS` below for the exact value and
     reasoning — never anywhere near the voice vendor's real ~10s ceiling.
  3. If Platform X responds in time, we use their variables.
  4. If Platform X times out or errors, we fall back to safe empty
     variables and still respond to the vendor promptly — a slow/down
     Platform X server must never cause US to blow the vendor's deadline.
  5. Every call logs which of these outcomes happened and why, with
     granular per-segment timing (see app/routers/webhooks.py's logging),
     so "was this our fault or Platform X's" is answerable from logs alone —
     including separating OUR OWN processing time from Platform X's actual
     response time, since a slow DB lookup on our side is just as much
     "our fault" as a slow relay call, and the mandate above only holds up
     if we can prove which side actually caused a delay, not just assert it.

This module owns only the HTTP relay itself — timestamping/logging of the
surrounding steps (webhook received, platform/agent resolved, final response
sent) lives in app/routers/webhooks.py, since those steps aren't calls this
module makes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("app.platform_relay")

# Retell's real, documented ceiling for the inbound-call webhook round trip:
# waits up to 10s, retries up to 3x on non-2xx. We must respond well inside
# that, with real margin left over for (a) our own processing before AND
# after this relay call, and (b) the voice vendor's own network round trip
# to/from us, which isn't accounted for in this number at all. 4 seconds
# leaves at least ~6s of that 10s ceiling for everything else — generous
# margin, not a number chosen to be "as large as possible while technically
# safe." Kept well clear of eating the vendor's whole budget, per the
# explicit "not our fault" design mandate above: a Platform X integrator
# who reads this number knows exactly what they're being held to, and 4s is
# a reasonable, generous bar for a webhook handler that's expected to do a
# fast DB lookup and nothing else.
PLATFORM_RELAY_TIMEOUT_SECONDS = 4.0


class PlatformRelayResult:
    """Outcome of one relay attempt to Platform X's registered webhook.

    `outcome` is one of "success", "timeout", "error", "not_registered" —
    mirrors exactly the reasons a caller falls back to safe defaults, so
    app/routers/webhooks.py can log a single clear field explaining why,
    rather than callers re-deriving it from exception types.
    """

    def __init__(
        self,
        *,
        outcome: str,
        dynamic_variables: dict[str, str],
        elapsed_ms: float,
        error_class: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        self.outcome = outcome
        self.dynamic_variables = dynamic_variables
        self.elapsed_ms = elapsed_ms
        self.error_class = error_class
        self.upstream_status = upstream_status


async def request_dynamic_variables(
    *,
    webhook_url: str,
    from_number: str | None,
    to_number: str | None,
    agent_id: str,
) -> PlatformRelayResult:
    """POST to Platform X's own `inbound_variables_webhook_url`, enforcing
    our own strict `PLATFORM_RELAY_TIMEOUT_SECONDS` timeout — never Retell's
    much larger real ceiling.

    Never raises — every failure mode (timeout, connection error, non-2xx,
    malformed response body) is caught and folded into a `PlatformRelayResult`
    with `outcome != "success"` and empty `dynamic_variables`, so the caller
    always has a safe, immediate value to fall back to and never needs a
    try/except of its own around this call. This mirrors why
    retell_adapter's functions raise AppError instead — different shape
    because the caller here needs to keep going (respond to the vendor
    regardless), not stop and surface an error to an HTTP caller.
    """
    body: dict[str, Any] = {
        "from_number": from_number,
        "to_number": to_number,
        "agent_id": agent_id,
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PLATFORM_RELAY_TIMEOUT_SECONDS)
        ) as client:
            resp = await client.post(webhook_url, json=body)
    except httpx.TimeoutException as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.warning(
            "Platform X inbound-variables relay timed out",
            extra={
                "outcome": "timeout",
                "relay_timeout_seconds": PLATFORM_RELAY_TIMEOUT_SECONDS,
                "elapsed_ms": round(elapsed_ms, 1),
                "error_class": type(exc).__name__,
            },
        )
        return PlatformRelayResult(
            outcome="timeout",
            dynamic_variables={},
            elapsed_ms=elapsed_ms,
            error_class=type(exc).__name__,
        )
    except httpx.HTTPError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.warning(
            "Platform X inbound-variables relay failed",
            extra={
                "outcome": "error",
                "elapsed_ms": round(elapsed_ms, 1),
                "error_class": type(exc).__name__,
            },
        )
        return PlatformRelayResult(
            outcome="error",
            dynamic_variables={},
            elapsed_ms=elapsed_ms,
            error_class=type(exc).__name__,
        )

    elapsed_ms = (time.monotonic() - started) * 1000

    if resp.status_code >= 400:
        logger.warning(
            "Platform X inbound-variables relay returned an error",
            extra={
                "outcome": "error",
                "elapsed_ms": round(elapsed_ms, 1),
                "upstream_status": resp.status_code,
            },
        )
        return PlatformRelayResult(
            outcome="error",
            dynamic_variables={},
            elapsed_ms=elapsed_ms,
            upstream_status=resp.status_code,
        )

    try:
        data = resp.json()
        raw_vars = data.get("dynamic_variables") if isinstance(data, dict) else None
        variables = (
            {str(k): str(v) for k, v in raw_vars.items()} if isinstance(raw_vars, dict) else {}
        )
    except ValueError:
        # Malformed JSON body — treat exactly like any other Platform X
        # error, not a crash: we still owe the vendor a fast, safe response.
        logger.warning(
            "Platform X inbound-variables relay returned a non-JSON response",
            extra={"outcome": "error", "elapsed_ms": round(elapsed_ms, 1)},
        )
        return PlatformRelayResult(
            outcome="error",
            dynamic_variables={},
            elapsed_ms=elapsed_ms,
            upstream_status=resp.status_code,
        )

    logger.info(
        "Platform X inbound-variables relay succeeded",
        extra={
            "outcome": "success",
            "elapsed_ms": round(elapsed_ms, 1),
            "upstream_status": resp.status_code,
            "variable_count": len(variables),
        },
    )
    return PlatformRelayResult(
        outcome="success",
        dynamic_variables=variables,
        elapsed_ms=elapsed_ms,
        upstream_status=resp.status_code,
    )
