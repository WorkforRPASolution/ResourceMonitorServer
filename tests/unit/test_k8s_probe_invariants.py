"""Static invariants between k8s/deployment.yaml and AppSettings.

v6 P1-4 — these guard the dead-zone fix from regressing. The whole point of
``zk_startup_budget_sec`` is that it must finish (or fail) BEFORE the K8s
liveness probe starts firing — otherwise we are right back in CrashLoopBackoff.
If anyone bumps the ZK budget without bumping ``initialDelaySeconds`` (or vice
versa), this test fails loudly at PR time instead of silently in production.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.config.settings import AppSettings

_DEPLOYMENT_PATH = (
    Path(__file__).resolve().parents[2] / "k8s" / "deployment.yaml"
)


def _load_container() -> dict:
    manifest = yaml.safe_load(_DEPLOYMENT_PATH.read_text())
    containers = manifest["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, "expected a single container in the pod spec"
    return containers[0]


@pytest.mark.unit
class TestK8sProbeInvariants:
    def test_liveness_initial_delay_exceeds_zk_startup_budget(self):
        """liveness must not fire while ZK startup is still within its budget.

        Safety margin = 10s for the rest of the lifespan (mongo retry, repos,
        seed, distributed wiring, scheduler start). If you ever shrink this,
        re-walk the lifespan phases first.
        """
        container = _load_container()
        initial_delay = container["livenessProbe"]["initialDelaySeconds"]
        zk_budget = AppSettings().zk_startup_budget_sec
        assert initial_delay >= zk_budget + 10, (
            f"livenessProbe.initialDelaySeconds ({initial_delay}) must be "
            f">= zk_startup_budget_sec ({zk_budget}) + 10s safety margin. "
            f"Otherwise k8s will start killing pods before ZK has even given "
            f"up — exactly the dead-zone we shipped P0-1 to fix."
        )

    def test_readiness_failure_threshold_allows_60s_grace(self):
        """A blip in any of the 5 infras must not bounce traffic instantly.

        readiness ``failureThreshold * periodSeconds`` is the grace window
        before k8s marks the pod NotReady. We want at least 60s so a brief
        ES/Mongo/Redis/ZK reconnection does not cascade.
        """
        container = _load_container()
        readiness = container["readinessProbe"]
        grace = readiness["failureThreshold"] * readiness["periodSeconds"]
        assert grace >= 60, (
            f"readiness grace window = {grace}s "
            f"(failureThreshold {readiness['failureThreshold']} × "
            f"periodSeconds {readiness['periodSeconds']}). "
            f"Must be >= 60s so transient infra blips do not flap traffic."
        )

    def test_termination_grace_period_covers_scheduler_shutdown(self):
        """Scheduler graceful shutdown is 30s + PreStop sleep 5s + slack.

        terminationGracePeriodSeconds must be at least 60s so APScheduler can
        let in-flight analysis jobs finish during a rolling update.
        """
        manifest = yaml.safe_load(_DEPLOYMENT_PATH.read_text())
        grace = manifest["spec"]["template"]["spec"][
            "terminationGracePeriodSeconds"
        ]
        assert grace >= 60, (
            f"terminationGracePeriodSeconds={grace} is too short. Scheduler "
            f"shutdown alone is 30s, plus a PreStop sleep of 5s for endpoint "
            f"propagation. Need >= 60s total."
        )
