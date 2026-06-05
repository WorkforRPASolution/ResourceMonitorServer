"""Tests for src.analyzer.alert_builder — v2 alert payload construction."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.analyzer.alert_builder import build_alert_request, make_cooldown_key
from src.analyzer.threshold import ThresholdBreach
from src.db.models import NotifyChannel

pytestmark = pytest.mark.unit


def _make_settings(grafana_base_url="http://grafana:3000",
                   grafana_dashboard_uid="abc123", email_app_name="ARS"):
    settings = MagicMock()
    settings.grafana_base_url = grafana_base_url
    settings.grafana_dashboard_uid = grafana_dashboard_uid
    settings.email_app_name = email_app_name
    return settings


def _breach(**over):
    base = {
        "eqp_id": "EQP01", "proc": "@system", "rule_id": "cpu_warn",
        "fact": "cpu.max", "category": "cpu", "op": ">=", "current_value": 92.5,
        "threshold_value": 80.0, "severity": "WARNING",
    }
    base.update(over)
    return ThresholdBreach(**base)


_EQP = {"localpc": "HOST-01", "ipAddr": "10.0.0.1", "eqpModel": "MODEL-X", "line": "LINE-A"}


class TestBuildAlertRequest:
    def test_field_mapping(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=15,
        )
        assert req.hostname == "HOST-01"
        assert req.ip == "10.0.0.1"
        assert req.eqp_model == "MODEL-X"
        assert req.line == "LINE-A"
        assert req.process == "CVD"

    def test_category_from_breach_uppercased(self):
        req = build_alert_request(
            _breach(category="memory"), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=15,
        )
        assert req.variables["Category"] == "MEMORY"
        assert req.subcode == "MEMORY_WARNING"

    def test_subcode_default_category_severity(self):
        req = build_alert_request(
            _breach(severity="CRITICAL"), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=15,
        )
        assert req.subcode == "CPU_CRITICAL"

    def test_subcode_override_from_notify(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30, email_subcode="PAGER"),
            window_minutes=15,
        )
        assert req.subcode == "PAGER"

    def test_code_from_notify_channel(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30, email_code="CUSTOM_CODE"),
            window_minutes=15,
        )
        assert req.code == "CUSTOM_CODE"

    def test_variables_keys_and_values(self):
        req = build_alert_request(
            _breach(fact="cpu.p95", current_value=88.0, threshold_value=85.0,
                    severity="CRITICAL"),
            _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=10,
        )
        assert set(req.variables) == {
            "Severity", "Category", "MetricName",
            "CurrentValue", "Threshold", "WindowMin", "GrafanaUrl",
        }
        assert req.variables["MetricName"] == "cpu.p95"
        assert req.variables["CurrentValue"] == "88.0"
        assert req.variables["Threshold"] == "85.0"
        assert req.variables["WindowMin"] == "10"

    def test_grafana_url_built(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=5,
        )
        assert req.variables["GrafanaUrl"] == (
            "http://grafana:3000/d/abc123?var-eqpId=EQP01&var-process=CVD"
        )

    def test_grafana_url_empty_when_no_uid(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(grafana_dashboard_uid=""),
            NotifyChannel(cooldown_minutes=30), window_minutes=5,
        )
        assert req.variables["GrafanaUrl"] == ""

    def test_app_from_settings(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(email_app_name="CUSTOM_APP"),
            NotifyChannel(cooldown_minutes=30), window_minutes=5,
        )
        assert req.app == "CUSTOM_APP"


class TestMakeCooldownKey:
    def test_tuple_shape(self):
        key = make_cooldown_key("CVD", _breach(proc="@system"), "default")
        assert key == ("CVD", "EQP01", "@system", "default", "WARNING")

    def test_proc_and_severity_in_key(self):
        key = make_cooldown_key("CVD", _breach(proc="svc", severity="CRITICAL"), "pager")
        assert key == ("CVD", "EQP01", "svc", "pager", "CRITICAL")
