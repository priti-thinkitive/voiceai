"""Unit tests for app/services/storage.py — the S3 storage adapter.

Per the standards doc's hard no-real-network-calls-in-tests rule, extended
to AWS/S3 per this task's brief: never a real boto3 call against real AWS.
Two kinds of tests here:

  1. The "AWS_S3_BUCKET empty" fast-path (no boto3 client construction/call
     at all — checked explicitly up front in storage.py) needs no mocking,
     it's pure Python control flow.
  2. A genuine credentials-missing failure IS exercised for real against
     boto3 (not monkeypatched) — this is safe and makes no real network
     call because botocore's own NoCredentialsError is raised locally,
     before any HTTP request is attempted, when no credentials resolve from
     any source (explicit args, env, ~/.aws, instance metadata all absent/
     empty in this test environment). This was confirmed manually before
     writing this test (see the task's live-verification notes) — it is
     NOT an assumption.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import AppError
from app.services.storage import S3StorageService, recording_key, transcript_key


def _settings(**overrides: str) -> Settings:
    base = {
        "AWS_ACCESS_KEY_ID": "",
        "AWS_SECRET_ACCESS_KEY": "",
        "AWS_S3_BUCKET": "",
        "AWS_REGION": "us-east-1",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_recording_key_and_transcript_key_are_deterministic_by_call_id() -> None:
    assert recording_key("abc123") == "recordings/abc123.wav"
    assert transcript_key("abc123") == "transcripts/abc123.txt"
    # Same input -> same key, every time -> the idempotency guarantee this
    # module's docstring documents (a re-upload overwrites, not duplicates).
    assert recording_key("abc123") == recording_key("abc123")


async def test_upload_with_empty_bucket_raises_storage_failed_no_network_call() -> None:
    """AWS_S3_BUCKET="" is the exact real placeholder state in .env today —
    must fail clean and fast, checked before any boto3 client/call is even
    constructed.
    """
    settings = _settings()
    service = S3StorageService()
    with pytest.raises(AppError) as exc_info:
        await service.upload(
            key="recordings/x.wav", content=b"data", content_type="audio/wav", settings=settings
        )
    assert exc_info.value.code == "storage_failed"
    assert exc_info.value.status_code == 502


async def test_download_with_empty_bucket_raises_storage_failed_no_network_call() -> None:
    settings = _settings()
    service = S3StorageService()
    with pytest.raises(AppError) as exc_info:
        await service.download(key="recordings/x.wav", settings=settings)
    assert exc_info.value.code == "storage_failed"
    assert exc_info.value.status_code == 502


async def test_upload_with_bucket_but_no_credentials_fails_clean_not_a_crash() -> None:
    """A bucket IS configured but credentials are missing — boto3's own
    NoCredentialsError fires locally (no real HTTP request is made; botocore
    checks for resolvable credentials before attempting a network call) and
    must be caught and converted to the same clean AppError contract as
    every other vendor/storage failure in this codebase, never a raw
    botocore exception leaking to a caller.
    """
    settings = _settings(AWS_S3_BUCKET="some-bucket-that-does-not-need-to-exist-for-this-test")
    service = S3StorageService()
    with pytest.raises(AppError) as exc_info:
        await service.upload(
            key="recordings/x.wav", content=b"data", content_type="audio/wav", settings=settings
        )
    assert exc_info.value.code == "storage_failed"
    assert exc_info.value.status_code == 502
    # Never leak the raw botocore exception text/class to the caller-facing
    # message — only our own generic, clean sentence.
    assert "NoCredentials" not in exc_info.value.message
    assert "boto" not in exc_info.value.message.lower()
