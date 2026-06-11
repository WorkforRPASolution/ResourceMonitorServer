"""Analysis job scheduler.

Phase 0 scope: lifecycle plumbing only. The actual analysis job is a no-op
stub — Phase 1 will plug in the real ES query + threshold evaluation.

Critical v4 design points:
- Every scheduled job runs through ``_job_wrapper`` which catches *all*
  exceptions and bumps a Prometheus failure counter. A job that throws must
  never crash the scheduler thread.
- ``shutdown()`` waits up to ``timeout`` seconds for in-flight jobs to drain,
  then FORCE-cancels anything still running. APScheduler's ``shutdown(wait=True)``
  alone does not propagate cancellation to user-level asyncio.Tasks, so we
  track them ourselves in ``_running_jobs``.
- ``pause_all_jobs()`` is reentrancy-safe — the wrapper checks ``_paused``
  on entry and returns immediately if set, so a paused scheduler does no work.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.api.metrics import JOB_TOTAL
from src.config.constants import ES_QUERY_SEMAPHORE
from src.config.settings import AppSettings
from src.db.models import MongoUnavailableError
from src.distributed.lock import LockAcquisitionTimeout

logger = structlog.get_logger(__name__)

# job id ⇄ (process, interval). process may itself contain '-', so the
# interval is anchored as the trailing -<digits>m group and process is the
# (greedy) remainder. Keep in lockstep with _add_interval_job's id format.
_JOB_ID_RE = re.compile(r"^analysis-(.+)-(\d+)m$")


def _classify_failure_reason(exc: BaseException) -> str:
    """v6 P1-2: bucket the failure into a stable Prometheus label.

    Order matters: check the most specific class first. The default
    ``"other"`` covers logic errors, validation, anything we have not
    bucketed yet.
    """
    if isinstance(exc, MongoUnavailableError):
        return "mongo_unavailable"
    if isinstance(exc, LockAcquisitionTimeout):
        return "lock_timeout"
    # Elasticsearch driver exceptions all live under elasticsearch.exceptions.
    # We match by module to avoid importing the driver here just for isinstance.
    module = type(exc).__module__ or ""
    if module.startswith("elasticsearch"):
        return "es_unavailable"
    return "other"


class AnalysisScheduler:
    """Wraps APScheduler with our own pause/resume/cancel guarantees."""

    def __init__(self, settings: AppSettings, deps: Any) -> None:
        self._settings = settings
        self._deps = deps  # bag of dependencies (es, repos, lock, ...)
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "misfire_grace_time": settings.scheduler_misfire_grace_time,
                "coalesce": True,
                "max_instances": 1,
            }
        )
        self._es_semaphore = asyncio.Semaphore(ES_QUERY_SEMAPHORE)
        self._running_jobs: set[asyncio.Task] = set()
        self._paused = False
        # APScheduler's `running` property does not flip to False reliably on
        # `shutdown(wait=False)` across versions. We track our own state.
        self._running = False
        # Phase 1: analysis engine — created lazily to allow deps to be
        # fully wired before first use.
        self._engine: Any = None
        # The processes this instance currently owns (set by reload()). None
        # means "not assigned yet" — reconcile() no-ops in normal mode until a
        # partition assignment arrives. Drives the periodic cadence reconcile.
        self._owned_processes: list[str] | None = None
        self._reconcile_task: asyncio.Task | None = None
        # Serializes the two job-set mutators — reload() (partition reassignment)
        # and reconcile() (cadence change) — so their snapshot→apply windows can
        # never interleave at an await boundary on the single event loop (which
        # would otherwise let reload's remove_all_jobs run mid-reconcile and
        # resurrect jobs for a no-longer-owned process).
        self._reconcile_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._running

    def is_paused(self) -> bool:
        return self._paused

    async def start(self) -> None:
        self._scheduler.start()
        self._running = True
        # Debug Read-Only keeps the "manual observer" contract — no auto-started
        # analysis. The operator triggers a reconcile via POST /admin/scheduler/
        # reload (or a profile write) when they want jobs to appear.
        if (
            not self._settings.debug_read_only
            and self._settings.scheduler_reconcile_interval_sec > 0
        ):
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info("scheduler_started")

    def _get_engine(self):
        if self._engine is None:
            from src.analyzer.engine import AnalysisEngine

            self._engine = AnalysisEngine(self._deps, self._settings)
            self._engine._es_semaphore = self._es_semaphore
        return self._engine

    async def reload(self, processes: list[str] | None = None) -> None:
        """Re-register analysis jobs after partition reassignment.

        ``processes`` is the list of process names this instance is
        responsible for. When ``None`` and debug mode is active, falls
        back to ``resolve_processes_for_debug()``.

        Serialized with ``reconcile()`` via ``_reconcile_lock`` so the
        remove-all-then-rebuild can't interleave with a concurrent reconcile.
        """
        async with self._reconcile_lock:
            self._scheduler.remove_all_jobs()

            if processes is None:
                if self._settings.debug_read_only:
                    processes = await self.resolve_processes_for_debug()
                else:
                    logger.warning("reload_called_without_processes_in_normal_mode")
                    return

            # Remember ownership so the periodic/triggered reconcile knows which
            # processes' cadence to keep in sync between reassignments.
            self._owned_processes = list(processes)
            engine = self._get_engine()

            for process in processes:
                # Job cadence = the union of rule intervals across EVERY scope doc
                # that can affect this process (process-specific + model/eqp
                # overlays + globals). We must NOT derive it from
                # resolve_profile(process,*,*), which sees only the process-level/
                # global ancestors — a profile scoped to a specific model/
                # equipment would then never be scheduled. The engine re-resolves
                # the effective profile per equipment at run time, so thresholds/
                # overrides still apply; here we only need cadence. Only enabled
                # rules contribute (a disabled rule needs no job — the engine
                # skips it anyway). See get_scheduling_intervals.
                intervals = await self._deps.profile_repo.get_scheduling_intervals(
                    process
                )
                if not intervals:
                    logger.warning(
                        "reload_no_intervals_for_process", process=process
                    )
                    continue
                for interval in intervals:
                    self._add_interval_job(engine, process, interval)

            logger.info(
                "scheduler_reloaded",
                processes=processes,
                job_count=len(self._scheduler.get_jobs()),
            )

    # ------------------------------------------------------------------
    # Cadence reconcile — apply only the (process, interval) delta so a
    # profile edit's new evaluation cadence takes effect without a pod
    # restart or a partition reassignment, and WITHOUT the remove_all_jobs
    # rebuild that reload() does (which would reset every job's next_run).
    # ------------------------------------------------------------------
    def _add_interval_job(self, engine: Any, process: str, interval: int) -> None:
        self._scheduler.add_job(
            self._job_wrapper,
            "interval",
            minutes=interval,
            args=[engine.run_analysis, process, interval],
            id=f"analysis-{process}-{interval}m",
            replace_existing=True,
        )

    def _current_interval_jobs(self) -> dict[tuple[str, int], str]:
        """Map of (process, interval) → job_id for the analysis jobs currently
        registered. Non-analysis jobs (if any) are ignored."""
        out: dict[tuple[str, int], str] = {}
        for job in self._scheduler.get_jobs():
            m = _JOB_ID_RE.match(job.id)
            if m:
                out[(m.group(1), int(m.group(2)))] = job.id
        return out

    async def _processes_to_schedule(self) -> list[str] | None:
        """The processes whose cadence reconcile should maintain. Normal mode:
        whatever the last assignment gave us (None until first assignment).
        Debug mode: resolved from settings/EQP_INFO so a debug instance still
        self-schedules."""
        if self._owned_processes is not None:
            return list(self._owned_processes)  # snapshot — never alias the field
        if self._settings.debug_read_only:
            return await self.resolve_processes_for_debug()
        return None

    async def reconcile(self) -> bool:
        """Re-derive the owned processes' scheduling intervals from Mongo and
        apply only the delta. Returns True iff a job was added or removed.

        Cheap and idempotent: when the cadence is unchanged it does one Mongo
        query per owned process and touches no jobs — so callers (write API,
        admin, periodic loop) may invoke it freely; the set-diff IS the
        cadence-change detection. Content-only edits (thresholds, rule
        enabled/disabled, notify) never change the interval set and the engine
        re-reads them per equipment each tick, so this correctly no-ops on them.

        Respects the quiescence contract (no-op when paused or not running) and
        is serialized with reload() via ``_reconcile_lock``.
        """
        async with self._reconcile_lock:
            if self._paused or not self._running:
                return False
            processes = await self._processes_to_schedule()
            if not processes:
                return False

            desired: set[tuple[str, int]] = set()
            for process in processes:
                intervals = await self._deps.profile_repo.get_scheduling_intervals(
                    process
                )
                for interval in intervals:
                    desired.add((process, interval))

            current = self._current_interval_jobs()
            current_keys = set(current.keys())
            to_add = desired - current_keys
            to_remove = current_keys - desired
            if not to_add and not to_remove:
                return False

            engine = self._get_engine()
            for key in to_remove:
                self._scheduler.remove_job(current[key])
            for process, interval in sorted(to_add):
                self._add_interval_job(engine, process, interval)

            logger.info(
                "scheduler_reconciled",
                added=sorted(f"{p}-{i}m" for p, i in to_add),
                removed=sorted(f"{p}-{i}m" for p, i in to_remove),
                job_count=len(self._scheduler.get_jobs()),
            )
        return True

    async def _reconcile_loop(self) -> None:
        """Periodically reconcile owned-process cadence. Each tick is wrapped
        so a transient failure (e.g. Mongo blip) never kills the loop — the
        next tick self-heals."""
        interval = self._settings.scheduler_reconcile_interval_sec
        while True:
            try:
                await asyncio.sleep(interval)
                if self._paused or not self._running:
                    continue
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("reconcile_loop_error", error=str(e), exc_info=True)

    async def resolve_processes_for_debug(self) -> list[str]:
        """Return the list of processes this debug instance should analyze.

        Resolution order:
            1. ``settings.debug_processes`` if non-empty — operator specified
            2. ``eqp_info_repo.get_distinct_processes()`` — every active process

        Called by Phase 1's ``reload()`` when ``settings.debug_read_only`` is
        True. Raises if called in normal mode — debug and normal code paths
        must not silently cross over.
        """
        if not self._settings.debug_read_only:
            raise RuntimeError(
                "resolve_processes_for_debug called with debug_read_only=False"
            )
        if self._settings.debug_processes:
            logger.info(
                "debug_processes_explicit",
                processes=self._settings.debug_processes,
            )
            return list(self._settings.debug_processes)
        # Fall back to every active process in EQP_INFO
        processes = await self._deps.eqp_info_repo.get_distinct_processes()
        logger.info("debug_processes_from_eqp_info", count=len(processes))
        return processes

    async def pause_all_jobs(self) -> None:
        self._paused = True
        if self._scheduler.running:
            self._scheduler.pause()
        logger.info("scheduler_paused")

    async def resume_jobs_for(self, processes: list[str]) -> None:
        self._paused = False
        if self._scheduler.running:
            self._scheduler.resume()
        logger.info("scheduler_resumed", processes=processes)

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Stop the scheduler and force-cancel any in-flight jobs.

        APScheduler's ``shutdown(wait=False)`` returns immediately. We then
        gather the user-level Tasks we're tracking and cancel anything that
        does not complete within ``timeout``.
        """
        self._paused = True  # block new jobs from starting
        # Stop the periodic reconcile loop first so it can't re-add jobs while
        # we're tearing down.
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("reconcile_task_shutdown_error", error=str(e))
            self._reconcile_task = None
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._running = False

        if not self._running_jobs:
            logger.info("scheduler_shutdown_clean")
            return

        # Take a snapshot — discard mutates the set
        pending = set(self._running_jobs)
        try:
            done, still_pending = await asyncio.wait(
                pending, timeout=timeout
            )
        except Exception as e:
            logger.error("scheduler_shutdown_wait_failed", error=str(e))
            still_pending = pending

        if still_pending:
            logger.warning(
                "scheduler_force_cancel", count=len(still_pending)
            )
            for t in still_pending:
                t.cancel()
            # Drain the cancellations so we don't leave orphan tasks
            await asyncio.gather(*still_pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # Job wrapper — every scheduled coroutine flows through here
    # ------------------------------------------------------------------
    async def _job_wrapper(
        self,
        job_fn: Callable[..., Awaitable[None]],
        process: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Wrap a job with paused-check, exception capture, and metrics."""
        if self._paused:
            return
        task = asyncio.current_task()
        if task is not None:
            self._running_jobs.add(task)
        try:
            await job_fn(process, *args, **kwargs)
            # v6 P1-2: success uses an empty reason label so dashboards can
            # sum across all reason labels for a process without
            # double-counting.
            JOB_TOTAL.labels(
                process=process, status="success", reason=""
            ).inc()
        except asyncio.CancelledError:
            # Propagate cancellation — but DO log so we know shutdown cancelled us
            logger.info("scheduled_job_cancelled", process=process)
            raise
        except Exception as e:
            reason = _classify_failure_reason(e)
            JOB_TOTAL.labels(
                process=process, status="failure", reason=reason
            ).inc()
            logger.error(
                "scheduled_job_failed",
                process=process,
                reason=reason,
                error=str(e),
                exc_info=True,
            )
        finally:
            if task is not None:
                self._running_jobs.discard(task)
