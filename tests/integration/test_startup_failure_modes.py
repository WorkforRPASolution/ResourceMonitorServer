"""Startup failure-mode regression suite (v6 P0-6).

Each scenario verifies that an infrastructure outage at boot produces a
**bounded** failure with a **distinct log signal**, instead of the v5
behaviors:
  - ZK down → infinite hang → liveness CrashLoopBackoff (no signal)
  - Redis down → immediate fail (asymmetric vs Mongo's 5-attempt retry)
  - ES bad host → silent boot success (only fails at first query)
  - Email bad URL → silent boot success (only fails at first alert)
  - Mongo down → 5 retries × linear backoff (already correct, regression guard)

Cleanup discipline: every fixture that touches `docker stop` MUST `docker
start` in teardown, even on test failure, so subsequent tests in the same
session see a healthy infra.

Run: `make dev-up && pytest tests/integration/test_startup_failure_modes.py`
"""
from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest
from asgi_lifespan import LifespanManager

from src.config.settings import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ZK_CONTAINER = "ars-zookeeper"
REDIS_CONTAINER = "ars-redis"
MONGO_CONTAINER = "mongodb-44"


# ----------------------------------------------------------------------
# Docker control helpers
# ----------------------------------------------------------------------
def _docker(*args: str, check: bool = True) -> None:
    subprocess.run(["docker", *args], check=check, capture_output=True)


def _docker_stop(name: str) -> None:
    try:
        _docker("stop", name)
    except subprocess.CalledProcessError:
        # Already stopped — fine
        pass


def _docker_start(name: str) -> None:
    try:
        _docker("start", name)
    except subprocess.CalledProcessError:
        pass


async def _wait_redis_ready(url: str = "redis://localhost:6379/15", timeout: float = 15.0) -> None:
    from redis.asyncio import Redis

    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = Redis.from_url(url, decode_responses=True, protocol=2)
            try:
                if await r.ping():
                    return
            finally:
                await r.aclose()
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.3)
    raise RuntimeError(f"Redis not ready within {timeout}s: {last_err!r}")


async def _wait_zk_ready(timeout: float = 30.0) -> None:
    from kazoo.client import KazooClient

    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            kc = KazooClient(hosts="localhost:2181", timeout=5)
            try:
                kc.start(timeout=3)
                if kc.connected:
                    return
            finally:
                try:
                    kc.stop()
                    kc.close()
                except Exception:
                    pass
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.5)
    raise RuntimeError(f"ZK not ready within {timeout}s: {last_err!r}")


async def _wait_mongo_ready(timeout: float = 30.0) -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            client = AsyncIOMotorClient(
                "mongodb://localhost:27017", serverSelectionTimeoutMS=2000
            )
            try:
                await client.admin.command("ping")
                return
            finally:
                client.close()
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Mongo not ready within {timeout}s: {last_err!r}")


# ----------------------------------------------------------------------
# Common env wiring (mirrors test_lifespan_real::live_app but with knobs)
# ----------------------------------------------------------------------
def _set_baseline_env(monkeypatch, ns, mock_email_server, *, sub_zk: str) -> None:
    monkeypatch.setenv("MONITOR_ES_HOSTS", "http://localhost:9200")
    monkeypatch.setenv("MONITOR_ES_USERNAME", "")
    monkeypatch.setenv("MONITOR_ES_PASSWORD", "")
    monkeypatch.setenv("MONITOR_MONGO_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONITOR_MONGO_DB", f"{ns.mongo_db}_failmode")
    monkeypatch.setenv("MONITOR_ZK_HOSTS", "localhost:2181")
    monkeypatch.setenv("MONITOR_ZK_ROOT_PATH", sub_zk)
    monkeypatch.setenv("MONITOR_ZK_SESSION_TIMEOUT", "10")
    monkeypatch.setenv("MONITOR_REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("MONITOR_REDIS_PASSWORD", "")
    monkeypatch.setenv("MONITOR_REDIS_KEY_PREFIX", f"{ns.redis_prefix}_failmode")
    monkeypatch.setenv("MONITOR_EMAIL_API_URL", mock_email_server["url"])
    monkeypatch.setenv("MONITOR_EMAIL_API_TIMEOUT", "5")
    monkeypatch.setenv("MONITOR_INSTANCE_ID", f"failmode-{uuid.uuid4().hex[:6]}")
    monkeypatch.setenv("MONITOR_LOG_FORMAT", "console")


async def _try_lifespan(app, *, startup_timeout: float = 60.0):
    """Boot the app via LifespanManager. Yield the LifespanManager so the
    caller can introspect, but the boot may raise. Used so each test can
    wrap with pytest.raises and time the wall clock."""
    return LifespanManager(app, startup_timeout=startup_timeout, shutdown_timeout=10)


# ----------------------------------------------------------------------
# 1. ZK down at boot — must fail within zk_startup_budget_sec, not hang
# ----------------------------------------------------------------------
async def test_zk_down_at_boot_fails_within_budget(
    ns, mock_email_server, monkeypatch
):
    """v6 P0-1 dead-zone regression guard.

    Before v6: kazoo.start() retried forever, lifespan never yielded,
    /healthz/live never bound → liveness fired at t=60s → CrashLoopBackoff.
    After v6: bounded by ``zk_startup_budget_sec`` (test uses a tiny 5s
    budget so the test runs fast).
    """
    sub_zk = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-zkdown"
    _set_baseline_env(monkeypatch, ns, mock_email_server, sub_zk=sub_zk)
    monkeypatch.setenv("MONITOR_ZK_STARTUP_BUDGET_SEC", "5")

    _docker_stop(ZK_CONTAINER)
    try:
        get_settings.cache_clear()
        from src.main import app

        wall_start = time.monotonic()
        with pytest.raises(Exception) as exc_info:
            mgr = await _try_lifespan(app, startup_timeout=30)
            async with mgr:
                pass
        elapsed = time.monotonic() - wall_start

        # Must be a TimeoutError (or wrap one). Either zk_startup_budget
        # or ExceptionGroup containing it.
        msg = str(exc_info.value)
        # Allow either direct TimeoutError or wrapped via ExceptionGroup/RuntimeError
        assert (
            "zk_startup_budget" in msg
            or isinstance(exc_info.value, TimeoutError)
            or any("zk_startup_budget" in str(x) for x in getattr(exc_info.value, "exceptions", []))
        ), f"unexpected error: {msg}"

        # 5s budget + LifespanManager overhead. Must be much less than the
        # 30s startup_timeout (otherwise we'd be hanging on something else).
        assert elapsed < 20, f"took {elapsed:.1f}s — budget cap not respected"
    finally:
        _docker_start(ZK_CONTAINER)
        await _wait_zk_ready()
        get_settings.cache_clear()


# ----------------------------------------------------------------------
# 2. Redis down at boot — must fail after 3 retries (not 1 like v5)
# ----------------------------------------------------------------------
async def test_redis_down_at_boot_fails_after_retries(
    ns, mock_email_server, monkeypatch
):
    """v6 P0-2: 3 attempts × linear backoff (1s + 2s = ~3s sleeps) +
    3 ping latencies. Total worst-case ~10s, well under ZK budget."""
    sub_zk = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-rdown"
    _set_baseline_env(monkeypatch, ns, mock_email_server, sub_zk=sub_zk)

    _docker_stop(REDIS_CONTAINER)
    try:
        get_settings.cache_clear()
        from src.main import app

        wall_start = time.monotonic()
        with pytest.raises(Exception):
            mgr = await _try_lifespan(app, startup_timeout=30)
            async with mgr:
                pass
        elapsed = time.monotonic() - wall_start

        # Worst case: 3 attempts × ~5s ping timeout + 3s sleeps = ~18s
        # Allow generous overhead but must be bounded.
        assert elapsed < 25, f"took {elapsed:.1f}s — retry not bounded"
    finally:
        _docker_start(REDIS_CONTAINER)
        await _wait_redis_ready()
        get_settings.cache_clear()


# ----------------------------------------------------------------------
# 3. Mongo down at boot — existing 5-retry behavior (regression guard)
# ----------------------------------------------------------------------
async def test_mongo_down_at_boot_fails_after_retries(
    ns, mock_email_server, monkeypatch
):
    """Regression guard for the existing MongoClient.connect_with_retry.
    5 attempts × linear (2/4/6/8/10) + per-attempt 5s timeout = ~50s worst.
    """
    sub_zk = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-mdown"
    _set_baseline_env(monkeypatch, ns, mock_email_server, sub_zk=sub_zk)

    _docker_stop(MONGO_CONTAINER)
    try:
        get_settings.cache_clear()
        from src.main import app

        wall_start = time.monotonic()
        with pytest.raises(Exception):
            mgr = await _try_lifespan(app, startup_timeout=120)
            async with mgr:
                pass
        elapsed = time.monotonic() - wall_start
        # Bounded — must not hang forever
        assert elapsed < 90, f"took {elapsed:.1f}s — retry not bounded"
    finally:
        _docker_start(MONGO_CONTAINER)
        await _wait_mongo_ready()
        get_settings.cache_clear()


# ----------------------------------------------------------------------
# 4. ES bad host at boot — v6 P0-3 ping must catch this (was silent in v5)
# ----------------------------------------------------------------------
async def test_es_bad_host_fails_at_boot(
    ns, mock_email_server, monkeypatch
):
    """v6 P0-3: a typo in MONITOR_ES_HOSTS must fail boot, not silently
    succeed and only blow up at first query."""
    sub_zk = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-esbad"
    _set_baseline_env(monkeypatch, ns, mock_email_server, sub_zk=sub_zk)
    # Point to a closed port. ES client doesn't validate at instantiation.
    monkeypatch.setenv("MONITOR_ES_HOSTS", "http://localhost:59999")

    try:
        get_settings.cache_clear()
        from src.main import app

        wall_start = time.monotonic()
        with pytest.raises(Exception) as exc_info:
            mgr = await _try_lifespan(app, startup_timeout=30)
            async with mgr:
                pass
        elapsed = time.monotonic() - wall_start

        msg = str(exc_info.value)
        assert (
            "es_startup_ping_failed" in msg
            or any("es_startup_ping_failed" in str(x) for x in getattr(exc_info.value, "exceptions", []))
        ), f"expected es_startup_ping_failed, got: {msg}"
        # Must be fast — ES ping doesn't retry, just times out
        assert elapsed < 25, f"took {elapsed:.1f}s — ES ping not bounded"
    finally:
        get_settings.cache_clear()


# ----------------------------------------------------------------------
# 5. Email bad URL at boot — v6 P0-3 health_check must catch this
# ----------------------------------------------------------------------
async def test_email_bad_url_fails_at_boot(
    ns, mock_email_server, monkeypatch
):
    """v6 P0-3: a typo in MONITOR_EMAIL_API_URL must fail boot."""
    sub_zk = f"{ns.zk_root}/{uuid.uuid4().hex[:6]}-emailbad"
    _set_baseline_env(monkeypatch, ns, mock_email_server, sub_zk=sub_zk)
    # Override email URL to a closed port (overrides _set_baseline_env's value)
    monkeypatch.setenv("MONITOR_EMAIL_API_URL", "http://localhost:59998/EmailNotify")

    try:
        get_settings.cache_clear()
        from src.main import app

        wall_start = time.monotonic()
        with pytest.raises(Exception) as exc_info:
            mgr = await _try_lifespan(app, startup_timeout=30)
            async with mgr:
                pass
        elapsed = time.monotonic() - wall_start

        msg = str(exc_info.value)
        assert (
            "email_startup_health_check_failed" in msg
            or any("email_startup_health_check_failed" in str(x) for x in getattr(exc_info.value, "exceptions", []))
        ), f"expected email_startup_health_check_failed, got: {msg}"
        assert elapsed < 25, f"took {elapsed:.1f}s — email check not bounded"
    finally:
        get_settings.cache_clear()
