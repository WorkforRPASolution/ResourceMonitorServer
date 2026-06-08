"""Tests for src.analyzer.engine — v2 per-equipment orchestration."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analyzer.engine import AnalysisEngine
from src.config.settings import AppSettings
from src.db.models import (
    Condition,
    Fact,
    Measure,
    MonitorProfile,
    NotifyChannel,
    Rule,
    Scope,
)
from src.es.queries import QueryBuilder

pytestmark = pytest.mark.unit

_EQP01 = {"eqpId": "EQP01", "eqpModel": "MODEL_A", "process": "CVD",
          "localpc": "PC001", "ipAddr": "10.0.0.1", "line": "L1", "category": "MAIN"}
_EQP02 = {"eqpId": "EQP02", "eqpModel": "MODEL_B", "process": "CVD",
          "localpc": "PC002", "ipAddr": "10.0.0.2", "line": "L2", "category": "MAIN"}


def _cpu_profile(rules=None):
    return MonitorProfile(
        scope=Scope(process="*"),
        measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                          window_minutes=15, facts=[Fact(type="max")])],
        rules=rules if rules is not None else [
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        ],
        notify={"default": NotifyChannel(cooldown_minutes=30)},
    )


def _es_max(eqp_id, value):
    return {"aggregations": {"by_eqp": {"buckets": [
        {"key": eqp_id, "max": {"value": value}}
    ]}}}


def _settings():
    return AppSettings(grafana_base_url="http://grafana:3000",
                       grafana_dashboard_uid="abc123", email_app_name="ARS")


def _make_deps(profile, equipment=None, *, real_qb=False):
    deps = MagicMock()
    deps.es = MagicMock()
    deps.es.client = AsyncMock()
    deps.es.get_metric_names = AsyncMock(return_value=[])
    deps.profile_repo = AsyncMock()
    deps.profile_repo.resolve_profile = AsyncMock(return_value=profile)
    deps.eqp_info_repo = AsyncMock()
    deps.eqp_info_repo.get_active_equipment_by_process = AsyncMock(
        return_value=equipment if equipment is not None else [_EQP01]
    )
    deps.zk_lock = MagicMock()
    deps.zk_lock.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None), __aexit__=AsyncMock(return_value=False),
    ))
    deps.cooldown_mgr = AsyncMock()
    deps.cooldown_mgr.is_cooling_down_batch = AsyncMock(return_value={})
    deps.email_client = AsyncMock()
    deps.email_client.send_alert = AsyncMock(return_value=True)
    if real_qb:
        deps.query_builder = QueryBuilder(AppSettings(local_tz="Asia/Seoul"))
    else:
        deps.query_builder = MagicMock()
        deps.query_builder.resolve_index_range = MagicMock(return_value="cvd_all-2026.04.10")
        deps.query_builder.build_metric_aggregation_query = MagicMock(return_value={"size": 0})
    return deps


def _engine(deps):
    e = AnalysisEngine(deps, _settings())
    e._es_semaphore = asyncio.Semaphore(3)
    return e


class TestHappyPath:
    async def test_breach_sends_email_and_sets_cooldown(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 92.5))
        result = await _engine(deps).run_analysis("CVD", 5)
        deps.email_client.send_alert.assert_awaited_once()
        deps.cooldown_mgr.set_cooldown.assert_awaited_once_with(
            "CVD", "EQP01", "@system", "default", "WARNING", 30
        )
        assert len(result.breaches) == 1
        assert result.breaches[0].severity == "WARNING"
        assert result.breaches[0].fact == "cpu.max"

    async def test_no_breach_no_email(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 50.0))
        result = await _engine(deps).run_analysis("CVD", 5)
        deps.email_client.send_alert.assert_not_awaited()
        assert result.breaches == []

    async def test_only_rules_at_this_interval_run(self):
        profile = _cpu_profile(rules=[
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)]),
            Rule(id="cpu_slow", interval_minutes=60, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=10)]),
        ])
        deps = _make_deps(profile)
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 85.0))
        result = await _engine(deps).run_analysis("CVD", 5)
        assert result.rule_ids == ["cpu_warn"]  # cpu_slow (60m) not in this tick


class TestCooldown:
    async def test_cooldown_suppresses_email(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 92.5))
        deps.cooldown_mgr.is_cooling_down_batch = AsyncMock(return_value={
            ("CVD", "EQP01", "@system", "default", "WARNING"): True,
        })
        result = await _engine(deps).run_analysis("CVD", 5)
        assert len(result.breaches) == 1
        deps.email_client.send_alert.assert_not_awaited()
        deps.cooldown_mgr.set_cooldown.assert_not_awaited()

    async def test_send_failure_does_not_set_cooldown(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 92.5))
        deps.email_client.send_alert = AsyncMock(return_value=False)
        await _engine(deps).run_analysis("CVD", 5)
        deps.email_client.send_alert.assert_awaited_once()
        deps.cooldown_mgr.set_cooldown.assert_not_awaited()


class TestEdgeCases:
    async def test_no_equipment_returns_early(self):
        deps = _make_deps(_cpu_profile(), equipment=[])
        result = await _engine(deps).run_analysis("CVD", 5)
        deps.es.client.search.assert_not_awaited()
        assert result.breaches == []

    async def test_eqp_not_in_lookup_skipped(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP99", 92.5))
        result = await _engine(deps).run_analysis("CVD", 5)
        assert len(result.breaches) == 1  # breach detected
        deps.email_client.send_alert.assert_not_awaited()  # but eqp unknown → skip

    async def test_disabled_profile_skipped(self):
        prof = _cpu_profile()
        prof.enabled = False
        deps = _make_deps(prof)
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 99.0))
        result = await _engine(deps).run_analysis("CVD", 5)
        assert result.breaches == []
        deps.es.client.search.assert_not_awaited()

    async def test_disabled_rule_skipped(self):
        # the only rule at this interval is disabled → no evaluation, no email,
        # and the measure it references is never queried (rules filtered first).
        profile = _cpu_profile(rules=[
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING", enabled=False,
                 when=[Condition(fact="cpu.max", op=">=", value=80)]),
        ])
        deps = _make_deps(profile)
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 99.0))
        result = await _engine(deps).run_analysis("CVD", 5)
        assert result.breaches == []
        assert result.rule_ids == []
        deps.email_client.send_alert.assert_not_awaited()
        deps.es.client.search.assert_not_awaited()

    async def test_disabled_rule_skipped_among_enabled(self):
        # two rules at the SAME interval: warn enabled, crit disabled. value 99
        # crosses both thresholds, but only the enabled WARNING may fire — the
        # disabled rule must be dropped from the (non-empty) rule set.
        profile = _cpu_profile(rules=[
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)]),
            Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL", enabled=False,
                 when=[Condition(fact="cpu.max", op=">=", value=95)]),
        ])
        deps = _make_deps(profile)
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 99.0))
        result = await _engine(deps).run_analysis("CVD", 5)
        assert result.rule_ids == ["cpu_warn"]
        assert {b.severity for b in result.breaches} == {"WARNING"}

    async def test_phase2_fact_skipped(self):
        prof = MonitorProfile(
            scope=Scope(process="*"),
            measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                              window_minutes=15,
                              facts=[Fact(type="max"), Fact(type="zscore")])],
            rules=[Rule(id="cpu_z", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.zscore", op=">=", value=3)])],
            notify={"default": NotifyChannel(cooldown_minutes=30)},
        )
        deps = _make_deps(prof)
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 99.0))
        result = await _engine(deps).run_analysis("CVD", 5)
        assert result.breaches == []  # zscore (Phase 2) skipped → rule never fires
        deps.email_client.send_alert.assert_not_awaited()

    async def test_zk_lock_acquired(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 50.0))
        await _engine(deps).run_analysis("CVD", 5)
        deps.zk_lock.acquire.assert_called_once_with("CVD")


class TestDeadPathRegression:
    """🔴 The headline bug: model/eqp overrides must reach alerts. A MODEL_B
    overlay adds a CRITICAL rule the base MODEL_A profile lacks; both equipment
    breach the same value, but only MODEL_B may page CRITICAL."""

    async def test_model_overlay_reaches_alerts(self):
        base = _cpu_profile()  # cpu_warn only
        overlaid = _cpu_profile(rules=[
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)]),
            Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
                 when=[Condition(fact="cpu.max", op=">=", value=95)]),
        ])

        async def resolve(process, eqp_model, eqp_id):
            return overlaid if eqp_model == "MODEL_B" else base

        deps = _make_deps(base, equipment=[_EQP01, _EQP02], real_qb=True)
        deps.profile_repo.resolve_profile = AsyncMock(side_effect=resolve)

        async def search(index, body):
            eqp_ids = []
            for f in body["query"]["bool"]["filter"]:
                if "terms" in f and "EARS_EQPID.keyword" in f["terms"]:
                    eqp_ids = f["terms"]["EARS_EQPID.keyword"]
            return {"aggregations": {"by_eqp": {"buckets": [
                {"key": e, "max": {"value": 98.0}} for e in eqp_ids
            ]}}}

        deps.es.client.search = AsyncMock(side_effect=search)
        result = await _engine(deps).run_analysis("CVD", 5)

        by_eqp_sev = {(b.eqp_id, b.severity) for b in result.breaches}
        # base equipment: WARNING only
        assert ("EQP01", "WARNING") in by_eqp_sev
        assert ("EQP01", "CRITICAL") not in by_eqp_sev
        # overlay equipment: the CRITICAL rule from the overlay DID reach alerts
        assert ("EQP02", "CRITICAL") in by_eqp_sev
