"""Health probes — split into liveness and readiness.

``/healthz/live`` — process is alive. NEVER touches infrastructure. K8s uses
this for the liveness probe; if it returns non-200, the pod is restarted.

``/healthz/ready`` — all infrastructure is reachable. K8s uses this for the
readiness probe; if it returns non-200, the pod is removed from the service
load balancer but NOT restarted. This split prevents a transient ES outage
from triggering a restart loop.

Each infra check has a 2-second timeout and is shielded with a try/except.
``CancelledError`` is re-raised because we want to honor the loop's shutdown
signal — the caller is responsible for the timeout itself.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.api import deps
from src.api.metrics import INFRA_UP

router = APIRouter()

_CHECK_TIMEOUT_SEC = 2.0
_VERSION = "0.1.0"


async def _safe_check(coro: Awaitable[Any], timeout: float = _CHECK_TIMEOUT_SEC) -> bool:
    """Run an infra check with a timeout. Cancellation propagates upward."""
    try:
        return bool(await asyncio.wait_for(coro, timeout=timeout))
    except asyncio.CancelledError:
        # Honor cooperative cancellation — do NOT swallow this
        raise
    except (TimeoutError, asyncio.TimeoutError):
        return False
    except Exception:
        return False


@router.get("/healthz/live")
async def liveness() -> dict[str, str]:
    """Process is up. No infra access."""
    return {"status": "alive"}


@router.get("/healthz/ready")
async def readiness(
    settings=Depends(deps.get_settings),
    es=Depends(deps.get_es_client),
    mongo=Depends(deps.get_mongo_client),
    redis=Depends(deps.get_redis_client),
    zk=Depends(deps.get_zk_client),
    email=Depends(deps.get_email_client),
    pm=Depends(deps.get_partition_manager),
    scheduler=Depends(deps.get_scheduler),
) -> JSONResponse:
    debug_mode = bool(getattr(settings, "debug_read_only", False))
    checks = {
        "elasticsearch": await _safe_check(es.ping()),
        "mongodb": await _safe_check(mongo.ping()),
        "redis": await _safe_check(redis.ping()),
        "email_api": await _safe_check(email.health_check()),
    }
    # ZK has a synchronous is_connected() — no coroutine to await.
    # In debug mode ZK is intentionally not connected; mark it explicitly
    # so the /healthz/ready response is interpretable (not just "broken").
    if debug_mode:
        checks["zookeeper"] = "skipped_debug"
    else:
        checks["zookeeper"] = bool(zk.is_connected()) if zk is not None else False

    # In debug mode ZK check is skipped, so all() must ignore it.
    all_ok = all(
        v is True
        for k, v in checks.items()
        if not (debug_mode and k == "zookeeper")
    )

    # v6 P0-5: update the per-infra Gauge so Prometheus alerts can fire on
    # `infra_up == 0`. ZK in debug mode is intentionally not updated (the
    # gauge keeps whatever its prior value was — usually 0 from never being
    # set, which is the right "this debug pod is not in the cluster" signal).
    for k, v in checks.items():
        if debug_mode and k == "zookeeper":
            continue
        INFRA_UP.labels(infra=k).set(1.0 if v is True else 0.0)

    # v6 P0-4: a leader that has exhausted its redistribute retries is
    # technically connected to every infra but cannot do its job. Surface
    # this as readiness 503 so K8s pulls traffic and operators get paged.
    redistribute_unhealthy = (
        pm is not None and getattr(pm, "redistribute_unhealthy", False)
    )
    if redistribute_unhealthy:
        all_ok = False

    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ready" if all_ok else "not_ready",
            "debug_read_only": debug_mode,
            "checks": checks,
            "scheduler_running": scheduler.is_running(),
            "is_leader": pm.is_leader() if pm is not None else None,
            "redistribute_unhealthy": redistribute_unhealthy,
            "version": _VERSION,
        },
    )
