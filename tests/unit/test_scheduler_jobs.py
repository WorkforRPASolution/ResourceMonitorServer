"""Tests for src.scheduler.jobs (AnalysisScheduler).

Phase 0 only needs the lifecycle plumbing — start/stop, pause/resume,
job wrapper exception handling, and the force-cancel-on-shutdown path.
The actual analysis job is a stub.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.settings import AppSettings
from src.scheduler.jobs import AnalysisScheduler


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(scheduler_misfire_grace_time=60)


@pytest.fixture
def deps():
    """Bag of stub dependencies the scheduler accepts."""
    return MagicMock()


@pytest.mark.unit
class TestSchedulerLifecycle:
    async def test_start_then_shutdown(self, settings, deps):
        sched = AnalysisScheduler(settings, deps)
        await sched.start()
        assert sched.is_running() is True
        await sched.shutdown(timeout=1.0)
        assert sched.is_running() is False

    async def test_pause_and_resume(self, settings, deps):
        sched = AnalysisScheduler(settings, deps)
        await sched.start()
        await sched.pause_all_jobs()
        assert sched.is_paused() is True
        await sched.resume_jobs_for(["CVD"])
        assert sched.is_paused() is False
        await sched.shutdown(timeout=1.0)


@pytest.mark.unit
class TestJobWrapperExceptionHandling:
    async def test_wrapper_logs_and_increments_failure_counter(
        self, settings, deps
    ):
        from src.api.metrics import JOB_TOTAL

        sched = AnalysisScheduler(settings, deps)

        async def failing_job(process):
            raise RuntimeError("simulated failure")

        before = JOB_TOTAL.labels(
            process="CVD", status="failure", reason="other"
        )._value.get()
        # Must NOT raise even though the inner job did
        await sched._job_wrapper(failing_job, "CVD")
        after = JOB_TOTAL.labels(
            process="CVD", status="failure", reason="other"
        )._value.get()
        assert after == before + 1

    async def test_wrapper_increments_success_counter(self, settings, deps):
        from src.api.metrics import JOB_TOTAL

        sched = AnalysisScheduler(settings, deps)

        async def good_job(process):
            return None

        before = JOB_TOTAL.labels(
            process="CVD", status="success", reason=""
        )._value.get()
        await sched._job_wrapper(good_job, "CVD")
        after = JOB_TOTAL.labels(
            process="CVD", status="success", reason=""
        )._value.get()
        assert after == before + 1

    @pytest.mark.parametrize(
        "exc_factory,expected_reason",
        [
            (
                lambda: __import__(
                    "src.db.models", fromlist=["MongoUnavailableError"]
                ).MongoUnavailableError("mongo down"),
                "mongo_unavailable",
            ),
            (
                lambda: __import__(
                    "src.distributed.lock", fromlist=["LockAcquisitionTimeout"]
                ).LockAcquisitionTimeout("CVD"),
                "lock_timeout",
            ),
            (
                lambda: __import__(
                    "elasticsearch.exceptions", fromlist=["NotFoundError"]
                ).NotFoundError(404, "missing", "no such index"),
                "es_unavailable",
            ),
            (lambda: ValueError("bad data"), "other"),
        ],
    )
    async def test_wrapper_failure_reason_label(
        self, settings, deps, exc_factory, expected_reason
    ):
        """v6 P1-2: failures must be bucketed by exception class so
        Prometheus dashboards can alert per infra."""
        from src.api.metrics import JOB_TOTAL

        sched = AnalysisScheduler(settings, deps)
        exc = exc_factory()

        async def failing_job(process):
            raise exc

        before = JOB_TOTAL.labels(
            process="CVD", status="failure", reason=expected_reason
        )._value.get()
        await sched._job_wrapper(failing_job, "CVD")
        after = JOB_TOTAL.labels(
            process="CVD", status="failure", reason=expected_reason
        )._value.get()
        assert after == before + 1

    async def test_wrapper_skips_when_paused(self, settings, deps):
        sched = AnalysisScheduler(settings, deps)
        await sched.pause_all_jobs()

        called = {"n": 0}

        async def job(process):
            called["n"] += 1

        await sched._job_wrapper(job, "CVD")
        assert called["n"] == 0


@pytest.mark.unit
class TestShutdownForceCancel:
    async def test_shutdown_cancels_pending_tasks_after_timeout(
        self, settings, deps
    ):
        """v3 P0: pending Tasks must be force-cancelled if shutdown timeout hits."""
        sched = AnalysisScheduler(settings, deps)
        await sched.start()

        # Inject a long-running task into the scheduler's tracking set
        cancelled = asyncio.Event()

        async def long_task():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(long_task())
        sched._running_jobs.add(task)

        # Shutdown with a tight timeout — must cancel `task`
        await sched.shutdown(timeout=0.05)
        assert cancelled.is_set()


@pytest.mark.unit
class TestDebugProcessesResolution:
    """Debug Read-Only mode: with no partition manager, the scheduler must
    still have a way to decide which processes to analyze. Resolution order:

    1. Explicit ``settings.debug_processes`` — operator specified
    2. Fall back to ``eqp_info_repo.get_distinct_processes()`` — everything active

    Phase 0 doesn't actually register jobs, but Phase 1's reload() will
    call this helper; the regression guard locks in the contract now.
    """

    async def test_explicit_debug_processes_wins(self, settings):
        """If settings.debug_processes is non-empty, use it verbatim."""
        debug_settings = AppSettings(
            debug_read_only=True,
            debug_processes=["ETCH", "CVD"],
        )
        eqp_repo = MagicMock(
            get_distinct_processes=AsyncMock(return_value=["SHOULD_NOT_APPEAR"])
        )
        deps = SimpleNamespace(eqp_info_repo=eqp_repo)
        sched = AnalysisScheduler(debug_settings, deps)

        result = await sched.resolve_processes_for_debug()

        assert result == ["ETCH", "CVD"]
        # eqp_repo.get_distinct_processes must NOT be called if debug_processes set
        eqp_repo.get_distinct_processes.assert_not_awaited()

    async def test_falls_back_to_get_distinct_when_empty(self, settings):
        """Empty debug_processes → query EQP_INFO for all active processes."""
        debug_settings = AppSettings(debug_read_only=True)
        assert debug_settings.debug_processes == []
        eqp_repo = MagicMock(
            get_distinct_processes=AsyncMock(return_value=["P1", "P2", "P3"])
        )
        deps = SimpleNamespace(eqp_info_repo=eqp_repo)
        sched = AnalysisScheduler(debug_settings, deps)

        result = await sched.resolve_processes_for_debug()

        assert result == ["P1", "P2", "P3"]
        eqp_repo.get_distinct_processes.assert_awaited_once()

    async def test_resolve_raises_if_not_debug_mode(self, settings):
        """Regression guard: this helper is for debug mode only. Calling it
        in production mode should fail loudly so we don't accidentally
        bypass the partition manager in prod."""
        normal_settings = AppSettings(debug_read_only=False)
        deps = SimpleNamespace(eqp_info_repo=MagicMock())
        sched = AnalysisScheduler(normal_settings, deps)

        with pytest.raises(RuntimeError, match="debug_read_only"):
            await sched.resolve_processes_for_debug()
