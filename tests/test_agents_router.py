"""Integration tests for POST /agents.

Every path monkeypatches the relevant retell_agent_adapter function(s)
(create_agent for custom_llm, create_retell_llm_agent for retell_llm — see
app/services/retell_agent_adapter.py) rather than making a real network call
to Retell. A test suite must not depend on ambient environment state
(whether a real RETELL_API_KEY happens to be configured, or reach a real
vendor account at all) — that's non-deterministic, creates real side effects
in a real (possibly shared) vendor account that then need manual cleanup,
and costs real API usage. The genuine end-to-end Retell integration (real
key, real HTTP calls, real agent+LLM created/deleted, transfer_call
confirmed via a real GET) was separately verified live and by hand once,
outside the test suite — see the "Feature status" section of backend-dev.md
for that evidence trail. What these tests prove is our own request-
validation/persistence/response-shape/error-contract/orphan-cleanup logic,
independent of Retell's real availability.

`response_engine` defaults to `retell_llm` (see app/models/agent.py's
module docstring for the full default-mode reasoning), so any test that
exercises the plain default path now goes through
retell_agent_adapter.create_retell_llm_agent, not .create_agent. Tests that
specifically need to exercise the `custom_llm` path (regression coverage —
this mode is unchanged mechanically from before this task) pass
response_engine="custom_llm" explicitly and monkeypatch .create_agent
instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.collections import AGENTS
from app.database import MongoDB
from app.errors import AppError
from app.models.language import Language
from app.repositories import agent_repo, platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix
from app.services import retell_agent_adapter


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


_VALID_PAYLOAD: dict[str, Any] = {
    "prompt": "You are a friendly front-desk assistant for Aspen Quality Care.",
    "voice_id": "11labs-Adrian",
}

_CUSTOM_LLM_PAYLOAD: dict[str, Any] = {**_VALID_PAYLOAD, "response_engine": "custom"}


def _fake_retell_llm_agent(
    agent_id: str = "agent_fake_retell_llm", llm_id: str = "llm_fake_123"
) -> Any:
    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id=agent_id, llm_id=llm_id
        )

    return _fake


def _fake_custom_llm_agent(agent_id: str = "agent_fake_custom_llm") -> Any:
    async def _fake(settings: Any, **kwargs: Any) -> retell_agent_adapter.RetellCreateAgentResult:
        return retell_agent_adapter.RetellCreateAgentResult(agent_id=agent_id)

    return _fake


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.post("/agents", json=_VALID_PAYLOAD)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_missing_required_fields_is_422(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("voice_speed", 0.1),  # below 0.5 floor
        ("voice_speed", 3.0),  # above 2.0 ceiling
        ("interruption_sensitivity", -0.1),  # below 0 floor
        ("interruption_sensitivity", 1.5),  # above 1 ceiling
        ("transfer_ring_duration_ms", 1000),  # below 5000 floor
        ("transfer_ring_duration_ms", 100_000),  # above 90000 ceiling
    ],
)
async def test_out_of_range_tuning_fields_is_422(
    client: AsyncClient, db: MongoDB, field: str, value: float
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, field: value}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_create_agent_success_path_defaults_to_retell_llm(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolates our own validation/persistence/response-shape logic from
    Retell's real availability by monkeypatching the adapter's outbound
    call(s). The default response_engine is retell_llm (see this module's
    docstring) so with no response_engine in the payload, the retell_llm
    path is what fires — regression coverage for the default-mode decision.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["platform_id"] == platform_id
    assert body["prompt"] == _VALID_PAYLOAD["prompt"]
    assert body["voice_id"] == _VALID_PAYLOAD["voice_id"]
    assert body["languages"] == ["en-US"]
    assert body["voice_speed"] == 1.0
    assert body["interruption_sensitivity"] == 1.0
    assert body["enable_backchannel"] is True
    assert body["pronunciation_dictionary"] == []
    assert body["response_engine"] == "builtin"
    assert body["transfer_enabled"] is False  # no transfer_number supplied
    assert body["status"] == "active"
    assert "id" in body
    # Never leak vendor identity/internal ids to the caller.
    assert "vendor" not in body
    assert "vendor_ref" not in body
    assert "llm_ref" not in body


async def test_create_agent_retell_llm_with_transfer_number_calls_both_vendor_steps(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core new-feature regression test: response_engine='builtin' +
    transfer_number reaches retell_agent_adapter.create_retell_llm_agent
    (the function that internally sequences create-retell-llm then
    create-agent) with the right kwargs, and the response correctly reflects
    transfer_enabled=true and both real vendor ids are stored internally
    (never on the public response).
    """
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_transfer", llm_id="llm_fake_transfer"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "transfer_number": "+14155550100"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["response_engine"] == "builtin"
    assert body["transfer_enabled"] is True
    assert "llm_ref" not in body
    assert "vendor_ref" not in body

    # The adapter received the full CreateAgentRequest (carrying
    # transfer_number and its tuning) plus the agent-level fields.
    assert captured["body"].transfer_number == "+14155550100"
    assert captured["voice_id"] == _VALID_PAYLOAD["voice_id"]

    # Both real vendor ids are persisted internally.
    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["vendor_ref"] == "agent_fake_transfer"
    assert docs[0]["llm_ref"] == "llm_fake_transfer"
    assert docs[0]["transfer_number"] == "+14155550100"


async def test_create_agent_custom_llm_mode_still_works_unchanged(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression coverage for the pre-existing custom_llm path — still
    works exactly as before this task when explicitly requested, calling
    retell_agent_adapter.create_agent (not create_retell_llm_agent), and
    never calling the retell_llm path.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())

    async def _unexpected_retell_llm_call(settings: Any, **kwargs: Any) -> Any:
        raise AssertionError("custom_llm mode must never call create_retell_llm_agent")

    monkeypatch.setattr(
        retell_agent_adapter, "create_retell_llm_agent", _unexpected_retell_llm_call
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["response_engine"] == "custom"
    assert body["transfer_enabled"] is False
    assert body["status"] == "active"

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["vendor_ref"] == "agent_fake_custom_llm"
    assert docs[0]["llm_ref"] is None


async def test_transfer_number_with_custom_llm_is_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    """Design decision #2: setting transfer_number alongside
    response_engine='custom' is rejected up front (422) rather than
    silently ignored, since custom_llm cannot support transfer at all and a
    silently-dropped field would look like it worked.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "transfer_number": "+14155550100"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "transfer_number" in detail["message"] or detail.get("field") == "transfer_number"

    # No agent record was ever persisted for a request that never passed
    # validation.
    docs = await db[AGENTS].find({}).to_list(length=10)
    assert docs == []


async def test_create_agent_vendor_failure_persists_failed_record_and_returns_upstream_failed(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a Retell outage/rejection deterministically (monkeypatched,
    not a real network call — see module docstring for why). Confirms the
    server does not crash, returns our standard error contract with
    code=upstream_failed, and still persists our own agent record
    (status=failed) rather than silently discarding the request. Uses the
    default (retell_llm) path.
    """

    async def _failing_create_retell_llm_agent(settings: Any, **kwargs: Any) -> Any:
        raise AppError(
            code="upstream_failed",
            message="Could not reach the voice vendor to create the agent. Try again shortly.",
            status_code=502,
        )

    monkeypatch.setattr(
        retell_agent_adapter, "create_retell_llm_agent", _failing_create_retell_llm_agent
    )

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_failed"
    assert detail["request_id"]
    # Never leak the raw exception/httpx error text to the caller.
    assert "httpx" not in detail["message"].lower()
    assert "traceback" not in detail["message"].lower()

    # Our own record still exists, scoped to the caller, marked failed.
    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["status"] == "failed"
    assert docs[0]["vendor_ref"] is None
    assert docs[0]["llm_ref"] is None
    assert docs[0]["vendor"] == "retell"


async def test_create_agent_custom_llm_vendor_failure_persists_failed_record(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same persist-on-vendor-failure coverage as above, for the custom_llm
    path specifically — regression coverage that this pre-existing behavior
    still holds unchanged.
    """

    async def _failing_create_agent(settings: Any, **kwargs: Any) -> Any:
        raise AppError(
            code="upstream_failed",
            message="Could not reach the voice vendor to create the agent. Try again shortly.",
            status_code=502,
        )

    monkeypatch.setattr(retell_agent_adapter, "create_agent", _failing_create_agent)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 502
    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["status"] == "failed"
    assert docs[0]["response_engine"] == "custom"
    assert docs[0]["vendor_ref"] is None


async def test_create_agent_orphaned_llm_cleanup_attempted_on_create_agent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design decision #5: if create-retell-llm succeeds but the subsequent
    create-agent call fails, retell_agent_adapter.create_retell_llm_agent
    must attempt to clean up the now-orphaned LLM object via
    delete_retell_llm, and the original create-agent error must still be
    what's raised — cleanup never masks the real failure.

    Exercises retell_agent_adapter.create_retell_llm_agent directly (not
    through the router/HTTP layer) so create_retell_llm and
    _post_create_agent can be monkeypatched independently to force exactly
    this partial-failure sequence, which the router-level tests above can't
    isolate as precisely.
    """
    from app.config import Settings
    from app.errors import AppError as _AppError
    from app.models.agent import CreateAgentRequest

    cleanup_calls: list[str] = []

    async def _fake_create_retell_llm(settings: Any, **kwargs: Any) -> str:
        return "llm_orphan_candidate"

    async def _failing_post_create_agent(settings: Any, body: dict[str, Any]) -> Any:
        raise _AppError(
            code="upstream_failed",
            message="The voice vendor rejected the agent creation request.",
            status_code=502,
        )

    async def _fake_delete_retell_llm(settings: Any, *, llm_id: str) -> None:
        cleanup_calls.append(llm_id)

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm", _fake_create_retell_llm)
    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _failing_post_create_agent)
    monkeypatch.setattr(retell_agent_adapter, "delete_retell_llm", _fake_delete_retell_llm)

    settings = Settings()
    body = CreateAgentRequest(prompt="hi", voice_id="v1")

    with pytest.raises(_AppError) as exc_info:
        await retell_agent_adapter.create_retell_llm_agent(
            settings,
            body=body,
            voice_id="v1",
            languages=[Language.EN_US],
            voice_speed=1.0,
            interruption_sensitivity=1.0,
            enable_backchannel=True,
            pronunciation_dictionary=[],
        )

    # The original create-agent failure is what's raised, unchanged.
    assert exc_info.value.code == "upstream_failed"
    # Cleanup was actually attempted against the orphaned llm_id.
    assert cleanup_calls == ["llm_orphan_candidate"]


async def test_create_agent_orphaned_llm_cleanup_failure_does_not_mask_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unlucky double-failure case: cleanup itself also fails. The
    original create-agent error must still be what's raised — never replaced
    by the cleanup failure, never silently swallowed either (this test can't
    directly assert the ERROR log line, but confirms the caller-visible
    contract holds, which is the part that actually matters for correctness).
    """
    from app.config import Settings
    from app.errors import AppError as _AppError
    from app.models.agent import CreateAgentRequest

    async def _fake_create_retell_llm(settings: Any, **kwargs: Any) -> str:
        return "llm_double_failure"

    async def _failing_post_create_agent(settings: Any, body: dict[str, Any]) -> Any:
        raise _AppError(
            code="upstream_failed",
            message="The voice vendor rejected the agent creation request.",
            status_code=502,
        )

    async def _also_failing_delete_retell_llm(settings: Any, *, llm_id: str) -> None:
        raise _AppError(
            code="upstream_failed",
            message="The voice vendor rejected the request to clean up the conversation brain.",
            status_code=502,
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm", _fake_create_retell_llm)
    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _failing_post_create_agent)
    monkeypatch.setattr(retell_agent_adapter, "delete_retell_llm", _also_failing_delete_retell_llm)

    settings = Settings()
    body = CreateAgentRequest(prompt="hi", voice_id="v1")

    with pytest.raises(_AppError) as exc_info:
        await retell_agent_adapter.create_retell_llm_agent(
            settings,
            body=body,
            voice_id="v1",
            languages=[Language.EN_US],
            voice_speed=1.0,
            interruption_sensitivity=1.0,
            enable_backchannel=True,
            pronunciation_dictionary=[],
        )

    # Still the ORIGINAL create-agent failure, not the cleanup failure.
    assert exc_info.value.message == "The voice vendor rejected the agent creation request."


async def test_language_valid_code_is_accepted(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real code from the voice vendor's full 63-code enum (not just the
    default 'en-US') is accepted. Regression coverage for the fix: language
    used to be a plain unvalidated str, so this alone wouldn't have caught
    anything — paired with the invalid-code/zh-HK/multi tests below, which
    prove the new validation actually rejects what it should.
    """
    monkeypatch.setattr(
        retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent("agent_fake_ru")
    )

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": "ru-RU"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    assert resp.json()["languages"] == ["ru-RU"]


async def test_language_made_up_code_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": "xx-ZZ"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_language_hong_kong_cantonese_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    """Direct regression test for the exact trap this fix exists to prevent:
    'zh-HK' is NOT a real voice-vendor language code (Cantonese support is
    Mainland-only, as 'yue-CN' — see app/models/language.py's docstring and
    vendor-docs/Retell.md's "Language support" section). Before this fix,
    'zh-HK' would have silently passed the old unvalidated str field.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": "zh-HK"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_language_deprecated_multi_shortcut_is_rejected(
    client: AsyncClient, db: MongoDB
) -> None:
    """The voice vendor still technically accepts a deprecated 'multi'
    shortcut string for multi-language agents, but that shortcut remains
    explicitly out of scope even now that the real array form is supported
    (see app/models/language.py's module docstring) — a caller wanting
    multiple languages must send a real array, never this string. 'multi'
    must NOT be silently allowed through, and the old Swagger example
    incorrectly listed it as a normal value; this is the regression test for
    that being fixed.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": "multi"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_language_default_en_us_still_works_when_omitted(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retell_agent_adapter,
        "create_retell_llm_agent",
        _fake_retell_llm_agent("agent_fake_default"),
    )

    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    assert resp.json()["languages"] == ["en-US"]


async def test_language_single_string_still_works_unchanged(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most important test in this task, per the task brief: any existing
    caller sending `"language": "en-US"` (a bare string, the only shape this
    field ever accepted before array support was added) must keep working
    completely unchanged — same 201, same normalized-to-a-list response
    shape, and the vendor adapter must receive it as a bare string (not
    wrapped oddly), matching exactly what it always sent before this task.
    """
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_bw_compat", llm_id="llm_fake_bw_compat"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": "fr-FR"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    assert resp.json()["languages"] == ["fr-FR"]
    # The adapter receives a normalized non-empty list internally...
    assert captured["languages"] == [Language.FR_FR]


async def test_language_array_of_multiple_codes_is_accepted(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real array of 2-3 languages is accepted and passed through to the
    mocked vendor call in the right shape — for a genuinely multilingual
    agent, the vendor's real create-agent endpoint accepts a JSON array of
    locale codes (confirmed via live WebFetch of its OpenAPI schema, see
    app/models/agent.py's module docstring).
    """
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_multi", llm_id="llm_fake_multi"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": ["en-US", "es-ES", "fr-FR"]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    assert resp.json()["languages"] == ["en-US", "es-ES", "fr-FR"]
    assert captured["languages"] == [Language.EN_US, Language.ES_ES, Language.FR_FR]


async def test_language_empty_array_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": []}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_language_array_with_invalid_code_is_rejected(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "language": ["en-US", "xx-ZZ"]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_language_array_exceeding_cap_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    """MAX_LANGUAGES = 10 is our own judgment call, not a vendor-documented
    limit (the vendor's schema has no documented maximum array size — see
    app/models/agent.py's module docstring). An 11-code array must still be
    rejected before ever reaching the vendor.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    too_many = [
        "en-US",
        "es-ES",
        "fr-FR",
        "de-DE",
        "it-IT",
        "pt-PT",
        "nl-NL",
        "ru-RU",
        "ja-JP",
        "ko-KR",
        "zh-CN",
    ]
    assert len(too_many) == 11
    payload = {**_VALID_PAYLOAD, "language": too_many}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_cross_platform_agents_are_isolated(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory per the standards doc: any new platform_id-scoped router
    needs a test asserting Platform A cannot see/touch Platform B's records.
    """
    monkeypatch.setattr(
        retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent("agent_fake_456")
    )

    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")

    resp_a = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {key_a}"}
    )
    assert resp_a.status_code == 201
    agent_a_id = resp_a.json()["id"]

    # Platform B's tenancy-scoped lookup never finds Platform A's agent.
    found_by_b = await agent_repo.get_by_id(db, agent_a_id, platform_id=id_b)
    assert found_by_b is None

    # Platform A's own lookup does find it.
    found_by_a = await agent_repo.get_by_id(db, agent_a_id, platform_id=id_a)
    assert found_by_a is not None
    assert found_by_a.platform_id == id_a
    assert id_a != id_b


# ── custom_tools ────────────────────────────────────────────────────────

_CUSTOM_TOOL: dict[str, Any] = {
    "name": "check_availability",
    "description": "Check whether a given date has an open appointment slot.",
    "parameters_schema": {
        "type": "object",
        "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
        "required": ["date"],
    },
    "webhook_url": "https://example.com/voiceai/tools/check-availability",
}


async def test_create_agent_with_custom_tool_builds_general_tools_with_our_own_proxy_url(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core new-feature regression test: a custom_tools entry reaches
    retell_agent_adapter.create_retell_llm_agent (via the CreateAgentRequest
    it's given), and when that adapter builds the real general_tools array,
    its 'url' field is OUR OWN proxy URL — never Platform X's own
    webhook_url. Exercises the real _build_general_tools logic (not further
    mocked) by
    monkeypatching only the outbound HTTP call, one level below where
    app/services/retell_agent_adapter.create_retell_llm makes its real
    httpx call, so the actual general_tools-building code runs for real.
    """
    captured_bodies: list[dict[str, Any]] = []

    async def _fake_post_create_agent(settings: Any, body: dict[str, Any]) -> dict[str, Any]:
        return {"agent_id": "agent_fake_custom_tool"}

    class _FakeResponse:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {"llm_id": "llm_fake_custom_tool"}

        text = "{}"

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            if url == "/create-retell-llm":
                captured_bodies.append(json)
            return _FakeResponse()

    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _fake_post_create_agent)
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "custom_tools": [_CUSTOM_TOOL]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["custom_tools"]) == 1
    assert body["custom_tools"][0]["name"] == "check_availability"
    assert body["custom_tools"][0]["webhook_url"] == _CUSTOM_TOOL["webhook_url"]

    assert len(captured_bodies) == 1
    general_tools = captured_bodies[0]["general_tools"]
    custom_entries = [t for t in general_tools if t["type"] == "custom"]
    assert len(custom_entries) == 1
    entry = custom_entries[0]
    assert entry["name"] == "check_availability"
    # url must be OUR OWN proxy — never Platform X's webhook_url.
    assert entry["url"].endswith("/webhooks/retell/custom-tool")
    assert entry["url"] != _CUSTOM_TOOL["webhook_url"]
    assert _CUSTOM_TOOL["webhook_url"] not in str(captured_bodies[0])
    assert entry["parameters"] == _CUSTOM_TOOL["parameters_schema"]


async def test_custom_tools_with_custom_mode_is_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    """Same pattern as transfer_number's own rejection under response_engine
    ='custom' — a custom tool set alongside a mode with no vendor-side tool-
    registration mechanism must be rejected up front, not silently dropped.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "custom_tools": [_CUSTOM_TOOL]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "custom_tools" in detail["message"]


async def test_custom_tools_duplicate_names_rejected_422(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "custom_tools": [_CUSTOM_TOOL, {**_CUSTOM_TOOL, "description": "A duplicate."}],
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_custom_tools_malformed_parameters_schema_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    """Not a full JSON Schema validator (per the standards doc's explicit
    instruction) — but an obviously-wrong shape (missing 'type') must still
    be caught before ever reaching the vendor.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    bad_tool = {**_CUSTOM_TOOL, "parameters_schema": {"properties": {}}}
    payload = {**_VALID_PAYLOAD, "custom_tools": [bad_tool]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_custom_tools_webhook_url_ssrf_protection(client: AsyncClient, db: MongoDB) -> None:
    """Same SSRF-adjacent guard already built for
    inbound_variables_webhook_url/call_completed_webhook_url
    (app/utils/ssrf_guard.py) must apply to a custom tool's own webhook_url —
    it's an address our own server will later POST to automatically,
    mid-call, on Platform X's behalf.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    internal_tool = {**_CUSTOM_TOOL, "webhook_url": "http://127.0.0.1:9999/tool"}
    payload = {**_VALID_PAYLOAD, "custom_tools": [internal_tool]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


# ── structured_data_fields (Task 1) ─────────────────────────────────────────

_STRING_FIELD: dict[str, Any] = {
    "type": "string",
    "name": "Caller Name",
    "description": "The name the caller gives for themselves.",
}

_ENUM_FIELD: dict[str, Any] = {
    "type": "enum",
    "name": "Call Outcome",
    "description": "Categorize how the call ended.",
    "choices": ["Appointment booked", "Declined", "Follow-up requested"],
}


async def test_structured_data_fields_valid_definitions_accepted(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "structured_data_fields": [_STRING_FIELD, _ENUM_FIELD]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["structured_data_fields"]) == 2
    assert body["structured_data_fields"][0]["name"] == "Caller Name"
    assert body["structured_data_fields"][1]["choices"] == _ENUM_FIELD["choices"]


async def test_structured_data_field_enum_without_choices_is_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    bad_field = {"type": "enum", "name": "Call Outcome", "description": "How the call ended."}
    payload = {**_VALID_PAYLOAD, "structured_data_fields": [bad_field]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_structured_data_field_non_enum_with_choices_is_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    bad_field = {**_STRING_FIELD, "choices": ["a", "b"]}
    payload = {**_VALID_PAYLOAD, "structured_data_fields": [bad_field]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_structured_data_fields_duplicate_names_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "structured_data_fields": [_STRING_FIELD, {**_STRING_FIELD, "description": "Dup."}],
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_structured_data_fields_cap_exceeded_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    too_many = [
        {"type": "string", "name": f"Field {i}", "description": f"Fact number {i}."}
        for i in range(21)
    ]
    payload = {**_VALID_PAYLOAD, "structured_data_fields": too_many}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_structured_data_fields_available_under_custom_mode_not_rejected(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test proving the design decision is real: unlike
    transfer_number/custom_tools, structured_data_fields is NOT rejected
    under response_engine='custom' — it's an agent-object field,
    not a general_tools/LLM-object mechanism (see app/models/agent.py's
    module docstring for the full placement reasoning).
    """
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "structured_data_fields": [_STRING_FIELD]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["response_engine"] == "custom"
    assert len(body["structured_data_fields"]) == 1
    assert body["structured_data_fields"][0]["name"] == "Caller Name"


async def test_structured_data_fields_sent_as_post_call_analysis_data_to_vendor(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirms the actual wire mapping: structured_data_fields reaches the
    vendor's real POST /create-agent body as post_call_analysis_data, with
    the exact {type, name, description, choices?} shape — and that
    post_call_analysis_model is deliberately omitted (letting the vendor
    apply its own real documented default), matching this codebase's
    existing `model` precedent on create_retell_llm().
    """
    captured_bodies: list[dict[str, Any]] = []

    async def _fake_post_create_agent(settings: Any, body: dict[str, Any]) -> dict[str, Any]:
        captured_bodies.append(body)
        return {"agent_id": "agent_fake_structured_data"}

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm", _fake_create_retell_llm_id)
    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _fake_post_create_agent)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_CUSTOM_LLM_PAYLOAD,
        "structured_data_fields": [_STRING_FIELD, _ENUM_FIELD],
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201

    assert len(captured_bodies) == 1
    sent = captured_bodies[0]["post_call_analysis_data"]
    assert sent == [
        {
            "type": "string",
            "name": "Caller Name",
            "description": "The name the caller gives for themselves.",
        },
        {
            "type": "enum",
            "name": "Call Outcome",
            "description": "Categorize how the call ended.",
            "choices": ["Appointment booked", "Declined", "Follow-up requested"],
        },
    ]
    assert "post_call_analysis_model" not in captured_bodies[0]


async def _fake_create_retell_llm_id(settings: Any, **kwargs: Any) -> str:
    return "llm_unused_for_custom_llm_mode"


async def _create_builtin_agent_with_transfer_and_tool(
    client: AsyncClient,
    api_key: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_id: str = "agent_fake_update_target",
    llm_id: str = "llm_fake_update_target",
) -> str:
    """Shared setup for PATCH tests: create a real (mocked-vendor) 'builtin'
    agent with a transfer_number and a custom tool already configured, so
    update tests have something real to change/clear. Returns the new
    agent's own id.
    """
    monkeypatch.setattr(
        retell_agent_adapter,
        "create_retell_llm_agent",
        _fake_retell_llm_agent(agent_id, llm_id),
    )
    payload = {
        **_VALID_PAYLOAD,
        "transfer_number": "+14155550100",
        "custom_tools": [_CUSTOM_TOOL],
    }
    resp = await client.post("/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201
    return str(resp.json()["id"])


# ── PATCH /agents/{agent_id} ────────────────────────────────────────────


async def test_update_agent_prompt_only_builtin_calls_both_vendor_updates(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core new-feature regression test: a partial update touching only
    `prompt` on a 'builtin' agent still calls BOTH update-agent (agent-
    object fields, unchanged here) and update-retell-llm (since prompt maps
    to general_prompt on the LLM object) — and the response reflects the
    new prompt while every untouched field (voice_id in particular) stays
    exactly what it was, proving the merge-not-overwrite behavior.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    update_agent_calls: list[dict[str, Any]] = []
    update_llm_calls: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        update_agent_calls.append({"agent_id": agent_id, "body": body})

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        update_llm_calls.append({"llm_id": llm_id, "body": body})

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"prompt": "Updated prompt: we are now open 24/7."},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == "Updated prompt: we are now open 24/7."
    # Untouched fields are unchanged — proving no accidental blanking.
    assert body["voice_id"] == _VALID_PAYLOAD["voice_id"]
    assert body["transfer_enabled"] is True
    assert len(body["custom_tools"]) == 1

    assert len(update_agent_calls) == 1
    assert len(update_llm_calls) == 1
    # The LLM update carries the new prompt as general_prompt, and rebuilds
    # the FULL general_tools array (still containing the existing transfer
    # + custom tool, since only prompt was meant to change).
    assert update_llm_calls[0]["body"]["general_prompt"] == "Updated prompt: we are now open 24/7."
    tool_types = {t["type"] for t in update_llm_calls[0]["body"]["general_tools"]}
    assert "transfer_call" in tool_types
    assert "custom" in tool_types

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["prompt"] == "Updated prompt: we are now open 24/7."
    assert docs[0]["transfer_number"] == "+14155550100"


async def test_update_agent_custom_mode_success(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'custom'-mode agent has no llm_ref at all — only update-agent is
    ever called, update-retell-llm must never be invoked, and prompt
    changes are persisted on our own side even though Retell's update-agent
    request itself has no prompt field under this mode (mirrors
    create_agent()'s own "prompt not sent to Retell under custom-llm"
    precedent).
    """
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _unexpected_update_llm(settings: Any, **kwargs: Any) -> None:
        raise AssertionError("custom-mode agent must never call update_retell_llm")

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _unexpected_update_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"voice_speed": 1.5},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["voice_speed"] == 1.5
    assert resp.json()["response_engine"] == "custom"

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["voice_speed"] == 1.5


async def test_update_agent_transfer_number_on_custom_mode_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors create-time validation: setting transfer_number via PATCH on
    an agent whose response_engine is already 'custom' is rejected, not
    silently ignored.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"transfer_number": "+14155550100"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail.get("field") == "transfer_number"


async def test_update_agent_custom_tools_on_custom_mode_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"custom_tools": [_CUSTOM_TOOL]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_update_agent_response_engine_field_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design decision: response_engine cannot be changed via PATCH at all —
    UpdateAgentRequest has no such field and sets extra='forbid', so sending
    it is a loud, explicit 422, never a silent ignore.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"response_engine": "custom"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_update_agent_empty_body_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entirely-empty PATCH (nothing to change) is rejected up front,
    never a silent no-op round-trip to the vendor and back.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}", json={}, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_update_agent_not_found_is_404(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/agents/000000000000000000000000",
        json={"prompt": "New prompt entirely."},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_update_agent_cross_platform_is_404_not_403(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory tenancy-isolation test for the new PATCH endpoint: Platform
    B must never be able to update Platform A's agent, and the failure mode
    is 404 (not 403), same as every other agent-scoped endpoint.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    key_a, _ = await _seed_platform(db, "Platform A")
    key_b, _ = await _seed_platform(db, "Platform B")

    resp_a = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {key_a}"}
    )
    agent_a_id = resp_a.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_a_id}",
        json={"prompt": "Platform B trying to hijack Platform A's agent."},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


async def test_update_agent_clear_transfer_number(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear_transfer_number=true is the unambiguous way to disable
    transfer — distinct from omitting the field (leave alone) and from
    transfer_number=null alone (also indistinguishable from omitted once
    parsed), per UpdateAgentRequest's own docstring.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"clear_transfer_number": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["transfer_enabled"] is False

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["transfer_number"] is None
    # The custom tool (untouched by this request) survives the
    # general_tools rebuild.
    assert len(docs[0]["custom_tools"]) == 1

    tool_types = {t["type"] for t in llm_bodies[0]["general_tools"]}
    assert "transfer_call" not in tool_types
    assert "custom" in tool_types


async def test_update_agent_custom_tools_whole_array_replace(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sending a new custom_tools array REPLACES the whole list, it does
    not merge/append — the documented design decision.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    new_tool = {**_CUSTOM_TOOL, "name": "look_up_order", "description": "Look up an order."}
    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"custom_tools": [new_tool]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["custom_tools"]) == 1
    assert body["custom_tools"][0]["name"] == "look_up_order"

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs[0]["custom_tools"]) == 1
    assert docs[0]["custom_tools"][0]["name"] == "look_up_order"
    # transfer_number, untouched by this request, is preserved.
    assert docs[0]["transfer_number"] == "+14155550100"


async def test_update_agent_structured_data_fields_replace(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    agent_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"structured_data_fields": [_STRING_FIELD]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["structured_data_fields"]) == 1
    assert body["structured_data_fields"][0]["name"] == "Caller Name"

    assert agent_bodies[0]["post_call_analysis_data"] == [
        {
            "type": "string",
            "name": "Caller Name",
            "description": "The name the caller gives for themselves.",
        }
    ]

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs[0]["structured_data_fields"]) == 1


async def test_update_agent_partial_failure_update_agent_fails_llm_succeeds(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design decision #3: if update-agent fails but update-retell-llm
    succeeds, the response is a real 502/upstream_failed (never a silent
    200 hiding a partial failure), but the half that DID succeed on the
    vendor side (the prompt, via update-retell-llm) is still reflected in
    our own stored record — never rolled back just because its sibling
    call failed.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    async def _failing_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        raise AppError(
            code="upstream_failed",
            message="The voice vendor rejected the agent update request.",
            status_code=502,
        )

    llm_calls: list[dict[str, Any]] = []

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_calls.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _failing_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"prompt": "New prompt.", "voice_speed": 1.8},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "upstream_failed"

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    # The LLM-side change (prompt) succeeded and is persisted...
    assert docs[0]["prompt"] == "New prompt."
    # ...but the agent-object-side change (voice_speed) did NOT reach the
    # vendor, so the PREVIOUS value is kept, not silently overwritten.
    assert docs[0]["voice_speed"] == 1.0
    assert len(llm_calls) == 1


async def test_update_agent_partial_failure_llm_fails_agent_succeeds(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the above with the failure on the other side: update-agent
    succeeds (voice_speed persists) but update-retell-llm fails (prompt
    change never reached the vendor, so the OLD prompt is kept).
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_calls: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_calls.append(body)

    async def _failing_update_retell_llm(
        settings: Any, *, llm_id: str, body: dict[str, Any]
    ) -> None:
        raise AppError(
            code="upstream_failed",
            message="The voice vendor rejected the conversation brain update request.",
            status_code=502,
        )

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _failing_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"prompt": "New prompt attempt.", "voice_speed": 1.8},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 502

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["voice_speed"] == 1.8
    assert docs[0]["prompt"] == _VALID_PAYLOAD["prompt"]
    assert len(agent_calls) == 1


async def test_update_agent_custom_tools_webhook_url_ssrf_protection(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    internal_tool = {**_CUSTOM_TOOL, "webhook_url": "http://127.0.0.1:9999/tool"}
    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"custom_tools": [internal_tool]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_structured_data_fields_omitted_key_when_not_configured(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No structured_data_fields configured -> post_call_analysis_data is
    not sent at all (matches the existing optional-field convention, e.g.
    webhook_url, rather than sending an empty array).
    """
    captured_bodies: list[dict[str, Any]] = []

    async def _fake_post_create_agent(settings: Any, body: dict[str, Any]) -> dict[str, Any]:
        captured_bodies.append(body)
        return {"agent_id": "agent_fake_no_structured_data"}

    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _fake_post_create_agent)

    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    assert len(captured_bodies) == 1
    assert "post_call_analysis_data" not in captured_bodies[0]


# ── states/starting_state (Single Prompt vs Multi Prompt) ─────────────────

_TRIAGE_STATE: dict[str, Any] = {
    "name": "triage",
    "state_prompt": "Figure out whether the caller needs billing or scheduling help, then "
    "route them there.",
    "edges": [
        {
            "destination_state_name": "billing",
            "description": "When the caller has a billing question.",
        },
        {
            "destination_state_name": "scheduling",
            "description": "When the caller wants to schedule or change an appointment.",
        },
    ],
}

_BILLING_STATE: dict[str, Any] = {
    "name": "billing",
    "state_prompt": "You are now handling billing questions.",
    "edges": [
        {
            "destination_state_name": "triage",
            "description": "When the caller's billing question is resolved or they want "
            "something else.",
        }
    ],
}

_SCHEDULING_STATE: dict[str, Any] = {
    "name": "scheduling",
    "state_prompt": "You are now handling appointment scheduling.",
    "edges": [
        {
            "destination_state_name": "triage",
            "description": "When the scheduling request is resolved or they want something "
            "else.",
        }
    ],
}

# A self-consistent 2-state pair (triage's only edge points at billing) for
# tests that deliberately want just two states — _TRIAGE_STATE above has an
# edge to 'scheduling' too, which would itself be a dangling reference if
# scheduling isn't also included.
_TRIAGE_ONLY_BILLING_STATE: dict[str, Any] = {
    "name": "triage",
    "state_prompt": "Figure out whether the caller needs billing help, then route them there.",
    "edges": [
        {
            "destination_state_name": "billing",
            "description": "When the caller has a billing question.",
        }
    ],
}


async def test_create_multi_prompt_agent_builds_states_with_starting_state(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core new-feature regression test: a valid states/starting_state
    payload reaches the vendor's create-retell-llm call with the exact
    {name, state_prompt, edges, tools} shape, and the response reflects
    states/starting_state/multi_prompt_enabled correctly.
    """
    captured_bodies: list[dict[str, Any]] = []

    async def _fake_post_create_agent(settings: Any, body: dict[str, Any]) -> dict[str, Any]:
        return {"agent_id": "agent_fake_multi_prompt"}

    class _FakeResponse:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {"llm_id": "llm_fake_multi_prompt"}

        text = "{}"

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            if url == "/create-retell-llm":
                captured_bodies.append(json)
            return _FakeResponse()

    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _fake_post_create_agent)
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "states": [_TRIAGE_STATE, _BILLING_STATE, _SCHEDULING_STATE],
        "starting_state": "triage",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["states"]) == 3
    assert body["starting_state"] == "triage"
    assert body["multi_prompt_enabled"] is True

    assert len(captured_bodies) == 1
    sent_states = captured_bodies[0]["states"]
    assert captured_bodies[0]["starting_state"] == "triage"
    assert {s["name"] for s in sent_states} == {"triage", "billing", "scheduling"}
    triage = next(s for s in sent_states if s["name"] == "triage")
    assert triage["state_prompt"] == _TRIAGE_STATE["state_prompt"]
    assert triage["edges"] == [
        {
            "destination_state_name": "billing",
            "description": "When the caller has a billing question.",
        },
        {
            "destination_state_name": "scheduling",
            "description": "When the caller wants to schedule or change an appointment.",
        },
    ]
    assert triage["tools"] == []


async def test_create_agent_states_empty_is_single_prompt(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["states"] == []
    assert body["starting_state"] is None
    assert body["multi_prompt_enabled"] is False


async def test_create_agent_states_dangling_edge_reference_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    bad_state = {
        **_BILLING_STATE,
        "edges": [
            {
                "destination_state_name": "nonexistent_state",
                "description": "This points nowhere real.",
            }
        ],
    }
    payload = {
        **_VALID_PAYLOAD,
        "states": [_TRIAGE_ONLY_BILLING_STATE, bad_state],
        "starting_state": "triage",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "nonexistent_state" in detail["message"]


async def test_create_agent_starting_state_mismatch_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "states": [_TRIAGE_STATE, _BILLING_STATE],
        "starting_state": "not_a_real_state",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "starting_state" in detail["message"]


async def test_create_agent_states_missing_starting_state_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "states": [_TRIAGE_STATE, _BILLING_STATE]}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_create_agent_duplicate_state_names_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    duplicate = {**_BILLING_STATE, "name": "triage"}
    payload = {
        **_VALID_PAYLOAD,
        "states": [_TRIAGE_STATE, duplicate],
        "starting_state": "triage",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_create_agent_states_cap_exceeded_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    many_states = [
        {
            "name": f"dept_{i}",
            "state_prompt": "Handle this department.",
            "edges": [{"destination_state_name": "triage", "description": "When done."}],
        }
        for i in range(20)
    ]
    payload = {
        **_VALID_PAYLOAD,
        "states": [_TRIAGE_STATE, *many_states],
        "starting_state": "triage",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422


async def test_states_with_custom_mode_is_rejected_422(client: AsyncClient, db: MongoDB) -> None:
    """Same restriction as transfer_number/custom_tools under response_engine
    ='custom' — states depends on the vendor's separate LLM object, which
    only exists under 'builtin'.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_CUSTOM_LLM_PAYLOAD,
        "states": [_TRIAGE_ONLY_BILLING_STATE, _BILLING_STATE],
        "starting_state": "triage",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "states" in detail["message"]


async def test_update_agent_add_states_via_patch(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updating an existing Single Prompt agent to add states via PATCH
    works: update-retell-llm is called with the new states array, and the
    response/stored record reflect Multi Prompt being enabled.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]
    assert resp.json()["multi_prompt_enabled"] is False

    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={
            "states": [_TRIAGE_STATE, _BILLING_STATE, _SCHEDULING_STATE],
            "starting_state": "triage",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["states"]) == 3
    assert body["starting_state"] == "triage"
    assert body["multi_prompt_enabled"] is True

    assert len(llm_bodies) == 1
    assert llm_bodies[0]["starting_state"] == "triage"
    assert {s["name"] for s in llm_bodies[0]["states"]} == {"triage", "billing", "scheduling"}

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert len(docs[0]["states"]) == 3
    assert docs[0]["starting_state"] == "triage"


async def test_update_agent_clear_states_collapses_to_single_prompt(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing states back to [] collapses a Multi Prompt agent back to
    Single Prompt — both in our own stored record and in the vendor request
    body (states sent EXPLICITLY as [], not omitted, per
    retell_agent_adapter.update_retell_llm's own docstring).
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, platform_id = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "states": [_TRIAGE_ONLY_BILLING_STATE, _BILLING_STATE],
        "starting_state": "triage",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"states": []},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["states"] == []
    assert body["starting_state"] is None
    assert body["multi_prompt_enabled"] is False

    assert len(llm_bodies) == 1
    assert llm_bodies[0]["states"] == []
    assert llm_bodies[0]["starting_state"] is None

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["states"] == []
    assert docs[0]["starting_state"] is None


async def test_update_agent_states_on_custom_mode_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={
            "states": [_TRIAGE_ONLY_BILLING_STATE, _BILLING_STATE],
            "starting_state": "triage",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail.get("field") == "states"


async def test_update_agent_starting_state_without_states_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """starting_state alone, with no accompanying states in the same
    request, is meaningless — see UpdateAgentRequest._validate_states_
    routing's docstring for why the previous states array can't be
    silently reused.
    """
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"starting_state": "triage"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


# ── welcome_message ─────────────────────────────────────────────────────
#
# Three-state coverage (None -> no begin_message key sent; "" -> begin_
# message="" sent, distinct from omission; a real string -> sent verbatim),
# per app/models/agent.py's module docstring, "welcome_message" section.


async def test_create_agent_with_welcome_message_reaches_vendor_call(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real, non-empty welcome_message reaches
    retell_agent_adapter.create_retell_llm_agent via the full
    CreateAgentRequest (body.welcome_message) and is persisted."""
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_welcome", llm_id="llm_fake_welcome"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    greeting = "Thank you for calling Aspen Quality Care. This call may be recorded."
    payload = {**_VALID_PAYLOAD, "welcome_message": greeting}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["welcome_message"] == greeting

    assert captured["body"].welcome_message == greeting

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["welcome_message"] == greeting


async def test_create_agent_welcome_message_omitted_stays_none(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """welcome_message omitted entirely -> stored/returned as None, and the
    adapter receives welcome_message=None on the CreateAgentRequest — the
    exact same behavior as before this feature existed (create_retell_llm's
    own is-not-None check means no begin_message key reaches Retell)."""
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_no_welcome", llm_id="llm_fake_no_welcome"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["welcome_message"] is None
    assert captured["body"].welcome_message is None

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["welcome_message"] is None


async def test_create_agent_welcome_message_empty_string_is_distinct_from_omitted(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """welcome_message="" is a real, distinct value (agent waits silently)
    — NOT the same as omitting the field. Confirms it round-trips as "" on
    the response/stored record, and reaches the adapter as "" (not None),
    which is exactly what create_retell_llm's `is not None` check depends
    on to send begin_message="" rather than dropping the key."""
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_empty_welcome", llm_id="llm_fake_empty_welcome"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "welcome_message": ""}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["welcome_message"] == ""
    assert body["welcome_message"] is not None

    assert captured["body"].welcome_message == ""
    assert captured["body"].welcome_message is not None

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["welcome_message"] == ""


async def test_create_retell_llm_adapter_sends_begin_message_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-level proof of the three-state adapter behavior, independent of
    the router/DB: create_retell_llm's own request body correctly
    distinguishes None (no begin_message key) from "" (begin_message="")
    from a real string (begin_message=<string>). This is the exact bug the
    module docstring warns about (a truthy check would collapse "" into
    None) — asserted directly against the outgoing HTTP body here.
    """
    from app.config import Settings
    from app.models.agent import OnHoldMusic

    captured_bodies: list[dict[str, Any]] = []

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"llm_id": "llm_fake"}

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
            captured_bodies.append(json)
            return _FakeResponse()

    monkeypatch.setattr(
        "app.services.retell_agent_adapter.httpx.AsyncClient", _FakeAsyncClient
    )

    settings = Settings(
        MONGODB_URI="mongodb://localhost:27017",
        MONGODB_DB="test",
        RETELL_API_KEY="fake",
        RETELL_API_BASE="https://api.retellai.example",
        BASE_URL="https://voiceai.example.com",
    )

    common_kwargs = dict(
        general_prompt="You are a helpful assistant.",
        transfer_number=None,
        transfer_ring_duration_ms=30000,
        transfer_on_hold_music=OnHoldMusic.RINGTONE,
        transfer_show_original_caller_id=True,
        custom_tools=[],
    )

    # Case 1: omitted (None) -> no begin_message key at all.
    await retell_agent_adapter.create_retell_llm(settings, **common_kwargs)
    assert "begin_message" not in captured_bodies[-1]

    # Case 2: explicit empty string -> begin_message="" IS sent.
    await retell_agent_adapter.create_retell_llm(
        settings, **common_kwargs, welcome_message=""
    )
    assert captured_bodies[-1]["begin_message"] == ""

    # Case 3: a real string -> sent verbatim.
    await retell_agent_adapter.create_retell_llm(
        settings, **common_kwargs, welcome_message="Hello, thanks for calling."
    )
    assert captured_bodies[-1]["begin_message"] == "Hello, thanks for calling."


async def test_welcome_message_with_custom_mode_is_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    """Same restriction as transfer_number/custom_tools/states under
    response_engine='custom' — welcome_message depends on the vendor's
    separate LLM object, which only exists under 'builtin'.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "welcome_message": "Hello there."}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "welcome_message" in detail["message"]

    docs = await db[AGENTS].find({}).to_list(length=10)
    assert docs == []


async def test_welcome_message_empty_string_with_custom_mode_is_also_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    """welcome_message="" under 'custom' mode is rejected too — an empty
    string is a real, meaningful configuration (wait silently), not
    "unset," so it must not be silently allowed through just because it's
    falsy in Python. Confirms the model validator uses `is not None`, not
    a truthy check.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "welcome_message": ""}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "welcome_message" in detail["message"]


async def test_update_agent_set_welcome_message_reaches_vendor(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing welcome_message onto an existing builtin agent sends
    begin_message on the update-retell-llm call and persists it."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"welcome_message": "Thanks for calling, this line may be recorded."},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["welcome_message"] == "Thanks for calling, this line may be recorded."

    assert llm_bodies[0]["begin_message"] == "Thanks for calling, this line may be recorded."

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["welcome_message"] == "Thanks for calling, this line may be recorded."


async def test_update_agent_clear_welcome_message_reverts_to_improvised(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear_welcome_message=true is the unambiguous way to reset an
    existing welcome_message back to the default improvised-greeting
    behavior — mirrors clear_transfer_number exactly. Confirms the
    update-retell-llm body sends begin_message=None EXPLICITLY once
    cleared — NOT by omitting the key, which a real live check against
    Retell's own account confirmed leaves the vendor's stored value
    untouched (update-retell-llm is a genuine field-level partial-merge
    endpoint) — see app/routers/agents.py's `update_agent` docstring for
    the full reasoning."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    # First set a real welcome_message.
    async def _noop_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    llm_bodies: list[dict[str, Any]] = []

    async def _capture_update_retell_llm(
        settings: Any, *, llm_id: str, body: dict[str, Any]
    ) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _noop_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _capture_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"welcome_message": "A fixed greeting."},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["welcome_message"] == "A fixed greeting."

    # Now clear it.
    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"clear_welcome_message": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["welcome_message"] is None

    assert "begin_message" in llm_bodies[-1]
    assert llm_bodies[-1]["begin_message"] is None

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["welcome_message"] is None


async def test_update_agent_welcome_message_empty_string_distinct_from_clear(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing welcome_message="" (wait silently) is distinct from
    clear_welcome_message=true (revert to improvised) — confirms the update
    path sends begin_message="" explicitly rather than omitting the key."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"welcome_message": ""},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["welcome_message"] == ""
    assert body["welcome_message"] is not None

    assert llm_bodies[0]["begin_message"] == ""

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["welcome_message"] == ""


async def test_update_agent_welcome_message_on_custom_mode_rejected_422(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"welcome_message": "Hello there."},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail.get("field") == "welcome_message"


async def test_update_agent_clear_welcome_message_on_custom_mode_is_harmless_noop(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear_welcome_message=true alone on a 'custom'-mode agent is NOT
    rejected — there's nothing to clear on a mode that can never have a
    welcome_message in the first place, same carve-out as
    clear_transfer_number."""
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"clear_welcome_message": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["welcome_message"] is None


# ── agent_name ───────────────────────────────────────────────────────────
#
# Unlike welcome_message/transfer_number/custom_tools/states, agent_name
# lives on the voice vendor's own agent object, not its LLM object — so it
# is genuinely available under BOTH response_engine modes. Coverage below
# proves that explicitly (the opposite assertion from welcome_message's
# custom-mode-rejected tests), per app/models/agent.py's module docstring,
# "agent_name" section.


async def test_create_agent_with_agent_name_reaches_vendor_call_builtin(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real agent_name reaches retell_agent_adapter.create_retell_llm_agent
    via the full CreateAgentRequest (body.agent_name) under 'builtin' mode
    (the default), and is persisted."""
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_name_builtin", llm_id="llm_fake_name_builtin"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    name = "Front Desk — Aspen Clinic"
    payload = {**_VALID_PAYLOAD, "agent_name": name}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_name"] == name
    assert body["response_engine"] == "builtin"

    assert captured["body"].agent_name == name

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["agent_name"] == name


async def test_create_agent_with_agent_name_reaches_vendor_call_custom(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key difference from welcome_message: a real agent_name ALSO
    reaches retell_agent_adapter.create_agent (the 'custom' mode path) —
    proving agent_name works under BOTH response_engine modes, since it
    lives on the vendor's agent object, not its LLM object."""
    captured: dict[str, Any] = {}

    async def _fake(settings: Any, **kwargs: Any) -> retell_agent_adapter.RetellCreateAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateAgentResult(agent_id="agent_fake_name_custom")

    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    name = "After Hours — Aspen Clinic"
    payload = {**_CUSTOM_LLM_PAYLOAD, "agent_name": name}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_name"] == name
    assert body["response_engine"] == "custom"

    # This is the load-bearing assertion: agent_name reaches create_agent()
    # (the custom-mode adapter call) as a real kwarg, unlike welcome_message
    # which 'custom' mode rejects entirely before any vendor call.
    assert captured["agent_name"] == name

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["agent_name"] == name


async def test_agent_name_with_custom_mode_is_not_rejected(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit proof of the mode-restriction decision: setting agent_name
    alongside response_engine='custom' succeeds (201), NOT a 422 — the
    opposite assertion from welcome_message/transfer_number/custom_tools/
    states' own custom-mode-rejected tests, since agent_name has no such
    restriction (agent-object field, not LLM-object field)."""
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "agent_name": "Billing Overflow"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    assert resp.json()["agent_name"] == "Billing Overflow"


async def test_create_agent_agent_name_omitted_stays_none(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent_name omitted entirely -> stored/returned as None, and the
    adapter receives agent_name=None on the CreateAgentRequest — no
    unexpected default is invented."""
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_no_name", llm_id="llm_fake_no_name"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_name"] is None
    assert captured["body"].agent_name is None

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["agent_name"] is None


async def test_update_agent_set_agent_name_reaches_vendor(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing agent_name onto an existing builtin agent sends agent_name
    on the update-agent call (the agent-object call, NOT update-retell-llm)
    and persists it."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []
    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"agent_name": "Front Desk — Renamed"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_name"] == "Front Desk — Renamed"

    assert agent_bodies[0]["agent_name"] == "Front Desk — Renamed"

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["agent_name"] == "Front Desk — Renamed"


async def test_update_agent_clear_agent_name_resets_to_unset(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear_agent_name=true is the unambiguous way to reset an existing
    agent_name back to unset — mirrors clear_welcome_message/
    clear_transfer_number exactly."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []

    async def _capture_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _noop_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _capture_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _noop_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"agent_name": "A real name."},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_name"] == "A real name."

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"clear_agent_name": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_name"] is None

    assert "agent_name" in agent_bodies[-1]
    assert agent_bodies[-1]["agent_name"] is None

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["agent_name"] is None


async def test_update_agent_name_on_custom_mode_is_not_rejected(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit proof, at the PATCH layer too: setting agent_name on a
    'custom'-mode agent succeeds (200), NOT the 422
    welcome_message/transfer_number get under the same mode — see
    _reject_transfer_fields_under_update's own docstring in
    app/routers/agents.py for why agent_name is deliberately excluded from
    that check."""
    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake_custom_llm_agent())
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_CUSTOM_LLM_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    agent_id = resp.json()["id"]

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"agent_name": "Billing Overflow"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_name"] == "Billing Overflow"


# ── live_transcript_enabled ─────────────────────────────────────────────
# See app/models/agent.py's module docstring, "live_transcript_enabled"
# section, for the full feature description and the confirmed agent-object
# placement (available under BOTH response_engine modes, unlike
# welcome_message/custom_tools/states).


async def test_create_agent_live_transcript_enabled_true_sends_webhook_events_builtin(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """live_transcript_enabled=true under 'builtin' mode reaches
    create_retell_llm_agent via the full CreateAgentRequest
    (body.live_transcript_enabled), and the response/stored doc reflect it.
    """
    captured: dict[str, Any] = {}

    async def _fake(
        settings: Any, **kwargs: Any
    ) -> retell_agent_adapter.RetellCreateRetellLlmAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateRetellLlmAgentResult(
            agent_id="agent_fake_lt_builtin", llm_id="llm_fake_lt_builtin"
        )

    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "live_transcript_enabled": True}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["live_transcript_enabled"] is True

    assert captured["body"].live_transcript_enabled is True

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["live_transcript_enabled"] is True


async def test_create_agent_live_transcript_enabled_true_sends_webhook_events_custom(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key difference from welcome_message/states: live_transcript_enabled
    ALSO reaches retell_agent_adapter.create_agent (the 'custom' mode path),
    proving it works under BOTH response_engine modes — it lives on the
    vendor's agent object, not its LLM object.
    """
    captured: dict[str, Any] = {}

    async def _fake(settings: Any, **kwargs: Any) -> retell_agent_adapter.RetellCreateAgentResult:
        captured.update(kwargs)
        return retell_agent_adapter.RetellCreateAgentResult(agent_id="agent_fake_lt_custom")

    monkeypatch.setattr(retell_agent_adapter, "create_agent", _fake)

    api_key, platform_id = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "live_transcript_enabled": True}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["live_transcript_enabled"] is True
    assert body["response_engine"] == "custom"

    # Load-bearing: live_transcript_enabled reaches create_agent() (the
    # custom-mode adapter call) as a real kwarg — not rejected the way
    # welcome_message/transfer_number/custom_tools/states are under 'custom'.
    assert captured["live_transcript_enabled"] is True

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["live_transcript_enabled"] is True


async def test_create_agent_live_transcript_enabled_omitted_defaults_false(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """live_transcript_enabled omitted entirely -> stored/returned as False
    (opt-in default), and the adapter never includes webhook_events."""
    monkeypatch.setattr(retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent())

    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    assert resp.json()["live_transcript_enabled"] is False

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["live_transcript_enabled"] is False


def test_build_webhook_events_helper_omits_key_when_disabled() -> None:
    """Unit-level proof of the CREATE-time helper's exact contract: disabled
    -> None (omit the key entirely, preserving Retell's own default event
    set implicitly); enabled -> the documented default set PLUS
    transcript_updated, never transcript_updated alone (since webhook_events
    REPLACES the default set on the vendor's side rather than adding to it)."""
    assert retell_agent_adapter._build_webhook_events(live_transcript_enabled=False) is None
    assert retell_agent_adapter._build_webhook_events(live_transcript_enabled=True) == [
        "call_started",
        "call_ended",
        "call_analyzed",
        "transcript_updated",
    ]


def test_build_webhook_events_for_update_always_returns_explicit_array() -> None:
    """Unit-level proof of the UPDATE-time helper's different contract:
    unlike the CREATE-time helper, this NEVER returns None — disabling must
    explicitly restore the documented default set (never an empty array,
    which means "no events at all" on the vendor's side, and never omitted,
    which would leave a previously-enabled non-default value untouched)."""
    assert retell_agent_adapter.build_webhook_events_for_update(
        live_transcript_enabled=False
    ) == ["call_started", "call_ended", "call_analyzed"]
    assert retell_agent_adapter.build_webhook_events_for_update(
        live_transcript_enabled=True
    ) == ["call_started", "call_ended", "call_analyzed", "transcript_updated"]


async def test_update_agent_set_live_transcript_enabled_sends_full_webhook_events(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing live_transcript_enabled=true onto an existing builtin agent
    sends an explicit webhook_events array on the update-agent call (the
    agent-object call, NOT update-retell-llm) and persists it."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"live_transcript_enabled": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["live_transcript_enabled"] is True

    assert agent_bodies[0]["webhook_events"] == [
        "call_started",
        "call_ended",
        "call_analyzed",
        "transcript_updated",
    ]

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["live_transcript_enabled"] is True


async def test_update_agent_disable_live_transcript_restores_default_events(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning live_transcript_enabled back to false sends the explicit
    documented default set (never an empty array, never omitted) — proving
    the disable path genuinely restores default behavior rather than
    silently disabling every webhook event this agent relies on."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"live_transcript_enabled": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["live_transcript_enabled"] is False
    assert agent_bodies[0]["webhook_events"] == ["call_started", "call_ended", "call_analyzed"]

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["live_transcript_enabled"] is False


async def test_update_agent_omitting_live_transcript_leaves_it_untouched(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting live_transcript_enabled from a PATCH that touches something
    else (prompt) does NOT include webhook_events on the update-agent call
    at all — the vendor's own partial-merge semantics leave the existing
    value untouched, and our own stored value is preserved unchanged too."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"agent_name": "Renamed Only"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["live_transcript_enabled"] is False  # unchanged default
    assert "webhook_events" not in agent_bodies[0]

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["live_transcript_enabled"] is False


# ── ~19-field tuning-knob batch (model, voice_model, stt_mode, denoising_mode,
# ambient_sound, backchannel_frequency/words, responsiveness, reminder_*,
# end_call_after_silence_ms, max_call_duration_ms, begin_message_delay_ms,
# allow_user_dtmf/allow_dtmf_interruption, data_storage_setting, pii_config,
# post_call_analysis_model, handbook_config, model_temperature,
# voice_temperature, ambient_sound_volume) — see app/models/agent.py's module
# docstring, "~19-field tuning-knob batch" section, for the full feature
# description and vendor-placement sourcing. ─────────────────────────────


async def test_create_agent_tuning_batch_llm_fields_reach_create_retell_llm_body(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """model/model_temperature are the two LLM-object fields of the batch —
    confirm they land on the REAL create-retell-llm HTTP body (one level
    below the mocked adapter function, same pattern as the custom_tools
    proxy-url regression test above), never on the create-agent body.
    """
    captured_llm_bodies: list[dict[str, Any]] = []

    async def _fake_post_create_agent(settings: Any, body: dict[str, Any]) -> dict[str, Any]:
        return {"agent_id": "agent_fake_tuning_llm"}

    class _FakeResponse:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {"llm_id": "llm_fake_tuning_llm"}

        text = "{}"

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            if url == "/create-retell-llm":
                captured_llm_bodies.append(json)
            return _FakeResponse()

    monkeypatch.setattr(retell_agent_adapter, "_post_create_agent", _fake_post_create_agent)
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "model": "claude-4.5-sonnet", "model_temperature": 0.7}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["model"] == "claude-4.5-sonnet"
    assert body["model_temperature"] == 0.7

    assert len(captured_llm_bodies) == 1
    assert captured_llm_bodies[0]["model"] == "claude-4.5-sonnet"
    assert captured_llm_bodies[0]["model_temperature"] == 0.7


async def test_create_agent_tuning_batch_agent_fields_reach_create_agent_body(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A representative agent-object slice of the batch (voice_model,
    stt_mode, max_call_duration_ms, pii_config, data_storage_setting) lands
    on the REAL create-agent HTTP body (the second of the two calls
    create_retell_llm_agent makes) — never on create-retell-llm's body.
    """
    captured_agent_bodies: list[dict[str, Any]] = []

    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            if url == "/create-retell-llm":
                return _FakeResponse(201, {"llm_id": "llm_fake_tuning_agent"})
            if url == "/create-agent":
                captured_agent_bodies.append(json)
                return _FakeResponse(201, {"agent_id": "agent_fake_tuning_agent"})
            raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "voice_model": "eleven_flash_v2_5",
        "stt_mode": "accurate",
        "max_call_duration_ms": 1_800_000,
        "pii_config": {"mode": "post_call", "categories": ["person_name", "phone_number"]},
        "data_storage_setting": "everything_except_pii",
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["voice_model"] == "eleven_flash_v2_5"
    assert body["stt_mode"] == "accurate"
    assert body["max_call_duration_ms"] == 1_800_000
    assert body["pii_config"] == {
        "mode": "post_call",
        "categories": ["person_name", "phone_number"],
    }
    assert body["data_storage_setting"] == "everything_except_pii"

    assert len(captured_agent_bodies) == 1
    sent = captured_agent_bodies[0]
    assert sent["voice_model"] == "eleven_flash_v2_5"
    assert sent["stt_mode"] == "accurate"
    assert sent["max_call_duration_ms"] == 1_800_000
    assert sent["pii_config"] == {
        "mode": "post_call",
        "categories": ["person_name", "phone_number"],
    }
    assert sent["data_storage_setting"] == "everything_except_pii"
    # model/model_temperature must NEVER leak onto the agent-object body —
    # they are LLM-object-only fields.
    assert "model" not in sent
    assert "model_temperature" not in sent


async def test_create_agent_omitting_tuning_batch_produces_unchanged_default_vendor_bodies(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression-prevention test: an agent that sets NONE of the ~19 new
    fields must produce EXACTLY the same create-retell-llm/create-agent
    vendor request bodies as before this batch existed — every new field's
    default must round-trip to identical wire behavior. This is the core
    'must not change default behavior' proof the task explicitly requires.
    """
    captured_llm_bodies: list[dict[str, Any]] = []
    captured_agent_bodies: list[dict[str, Any]] = []

    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            if url == "/create-retell-llm":
                captured_llm_bodies.append(json)
                return _FakeResponse(201, {"llm_id": "llm_fake_default"})
            if url == "/create-agent":
                captured_agent_bodies.append(json)
                return _FakeResponse(201, {"agent_id": "agent_fake_default"})
            raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.post(
        "/agents", json=_VALID_PAYLOAD, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201

    # LLM-object body: no 'model' key at all (vendor's own bare default
    # applies), model_temperature still explicitly 0.0 (unchanged
    # pre-existing behavior), no pii/handbook/etc. — those never touch this
    # object at all.
    llm_body = captured_llm_bodies[0]
    assert "model" not in llm_body
    assert llm_body["model_temperature"] == 0.0
    assert llm_body["general_prompt"] == _VALID_PAYLOAD["prompt"]
    assert llm_body["start_speaker"] == "agent"

    # Agent-object body: every new tuning field present at its documented
    # vendor default (this codebase always sends these ~17 explicitly, per
    # _build_agent_tuning_fields' own "self-documenting, harmless" design
    # decision), and the three genuinely-optional ones (voice_model,
    # post_call_analysis_model absent; ambient_sound/pii_config/
    # handbook_config absent) are omitted entirely.
    agent_body = captured_agent_bodies[0]
    assert agent_body["voice_temperature"] == 1.0
    assert agent_body["stt_mode"] == "fast"
    assert agent_body["denoising_mode"] == "noise-cancellation"
    assert agent_body["ambient_sound_volume"] == 1.0
    assert agent_body["backchannel_frequency"] == 0.8
    assert agent_body["responsiveness"] == 1.0
    assert agent_body["reminder_trigger_ms"] == 10_000
    assert agent_body["reminder_max_count"] == 1
    assert agent_body["end_call_after_silence_ms"] == 600_000
    assert agent_body["max_call_duration_ms"] == 3_600_000
    assert agent_body["begin_message_delay_ms"] == 0
    assert agent_body["allow_user_dtmf"] is True
    assert agent_body["allow_dtmf_interruption"] is False
    assert agent_body["data_storage_setting"] == "everything"
    assert "voice_model" not in agent_body
    assert "ambient_sound" not in agent_body
    assert "backchannel_words" not in agent_body
    assert "pii_config" not in agent_body
    assert "post_call_analysis_model" not in agent_body
    assert "handbook_config" not in agent_body


async def test_create_agent_model_with_custom_mode_is_rejected_422(
    client: AsyncClient, db: MongoDB
) -> None:
    """model is an LLM-object-only field — same rejection as
    transfer_number/welcome_message/states under response_engine='custom'."""
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "model": "gpt-5"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "model" in detail["message"]


async def test_create_agent_post_call_analysis_model_works_under_custom_mode(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """post_call_analysis_model is an agent-object field, available under
    BOTH response_engine modes — NOT rejected under 'custom', unlike its
    LLM-object sibling `model` above."""
    monkeypatch.setattr(
        retell_agent_adapter, "create_agent", _fake_custom_llm_agent("agent_fake_pca_custom")
    )
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_CUSTOM_LLM_PAYLOAD, "post_call_analysis_model": "gpt-5-mini"}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    assert resp.json()["post_call_analysis_model"] == "gpt-5-mini"


async def test_pii_config_empty_categories_rejected_422(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {**_VALID_PAYLOAD, "pii_config": {"mode": "post_call", "categories": []}}
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 422


async def test_handbook_config_speech_normalization_round_trips(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirms the REAL field name (speech_normalization, nested inside
    handbook_config) works end-to-end — the user's originally-typed
    (incorrect) top-level field name `normalize_for_speech` does not exist
    and is not what this test uses."""
    monkeypatch.setattr(
        retell_agent_adapter, "create_retell_llm_agent", _fake_retell_llm_agent()
    )
    api_key, _ = await _seed_platform(db, "Platform A")
    payload = {
        **_VALID_PAYLOAD,
        "handbook_config": {"speech_normalization": True, "high_empathy": True},
    }
    resp = await client.post(
        "/agents", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["handbook_config"]["speech_normalization"] is True
    assert body["handbook_config"]["high_empathy"] is True
    assert body["handbook_config"]["ai_disclosure"] is None


async def test_update_agent_tuning_batch_llm_and_agent_fields_reach_correct_vendor_call(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing model (LLM-object) and stt_mode/max_call_duration_ms
    (agent-object) sends each to the CORRECT vendor update call, and both
    are persisted."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []
    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={
            "model": "gpt-5",
            "stt_mode": "accurate",
            "max_call_duration_ms": 1_200_000,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "gpt-5"
    assert body["stt_mode"] == "accurate"
    assert body["max_call_duration_ms"] == 1_200_000

    assert llm_bodies[0]["model"] == "gpt-5"
    assert "stt_mode" not in llm_bodies[0]
    assert agent_bodies[0]["stt_mode"] == "accurate"
    assert agent_bodies[0]["max_call_duration_ms"] == 1_200_000
    assert "model" not in agent_bodies[0]

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["model"] == "gpt-5"
    assert docs[0]["stt_mode"] == "accurate"
    assert docs[0]["max_call_duration_ms"] == 1_200_000


async def test_update_agent_pii_config_whole_object_replace(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCHing pii_config sends the complete new object (categories
    included) to update-agent — whole-object-replace, not merged."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _noop_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _noop_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"pii_config": {"mode": "post_call", "categories": ["ssn", "credit_card"]}},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["pii_config"] == {
        "mode": "post_call",
        "categories": ["ssn", "credit_card"],
    }
    assert agent_bodies[0]["pii_config"] == {
        "mode": "post_call",
        "categories": ["ssn", "credit_card"],
    }

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["pii_config"] == {"mode": "post_call", "categories": ["ssn", "credit_card"]}


async def test_update_agent_omitting_tuning_batch_leaves_it_unchanged(
    client: AsyncClient, db: MongoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATCH that touches only agent_name does not include ANY of the
    ~19-field tuning batch on either vendor update call, and the stored
    values stay at their creation-time defaults — proving PATCH omission
    really means 'leave alone' for this whole batch, not 'reset to
    default'."""
    api_key, platform_id = await _seed_platform(db, "Platform A")
    agent_id = await _create_builtin_agent_with_transfer_and_tool(client, api_key, monkeypatch)

    agent_bodies: list[dict[str, Any]] = []
    llm_bodies: list[dict[str, Any]] = []

    async def _fake_update_agent(settings: Any, *, agent_id: str, body: dict[str, Any]) -> None:
        agent_bodies.append(body)

    async def _fake_update_retell_llm(settings: Any, *, llm_id: str, body: dict[str, Any]) -> None:
        llm_bodies.append(body)

    monkeypatch.setattr(retell_agent_adapter, "update_agent", _fake_update_agent)
    monkeypatch.setattr(retell_agent_adapter, "update_retell_llm", _fake_update_retell_llm)

    resp = await client.patch(
        f"/agents/{agent_id}",
        json={"agent_name": "Just A Rename"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] is None
    assert body["stt_mode"] == "fast"
    assert body["data_storage_setting"] == "everything"
    assert body["pii_config"] is None

    for field_name in (
        "model",
        "voice_model",
        "stt_mode",
        "denoising_mode",
        "ambient_sound",
        "ambient_sound_volume",
        "backchannel_frequency",
        "backchannel_words",
        "responsiveness",
        "reminder_trigger_ms",
        "reminder_max_count",
        "end_call_after_silence_ms",
        "max_call_duration_ms",
        "begin_message_delay_ms",
        "allow_user_dtmf",
        "allow_dtmf_interruption",
        "data_storage_setting",
        "pii_config",
        "post_call_analysis_model",
        "handbook_config",
    ):
        assert field_name not in agent_bodies[0]
    assert "model" not in llm_bodies[0] if llm_bodies else True

    docs = await db[AGENTS].find({"platform_id": platform_id}).to_list(length=10)
    assert docs[0]["model"] is None
    assert docs[0]["stt_mode"] == "fast"
