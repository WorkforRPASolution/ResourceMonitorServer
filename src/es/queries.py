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
