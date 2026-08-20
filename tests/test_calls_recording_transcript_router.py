"""Integration tests for GET /calls/{call_id}/recording and
GET /calls/{call_id}/transcript — the serving side of post-call re-hosting.

`storage.get_storage_service()`'s underlying service is monkeypatched to a
fake in-memory store — never a real S3 call, per the standards doc's hard
no-real-network-calls-in-tests rule (extended to AWS/S3 per this task).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.database import MongoDB
from app.models.agent import AgentStatus
from app.models.call import CallStatus
from app.models.language import Language
from app.repositories import agent_repo, call_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import storage as storage_module
from app.services.retell_adapter import VENDOR_NAME


class _FakeStorageService:
    def __init__(self) -> None:
        self.store: dict[str, tuple[bytes, str]] = {}

    async def upload(self, *, key: str, content: bytes, content_type: str, settings: Any) -> None:
        del settings
        self.store[key] = (content, content_type)

    async def download(self, *, key: str, settings: Any) -> tuple[bytes, str]:
        del settings
        if key not in self.store:
            from app.errors import AppError

            raise AppError(code="storage_failed", message="not found", status_code=502)
        return self.store[key]


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


async def _seed_completed_call(
    db: MongoDB, *, platform_id: str, agent_id: str, vendor_ref: str = "call_retell_1"
) -> str:
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
    await call_repo.update_post_call_outcome(
        db,
        call.id,
        status=CallStatus.COMPLETED,
        recording_url=f"/calls/{call.id}/recording",
        transcript_url=f"/calls/{call.id}/transcript",
        summary="A summary.",
        sentiment="Positive",
        extracted_data=None,
        recording_rehost_failed=False,
    )
    return call.id


async def test_recording_success_streams_audio(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_id = await _seed_completed_call(db, platform_id=platform_id, agent_id=agent_id)
    fake_storage.store[f"recordings/{call_id}.wav"] = (b"FAKE_AUDIO_BYTES", "audio/wav")

    resp = await client.get(
        f"/calls/{call_id}/recording", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
    assert resp.content == b"FAKE_AUDIO_BYTES"
    assert resp.headers["content-type"] == "audio/wav"


async def test_transcript_success_streams_text(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call_id = await _seed_completed_call(db, platform_id=platform_id, agent_id=agent_id)
    fake_storage.store[f"transcripts/{call_id}.txt"] = (
        b"Agent: Hello.",
        "text/plain; charset=utf-8",
    )

    resp = await client.get(
        f"/calls/{call_id}/transcript", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
    assert resp.content == b"Agent: Hello."


async def test_recording_unknown_call_id_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "_service", _FakeStorageService())
    api_key, _ = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/calls/000000000000000000000000/recording",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_recording_cross_platform_access_is_404_not_403(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory tenancy-isolation test — a call recording is real,
    potentially PII-carrying platform-owned data; Platform B must never
    fetch Platform A's call recording, even with a valid API key.
    """
    fake_storage = _FakeStorageService()
    monkeypatch.setattr(storage_module, "_service", fake_storage)

    _, platform_a_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_a_id)
    call_id = await _seed_completed_call(db, platform_id=platform_a_id, agent_id=agent_id)
    fake_storage.store[f"recordings/{call_id}.wav"] = (b"SECRET_AUDIO", "audio/wav")

    api_key_b, _ = await _seed_platform(db, "Platform B")

    resp = await client.get(
        f"/calls/{call_id}/recording", headers={"Authorization": f"Bearer {api_key_b}"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_recording_missing_auth_is_401(client: AsyncClient, db: MongoDB) -> None:
    resp = await client.get("/calls/000000000000000000000000/recording")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_recording_not_yet_rehosted_is_handled_not_a_crash(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call exists and belongs to the caller, but nothing has been
    uploaded to that key yet (e.g. call still in progress, or re-hosting
    hasn't run) — must be a clean, handled error, never a raw crash or a
    corrupt/empty body. The fake storage here mirrors the real
    S3StorageService's behavior of raising AppError(storage_failed) for a
    missing key (see storage.py's download() docstring).
    """
    monkeypatch.setattr(storage_module, "_service", _FakeStorageService())

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_agent(db, platform_id=platform_id)
    call = await call_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        from_number="+19129143920",
        to_number="+15551234567",
        dynamic_variables={},
        status=CallStatus.REGISTERED,
        vendor=VENDOR_NAME,
        vendor_ref="call_never_finished",
    )

    resp = await client.get(
        f"/calls/{call.id}/recording", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "storage_failed"
