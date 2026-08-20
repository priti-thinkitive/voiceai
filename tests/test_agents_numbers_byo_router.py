"""Integration tests for POST /agents/{agent_id}/numbers/byo — Option 2
telephony (bring your own SIP trunk), the sibling of
POST /agents/{agent_id}/numbers (buy-new).

Same monkeypatching discipline as test_agents_numbers_router.py: never make
a real network call to Retell from pytest. The one real, live, manually-run
check against the real Retell account (plausible-but-fake phone_number/
termination_uri against the real existing agent) was done separately
outside the test suite — see backend-dev.md's Feature status section for
that evidence.
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

_BYO_BODY: dict[str, Any] = {
    "phone_number": "+14155551234",
    "termination_uri": "platformx.pstn.twilio.com",
    "sip_trunk_auth_username": "platformx_trunk_user",
    "sip_trunk_auth_password": "super-secret-trunk-password",
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


def _fake_import_phone_number(
    *,
    phone_number: str = "+14155551234",
    area_code: int | None = 415,
    nickname: str | None = None,
) -> Any:
    async def _fake(settings: Any, **kwargs: Any) -> retell_adapter.RetellCreatePhoneNumberResult:
        return retell_adapter.RetellCreatePhoneNumberResult(
            phone_number=phone_number, area_code=area_code, nickname=nickname
        )

    return _fake


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.post("/agents/000000000000000000000000/numbers/byo", json=_BYO_BODY)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_success_path(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter,
        "import_phone_number",
        _fake_import_phone_number(nickname="BYO main line"),
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.post(
        f"/agents/{agent_id}/numbers/byo",
        json={**_BYO_BODY, "nickname": "BYO main line"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["phone_number"] == "+14155551234"
    assert body["area_code"] == 415
    assert body["nickname"] == "BYO main line"
    assert body["agent_id"] == agent_id
    assert "created_at" in body

    # No Retell-internal fields leak.
    assert "vendor" not in body
    assert "vendor_ref" not in body
    assert "phone_number_pretty" not in body
    assert "sip_outbound_trunk_config" not in body

    # No SIP credentials appear anywhere in the response, under any key.
    body_str = str(body)
    assert "sip_trunk_auth_username" not in body_str
    assert "sip_trunk_auth_password" not in body_str
    assert "platformx_trunk_user" not in body_str
    assert "super-secret-trunk-password" not in body_str

    docs = await db[PHONE_NUMBERS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["agent_id"] == agent_id
    assert docs[0]["phone_number"] == "+14155551234"
    assert docs[0]["vendor"] == "retell"


async def test_credentials_never_persisted_to_phone_numbers_collection(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct regression test for the credential-non-persistence decision —
    the field most likely to accidentally regress later. Asserts the raw
    Mongo document contains no trace of the SIP auth values, by key or by
    value, not just that the public response omits them.
    """
    monkeypatch.setattr(
        retell_adapter,
        "import_phone_number",
        _fake_import_phone_number(),
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.post(
        f"/agents/{agent_id}/numbers/byo",
        json=_BYO_BODY,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201

    doc = await db[PHONE_NUMBERS].find_one({"platform_id": platform_id})
    assert doc is not None
    assert "sip_trunk_auth_username" not in doc
    assert "sip_trunk_auth_password" not in doc
    doc_str = str(doc)
    assert "platformx_trunk_user" not in doc_str
    assert "super-secret-trunk-password" not in doc_str


async def test_missing_required_fields_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_adapter, "import_phone_number", _fake_import_phone_number())
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    # Missing both phone_number and termination_uri.
    resp = await client.post(
        f"/agents/{agent_id}/numbers/byo",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"

    # Missing only termination_uri.
    resp2 = await client.post(
        f"/agents/{agent_id}/numbers/byo",
        json={"phone_number": "+14155551234"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp2.status_code == 422
    assert resp2.json()["detail"]["code"] == "invalid_request"


async def test_agent_not_found_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_adapter, "import_phone_number", _fake_import_phone_number())
    api_key, _ = await _seed_platform(db, "Platform A")

    resp = await client.post(
        "/agents/000000000000000000000000/numbers/byo",
        json=_BYO_BODY,
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
    monkeypatch.setattr(retell_adapter, "import_phone_number", _fake_import_phone_number())

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")

    resp = await client.post(
        f"/agents/{agent_a_id}/numbers/byo",
        json=_BYO_BODY,
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"

    # The owning platform can still import a number for its own agent.
    resp_owner = await client.post(
        f"/agents/{agent_a_id}/numbers/byo",
        json=_BYO_BODY,
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
        raise AssertionError("import_phone_number must not be called for a failed agent")

    monkeypatch.setattr(retell_adapter, "import_phone_number", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_failed_agent(db, platform_id=platform_id)

    resp = await client.post(
        f"/agents/{agent_id}/numbers/byo",
        json=_BYO_BODY,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert not called


async def test_vendor_failure_returns_upstream_failed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _failing_import_phone_number(settings: Any, **kwargs: Any) -> Any:
        raise AppError(
            code="upstream_failed",
            message="The voice vendor rejected the phone number import request. Confirm the "
            "SIP trunk is reachable and Retell's IP ranges are whitelisted on your "
            "provider's side.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "import_phone_number", _failing_import_phone_number)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")

    resp = await client.post(
        f"/agents/{agent_id}/numbers/byo",
        json=_BYO_BODY,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_failed"
    assert detail["request_id"]
    assert "httpx" not in detail["message"].lower()
    # No leaked credentials in the error message either.
    assert "super-secret-trunk-password" not in detail["message"]

    # No PhoneNumbers doc persisted on a failed import — nothing was
    # actually imported, so there is no stable id worth keeping for a retry.
    docs = await db[PHONE_NUMBERS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs == []
