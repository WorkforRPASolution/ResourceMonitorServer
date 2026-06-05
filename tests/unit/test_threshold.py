"""Tests for src.analyzer.threshold — v2 rule evaluation."""
from __future__ import annotations

import pytest

from src.analyzer.threshold import evaluate_condition, evaluate_rule, op_compare
from src.db.models import Condition, Rule

pytestmark = pytest.mark.unit

_CAT = {"cpu": "cpu", "disk": "disk", "proc": "process_watch", "volt": "voltage"}


def _rule(*conditions, combine="AND", severity="WARNING", rid="r"):
    return Rule(
        id=rid, interval_minutes=5, severity=severity, combine=combine,
        when=list(conditions),
    )


def _eval(rule, facts):
    return evaluate_rule(
        rule, facts, eqp_id="E1", proc="@system", measure_category=_CAT
    )


class TestOpCompare:
    @pytest.mark.parametrize("op,a,b,expected", [
        (">=", 95, 95, True), (">=", 94, 95, False),
        (">", 96, 95, True), (">", 95, 95, False),
        ("<=", 5, 5, True), ("<", 4, 5, True),
        ("==", 0, 0, True), ("!=", 1, 0, True),
    ])
    def test_numeric(self, op, a, b, expected):
        assert op_compare(a, op, b) is expected

    def test_none_value_is_false(self):
        assert op_compare(None, ">=", 80) is False

    def test_trend_string_equality(self):
        assert op_compare("increasing", "trend==", "increasing") is True
        assert op_compare("stable", "trend==", "increasing") is False


class TestEvaluateCondition:
    def test_any_one_instance_passes(self):
        cond = Condition(fact="disk.max", op=">=", value=95, quantifier="any")
        passed, rep = evaluate_condition(cond, [80.0, 96.0, 50.0])
        assert passed is True and rep == 96.0

    def test_all_requires_every_instance(self):
        cond = Condition(fact="disk.max", op=">=", value=95, quantifier="all")
        assert evaluate_condition(cond, [96.0, 97.0])[0] is True
        assert evaluate_condition(cond, [96.0, 50.0])[0] is False

    def test_all_empty_is_false(self):
        cond = Condition(fact="disk.max", op=">=", value=95, quantifier="all")
        assert evaluate_condition(cond, [None])[0] is False

    def test_count_min(self):
        cond = Condition(fact="disk.max", op=">=", value=95, quantifier="count", count_min=3)
        assert evaluate_condition(cond, [96, 97, 98])[0] is True
        assert evaluate_condition(cond, [96, 97, 50])[0] is False

    def test_low_op_rep_is_min(self):
        cond = Condition(fact="fan.min", op="<=", value=300, quantifier="any")
        passed, rep = evaluate_condition(cond, [250.0, 280.0, 999.0])
        assert passed is True and rep == 250.0


class TestEvaluateRule:
    def test_single_condition_breach(self):
        rule = _rule(Condition(fact="cpu.max", op=">=", value=80))
        breach = _eval(rule, {"cpu.max": [85.0]})
        assert breach is not None
        assert breach.rule_id == "r"
        assert breach.severity == "WARNING"
        assert breach.fact == "cpu.max"
        assert breach.category == "cpu"
        assert breach.current_value == 85.0
        assert breach.threshold_value == 80
        assert breach.eqp_id == "E1" and breach.proc == "@system"

    def test_no_breach_returns_none(self):
        rule = _rule(Condition(fact="cpu.max", op=">=", value=80))
        assert _eval(rule, {"cpu.max": [50.0]}) is None

    def test_combine_and_needs_all(self):
        rule = _rule(
            Condition(fact="cpu.p95", op=">", value=80),
            Condition(fact="cpu.spike_count", op=">", value=5),
            combine="AND",
        )
        assert _eval(rule, {"cpu.p95": [85.0], "cpu.spike_count": [7]}) is not None
        assert _eval(rule, {"cpu.p95": [85.0], "cpu.spike_count": [2]}) is None

    def test_combine_or_needs_any(self):
        rule = _rule(
            Condition(fact="volt.min", op="<", value=1.1),
            Condition(fact="volt.max", op=">", value=1.4),
            combine="OR",
        )
        breach = _eval(rule, {"volt.min": [1.5], "volt.max": [1.45]})
        assert breach is not None and breach.fact == "volt.max"

    def test_state_check_required_down_via_min_eq_zero(self):
        rule = _rule(Condition(fact="proc.min", op="==", value=0), severity="CRITICAL")
        breach = _eval(rule, {"proc.min": [0.0]})
        assert breach is not None and breach.severity == "CRITICAL"
        assert breach.category == "process_watch"

    def test_state_check_required_running_no_breach(self):
        rule = _rule(Condition(fact="proc.min", op="==", value=0))
        assert _eval(rule, {"proc.min": [1.0]}) is None

    def test_missing_fact_no_breach(self):
        rule = _rule(Condition(fact="cpu.max", op=">=", value=80))
        assert _eval(rule, {}) is None
