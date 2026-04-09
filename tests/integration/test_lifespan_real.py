"""FastAPI lifespan integration — 11-phase startup against real infra.

이 테스트는 `src.main:app`을 실제로 띄운다. 진짜로 ES/Mongo/Redis/ZK에
연결되고, in-process email mock 서버를 Akka 대신으로 쓴다. 각 phase가
동작하는지를 /healthz/ready 와 /admin/status로 확인한다.

namespace 격리:
  - env 변수로 `MONITOR_*` 를 주입하여 run_id 기반 prefix 적용
  - MONGO_DB, REDIS_KEY_PREFIX, ZK_ROOT_PATH 모두 test 전용
  - 테스트 종료 시 startup에서 생성한 파티션/members 정리는 session finalizer가 담당
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.config.settings import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
async def live_app(ns, mock_email_server, monkeypatch):
    """실제 infra에 연결된 FastAPI app을 lifespan 내에서 제공.

    monkeypatch로 MONITOR_* env를 설정 후 get_settings 캐시 클리어하여
    lifespan이 우리 test settings를 읽게 한다.
    """
    sub_zk = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-life"
    monkeypatch.setenv("MONITOR_ES_HOSTS", "http://localhost:9200")
    monkeypatch.setenv("MONITOR_ES_USERNAME", "")
    monkeypatch.setenv("MONITOR_ES_PASSWORD", "")
    monkeypatch.setenv("MONITOR_MONGO_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONITOR_MONGO_DB", f"{ns.mongo_db}_lifespan")
    monkeypatch.setenv("MONITOR_ZK_HOSTS", "localhost:2181")
    monkeypatch.setenv("MONITOR_ZK_ROOT_PATH", sub_zk)
    monkeypatch.setenv("MONITOR_ZK_SESSION_TIMEOUT", "10")
    monkeypatch.setenv("MONITOR_REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("MONITOR_REDIS_PASSWORD", "")
    monkeypatch.setenv("MONITOR_REDIS_KEY_PREFIX", f"{ns.redis_prefix}_life")
    monkeypatch.setenv("MONITOR_EMAIL_API_URL", mock_email_server["url"])
    monkeypatch.setenv("MONITOR_EMAIL_API_TIMEOUT", "5")
    monkeypatch.setenv("MONITOR_INSTANCE_ID", f"life-{uuid.uuid4().hex[:6]}")
    monkeypatch.setenv("MONITOR_LOG_FORMAT", "console")

    get_settings.cache_clear()
    from src.main import app

    async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60):
        yield app
    get_settings.cache_clear()


# ----------------------------------------------------------------------
# 1. Full 11-phase startup + /healthz/ready
# ----------------------------------------------------------------------
async def test_lifespan_startup_all_phases(live_app):
    """모든 11개 phase가 실패 없이 시동되고 ready가 OK여야 한다."""
    transport = httpx.ASGITransport(app=live_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # /healthz/live — 인프라 무관, 항상 200
        r = await client.get("/healthz/live")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

        # /healthz/ready — 5개 인프라 ping 모두 성공
        r = await client.get("/healthz/ready")
        assert r.status_code == 200, f"ready failed: {r.text}"
        body = r.json()
        assert body["status"] == "ready"
        checks = body["checks"]
        assert checks["elasticsearch"] is True
        assert checks["mongodb"] is True
        assert checks["redis"] is True
        assert checks["zookeeper"] is True
        assert checks["email_api"] is True
        assert body["scheduler_running"] is True
        assert body["version"] == "0.1.0"


# ----------------------------------------------------------------------
# 2. /admin/status — leader_epoch + assigned_processes + jobs
# ----------------------------------------------------------------------
async def test_admin_status_shows_leader_state(live_app):
    """single instance로 띄우면 결국 is_leader=True가 돼야 하고,
    epoch + assigned_processes 모두 노출돼야 한다."""
    import asyncio as _asyncio

    transport = httpx.ASGITransport(app=live_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # election + watch가 안정화될 때까지 잠깐 대기 (최대 15s)
        deadline = _asyncio.get_event_loop().time() + 15.0
        body = None
        while _asyncio.get_event_loop().time() < deadline:
            r = await client.get("/admin/status")
            assert r.status_code == 200
            body = r.json()
            if body["is_leader"]:
                break
            await _asyncio.sleep(0.5)
        assert body is not None
        assert body["is_leader"] is True, f"did not become leader: {body}"
        assert body["leader_epoch"] is not None and body["leader_epoch"] >= 1
        assert body["scheduler_running"] is True
        assert body["zk_connected"] is True
        assert body["instance_id"].startswith("life-")
        # zk_server_version은 4lw 허용 상태면 "3.5.x", 아니면 "unknown"
        assert "3.5" in body["zk_server_version"] or body["zk_server_version"] == "unknown"


# ----------------------------------------------------------------------
# 3. /metrics — Prometheus exposition
# ----------------------------------------------------------------------
async def test_metrics_endpoint(live_app):
    transport = httpx.ASGITransport(app=live_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/metrics")
        assert r.status_code == 200
        body = r.text
        # Prometheus text format should contain HELP + TYPE lines
        assert "# HELP" in body
        assert "# TYPE" in body


# ----------------------------------------------------------------------
# 4. Debug Read-Only lifespan — startup must succeed without ZK
# ----------------------------------------------------------------------
@pytest.fixture
async def debug_app(ns, mock_email_server, monkeypatch):
    """Boot the app in debug_read_only mode. ZK is intentionally NOT connected."""
    monkeypatch.setenv("MONITOR_ES_HOSTS", "http://localhost:9200")
    monkeypatch.setenv("MONITOR_ES_USERNAME", "")
    monkeypatch.setenv("MONITOR_ES_PASSWORD", "")
    monkeypatch.setenv("MONITOR_MONGO_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONITOR_MONGO_DB", f"{ns.mongo_db}_debug")
    # ZK env is still set (so if the debug guard breaks, the test would fail
    # trying to connect). The guard must override this.
    monkeypatch.setenv("MONITOR_ZK_HOSTS", "localhost:2181")
    monkeypatch.setenv("MONITOR_ZK_ROOT_PATH", f"{ns.zk_root}-debug")
    monkeypatch.setenv("MONITOR_ZK_SESSION_TIMEOUT", "10")
    monkeypatch.setenv("MONITOR_REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("MONITOR_REDIS_PASSWORD", "")
    monkeypatch.setenv("MONITOR_REDIS_KEY_PREFIX", f"{ns.redis_prefix}_debug")
    monkeypatch.setenv("MONITOR_EMAIL_API_URL", mock_email_server["url"])
    monkeypatch.setenv("MONITOR_EMAIL_API_TIMEOUT", "5")
    monkeypatch.setenv("MONITOR_INSTANCE_ID", f"debug-{uuid.uuid4().hex[:6]}")
    monkeypatch.setenv("MONITOR_LOG_FORMAT", "console")
    # ★ Debug flag
    monkeypatch.setenv("MONITOR_DEBUG_READ_ONLY", "true")

    get_settings.cache_clear()
    from src.main import app

    async with LifespanManager(app, startup_timeout=30, shutdown_timeout=30):
        yield app
    get_settings.cache_clear()


async def test_debug_lifespan_boots_without_zk(debug_app):
    """★ Debug Read-Only: lifespan must complete even though ZK is intentionally
    not connected. /healthz/live and /metrics should still work — they don't
    depend on ZK or partition manager."""
    transport = httpx.ASGITransport(app=debug_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz/live")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

        r = await client.get("/metrics")
        assert r.status_code == 200
        assert "# HELP" in r.text


async def test_debug_lifespan_sets_no_zk_state(debug_app):
    """app.state.zk_client is None, partition_manager/leader_election NOT set."""
    assert debug_app.state.zk_client is None
    assert not hasattr(debug_app.state, "partition_manager")
    assert not hasattr(debug_app.state, "leader_election")
    # But scheduler IS started
    assert debug_app.state.scheduler.is_running() is True


async def test_debug_lifespan_does_not_create_profile_index(debug_app, real_mongo, ns):
    """★ Regression guard: in debug mode init_repos must NOT create the
    uniq_scope index on the prod-adjacent collection. We verify by checking
    that the namespace-isolated test DB does NOT have the index."""
    db = real_mongo[f"{ns.mongo_db}_debug"]
    # The test DB may not even have the collection since init_repos didn't
    # create the index — collection won't exist at all.
    existing = await db.list_collection_names()
    from src.config.constants import COLL_PROFILE
    if COLL_PROFILE in existing:
        # If the collection exists (e.g. created by another test run), at
        # least the uniq_scope index must NOT have been created by this
        # debug boot.
        indexes = await db[COLL_PROFILE].index_information()
        assert "uniq_scope" not in indexes, (
            f"debug mode created uniq_scope index: {list(indexes.keys())}"
        )
    # else: collection doesn't exist — perfect, init_repos was fully skipped


async def test_debug_readyz_exposes_debug_flag(debug_app):
    """/healthz/ready in debug mode must:
    - include debug_read_only=true
    - mark zookeeper as "skipped_debug"
    - still return 200 (ES/Mongo/Redis/Email still healthy)
    """
    transport = httpx.ASGITransport(app=debug_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz/ready")
        assert r.status_code == 200, f"ready failed: {r.text}"
        body = r.json()
        assert body["debug_read_only"] is True
        assert body["checks"]["zookeeper"] == "skipped_debug"
        assert body["checks"]["elasticsearch"] is True
        assert body["checks"]["mongodb"] is True
        assert body["checks"]["redis"] is True
        # is_leader is None in debug mode (partition manager not initialized)
        assert body["is_leader"] is None
        assert body["scheduler_running"] is True


async def test_debug_admin_status_exposes_debug_flag(debug_app):
    """/admin/status in debug mode shows debug_read_only=true and None
    for all distributed fields."""
    transport = httpx.ASGITransport(app=debug_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/admin/status")
        assert r.status_code == 200
        body = r.json()
        assert body["debug_read_only"] is True
        assert body["is_leader"] is None
        assert body["leader_epoch"] is None
        assert body["assigned_processes"] is None
        assert body["zk_connected"] is None
        assert body["zk_server_version"] is None
        # But scheduler state IS reported
        assert body["scheduler_running"] is True
        assert body["instance_id"].startswith("debug-")
