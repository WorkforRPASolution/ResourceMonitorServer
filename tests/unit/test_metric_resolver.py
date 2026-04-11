"""Tests for src.analyzer.metric_resolver — pattern matching and agg type resolution."""
from __future__ import annotations

import pytest

from src.analyzer.metric_resolver import get_agg_type, resolve_metric_patterns


@pytest.mark.unit
class TestResolveMetricPatterns:
    def test_literal_pattern_matches_exact_field(self):
        result = resolve_metric_patterns(
            ["total_used_pct"], ["total_used_pct", "total_used_size"]
        )
        assert result == {"total_used_pct": ["total_used_pct"]}

    def test_wildcard_star_prefix_matches_multiple(self):
        fields = ["cpu0_core_load", "cpu1_core_load", "memory_used"]
        result = resolve_metric_patterns(["*_core_load"], fields)
        assert result == {"*_core_load": ["cpu0_core_load", "cpu1_core_load"]}

    def test_wildcard_star_suffix_matches(self):
        fields = ["total_used_pct", "total_used_size", "swap_used"]
        result = resolve_metric_patterns(["total_*"], fields)
        assert result == {"total_*": ["total_used_pct", "total_used_size"]}

    def test_no_match_returns_empty_list(self):
        result = resolve_metric_patterns(["nonexistent_*"], ["cpu_load", "mem_used"])
        assert result == {"nonexistent_*": []}

    def test_multiple_patterns_returns_dict(self):
        fields = ["cpu0_core_load", "cpu1_core_load", "total_used_pct"]
        result = resolve_metric_patterns(["*_core_load", "total_used_pct"], fields)
        assert result == {
            "*_core_load": ["cpu0_core_load", "cpu1_core_load"],
            "total_used_pct": ["total_used_pct"],
        }


@pytest.mark.unit
class TestGetAggType:
    def test_agg_type_required_is_state_check(self):
        assert get_agg_type("process_watch", "required") == "state_check"

    def test_agg_type_forbidden_is_state_check(self):
        assert get_agg_type("process_watch", "forbidden") == "state_check"

    def test_agg_type_default_is_max(self):
        assert get_agg_type("cpu_*", "cpu0_core_load") == "max"
