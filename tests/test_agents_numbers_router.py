"""Integration tests for POST /agents/{agent_id}/numbers.

Same monkeypatching discipline as test_agents_router.py: never make a real
network call to Retell from pytest. The success/vendor-failure paths here
monkeypatch retell_adapter.create_phone_number directly, and agent setup
monkeypatches retell_adapter.create_agent so seeding an "active" agent to
attach a number to also never touches the network. The one real, live,
manually-run end-to-end check against the real Retell account (buy a real
number, confirm it, then release it) was done separately outside the test
suite — see backend-dev.md's Feature status section for that evidence.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.collections import PHONE_NUMBERS
from app.database import MongoDB
from app.errors import AppError
from app.models.agent import AgentStatus
from app.models.language import Language
from app.repositories import agent_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import retell_adapter

_AGENT_PAYLOAD: dict[str, Any] = {
    "prompt": "You are a friendly front-desk assistant for Aspen Quality Care.",
    "voice_id": "11labs-Adrian",
}


async def _seed_platform(db: MongoDB, name: str) -> tuple[str, str]:
    """Returns (plaintext_api_key, platform_id)."""
    api_key = generate_api_key()
    platform = await platform_repo.create(
        db,
        name=name,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_display_prefix(api_key),
    )
    return api_key, platform.id


async def _seed_active_agent(db: MongoDB, *, platform_id: str, vendor_ref: str) -> str:
    """Bypasses the router/vendor call entirely — writes an "active" Agents
    doc directly via the repository, exactly like a successful POST /agents
    would have persisted. Returns our own agent id.
    """
    agent = await agent_repo.create(
        db,
        platform_id=platform_id,
        prompt=_AGENT_PAYLOAD["prompt"],
        voice_id=_AGENT_PAYLOAD["voice_id"],
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[],
        status=AgentStatus.ACTIVE,
        vendor=retell_adapter.VENDOR_NAME,
        vendor_ref=vendor_ref,
    )
    return agent.id


async def _seed_failed_agent(db: MongoDB, *, platform_id: str) -> str:
    agent = await agent_repo.create(
        db,
        platform_id=platform_id,
        prompt=_AGENT_PAYLOAD["prompt"],
        voice_id=_AGENT_PAYLOAD["voice_id"],
        languages=[Language.EN_US],
        voice_speed=1.0,
        interruption_sensitivity=1.0,
        enable_backchannel=True,
        pronunciation_dictionary=[],
        status=AgentStatus.FAILED,
        vendor=retell_adapter.VENDOR_NAME,
        vendor_ref=None,
    )
    return agent.id


def _fake_create_phone_number(
    *, phone_number: str = "+19129143920", area_code: int | None = 912, nickname: str | None = None
) -> Any:
    async def _fake(settings: Any, **kwargs: Any) -> retell_adapter.RetellCreatePhoneNumberResult:
        return retell_adapter.RetellCreatePhoneNumberResult(
            phone_number=phone_number, area_code=area_code, nickname=nickname
        )

    return _fake


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.post("/agents/000000000000000000000000/numbers", json={})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_success_path(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter,
        "create_phone_number",
        _fake_create_phone_number(nickname="Main line"),
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.post(
        f"/agents/{agent_id}/numbers",
        json={"area_code": 415, "nickname": "Main line"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["phone_number"] == "+19129143920"
    assert body["area_code"] == 912
    assert body["nickname"] == "Main line"
    assert body["agent_id"] == agent_id
    assert "created_at" in body
    # Never leak vendor identity or Retell-internal fields.
    assert "vendor" not in body
    assert "vendor_ref" not in body
    assert "phone_number_pretty" not in body
    assert "sip_outbound_trunk_config" not in body

    docs = await db[PHONE_NUMBERS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["agent_id"] == agent_id
    assert docs[0]["phone_number"] == "+19129143920"
    assert docs[0]["vendor"] == "retell"


async def test_empty_body_uses_retell_defaults(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every field is optional — an empty JSON body must still succeed."""
    monkeypatch.setattr(retell_adapter, "create_phone_number", _fake_create_phone_number())

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.post(
        f"/agents/{agent_id}/numbers",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201


async def test_agent_not_found_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_adapter, "create_phone_number", _fake_create_phone_number())
    api_key, _ = await _seed_platform(db, "Platform A")

    resp = await client.post(
        "/agents/000000000000000000000000/numbers",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_agent_belongs_to_another_platform_is_404_not_403(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenancy isolation: cross-platform access to a single record is
    disguised as 404, never 403, per the standards doc.
    """
    monkeypatch.setattr(retell_adapter, "create_phone_number", _fake_create_phone_number())

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")

    resp = await client.post(
        f"/agents/{agent_a_id}/numbers",
        json={},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"

    # The owning platform can still buy a number for its own agent.
    resp_owner = await client.post(
        f"/agents/{agent_a_id}/numbers",
        json={},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp_owner.status_code == 201

    # No PhoneNumbers doc was created under Platform B.
    docs_b = await db[PHONE_NUMBERS].find({"platform_id": id_b}).to_list(length=10)
    assert docs_b == []


async def test_agent_status_failed_is_rejected_with_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent whose own vendor creation failed has no real Retell agent_id
    to bind a number to — must be rejected before ever calling Retell.
    """
    called = False

    async def _should_not_be_called(settings: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("create_phone_number must not be called for a failed agent")

    monkeypatch.setattr(retell_adapter, "create_phone_number", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_failed_agent(db, platform_id=platform_id)

    resp = await client.post(
        f"/agents/{agent_id}/numbers",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert not called


async def test_vendor_failure_returns_upstream_failed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _failing_create_phone_number(settings: Any, **kwargs: Any) -> Any:
        raise AppError(
            code="upstream_failed",
            message="Could not reach the voice vendor to buy a phone number. Try again shortly.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "create_phone_number", _failing_create_phone_number)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.post(
        f"/agents/{agent_id}/numbers",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_failed"
    assert detail["request_id"]
    assert "httpx" not in detail["message"].lower()

    # No PhoneNumbers doc persisted on a failed purchase — nothing was
    # actually bought, so unlike Agent's persist-on-failure design there is
    # no stable id worth keeping for a retry.
    docs = await db[PHONE_NUMBERS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs == []
