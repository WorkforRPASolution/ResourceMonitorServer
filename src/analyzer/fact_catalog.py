"""Fact (= type) catalog — single source of truth for v2 monitoring facts.

A *fact* is one named output a measure produces; its ``type`` name is also the
name a rule references via ``measureId.type`` (SCHEMA.md §2). This module
centralises everything the rest of the codebase needs to know about fact types:

- the closed ``FactType`` enum (no free-text types allowed),
- ``ALLOWED_OPS`` — which rule operators are semantically valid per type
  (e.g. ``max`` only compares ``>``/``>=``; ``trend`` only ``trend==``),
- ``PHASE_OF_FACT`` — implementation phase, used to gate Phase 2/3 facts that
  the schema accepts but the engine does not yet evaluate,
- parameter-requirement sets (``NEEDS_BUCKETING`` etc.) shared by model
  validation.

Consumed by: ``db.models`` (validation), ``analyzer.metric_resolver`` (ES agg
mapping) and ``analyzer.threshold`` (rule evaluation).
"""
from __future__ import annotations

from enum import StrEnum


class FactType(StrEnum):
    # Phase 1 — single-window aggregation
    MAX = "max"
    MIN = "min"
    AVG = "avg"
    LAST = "last"
    P50 = "p50"
    P90 = "p90"
    P95 = "p95"
    P99 = "p99"
    SPIKE_COUNT = "spike_count"
    # Phase 2 — time-bucketed / stateful
    DURATION = "duration"
    DELTA = "delta"
    GROWTH_RATE = "growth_rate"
    MOVING_AVG = "moving_avg"
    TREND = "trend"
    ZSCORE = "zscore"
    # Phase 3 — historical
    BASELINE_DEV = "baseline_dev"


# ----------------------------------------------------------------------
# Operators (mirror of Operator Literal in db.models)
# ----------------------------------------------------------------------
OP_GE, OP_GT, OP_LE, OP_LT, OP_EQ, OP_NE, OP_TREND = (
    ">=", ">", "<=", "<", "==", "!=", "trend==",
)
ALL_OPERATORS: frozenset[str] = frozenset(
    {OP_GE, OP_GT, OP_LE, OP_LT, OP_EQ, OP_NE, OP_TREND}
)

_HIGH = frozenset({OP_GT, OP_GE})           # alert when value is high
_LOW = frozenset({OP_LT, OP_LE})            # alert when value is low
_NUMERIC = _HIGH | _LOW                      # either direction


# type → operators that are semantically valid for it
ALLOWED_OPS: dict[FactType, frozenset[str]] = {
    FactType.MAX: _HIGH,
    FactType.MIN: _LOW | {OP_EQ},            # state_check required: min == 0
    FactType.AVG: _NUMERIC,
    FactType.LAST: _NUMERIC | {OP_EQ, OP_NE},
    FactType.P50: _NUMERIC,
    FactType.P90: _NUMERIC,
    FactType.P95: _NUMERIC,
    FactType.P99: _NUMERIC,
    FactType.SPIKE_COUNT: _HIGH,
    FactType.DURATION: _HIGH,
    FactType.DELTA: _NUMERIC | {OP_NE},
    FactType.GROWTH_RATE: _NUMERIC,
    FactType.MOVING_AVG: _NUMERIC,
    FactType.TREND: frozenset({OP_TREND}),
    FactType.ZSCORE: _HIGH,
    FactType.BASELINE_DEV: _NUMERIC,
}


# implementation phase per fact type
PHASE_OF_FACT: dict[FactType, int] = {
    FactType.MAX: 1,
    FactType.MIN: 1,
    FactType.AVG: 1,
    FactType.LAST: 1,
    FactType.P50: 1,
    FactType.P90: 1,
    FactType.P95: 1,
    FactType.P99: 1,
    FactType.SPIKE_COUNT: 1,
    FactType.DURATION: 2,
    FactType.DELTA: 2,
    FactType.GROWTH_RATE: 2,
    FactType.MOVING_AVG: 2,
    FactType.TREND: 2,
    FactType.ZSCORE: 2,
    FactType.BASELINE_DEV: 3,
}

# phases the engine currently evaluates; others are schema-accepted but skipped
IMPLEMENTED_PHASES: frozenset[int] = frozenset({1})


# ----------------------------------------------------------------------
# Parameter-requirement sets (used by Measure/Fact validators)
# ----------------------------------------------------------------------
# facts needing measure-level ``bucketing`` (date_histogram sub-window)
NEEDS_BUCKETING: frozenset[FactType] = frozenset(
    {FactType.DURATION, FactType.GROWTH_RATE, FactType.MOVING_AVG, FactType.TREND}
)
# facts additionally needing ``bucketing.points``
NEEDS_POINTS: frozenset[FactType] = frozenset({FactType.MOVING_AVG, FactType.TREND})
# facts needing measure-level ``baseline`` config
NEEDS_BASELINE: frozenset[FactType] = frozenset({FactType.BASELINE_DEV})
# facts whose Fact entry must carry ``over`` + ``direction`` (event boundary)
REQUIRES_OVER_DIRECTION: frozenset[FactType] = frozenset(
    {FactType.SPIKE_COUNT, FactType.DURATION}
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def is_implemented(fact_type: FactType) -> bool:
    """True if the engine evaluates this fact today (Phase 1)."""
    return PHASE_OF_FACT[fact_type] in IMPLEMENTED_PHASES


def op_allowed(fact_type: FactType, op: str) -> bool:
    """True if ``op`` is semantically valid for ``fact_type``."""
    return op in ALLOWED_OPS[fact_type]


# ----------------------------------------------------------------------
# ES aggregation strategy per fact type (drives es.queries + es_parser)
# ----------------------------------------------------------------------
class AggStrategy(StrEnum):
    """How a fact is computed from ``EARS_VALUE`` in Elasticsearch."""

    STAT = "stat"                   # single-metric agg (max/min/avg) -> .value
    PERCENTILES = "percentiles"     # percentiles agg -> .values["<pct>.0"]
    TOP_HITS = "top_hits"           # last: newest doc's EARS_VALUE
    FILTER_RANGE = "filter_range"   # spike_count: filter{range} -> .doc_count
    DATE_HISTOGRAM = "date_histogram"  # Phase 2 time-bucketed (duration/growth/ma/trend)
    EXTENDED_STATS = "extended_stats"  # Phase 2 zscore (avg + std_deviation)
    BASELINE_QUERY = "baseline_query"  # Phase 3 baseline_dev (separate past-index query)


AGG_STRATEGY: dict[FactType, AggStrategy] = {
    FactType.MAX: AggStrategy.STAT,
    FactType.MIN: AggStrategy.STAT,
    FactType.AVG: AggStrategy.STAT,
    FactType.LAST: AggStrategy.TOP_HITS,
    FactType.P50: AggStrategy.PERCENTILES,
    FactType.P90: AggStrategy.PERCENTILES,
    FactType.P95: AggStrategy.PERCENTILES,
    FactType.P99: AggStrategy.PERCENTILES,
    FactType.SPIKE_COUNT: AggStrategy.FILTER_RANGE,
    FactType.DURATION: AggStrategy.DATE_HISTOGRAM,
    FactType.DELTA: AggStrategy.TOP_HITS,
    FactType.GROWTH_RATE: AggStrategy.DATE_HISTOGRAM,
    FactType.MOVING_AVG: AggStrategy.DATE_HISTOGRAM,
    FactType.TREND: AggStrategy.DATE_HISTOGRAM,
    FactType.ZSCORE: AggStrategy.EXTENDED_STATS,
    FactType.BASELINE_DEV: AggStrategy.BASELINE_QUERY,
}

# ES single-metric aggregation name for STAT-strategy facts.
STAT_AGG_NAME: dict[FactType, str] = {
    FactType.MAX: "max",
    FactType.MIN: "min",
    FactType.AVG: "avg",
}

# percentile number for PERCENTILES-strategy facts.
PERCENTILE_OF_FACT: dict[FactType, float] = {
    FactType.P50: 50.0,
    FactType.P90: 90.0,
    FactType.P95: 95.0,
    FactType.P99: 99.0,
}


def agg_strategy(fact_type: FactType) -> AggStrategy:
    """ES aggregation strategy used to compute ``fact_type``."""
    return AGG_STRATEGY[fact_type]
