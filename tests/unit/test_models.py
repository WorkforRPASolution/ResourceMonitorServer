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
    lint_effective,
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

    def test_rule_enabled_roundtrips(self):
        p = _make_profile(
            rules=[Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                        enabled=False,
                        when=[Condition(fact="cpu.max", op=">=", value=80)])]
        )
        doc = p.to_mongo()
        assert doc["rules"][0]["enabled"] is False
        restored = MonitorProfile.from_mongo(doc)
        assert restored.rules[0].enabled is False

    def test_legacy_rule_without_enabled_defaults_true(self):
        """Existing Mongo docs predate rule.enabled — loading must default True."""
        doc = _make_profile().to_mongo()
        doc["rules"][0].pop("enabled", None)  # simulate legacy document
        restored = MonitorProfile.from_mongo(doc)
        assert restored.rules[0].enabled is True

    def test_notify_group_by_roundtrips(self):
        p = _make_profile(notify={"default": NotifyChannel(
            cooldown_minutes=30, group_by="model", email_group="TEAM1")})
        doc = p.to_mongo()
        assert doc["notify"]["default"]["group_by"] == "model"
        restored = MonitorProfile.from_mongo(doc)
        assert restored.notify["default"].group_by == "model"
        assert restored.notify["default"].email_group == "TEAM1"

    def test_legacy_notify_without_group_by_defaults_eqp(self):
        """Existing Mongo docs predate notify.group_by — loading must default eqp."""
        doc = _make_profile().to_mongo()
        doc["notify"]["default"].pop("group_by", None)
        restored = MonitorProfile.from_mongo(doc)
        assert restored.notify["default"].group_by == "eqp"
        assert restored.notify["default"].email_group is None

    def test_from_mongo_strips_legacy_representatives(self):
        """Pre-existing docs carry the removed ``representatives`` field; with
        NotifyChannel ``extra="forbid"`` it must be stripped on load (no
        migration), not raise ValidationError."""
        doc = _make_profile(notify={"default": NotifyChannel(
            cooldown_minutes=30, group_by="model")}).to_mongo()
        # simulate a legacy stored doc that still has the dropped field
        doc["notify"]["default"]["representatives"] = {"MODEL_A": "EQP001"}
        restored = MonitorProfile.from_mongo(doc)  # must not raise
        assert restored.notify["default"].group_by == "model"
        assert restored.notify["default"].email_group is None
        assert not hasattr(restored.notify["default"], "representatives")

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

    def test_signature_changes_with_rule_enabled(self):
        # toggling a rule's enabled changes behaviour → must change the bucket
        a = _make_profile()
        b = _make_profile(
            rules=[Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                        enabled=False,
                        when=[Condition(fact="cpu.max", op=">=", value=80)])]
        )
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

    def test_enabled_most_specific_wins(self):
        """``enabled`` follows the same cascade rule as every other field:
        the most-specific document's value wins (no AND kill-switch).
        회귀(2026-06-12 사고): 전역 off + eqp overlay on → effective는 켜져야 한다."""
        root_on = MonitorProfile(scope=Scope(process="*"), enabled=True)
        proc_off = MonitorProfile(scope=Scope(process="P"), enabled=False)
        eqp_on = MonitorProfile(
            scope=Scope(process="P", eqp_model="M", eqp_id="E1"), enabled=True
        )
        root_off = MonitorProfile(scope=Scope(process="*"), enabled=False)

        # 구체 scope의 off는 그대로 끈다 (기존 동작 유지)
        assert fold_profiles([root_on, proc_off], proc_off.scope).enabled is False
        # 사고 케이스: 꺼진 조상 밑에서도 구체 scope의 on이 이긴다
        assert fold_profiles([root_off, eqp_on], eqp_on.scope).enabled is True
        # 3단 케이스: 가장 구체적인 값이 이긴다
        assert fold_profiles([root_on, proc_off, eqp_on], eqp_on.scope).enabled is True
        # 단일 문서는 자기 값 그대로
        assert fold_profiles([root_off], root_off.scope).enabled is False

    def test_overlay_disables_inherited_rule(self):
        """Soft tombstone: an overlay re-declaring a rule id with enabled=False
        mutes that rule for the narrower scope (whole-object replace)."""
        glob = _make_profile(scope=Scope(process="*"))  # cpu_warn enabled
        overlay = MonitorProfile(
            scope=Scope(process="CVD", eqp_id="E1"),
            rules=[
                Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                     enabled=False,
                     when=[Condition(fact="cpu.max", op=">=", value=80)])
            ],
        )
        eff = fold_profiles([glob, overlay], overlay.scope)
        warn = next(r for r in eff.rules if r.id == "cpu_warn")
        assert warn.enabled is False


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

    def test_disabled_rule_still_validated(self):
        """Strict policy: a disabled rule is still reference-checked so that
        re-enabling it later is always safe (broken ref rejected at write)."""
        p = _make_profile(
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        enabled=False,
                        when=[Condition(fact="ghost.max", op=">=", value=1)])]
        )
        errs = validate_effective(p)
        assert any("measure 'ghost' not found" in e for e in errs)

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

class TestLintEffective:
    """Non-fatal hygiene warnings (SCHEMA §5 items 8/9) — never reject."""

    def test_clean_profile_no_warnings(self):
        # every declared fact referenced, no gauge+delta misuse
        p = _make_profile(
            measures=[
                Measure(id="cpu", category="cpu", metric="total_used_pct",
                        window_minutes=15, facts=[Fact(type="max")]),
            ],
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.max", op=">=", value=80)])],
        )
        assert lint_effective(p) == []

    def test_dead_fact_warned(self):
        # measure 'cpu' declares max+avg but only cpu.max is referenced → avg dead
        p = _make_profile(
            measures=[
                Measure(id="cpu", category="cpu", metric="total_used_pct",
                        window_minutes=15, facts=[Fact(type="max"), Fact(type="avg")]),
            ],
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.max", op=">=", value=80)])],
        )
        warns = lint_effective(p)
        assert any("cpu.avg" in w and "no rule" in w for w in warns)

    def test_fact_referenced_only_by_disabled_rule_not_dead(self):
        # strict policy: a disabled rule still "references" its fact, so the fact
        # is not flagged dead (re-enabling the rule keeps it valid).
        p = _make_profile(
            measures=[
                Measure(id="cpu", category="cpu", metric="total_used_pct",
                        window_minutes=15, facts=[Fact(type="max")]),
            ],
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING", enabled=False,
                        when=[Condition(fact="cpu.max", op=">=", value=80)])],
        )
        assert not any("cpu.max" in w and "no rule" in w for w in lint_effective(p))

    def test_gauge_with_delta_warned(self):
        # delta/growth_rate on a gauge metric is almost always a modeling mistake
        p = _make_profile(
            measures=[
                Measure(id="cpu", category="cpu", metric="total_used_pct",
                        metric_kind="gauge", window_minutes=15,
                        facts=[Fact(type="max"), Fact(type="delta")]),
            ],
            rules=[
                Rule(id="r1", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=80)]),
                Rule(id="r2", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.delta", op=">", value=0)]),
            ],
        )
        warns = lint_effective(p)
        assert any("gauge" in w and "delta" in w for w in warns)

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
