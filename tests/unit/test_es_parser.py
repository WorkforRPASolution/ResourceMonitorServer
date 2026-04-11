"""Tests for src.analyzer.es_parser — ES aggregation response parsing."""
from __future__ import annotations

import pytest

from src.analyzer.es_parser import parse_metric_aggregation


@pytest.mark.unit
class TestParseMetricAggregation:
    def test_parses_single_eqp_single_metric(self):
        response = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "EQP1",
                            "total_used_pct_max": {"value": 85.5},
                        }
                    ]
                }
            }
        }
        result = parse_metric_aggregation(
            response, ["total_used_pct"], {"total_used_pct": "max"}
        )
        assert result == {"EQP1": {"total_used_pct": 85.5}}

    def test_parses_multiple_eqps_multiple_metrics(self):
        response = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "EQP1",
                            "cpu_load_max": {"value": 90.0},
                            "mem_used_max": {"value": 75.0},
                        },
                        {
                            "key": "EQP2",
                            "cpu_load_max": {"value": 60.0},
                            "mem_used_max": {"value": 55.0},
                        },
                    ]
                }
            }
        }
        result = parse_metric_aggregation(
            response, ["cpu_load", "mem_used"], {"cpu_load": "max", "mem_used": "max"}
        )
        assert result == {
            "EQP1": {"cpu_load": 90.0, "mem_used": 75.0},
            "EQP2": {"cpu_load": 60.0, "mem_used": 55.0},
        }

    def test_missing_agg_value_returns_none(self):
        response = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "EQP1",
                            # no sub-agg for total_used_pct
                        }
                    ]
                }
            }
        }
        result = parse_metric_aggregation(
            response, ["total_used_pct"], {"total_used_pct": "max"}
        )
        assert result == {"EQP1": {"total_used_pct": None}}

    def test_empty_buckets_returns_empty_dict(self):
        response = {"aggregations": {"by_eqp": {"buckets": []}}}
        result = parse_metric_aggregation(
            response, ["cpu_load"], {"cpu_load": "max"}
        )
        assert result == {}

    def test_handles_missing_aggregations_key(self):
        response = {}
        result = parse_metric_aggregation(
            response, ["cpu_load"], {"cpu_load": "max"}
        )
        assert result == {}

    def test_different_agg_types(self):
        """Verify max vs min vs avg sub-agg keys are read correctly."""
        response = {
            "aggregations": {
                "by_eqp": {
                    "buckets": [
                        {
                            "key": "EQP1",
                            "cpu_load_max": {"value": 95.0},
                            "required_min": {"value": 1.0},
                            "temp_avg": {"value": 42.5},
                        }
                    ]
                }
            }
        }
        result = parse_metric_aggregation(
            response,
            ["cpu_load", "required", "temp"],
            {"cpu_load": "max", "required": "min", "temp": "avg"},
        )
        assert result == {
            "EQP1": {"cpu_load": 95.0, "required": 1.0, "temp": 42.5}
        }
