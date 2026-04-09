"""Leader election with persistent epoch and LOST-recovery.

Two critical v4 design points:

1. **Fire-and-forget**: ``election.run(callback)`` is a SYNCHRONOUS, blocking
   call (it waits for the callback to return, which we keep blocking on a
   ``threading.Event``). If we ``await loop.run_in_executor(None, …)`` on it
   directly we lose the worker thread for the lifetime of the program. We
   instead schedule it on a dedicated executor and DO NOT await the future
   from ``start()``.

2. **Restart after LOST**: when the ZK session dies, the in-memory ``Election``
   object is bound to the dead session and is unrecoverable. ``restart_after_loss``
   constructs a brand-new ``Election`` and reschedules ``run`` on the executor.
   The dead future will already have returned (election.run unwinds when the
   session dies), so this is safe.

The leader epoch is persisted at ``{root}/leader-epoch`` so a fresh process
starting up does not reset to 0 (which would let stale assignments from the
previous leader silently win the freshness comparison in PartitionManager).
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor

import structlog
from kazoo.exceptions import ConnectionClosedError, SessionExpiredError
from kazoo.recipe.election import Election

from src.config.constants import ZK_PATH_LEADER_ELECTION, ZK_PATH_LEADER_EPOCH
from src.distributed.zk_client import ZKClient

logger = structlog.get_logger(__name__)

OnAcquiredCallback = Callable[[int], Awaitable[None]]


class LeaderElection:
    def __init__(self, zk_client: ZKClient, instance_id: str) -> None:
        self._zk = zk_client
        self._instance_id = instance_id
        self._election_path = f"{zk_client.root_path}/{ZK_PATH_LEADER_ELECTION}"
        self._epoch_path = f"{zk_client.root_path}/{ZK_PATH_LEADER_EPOCH}"
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="zk-election"
        )
        self._stop_event = threading.Event()
        self._is_leader = False
        self._epoch = 0
        self._on_acquired_callbacks: list[OnAcquiredCallback] = []
        self._election: Election | None = None
        self._election_future: Future[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def epoch(self) -> int:
        return self._epoch

    def add_on_acquired_callback(self, cb: OnAcquiredCallback) -> None:
        self._on_acquired_callbacks.append(cb)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Schedule the election in the background. Returns immediately."""
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._epoch_path)
        )
        self._start_election_run()

    def _start_election_run(self) -> None:
        """Build a fresh ``Election`` and schedule ``run`` — non-blocking."""
        self._election = Election(
            self._zk.kazoo, self._election_path, self._instance_id
        )
        self._stop_event.clear()
        self._election_future = self._zk.loop.run_in_executor(
            self._executor,
            self._election.run,
            self._on_become_leader_sync,
        )

    async def restart_after_loss(self) -> None:
        """Recreate ``Election`` after a LOST session.

        The old Election is bound to a dead session and cannot be reused. The
        old run() will already have returned with an exception by the time we
        get here. We drain its future, then schedule a new Election.
        """
        if self._stopped:
            return
        logger.info("leader_election_restarting_after_loss")
        # Wake the old callback's wait() so the executor thread unblocks.
        self._stop_event.set()
        if self._election_future is not None:
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(self._election_future), timeout=5
                )
            except (asyncio.TimeoutError, Exception):
                pass
        self._is_leader = False
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._epoch_path)
        )
        self._start_election_run()

    async def stop(self) -> None:
        self._stopped = True
        self._stop_event.set()
        try:
            if self._election_future is not None:
                await asyncio.wait_for(
                    asyncio.wrap_future(self._election_future), timeout=10
                )
        except asyncio.TimeoutError:
            logger.warning("leader_election_stop_timeout")
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Election callback (runs on the kazoo election thread)
    # ------------------------------------------------------------------
    def _on_become_leader_sync(self) -> None:
        """Invoked by kazoo's Election recipe in its dedicated thread.

        We:
        1. Read + bump + persist the leader epoch
        2. Bridge `on_acquired` callbacks back to the asyncio loop
        3. BLOCK until ``stop_event`` is set so ``election.run`` does not return
           (returning would relinquish leadership immediately)
        """
        try:
            data, _ = self._zk.kazoo.get(self._epoch_path)
            current = int(data.decode()) if data else 0
            new_epoch = current + 1
            self._zk.kazoo.set(self._epoch_path, str(new_epoch).encode())
            self._epoch = new_epoch
            self._is_leader = True
            logger.info(
                "became_leader",
                instance=self._instance_id,
                epoch=new_epoch,
            )

            loop = self._zk.loop
            if loop is not None and not loop.is_closed():
                for cb in self._on_acquired_callbacks:
                    asyncio.run_coroutine_threadsafe(cb(new_epoch), loop)

            # Hold leadership until stop()/restart_after_loss() releases us.
            self._stop_event.wait()
        except (SessionExpiredError, ConnectionClosedError) as e:
            logger.warning(
                "leader_election_session_lost_in_handler", error=str(e)
            )
        except Exception as e:
            logger.error(
                "leader_election_handler_failed", error=str(e), exc_info=True
            )
        finally:
            self._is_leader = False
