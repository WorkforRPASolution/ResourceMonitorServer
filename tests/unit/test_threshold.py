"""Tests for src.analyzer.threshold — threshold comparison and state checks."""
from __future__ import annotations

import pytest

from src.analyzer.threshold import evaluate_state_check, evaluate_thresholds
from src.db.models import ThresholdConfig


@pytest.mark.unit
class TestEvaluateThresholds:
    def _cfg(self, warning: float = 80.0, critical: float = 95.0) -> ThresholdConfig:
        return ThresholdConfig(warning=warning, critical=critical, cooldown_minutes=30)

    def test_no_breach_when_below_warning(self):
        eqp_metrics = {"EQP1": {"cpu_load": 50.0}}
        result = evaluate_thresholds(eqp_metrics, self._cfg(), ["cpu_load"])
        assert result == []

    def test_warning_breach_when_above_warning_below_critical(self):
        eqp_metrics = {"EQP1": {"cpu_load": 85.0}}
        result = evaluate_thresholds(eqp_metrics, self._cfg(), ["cpu_load"])
        assert len(result) == 1
        assert result[0].severity == "WARNING"
        assert result[0].current_value == 85.0
        assert result[0].threshold_value == 80.0

    def test_critical_breach_when_above_critical(self):
        eqp_metrics = {"EQP1": {"cpu_load": 98.0}}
        result = evaluate_thresholds(eqp_metrics, self._cfg(), ["cpu_load"])
        assert len(result) == 1
        assert result[0].severity == "CRITICAL"
        assert result[0].current_value == 98.0
        assert result[0].threshold_value == 95.0

    def test_critical_takes_precedence(self):
        """When value >= critical, only CRITICAL breach is reported, not WARNING."""
        eqp_metrics = {"EQP1": {"cpu_load": 95.0}}
        result = evaluate_thresholds(eqp_metrics, self._cfg(), ["cpu_load"])
        assert len(result) == 1
        assert result[0].severity == "CRITICAL"

    def test_none_value_skipped(self):
        eqp_metrics = {"EQP1": {"cpu_load": None}}
        result = evaluate_thresholds(eqp_metrics, self._cfg(), ["cpu_load"])
        assert result == []

    def test_multiple_eqps_multiple_metrics(self):
        eqp_metrics = {
            "EQP1": {"cpu_load": 85.0, "mem_used": 96.0},
            "EQP2": {"cpu_load": 50.0, "mem_used": 82.0},
        }
        result = evaluate_thresholds(
            eqp_metrics, self._cfg(), ["cpu_load", "mem_used"]
        )
        # EQP1: cpu WARNING + mem CRITICAL; EQP2: cpu none + mem WARNING
        assert len(result) == 3
        severities = {(b.eqp_id, b.metric, b.severity) for b in result}
        assert ("EQP1", "cpu_load", "WARNING") in severities
        assert ("EQP1", "mem_used", "CRITICAL") in severities
        assert ("EQP2", "mem_used", "WARNING") in severities

    def test_empty_input_returns_empty(self):
        result = evaluate_thresholds({}, self._cfg(), ["cpu_load"])
        assert result == []


@pytest.mark.unit
class TestEvaluateStateCheck:
    def test_state_check_required_down(self):
        """Required process was down (min=0) -> CRITICAL breach."""
        eqp_metrics = {"EQP1": {"required": 0.0}}
        result = evaluate_state_check(eqp_metrics, "required", expected=1.0)
        assert len(result) == 1
        assert result[0].severity == "CRITICAL"
        assert result[0].current_value == 0.0

    def test_state_check_required_running(self):
        """Required process running (min=1) -> no breach."""
        eqp_metrics = {"EQP1": {"required": 1.0}}
        result = evaluate_state_check(eqp_metrics, "required", expected=1.0)
        assert result == []

    def test_state_check_forbidden_running(self):
        """Forbidden process running (max=1) -> CRITICAL breach."""
        eqp_metrics = {"EQP1": {"forbidden": 1.0}}
        result = evaluate_state_check(eqp_metrics, "forbidden", expected=0.0)
        assert len(result) == 1
        assert result[0].severity == "CRITICAL"
        assert result[0].current_value == 1.0

    def test_state_check_forbidden_stopped(self):
        """Forbidden process stopped (max=0) -> no breach."""
        eqp_metrics = {"EQP1": {"forbidden": 0.0}}
        result = evaluate_state_check(eqp_metrics, "forbidden", expected=0.0)
        assert result == []

    def test_state_check_none_skipped(self):
        eqp_metrics = {"EQP1": {"required": None}}
        result = evaluate_state_check(eqp_metrics, "required", expected=1.0)
        assert result == []
