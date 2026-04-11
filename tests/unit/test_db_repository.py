"""Tests for src.db.repository (ProfileRepository + EqpInfoRepository)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    NetworkTimeout,
    ServerSelectionTimeoutError,
)

from src.db.models import (
    AnalysisConfig,
    MetricSchedule,
    MongoUnavailableError,
    MonitorProfile,
    ProfileAlreadyExistsError,
    Scope,
    ThresholdConfig,
)
from src.db.repository import EqpInfoRepository, ProfileRepository


def _make_profile(process="CVD", eqp_model="*", eqp_id="*") -> MonitorProfile:
    return MonitorProfile(
        scope=Scope(process=process, eqp_model=eqp_model, eqp_id=eqp_id),
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


@pytest.fixture
def mock_collection() -> MagicMock:
    """motor collection mock — all methods are AsyncMock."""
    coll = MagicMock()
    coll.insert_one = AsyncMock()
    coll.replace_one = AsyncMock()
    coll.find_one = AsyncMock()
    coll.distinct = AsyncMock()
    coll.count_documents = AsyncMock()
    return coll


# ----------------------------------------------------------------------
# ProfileRepository
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestProfileRepositoryCreate:
    async def test_create_returns_stringified_id(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        oid = ObjectId()
        mock_collection.insert_one.return_value = MagicMock(inserted_id=oid)
        result = await repo.create(_make_profile())
        assert result == str(oid)

    async def test_create_passes_to_mongo_dict(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.insert_one.return_value = MagicMock(
            inserted_id=ObjectId()
        )
        p = _make_profile(process="CVD", eqp_model="ABC")
        await repo.create(p)
        doc = mock_collection.insert_one.call_args.args[0]
        assert doc["scope"]["eqpModel"] == "ABC"  # NOT "model"

    async def test_create_translates_duplicate_key_to_domain_exception(
        self, mock_collection
    ):
        repo = ProfileRepository(mock_collection)
        mock_collection.insert_one.side_effect = DuplicateKeyError("dup")
        with pytest.raises(ProfileAlreadyExistsError):
            await repo.create(_make_profile())


@pytest.mark.unit
class TestProfileRepositoryFind:
    async def test_find_by_scope_returns_profile_when_present(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        doc = _make_profile().to_mongo()
        doc["_id"] = ObjectId()
        mock_collection.find_one.return_value = doc
        result = await repo.find_by_scope(Scope(process="CVD"))
        assert result is not None
        assert result.scope.process == "CVD"
        # Lookup filter uses nested scope.* keys
        filter_ = mock_collection.find_one.call_args.args[0]
        assert filter_ == {"scope.process": "CVD"}

    async def test_find_by_scope_returns_none_when_absent(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.return_value = None
        result = await repo.find_by_scope(Scope(process="NOPE"))
        assert result is None

    async def test_find_by_scope_with_model_uses_nested_eqp_model(
        self, mock_collection
    ):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.return_value = None
        await repo.find_by_scope(Scope(process="CVD", eqp_model="ABC"))
        filter_ = mock_collection.find_one.call_args.args[0]
        assert filter_ == {"scope.process": "CVD", "scope.eqpModel": "ABC"}


@pytest.mark.unit
class TestProfileRepositoryUpsert:
    async def test_upsert_clears_resolve_cache(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        # Prime the cache.
        repo._resolve_cache["sentinel:key"] = _make_profile()
        assert "sentinel:key" in repo._resolve_cache
        await repo.upsert(_make_profile())
        assert "sentinel:key" not in repo._resolve_cache


@pytest.mark.unit
class TestProfileRepositoryCache:
    def test_cache_is_ttl_bounded(self, mock_collection):
        """`resolve_profile` cache must be a bounded TTLCache, not a plain dict."""
        from cachetools import TTLCache

        repo = ProfileRepository(mock_collection)
        assert isinstance(repo._resolve_cache, TTLCache)

    def test_cache_has_maxsize(self, mock_collection):
        from src.config.constants import PROFILE_CACHE_MAX_SIZE

        repo = ProfileRepository(mock_collection)
        assert repo._resolve_cache.maxsize == PROFILE_CACHE_MAX_SIZE


# ----------------------------------------------------------------------
# EqpInfoRepository
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestEqpInfoRepository:
    async def test_get_distinct_processes_applies_active_filter(
        self, mock_collection
    ):
        """onoff=1, webmanagerUse=1 — skip decommissioned/unmanaged PCs."""
        repo = EqpInfoRepository(mock_collection)
        mock_collection.distinct.return_value = ["CVD", "ETCH", "PHOTO"]
        result = await repo.get_distinct_processes()
        mock_collection.distinct.assert_awaited_once_with(
            "process", filter={"onoff": 1, "webmanagerUse": 1}
        )
        assert result == ["CVD", "ETCH", "PHOTO"]

    async def test_count_active_by_process(self, mock_collection):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.count_documents.return_value = 42
        n = await repo.count_active_by_process("CVD")
        mock_collection.count_documents.assert_awaited_once_with(
            {"process": "CVD", "onoff": 1, "webmanagerUse": 1}
        )
        assert n == 42


# ----------------------------------------------------------------------
# v6 P1-1: Mongo unavailable → MongoUnavailableError translation
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestMongoUnavailableTranslation:
    """Every public repository method must translate connection-level
    pymongo errors to ``MongoUnavailableError`` so callers like
    ``PartitionManager._do_redistribute`` can distinguish 'infra blip,
    retry me' from 'schema/permission error, fail the job'."""

    @pytest.fixture(params=[
        ServerSelectionTimeoutError("no servers"),
        NetworkTimeout("packet drop"),
        ConnectionFailure("conn refused"),
    ])
    def driver_exc(self, request):
        return request.param

    async def test_create_translates(self, mock_collection, driver_exc):
        repo = ProfileRepository(mock_collection)
        mock_collection.insert_one.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.create(_make_profile())

    async def test_upsert_translates(self, mock_collection, driver_exc):
        repo = ProfileRepository(mock_collection)
        mock_collection.replace_one.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.upsert(_make_profile())

    async def test_find_by_scope_translates(
        self, mock_collection, driver_exc
    ):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.find_by_scope(Scope(process="CVD"))

    async def test_resolve_profile_propagates_via_find_by_scope(
        self, mock_collection, driver_exc
    ):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.resolve_profile("CVD", "ABC", "EQP01")

    async def test_get_distinct_processes_translates(
        self, mock_collection, driver_exc
    ):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.distinct.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.get_distinct_processes()

    async def test_count_active_by_process_translates(
        self, mock_collection, driver_exc
    ):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.count_documents.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.count_active_by_process("CVD")

    async def test_duplicate_key_still_translates_to_already_exists(
        self, mock_collection
    ):
        """Regression: DuplicateKeyError must NOT be swallowed by the
        new MongoUnavailable translator (it's a different exception class)."""
        repo = ProfileRepository(mock_collection)
        mock_collection.insert_one.side_effect = DuplicateKeyError("dup")
        with pytest.raises(ProfileAlreadyExistsError):
            await repo.create(_make_profile())

    async def test_get_active_equipment_translates(
        self, mock_collection, driver_exc
    ):
        repo = EqpInfoRepository(mock_collection)
        cursor = MagicMock()
        cursor.to_list = AsyncMock(side_effect=driver_exc)
        mock_collection.find = MagicMock(return_value=cursor)
        with pytest.raises(MongoUnavailableError):
            await repo.get_active_equipment_by_process("CVD")


# ----------------------------------------------------------------------
# EqpInfoRepository — get_active_equipment_by_process (Phase 1)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestEqpInfoGetActiveEquipment:
    @pytest.fixture
    def mock_coll_with_find(self, mock_collection):
        """Extend mock_collection with a find() that returns a cursor mock."""
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        mock_collection.find = MagicMock(return_value=cursor)
        mock_collection._cursor = cursor  # expose for assertions
        return mock_collection

    async def test_returns_equipment_list(self, mock_coll_with_find):
        docs = [
            {"eqpId": "EQP01", "eqpModel": "MODEL_A", "process": "CVD",
             "localpc": "PC001", "ipAddr": "10.0.0.1", "line": "L1", "category": "MAIN"},
            {"eqpId": "EQP02", "eqpModel": "MODEL_B", "process": "CVD",
             "localpc": "PC002", "ipAddr": "10.0.0.2", "line": "L2", "category": "SUB"},
        ]
        mock_coll_with_find._cursor.to_list.return_value = docs
        repo = EqpInfoRepository(mock_coll_with_find)
        result = await repo.get_active_equipment_by_process("CVD")
        assert result == docs
        assert len(result) == 2

    async def test_applies_active_filter_and_process(self, mock_coll_with_find):
        repo = EqpInfoRepository(mock_coll_with_find)
        await repo.get_active_equipment_by_process("ETCH")
        call_args = mock_coll_with_find.find.call_args
        filter_arg = call_args[0][0] if call_args[0] else call_args[1].get("filter")
        assert filter_arg == {"process": "ETCH", "onoff": 1, "webmanagerUse": 1}

    async def test_uses_projection(self, mock_coll_with_find):
        repo = EqpInfoRepository(mock_coll_with_find)
        await repo.get_active_equipment_by_process("CVD")
        call_args = mock_coll_with_find.find.call_args
        projection = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("projection")
        # Must include these fields for email payload construction
        for field in ("eqpId", "eqpModel", "process", "localpc", "ipAddr", "line", "category"):
            assert field in projection

    async def test_returns_empty_list_when_no_match(self, mock_coll_with_find):
        mock_coll_with_find._cursor.to_list.return_value = []
        repo = EqpInfoRepository(mock_coll_with_find)
        result = await repo.get_active_equipment_by_process("NONEXIST")
        assert result == []
