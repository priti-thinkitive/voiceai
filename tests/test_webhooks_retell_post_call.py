"""Integration tests for POST /webhooks/retell/post-call — recording/
transcript re-hosting.

Per the standards doc's hard rule, extended explicitly to storage per this
task's brief: no real network call to Retell AND no real AWS/S3 call from
pytest. `retell_adapter.fetch_recording_bytes`/`fetch_transcript_text` and
`storage.get_storage_service()` are both monkeypatched — never a real httpx
call, never a real boto3 call.

The webhook handler acknowledges fast and defers the actual re-hosting work
to a FastAPI BackgroundTasks callback (see app/routers/webhooks.py's
module docstring, "Latency design"). `ASGITransport`/httpx's AsyncClient
already runs BackgroundTasks to completion before returning control in this
test setup (FastAPI's own TestClient/ASGI transport behavior — background
tasks run as part of the same ASGI response cycle, after the response body
is sent but before the transport call returns), so `await client.post(...)`
in these tests already reflects the background work having finished by the
time each assertion runs — no manual sleep/poll needed. This mirrors real
production behavior closely enough for these tests: the response Retell
sees is fast/independent of the background work's duration, but from the
test's point of view we still get a deterministic way to assert on the
finished state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.database import MongoDB
from app.errors import AppError
from app.models.agent import AgentStatus
from app.models.call import CallStatus
from app.models.language import Language
from app.repositories import agent_repo, call_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import call_completed_webhook as ccw
from app.services import retell_adapter
from app.services import storage as storage_module
from app.services.retell_adapter import VENDOR_NAME

# Standing in for RETELL_API_KEY, which is now the webhook-signature secret
# (Retell's own current scheme — see app/routers/webhooks.py's module
# docstring for the full sourced reasoning behind this change).
_WEBHOOK_SECRET = "test-retell-api-key-for-post-call"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET, *, timestamp_ms: int | None = None) -> str:
    """Builds a real "v={timestamp},d={digest}" header value the same way
    _verify_signature computes/parses it: HMAC-SHA256 over
    raw_body + timestamp_string (string concat), keyed by the API key.
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    ts_str = str(ts)
    digest = hmac.new(
        secret.encode("utf-8"), body + ts_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v={ts_str},d={digest}"


def _sign_old_scheme(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    """The OLD (no-longer-valid) bare-hexdigest-over-raw-body-only scheme,
    for the regression test proving it's genuinely rejected now.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_call_payload(
    *,
    event: str = "call_ended",
    call_id: str = "call_retell_abc123",
    recording_url: str | None = "https://retell-utils-public.s3.amazonaws.com/rec.wav",
    transcript: str | None = "Agent: Hello. Caller: Hi there.",
    call_analysis: dict[str, Any] | None = None,
    disconnection_reason: str | None = None,
) -> dict[str, Any]:
    call: dict[str, Any] = {
        "call_id": call_id,
        "agent_id": "agent_retell_xyz",
        "call_status": "ended",
        "recording_url": recording_url,
        "transcript": transcript,
    }
    if call_analysis is not None:
        call["call_analysis"] = call_analysis
    if disconnection_reason is not None:
        call["disconnection_reason"] = disconnection_reason
    return {"event": event, "call": call, "event_timestamp": 1234567890}


async def _seed_platform(db: MongoDB, name: str) -> tuple[str, str]:
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key, platform.id


async def _seed_agent(db: MongoDB, *, platform_id: str) -> str:
    agent = await agent_repo.create(
        db,
        platform_id=platform_id,
        prompt="You are a friendly assistant.",
        voice_id="11labs-Adrian",
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[],
        status=AgentStatus.ACTIVE,
        vendor=VENDOR_NAME,
        vendor_ref="agent_retell_xyz",
    )
    return agent.id


async def _seed_registered_call(
    db: MongoDB, *, platform_id: str, agent_id: str, vendor_ref: str = "call_retell_abc123"
) -> str:
    """Seeds a Calls document the way a successful POST /calls/outbound
    would have — status=registered, a real vendor_ref — so the post-call
    webhook has something real to correlate against and update.
    """
    call = await call_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        from_number="+19129143920",
        to_number="+15551234567",
        dynamic_variables={},
        status=CallStatus.REGISTERED,
        vendor=VENDOR_NAME,
        vendor_ref=vendor_ref,
    )
    return call.id


@pytest.fixture(autouse=True)
def _configure_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """RETELL_API_KEY is the webhook-signature secret now, not a separate
    RETELL_WEBHOOK_SECRET (removed entirely — see app/routers/webhooks.py's
    module docstring).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", _WEBHOOK_SECRET)


class _FakeStorageService:
    """Records every upload call instead of touching real S3 — the
    "no real AWS calls in tests" mock, mirroring how retell_adapter.* is
    monkeypatched everywhere else in this suite.
    """

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    async def upload(self, *, key: str, content: bytes, content_type: str, settings: Any) -> None:
        del settings
        self.uploads.append({"key": key, "content": content, "content_type": content_type})

    async def download(self, *, key: str, settings: Any) -> tuple[bytes, str]:
        del settings
        for u in self.uploads:
            if u["key"] == key:
                return u["content"], u["content_type"]
        raise AssertionError(f"no upload recorded for key {key}")


class _FailingStorageService:
    """Simulates a genuine S3 failure (e.g. missing/invalid credentials) —
    the exact shape storage.py's S3StorageService raises for real against
    an empty AWS_S3_BUCKET, without making any real boto3 call.
    """

    async def upload(self, *, key: str, content: bytes, content_type: str, settings: Any) -> None:
        del key, content, content_type, settings
        raise AppError(
            code="storage_failed",
            message="File storage is not configured. Try again shortly.",
            status_code=502,
        )

    async def download(self, *, key: str, settings: Any) -> tuple[bytes, str]:
        del key, settings
        raise AppError(
            code="storage_failed",
            message="Could not retrieve the file. Try again shortly.",
            status_code=502,
        )


async def _fake_fetch_recording(settings: Any, *, recording_url: str) -> tuple[bytes, str]:
    del settings, recording_url
    return b"FAKE_WAV_BYTES", "audio/wav"


async def test_valid_webhook_processes_and_updates_call_and_uploads_files(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Caller asked about visiting hours.",
            "user_sentiment": "Positive",
        },
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.status == CallStatus.COMPLETED
    assert updated.summary == "Caller asked about visiting hours."
    assert updated.sentiment == "Positive"
    assert updated.recording_url == f"/calls/{call_mongo_id}/recording"
    assert updated.transcript_url == f"/calls/{call_mongo_id}/transcript"
    assert updated.recording_rehost_failed is False

    # Two uploads: recording (fake bytes from the monkeypatched adapter) and
    # transcript (the plain payload text, re-hosted as-is).
    uploaded_keys = {u["key"] for u in fake_storage.uploads}
    assert uploaded_keys == {
        f"recordings/{call_mongo_id}.wav",
        f"transcripts/{call_mongo_id}.txt",
    }


async def test_call_ended_without_analysis_leaves_summary_sentiment_none(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """call_ended fires first, usually before analysis finishes — confirmed
    real ordering (see this module's docstring / webhooks.py's research).
    Summary/sentiment must stay None, not be forced to some default.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(event="call_ended", call_analysis=None)
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.status == CallStatus.COMPLETED
    assert updated.summary is None
    assert updated.sentiment is None
    assert updated.recording_url == f"/calls/{call_mongo_id}/recording"


async def test_call_started_is_acknowledged_and_ignored(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(event="call_started", recording_url=None, transcript=None)
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": "call_started"}

    # Untouched — call_started does no re-hosting work at all.
    unchanged = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert unchanged is not None
    assert unchanged.status == CallStatus.REGISTERED
    assert fake_storage.uploads == []


async def test_missing_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    body = json.dumps(_post_call_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_invalid_signature_is_401(client: AsyncClient, db: MongoDB) -> None:
    body = json.dumps(_post_call_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": "not-the-real-sig"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_malformed_signature_format_missing_v_and_d_is_401(
    client: AsyncClient, db: MongoDB
) -> None:
    """A header value not shaped like "v=...,d=..." at all must be rejected
    before any HMAC comparison is attempted.
    """
    body = json.dumps(_post_call_payload()).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": "deadbeef1234"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_old_bare_hexdigest_scheme_is_now_correctly_rejected(
    client: AsyncClient, db: MongoDB
) -> None:
    """Regression test: eCareVoiceAI's older scheme (bare HMAC-SHA256
    hexdigest over the raw body only, no v=/d=/timestamp) must NOT still be
    accepted now that the real, current Retell scheme has been implemented.
    """
    body = json.dumps(_post_call_payload()).encode("utf-8")
    old_style_signature = _sign_old_scheme(body)
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": old_style_signature},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_stale_timestamp_correct_digest_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    """A digest correct for its own timestamp, but the timestamp itself is
    more than 5 minutes (300s) old, must still be rejected.
    """
    body = json.dumps(_post_call_payload()).encode("utf-8")
    stale_ts_ms = int((time.time() - 400) * 1000)  # 400s old > 300s window
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Retell-Signature": _sign(body, timestamp_ms=stale_ts_ms),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_malformed_body_is_422_not_500(client: AsyncClient, db: MongoDB) -> None:
    body = b"{not valid json"
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_unknown_call_id_handled_gracefully_not_a_crash(
    client: AsyncClient, db: MongoDB
) -> None:
    """No Calls document has this vendor_ref at all — must not crash, must
    respond 200 (there's nothing to retry for, and nowhere to put this).
    """
    payload = _post_call_payload(call_id="call_never_seen_before")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "unrecognized_call_id": True}


async def test_idempotent_redelivery_does_not_duplicate_or_corrupt(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same event delivered twice (Retell's real retry behavior on a
    non-2xx, or just a genuine duplicate) must be safe: same end state, no
    duplicated S3 objects (same deterministic key, later upload overwrites),
    no corrupted Calls record.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={"call_summary": "First delivery summary.", "user_sentiment": "Neutral"},
    )
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Retell-Signature": _sign(body)}

    resp1 = await client.post("/webhooks/retell/post-call", content=body, headers=headers)
    assert resp1.status_code == 200
    resp2 = await client.post("/webhooks/retell/post-call", content=body, headers=headers)
    assert resp2.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.status == CallStatus.COMPLETED
    assert updated.summary == "First delivery summary."
    assert updated.recording_rehost_failed is False

    # Same deterministic key uploaded twice (once per delivery) — never a
    # SEPARATE/duplicated object; the second upload overwrote the first at
    # the exact same key.
    recording_uploads = [
        u for u in fake_storage.uploads if u["key"] == f"recordings/{call_mongo_id}.wav"
    ]
    assert len(recording_uploads) == 2
    assert {u["key"] for u in fake_storage.uploads} == {
        f"recordings/{call_mongo_id}.wav",
        f"transcripts/{call_mongo_id}.txt",
    }


async def test_s3_credential_failure_produces_clean_partial_success_not_a_crash(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the real AWS_S3_BUCKET-empty-placeholder situation (storage
    genuinely fails) without making a real boto3 call. Must still mark the
    call completed with summary/sentiment/status, per the partial-success
    design (see webhooks.py's module docstring) — a storage failure must
    never look like the call itself never finished.
    """
    monkeypatch.setattr(storage_module, "_service", _FailingStorageService())
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Storage is down but call still finished.",
            "user_sentiment": "Neutral",
        },
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    # Retell still gets a clean 200 — our own storage failing is not a
    # reason to make Retell retry a webhook we successfully received.
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.status == CallStatus.COMPLETED
    assert updated.summary == "Storage is down but call still finished."
    assert updated.recording_url is None
    assert updated.transcript_url is None
    assert updated.recording_rehost_failed is True


async def test_dev_permissive_when_no_secret_configured(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETELL_API_KEY unset (rather than RETELL_WEBHOOK_SECRET, now removed)
    is the dev-permissive trigger — see app/routers/webhooks.py's module
    docstring for the re-derived fail-closed/dev-permissive contract.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "RETELL_API_KEY", "")
    assert settings.ENV != "production"

    payload = _post_call_payload(call_id="call_never_seen_before_2")
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


async def test_openapi_excludes_this_endpoint(client: AsyncClient, db: MongoDB) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert "/webhooks/retell/post-call" not in spec.get("paths", {})


# ── Call-completed notification, wired in after re-hosting (see this
# module's docstring / app/services/call_completed_webhook.py) ─────────────


async def test_call_completed_notification_fires_when_url_registered(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    captured: dict[str, Any] = {}

    async def _fake_post_once(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        captured["url"] = webhook_url
        captured["raw_body"] = raw_body
        captured["signature"] = signature
        return True, 200, None

    # Patched at the module level (never httpx.AsyncClient.post globally) —
    # the `client` fixture itself IS an httpx.AsyncClient making the real
    # ASGI-transport request in this test, so a global patch on
    # AsyncClient.post would break the test's own call into the app, not
    # just the internal relay this test wants to observe.
    monkeypatch.setattr(ccw, "_post_once", _fake_post_once)

    _, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_call_completed_webhook_url(
        db,
        platform_id,
        url="https://platformx.example.com/voiceai/call-completed",
        new_secret="test-call-completed-secret",
    )
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Caller asked about visiting hours.",
            "user_sentiment": "Positive",
        },
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    assert captured["url"] == "https://platformx.example.com/voiceai/call-completed"
    sent_payload = json.loads(captured["raw_body"])
    assert sent_payload["call_id"] == call_mongo_id
    assert sent_payload["status"] == "completed"
    assert sent_payload["summary"] == "Caller asked about visiting hours."
    assert sent_payload["sentiment"] == "Positive"
    assert sent_payload["recording_url"] == f"/calls/{call_mongo_id}/recording"
    assert sent_payload["transcript_url"] == f"/calls/{call_mongo_id}/transcript"
    assert sent_payload["direction"] == "outbound"

    # Never leaks vendor identity or our own internal field names.
    assert "retell" not in json.dumps(sent_payload).lower()
    assert "vendor" not in sent_payload
    assert "platform_id" not in sent_payload

    # Correctly signed with the platform's own secret.
    expected_signature = ccw.sign_payload(
        raw_body=captured["raw_body"], secret="test-call-completed-secret"
    )
    assert captured["signature"] == expected_signature


async def test_call_completed_notification_skipped_cleanly_when_no_url_registered(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case today: no platform has registered a
    call_completed_webhook_url yet. Must never attempt a request.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    called = {"n": 0}

    async def _should_not_be_called(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        called["n"] += 1
        return True, 200, None

    monkeypatch.setattr(ccw, "_post_once", _should_not_be_called)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(event="call_ended", call_analysis=None)
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert called["n"] == 0


async def test_call_completed_notification_fires_even_when_recording_rehost_failed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 2 from the task brief: partial data (S3 re-hosting failed)
    must still trigger a notification — Platform X should still learn the
    call finished, with whatever summary/sentiment data IS available, rather
    than being blocked entirely by an unrelated storage failure.
    """
    monkeypatch.setattr(storage_module, "_service", _FailingStorageService())
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    captured: dict[str, Any] = {}

    async def _fake_post_once(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        captured["called"] = True
        captured["raw_body"] = raw_body
        return True, 200, None

    monkeypatch.setattr(ccw, "_post_once", _fake_post_once)

    _, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_call_completed_webhook_url(
        db,
        platform_id,
        url="https://platformx.example.com/voiceai/call-completed",
        new_secret="test-call-completed-secret",
    )
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Storage is down but call still finished.",
            "user_sentiment": "Neutral",
        },
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    assert captured.get("called") is True
    sent_payload = json.loads(captured["raw_body"])
    assert sent_payload["call_id"] == call_mongo_id
    assert sent_payload["status"] == "completed"
    assert sent_payload["summary"] == "Storage is down but call still finished."
    # The recording genuinely never got re-hosted — the notification is
    # honest about that (None link), not a failure to notify at all.
    assert sent_payload["recording_url"] is None
    assert sent_payload["transcript_url"] is None


async def test_call_completed_notification_exhausts_retries_without_crashing_response(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platform X's server being fully down must never affect the response
    already sent to Retell, and must not raise/crash the background task.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)
    monkeypatch.setattr(ccw, "CALL_COMPLETED_WEBHOOK_BACKOFF_SECONDS", 0.0)

    attempts = {"n": 0}

    async def _always_down(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        attempts["n"] += 1
        return False, None, "ConnectError"

    monkeypatch.setattr(ccw, "_post_once", _always_down)

    _, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_call_completed_webhook_url(
        db,
        platform_id,
        url="https://platformx.example.com/voiceai/call-completed",
        new_secret="test-call-completed-secret",
    )
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(event="call_ended", call_analysis=None)
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    # Retell still gets a clean 200 regardless of Platform X being down.
    assert resp.status_code == 200
    assert attempts["n"] == ccw.CALL_COMPLETED_WEBHOOK_MAX_ATTEMPTS

    # The call's own re-hosting/outcome work still completed successfully —
    # a failed notification never corrupts or rolls back that work.
    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.status == CallStatus.COMPLETED


# ── Task 1: structured data extraction (custom_analysis_data -> extracted_data) ──


async def test_custom_analysis_data_flows_through_to_extracted_data(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core Task 1 regression test: a fake payload's
    call_analysis.custom_analysis_data ends up as extracted_data on the
    Calls record (repo layer) AND on the GET /calls/{id} response
    (CallPublic layer) — proving the field is wired end to end, not just
    modeled.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    fake_extracted = {"Caller Name": "Jane Doe", "Call Outcome": "Appointment booked"}
    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Caller booked an appointment.",
            "user_sentiment": "Positive",
            "custom_analysis_data": fake_extracted,
        },
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    # Repository layer.
    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.extracted_data == fake_extracted

    # CallPublic layer, via the real GET /calls/{id} endpoint (Task 2).
    get_resp = await client.get(
        f"/calls/{call_mongo_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["extracted_data"] == fake_extracted


async def test_custom_analysis_data_absent_leaves_extracted_data_none(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No custom_analysis_data on the payload (e.g. the agent had no
    structured_data_fields configured) -> extracted_data stays None, never
    an empty dict or an error.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={"call_summary": "A call.", "user_sentiment": "Neutral"},
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.extracted_data is None


async def test_call_completed_notification_includes_extracted_data(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extracted_data appears in build_call_completed_payload()'s output —
    the actual point of the feature: Platform X learns the extracted facts
    via the push notification, not just by polling GET /calls/{id}.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    captured: dict[str, Any] = {}

    async def _fake_post_once(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        captured["raw_body"] = raw_body
        return True, 200, None

    monkeypatch.setattr(ccw, "_post_once", _fake_post_once)

    _, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_call_completed_webhook_url(
        db,
        platform_id,
        url="https://platformx.example.com/voiceai/call-completed",
        new_secret="test-call-completed-secret",
    )
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    fake_extracted = {"Caller Name": "Jane Doe", "Call Outcome": "Appointment booked"}
    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Caller booked an appointment.",
            "user_sentiment": "Positive",
            "custom_analysis_data": fake_extracted,
        },
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    sent_payload = json.loads(captured["raw_body"])
    assert sent_payload["extracted_data"] == fake_extracted


# ── Inbound-call gap closed: handle_post_call creates a missing Calls
# document when no prior POST /calls/outbound ever ran for this call_id ──


async def test_post_call_for_unrecognized_call_id_with_known_agent_creates_inbound_record(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core new-behavior test: a post-call webhook for a call_id with no
    existing Calls document, where the vendor's agent_id DOES resolve to a
    real agent, must create a new Calls document (direction=inbound, correct
    platform_id/agent_id/vendor_ref/status) and proceed through the exact
    same re-hosting/notification flow every outbound call already goes
    through — GET /calls/{id} must then find it.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    captured: dict[str, Any] = {}

    async def _fake_post_once(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        captured["raw_body"] = raw_body
        return True, 200, None

    monkeypatch.setattr(ccw, "_post_once", _fake_post_once)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_call_completed_webhook_url(
        db,
        platform_id,
        url="https://platformx.example.com/voiceai/call-completed",
        new_secret="test-call-completed-secret",
    )
    agent_id = await _seed_agent(db, platform_id=platform_id)

    # Deliberately NOT seeding a Calls document — this call_id has never been
    # seen before, simulating a real inbound call nobody triggered via our
    # own POST /calls/outbound.
    payload = _post_call_payload(
        event="call_analyzed",
        call_id="call_retell_inbound_never_seen",
        call_analysis={
            "call_summary": "Caller asked about hours.",
            "user_sentiment": "Positive",
        },
    )
    payload["call"]["from_number"] = "+15559876543"
    payload["call"]["to_number"] = "+19129143920"
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    created = await call_repo.get_by_vendor_ref(db, "call_retell_inbound_never_seen")
    assert created is not None
    assert created.direction.value == "inbound"
    assert created.platform_id == platform_id
    assert created.agent_id == agent_id
    assert created.from_number == "+15559876543"
    assert created.to_number == "+19129143920"
    assert created.dynamic_variables == {}
    assert created.status == CallStatus.COMPLETED
    assert created.summary == "Caller asked about hours."
    assert created.sentiment == "Positive"
    assert created.recording_url == f"/calls/{created.id}/recording"
    assert created.transcript_url == f"/calls/{created.id}/transcript"

    # GET /calls/{id} — the actual point of the whole gap: an inbound call
    # must now be visible through the API instead of 404ing.
    get_resp = await client.get(
        f"/calls/{created.id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert get_resp.status_code == 200
    body_json = get_resp.json()
    assert body_json["direction"] == "inbound"
    assert body_json["status"] == "completed"
    assert body_json["summary"] == "Caller asked about hours."

    # Proceeded through the same re-hosting flow as any outbound call.
    uploaded_keys = {u["key"] for u in fake_storage.uploads}
    assert uploaded_keys == {
        f"recordings/{created.id}.wav",
        f"transcripts/{created.id}.txt",
    }

    # And the same call-completed notification fired, direction=inbound.
    sent_payload = json.loads(captured["raw_body"])
    assert sent_payload["call_id"] == created.id
    assert sent_payload["direction"] == "inbound"
    assert sent_payload["status"] == "completed"


async def test_post_call_for_unrecognized_call_id_and_unrecognized_agent_is_graceful(
    client: AsyncClient, db: MongoDB
) -> None:
    """No existing Calls document AND the vendor's agent_id doesn't resolve
    to any agent we recognize — there is no Platform to own a new record
    under, so none is created. Must still be a clean 200, never a crash.
    """
    payload = _post_call_payload(call_id="call_never_seen_before_and_unknown_agent")
    payload["call"]["agent_id"] = "agent_retell_totally_unknown"
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "unrecognized_call_id": True}

    assert await call_repo.get_by_vendor_ref(
        db, "call_never_seen_before_and_unknown_agent"
    ) is None


# ── voicemail-detection result (in_voicemail/disconnection_reason) — see
# app/models/call.py's module docstring for the full sourced reasoning ────


async def test_voicemail_reached_updates_call_and_get_response(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call_analyzed payload carrying call_analysis.in_voicemail=true and
    call.disconnection_reason='voicemail_reached' must correctly update the
    Calls record and appear on GET /calls/{id}.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Reached voicemail, left a message.",
            "user_sentiment": "Unknown",
            "in_voicemail": True,
        },
        disconnection_reason="voicemail_reached",
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.in_voicemail is True
    assert updated.disconnection_reason == "voicemail_reached"

    get_resp = await client.get(
        f"/calls/{call_mongo_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["in_voicemail"] is True
    assert get_body["disconnection_reason"] == "voicemail_reached"


async def test_human_answered_call_shows_in_voicemail_false_not_null(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retell's own documented example payload shows an explicit `false` (not
    an absent/omitted field) for a call a human answered — confirm this
    codebase passes that real `false` through rather than collapsing it to
    null, and that it's distinguishable from "analysis hasn't run yet."
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Caller asked about visiting hours.",
            "user_sentiment": "Positive",
            "in_voicemail": False,
        },
        disconnection_reason="user_hangup",
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.in_voicemail is False
    assert updated.disconnection_reason == "user_hangup"

    get_resp = await client.get(
        f"/calls/{call_mongo_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["in_voicemail"] is False
    assert get_body["disconnection_reason"] == "user_hangup"


async def test_call_ended_without_analysis_leaves_in_voicemail_null(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before call_analyzed arrives, in_voicemail must stay null (unknown),
    never defaulted to false — mirrors the existing summary/sentiment-stays-
    None-on-call_ended-alone test. disconnection_reason, however, lives
    directly on `call` (not inside call_analysis) so it IS available on
    call_ended alone once the vendor sends it.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    _, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_mongo_id = await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_ended", call_analysis=None, disconnection_reason="user_hangup"
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    updated = await call_repo.get_by_id(db, call_mongo_id, platform_id=platform_id)
    assert updated is not None
    assert updated.in_voicemail is None
    assert updated.disconnection_reason == "user_hangup"


async def test_voicemail_result_included_in_call_completed_notification(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """in_voicemail/disconnection_reason must appear in the call-completed
    notification payload too, same as extracted_data does — Platform X
    shouldn't have to separately poll GET /calls/{id} to learn this.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)
    monkeypatch.setattr(retell_adapter, "fetch_recording_bytes", _fake_fetch_recording)

    captured: dict[str, Any] = {}

    async def _fake_post_once(
        *, webhook_url: str, raw_body: bytes, signature: str
    ) -> tuple[bool, int | None, str | None]:
        captured["raw_body"] = raw_body
        return True, 200, None

    monkeypatch.setattr(ccw, "_post_once", _fake_post_once)

    _, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_call_completed_webhook_url(
        db,
        platform_id,
        url="https://platformx.example.com/voiceai/call-completed",
        new_secret="test-call-completed-secret",
    )
    agent_id = await _seed_agent(db, platform_id=platform_id)
    await _seed_registered_call(db, platform_id=platform_id, agent_id=agent_id)

    payload = _post_call_payload(
        event="call_analyzed",
        call_analysis={
            "call_summary": "Reached voicemail.",
            "user_sentiment": "Unknown",
            "in_voicemail": True,
        },
        disconnection_reason="voicemail_reached",
    )
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(
        "/webhooks/retell/post-call",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": _sign(body)},
    )
    assert resp.status_code == 200

    sent_payload = json.loads(captured["raw_body"])
    assert sent_payload["in_voicemail"] is True
    assert sent_payload["disconnection_reason"] == "voicemail_reached"


def test_build_call_completed_payload_includes_extracted_data_directly() -> None:
    """Unit-level version of the above, exercising
    build_call_completed_payload() directly with a hand-built CallInDB, no
    HTTP/webhook machinery involved — mirrors
    test_call_completed_webhook.py's own direct-unit-test style."""
    from datetime import UTC, datetime

    from app.models.call import CallDirection, CallInDB, CallStatus

    now = datetime.now(UTC)
    call = CallInDB(
        id="call_mongo_1",
        platform_id="platform_1",
        agent_id="agent_mongo_1",
        from_number="+19129143920",
        to_number="+15551234567",
        dynamic_variables={},
        status=CallStatus.COMPLETED,
        direction=CallDirection.OUTBOUND,
        vendor="retell",
        vendor_ref="call_retell_abc123",
        summary="Caller booked an appointment.",
        sentiment="Positive",
        extracted_data={"Caller Name": "Jane Doe", "Call Outcome": "Appointment booked"},
        created_at=now,
        updated_at=now,
    )
    payload = ccw.build_call_completed_payload(call)
    assert payload["extracted_data"] == {
        "Caller Name": "Jane Doe",
        "Call Outcome": "Appointment booked",
    }
