"""Phase 1 통합 E2E — 실제 ES/Mongo/Redis 위에서 v2 분석→이메일 발송 흐름 검증.

``AnalysisEngine.run_analysis(process, interval)`` 를 in-process 로 직접 호출한다.
운영 ES 스키마(EARS_* row)로 색인하고, v2 프로파일(measures/rules/notify)을
seed 한 뒤 전 구간을 초 단위·결정적으로 탄다:

    EARS_* 집계 → measure→fact→rule 평가 → cooldown(실 Redis) → email(mock)

ZK 분산 조정은 분석 로직에 영향이 없어 ``NoOpZKLock`` 으로 대체한다.
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
from src.db.models import (
    Condition,
    Fact,
    Measure,
    MonitorProfile,
    NotifyChannel,
    Rule,
    Scope,
)
from src.config.constants import COLL_RMS_EMAIL_TEMPLATE
from src.db.repository import (
    EqpInfoRepository,
    ProfileRepository,
    RmsEmailTemplateRepository,
)
from src.distributed.lock import NoOpZKLock
from src.es.client import ESClient
from src.es.queries import QueryBuilder

pytestmark = [pytest.mark.integration]

_INTERVAL = 5  # all rules below use interval_minutes=5


# ======================================================================
# 공통 헬퍼
# ======================================================================
def _make_settings(email_url: str, ns: Any, *, custom_body: bool = False) -> AppSettings:
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
        # Option C dark-launch flag: off by default so the legacy 9-field payload
        # tests are unaffected; flipped on per-test for the renderedBody cases.
        rms_custom_body_enabled=custom_body,
    )


def _index_name(process: str) -> str:
    # Indices roll over on the UTC calendar (same clock resolve_index_range +
    # the EARS_TIMESTAMP filter use). Seed docs carry a UTC timestamp, so the
    # index must be named by the UTC date to match what the engine queries.
    day = datetime.now(UTC).strftime("%Y.%m.%d")
    return f"{process.lower()}_all-{day}"


async def _seed_es(real_es: Any, process: str, rows: list[dict[str, Any]]) -> str:
    """운영 EARS_* row 형식으로 인덱스 생성 + 색인.

    각 row = {eqpId, category, metric, value, [proc]} — EARS_* 필드로 매핑된다.
    """
    index = _index_name(process)
    # 운영 EARS 인덱스와 동일하게 문자열 필드는 text + .keyword 서브필드로 매핑한다.
    # (운영 확인됨) 이래야 코드의 `.keyword` 집계/필터 경로가 실제로 검증된다 —
    # 과거 bare keyword 픽스처는 운영을 재현 못 해 집계 깨짐 버그를 못 잡았다.
    _text_kw = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}
    properties = {
        "EARS_TIMESTAMP": {"type": "date"},
        "EARS_EQPID": _text_kw,
        "EARS_PROCNAME": _text_kw,
        "EARS_CATEGORY": _text_kw,
        "EARS_METRIC": _text_kw,
        "EARS_VALUE": {"type": "double"},
    }
    await real_es.indices.delete(index=index, ignore=[404])
    await real_es.indices.create(index=index, body={"mappings": {"properties": properties}})
    now_iso = datetime.now(UTC).isoformat()
    for row in rows:
        await real_es.index(
            index=index,
            body={
                "EARS_TIMESTAMP": now_iso,
                "EARS_EQPID": row["eqpId"],
                "EARS_PROCNAME": row.get("proc", "@system"),
                "EARS_CATEGORY": row["category"],
                "EARS_METRIC": row["metric"],
                "EARS_VALUE": row["value"],
            },
            refresh="true",
        )
    return index


async def _seed_eqp_info(mongo_db: Any, process: str, eqp_id: str, **overrides: Any) -> None:
    doc = {
        "eqpId": eqp_id, "eqpModel": "MODEL-E2E", "process": process,
        "line": "L-E2E", "localpc": f"PC-{eqp_id}", "ipAddr": "10.0.0.99",
        "category": process.lower(), "onoff": 1, "webmanagerUse": 1,
    }
    doc.update(overrides)
    await mongo_db["EQP_INFO"].insert_one(doc)


async def _seed_profile(mongo_db: Any, profile: MonitorProfile) -> None:
    await ProfileRepository(mongo_db["RESOURCE_MONITOR_PROFILE"]).upsert(profile)


async def _seed_template(
    mongo_db: Any, *, process: str, model: str, subcode: str,
    html: str, title: str, app: str = "ARS", code: str = "RESOURCE_MONITOR",
) -> None:
    """Insert one RESOURCE_MONITOR_EMAIL_TEMPLATE row (operator-authored body).

    The RMS accessor is read-only (it never writes), so the test seeds the
    collection directly — the engine's _dispatch then fetches it via the 5-tier
    fallback when ``rms_custom_body_enabled`` is on."""
    await mongo_db[COLL_RMS_EMAIL_TEMPLATE].insert_one({
        "app": app, "process": process, "model": model,
        "code": code, "subcode": subcode, "html": html, "title": title,
    })


def _cpu_profile(process: str, *, rules: list[Rule], cooldown: int = 30) -> MonitorProfile:
    return MonitorProfile(
        scope=Scope(process=process),
        measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                          window_minutes=10, facts=[Fact(type="max")])],
        rules=rules,
        notify={"default": NotifyChannel(cooldown_minutes=cooldown)},
    )


def _warn(value=80):
    return Rule(id="cpu_warn", interval_minutes=_INTERVAL, severity="WARNING",
                when=[Condition(fact="cpu.max", op=">=", value=value)])


def _crit(value=95):
    return Rule(id="cpu_crit", interval_minutes=_INTERVAL, severity="CRITICAL",
                when=[Condition(fact="cpu.max", op=">=", value=value)])


def _make_engine(real_es, mongo_db, real_redis, settings) -> AnalysisEngine:
    es_client = ESClient(settings)
    es_client._client = real_es
    redis_client = RedisClient(settings)
    redis_client._client = real_redis
    deps = SimpleNamespace(
        es=es_client,
        eqp_info_repo=EqpInfoRepository(mongo_db["EQP_INFO"]),
        profile_repo=ProfileRepository(mongo_db["RESOURCE_MONITOR_PROFILE"]),
        query_builder=QueryBuilder(settings),
        zk_lock=NoOpZKLock(),
        cooldown_mgr=AlertCooldownManager(redis_client, settings=settings),
        email_client=EmailAlertClient(settings),
        template_repo=RmsEmailTemplateRepository(mongo_db[COLL_RMS_EMAIL_TEMPLATE]),
    )
    return AnalysisEngine(deps, settings)


@pytest.fixture
async def phase1_db(real_mongo: Any, ns: Any):
    db_name = f"{ns.mongo_db}_phase1_{uuid.uuid4().hex[:6]}"
    yield real_mongo[db_name]
    await real_mongo.drop_database(db_name)


async def _drive(real_es, mongo_db, real_redis, ns, email_url, process, *, runs=1,
                 custom_body=False):
    settings = _make_settings(email_url, ns, custom_body=custom_body)
    engine = _make_engine(real_es, mongo_db, real_redis, settings)
    await engine._deps.email_client.connect()
    results = []
    try:
        for _ in range(runs):
            results.append(await engine.run_analysis(process, _INTERVAL))
    finally:
        await engine._deps.email_client.close()
    return results


# ======================================================================
# E0 — fixture 가용성
# ======================================================================
async def test_fixtures_available(real_es, real_mongo, real_redis, ns, mock_email_server):
    assert await real_es.ping() is True
    assert (await real_mongo.admin.command("ping"))["ok"] == 1.0
    assert await real_redis.ping() is True
    assert mock_email_server["url"].endswith("/EmailNotify")


# ======================================================================
# E1 — 임계 초과 → WARNING 이메일 (happy path)
# ======================================================================
async def test_threshold_breach_sends_email(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_CPU", "E2E-CPU-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 92.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    assert result.breaches[0].eqp_id == eqp_id
    (payload,) = mock_email_server["received"]
    assert payload["hostname"] == eqp_id  # hostname=eqpId (localpc 아님)
    assert payload["code"] == "RESOURCE_MONITOR"
    assert payload["subcode"] == "CPU_WARNING"
    assert payload["app"] == "ARS"
    assert payload["model"] == "MODEL-E2E"
    assert float(payload["variables"]["CurrentValue"]) == pytest.approx(92.0)
    assert payload["variables"]["MetricName"] == "cpu.max"
    assert payload["variables"]["Severity"] == "WARNING"


# ======================================================================
# E2 — CRITICAL 분류
# ======================================================================
async def test_critical_severity(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_CPUC", "E2E-CPUC-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 97.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_crit()]))
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert result.breaches[0].severity == "CRITICAL"
    (payload,) = mock_email_server["received"]
    assert payload["subcode"] == "CPU_CRITICAL"


# ======================================================================
# E3 — 임계 미만 무발송
# ======================================================================
async def test_below_threshold_silent(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_QUIET", "E2E-QUIET-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 50.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert result.breaches == []
    assert mock_email_server["received"] == []


# ======================================================================
# E3b — 비활성 rule(enabled=False)은 임계 초과여도 발화하지 않는다
# ======================================================================
async def test_disabled_rule_does_not_fire(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_DISABLED", "E2E-DISABLED-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 99.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    disabled_warn = Rule(id="cpu_warn", interval_minutes=_INTERVAL, severity="WARNING",
                         enabled=False,
                         when=[Condition(fact="cpu.max", op=">=", value=80)])
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[disabled_warn]))
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert result.breaches == []  # 99 ≥ 80 이지만 rule이 꺼져 있어 평가 안 됨
    assert mock_email_server["received"] == []


# ======================================================================
# E4 — cooldown 억제 + 5-dim 키 (실 Redis)
# ======================================================================
async def test_cooldown_suppresses_second_alert(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_COOL", "E2E-COOL-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 92.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()], cooldown=30))
    try:
        await _drive(real_es, phase1_db, real_redis, ns,
                     mock_email_server["url"], process, runs=2)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(mock_email_server["received"]) == 1  # second run suppressed
    key = f"{ns.redis_prefix}:cooldown:{process}:{eqp_id}:@system:default:WARNING"
    assert await real_redis.exists(key) == 1
    ttl = await real_redis.ttl(key)
    assert 0 < ttl <= 30 * 60


# ======================================================================
# E5 — process_watch (required down) — min==0 condition, proc grouping
# ======================================================================
async def test_process_watch_required_down(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_PROC", "E2E-PROC-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "process_watch",
                             "metric": "required", "value": 0.0, "proc": "critd"}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    profile = MonitorProfile(
        scope=Scope(process=process),
        measures=[Measure(id="proc_req", category="process_watch", metric="required",
                          proc="*", window_minutes=10, facts=[Fact(type="min")])],
        rules=[Rule(id="proc_down", interval_minutes=_INTERVAL, severity="CRITICAL",
                    when=[Condition(fact="proc_req.min", op="==", value=0, quantifier="any")])],
        notify={"default": NotifyChannel(cooldown_minutes=30)},
    )
    await _seed_profile(phase1_db, profile)
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    assert result.breaches[0].severity == "CRITICAL"
    assert result.breaches[0].proc == "critd"  # proc dimension from EARS_PROCNAME
    (payload,) = mock_email_server["received"]
    assert payload["subcode"] == "PROCESS_WATCH_CRITICAL"
    assert payload["variables"]["MetricName"] == "proc_req.min"


# ======================================================================
# E6 — 다중 장비 부분 발송
# ======================================================================
async def test_multi_equipment_partial(real_es, phase1_db, real_redis, ns, mock_email_server):
    process = "E2E_MULTI"
    breached = {"E2E-MULTI-01", "E2E-MULTI-03"}
    index = await _seed_es(real_es, process, [
        {"eqpId": "E2E-MULTI-01", "category": "cpu", "metric": "total_used_pct", "value": 91.0},
        {"eqpId": "E2E-MULTI-02", "category": "cpu", "metric": "total_used_pct", "value": 40.0},
        {"eqpId": "E2E-MULTI-03", "category": "cpu", "metric": "total_used_pct", "value": 99.0},
    ])
    for e in ("E2E-MULTI-01", "E2E-MULTI-02", "E2E-MULTI-03"):
        await _seed_eqp_info(phase1_db, process, e)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert {b.eqp_id for b in result.breaches} == breached
    received = mock_email_server["received"]
    assert {p["hostname"] for p in received} == breached  # hostname=eqpId


# ======================================================================
# E6b — notify.group_by="model": 동일 모델 다중 장비 → 메일 1통(집계)
# ======================================================================
async def test_group_by_model_collapses_to_one_email(real_es, phase1_db, real_redis, ns, mock_email_server):
    process = "E2E_GROUP"
    index = await _seed_es(real_es, process, [
        {"eqpId": "E2E-G-01", "category": "cpu", "metric": "total_used_pct", "value": 91.0},
        {"eqpId": "E2E-G-02", "category": "cpu", "metric": "total_used_pct", "value": 99.0},
    ])
    for e in ("E2E-G-01", "E2E-G-02"):
        await _seed_eqp_info(phase1_db, process, e, eqpModel="MODEL-G")
    profile = MonitorProfile(
        scope=Scope(process=process),
        measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                          window_minutes=10, facts=[Fact(type="max")])],
        rules=[_warn()],
        notify={"default": NotifyChannel(cooldown_minutes=30, group_by="model")},
    )
    await _seed_profile(phase1_db, profile)
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert {b.eqp_id for b in result.breaches} == {"E2E-G-01", "E2E-G-02"}
    received = mock_email_server["received"]
    assert len(received) == 1  # 동일 모델 2대 → 1통으로 집계
    (payload,) = received
    assert payload["hostname"] == "E2E-G-01"  # 대표 = 최소 eqpId
    assert set(payload["variables"]["AffectedEquipment"].split(", ")) == {"E2E-G-01", "E2E-G-02"}
    assert payload["variables"]["AffectedCount"] == "2"


# ======================================================================
# E7 — 🔴 dead-path 회귀 가드: model overlay 가 실제 알림에 반영
# ======================================================================
async def test_model_overlay_reaches_alerts(real_es, phase1_db, real_redis, ns, mock_email_server):
    process = "E2E_OVERLAY"
    base_eqp, ov_eqp = "E2E-BASE-01", "E2E-OV-01"
    index = await _seed_es(real_es, process, [
        {"eqpId": base_eqp, "category": "cpu", "metric": "total_used_pct", "value": 98.0},
        {"eqpId": ov_eqp, "category": "cpu", "metric": "total_used_pct", "value": 98.0},
    ])
    await _seed_eqp_info(phase1_db, process, base_eqp, eqpModel="MODEL-E2E")
    await _seed_eqp_info(phase1_db, process, ov_eqp, eqpModel="MODEL-B")
    # process-level base: WARNING only
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    # eqp overlay: adds a CRITICAL rule for MODEL-B/ov_eqp (inherits cpu measure)
    overlay = MonitorProfile(
        scope=Scope(process=process, eqp_model="MODEL-B", eqp_id=ov_eqp),
        rules=[_crit()],
    )
    await _seed_profile(phase1_db, overlay)
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    received = mock_email_server["received"]
    crit = [p for p in received if p["variables"]["Severity"] == "CRITICAL"]
    # the overlay's CRITICAL rule DID reach alerts, and only for the overlay eqp
    assert len(crit) == 1
    assert crit[0]["hostname"] == ov_eqp  # hostname=eqpId
    # base eqp never escalates to CRITICAL
    by_sev = {(b.eqp_id, b.severity) for b in result.breaches}
    assert (ov_eqp, "CRITICAL") in by_sev
    assert (base_eqp, "CRITICAL") not in by_sev


# ======================================================================
# E8 — 🔴 category 충돌 가드 (P7): cpu vs memory 의 동일 metric 분리
# ======================================================================
async def test_category_filter_separates_same_metric(real_es, phase1_db, real_redis, ns, mock_email_server):
    process, eqp_id = "E2E_CAT", "E2E-CAT-01"
    # 동일 metric 이름 total_used_pct 가 cpu=40(정상) / memory=90(초과)
    index = await _seed_es(real_es, process, [
        {"eqpId": eqp_id, "category": "cpu", "metric": "total_used_pct", "value": 40.0},
        {"eqpId": eqp_id, "category": "memory", "metric": "total_used_pct", "value": 90.0},
    ])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    profile = MonitorProfile(
        scope=Scope(process=process),
        measures=[
            Measure(id="cpu", category="cpu", metric="total_used_pct",
                    window_minutes=10, facts=[Fact(type="max")]),
            Measure(id="mem", category="memory", metric="total_used_pct",
                    window_minutes=10, facts=[Fact(type="max")]),
        ],
        rules=[
            Rule(id="cpu_warn", interval_minutes=_INTERVAL, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)]),
            Rule(id="mem_warn", interval_minutes=_INTERVAL, severity="WARNING",
                 when=[Condition(fact="mem.max", op=">=", value=80)]),
        ],
        notify={"default": NotifyChannel(cooldown_minutes=30)},
    )
    await _seed_profile(phase1_db, profile)
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    # category 필터가 동작하면 memory(90)만 breach, cpu(40)는 정상
    assert len(result.breaches) == 1
    assert result.breaches[0].fact == "mem.max"
    assert result.breaches[0].category == "memory"
    (payload,) = mock_email_server["received"]
    assert payload["subcode"] == "MEMORY_WARNING"


# ======================================================================
# P4 — Option C: renderedBody/title 페이로드 (rms_custom_body_enabled ON)
# ======================================================================
# 공통 ERB 템플릿 조각: 행마다 1<tr>. P4-1/P4-2가 행 개수로 단일/그룹을 구분.
_ERB_HTML_3COL = (
    "<table><!--@EachEquipment-->"
    "<tr><td>@Row.EqpId</td><td>@Row.Model</td><td>@Row.CurrentValue</td></tr>"
    "<!--@EndEachEquipment--></table>"
)
_ERB_HTML_2COL = (
    "<table><!--@EachEquipment-->"
    "<tr><td>@Row.EqpId</td><td>@Row.CurrentValue</td></tr>"
    "<!--@EndEachEquipment--></table>"
)


# P4-1 — group_by="model" + 운영자 템플릿: 동일 모델 3대 → 1통, renderedBody N행 + escape
async def test_group_model_renders_n_row_table(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process = "E2E_TMPL"
    # eqpModel 에 '&' 를 넣어 @Row.Model 의 HTML escape 를 end-to-end 로 검증한다.
    model = "M&G"
    rows = [
        {"eqpId": "E2E-T-01", "category": "cpu", "metric": "total_used_pct", "value": 91.0},
        {"eqpId": "E2E-T-02", "category": "cpu", "metric": "total_used_pct", "value": 95.0},
        {"eqpId": "E2E-T-03", "category": "cpu", "metric": "total_used_pct", "value": 99.0},
    ]
    index = await _seed_es(real_es, process, rows)
    for r in rows:
        await _seed_eqp_info(phase1_db, process, r["eqpId"], eqpModel=model)
    profile = MonitorProfile(
        scope=Scope(process=process),
        measures=[Measure(id="cpu", category="cpu", metric="total_used_pct",
                          window_minutes=10, facts=[Fact(type="max")])],
        rules=[_warn()],
        notify={"default": NotifyChannel(cooldown_minutes=30, group_by="model")},
    )
    await _seed_profile(phase1_db, profile)
    await _seed_template(
        phase1_db, process=process, model=model, subcode="CPU_WARNING",
        html=_ERB_HTML_3COL, title="[EARS] @Category @Severity x@AffectedCount",
    )
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process, custom_body=True)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert {b.eqp_id for b in result.breaches} == {"E2E-T-01", "E2E-T-02", "E2E-T-03"}
    (payload,) = mock_email_server["received"]  # 동일 모델 3대 → 1통
    body = payload["renderedBody"]
    # 3행이 모두 펼쳐졌다
    assert body.count("<tr>") == 3
    for eqp in ("E2E-T-01", "E2E-T-02", "E2E-T-03"):
        assert eqp in body
    # @Row.Model 값의 '&' 가 escape 됐다(M&G → M&amp;G). raw 'M&G' 는 남지 않는다.
    assert "M&amp;G" in body
    assert "M&G" not in body
    # 기본 정렬(현재값 worst 우선): 99(T-03) 행이 91(T-01) 행보다 앞선다
    assert body.index("E2E-T-03") < body.index("E2E-T-01")
    # 제목 = 스칼라 치환 결과(콜론 제거 규칙은 'CPU'/'WARNING' 에 영향 없음)
    assert payload["title"] == "[EARS] CPU WARNING x3"
    # 대표 hostname = 최소 eqpId, affected 변수도 함께 유지
    assert payload["hostname"] == "E2E-T-01"
    assert payload["variables"]["AffectedCount"] == "3"


# P4-2 — group_by="eqp"(기본) + 운영자 템플릿: 단일 장비 → renderedBody 1행
async def test_single_mode_one_row(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process, eqp_id = "E2E_TMPL1", "E2E-S-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 92.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)  # eqpModel=MODEL-E2E (기본)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    await _seed_template(
        phase1_db, process=process, model="MODEL-E2E", subcode="CPU_WARNING",
        html=_ERB_HTML_2COL, title="[EARS] @Hostname",
    )
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process, custom_body=True)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    (payload,) = mock_email_server["received"]
    body = payload["renderedBody"]
    assert body.count("<tr>") == 1  # 단일 장비 → 1행
    assert eqp_id in body
    assert payload["title"] == "[EARS] E2E-S-01"


# P4-3 — 회귀 가드: flag OFF → 페이로드 정확히 9필드(renderedBody/title 없음)
async def test_flag_off_legacy_payload(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process, eqp_id = "E2E_OFF", "E2E-OFF-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 92.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    # 템플릿이 존재해도 flag OFF 면 조회/렌더하지 않아야 한다(다크런치 보장).
    await _seed_template(
        phase1_db, process=process, model="MODEL-E2E", subcode="CPU_WARNING",
        html=_ERB_HTML_2COL, title="[EARS] should-not-be-used",
    )
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process, custom_body=False)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    (payload,) = mock_email_server["received"]
    assert set(payload.keys()) == {
        "hostname", "ip", "app", "process", "model", "line",
        "code", "subcode", "variables",
    }
    assert "renderedBody" not in payload
    assert "title" not in payload


# P4-4 — flag ON 이지만 매칭 템플릿 없음 → 내장 기본 본문으로 발송(실패 0)
async def test_template_miss_builtin(
    real_es, phase1_db, real_redis, ns, mock_email_server
):
    process, eqp_id = "E2E_MISS", "E2E-MISS-01"
    index = await _seed_es(real_es, process,
                           [{"eqpId": eqp_id, "category": "cpu",
                             "metric": "total_used_pct", "value": 92.0}])
    await _seed_eqp_info(phase1_db, process, eqp_id)
    await _seed_profile(phase1_db, _cpu_profile(process, rules=[_warn()]))
    # 템플릿 미시드 → find_template None → DEFAULT_BODY/DEFAULT_TITLE 폴백
    try:
        (result,) = await _drive(real_es, phase1_db, real_redis, ns,
                                 mock_email_server["url"], process, custom_body=True)
    finally:
        await real_es.indices.delete(index=index, ignore=[404])

    assert len(result.breaches) == 1
    (payload,) = mock_email_server["received"]  # "no template" 로 실패하지 않고 발송됨
    body = payload["renderedBody"]
    assert body  # 비어있지 않음
    assert "임계 초과" in body  # 내장 기본 본문 시그니처
    assert eqp_id in body  # ERB 가 장비 행을 채움
    assert payload["title"] == "[EARS] 자원 모니터링 알림"  # 내장 기본 제목(콜론 없음)
