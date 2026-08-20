"""Relay client for the custom-tool proxy — the OUTBOUND half of the
mid-call custom-tool mechanism (see app/routers/webhooks.py's custom-tool
handler for the full mechanism, and app/models/custom_tool_call.py for the
request/response contract Platform X implements on their own registered
`webhook_url`).

Structurally the SAME pattern as app/services/platform_relay.py (the
existing inbound pre-call webhook relay) — reused deliberately, not
reinvented, per this task's explicit instruction. The one real difference:
this relay's response body is passed back to the voice vendor largely
UNCHANGED (any JSON object Platform X returns becomes the tool's result —
see PlatformCustomToolRequest's docstring), rather than being narrowed down
to one specific known key (`dynamic_variables`) the way the inbound relay's
response is. So this module's success case returns the raw parsed JSON body,
not a project-defined shape.

**Signing — closes former Known open item 3 (see backend-dev.md's Feature
status section for the closing entry).** Every relay call now carries an
`X-VoiceAI-Signature` header, HMAC-SHA256 over the raw JSON body, using the
EXACT SAME per-platform-secret pattern already built and proven for
`app/services/call_completed_webhook.py`'s outbound notification — signing
is computed via the shared `app/utils/webhook_signing.sign_webhook_body`
helper (see that module's docstring for why it was extracted once this
became the second identical HMAC computation in the codebase).

**Secret-reuse decision, deliberate: `PlatformInDB.call_completed_webhook_
secret` is reused here, not a new separate secret field.** A platform's
signing secret's whole job is "prove this request genuinely came from
VoiceAI" — that property is identical regardless of which VoiceAI-initiated
notification type is being verified, mirroring exactly how
`call_completed_webhook_secret`'s own docstring (app/models/platform.py)
already reasons about why the secret is per-PLATFORM rather than per-URL:
the thing being protected is "this came from VoiceAI, for MY integration,"
not "this came from VoiceAI, for THIS SPECIFIC notification type." Both
notification types (call-completed, custom-tool relay) are already scoped
to the one platform relationship the secret belongs to, so splitting them
would buy no real security benefit — a compromised secret already lets an
attacker forge either notification type for that one platform, whether the
secret is shared or split. Reusing it avoids: a second secret field on
`PlatformInDB`, a second one-time-reveal flow in `PATCH /platform`'s
response, and one more credential for a Platform X integrator to generate,
store, and rotate. If a genuinely new reason to split them ever surfaces
(e.g. independently rotating one notification type without affecting the
other), that's a new, explicit decision to make then — not assumed now.

**Hard-fail-if-unsigned, same discipline as `call_completed_webhook.deliver()`'s
missing-secret handling, re-applied here deliberately, not by accident.**
`call_completed_webhook.deliver()` treats "URL registered but no secret"
(a state that shouldn't normally happen via the real `PATCH /platform` path,
but is possible via a direct repository write, or — for THIS relay
specifically — a platform that only ever registered custom tools and never
set `call_completed_webhook_url`, so no secret was ever generated for it at
all) as a hard failure, never a silently-unsigned send. `relay_tool_call`
applies the identical rule: a missing `secret` short-circuits to a clean
`CustomToolRelayResult(outcome="error", ...)` before any HTTP call is even
attempted, logged loudly. Silently sending an unsigned request here would
defeat the entire point of this task — Platform X's own tool endpoint would
have no way to tell a genuine VoiceAI relay from an impersonator who merely
discovered the URL, exactly the gap this task exists to close.

**Timeout, reasoned separately from PLATFORM_RELAY_TIMEOUT_SECONDS (4.0s),
not copied blindly.** That value was sized against the voice vendor's real
inbound-call-webhook ceiling (~10s). A custom tool call has a DIFFERENT,
per-tool-configurable ceiling instead: `CustomToolDefinition.timeout_ms`
(the value we told the vendor to use when calling OUR OWN proxy —
`url`'s timeout_ms, capped at 30s — see app/models/agent.py).
This relay's own timeout to Platform X's `webhook_url` must leave real
margin INSIDE that per-tool budget, on both sides of our own processing
(receiving the vendor's call, building/parsing this relay's request and
response) plus the vendor's own network round trip. Rather than a second
fixed constant that could drift out of sync with the per-tool value, this
relay computes its own timeout as a fraction of the SAME `timeout_ms` the
tool was registered with (`_RELAY_TIMEOUT_FRACTION`), floored at a sane
minimum — see `_relay_timeout_seconds()` below for the exact numbers.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.utils.webhook_signing import sign_webhook_body

logger = logging.getLogger("app.custom_tool_relay")

_SIGNATURE_HEADER = "X-VoiceAI-Signature"

# What fraction of the tool's own vendor-facing timeout_ms this relay call
# to Platform X is allowed to consume — leaves the remaining ~30% as real
# margin for our own processing before/after the relay call (DB lookups,
# building/parsing JSON) plus the voice vendor's own network round trip to
# us, mirroring PLATFORM_RELAY_TIMEOUT_SECONDS' "generous margin, not the
# maximum technically safe number" principle.
_RELAY_TIMEOUT_FRACTION = 0.7

# A floor so an unusually small timeout_ms (e.g. the model minimum, 1000ms)
# never shrinks the relay's own budget to something that can't complete even
# a fast, healthy round trip — 2s is a reasonable minimum for any real HTTP
# call, vendor timeout notwithstanding.
_MIN_RELAY_TIMEOUT_SECONDS = 2.0


def _relay_timeout_seconds(tool_timeout_ms: int) -> float:
    return max(_MIN_RELAY_TIMEOUT_SECONDS, (tool_timeout_ms / 1000) * _RELAY_TIMEOUT_FRACTION)


class CustomToolRelayResult:
    """Outcome of one relay attempt to Platform X's registered per-tool
    webhook. `outcome` is one of "success", "timeout", "error" — mirrors
    platform_relay.PlatformRelayResult's shape/reasoning so
    app/routers/webhooks.py can log a single clear field explaining why.

    `response_body` is the raw parsed JSON object Platform X returned, only
    populated on `outcome == "success"` — see this module's docstring for
    why this relay passes it through largely unchanged rather than narrowing
    it to one known key.
    """

    def __init__(
        self,
        *,
        outcome: str,
        response_body: dict[str, Any] | None,
        elapsed_ms: float,
        error_class: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        self.outcome = outcome
        self.response_body = response_body
        self.elapsed_ms = elapsed_ms
        self.error_class = error_class
        self.upstream_status = upstream_status


async def relay_tool_call(
    *,
    webhook_url: str,
    method: str,
    tool_timeout_ms: int,
    body: dict[str, Any],
    secret: str | None,
) -> CustomToolRelayResult:
    """POST (or whichever method the tool was configured with) to Platform
    X's own registered per-tool `webhook_url`, enforcing our own timeout
    derived from the tool's own vendor-facing `timeout_ms` (see this
    module's docstring for the exact fraction/floor), and signing the
    request with `X-VoiceAI-Signature` (see this module's docstring,
    "Signing" section, for the secret-reuse decision and the hard-fail-if-
    unsigned discipline).

    `secret` is the calling platform's own `PlatformInDB.
    call_completed_webhook_secret` (see this module's docstring for why
    that secret is reused here rather than adding a new one) — `None` is a
    real, handled case (a platform that only ever registered custom tools
    and never set `call_completed_webhook_url`, so no secret was ever
    generated), NOT sent through unsigned: this returns a clean
    `outcome="error"` result before any HTTP call is attempted.

    Never raises — every failure mode (missing secret, timeout, connection
    error, non-2xx, malformed response body) is caught and folded into a
    `CustomToolRelayResult` with `outcome != "success"`, so the caller always
    has a safe outcome to build a clean error response from and never needs
    a try/except of its own around this call — same "never raises" contract
    as platform_relay.request_dynamic_variables().
    """
    if not secret:
        # See this module's docstring — mirrors
        # call_completed_webhook.deliver()'s identical missing-secret
        # handling. A genuinely unexpected state for a platform that has
        # registered call_completed_webhook_url (which always generates a
        # secret in the same PATCH /platform call), but real and possible
        # for a platform that only ever used custom tools. Surfaced loudly,
        # never silently sent unsigned.
        logger.error(
            "Custom-tool relay has no signing secret for this platform — "
            "refusing to send an unsigned request",
            extra={"outcome": "error", "reason": "missing_secret"},
        )
        return CustomToolRelayResult(
            outcome="error",
            response_body=None,
            elapsed_ms=0.0,
            error_class="missing_secret",
        )

    timeout_seconds = _relay_timeout_seconds(tool_timeout_ms)
    started = time.monotonic()
    raw_body = json.dumps(body).encode("utf-8")
    signature = sign_webhook_body(raw_body=raw_body, secret=secret)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
            resp = await client.request(
                method,
                webhook_url,
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    _SIGNATURE_HEADER: signature,
                },
            )
    except httpx.TimeoutException as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.warning(
            "Custom-tool relay to Platform X timed out",
            extra={
                "outcome": "timeout",
                "relay_timeout_seconds": round(timeout_seconds, 2),
                "elapsed_ms": round(elapsed_ms, 1),
                "error_class": type(exc).__name__,
            },
        )
        return CustomToolRelayResult(
            outcome="timeout",
            response_body=None,
            elapsed_ms=elapsed_ms,
            error_class=type(exc).__name__,
        )
    except httpx.HTTPError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.warning(
            "Custom-tool relay to Platform X failed",
            extra={
                "outcome": "error",
                "elapsed_ms": round(elapsed_ms, 1),
                "error_class": type(exc).__name__,
            },
        )
        return CustomToolRelayResult(
            outcome="error",
            response_body=None,
            elapsed_ms=elapsed_ms,
            error_class=type(exc).__name__,
        )

    elapsed_ms = (time.monotonic() - started) * 1000

    if resp.status_code >= 400:
        logger.warning(
            "Custom-tool relay to Platform X returned an error",
            extra={
                "outcome": "error",
                "elapsed_ms": round(elapsed_ms, 1),
                "upstream_status": resp.status_code,
            },
        )
        return CustomToolRelayResult(
            outcome="error",
            response_body=None,
            elapsed_ms=elapsed_ms,
            upstream_status=resp.status_code,
        )

    try:
        data = resp.json()
    except ValueError:
        logger.warning(
            "Custom-tool relay to Platform X returned a non-JSON response",
            extra={"outcome": "error", "elapsed_ms": round(elapsed_ms, 1)},
        )
        return CustomToolRelayResult(
            outcome="error",
            response_body=None,
            elapsed_ms=elapsed_ms,
            upstream_status=resp.status_code,
        )

    if not isinstance(data, dict):
        # A valid JSON body but not an object (e.g. a bare array/string) —
        # the voice vendor's real response contract expects a JSON object
        # for the tool result, so this is treated the same as any other
        # malformed-response error rather than forwarded as-is.
        logger.warning(
            "Custom-tool relay to Platform X returned a non-object JSON response",
            extra={"outcome": "error", "elapsed_ms": round(elapsed_ms, 1)},
        )
        return CustomToolRelayResult(
            outcome="error",
            response_body=None,
            elapsed_ms=elapsed_ms,
            upstream_status=resp.status_code,
        )

    logger.info(
        "Custom-tool relay to Platform X succeeded",
        extra={
            "outcome": "success",
            "elapsed_ms": round(elapsed_ms, 1),
            "upstream_status": resp.status_code,
        },
    )
    return CustomToolRelayResult(
        outcome="success",
        response_body=data,
        elapsed_ms=elapsed_ms,
        upstream_status=resp.status_code,
    )
