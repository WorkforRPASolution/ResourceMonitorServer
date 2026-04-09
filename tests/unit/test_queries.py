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
