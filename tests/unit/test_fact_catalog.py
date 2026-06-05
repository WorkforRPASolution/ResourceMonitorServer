"""Fact (=type) catalog — single source of truth for v2 fact types.

The catalog is shared by db.models (validation), analyzer.metric_resolver
(ES agg mapping) and analyzer.threshold (rule evaluation), so it must be
internally complete and consistent.
"""
import pytest

from src.analyzer import fact_catalog as fc
from src.analyzer.fact_catalog import FactType

pytestmark = pytest.mark.unit


class TestCatalogCompleteness:
    def test_every_facttype_has_allowed_ops(self):
        assert set(fc.ALLOWED_OPS) == set(FactType)

    def test_every_facttype_has_phase(self):
        assert set(fc.PHASE_OF_FACT) == set(FactType)

    def test_phases_are_1_2_3(self):
        assert set(fc.PHASE_OF_FACT.values()) <= {1, 2, 3}

    def test_allowed_ops_are_known_operators(self):
        for ops in fc.ALLOWED_OPS.values():
            assert ops <= fc.ALL_OPERATORS


class TestPhaseGating:
    def test_phase1_facts_implemented(self):
        for t in (
            FactType.MAX, FactType.MIN, FactType.AVG, FactType.LAST,
            FactType.P95, FactType.SPIKE_COUNT,
        ):
            assert fc.is_implemented(t)

    def test_phase2_and_3_not_implemented(self):
        for t in (FactType.DURATION, FactType.ZSCORE, FactType.BASELINE_DEV):
            assert not fc.is_implemented(t)


class TestAllowedOps:
    def test_max_is_high_only(self):
        assert fc.op_allowed(FactType.MAX, ">=")
        assert not fc.op_allowed(FactType.MAX, "<=")

    def test_min_is_low_and_eq(self):
        assert fc.op_allowed(FactType.MIN, "<=")
        assert fc.op_allowed(FactType.MIN, "==")
        assert not fc.op_allowed(FactType.MIN, ">=")

    def test_trend_only_trend_op(self):
        assert fc.op_allowed(FactType.TREND, "trend==")
        assert not fc.op_allowed(FactType.TREND, ">=")


class TestAggStrategy:
    def test_every_facttype_has_strategy(self):
        assert set(fc.AGG_STRATEGY) == set(FactType)

    def test_stat_facts(self):
        for t in (FactType.MAX, FactType.MIN, FactType.AVG):
            assert fc.agg_strategy(t) is fc.AggStrategy.STAT
        assert fc.STAT_AGG_NAME[FactType.MAX] == "max"
        assert fc.STAT_AGG_NAME[FactType.MIN] == "min"
        assert fc.STAT_AGG_NAME[FactType.AVG] == "avg"

    def test_percentile_facts(self):
        for t in (FactType.P50, FactType.P90, FactType.P95, FactType.P99):
            assert fc.agg_strategy(t) is fc.AggStrategy.PERCENTILES
        assert fc.PERCENTILE_OF_FACT[FactType.P95] == 95.0
        assert set(fc.PERCENTILE_OF_FACT) == {
            FactType.P50, FactType.P90, FactType.P95, FactType.P99
        }

    def test_last_is_top_hits(self):
        assert fc.agg_strategy(FactType.LAST) is fc.AggStrategy.TOP_HITS

    def test_spike_is_filter_range(self):
        assert fc.agg_strategy(FactType.SPIKE_COUNT) is fc.AggStrategy.FILTER_RANGE

    def test_phase1_strategies_are_implementable(self):
        impl = {
            fc.AggStrategy.STAT, fc.AggStrategy.PERCENTILES,
            fc.AggStrategy.TOP_HITS, fc.AggStrategy.FILTER_RANGE,
        }
        for t in FactType:
            if fc.is_implemented(t):
                assert fc.agg_strategy(t) in impl


class TestParamRequirementSets:
    def test_needs_bucketing(self):
        assert FactType.MOVING_AVG in fc.NEEDS_BUCKETING
        assert FactType.DURATION in fc.NEEDS_BUCKETING
        assert FactType.MAX not in fc.NEEDS_BUCKETING

    def test_points_subset_of_bucketing(self):
        assert fc.NEEDS_POINTS <= fc.NEEDS_BUCKETING

    def test_needs_baseline(self):
        assert FactType.BASELINE_DEV in fc.NEEDS_BASELINE

    def test_over_direction_required_for_event_facts(self):
        assert FactType.SPIKE_COUNT in fc.REQUIRES_OVER_DIRECTION
        assert FactType.DURATION in fc.REQUIRES_OVER_DIRECTION
        assert FactType.MAX not in fc.REQUIRES_OVER_DIRECTION
