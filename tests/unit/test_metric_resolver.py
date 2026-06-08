"""Tests for src.analyzer.metric_resolver — EARS_METRIC pattern matching."""
from __future__ import annotations

import pytest

from src.analyzer.metric_resolver import resolve_metric_patterns

pytestmark = pytest.mark.unit


class TestResolveMetricPatterns:
    def test_literal_pattern_matches_exact_metric(self):
        result = resolve_metric_patterns(
            ["total_used_pct"], ["total_used_pct", "total_used_size"]
        )
        assert result == {"total_used_pct": ["total_used_pct"]}

    def test_wildcard_star_suffix_matches_multiple(self):
        metrics = ["cpu0_core_load", "cpu1_core_load", "memory_used"]
        result = resolve_metric_patterns(["*_core_load"], metrics)
        assert result == {"*_core_load": ["cpu0_core_load", "cpu1_core_load"]}

    def test_wildcard_match_all(self):
        metrics = ["C:", "D:", "E:"]
        result = resolve_metric_patterns(["*"], metrics)
        assert result == {"*": ["C:", "D:", "E:"]}

    def test_no_match_returns_empty_list(self):
        result = resolve_metric_patterns(["nonexistent_*"], ["cpu_load", "mem_used"])
        assert result == {"nonexistent_*": []}

    def test_multiple_patterns(self):
        metrics = ["cpu0_core_load", "cpu1_core_load", "total_used_pct"]
        result = resolve_metric_patterns(["*_core_load", "total_used_pct"], metrics)
        assert result == {
            "*_core_load": ["cpu0_core_load", "cpu1_core_load"],
            "total_used_pct": ["total_used_pct"],
        }
