"""Per-process distributed lock with two-tier serialization.

Layer 1 — `asyncio.Lock` per process name:
    Coroutines on the SAME instance never race for the SAME process. This
    avoids hammering ZK with redundant lock attempts and prevents the kazoo
    Lock object from being shared across awaits (it's not designed for that).

Layer 2 — kazoo `Lock`:
    Cross-instance mutex. Critically, we create a NEW `kazoo.Lock` object
    on every acquire. The kazoo recipe is NOT re-entrant; reusing one
    instance for two acquire() calls has undefined behavior. The on-disk
    ephemeral node is what gives us the actual mutex, so a fresh in-memory
    object is fine.

Session expiry handling:
    If the ZK session dies between acquire and release, kazoo's `release()`
    will throw `SessionExpiredError` / `ConnectionClosedError`. We swallow
    these because the ephemeral lock node is auto-cleaned by ZK when the
    session goes away — there's nothing left to release.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from kazoo.exceptions import ConnectionClosedError, SessionExpiredError

from src.distributed.zk_client import ZKClient

logger = structlog.get_logger(__name__)


class LockAcquisitionTimeout(Exception):
    def __init__(self, process: str) -> None:
        super().__init__(f"could not acquire ZK lock for process={process!r}")
        self.process = process


class NoOpZKLock:
    """No-op lock for Debug Read-Only mode.

    A debug instance doesn't connect to ZK, so there's no distributed lock
    to acquire. The scheduler's analysis jobs wrap themselves in
    ``async with zk_lock.acquire(process)``; in debug mode that block should
    just execute. Since the debug instance runs as a single process on a
    single machine, mutual exclusion between "replicas" is vacuous.

    Same interface as ``ZKAnalysisLock.acquire`` so the scheduler code can
    be oblivious.
    """

    @asynccontextmanager
    async def acquire(
        self, process: str, timeout_sec: int = 10
    ) -> AsyncIterator[None]:
        # Intentionally do nothing — debug mode is single-instance by design.
        # ``process`` and ``timeout_sec`` are accepted for signature parity.
        yield


class ZKAnalysisLock:
    def __init__(self, zk_client: ZKClient) -> None:
        self._zk = zk_client
        self._asyncio_locks: dict[str, asyncio.Lock] = {}

    def _get_asyncio_lock(self, process: str) -> asyncio.Lock:
        lock = self._asyncio_locks.get(process)
        if lock is None:
            lock = asyncio.Lock()
            self._asyncio_locks[process] = lock
        return lock

    @asynccontextmanager
    async def acquire(
        self, process: str, timeout_sec: int = 10
    ) -> AsyncIterator[None]:
        """Acquire the per-process ZK lock.

        Raises ``LockAcquisitionTimeout`` if the kazoo lock cannot be obtained
        within ``timeout_sec``. The asyncio Lock is held for the entire ZK
        critical section, so concurrent calls on the same instance queue.
        """
        async with self._get_asyncio_lock(process):
            path = f"{self._zk.root_path}/locks/analysis-{process}"
            # New Lock object every time — kazoo Lock is NOT re-entrant.
            kazoo_lock = self._zk.kazoo.Lock(path)
            acquired = False
            try:
                acquired = await self._zk.loop.run_in_executor(
                    None, lambda: kazoo_lock.acquire(timeout=timeout_sec)
                )
                if not acquired:
                    raise LockAcquisitionTimeout(process)
                yield
            finally:
                if acquired:
                    try:
                        await self._zk.loop.run_in_executor(
                            None, kazoo_lock.release
                        )
                    except (SessionExpiredError, ConnectionClosedError) as e:
                        logger.warning(
                            "lock_release_skipped_session_lost",
                            process=process,
                            error=str(e),
                        )
                    except Exception as e:
                        logger.error(
                            "lock_release_failed",
                            process=process,
                            error=str(e),
                            exc_info=True,
                        )
