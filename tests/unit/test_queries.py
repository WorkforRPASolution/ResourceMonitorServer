"""Tests for src.es.queries — index range + v2 EARS_* aggregation builders."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import time_machine

from src.config.settings import AppSettings
from src.db.models import Fact
from src.es.queries import QueryBuilder

pytestmark = pytest.mark.unit


@pytest.fixture
def qb() -> QueryBuilder:
    return QueryBuilder(AppSettings(local_tz="Asia/Seoul"))


NOW = datetime(2026, 4, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def _terms(filters, field):
    """Return the value of the first term/terms filter on `field`, or None."""
    for f in filters:
        if "term" in f and field in f["term"]:
            return f["term"][field]
        if "terms" in f and field in f["terms"]:
            return f["terms"][field]
    return None


class TestResolveIndexRange:
    def test_within_single_day_returns_one_index(self, qb):
        with time_machine.travel(NOW):
            assert qb.resolve_index_range("CVD", 5) == "cvd_all-2026.04.07"

    def test_crosses_midnight_returns_two_indexes(self, qb):
        with time_machine.travel(
            datetime(2026, 4, 7, 0, 2, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ):
            assert qb.resolve_index_range("CVD", 5) == (
                "cvd_all-2026.04.06,cvd_all-2026.04.07"
            )

    def test_process_is_lowercased(self, qb):
        with time_machine.travel(NOW):
            assert qb.resolve_index_range("ETCH", 5) == "etch_all-2026.04.07"

    def test_respects_configured_timezone(self, qb):
        with time_machine.travel(datetime(2026, 4, 6, 15, 30, 0, tzinfo=ZoneInfo("UTC"))):
            assert qb.resolve_index_range("CVD", 5) == "cvd_all-2026.04.07"


class TestBuildTimeRangeFilter:
    def test_range_filter_on_ears_timestamp(self, qb):
        f = qb.build_time_range_filter(NOW, window_minutes=5)
        assert "EARS_TIMESTAMP" in f["range"]
        assert "11:55" in f["range"]["EARS_TIMESTAMP"]["gte"]
        assert "12:00" in f["range"]["EARS_TIMESTAMP"]["lte"]


class TestBuildMetricAggregationQuery:
    def _q(self, qb, **over):
        params = {
            "window_minutes": 15,
            "category": "cpu",
            "metrics": ["total_used_pct"],
            "proc": "@system",
            "facts": [Fact(type="max")],
        }
        params.update(over)
        return qb.build_metric_aggregation_query(NOW, **params)

    def test_size_zero_and_terms_on_eqp(self, qb):
        body = self._q(qb)
        assert body["size"] == 0
        by_eqp = body["aggs"]["by_eqp"]
        assert by_eqp["terms"]["field"] == "EARS_EQPID"
        assert by_eqp["terms"]["size"] == 30000

    def test_category_and_metric_and_time_filters(self, qb):
        filters = self._q(qb)["query"]["bool"]["filter"]
        assert _terms(filters, "EARS_CATEGORY") == "cpu"
        assert _terms(filters, "EARS_METRIC") == ["total_used_pct"]
        assert any("range" in f and "EARS_TIMESTAMP" in f["range"] for f in filters)

    def test_proc_filter_when_concrete(self, qb):
        filters = self._q(qb, proc="@system")["query"]["bool"]["filter"]
        assert _terms(filters, "EARS_PROCNAME") == "@system"

    def test_max_subagg_over_ears_value(self, qb):
        leaf = self._q(qb)["aggs"]["by_eqp"]["aggs"]
        assert leaf["max"] == {"max": {"field": "EARS_VALUE"}}

    def test_percentile_subagg(self, qb):
        leaf = self._q(qb, facts=[Fact(type="p95")])["aggs"]["by_eqp"]["aggs"]
        assert leaf["p95"]["percentiles"]["field"] == "EARS_VALUE"
        assert leaf["p95"]["percentiles"]["percents"] == [95.0]

    def test_spike_count_filter_range_above(self, qb):
        facts = [Fact(type="spike_count", over=90, direction="above")]
        leaf = self._q(qb, facts=facts)["aggs"]["by_eqp"]["aggs"]
        assert leaf["spike_count"] == {
            "filter": {"range": {"EARS_VALUE": {"gte": 90}}}
        }

    def test_spike_count_filter_range_below(self, qb):
        facts = [Fact(type="spike_count", over=5, direction="below")]
        leaf = self._q(qb, facts=facts)["aggs"]["by_eqp"]["aggs"]
        assert leaf["spike_count"]["filter"]["range"]["EARS_VALUE"] == {"lte": 5}

    def test_last_top_hits(self, qb):
        leaf = self._q(qb, facts=[Fact(type="last")])["aggs"]["by_eqp"]["aggs"]
        th = leaf["last"]["top_hits"]
        assert th["size"] == 1
        assert th["sort"] == [{"EARS_TIMESTAMP": {"order": "desc"}}]

    def test_proc_wildcard_groups_by_procname(self, qb):
        body = self._q(qb, proc="*")
        by_eqp_aggs = body["aggs"]["by_eqp"]["aggs"]
        assert "by_proc" in by_eqp_aggs
        assert by_eqp_aggs["by_proc"]["terms"]["field"] == "EARS_PROCNAME"
        # leaf facts live under by_proc, not directly under by_eqp
        assert "max" in by_eqp_aggs["by_proc"]["aggs"]
        # proc not term-filtered when wildcard
        filters = body["query"]["bool"]["filter"]
        assert _terms(filters, "EARS_PROCNAME") is None

    def test_expand_instance_groups_by_metric(self, qb):
        body = self._q(
            qb, metrics=["C", "D"], expand_instance=True, facts=[Fact(type="max")]
        )
        by_eqp_aggs = body["aggs"]["by_eqp"]["aggs"]
        assert "by_metric" in by_eqp_aggs
        assert by_eqp_aggs["by_metric"]["terms"]["field"] == "EARS_METRIC"
        assert "max" in by_eqp_aggs["by_metric"]["aggs"]

    def test_eqp_ids_restrict_filter(self, qb):
        filters = self._q(qb, eqp_ids=["E1", "E2"])["query"]["bool"]["filter"]
        assert _terms(filters, "EARS_EQPID") == ["E1", "E2"]


class TestBuildMetricNamesQuery:
    def test_terms_on_metric_with_category_filter(self, qb):
        body = qb.build_metric_names_query(NOW, 15, "disk", proc="@system")
        assert body["aggs"]["metrics"]["terms"]["field"] == "EARS_METRIC"
        filters = body["query"]["bool"]["filter"]
        assert _terms(filters, "EARS_CATEGORY") == "disk"
        assert _terms(filters, "EARS_PROCNAME") == "@system"
