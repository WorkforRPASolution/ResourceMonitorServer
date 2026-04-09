"""Zookeeper 3.5.5 integration — real kazoo round-trip.

핵심 검증:
  - ZKClient connect/close + kazoo-asyncio bridge
  - Transaction 원자성 (multi set_data)
  - ChildrenWatch 콜백이 실제 asyncio loop으로 전달됨
  - DataWatch 빈 노드(ensure_path 직후) 가드 — JSONDecodeError 없음 (G5)
  - get_server_version: 4lw 허용 시 버전 / 차단 시 "unknown" 폴백
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest
from kazoo.exceptions import NoNodeError
from pydantic import SecretStr

from src.config.settings import AppSettings
from src.distributed.zk_client import ZKClient

pytestmark = pytest.mark.integration


def _make_settings(zk_root: str) -> AppSettings:
    return AppSettings(
        zk_hosts="localhost:2181",
        zk_root_path=zk_root,
        zk_session_timeout=10,
        zk_sasl_password=SecretStr(""),
    )


# ----------------------------------------------------------------------
# ZKClient lifecycle
# ----------------------------------------------------------------------
async def test_zk_client_connect_and_ensure_root(ns):
    sub = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-lifecycle"
    settings = _make_settings(sub)
    client = ZKClient(settings)
    await client.connect()
    try:
        assert client.is_connected() is True
        # ensure_path 효과: root_path가 ZK에 생성되어야 함
        exists = await client.loop.run_in_executor(
            None, lambda: client.kazoo.exists(sub)
        )
        assert exists is not None
    finally:
        await client.close()
    assert client.is_connected() is False


# ----------------------------------------------------------------------
# Transaction atomicity — 모든 set_data가 원자적으로 적용되거나 전체 롤백
# ----------------------------------------------------------------------
async def test_transaction_atomic_multi_set(real_zk, fresh_zk_root):
    """Transaction.set_data가 여러 노드에 atomic하게 적용돼야 한다."""
    loop = asyncio.get_running_loop()
    a = f"{fresh_zk_root}/a"
    b = f"{fresh_zk_root}/b"
    c = f"{fresh_zk_root}/c"
    await loop.run_in_executor(None, lambda: real_zk.create(a, b"init"))
    await loop.run_in_executor(None, lambda: real_zk.create(b, b"init"))
    await loop.run_in_executor(None, lambda: real_zk.create(c, b"init"))

    tx = real_zk.transaction()
    tx.set_data(a, b"new-a")
    tx.set_data(b, b"new-b")
    tx.set_data(c, b"new-c")
    results = await loop.run_in_executor(None, tx.commit)

    for r in results:
        assert not isinstance(r, Exception), f"op failed: {r!r}"

    for path, expected in ((a, b"new-a"), (b, b"new-b"), (c, b"new-c")):
        data, _ = await loop.run_in_executor(None, lambda p=path: real_zk.get(p))
        assert data == expected


async def test_transaction_rollback_on_missing_node(real_zk, fresh_zk_root):
    """Transaction 안의 한 op가 NoNode면 나머지도 적용되면 안 된다."""
    loop = asyncio.get_running_loop()
    a = f"{fresh_zk_root}/exists"
    ghost = f"{fresh_zk_root}/ghost"
    await loop.run_in_executor(None, lambda: real_zk.create(a, b"init"))

    tx = real_zk.transaction()
    tx.set_data(a, b"should-not-persist")
    tx.set_data(ghost, b"phantom")   # NoNode
    results = await loop.run_in_executor(None, tx.commit)

    # 최소 하나는 exception, 원자성: a는 여전히 init
    assert any(isinstance(r, Exception) for r in results)
    data, _ = await loop.run_in_executor(None, lambda: real_zk.get(a))
    assert data == b"init"


# ----------------------------------------------------------------------
# ChildrenWatch — kazoo thread → asyncio loop bridge
# ----------------------------------------------------------------------
async def test_children_watch_fires_on_add(real_zk, fresh_zk_root):
    """자식 노드 생성 시 ChildrenWatch 콜백이 호출되고 값이 전달된다."""
    from kazoo.recipe.watchers import ChildrenWatch

    loop = asyncio.get_running_loop()
    parent = f"{fresh_zk_root}/parent"
    await loop.run_in_executor(None, lambda: real_zk.ensure_path(parent))

    captured: list[list[str]] = []
    done = threading.Event()

    def cb(children):
        captured.append(list(children))
        if len(children) >= 2:
            done.set()
        return True  # kazoo 재등록 유지

    ChildrenWatch(real_zk, parent, cb)

    # 초기 콜백은 빈 리스트로 1회 호출됨
    await loop.run_in_executor(
        None, lambda: real_zk.create(f"{parent}/child-1", b"", ephemeral=True)
    )
    await loop.run_in_executor(
        None, lambda: real_zk.create(f"{parent}/child-2", b"", ephemeral=True)
    )

    # 최대 5초 대기
    await loop.run_in_executor(None, lambda: done.wait(timeout=5.0))
    assert done.is_set(), f"watch did not reach 2 children; captured={captured}"
    last = captured[-1]
    assert sorted(last) == ["child-1", "child-2"]


# ----------------------------------------------------------------------
# DataWatch 빈 노드 가드 — G5 회귀 가드
# ----------------------------------------------------------------------
async def test_datawatch_empty_node_guard(real_zk, fresh_zk_root):
    """``ensure_path`` 로 만든 빈 노드에 DataWatch가 걸렸을 때 json.loads(b'')
    로 폭사하지 않고 가드를 통과해야 한다 (PartitionManager의 실제 버그 케이스).
    """
    from kazoo.recipe.watchers import DataWatch

    loop = asyncio.get_running_loop()
    path = f"{fresh_zk_root}/empty-node"
    await loop.run_in_executor(None, lambda: real_zk.ensure_path(path))

    fired = threading.Event()
    errors: list[Exception] = []

    # 실제 PartitionManager의 handler 로직을 그대로 복제
    def cb(data, stat, event):
        try:
            if data is None or len(data) == 0:
                fired.set()
                return
            # 이 경로는 비어있지 않을 때만 도달
            json.loads(data.decode("utf-8"))
            fired.set()
        except Exception as e:
            errors.append(e)
            fired.set()

    DataWatch(real_zk, path, cb)

    await loop.run_in_executor(None, lambda: fired.wait(timeout=5.0))
    assert fired.is_set()
    assert not errors, f"empty node caused handler error: {errors}"


async def test_datawatch_non_empty_node_parses(real_zk, fresh_zk_root):
    """빈 가드 통과 후 실제 payload가 들어오면 json.loads가 정상 동작."""
    from kazoo.recipe.watchers import DataWatch

    loop = asyncio.get_running_loop()
    path = f"{fresh_zk_root}/payload-node"
    payload = json.dumps({"processes": ["a", "b"], "epoch": 1}).encode()
    await loop.run_in_executor(None, lambda: real_zk.create(path, payload))

    captured: list[dict] = []
    fired = threading.Event()

    def cb(data, stat, event):
        if data is None or len(data) == 0:
            return
        captured.append(json.loads(data.decode("utf-8")))
        fired.set()

    DataWatch(real_zk, path, cb)

    await loop.run_in_executor(None, lambda: fired.wait(timeout=5.0))
    assert fired.is_set()
    assert captured[0]["processes"] == ["a", "b"]
    assert captured[0]["epoch"] == 1


# ----------------------------------------------------------------------
# get_server_version — 4lw whitelist enabled in our compose
# ----------------------------------------------------------------------
async def test_get_server_version_with_4lw_whitelist(ns):
    """ARS compose의 ZOO_4LW_COMMANDS_WHITELIST=stat,... 이 적용돼 있으므로
    stat 명령이 동작하고 3.5.x 버전 문자열이 반환돼야 한다.
    """
    sub = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-version"
    settings = _make_settings(sub)
    client = ZKClient(settings)
    await client.connect()
    try:
        version = await client.get_server_version()
        # "3.5.5-390fe37ea45dee01bf87dc1c042b5e3dcce88653" 같은 형태
        assert "3.5" in version, f"expected 3.5.x, got {version!r}"
    finally:
        await client.close()
