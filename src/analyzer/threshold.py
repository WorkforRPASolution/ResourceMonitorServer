"""Threshold comparison and process state check logic."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.db.models import ThresholdConfig


class ThresholdBreach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eqp_id: str
    metric: str
    current_value: float
    threshold_value: float
    severity: str  # "WARNING" | "CRITICAL"


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process: str
    metric_pattern: str
    breaches: list[ThresholdBreach]
    total_evaluated: int
    timestamp: datetime


def evaluate_thresholds(
    eqp_metrics: dict[str, dict[str, float | None]],
    threshold_config: ThresholdConfig,
    metric_fields: list[str],
) -> list[ThresholdBreach]:
    """Compare metric values against thresholds.
    - Check critical first, then warning (highest severity only per eqp+metric).
    - Skip None values (empty ES buckets).
    """
    breaches = []
    for eqp_id, metrics in eqp_metrics.items():
        for field in metric_fields:
            value = metrics.get(field)
            if value is None:
                continue
            if value >= threshold_config.critical:
                breaches.append(
                    ThresholdBreach(
                        eqp_id=eqp_id,
                        metric=field,
                        current_value=value,
                        threshold_value=threshold_config.critical,
                        severity="CRITICAL",
                    )
                )
            elif value >= threshold_config.warning:
                breaches.append(
                    ThresholdBreach(
                        eqp_id=eqp_id,
                        metric=field,
                        current_value=value,
                        threshold_value=threshold_config.warning,
                        severity="WARNING",
                    )
                )
    return breaches


def evaluate_state_check(
    eqp_metrics: dict[str, dict[str, float | None]],
    field_name: str,
    expected: float,
) -> list[ThresholdBreach]:
    """Process watch check: required (expected=1.0, min aggregated) or forbidden (expected=0.0, max aggregated).
    For required: if min value is 0 (process was down at some point), breach.
    For forbidden: if max value > 0 (process ran at some point), breach.
    Severity is always CRITICAL for process watch.
    """
    breaches = []
    for eqp_id, metrics in eqp_metrics.items():
        value = metrics.get(field_name)
        if value is None:
            continue
        if expected == 1.0 and value == 0.0:
            # required process was down (min agg returned 0)
            breaches.append(
                ThresholdBreach(
                    eqp_id=eqp_id,
                    metric=field_name,
                    current_value=value,
                    threshold_value=expected,
                    severity="CRITICAL",
                )
            )
        elif expected == 0.0 and value > 0.0:
            # forbidden process was running (max agg returned > 0)
            breaches.append(
                ThresholdBreach(
                    eqp_id=eqp_id,
                    metric=field_name,
                    current_value=value,
                    threshold_value=expected,
                    severity="CRITICAL",
                )
            )
    return breaches
