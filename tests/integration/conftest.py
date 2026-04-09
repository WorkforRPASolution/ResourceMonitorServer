"""Integration test fixtures — OrbStack 기반.

전제: `make dev-up` 으로 다음 4개 서비스가 떠 있어야 함:
  - ES 7.11.2    (localhost:9200)    docker.elastic.co/elasticsearch/elasticsearch:7.11.2
  - ZK 3.5.5     (localhost:2181)    zookeeper:3.5.5
  - Redis 5.0.6  (localhost:6379)    redis:5.0.6-alpine
  - Mongo 4.4.30 (localhost:27017)   mongo:4.4.30 (기존 컨테이너)

격리 전략:
  - session 시작 시 UUID 기반 `run_id` 생성
  - 모든 Mongo/Redis/ES/ZK 경로에 run_id prefix
  - pytest-session 종료 직전 단일 cleanup으로 잔재 0건 보장

Scope 결정:
  - async client(motor/redis/es)는 **function scope**. motor는 생성 시 loop에
    바운드되므로 session fixture를 function test가 쓰면 "attached to a
    different loop" 에러. aiohttp/httpx도 동일 함정.
  - `real_zk` 는 sync KazooClient라 scope 무관 → session scope 유지
  - `run_id`, `ns`, `mock_email_server` 는 단순하거나 per-test 필요
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from aiohttp import web
from elasticsearch import AsyncElasticsearch
from kazoo.client import KazooClient
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

# ----- 환경 변수 (개발자 PC default) --------------------------------------
ES_HOSTS = os.getenv("TEST_ES_HOSTS", "http://localhost:9200")
MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")  # DB 15 = 테스트 전용
ZK_HOSTS = os.getenv("TEST_ZK_HOSTS", "localhost:2181")


# ----- session 스코프: 한 번의 pytest run 동안 유일한 ID ------------------
@pytest.fixture(scope="session")
def run_id() -> str:
    return uuid.uuid4().hex[:8]


class _Namespace:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.mongo_db = f"EARS_test_{run_id}"
        self.redis_prefix = f"RESOURCE_ALERT_test_{run_id}"
        self.es_index_prefix = f"test_{run_id}_"
        self.zk_root = f"/resource-monitor-test-{run_id}"

    def __repr__(self) -> str:
        return f"NS(run_id={self.run_id})"


@pytest.fixture(scope="session")
def ns(run_id: str) -> _Namespace:
    return _Namespace(run_id)


# ----- function 스코프: 실 인프라 async 클라이언트 ------------------------
# 왜 function? motor/redis/httpx는 생성 시 asyncio loop에 바운드된다.
# session scope로 만들면 function-scoped test loop과 다른 loop에서 생성된
# Future를 쓰게 돼 "got Future attached to a different loop" 에러 발생.
@pytest_asyncio.fixture
async def real_es() -> AsyncIterator[AsyncElasticsearch]:
    client = AsyncElasticsearch(hosts=[ES_HOSTS], timeout=10)
    try:
        ok = await client.ping()
    except Exception:
        ok = False
    if not ok:
        await client.close()
        pytest.skip(f"Elasticsearch not reachable at {ES_HOSTS} — run `make dev-up`")
    yield client
    await client.close()


@pytest_asyncio.fixture
async def real_mongo() -> AsyncIterator[AsyncIOMotorClient]:
    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        await client.admin.command("ping")
    except Exception:
        client.close()
        pytest.skip(f"MongoDB not reachable at {MONGO_URI} — start mongodb-44 in OrbStack")
    yield client
    client.close()  # motor: sync


@pytest_asyncio.fixture
async def real_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip(f"Redis not reachable at {REDIS_URL} — run `make dev-up`")
    yield client
    await client.aclose()


# ----- session 스코프: sync ZK 클라이언트 (loop 무관) --------------------
@pytest.fixture(scope="session")
def real_zk() -> Iterator[KazooClient]:
    client = KazooClient(hosts=ZK_HOSTS, timeout=10.0)
    try:
        client.start(timeout=5)
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        pytest.skip(f"Zookeeper not reachable at {ZK_HOSTS} — run `make dev-up`")
    yield client
    try:
        client.stop()
    finally:
        client.close()


# ----- function 스코프: in-process Email mock 서버 -----------------------
@pytest_asyncio.fixture
async def mock_email_server():
    """aiohttp test server — Akka /EmailNotify endpoint의 제어 가능한 mock.

    기본 동작은 성공(`{"result":"success","message":"send ok"}`) 반환.
    테스트가 필요에 따라 `state["next_response"]` 와 `state["next_status"]` 를
    변경해 실패/지연 시나리오를 만들 수 있다.

    `state["received"]` 에는 모든 요청이 기록됨 — 어서션에 사용.
    """
    state: dict = {
        "received": [],
        "next_response": {"result": "success", "message": "send ok"},
        "next_status": 200,
        "delay_sec": 0.0,
    }

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        state["received"].append(body)
        if state["delay_sec"] > 0:
            await asyncio.sleep(state["delay_sec"])
        return web.json_response(state["next_response"], status=state["next_status"])

    app = web.Application()
    app.router.add_post("/EmailNotify", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    state["url"] = f"http://127.0.0.1:{port}/EmailNotify"
    yield state
    await runner.cleanup()


# ----- function 스코프: 각 테스트가 쓰기 전에 자기 namespace 준비 --------
@pytest_asyncio.fixture
async def fresh_zk_root(real_zk: KazooClient, ns: _Namespace) -> AsyncIterator[str]:
    """각 테스트가 자기만의 하위 경로를 갖도록. 끝나면 해당 경로만 삭제."""
    sub = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: real_zk.ensure_path(sub))
    yield sub
    try:
        await loop.run_in_executor(
            None, lambda: real_zk.delete(sub, recursive=True) if real_zk.exists(sub) else None
        )
    except Exception:
        pass


@pytest_asyncio.fixture
async def fresh_mongo_db(real_mongo: AsyncIOMotorClient, ns: _Namespace) -> AsyncIterator:
    """각 테스트가 자기만의 test DB를 갖도록.

    Teardown은 독립 short-lived motor client를 써서 drop한다. `real_mongo`와
    fixture teardown 순서가 얽혀 event loop가 닫히는 타이밍 이슈로 drop이
    조용히 실패하는 경우를 방지.
    """
    db_name = f"{ns.mongo_db}_{uuid.uuid4().hex[:6]}"
    yield real_mongo[db_name]
    try:
        drop_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            await drop_client.drop_database(db_name)
        finally:
            drop_client.close()
    except Exception:
        pass


# ----- session finalizer: pytest 종료 직전 namespace 잔재 청소 -----------
# async autouse는 loop 문제가 복잡하므로 sync finalizer로 단일 cleanup.
# 각 자원별로 독립 short-lived client를 만들어 쓰고 버림 (비용 무시 가능).
def pytest_sessionfinish(session, exitstatus):
    run_id_val = getattr(session.config, "_rms_run_id", None)
    if run_id_val is None:
        return

    import asyncio as _asyncio

    ns_obj = _Namespace(run_id_val)

    async def _async_cleanup() -> None:
        # ES
        try:
            es = AsyncElasticsearch(hosts=[ES_HOSTS], timeout=5)
            try:
                await es.indices.delete(index=f"{ns_obj.es_index_prefix}*", ignore=[404])
            finally:
                await es.close()
        except Exception:
            pass

        # Mongo
        try:
            m = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
            try:
                # drop the session default + any test-specific per-test DBs
                db_names = await m.list_database_names()
                for name in db_names:
                    if name.startswith(f"EARS_test_{run_id_val}"):
                        await m.drop_database(name)
            finally:
                m.close()
        except Exception:
            pass

        # Redis
        try:
            r = Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
            try:
                async for key in r.scan_iter(f"{ns_obj.redis_prefix}*"):
                    await r.delete(key)
            finally:
                await r.aclose()
        except Exception:
            pass

    try:
        _asyncio.run(_async_cleanup())
    except Exception:
        pass

    # ZK (sync)
    try:
        zk = KazooClient(hosts=ZK_HOSTS, timeout=5.0)
        zk.start(timeout=3)
        try:
            if zk.exists(ns_obj.zk_root):
                zk.delete(ns_obj.zk_root, recursive=True)
        finally:
            zk.stop()
            zk.close()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _stash_run_id(run_id: str, pytestconfig):
    """run_id를 session config에 저장해 pytest_sessionfinish가 접근 가능."""
    pytestconfig._rms_run_id = run_id
    yield
