"""PartitionManager integration — real ZK with stale defense + round-robin.

Unit test는 ZK을 mock하므로 Transaction/ChildrenWatch/ensure_path의 실제 동작
경로가 안 보인다. 본 파일은 실제 OrbStack ZK 3.5.5에 대고 다음을 검증한다:

  1. 라운드로빈 분배 (정적 계산)
  2. leader가 됐을 때 onBecomeLeader → Transaction으로 assignment 기록
  3. 두 인스턴스가 같은 root path를 공유하면 process가 절반씩 분배
  4. stale assignment(낮은 epoch / 같은 epoch 옛 timestamp) 거부 (epoch+ts 가드)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from pydantic import SecretStr

from src.config.settings import AppSettings
from src.distributed.leader_election import LeaderElection
from src.distributed.partition_manager import PartitionManager
from src.distributed.zk_client import ZKClient

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ----------------------------------------------------------------------
# Fake EQP repo — 테스트가 원하는 process 리스트를 반환
# ----------------------------------------------------------------------
class FakeEqpRepo:
    def __init__(self, processes: list[str]) -> None:
        self._procs = processes

    async def get_distinct_processes(self) -> list[str]:
        return list(self._procs)

    async def count_active_by_process(self, process: str) -> int:
        return 1 if process in self._procs else 0


async def _make_zk_ctx(ns, suffix: str, root: str | None = None):
    sub = root or f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-{suffix}"
    settings = AppSettings(
        zk_hosts="localhost:2181",
        zk_root_path=sub,
        zk_session_timeout=10,
        zk_sasl_password=SecretStr(""),
    )
    zk = ZKClient(settings)
    await zk.connect()
    return zk


async def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


# ----------------------------------------------------------------------
# 1. Round-robin pure function
# ----------------------------------------------------------------------
def test_compute_assignments_round_robin():
    """라운드로빈은 정렬된 instances/processes를 기준으로 idx % N."""
    instances = ["inst-2", "inst-1"]  # 정렬 전
    processes = ["P4", "P1", "P3", "P2"]  # 정렬 전
    result = PartitionManager._compute_assignments(instances, processes)
    # sorted: inst-1, inst-2 / P1, P2, P3, P4
    # idx 0 P1 → inst-1, idx 1 P2 → inst-2, idx 2 P3 → inst-1, idx 3 P4 → inst-2
    assert result == {
        "inst-1": ["P1", "P3"],
        "inst-2": ["P2", "P4"],
    }


def test_compute_assignments_empty_instances():
    assert PartitionManager._compute_assignments([], ["P1", "P2"]) == {}


def test_compute_assignments_more_instances_than_processes():
    result = PartitionManager._compute_assignments(
        ["a", "b", "c"], ["P1"]
    )
    assert result == {"a": ["P1"], "b": [], "c": []}


# ----------------------------------------------------------------------
# 2. Two-instance partition manager — leader distributes processes
# ----------------------------------------------------------------------
async def test_two_instance_partition_distribution(ns):
    """두 인스턴스가 같은 root에서 동작, 리더가 4 process를 2/2로 분배."""
    zk_a = await _make_zk_ctx(ns, "part-a")
    zk_b = await _make_zk_ctx(ns, "part-b-should-use-a-root", root=zk_a.root_path)

    le_a = LeaderElection(zk_a, instance_id="inst-A")
    le_b = LeaderElection(zk_b, instance_id="inst-B")

    eqp_repo = FakeEqpRepo(["P1", "P2", "P3", "P4"])

    pm_a = PartitionManager(
        zk_client=zk_a,
        leader_election=le_a,
        eqp_repo=eqp_repo,
        instance_id="inst-A",
        scheduler_provider=lambda: None,
    )
    pm_b = PartitionManager(
        zk_client=zk_b,
        leader_election=le_b,
        eqp_repo=eqp_repo,
        instance_id="inst-B",
        scheduler_provider=lambda: None,
    )

    try:
        # 두 PM 모두 start (members 등록 + watch)
        await pm_a.start()
        await pm_b.start()
        # 각자의 leader election 시작
        await le_a.start()
        await le_b.start()

        # 누가 leader든 상관없이 분배가 이뤄지면 OK. 초기 onBecomeLeader →
        # _do_redistribute 이후 assignment_watch가 fire해서 _apply_assignment.
        def _both_have_procs() -> bool:
            a_count = len(pm_a.get_my_processes())
            b_count = len(pm_b.get_my_processes())
            return a_count >= 1 and b_count >= 1 and (a_count + b_count) == 4

        ok = await _wait_until(_both_have_procs, timeout=15.0)
        assert ok, (
            f"distribution did not settle; A={pm_a.get_my_processes()}, "
            f"B={pm_b.get_my_processes()}"
        )
        combined = sorted(pm_a.get_my_processes() + pm_b.get_my_processes())
        assert combined == ["P1", "P2", "P3", "P4"]
        # 균등 분배: 2/2
        assert abs(len(pm_a.get_my_processes()) - len(pm_b.get_my_processes())) <= 0
    finally:
        try:
            await le_b.stop()
        except Exception:
            pass
        try:
            await le_a.stop()
        except Exception:
            pass
        try:
            await pm_b.stop()
        except Exception:
            pass
        try:
            await pm_a.stop()
        except Exception:
            pass
        await zk_b.close()
        await zk_a.close()


# ----------------------------------------------------------------------
# 3. Stale assignment rejection — epoch + ts 가드
# ----------------------------------------------------------------------
async def test_apply_assignment_stale_epoch_rejected(ns):
    """낮은 epoch의 assignment는 무시되고 높은 epoch는 적용돼야 함."""
    zk = await _make_zk_ctx(ns, "stale")
    le = LeaderElection(zk, instance_id="inst-stale")
    eqp_repo = FakeEqpRepo(["P1"])
    pm = PartitionManager(
        zk_client=zk,
        leader_election=le,
        eqp_repo=eqp_repo,
        instance_id="inst-stale",
        scheduler_provider=lambda: None,
    )

    try:
        # _known_epoch를 직접 5로 설정
        pm._known_epoch = 5
        pm._known_assigned_at = 1000.0

        # 낮은 epoch → 거부
        await pm._apply_assignment({
            "processes": ["STALE_PROC"],
            "leader_epoch": 3,
            "assigned_at": 2000.0,
        })
        assert pm.get_my_processes() == []

        # 같은 epoch + 옛 timestamp → 거부
        await pm._apply_assignment({
            "processes": ["OLD_TS"],
            "leader_epoch": 5,
            "assigned_at": 999.0,
        })
        assert pm.get_my_processes() == []

        # 같은 epoch + 새 timestamp → 허용
        await pm._apply_assignment({
            "processes": ["NEW_TS"],
            "leader_epoch": 5,
            "assigned_at": 1500.0,
        })
        assert pm.get_my_processes() == ["NEW_TS"]

        # 더 높은 epoch → 허용
        await pm._apply_assignment({
            "processes": ["FRESH"],
            "leader_epoch": 6,
            "assigned_at": 100.0,  # 낮은 ts도 OK
        })
        assert pm.get_my_processes() == ["FRESH"]
    finally:
        await pm.stop()
        await zk.close()


# ----------------------------------------------------------------------
# 4. _apply_assignment + 실제 JSON 노드에서 refresh
# ----------------------------------------------------------------------
async def test_refresh_assignment_from_zk_real(ns):
    """실제 ZK 노드에 payload를 심어놓고 refresh_assignment_from_zk가
    읽어서 적용하는지 확인 (startup 경로)."""
    zk = await _make_zk_ctx(ns, "refresh")
    le = LeaderElection(zk, instance_id="inst-refresh")
    eqp_repo = FakeEqpRepo([])
    pm = PartitionManager(
        zk_client=zk,
        leader_election=le,
        eqp_repo=eqp_repo,
        instance_id="inst-refresh",
        scheduler_provider=lambda: None,
    )
    try:
        # 수동으로 assignments 경로에 유효한 payload 작성
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: zk.kazoo.ensure_path(pm._my_assignment_path)
        )
        payload = json.dumps({
            "processes": ["R1", "R2"],
            "leader_epoch": 7,
            "assigned_at": time.time(),
        }).encode()
        await loop.run_in_executor(
            None, lambda: zk.kazoo.set(pm._my_assignment_path, payload)
        )

        await pm._refresh_assignment_from_zk()
        assert sorted(pm.get_my_processes()) == ["R1", "R2"]
        assert pm._known_epoch == 7
    finally:
        await pm.stop()
        await zk.close()
