"""Tests for src.db.seed — v2 default profile + hash-based conditional upsert."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.models import (
    Condition,
    Fact,
    Measure,
    MonitorProfile,
    NotifyChannel,
    Rule,
    Scope,
    validate_effective,
)
from src.db.repository import ProfileRepository
from src.db.seed import build_default_profile, seed_default_profile

pytestmark = pytest.mark.unit


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


class TestDefaultProfile:
    def test_global_wildcard_scope(self):
        p = build_default_profile()
        assert p.scope.process == "*" and p.scope.eqp_model == "*" and p.scope.eqp_id == "*"

    def test_has_measures_and_rules_and_notify(self):
        p = build_default_profile()
        assert len(p.measures) >= 1
        assert len(p.rules) >= 1
        assert "default" in p.notify

    def test_default_profile_is_reference_valid(self):
        """Every rule must resolve to a declared measure/fact with an allowed op."""
        assert validate_effective(build_default_profile()) == []

    def test_only_phase1_fact_types(self):
        from src.analyzer import fact_catalog as fc
        for m in build_default_profile().measures:
            for f in m.facts:
                assert fc.is_implemented(f.type), f"{m.id}.{f.type} is not Phase 1"


class TestSeedDefaultProfile:
    async def test_upserts_when_no_existing(self, repo, mock_collection):
        mock_collection.find_one.return_value = None
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_awaited_once()

    async def test_skips_when_hash_unchanged(self, repo, mock_collection):
        mock_collection.find_one.return_value = build_default_profile().to_mongo()
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_not_awaited()

    async def test_skips_even_when_governance_differs(self, repo, mock_collection):
        """Governance is excluded from the hash, so a different version/timestamp
        on the stored doc must NOT trigger a reseed (no stomping operator edits)."""
        doc = build_default_profile().to_mongo()
        doc["governance"]["version"] = 7
        doc["governance"]["updated_by"] = "operator"
        mock_collection.find_one.return_value = doc
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_not_awaited()

    def _different(self, *, updated_by):
        from src.db.models import Governance
        return MonitorProfile(
            scope=Scope(process="*"),
            governance=Governance(updated_by=updated_by),
            measures=[Measure(id="cpu", category="cpu", metric="x",
                              window_minutes=5, facts=[Fact(type="max")])],
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.max", op=">=", value=50)])],
            notify={"default": NotifyChannel(cooldown_minutes=5)},
        )

    async def test_reseeds_when_code_owned_default_drifts(self, repo, mock_collection):
        # stored differs but is still code-owned (updated_by="system") → reseed
        mock_collection.find_one.return_value = self._different(updated_by="system").to_mongo()
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_awaited_once()

    async def test_skips_when_operator_edited(self, repo, mock_collection):
        # operator edited the global scope (updated_by != "system") → never stomp
        mock_collection.find_one.return_value = self._different(updated_by="api").to_mongo()
        await seed_default_profile(repo)
        mock_collection.replace_one.assert_not_awaited()

    async def test_concurrent_create_race_is_swallowed(self, repo, mock_collection):
        # two pods on a fresh DB: the loser's upsert hits the unique index →
        # DuplicateKeyError must be treated as "already seeded", not crash startup.
        from pymongo.errors import DuplicateKeyError
        mock_collection.find_one.return_value = None
        mock_collection.replace_one.side_effect = DuplicateKeyError("dup")
        await seed_default_profile(repo)  # must not raise
