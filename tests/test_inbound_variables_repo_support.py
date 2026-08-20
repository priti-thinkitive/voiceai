"""Unit tests for the repository additions inbound dynamic-variable
injection depends on:
  - platform_repo.set_inbound_variables_webhook_url (the only write path for
    Platform.inbound_variables_webhook_url, since no real onboarding API
    exists for this field yet — see PlatformInDB's docstring).
  - phone_number_repo.get_by_phone_number_any_platform (the tenancy-
    resolving lookup the inbound webhook handler uses, deliberately not
    scoped to a caller-supplied platform_id).
"""

from __future__ import annotations

from app.database import MongoDB
from app.repositories import phone_number_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix


async def _seed_platform(db: MongoDB, name: str) -> str:
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return platform.id


async def test_new_platform_has_no_webhook_url_by_default(db: MongoDB) -> None:
    platform_id = await _seed_platform(db, "Platform A")
    platform = await platform_repo.get_by_id(db, platform_id)
    assert platform is not None
    assert platform.inbound_variables_webhook_url is None


async def test_set_inbound_variables_webhook_url_persists(db: MongoDB) -> None:
    platform_id = await _seed_platform(db, "Platform A")

    ok = await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://platformx.example.com/voiceai/variables"
    )
    assert ok is True

    platform = await platform_repo.get_by_id(db, platform_id)
    assert platform is not None
    assert (
        platform.inbound_variables_webhook_url == "https://platformx.example.com/voiceai/variables"
    )


async def test_set_inbound_variables_webhook_url_can_clear_it(db: MongoDB) -> None:
    platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://platformx.example.com/voiceai/variables"
    )

    await platform_repo.set_inbound_variables_webhook_url(db, platform_id, url=None)

    platform = await platform_repo.get_by_id(db, platform_id)
    assert platform is not None
    assert platform.inbound_variables_webhook_url is None


async def test_set_inbound_variables_webhook_url_unknown_platform_returns_false(
    db: MongoDB,
) -> None:
    ok = await platform_repo.set_inbound_variables_webhook_url(
        db, "000000000000000000000000", url="https://platformx.example.com/voiceai/variables"
    )
    assert ok is False


async def test_get_by_phone_number_any_platform_finds_number_regardless_of_owner(
    db: MongoDB,
) -> None:
    platform_id = await _seed_platform(db, "Platform A")
    await phone_number_repo.create(
        db,
        platform_id=platform_id,
        agent_id="agent_1",
        phone_number="+19129143920",
        area_code=912,
        nickname=None,
        vendor="retell",
    )

    found = await phone_number_repo.get_by_phone_number_any_platform(db, "+19129143920")
    assert found is not None
    assert found.platform_id == platform_id


async def test_get_by_phone_number_any_platform_returns_none_for_unknown_number(
    db: MongoDB,
) -> None:
    found = await phone_number_repo.get_by_phone_number_any_platform(db, "+10000000000")
    assert found is None
