"""Shared FastAPI dependency: the request-scoped AppContext."""
from __future__ import annotations

from fastapi import Request

from finmeta.api.state import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.context


__all__ = ["get_ctx"]
