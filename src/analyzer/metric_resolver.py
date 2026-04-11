"""Resolve metric pattern wildcards to actual ES field names."""
from __future__ import annotations

import fnmatch


def resolve_metric_patterns(
    patterns: list[str], available_fields: list[str]
) -> dict[str, list[str]]:
    """For each pattern, find matching field names using fnmatch.
    Returns {pattern: [matched_fields]}. Non-matching patterns get empty list."""
    result = {}
    for pattern in patterns:
        result[pattern] = [f for f in available_fields if fnmatch.fnmatch(f, pattern)]
    return result


def get_agg_type(metric_pattern: str, field_name: str) -> str:
    """Determine the ES aggregation type for a metric.
    - process_watch fields (required, forbidden) -> "state_check"
    - Everything else -> "max"
    """
    if field_name in ("required", "forbidden"):
        return "state_check"
    return "max"
