"""Tests for src.analyzer.engine — v2 per-equipment orchestration."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

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
    # Pin custom-body OFF: these engine tests assert the legacy/off path
    # explicitly. The production default is True (see test_settings.py); the
    # ON path has its own tests via _settings_body_on().
    return AppSettings(grafana_base_url="http://grafana:3000",
                       grafana_dashboard_uid="abc123", email_app_name="ARS",
                       rms_custom_body_enabled=False)


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

    async def test_disabled_profile_skip_logs_aggregate(self):
        """disabled 스킵은 조용히 죽지 않는다(2026-06-12 사고 재발 방지) —
        process당 1줄 집계 로그. profile=None(문서 없음)은 집계에 안 들어간다."""
        prof = _cpu_profile()
        prof.enabled = False
        deps = _make_deps(prof, equipment=[_EQP01, _EQP02])
        with structlog.testing.capture_logs() as cap:
            await _engine(deps).run_analysis("CVD", 5)
        events = [e for e in cap if e["event"] == "equipment_skipped_disabled"]
        assert len(events) == 1  # per-eqp 로그 금지 — 집계 1줄
        assert events[0]["process"] == "CVD"
        assert events[0]["count"] == 2

        # 문서가 아예 없는 장비(resolve→None)는 disabled 집계가 아니다
        deps_none = _make_deps(None)
        with structlog.testing.capture_logs() as cap2:
            await _engine(deps_none).run_analysis("CVD", 5)
        assert not [e for e in cap2 if e["event"] == "equipment_skipped_disabled"]

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


# ----------------------------------------------------------------------
# Group send (notify.group_by) — collapse N equipment into one email
# ----------------------------------------------------------------------
_EQP_A1 = {"eqpId": "EQP-A1", "eqpModel": "MODEL_A", "process": "CVD",
           "localpc": "PCA1", "ipAddr": "10.0.0.11", "line": "L1", "category": "MAIN"}
_EQP_A2 = {"eqpId": "EQP-A2", "eqpModel": "MODEL_A", "process": "CVD",
           "localpc": "PCA2", "ipAddr": "10.0.0.12", "line": "L1", "category": "MAIN"}
_EQP_B1 = {"eqpId": "EQP-B1", "eqpModel": "MODEL_B", "process": "CVD",
           "localpc": "PCB1", "ipAddr": "10.0.0.21", "line": "L2", "category": "MAIN"}


def _cpu_profile_group(group_by="eqp", email_group=None):
    return MonitorProfile(
        scope=Scope(process="*"),
        measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                          window_minutes=15, facts=[Fact(type="max")])],
        rules=[Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                    when=[Condition(fact="cpu.max", op=">=", value=80)])],
        notify={"default": NotifyChannel(cooldown_minutes=30, group_by=group_by,
                                         email_group=email_group)},
    )


def _search_all_breach(value=99.0):
    async def _search(index, body):
        eqp_ids = []
        for f in body["query"]["bool"]["filter"]:
            if "terms" in f and "EARS_EQPID.keyword" in f["terms"]:
                eqp_ids = f["terms"]["EARS_EQPID.keyword"]
        return {"aggregations": {"by_eqp": {"buckets": [
            {"key": e, "max": {"value": value}} for e in eqp_ids
        ]}}}
    return _search


class TestGroupSend:
    async def test_group_by_eqp_default_one_email_each(self):
        # regression guard: default eqp mode keeps per-equipment fan-out
        deps = _make_deps(_cpu_profile_group("eqp"),
                          equipment=[_EQP_A1, _EQP_A2], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        result = await _engine(deps).run_analysis("CVD", 5)
        assert deps.email_client.send_alert.await_count == 2
        assert {b.eqp_id for b in result.breaches} == {"EQP-A1", "EQP-A2"}

    async def test_group_by_model_collapses_to_one(self):
        deps = _make_deps(_cpu_profile_group("model"),
                          equipment=[_EQP_A1, _EQP_A2], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        await _engine(deps).run_analysis("CVD", 5)
        assert deps.email_client.send_alert.await_count == 1
        alert = deps.email_client.send_alert.await_args.args[0]
        assert alert.hostname == "EQP-A1"  # representative = min eqpId
        assert alert.variables["AffectedEquipment"] == "EQP-A1, EQP-A2"
        assert alert.variables["AffectedCount"] == "2"
        deps.cooldown_mgr.set_cooldown.assert_awaited_once_with(
            "CVD", "MODEL_A", "@system", "default", "WARNING", 30
        )

    async def test_group_by_model_sets_email_category_and_display_id(self):
        deps = _make_deps(_cpu_profile_group("model", email_group="TEAM1"),
                          equipment=[_EQP_A1, _EQP_A2], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        await _engine(deps).run_analysis("CVD", 5)
        alert = deps.email_client.send_alert.await_args.args[0]
        # RMS composes the recipient category directly; model_token = eqpModel
        assert alert.email_category == "EMAIL-CVD-MODEL_A-TEAM1"
        assert alert.display_id == "MODEL_A"  # title headline = group_value
        assert alert.to_payload()["emailCategory"] == "EMAIL-CVD-MODEL_A-TEAM1"

    async def test_group_by_process_sets_all_token(self):
        deps = _make_deps(_cpu_profile_group("process", email_group="TEAM1"),
                          equipment=[_EQP_A1, _EQP_B1], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        await _engine(deps).run_analysis("CVD", 5)
        alert = deps.email_client.send_alert.await_args.args[0]
        # process grouping mixes models → model_token = ALL
        assert alert.email_category == "EMAIL-CVD-ALL-TEAM1"
        assert alert.display_id == "CVD"

    async def test_email_group_unset_omits_routing_fields(self):
        deps = _make_deps(_cpu_profile_group("model"),  # no email_group
                          equipment=[_EQP_A1, _EQP_A2], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        await _engine(deps).run_analysis("CVD", 5)
        alert = deps.email_client.send_alert.await_args.args[0]
        assert alert.email_category is None  # → Akka derives (fallback)
        assert "emailCategory" not in alert.to_payload()
        assert alert.display_id == "MODEL_A"  # headline still set for group send

    async def test_email_group_without_custom_body_warns(self):
        deps = _make_deps(_cpu_profile_group("model", email_group="TEAM1"),
                          equipment=[_EQP_A1, _EQP_A2], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        cap = structlog.testing.LogCapture()
        structlog.configure(processors=[cap])
        try:
            await _engine(deps).run_analysis("CVD", 5)  # _settings(): flag off
        finally:
            structlog.reset_defaults()
        events = [e["event"] for e in cap.entries]
        assert "email_group_without_custom_body" in events

    async def test_group_by_process_spans_models_one_email(self):
        # ⚠ routing caveat: MODEL_A + MODEL_B under process grouping → ONE email
        # (addressed via the representative's category only — documented limit).
        deps = _make_deps(_cpu_profile_group("process"),
                          equipment=[_EQP_A1, _EQP_B1], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        await _engine(deps).run_analysis("CVD", 5)
        assert deps.email_client.send_alert.await_count == 1
        alert = deps.email_client.send_alert.await_args.args[0]
        assert set(alert.variables["AffectedEquipment"].split(", ")) == {"EQP-A1", "EQP-B1"}

    async def test_group_cooldown_suppresses(self):
        deps = _make_deps(_cpu_profile_group("model"),
                          equipment=[_EQP_A1, _EQP_A2], real_qb=True)
        deps.es.client.search = AsyncMock(side_effect=_search_all_breach())
        deps.cooldown_mgr.is_cooling_down_batch = AsyncMock(return_value={
            ("CVD", "MODEL_A", "@system", "default", "WARNING"): True,
        })
        await _engine(deps).run_analysis("CVD", 5)
        deps.email_client.send_alert.assert_not_awaited()


def _settings_body_on():
    return AppSettings(grafana_base_url="http://grafana:3000",
                       grafana_dashboard_uid="abc123", email_app_name="ARS",
                       rms_custom_body_enabled=True)


class TestRenderedBodyDispatch:
    async def test_flag_on_fetches_template_and_renders(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 92.5))
        deps.template_repo = AsyncMock()
        deps.template_repo.find_template = AsyncMock(return_value={
            "html": "<p>@Hostname @CurrentValue</p>", "title": "[EARS] @Severity"})
        engine = AnalysisEngine(deps, _settings_body_on())
        engine._es_semaphore = asyncio.Semaphore(3)

        await engine.run_analysis("CVD", 5)

        deps.template_repo.find_template.assert_awaited()
        alert = deps.email_client.send_alert.await_args.args[0]
        assert alert.rendered_body == "<p>EQP01 92.5</p>"
        assert alert.title == "[EARS] WARNING"
        assert "renderedBody" in alert.to_payload()

    async def test_flag_off_skips_template_repo(self):
        deps = _make_deps(_cpu_profile())
        deps.es.client.search = AsyncMock(return_value=_es_max("EQP01", 92.5))
        deps.template_repo = AsyncMock()
        deps.template_repo.find_template = AsyncMock()

        await _engine(deps).run_analysis("CVD", 5)  # _settings(): flag off

        deps.template_repo.find_template.assert_not_awaited()
        alert = deps.email_client.send_alert.await_args.args[0]
        assert alert.rendered_body is None
