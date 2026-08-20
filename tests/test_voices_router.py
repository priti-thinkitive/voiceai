"""Integration tests for GET /voices and GET /voices/{voice_id}/preview.

Monkeypatches retell_adapter.list_voices()/get_voice()/fetch_preview_audio()
with real-shaped fake data rather than making a real network call to Retell
— same hard rule as test_agents_router.py (see that module's docstring): a
pytest test must never depend on ambient credential state or make an
uncontrolled real vendor call. The genuine end-to-end Retell integration is
verified live and by hand separately, outside the test suite — see
backend-dev.md's Feature status section for that evidence trail.

No cross-platform-isolation test here, deliberately — GET /voices returns
Retell's own catalog, identical for every platform, not platform-scoped
data, so that standard tenancy test doesn't apply to this endpoint.

The preview-proxy tests cover the actual vendor-identity-leak bug that was
found and fixed: `test_preview_audio_url_never_leaks_retell` is a direct
regression test asserting the raw JSON body of a GET /voices response never
contains the string "retell" (case-insensitive) anywhere — this is the
literal defect that shipped (a raw Retell S3 URL in `preview_audio_url`).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.database import MongoDB
from app.errors import AppError
from app.repositories import platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import retell_adapter


async def _seed_platform(db: MongoDB, name: str) -> str:
    """Returns the plaintext API key."""
    api_key = generate_api_key()
    await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key


# Real-shaped fake data, matching Retell's confirmed live response fields
# exactly (see retell_adapter.list_voices()'s docstring) — including one
# entry with `recommended` entirely absent, since that's a real, confirmed
# Retell behavior (absence means not-recommended, not an error).
_FAKE_VOICES: list[dict[str, Any]] = [
    {
        "voice_id": "11labs-Adrian",
        "voice_type": "standard",
        "standard_voice_type": "preset",
        "voice_name": "Adrian",
        "provider": "elevenlabs",
        "accent": "American",
        "gender": "male",
        "age": "Middle Aged",
        "avatar_url": "https://example.com/adrian.png",
        "preview_audio_url": "https://example.com/adrian.mp3",
        "recommended": True,
    },
    {
        "voice_id": "cartesia-Cleo",
        "voice_type": "standard",
        "standard_voice_type": "preset",
        "voice_name": "Cleo",
        "provider": "cartesia",
        "accent": "British",
        "gender": "female",
        "age": "Young",
        "avatar_url": "https://example.com/cleo.png",
        "preview_audio_url": "https://example.com/cleo.mp3",
        "recommended": False,
    },
    {
        "voice_id": "retell-Willa",
        "voice_type": "standard",
        "standard_voice_type": "retell",
        "voice_name": "Willa",
        "provider": "platform",
        "accent": "American",
        "gender": "female",
        "age": "Young",
        "avatar_url": "https://example.com/willa.png",
        "preview_audio_url": "https://example.com/willa.mp3",
        # `recommended` deliberately absent — a real, confirmed Retell
        # behavior; must be treated as not-recommended, not an error.
    },
]


def _patch_list_voices(monkeypatch: pytest.MonkeyPatch, voices: list[dict[str, Any]]) -> None:
    async def _fake_list_voices(settings: Any) -> list[dict[str, Any]]:
        return voices

    monkeypatch.setattr(retell_adapter, "list_voices", _fake_list_voices)


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/voices")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_list_voices_success_path(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/voices", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 3

    first = body["items"][0]
    assert first["voice_id"] == "11labs-Adrian"
    assert first["voice_name"] == "Adrian"
    assert first["provider"] == "elevenlabs"
    assert first["gender"] == "male"
    assert first["accent"] == "American"
    assert first["age"] == "Middle Aged"
    # preview_audio_url is OUR own proxy path, never Retell's raw URL — see
    # test_preview_audio_url_never_leaks_retell below for the full
    # regression test on the bug this fixes.
    assert first["preview_audio_url"] == "/voices/11labs-Adrian/preview"
    assert first["recommended"] is True
    # Retell's internal categorization fields never leak into our response.
    assert "voice_type" not in first
    assert "standard_voice_type" not in first
    assert "avatar_url" not in first

    # Entry with `recommended` absent from Retell is treated as False.
    willa = next(v for v in body["items"] if v["voice_id"] == "retell-Willa")
    assert willa["recommended"] is False


async def test_pagination_limit_and_offset(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices",
        params={"limit": 1, "offset": 1},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 3
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["voice_id"] == "cartesia-Cleo"


async def test_limit_is_capped_at_max(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices",
        params={"limit": 500},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_filter_by_provider(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices",
        params={"provider": "cartesia"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert all(v["provider"] == "cartesia" for v in body["items"])


async def test_filter_by_gender(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices",
        params={"gender": "female"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 2
    assert all(v["gender"] == "female" for v in body["items"])


async def test_filter_by_recommended(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices",
        params={"recommended": "true"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert body["items"][0]["voice_id"] == "11labs-Adrian"


async def test_combined_filters(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list_voices(monkeypatch, _FAKE_VOICES)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices",
        params={"gender": "female", "recommended": "false"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 2
    assert all(v["gender"] == "female" and v["recommended"] is False for v in body["items"])


async def test_vendor_failure_returns_upstream_failed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _failing_list_voices(settings: Any) -> list[dict[str, Any]]:
        raise AppError(
            code="upstream_failed",
            message="Could not reach the voice vendor to list voices. Try again shortly.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "list_voices", _failing_list_voices)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/voices", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_failed"
    assert detail["request_id"]
    assert "httpx" not in detail["message"].lower()
    assert "traceback" not in detail["message"].lower()


async def test_preview_audio_url_never_leaks_retell(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the real, live vendor-identity leak: GET /voices'
    `preview_audio_url` used to be Retell's raw S3 URL verbatim (the literal
    string "retell" right in the URL domain, e.g.
    "https://retell-utils-public.s3.us-west-2.amazonaws.com/...").

    Uses real-shaped fake data that deliberately includes a "retell-"
    prefixed `voice_id` (a legitimate real Retell catalog naming convention
    for its own curated "platform" voices, e.g. "retell-Cimo" — confirmed in
    vendor-docs/Retell.md) alongside a raw Retell-domain
    `preview_audio_url`, to prove the fix is specific to the actual leak
    (the vendor's URL/domain appearing in a response) and not merely
    "the substring retell never appears" — a `voice_id` value is legitimate
    Platform-X-visible catalog data, not an implementation-detail leak, even
    when it happens to contain "retell".
    """
    leaky_voices: list[dict[str, Any]] = [
        {
            "voice_id": "retell-Cimo",
            "voice_name": "Cimo",
            "provider": "platform",
            "accent": "American",
            "gender": "male",
            "age": "Young",
            "preview_audio_url": (
                "https://retell-utils-public.s3.us-west-2.amazonaws.com/cimo.mp3"
            ),
            "recommended": True,
        },
    ]
    _patch_list_voices(monkeypatch, leaky_voices)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get("/voices", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200
    # The vendor's own S3/domain string must never appear in the response,
    # regardless of it being present in the underlying Retell data we fetched.
    assert "retell-utils-public" not in resp.text.lower()
    assert "s3" not in resp.text.lower()
    assert "amazonaws" not in resp.text.lower()

    body = resp.json()
    for voice in body["items"]:
        assert voice["preview_audio_url"] == f"/voices/{voice['voice_id']}/preview"
        assert voice["preview_audio_url"].startswith("/voices/")


# Real-shaped single-voice response, matching retell_adapter.get_voice()'s
# confirmed real schema (same fields as one /list-voices entry).
_FAKE_VOICE_DETAIL: dict[str, Any] = {
    "voice_id": "cartesia-Cleo",
    "voice_type": "standard",
    "standard_voice_type": "preset",
    "voice_name": "Cleo",
    "provider": "cartesia",
    "accent": "American",
    "gender": "female",
    "age": "Middle Aged",
    "avatar_url": "https://example.com/Cleo.png",
    "preview_audio_url": "https://example.com/cartesia-cleo-real.mp3",
    "recommended": False,
}

_FAKE_AUDIO_BYTES = b"ID3-fake-mp3-bytes-not-real-audio-but-non-trivial-size" * 20


async def test_voice_preview_success_streams_audio(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_get_voice(settings: Any, *, voice_id: str) -> dict[str, Any]:
        assert voice_id == "cartesia-Cleo"
        return _FAKE_VOICE_DETAIL

    async def _fake_fetch_preview_audio(settings: Any, *, audio_url: str) -> tuple[bytes, str]:
        assert audio_url == _FAKE_VOICE_DETAIL["preview_audio_url"]
        return _FAKE_AUDIO_BYTES, "audio/mpeg"

    monkeypatch.setattr(retell_adapter, "get_voice", _fake_get_voice)
    monkeypatch.setattr(retell_adapter, "fetch_preview_audio", _fake_fetch_preview_audio)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices/cartesia-Cleo/preview", headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == _FAKE_AUDIO_BYTES
    assert len(resp.content) > 0


async def test_voice_preview_unknown_voice_id_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_get_voice_not_found(settings: Any, *, voice_id: str) -> dict[str, Any]:
        raise AppError(
            code="not_found",
            message="No voice exists with that voice_id.",
            status_code=404,
            field="voice_id",
        )

    monkeypatch.setattr(retell_adapter, "get_voice", _fake_get_voice_not_found)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices/not-a-real-voice/preview", headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "not_found"
    assert detail["field"] == "voice_id"


async def test_voice_preview_vendor_failure_returns_upstream_failed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_get_voice_ok(settings: Any, *, voice_id: str) -> dict[str, Any]:
        return _FAKE_VOICE_DETAIL

    async def _fake_fetch_preview_audio_failing(
        settings: Any, *, audio_url: str
    ) -> tuple[bytes, str]:
        raise AppError(
            code="upstream_failed",
            message="Could not reach the voice vendor to fetch preview audio. "
            "Try again shortly.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "get_voice", _fake_get_voice_ok)
    monkeypatch.setattr(retell_adapter, "fetch_preview_audio", _fake_fetch_preview_audio_failing)
    api_key = await _seed_platform(db, "Platform A")

    resp = await client.get(
        "/voices/cartesia-Cleo/preview", headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_failed"
    assert detail["request_id"]
    assert "httpx" not in detail["message"].lower()


async def test_voice_preview_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/voices/cartesia-Cleo/preview")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"
