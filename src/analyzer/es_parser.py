"""Parse ES aggregation responses into structured metric dictionaries."""
from __future__ import annotations


def parse_metric_aggregation(
    response: dict,
    metric_fields: list[str],
    agg_types: dict[str, str],
) -> dict[str, dict[str, float | None]]:
    """Parse ES aggregation response into {eqp_id: {metric: value}}.

    agg_types maps field_name -> "max"|"min"|"avg" to know which sub-agg key to read.
    Sub-agg key format: "{field}_{agg_type}" e.g. "total_used_pct_max"
    """
    result: dict[str, dict[str, float | None]] = {}
    aggs = response.get("aggregations", {})
    by_eqp = aggs.get("by_eqp", {})
    buckets = by_eqp.get("buckets", [])

    for bucket in buckets:
        eqp_id = bucket["key"]
        metrics: dict[str, float | None] = {}
        for field in metric_fields:
            agg_type = agg_types.get(field, "max")
            agg_key = f"{field}_{agg_type}"
            sub_agg = bucket.get(agg_key, {})
            metrics[field] = sub_agg.get("value")
        result[eqp_id] = metrics

    return result
