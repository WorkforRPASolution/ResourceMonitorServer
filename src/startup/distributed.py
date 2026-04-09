"""Build the distributed-coordination layer (leader, lock, partition manager)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.cache.cooldown import AlertCooldownManager
from src.config.settings import AppSettings
from src.distributed.leader_election import LeaderElection
from src.distributed.lock import ZKAnalysisLock
from src.distributed.partition_manager import PartitionManager
from src.startup.infra import InfraContext
from src.startup.repos import RepositoryContext


@dataclass
class DistributedContext:
    leader_election: LeaderElection
    zk_lock: ZKAnalysisLock
    partition_mgr: PartitionManager
    cooldown_mgr: AlertCooldownManager


async def init_distributed(
    infra: InfraContext,
    repos: RepositoryContext,
    instance_id: str,
    scheduler_provider: Callable[[], Any],
    settings: AppSettings | None = None,
) -> DistributedContext:
    """Wire the four distributed components.

    ``scheduler_provider`` is a callable rather than the scheduler itself
    because the scheduler is built AFTER the partition manager (it depends
    on the lock and the partition manager). The callable closes over a
    ``ContainerStateProvider``-style holder so the partition manager can
    reach the scheduler that doesn't exist yet at construction time.
    """
    if infra.zk is None:
        raise RuntimeError("init_distributed requires a connected ZKClient")
    if infra.redis is None:
        raise RuntimeError("init_distributed requires a connected RedisClient")

    leader = LeaderElection(infra.zk, instance_id)
    zk_lock = ZKAnalysisLock(infra.zk)
    partition_mgr = PartitionManager(
        zk_client=infra.zk,
        leader_election=leader,
        eqp_repo=repos.eqp_info_repo,
        instance_id=instance_id,
        scheduler_provider=scheduler_provider,
    )
    cooldown_mgr = AlertCooldownManager(infra.redis, settings=settings)
    return DistributedContext(
        leader_election=leader,
        zk_lock=zk_lock,
        partition_mgr=partition_mgr,
        cooldown_mgr=cooldown_mgr,
    )
