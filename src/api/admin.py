"""Admin endpoints — operational visibility and manual overrides.

These are intentionally NOT auth-protected at the application layer; they
are meant to be reachable only via in-cluster networking (a NetworkPolicy
should restrict ingress to the operator namespace). Adding application-layer
auth is tracked as a Phase 1+ item.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.api import deps
from src.db.models import MongoUnavailableError

router = APIRouter(prefix="/admin")


@router.get("/status")
async def admin_status(
    settings: Any = Depends(deps.get_settings),
    pm: Any = Depends(deps.get_partition_manager),
    scheduler: Any = Depends(deps.get_scheduler),
    zk: Any = Depends(deps.get_zk_client),
    leader: Any = Depends(deps.get_leader_election),
) -> dict[str, Any]:
    """Snapshot of the instance's distributed-coordination state.

    Useful when debugging "why is this pod not analyzing process X" — the
    response shows leadership, current assignment, ZK connectivity, and
    upcoming scheduled jobs.

    In Debug Read-Only mode: ``is_leader``/``leader_epoch``/
    ``assigned_processes``/``zk_*`` are ``None`` because the distributed
    layer was never initialized.
    """
    debug_mode = bool(getattr(settings, "debug_read_only", False))

    jobs = []
    try:
        for j in scheduler._scheduler.get_jobs():
            jobs.append(
                {
                    "id": j.id,
                    "next_run": j.next_run_time.isoformat()
                    if j.next_run_time
                    else None,
                }
            )
    except Exception:
        pass

    response: dict[str, Any] = {
        "instance_id": settings.instance_id,
        "debug_read_only": debug_mode,
        "scheduled_jobs": jobs,
        "scheduler_running": scheduler.is_running(),
    }

    if pm is not None:
        response["is_leader"] = pm.is_leader()
        response["leader_epoch"] = (
            leader.epoch if (leader is not None and pm.is_leader()) else None
        )
        response["assigned_processes"] = pm.get_my_processes()
    else:
        response["is_leader"] = None
        response["leader_epoch"] = None
        response["assigned_processes"] = None

    if zk is not None:
        response["zk_connected"] = zk.is_connected()
        response["zk_server_version"] = await zk.get_server_version()
    else:
        response["zk_connected"] = None
        response["zk_server_version"] = None

    return response


@router.delete("/cooldowns")
async def clear_cooldown(
    process: str,
    eqp_id: str,
    notify: str,
    severity: str,
    proc: str = "@system",
    cooldown_mgr: Any = Depends(deps.get_cooldown_manager),
) -> dict[str, str]:
    """Manually clear an alert cooldown (v2 5-dim identity).

    Query params carry the cooldown identity
    ``(process, eqp_id, proc, notify, severity)`` — they are query (not path)
    params because ``proc`` can be ``@system`` / ``*``, which are path-hostile.
    Useful when an operator has fixed an issue and wants the next breach to page
    immediately, rather than waiting for the cooldown window to expire.
    """
    await cooldown_mgr.clear_cooldown(process, eqp_id, proc, notify, severity)
    return {"cleared": f"{process}:{eqp_id}:{proc}:{notify}:{severity}"}


@router.post("/scheduler/reload")
async def reload_scheduler(
    scheduler: Any = Depends(deps.get_scheduler),
) -> dict[str, bool]:
    """Force an immediate cadence reconcile for this pod's owned processes
    (e.g. after editing a profile's evaluation interval).

    This drives ``scheduler.reconcile()`` — which re-derives the owned
    processes' scheduling intervals from Mongo and applies only the delta —
    rather than the legacy ``reload()`` with no args, which was a no-op in
    normal (non-debug) mode. ``reconciled`` is True iff a job was added or
    removed (False when the cadence was already up to date)."""
    try:
        changed = await scheduler.reconcile()
    except MongoUnavailableError as e:
        # Mirror the profile write API: a transient DB outage is 503 (retryable),
        # not a 500 that reads like a server bug.
        raise HTTPException(status_code=503, detail="database unavailable") from e
    return {"reconciled": bool(changed)}


@router.get("/email-outbox")
async def email_outbox(
    email_client: Any = Depends(deps.get_email_client_optional),
) -> dict[str, Any]:
    """v6 P1-3: snapshot of the in-memory failed-send outbox.

    Phase 0 substitute for a persistent outbox/DLQ — the deque is bounded
    at 1000 entries and is NOT durable across pod restarts. Lets operators
    quickly answer "did we drop any alerts during the last Akka outage?".

    Returns the most recent 50 entries (newest last) plus total count.
    """
    if email_client is None:
        return {
            "count": 0,
            "max_size": 0,
            "entries": [],
            "available": False,
        }
    snapshot = email_client.get_outbox_snapshot()
    return {
        "count": len(snapshot),
        "max_size": email_client.outbox_max_size,
        "entries": snapshot[-50:],
        "available": True,
    }
