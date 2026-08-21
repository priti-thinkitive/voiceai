"""Storage adapter for re-hosted call recordings/transcripts — AWS S3 today,
swappable later, mirroring the vendor-adapter-layer precedent already
established for Retell (see retell_adapter.py's module docstring: "a future
second vendor gets its own adapter module with the same call shape, never a
branch inside this one"). The same idea applied to storage: a future second
backend (e.g. GCS, local disk for a self-hosted deploy) gets its own module
implementing the same small function shapes below, never a branch inside this
one.

**Why S3, decided**: vendor-docs/White-Label-Launch-Plan.html's Phase 1 item
15 ("Recording & transcript delivery") mandates "re-host, don't pass
through" — we download the recording/transcript from Retell's own
vendor-hosted URLs and store our own copy, so Platform X (and any API
response) never sees a Retell-hosted URL. The standards doc's Config &
secrets section already recorded the explicit user decision: AWS S3 over
local disk, real `boto3` integration built now even though real credentials
(`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_S3_BUCKET`) are still empty
placeholders — same "field exists, real value comes later" pattern
`RETELL_API_KEY` started from. Never stub/mock the S3 calls in application
code; only tests mock them (see the standards doc's hard no-real-network-
calls-in-tests rule, which extends naturally to no-real-AWS-calls-in-tests).

**Serving mechanism — proxy through our own domain, not a presigned URL —
decided, and reasoned through explicitly (not defaulted):**

Two real options were weighed:
  1. A presigned S3 URL — simpler, standard practice, no proxy hop/cost on
     our own server for every access.
  2. Stream the bytes back through our own API endpoint (the same pattern
     already proven in this codebase for GET /voices/{voice_id}/preview,
     which re-hosts Retell's own preview-audio S3 URLs the exact same way).

Chosen: **option 2, the proxy**. Reasoning:
  - **Consistency with an already-shipped, already-verified precedent in
    this exact codebase.** GET /voices/{voice_id}/preview already solved
    the identical problem (re-host a vendor-hosted S3-backed audio file
    under our own domain) the same way, for the same underlying reason
    (vendor-docs/Full-System-Architecture.html's "download and re-store...
    on our own domain" rule). Recordings/transcripts are the *original*,
    higher-stakes case that rule was written for — using a different
    mechanism here than the one already proven for the lower-stakes voice-
    preview case would be an unjustified inconsistency, not a considered
    choice.
  - **A presigned URL still requires a real expiry policy decision, and
    every option is worse for this specific data.** Recordings/transcripts
    are exactly the kind of "potentially long-lived reference link Platform
    X may store" the task brief flags — a platform integrator could
    reasonably persist a recording URL in their own database to show in a
    UI weeks later. A short presigned expiry (e.g. 1 hour) would silently
    break that later; a long/indefinite one undermines the entire reason
    presigned URLs exist (bounded, revocable access) and starts to
    resemble "just give them a permanent URL" without any of a proxy's
    other benefits.
  - **A presigned URL still names our real bucket/account identity in the
    URL itself** (`https://<bucket>.s3.<region>.amazonaws.com/...` or
    `https://s3.<region>.amazonaws.com/<bucket>/...`), which is the same
    class of defect this project has now found and fixed twice this
    session (GET /voices' preview_audio_url leak, and the Swagger-text
    leak) — just shifted from "Retell's domain" to "our own AWS domain."
    It would stop naming the *voice vendor*, but it would start naming our
    own cloud infrastructure provider/account/bucket instead, which is a
    real, if smaller, information leak of its own and a needless one to
    introduce when a same-domain alternative already exists and is already
    proven in this codebase.
  - **Trade-off accepted, explicitly**: a proxy adds a real hop and
    bandwidth cost on our own server for every access, and doesn't benefit
    from S3/CDN edge caching the way a presigned URL redirecting a browser
    directly to S3 would. This is accepted for the same reason it was
    accepted for voice previews — access-controlled, permanently-stable,
    same-domain URLs are worth the extra hop, and this is not a
    high-volume/low-latency streaming media use case (a played-back call
    recording, not a live stream) where that cost would be prohibitive.
    Revisit only if this becomes an observed real performance/cost problem
    at scale, not speculatively.

**Access control on the serving endpoint**: unlike voice-preview audio
(public catalog data, not platform-owned), a call recording/transcript is
platform-owned data that may contain real PII/PHI (per the standards doc's
Logging section, PII/PHI-safety is already a first-class concern in this
project) — the serving endpoint requires `get_current_platform` AND is
tenancy-scoped to the calling platform's own `Calls` record, never served to
an arbitrary bearer of a valid API key for someone else's call. See
app/routers/calls.py's `get_call_recording`/`get_call_transcript` for the
enforcement.

**S3 key structure — deterministic, keyed by our own Mongo `_id`, chosen
specifically for idempotency (see app/routers/webhooks.py's post-call
handler docstring for the full idempotency reasoning)**:
`recordings/{call_mongo_id}.wav` / `transcripts/{call_mongo_id}.txt`. A
retried/re-delivered webhook for the same call re-uploads to the exact same
key — S3 `put_object` overwrites in place, so a retry is naturally
idempotent with no separate "already uploaded" bookkeeping needed.

**Error handling — `AppError(code="storage_failed")`, deliberately NOT
`upstream_failed`**: see errors.py's CODE_STORAGE_FAILED for the full
reasoning (a storage failure is OUR OWN infrastructure, not the voice
vendor's, so conflating the two codes would blur two genuinely different
failure classes for Platform X's own error handling). Never leaks a raw
boto3 exception message to a caller (boto3 error strings can carry account
IDs, bucket ARNs, or other internal AWS details) — logged for our own
debugging, generic message returned, same never-leak-raw-response pattern as
every retell_adapter.py function.

**Async discipline**: `boto3`'s S3 client is synchronous — every method here
wraps the actual boto3 call in `asyncio.to_thread` per the standards doc's
async-discipline rule (never block the single event loop with a sync SDK
call), the same pattern already used for DNS resolution in
app/routers/platform.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.errors import CODE_STORAGE_FAILED, AppError

logger = logging.getLogger("app.storage")

VENDOR_NAME_S3 = "s3"


class StorageService(Protocol):
    """The small, swappable storage interface — any future backend (a
    second cloud provider, local disk for a self-hosted deploy) implements
    this same shape. Only `S3StorageService` exists today; nothing else in
    this codebase should call `boto3` directly.
    """

    async def upload(
        self, *, key: str, content: bytes, content_type: str, settings: Settings
    ) -> None: ...

    async def download(self, *, key: str, settings: Settings) -> tuple[bytes, str]: ...


def _client(settings: Settings) -> Any:
    """Construct a real boto3 S3 client from Settings.

    Returns `Any` — boto3's client factory has no useful static type (it
    returns a dynamically-generated class per service), same reason
    botocore-stubs/boto3-stubs are a separate optional package this project
    doesn't depend on; every call site here only calls documented S3 client
    methods (`put_object`/`get_object`), so the dynamic typing is contained
    to this one function.

    Missing credentials are NOT checked here — boto3 itself will raise a
    real `NoCredentialsError`/`ClientError` the moment a call is actually
    attempted, which is exactly the "let it genuinely fail with a clean
    error" behavior the task requires (never stub/fake the integration in
    application code). Constructing the client with empty-string
    credentials is itself harmless; the failure surfaces on the first real
    API call.
    """
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        aws_session_token=settings.AWS_SESSION_TOKEN or None,
        region_name=settings.AWS_REGION,
    )


class S3StorageService:
    """Real AWS S3 implementation of StorageService, using boto3."""

    async def upload(
        self, *, key: str, content: bytes, content_type: str, settings: Settings
    ) -> None:
        """Upload `content` to `key` in `settings.AWS_S3_BUCKET`, overwriting
        whatever (if anything) already exists at that key — the source of
        this module's idempotency guarantee (see module docstring).

        Raises `AppError(code="storage_failed")` if `AWS_S3_BUCKET` is empty
        (nothing configured to upload to — checked explicitly up front so
        the error message is a clear "not configured" rather than an opaque
        boto3 error about a bucket named the empty string) or on any
        botocore error (missing/invalid credentials, network failure,
        access-denied, bucket doesn't exist, etc.) — never leaks the raw
        boto3 exception text to a caller, only logs it.
        """
        if not settings.AWS_S3_BUCKET:
            logger.warning(
                "S3 upload skipped — AWS_S3_BUCKET not configured",
                extra={"vendor": VENDOR_NAME_S3, "key": key},
            )
            raise AppError(
                code=CODE_STORAGE_FAILED,
                message="File storage is not configured. Try again shortly.",
                status_code=502,
                log_extra={"vendor": VENDOR_NAME_S3, "key": key, "reason": "bucket_not_configured"},
            )

        try:
            client = _client(settings)
            await _run_sync(
                client.put_object,
                Bucket=settings.AWS_S3_BUCKET,
                Key=key,
                Body=content,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "S3 upload failed",
                extra={"vendor": VENDOR_NAME_S3, "key": key, "error_class": type(exc).__name__},
            )
            raise AppError(
                code=CODE_STORAGE_FAILED,
                message="Could not store the file. Try again shortly.",
                status_code=502,
                log_extra={
                    "vendor": VENDOR_NAME_S3,
                    "key": key,
                    "error_class": type(exc).__name__,
                },
            ) from exc

    async def download(self, *, key: str, settings: Settings) -> tuple[bytes, str]:
        """Fetch `key`'s bytes + content type back from
        `settings.AWS_S3_BUCKET` — used by the GET /calls/{id}/recording and
        /transcript proxy endpoints (see app/routers/calls.py) to stream the
        re-hosted file back to Platform X under our own domain.

        Raises `AppError(code="not_found")`-shaped semantics are NOT handled
        here — a missing key is a real, expected "nothing re-hosted yet"
        case the router distinguishes from a genuine storage failure (see
        that router for the 404 vs 502 split), so this re-raises
        `ClientError` for a 404/NoSuchKey response distinctly from any other
        botocore error.
        """
        if not settings.AWS_S3_BUCKET:
            raise AppError(
                code=CODE_STORAGE_FAILED,
                message="File storage is not configured. Try again shortly.",
                status_code=502,
                log_extra={"vendor": VENDOR_NAME_S3, "key": key, "reason": "bucket_not_configured"},
            )

        try:
            client = _client(settings)
            response = await _run_sync(client.get_object, Bucket=settings.AWS_S3_BUCKET, Key=key)
            body = await _run_sync(response["Body"].read)
            content_type = response.get("ContentType", "application/octet-stream")
            return body, content_type
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise
            logger.warning(
                "S3 download failed",
                extra={"vendor": VENDOR_NAME_S3, "key": key, "error_class": type(exc).__name__},
            )
            raise AppError(
                code=CODE_STORAGE_FAILED,
                message="Could not retrieve the file. Try again shortly.",
                status_code=502,
                log_extra={
                    "vendor": VENDOR_NAME_S3,
                    "key": key,
                    "error_class": type(exc).__name__,
                },
            ) from exc
        except BotoCoreError as exc:
            logger.warning(
                "S3 download failed",
                extra={"vendor": VENDOR_NAME_S3, "key": key, "error_class": type(exc).__name__},
            )
            raise AppError(
                code=CODE_STORAGE_FAILED,
                message="Could not retrieve the file. Try again shortly.",
                status_code=502,
                log_extra={
                    "vendor": VENDOR_NAME_S3,
                    "key": key,
                    "error_class": type(exc).__name__,
                },
            ) from exc


async def _run_sync(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """`asyncio.to_thread` wrapper — see module docstring's async-discipline
    note. A tiny local helper rather than repeating the wrapping at every
    call site; kept private to this module.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


def recording_key(call_id: str) -> str:
    """Deterministic S3 key for a call's recording, keyed by OUR OWN Mongo
    `_id` — never Retell's call_id (that would put a vendor-correlated
    identifier in a storage key an ops engineer might see, no real benefit
    over our own id, which is what every other part of this codebase already
    uses as the stable public-facing identifier for a call).
    """
    return f"recordings/{call_id}.wav"


def transcript_key(call_id: str) -> str:
    """Deterministic S3 key for a call's transcript. See recording_key's
    docstring for why this is keyed by our own Mongo id.
    """
    return f"transcripts/{call_id}.txt"


_service: StorageService = S3StorageService()


def get_storage_service() -> StorageService:
    """The one place callers get a StorageService instance — mirrors
    retell_adapter's module-level-function pattern (no DI container in this
    codebase yet), but exposed as a function so a future test or a future
    second backend can swap the module-level `_service` singleton without
    every call site needing to change.
    """
    return _service
