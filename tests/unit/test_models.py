"""Tests for src.db.models — Scope mapping + v2 MonitorProfile aggregate.

The v2 building-block models (Fact/Measure/Condition/Rule/NotifyChannel) are
covered in test_models_v2.py; this module covers Scope (unchanged from v1) and
the v2 aggregate root + to_mongo/from_mongo roundtrip + cascade fold +
effective reference-integrity validation.
"""
from datetime import UTC, datetime

import pytest
from bson import ObjectId

from src.db.models import (
    Condition,
    Fact,
    Measure,
    MonitorProfile,
    NotifyChannel,
    Rule,
    Scope,
    fold_profiles,
    validate_effective,
)

pytestmark = pytest.mark.unit


class TestScopeMapping:
    def test_default_wildcard_scope(self):
        s = Scope(process="*")
        assert s.process == "*"
        assert s.eqp_model == "*"
        assert s.eqp_id == "*"

    def test_json_alias_model_maps_to_eqp_model(self):
        """External JSON uses `model` and `eqpId`; internal uses `eqp_model`/`eqp_id`."""
        s = Scope.model_validate({"process": "CVD", "model": "ABC123", "eqpId": "E1"})
        assert s.eqp_model == "ABC123"
        assert s.eqp_id == "E1"

    def test_to_mongo_query_includes_eqp_model_not_model(self):
        """EQP_INFO uses `eqpModel` (not `model`)."""
        s = Scope(process="CVD", eqp_model="ABC123", eqp_id="E1")
        q = s.to_mongo_query()
        assert q == {"process": "CVD", "eqpModel": "ABC123", "eqpId": "E1"}
        assert "model" not in q  # critical: don't leak Pydantic alias

    def test_to_mongo_query_skips_wildcards(self):
        """Wildcard fields are not added to the Mongo query."""
        s = Scope(process="CVD")
        q = s.to_mongo_query()
        assert q == {"process": "CVD"}

    def test_scope_equality(self):
        s1 = Scope(process="CVD", eqp_model="M", eqp_id="E1")
        s2 = Scope(process="CVD", eqp_model="M", eqp_id="E1")
        assert s1 == s2


def _make_profile(**over) -> MonitorProfile:
    base = {
        "scope": Scope(process="CVD", eqp_model="ABC", eqp_id="*"),
        "measures": [
            Measure(
                id="cpu",
                category="cpu",
                metric="total_used_pct",
                window_minutes=15,
                facts=[Fact(type="max"), Fact(type="avg")],
            )
        ],
        "rules": [
            Rule(
                id="cpu_warn",
                interval_minutes=5,
                severity="WARNING",
                when=[Condition(fact="cpu.max", op=">=", value=80)],
            )
        ],
        "notify": {"default": NotifyChannel(cooldown_minutes=30)},
    }
    base.update(over)
    return MonitorProfile(**base)


class TestMonitorProfileRoundtrip:
    def test_to_mongo_keys(self):
        doc = _make_profile().to_mongo()
        assert set(doc) == {"scope", "enabled", "governance", "measures", "rules", "notify"}
        assert "_id" not in doc

    def test_to_mongo_from_mongo_roundtrip(self):
        p = _make_profile()
        restored = MonitorProfile.from_mongo(p.to_mongo())
        assert restored.scope == p.scope
        assert [m.id for m in restored.measures] == ["cpu"]
        assert [r.id for r in restored.rules] == ["cpu_warn"]
        assert restored.notify["default"].cooldown_minutes == 30
        assert restored.measures[0].facts[0].type.value == "max"

    def test_to_mongo_uses_eqp_model_field_name(self):
        doc = _make_profile().to_mongo()
        assert doc["scope"]["eqpModel"] == "ABC"
        assert "model" not in doc["scope"]

    def test_governance_roundtrips(self):
        restored = MonitorProfile.from_mongo(_make_profile().to_mongo())
        assert restored.governance.version == 1

    def test_from_mongo_strips_objectid_into_str_id(self):
        doc = _make_profile().to_mongo()
        doc["_id"] = ObjectId()
        doc["created_at"] = datetime.now(UTC)
        restored = MonitorProfile.from_mongo(doc)
        assert isinstance(restored.id, str)

    def test_from_mongo_without_id(self):
        restored = MonitorProfile.from_mongo(_make_profile().to_mongo())
        assert restored.id is None

    def test_duplicate_measure_id_rejected(self):
        with pytest.raises(ValueError, match="duplicate measure id"):
            _make_profile(
                measures=[
                    Measure(id="cpu", category="cpu", metric="x", window_minutes=15,
                            facts=[Fact(type="max")]),
                    Measure(id="cpu", category="cpu", metric="y", window_minutes=15,
                            facts=[Fact(type="avg")]),
                ]
            )

    def test_duplicate_rule_id_rejected(self):
        with pytest.raises(ValueError, match="duplicate rule id"):
            _make_profile(
                rules=[
                    Rule(id="r", interval_minutes=5, severity="WARNING",
                         when=[Condition(fact="cpu.max", op=">=", value=80)]),
                    Rule(id="r", interval_minutes=5, severity="CRITICAL",
                         when=[Condition(fact="cpu.max", op=">=", value=95)]),
                ]
            )


class TestEffectiveSignature:
    def test_signature_ignores_scope_and_governance(self):
        a = _make_profile(scope=Scope(process="A"))
        b = _make_profile(scope=Scope(process="B"))
        assert a.effective_signature() == b.effective_signature()

    def test_signature_changes_with_rules(self):
        a = _make_profile()
        b = _make_profile(rules=[])
        assert a.effective_signature() != b.effective_signature()

    def test_structural_mongo_excludes_governance(self):
        assert "governance" not in _make_profile().structural_mongo()


class TestCascadeFold:
    def test_overlay_adds_rule_referencing_parent_measure(self):
        glob = _make_profile(scope=Scope(process="*"))
        overlay = MonitorProfile(
            scope=Scope(process="CVD", eqp_id="E1"),
            rules=[
                Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
                     when=[Condition(fact="cpu.max", op=">=", value=95)])
            ],
        )
        eff = fold_profiles([glob, overlay], overlay.scope)
        assert {r.id for r in eff.rules} == {"cpu_warn", "cpu_crit"}
        assert [m.id for m in eff.measures] == ["cpu"]
        assert validate_effective(eff) == []

    def test_specific_replaces_whole_object(self):
        glob = _make_profile(scope=Scope(process="*"))
        overlay = MonitorProfile(
            scope=Scope(process="CVD"),
            rules=[
                Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=70)])
            ],
        )
        eff = fold_profiles([glob, overlay], overlay.scope)
        warn = next(r for r in eff.rules if r.id == "cpu_warn")
        assert warn.when[0].value == 70  # overlay won whole-object

    def test_enabled_folds_with_and(self):
        a = MonitorProfile(scope=Scope(process="*"), enabled=True)
        b = MonitorProfile(scope=Scope(process="P"), enabled=False)
        assert fold_profiles([a, b], b.scope).enabled is False


class TestValidateEffective:
    def test_valid_profile_no_errors(self):
        assert validate_effective(_make_profile()) == []

    def test_missing_measure(self):
        p = _make_profile(
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="ghost.max", op=">=", value=1)])]
        )
        errs = validate_effective(p)
        assert any("measure 'ghost' not found" in e for e in errs)

    def test_undeclared_fact(self):
        p = _make_profile(
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.p99", op=">=", value=1)])]
        )
        assert any("declares no fact 'p99'" in e for e in validate_effective(p))

    def test_disallowed_op(self):
        p = _make_profile(
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.max", op="<", value=1)])]
        )
        assert any("not allowed for fact 'max'" in e for e in validate_effective(p))

    def test_undefined_notify_channel(self):
        p = _make_profile(
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        notify="pager",
                        when=[Condition(fact="cpu.max", op=">=", value=80)])]
        )
        assert any("channel 'pager' is not defined" in e for e in validate_effective(p))

    def test_interval_exceeds_window(self):
        p = _make_profile(
            rules=[Rule(id="r", interval_minutes=99, severity="WARNING",
                        when=[Condition(fact="cpu.max", op=">=", value=80)])]
        )
        assert any("exceeds measure 'cpu'.window_minutes" in e for e in validate_effective(p))

    def test_rule_spanning_differing_proc_rejected(self):
        # cpu (proc=@system) + proc_req (proc=*) in one AND rule → engine could
        # never fire it (disjoint proc keys); validation must reject.
        p = _make_profile(
            measures=[
                Measure(id="cpu", category="cpu", metric="total_used_pct",
                        window_minutes=15, facts=[Fact(type="max")]),
                Measure(id="proc_req", category="process_watch", metric="required",
                        proc="*", window_minutes=15, facts=[Fact(type="min")]),
            ],
            rules=[Rule(id="r", interval_minutes=5, severity="CRITICAL", combine="AND",
                        when=[Condition(fact="cpu.max", op=">=", value=80),
                              Condition(fact="proc_req.min", op="==", value=0)])],
        )
        assert any("differing proc" in e for e in validate_effective(p))

    def test_rule_same_proc_measures_ok(self):
        # two scalar @system measures combined is fine
        p = _make_profile(
            measures=[
                Measure(id="cpu", category="cpu", metric="total_used_pct",
                        window_minutes=15, facts=[Fact(type="max")]),
                Measure(id="mem", category="memory", metric="total_used_pct",
                        window_minutes=15, facts=[Fact(type="max")]),
            ],
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING", combine="AND",
                        when=[Condition(fact="cpu.max", op=">=", value=80),
                              Condition(fact="mem.max", op=">=", value=80)])],
        )
        assert validate_effective(p) == []
