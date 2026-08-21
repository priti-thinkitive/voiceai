"""Integration tests for Tier 1's GET-single/GET-list/DELETE agent endpoints
(GET /agents/{id}, GET /agents, GET /agents/{id}/numbers, DELETE
/agents/{id}, DELETE /agents/{id}/numbers/{phone_number}) — see
vendor-docs/Phase1-Status-Report.html's Tier 1 table for the exact gap this
closes and backend-dev.md's Feature status section for the full evidence
trail (including real live-verification against the real vendor account for
the DELETE endpoints, which cannot be reproduced here).

Same monkeypatching discipline as every other router's tests in this
codebase: never make a real network call to the voice vendor from pytest.
Agent/number setup writes repository records directly (or goes through the
already-tested POST /agents/... endpoints with their own adapters
monkeypatched), exactly like a real successful creation would have
persisted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from app.collections import PHONE_NUMBERS
from app.database import MongoDB
from app.models.agent import AgentStatus, ResponseEngine
from app.models.language import Language
from app.repositories import agent_repo, phone_number_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import retell_adapter, retell_agent_adapter

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


async def _seed_active_agent(
    db: MongoDB,
    *,
    platform_id: str,
    vendor_ref: str,
    llm_ref: str | None = None,
    response_engine: ResponseEngine = ResponseEngine.BUILTIN,
) -> str:
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
        llm_ref=llm_ref,
        response_engine=response_engine,
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


async def _seed_owned_number(
    db: MongoDB, *, platform_id: str, agent_id: str, phone_number: str = "+19129143920"
) -> None:
    await phone_number_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        phone_number=phone_number,
        area_code=912,
        nickname=None,
        vendor=retell_adapter.VENDOR_NAME,
    )


def _no_call_allowed(name: str) -> Any:
    async def _fake(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{name} should never be called for this scenario")

    return _fake


# ── GET /agents/{agent_id} ──────────────────────────────────────────────


async def test_get_agent_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/agents/000000000000000000000000")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_get_agent_success_matches_create_response_shape(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.get(f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == agent_id
    assert body["platform_id"] == platform_id
    assert body["prompt"] == _AGENT_PAYLOAD["prompt"]
    assert body["voice_id"] == _AGENT_PAYLOAD["voice_id"]
    assert body["status"] == "active"
    # Never leak vendor identity.
    assert "vendor" not in body
    assert "vendor_ref" not in body
    assert "llm_ref" not in body


async def test_get_agent_unknown_id_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.get(
        "/agents/000000000000000000000000", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_get_agent_cross_platform_access_is_404_not_403(
    client: AsyncClient, db: MongoDB
) -> None:
    """Mandatory per the standards doc: cross-platform access to a single
    record is disguised as 404, never 403.
    """
    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")

    resp = await client.get(f"/agents/{agent_a_id}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"

    resp_owner = await client.get(
        f"/agents/{agent_a_id}", headers={"Authorization": f"Bearer {key_a}"}
    )
    assert resp_owner.status_code == 200
    assert id_a != id_b


# ── GET /agents ──────────────────────────────────────────────────────────


async def test_list_agents_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/agents")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_list_agents_empty_for_new_platform(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.get("/agents", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total_count": 0, "limit": 20, "offset": 0}


async def test_list_agents_success_newest_first(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_ids = []
    for i in range(3):
        agent_ids.append(
            await _seed_active_agent(db, platform_id=platform_id, vendor_ref=f"agent_retell_{i}")
        )
        # A tiny real delay between inserts — `created_at` is only
        # millisecond-resolution `datetime.now(UTC)`, and three rapid-fire
        # inserts in the same test can otherwise tie, making "newest first"
        # genuinely ambiguous rather than actually wrong. This proves real
        # sort-by-created_at behavior without being flaky.
        await asyncio.sleep(0.01)

    resp = await client.get("/agents", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 3
    assert len(body["items"]) == 3
    assert {item["id"] for item in body["items"]} == set(agent_ids)
    assert [item["id"] for item in body["items"]] == list(reversed(agent_ids))
    for item in body["items"]:
        assert "vendor" not in item
        assert "vendor_ref" not in item


async def test_list_agents_pagination_limit_and_offset(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    for i in range(5):
        await _seed_active_agent(db, platform_id=platform_id, vendor_ref=f"agent_retell_{i}")

    resp = await client.get(
        "/agents", params={"limit": 2, "offset": 1}, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2


async def test_list_agents_limit_over_cap_is_422(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.get(
        "/agents", params={"limit": 101}, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422


async def test_list_agents_tenancy_isolation(client: AsyncClient, db: MongoDB) -> None:
    """Mandatory per the standards doc: Platform A never sees Platform B's
    agents in a list response.
    """
    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")

    resp_b = await client.get("/agents", headers={"Authorization": f"Bearer {key_b}"})
    assert resp_b.status_code == 200
    assert resp_b.json() == {"items": [], "total_count": 0, "limit": 20, "offset": 0}

    resp_a = await client.get("/agents", headers={"Authorization": f"Bearer {key_a}"})
    assert resp_a.status_code == 200
    assert resp_a.json()["total_count"] == 1
    assert id_a != id_b


# ── GET /agents/{agent_id}/numbers ──────────────────────────────────────


async def test_list_agent_numbers_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/agents/000000000000000000000000/numbers")
    assert resp.status_code == 401


async def test_list_agent_numbers_unknown_agent_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.get(
        "/agents/000000000000000000000000/numbers",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_list_agent_numbers_empty_when_none_bound(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    resp = await client.get(
        f"/agents/{agent_id}/numbers", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


async def test_list_agent_numbers_success(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143920"
    )
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143921"
    )

    resp = await client.get(
        f"/agents/{agent_id}/numbers", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    numbers = {item["phone_number"] for item in body["items"]}
    assert numbers == {"+19129143920", "+19129143921"}
    for item in body["items"]:
        assert item["agent_id"] == agent_id


async def test_list_agent_numbers_cross_platform_is_404_not_403(
    client: AsyncClient, db: MongoDB
) -> None:
    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    await _seed_owned_number(db, platform_id=id_a, agent_id=agent_a_id)

    resp = await client.get(
        f"/agents/{agent_a_id}/numbers", headers={"Authorization": f"Bearer {key_b}"}
    )
    assert resp.status_code == 404
    assert id_a != id_b


async def test_list_agent_numbers_only_shows_numbers_for_this_agent(
    client: AsyncClient, db: MongoDB
) -> None:
    """A platform's own second agent's numbers must not leak into this
    agent's list, even though both belong to the same platform.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_1_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    agent_2_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_2")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_1_id, phone_number="+19129143920"
    )
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_2_id, phone_number="+19129143921"
    )

    resp = await client.get(
        f"/agents/{agent_1_id}/numbers", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["phone_number"] == "+19129143920"


# ── DELETE /agents/{agent_id} ────────────────────────────────────────────


async def test_delete_agent_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.delete("/agents/000000000000000000000000")
    assert resp.status_code == 401


async def test_delete_agent_unknown_id_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.delete(
        "/agents/000000000000000000000000", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_delete_agent_cross_platform_is_404_not_403(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_agent_adapter, "delete_retell_llm", _no_call_allowed("delete_retell_llm")
    )
    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _no_call_allowed("delete_agent"))

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(
        db, platform_id=id_a, vendor_ref="agent_retell_a", llm_ref="llm_retell_a"
    )

    resp = await client.delete(
        f"/agents/{agent_a_id}", headers={"Authorization": f"Bearer {key_b}"}
    )
    assert resp.status_code == 404
    assert id_a != id_b

    # Confirm it's genuinely untouched.
    still_there = await agent_repo.get_by_id(db, agent_a_id, platform_id=id_a)
    assert still_there is not None


async def test_delete_agent_builtin_mode_deletes_agent_and_llm(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `builtin`-mode agent has a real llm_ref — deleting it must call
    delete_retell_llm() before delete_agent(), per this endpoint's own
    documented ordering, and remove our own record only after both vendor
    calls succeed.
    """
    calls: list[str] = []

    async def _fake_delete_llm(settings: Any, *, llm_id: str) -> None:
        calls.append(f"delete_retell_llm:{llm_id}")

    async def _fake_delete_agent(settings: Any, *, agent_id: str) -> None:
        calls.append(f"delete_agent:{agent_id}")

    monkeypatch.setattr(retell_agent_adapter, "delete_retell_llm", _fake_delete_llm)
    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _fake_delete_agent)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(
        db,
        platform_id=platform_id,
        vendor_ref="agent_retell_1",
        llm_ref="llm_retell_1",
        response_engine=ResponseEngine.BUILTIN,
    )

    resp = await client.delete(
        f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 204
    assert resp.content == b""

    # LLM deleted before the agent object, per the documented ordering.
    assert calls == ["delete_retell_llm:llm_retell_1", "delete_agent:agent_retell_1"]

    gone = await agent_repo.get_by_id(db, agent_id, platform_id=platform_id)
    assert gone is None


async def test_delete_agent_custom_mode_skips_llm_delete(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `custom`-mode agent has no llm_ref at all — delete_retell_llm must
    never be called for it.
    """
    calls: list[str] = []

    async def _fake_delete_agent(settings: Any, *, agent_id: str) -> None:
        calls.append(f"delete_agent:{agent_id}")

    monkeypatch.setattr(
        retell_agent_adapter, "delete_retell_llm", _no_call_allowed("delete_retell_llm")
    )
    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _fake_delete_agent)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(
        db,
        platform_id=platform_id,
        vendor_ref="agent_retell_custom",
        llm_ref=None,
        response_engine=ResponseEngine.CUSTOM,
    )

    resp = await client.delete(
        f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 204
    assert calls == ["delete_agent:agent_retell_custom"]


async def test_delete_agent_unbinds_and_releases_bound_numbers_first(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core product decision under test: deleting an agent must release
    every bound phone number FIRST (real vendor delete + local record
    removal), never silently orphan it — see this endpoint's own docstring
    in app/routers/agents.py for the real, live-observed vendor behavior
    that motivated this.
    """
    calls: list[str] = []

    async def _fake_delete_number(settings: Any, *, phone_number: str) -> None:
        calls.append(f"delete_phone_number:{phone_number}")

    async def _fake_delete_llm(settings: Any, *, llm_id: str) -> None:
        calls.append(f"delete_retell_llm:{llm_id}")

    async def _fake_delete_agent(settings: Any, *, agent_id: str) -> None:
        calls.append(f"delete_agent:{agent_id}")

    monkeypatch.setattr(retell_adapter, "delete_phone_number", _fake_delete_number)
    monkeypatch.setattr(retell_agent_adapter, "delete_retell_llm", _fake_delete_llm)
    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _fake_delete_agent)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(
        db, platform_id=platform_id, vendor_ref="agent_retell_1", llm_ref="llm_retell_1"
    )
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143920"
    )
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143921"
    )

    resp = await client.delete(
        f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 204

    # Both numbers released before the LLM/agent object.
    number_calls = [c for c in calls if c.startswith("delete_phone_number")]
    assert set(number_calls) == {
        "delete_phone_number:+19129143920",
        "delete_phone_number:+19129143921",
    }
    assert calls.index(number_calls[0]) < calls.index("delete_retell_llm:llm_retell_1")
    assert calls.index(number_calls[1]) < calls.index("delete_retell_llm:llm_retell_1")

    remaining_numbers = await phone_number_repo.list_by_agent_id(
        db, agent_id, platform_id=platform_id
    )
    assert remaining_numbers == []

    docs = await db[PHONE_NUMBERS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs == []


async def test_delete_agent_failed_status_skips_vendor_calls(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `status='failed'` agent has no real vendor-side object (see POST
    /agents' persist-on-vendor-failure precedent) — deleting it must not
    attempt any vendor call, only remove our own local record.
    """
    monkeypatch.setattr(
        retell_agent_adapter, "delete_retell_llm", _no_call_allowed("delete_retell_llm")
    )
    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _no_call_allowed("delete_agent"))
    monkeypatch.setattr(
        retell_adapter, "delete_phone_number", _no_call_allowed("delete_phone_number")
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_failed_agent(db, platform_id=platform_id)

    resp = await client.delete(
        f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 204

    gone = await agent_repo.get_by_id(db, agent_id, platform_id=platform_id)
    assert gone is None


async def test_delete_agent_vendor_failure_leaves_record_and_raises_502(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a real vendor-side delete call fails partway through, this must
    raise the real upstream_failed/502 contract and leave our own record in
    place — a failed delete is always safe to simply retry.
    """
    from app.errors import CODE_UPSTREAM_FAILED, AppError

    async def _fake_delete_agent_fails(settings: Any, *, agent_id: str) -> None:
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to delete the agent.",
            status_code=502,
        )

    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _fake_delete_agent_fails)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(
        db,
        platform_id=platform_id,
        vendor_ref="agent_retell_1",
        llm_ref=None,
        response_engine=ResponseEngine.CUSTOM,
    )

    resp = await client.delete(
        f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "upstream_failed"

    still_there = await agent_repo.get_by_id(db, agent_id, platform_id=platform_id)
    assert still_there is not None


async def test_delete_agent_leaves_call_history_untouched(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an agent must not touch any Calls history for it — a past
    call is a historical fact, not a live reference to the agent.
    """
    from app.models.call import CallStatus
    from app.repositories import call_repo

    async def _fake_delete_agent(settings: Any, *, agent_id: str) -> None:
        return None

    monkeypatch.setattr(
        retell_agent_adapter, "delete_retell_llm", _no_call_allowed("delete_retell_llm")
    )
    monkeypatch.setattr(retell_agent_adapter, "delete_agent", _fake_delete_agent)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(
        db,
        platform_id=platform_id,
        vendor_ref="agent_retell_1",
        llm_ref=None,
        response_engine=ResponseEngine.CUSTOM,
    )
    call = await call_repo.create(
        db,
        platform_id=platform_id,
        agent_id=agent_id,
        from_number="+19129143920",
        to_number="+15551234567",
        dynamic_variables={},
        status=CallStatus.REGISTERED,
        vendor=retell_adapter.VENDOR_NAME,
        vendor_ref="call_retell_1",
    )

    resp = await client.delete(
        f"/agents/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 204

    still_there = await call_repo.get_by_id(db, call.id, platform_id=platform_id)
    assert still_there is not None
    assert still_there.agent_id == agent_id


# ── DELETE /agents/{agent_id}/numbers/{phone_number} ────────────────────


async def test_delete_number_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.delete("/agents/000000000000000000000000/numbers/%2B19129143920")
    assert resp.status_code == 401


async def test_delete_number_unknown_agent_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.delete(
        "/agents/000000000000000000000000/numbers/%2B19129143920",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"
    assert resp.json()["detail"]["field"] == "agent_id"


async def test_delete_number_unknown_number_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.delete(
        f"/agents/{agent_id}/numbers/%2B19129143920",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["field"] == "phone_number"


async def test_delete_number_success(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _fake_delete_number(settings: Any, *, phone_number: str) -> None:
        calls.append(phone_number)

    monkeypatch.setattr(retell_adapter, "delete_phone_number", _fake_delete_number)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143920"
    )

    resp = await client.delete(
        f"/agents/{agent_id}/numbers/%2B19129143920",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 204
    assert resp.content == b""
    assert calls == ["+19129143920"]

    remaining = await phone_number_repo.list_by_agent_id(db, agent_id, platform_id=platform_id)
    assert remaining == []


async def test_delete_number_cross_platform_agent_is_404_not_403(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter, "delete_phone_number", _no_call_allowed("delete_phone_number")
    )

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    await _seed_owned_number(db, platform_id=id_a, agent_id=agent_a_id)

    resp = await client.delete(
        f"/agents/{agent_a_id}/numbers/%2B19129143920",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert id_a != id_b

    still_there = await phone_number_repo.list_by_agent_id(db, agent_a_id, platform_id=id_a)
    assert len(still_there) == 1


async def test_delete_number_belonging_to_different_agent_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A number bound to a DIFFERENT agent (even one owned by the same
    platform) must not be deletable via this agent's own endpoint.
    """
    monkeypatch.setattr(
        retell_adapter, "delete_phone_number", _no_call_allowed("delete_phone_number")
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_1_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    agent_2_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_2")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_2_id, phone_number="+19129143920"
    )

    resp = await client.delete(
        f"/agents/{agent_1_id}/numbers/%2B19129143920",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404

    still_there = await phone_number_repo.list_by_agent_id(db, agent_2_id, platform_id=platform_id)
    assert len(still_there) == 1


async def test_update_number_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.patch(
        "/agents/000000000000000000000000/numbers/%2B19129143920", json={"nickname": "x"}
    )
    assert resp.status_code == 401


async def test_update_number_unknown_agent_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/agents/000000000000000000000000/numbers/%2B19129143920",
        json={"nickname": "New name"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"
    assert resp.json()["detail"]["field"] == "agent_id"


async def test_update_number_unknown_number_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.patch(
        f"/agents/{agent_id}/numbers/%2B19129143920",
        json={"nickname": "New name"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["field"] == "phone_number"


async def test_update_number_entirely_empty_request_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter, "update_phone_number", _no_call_allowed("update_phone_number")
    )
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.patch(
        f"/agents/{agent_id}/numbers/%2B19129143920",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    # Pydantic itself may reject an entirely-empty JSON object at the schema
    # level (both fields have None defaults, so {} is well-formed) — either
    # a 422 from Pydantic parsing or from our own has_any_field_set() check
    # is acceptable here; what matters is it never reaches the vendor.
    assert resp.status_code == 422


async def test_update_number_nickname_only_rename_success(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None, str | None]] = []

    async def _fake_update(
        settings: Any,
        *,
        phone_number: str,
        nickname: str | None = None,
        retell_agent_id: str | None = None,
    ) -> Any:
        calls.append((phone_number, nickname, retell_agent_id))
        return retell_adapter.RetellCreatePhoneNumberResult(
            phone_number=phone_number, area_code=912, nickname=nickname
        )

    monkeypatch.setattr(retell_adapter, "update_phone_number", _fake_update)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143920"
    )

    resp = await client.patch(
        f"/agents/{agent_id}/numbers/%2B19129143920",
        json={"nickname": "After-hours line"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["phone_number"] == "+19129143920"
    assert body["nickname"] == "After-hours line"
    assert body["agent_id"] == agent_id  # unchanged, since agent_id was omitted

    assert calls == [("+19129143920", "After-hours line", None)]

    remaining = await phone_number_repo.list_by_agent_id(db, agent_id, platform_id=platform_id)
    assert remaining[0].nickname == "After-hours line"
    assert remaining[0].agent_id == agent_id


async def test_update_number_rebind_only_success(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None, str | None]] = []

    async def _fake_update(
        settings: Any,
        *,
        phone_number: str,
        nickname: str | None = None,
        retell_agent_id: str | None = None,
    ) -> Any:
        calls.append((phone_number, nickname, retell_agent_id))
        return retell_adapter.RetellCreatePhoneNumberResult(
            phone_number=phone_number, area_code=912, nickname=None
        )

    monkeypatch.setattr(retell_adapter, "update_phone_number", _fake_update)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_1_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    agent_2_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_2")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_1_id, phone_number="+19129143920"
    )

    resp = await client.patch(
        f"/agents/{agent_1_id}/numbers/%2B19129143920",
        json={"agent_id": agent_2_id},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == agent_2_id

    assert calls == [("+19129143920", None, "agent_retell_2")]

    # Now shows up under agent 2's numbers, not agent 1's.
    agent_2_numbers = await phone_number_repo.list_by_agent_id(
        db, agent_2_id, platform_id=platform_id
    )
    assert len(agent_2_numbers) == 1
    agent_1_numbers = await phone_number_repo.list_by_agent_id(
        db, agent_1_id, platform_id=platform_id
    )
    assert agent_1_numbers == []


async def test_update_number_rename_and_rebind_both_at_once(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_update(
        settings: Any,
        *,
        phone_number: str,
        nickname: str | None = None,
        retell_agent_id: str | None = None,
    ) -> Any:
        return retell_adapter.RetellCreatePhoneNumberResult(
            phone_number=phone_number, area_code=912, nickname=nickname
        )

    monkeypatch.setattr(retell_adapter, "update_phone_number", _fake_update)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_1_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    agent_2_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_2")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_1_id, phone_number="+19129143920"
    )

    resp = await client.patch(
        f"/agents/{agent_1_id}/numbers/%2B19129143920",
        json={"nickname": "Moved line", "agent_id": agent_2_id},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["nickname"] == "Moved line"
    assert body["agent_id"] == agent_2_id


async def test_update_number_rebind_to_another_platforms_agent_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core tenancy boundary this endpoint has to enforce: a caller must
    never be able to rebind their own number to point at another platform's
    agent.
    """
    monkeypatch.setattr(
        retell_adapter, "update_phone_number", _no_call_allowed("update_phone_number")
    )

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    agent_b_id = await _seed_active_agent(db, platform_id=id_b, vendor_ref="agent_retell_b")
    await _seed_owned_number(db, platform_id=id_a, agent_id=agent_a_id)

    resp = await client.patch(
        f"/agents/{agent_a_id}/numbers/%2B19129143920",
        json={"agent_id": agent_b_id},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["field"] == "agent_id"

    # Untouched.
    still_there = await phone_number_repo.list_by_agent_id(db, agent_a_id, platform_id=id_a)
    assert len(still_there) == 1
    assert id_a != id_b


async def test_update_number_operating_on_another_platforms_number_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter, "update_phone_number", _no_call_allowed("update_phone_number")
    )

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    await _seed_owned_number(db, platform_id=id_a, agent_id=agent_a_id)

    resp = await client.patch(
        f"/agents/{agent_a_id}/numbers/%2B19129143920",
        json={"nickname": "Hijacked"},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert id_a != id_b

    still_there = await phone_number_repo.list_by_agent_id(db, agent_a_id, platform_id=id_a)
    assert still_there[0].nickname is None


async def test_update_number_rebind_to_failed_agent_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter, "update_phone_number", _no_call_allowed("update_phone_number")
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_1_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    failed_agent_id = await _seed_failed_agent(db, platform_id=platform_id)
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_1_id, phone_number="+19129143920"
    )

    resp = await client.patch(
        f"/agents/{agent_1_id}/numbers/%2B19129143920",
        json={"agent_id": failed_agent_id},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "agent_id"

    still_there = await phone_number_repo.list_by_agent_id(db, agent_1_id, platform_id=platform_id)
    assert len(still_there) == 1


async def test_update_number_vendor_failure_leaves_local_state_unchanged(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.errors import CODE_UPSTREAM_FAILED, AppError

    async def _fake_update_fails(
        settings: Any,
        *,
        phone_number: str,
        nickname: str | None = None,
        retell_agent_id: str | None = None,
    ) -> Any:
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the phone number update request.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "update_phone_number", _fake_update_fails)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_1_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    agent_2_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_2")
    await _seed_owned_number(
        db,
        platform_id=platform_id,
        agent_id=agent_1_id,
        phone_number="+19129143920",
    )

    resp = await client.patch(
        f"/agents/{agent_1_id}/numbers/%2B19129143920",
        json={"nickname": "Should not stick", "agent_id": agent_2_id},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "upstream_failed"

    # Local state completely unchanged — no partial update.
    still_agent_1 = await phone_number_repo.list_by_agent_id(
        db, agent_1_id, platform_id=platform_id
    )
    assert len(still_agent_1) == 1
    assert still_agent_1[0].nickname is None
    assert still_agent_1[0].agent_id == agent_1_id

    still_agent_2 = await phone_number_repo.list_by_agent_id(
        db, agent_2_id, platform_id=platform_id
    )
    assert still_agent_2 == []


async def test_delete_number_vendor_failure_leaves_record_and_raises_502(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.errors import CODE_UPSTREAM_FAILED, AppError

    async def _fake_delete_number_fails(settings: Any, *, phone_number: str) -> None:
        raise AppError(
            code=CODE_UPSTREAM_FAILED,
            message="The voice vendor rejected the request to release the phone number.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "delete_phone_number", _fake_delete_number_fails)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(
        db, platform_id=platform_id, agent_id=agent_id, phone_number="+19129143920"
    )

    resp = await client.delete(
        f"/agents/{agent_id}/numbers/%2B19129143920",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "upstream_failed"

    # Our own record is untouched — safe to retry.
    still_there = await phone_number_repo.list_by_agent_id(db, agent_id, platform_id=platform_id)
    assert len(still_there) == 1
