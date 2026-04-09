"""Tests for src.distributed.lock (ZKAnalysisLock).

Critical v4 invariant: a NEW kazoo Lock object must be created on every
acquire — kazoo Lock is not re-entrant. Caching the Lock instance and
re-acquiring it has undefined behavior.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from kazoo.exceptions import (
    ConnectionClosedError,
    SessionExpiredError,
)

from src.distributed.lock import (
    LockAcquisitionTimeout,
    NoOpZKLock,
    ZKAnalysisLock,
)


@pytest.fixture
async def mock_zk_client():
    """ZKClient stub with a synchronous kazoo handle and the running asyncio loop.

    Async fixture so we can capture the running loop the test is on; using
    `asyncio.get_event_loop()` (deprecated in 3.14) crashes outside a loop.
    """
    zk = MagicMock()
    zk.root_path = "/resource-monitor"
    zk.kazoo = MagicMock()
    zk.loop = asyncio.get_running_loop()
    return zk


@pytest.mark.unit
class TestZKAnalysisLockAcquire:
    async def test_acquires_and_releases_kazoo_lock(self, mock_zk_client):
        zk_lock = ZKAnalysisLock(mock_zk_client)
        kazoo_lock = MagicMock()
        kazoo_lock.acquire.return_value = True
        mock_zk_client.kazoo.Lock.return_value = kazoo_lock

        async with zk_lock.acquire("CVD", timeout_sec=5):
            pass

        mock_zk_client.kazoo.Lock.assert_called_once_with(
            "/resource-monitor/locks/analysis-CVD"
        )
        kazoo_lock.acquire.assert_called_once()
        kazoo_lock.release.assert_called_once()

    async def test_creates_new_kazoo_lock_per_acquire(self, mock_zk_client):
        """v4 critical: kazoo Lock is NOT re-entrant — must be re-created."""
        zk_lock = ZKAnalysisLock(mock_zk_client)
        lock_objs = []

        def make_lock(_path):
            lock = MagicMock()
            lock.acquire.return_value = True
            lock_objs.append(lock)
            return lock

        mock_zk_client.kazoo.Lock.side_effect = make_lock

        async with zk_lock.acquire("CVD"):
            pass
        async with zk_lock.acquire("CVD"):
            pass

        assert len(lock_objs) == 2
        assert lock_objs[0] is not lock_objs[1]

    async def test_raises_on_timeout(self, mock_zk_client):
        zk_lock = ZKAnalysisLock(mock_zk_client)
        kazoo_lock = MagicMock()
        kazoo_lock.acquire.return_value = False  # timeout
        mock_zk_client.kazoo.Lock.return_value = kazoo_lock

        with pytest.raises(LockAcquisitionTimeout):
            async with zk_lock.acquire("CVD", timeout_sec=1):
                pass
        # release should NOT have been called (we never held it)
        kazoo_lock.release.assert_not_called()

    async def test_release_swallows_session_expired(self, mock_zk_client):
        """If the session dies mid-critical-section, kazoo's release will raise
        — but the ephemeral lock node is auto-cleaned, so we can ignore it."""
        zk_lock = ZKAnalysisLock(mock_zk_client)
        kazoo_lock = MagicMock()
        kazoo_lock.acquire.return_value = True
        kazoo_lock.release.side_effect = SessionExpiredError()
        mock_zk_client.kazoo.Lock.return_value = kazoo_lock

        # Must NOT raise
        async with zk_lock.acquire("CVD"):
            pass

    async def test_release_swallows_connection_closed(self, mock_zk_client):
        zk_lock = ZKAnalysisLock(mock_zk_client)
        kazoo_lock = MagicMock()
        kazoo_lock.acquire.return_value = True
        kazoo_lock.release.side_effect = ConnectionClosedError()
        mock_zk_client.kazoo.Lock.return_value = kazoo_lock

        async with zk_lock.acquire("CVD"):
            pass

    async def test_release_runs_even_if_body_raises(self, mock_zk_client):
        """The ZK lock must always be released even if the user body throws."""
        zk_lock = ZKAnalysisLock(mock_zk_client)
        kazoo_lock = MagicMock()
        kazoo_lock.acquire.return_value = True
        mock_zk_client.kazoo.Lock.return_value = kazoo_lock

        with pytest.raises(ValueError):
            async with zk_lock.acquire("CVD"):
                raise ValueError("oops")
        kazoo_lock.release.assert_called_once()


@pytest.mark.unit
class TestAsyncioLockSerialization:
    async def test_concurrent_calls_for_same_process_serialized(self, mock_zk_client):
        """Two coroutines acquiring the same process must NOT both create kazoo
        Lock objects in parallel — the per-process asyncio.Lock serializes them."""
        zk_lock = ZKAnalysisLock(mock_zk_client)
        active = 0
        peak = 0

        def make_lock(_path):
            nonlocal active, peak
            lock = MagicMock()
            lock.acquire.return_value = True

            def real_acquire(*_a, **_kw):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                return True

            def real_release():
                nonlocal active
                active -= 1

            lock.acquire.side_effect = real_acquire
            lock.release.side_effect = real_release
            return lock

        mock_zk_client.kazoo.Lock.side_effect = make_lock

        async def worker():
            async with zk_lock.acquire("CVD"):
                await asyncio.sleep(0.01)

        await asyncio.gather(worker(), worker(), worker())
        assert peak == 1  # never overlapping

    async def test_different_processes_get_independent_asyncio_locks(
        self, mock_zk_client
    ):
        zk_lock = ZKAnalysisLock(mock_zk_client)
        zk_lock._get_asyncio_lock("CVD")
        zk_lock._get_asyncio_lock("ETCH")
        assert "CVD" in zk_lock._asyncio_locks
        assert "ETCH" in zk_lock._asyncio_locks
        assert (
            zk_lock._asyncio_locks["CVD"]
            is not zk_lock._asyncio_locks["ETCH"]
        )


class TestNoOpZKLock:
    """Debug Read-Only mode: when ZK is not connected, the scheduler still
    needs a lock-shaped object so analysis jobs can wrap themselves in
    ``async with zk_lock.acquire(process)``. NoOpZKLock is the stub."""

    async def test_acquire_returns_context_manager(self):
        lock = NoOpZKLock()
        async with lock.acquire("ETCH"):
            pass  # must not raise

    async def test_acquire_ignores_timeout_kwarg(self):
        """Same signature as ZKAnalysisLock.acquire — timeout_sec param exists
        but is ignored (there's nothing to wait on)."""
        lock = NoOpZKLock()
        async with lock.acquire("CVD", timeout_sec=5):
            pass

    async def test_acquire_is_reentrant_across_processes(self):
        """Different processes: independent no-op entries."""
        lock = NoOpZKLock()
        # Intentionally nested to exercise "two locks held at once"
        async with lock.acquire("P1"):  # noqa: SIM117
            async with lock.acquire("P2"):
                pass
