"""ASGI middleware shared across the application.

``RequestIdMiddleware`` propagates an ``X-Request-ID`` header through the
log context so every structlog event emitted while handling the request
is automatically tagged. Skip the K8s probe paths to avoid cluttering
the logs (those endpoints fire every few seconds per pod).
"""
from __future__ import annotations

from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind ``request_id`` to structlog contextvars for the duration of the request."""

    SKIP_PATHS = frozenset(["/healthz/live", "/healthz/ready", "/metrics"])

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request, call_next):
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["X-Request-ID"] = request_id
        return response
