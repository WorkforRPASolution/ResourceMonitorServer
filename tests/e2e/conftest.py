"""E2E test fixtures — real uvicorn subprocess against OrbStack infra.

These tests spawn ``uvicorn src.main:app`` as an actual child process and
interact with it via HTTP. Everything integration-test fixtures give you
(Mongo/Redis/ZK/ES clients) is reused here so namespace cleanup stays
consistent.

Why subprocess (vs asgi_lifespan in-process)?
  - Multi-instance leader-election tests require **two independent
    Python processes** competing on ZK — you cannot do that in a single
    asyncio loop.
  - Phase A single-instance tests double as wall-clock regression guards
    for V7/V8 in the v6 plan (normal boot + runtime Redis degraded).

Differences from integration:
  - Settings must be injected via process environment (``env=`` to
    ``Popen``), not ``monkeypatch``, because monkeypatch only affects the
    current process.
  - The email mock runs in a **stdlib** ``http.server`` background thread,
    not aiohttp, because the uvicorn subprocess reaches it over real TCP
    and must not depend on the pytest event loop.
  - Readiness polling is HTTP-based with a generous timeout (60s) to
    cover the 11-phase lifespan against real infra.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

# Note: the session-level ``run_id`` / ``ns`` + ``pytest_sessionfinish``
# cleanup are defined below, independent of the integration conftest.
# We cannot use ``pytest_plugins = [...]`` here because pytest blocks
# that directive from non-top-level conftests. Duplicating the ~20 lines
# of namespace scaffolding is cheaper than promoting them to the root
# conftest (which would pollute unit test collection).


pytestmark_default = [pytest.mark.e2e, pytest.mark.slow]

# ----- Shared endpoints (same as integration) -----------------------------
ES_HOSTS = os.getenv("TEST_ES_HOSTS", "http://localhost:9200")
MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
ZK_HOSTS = os.getenv("TEST_ZK_HOSTS", "localhost:2181")


# ----- Session namespace (mirrors integration/conftest.py) ----------------
class _Namespace:
    """UUID-scoped prefixes — every e2e test run gets its own."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.mongo_db = f"EARS_test_{run_id}"
        self.redis_prefix = f"RESOURCE_ALERT_test_{run_id}"
        self.es_index_prefix = f"test_{run_id}_"
        self.zk_root = f"/resource-monitor-test-{run_id}"


@pytest.fixture(scope="session")
def run_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def ns(run_id: str) -> _Namespace:
    return _Namespace(run_id)


@pytest.fixture(scope="session", autouse=True)
def _stash_e2e_run_id(run_id: str, pytestconfig):
    """Stash run_id so ``pytest_sessionfinish`` can reach it for
    cleanup. Separate attribute from the integration conftest so the
    two suites can coexist in a single pytest run without clobbering."""
    pytestconfig._rms_e2e_run_id = run_id
    yield


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Clean up the e2e namespace — Mongo DBs, Redis keys, ZK tree.

    This runs independently of the integration conftest's own
    sessionfinish: each subtree only cleans up its own prefixes, and
    the two prefixes are distinct because each subtree generates its
    own ``run_id`` at the start of the session.
    """
    run_id_val = getattr(session.config, "_rms_e2e_run_id", None)
    if run_id_val is None:
        return

    ns_obj = _Namespace(run_id_val)

    async def _async_cleanup() -> None:
        # Mongo — drop every DB that starts with the session prefix
        try:
            m = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
            try:
                db_names = await m.list_database_names()
                for name in db_names:
                    if name.startswith(f"EARS_test_{run_id_val}"):
                        await m.drop_database(name)
            finally:
                m.close()
        except Exception:
            pass

        # Redis — scan + delete per key
        try:
            from redis.asyncio import Redis  # local import keeps import
            # time fast for unit-only pytest runs
            r = Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
            try:
                async for key in r.scan_iter(f"{ns_obj.redis_prefix}*"):
                    await r.delete(key)
            finally:
                await r.aclose()
        except Exception:
            pass

    try:
        asyncio.run(_async_cleanup())
    except Exception:
        pass

    # ZK — recursive delete of the session root (sync kazoo)
    try:
        from kazoo.client import KazooClient

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


# ----- Stdlib email mock (subprocess-reachable) ---------------------------
class _EmailMockHandler(BaseHTTPRequestHandler):
    """Minimal Akka /EmailNotify stand-in — POST always returns success,
    HEAD returns 200 so ``email_client.health_check()`` passes at startup.
    """

    def do_POST(self):  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)
        body = b'{"result":"success","message":"send ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args, **kwargs):  # silence default access log
        pass


@pytest.fixture(scope="session")
def email_mock_url() -> Iterator[str]:
    """Session-wide email mock. A daemon thread serves requests until
    pytest exits — subprocess children reach it over real loopback TCP."""
    server = HTTPServer(("127.0.0.1", 0), _EmailMockHandler)
    port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, name="e2e-email-mock", daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/EmailNotify"
    finally:
        server.shutdown()
        server.server_close()


# ----- Free-port helper ---------------------------------------------------
def _free_tcp_port() -> int:
    """Ask the kernel for a free TCP port. Closes immediately — slim race
    window but acceptable for tests that bind right after."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ----- EQP_INFO seed ------------------------------------------------------
# Three processes so round-robin partitioning to 2 instances produces a
# 2:1 split — easy to assert uneven-but-covered distribution.
_SEED_EQP_INFO: list[dict[str, Any]] = [
    {
        "eqpId": "E2E-CVD-01", "process": "E2E_CVD",
        "eqpModel": "M1", "line": "L1", "category": "cvd",
        "ipAddr": "10.0.0.11", "localpc": "PC11",
        "onoff": 1, "webmanagerUse": 1,
    },
    {
        "eqpId": "E2E-ETCH-01", "process": "E2E_ETCH",
        "eqpModel": "M2", "line": "L1", "category": "etch",
        "ipAddr": "10.0.0.12", "localpc": "PC12",
        "onoff": 1, "webmanagerUse": 1,
    },
    {
        "eqpId": "E2E-PVD-01", "process": "E2E_PVD",
        "eqpModel": "M3", "line": "L1", "category": "pvd",
        "ipAddr": "10.0.0.13", "localpc": "PC13",
        "onoff": 1, "webmanagerUse": 1,
    },
]


@pytest_asyncio.fixture
async def seeded_mongo_db(ns) -> AsyncIterator[str]:
    """Create a fresh test DB, seed 3 active EQP_INFO rows, yield the DB
    name. Teardown drops the DB via a short-lived client (avoiding motor
    event-loop entanglement with session clients)."""
    db_name = f"{ns.mongo_db}_e2e_{uuid.uuid4().hex[:6]}"
    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        await client[db_name]["EQP_INFO"].insert_many(_SEED_EQP_INFO)
    finally:
        client.close()

    yield db_name

    drop_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        await drop_client.drop_database(db_name)
    finally:
        drop_client.close()


# ----- Uvicorn subprocess manager -----------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class UvicornProcess:
    """Handle for a running uvicorn child process."""

    instance_id: str
    port: int
    base_url: str
    proc: subprocess.Popen
    log_path: Path
    log_fp: Any = field(default=None)

    def terminate(self, timeout: float = 15.0) -> None:
        """Graceful shutdown — SIGTERM → wait → SIGKILL on timeout."""
        if self.proc.poll() is not None:
            self._close_log()
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        finally:
            self._close_log()

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def dump_log_tail(self, lines: int = 60) -> str:
        """Read the last N lines of captured stdout+stderr. For use in
        assertion failure messages."""
        try:
            text = self.log_path.read_text(errors="replace")
        except FileNotFoundError:
            return "<log file missing>"
        tail = text.splitlines()[-lines:]
        return "\n".join(tail)

    def _close_log(self) -> None:
        if self.log_fp is not None:
            try:
                self.log_fp.flush()
                self.log_fp.close()
            except Exception:
                pass
            self.log_fp = None


def _build_env(
    *,
    instance_id: str,
    mongo_db: str,
    redis_prefix: str,
    zk_root: str,
    email_url: str,
    zk_session_timeout: int = 10,
    zk_startup_budget_sec: int = 20,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess env — inherit the parent's PATH/venv but
    override every MONITOR_* that would otherwise collide with production
    defaults. Tuning knobs (session timeout, startup budget) are tightened
    so failover tests finish in under a minute."""
    env = os.environ.copy()
    env.update(
        {
            "MONITOR_ES_HOSTS": ES_HOSTS,
            "MONITOR_ES_USERNAME": "",
            "MONITOR_ES_PASSWORD": "",
            "MONITOR_MONGO_URI": MONGO_URI,
            "MONITOR_MONGO_DB": mongo_db,
            "MONITOR_ZK_HOSTS": ZK_HOSTS,
            "MONITOR_ZK_ROOT_PATH": zk_root,
            "MONITOR_ZK_SESSION_TIMEOUT": str(zk_session_timeout),
            "MONITOR_ZK_STARTUP_BUDGET_SEC": str(zk_startup_budget_sec),
            "MONITOR_REDIS_URL": REDIS_URL,
            "MONITOR_REDIS_PASSWORD": "",
            "MONITOR_REDIS_KEY_PREFIX": redis_prefix,
            "MONITOR_EMAIL_API_URL": email_url,
            "MONITOR_EMAIL_API_TIMEOUT": "5",
            "MONITOR_INSTANCE_ID": instance_id,
            "MONITOR_LOG_FORMAT": "json",
            "MONITOR_LOG_LEVEL": "INFO",
            # Disable debug read-only so ZK participation actually happens.
            "MONITOR_DEBUG_READ_ONLY": "false",
        }
    )
    if extra:
        env.update(extra)
    return env


def _wait_for_ready(
    base_url: str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
    require_200: bool = True,
) -> dict[str, Any]:
    """Poll /healthz/ready until it returns the expected status or
    timeout. Returns the last parsed JSON body on success."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    last_body: Any = None
    last_status = 0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz/ready", timeout=3.0)
            last_status = r.status_code
            try:
                last_body = r.json()
            except Exception:
                last_body = r.text
            if require_200 and r.status_code == 200:
                return last_body
            if not require_200:
                return last_body
        except Exception as e:
            last_err = e
        time.sleep(poll_interval)
    raise TimeoutError(
        f"ready poll timed out after {timeout}s "
        f"(last_status={last_status}, last_err={last_err!r}, "
        f"last_body={json.dumps(last_body)[:200] if last_body else 'None'})"
    )


def _spawn_uvicorn(
    *,
    instance_id: str,
    env: dict[str, str],
    log_dir: Path,
) -> UvicornProcess:
    """Fork uvicorn. Stdout+stderr go to a file in ``log_dir`` so we can
    dump it in assertion messages without blocking on a pipe."""
    port = _free_tcp_port()
    log_path = log_dir / f"{instance_id}.log"
    log_fp = log_path.open("w")
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",  # keep stdout small; our structlog JSON still goes through
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
    )
    return UvicornProcess(
        instance_id=instance_id,
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        proc=proc,
        log_path=log_path,
        log_fp=log_fp,
    )


@pytest.fixture
def uvicorn_spawner(tmp_path, ns, email_mock_url, seeded_mongo_db):
    """Factory fixture: returns a function that spawns a uvicorn child.

    Each call:
      - allocates a free port
      - builds MONITOR_* env with the test's namespace
      - launches uvicorn as a subprocess
      - the test is responsible for calling ``_wait_for_ready(base_url)``

    Cleanup terminates every spawned child even on test failure and dumps
    the last log lines into the failure message for debugging.
    """
    spawned: list[UvicornProcess] = []
    # Per-test ZK sub-path so parallel tests (if added later) don't
    # clobber each other's members/elections.
    test_zk_root = f"{ns.zk_root}/e2e-{uuid.uuid4().hex[:6]}"

    def _spawn(
        instance_id: str | None = None,
        *,
        zk_session_timeout: int = 10,
        zk_startup_budget_sec: int = 20,
        extra_env: dict[str, str] | None = None,
    ) -> UvicornProcess:
        iid = instance_id or f"e2e-{uuid.uuid4().hex[:6]}"
        env = _build_env(
            instance_id=iid,
            mongo_db=seeded_mongo_db,
            redis_prefix=f"{ns.redis_prefix}_{iid}",
            zk_root=test_zk_root,
            email_url=email_mock_url,
            zk_session_timeout=zk_session_timeout,
            zk_startup_budget_sec=zk_startup_budget_sec,
            extra=extra_env,
        )
        proc = _spawn_uvicorn(
            instance_id=iid,
            env=env,
            log_dir=tmp_path,
        )
        spawned.append(proc)
        return proc

    yield _spawn

    # Teardown: terminate every spawned child in reverse order, then
    # dump logs if any failed to exit cleanly.
    for proc in reversed(spawned):
        if proc.is_alive():
            proc.terminate(timeout=15.0)
        else:
            proc._close_log()


# Re-export the wait-for helper so tests can import it from conftest.
def wait_for_ready(
    base_url: str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
    require_200: bool = True,
) -> dict[str, Any]:
    return _wait_for_ready(
        base_url,
        timeout=timeout,
        poll_interval=poll_interval,
        require_200=require_200,
    )
