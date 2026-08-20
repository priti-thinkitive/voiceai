"""API key generation and hashing for the platform authentication system.

Keys are `voiceai_live_<32+ random bytes, url-safe base64>` — the prefix makes
a leaked key instantly recognizable (e.g. in a scanned GitHub commit or a log
aggregator alert). Only the SHA-256 hash of the full key is ever persisted;
the plaintext exists only in-memory at issuance time and in the one-time
response shown to the platform.
"""

from __future__ import annotations

import hashlib
import secrets

API_KEY_PREFIX = "voiceai_live_"


def generate_api_key() -> str:
    """Return a new plaintext API key. Caller must show it once and discard it."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """SHA-256 hash of a plaintext API key, for storage/lookup.

    SHA-256 (not bcrypt/argon2) is appropriate here because the input is
    already a high-entropy random token, not a human-chosen password — there
    is no offline brute-force risk to defend against with a slow hash.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def key_display_prefix(api_key: str) -> str:
    """Short, safe-to-log/display fragment (e.g. 'voiceai_live_ab12cd34')."""
    return api_key[: len(API_KEY_PREFIX) + 8]


def generate_webhook_secret() -> str:
    """Return a new random per-platform HMAC signing secret, for signing our
    OUTGOING webhooks to Platform X (e.g. the call-completed notification —
    see app/services/call_completed_webhook.py).

    Same `secrets.token_urlsafe` construction as `generate_api_key()` above
    (high-entropy, URL-safe), but deliberately a separate function/value
    rather than reusing a platform's own API key as its webhook secret: the
    API key is a credential a platform sends TO us on every request (and
    could in principle leak via their own request logging); the webhook
    secret is a credential WE hold and use to sign requests TO them — mixing
    the two would mean a leak of either credential compromises both
    directions of the integration at once. No prefix (unlike the API key) —
    this value is never sent over the wire by Platform X and never needs to
    be recognized in a log/git-history grep the way a leaked bearer
    credential does; it exists only to be HMAC'd against, never presented as
    an identifier.
    """
    return secrets.token_urlsafe(32)
