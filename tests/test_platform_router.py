"""Integration tests for PATCH /platform — set/clear the calling platform's
own `inbound_variables_webhook_url`.

No real network/DNS calls are stubbed out here for the success-path tests:
`socket.getaddrinfo` genuinely resolves real, well-known public hostnames
(e.g. example.com) and loopback/private literals (which resolve without any
network call at all, since they're already valid IP literals) — this is
consistent with the project's "monkeypatch only the VENDOR adapter" rule;
DNS resolution isn't a vendor call, it's a local standard-library operation,
and using a real public hostname keeps the test honest about what the
SSRF-adjacent check actually does.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.database import MongoDB
from app.repositories import platform_repo
from app.security import generate_api_key, hash_api_key, key_display_prefix


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


async def test_missing_auth_is_401(client: AsyncClient) -> None:
    resp = await client.patch(
        "/platform", json={"inbound_variables_webhook_url": "https://example.com/webhook"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


async def test_set_webhook_url_success(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "https://example.com/voiceai/variables"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["inbound_variables_webhook_url"] == "https://example.com/voiceai/variables"
    assert body["id"] == platform_id

    # Confirm it actually persisted, not just echoed in the response.
    persisted = await platform_repo.get_by_id(db, platform_id)
    assert persisted is not None
    assert persisted.inbound_variables_webhook_url == "https://example.com/voiceai/variables"


async def test_unset_webhook_url_with_null(client: AsyncClient, db: MongoDB) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://example.com/voiceai/variables"
    )

    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": None},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["inbound_variables_webhook_url"] is None

    persisted = await platform_repo.get_by_id(db, platform_id)
    assert persisted is not None
    assert persisted.inbound_variables_webhook_url is None


async def test_omitted_field_defaults_to_null_and_clears(client: AsyncClient, db: MongoDB) -> None:
    """The field is optional with a `None` default — an empty body is
    equivalent to explicitly sending null, per UpdatePlatformSettingsRequest.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    await platform_repo.set_inbound_variables_webhook_url(
        db, platform_id, url="https://example.com/voiceai/variables"
    )

    resp = await client.patch(
        "/platform",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["inbound_variables_webhook_url"] is None


async def test_invalid_url_format_is_422(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "not-a-url"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_cross_platform_isolation_no_id_param(client: AsyncClient, db: MongoDB) -> None:
    """There is no id parameter to even attempt cross-platform access with —
    this proves Platform A's PATCH call only ever affects its own record,
    never Platform B's, which is the whole point of resolving identity from
    the API key rather than a path parameter.
    """
    key_a, id_a = await _seed_platform(db, "Platform A")
    key_b, id_b = await _seed_platform(db, "Platform B")

    await platform_repo.set_inbound_variables_webhook_url(
        db, id_b, url="https://example.com/platform-b-original"
    )

    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "https://example.com/platform-a-set-this"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == id_a

    platform_b = await platform_repo.get_by_id(db, id_b)
    assert platform_b is not None
    assert platform_b.inbound_variables_webhook_url == "https://example.com/platform-b-original"

    # Platform B's own call still works normally and only touches its own record.
    resp_b = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": None},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["id"] == id_b

    platform_a = await platform_repo.get_by_id(db, id_a)
    assert platform_a is not None
    assert platform_a.inbound_variables_webhook_url == "https://example.com/platform-a-set-this"


async def test_loopback_url_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "http://127.0.0.1:8000/variables"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert resp.json()["detail"]["field"] == "inbound_variables_webhook_url"


async def test_localhost_hostname_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "http://localhost:8000/variables"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_private_network_ip_is_rejected(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "http://10.0.0.5/variables"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


async def test_response_never_leaks_api_key_hash(client: AsyncClient, db: MongoDB) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "https://example.com/voiceai/variables"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert "api_key_hash" not in resp.json()


# ── call_completed_webhook_url + per-platform signing secret ───────────────


async def test_set_call_completed_webhook_url_generates_secret_once(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"call_completed_webhook_url": "https://example.com/voiceai/call-completed"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["call_completed_webhook_url"] == "https://example.com/voiceai/call-completed"
    secret = body["call_completed_webhook_secret"]
    assert secret is not None
    assert len(secret) > 20  # real high-entropy token, not a placeholder

    persisted = await platform_repo.get_by_id(db, platform_id)
    assert persisted is not None
    assert persisted.call_completed_webhook_secret == secret


async def test_secret_not_regenerated_on_subsequent_url_updates(
    client: AsyncClient, db: MongoDB
) -> None:
    """The secret is generated exactly once — changing the URL again later
    (while a secret already exists) must not silently rotate it out from
    under a platform that already saved the original value.
    """
    api_key, platform_id = await _seed_platform(db, "Platform A")
    first = await client.patch(
        "/platform",
        json={"call_completed_webhook_url": "https://example.com/voiceai/call-completed"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    original_secret = first.json()["call_completed_webhook_secret"]
    assert original_secret is not None

    second = await client.patch(
        "/platform",
        json={"call_completed_webhook_url": "https://example.com/voiceai/call-completed-v2"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["call_completed_webhook_url"] == "https://example.com/voiceai/call-completed-v2"
    # Never shown again once already generated.
    assert body["call_completed_webhook_secret"] is None

    persisted = await platform_repo.get_by_id(db, platform_id)
    assert persisted is not None
    assert persisted.call_completed_webhook_secret == original_secret


async def test_call_completed_webhook_secret_never_on_platform_public_get_back(
    client: AsyncClient, db: MongoDB
) -> None:
    """Setting inbound_variables_webhook_url alone (not touching
    call_completed_webhook_url at all) must never surface a secret in the
    response — only the exact call that FIRST generates one does.
    """
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"inbound_variables_webhook_url": "https://example.com/voiceai/variables"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["call_completed_webhook_secret"] is None


async def test_clearing_call_completed_webhook_url_does_not_touch_secret(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, platform_id = await _seed_platform(db, "Platform A")
    first = await client.patch(
        "/platform",
        json={"call_completed_webhook_url": "https://example.com/voiceai/call-completed"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    original_secret = first.json()["call_completed_webhook_secret"]

    resp = await client.patch(
        "/platform",
        json={"call_completed_webhook_url": None},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["call_completed_webhook_url"] is None
    assert resp.json()["call_completed_webhook_secret"] is None

    persisted = await platform_repo.get_by_id(db, platform_id)
    assert persisted is not None
    assert persisted.call_completed_webhook_url is None
    # The secret itself survives clearing the URL — re-registering later
    # reuses it rather than silently generating a second one.
    assert persisted.call_completed_webhook_secret == original_secret


async def test_call_completed_webhook_url_loopback_is_rejected(
    client: AsyncClient, db: MongoDB
) -> None:
    api_key, _ = await _seed_platform(db, "Platform A")
    resp = await client.patch(
        "/platform",
        json={"call_completed_webhook_url": "http://127.0.0.1:9000/call-completed"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "call_completed_webhook_url"
