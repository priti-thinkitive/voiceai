"""Unit tests for app/repositories/phone_number_repo.py."""

from __future__ import annotations

from app.database import MongoDB
from app.models.phone_number import PhoneNumberInDB
from app.repositories import phone_number_repo


async def _create_sample(db: MongoDB, *, platform_id: str, agent_id: str) -> PhoneNumberInDB:
    return await phone_number_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        phone_number="+19129143920",
        area_code=912,
        nickname="Main line",
        vendor="retell",
    )


async def test_create_persists_all_fields(db: MongoDB) -> None:
    number = await _create_sample(db, platform_id="plat_1", agent_id="agent_1")

    assert number.id
    assert number.platform_id == "plat_1"
    assert number.agent_id == "agent_1"
    assert number.phone_number == "+19129143920"
    assert number.area_code == 912
    assert number.nickname == "Main line"
    assert number.vendor == "retell"
    assert number.created_at is not None
    assert number.updated_at is not None


async def test_create_with_no_area_code_or_nickname(db: MongoDB) -> None:
    number = await phone_number_repo.create(
        db,
        platform_id="plat_1",
        agent_id="agent_1",
        phone_number="+14155551234",
        area_code=None,
        nickname=None,
        vendor="retell",
    )
    assert number.area_code is None
    assert number.nickname is None


async def test_get_by_id_returns_none_for_unknown_id(db: MongoDB) -> None:
    result = await phone_number_repo.get_by_id(db, "000000000000000000000000", platform_id="plat_1")
    assert result is None


async def test_get_by_id_returns_none_for_malformed_id(db: MongoDB) -> None:
    result = await phone_number_repo.get_by_id(db, "not-a-valid-object-id", platform_id="plat_1")
    assert result is None


async def test_get_by_id_scoped_to_platform(db: MongoDB) -> None:
    number = await _create_sample(db, platform_id="plat_owner", agent_id="agent_1")

    found = await phone_number_repo.get_by_id(db, number.id, platform_id="plat_owner")
    assert found is not None
    assert found.id == number.id

    not_found = await phone_number_repo.get_by_id(db, number.id, platform_id="plat_other")
    assert not_found is None
