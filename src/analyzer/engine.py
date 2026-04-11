"""Analysis engine — Phase 1 orchestration coroutine.

Ties together: ES query → threshold comparison → cooldown check → email alert.
Called by AnalysisScheduler's APScheduler jobs via _job_wrapper.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from src.analyzer.alert_builder import (
    build_alert_request,
    classify_metric_category,
    group_breaches_by_equipment,
)
from src.analyzer.es_parser import parse_metric_aggregation
from src.analyzer.metric_resolver import get_agg_type, resolve_metric_patterns
from src.analyzer.threshold import (
    AnalysisResult,
    ThresholdBreach,
    evaluate_state_check,
    evaluate_thresholds,
)
from src.api.metrics import (
    ALERTS_SENT,
    ALERTS_SUPPRESSED,
    ES_QUERY_DURATION,
    THRESHOLD_BREACHES,
)
from src.config.settings import AppSettings
from src.db.models import AnalysisConfig

logger = structlog.get_logger(__name__)


class AnalysisEngine:
    """Orchestrates a single analysis run for a (process, config) pair."""

    def __init__(self, deps: Any, settings: AppSettings) -> None:
        self._deps = deps
        self._settings = settings
        self._es_semaphore = asyncio.Semaphore(3)

    async def run_analysis(
        self, process: str, config: AnalysisConfig
    ) -> AnalysisResult:
        metric_pattern = config.metric_pattern
        window = config.schedule.window_minutes

        async with self._deps.zk_lock.acquire(process), self._es_semaphore:
            return await self._do_analysis(process, config, metric_pattern, window)

    async def _do_analysis(
        self,
        process: str,
        config: AnalysisConfig,
        metric_pattern: str,
        window: int,
    ) -> AnalysisResult:
        now = datetime.now(UTC)

        # 1. Bulk fetch active equipment
        equipment_list = await self._deps.eqp_info_repo.get_active_equipment_by_process(process)
        eqp_lookup: dict[str, dict[str, Any]] = {
            doc["eqpId"]: doc for doc in equipment_list
        }

        # 2. Resolve metric pattern wildcards
        index_pattern = self._deps.query_builder.resolve_index_range(process, window)
        available_fields = await self._deps.es.get_numeric_field_names(index_pattern)
        resolved = resolve_metric_patterns([metric_pattern], available_fields)
        matched_fields = resolved.get(metric_pattern, [])

        if not matched_fields:
            logger.warning(
                "metric_pattern_no_match",
                process=process,
                pattern=metric_pattern,
                available_count=len(available_fields),
            )
            return AnalysisResult(
                process=process,
                metric_pattern=metric_pattern,
                breaches=[],
                total_evaluated=0,
                timestamp=now,
            )

        # 3. Determine agg types per field
        agg_types = {f: get_agg_type(metric_pattern, f) for f in matched_fields}
        # For state_check fields, use min for required, max for forbidden
        es_agg_types: dict[str, str] = {}
        state_check_fields: list[str] = []
        threshold_fields: list[str] = []
        for f, at in agg_types.items():
            if at == "state_check":
                state_check_fields.append(f)
                es_agg_types[f] = "min" if f == "required" else "max"
            else:
                threshold_fields.append(f)
                es_agg_types[f] = at

        # 4. Build and execute ES query
        body = self._deps.query_builder.build_metric_aggregation_query(
            now, window, matched_fields, es_agg_types, process=process
        )
        t0 = time.monotonic()
        response = await self._deps.es.client.search(index=index_pattern, body=body)
        ES_QUERY_DURATION.labels(process=process).observe(time.monotonic() - t0)

        # 5. Parse response
        eqp_metrics = parse_metric_aggregation(response, matched_fields, es_agg_types)

        # 6. Evaluate thresholds and state checks
        breaches: list[ThresholdBreach] = []
        if threshold_fields:
            breaches.extend(
                evaluate_thresholds(eqp_metrics, config.threshold, threshold_fields)
            )
        for f in state_check_fields:
            expected = 1.0 if f == "required" else 0.0
            breaches.extend(evaluate_state_check(eqp_metrics, f, expected))

        # 7. Record breach metrics
        for b in breaches:
            THRESHOLD_BREACHES.labels(
                process=process, metric=b.metric, severity=b.severity
            ).inc()

        if not breaches:
            return AnalysisResult(
                process=process,
                metric_pattern=metric_pattern,
                breaches=[],
                total_evaluated=len(eqp_metrics),
                timestamp=now,
            )

        # 8. Group by equipment and batch cooldown check
        grouped = group_breaches_by_equipment(breaches)
        cooldown_checks: list[tuple[str, str, str]] = []
        for eqp_id, eqp_breaches in grouped.items():
            for b in eqp_breaches:
                category = classify_metric_category(metric_pattern, b.metric)
                cooldown_checks.append((eqp_id, category, b.metric))

        cooldown_status = await self._deps.cooldown_mgr.is_cooling_down_batch(
            cooldown_checks
        )

        # 9. Send alerts for non-cooled-down equipment
        for eqp_id, eqp_breaches in grouped.items():
            eqp_info = eqp_lookup.get(eqp_id)
            if eqp_info is None:
                logger.debug("eqp_not_in_lookup", eqp_id=eqp_id, process=process)
                continue

            # Use the highest-severity breach for the email
            worst = max(
                eqp_breaches,
                key=lambda b: (0 if b.severity == "WARNING" else 1),
            )
            category = classify_metric_category(metric_pattern, worst.metric)
            cooldown_key = (eqp_id, category, worst.metric)

            if cooldown_status.get(cooldown_key, False):
                for b in eqp_breaches:
                    ALERTS_SUPPRESSED.labels(
                        process=process, metric=b.metric, severity=b.severity
                    ).inc()
                continue

            alert = build_alert_request(
                worst, eqp_info, process, self._settings,
                metric_pattern, window,
            )
            sent = await self._deps.email_client.send_alert(alert)
            if sent:
                await self._deps.cooldown_mgr.set_cooldown(
                    eqp_id, category, worst.metric,
                    config.threshold.cooldown_minutes,
                )
                ALERTS_SENT.labels(
                    code=alert.code, subcode=alert.subcode
                ).inc()

        return AnalysisResult(
            process=process,
            metric_pattern=metric_pattern,
            breaches=breaches,
            total_evaluated=len(eqp_metrics),
            timestamp=now,
        )
