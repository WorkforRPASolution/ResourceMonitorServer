"""Tests for src.es.queries — pure logic (index range + query builders)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import time_machine

from src.config.settings import AppSettings
from src.es.queries import QueryBuilder


@pytest.fixture
def qb() -> QueryBuilder:
    return QueryBuilder(AppSettings(local_tz="Asia/Seoul"))


@pytest.mark.unit
class TestResolveIndexRange:
    def test_within_single_day_returns_one_index(self, qb):
        """If start and end fall on the same day, return a single index pattern."""
        # 2026-04-07 12:00 KST, 5 minute window → start 11:55
        with time_machine.travel(
            datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ):
            result = qb.resolve_index_range("CVD", time_range_minutes=5)
        assert result == "cvd_all-2026.04.07"

    def test_crosses_midnight_returns_two_indexes(self, qb):
        """If window crosses midnight, return both days as comma-separated pattern."""
        # 2026-04-07 00:02 KST, 5 minute window → start 2026-04-06 23:57
        with time_machine.travel(
            datetime(2026, 4, 7, 0, 2, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ):
            result = qb.resolve_index_range("CVD", time_range_minutes=5)
        assert result == "cvd_all-2026.04.06,cvd_all-2026.04.07"

    def test_process_is_lowercased(self, qb):
        with time_machine.travel(
            datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ):
            result = qb.resolve_index_range("ETCH", time_range_minutes=5)
        assert result == "etch_all-2026.04.07"

    def test_respects_configured_timezone(self):
        """Index date must be computed in the configured local timezone, not UTC."""
        # 2026-04-07 00:30 KST == 2026-04-06 15:30 UTC
        # If we used UTC, we'd pick "2026.04.06" — but local_tz=Asia/Seoul means 04.07
        qb = QueryBuilder(AppSettings(local_tz="Asia/Seoul"))
        with time_machine.travel(
            datetime(2026, 4, 6, 15, 30, 0, tzinfo=ZoneInfo("UTC"))
        ):
            result = qb.resolve_index_range("CVD", time_range_minutes=5)
        # Start: 00:25 KST, end: 00:30 KST → same day (2026-04-07)
        assert result == "cvd_all-2026.04.07"

    def test_long_window_still_just_two_days(self, qb):
        """Even with a 120 min window crossing midnight, we return two days."""
        with time_machine.travel(
            datetime(2026, 4, 7, 0, 30, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ):
            result = qb.resolve_index_range("CVD", time_range_minutes=120)
        assert result == "cvd_all-2026.04.06,cvd_all-2026.04.07"


@pytest.mark.unit
class TestBuildTimeRangeQuery:
    def test_range_filter_structure(self, qb):
        """build_time_range_filter returns an ES range filter on @timestamp."""
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        filter_ = qb.build_time_range_filter(now, window_minutes=5)
        assert "range" in filter_
        assert "@timestamp" in filter_["range"]
        assert "gte" in filter_["range"]["@timestamp"]
        assert "lte" in filter_["range"]["@timestamp"]

    def test_window_minutes_applied(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        filter_ = qb.build_time_range_filter(now, window_minutes=5)
        gte = filter_["range"]["@timestamp"]["gte"]
        lte = filter_["range"]["@timestamp"]["lte"]
        # gte should be 5 minutes earlier than lte
        assert "11:55" in gte
        assert "12:00" in lte


# ----------------------------------------------------------------------
# Phase 1: build_metric_aggregation_query
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestBuildMetricAggregationQuery:
    def test_size_is_zero(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10, metric_fields=["total_used_pct"]
        )
        assert body["size"] == 0

    def test_has_time_range_filter(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10, metric_fields=["total_used_pct"]
        )
        filters = body["query"]["bool"]["filter"]
        assert any("range" in f for f in filters)

    def test_terms_on_eqp_id_with_size_30000(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10, metric_fields=["total_used_pct"]
        )
        by_eqp = body["aggs"]["by_eqp"]
        assert by_eqp["terms"]["field"] == "eqpId.keyword"
        assert by_eqp["terms"]["size"] == 30000

    def test_default_agg_type_is_max(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10, metric_fields=["total_used_pct"]
        )
        sub_aggs = body["aggs"]["by_eqp"]["aggs"]
        assert "total_used_pct_max" in sub_aggs
        assert sub_aggs["total_used_pct_max"] == {"max": {"field": "total_used_pct"}}

    def test_custom_agg_types(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10,
            metric_fields=["required", "forbidden", "total_used_pct"],
            agg_types={"required": "min", "forbidden": "max", "total_used_pct": "avg"},
        )
        sub_aggs = body["aggs"]["by_eqp"]["aggs"]
        assert sub_aggs["required_min"] == {"min": {"field": "required"}}
        assert sub_aggs["forbidden_max"] == {"max": {"field": "forbidden"}}
        assert sub_aggs["total_used_pct_avg"] == {"avg": {"field": "total_used_pct"}}

    def test_multiple_metrics(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=5,
            metric_fields=["cpu0_core_load", "cpu1_core_load"],
        )
        sub_aggs = body["aggs"]["by_eqp"]["aggs"]
        assert "cpu0_core_load_max" in sub_aggs
        assert "cpu1_core_load_max" in sub_aggs

    def test_empty_metric_fields_still_valid(self, qb):
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10, metric_fields=[]
        )
        assert body["size"] == 0
        assert body["aggs"]["by_eqp"]["aggs"] == {}

    def test_includes_process_filter(self, qb):
        """ES query must filter by process even though the index is per-process."""
        now = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        body = qb.build_metric_aggregation_query(
            now, window_minutes=10, metric_fields=["total_used_pct"],
            process="CVD",
        )
        filters = body["query"]["bool"]["filter"]
        process_filters = [
            f for f in filters
            if "term" in f and "process.keyword" in f.get("term", {})
        ]
        assert len(process_filters) == 1
        assert process_filters[0]["term"]["process.keyword"] == "CVD"
