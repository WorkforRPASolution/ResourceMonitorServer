"""Prometheus metric definitions + ``/metrics`` exposition.

Metrics are module-level so they can be imported and updated from anywhere
in the application without depending on a registry handle. The default
``REGISTRY`` is used implicitly by ``generate_latest()``.
"""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ----------------------------------------------------------------------
# Counters
# ----------------------------------------------------------------------
JOB_TOTAL = Counter(
    "resource_monitor_job_total",
    "Number of analysis job runs",
    # v6 P1-2: ``reason`` distinguishes failure causes so dashboards can
    # alert on infra-specific failures (mongo_unavailable, es_unavailable)
    # versus generic logic errors. Empty string for success/skip.
    ["process", "status", "reason"],
)

ALERTS_SENT = Counter(
    "resource_monitor_alerts_sent_total",
    "Number of alerts dispatched to the Email API",
    ["code", "subcode"],
)

THRESHOLD_BREACHES = Counter(
    "resource_monitor_threshold_breaches_total",
    "Number of threshold breaches detected",
    ["process", "metric", "severity"],
)

ALERTS_SUPPRESSED = Counter(
    "resource_monitor_alerts_suppressed_by_cooldown_total",
    "Breaches not alerted due to active cooldown",
    ["process", "metric", "severity"],
)

# ----------------------------------------------------------------------
# Histograms
# ----------------------------------------------------------------------
JOB_DURATION = Histogram(
    "resource_monitor_job_duration_seconds",
    "Wall-clock duration of an analysis job",
    ["process", "metric_category"],
)

ES_QUERY_DURATION = Histogram(
    "resource_monitor_es_query_duration_seconds",
    "Latency of ES search calls",
    ["process"],
)

# ----------------------------------------------------------------------
# Gauges
# ----------------------------------------------------------------------
ZK_LEADER = Gauge(
    "resource_monitor_zk_leader",
    "1 if this instance currently holds the ZK leader lock, 0 otherwise",
)

ASSIGNED_PROCESSES = Gauge(
    "resource_monitor_assigned_processes",
    "Number of processes this instance is currently responsible for analyzing",
)

# v6 P0-5: per-infra reachability gauge.
# Updated by /healthz/ready on every K8s probe so a Prometheus alert can
# fire on `infra_up == 0` without parsing log lines. Labels are stable;
# adding a new infra requires updating ``INFRA_LABELS`` below AND the
# ``readiness()`` handler.
INFRA_LABELS = ("elasticsearch", "mongodb", "redis", "email_api", "zookeeper")
INFRA_UP = Gauge(
    "resource_monitor_infra_up",
    "1 if the infrastructure is reachable from this pod, 0 otherwise",
    ["infra"],
)

# v6 P0-5: 1 after lifespan.yield is reached, 0 during init or shutdown.
# Lets operators plot wall-clock startup time without parsing logs.
STARTUP_COMPLETE = Gauge(
    "resource_monitor_startup_complete",
    "1 once the FastAPI lifespan has yielded (init finished), 0 otherwise",
)


def render_metrics() -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for the ``/metrics`` endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
