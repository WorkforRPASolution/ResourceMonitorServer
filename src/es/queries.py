"""Elasticsearch query builders for v2 metric analysis.

The real production documents are **EARS rows** (PRD §7.2): one document per
(equipment, metric, sample) carrying ``EARS_CATEGORY`` / ``EARS_METRIC`` /
``EARS_VALUE`` / ``EARS_EQPID`` / ``EARS_PROCNAME`` / ``EARS_TIMESTAMP``. The
metric identity is therefore a *filter* (term on EARS_CATEGORY + EARS_METRIC),
not a top-level numeric field; every fact aggregates the single ``EARS_VALUE``
column. The EARS_* string fields are mapped as ``keyword`` (no text+.keyword
subfield), so term filters and terms aggregations target the bare field name.

Aggregation nesting (outer→inner):
    by_eqp (terms EARS_EQPID)
      └─ by_proc (terms EARS_PROCNAME)         # only when measure.proc == "*"
           └─ by_metric (terms EARS_METRIC)    # only when expand == "instance"
                └─ one sub-agg per fact, keyed by the fact's type name
``src.analyzer.es_parser`` mirrors this nesting; the sub-agg key (= fact type
name) is the contract between the two modules.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.analyzer import fact_catalog as fc
from src.config.settings import AppSettings

# EARS_* field roles (PRD §7.2). All keyword fields are queried by bare name.
TS_FIELD = "EARS_TIMESTAMP"
VALUE_FIELD = "EARS_VALUE"
CATEGORY_FIELD = "EARS_CATEGORY"
METRIC_FIELD = "EARS_METRIC"
PROC_FIELD = "EARS_PROCNAME"
EQP_FIELD = "EARS_EQPID"


def build_fact_sub_aggs(facts: Iterable[Any]) -> dict[str, dict]:
    """Build the leaf sub-aggregations (one per fact) over ``EARS_VALUE``.

    Each fact is keyed by its type name (e.g. ``"max"``, ``"p95"``,
    ``"spike_count"``). Only Phase-1 strategies are emitted; Phase 2/3 facts are
    skipped here (the engine never asks for them).
    """
    sub: dict[str, dict] = {}
    for fact in facts:
        ftype = fact.type
        name = ftype.value
        strat = fc.agg_strategy(ftype)
        if strat is fc.AggStrategy.STAT:
            sub[name] = {fc.STAT_AGG_NAME[ftype]: {"field": VALUE_FIELD}}
        elif strat is fc.AggStrategy.PERCENTILES:
            sub[name] = {
                "percentiles": {
                    "field": VALUE_FIELD,
                    "percents": [fc.PERCENTILE_OF_FACT[ftype]],
                }
            }
        elif strat is fc.AggStrategy.TOP_HITS:
            sub[name] = {
                "top_hits": {
                    "size": 1,
                    "sort": [{TS_FIELD: {"order": "desc"}}],
                    "_source": [VALUE_FIELD],
                }
            }
        elif strat is fc.AggStrategy.FILTER_RANGE:
            rng = {"gte": fact.over} if fact.direction == "above" else {"lte": fact.over}
            sub[name] = {"filter": {"range": {VALUE_FIELD: rng}}}
        # Phase 2/3 strategies (date_histogram / extended_stats / baseline) are
        # not emitted — those facts are engine-skipped until implemented.
    return sub


class QueryBuilder:
    """Stateless helper. Holds `AppSettings` only to read `local_tz`."""

    _TERMS_AGG_SIZE = 30000  # 20K PCs + headroom
    _METRIC_TERMS_SIZE = 1000  # distinct metric instances per equipment
    _PROC_TERMS_SIZE = 1000  # distinct procnames per equipment

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # Index resolution
    # ------------------------------------------------------------------
    def resolve_index_range(self, process: str, time_range_minutes: int) -> str:
        """Return comma-separated index pattern covering the query window.

        Index naming convention: ``{process_lower}_all-{YYYY.MM.DD}`` (one per
        day). If the window crosses midnight in ``local_tz``, return two day
        indexes; otherwise a single day index. ES 7.x accepts comma-separated
        lists as-is in the ``index`` parameter.
        """
        tz = ZoneInfo(self._settings.local_tz)
        now = datetime.now(tz)
        start = now - timedelta(minutes=time_range_minutes)
        proc = process.lower()
        end_day = now.strftime("%Y.%m.%d")
        if start.date() == now.date():
            return f"{proc}_all-{end_day}"
        start_day = start.strftime("%Y.%m.%d")
        return f"{proc}_all-{start_day},{proc}_all-{end_day}"

    # ------------------------------------------------------------------
    # Query fragments
    # ------------------------------------------------------------------
    def build_time_range_filter(self, now: datetime, window_minutes: int) -> dict:
        """ES ``range`` filter on ``EARS_TIMESTAMP`` for the trailing window."""
        start = now - timedelta(minutes=window_minutes)
        return {
            "range": {
                TS_FIELD: {
                    "gte": start.isoformat(),
                    "lte": now.isoformat(),
                    "format": "strict_date_optional_time",
                }
            }
        }

    def build_metric_names_query(
        self, now: datetime, window_minutes: int, category: str, proc: str = "@system"
    ) -> dict:
        """``size=0`` query enumerating the distinct ``EARS_METRIC`` values for a
        category (used to resolve wildcard metric patterns to instances)."""
        filters: list[dict] = [
            self.build_time_range_filter(now, window_minutes),
            {"term": {CATEGORY_FIELD: category}},
        ]
        if proc != "*":
            filters.append({"term": {PROC_FIELD: proc}})
        return {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "metrics": {
                    "terms": {"field": METRIC_FIELD, "size": self._METRIC_TERMS_SIZE}
                }
            },
        }

    # ------------------------------------------------------------------
    # Phase 1: metric aggregation query (EARS_* rows)
    # ------------------------------------------------------------------
    def build_metric_aggregation_query(
        self,
        now: datetime,
        *,
        window_minutes: int,
        category: str,
        metrics: list[str],
        proc: str,
        facts: Iterable[Any],
        expand_instance: bool = False,
        eqp_ids: list[str] | None = None,
    ) -> dict:
        """Build an ES 7.x ``size=0`` search body for one measure's facts.

        ``metrics`` are the concrete EARS_METRIC instance names to include (a
        single-element list for a scalar measure, the resolved instances for a
        wildcard one). ``proc == "*"`` groups by EARS_PROCNAME instead of
        filtering it. ``expand_instance`` groups by EARS_METRIC so each instance
        is a separate quantifier sample. ``eqp_ids`` optionally restricts the
        terms aggregation to one equipment bucket.
        """
        facts = list(facts)
        leaf = build_fact_sub_aggs(facts)

        inner: dict = leaf
        if expand_instance:
            inner = {
                "by_metric": {
                    "terms": {"field": METRIC_FIELD, "size": self._METRIC_TERMS_SIZE},
                    "aggs": inner,
                }
            }
        if proc == "*":
            inner = {
                "by_proc": {
                    "terms": {"field": PROC_FIELD, "size": self._PROC_TERMS_SIZE},
                    "aggs": inner,
                }
            }

        filters: list[dict] = [
            self.build_time_range_filter(now, window_minutes),
            {"term": {CATEGORY_FIELD: category}},
        ]
        if metrics:
            filters.append({"terms": {METRIC_FIELD: metrics}})
        if proc != "*":
            filters.append({"term": {PROC_FIELD: proc}})
        if eqp_ids:
            filters.append({"terms": {EQP_FIELD: eqp_ids}})

        return {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "by_eqp": {
                    "terms": {"field": EQP_FIELD, "size": self._TERMS_AGG_SIZE},
                    "aggs": inner,
                }
            },
        }
