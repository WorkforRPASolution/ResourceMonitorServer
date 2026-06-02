"""FastAPI application entry point.

The lifespan is intentionally a thin orchestrator: each phase delegates to a
helper in ``src.startup``. This makes the boot sequence diff-friendly and
testable in isolation.

Boot sequence (each phase logged via ``startup_phase``):

    1. setup_logging_minimal      — bootstrap logger before settings
    2. get_settings + setup_logging — full structlog
    3. init_infra                  — connect ES/Mongo/Redis/Email/ZK
    4. _verify_infra_versions      — warn if running against unexpected versions
    5. init_repos                  — wrap Mongo collections in repositories
    6. seed_default_profile        — hash-compare; idempotent
    7. init_distributed            — leader/lock/partition_mgr/cooldown
    8. init_scheduler              — APScheduler wrapper
    9. partition_mgr.start         — register members + watches
   10. leader_election.start       — fire-and-forget election thread
   11. scheduler.start             — finally accept jobs
"""
from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from src.api import admin, health
from src.api.metrics import STARTUP_COMPLETE, render_metrics
from src.cache.cooldown import AlertCooldownManager
from src.config.constants import (
    ALERT_CODE_RESOURCE_MONITOR,
    ALERT_SUBCODE_SELF,
    SELF_ALERT_LINE,
    SELF_ALERT_MODEL,
    SELF_ALERT_PROCESS,
)
from src.config.settings import get_settings
from src.db.seed import seed_default_profile
from src.distributed.lock import NoOpZKLock
from src.logging_config import setup_logging, setup_logging_minimal
from src.middleware import RequestIdMiddleware
from src.startup.distributed import init_distributed
from src.startup.infra import InfraContext, init_infra, startup_phase
from src.startup.repos import init_repos
from src.startup.scheduler_init import SchedulerDeps, init_scheduler

logger = structlog.get_logger(__name__)


# ----------------------------------------------------------------------
# Lifespan
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging_minimal()
    settings = get_settings()
    setup_logging(settings)
    instance_id = settings.instance_id or socket.gethostname()
    structlog.contextvars.bind_contextvars(instance_id=instance_id)

    infra: InfraContext | None = None
    try:
        async with startup_phase("init_infra"):
            infra = await init_infra(settings)

        async with startup_phase("verify_versions"):
            await _verify_infra_versions(infra)

        async with startup_phase("init_repos"):
            repos = await init_repos(infra, settings)

        if not settings.debug_read_only:
            async with startup_phase("seed_default_profile"):
                await seed_default_profile(repos.profile_repo)
        else:
            logger.warning(
                "debug_read_only_skip_phase",
                phase="seed_default_profile",
            )

        if not settings.debug_read_only:
            # Build the scheduler holder BEFORE init_distributed so the
            # partition manager can pull the (yet-to-be-created) scheduler
            # via a lazy provider closure.
            scheduler_holder: dict = {"scheduler": None}

            async with startup_phase("init_distributed"):
                distributed = await init_distributed(
                    infra=infra,
                    repos=repos,
                    instance_id=instance_id,
                    scheduler_provider=lambda: scheduler_holder["scheduler"],
                    settings=settings,
                )
        else:
            logger.warning(
                "debug_read_only_skip_phase",
                phase="init_distributed",
                reason="no ZK, no leader election, no partition manager",
            )
            distributed = None
            scheduler_holder = None

        async with startup_phase("init_scheduler"):
            from src.es.queries import QueryBuilder

            if settings.debug_read_only:
                # No distributed context — build a minimal SchedulerDeps with
                # a no-op lock and a cooldown manager that talks directly to
                # Redis. The cooldown manager itself reads settings to decide
                # whether to actually write (see D6).
                sched_deps = SchedulerDeps(
                    es=infra.es,
                    profile_repo=repos.profile_repo,
                    eqp_info_repo=repos.eqp_info_repo,
                    zk_lock=NoOpZKLock(),
                    cooldown_mgr=AlertCooldownManager(
                        infra.redis, settings=settings
                    ),
                    email_client=infra.email,
                    query_builder=QueryBuilder(settings),
                )
            else:
                sched_deps = SchedulerDeps(
                    es=infra.es,
                    profile_repo=repos.profile_repo,
                    eqp_info_repo=repos.eqp_info_repo,
                    zk_lock=distributed.zk_lock,
                    cooldown_mgr=distributed.cooldown_mgr,
                    email_client=infra.email,
                    query_builder=QueryBuilder(settings),
                )
            scheduler = await init_scheduler(settings, sched_deps)
            if scheduler_holder is not None:
                scheduler_holder["scheduler"] = scheduler

        # Stash everything on app.state for the FastAPI deps providers
        app.state.settings = settings
        app.state.infra = infra
        app.state.repos = repos
        app.state.scheduler = scheduler
        app.state.es_client = infra.es
        app.state.mongo_client = infra.mongo
        app.state.redis_client = infra.redis
        app.state.zk_client = infra.zk
        app.state.email_client = infra.email
        if distributed is not None:
            app.state.distributed = distributed
            app.state.partition_manager = distributed.partition_mgr
            app.state.leader_election = distributed.leader_election
            app.state.cooldown_manager = distributed.cooldown_mgr
        else:
            app.state.cooldown_manager = sched_deps.cooldown_mgr

        if not settings.debug_read_only:
            async with startup_phase("partition_manager_start"):
                await distributed.partition_mgr.start()

            async with startup_phase("leader_election_start"):
                await distributed.leader_election.start()
        else:
            logger.warning(
                "debug_read_only_skip_phase",
                phase="partition_manager_start + leader_election_start",
            )

        async with startup_phase("scheduler_start"):
            await scheduler.start()

        if settings.debug_read_only:
            logger.warning(
                "startup_complete_in_debug_read_only_mode",
                skipped_phases=[
                    "seed_default_profile",
                    "init_distributed",
                    "partition_manager_start",
                    "leader_election_start",
                ],
                reason="DEBUG_READ_ONLY=true — MUST NOT run on prod K8s",
            )
        else:
            logger.info("startup_complete")
        # v6 P0-5: signal "init done" to Prometheus so operators can plot
        # wall-clock startup time. Set BEFORE yield so the very first
        # /metrics scrape after readiness sees 1.
        STARTUP_COMPLETE.set(1.0)
        yield

    except Exception as e:
        logger.error("startup_failed", error=str(e), exc_info=True)
        await _self_alert_critical(infra, settings, f"startup_failed: {e}")
        raise

    finally:
        logger.info("shutting_down")
        # v6 P0-5: clear the "ready" gauge first so any final scrape during
        # graceful shutdown shows 0.
        STARTUP_COMPLETE.set(0.0)
        # Strict reverse order. Each step swallows its own errors so the
        # next one always runs.
        if hasattr(app.state, "scheduler"):
            try:
                await app.state.scheduler.shutdown(timeout=30)
            except Exception as e:
                logger.warning("scheduler_shutdown_failed", error=str(e))
        if hasattr(app.state, "partition_manager"):
            try:
                await app.state.partition_manager.stop()
            except Exception as e:
                logger.warning("partition_mgr_stop_failed", error=str(e))
        if hasattr(app.state, "leader_election"):
            try:
                await app.state.leader_election.stop()
            except Exception as e:
                logger.warning("leader_election_stop_failed", error=str(e))
        if infra is not None:
            await infra.close_partial()
        # Clear every attribute we set so a subsequent lifespan on the same
        # FastAPI app object (common in tests) starts from a clean slate.
        # Starlette does NOT do this for us.
        for name in (
            "settings",
            "infra",
            "repos",
            "scheduler",
            "es_client",
            "mongo_client",
            "redis_client",
            "zk_client",
            "email_client",
            "distributed",
            "partition_manager",
            "leader_election",
            "cooldown_manager",
        ):
            if hasattr(app.state, name):
                delattr(app.state, name)
        logger.info("shutdown_complete")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
async def _verify_infra_versions(infra: InfraContext) -> None:
    """Log a warning if the connected infra is not the version we built against.

    This is intentionally non-fatal — operators should know if a newer Redis
    or ZK ends up in front of us, but we should not refuse to start.
    """
    if infra.redis is not None:
        try:
            info = await infra.redis.client.info("server")
            version = info.get("redis_version", "")
            if not version.startswith("5."):
                logger.warning(
                    "redis_version_mismatch",
                    expected="5.0.x",
                    actual=version,
                )
        except Exception:
            pass
    if infra.zk is not None:
        try:
            zk_version = await infra.zk.get_server_version()
            if zk_version != "unknown" and "3.5." not in zk_version:
                logger.warning(
                    "zk_version_mismatch",
                    expected="3.5.x",
                    actual=zk_version,
                )
        except Exception:
            pass


async def _self_alert_critical(
    infra: InfraContext | None, settings, message: str
) -> None:
    """Send an alert about the service itself dying.

    Best effort — if the email client is unavailable (which it is during
    startup failures by definition), we just log and move on. The K8s
    probe + alerting on Prometheus ``up{}`` is the real fallback.
    """
    if infra is None or infra.email is None:
        return
    try:
        from src.alert.models import EmailAlertRequest

        await infra.email.send_alert(
            EmailAlertRequest(
                hostname=settings.instance_id or "unknown",
                ip="self",
                app=settings.email_app_name,
                process=SELF_ALERT_PROCESS,
                eqp_model=SELF_ALERT_MODEL,
                line=SELF_ALERT_LINE,
                code=ALERT_CODE_RESOURCE_MONITOR,
                subcode=ALERT_SUBCODE_SELF,
                variables={"MESSAGE": message},
            )
        )
    except Exception as e:
        logger.error("self_alert_failed", error=str(e))


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------
app = FastAPI(
    title="ResourceMonitorServer",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestIdMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_exception",
        path=str(request.url.path),
        error=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        status_code=500, content={"error": "internal_server_error"}
    )


@app.get("/metrics")
async def metrics() -> Response:
    body, ctype = render_metrics()
    return Response(content=body, media_type=ctype)


app.include_router(health.router)
app.include_router(admin.router)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
    # Quiet pyflakes about asyncio import (used elsewhere conditionally)
    _ = asyncio
