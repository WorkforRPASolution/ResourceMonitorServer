"""Tests for src.distributed.partition_manager.

PartitionManager is the most complex piece of the distributed layer. We test:
- Even distribution algorithm (pure logic, no ZK)
- Stale assignment defense (epoch + assigned_at comparison)
- DataWatch empty-node guard (v4 fix)
- Membership debounce via Task cancel/recreate
- LOST → CONNECTED reinit triggers LeaderElection.restart_after_loss
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from kazoo.protocol.states import KazooState

from src.distributed.partition_manager import PartitionManager


@pytest.fixture
async def mock_zk():
    zk = MagicMock()
    zk.root_path = "/resource-monitor"
    zk.kazoo = MagicMock()
    zk.loop = asyncio.get_running_loop()
    zk.add_state_handler = MagicMock()
    return zk


@pytest.fixture
def mock_leader():
    leader = MagicMock()
    leader.is_leader.return_value = True
    leader.epoch = 1
    leader.add_on_acquired_callback = MagicMock()
    leader.restart_after_loss = AsyncMock()
    return leader


@pytest.fixture
def mock_eqp_repo():
    repo = MagicMock()
    repo.get_distinct_processes = AsyncMock(return_value=["CVD", "ETCH", "PHOTO"])
    return repo


def _make_pm(mock_zk, mock_leader, mock_eqp_repo, scheduler=None):
    return PartitionManager(
        zk_client=mock_zk,
        leader_election=mock_leader,
        eqp_repo=mock_eqp_repo,
        instance_id="inst-1",
        scheduler_provider=lambda: scheduler,
    )


# ----------------------------------------------------------------------
# Pure assignment logic
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestComputeAssignments:
    async def test_round_robin_distribution(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        result = pm._compute_assignments(
            ["a", "b", "c"], ["P1", "P2", "P3", "P4", "P5"]
        )
        # Sorted instances first, then round-robin sorted processes
        assert sorted(sum(result.values(), [])) == ["P1", "P2", "P3", "P4", "P5"]
        # Each instance gets at least one
        for procs in result.values():
            assert len(procs) >= 1

    async def test_single_instance_gets_all(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        result = pm._compute_assignments(["only"], ["A", "B", "C"])
        assert result == {"only": ["A", "B", "C"]}

    async def test_more_instances_than_processes(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        result = pm._compute_assignments(["a", "b", "c"], ["P1"])
        assigned = [k for k, v in result.items() if v]
        empty = [k for k, v in result.items() if not v]
        assert len(assigned) == 1
        assert len(empty) == 2


# ----------------------------------------------------------------------
# apply_assignment — stale defense
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestApplyAssignmentStaleDefense:
    async def test_higher_epoch_wins(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._apply_assignment(
            {"processes": ["A"], "leader_epoch": 1, "assigned_at": 100.0}
        )
        await pm._apply_assignment(
            {"processes": ["B"], "leader_epoch": 2, "assigned_at": 50.0}
        )
        assert pm.get_my_processes() == ["B"]

    async def test_lower_epoch_ignored(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._apply_assignment(
            {"processes": ["A"], "leader_epoch": 5, "assigned_at": 100.0}
        )
        await pm._apply_assignment(
            {"processes": ["B"], "leader_epoch": 4, "assigned_at": 200.0}
        )
        assert pm.get_my_processes() == ["A"]

    async def test_same_epoch_newer_timestamp_wins(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._apply_assignment(
            {"processes": ["A"], "leader_epoch": 1, "assigned_at": 100.0}
        )
        await pm._apply_assignment(
            {"processes": ["B"], "leader_epoch": 1, "assigned_at": 200.0}
        )
        assert pm.get_my_processes() == ["B"]

    async def test_same_epoch_older_timestamp_ignored(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._apply_assignment(
            {"processes": ["A"], "leader_epoch": 1, "assigned_at": 200.0}
        )
        await pm._apply_assignment(
            {"processes": ["B"], "leader_epoch": 1, "assigned_at": 100.0}
        )
        assert pm.get_my_processes() == ["A"]


# ----------------------------------------------------------------------
# DataWatch empty-node guard (v4 fix)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestDataWatchEmptyGuard:
    async def test_empty_data_does_not_crash(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """ensure_path creates an empty node — DataWatch fires with b''.
        json.loads(b'') would raise. Must be guarded."""
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        # Must NOT raise
        pm._on_assignment_changed_sync(b"", None, None)
        pm._on_assignment_changed_sync(None, None, None)

    async def test_invalid_json_does_not_crash(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        pm._on_assignment_changed_sync(b"not json {", None, None)

    async def test_valid_payload_is_applied(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        payload = {
            "processes": ["CVD"],
            "leader_epoch": 1,
            "assigned_at": time.time(),
        }
        pm._on_assignment_changed_sync(
            json.dumps(payload).encode(), None, None
        )
        await asyncio.sleep(0.05)
        assert pm.get_my_processes() == ["CVD"]


# ----------------------------------------------------------------------
# Membership debounce — Task cancel/recreate (NOT a flag)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestDebouncedRedistribute:
    async def test_burst_only_runs_once(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        pm._do_redistribute = AsyncMock()
        # Make the debounce window short for the test
        pm._DEBOUNCE_SEC = 0.05

        for _ in range(5):
            await pm._handle_membership_change(["a", "b"])
            await asyncio.sleep(0.01)

        # Wait long enough for the FINAL debounce to fire
        await asyncio.sleep(0.15)
        assert pm._do_redistribute.await_count == 1

    async def test_non_leader_does_nothing(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        mock_leader.is_leader.return_value = False
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        pm._do_redistribute = AsyncMock()
        await pm._handle_membership_change(["a", "b"])
        await asyncio.sleep(0.05)
        pm._do_redistribute.assert_not_called()


# ----------------------------------------------------------------------
# Redistribute retry + unhealthy flag (P0-4)
# ----------------------------------------------------------------------
def _ok_transaction(mock_zk):
    """Set up mock_zk so kazoo.transaction().commit() returns []."""
    txn = MagicMock()
    txn.set_data = MagicMock()
    txn.commit = MagicMock(return_value=[])
    mock_zk.kazoo.transaction.return_value = txn
    return txn


async def _drain_retry_task(pm) -> None:
    """Cancel and await the auto-scheduled retry task so background recursion
    doesn't pollute the next test step. Tests are direct-call-driven, so the
    retry tasks would only ever waste time anyway."""
    task = pm._redistribute_retry_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.unit
class TestRedistributeRetry:
    """v6 P0-4: a Mongo blip during leader redistribution must NOT silently
    leave the cluster with stale assignments. The leader retries up to 5
    times with exponential backoff; on persistent failure the leader sets
    ``redistribute_unhealthy=True``, which surfaces in /healthz/ready as 503
    so K8s pulls traffic and operators get paged. The previous behavior was
    a single uncaught exception in the leader's election callback (silent
    stall — leader stays leader, no assignments ever written)."""

    async def test_initial_state_healthy(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        assert pm.redistribute_unhealthy is False
        assert pm._redistribute_attempt == 0
        assert pm._redistribute_retry_task is None

    async def test_successful_redistribute_keeps_state_clean(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        _ok_transaction(mock_zk)
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._do_redistribute(["inst-1"])
        assert pm.redistribute_unhealthy is False
        assert pm._redistribute_attempt == 0

    async def test_successful_redistribute_clears_prior_unhealthy(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """Recovery: if a prior call set unhealthy, success must clear it."""
        _ok_transaction(mock_zk)
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        pm._redistribute_unhealthy = True
        pm._redistribute_attempt = 3
        await pm._do_redistribute(["inst-1"])
        assert pm.redistribute_unhealthy is False
        assert pm._redistribute_attempt == 0

    async def test_mongo_failure_increments_attempt_and_schedules_retry(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        mock_eqp_repo.get_distinct_processes.side_effect = ConnectionError("down")
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)

        await pm._do_redistribute(["inst-1"])

        assert pm._redistribute_attempt == 1
        assert pm.redistribute_unhealthy is False
        assert pm._redistribute_retry_task is not None
        await _drain_retry_task(pm)

    async def test_persistent_mongo_failure_flags_unhealthy_after_5(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        mock_eqp_repo.get_distinct_processes.side_effect = ConnectionError("down")
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)

        for _ in range(5):
            await pm._do_redistribute(["inst-1"])
            await _drain_retry_task(pm)

        assert pm._redistribute_attempt == 5
        assert pm.redistribute_unhealthy is True

    async def test_recovery_after_two_failures(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        _ok_transaction(mock_zk)
        call_count = {"n": 0}

        async def get_distinct():
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ConnectionError("transient")
            return ["CVD"]

        mock_eqp_repo.get_distinct_processes = AsyncMock(side_effect=get_distinct)
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)

        await pm._do_redistribute(["inst-1"])  # fail 1
        assert pm._redistribute_attempt == 1
        await _drain_retry_task(pm)

        await pm._do_redistribute(["inst-1"])  # fail 2
        assert pm._redistribute_attempt == 2
        await _drain_retry_task(pm)

        await pm._do_redistribute(["inst-1"])  # success
        assert pm._redistribute_attempt == 0
        assert pm.redistribute_unhealthy is False

    async def test_zk_transaction_failure_triggers_retry(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """ZK side failure (e.g. transaction.commit raises) must also retry,
        not just Mongo. The leader's job is to keep trying until either it
        succeeds or it gives up loudly."""
        txn = MagicMock()
        txn.set_data = MagicMock()
        txn.commit = MagicMock(side_effect=RuntimeError("zk down"))
        mock_zk.kazoo.transaction.return_value = txn

        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._do_redistribute(["inst-1"])

        assert pm._redistribute_attempt == 1
        assert pm._redistribute_retry_task is not None
        await _drain_retry_task(pm)


# ----------------------------------------------------------------------
# Orphan assignment cleanup (H1)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestOrphanAssignmentCleanup:
    """v6 H1: on every rolling update MONITOR_INSTANCE_ID changes because it's
    bound to metadata.name, so assignment znodes from departed pods would
    accumulate forever (they're persistent, not ephemeral). The leader GCs
    orphans at the tail of every successful redistribute. Correctness is
    untouched either way (epoch+ts guard in _apply_assignment handles stale
    reads), but without GC the ZK snapshot bloats and operators have to
    hand-clean.
    """

    async def test_cleanup_deletes_orphans_only(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        # ZK reports 4 existing assignment children but only 2 are live
        mock_zk.kazoo.get_children = MagicMock(
            return_value=["rms-old-1", "rms-old-2", "inst-1", "inst-2"]
        )
        mock_zk.kazoo.delete = MagicMock()

        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._cleanup_orphan_assignments(["inst-1", "inst-2"])

        deleted_paths = [
            call.args[0] for call in mock_zk.kazoo.delete.call_args_list
        ]
        assert sorted(deleted_paths) == [
            "/resource-monitor/assignments/rms-old-1",
            "/resource-monitor/assignments/rms-old-2",
        ]
        # Live instance nodes must NOT be touched
        for path in deleted_paths:
            assert "inst-1" not in path
            assert "inst-2" not in path

    async def test_cleanup_no_orphans_is_noop(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        mock_zk.kazoo.get_children = MagicMock(return_value=["inst-1"])
        mock_zk.kazoo.delete = MagicMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._cleanup_orphan_assignments(["inst-1"])
        mock_zk.kazoo.delete.assert_not_called()

    async def test_cleanup_delete_failure_does_not_raise(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """ZK delete can fail for many transient reasons (lost connection,
        BadVersion, race with another cleanup). Cleanup must swallow them —
        the transaction has already committed and leaking means we'd set
        redistribute_unhealthy for a housekeeping failure."""
        mock_zk.kazoo.get_children = MagicMock(return_value=["rms-old-1"])
        mock_zk.kazoo.delete = MagicMock(side_effect=RuntimeError("zk boom"))
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        # Must NOT raise
        await pm._cleanup_orphan_assignments(["inst-1"])
        assert pm.redistribute_unhealthy is False

    async def test_cleanup_nonode_error_is_benign(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """Racing reinit may delete the same node first; NoNodeError is fine."""
        from kazoo.exceptions import NoNodeError
        mock_zk.kazoo.get_children = MagicMock(return_value=["rms-old-1"])
        mock_zk.kazoo.delete = MagicMock(side_effect=NoNodeError())
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._cleanup_orphan_assignments(["inst-1"])
        assert pm.redistribute_unhealthy is False

    async def test_cleanup_get_children_failure_is_logged_not_raised(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        mock_zk.kazoo.get_children = MagicMock(
            side_effect=RuntimeError("zk list failed")
        )
        mock_zk.kazoo.delete = MagicMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._cleanup_orphan_assignments(["inst-1"])
        mock_zk.kazoo.delete.assert_not_called()
        assert pm.redistribute_unhealthy is False

    async def test_successful_do_redistribute_invokes_cleanup(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """End-to-end wiring: a clean _do_redistribute must call cleanup with
        the current instances list. This is the regression guard for the
        'someone deletes the await call' foot-gun."""
        _ok_transaction(mock_zk)
        mock_zk.kazoo.get_children = MagicMock(
            return_value=["inst-1", "rms-stale"]
        )
        mock_zk.kazoo.delete = MagicMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._do_redistribute(["inst-1"])
        mock_zk.kazoo.delete.assert_called_once_with(
            "/resource-monitor/assignments/rms-stale"
        )

    async def test_failed_redistribute_does_not_cleanup(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        """If the transaction itself fails, cleanup must NOT run — retries
        happen with a fresh _do_redistribute and cleanup is gated on success."""
        txn = MagicMock()
        txn.set_data = MagicMock()
        txn.commit = MagicMock(side_effect=RuntimeError("zk down"))
        mock_zk.kazoo.transaction.return_value = txn
        mock_zk.kazoo.get_children = MagicMock(
            return_value=["inst-1", "rms-stale"]
        )
        mock_zk.kazoo.delete = MagicMock()

        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
        await pm._do_redistribute(["inst-1"])
        await _drain_retry_task(pm)

        mock_zk.kazoo.delete.assert_not_called()


# ----------------------------------------------------------------------
# State change → reinit
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestOnZKStateChange:
    async def test_lost_pauses_scheduler_and_resets_state(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        scheduler = MagicMock()
        scheduler.pause_all_jobs = AsyncMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo, scheduler=scheduler)
        # Pre-populate state
        pm._known_epoch = 5
        pm._assigned_processes = ["CVD"]

        await pm.on_zk_state_change(KazooState.LOST)
        assert pm._session_lost is True
        assert pm._known_epoch == 0
        assert pm._assigned_processes == []
        scheduler.pause_all_jobs.assert_awaited_once()

    async def test_suspended_pauses_scheduler(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        scheduler = MagicMock()
        scheduler.pause_all_jobs = AsyncMock()
        scheduler.resume_jobs_for = AsyncMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo, scheduler=scheduler)
        await pm.on_zk_state_change(KazooState.SUSPENDED)
        scheduler.pause_all_jobs.assert_awaited_once()

    async def test_connected_after_loss_calls_restart_after_loss(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        scheduler = MagicMock()
        scheduler.pause_all_jobs = AsyncMock()
        scheduler.resume_jobs_for = AsyncMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo, scheduler=scheduler)
        # Stub out the ZK-touching parts of reinit
        pm._register_member = AsyncMock()
        pm._register_watches = MagicMock()
        pm._refresh_assignment_from_zk = AsyncMock()

        # First mark as lost
        await pm.on_zk_state_change(KazooState.LOST)
        # Then reconnect
        await pm.on_zk_state_change(KazooState.CONNECTED)

        # v4: critical that LeaderElection is restarted, not just watches
        mock_leader.restart_after_loss.assert_awaited_once()
        # And state machine is cleared
        assert pm._session_lost is False

    async def test_connected_without_prior_loss_does_not_restart(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        scheduler = MagicMock()
        scheduler.resume_jobs_for = AsyncMock()
        pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo, scheduler=scheduler)
        await pm.on_zk_state_change(KazooState.CONNECTED)
        mock_leader.restart_after_loss.assert_not_awaited()


# ----------------------------------------------------------------------
# watches re-registration (idempotent via watch_epoch)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestWatchEpoch:
    async def test_register_watches_increments_epoch(
        self, mock_zk, mock_leader, mock_eqp_repo
    ):
        # Stub out ChildrenWatch / DataWatch to capture registrations
        from src.distributed import partition_manager as pm_mod

        captured = {"calls": 0}

        class FakeWatch:
            def __init__(self, *_a, **_kw):
                captured["calls"] += 1

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(pm_mod, "ChildrenWatch", FakeWatch)
            mp.setattr(pm_mod, "DataWatch", FakeWatch)

            pm = _make_pm(mock_zk, mock_leader, mock_eqp_repo)
            pm._register_watches()
            first = pm._watch_epoch
            pm._register_watches()
            second = pm._watch_epoch

        assert first < second
        assert captured["calls"] == 4  # 2 watches × 2 register calls
