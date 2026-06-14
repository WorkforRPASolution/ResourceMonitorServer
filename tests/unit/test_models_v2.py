"""v2 building-block models: Fact / Measure / Condition / Rule / NotifyChannel.

These are added additively alongside the v1 model during migration. The
cross-object validators (op↔fact, interval≤window, reference integrity) live on
the v2 MonitorProfile aggregate and are tested separately once that lands.
"""
import pytest
from pydantic import ValidationError

from src.analyzer.fact_catalog import FactType
from src.db.models import (
    Bucketing,
    Condition,
    Fact,
    Governance,
    Measure,
    NotifyChannel,
    Rule,
)

pytestmark = pytest.mark.unit


class TestFact:
    def test_simple_fact(self):
        f = Fact(type="max")
        assert f.type is FactType.MAX

    def test_spike_count_requires_over_and_direction(self):
        with pytest.raises(ValidationError):
            Fact(type="spike_count")  # missing over + direction
        f = Fact(type="spike_count", over=90, direction="above")
        assert f.over == 90 and f.direction == "above"

    def test_duration_requires_over_and_direction(self):
        with pytest.raises(ValidationError):
            Fact(type="duration", over=80)  # missing direction

    def test_delta_defaults_mode(self):
        assert Fact(type="delta").mode == "last_minus_first"

    def test_growth_rate_defaults_unit(self):
        assert Fact(type="growth_rate").unit == "per_hour"

    def test_zscore_defaults_direction_high(self):
        assert Fact(type="zscore").direction == "high"

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            Fact(type="max", bogus=1)


class TestMeasure:
    def _facts(self, *types):
        return [Fact(type=t) for t in types]

    def test_valid_scalar_measure(self):
        m = Measure(
            id="cpu", category="cpu", metric="total_used_pct",
            window_minutes=15, facts=[Fact(type="max")],
        )
        assert m.proc == "@system" and m.expand == "scalar" and m.group_by == ["eqpId"]

    def test_duplicate_fact_type_rejected(self):
        with pytest.raises(ValidationError):
            Measure(id="cpu", category="cpu", metric="x", window_minutes=15,
                    facts=[Fact(type="max"), Fact(type="max")])

    def test_moving_avg_requires_bucketing_points(self):
        with pytest.raises(ValidationError):
            Measure(id="m", category="memory", metric="x", window_minutes=60,
                    facts=[Fact(type="moving_avg")])  # no bucketing
        m = Measure(id="m", category="memory", metric="x", window_minutes=60,
                    bucketing=Bucketing(seconds=300, points=6),
                    facts=[Fact(type="moving_avg")])
        assert m.bucketing.points == 6

    def test_duration_requires_bucketing(self):
        with pytest.raises(ValidationError):
            Measure(id="m", category="cpu", metric="x", window_minutes=15,
                    facts=[Fact(type="duration", over=80, direction="above")])

    def test_baseline_dev_requires_baseline(self):
        with pytest.raises(ValidationError):
            Measure(id="m", category="cpu", metric="x", window_minutes=15,
                    facts=[Fact(type="baseline_dev")])

    def test_points_times_seconds_must_fit_window(self):
        with pytest.raises(ValidationError):
            Measure(id="m", category="memory", metric="x", window_minutes=10,
                    bucketing=Bucketing(seconds=300, points=10),  # 3000s > 600s
                    facts=[Fact(type="moving_avg")])

    def test_wildcard_proc_auto_groups_by_proc(self):
        m = Measure(id="pw", category="process_watch", metric="required",
                    proc="*", window_minutes=5, facts=[Fact(type="min")])
        assert m.group_by == ["eqpId", "proc"]

    def test_wildcard_metric_auto_expands_instance(self):
        m = Measure(id="disk", category="disk", metric="*",
                    window_minutes=30, facts=[Fact(type="max")])
        assert m.expand == "instance"


class TestCondition:
    def test_valid(self):
        c = Condition(fact="cpu.max", op=">=", value=80)
        assert c.quantifier == "any"

    def test_count_requires_count_min(self):
        with pytest.raises(ValidationError):
            Condition(fact="disk.max", op=">=", value=85, quantifier="count")
        c = Condition(fact="disk.max", op=">=", value=85, quantifier="count", count_min=3)
        assert c.count_min == 3

    def test_count_min_must_be_positive(self):
        # count_min=0 would make 'n >= 0' always true → silent always-alert
        with pytest.raises(ValidationError):
            Condition(fact="disk.max", op=">=", value=85, quantifier="count", count_min=0)
        with pytest.raises(ValidationError):
            Condition(fact="disk.max", op=">=", value=85, quantifier="count", count_min=-1)

    def test_trend_op_requires_string_value(self):
        c = Condition(fact="m.trend", op="trend==", value="increasing")
        assert c.value == "increasing"
        with pytest.raises(ValidationError):
            Condition(fact="m.trend", op="trend==", value=1)

    def test_numeric_op_rejects_string_value(self):
        with pytest.raises(ValidationError):
            Condition(fact="cpu.max", op=">=", value="80")

    def test_fact_must_be_dotted(self):
        with pytest.raises(ValidationError):
            Condition(fact="cpumax", op=">=", value=80)


class TestRuleAndNotify:
    def test_valid_rule(self):
        r = Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        assert r.combine == "AND" and r.notify == "default"

    def test_rule_enabled_defaults_true(self):
        r = Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        assert r.enabled is True

    def test_rule_can_be_disabled(self):
        r = Rule(id="cpu_warn", interval_minutes=5, severity="WARNING", enabled=False,
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        assert r.enabled is False

    def test_rule_when_must_be_nonempty(self):
        with pytest.raises(ValidationError):
            Rule(id="x", interval_minutes=5, severity="WARNING", when=[])

    def test_notify_channel_defaults(self):
        n = NotifyChannel(cooldown_minutes=30)
        assert n.email_code == "RESOURCE_MONITOR" and n.email_subcode is None

    def test_notify_group_by_defaults_eqp(self):
        n = NotifyChannel(cooldown_minutes=30)
        assert n.group_by == "eqp" and n.email_group is None

    def test_notify_group_by_accepts_model_and_process(self):
        assert NotifyChannel(cooldown_minutes=30, group_by="model").group_by == "model"
        assert NotifyChannel(cooldown_minutes=30, group_by="process").group_by == "process"

    def test_notify_group_by_rejects_unknown(self):
        with pytest.raises(ValidationError):
            NotifyChannel(cooldown_minutes=30, group_by="rack")

    def test_notify_email_group_field(self):
        n = NotifyChannel(cooldown_minutes=30, group_by="model",
                          email_group="TEAM1")
        assert n.email_group == "TEAM1"

    def test_notify_rejects_legacy_representatives(self):
        # representatives was removed; extra="forbid" rejects it at construction
        # (from_mongo strips it for stored docs — see test_models.py)
        with pytest.raises(ValidationError):
            NotifyChannel(cooldown_minutes=30, representatives={"MODEL_A": "EQP001"})

    def test_governance_defaults(self):
        g = Governance()
        assert g.version == 1 and g.updated_by == ""
