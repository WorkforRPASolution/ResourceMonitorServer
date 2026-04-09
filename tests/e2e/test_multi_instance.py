"""E2E Phase B — multi-instance ZK coordination.

These are the highest-value e2e tests because they exercise the
distributed code (LeaderElection, PartitionManager, watch machinery,
epoch/timestamp stale defense) end-to-end against a real ZK 3.5.5 with
real ephemerals and real session timeouts.

Each test spawns 2+ uvicorn subprocesses sharing the same ZK root but
with distinct ``MONITOR_INSTANCE_ID`` values. They then:
  - elect a leader
  - distribute the 3 seeded processes (E2E_CVD / E2E_ETCH / E2E_PVD)
  - react to another instance joining / leaving

Tunings:
  - ``zk_session_timeout=10`` (minimum kazoo accepts × safety) so
    failover finishes within the test budget instead of the 30s default.
  - Seed has exactly 3 active processes so round-robin on 2 instances
    produces a 2:1 split — easy to assert both have > 0.
"""
from __future__ import annotations

import time

import httpx
import pytest

from tests.e2e.conftest import wait_for_ready

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _get_status(base_url: str, timeout: float = 5.0) -> dict:
    r = httpx.get(f"{base_url}/admin/status", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _poll(
    pred,
    *,
    timeout: float,
    interval: float = 0.5,
    description: str = "condition",
):
    """Poll a predicate until it returns truthy or timeout. Returns the
    last value returned by the predicate (truthy means success).
    Raises TimeoutError with the description on failure.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception:
            last = None
        time.sleep(interval)
    raise TimeoutError(f"{description} did not hold within {timeout}s (last={last})")


# ----------------------------------------------------------------------
# Test 1 — exactly one leader
# ----------------------------------------------------------------------
def test_two_instances_elect_exactly_one_leader(uvicorn_spawner):
    """Spawn two uvicorns sharing the same ZK root → wait for both to
    reach ready → assert that exactly one reports ``is_leader == True``.

    This verifies the kazoo Election recipe + our fire-and-forget wrapper
    actually negotiates leadership between two real processes (as opposed
    to the single-process in-process tests which only mock one side).
    """
    inst_a = uvicorn_spawner(instance_id="e2e-lead-A")
    inst_b = uvicorn_spawner(instance_id="e2e-lead-B")

    try:
        wait_for_ready(inst_a.base_url, timeout=60.0)
        wait_for_ready(inst_b.base_url, timeout=60.0)
    except TimeoutError as e:
        pytest.fail(
            f"ready timeout\n{e}\n\n"
            f"--- A log tail ---\n{inst_a.dump_log_tail()}\n\n"
            f"--- B log tail ---\n{inst_b.dump_log_tail()}"
        )

    # Give the election recipe a moment to propagate after ready.
    # Ready only checks that the lifespan has yielded, not that the
    # election callback has fired on every instance.
    def _exactly_one_leader() -> dict | None:
        sa = _get_status(inst_a.base_url)
        sb = _get_status(inst_b.base_url)
        leaders = [s for s in (sa, sb) if s.get("is_leader") is True]
        if len(leaders) == 1:
            return {"a": sa, "b": sb, "leader": leaders[0]}
        return None

    result = _poll(
        _exactly_one_leader,
        timeout=20.0,
        description="exactly one instance becomes leader",
    )

    # Exactly one leader, the other is a follower
    sa, sb = result["a"], result["b"]
    assert (sa["is_leader"], sb["is_leader"]).count(True) == 1, (
        f"expected exactly 1 leader, got A={sa['is_leader']} B={sb['is_leader']}"
    )
    leader = result["leader"]
    assert leader["leader_epoch"] is not None
    assert leader["leader_epoch"] >= 1


# ----------------------------------------------------------------------
# Test 2 — new instance triggers redistribute
# ----------------------------------------------------------------------
def test_new_instance_triggers_redistribute(uvicorn_spawner):
    """Boot A alone → wait until it owns all 3 seeded processes → boot B →
    after debounce (2s) + redistribute, both instances report a non-empty
    ``assigned_processes`` list that together covers all 3.

    The predicate is "both own >= 1 and the union covers the seed set",
    not an exact split. Round-robin on 3/2 is 2:1 but we do not pin the
    order so tests stay robust against sort changes.
    """
    inst_a = uvicorn_spawner(instance_id="e2e-redist-A")
    try:
        wait_for_ready(inst_a.base_url, timeout=60.0)
    except TimeoutError as e:
        pytest.fail(f"A ready timeout\n{e}\n{inst_a.dump_log_tail()}")

    # A alone — should eventually own all 3 processes
    def _a_owns_all() -> list | None:
        s = _get_status(inst_a.base_url)
        if s.get("is_leader") is True:
            procs = sorted(s.get("assigned_processes") or [])
            if set(procs) == {"E2E_CVD", "E2E_ETCH", "E2E_PVD"}:
                return procs
        return None

    _poll(_a_owns_all, timeout=20.0, description="A owns all 3 seeded processes")

    # Now bring B online
    inst_b = uvicorn_spawner(instance_id="e2e-redist-B")
    try:
        wait_for_ready(inst_b.base_url, timeout=60.0)
    except TimeoutError as e:
        pytest.fail(f"B ready timeout\n{e}\n{inst_b.dump_log_tail()}")

    # Wait for redistribute: debounce is ~2s + ZK roundtrip + apply
    # assignment. Give it 25s comfortably.
    def _both_covered() -> dict | None:
        sa = _get_status(inst_a.base_url)
        sb = _get_status(inst_b.base_url)
        pa = sorted(sa.get("assigned_processes") or [])
        pb = sorted(sb.get("assigned_processes") or [])
        if len(pa) >= 1 and len(pb) >= 1 and set(pa + pb) == {
            "E2E_CVD", "E2E_ETCH", "E2E_PVD"
        } and not (set(pa) & set(pb)):
            return {"a": pa, "b": pb}
        return None

    split = _poll(
        _both_covered,
        timeout=25.0,
        description="both instances own >=1 process with disjoint coverage",
    )

    # Sanity: the split is either (2,1) or (1,2) given 3 processes / 2 instances
    a_count, b_count = len(split["a"]), len(split["b"])
    assert {a_count, b_count} == {1, 2}, f"unexpected split A={split['a']} B={split['b']}"


# ----------------------------------------------------------------------
# Test 3 — leader failover on SIGTERM
# ----------------------------------------------------------------------
def test_leader_failover_on_sigterm(uvicorn_spawner):
    """Boot A + B, find the leader, SIGTERM it, verify the other becomes
    the new leader within the ZK session timeout + debounce and owns all
    3 processes.

    This is THE key regression guard for the distributed module: it
    exercises leader election, session expiry on the ZK side, the new
    leader's redistribute callback, and the follower taking over the
    ephemeral assignment nodes.
    """
    inst_a = uvicorn_spawner(instance_id="e2e-failover-A")
    inst_b = uvicorn_spawner(instance_id="e2e-failover-B")

    try:
        wait_for_ready(inst_a.base_url, timeout=60.0)
        wait_for_ready(inst_b.base_url, timeout=60.0)
    except TimeoutError as e:
        pytest.fail(
            f"ready timeout\n{e}\n\n"
            f"--- A log tail ---\n{inst_a.dump_log_tail()}\n\n"
            f"--- B log tail ---\n{inst_b.dump_log_tail()}"
        )

    def _who_is_leader() -> dict | None:
        sa = _get_status(inst_a.base_url)
        sb = _get_status(inst_b.base_url)
        if sa.get("is_leader") and not sb.get("is_leader"):
            return {"leader": inst_a, "follower": inst_b,
                    "leader_epoch": sa["leader_epoch"]}
        if sb.get("is_leader") and not sa.get("is_leader"):
            return {"leader": inst_b, "follower": inst_a,
                    "leader_epoch": sb["leader_epoch"]}
        return None

    initial = _poll(_who_is_leader, timeout=20.0, description="one leader elected")
    original_leader = initial["leader"]
    follower = initial["follower"]
    original_epoch = initial["leader_epoch"]

    # SIGTERM the leader — simulates a rolling restart or K8s eviction
    original_leader.terminate(timeout=20.0)
    assert not original_leader.is_alive()

    # Failover budget:
    #   ZK session_timeout (10s) + election propagation + debounce (2s)
    #   + redistribute commit + apply-assignment roundtrip
    # 40s is comfortable.
    def _follower_took_over() -> dict | None:
        try:
            s = _get_status(follower.base_url)
        except Exception:
            return None
        if not s.get("is_leader"):
            return None
        procs = sorted(s.get("assigned_processes") or [])
        if set(procs) != {"E2E_CVD", "E2E_ETCH", "E2E_PVD"}:
            return None
        new_epoch = s.get("leader_epoch")
        if new_epoch is None or new_epoch <= original_epoch:
            # Epoch must strictly advance — this is the whole point of
            # the persistent epoch counter.
            return None
        return {"procs": procs, "epoch": new_epoch}

    try:
        result = _poll(
            _follower_took_over,
            timeout=45.0,
            description="follower takes over leadership + all 3 processes + new epoch",
        )
    except TimeoutError as e:
        pytest.fail(
            f"failover did not complete\n{e}\n\n"
            f"--- original leader log tail ---\n{original_leader.dump_log_tail()}\n\n"
            f"--- follower log tail ---\n{follower.dump_log_tail()}"
        )

    # Follower now owns everything
    assert result["procs"] == ["E2E_CVD", "E2E_ETCH", "E2E_PVD"]
    assert result["epoch"] > original_epoch
