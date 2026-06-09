"""Analysis engine — v2 per-equipment orchestration coroutine.

One job tick covers a (process, interval) pair. The engine resolves the
**effective** profile *per equipment* (fixing the v1 dead path where only the
process-level profile was ever consulted, so model/eqp overrides never reached
alerts), buckets equipment by identical effective profile so each distinct
profile costs one ES query set, then runs the measure→fact→rule pipeline:

    measure (잰다)  → ES aggregation over EARS_VALUE → facts
    rule (판단)     → op/quantifier/combine over facts → breach
    notify (알린다) → cooldown check → email

Phase 2/3 fact types are schema-accepted but engine-skipped (logged), so a rule
referencing only unimplemented facts simply never fires.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

import structlog

from src.analyzer import fact_catalog as fc
from src.analyzer.alert_builder import (
    build_alert_request,
    make_cooldown_key,
    resolve_code_subcode,
    resolve_group_value,
)
from src.analyzer.es_parser import parse_metric_aggregation
from src.analyzer.metric_resolver import resolve_metric_patterns
from src.analyzer.threshold import AnalysisResult, ThresholdBreach, evaluate_rule
from src.api.metrics import (
    ALERTS_SENT,
    ALERTS_SUPPRESSED,
    ES_QUERY_DURATION,
    THRESHOLD_BREACHES,
)
from src.config.settings import AppSettings
from src.db.models import Measure, MonitorProfile, NotifyChannel, Rule

logger = structlog.get_logger(__name__)


class _Pending(NamedTuple):
    """A fired breach plus the context needed to cool it down and alert."""

    breach: ThresholdBreach
    notify_name: str
    channel: NotifyChannel
    window_minutes: int


class AnalysisEngine:
    """Orchestrates one analysis tick for a (process, interval) pair."""

    def __init__(self, deps: Any, settings: AppSettings) -> None:
        self._deps = deps
        self._settings = settings
        self._es_semaphore = asyncio.Semaphore(3)

    async def run_analysis(self, process: str, interval_minutes: int) -> AnalysisResult:
        """Public entry: lock the process, then run every rule whose
        ``interval_minutes`` matches this tick, across all active equipment."""
        async with self._deps.zk_lock.acquire(process), self._es_semaphore:
            return await self._do_analysis(process, interval_minutes)

    async def _do_analysis(self, process: str, interval_minutes: int) -> AnalysisResult:
        now = datetime.now(UTC)

        # 1. active equipment for this process
        equipment = await self._deps.eqp_info_repo.get_active_equipment_by_process(process)
        eqp_lookup: dict[str, dict[str, Any]] = {d["eqpId"]: d for d in equipment}
        if not equipment:
            return AnalysisResult(process=process, breaches=[], total_evaluated=0, timestamp=now)

        # 2. resolve the effective profile per equipment; bucket by signature so
        #    equipment sharing a profile is analysed with one query set.
        buckets: dict[str, dict[str, Any]] = {}
        for doc in equipment:
            profile = await self._deps.profile_repo.resolve_profile(
                process, doc.get("eqpModel", "*"), doc["eqpId"]
            )
            if profile is None or not profile.enabled:
                continue
            sig = profile.effective_signature()
            bucket = buckets.setdefault(sig, {"profile": profile, "eqp_ids": []})
            bucket["eqp_ids"].append(doc["eqpId"])

        # 3. evaluate each bucket
        pending: list[_Pending] = []
        rule_ids: set[str] = set()
        for bucket in buckets.values():
            profile: MonitorProfile = bucket["profile"]
            eqp_ids: list[str] = bucket["eqp_ids"]
            rules = [
                r
                for r in profile.rules
                if r.interval_minutes == interval_minutes and r.enabled
            ]
            if not rules:
                continue
            await self._evaluate_bucket(
                process, now, profile, rules, eqp_ids, pending, rule_ids
            )

        # 4. breach metrics
        for p in pending:
            THRESHOLD_BREACHES.labels(
                process=process, metric=p.breach.fact, severity=p.breach.severity
            ).inc()

        breaches = [p.breach for p in pending]
        if pending:
            await self._dispatch(process, pending, eqp_lookup, now)

        return AnalysisResult(
            process=process,
            rule_ids=sorted(rule_ids),
            breaches=breaches,
            total_evaluated=len(equipment),
            timestamp=now,
        )

    # ------------------------------------------------------------------
    async def _evaluate_bucket(
        self,
        process: str,
        now: datetime,
        profile: MonitorProfile,
        rules: list[Rule],
        eqp_ids: list[str],
        pending: list[_Pending],
        rule_ids: set[str],
    ) -> None:
        measures_by_id = {m.id: m for m in profile.measures}
        category = {m.id: m.category for m in profile.measures}

        # compute every measure referenced by the rules in this tick (once)
        referenced = {
            cond.fact.partition(".")[0] for rule in rules for cond in rule.when
        }
        results: dict[str, dict[tuple[str, str], dict[str, list]]] = {}
        for mid in referenced:
            measure = measures_by_id.get(mid)
            if measure is not None:
                results[mid] = await self._compute_measure(process, now, measure, eqp_ids)

        for rule in rules:
            rule_ids.add(rule.id)
            ref = {cond.fact.partition(".")[0] for cond in rule.when}
            targets: set[tuple[str, str]] = set()
            for mid in ref:
                targets |= set(results.get(mid, {}).keys())
            for eqp_id, proc in targets:
                facts_by_ref = {
                    cond.fact: results.get(
                        cond.fact.partition(".")[0], {}
                    ).get((eqp_id, proc), {}).get(cond.fact.partition(".")[2], [])
                    for cond in rule.when
                }
                breach = evaluate_rule(
                    rule, facts_by_ref, eqp_id=eqp_id, proc=proc, measure_category=category
                )
                if breach is None:
                    continue
                channel = profile.notify.get(rule.notify)
                if channel is None:
                    logger.warning(
                        "notify_channel_missing", rule=rule.id, notify=rule.notify
                    )
                    continue
                trigger_mid = breach.fact.partition(".")[0]
                window = measures_by_id[trigger_mid].window_minutes
                pending.append(_Pending(breach, rule.notify, channel, window))

    async def _compute_measure(
        self, process: str, now: datetime, measure: Measure, eqp_ids: list[str]
    ) -> dict[tuple[str, str], dict[str, list]]:
        """Run one measure's ES aggregation and parse it to per-(eqp, proc) facts."""
        impl_facts = [f for f in measure.facts if fc.is_implemented(f.type)]
        skipped = [f.type.value for f in measure.facts if not fc.is_implemented(f.type)]
        if skipped:
            logger.warning(
                "fact_phase_not_implemented", measure=measure.id, facts=skipped
            )
        if not impl_facts:
            return {}

        index = self._deps.query_builder.resolve_index_range(
            process, measure.window_minutes, now=now
        )
        expand = measure.expand == "instance"
        if expand:
            names = await self._deps.es.get_metric_names(
                index, measure.category, measure.proc
            )
            metrics = resolve_metric_patterns([measure.metric], names).get(
                measure.metric, []
            )
            if not metrics:
                return {}
        else:
            metrics = [measure.metric]

        body = self._deps.query_builder.build_metric_aggregation_query(
            now,
            window_minutes=measure.window_minutes,
            category=measure.category,
            metrics=metrics,
            proc=measure.proc,
            facts=impl_facts,
            expand_instance=expand,
            eqp_ids=eqp_ids,
        )
        t0 = time.monotonic()
        resp = await self._deps.es.client.search(index=index, body=body)
        ES_QUERY_DURATION.labels(process=process).observe(time.monotonic() - t0)
        return parse_metric_aggregation(
            resp, impl_facts, proc=measure.proc, expand_instance=expand
        )

    async def _dispatch(
        self,
        process: str,
        pending: list[_Pending],
        eqp_lookup: dict[str, dict[str, Any]],
        now: datetime,
    ) -> None:
        """Cooldown-gate and send one email per group.

        The group identity is the cooldown key ``(process, group, proc, notify,
        severity)`` where ``group`` is the eqpId (``group_by="eqp"``, current
        behaviour) or the model/process value. Equipment breaching under one
        group collapse into a single email addressed via a representative
        equipment's emailCategory (pinned via ``channel.representatives`` else
        the smallest breaching eqpId); the affected equipment are listed in the
        alert variables (group modes only)."""
        # 1. bucket pending by group cooldown key
        groups: dict[tuple[str, str, str, str, str], list[_Pending]] = {}
        group_value_by_key: dict[tuple[str, str, str, str, str], str] = {}
        for p in pending:
            eqp_info = eqp_lookup.get(p.breach.eqp_id)
            if eqp_info is None:
                logger.debug("eqp_not_in_lookup", eqp_id=p.breach.eqp_id, process=process)
                continue
            gv = resolve_group_value(p.channel.group_by, p.breach, eqp_info, process)
            key = make_cooldown_key(process, p.breach, p.notify_name, group_value=gv)
            groups.setdefault(key, []).append(p)
            group_value_by_key[key] = gv

        if not groups:
            return
        cooling = await self._deps.cooldown_mgr.is_cooling_down_batch(list(groups))

        # 2. one email per group
        for key, members in groups.items():
            if cooling.get(key, False):
                for m in members:
                    ALERTS_SUPPRESSED.labels(
                        process=process, metric=m.breach.fact, severity=m.breach.severity
                    ).inc()
                continue
            channel = members[0].channel
            group_value = group_value_by_key[key]
            breaching_eqps = sorted({m.breach.eqp_id for m in members})
            # representative: operator-pinned (if it actually breached) else min
            rep_eqp = channel.representatives.get(group_value)
            if rep_eqp not in breaching_eqps:
                rep_eqp = breaching_eqps[0]
            rep = next(m for m in members if m.breach.eqp_id == rep_eqp)
            affected = breaching_eqps if channel.group_by != "eqp" else None
            # Option C: when enabled, fetch the operator template (async) here —
            # keeping build_alert_request synchronous (review fix). Off → no fetch,
            # payload stays the legacy 9 fields.
            template = None
            if self._settings.rms_custom_body_enabled:
                code, subcode = resolve_code_subcode(channel, rep.breach)
                template = await self._deps.template_repo.find_template(
                    self._settings.email_app_name,
                    process,
                    eqp_lookup[rep_eqp].get("eqpModel", ""),
                    code,
                    subcode,
                )
            alert = build_alert_request(
                rep.breach, eqp_lookup[rep_eqp], process, self._settings,
                channel, rep.window_minutes, affected_equipment=affected,
                members=[m.breach for m in members], eqp_lookup=eqp_lookup,
                timestamp=now, template=template,
            )
            if await self._deps.email_client.send_alert(alert):
                await self._deps.cooldown_mgr.set_cooldown(
                    *key, channel.cooldown_minutes
                )
                ALERTS_SENT.labels(code=alert.code, subcode=alert.subcode).inc()
