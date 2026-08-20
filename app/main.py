"""VoiceAI API entrypoint.

Bootstrap step 1/3/4/5. Wires together config, the error contract, the DB
connection lifecycle, and the request_id middleware every log line and error
response depends on.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.database import connect_to_mongo, disconnect_from_mongo
from app.errors import install_exception_handlers
from app.routers import _error_check, admin_docs, agents, calls, health, languages, platform, voices, webhooks
from app.utils.request_context import set_request_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")


class RequestIdMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) that stamps a
    request_id on every request/response.

    BaseHTTPMiddleware (the `@app.middleware("http")` decorator) wraps
    exceptions in a way that can bypass Starlette's own exception-handling
    layer for unhandled exceptions raised deep in a route — the same
    exception then propagates out of the ASGI call instead of being turned
    into the catch-all 500 registered in errors.py. A plain ASGI middleware
    sits at the correct layer and doesn't have that failure mode.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id")
        request_id = incoming.decode("latin-1") if incoming else uuid.uuid4().hex
        set_request_id(request_id)

        async def send_with_request_id(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_request_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await connect_to_mongo(settings)
    yield
    await disconnect_from_mongo()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="VoiceAI API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    install_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(agents.router)
    app.include_router(platform.router)
    app.include_router(platform.admin_router)
    app.include_router(voices.router)
    app.include_router(languages.router)
    app.include_router(calls.router)
    app.include_router(webhooks.router)
    if settings.docs_enabled:
        # Debug-only error-contract verification endpoints — never mounted
        # outside development (no auth surface, but no reason to ship it).
        app.include_router(_error_check.router)
        # Second, separate admin-only Swagger UI (/admin/docs +
        # /admin/openapi.json) — same dev-only gate as the public /docs
        # above, PLUS its own HTTP Basic Auth requirement (ADMIN_API_KEY) on
        # top, checked per-request by AdminDocsCaller. See
        # app/routers/admin_docs.py's module docstring for the full design
        # (why a superset schema, why HTTP Basic over a query param, why not
        # a second FastAPI sub-app) and app/deps.py's get_admin_docs_caller
        # for the auth mechanism itself. The public /docs/openapi.json above
        # is completely unaffected by this — it is registered independently,
        # with no auth, exactly as before.
        admin_docs.register_admin_docs(app)

    return app


app = create_app()
