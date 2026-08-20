"""Application error contract.

Every non-2xx response leaves this API in the same shape so integrating
platforms can parse errors consistently:

    {"detail": {"code": "...", "field": null, "message": "...", "request_id": "..."}}

Two ways for a router/service to produce that envelope:

  1. Raise `AppError(code=..., message=..., status_code=..., field=...)`.
     Preferred — the code/message/field live right next to the business
     logic that decided to fail.

  2. Raise `HTTPException(detail={"code": ..., "field": ..., "message": ...})`.
     Normalised into the same shape by a handler, for FastAPI/Starlette
     internals that raise bare HTTPException on our behalf.

Anything else — `HTTPException(detail="string")`, `RequestValidationError`,
or an unhandled `Exception` — is also normalised. A caller never sees a raw
Python exception, a pydantic error array, or `str(exc)`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status as http_status

from app.utils.request_context import get_request_id

logger = logging.getLogger("app.error")

# Code taxonomy. Keep snake_case, grouped by suffix:
#   *_required     — a needed field/resource/header was missing
#   *_invalid      — present but malformed/semantically wrong
#   forbidden_*    — authenticated but not allowed to do this
#   *_not_found    — no such record (or cross-tenant access, deliberately
#                    disguised as not_found — see deps.py)
#   unauthenticated, rate_limited, upstream_failed, internal, storage_failed
#   — fixed codes
CODE_INTERNAL = "internal"
CODE_NOT_FOUND = "not_found"
CODE_FORBIDDEN = "forbidden"
CODE_UNAUTHENTICATED = "unauthenticated"
CODE_VALIDATION = "invalid_request"
CODE_RATE_LIMITED = "rate_limited"
CODE_UPSTREAM_FAILED = "upstream_failed"
# Deliberately separate from CODE_UPSTREAM_FAILED: that code means "the
# THIRD-PARTY VOICE VENDOR (Retell) rejected/was unreachable" — a failure of
# someone else's system we're calling on Platform X's behalf. A storage
# failure (S3 unreachable/misconfigured/missing credentials) is OUR OWN
# infrastructure failing, not the voice vendor's — conflating the two would
# make "upstream_failed" ambiguous between "the vendor is down" and "our own
# storage is down," which are different problems with different remediation
# (retry later vs. our own ops needs to fix config) and different meaning for
# Platform X's own error handling/alerting. See storage.py's module
# docstring for where this is raised.
CODE_STORAGE_FAILED = "storage_failed"


class AppError(Exception):
    """Application-level error with structured detail.

    Prefer this over `HTTPException` in routers/services — keeps the
    response shape and log payload consistent without each call site
    needing to remember the contract.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = http_status.HTTP_400_BAD_REQUEST,
        field: str | None = None,
        log_extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        self.log_extra = log_extra or {}


def _envelope(*, code: str, message: str, field: str | None) -> dict[str, Any]:
    return {
        "detail": {
            "code": code,
            "field": field,
            "message": message,
            "request_id": get_request_id(),
        }
    }


def _log_error(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    error_class: str,
    field: str | None = None,
    exc: BaseException | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Single place that emits the error log line. 4xx -> WARNING, 5xx -> ERROR."""
    level = logging.ERROR if status_code >= 500 else logging.WARNING
    payload: dict[str, Any] = {
        "route": f"{request.method} {request.url.path}",
        "status_code": status_code,
        "error_code": code,
        "error_class": error_class,
    }
    if field:
        payload["error_field"] = field
    if extra:
        payload.update(extra)
    # exc_info logs the stack only for 5xx — for 4xx the caller's mistake is
    # the cause, and the stack is noise.
    logger.log(
        level,
        message,
        exc_info=exc if status_code >= 500 else None,
        extra=payload,
    )


# ── Exception handlers ────────────────────────────────────────────────────


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    _log_error(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        error_class="AppError",
        field=exc.field,
        exc=exc,
        extra=exc.log_extra,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code=exc.code, message=exc.message, field=exc.field),
    )


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalise the legacy `HTTPException(detail=...)` shape.

    Accepts:
      detail = {"code": "...", "field": "...", "message": "..."}  (preferred)
      detail = "free text"                                         (legacy)
    """
    code, field, message = _normalise_detail(exc.detail, exc.status_code)
    _log_error(
        request=request,
        status_code=exc.status_code,
        code=code,
        message=message,
        error_class="HTTPException",
        field=field,
        exc=exc,
    )
    response = JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code=code, message=message, field=field),
    )
    if exc.headers:
        for k, v in exc.headers.items():
            response.headers[k] = v
    return response


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Turn FastAPI/Pydantic 422 arrays into a single structured field error.

    Picks the first error and routes it to that field name; the full error
    list is still logged so a developer can see everything that was wrong.
    """
    errors = exc.errors()
    first = errors[0] if errors else None
    field = None
    message = "Request payload failed validation."
    if first:
        loc = first.get("loc") or ()
        parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
        field = ".".join(parts) if parts else None
        message = _humanise_pydantic_msg(
            first.get("msg", ""), field, first.get("type"), first.get("ctx")
        )
    _log_error(
        request=request,
        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        code=CODE_VALIDATION,
        message=message,
        error_class="RequestValidationError",
        field=field,
        exc=exc,
        extra={"pydantic_errors_count": len(errors)},
    )
    return JSONResponse(
        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_envelope(code=CODE_VALIDATION, message=message, field=field),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler — anything we didn't anticipate becomes a 500.

    The caller sees a generic message + request_id. Logs carry the full
    traceback, joined to the request via request_id.
    """
    _log_error(
        request=request,
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=CODE_INTERNAL,
        message="unhandled exception",
        error_class=type(exc).__name__,
        exc=exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            code=CODE_INTERNAL,
            field=None,
            # Deliberately vague — never leak repr(exc), which can contain
            # internal details, credentials fragments, etc.
            message="An unexpected error occurred. Please share the reference ID with support.",
        ),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register the handlers. Call once during app startup."""
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        _validation_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, _unhandled_exception_handler)


# ── Helpers ───────────────────────────────────────────────────────────────


def _default_code_for_status(status_code: int) -> str:
    if status_code == 401:
        return CODE_UNAUTHENTICATED
    if status_code == 403:
        return CODE_FORBIDDEN
    if status_code == 404:
        return CODE_NOT_FOUND
    if status_code == 422:
        return CODE_VALIDATION
    if status_code == 429:
        return CODE_RATE_LIMITED
    if 500 <= status_code < 600:
        return CODE_INTERNAL
    return "error"


def _normalise_detail(detail: Any, status_code: int) -> tuple[str, str | None, str]:
    """Coerce HTTPException's polymorphic `detail` into (code, field, message)."""
    if isinstance(detail, dict):
        code = str(detail.get("code") or _default_code_for_status(status_code))
        raw_field = detail.get("field")
        field = str(raw_field) if isinstance(raw_field, str) and raw_field else None
        message = str(detail.get("message") or detail.get("msg") or "Request failed.")
        return code, field, message
    if isinstance(detail, str) and detail:
        return _default_code_for_status(status_code), None, detail
    if isinstance(detail, list):
        return _default_code_for_status(status_code), None, "Request failed validation."
    return _default_code_for_status(status_code), None, "Request failed."


_PYDANTIC_TYPE_HUMAN_MAP = {
    "missing": "is required.",
}


def _humanise_pydantic_msg(
    msg: str,
    field: str | None,
    error_type: str | None = None,
    ctx: dict[str, Any] | None = None,
) -> str:
    """Turn a Pydantic v2 validation error into a plain-language message."""
    if field and error_type == "string_too_short":
        nicefield = field.replace("_", " ").strip()
        min_length = (ctx or {}).get("min_length")
        if min_length == 1:
            return f"{nicefield.capitalize()} can't be left blank."
        if min_length:
            return f"{nicefield.capitalize()} must be at least {min_length} characters."
    if field and error_type in _PYDANTIC_TYPE_HUMAN_MAP:
        nicefield = field.replace("_", " ").strip()
        return f"{nicefield.capitalize()} {_PYDANTIC_TYPE_HUMAN_MAP[error_type]}"
    # Pydantic v2 prefixes custom-validator messages with boilerplate; strip
    # it so the caller sees only the sentence the validator wrote.
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            msg = msg[len(prefix) :]
            break
    return msg or "This request failed validation."
