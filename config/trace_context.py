"""Request trace context helpers."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4


_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    return uuid4().hex


def set_trace_id(trace_id: str):
    return _trace_id.set(trace_id)


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


def get_trace_id(default: str = "") -> str:
    return _trace_id.get() or default
