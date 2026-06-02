"""Build EmailAlertRequest from analysis results."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.alert.models import EmailAlertRequest
from src.config.constants import (
    ALERT_CATEGORY_CPU,
    ALERT_CATEGORY_DISK,
    ALERT_CATEGORY_GPU,
    ALERT_CATEGORY_MEMORY,
    ALERT_CATEGORY_PROCESS_WATCH,
    ALERT_CATEGORY_RESOURCE,
    ALERT_CATEGORY_TEMPERATURE,
    ALERT_CODE_RESOURCE_MONITOR,
)
from src.config.settings import AppSettings

if TYPE_CHECKING:
    from src.analyzer.threshold import ThresholdBreach


def classify_metric_category(metric_pattern: str, field_name: str) -> str:
    """Map a metric pattern/field to an alert category for sub_code."""
    # Process watch
    if field_name in ("required", "forbidden"):
        return ALERT_CATEGORY_PROCESS_WATCH

    # GPU (check before CPU since gpu fields may contain _core_load)
    lower = field_name.lower()
    if lower.startswith("gpu") or metric_pattern.lower().startswith("gpu"):
        return ALERT_CATEGORY_GPU

    # CPU
    if "core_load" in lower or field_name == "total_used_pct":
        return ALERT_CATEGORY_CPU

    # Memory
    if lower.startswith("mem") or "mem_" in metric_pattern.lower():
        return ALERT_CATEGORY_MEMORY

    # Disk
    if lower.startswith("disk") or "disk" in metric_pattern.lower():
        return ALERT_CATEGORY_DISK

    # Temperature
    if "temp" in lower:
        return ALERT_CATEGORY_TEMPERATURE

    return ALERT_CATEGORY_RESOURCE


def build_alert_request(
    breach: ThresholdBreach,
    eqp_info: dict[str, Any],
    process: str,
    settings: AppSettings,
    metric_pattern: str,
    window_minutes: int,
) -> EmailAlertRequest:
    """Construct an EmailAlertRequest from a breach + equipment info."""
    category = classify_metric_category(metric_pattern, breach.metric)
    grafana_url = ""
    if settings.grafana_base_url and settings.grafana_dashboard_uid:
        grafana_url = (
            f"{settings.grafana_base_url}/d/{settings.grafana_dashboard_uid}"
            f"?var-eqpId={breach.eqp_id}&var-process={process}"
        )

    return EmailAlertRequest(
        hostname=eqp_info.get("localpc", ""),
        ip=eqp_info.get("ipAddr", ""),
        app=settings.email_app_name,
        process=process,
        eqp_model=eqp_info.get("eqpModel", ""),
        line=eqp_info.get("line", ""),
        code=ALERT_CODE_RESOURCE_MONITOR,
        subcode=f"{category}_{breach.severity}",
        variables={
            "Severity": breach.severity,
            "Category": category,
            "MetricName": breach.metric,
            "CurrentValue": str(breach.current_value),
            "Threshold": str(breach.threshold_value),
            "WindowMin": str(window_minutes),
            "GrafanaUrl": grafana_url,
        },
    )


def group_breaches_by_equipment(
    breaches: list[ThresholdBreach],
) -> dict[str, list[ThresholdBreach]]:
    """Group breaches by eqp_id for per-equipment alerting."""
    groups: dict[str, list[ThresholdBreach]] = {}
    for breach in breaches:
        groups.setdefault(breach.eqp_id, []).append(breach)
    return groups
