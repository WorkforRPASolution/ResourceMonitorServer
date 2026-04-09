"""Partition manager — owns the per-instance "which processes do I analyze" mapping.

Coordination:
- Membership is tracked via ephemeral nodes under ``{root}/members/<instance_id>``.
- The current leader (see ``LeaderElection``) atomically writes one assignment
  blob per instance under ``{root}/assignments/<instance_id>``.
- Each instance watches its OWN assignment node and reacts to changes.
- A ``ChildrenWatch`` on members lets the leader detect joins/leaves and
  trigger redistribution.

v4 fixes baked in:
- ``DataWatch`` empty-node guard (an ``ensure_path``-created node fires with
  ``b''`` and ``json.loads`` would explode)
- watch re-registration uses a ``watch_epoch`` counter so stale callbacks
  from previous registrations short-circuit themselves
- the membership debounce is implemented with Task cancel/recreate, NOT a
  flag (the flag pattern drops events that arrive faster than the debounce)
- LOST → CONNECTED reinit calls ``LeaderElection.restart_after_loss`` because
  the old Election object is bound to the dead session
- stale-assignment defense uses (epoch, assigned_at) tuple comparison so
  same-epoch retries from the same leader still progress
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import structlog
from kazoo.exceptions import NoNodeError, NodeExistsError
from kazoo.protocol.states import KazooState
from kazoo.recipe.watchers import ChildrenWatch, DataWatch

from src.config.constants import (
    REDISTRIBUTE_DEBOUNCE_SEC,
    ZK_PATH_ASSIGNMENTS,
    ZK_PATH_MEMBERS,
)
from src.distributed.leader_election import LeaderElection
from src.distributed.zk_client import ZKClient

logger = structlog.get_logger(__name__)

SchedulerProvider = Callable[[], Any]


class PartitionManager:
    # Class-level so tests can shrink it for fast debounce verification
    _DEBOUNCE_SEC: float = REDISTRIBUTE_DEBOUNCE_SEC
    # v6 P0-4: max retries before flagging the leader unhealthy.
    _REDISTRIBUTE_MAX_ATTEMPTS: int = 5

    def __init__(
        self,
        zk_client: ZKClient,
        leader_election: LeaderElection,
        eqp_repo: Any,  # EqpInfoRepository — typed loosely to avoid cycles
        instance_id: str,
        scheduler_provider: SchedulerProvider,
    ) -> None:
        self._zk = zk_client
        self._leader = leader_election
        self._eqp_repo = eqp_repo
        self._instance_id = instance_id
        self._get_scheduler = scheduler_provider

        self._members_path = f"{zk_client.root_path}/{ZK_PATH_MEMBERS}"
        self._assignments_path = f"{zk_client.root_path}/{ZK_PATH_ASSIGNMENTS}"
        self._my_assignment_path = f"{self._assignments_path}/{instance_id}"

        # State that survives a session loss IS reset on LOST.
        self._known_epoch: int = 0
        self._known_assigned_at: float = 0.0
        self._assigned_processes: list[str] = []
        self._session_lost: bool = False

        self._watch_epoch: int = 0
        self._members_watch: ChildrenWatch | None = None
        self._assignment_watch: DataWatch | None = None
        self._redistribution_task: asyncio.Task | None = None

        # v6 P0-4: leader redistribution retry state
        self._redistribute_attempt: int = 0
        self._redistribute_unhealthy: bool = False
        self._redistribute_retry_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def is_leader(self) -> bool:
        return self._leader.is_leader()

    def get_my_processes(self) -> list[str]:
        return list(self._assigned_processes)

    @property
    def redistribute_unhealthy(self) -> bool:
        """v6 P0-4: True iff this leader has exhausted its redistribute
        retries. Surfaces in /healthz/ready as 503 so K8s pulls traffic and
        operators get paged. Cleared by the next successful redistribute."""
        return self._redistribute_unhealthy

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        # Ensure parent paths
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._members_path)
        )
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._assignments_path)
        )
        await self._register_member()
        await self._zk.loop.run_in_executor(
            None,
            lambda: self._zk.kazoo.ensure_path(self._my_assignment_path),
        )
        self._zk.add_state_handler(self.on_zk_state_change)
        self._leader.add_on_acquired_callback(self.on_become_leader)
        self._register_watches()
        await self._refresh_assignment_from_zk()

    async def stop(self) -> None:
        for task in (
            self._redistribution_task,
            self._redistribute_retry_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ------------------------------------------------------------------
    # ZK state machine bridge
    # ------------------------------------------------------------------
    async def on_zk_state_change(self, state: Any) -> None:
        scheduler = self._get_scheduler()
        if state == KazooState.SUSPENDED:
            logger.warning("zk_suspended_pausing_jobs")
            if scheduler is not None:
                await scheduler.pause_all_jobs()
        elif state == KazooState.LOST:
            logger.error("zk_session_lost")
            self._session_lost = True
            self._known_epoch = 0
            self._known_assigned_at = 0.0
            self._assigned_processes = []
            if scheduler is not None:
                await scheduler.pause_all_jobs()
        elif state == KazooState.CONNECTED:
            if self._session_lost:
                logger.info("zk_reconnected_after_loss_reinit")
                await self._reinit_after_loss()
                self._session_lost = False
            if scheduler is not None:
                await scheduler.resume_jobs_for(self._assigned_processes)

    async def _reinit_after_loss(self) -> None:
        """Recreate everything that the new ZK session lost.

        v4: critically calls ``LeaderElection.restart_after_loss`` because the
        old Election object is bound to the dead session and cannot be reused.
        """
        try:
            await self._register_member()
            await self._zk.loop.run_in_executor(
                None,
                lambda: self._zk.kazoo.ensure_path(self._my_assignment_path),
            )
            self._register_watches()
            await self._refresh_assignment_from_zk()
            await self._leader.restart_after_loss()
        except Exception as e:
            logger.error(
                "reinit_after_loss_failed", error=str(e), exc_info=True
            )

    # ------------------------------------------------------------------
    # ZK node bookkeeping
    # ------------------------------------------------------------------
    async def _register_member(self) -> None:
        path = f"{self._members_path}/{self._instance_id}"
        try:
            await self._zk.loop.run_in_executor(
                None,
                lambda: self._zk.kazoo.create(path, b"", ephemeral=True),
            )
        except NodeExistsError:
            # Old session's ephemeral hasn't been reaped yet — harmless
            pass

    def _register_watches(self) -> None:
        """(Re)register members + my-assignment watches.

        Idempotency trick: every call bumps ``_watch_epoch``. The closures we
        register capture the epoch at registration time and short-circuit if
        a newer epoch exists, so listener callbacks from prior registrations
        cannot mutate state. (We can't unregister kazoo watches directly, so
        we make them no-op by version check.)
        """
        self._watch_epoch += 1
        epoch = self._watch_epoch

        def members_cb(children):
            if epoch != self._watch_epoch:
                return False  # tells kazoo to stop renewing this watch
            self._on_members_changed_sync(children)

        def assignment_cb(data, stat, event):
            if epoch != self._watch_epoch:
                return False
            self._on_assignment_changed_sync(data, stat, event)

        self._members_watch = ChildrenWatch(
            self._zk.kazoo,
            self._members_path,
            members_cb,
            allow_session_lost=False,
        )
        self._assignment_watch = DataWatch(
            self._zk.kazoo,
            self._my_assignment_path,
            assignment_cb,
        )

    # ------------------------------------------------------------------
    # Watch callbacks (kazoo thread → asyncio loop)
    # ------------------------------------------------------------------
    def _on_members_changed_sync(self, children: list[str]) -> None:
        loop = self._zk.loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_membership_change(children), loop
        )

    def _on_assignment_changed_sync(
        self, data: bytes | None, stat: Any, event: Any
    ) -> None:
        """v4: must guard empty/missing data and JSON parse errors.

        ``ensure_path`` creates a node with ``b''`` so the FIRST DataWatch fire
        will hit this case. ``json.loads(b'')`` would raise — we just return.
        """
        if data is None or len(data) == 0:
            return
        loop = self._zk.loop
        if loop is None or loop.is_closed():
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("assignment_invalid_payload", error=str(e))
            return
        asyncio.run_coroutine_threadsafe(self._apply_assignment(payload), loop)

    # ------------------------------------------------------------------
    # Membership change → debounced redistribute (leader only)
    # ------------------------------------------------------------------
    async def _handle_membership_change(self, children: list[str]) -> None:
        if not self._leader.is_leader():
            return
        # Cancel any in-flight debounce — keep only the LATEST event
        if (
            self._redistribution_task is not None
            and not self._redistribution_task.done()
        ):
            self._redistribution_task.cancel()
        self._redistribution_task = asyncio.create_task(
            self._debounced_redistribute(children)
        )

    async def _debounced_redistribute(self, children: list[str]) -> None:
        try:
            await asyncio.sleep(self._DEBOUNCE_SEC)
            if not self._leader.is_leader():
                return
            # Re-fetch in case membership changed during the debounce
            current = await self._zk.loop.run_in_executor(
                None,
                lambda: self._zk.kazoo.get_children(self._members_path),
            )
            await self._do_redistribute(current)
        except asyncio.CancelledError:
            # Cancelled by a newer event — by design
            return

    async def on_become_leader(self, epoch: int) -> None:
        """Triggered by LeaderElection on acquisition. Redistribute immediately."""
        logger.info(
            "became_leader_redistributing",
            instance=self._instance_id,
            epoch=epoch,
        )
        members = await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.get_children(self._members_path)
        )
        await self._do_redistribute(members)

    async def _do_redistribute(self, instances: list[str]) -> None:
        """Compute and write the partition assignment for ``instances``.

        v6 P0-4: every exception path MUST schedule a retry or set
        ``_redistribute_unhealthy=True``. Silent stall is forbidden — a
        leader that holds election but never updates assignments is the
        worst-case failure mode (no signal to operators, no redistribution
        even if a follower joins/leaves).
        """
        try:
            processes = await self._eqp_repo.get_distinct_processes()
            assignments = self._compute_assignments(instances, processes)

            # 1. ensure_path EVERY assignment node BEFORE the Transaction.
            #    The Transaction's set_data is fail-fast — if the node is
            #    missing we get NoNodeError and the whole atomic write rolls
            #    back.
            for inst_id in assignments:
                path = f"{self._assignments_path}/{inst_id}"
                try:
                    await self._zk.loop.run_in_executor(
                        None, lambda p=path: self._zk.kazoo.ensure_path(p)
                    )
                except Exception as e:
                    logger.warning(
                        "ensure_assignment_path_failed",
                        path=path,
                        error=str(e),
                    )

            # 2. Atomic multi set_data — every instance gets the same epoch+ts
            transaction = self._zk.kazoo.transaction()
            timestamp = time.time()
            for inst_id, procs in assignments.items():
                data = json.dumps(
                    {
                        "processes": procs,
                        "leader_epoch": self._leader.epoch,
                        "assigned_at": timestamp,
                    }
                ).encode()
                transaction.set_data(
                    f"{self._assignments_path}/{inst_id}", data
                )
            results = await self._zk.loop.run_in_executor(
                None, transaction.commit
            )
            for r in results:
                if isinstance(r, Exception):
                    # Promote per-op failure to a top-level error so the
                    # outer retry path runs. We can't partially redistribute.
                    raise RuntimeError(
                        f"redistribute_transaction_op_failed: {r}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._redistribute_attempt += 1
            attempt = self._redistribute_attempt
            logger.error(
                "redistribute_failed_retrying",
                attempt=attempt,
                max_attempts=self._REDISTRIBUTE_MAX_ATTEMPTS,
                instances=instances,
                error=str(e),
                exc_info=True,
            )
            if attempt < self._REDISTRIBUTE_MAX_ATTEMPTS:
                # Cancel any pending retry from a prior failure — we
                # always honor the most recent attempt's backoff schedule.
                if (
                    self._redistribute_retry_task is not None
                    and not self._redistribute_retry_task.done()
                ):
                    self._redistribute_retry_task.cancel()
                self._redistribute_retry_task = asyncio.create_task(
                    self._retry_redistribute(instances, attempt)
                )
            else:
                logger.error(
                    "redistribute_giving_up",
                    attempts=attempt,
                    instances=instances,
                )
                self._redistribute_unhealthy = True
            return

        # Success path — clear retry state.
        if self._redistribute_attempt != 0 or self._redistribute_unhealthy:
            logger.info(
                "redistribute_recovered",
                prior_attempts=self._redistribute_attempt,
            )
        self._redistribute_attempt = 0
        self._redistribute_unhealthy = False
        if (
            self._redistribute_retry_task is not None
            and not self._redistribute_retry_task.done()
        ):
            self._redistribute_retry_task.cancel()

        # v6 H1: GC orphan assignment znodes from departed pods.
        # Rationale: MONITOR_INSTANCE_ID=metadata.name, so pod names change on
        # every rolling update. members/<pod> is ephemeral and disappears on
        # session close, but assignments/<pod> is persistent and would
        # accumulate forever without cleanup. Correctness is unaffected
        # (epoch+ts guard protects _apply_assignment), but ZK snapshot size
        # grows and operators eventually have to hand-clean. Failures here
        # are logged, NEVER escalated — the transaction already committed.
        await self._cleanup_orphan_assignments(instances)

    async def _cleanup_orphan_assignments(self, live_instances: list[str]) -> None:
        """Delete assignment znodes whose pod is no longer in ``live_instances``.

        Pure housekeeping. Runs only after a successful ``_do_redistribute``
        commit. Any failure is a warning — never marks the leader unhealthy
        and never raises back to the caller.
        """
        try:
            existing = await self._zk.loop.run_in_executor(
                None,
                lambda: self._zk.kazoo.get_children(self._assignments_path),
            )
        except Exception as e:
            logger.warning("orphan_cleanup_list_failed", error=str(e))
            return

        live = set(live_instances)
        orphans = [name for name in existing if name not in live]
        if not orphans:
            return

        for orphan in orphans:
            path = f"{self._assignments_path}/{orphan}"
            try:
                await self._zk.loop.run_in_executor(
                    None, lambda p=path: self._zk.kazoo.delete(p)
                )
                logger.info("orphan_assignment_deleted", orphan=orphan)
            except NoNodeError:
                # Concurrent delete (e.g. a racing reinit) — benign
                pass
            except Exception as e:
                logger.warning(
                    "orphan_assignment_delete_failed",
                    orphan=orphan,
                    error=str(e),
                )

    async def _retry_redistribute(
        self, instances: list[str], attempt: int
    ) -> None:
        """Sleep with exponential backoff then re-call ``_do_redistribute``.

        v6 P0-4: backoff is ``min(30, 2**attempt)`` seconds — 2, 4, 8, 16
        for attempts 1..4, capped at 30 thereafter. CancelledError is the
        normal case (a newer redistribute superseded us); we just exit.
        """
        delay = min(30.0, float(2 ** attempt))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._leader.is_leader():
            # Lost leadership while waiting — let the new leader handle it.
            return
        await self._do_redistribute(instances)

    @staticmethod
    def _compute_assignments(
        instances: list[str], processes: list[str]
    ) -> dict[str, list[str]]:
        instances_sorted = sorted(instances)
        result: dict[str, list[str]] = {i: [] for i in instances_sorted}
        if not instances_sorted:
            return result
        for idx, proc in enumerate(sorted(processes)):
            result[instances_sorted[idx % len(instances_sorted)]].append(proc)
        return result

    # ------------------------------------------------------------------
    # Apply received assignment
    # ------------------------------------------------------------------
    async def _apply_assignment(self, data: dict) -> None:
        """Apply an incoming assignment with stale defense.

        Newer (epoch, assigned_at) wins. Same-epoch + newer timestamp also
        progresses (so a leader can re-issue an assignment without bumping
        epoch first).
        """
        try:
            incoming_epoch = int(data["leader_epoch"])
            incoming_ts = float(data["assigned_at"])
            processes = list(data["processes"])
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("assignment_payload_malformed", error=str(e))
            return

        if incoming_epoch < self._known_epoch:
            return
        if (
            incoming_epoch == self._known_epoch
            and incoming_ts <= self._known_assigned_at
        ):
            return
        self._known_epoch = incoming_epoch
        self._known_assigned_at = incoming_ts
        self._assigned_processes = processes
        scheduler = self._get_scheduler()
        if scheduler is not None:
            await scheduler.reload()

    async def _refresh_assignment_from_zk(self) -> None:
        try:
            data, _ = await self._zk.loop.run_in_executor(
                None,
                lambda: self._zk.kazoo.get(self._my_assignment_path),
            )
            if data is None or len(data) == 0:
                return
            try:
                payload = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(
                    "refresh_assignment_invalid_payload", error=str(e)
                )
                return
            await self._apply_assignment(payload)
        except NoNodeError:
            logger.warning("refresh_assignment_no_node")
        except Exception as e:
            logger.warning("refresh_assignment_failed", error=str(e))
