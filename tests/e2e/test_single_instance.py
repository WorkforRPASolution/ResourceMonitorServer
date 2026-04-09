"""E2E Phase A — single-instance wall-clock scenarios.

These automate V7/V8 from the v6 Step 8.5 verification plan:
  V7: normal boot → /healthz/ready 200 + infra_up gauges + startup_complete
  V8: runtime Redis outage → ready flips 503 → recovers after docker start

Both tests spawn a real uvicorn subprocess, so the full lifespan (all
11 phases) runs against OrbStack. This is the only automated coverage
we have for the subprocess boot path; in-process ``asgi_lifespan``
cannot detect issues that only appear with a real process / real loop.
"""
from __future__ import annotations

import re
import subprocess
import time

import httpx
import pytest

from tests.e2e.conftest import wait_for_ready

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


REDIS_CONTAINER = "ars-redis"


# ----------------------------------------------------------------------
# /metrics text-format parser
# ----------------------------------------------------------------------
_METRIC_LINE_RE = re.compile(
    r"^(?P<name>\w+)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)"
)


def _parse_metrics(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Parse Prometheus exposition text into a ``{(name, labels): value}`` dict.

    Labels are a tuple of ``(key, value)`` pairs, sorted, so lookups are
    order-independent. Ignores ``#`` comment lines.
    """
    out: dict = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        labels_raw = m.group("labels") or ""
        labels: list[tuple[str, str]] = []
        if labels_raw:
            # Simple parser: k="v",k2="v2"  (no escaped quotes in our labels)
            for pair in labels_raw.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    labels.append((k.strip(), v.strip().strip('"')))
        labels.sort()
        out[(name, tuple(labels))] = float(m.group("value"))
    return out


def _get(
    metrics: dict, name: str, **labels
) -> float:
    key = (name, tuple(sorted(labels.items())))
    return metrics[key]


# ----------------------------------------------------------------------
# Test 1 — V7: normal boot + metrics
# ----------------------------------------------------------------------
def test_normal_boot_reports_all_infra_up_and_metrics(uvicorn_spawner):
    """Boot a real uvicorn against OrbStack, hit /healthz/ready + /metrics,
    verify all 5 infras report ``infra_up == 1`` and startup_complete flips.

    This is the automated version of V7 in the v6 plan — a wall-clock
    regression guard that P0-5 (infra_up Gauge) and P0-3 (ES/Email
    startup ping) are both wired up correctly.
    """
    inst = uvicorn_spawner(instance_id="e2e-single-normal")
    try:
        body = wait_for_ready(inst.base_url, timeout=60.0)
    except TimeoutError as e:
        pytest.fail(f"ready timeout\n{e}\n\nLog tail:\n{inst.dump_log_tail()}")

    # 1. /healthz/ready body — all 5 checks must be True
    assert body["status"] == "ready", body
    checks = body["checks"]
    for infra in ("elasticsearch", "mongodb", "redis", "email_api", "zookeeper"):
        assert checks[infra] is True, f"{infra} not ready: {checks}"
    assert body["scheduler_running"] is True

    # 2. /healthz/live — always 200, infra-agnostic
    r = httpx.get(f"{inst.base_url}/healthz/live", timeout=3.0)
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}

    # 3. /metrics — infra_up + startup_complete
    r = httpx.get(f"{inst.base_url}/metrics", timeout=5.0)
    assert r.status_code == 200
    metrics = _parse_metrics(r.text)

    for infra in ("elasticsearch", "mongodb", "redis", "email_api", "zookeeper"):
        assert _get(metrics, "resource_monitor_infra_up", infra=infra) == 1.0, (
            f"{infra} infra_up not 1.0 — likely readiness did not update it.\n"
            f"Log tail:\n{inst.dump_log_tail()}"
        )
    assert _get(metrics, "resource_monitor_startup_complete") == 1.0

    # 4. /admin/status — minimal sanity check, leader + epoch present
    r = httpx.get(f"{inst.base_url}/admin/status", timeout=3.0)
    assert r.status_code == 200
    status = r.json()
    assert status["instance_id"] == "e2e-single-normal"
    # Single instance ⇒ it's the leader (possibly after a tick), but the
    # election can take up to the ZK election latency. Be lenient.
    assert "is_leader" in status
    assert "assigned_processes" in status


# ----------------------------------------------------------------------
# Test 2 — V8: runtime Redis outage + recovery
# ----------------------------------------------------------------------
def _docker_stop(name: str) -> None:
    subprocess.run(["docker", "stop", name], check=False, capture_output=True)


def _docker_start(name: str) -> None:
    subprocess.run(["docker", "start", name], check=False, capture_output=True)


@pytest.fixture
def ensure_redis_up():
    """Safety net — if a test leaves ars-redis stopped, this brings it
    back before the next test runs. Test-level, runs even on failure."""
    yield
    _docker_start(REDIS_CONTAINER)


def test_runtime_redis_stop_flips_ready_503_then_recovers(
    uvicorn_spawner, ensure_redis_up
):
    """Boot normally → stop Redis → ready becomes 503 (checks.redis=false,
    infra_up{redis}=0) but scheduler_running stays True → start Redis →
    ready returns to 200 + checks.redis=true.

    This is the automated version of V8 — the guarantee that a Redis
    outage degrades gracefully (cooldown falls back to local TTLCache,
    scheduler keeps running) instead of killing the pod.
    """
    inst = uvicorn_spawner(instance_id="e2e-single-redis-degraded")
    try:
        wait_for_ready(inst.base_url, timeout=60.0)
    except TimeoutError as e:
        pytest.fail(f"initial ready timeout\n{e}\n\nLog tail:\n{inst.dump_log_tail()}")

    # --- Phase 1: kill Redis ---
    _docker_stop(REDIS_CONTAINER)

    # Poll /healthz/ready until it flips to 503 (or timeout).
    # The ping has a 2s timeout so the flip should happen within a few
    # poll cycles.
    deadline = time.monotonic() + 25.0
    flipped = False
    body = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{inst.base_url}/healthz/ready", timeout=5.0)
            body = r.json() if r.headers.get("content-type", "").startswith(
                "application/json"
            ) else None
            if r.status_code == 503:
                flipped = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    assert flipped, (
        f"ready did not flip to 503 after stopping Redis within 25s. "
        f"last body: {body}\nLog tail:\n{inst.dump_log_tail()}"
    )
    assert body is not None
    assert body["checks"]["redis"] is False, body
    # Other infras should still be green
    assert body["checks"]["elasticsearch"] is True
    assert body["checks"]["mongodb"] is True
    assert body["checks"]["zookeeper"] is True

    # infra_up gauge reflects the outage
    r = httpx.get(f"{inst.base_url}/metrics", timeout=5.0)
    assert r.status_code == 200
    metrics = _parse_metrics(r.text)
    assert _get(metrics, "resource_monitor_infra_up", infra="redis") == 0.0
    assert _get(metrics, "resource_monitor_infra_up", infra="elasticsearch") == 1.0

    # Scheduler must still be running — degraded, not dead
    r = httpx.get(f"{inst.base_url}/admin/status", timeout=3.0)
    assert r.status_code == 200
    assert r.json()["scheduler_running"] is True

    # The subprocess itself must still be alive — Redis outage is NOT
    # fatal. If the process died, /admin/status would have raised above,
    # but double-check explicitly for a clearer failure message.
    assert inst.is_alive(), (
        f"uvicorn died during Redis outage — cooldown fallback is broken.\n"
        f"Log tail:\n{inst.dump_log_tail()}"
    )

    # --- Phase 2: restore Redis ---
    _docker_start(REDIS_CONTAINER)

    # Allow the container up to 15s to accept connections (ars-redis is
    # fast-booting; this is safety margin).
    deadline = time.monotonic() + 30.0
    recovered = False
    body = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{inst.base_url}/healthz/ready", timeout=5.0)
            body = r.json() if r.headers.get("content-type", "").startswith(
                "application/json"
            ) else None
            if r.status_code == 200 and body and body["checks"]["redis"] is True:
                recovered = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    assert recovered, (
        f"ready did not recover after restarting Redis. last body: {body}\n"
        f"Log tail:\n{inst.dump_log_tail()}"
    )
    r = httpx.get(f"{inst.base_url}/metrics", timeout=5.0)
    metrics = _parse_metrics(r.text)
    assert _get(metrics, "resource_monitor_infra_up", infra="redis") == 1.0
