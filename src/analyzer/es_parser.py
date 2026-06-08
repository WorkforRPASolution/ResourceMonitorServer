"""Parse v2 EARS_* aggregation responses into per-(eqp, proc) fact values.

Mirrors the nesting built by :class:`src.es.queries.QueryBuilder`:
    by_eqp → [by_proc] → [by_metric] → one sub-agg per fact (keyed by type name)

Return shape: ``{(eqp_id, proc): {fact_type_name: [value, ...]}}`` where the
list holds one value per metric instance (length 1 for a scalar measure, N for
an ``expand="instance"`` measure). The instance list is what rule quantifiers
(any/all/count) range over. ``None`` marks a missing/empty sub-agg.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.analyzer import fact_catalog as fc
from src.es.queries import VALUE_FIELD


def _read_fact(bucket: dict, fact: Any) -> float | None:
    """Extract one fact's value from its leaf sub-agg bucket."""
    name = fact.type.value
    sub = bucket.get(name)
    if sub is None:
        return None
    strat = fc.agg_strategy(fact.type)
    if strat is fc.AggStrategy.STAT:
        return sub.get("value")
    if strat is fc.AggStrategy.FILTER_RANGE:
        return sub.get("doc_count")
    if strat is fc.AggStrategy.PERCENTILES:
        values = sub.get("values", {})
        pct = fc.PERCENTILE_OF_FACT[fact.type]
        for key in (f"{pct:.1f}", str(pct), str(int(pct))):
            if key in values:
                return values[key]
        return None
    if strat is fc.AggStrategy.TOP_HITS:
        hits = sub.get("hits", {}).get("hits", [])
        if not hits:
            return None
        return hits[0].get("_source", {}).get(VALUE_FIELD)
    return None


def parse_metric_aggregation(
    response: dict,
    facts: Iterable[Any],
    *,
    proc: str,
    expand_instance: bool = False,
) -> dict[tuple[str, str], dict[str, list[float | None]]]:
    """Parse an EARS_* aggregation response.

    ``facts`` is the list of Fact objects the query requested (objects with a
    ``.type`` FactType). ``proc`` is the measure's proc (used as the proc-key
    when not grouped by proc). ``expand_instance`` must match the query.
    """
    facts = list(facts)
    result: dict[tuple[str, str], dict[str, list[float | None]]] = {}
    aggs = response.get("aggregations", {})
    by_eqp = aggs.get("by_eqp", {})

    for eqp_bucket in by_eqp.get("buckets", []):
        eqp = eqp_bucket["key"]
        if "by_proc" in eqp_bucket:
            proc_buckets = eqp_bucket["by_proc"].get("buckets", [])
        else:
            proc_buckets = [{**eqp_bucket, "key": proc}]

        for proc_bucket in proc_buckets:
            pkey = proc_bucket["key"]
            if expand_instance and "by_metric" in proc_bucket:
                inst_buckets = proc_bucket["by_metric"].get("buckets", [])
            else:
                inst_buckets = [proc_bucket]

            fact_values: dict[str, list[float | None]] = {
                f.type.value: [] for f in facts
            }
            for inst in inst_buckets:
                for fact in facts:
                    fact_values[fact.type.value].append(_read_fact(inst, fact))
            result[(eqp, pkey)] = fact_values

    return result
