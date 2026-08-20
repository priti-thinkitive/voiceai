"""Throwaway endpoint proving the error contract works end to end.

Bootstrap step 3 verification only — not a real feature. Hits every handler
registered in errors.py: AppError, legacy HTTPException, RequestValidationError,
and the unhandled-exception catch-all. Remove once verified (or leave mounted
under /_debug — harmless, no auth-bearing surface — while other bootstrap
steps still reference it for manual re-checks).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.deps import CurrentPlatform, get_platform_filter
from app.errors import AppError

router = APIRouter(prefix="/_debug", tags=["_debug"])


class WhoAmIResponse(BaseModel):
    platform_id: str
    platform_name: str
    resolved_filter: dict[str, str]


@router.get(
    "/whoami",
    response_model=WhoAmIResponse,
    summary="Verify get_current_platform + get_platform_filter (bootstrap step 7 check)",
)
async def whoami(caller: CurrentPlatform) -> WhoAmIResponse:
    return WhoAmIResponse(
        platform_id=caller.id,
        platform_name=caller.name,
        resolved_filter=get_platform_filter(caller),
    )


class EchoBody(BaseModel):
    name: str


@router.get("/error/app-error", summary="Raise AppError (verifies AppError handler)")
async def raise_app_error() -> None:
    raise AppError(
        code="platform_not_found",
        message="No platform matches that identifier.",
        status_code=404,
        field="platform_id",
    )


@router.get(
    "/error/http-exception",
    summary="Raise legacy HTTPException (verifies normalisation handler)",
)
async def raise_http_exception() -> None:
    raise HTTPException(status_code=403, detail="You cannot do that.")


@router.post(
    "/error/validation",
    summary="Trigger a 422 (verifies RequestValidationError handler)",
)
async def raise_validation(body: EchoBody) -> EchoBody:
    return body


@router.get(
    "/error/unhandled",
    summary="Raise a bare exception (verifies the catch-all handler)",
)
async def raise_unhandled() -> Literal["unreachable"]:
    raise RuntimeError("boom - simulated unhandled exception")
