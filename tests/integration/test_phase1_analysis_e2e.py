"""Phase 1 통합 E2E — 실제 ES/Mongo/Redis 위에서 분석→이메일 발송 흐름 검증.

`AnalysisEngine.run_analysis()` 를 in-process 로 직접 호출한다. uvicorn 전체
부팅이나 스케줄러 interval 대기 없이 초 단위·결정적으로 Phase 1 전 구간을 탄다:

    ES 집계 → threshold 평가 → cooldown(실 Redis) → email(mock) → cooldown set

ZK 분산 조정은 분석 로직에 영향이 없어 ``NoOpZKLock`` 으로 대체한다
(leader election/partition 은 tests/e2e/test_multi_instance.py 가 커버).

기존 integration conftest 의 ``real_es / real_mongo / real_redis / ns /
mock_email_server`` fixture 를 그대로 재사용한다.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.alert.email_client import EmailAlertClient
from src.analyzer.engine import AnalysisEngine
from src.cache.cooldown import AlertCooldownManager
from src.cache.redis_client import RedisClient
from src.config.settings import AppSettings
from src.db.models import AnalysisConfig, MetricSchedule, MonitorProfile, Scope, ThresholdConfig
from src.db.repository import EqpInfoRepository, ProfileRepository
from src.distributed.lock import NoOpZKLock
from src.es.client import ESClient
from src.es.queries import QueryBuilder

pytestmark = [pytest.mark.integration]


# ======================================================================
# 공통 헬퍼
# ======================================================================
def _make_settings(email_url: str, ns: Any) -> AppSettings:
    """테스트 인프라를 가리키는 AppSettings.

    redis_key_prefix 는 ns 로 격리해 cooldown 키가 다른 run 과 충돌하지 않게 한다.
    grafana 는 비워서 alert variables 의 GrafanaUrl 이 "" 가 되도록.
    """
    return AppSettings(
        es_hosts=["http://localhost:9200"],
        redis_url="redis://localhost:6379/15",
        redis_key_prefix=ns.redis_prefix,
        email_api_url=email_url,
        email_api_timeout=5,
        email_app_name="ARS",
        grafana_base_url="",
        grafana_dashboard_uid="",
        local_tz="Asia/Seoul",
        debug_read_only=False,
    )


def _index_name(ns: Any, process: str) -> str:
    """분석 엔진의 resolve_index_range 와 동일한 규칙으로 오늘 인덱스명 생성.

    엔진은 ``{process_lower}_all-{YYYY.MM.DD}`` (Asia/Seoul) 를 조회한다.
    ns prefix 를 붙여 격리하되, process 는 prefix 안에 녹여 분석 호출 시
    같은 이름이 나오도록 한다.
    """
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d")
    proc = process.lower()
    return f"{proc}_all-{day}"


async def _seed_es_metrics(
    real_es: Any,
    process: str,
    docs: list[dict[str, Any]],
    numeric_fields: list[str],
) -> str:
    """ES 인덱스를 mapping 과 함께 만들고 메트릭 문서를 색인(refresh)한다.

    :param docs: 각 문서는 최소 {eqpId, <numeric_fields...>} 를 포함.
                 @timestamp/process 는 자동으로 채운다.
    :returns: 생성된 인덱스명
    """
    index = _index_name(None, process)  # ns 미사용 — 엔진 규칙과 정확히 일치해야 함
    # mapping: 엔진은 eqpId.keyword / process.keyword 서브필드를 쓴다.
    # 운영 데이터의 dynamic mapping(문자열 → text + .keyword)을 그대로 재현한다.
    # 메트릭은 double, @timestamp 는 date 로 명시.
    str_with_keyword = {
        "type": "text",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
    }
    properties: dict[str, Any] = {
        "@timestamp": {"type": "date"},
        "eqpId": str_with_keyword,
        "process": str_with_keyword,
    }
    for f in numeric_fields:
        properties[f] = {"type": "double"}

    # 이전 잔재 제거 후 생성 (격리)
    await real_es.indices.delete(index=index, ignore=[404])
    await real_es.indices.create(index=index, body={"mappings": {"properties": properties}})

    now_iso = datetime.now(UTC).isoformat()
    for doc in docs:
        body = {"@timestamp": now_iso, "process": process, **doc}
        await real_es.index(index=index, body=body, refresh="true")
    return index


async def _seed_eqp_info(mongo_db: Any, process: str, eqp_id: str, **overrides: Any) -> None:
    """EQP_INFO 에 active 장비 1대 삽입."""
    doc = {
        "eqpId": eqp_id,
        "eqpModel": "MODEL-E2E",
        "process": process,
        "line": "L-E2E",
        "localpc": f"PC-{eqp_id}",
        "ipAddr": "10.0.0.99",
        "category": process.lower(),
        "onoff": 1,
        "webmanagerUse": 1,
    }
    doc.update(overrides)
    await mongo_db["EQP_INFO"].insert_one(doc)


async def _seed_profile(
    mongo_db: Any,
    process: str,
    metric_pattern: str,
    *,
    warning: float = 80.0,
    critical: float = 95.0,
    cooldown_minutes: int = 30,
) -> AnalysisConfig:
    """RESOURCE_MONITOR_PROFILE 에 process 스코프 프로파일 upsert + config 반환."""
    config = AnalysisConfig(
        metric_pattern=metric_pattern,
        threshold=ThresholdConfig(
            warning=warning, critical=critical, cooldown_minutes=cooldown_minutes
        ),
        schedule=MetricSchedule(interval_minutes=5, window_minutes=10),
    )
    profile = MonitorProfile(
        scope=Scope(process=process, eqp_model="*", eqp_id="*"),
        analysis_configs=[config],
    )
    repo = ProfileRepository(mongo_db["RESOURCE_MONITOR_PROFILE"])
    await repo.upsert(profile)
    return config


def _make_engine(
    real_es: Any,
    mongo_db: Any,
    real_redis: Any,
    settings: AppSettings,
) -> AnalysisEngine:
    """실 클라이언트로 AnalysisEngine deps 를 조립한다 (ZK 는 NoOpLock)."""
    es_client = ESClient(settings)
    es_client._client = real_es  # 이미 연결된 raw AsyncElasticsearch 주입

    redis_client = RedisClient(settings)
    redis_client._client = real_redis  # 이미 연결된 raw Redis 주입

    deps = SimpleNamespace(
        es=es_client,
        eqp_info_repo=EqpInfoRepository(mongo_db["EQP_INFO"]),
        profile_repo=ProfileRepository(mongo_db["RESOURCE_MONITOR_PROFILE"]),
        query_builder=QueryBuilder(settings),
        zk_lock=NoOpZKLock(),
        cooldown_mgr=AlertCooldownManager(redis_client, settings=settings),
        email_client=EmailAlertClient(settings),
    )
    return AnalysisEngine(deps, settings)


@pytest.fixture
async def phase1_db(real_mongo: Any, ns: Any):
    """격리된 테스트 DB. 끝나면 drop."""
    db_name = f"{ns.mongo_db}_phase1_{uuid.uuid4().hex[:6]}"
    yield real_mongo[db_name]
    await real_mongo.drop_database(db_name)


async def _drive_analysis(
    real_es: Any,
    mongo_db: Any,
    real_redis: Any,
    ns: Any,
    email_url: str,
    process: str,
    config: AnalysisConfig,
    *,
    runs: int = 1,
) -> list[Any]:
    """엔진을 조립해 ``run_analysis`` 를 ``runs`` 회 호출하고 결과 리스트 반환.

    email client 의 connect/close 보일러플레이트를 캡슐화한다.
    """
    settings = _make_settings(email_url, ns)
    engine = _make_engine(real_es, mongo_db, real_redis, settings)
    await engine._deps.email_client.connect()
    results = []
    try:
        for _ in range(runs):
            results.append(await engine.run_analysis(process, config))
    finally:
        await engine._deps.email_client.close()
    return results


# ======================================================================
# E0 — fixture 가용성 검증 (위험 1: e2e/integration conftest 호환)
# ======================================================================
async def test_fixtures_available(real_es, real_mongo, real_redis, ns, mock_email_server):
    """이 파일 위치에서 실 인프라 fixture 들이 주입되는지부터 확인한다."""
    assert await real_es.ping() is True
    assert (await real_mongo.admin.command("ping"))["ok"] == 1.0
    assert await real_redis.ping() is True
    assert ns.es_index_prefix.startswith("test_")
    assert mock_email_server["url"].endswith("/EmailNotify")


# ======================================================================
# E1 — 임계 초과 시 이메일 발송 (happy path)
# ======================================================================
async def test_threshold_breach_sends_email(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_CPU"
    eqp_id = "E2E-CPU-01"
    index = await _seed_es_metrics(
        real_es, process,
        docs=[{"eqpId": eqp_id, "total_used_pct": 92.0}],
        numeric_fields=["total_used_pct"],
    )
    await _seed_eqp_info(phase1_db, process, eqp_id)
    config = await _seed_profile(phase1_db, process, "total_used_pct")

    try:
        (result,) = await _drive_analysis(
            real_es, phase1_db, real_redis, ns,
            mock_email_server["url"], process, config,
        )
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    # 분석 결과: 1건 breach
    assert len(result.breaches) == 1
    assert result.breaches[0].eqp_id == eqp_id

    # 이메일 1건 발송됨
    received = mock_email_server["received"]
    assert len(received) == 1
    payload = received[0]
    assert payload["code"] == "RESOURCE_MONITOR"
    assert payload["subcode"] == "CPU_WARNING"
    assert payload["app"] == "ARS"
    assert payload["model"] == "MODEL-E2E"
    assert float(payload["variables"]["CurrentValue"]) == pytest.approx(92.0)
    assert payload["variables"]["Severity"] == "WARNING"


# ======================================================================
# E2 — CRITICAL 심각도 분류
# ======================================================================
async def test_critical_severity_classification(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_CPUC"
    eqp_id = "E2E-CPUC-01"
    index = await _seed_es_metrics(
        real_es, process,
        docs=[{"eqpId": eqp_id, "total_used_pct": 97.0}],  # critical=95 초과
        numeric_fields=["total_used_pct"],
    )
    await _seed_eqp_info(phase1_db, process, eqp_id)
    config = await _seed_profile(phase1_db, process, "total_used_pct")

    try:
        (result,) = await _drive_analysis(
            real_es, phase1_db, real_redis, ns,
            mock_email_server["url"], process, config,
        )
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    assert result.breaches[0].severity == "CRITICAL"

    payload = mock_email_server["received"][0]
    assert payload["subcode"] == "CPU_CRITICAL"
    assert payload["variables"]["Severity"] == "CRITICAL"


# ======================================================================
# E3 — 임계 미만이면 무발송
# ======================================================================
async def test_below_threshold_sends_nothing(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_QUIET"
    eqp_id = "E2E-QUIET-01"
    index = await _seed_es_metrics(
        real_es, process,
        docs=[{"eqpId": eqp_id, "total_used_pct": 50.0}],  # warning=80 미만
        numeric_fields=["total_used_pct"],
    )
    await _seed_eqp_info(phase1_db, process, eqp_id)
    config = await _seed_profile(phase1_db, process, "total_used_pct")

    try:
        (result,) = await _drive_analysis(
            real_es, phase1_db, real_redis, ns,
            mock_email_server["url"], process, config,
        )
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert result.breaches == []
    assert len(mock_email_server["received"]) == 0


# ======================================================================
# E4 — cooldown 억제 (실 Redis)
# ======================================================================
async def test_cooldown_suppresses_second_alert(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_COOL"
    eqp_id = "E2E-COOL-01"
    index = await _seed_es_metrics(
        real_es, process,
        docs=[{"eqpId": eqp_id, "total_used_pct": 92.0}],
        numeric_fields=["total_used_pct"],
    )
    await _seed_eqp_info(phase1_db, process, eqp_id)
    config = await _seed_profile(phase1_db, process, "total_used_pct")

    try:
        # 같은 breach 로 2회 연속 분석 — 2번째는 cooldown 으로 억제돼야 함
        await _drive_analysis(
            real_es, phase1_db, real_redis, ns,
            mock_email_server["url"], process, config, runs=2,
        )
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    # 발송은 1회만
    assert len(mock_email_server["received"]) == 1

    # 실 Redis 에 cooldown 키가 실제로 SETEX 됐는지 확인
    cooldown_key = f"{ns.redis_prefix}:cooldown:{eqp_id}:CPU:total_used_pct"
    assert await real_redis.exists(cooldown_key) == 1
    ttl = await real_redis.ttl(cooldown_key)
    assert 0 < ttl <= config.threshold.cooldown_minutes * 60


# ======================================================================
# E5 — process_watch state_check (required 프로세스 미검출)
# ======================================================================
async def test_process_watch_required_down_alerts(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_PROC"
    eqp_id = "E2E-PROC-01"
    # required=0 → min 집계 0 → "필수 프로세스가 한 번이라도 죽었다" → CRITICAL
    index = await _seed_es_metrics(
        real_es, process,
        docs=[{"eqpId": eqp_id, "required": 0.0}],
        numeric_fields=["required"],
    )
    await _seed_eqp_info(phase1_db, process, eqp_id)
    config = await _seed_profile(phase1_db, process, "required")

    try:
        (result,) = await _drive_analysis(
            real_es, phase1_db, real_redis, ns,
            mock_email_server["url"], process, config,
        )
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    assert result.breaches[0].severity == "CRITICAL"

    payload = mock_email_server["received"][0]
    assert payload["subcode"] == "PROCESS_WATCH_CRITICAL"
    assert payload["variables"]["MetricName"] == "required"


# ======================================================================
# E6 — 다중 장비 부분 발송 (3대 중 2대만 임계 초과)
# ======================================================================
async def test_multi_equipment_partial_alerting(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_MULTI"
    breached = {"E2E-MULTI-01", "E2E-MULTI-03"}
    index = await _seed_es_metrics(
        real_es, process,
        docs=[
            {"eqpId": "E2E-MULTI-01", "total_used_pct": 91.0},  # breach
            {"eqpId": "E2E-MULTI-02", "total_used_pct": 40.0},  # ok
            {"eqpId": "E2E-MULTI-03", "total_used_pct": 99.0},  # breach (critical)
        ],
        numeric_fields=["total_used_pct"],
    )
    for eqp_id in ("E2E-MULTI-01", "E2E-MULTI-02", "E2E-MULTI-03"):
        await _seed_eqp_info(phase1_db, process, eqp_id)
    config = await _seed_profile(phase1_db, process, "total_used_pct")

    try:
        (result,) = await _drive_analysis(
            real_es, phase1_db, real_redis, ns,
            mock_email_server["url"], process, config,
        )
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    # breach 는 2건, 발송도 2건, eqpId 집합이 초과한 2대와 일치
    assert {b.eqp_id for b in result.breaches} == breached
    received = mock_email_server["received"]
    assert len(received) == 2
    assert {p["hostname"] for p in received} == {f"PC-{e}" for e in breached}
