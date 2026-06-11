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


# ----------------------------------------------------------------------
# Phase 1: reload() job registration
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestSchedulerReload:
    def _make_deps_with_profile(self):
        from src.db.models import (
            Condition,
            Fact,
            Measure,
            MonitorProfile,
            NotifyChannel,
            Rule,
            Scope,
        )

        # two rules at two distinct intervals → one job per (process, interval)
        profile = MonitorProfile(
            scope=Scope(process="*"),
            measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                              window_minutes=15, facts=[Fact(type="max")])],
            rules=[
                Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=80)]),
                Rule(id="cpu_slow", interval_minutes=10, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=70)]),
            ],
            notify={"default": NotifyChannel(cooldown_minutes=30)},
        )
        # reload() derives job cadence from get_scheduling_intervals (the union
        # of intervals across all scope docs for the process), NOT from the
        # process-level resolve_profile. resolve_profile stays for the engine's
        # per-equipment resolution at run time.
        intervals = sorted({r.interval_minutes for r in profile.rules})
        deps = SimpleNamespace(
            es=MagicMock(),
            profile_repo=MagicMock(
                resolve_profile=AsyncMock(return_value=profile),
                get_scheduling_intervals=AsyncMock(return_value=intervals),
            ),
            eqp_info_repo=MagicMock(),
            zk_lock=MagicMock(),
            cooldown_mgr=MagicMock(),
            email_client=MagicMock(),
            query_builder=MagicMock(),
        )
        return deps

    async def test_reload_registers_jobs_per_process_interval(self):
        deps = self._make_deps_with_profile()
        sched = AnalysisScheduler(
            AppSettings(scheduler_misfire_grace_time=60), deps
        )
        await sched.start()
        await sched.reload(["CVD", "ETCH"])

        jobs = sched._scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        # 2 processes × 2 distinct intervals = 4 jobs
        assert len(jobs) == 4
        assert "analysis-CVD-5m" in job_ids
        assert "analysis-CVD-10m" in job_ids
        assert "analysis-ETCH-5m" in job_ids
        assert "analysis-ETCH-10m" in job_ids
        await sched.shutdown(timeout=1.0)

    async def test_reload_removes_old_jobs_first(self):
        deps = self._make_deps_with_profile()
        sched = AnalysisScheduler(
            AppSettings(scheduler_misfire_grace_time=60), deps
        )
        await sched.start()
        await sched.reload(["CVD"])
        assert len(sched._scheduler.get_jobs()) == 2
        await sched.reload(["ETCH"])
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert "analysis-CVD-5m" not in job_ids
        assert "analysis-ETCH-5m" in job_ids
        await sched.shutdown(timeout=1.0)

    async def test_reload_skips_process_with_no_intervals(self):
        deps = self._make_deps_with_profile()
        deps.profile_repo.get_scheduling_intervals = AsyncMock(return_value=[])
        sched = AnalysisScheduler(
            AppSettings(scheduler_misfire_grace_time=60), deps
        )
        await sched.start()
        await sched.reload(["CVD"])
        assert len(sched._scheduler.get_jobs()) == 0
        await sched.shutdown(timeout=1.0)

    async def test_reload_schedules_eqp_only_process(self):
        # Regression: a process whose ONLY profile doc is eqp/model-scoped must
        # still get a job. reload trusts get_scheduling_intervals (which folds in
        # overlays), so a single interval from an eqp-only doc registers a job —
        # whereas resolve_profile(process,"*","*") would have returned None.
        deps = self._make_deps_with_profile()
        deps.profile_repo.get_scheduling_intervals = AsyncMock(return_value=[5])
        sched = AnalysisScheduler(
            AppSettings(scheduler_misfire_grace_time=60), deps
        )
        await sched.start()
        await sched.reload(["CVD"])
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert job_ids == {"analysis-CVD-5m"}
        await sched.shutdown(timeout=1.0)

    async def test_reload_uses_debug_processes_when_none_passed(self):
        deps = self._make_deps_with_profile()
        debug_settings = AppSettings(
            scheduler_misfire_grace_time=60,
            debug_read_only=True,
            debug_processes=["PVD"],
        )
        sched = AnalysisScheduler(debug_settings, deps)
        await sched.start()
        await sched.reload()  # no processes arg → debug mode resolution
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert "analysis-PVD-5m" in job_ids
        assert "analysis-PVD-10m" in job_ids
        await sched.shutdown(timeout=1.0)


# ----------------------------------------------------------------------
# Cadence reconcile — pick up profile cadence changes WITHOUT a full
# remove_all_jobs rebuild. reconcile() re-derives the owned processes'
# scheduling intervals and applies only the delta; unchanged jobs keep
# their next_run_time. This is what makes a profile edit's new evaluation
# cadence take effect without a pod restart / partition reassignment.
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestSchedulerReconcile:
    def _make_deps(self, intervals):
        deps = SimpleNamespace(
            es=MagicMock(),
            profile_repo=MagicMock(
                get_scheduling_intervals=AsyncMock(return_value=list(intervals)),
            ),
            eqp_info_repo=MagicMock(),
            zk_lock=MagicMock(),
            cooldown_mgr=MagicMock(),
            email_client=MagicMock(),
            query_builder=MagicMock(),
        )
        return deps

    def _settings(self, **kw):
        # reconcile loop disabled (0) so these tests drive reconcile() directly
        kw.setdefault("scheduler_misfire_grace_time", 60)
        kw.setdefault("scheduler_reconcile_interval_sec", 0)
        return AppSettings(**kw)

    async def test_reconcile_noop_when_cadence_unchanged(self):
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])
        before = {j.id: j.next_run_time for j in sched._scheduler.get_jobs()}

        changed = await sched.reconcile()

        assert changed is False
        after = {j.id: j.next_run_time for j in sched._scheduler.get_jobs()}
        assert after.keys() == before.keys()
        # unchanged jobs must NOT be recreated (next_run preserved)
        assert after == before
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_adds_new_interval_without_touching_existing(self):
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])
        nrt_5m_before = {
            j.id: j.next_run_time
            for j in sched._scheduler.get_jobs()
            if j.id == "analysis-CVD-5m"
        }
        # operator added a rule at a brand-new cadence
        deps.profile_repo.get_scheduling_intervals = AsyncMock(
            return_value=[5, 10, 15]
        )

        changed = await sched.reconcile()

        assert changed is True
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert job_ids == {"analysis-CVD-5m", "analysis-CVD-10m", "analysis-CVD-15m"}
        # the pre-existing 5m job must be the SAME job (not rebuilt)
        nrt_5m_after = {
            j.id: j.next_run_time
            for j in sched._scheduler.get_jobs()
            if j.id == "analysis-CVD-5m"
        }
        assert nrt_5m_after == nrt_5m_before
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_removes_dropped_interval(self):
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])
        deps.profile_repo.get_scheduling_intervals = AsyncMock(return_value=[5])

        changed = await sched.reconcile()

        assert changed is True
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert job_ids == {"analysis-CVD-5m"}
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_noop_when_not_yet_assigned_normal_mode(self):
        # normal mode, reload() never called → no owned processes → no-op,
        # and Mongo must NOT be queried.
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(self._settings(debug_read_only=False), deps)
        await sched.start()

        changed = await sched.reconcile()

        assert changed is False
        assert sched._scheduler.get_jobs() == []
        deps.profile_repo.get_scheduling_intervals.assert_not_awaited()
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_resolves_debug_processes_when_unassigned(self):
        # debug mode, no reload yet → reconcile falls back to debug processes
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(
            self._settings(debug_read_only=True, debug_processes=["PVD"]), deps
        )
        await sched.start()

        changed = await sched.reconcile()

        assert changed is True
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert job_ids == {"analysis-PVD-5m", "analysis-PVD-10m"}
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_drops_jobs_for_no_longer_owned_process(self):
        # owned set shrinks (e.g. all rules disabled) → its jobs are removed
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])
        deps.profile_repo.get_scheduling_intervals = AsyncMock(return_value=[])

        changed = await sched.reconcile()

        assert changed is True
        assert sched._scheduler.get_jobs() == []
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_noop_when_paused(self):
        # quiescence contract: a paused scheduler (ZK SUSPENDED/LOST) must not
        # have its job set mutated by a write/admin-triggered reconcile.
        deps = self._make_deps([5, 10])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])
        before = {j.id for j in sched._scheduler.get_jobs()}
        # a cadence change is pending, but we're paused → must be ignored
        deps.profile_repo.get_scheduling_intervals = AsyncMock(return_value=[5, 10, 15])
        await sched.pause_all_jobs()

        changed = await sched.reconcile()

        assert changed is False
        assert {j.id for j in sched._scheduler.get_jobs()} == before
        deps.profile_repo.get_scheduling_intervals.assert_not_awaited()
        await sched.shutdown(timeout=1.0)

    async def test_reconcile_noop_after_shutdown(self):
        # a late write-trigger reconcile arriving after shutdown must not
        # re-add jobs to a torn-down scheduler.
        deps = self._make_deps([5])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])
        await sched.shutdown(timeout=1.0)
        deps.profile_repo.get_scheduling_intervals = AsyncMock(return_value=[5, 10])

        changed = await sched.reconcile()

        assert changed is False
        deps.profile_repo.get_scheduling_intervals.assert_not_awaited()

    async def test_reconcile_and_reload_are_serialized(self):
        # reconcile() and reload() must be mutually exclusive: a reconcile
        # in-flight (holding the lock) blocks a concurrent partition reload
        # until it finishes, so reload's remove_all_jobs can't interleave with
        # reconcile's snapshot→apply. Final state reflects the owner only.
        deps = self._make_deps([5])
        sched = AnalysisScheduler(self._settings(), deps)
        await sched.start()
        await sched.reload(["CVD"])

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_intervals(process):
            entered.set()
            await release.wait()
            return [5]

        deps.profile_repo.get_scheduling_intervals = AsyncMock(
            side_effect=blocking_intervals
        )

        recon_task = asyncio.create_task(sched.reconcile())
        await entered.wait()  # reconcile now holds the lock, blocked on Mongo
        reload_task = asyncio.create_task(sched.reload(["PVD"]))
        await asyncio.sleep(0.02)
        # reload must be blocked on the lock — it has NOT run remove_all_jobs yet
        assert {j.id for j in sched._scheduler.get_jobs()} == {"analysis-CVD-5m"}

        release.set()
        await recon_task
        await reload_task
        # reload won: only the owner's job remains, no stale un-owned job
        assert {j.id for j in sched._scheduler.get_jobs()} == {"analysis-PVD-5m"}
        await sched.shutdown(timeout=1.0)


@pytest.mark.unit
class TestReconcileLoop:
    def _make_sched(self, interval_sec):
        deps = MagicMock()
        settings = AppSettings(
            scheduler_misfire_grace_time=60,
            scheduler_reconcile_interval_sec=interval_sec,
        )
        return AnalysisScheduler(settings, deps)

    async def test_loop_calls_reconcile_periodically_and_shutdown_cancels(self):
        sched = self._make_sched(0.01)
        sched.reconcile = AsyncMock(return_value=False)
        await sched.start()
        await asyncio.sleep(0.06)
        assert sched.reconcile.await_count >= 2
        await sched.shutdown(timeout=1.0)
        # task is cancelled/cleared on shutdown
        assert sched._reconcile_task is None
        count_at_shutdown = sched.reconcile.await_count
        await asyncio.sleep(0.03)
        assert sched.reconcile.await_count == count_at_shutdown  # stopped

    async def test_loop_survives_reconcile_exception(self):
        sched = self._make_sched(0.01)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient mongo blip")
            return False

        sched.reconcile = flaky
        await sched.start()
        await asyncio.sleep(0.06)
        await sched.shutdown(timeout=1.0)
        # kept ticking after the first call raised
        assert calls["n"] >= 2

    async def test_loop_disabled_when_interval_zero(self):
        sched = self._make_sched(0)
        sched.reconcile = AsyncMock(return_value=False)
        await sched.start()
        await asyncio.sleep(0.04)
        assert sched._reconcile_task is None
        sched.reconcile.assert_not_awaited()
        await sched.shutdown(timeout=1.0)

    async def test_loop_not_started_in_debug_mode(self):
        # debug_read_only preserves the "manual observer" contract: the periodic
        # loop does NOT auto-start analysis jobs (operator triggers via admin /
        # the write path). reconcile() itself still works when called directly.
        settings = AppSettings(
            scheduler_misfire_grace_time=60,
            scheduler_reconcile_interval_sec=0.01,
            debug_read_only=True,
            debug_processes=["X"],
        )
        sched = AnalysisScheduler(settings, MagicMock())
        sched.reconcile = AsyncMock(return_value=False)
        await sched.start()
        await asyncio.sleep(0.04)
        assert sched._reconcile_task is None
        sched.reconcile.assert_not_awaited()
        await sched.shutdown(timeout=1.0)
