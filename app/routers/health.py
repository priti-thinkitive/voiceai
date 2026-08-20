"""Health check — bootstrap step 5.

No auth required. This is the first real endpoint: confirms config, the
error contract, and the DB connection are all wired together correctly, and
is what gets curled first every time the dev server starts.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.database import MongoDB, get_db, ping

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["connected", "unreachable"]


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=200,
    summary="Service health and DB connectivity",
)
async def get_health(db: Annotated[MongoDB, Depends(get_db)]) -> HealthResponse:
    db_ok = await ping(db)
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "unreachable",
    )
