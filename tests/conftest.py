from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import database
from app.config import get_settings
from app.database import MongoDB
from app.main import create_app


@pytest_asyncio.fixture
async def db() -> AsyncIterator[MongoDB]:
    """Real Mongo connection against a dedicated test database, so tests
    exercise actual Motor queries/indexes rather than a mock. Dropped after
    every test for isolation.
    """
    settings = get_settings()
    settings = settings.model_copy(update={"MONGODB_DB_NAME": "VoiceAI_test"})
    database._state.client = None
    database._state.db = None
    conn = await database.connect_to_mongo(settings)
    yield conn
    await conn.client.drop_database(settings.MONGODB_DB_NAME)
    await database.disconnect_from_mongo()


@pytest_asyncio.fixture
async def client(db: MongoDB) -> AsyncIterator[AsyncClient]:
    """`db` fixture already connected Mongo; override the app's own
    lifespan-driven connect/disconnect so it doesn't clobber that connection
    or drop the test database out from under a still-running test.
    """
    app = create_app()
    # raise_app_exceptions=False matches real deployed behavior: a real ASGI
    # server never re-raises a Python exception to the client once our
    # catch-all handler (errors.py) has converted it into a 500 response.
    # The default (True) would make this test client fail on an exception
    # that a live server correctly turns into a handled response.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
