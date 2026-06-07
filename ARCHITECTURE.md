# ARCHITECTURE — ResourceMonitorServer Phase 0–1

이 문서는 **왜 이렇게 설계됐는가**에 집중합니다. "무엇을 만드는가"는 [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md), "어떻게 빌드/실행하는가"는 [README.md](README.md) / [CONTRIBUTING.md](CONTRIBUTING.md) 참고.

> 🟢 **기준정보 데이터 모델 v2 (2026-06-05, 구현 완료)**: 모니터링 기준정보 스키마가 **단일 컬렉션 `measures`/`rules`/`notify` 3계층 + scope 계층 상속(cascade)** 으로 구현되었습니다. **이 문서 §2·§2.1·§2.2는 v2 as-built(현재 코드)를 기술합니다.** v2 데이터 모델·평가 흐름은 아래 [§2.2](#22-v2-데이터-모델--measuresrulesnotify)에 요약하고, 권위 스펙은 [SCHEMA.md](SCHEMA.md), 관리 UI/시인성 설계는 [docs/ADMIN-UI-LEGIBILITY.md](docs/ADMIN-UI-LEGIBILITY.md)에 있습니다.

---

## 1. 시스템 전체 흐름

```
ResourceAgent (Go, 10k~20k)
        │
        ▼
      Kafka  ──►  Elasticsearch 7.11.9
                  ({process}_all-yyyy.MM.dd)
                          │
                          ▼
   ┌──────────────────────────────────────────┐
   │     ResourceMonitorServer (Python)       │
   │                                          │
   │  ┌───────────┐   ┌──────────────────┐   │
   │  │ APScheduler│──►│  Analysis Job    │   │
   │  └───────────┘   │ (per process)    │   │
   │                  └────────┬─────────┘   │
   │                           │             │
   │     ┌──────────┬──────────┼──────────┐  │
   │     ▼          ▼          ▼          ▼  │
   │  ┌────┐    ┌──────┐   ┌─────┐   ┌──────┐│
   │  │ ES │    │Mongo │   │Redis│   │Email ││
   │  │    │    │EARS  │   │TTL  │   │Akka  ││
   │  └────┘    └──────┘   └─────┘   └──────┘│
   │                                          │
   │     ┌─────────────────────────────┐      │
   │     │       Zookeeper 3.5.5       │      │
   │     │  ┌────────┐  ┌────────────┐ │      │
   │     │  │Election│  │ Members /  │ │      │
   │     │  │+ epoch │  │Assignments │ │      │
   │     │  └────────┘  └────────────┘ │      │
   │     └─────────────────────────────┘      │
   └──────────────────────────────────────────┘
```

핵심 데이터 경로:

1. **APScheduler** 가 (process, interval) 단위 분석 job을 트리거
2. job은 ZK 분산 락을 잡고 장비별 effective profile을 cascade로 해석 → measure를 ES에서 집계(EARS_VALUE)해 **fact** 산출 → rule(op/quantifier/combine)로 판단
3. breach 발견 → Redis cooldown 배치 체크 → 장비 단위 알림 → Akka Email API 호출
4. 메트릭/로그/이벤트는 Prometheus + structlog JSON으로 표출

Phase 0에서 **인프라 골격** (락/쿼리/스케줄/cooldown/email 클라이언트) 완성, Phase 1에서 **실제 분석 로직** (AnalysisEngine, measure→fact→rule 평가, 알림 생성) 구현 완료. v2 스키마(단일 컬렉션 measures/rules/notify + cascade + per-eqp 해석)도 구현 완료.

---

## 2. 컴포넌트 책임

### `src/config/`
- `settings.py` — `MONITOR_*` 환경 변수를 `AppSettings`로 검증/주입. `lru_cache`된 `get_settings()`. 테스트에서는 `clear_settings_cache` autouse fixture로 매번 초기화.
- `constants.py` — env로 빼면 안 되는 값(`ALERT_CODE_*`, ZK paths, 캐시 크기). 변경하려면 코드 리뷰가 필요한 값.

### `src/es/` (Elasticsearch 7.11.9)
- `client.py` — `AsyncElasticsearch` 래퍼. `ping()`, `introspect_field_type()`, `get_metric_names(index, category, proc)` (와일드카드 인스턴스 해석용 `EARS_METRIC` terms agg). 양성/음성 TTL 캐시 분리.
- `queries.py` — EARS row 모델 기준. `QueryBuilder.resolve_index_range(process, time_range_minutes)` (자정 횡단 시 `proc_all-2026.04.06,proc_all-2026.04.07` 콤마 결합), `build_metric_aggregation_query(...)` (by_eqp → [by_proc] → [by_metric] → fact별 leaf 중첩), 모듈 함수 `build_fact_sub_aggs(facts)` (fact type명을 키로 한 `EARS_VALUE` 집계 leaf), `build_metric_names_query(...)`.

### `src/db/` (MongoDB / motor)
- `client.py` — `MongoClient` + `connect_with_retry(max_attempts=5, backoff=2.0)` 선형 백오프.
- `models.py` — Pydantic v2 기반 `Scope`, `MonitorProfile` 및 빌딩블록(`Measure`/`Fact`/`Rule`/`Condition`/`NotifyChannel`/`Governance`). `extra="forbid"`, `populate_by_name=True`. `Scope.eqp_model = Field(alias="model")` (Mongo 필드명은 `model`). `MonitorProfile = scope + enabled + governance + measures[] + rules[] + notify{}` ([SCHEMA.md §1](SCHEMA.md), [§2.2](#22-v2-데이터-모델--measuresrulesnotify)). 모듈 함수 `fold_profiles()` (cascade fold), `validate_effective()` (참조 무결성), `lint_effective()` (비치명 경고), `effective_signature()` (버킷팅 해시).
- `repository.py` — `ProfileRepository` (TTLCache 내장), `EqpInfoRepository` (`{onoff: 1, webmanagerUse: 1}` 활성 필터 자동 적용). `resolve_profile(process, model, eqpId)` 는 `collect_scope_docs` 가 매칭 scope를 단일 `$or` 쿼리로 수집 → base→specific `fold_profiles` 합성 → `validate_effective`/`lint_effective` 로그 → effective profile을 `(process, model, eqpId)` 버킷 키로 캐시. (옵티미스틱락 `replace_with_version`, 시더용 `upsert` 별도.)
- `seed.py` — 기본 프로파일 SHA256 비교 후 변경 시에만 upsert.

### `src/cache/` (Redis 5.0.6)
- `redis_client.py` — `Redis.from_url(..., protocol=2)` (Redis 5.0.6은 RESP3 미지원).
- `cooldown.py` — `AlertCooldownManager`. cooldown 키는 5-dim `{prefix}:cooldown:{process}:{eqp}:{proc}:{notify}:{severity}` (suppression 단위 = 장비 × EARS proc × notify 채널 × severity). **Redis 다운 시 local TTLCache로 fallback** (이메일 폭주 방지). `is_cooling_down_batch()`는 pipeline 사용.

### `src/alert/` (Akka HttpWebServer)
- `models.py` — `EmailAlertRequest`. `to_payload()`는 Akka 스키마(`{"model": ...}`)로 변환.
- `email_client.py` — Akka `"Success"` 응답을 case-insensitive 비교 (G8 참고). 5가지 예외 분기 + in-memory outbox.

### `src/distributed/` (Zookeeper 3.5.5 / kazoo)
- `zk_client.py` — `KazooClient` 래퍼. asyncio 루프에서 사용 가능하도록 thread bridge.
- `lock.py` — `ZKAnalysisLock`. **per-process `asyncio.Lock` 직렬화 + 매번 새 `kazoo.Lock` 인스턴스**.
- `leader_election.py` — `LeaderElection`. 전용 `ThreadPoolExecutor`, persistent epoch, `restart_after_loss()`.
- `partition_manager.py` — `PartitionManager`. ChildrenWatch + DataWatch + epoch+timestamp stale defense + 디바운스(2s) 라운드로빈.

### `src/analyzer/` (분석 엔진, v2)
> 흐름: measure(잰다) → fact 산출 → rule(판단: combine/quantifier) → notify(알린다). **엔진이 per-eqp로 effective profile을 cascade 해석**한다 — 과거 process 레벨만 resolve 하던 dead path(model/eqp override가 알림에 미반영)는 **수정 완료** (통합테스트 E7 회귀 가드, [SCHEMA.md §6.5](SCHEMA.md)).
- `engine.py` — `AnalysisEngine`. `run_analysis(process, interval_minutes)` 오케스트레이션: ZK 락 획득 → EQP_INFO 일괄 조회 → 장비별 `resolve_profile` (per-eqp) → `effective_signature` 로 버킷팅 → 버킷당 measure ES 쿼리(`_compute_measure`) → fact 산출(es_parser) → rule 평가(`evaluate_rule`) → 쿨다운 배치 체크 → 장비 단위 알림 발송. Phase 2/3 fact는 스키마 수용·엔진 skip(경고 로그).
- `fact_catalog.py` — 닫힌 `FactType` enum(단일 진실), `ALLOWED_OPS`(type별 허용 연산자), `PHASE_OF_FACT`/`is_implemented()`(Phase 게이팅), `AGG_STRATEGY`/`agg_strategy()`(ES 집계 전략), `op_allowed()` 등.
- `threshold.py` — `evaluate_rule(rule, facts_by_ref, ...)` (op/quantifier any·all·count/combine AND·OR), `evaluate_condition()`, `op_compare()`. `ThresholdBreach`/`AnalysisResult` Pydantic 모델.
- `alert_builder.py` — `build_alert_request()` (category=`breach.category`(=measure.category) 직결, 휴리스틱 분류 없음), `make_cooldown_key()` (`(process, eqpId, proc, notify, severity)` 5-tuple). sub_code = `notify.email_subcode or "{CATEGORY}_{SEVERITY}"`.
- `es_parser.py` — `parse_metric_aggregation()`. EARS_* 중첩 agg 응답(by_eqp → [by_proc] → [by_metric] → fact별 leaf) → `{(eqpId, proc): {fact_type: [value, ...]}}` dict (인스턴스 리스트는 quantifier 범위).
- `metric_resolver.py` — `resolve_metric_patterns()` (fnmatch 와일드카드; 후보 인스턴스는 `ESClient.get_metric_names` 의 `EARS_METRIC` terms agg에서).

### `src/scheduler/`
- `jobs.py` — `AnalysisScheduler`. `AsyncIOScheduler` 래핑. `reload(processes)` 가 process 별 (process,*,*) effective profile 조회 → rule들의 distinct `interval_minutes` 별로 job 등록(id=`analysis-{process}-{interval}m`). interval은 rule 소유(measure는 window만); 엔진이 런타임에 장비별 effective를 재해석하므로 per-eqp override는 그대로 적용됨. `_engine` lazy 초기화로 `AnalysisEngine` 연동.

### `src/api/`
- `health.py` — `/healthz/live` (인프라 무접근), `/healthz/ready` (각 ping에 2s 타임아웃, ZK는 sync `is_connected()`).
- `profiles.py` — 프로파일 CRUD API(`/profiles`). overlay GET/POST/PUT/DELETE, `GET /profiles/effective` (`withProvenance`), measure/rule/notify item 엔드포인트. 모든 쓰기는 `governance.version` 옵티미스틱락(stale=409), 합성된 effective 프로파일 참조무결성 재검증(=422), Mongo 다운=503. (관리 UI는 후속, 미구현)
- `admin.py` — `/admin/status`, `DELETE /admin/cooldowns` (query param `process,eqp_id,proc,notify,severity` — 5-dim identity), `POST /admin/scheduler/reload`, `GET /admin/email-outbox`.
- `metrics.py` — Prometheus collectors (`JOB_TOTAL`, `JOB_DURATION`, `ES_QUERY_DURATION`, `ALERTS_SENT`, `THRESHOLD_BREACHES`, `ALERTS_SUPPRESSED`, `ZK_LEADER`, `ASSIGNED_PROCESSES`).
- `deps.py` — `request.app.state` 기반 DI.

### `src/startup/`
- `infra.py` — `init_infra()` 5개 인프라 순차 연결, 부분 실패 시 `close_partial()`로 역순 정리.
- `repos.py` — `init_repos()`.
- `distributed.py` — `init_distributed()` + **lazy `scheduler_provider` 클로저** (PartitionManager ↔ Scheduler 순환 의존 해결).
- `scheduler_init.py` — `SchedulerDeps` dataclass.

### `src/main.py`
- 11단계 lifespan. 각 phase는 `startup_phase(name)` 컨텍스트로 begin/done/failed 로그.
- 종료 시 역순 try/except.
- 글로벌 Exception handler, `_self_alert_critical()` (best-effort 자가 알림).

---

## 2.1 분석 흐름 (Analysis Flow, v2)

```
PartitionManager._apply_assignment(processes)
        │
        ▼
AnalysisScheduler.reload(processes)
        │
        ├─ for process in processes:
        │      resolve_profile(process, "*", "*")   # 스케줄링용 interval 집합만
        │      for interval in {r.interval_minutes for r in profile.rules}:
        │          register APScheduler job  id=analysis-{process}-{interval}m
        │
        ▼ (매 interval 마다)
AnalysisEngine.run_analysis(process, interval_minutes)
        │
        ├── 1. ZK 분산 락 획득 + ES semaphore (동시 3개 제한)
        ├── 2. EQP_INFO 일괄 조회 → eqp_lookup dict
        ├── 3. 장비별 resolve_profile(process, model, eqpId)  (per-eqp cascade fold)
        │       → effective_signature() 로 동일 프로파일 장비 버킷팅
        ├── 4. 버킷마다, 이 interval 의 rule 선별
        │       ├─ rule들이 참조하는 measure 집합 수집(중복 제거)
        │       ├─ measure별 ES 집계 쿼리(_compute_measure): EARS_VALUE 집계 → fact
        │       │     (와일드카드면 get_metric_names 로 인스턴스 확장)
        │       ├─ es_parser → {(eqpId, proc): {fact_type: [value,...]}}
        │       └─ rule별 (eqp, proc) 타깃마다 evaluate_rule()
        │             (op / quantifier any·all·count / combine AND·OR) → breach
        ├── 5. THRESHOLD_BREACHES 카운터 증가
        ├── 6. is_cooling_down_batch() (단일 Redis 파이프라인, 5-dim 키)
        └── 7. 비쿨다운 (eqp,proc,notify,severity) → build_alert_request() → send_alert()
                                   └── 성공 시 set_cooldown()
```

### fact별 ES 집계 전략 (EARS row 모델)

실제 운영 문서는 **EARS row** (PRD §7.2): (장비, 메트릭, 샘플) 당 1 문서로 `EARS_CATEGORY`/`EARS_METRIC`/`EARS_VALUE`/`EARS_EQPID`/`EARS_PROCNAME`/`EARS_TIMESTAMP` 를 가짐. 문자열 필드는 **`text` + `.keyword` 서브필드**(운영 확인)라 term/terms 필터와 terms 집계는 **`<field>.keyword`** 를 쓴다(`settings.es_keyword_suffix`, 기본 `.keyword`). 메트릭 정체성은 **필터**(term on `EARS_CATEGORY.keyword` + `EARS_METRIC.keyword`)이고 집계 대상은 항상 단일 `EARS_VALUE`(numeric, bare) 컬럼. fact의 type 이름이 곧 집계 leaf의 키.

| fact type | ES 집계 전략 (`AGG_STRATEGY`) | 파싱 |
|-----------|------------------------------|------|
| `max`/`min`/`avg` | `stat` (single-metric agg) | `.value` |
| `p50`/`p90`/`p95`/`p99` | `percentiles` | `.values["<pct>.0"]` |
| `last` | `top_hits` (size 1, EARS_TIMESTAMP desc) | 최신 doc의 `EARS_VALUE` |
| `spike_count` | `filter_range` (fact의 `over`/`direction`) | `.doc_count` |

`state_check` 은 별도 type/함수가 없다 — `min`/`max` + 비교 연산자로 흡수: required(다운) = `min == 0`, forbidden = `max > 0`, health = `max >= 2`. Phase 2/3 fact(`duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`)는 스키마·검증은 수용하되 엔진은 skip(경고 로그).

### code / sub_code 설계

```
code     = notify.email_code  (기본 "RESOURCE_MONITOR")
sub_code = notify.email_subcode or "{CATEGORY}_{SEVERITY}"

CATEGORY = breach.category.upper()  (= measure.category 직결; 휴리스틱 분류 없음)
SEVERITY: WARNING, CRITICAL  (rule이 소유)

예: CPU_WARNING, DISK_CRITICAL — 또는 notify.email_subcode 가 있으면 그 값 그대로
```

`code`/`sub_code` 는 rule이 참조하는 notify 채널에서 나온다(`alert_builder.build_alert_request`). category는 breach가 직접 운반(=measure의 `category`)하므로 과거의 `classify_metric_category` 같은 필드명 휴리스틱 분류는 없다(cpu/memory `total_used_pct` 충돌 제거). Akka `EMAIL_TEMPLATE_REPOSITORY` 에서 `(process, model, code, sub_code)` 로 템플릿 매칭. 이메일 제목은 sub_code 별 고정, 본문은 variables map (`@Severity`, `@Category`, `@MetricName`, `@CurrentValue`, `@Threshold`, `@WindowMin`, `@GrafanaUrl`) 으로 치환.

### 알려진 제약 (Phase 2 대상)

| 항목 | 현재 동작 | Phase 2 계획 |
|------|----------|-------------|
| Profile 갱신 | 분석 시점 per-eqp resolution (구현 완료) | — |
| 데이터 미수신 장비 | 알림 없음 (ES 에 데이터가 없으면 terms bucket 미생성) | DATA_MISSING 알림 타입 |
| 장비별 프로파일 오버라이드 | cascade + per-eqp 해석으로 model/eqp override 동작 (구현 완료) | — |
| 통계/패턴 fact (`duration`/`zscore`/...) | 스키마 수용·엔진 skip | Phase 2/3 엔진 구현 |

---

## 2.2 v2 데이터 모델 — measures/rules/notify

> 🟢 구현 완료(as-built). 권위 스펙은 [SCHEMA.md](SCHEMA.md), 구설계(2분할 RESOURCE_MONITOR_RULE) 대비 차이표는 [SCHEMA.md §13](SCHEMA.md).

기준정보를 **단일 컬렉션** `RESOURCE_MONITOR_PROFILE`(scope당 문서 1개)에 3계층으로 둡니다(구설계의 `RESOURCE_MONITOR_RULE` 2분할은 폐기):

- **measures[]** (잰다) — `{id, category, metric, proc, window_minutes, facts[]}`. 무엇을·어떻게 ES에서 집계해 어떤 **fact**를 산출할지. 주기(interval)는 갖지 않음(집계창 window만).
- **rules[]** (판단 + 주기) — `{id, interval_minutes, severity, combine, when[], notify}`. `when[].fact = "measureId.type"` 로 measure의 fact를 참조. **단순 임계값 = 조건 1개 rule, 복합 = 조건 여러 개 rule** (단일/복합을 컬렉션으로 가르지 않음).
- **notify{}** (알린다) — 이름→`{cooldown_minutes, email_code, email_subcode?}` 맵. rule이 이름으로 참조.

핵심 규칙:
- **1 measure 항목 = 1 fact = 1 type.** `type` 이름이 곧 fact 이름(`max`/`min`/`avg`/`last`/`p50`·`p90`·`p95`·`p99`/`spike_count`/`duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`). 한 measure 내 type 유일. type 카탈로그는 닫힌 enum(`src/analyzer/fact_catalog.py`). Phase 1(`max`~`spike_count`)은 엔진 평가, Phase 2/3은 스키마 수용·엔진 skip.
- **경보 방향**은 rule의 op로 표현(높을때 `>=`, 낮을때 `<=`, 범위이탈 두 조건 OR, 상태 `==`). `state_check`은 별도 type 없이 `min`/`max`로 흡수.
- **scope 계층 상속(cascade)**: 넓은 scope를 base로, 구체 scope가 key(measure.id/rule.id/notify) 기준 통째 override(sparse overlay), `enabled`는 AND fold. [SCHEMA.md §6](SCHEMA.md).

### v2 평가 흐름 (as-built)

```
스케줄러: (process, interval) 단위로 job 등록 (rule.interval_minutes)
        │ 매 interval 틱
        ▼
1. 장비별 per-eqp effective profile 해석(cascade fold) → effective_signature 버킷팅
2. 버킷마다 이 틱 rule들의 when[].fact → 참조 measure 집합 수집(중복 제거)
3. 각 measure를 ES에서 계산(window 적용, 버킷당 한 쿼리 세트) → fact 산출
   - EARS_CATEGORY/EARS_METRIC term 필터 + by_eqp(필요시 ×by_proc/by_metric) + max/min/percentiles/top_hits/filter_range
4. eqp별 fact로 rule when 평가(op/quantifier/combine) → breach
5. breach 장비 → cooldown 배치 체크 → notify 발송
```

> ✅ **dead path 수정 완료**: 과거 엔진은 `resolve_profile(process,"*","*")`로 process 레벨만 해석해 model/eqp override가 알림에 반영되지 않았다. 현재 엔진은 장비별 `resolve_profile(process, model, eqpId)` 로 per-eqp 해석하며, model overlay가 실제 알림에 반영되는지 통합테스트 E7이 회귀 가드한다. ES 집계 실현성·메트릭 커버리지·검증 규칙은 [SCHEMA.md §2·§4·§5·§8](SCHEMA.md).

---

## 3. 분산 조정 (Distributed Coordination)

이 프로젝트의 가장 복잡한 부분. 다음 4개 책임을 ZK 위에서 조립합니다.

### 3.1 멤버십

- 각 인스턴스는 `{root}/members/{instance_id}` 에 **ephemeral** 노드 생성.
- 인스턴스가 죽으면 ZK가 자동으로 노드를 reap → 다른 인스턴스가 ChildrenWatch로 감지.

### 3.2 리더 선출

- `kazoo.recipe.election.Election` 사용.
- 리더가 되면 `{root}/leader-epoch` 의 정수를 +1 후 persist.
- **이 epoch는 stale assignment 거부의 핵심 키**.

### 3.3 파티션 분배 (round-robin)

- 리더가 `EqpInfoRepository.get_distinct_processes()` 로 활성 process 리스트를 가져옴.
- 정렬된 instance 리스트에 round-robin으로 분배.
- 리더가 ZK Transaction으로 모든 인스턴스의 `{root}/assignments/{id}` 에 atomic write:
  ```json
  {
    "processes": ["procA", "procB"],
    "leader_epoch": 7,
    "assigned_at": 1738976543.21
  }
  ```
- 각 인스턴스는 자기 assignment 노드에 DataWatch → 변경 시 scheduler reload.

### 3.4 Stale defense

- `_apply_assignment()` 는 `(epoch, assigned_at)` 튜플 비교.
- 더 큰 epoch면 무조건 적용. 같은 epoch면 timestamp가 더 큰 경우만 적용.
- → 동일 리더가 epoch를 안 올리고 재전송해도 진행 가능. 리더 교체로 epoch가 역행하면 묵살.

### 3.4.1 Redistribute 재시도 + circuit (v6 P0-4)

리더가 `_do_redistribute()` 도중 Mongo/ZK 실패로 죽으면 silent stall 이 발생할 수 있습니다. v6 에서 5회 exponential backoff (`min(30, 2**attempt)`) 후 `redistribute_unhealthy=True` 로 surface → `/healthz/ready` 503. 자세한 내용은 §8.5 참조.

### 3.4.2 Orphan assignment GC (v6 H1)

`MONITOR_INSTANCE_ID` 는 K8s `metadata.name` 에 바인딩되어 있어 rolling update 마다 pod 이름이 바뀝니다. `members/<pod>` 는 ephemeral 이라 세션 종료 시 자동 삭제되지만, `assignments/<pod>` 는 **persistent** 이라 그대로 남습니다. GC 가 없으면 배포 횟수만큼 orphan znode 가 누적 → ZK 스냅샷 비대화.

**해결**: `_do_redistribute()` 성공 경로 말미에 `_cleanup_orphan_assignments(live_instances)` 호출.
- `get_children(_assignments_path)` 결과에서 현재 `instances` 에 없는 이름을 찾아 `kazoo.delete` 로 삭제.
- **Correctness 영향 없음**: 옛 노드가 우연히 남아 있어도 `_apply_assignment` 의 epoch+ts 가드가 stale 적용을 막음. 순수 housekeeping.
- **실패 처리**: `NoNodeError` 는 경쟁 삭제로 간주해 무시, 그 외 예외는 `logger.warning` 만. **절대 `redistribute_unhealthy` 를 올리지 않음** — cleanup 실패가 readiness 를 떨어뜨리면 housekeeping 이 service 를 죽이는 꼴.
- **실패 시점 제한**: transaction 이 성공해야만 cleanup 이 돌아감. 재시도 중에는 cleanup 이 실행되지 않으므로 retry 경로와 충돌 없음.

**회귀 가드**: `tests/unit/test_partition_manager.py::TestOrphanAssignmentCleanup` 7개 케이스 — orphan 선별, no-op, delete/list 실패 시 healthy 유지, redistribute 성공/실패 연동.

### 3.5 ZK 세션 LOST 복구

이 부분이 v4에서 가장 많이 갈아엎힌 영역입니다.

```
KazooState.SUSPENDED  → scheduler.pause_all_jobs()
KazooState.LOST       → 모든 in-memory state 리셋 + scheduler.pause_all_jobs()
KazooState.CONNECTED (after LOST) → _reinit_after_loss()
```

`_reinit_after_loss()` 는 다음을 순서대로:

1. ephemeral member 노드 재생성
2. 자기 assignment 경로 ensure_path
3. 와치 재등록 (`_watch_epoch++` 로 이전 콜백 무력화)
4. ZK에서 현재 assignment 강제 재로드
5. **`LeaderElection.restart_after_loss()` 호출**

5번이 핵심: 옛 `Election` 객체는 죽은 세션에 묶여 있어 재사용 불가. 반드시 새 `Election` 객체를 만들어야 함.

---

## 4. Critical Gotchas / Pitfalls

> v4 계획에서 8개의 critical fix가 있었습니다. **다음 세션에서 동일한 함정에 빠지지 않도록 영구 보존**합니다.

### G1. `motor` `MongoClient.close()` 는 동기 함수

```python
# ❌ AttributeError: __aenter__ 또는 hangs
await self._client.close()

# ✅
self._client.close()
```

`AsyncIOMotorClient.close()` 는 코루틴이 아닙니다. `await` 하면 `TypeError` 또는 hang. `src/db/client.py` 의 `MongoClient.close()` 는 **async 함수지만 내부적으로 sync 호출**합니다.

---

### G2. `kazoo.recipe.lock.Lock` 은 비재진입 + 재사용 금지

```python
# ❌ 같은 객체를 재사용하면 deadlock
self._lock = self._zk.kazoo.Lock(path)
async with self.acquire(): ...   # 첫 호출은 OK
async with self.acquire(): ...   # 두 번째 호출 행

# ✅ 매번 새 Lock 인스턴스
async with self.acquire():
    lock = self._zk.kazoo.Lock(path)
    ...
```

추가로 **per-process `asyncio.Lock`** 으로 같은 프로세스 내 동시 호출을 직렬화해야 합니다 (kazoo Lock은 thread-safe하지 않음).

---

### G3. `LeaderElection.run()` 은 블로킹 함수 — fire-and-forget 필수

```python
# ❌ run()은 콜백이 return할 때까지 안 끝남 → 워커 thread 영구 점거
await loop.run_in_executor(None, election.run, callback)

# ✅ 전용 executor + 미await
self._executor = ThreadPoolExecutor(max_workers=1)
self._election_future = loop.run_in_executor(
    self._executor, election.run, self._on_become_leader_sync
)
```

콜백 자체도 `threading.Event.wait()` 으로 블로킹해야 (return하면 즉시 leadership 반환).

---

### G4. LOST 후에는 새 `Election` 객체를 만들어야 함

```python
# ❌ 옛 Election은 죽은 세션 binding — 영원히 leader 못 됨
election.cancel()
loop.run_in_executor(self._executor, election.run, callback)  # 같은 객체

# ✅
self._stop_event.set()  # 옛 콜백의 wait() 깨움
await drain(self._election_future)
self._election = Election(self._zk.kazoo, path, instance_id)  # 새 객체
self._election_future = loop.run_in_executor(self._executor, ...)
```

`PartitionManager._reinit_after_loss()` 에서 반드시 `await self._leader.restart_after_loss()` 호출.

---

### G5. `DataWatch` 빈 노드 가드

```python
# ❌ ensure_path가 만든 b'' 노드의 첫 콜백에서 폭사
def assignment_cb(data, stat, event):
    payload = json.loads(data.decode())  # JSONDecodeError on b''

# ✅
def _on_assignment_changed_sync(self, data, stat, event):
    if data is None or len(data) == 0:
        return
    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
```

`ensure_path` 직후 watch가 첫 fire를 하므로 이 가드가 없으면 startup 직후 즉시 죽습니다.

---

### G6. 와치 재등록은 epoch counter로 무력화

`kazoo.recipe.watchers.ChildrenWatch` 는 명시적 unregister가 없습니다. `_register_watches()` 가 호출될 때마다:

```python
self._watch_epoch += 1
epoch = self._watch_epoch  # 클로저로 캡처

def members_cb(children):
    if epoch != self._watch_epoch:
        return False  # kazoo가 이 watch 갱신을 멈춤
    self._on_members_changed_sync(children)
```

`return False` 가 핵심: 옛 콜백이 자발적으로 사망 신고.

---

### G7. APScheduler `running` 속성은 `shutdown(wait=False)` 후에도 True

`AnalysisScheduler` 에 자체 `_running` 플래그를 두고 `start()` / `shutdown()` 에서 직접 갱신:

```python
async def start(self):
    self._scheduler.start()
    self._running = True

async def shutdown(self, timeout):
    self._running = False
    self._scheduler.shutdown(wait=False)
    # ... drain self._running_jobs
```

`is_running()` 은 `self._running` 만 보고 판단.

---

### G8. Akka Email API `result` 는 대문자 `"Success"` — case-insensitive 로 비교할 것

Akka `EmailWorker` 는 성공 시 대문자 `"Success"` 를 반환한다 (`SendEmail` /
`SendEmailForRTM` 모두). 과거 코드는 lowercase `"success"` 만 인정해서 **모든
발송이 실패로 기록**됐다 — 메일은 실제로 갔는데 cooldown 이 설정되지 않아
같은 알림이 매 주기 재발송되는 중복 알림 버그가 있었다.

```python
# ❌ 대소문자 엄격 비교 — Akka 의 "Success" 를 놓침
return data.get("result") == "success"

# ✅ case-insensitive — "Success"/"success" 모두 허용
_SUCCESS_RESULT = "success"
result = data.get("result", "")
return isinstance(result, str) and result.lower() == _SUCCESS_RESULT
```

또한 payload 에 `app` 필드가 **필수**다 (Akka `EmailHttpDataFormat` 가
`EMAIL_TEMPLATE`/`EMAIL_CATEGORY` 조회 키로 사용 — 누락 시 json4s `extract`
예외로 알림이 서버 측에서 조용히 폐기됨). `settings.email_app_name` (기본 `"ARS"`)
에서 채운다.

`tests/unit/test_email_client.py::test_returns_true_on_capital_success` 가 회귀 가드.

---

### G9. (보너스) `pydantic-settings` `list[str]` 자동 JSON 디코드

`MONITOR_ES_HOSTS=http://a:9200,http://b:9200` 같은 콤마 문자열을 받으려면 `Annotated[list[str], NoDecode]` 로 자동 JSON 파싱을 끄고 `field_validator(mode="before")` 로 직접 처리해야 합니다.

```python
from pydantic_settings import NoDecode

class AppSettings(BaseSettings):
    es_hosts: Annotated[list[str], NoDecode] = ["http://es-cluster:9200"]

    @field_validator("es_hosts", mode="before")
    @classmethod
    def parse_es_hosts(cls, v):
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v
```

---

### G10. (보너스) Pydantic v2는 `model_*` 식별자 예약

`Scope` 모델의 `model` 필드는 `eqp_model: str = Field(alias="model")` 로 우회. Mongo 직렬화 시 `to_mongo_query()` 가 alias로 변환.

---

## 5. 라이브러리 버전 고정 이유

| 라이브러리 | 핀 | 이유 |
|-----------|----|----|
| `elasticsearch[async]>=7.11.0,<8.0.0` | 7.x | 운영 ES가 7.11.9. 8.x는 `http_auth`/`timeout`/`body=` API가 모두 다름. raw dict 응답도 wrapped object로 변경. |
| `kazoo>=2.9.0,<2.11.0` | 2.9~2.10 | 2.11에서 watch 동작 변경. ZK 3.5.5와 검증된 조합. |
| `redis[hiredis]>=4.5.0,<5.1.0` | 4.x~5.0 | 5.1+에서 ACL/RESP3 default가 변하면서 Redis 5.0.6 서버와 incompat 가능. `protocol=2` 명시로 회피. |
| `apscheduler>=3.10.0,<4.0.0` | 3.x | 4.x는 API 전면 변경. AsyncIOScheduler 동작이 다름. |
| `motor>=3.3.0` | 3.x | PyMongo 4.x와 호환. `close()` sync 동작은 3.x 전체 동일. |
| `cachetools>=5.3.0` | 5.3+ | TTLCache 정확한 만료 동작. |
| `pydantic>=2.0.0` + `pydantic-settings>=2.0.0` | 2.x | `ConfigDict`, `field_validator`, `NoDecode`. |
| Python | `>=3.11` | `Annotated`, structural pattern matching, 더 빠른 asyncio. 3.14에서 검증됨. |

---

## 6. 인프라 ↔ 코드 매핑

| 인프라 | 코드 위치 | 설정 키 |
|--------|----------|---------|
| Elasticsearch 7.11.9 | `src/es/client.py` | `MONITOR_ES_HOSTS`, `MONITOR_ES_USERNAME`, `MONITOR_ES_PASSWORD` |
| MongoDB (EARS DB) | `src/db/client.py` | `MONITOR_MONGO_URI`, `MONITOR_MONGO_DB` (컬렉션 스키마는 [SCHEMA.md](SCHEMA.md)) |
| Zookeeper 3.5.5 | `src/distributed/zk_client.py` | `MONITOR_ZK_HOSTS`, `MONITOR_ZK_ROOT_PATH`, `MONITOR_ZK_SESSION_TIMEOUT`, `MONITOR_ZK_SASL_*` |
| Redis 5.0.6 | `src/cache/redis_client.py` | `MONITOR_REDIS_URL`, `MONITOR_REDIS_PASSWORD`, `MONITOR_REDIS_KEY_PREFIX` |
| Akka HttpWebServer | `src/alert/email_client.py` | `MONITOR_EMAIL_API_URL`, `MONITOR_EMAIL_API_TIMEOUT` |
| Grafana (link only) | (alert body 빌드 시) | `MONITOR_GRAFANA_BASE_URL`, `MONITOR_GRAFANA_DASHBOARD_UID` |

---

## 7. 캐시/타임아웃/동시성 상수

`src/config/constants.py` 에 모여 있는 튜닝 파라미터:

| 상수 | 값 | 의미 |
|------|----|----|
| `PROFILE_CACHE_MAX_SIZE` | 10000 | profile resolve 캐시 (process×model×eqp 키) |
| `PROFILE_CACHE_TTL_SEC` | 300 | 5분 |
| `COOLDOWN_LOCAL_CACHE_MAX_SIZE` | 50000 | Redis 다운 시 fallback 한도 (메모리 포로 방지) |
| `COOLDOWN_LOCAL_CACHE_MAX_TTL_SEC` | 3600 | 1시간 (TTLCache 자체 상한) |
| `ES_QUERY_SEMAPHORE` | 3 | per-instance 동시 ES 쿼리 수 |
| `REDISTRIBUTE_DEBOUNCE_SEC` | 2.0 | 멤버십 변화 폭탄 대비 |

---

## 8. 메트릭 / 관측

| 메트릭 | 타입 | 라벨 | 용도 |
|--------|-----|-----|----|
| `resource_monitor_job_total` | counter | `process`, `status`, `reason` | 분석 job 실행 카운트. `reason` 은 failure 시 `mongo_unavailable` / `es_unavailable` / `lock_timeout` / `other` 중 하나, success/skip 시 `""` (v6 P1-2) |
| `resource_monitor_job_duration_seconds` | histogram | `process`, `metric_category` | job wall-clock |
| `resource_monitor_es_query_duration_seconds` | histogram | `process` | ES 응답 시간 |
| `resource_monitor_alerts_sent_total` | counter | `code`, `subcode` | 발송된 알림 수 |
| `resource_monitor_zk_leader` | gauge | — | 리더 1, 아니면 0 |
| `resource_monitor_assigned_processes` | gauge | — | 현재 인스턴스가 맡은 process 수 |
| **`resource_monitor_infra_up`** | gauge | `infra` (5 values) | 5개 인프라(`elasticsearch`, `mongodb`, `redis`, `email_api`, `zookeeper`) 각각의 reachability. `/healthz/ready` 가 호출될 때 갱신. **인프라 추가/제거 시 `INFRA_LABELS` 와 `readiness()` 둘 다 업데이트** (v6 P0-5) |
| **`resource_monitor_startup_complete`** | gauge | — | lifespan yield 후 1, 그 외 0. 부팅 시간 wall-clock 측정용 (v6 P0-5) |

`JOB_TOTAL` 의 `reason` 라벨 추가는 dashboard breaking change였습니다. 대시보드 쿼리는 반드시 `sum by (status, reason)` 형태로 수정.

로그는 structlog JSON. `RequestIdMiddleware` 가 X-Request-ID를 contextvars에 바인딩 → 모든 후속 로그에 포함.

---

## 8.5 Failure Modes (v6)

5개 인프라(ES / Mongo / Redis / Email / ZK) 의 startup 및 runtime 실패 시 동작이 일관되도록 v6 에서 정비했습니다. 운영자가 "지금 이 pod 어디서 막혔지?" 를 빠르게 답할 수 있는 것이 목적입니다.

### Startup behavior (모두 fail-fast)

| 인프라 | Retry 정책 | 최악 시간 | 실패 시 | v6 변경 |
|-------|----------|----------|--------|--------|
| **Elasticsearch** | `connect()` 끝에 `ping()` 1회 | 즉시 | `RuntimeError("es_startup_ping_failed")` → `init_infra` rollback | P0-3: ping 추가. 이전엔 silent pass — 잘못된 host도 boot 통과 |
| **MongoDB** | `connect_with_retry(max_attempts=5, backoff=2)` 선형 | ~30s | last exception 그대로 raise | 변경 없음 (기존 동작 유지) |
| **Redis** | `connect_with_retry(max_attempts=3, backoff=1)` 선형 | ~6s | last exception | P0-2: retry 횟수 0→3. Mongo 와 대칭 |
| **Email** | `connect()` 끝에 `health_check()` (HEAD) 1회. **debug 모드 skip** | 즉시 | `RuntimeError("email_startup_health_check_failed")` | P0-3: health_check 추가 |
| **Zookeeper** | kazoo 내부 `KazooRetry(max_tries=-1)` + **외부 `asyncio.wait_for(timeout=zk_startup_budget_sec)`** | **45s 상한** | `TimeoutError("zk_startup_budget_exceeded")` | P0-1: 무한 hang 제거. 이전엔 lifespan yield 도달 못해 `/healthz/live` unreachable → CrashLoopBackoff (dead zone) |

**불변식 (test_k8s_probe_invariants.py 가 강제):**
> `livenessProbe.initialDelaySeconds (60s)` ≥ `zk_startup_budget_sec (45s)` + 10s 안전 마진.
>
> 둘 중 하나만 바꾸면 dead zone 이 다시 등장합니다. K8s manifest 또는 settings 를 수정하면 invariant test 가 빨갛게 잡습니다.

### Runtime behavior

| 인프라 | Readiness 영향 | Scheduler 영향 | Fallback / Self-heal |
|-------|---------------|---------------|---------------------|
| **Elasticsearch** | `infra_up{es}=0`, ready=503 | 계속 동작 (`JOB_TOTAL{reason="es_unavailable"}` 증가) | introspect 캐시는 negative TTL 5분 후 자동 재시도 (P1-5). 새 인덱스 발견까지 pod 재시작 불필요 |
| **MongoDB** | `infra_up{mongo}=0`, ready=503 | 계속 동작 | repository boundary 에서 `MongoUnavailableError` 로 변환되어 `_job_wrapper` 가 reason 라벨링 (P1-1, P1-2) |
| **Redis** | `infra_up{redis}=0`, ready=503 | 계속 동작 | `AlertCooldownManager` 의 in-memory `TTLCache` fallback. **이메일 폭주 방지** (기존 동작) |
| **Email** | `infra_up{email_api}=0`, ready=503 | 계속 동작 | 모든 실패 send 가 bounded `deque(maxlen=1000)` 에 기록. `GET /admin/email-outbox` 로 인스펙션 (P1-3). debug 모드는 outbox 오염 방지를 위해 skip |
| **Zookeeper** | `infra_up{zk}=0`, ready=503 | **PAUSE** (`SUSPENDED` / `LOST` 상태로 들어가면 즉시 일시중지) | LOST → CONNECTED 시 `_reinit_after_loss()` (G3.5) + `LeaderElection.restart_after_loss()` |

### Leader redistribute 회로 차단 (P0-4)

리더가 `_do_redistribute()` 도중 Mongo 가 죽거나 ZK transaction 이 깨지면, 이전 (v5) 에는 listener 콜백이 silent crash → 리더는 그대로지만 assignment 가 stale 한 채로 멈췄습니다. v6 에서:

1. 모든 예외 경로는 `_redistribute_attempt += 1` + 다음 실행 예약 (`min(30, 2**attempt)` backoff)
2. 5회 누적 실패 시 `redistribute_unhealthy = True`
3. `/healthz/ready` 가 이 플래그를 보고 503 반환 → K8s 트래픽 차단 + Prometheus alert
4. 다음 성공 시 attempt/flag 모두 reset

```python
# src/distributed/partition_manager.py
async def _do_redistribute(self, instances):
    try:
        ...  # 본체
        self._redistribute_attempt = 0
        self._redistribute_unhealthy = False
    except Exception as e:
        attempt = self._redistribute_attempt + 1
        ...
        if attempt < self._REDISTRIBUTE_MAX_ATTEMPTS:
            self._redistribute_retry_task = asyncio.create_task(
                self._retry_redistribute(instances, attempt)
            )
        else:
            self._redistribute_unhealthy = True
```

> Bright-line: `_do_redistribute` 의 모든 예외 경로는 retry 를 스케줄하거나 `redistribute_unhealthy=True` 로 설정해야 합니다. silent stall 금지.

### 회귀 가드

- `tests/integration/test_startup_failure_modes.py` — 5 시나리오 (`docker stop` 으로 ZK/Mongo/Redis 죽임 + ES/Email 잘못된 URL). 핵심은 **`test_zk_down_at_boot_fails_within_budget`** — dead zone 회귀 가드.
- `tests/unit/test_k8s_probe_invariants.py` — 위 timing invariant.
- `tests/unit/test_partition_manager.py::TestRedistributeRetry` — retry path + unhealthy flag.
- `tests/unit/test_es_client.py::test_retries_after_negative_ttl_expires` — introspect TTL.
- `tests/unit/test_email_client.py::TestEmailOutbox` — outbox 7 케이스.
- `tests/unit/test_health.py` — `redistribute_unhealthy` 가 503 으로 surface 되는지.

---

## 9. Debug Read-Only 모드

`MONITOR_DEBUG_READ_ONLY=true` 는 production 데이터에 대한 **관찰자 모드** 부팅입니다. 설계 동기: 개발자가 실수로 prod 에 쓰기를 하지 못하도록 구조적으로 차단 + Phase 1+ 분석 코드를 실제 prod 데이터에 대해 관찰 가능하게 함.

### 9.1 차단되는 경로 (전수)

| 위치 | 변경 |
|------|------|
| `src/startup/infra.py` | `ZKClient.connect()` 스킵 → `infra.zk = None` |
| `src/startup/repos.py` | `create_collection(COLL_PROFILE)` + `create_index(uniq_scope)` 스킵 (`scripts/create-profile-collection.ps1` 로 수동) |
| `src/main.py` lifespan | `init_distributed`, `partition_manager.start`, `leader_election.start` 스킵 (startup 자동 seed 없음 — 프로파일은 JSON 수동 입력) |
| `src/cache/cooldown.py` | `set_cooldown` / `clear_cooldown` / `is_cooling_down[_batch]` Redis 우회 — local TTLCache 만 사용 |
| `src/alert/email_client.py` | `send_alert` HTTP POST 차단 → `debug_would_send_email` 로그 + `True` 반환 |

### 9.2 유지되는 경로

- ES / Mongo 읽기
- `AnalysisScheduler.start()` — 분석 흐름 관찰 가능. Phase 1+ 에서는 `resolve_processes_for_debug()` 가 반환한 process 에 대해 job 등록
- `/healthz/live`, `/healthz/ready`, `/metrics`, `/admin/status` — 엔드포인트 전부 응답. `/healthz/ready` 에 `debug_read_only: true` + `checks.zookeeper: "skipped_debug"`, `/admin/status` 에 분산 필드 `null`

### 9.3 왜 이 구조인가

- **ZK 는 연결조차 안 함** (읽기 전용 세션도 허용 안 함): prod ZK `stat`/`mntr` 에 debug 클라이언트가 노출되어 운영팀 오탐 유발 가능 + state listener 경로에 실수로 write 가 섞이면 방어선이 무너짐. 단순히 "만지지 않는다" 가 안전
- **MongoDB runtime 쓰기는 원래 0건** (SCHEMA.md §0.3 — RMS 는 `RESOURCE_MONITOR_PROFILE` 만 소유, 분석 경로는 read-only): 분석 job 은 Mongo 를 읽기만 함. 그래서 Mongo 쪽 가드는 startup 2곳(`create_index`, `seed`) 만으로 충분
- **Scheduler 는 기동**: 없으면 "데이터만 읽는 REPL" 과 다를 바 없음 → 디버깅 가치 없음. 실제 분석 로직이 prod 데이터에 어떻게 반응하는지 보는 것이 debug 모드의 목적

### 9.4 금지 사항

- **Production K8s manifests 에 `MONITOR_DEBUG_READ_ONLY` 금지.** `deployment.yaml`, `configmap.yaml`, `secret.yaml` 어디에도 넣지 않음. 잘못 활성화되면 분석 스케줄러가 partition 을 가질 수 없어 **감지 공백** 발생
- 장애 대응 중 production pod 에서 임시로 켜지 말 것 — "잠깐 써보고 싶다" 는 유혹이 감지 공백을 만든다

### 9.5 회귀 가드

- Unit: `test_settings.py::TestDebugReadOnly`, `test_startup.py::{TestInitInfra,TestInitRepos}` 각 debug 테스트, `test_lock.py::TestNoOpZKLock`, `test_cooldown.py::TestDebugReadOnlyGuard`, `test_email_client.py::TestDebugReadOnlyGuard`, `test_scheduler_jobs.py::TestDebugProcessesResolution`
- Integration: `test_lifespan_real.py::test_debug_*` (6 tests — boot, state, no-index, /healthz/ready, /admin/status)

자세한 운영 가이드는 [CONTRIBUTING.md §8.4](CONTRIBUTING.md) 참고.

---

## 10. 향후 작업 (Phase 0 잔여 + Phase 1+)

| Step | 영역 | 상태 |
|------|------|------|
| 9 | Dockerfile (non-root, healthcheck, securityContext) + K8s manifests (Deployment, PDB, Secret 분리) | done |
| 10 | Integration tests (OrbStack 기반) + debug 모드 lifespan 테스트 | done |
| Phase 1 | 임계값 분석 로직 + 실제 알림 발송 + `AnalysisScheduler.reload()` 에서 `resolve_processes_for_debug()` 연결 | **done** (2026-04-12) |
| Phase 1.1 | Akka `/EmailNotify` case-insensitive `"Success"` + `app` 필수 필드 (G8) + 분석→알림 통합 E2E (`tests/integration/test_phase1_analysis_e2e.py`) | **done** (2026-06-02) |
| Phase 2+ | 통계/패턴 기반 이상탐지 (`duration`/`zscore`/`trend`/... fact 엔진 구현; 스키마·검증은 이미 수용) | 미착수 |
| **v2 스키마** | 기준정보 단일 컬렉션 measures/rules/notify + cascade 상속 ([SCHEMA.md](SCHEMA.md), [§2.2](#22-v2-데이터-모델--measuresrulesnotify)) | **done** |
| **v2 #0** | 엔진 per-eqp effective 해석 (구 process 레벨 dead path 수정) | **done** (통합테스트 E7 가드) |
| v2 후속 | 프로파일 관리 UI (CRUD API는 `src/api/profiles.py` 구현 완료) | 미착수 |

---

## 11. 참고

- Phase 0 v6 구현 계획 (완료 보관): [docs/archive/phase0-plan-v6.md](docs/archive/phase0-plan-v6.md)
- PRD: [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md)
- DB 스키마 레퍼런스 (**v2 권위 스펙**): [SCHEMA.md](SCHEMA.md)
- 관리 UI / 시인성 설계: [docs/ADMIN-UI-LEGIBILITY.md](docs/ADMIN-UI-LEGIBILITY.md)
- 코드 디렉토리 맵 / 빠른 시작: [README.md](README.md)
- 개발 워크플로우: [CONTRIBUTING.md](CONTRIBUTING.md)
- 상위 ARS 통합 설계: `/Users/hyunkyungmin/Developer/ARS/.claude/PLANNING.md`
