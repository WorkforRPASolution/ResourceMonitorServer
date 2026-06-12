"""Tests for src.db.repository (ProfileRepository + EqpInfoRepository) — v2."""
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
    Condition,
    Fact,
    Measure,
    MongoUnavailableError,
    MonitorProfile,
    NotifyChannel,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileVersionConflictError,
    Rule,
    Scope,
)
from src.db.repository import EqpInfoRepository, ProfileRepository


def _make_profile(process="CVD", eqp_model="*", eqp_id="*", *, rules=None, version=1):
    return MonitorProfile(
        scope=Scope(process=process, eqp_model=eqp_model, eqp_id=eqp_id),
        governance={"version": version},
        measures=[
            Measure(id="cpu", category="cpu", metric="total_used_pct",
                    window_minutes=15, facts=[Fact(type="max")])
        ],
        rules=rules if rules is not None else [
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        ],
        notify={"default": NotifyChannel(cooldown_minutes=30)},
    )


def _doc(profile):
    d = profile.to_mongo()
    d["_id"] = ObjectId()
    return d


def _cursor(docs):
    cur = MagicMock()
    cur.to_list = AsyncMock(return_value=docs)
    return cur


@pytest.fixture
def mock_collection() -> MagicMock:
    coll = MagicMock()
    coll.insert_one = AsyncMock()
    coll.replace_one = AsyncMock()
    coll.update_one = AsyncMock()
    coll.delete_one = AsyncMock()
    coll.find_one = AsyncMock()
    coll.find = MagicMock(return_value=_cursor([]))
    coll.distinct = AsyncMock()
    coll.count_documents = AsyncMock()
    return coll


pytestmark = pytest.mark.unit


class TestCreate:
    async def test_returns_stringified_id(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        oid = ObjectId()
        mock_collection.insert_one.return_value = MagicMock(inserted_id=oid)
        assert await repo.create(_make_profile()) == str(oid)

    async def test_persists_eqp_model_field_name(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.insert_one.return_value = MagicMock(inserted_id=ObjectId())
        await repo.create(_make_profile(eqp_model="ABC"))
        doc = mock_collection.insert_one.call_args.args[0]
        assert doc["scope"]["eqpModel"] == "ABC"

    async def test_duplicate_key_to_domain_exception(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.insert_one.side_effect = DuplicateKeyError("dup")
        with pytest.raises(ProfileAlreadyExistsError):
            await repo.create(_make_profile())


class TestFindByScope:
    async def test_returns_profile(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.return_value = _doc(_make_profile())
        result = await repo.find_by_scope(Scope(process="CVD"))
        assert result is not None and result.scope.process == "CVD"

    async def test_uses_exact_triple_filter(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.return_value = None
        await repo.find_by_scope(Scope(process="CVD"))
        assert mock_collection.find_one.call_args.args[0] == {
            "scope.process": "CVD", "scope.eqpModel": "*", "scope.eqpId": "*",
        }

    async def test_exact_triple_with_model_and_eqp(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.return_value = None
        await repo.find_by_scope(Scope(process="CVD", eqp_model="ABC", eqp_id="E1"))
        assert mock_collection.find_one.call_args.args[0] == {
            "scope.process": "CVD", "scope.eqpModel": "ABC", "scope.eqpId": "E1",
        }


class TestCascadeFold:
    async def test_or_query_collects_ancestor_triples(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        await repo.resolve_profile("CVD", "M", "E1")
        query = mock_collection.find.call_args.args[0]
        clauses = query["$or"]
        assert {"scope.process": "*", "scope.eqpModel": "*", "scope.eqpId": "*"} in clauses
        assert {"scope.process": "CVD", "scope.eqpModel": "*", "scope.eqpId": "*"} in clauses
        assert {"scope.process": "CVD", "scope.eqpModel": "M", "scope.eqpId": "*"} in clauses
        assert {"scope.process": "CVD", "scope.eqpModel": "M", "scope.eqpId": "E1"} in clauses

    async def test_folds_base_to_specific(self, mock_collection):
        glob = _make_profile(process="*")
        overlay = MonitorProfile(
            scope=Scope(process="CVD", eqp_model="M", eqp_id="E1"),
            rules=[Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
                        when=[Condition(fact="cpu.max", op=">=", value=95)])],
        )
        # returned out of order — repo must sort base→specific
        mock_collection.find = MagicMock(return_value=_cursor([_doc(overlay), _doc(glob)]))
        repo = ProfileRepository(mock_collection)
        eff = await repo.resolve_profile("CVD", "M", "E1")
        assert {r.id for r in eff.rules} == {"cpu_warn", "cpu_crit"}
        assert [m.id for m in eff.measures] == ["cpu"]

    async def test_no_docs_returns_none(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        assert await repo.resolve_profile("X", "Y", "Z") is None

    async def test_disabled_global_enabled_eqp_overlay_wins(self, mock_collection):
        """회귀(2026-06-12 사고): 전역 (*,*,*)이 enabled:false 여도 eqp overlay가
        enabled:true 면 effective는 켜진다 — enabled도 구체 scope가 이긴다."""
        glob = _make_profile(process="*")
        glob.enabled = False
        overlay = MonitorProfile(
            scope=Scope(process="CVD", eqp_model="M", eqp_id="E1"),
            enabled=True,
            rules=[Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
                        when=[Condition(fact="cpu.max", op=">=", value=95)])],
        )
        mock_collection.find = MagicMock(return_value=_cursor([_doc(glob), _doc(overlay)]))
        repo = ProfileRepository(mock_collection)
        eff = await repo.resolve_profile("CVD", "M", "E1")
        assert eff.enabled is True
        # 상속/오버라이드는 그대로: rule 둘 다, measure는 전역 것
        assert {r.id for r in eff.rules} == {"cpu_warn", "cpu_crit"}

    async def test_caches_effective(self, mock_collection):
        mock_collection.find = MagicMock(return_value=_cursor([_doc(_make_profile(process="*"))]))
        repo = ProfileRepository(mock_collection)
        await repo.resolve_profile("CVD", "M", "E1")
        await repo.resolve_profile("CVD", "M", "E1")
        assert mock_collection.find.call_count == 1  # second served from cache

    async def test_invalid_overlay_survives_returns_effective(self, mock_collection):
        # A bad overlay (rule references a missing measure) must NOT crash
        # resolution — analysis must survive; errors are logged, not raised.
        bad = _make_profile(
            process="*",
            rules=[Rule(id="r", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="ghost.max", op=">=", value=1)])],
        )
        mock_collection.find = MagicMock(return_value=_cursor([_doc(bad)]))
        repo = ProfileRepository(mock_collection)
        eff = await repo.resolve_profile("CVD", "M", "E1")
        assert eff is not None
        assert [r.id for r in eff.rules] == ["r"]  # returned despite invalid ref


class TestGetSchedulingIntervals:
    """reload()의 잡 cadence 출처. resolve_profile(p,*,*)와 달리 model/eqp 단독
    overlay까지 포함해 잡이 등록되도록 보장한다."""

    async def test_filter_includes_globals_without_doc_enabled_filter(self, mock_collection):
        mock_collection.find = MagicMock(return_value=_cursor([]))
        repo = ProfileRepository(mock_collection)
        await repo.get_scheduling_intervals("CVD")
        # 글로벌(*)까지 $in 으로 포함. doc 레벨 enabled는 필터하지 않는다 —
        # enabled는 구체 scope가 이기므로(last-wins), 꺼진 조상 doc의 rule을
        # 켜진 overlay가 상속해 쓸 수 있다. 그 rule의 interval은 조상 doc에만
        # 있으므로 모든 doc에서 수집해야 한다(아니면 silent lost breach).
        assert mock_collection.find.call_args.args[0] == {
            "scope.process": {"$in": ["CVD", "*"]},
        }

    async def test_unions_intervals_across_scopes(self, mock_collection):
        glob = _make_profile(
            process="*", eqp_model="*", eqp_id="*",
            rules=[
                Rule(id="g5", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=80)]),
                Rule(id="g10", interval_minutes=10, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=70)]),
            ],
        )
        eqp = _make_profile(
            process="CVD", eqp_model="M", eqp_id="E1",
            rules=[
                Rule(id="e5", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=85)]),
                Rule(id="e15", interval_minutes=15, severity="CRITICAL",
                     when=[Condition(fact="cpu.max", op=">=", value=95)]),
            ],
        )
        mock_collection.find = MagicMock(return_value=_cursor([_doc(glob), _doc(eqp)]))
        repo = ProfileRepository(mock_collection)
        assert await repo.get_scheduling_intervals("CVD") == [5, 10, 15]

    async def test_eqp_only_doc_yields_interval(self, mock_collection):
        # process 레벨 문서가 전혀 없고 eqp 단독 문서만 있어도 interval이 나와야 한다
        # (resolve_profile("CVD","*","*")는 None이던 회귀 케이스).
        eqp = _make_profile(
            process="CVD", eqp_model="M", eqp_id="E1",
            rules=[Rule(id="e5", interval_minutes=5, severity="WARNING",
                        when=[Condition(fact="cpu.max", op=">=", value=85)])],
        )
        mock_collection.find = MagicMock(return_value=_cursor([_doc(eqp)]))
        repo = ProfileRepository(mock_collection)
        assert await repo.get_scheduling_intervals("CVD") == [5]

    async def test_no_docs_returns_empty(self, mock_collection):
        mock_collection.find = MagicMock(return_value=_cursor([]))
        repo = ProfileRepository(mock_collection)
        assert await repo.get_scheduling_intervals("NOPE") == []

    async def test_projection_includes_rule_enabled(self, mock_collection):
        # the real query must fetch rules.enabled so disabled rules can be filtered
        mock_collection.find = MagicMock(return_value=_cursor([]))
        repo = ProfileRepository(mock_collection)
        await repo.get_scheduling_intervals("CVD")
        projection = mock_collection.find.call_args.args[1]
        assert projection.get("rules.enabled") == 1

    async def test_disabled_rule_interval_excluded(self, mock_collection):
        # interval 10 is used ONLY by a disabled rule → no job should be scheduled
        # for it; interval 5 (enabled) stays.
        glob = _make_profile(
            process="*", eqp_model="*", eqp_id="*",
            rules=[
                Rule(id="g5", interval_minutes=5, severity="WARNING",
                     when=[Condition(fact="cpu.max", op=">=", value=80)]),
                Rule(id="g10", interval_minutes=10, severity="WARNING", enabled=False,
                     when=[Condition(fact="cpu.max", op=">=", value=70)]),
            ],
        )
        mock_collection.find = MagicMock(return_value=_cursor([_doc(glob)]))
        repo = ProfileRepository(mock_collection)
        assert await repo.get_scheduling_intervals("CVD") == [5]

    async def test_legacy_rule_without_enabled_counts(self, mock_collection):
        # a pre-existing doc whose rule has no enabled field → treated as enabled
        doc = _doc(_make_profile(process="*"))
        doc["rules"][0].pop("enabled", None)
        mock_collection.find = MagicMock(return_value=_cursor([doc]))
        repo = ProfileRepository(mock_collection)
        assert await repo.get_scheduling_intervals("CVD") == [5]


class TestUpsert:
    async def test_uses_exact_filter_and_clears_cache(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        repo._resolve_cache["sentinel"] = _make_profile()
        await repo.upsert(_make_profile(eqp_model="M", eqp_id="E1"))
        filter_ = mock_collection.replace_one.call_args.args[0]
        assert filter_ == {"scope.process": "CVD", "scope.eqpModel": "M", "scope.eqpId": "E1"}
        assert "sentinel" not in repo._resolve_cache


class TestOptimisticLock:
    async def test_replace_with_version_bumps_and_filters(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.update_one.return_value = MagicMock(matched_count=1)
        new_v = await repo.replace_with_version(_make_profile(version=3), expected_version=3)
        assert new_v == 4
        filter_, update = mock_collection.update_one.call_args.args
        assert filter_["governance.version"] == 3
        assert update["$set"]["governance"]["version"] == 4

    async def test_replace_with_version_stamps_governance(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.update_one.return_value = MagicMock(matched_count=1)
        await repo.replace_with_version(_make_profile(version=1), expected_version=1)
        _, update = mock_collection.update_one.call_args.args
        gov = update["$set"]["governance"]
        assert gov["updated_by"] == "api"
        assert gov["updated_at"]  # stamped non-empty

    async def test_stale_version_raises_conflict(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.update_one.return_value = MagicMock(matched_count=0)
        mock_collection.find_one.return_value = _doc(_make_profile())  # doc exists
        with pytest.raises(ProfileVersionConflictError):
            await repo.replace_with_version(_make_profile(version=1), expected_version=1)

    async def test_absent_doc_raises_not_found(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.update_one.return_value = MagicMock(matched_count=0)
        mock_collection.find_one.return_value = None  # doc absent
        with pytest.raises(ProfileNotFoundError):
            await repo.replace_with_version(_make_profile(version=1), expected_version=1)

    async def test_replace_with_version_clears_cache(self, mock_collection):
        # operator edit must invalidate the effective cache, else a stale folded
        # profile lingers for up to the TTL and analysis uses the old thresholds.
        repo = ProfileRepository(mock_collection)
        mock_collection.update_one.return_value = MagicMock(matched_count=1)
        repo._resolve_cache["sentinel"] = _make_profile()
        await repo.replace_with_version(_make_profile(version=1), expected_version=1)
        assert "sentinel" not in repo._resolve_cache

    async def test_delete_with_version_ok(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.delete_one.return_value = MagicMock(deleted_count=1)
        await repo.delete_by_scope(Scope(process="CVD"), expected_version=2)
        filter_ = mock_collection.delete_one.call_args.args[0]
        assert filter_["governance.version"] == 2

    async def test_delete_clears_cache(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.delete_one.return_value = MagicMock(deleted_count=1)
        repo._resolve_cache["sentinel"] = _make_profile()
        await repo.delete_by_scope(Scope(process="CVD"), expected_version=2)
        assert "sentinel" not in repo._resolve_cache

    async def test_delete_unconditional_omits_version_filter(self, mock_collection):
        # expected_version=None → unconditional delete (no governance.version key)
        repo = ProfileRepository(mock_collection)
        mock_collection.delete_one.return_value = MagicMock(deleted_count=1)
        await repo.delete_by_scope(Scope(process="CVD"))
        filter_ = mock_collection.delete_one.call_args.args[0]
        assert "governance.version" not in filter_

    async def test_delete_unconditional_absent_raises_not_found(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.delete_one.return_value = MagicMock(deleted_count=0)
        mock_collection.find_one.return_value = None
        with pytest.raises(ProfileNotFoundError):
            await repo.delete_by_scope(Scope(process="CVD"))

    async def test_delete_absent_raises_not_found(self, mock_collection):
        repo = ProfileRepository(mock_collection)
        mock_collection.delete_one.return_value = MagicMock(deleted_count=0)
        mock_collection.find_one.return_value = None
        with pytest.raises(ProfileNotFoundError):
            await repo.delete_by_scope(Scope(process="CVD"), expected_version=2)


class TestCacheShape:
    def test_cache_is_ttl_bounded(self, mock_collection):
        from cachetools import TTLCache
        repo = ProfileRepository(mock_collection)
        assert isinstance(repo._resolve_cache, TTLCache)

    def test_cache_has_maxsize(self, mock_collection):
        from src.config.constants import PROFILE_CACHE_MAX_SIZE
        repo = ProfileRepository(mock_collection)
        assert repo._resolve_cache.maxsize == PROFILE_CACHE_MAX_SIZE


class TestEqpInfoRepository:
    async def test_get_distinct_processes_applies_active_filter(self, mock_collection):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.distinct.return_value = ["CVD", "ETCH"]
        result = await repo.get_distinct_processes()
        mock_collection.distinct.assert_awaited_once_with(
            "process", filter={"onoff": 1, "webmanagerUse": 1}
        )
        assert result == ["CVD", "ETCH"]

    async def test_count_active_by_process(self, mock_collection):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.count_documents.return_value = 42
        assert await repo.count_active_by_process("CVD") == 42
        mock_collection.count_documents.assert_awaited_once_with(
            {"process": "CVD", "onoff": 1, "webmanagerUse": 1}
        )


class TestMongoUnavailableTranslation:
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

    async def test_find_by_scope_translates(self, mock_collection, driver_exc):
        repo = ProfileRepository(mock_collection)
        mock_collection.find_one.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.find_by_scope(Scope(process="CVD"))

    async def test_resolve_profile_translates(self, mock_collection, driver_exc):
        repo = ProfileRepository(mock_collection)
        mock_collection.find = MagicMock(return_value=MagicMock(
            to_list=AsyncMock(side_effect=driver_exc)))
        with pytest.raises(MongoUnavailableError):
            await repo.resolve_profile("CVD", "ABC", "EQP01")

    async def test_get_scheduling_intervals_translates(self, mock_collection, driver_exc):
        repo = ProfileRepository(mock_collection)
        mock_collection.find = MagicMock(return_value=MagicMock(
            to_list=AsyncMock(side_effect=driver_exc)))
        with pytest.raises(MongoUnavailableError):
            await repo.get_scheduling_intervals("CVD")

    async def test_replace_with_version_translates(self, mock_collection, driver_exc):
        repo = ProfileRepository(mock_collection)
        mock_collection.update_one.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.replace_with_version(_make_profile(), expected_version=1)

    async def test_get_distinct_processes_translates(self, mock_collection, driver_exc):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.distinct.side_effect = driver_exc
        with pytest.raises(MongoUnavailableError):
            await repo.get_distinct_processes()

    async def test_get_active_equipment_translates(self, mock_collection, driver_exc):
        repo = EqpInfoRepository(mock_collection)
        mock_collection.find = MagicMock(return_value=MagicMock(
            to_list=AsyncMock(side_effect=driver_exc)))
        with pytest.raises(MongoUnavailableError):
            await repo.get_active_equipment_by_process("CVD")


class TestEqpInfoGetActiveEquipment:
    async def test_returns_equipment_list(self, mock_collection):
        docs = [
            {"eqpId": "EQP01", "eqpModel": "MODEL_A", "process": "CVD",
             "localpc": "PC001", "ipAddr": "10.0.0.1", "line": "L1", "category": "MAIN"},
        ]
        mock_collection.find = MagicMock(return_value=_cursor(docs))
        repo = EqpInfoRepository(mock_collection)
        assert await repo.get_active_equipment_by_process("CVD") == docs

    async def test_applies_active_filter_and_process(self, mock_collection):
        mock_collection.find = MagicMock(return_value=_cursor([]))
        repo = EqpInfoRepository(mock_collection)
        await repo.get_active_equipment_by_process("ETCH")
        filter_arg = mock_collection.find.call_args.args[0]
        assert filter_arg == {"process": "ETCH", "onoff": 1, "webmanagerUse": 1}

    async def test_uses_projection(self, mock_collection):
        mock_collection.find = MagicMock(return_value=_cursor([]))
        repo = EqpInfoRepository(mock_collection)
        await repo.get_active_equipment_by_process("CVD")
        projection = mock_collection.find.call_args.args[1]
        for field in ("eqpId", "eqpModel", "process", "localpc", "ipAddr", "line", "category"):
            assert field in projection
