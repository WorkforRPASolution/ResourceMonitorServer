"""FastAPI dependency providers.

Each provider reaches into ``request.app.state``, where ``main.lifespan`` has
stashed the connected client/repo instances. This indirection lets us swap
implementations in tests via ``app.dependency_overrides``.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request


def _state(request: Request, name: str) -> Any:
    obj = getattr(request.app.state, name, None)
    if obj is None:
        raise RuntimeError(f"app.state.{name} is not initialized")
    return obj


def _state_optional(request: Request, name: str) -> Any | None:
    """Like ``_state`` but returns None if the attribute is missing or None.

    Used by endpoints that must also work in Debug Read-Only mode, where
    distributed-coordination attributes (zk_client, partition_manager,
    leader_election) are intentionally not set.
    """
    return getattr(request.app.state, name, None)


def get_settings(request: Request) -> Any:
    return _state(request, "settings")


def get_es_client(request: Request) -> Any:
    return _state(request, "es_client")


def get_mongo_client(request: Request) -> Any:
    return _state(request, "mongo_client")


def get_redis_client(request: Request) -> Any:
    return _state(request, "redis_client")


def get_zk_client(request: Request) -> Any | None:
    """Optional: None in Debug Read-Only mode."""
    return _state_optional(request, "zk_client")


def get_email_client(request: Request) -> Any:
    return _state(request, "email_client")


def get_email_client_optional(request: Request) -> Any | None:
    """Optional variant — returns None if email_client is not on app.state.

    Used by /admin/email-outbox so the operator can still hit the endpoint
    during partial-startup or after a teardown without seeing a 500.
    """
    return _state_optional(request, "email_client")


def get_partition_manager(request: Request) -> Any | None:
    """Optional: None in Debug Read-Only mode."""
    return _state_optional(request, "partition_manager")


def get_leader_election(request: Request) -> Any | None:
    """Optional: None in Debug Read-Only mode."""
    return _state_optional(request, "leader_election")


def get_cooldown_manager(request: Request) -> Any:
    return _state(request, "cooldown_manager")


def get_scheduler(request: Request) -> Any:
    return _state(request, "scheduler")


def get_scheduler_optional(request: Request) -> Any | None:
    """Optional variant — returns None if the scheduler is not on app.state.

    Used by the profile write API to fire a best-effort cadence reconcile
    after a write: a missing scheduler (partial startup, some test harnesses)
    must never fail the write — the periodic reconcile loop is the safety net.
    """
    return _state_optional(request, "scheduler")
