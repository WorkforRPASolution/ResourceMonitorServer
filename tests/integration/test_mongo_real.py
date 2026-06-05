"""MongoDB integration — real motor client against OrbStack mongodb-44.

Unit tests mock Motor entirely, so the serialization boundary (Pydantic →
Mongo BSON → Pydantic round-trip, EQP_INFO field name aliasing, DuplicateKey
exception translation) is only exercised here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.config.constants import COLL_PROFILE
from src.config.settings import AppSettings
from src.db.models import (
    Condition,
    Fact,
    Measure,
    MonitorProfile,
    NotifyChannel,
    ProfileAlreadyExistsError,
    ProfileVersionConflictError,
    Rule,
    Scope,
)
from src.db.repository import EqpInfoRepository, ProfileRepository
from src.db.seed import build_default_profile, seed_default_profile
from src.startup.infra import InfraContext
from src.startup.repos import init_repos

pytestmark = pytest.mark.integration


async def _init_repos_against_db(mongo_db, *, debug_read_only: bool = False):
    """Run init_repos() against a real motor db (used by fresh_mongo_db).

    InfraContext normally holds a MongoClient wrapper — but init_repos only
    accesses infra.mongo.db, so we can adapt a raw database handle with a
    lightweight MagicMock shim.
    """
    infra = InfraContext()
    infra.mongo = MagicMock()
    infra.mongo.db = mongo_db
    settings = AppSettings(debug_read_only=debug_read_only)
    return await init_repos(infra, settings)


# ----------------------------------------------------------------------
# init_repos: unique index creation ★ Phase 0 gap regression guard
# ----------------------------------------------------------------------
async def test_init_repos_creates_unique_scope_index_on_fresh_db(fresh_mongo_db):
    """회귀 가드: fresh DB에서 init_repos를 돌리면 컬렉션+인덱스가
    모두 생성돼야 한다 (MongoDB implicit collection creation).
    """
    # Precondition: 컬렉션이 아예 없어야 의미가 있음
    existing = await fresh_mongo_db.list_collection_names()
    assert COLL_PROFILE not in existing, (
        f"precondition failed: {COLL_PROFILE} already exists on a fresh db"
    )

    await _init_repos_against_db(fresh_mongo_db)

    # 컬렉션이 자동 생성되었어야 함
    after = await fresh_mongo_db.list_collection_names()
    assert COLL_PROFILE in after

    # uniq_scope 인덱스가 unique=True로 존재해야 함
    indexes = await fresh_mongo_db[COLL_PROFILE].index_information()
    assert "uniq_scope" in indexes, f"got indexes: {list(indexes.keys())}"
    idx = indexes["uniq_scope"]
    assert idx.get("unique") is True
    # 인덱스 키가 scope 세 필드 모두 포함
    key_fields = {field for field, _direction in idx["key"]}
    assert key_fields == {"scope.process", "scope.eqpModel", "scope.eqpId"}


async def test_init_repos_is_idempotent(fresh_mongo_db):
    """두 번 호출해도 에러 없이 같은 상태가 유지돼야 한다."""
    await _init_repos_against_db(fresh_mongo_db)
    await _init_repos_against_db(fresh_mongo_db)   # 재호출 — "all indexes already exist"

    indexes = await fresh_mongo_db[COLL_PROFILE].index_information()
    # uniq_scope은 여전히 1개만 존재 (중복 생성 없음)
    uniq_scope_keys = [
        name for name in indexes if name == "uniq_scope"
    ]
    assert len(uniq_scope_keys) == 1


# ----------------------------------------------------------------------
# ProfileRepository — DuplicateKey → domain exception (using real init_repos)
# ----------------------------------------------------------------------
async def test_create_duplicate_raises_domain_error(fresh_mongo_db):
    """★ 통합 회귀 가드: init_repos()가 만든 인덱스로 중복 insert가 차단되고
    ProfileAlreadyExistsError로 변환되어야 한다. 수동 인덱스 setup 없음."""
    repos = await _init_repos_against_db(fresh_mongo_db)
    repo = repos.profile_repo

    profile = MonitorProfile(scope=Scope(process="P1"))
    await repo.create(profile)
    with pytest.raises(ProfileAlreadyExistsError):
        await repo.create(profile)


# ----------------------------------------------------------------------
# Scope alias + nested filter — stored as "eqpModel", not "model"
# ----------------------------------------------------------------------
async def test_scope_alias_persists_as_eqpModel(fresh_mongo_db):
    """회귀 가드: Pydantic `eqp_model` 필드가 Mongo에는 `eqpModel`로 저장돼야 한다."""
    coll = fresh_mongo_db["RESOURCE_MONITOR_PROFILE"]
    repo = ProfileRepository(coll)
    await repo.upsert(
        MonitorProfile(scope=Scope(process="ETCH", eqp_model="LAM_01"))
    )
    # raw Mongo 문서를 직접 확인
    raw = await coll.find_one({"scope.process": "ETCH"})
    assert raw is not None
    assert raw["scope"]["eqpModel"] == "LAM_01"
    assert "model" not in raw["scope"]
    assert "eqp_model" not in raw["scope"]


def _v2_profile(scope, *, rules=None, version=1):
    return MonitorProfile(
        scope=scope,
        governance={"version": version},
        measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                          window_minutes=15, facts=[Fact(type="max")])],
        rules=rules if rules is not None else [
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        ],
        notify={"default": NotifyChannel(cooldown_minutes=30)},
    )


async def test_find_by_scope_roundtrip(fresh_mongo_db):
    coll = fresh_mongo_db["RESOURCE_MONITOR_PROFILE"]
    repo = ProfileRepository(coll)
    original = _v2_profile(Scope(process="CVD", eqp_model="AMAT_02", eqp_id="CVD_001"))
    await repo.upsert(original)

    found = await repo.find_by_scope(original.scope)
    assert found is not None
    assert found.scope.process == "CVD"
    assert found.scope.eqp_model == "AMAT_02"  # alias 역변환 OK
    assert found.scope.eqp_id == "CVD_001"
    assert [m.id for m in found.measures] == ["cpu"]
    assert found.rules[0].when[0].value == 80
    assert found.id is not None  # _id → str 변환됨


async def test_resolve_profile_cascade_fold(fresh_mongo_db):
    """회귀 가드(dead-path): global + eqp overlay 가 fold 되어 effective 에 둘 다 반영."""
    coll = fresh_mongo_db["RESOURCE_MONITOR_PROFILE"]
    repo = ProfileRepository(coll)
    await repo.create(_v2_profile(Scope(process="*")))
    overlay = MonitorProfile(
        scope=Scope(process="CVD", eqp_model="M", eqp_id="E1"),
        rules=[Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
                    when=[Condition(fact="cpu.max", op=">=", value=95)])],
    )
    await repo.create(overlay)

    effective = await repo.resolve_profile("CVD", "M", "E1")
    assert {r.id for r in effective.rules} == {"cpu_warn", "cpu_crit"}
    assert [m.id for m in effective.measures] == ["cpu"]  # inherited from global


async def test_optimistic_lock_conflict(fresh_mongo_db):
    """version mismatch → ProfileVersionConflictError; 정상 버전 → bump."""
    coll = fresh_mongo_db["RESOURCE_MONITOR_PROFILE"]
    repo = ProfileRepository(coll)
    await repo.create(_v2_profile(Scope(process="CVD"), version=1))

    # stale version → conflict
    with pytest.raises(ProfileVersionConflictError):
        await repo.replace_with_version(_v2_profile(Scope(process="CVD")), expected_version=99)

    # correct version → bump to 2
    new_version = await repo.replace_with_version(
        _v2_profile(Scope(process="CVD")), expected_version=1
    )
    assert new_version == 2
    stored = await repo.find_by_scope(Scope(process="CVD"))
    assert stored.governance.version == 2


# ----------------------------------------------------------------------
# EqpInfoRepository — onoff / webmanagerUse 활성 필터
# ----------------------------------------------------------------------
async def test_get_distinct_processes_excludes_inactive(fresh_mongo_db):
    """회귀 가드: onoff=0 또는 webmanagerUse=0 장비는 결과에서 제외."""
    coll = fresh_mongo_db["EQP_INFO"]
    await coll.insert_many(
        [
            {"eqpId": "A1", "process": "ACTIVE_PROC_1", "eqpModel": "M",
             "onoff": 1, "webmanagerUse": 1},
            {"eqpId": "A2", "process": "ACTIVE_PROC_1", "eqpModel": "M",
             "onoff": 1, "webmanagerUse": 1},
            {"eqpId": "B1", "process": "ACTIVE_PROC_2", "eqpModel": "M",
             "onoff": 1, "webmanagerUse": 1},
            # 비활성: onoff=0 → 제외돼야 함
            {"eqpId": "C1", "process": "DECOMMISSIONED", "eqpModel": "M",
             "onoff": 0, "webmanagerUse": 1},
            # 제외: webmanagerUse=0 → 제외돼야 함
            {"eqpId": "D1", "process": "NOT_MANAGED", "eqpModel": "M",
             "onoff": 1, "webmanagerUse": 0},
        ]
    )
    repo = EqpInfoRepository(coll)
    processes = sorted(await repo.get_distinct_processes())
    assert processes == ["ACTIVE_PROC_1", "ACTIVE_PROC_2"]
    assert "DECOMMISSIONED" not in processes
    assert "NOT_MANAGED" not in processes


async def test_count_active_by_process(fresh_mongo_db):
    coll = fresh_mongo_db["EQP_INFO"]
    await coll.insert_many(
        [
            {"eqpId": "X1", "process": "P", "onoff": 1, "webmanagerUse": 1},
            {"eqpId": "X2", "process": "P", "onoff": 1, "webmanagerUse": 1},
            {"eqpId": "X3", "process": "P", "onoff": 0, "webmanagerUse": 1},
            {"eqpId": "X4", "process": "P", "onoff": 1, "webmanagerUse": 0},
            {"eqpId": "Y1", "process": "Q", "onoff": 1, "webmanagerUse": 1},
        ]
    )
    repo = EqpInfoRepository(coll)
    assert await repo.count_active_by_process("P") == 2
    assert await repo.count_active_by_process("Q") == 1
    assert await repo.count_active_by_process("Z") == 0


# ----------------------------------------------------------------------
# seed_default_profile — idempotency
# ----------------------------------------------------------------------
async def test_seed_is_idempotent(fresh_mongo_db):
    """두 번 호출해도 동일 프로파일에 대해 두 번째는 upsert 스킵."""
    coll = fresh_mongo_db["RESOURCE_MONITOR_PROFILE"]
    repo = ProfileRepository(coll)

    await seed_default_profile(repo)
    first_id = (await repo.find_by_scope(build_default_profile().scope)).id

    await seed_default_profile(repo)
    second_id = (await repo.find_by_scope(build_default_profile().scope)).id

    assert first_id == second_id  # 같은 문서 그대로 유지
    # 그리고 문서가 정확히 1개
    total = await coll.count_documents({})
    assert total == 1
