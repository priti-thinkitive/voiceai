"""Shared HMAC-SHA256 signing helper for VoiceAI's OWN outgoing webhooks to
Platform X — the `X-VoiceAI-Signature` header, per the standards doc's
"Webhook security — both directions" section.

Extracted once two call sites needed the identical one-liner:
`app/services/call_completed_webhook.py`'s `sign_payload()` (the outbound
"call completed" notification) and `app/services/custom_tool_relay.py`'s
relay to Platform X's own per-tool `webhook_url` (the mid-call custom-tool
proxy). Both compute `hmac.new(secret.encode(), raw_body, hashlib.sha256).
hexdigest()` — genuinely identical algorithm, encoding, and digest choice,
not just superficially similar — so this is the DRY-correct extraction, not
premature abstraction: a future change to how VoiceAI signs its own
outbound webhooks (e.g. switching hash algorithms) would otherwise have to
be made in two places and could silently drift out of sync.

Mirrors the verification-side counterpart in `app/routers/webhooks.py`'s
`_verify_signature` (which verifies Retell's signature TO us) — same
algorithm, opposite direction, opposite role (signer here, verifier there).
That function is NOT folded in here since its scheme (timestamp-prefixed
value, replay-window checking, `RETELL_API_KEY` as secret) is genuinely
different from this simple raw-body-only HMAC — sharing only the underlying
primitive (`hmac.new(..., hashlib.sha256)`) would not be a real
simplification, just a coincidence of both using SHA-256.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_webhook_body(*, raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256 hex digest over the raw request body, using the given
    per-platform secret. The caller sends this as the `X-VoiceAI-Signature`
    header value; the receiver (Platform X) verifies by recomputing the same
    HMAC over the raw body they received, using their own copy of the same
    secret, and comparing with `hmac.compare_digest`.
    """
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
