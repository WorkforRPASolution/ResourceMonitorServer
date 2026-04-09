"""Tests for src.db.seed (default profile hashing + conditional upsert)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.models import (
    AnalysisConfig,
    MetricSchedule,
    MonitorProfile,
    Scope,
    ThresholdConfig,
)
from src.db.repository import ProfileRepository
from src.db.seed import build_default_profile, seed_default_profile


@pytest.fixture
def mock_collection() -> MagicMock:
    coll = MagicMock()
    coll.insert_one = AsyncMock()
    coll.replace_one = AsyncMock()
    coll.find_one = AsyncMock()
    return coll


@pytest.fixture
def repo(mock_collection) -> ProfileRepository:
    return ProfileRepository(mock_collection)


@pytest.mark.unit
class TestDefaultProfile:
    def test_default_profile_has_global_wildcard_scope(self):
        p = build_default_profile()
        assert p.scope.process == "*"
        assert p.scope.eqp_model == "*"
        assert p.scope.eqp_id == "*"

    def test_default_profile_has_at_least_one_analysis_config(self):
        p = build_default_profile()
        assert len(p.analysis_configs) >= 1


@pytest.mark.unit
class TestSeedDefaultProfile:
    async def test_upserts_when_no_existing(self, repo, mock_collection):
        mock_collection.find_one.return_value = None
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_awaited_once()

    async def test_skips_when_hash_unchanged(self, repo, mock_collection):
        """If the stored profile matches the default, do not touch the DB."""
        default = build_default_profile()
        existing_doc = default.to_mongo()
        mock_collection.find_one.return_value = existing_doc
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_not_awaited()

    async def test_upserts_when_content_differs(self, repo, mock_collection):
        """If the stored profile drifted from the default, write the default back."""
        different = MonitorProfile(
            scope=Scope(process="*"),
            analysis_configs=[
                AnalysisConfig(
                    metric_pattern="cpu.DIFFERENT",
                    threshold=ThresholdConfig(
                        warning=50, critical=60, cooldown_minutes=5
                    ),
                    schedule=MetricSchedule(
                        interval_minutes=5, window_minutes=5
                    ),
                )
            ],
        )
        mock_collection.find_one.return_value = different.to_mongo()
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_awaited_once()
