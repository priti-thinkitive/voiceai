"""Unit tests for app/repositories/agent_repo.py."""

from __future__ import annotations

from app.database import MongoDB
from app.models.agent import AgentInDB, AgentStatus, PronunciationEntry
from app.models.language import Language
from app.repositories import agent_repo


async def _create_sample(db: MongoDB, *, platform_id: str) -> AgentInDB:
    return await agent_repo.create(
        db,
        platform_id=platform_id,
        prompt="You are a helpful front-desk assistant.",
        voice_id="11labs-Adrian",
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[
            PronunciationEntry(word="Aspen", pronunciation="AS-pen"),
        ],
        status=AgentStatus.ACTIVE,
        vendor="retell",
        vendor_ref="agent_abc123",
    )


async def test_create_persists_all_fields_including_internal_ones(db: MongoDB) -> None:
    agent = await _create_sample(db, platform_id="plat_1")

    assert agent.id
    assert agent.platform_id == "plat_1"
    assert agent.prompt == "You are a helpful front-desk assistant."
    assert agent.voice_id == "11labs-Adrian"
    assert agent.languages == [Language.EN_US]
    assert agent.voice_speed == 1.0
    assert agent.interruption_sensitivity == 1.0
    assert agent.enable_backchannel is True
    assert agent.pronunciation_dictionary == [
        PronunciationEntry(word="Aspen", pronunciation="AS-pen")
    ]
    assert agent.status == AgentStatus.ACTIVE
    # Internal-only fields — stored on AgentInDB, must never appear on AgentPublic.
    assert agent.vendor == "retell"
    assert agent.vendor_ref == "agent_abc123"
    assert agent.created_at is not None
    assert agent.updated_at is not None


async def test_get_by_id_returns_none_for_unknown_id(db: MongoDB) -> None:
    result = await agent_repo.get_by_id(db, "000000000000000000000000", platform_id="plat_1")
    assert result is None


async def test_get_by_id_returns_none_for_malformed_id(db: MongoDB) -> None:
    result = await agent_repo.get_by_id(db, "not-a-valid-object-id", platform_id="plat_1")
    assert result is None


async def test_get_by_id_scoped_to_platform(db: MongoDB) -> None:
    agent = await _create_sample(db, platform_id="plat_owner")

    # The owning platform can fetch it.
    found = await agent_repo.get_by_id(db, agent.id, platform_id="plat_owner")
    assert found is not None
    assert found.id == agent.id

    # A different platform gets None, not the record — repository-level
    # tenancy scoping, independent of the router's 404-vs-403 translation.
    not_found = await agent_repo.get_by_id(db, agent.id, platform_id="plat_other")
    assert not_found is None


async def test_create_with_empty_pronunciation_dictionary_default(db: MongoDB) -> None:
    agent = await agent_repo.create(
        db,
        platform_id="plat_1",
        prompt="Prompt.",
        voice_id="11labs-Adrian",
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[],
        status=AgentStatus.FAILED,
        vendor="retell",
        vendor_ref=None,
    )
    assert agent.pronunciation_dictionary == []
    assert agent.status == AgentStatus.FAILED
    assert agent.vendor_ref is None
