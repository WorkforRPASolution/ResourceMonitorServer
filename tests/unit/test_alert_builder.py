"""Tests for src.analyzer.alert_builder — alert payload construction."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.analyzer.alert_builder import (
    build_alert_request,
    classify_metric_category,
    group_breaches_by_equipment,
)
from src.analyzer.threshold import ThresholdBreach
from src.config.constants import (
    ALERT_CATEGORY_CPU,
    ALERT_CATEGORY_DISK,
    ALERT_CATEGORY_GPU,
    ALERT_CATEGORY_MEMORY,
    ALERT_CATEGORY_PROCESS_WATCH,
    ALERT_CATEGORY_RESOURCE,
    ALERT_CATEGORY_TEMPERATURE,
    ALERT_CODE_RESOURCE_MONITOR,
)


# ------------------------------------------------------------------
# classify_metric_category
# ------------------------------------------------------------------
@pytest.mark.unit
class TestClassifyMetricCategory:
    def test_classify_cpu_total_used_pct(self):
        assert classify_metric_category("cpu", "total_used_pct") == ALERT_CATEGORY_CPU

    def test_classify_cpu_core_load(self):
        assert classify_metric_category("cpu", "core_load_3") == ALERT_CATEGORY_CPU

    def test_classify_memory(self):
        assert classify_metric_category("mem_usage", "mem_used_pct") == ALERT_CATEGORY_MEMORY

    def test_classify_disk(self):
        assert classify_metric_category("disk_usage", "disk_c_pct") == ALERT_CATEGORY_DISK

    def test_classify_gpu(self):
        assert classify_metric_category("gpu_metrics", "gpu0_util") == ALERT_CATEGORY_GPU

    def test_classify_temperature(self):
        assert classify_metric_category("hw_stats", "cpu_temp") == ALERT_CATEGORY_TEMPERATURE

    def test_classify_process_watch_required(self):
        assert classify_metric_category("proc_watch", "required") == ALERT_CATEGORY_PROCESS_WATCH

    def test_classify_process_watch_forbidden(self):
        assert classify_metric_category("proc_watch", "forbidden") == ALERT_CATEGORY_PROCESS_WATCH

    def test_classify_fallback_resource(self):
        assert classify_metric_category("unknown_xyz", "some_field") == ALERT_CATEGORY_RESOURCE


# ------------------------------------------------------------------
# build_alert_request
# ------------------------------------------------------------------
def _make_settings(grafana_base_url: str = "http://grafana:3000",
                   grafana_dashboard_uid: str = "abc123") -> MagicMock:
    settings = MagicMock()
    settings.grafana_base_url = grafana_base_url
    settings.grafana_dashboard_uid = grafana_dashboard_uid
    return settings


@pytest.mark.unit
class TestBuildAlertRequest:
    def test_build_alert_request_field_mapping(self):
        """localpc -> hostname, ipAddr -> ip, eqpModel -> eqp_model."""
        breach = ThresholdBreach(
            eqp_id="EQP01",
            metric="total_used_pct",
            current_value=92.5,
            threshold_value=80.0,
            severity="WARNING",
        )
        eqp_info = {
            "localpc": "HOST-01",
            "ipAddr": "10.0.0.1",
            "eqpModel": "MODEL-X",
            "line": "LINE-A",
        }
        req = build_alert_request(
            breach=breach,
            eqp_info=eqp_info,
            process="CVD",
            settings=_make_settings(),
            metric_pattern="cpu",
            window_minutes=5,
        )
        assert req.hostname == "HOST-01"
        assert req.ip == "10.0.0.1"
        assert req.eqp_model == "MODEL-X"
        assert req.line == "LINE-A"
        assert req.process == "CVD"

    def test_build_alert_request_subcode_format(self):
        breach = ThresholdBreach(
            eqp_id="EQP01",
            metric="total_used_pct",
            current_value=92.5,
            threshold_value=80.0,
            severity="WARNING",
        )
        req = build_alert_request(
            breach=breach,
            eqp_info={"localpc": "H", "ipAddr": "1.2.3.4", "eqpModel": "M", "line": "L"},
            process="CVD",
            settings=_make_settings(),
            metric_pattern="cpu",
            window_minutes=5,
        )
        assert req.subcode == "CPU_WARNING"

    def test_build_alert_request_variables_keys(self):
        breach = ThresholdBreach(
            eqp_id="EQP01",
            metric="total_used_pct",
            current_value=92.5,
            threshold_value=80.0,
            severity="CRITICAL",
        )
        req = build_alert_request(
            breach=breach,
            eqp_info={"localpc": "H", "ipAddr": "1.2.3.4", "eqpModel": "M", "line": "L"},
            process="CVD",
            settings=_make_settings(),
            metric_pattern="cpu",
            window_minutes=10,
        )
        expected_keys = {
            "Severity", "Category", "MetricName",
            "CurrentValue", "Threshold", "WindowMin", "GrafanaUrl",
        }
        assert set(req.variables.keys()) == expected_keys
        assert req.variables["Severity"] == "CRITICAL"
        assert req.variables["Category"] == "CPU"
        assert req.variables["MetricName"] == "total_used_pct"
        assert req.variables["CurrentValue"] == "92.5"
        assert req.variables["Threshold"] == "80.0"
        assert req.variables["WindowMin"] == "10"

    def test_build_alert_request_grafana_url(self):
        breach = ThresholdBreach(
            eqp_id="EQP01",
            metric="total_used_pct",
            current_value=92.5,
            threshold_value=80.0,
            severity="WARNING",
        )
        settings = _make_settings(
            grafana_base_url="http://grafana:3000",
            grafana_dashboard_uid="abc123",
        )
        req = build_alert_request(
            breach=breach,
            eqp_info={"localpc": "H", "ipAddr": "1.2.3.4", "eqpModel": "M", "line": "L"},
            process="CVD",
            settings=settings,
            metric_pattern="cpu",
            window_minutes=5,
        )
        expected = "http://grafana:3000/d/abc123?var-eqpId=EQP01&var-process=CVD"
        assert req.variables["GrafanaUrl"] == expected

    def test_build_alert_request_grafana_url_empty_when_no_uid(self):
        breach = ThresholdBreach(
            eqp_id="EQP01",
            metric="total_used_pct",
            current_value=92.5,
            threshold_value=80.0,
            severity="WARNING",
        )
        settings = _make_settings(grafana_base_url="http://grafana:3000", grafana_dashboard_uid="")
        req = build_alert_request(
            breach=breach,
            eqp_info={"localpc": "H", "ipAddr": "1.2.3.4", "eqpModel": "M", "line": "L"},
            process="CVD",
            settings=settings,
            metric_pattern="cpu",
            window_minutes=5,
        )
        assert req.variables["GrafanaUrl"] == ""

    def test_build_alert_request_code_is_resource_monitor(self):
        breach = ThresholdBreach(
            eqp_id="EQP01",
            metric="total_used_pct",
            current_value=92.5,
            threshold_value=80.0,
            severity="WARNING",
        )
        req = build_alert_request(
            breach=breach,
            eqp_info={"localpc": "H", "ipAddr": "1.2.3.4", "eqpModel": "M", "line": "L"},
            process="CVD",
            settings=_make_settings(),
            metric_pattern="cpu",
            window_minutes=5,
        )
        assert req.code == ALERT_CODE_RESOURCE_MONITOR


# ------------------------------------------------------------------
# group_breaches_by_equipment
# ------------------------------------------------------------------
@pytest.mark.unit
class TestGroupBreachesByEquipment:
    def test_group_breaches_by_equipment_groups_correctly(self):
        b1 = ThresholdBreach(
            eqp_id="EQP01", metric="total_used_pct",
            current_value=90.0, threshold_value=80.0, severity="WARNING",
        )
        b2 = ThresholdBreach(
            eqp_id="EQP02", metric="mem_used_pct",
            current_value=85.0, threshold_value=70.0, severity="CRITICAL",
        )
        b3 = ThresholdBreach(
            eqp_id="EQP01", metric="disk_c_pct",
            current_value=95.0, threshold_value=90.0, severity="CRITICAL",
        )
        groups = group_breaches_by_equipment([b1, b2, b3])
        assert set(groups.keys()) == {"EQP01", "EQP02"}
        assert len(groups["EQP01"]) == 2
        assert len(groups["EQP02"]) == 1
        assert groups["EQP01"][0] is b1
        assert groups["EQP01"][1] is b3
        assert groups["EQP02"][0] is b2

    def test_group_breaches_empty_returns_empty(self):
        groups = group_breaches_by_equipment([])
        assert groups == {}
