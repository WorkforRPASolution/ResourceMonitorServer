"""Tests for src.analyzer.es_parser — v2 EARS_* response parsing."""
from __future__ import annotations

import pytest

from src.analyzer.es_parser import parse_metric_aggregation
from src.db.models import Fact

pytestmark = pytest.mark.unit


class TestScalarMeasure:
    def test_single_eqp_single_fact(self):
        resp = {
            "aggregations": {
                "by_eqp": {"buckets": [{"key": "EQP1", "max": {"value": 85.5}}]}
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="max")], proc="@system")
        assert result == {("EQP1", "@system"): {"max": [85.5]}}

    def test_multiple_eqps_and_facts(self):
        resp = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {"key": "E1", "max": {"value": 90.0}, "avg": {"value": 50.0}},
                        {"key": "E2", "max": {"value": 60.0}, "avg": {"value": 40.0}},
                    ]
                }
            }
        }
        result = parse_metric_aggregation(
            resp, [Fact(type="max"), Fact(type="avg")], proc="@system"
        )
        assert result[("E1", "@system")] == {"max": [90.0], "avg": [50.0]}
        assert result[("E2", "@system")] == {"max": [60.0], "avg": [40.0]}

    def test_missing_subagg_is_none(self):
        resp = {"aggregations": {"by_eqp": {"buckets": [{"key": "E1"}]}}}
        result = parse_metric_aggregation(resp, [Fact(type="max")], proc="@system")
        assert result == {("E1", "@system"): {"max": [None]}}

    def test_empty_buckets(self):
        resp = {"aggregations": {"by_eqp": {"buckets": []}}}
        assert parse_metric_aggregation(resp, [Fact(type="max")], proc="@system") == {}

    def test_missing_aggregations_key(self):
        assert parse_metric_aggregation({}, [Fact(type="max")], proc="@system") == {}


class TestFactTypes:
    def test_percentile_reads_values_dict(self):
        resp = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [{"key": "E1", "p95": {"values": {"95.0": 88.0}}}]
                }
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="p95")], proc="@system")
        assert result == {("E1", "@system"): {"p95": [88.0]}}

    def test_percentile_zero_value_not_lost(self):
        resp = {
            "aggregations": {
                "by_eqp": {"buckets": [{"key": "E1", "p50": {"values": {"50.0": 0.0}}}]}
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="p50")], proc="@system")
        assert result == {("E1", "@system"): {"p50": [0.0]}}

    def test_percentile_null_is_none(self):
        resp = {
            "aggregations": {
                "by_eqp": {"buckets": [{"key": "E1", "p99": {"values": {"99.0": None}}}]}
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="p99")], proc="@system")
        assert result == {("E1", "@system"): {"p99": [None]}}

    def test_spike_count_reads_doc_count(self):
        resp = {
            "aggregations": {
                "by_eqp": {"buckets": [{"key": "E1", "spike_count": {"doc_count": 7}}]}
            }
        }
        facts = [Fact(type="spike_count", over=90, direction="above")]
        result = parse_metric_aggregation(resp, facts, proc="@system")
        assert result == {("E1", "@system"): {"spike_count": [7]}}

    def test_last_reads_top_hit_source(self):
        resp = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "E1",
                            "last": {
                                "hits": {"hits": [{"_source": {"EARS_VALUE": 42.0}}]}
                            },
                        }
                    ]
                }
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="last")], proc="@system")
        assert result == {("E1", "@system"): {"last": [42.0]}}

    def test_last_empty_hits_is_none(self):
        resp = {
            "aggregations": {
                "by_eqp": {"buckets": [{"key": "E1", "last": {"hits": {"hits": []}}}]}
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="last")], proc="@system")
        assert result == {("E1", "@system"): {"last": [None]}}


class TestProcGrouping:
    def test_by_proc_buckets_become_separate_keys(self):
        resp = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "E1",
                            "by_proc": {
                                "buckets": [
                                    {"key": "procA", "min": {"value": 0.0}},
                                    {"key": "procB", "min": {"value": 1.0}},
                                ]
                            },
                        }
                    ]
                }
            }
        }
        result = parse_metric_aggregation(resp, [Fact(type="min")], proc="*")
        assert result == {
            ("E1", "procA"): {"min": [0.0]},
            ("E1", "procB"): {"min": [1.0]},
        }


class TestInstanceExpansion:
    def test_by_metric_values_collected_into_list(self):
        resp = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "E1",
                            "by_metric": {
                                "buckets": [
                                    {"key": "C", "max": {"value": 80.0}},
                                    {"key": "D", "max": {"value": 96.0}},
                                    {"key": "E", "max": {"value": 50.0}},
                                ]
                            },
                        }
                    ]
                }
            }
        }
        result = parse_metric_aggregation(
            resp, [Fact(type="max")], proc="@system", expand_instance=True
        )
        assert result == {("E1", "@system"): {"max": [80.0, 96.0, 50.0]}}
