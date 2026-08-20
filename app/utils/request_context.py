"""Per-request context propagated via contextvars.

A `request_id` is set by middleware at the start of every request and read
by the error handlers so every error response — and every log line for that
request — can be correlated back to one HTTP call, without threading the
value through every function signature.
"""

from __future__ import annotations

from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()
