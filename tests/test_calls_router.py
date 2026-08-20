"""Integration tests for POST /calls/outbound.

Same monkeypatching discipline as every other router's tests: never make a
real network call to Retell from pytest. The success/vendor-failure paths
monkeypatch retell_adapter.create_phone_call directly; agent/number setup
bypasses the vendor entirely by writing repository records directly, exactly
like a successful POST /agents / POST /agents/{agent_id}/numbers would have
persisted. The one real, live, manually-run end-to-end check (or the honest
account-blocker fallback) was done separately outside the test suite — see
backend-dev.md's Feature status section for that evidence trail.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.collections import CALLS
from app.database import MongoDB
from app.errors import AppError
from app.models.agent import AgentStatus
from app.models.language import Language
from app.repositories import agent_repo, phone_number_repo, platform_repo
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


def _fake_create_phone_call(
    *,
    call_id: str = "call_fake_abc123",
    agent_id: str = "agent_retell_1",
    call_status: str = "registered",
) -> Any:
    async def _fake(settings: Any, **kwargs: Any) -> retell_adapter.RetellCreatePhoneCallResult:
        return retell_adapter.RetellCreatePhoneCallResult(
            call_id=call_id, agent_id=agent_id, call_status=call_status
        )

    return _fake


_VALID_TO_NUMBER = "+15551234567"


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": "000000000000000000000000",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_missing_required_fields_is_422(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/calls/outbound",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_success_path(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter,
        "create_phone_call",
        _fake_create_phone_call(call_id="call_fake_success"),
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "dynamic_variables": {"contact_name": "John Smith"},
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["platform_id"] == platform_id
    assert body["agent_id"] == agent_id
    assert body["from_number"] == "+19129143920"
    assert body["to_number"] == _VALID_TO_NUMBER
    assert body["dynamic_variables"] == {"contact_name": "John Smith"}
    assert body["status"] == "registered"
    assert "id" in body
    # Never leak vendor identity or Retell's raw call_id to the caller — the
    # id returned is OUR OWN Mongo id, not Retell's call_fake_success.
    assert body["id"] != "call_fake_success"
    assert "vendor" not in body
    assert "vendor_ref" not in body
    assert "call_id" not in body

    docs = await db[CALLS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["agent_id"] == agent_id
    assert docs[0]["vendor"] == "retell"
    assert docs[0]["vendor_ref"] == "call_fake_success"
    assert docs[0]["status"] == "registered"


async def test_from_number_not_owned_by_caller_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core security boundary: a from_number that isn't provisioned for THIS
    platform must be rejected, even if it's a well-formed E.164 number (and
    even if it belongs to a different platform entirely).
    """
    called = False

    async def _should_not_be_called(settings: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("create_phone_call must not be called for an unowned from_number")

    monkeypatch.setattr(retell_adapter, "create_phone_call", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    # Deliberately do NOT seed a PhoneNumbers record for this platform.

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail["field"] == "from_number"
    assert not called

    docs = await db[CALLS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs == []


async def test_from_number_owned_by_another_platform_is_422_not_leaked(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platform A must not be able to place a call using a from_number that
    is genuinely owned by Platform B — the real cross-tenant boundary this
    endpoint exists to enforce.
    """
    monkeypatch.setattr(retell_adapter, "create_phone_call", _fake_create_phone_call())

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    agent_b_id = await _seed_active_agent(db, platform_id=id_b, vendor_ref="agent_retell_b")
    # Number belongs to Platform B only.
    await _seed_owned_number(db, platform_id=id_b, agent_id=agent_b_id, phone_number="+19998887777")

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19998887777",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_a_id,
        },
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "from_number"

    # Platform B itself can use its own number fine.
    resp_b = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19998887777",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_b_id,
        },
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp_b.status_code == 201


async def test_agent_not_found_is_404(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_adapter, "create_phone_call", _fake_create_phone_call())
    api_key, platform_id = await _seed_platform(db, "Platform A")

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": "000000000000000000000000",
        },
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
    monkeypatch.setattr(retell_adapter, "create_phone_call", _fake_create_phone_call())

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    await _seed_owned_number(db, platform_id=id_b, agent_id="whatever", phone_number="+19129143920")

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_a_id,
        },
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_agent_status_failed_is_rejected_with_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def _should_not_be_called(settings: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("create_phone_call must not be called for a failed agent")

    monkeypatch.setattr(retell_adapter, "create_phone_call", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_failed_agent(db, platform_id=platform_id)
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert resp.json()["detail"]["field"] == "agent_id"
    assert not called


async def test_agent_override_different_from_number_bound_agent_is_allowed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied agent_id does not need to match the agent bound to
    from_number in PhoneNumbers — this is intentional override behavior
    (Retell's real override_agent_id field), not a bug.
    """
    received_kwargs: dict[str, Any] = {}

    async def _capturing_fake(settings: Any, **kwargs: Any) -> Any:
        received_kwargs.update(kwargs)
        return retell_adapter.RetellCreatePhoneCallResult(
            call_id="call_override", agent_id="agent_retell_override", call_status="registered"
        )

    monkeypatch.setattr(retell_adapter, "create_phone_call", _capturing_fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    bound_agent_id = await _seed_active_agent(
        db, platform_id=platform_id, vendor_ref="agent_retell_bound"
    )
    override_agent_id = await _seed_active_agent(
        db, platform_id=platform_id, vendor_ref="agent_retell_override"
    )
    # Number is bound to bound_agent_id, but the request uses override_agent_id.
    await _seed_owned_number(db, platform_id=platform_id, agent_id=bound_agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": override_agent_id,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201
    assert resp.json()["agent_id"] == override_agent_id
    assert received_kwargs["retell_agent_id"] == "agent_retell_override"


async def test_non_string_dynamic_variable_value_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retell_llm_dynamic_variables is a flat dict of string-to-string pairs
    only (confirmed live against Retell's docs) — a non-string value (e.g. a
    JSON number/boolean) is rejected up front rather than silently
    stringified, since silent coercion would hide a likely caller mistake.
    """
    called = False

    async def _should_not_be_called(settings: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("create_phone_call must not be called for invalid dynamic_variables")

    monkeypatch.setattr(retell_adapter, "create_phone_call", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "dynamic_variables": {"call_count": 3},
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert not called


async def test_vendor_failure_returns_upstream_failed_and_persists_failed_record(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _failing_create_phone_call(settings: Any, **kwargs: Any) -> Any:
        raise AppError(
            code="upstream_failed",
            message="Could not reach the voice vendor to place the call. Try again shortly.",
            status_code=502,
        )

    monkeypatch.setattr(retell_adapter, "create_phone_call", _failing_create_phone_call)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_failed"
    assert detail["request_id"]
    assert "httpx" not in detail["message"].lower()

    # Persist-on-vendor-failure, same reasoning as POST /agents: the record
    # of what was attempted is real and worth keeping for a future
    # retry/audit trail, unlike the phone-number endpoints' no-persist
    # pattern (see app/models/call.py's module docstring for the reasoning).
    docs = await db[CALLS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["status"] == "failed"
    assert docs[0]["vendor_ref"] is None
    assert docs[0]["vendor"] == "retell"


async def test_cross_platform_calls_are_isolated(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory per the standards doc: any new platform_id-scoped collection
    needs a test asserting Platform A cannot see/touch Platform B's records.
    """
    monkeypatch.setattr(retell_adapter, "create_phone_call", _fake_create_phone_call())

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    await _seed_owned_number(db, platform_id=id_a, agent_id=agent_a_id)

    resp_a = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_a_id,
        },
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp_a.status_code == 201
    call_a_id = resp_a.json()["id"]

    from app.repositories import call_repo

    found_by_b = await call_repo.get_by_id(db, call_a_id, platform_id=id_b)
    assert found_by_b is None

    found_by_a = await call_repo.get_by_id(db, call_a_id, platform_id=id_a)
    assert found_by_a is not None
    assert found_by_a.platform_id == id_a
    assert id_a != id_b


# ── GET /calls/{id} (Task 2) ─────────────────────────────────────────────


async def test_get_call_success_returns_full_record_shape(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_adapter, "create_phone_call", _fake_create_phone_call(call_id="call_fake_get")
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    create_resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "dynamic_variables": {"contact_name": "John Smith"},
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert create_resp.status_code == 201
    call_id = create_resp.json()["id"]

    resp = await client.get(f"/calls/{call_id}", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    body = resp.json()

    # Full record shape — every field a real Platform X caller would need
    # in one response, per the standards doc's "how does Platform X use
    # this" checklist.
    assert body["id"] == call_id
    assert body["platform_id"] == platform_id
    assert body["agent_id"] == agent_id
    assert body["status"] == "registered"
    assert body["direction"] == "outbound"
    assert body["from_number"] == "+19129143920"
    assert body["to_number"] == _VALID_TO_NUMBER
    assert body["dynamic_variables"] == {"contact_name": "John Smith"}
    assert body["recording_url"] is None
    assert body["transcript_url"] is None
    assert body["summary"] is None
    assert body["sentiment"] is None
    assert body["extracted_data"] is None
    assert body["recording_rehost_failed"] is False
    assert "created_at" in body
    assert "updated_at" in body
    # Never leak vendor identity.
    assert "vendor" not in body
    assert "vendor_ref" not in body


async def test_get_call_unknown_id_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.get(
        "/calls/000000000000000000000000", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_get_call_cross_platform_access_is_404_not_403(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory per the standards doc: cross-platform access to a single
    record is disguised as 404, never 403.
    """
    monkeypatch.setattr(retell_adapter, "create_phone_call", _fake_create_phone_call())

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")
    agent_a_id = await _seed_active_agent(db, platform_id=id_a, vendor_ref="agent_retell_a")
    await _seed_owned_number(db, platform_id=id_a, agent_id=agent_a_id)

    create_resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_a_id,
        },
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert create_resp.status_code == 201
    call_a_id = create_resp.json()["id"]

    resp = await client.get(f"/calls/{call_a_id}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"

    # Platform A can still fetch its own call fine.
    resp_a = await client.get(f"/calls/{call_a_id}", headers={"Authorization": f"Bearer {key_a}"})
    assert resp_a.status_code == 200


async def test_get_call_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.get("/calls/000000000000000000000000")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


# Known-limitation note, not a test: a genuinely inbound call (one that
# rings in with no prior POST /calls/outbound trigger) has NO Calls record
# at all today (see backend-dev.md's Known open items) — there is no code
# path in this codebase that creates one, so there is no way to construct a
# real "inbound call that exists but 404s incorrectly" scenario to test
# here. GET /calls/{id} against such a call's would-be id 404s for the same
# reason test_get_call_unknown_id_is_404 above does (no matching document),
# which is the correct, expected behavior for the current state of the
# system, not a bug this endpoint needs to special-case.


# ── voicemail_detection (voicemail_option on the vendor's real
# create-phone-call) — see app/models/call.py's module docstring for the
# full sourced field-shape reasoning ─────────────────────────────────────


async def test_voicemail_detection_omitted_sends_no_vendor_field(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-feature behavior must be unchanged: no voicemail_detection in the
    request -> no voicemail_option key at all in the vendor call body (never
    sent as a default/null), matching how dynamic_variables is already
    omitted when empty.
    """
    received_kwargs: dict[str, Any] = {}

    async def _capturing_fake(settings: Any, **kwargs: Any) -> Any:
        received_kwargs.update(kwargs)
        return retell_adapter.RetellCreatePhoneCallResult(
            call_id="call_no_voicemail", agent_id="agent_retell_1", call_status="registered"
        )

    monkeypatch.setattr(retell_adapter, "create_phone_call", _capturing_fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201
    assert received_kwargs["voicemail_detection"] is None


@pytest.mark.parametrize(
    ("action", "extra_body"),
    [
        ("hangup", {}),
        ("prompt", {}),
        ("bridge_transfer", {}),
        ("static_text", {"text": "Sorry we missed you, please call back."}),
    ],
)
async def test_voicemail_detection_each_action_type_reaches_vendor_body(
    client: AsyncClient,
    db: MongoDB,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    extra_body: dict[str, Any],
) -> None:
    """All 4 real Retell VoicemailAction values must be accepted and passed
    through correctly to the adapter's voicemail_detection kwarg (the
    adapter itself, retell_adapter.create_phone_call, is responsible for the
    final voicemail_option wire shape — see its own unit-style assertions
    below for that layer).
    """
    received_kwargs: dict[str, Any] = {}

    async def _capturing_fake(settings: Any, **kwargs: Any) -> Any:
        received_kwargs.update(kwargs)
        return retell_adapter.RetellCreatePhoneCallResult(
            call_id=f"call_vm_{action}", agent_id="agent_retell_1", call_status="registered"
        )

    monkeypatch.setattr(retell_adapter, "create_phone_call", _capturing_fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "voicemail_detection": {"action": action, **extra_body},
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.json()
    vm = received_kwargs["voicemail_detection"]
    assert vm is not None
    assert vm.action.value == action
    if action == "static_text":
        assert vm.text == "Sorry we missed you, please call back."
    else:
        assert vm.text is None


async def test_voicemail_detection_static_text_without_text_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """action='static_text' requires the sibling `text` field — omitting it
    must be rejected with 422 before ever calling the vendor, the same
    required-for-one-enum-value validator shape as
    StructuredDataFieldDefinition.choices (app/models/agent.py).
    """
    called = False

    async def _should_not_be_called(settings: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("create_phone_call must not be called for invalid voicemail_detection")

    monkeypatch.setattr(retell_adapter, "create_phone_call", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "voicemail_detection": {"action": "static_text"},
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert not called


@pytest.mark.parametrize("action", ["hangup", "prompt", "bridge_transfer"])
async def test_voicemail_detection_non_static_text_with_text_is_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """text must be REJECTED (not silently ignored) for every action value
    other than static_text — mirrors choices being rejected for every
    StructuredDataFieldType other than enum.
    """
    called = False

    async def _should_not_be_called(settings: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("create_phone_call must not be called for invalid voicemail_detection")

    monkeypatch.setattr(retell_adapter, "create_phone_call", _should_not_be_called)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "voicemail_detection": {"action": action, "text": "should not be allowed here"},
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert not called


async def test_voicemail_detection_with_detection_prompt_reaches_adapter(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """detection_prompt is a real, optional sibling field independent of
    action — confirm it's threaded through untouched.
    """
    received_kwargs: dict[str, Any] = {}

    async def _capturing_fake(settings: Any, **kwargs: Any) -> Any:
        received_kwargs.update(kwargs)
        return retell_adapter.RetellCreatePhoneCallResult(
            call_id="call_vm_prompt", agent_id="agent_retell_1", call_status="registered"
        )

    monkeypatch.setattr(retell_adapter, "create_phone_call", _capturing_fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _seed_active_agent(db, platform_id=platform_id, vendor_ref="agent_retell_1")
    await _seed_owned_number(db, platform_id=platform_id, agent_id=agent_id)

    resp = await client.post(
        "/calls/outbound",
        json={
            "from_number": "+19129143920",
            "to_number": _VALID_TO_NUMBER,
            "agent_id": agent_id,
            "voicemail_detection": {
                "action": "hangup",
                "detection_prompt": "Treat a long silence followed by a beep as voicemail.",
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201
    vm = received_kwargs["voicemail_detection"]
    assert vm.detection_prompt == "Treat a long silence followed by a beep as voicemail."


async def test_retell_adapter_builds_correct_voicemail_option_wire_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter-level unit test: confirms create_phone_call() builds the real
    vendor voicemail_option shape ({"action": ..., "text": ...,
    "detection_prompt": ...}) in the outgoing HTTP body, not just that the
    router passes the field through. Monkeypatches httpx directly so this
    never makes a real network call.
    """
    import httpx

    from app.config import get_settings
    from app.models.call import VoicemailDetectionConfig

    captured: dict[str, Any] = {}

    class _FakeResponse:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {"call_id": "call_wire_shape", "agent_id": "agent_x", "call_status": "registered"}

    async def _fake_post(self: Any, url: str, json: dict[str, Any]) -> _FakeResponse:  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    settings = get_settings()
    await retell_adapter.create_phone_call(
        settings,
        from_number="+19129143920",
        to_number="+15551234567",
        retell_agent_id="agent_retell_1",
        dynamic_variables={},
        voicemail_detection=VoicemailDetectionConfig(
            action="static_text", text="Please call us back."
        ),
    )

    assert captured["json"]["voicemail_option"] == {
        "action": "static_text",
        "text": "Please call us back.",
    }


async def test_retell_adapter_omits_voicemail_option_when_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.config import get_settings

    captured: dict[str, Any] = {}

    class _FakeResponse:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {"call_id": "call_no_vm", "agent_id": "agent_x", "call_status": "registered"}

    async def _fake_post(self: Any, url: str, json: dict[str, Any]) -> _FakeResponse:  # noqa: A002
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    settings = get_settings()
    await retell_adapter.create_phone_call(
        settings,
        from_number="+19129143920",
        to_number="+15551234567",
        retell_agent_id="agent_retell_1",
        dynamic_variables={},
    )

    assert "voicemail_option" not in captured["json"]
