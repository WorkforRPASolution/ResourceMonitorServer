"""Elasticsearch query builders for metric analysis.

Phase 0 scope: index range resolution + basic time range filter.
Aggregation builders (terms/percentile/range union for baseline) are stubbed
and will be fleshed out in Phase 1 analysis.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config.settings import AppSettings


class QueryBuilder:
    """Stateless helper. Holds `AppSettings` only to read `local_tz`."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # Index resolution
    # ------------------------------------------------------------------
    def resolve_index_range(self, process: str, time_range_minutes: int) -> str:
        """Return comma-separated index pattern covering the query window.

        Index naming convention: ``{process_lower}_all-{YYYY.MM.DD}`` (one per day).
        If the window crosses midnight in ``local_tz``, return two day indexes;
        otherwise return a single day index.

        ES 7.x accepts comma-separated lists as-is in the `index` parameter.
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
    def build_time_range_filter(
        self, now: datetime, window_minutes: int
    ) -> dict:
        """Return an ES `range` filter on `@timestamp` for the trailing window.

        Timestamps are emitted in the caller-supplied timezone-aware ISO8601
        format. ES 7.x `strict_date_optional_time` parses this without extra
        format hints.
        """
        start = now - timedelta(minutes=window_minutes)
        return {
            "range": {
                "@timestamp": {
                    "gte": start.isoformat(),
                    "lte": now.isoformat(),
                    "format": "strict_date_optional_time",
                }
            }
        }

    # ------------------------------------------------------------------
    # Phase 1: metric aggregation query
    # ------------------------------------------------------------------
    _TERMS_AGG_SIZE = 30000  # 20K PCs + headroom

    def build_metric_aggregation_query(
        self,
        now: datetime,
        window_minutes: int,
        metric_fields: list[str],
        agg_types: dict[str, str] | None = None,
        process: str | None = None,
    ) -> dict:
        """Build an ES 7.x search body for metric aggregation.

        Returns a ``size=0`` query with a ``terms`` aggregation on
        ``eqpId.keyword`` and one sub-aggregation per metric field.
        The sub-aggregation type (max/min/avg) is determined by
        ``agg_types``; defaults to ``max`` if not specified.

        ``process`` adds a term filter on ``process.keyword`` as a safety
        guard — the index pattern already scopes to a single process, but
        this prevents cross-contamination if the naming convention changes.
        """
        if agg_types is None:
            agg_types = {}

        sub_aggs: dict = {}
        for field in metric_fields:
            agg = agg_types.get(field, "max")
            sub_aggs[f"{field}_{agg}"] = {agg: {"field": field}}

        filters: list[dict] = [
            self.build_time_range_filter(now, window_minutes),
        ]
        if process is not None:
            filters.append({"term": {"process.keyword": process}})

        return {
            "size": 0,
            "query": {
                "bool": {
                    "filter": filters,
                }
            },
            "aggs": {
                "by_eqp": {
                    "terms": {
                        "field": "eqpId.keyword",
                        "size": self._TERMS_AGG_SIZE,
                    },
                    "aggs": sub_aggs,
                }
            },
        }
