"""Tests for src.analyzer.alert_builder — v2 alert payload construction."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.analyzer.alert_builder import (
    build_alert_request,
    make_cooldown_key,
    resolve_group_value,
)
from src.analyzer.threshold import ThresholdBreach
from src.db.models import NotifyChannel

pytestmark = pytest.mark.unit


def _make_settings(grafana_base_url="http://grafana:3000",
                   grafana_dashboard_uid="abc123", email_app_name="ARS",
                   rms_custom_body_enabled=False, rms_erb_row_limit=50,
                   rms_body_byte_cap=256000):
    settings = MagicMock()
    settings.grafana_base_url = grafana_base_url
    settings.grafana_dashboard_uid = grafana_dashboard_uid
    settings.email_app_name = email_app_name
    # explicit so the MagicMock doesn't return a truthy flag and accidentally
    # trigger the custom-body path in legacy tests
    settings.rms_custom_body_enabled = rms_custom_body_enabled
    settings.rms_erb_row_limit = rms_erb_row_limit
    settings.rms_body_byte_cap = rms_body_byte_cap
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
        assert req.hostname == "EQP01"  # eqpId, NOT localpc — Akka가 hostname을 eqpId로 취급
        assert req.hostname != "HOST-01"  # localpc 아님 (eqpId 회귀 가드)
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

    def test_group_value_replaces_eqp_position(self):
        # group send: the eqp slot carries the group identifier instead
        key = make_cooldown_key("CVD", _breach(), "default", group_value="MODEL-X")
        assert key == ("CVD", "MODEL-X", "@system", "default", "WARNING")

    def test_group_value_none_falls_back_to_eqp(self):
        key = make_cooldown_key("CVD", _breach(), "default", group_value=None)
        assert key == ("CVD", "EQP01", "@system", "default", "WARNING")


class TestResolveGroupValue:
    def test_eqp_uses_eqp_id(self):
        b = _breach(eqp_id="EQP07")
        assert resolve_group_value("eqp", b, {"eqpModel": "MODEL-X"}, "CVD") == "EQP07"

    def test_model_uses_eqp_model(self):
        b = _breach(eqp_id="EQP07")
        assert resolve_group_value("model", b, {"eqpModel": "MODEL-X"}, "CVD") == "MODEL-X"

    def test_process_uses_process(self):
        b = _breach(eqp_id="EQP07")
        assert resolve_group_value("process", b, {"eqpModel": "MODEL-X"}, "CVD") == "CVD"


class TestAffectedEquipment:
    def test_affected_adds_variables(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30, group_by="model"),
            window_minutes=15, affected_equipment=["EQP01", "EQP02", "EQP03"],
        )
        assert req.variables["AffectedEquipment"] == "EQP01, EQP02, EQP03"
        assert req.variables["AffectedCount"] == "3"

    def test_no_affected_keeps_variable_set_unchanged(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=15,
        )
        assert "AffectedEquipment" not in req.variables
        assert "AffectedCount" not in req.variables


class TestRenderedBody:
    _TS = datetime(2026, 6, 9, 5, 5, tzinfo=UTC)  # 14:05 KST

    def test_flag_off_no_rendered_body(self):
        req = build_alert_request(
            _breach(), _EQP, "CVD", _make_settings(),
            NotifyChannel(cooldown_minutes=30), window_minutes=15,
        )
        assert req.rendered_body is None
        assert req.title is None
        assert "renderedBody" not in req.to_payload()

    def test_flag_on_template_renders_body_and_title(self):
        s = _make_settings(rms_custom_body_enabled=True)
        template = {"html": "<p>@Hostname @CurrentValue</p>",
                    "title": "[EARS] @Category @Severity"}
        b = _breach(eqp_id="EQP01", current_value=92.5)
        req = build_alert_request(
            b, _EQP, "CVD", s, NotifyChannel(cooldown_minutes=30),
            window_minutes=15, members=[b], eqp_lookup={"EQP01": _EQP},
            timestamp=self._TS, template=template,
        )
        assert req.rendered_body == "<p>EQP01 92.5</p>"
        assert req.title == "[EARS] CPU WARNING"

    def test_template_miss_uses_default_body(self):
        s = _make_settings(rms_custom_body_enabled=True)
        b = _breach(eqp_id="EQP01")
        req = build_alert_request(
            b, _EQP, "CVD", s, NotifyChannel(cooldown_minutes=30),
            window_minutes=15, members=[b], eqp_lookup={"EQP01": _EQP},
            timestamp=self._TS, template=None,
        )
        assert req.rendered_body is not None
        assert "임계 초과" in req.rendered_body  # DEFAULT_BODY marker
        assert req.title  # DEFAULT_TITLE fallback

    def test_render_error_falls_back_to_default(self):
        s = _make_settings(rms_custom_body_enabled=True)
        b = _breach(eqp_id="EQP01")
        # malformed: ERB start without end → render_body raises → fallback
        template = {"html": "<table><!--@EachEquipment--><tr>@Row.EqpId</tr>",
                    "title": "T"}
        req = build_alert_request(
            b, _EQP, "CVD", s, NotifyChannel(cooldown_minutes=30),
            window_minutes=15, members=[b], eqp_lookup={"EQP01": _EQP},
            timestamp=self._TS, template=template,
        )
        assert "임계 초과" in req.rendered_body  # fell back to DEFAULT_BODY

    def test_derived_tokens_bound(self):
        s = _make_settings(rms_custom_body_enabled=True)
        template = {
            "html": "op=@Operator gb=@GroupBy gv=@GroupValue ts=@Timestamp fact=@Fact",
            "title": "T",
        }
        b = _breach(eqp_id="EQP01", op=">=", fact="cpu.max")
        req = build_alert_request(
            b, _EQP, "CVD", s, NotifyChannel(cooldown_minutes=30, group_by="model"),
            window_minutes=15, members=[b], eqp_lookup={"EQP01": _EQP},
            timestamp=self._TS, template=template,
        )
        body = req.rendered_body
        assert "gb=model" in body
        assert "gv=MODEL-X" in body  # resolve_group_value(model) → eqpModel
        assert "fact=cpu.max" in body
        assert "ts=2026-06-09 14:05 KST" in body  # pinned KST format
        assert "op=&gt;=" in body  # @Operator '>=' html-escaped

    def test_group_erb_renders_ordered_rows(self):
        s = _make_settings(rms_custom_body_enabled=True)
        template = {
            "html": "<table><!--@EachEquipment--><tr><td>@Row.EqpId</td></tr>"
                    "<!--@EndEachEquipment--></table>",
            "title": "T",
        }
        b1 = _breach(eqp_id="EQP01", severity="WARNING", current_value=88.0)
        b2 = _breach(eqp_id="EQP02", severity="CRITICAL", current_value=95.0)
        req = build_alert_request(
            b1, _EQP, "CVD", s, NotifyChannel(cooldown_minutes=30, group_by="model"),
            window_minutes=15, members=[b1, b2],
            eqp_lookup={"EQP01": _EQP, "EQP02": _EQP},
            timestamp=self._TS, affected_equipment=["EQP01", "EQP02"],
            template=template,
        )
        body = req.rendered_body
        assert body.count("<tr>") == 2
        assert body.index("EQP02") < body.index("EQP01")  # CRITICAL first
