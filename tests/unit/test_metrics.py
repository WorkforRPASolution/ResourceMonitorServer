"""Tests for src.api.metrics (Prometheus exposition)."""
import pytest
from prometheus_client import CONTENT_TYPE_LATEST

from src.api.metrics import (
    ALERTS_SENT,
    ASSIGNED_PROCESSES,
    ES_QUERY_DURATION,
    INFRA_LABELS,
    INFRA_UP,
    JOB_DURATION,
    JOB_TOTAL,
    STARTUP_COMPLETE,
    ZK_LEADER,
    render_metrics,
)


@pytest.mark.unit
class TestMetricsRegistry:
    def test_job_total_counter_can_be_incremented(self):
        before = JOB_TOTAL.labels(
            process="CVD", status="success", reason=""
        )._value.get()
        JOB_TOTAL.labels(process="CVD", status="success", reason="").inc()
        after = JOB_TOTAL.labels(
            process="CVD", status="success", reason=""
        )._value.get()
        assert after == before + 1

    def test_job_duration_histogram_observes(self):
        JOB_DURATION.labels(process="CVD", metric_category="cpu").observe(0.5)

    def test_es_query_duration_observes(self):
        ES_QUERY_DURATION.labels(process="CVD").observe(0.1)

    def test_alerts_sent_counter(self):
        ALERTS_SENT.labels(code="RESOURCE_MONITOR", subcode="WARNING").inc()

    def test_zk_leader_gauge_set(self):
        ZK_LEADER.set(1)
        assert ZK_LEADER._value.get() == 1
        ZK_LEADER.set(0)

    def test_assigned_processes_gauge_set(self):
        ASSIGNED_PROCESSES.set(5)
        assert ASSIGNED_PROCESSES._value.get() == 5

    def test_infra_up_labels_cover_all_5_infras(self):
        """v6 P0-5: regression guard — adding/removing an infra without
        updating INFRA_LABELS would silently break Prometheus alerts."""
        assert set(INFRA_LABELS) == {
            "elasticsearch",
            "mongodb",
            "redis",
            "email_api",
            "zookeeper",
        }

    def test_infra_up_gauge_set_per_label(self):
        for infra in INFRA_LABELS:
            INFRA_UP.labels(infra=infra).set(1.0)
            assert INFRA_UP.labels(infra=infra)._value.get() == 1.0
            INFRA_UP.labels(infra=infra).set(0.0)
            assert INFRA_UP.labels(infra=infra)._value.get() == 0.0

    def test_startup_complete_gauge_initially_zero_then_set(self):
        STARTUP_COMPLETE.set(0.0)
        assert STARTUP_COMPLETE._value.get() == 0.0
        STARTUP_COMPLETE.set(1.0)
        assert STARTUP_COMPLETE._value.get() == 1.0


@pytest.mark.unit
class TestRenderMetrics:
    def test_render_returns_bytes_with_content_type(self):
        body, ctype = render_metrics()
        assert isinstance(body, bytes)
        assert ctype == CONTENT_TYPE_LATEST

    def test_render_includes_known_metric_names(self):
        body, _ = render_metrics()
        text = body.decode()
        assert "resource_monitor_job_total" in text
        assert "resource_monitor_zk_leader" in text
        assert "resource_monitor_infra_up" in text
        assert "resource_monitor_startup_complete" in text
