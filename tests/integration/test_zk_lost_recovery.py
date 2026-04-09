"""ZK LOST recovery — ★★★ 핵심 시나리오 ★★★

v4 critical fix G4 회귀 가드:
  옛 Election 객체는 죽은 세션에 바운드되어 재사용 불가.
  `restart_after_loss()` 는 새 Election 객체를 만들어 fire-and-forget으로
  다시 스케줄해야 한다.

검증 시나리오:
  1. LeaderElection.start() → leader 됨 → epoch=1
  2. 세션 강제 만료 (`ars-zookeeper` 컨테이너 stop/start OR `client._session`
     close) → 옛 session 종료
  3. `restart_after_loss()` 호출 → 새 Election → 다시 leader 됨 → epoch=2

+ watch_epoch idempotency (G6): `_register_watches()` 연속 호출 후 옛 콜백이
  발화해도 state mutation이 없어야 함 (PartitionManager 레벨 검증은 Step
  10의 test_partition_real.py에 둠 — 여기서는 단순 회귀 가드만).
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest
from pydantic import SecretStr

from src.config.settings import AppSettings
from src.distributed.leader_election import LeaderElection
from src.distributed.zk_client import ZKClient

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ZK_CONTAINER = "ars-zookeeper"


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


async def _wait_zk_ready(timeout: float = 20.0) -> None:
    """ZK가 ruok에 응답할 때까지 poll."""
    from kazoo.client import KazooClient

    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            k = KazooClient(hosts="localhost:2181", timeout=3.0)
            try:
                k.start(timeout=3)
                if k.connected:
                    return
            finally:
                try:
                    k.stop()
                except Exception:
                    pass
                try:
                    k.close()
                except Exception:
                    pass
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.5)
    raise RuntimeError(f"ZK not ready within {timeout}s: {last_err!r}")


@pytest.fixture
async def zk_lifecycle():
    """테스트 전/후 ZK 살아있음 보장."""
    await _wait_zk_ready()
    yield
    try:
        _docker("start", ZK_CONTAINER)
    except subprocess.CalledProcessError:
        pass
    await _wait_zk_ready()


async def _make_zk(ns, suffix: str) -> ZKClient:
    sub = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-{suffix}"
    settings = AppSettings(
        zk_hosts="localhost:2181",
        zk_root_path=sub,
        zk_session_timeout=10,
        zk_sasl_password=SecretStr(""),
    )
    client = ZKClient(settings)
    await client.connect()
    return client


async def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Busy-wait with small sleep until predicate() returns truthy."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


# ----------------------------------------------------------------------
# 1. Basic leader acquisition (sanity, then LOST recovery)
# ----------------------------------------------------------------------
async def test_leader_election_basic_acquire(ns, zk_lifecycle):
    """단일 인스턴스가 leader가 되고 epoch=1이 persist 되어야 함."""
    zk = await _make_zk(ns, "basic")
    election = LeaderElection(zk, instance_id="inst-1")
    try:
        await election.start()
        got_leader = await _wait_until(election.is_leader, timeout=5.0)
        assert got_leader, "did not become leader within 5s"
        assert election.epoch == 1
        # epoch 노드가 실제 ZK에 persist 됐는지
        loop = asyncio.get_running_loop()
        data, _ = await loop.run_in_executor(
            None, lambda: zk.kazoo.get(f"{zk.root_path}/leader-epoch")
        )
        assert int(data.decode()) == 1
    finally:
        await election.stop()
        await zk.close()


# ----------------------------------------------------------------------
# 2. LOST → restart_after_loss → re-acquired with epoch+1 ★★★
# ----------------------------------------------------------------------
async def test_leader_restart_after_loss_reacquires(ns, zk_lifecycle):
    """★★★ G4 회귀 가드:
    리더가 된 상태에서 ZK 세션이 강제 만료되면 `restart_after_loss()`를
    호출해야 새 Election 객체로 다시 리더를 재취득한다. epoch는 증가해야 함.
    """
    zk = await _make_zk(ns, "lost")
    election = LeaderElection(zk, instance_id="inst-lost")
    try:
        await election.start()
        assert await _wait_until(election.is_leader, timeout=5.0)
        assert election.epoch == 1

        # 세션 강제 만료 — kazoo 내부 session expire 트리거
        # 방법: 기존 연결을 강제로 닫고 재연결
        loop = asyncio.get_running_loop()
        # kazoo.client._connection._close() 식 private API는 불안정.
        # 대신 KazooClient.stop() + start()로 새 세션 생성 (이전 세션은 expire)
        # 단, election._election 객체는 옛 세션에 바인딩된 채로 남아 있음.
        old_session_id = zk.kazoo.client_id
        await loop.run_in_executor(None, zk.kazoo.stop)
        # 옛 election.run()은 ConnectionClosedError로 return되어 future 완료
        # 재연결
        await loop.run_in_executor(None, zk.kazoo.start)
        await loop.run_in_executor(
            None, lambda: zk.kazoo.ensure_path(zk.root_path)
        )
        new_session_id = zk.kazoo.client_id
        assert old_session_id != new_session_id, "session did not rotate"

        # restart_after_loss() 호출 — G4 핵심
        await election.restart_after_loss()
        assert await _wait_until(election.is_leader, timeout=10.0), \
            "did not re-acquire leadership after restart_after_loss"
        # epoch는 반드시 증가
        assert election.epoch >= 2, f"epoch did not increment: {election.epoch}"
    finally:
        try:
            await election.stop()
        except Exception:
            pass
        await zk.close()


# ----------------------------------------------------------------------
# 3. Two elections racing — only one leader at a time
# ----------------------------------------------------------------------
async def test_two_elections_exclusive_leadership(ns, zk_lifecycle):
    """같은 경로에 두 LeaderElection이 참여하면 동시에 한 명만 leader여야 함."""
    zk_a = await _make_zk(ns, "race-a")
    # 같은 root path를 공유해야 경쟁. 두 번째는 같은 root로 만든다.
    settings_b = AppSettings(
        zk_hosts="localhost:2181",
        zk_root_path=zk_a.root_path,
        zk_session_timeout=10,
        zk_sasl_password=SecretStr(""),
    )
    zk_b = ZKClient(settings_b)
    await zk_b.connect()

    e_a = LeaderElection(zk_a, instance_id="inst-A")
    e_b = LeaderElection(zk_b, instance_id="inst-B")
    try:
        await e_a.start()
        # A가 리더가 될 시간 준다
        assert await _wait_until(e_a.is_leader, timeout=5.0)

        await e_b.start()
        # B는 대기 상태라야 함 (A가 리더 보유 중)
        # 1초 정도 봐주고 확인
        await asyncio.sleep(1.0)
        assert e_a.is_leader() is True
        assert e_b.is_leader() is False

        a_epoch = e_a.epoch

        # A가 사임 → B가 올라와야 함
        await e_a.stop()
        assert await _wait_until(e_b.is_leader, timeout=10.0), \
            "B did not take over after A stepped down"
        # B의 epoch는 A의 epoch보다 커야 함 (persistent)
        assert e_b.epoch > a_epoch
    finally:
        try:
            await e_b.stop()
        except Exception:
            pass
        try:
            await e_a.stop()
        except Exception:
            pass
        await zk_a.close()
        await zk_b.close()
