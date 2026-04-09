"""Tests for src.db.models — Pydantic models + Mongo roundtrip."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from src.db.models import (
    AnalysisConfig,
    MetricSchedule,
    MonitorProfile,
    Scope,
    ThresholdConfig,
)


@pytest.mark.unit
class TestScopeMapping:
    def test_default_wildcard_scope(self):
        s = Scope(process="*")
        assert s.process == "*"
        assert s.eqp_model == "*"
        assert s.eqp_id == "*"

    def test_json_alias_model_maps_to_eqp_model(self):
        """External JSON uses `model` and `eqpId`; internal uses `eqp_model`/`eqp_id`."""
        s = Scope.model_validate({"process": "CVD", "model": "ABC123", "eqpId": "E1"})
        assert s.eqp_model == "ABC123"
        assert s.eqp_id == "E1"

    def test_to_mongo_query_includes_eqp_model_not_model(self):
        """EQP_INFO uses `eqpModel` (not `model`)."""
        s = Scope(process="CVD", eqp_model="ABC123", eqp_id="E1")
        q = s.to_mongo_query()
        assert q == {"process": "CVD", "eqpModel": "ABC123", "eqpId": "E1"}
        assert "model" not in q  # critical: don't leak Pydantic alias

    def test_to_mongo_query_skips_wildcards(self):
        """Wildcard fields are not added to the Mongo query."""
        s = Scope(process="CVD")
        q = s.to_mongo_query()
        assert q == {"process": "CVD"}

    def test_scope_equality(self):
        s1 = Scope(process="CVD", eqp_model="M", eqp_id="E1")
        s2 = Scope(process="CVD", eqp_model="M", eqp_id="E1")
        assert s1 == s2


@pytest.mark.unit
class TestAnalysisConfig:
    def test_threshold_config_basic_fields(self):
        t = ThresholdConfig(warning=80.0, critical=95.0, cooldown_minutes=30)
        assert t.warning == 80.0
        assert t.critical == 95.0
        assert t.cooldown_minutes == 30

    def test_metric_schedule_window_minutes(self):
        sch = MetricSchedule(interval_minutes=5, window_minutes=10)
        assert sch.interval_minutes == 5
        assert sch.window_minutes == 10

    def test_analysis_config_holds_threshold_and_schedule(self):
        ac = AnalysisConfig(
            metric_pattern="total_used_pct",
            threshold=ThresholdConfig(warning=80, critical=95, cooldown_minutes=30),
            schedule=MetricSchedule(interval_minutes=5, window_minutes=10),
        )
        assert ac.metric_pattern == "total_used_pct"
        assert ac.threshold.warning == 80


@pytest.mark.unit
class TestMonitorProfileRoundtrip:
    def _make_profile(self) -> MonitorProfile:
        return MonitorProfile(
            scope=Scope(process="CVD", eqp_model="ABC", eqp_id="*"),
            analysis_configs=[
                AnalysisConfig(
                    metric_pattern="total_used_pct",
                    threshold=ThresholdConfig(
                        warning=80, critical=95, cooldown_minutes=30
                    ),
                    schedule=MetricSchedule(
                        interval_minutes=5, window_minutes=10
                    ),
                )
            ],
        )

    def test_to_mongo_from_mongo_roundtrip(self):
        p = self._make_profile()
        doc = p.to_mongo()
        restored = MonitorProfile.from_mongo(doc)
        assert restored.scope == p.scope
        assert len(restored.analysis_configs) == 1
        assert restored.analysis_configs[0].metric_pattern == "total_used_pct"

    def test_to_mongo_uses_eqp_model_field_name(self):
        """The persisted document's scope must use `eqpModel`, not `model`."""
        p = self._make_profile()
        doc = p.to_mongo()
        assert "scope" in doc
        assert doc["scope"]["eqpModel"] == "ABC"
        assert "model" not in doc["scope"]

    def test_from_mongo_strips_objectid(self):
        """`_id` (ObjectId) must not leak into the model after from_mongo()."""
        p = self._make_profile()
        doc = p.to_mongo()
        doc["_id"] = ObjectId()
        doc["created_at"] = datetime.now(timezone.utc)
        doc["updated_at"] = datetime.now(timezone.utc)
        restored = MonitorProfile.from_mongo(doc)
        # id is optional and populated as a string
        assert restored.id is not None
        assert isinstance(restored.id, str)

    def test_from_mongo_without_id_and_timestamps(self):
        p = self._make_profile()
        doc = p.to_mongo()
        restored = MonitorProfile.from_mongo(doc)
        assert restored.id is None
