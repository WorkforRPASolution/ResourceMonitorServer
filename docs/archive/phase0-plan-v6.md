# ResourceMonitorServer Phase 0 구현 계획 (v6)

## Context

공장 내 PC(최대 20,000대)의 리소스 메트릭을 자동 이상탐지 + 알림하는 모니터링 서비스의 **기반 골격**을 구축한다. Phase 0은 분석 로직 없이 프로젝트 구조, 인프라 연동, 스키마, 스케줄러 골격만 만든다.

- **PRD**: `/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/PRD_Phase0_Foundation.md`
- **그린필드**: PRD만 존재, 코드 없음
- **참조 패턴**: `HttpWebServerTemp/app/` (FastAPI+Pydantic+Motor)
- **EARS DB 스키마**: `/Users/hyunkyungmin/Developer/ARS/WebManager/docs/SCHEMA.md`
- **EARS 메트릭 레퍼런스**: `/Users/hyunkyungmin/Developer/ARS/ResourceAgent/docs/EARS-METRICS-REFERENCE.md`

### 운영 인프라 버전 (확정)
| 인프라 | 버전 | 상태 | 주요 제약 |
|--------|------|------|----------|
| **Elasticsearch** | **7.11.9** | EOL | Kibana 7.11.9 쌍. 8.x API와 다름 (`body=`, response dict, timeout 파라미터) |
| **Redis** | **5.0.6** | EOL | ACL 없음(단순 AUTH), RESP3 미지원, GETEX/GETDEL 없음 |
| **Zookeeper** | **3.5.5** | EOL | LOST 후 watches 자동 재등록 안됨, TTL 노드 없음, container 노드 가능(3.5.3+), 4lw whitelist 기본 차단 |
| **MongoDB** | 확인 필요 | - | EARS DB 공유. 기존 컬렉션 7개(EQP_INFO 등), RESOURCE_MONITOR_* 신규 생성 충돌 없음 |

### EARS DB 기존 컬렉션 (SCHEMA.md 확인)
`EQP_INFO, ARS_USER_INFO, EMAIL_TEMPLATE_REPOSITORY, POPUP_TEMPLATE_REPOSITORY, EMAILINFO, EMAIL_RECIPIENTS, EMAIL_IMAGE_REPOSITORY`
→ `RESOURCE_MONITOR_PROFILE`, `RESOURCE_MONITOR_RULE` 신규 생성은 **충돌 없음**.

### EQP_INFO 핵심 필드 (스코프 해석용)
`eqpId (PK), line, process, eqpModel, category, ipAddr, localpc, onoff, webmanagerUse`
- Scope.process → EQP_INFO.process (1:1)
- Scope.model → **EQP_INFO.eqpModel** (필드명 매핑 주의)
- Scope.eqpId → EQP_INFO.eqpId
- `get_distinct_processes()` 쿼리 시 `onoff: 1, webmanagerUse: 1` 활성 필터 적용

### Email API 응답 포맷 (실제 코드 확인)
```scala
case class HttpResponse(result: String, message: String)
// 성공: {"result": "success", "message": "send ok"}  — 소문자 "success"!
// 실패: {"result": "fail",    "message": "..."}
```

### 검증 이력
- 1차 교차 검증 (Python/FastAPI, 분산, ES, MongoDB) → v2 반영
- 2차 재검증 (동일 4분야 + SRE/운영 + 테스팅/QA) → v3 반영
- 3차 버전 제약 검토 (Redis 5.0.6 / ZK 3.5.5) → v3 반영
- **4차 운영 확정 정보 반영** (ES 7.11.9, EQP_INFO 스키마, Email API 응답 포맷, EARS 메트릭 레퍼런스) → v4 반영
- **5차 — Step 0~8 구현 완료, Step 9/10 상세화 (2026-04-07)** → v5 반영
- **6차 — 인프라 실패 처리 전문가 패널 검토 (2026-04-08)** → v6 반영. Step 8.5 (Resilience Hardening) 신규 삽입

---

## Implementation Status (2026-04-07)

| Step | 영역 | 상태 | 비고 |
|------|------|------|------|
| 0 | 스켈레톤 + pyproject + Makefile | done | Python 3.14.2 venv |
| 1 | settings + structlog | done | `Annotated[..., NoDecode]` 적용 |
| 2 | ES 7.11 client + queries | done | http_auth/timeout 검증 |
| 3 | MongoDB + repository + seed | done | onoff 필터, alias 매핑 |
| 4 | Redis + cooldown | done | local fallback (TTLCache) |
| 5 | Email client | done | "success" 소문자 회귀 가드 |
| 6 | ZK + Lock + LeaderElection + PartitionManager | done | restart_after_loss + 8 critical fix |
| 7 | Health + Admin + Metrics + Scheduler | done | /healthz/{live,ready} 분리 |
| 8 | startup/ + main + middleware | done | 11-phase lifespan |
| 9 | Dockerfile + K8s + dev infra bootstrap | **pending** | 아래 v5 상세 플랜 |
| 10 | 통합/E2E 테스트 (OrbStack 기반) | **pending** | 아래 v5 상세 플랜 |

**문서**: README.md, ARCHITECTURE.md, CONTRIBUTING.md 작성 완료. 8개 critical fix는 ARCHITECTURE.md G1~G10 섹션에 영구 박제.

**테스트 현황**: unit tests 202개 통과, <5초.

**venv**: `/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/.venv/bin/python` (Python 3.14.2).

---

## Step 8.5 — Resilience Hardening (v6, 2026-04-08)

### Context

Step 9(K8s 배포) + Step 10(통합 테스트)에 들어가기 전, **5개 인프라의 연결 실패 처리**가 일관성 없이 짜여 있어 운영 리스크가 확인됨. 4명의 전문가(SRE, 분산시스템, Python backend, QA) 패널 검토 결과 11개 개선 항목(P0 6개 + P1 5개)이 도출되었음. 이 섹션은 Step 9/10 수행 전에 해결해야 할 기반 보강이다.

**Trigger**: 사용자 질문 — "각 인프라에 대해 초기 시작과 런타임 중 연결 장애가 발생했을 때 어떻게 동작해야 맞는지 전문가 여럿 검토".

### 현재 동작 (탐색으로 확정된 사실)

#### 시작 시 실패 동작

| 인프라 | `connect()` ping? | Retry | Worst-case | 실패 시 |
|-------|----------------|------|-----------|--------|
| **ES** | ❌ (`src/es/client.py:43-58` — 객체만 생성) | 없음 | 0초 | **Silent 통과** |
| **Mongo** | ✅ (`src/db/client.py:51`) | 5회, 선형 2/4/6/8/10s | ~30초 | 예외 raise |
| **Redis** | ✅ (`src/cache/redis_client.py:42`) | **0회 (1 attempt)** | ~5초 | 예외 raise |
| **Email** | ❌ (`src/alert/email_client.py:33-34`) | 없음 | 0초 | **Silent 통과** |
| **ZK** | ✅ (`src/distributed/zk_client.py:88`) | **`KazooRetry(max_tries=-1)` 무한** | **∞** | **영원히 hang** |

#### 치명 결함: ZK Dead Zone (CrashLoopBackoff)

`init_infra()`가 `kazoo.start`에 무한 대기 → lifespan yield 못함 → Starlette가 요청 버퍼링 → `/healthz/live` 도달 불가 → 60초 후 K8s liveness kill → pod 재기동 → **영원한 CrashLoopBackoff**. 단, 이 동안 운영자에게 전달되는 signal(로그, metric)은 **최소한**.

#### 런타임 실패 동작

| 인프라 | Readiness | Scheduler | Fallback | Silent 실패 위험 |
|-------|-----------|-----------|----------|----------------|
| **ES** | 503 | 계속 동작 | introspect cache→"unknown" 영구 | Job failure counter↑ 만 |
| **Mongo** | 503 | 계속 동작 | **없음** | repository에 예외 핸들러 전무. Leader `_do_redistribute` 실패 시 election 스레드 crash (로그만) |
| **Redis** | 503 | 계속 동작 | ✅ **`TTLCache` local fallback** (`cooldown.py:49`) | 유일한 우수 사례 |
| **Email** | 503 | 계속 동작 | **없음** | **알림 소실**, DLQ 없음 |
| **ZK** | 503 | **PAUSE** (유일한 adaptive 반응) | LOST→reinit→resume | reinit 실패 시 leader silent stall |

#### 확인된 gap 11개 (원인 → 결과)

1. **ES/Email startup silent pass** — config typo를 boot 시 감지 불가
2. **Redis startup retry=0** (Mongo 5회와 비대칭) — boot order 레이스 예민
3. **ZK infinite retry** — CrashLoopBackoff
4. **Email 실패 silent drop** — alert 영구 소실
5. **Mongo repository 예외 핸들러 전무** — raw PyMongo 예외가 `_job_wrapper`까지 누출
6. **Leader `_do_redistribute` Mongo 실패** — election callback silent crash, 리더십은 유지되나 assignment 미갱신
7. **ES introspect cache 영구 "unknown"** — 서비스 기동 후 만들어진 인덱스는 pod 재시작까지 감지 불가
8. **infra health gauge 부재** — Prometheus로는 `job_total` counter만 알 수 있음
9. **ZK-down-at-startup 테스트 부재** — dead zone 회귀 가드 없음
10. **K8s probe timing invariant 미검증** — liveness vs startup race 수학 검증 없음
11. **circuit breaker 없음** — 실패한 infra에 매 주기 전량 재시도

---

### 전문가 패널 결정 사항 (10개)

각 결정은 4명 전문가(SRE / 분산시스템 / Python / QA) 중 과반수 동의 필요. 모두 합의됨.

| # | 결정 | 선택 | 근거 |
|---|------|------|------|
| 1 | Startup 철학 | **Fail-fast 일관 적용** | 단일 pod에서 "degraded boot"는 의미 없음. K8s가 loop+alert 담당 |
| 2 | ZK startup retry | **45초 시간 제한** | livenessProbe.initialDelaySeconds=60 - 15초 여유. kazoo 내부 retry는 유지, 외부 `asyncio.wait_for` 로 cap |
| 3 | ES/Email ping | **`connect()` 끝에 추가** | 이미 존재하는 `ping()`/`health_check()` 재사용. bad config를 boot 시점에 감지 |
| 4 | Redis retry | **3회 linear (1/2/3s, 총 ~6초)** | Mongo의 5회와 대칭. Redis는 더 빠른 복구 기대 → 단축 |
| 5 | 런타임 scheduler pause | **ES/Mongo/Email은 pause 안 함** | readiness 503 만으로 충분. pause는 ZK(consistency)에만 의미 |
| 6 | Email DLQ | **in-memory `deque(maxlen=1000)` + admin endpoint** | 영구 outbox는 Phase 1+. Phase 0는 가시성만 |
| 7 | Circuit breaker | **Phase 0에는 도입 안 함** | APScheduler `coalesce=True`가 자연스러운 throttle. Analyzer 들어온 후 재평가 |
| 8 | Mongo 예외 변환 | **Repository boundary에서 `MongoUnavailableError` 로 변환** | 호출자(job vs election)가 다르게 반응 가능 |
| 9 | Leader `_do_redistribute` Mongo 실패 | **재시도 (5회 exponential) + 최종 실패 시 `redistribute_unhealthy=True`** | silent stall 방지. 실패가 readiness 503 으로 surface |
| 10 | infra health 메트릭 | **`resource_monitor_infra_up{infra=...}` Gauge 추가** | readiness 및 startup ping 에서 업데이트 |

---

### P0 (Step 9/10 전 필수) — 6개

#### P0-1. ZK startup time-bound (45초)

**파일**:
- `src/distributed/zk_client.py` — `ZKClient.__init__` / `ZKClient.connect`
- `src/config/settings.py` — 신규 필드 `zk_startup_budget_sec: int = 45`

**패턴**:
- `__init__`: `self._start_executor: ThreadPoolExecutor | None = None`
- `connect()`:
  - 전용 단일 스레드 executor 생성 (`zk-startup` prefix)
  - `future = self._start_executor.submit(self._kazoo.start)` → `asyncio.wait_for(asyncio.wrap_future(future), timeout=settings.zk_startup_budget_sec)`
  - `TimeoutError` 발생 시: `logger.error("zk_startup_timeout", elapsed_sec=...)` → `self._start_executor.shutdown(wait=False)` (daemon 스레드 leak 허용) → `raise TimeoutError("zk_startup_budget_exceeded")`
  - 성공 시에도 `start_executor`는 `close()`에서 정리
- **내부 `KazooRetry(max_tries=-1, delay=1, backoff=2, max_delay=5)` 는 그대로 유지** — kazoo 내부 재시도는 빠르므로 무방. 바깥의 `wait_for`가 cap 역할

**테스트**:
- `tests/unit/test_zk_client.py::test_connect_raises_timeout_after_budget` — `kazoo.start` 를 `time.sleep(60)` 로 mock, `zk_startup_budget_sec=2` 로 설정, 3초 이내 raise 확인
- `tests/integration/test_startup_failure_modes.py::test_zk_down_at_boot_fails_within_budget` — `docker stop ars-zookeeper` fixture, `LifespanManager(startup_timeout=60)`, `TimeoutError` 확인, wall-clock ≤ 50초

**문서**: ARCHITECTURE.md §Failure Modes 신설 또는 기존 §9에 편입. "ZK startup budget = 45s < liveness initialDelay 60s" 불변식 명시

**회귀 가드**: 신규 unit test + invariant test(P1-4)

---

#### P0-2. Redis startup retry (3회)

**파일**:
- `src/cache/redis_client.py` — 신규 `connect_with_retry(max_attempts=3, backoff=1.0)`
- `src/startup/infra.py` — `init_infra` 에서 `redis.connect_with_retry()` 호출로 변경

**패턴**: `MongoClient.connect_with_retry` (`src/db/client.py:35-67`) 와 동일 구조. 3회, linear backoff 1s/2s/3s, 총 worst-case ~6s. 실패 시 `logger.warning("redis_connect_retry", attempt, max_attempts, error)`.

**테스트**:
- `tests/unit/test_redis_client.py::test_connect_with_retry_succeeds_on_second_attempt` — `Redis.from_url` monkeypatch로 1회 실패 후 성공
- `tests/unit/test_redis_client.py::test_connect_with_retry_exhausts` — 3회 모두 실패 시 예외 raise
- `tests/integration/test_startup_failure_modes.py::test_redis_down_at_boot` — `docker stop ars-redis`, ~10초 이내 실패

**문서**: ARCHITECTURE.md §Failure Modes Redis 행 업데이트

**회귀 가드**: unit test + `connect_with_retry(3, 1.0)` 기본값 assertion

---

#### P0-3. ES + Email startup ping

**파일**:
- `src/es/client.py` — `ESClient.connect()` 끝
- `src/alert/email_client.py` — `EmailAlertClient.connect()` 끝

**패턴**:
- ES: `if not await self.ping(): raise RuntimeError("es_startup_ping_failed")` — 기존 `ping()` 재사용 (`src/es/client.py:60-68`)
- Email: `if not await self.health_check(): raise RuntimeError("email_startup_health_check_failed")` — 기존 `health_check()` 재사용 (`src/alert/email_client.py:101-116`)
- 둘 다 raise 된 예외는 `init_infra`의 기존 `except → close_partial → raise` 경로 (`src/startup/infra.py:129-131`)로 흐름

**테스트**:
- `tests/unit/test_es_client.py::test_connect_pings_and_raises_on_failure` — `ping()` 이 False 반환하도록 mock, `connect()` 가 `RuntimeError` raise 확인
- `tests/unit/test_email_client.py::test_connect_health_checks_and_raises_on_failure` — 동일 패턴
- `tests/integration/test_startup_failure_modes.py::test_es_bad_host_fails_at_boot` — `MONITOR_ES_HOSTS=http://localhost:59999`
- `tests/integration/test_startup_failure_modes.py::test_email_bad_url_fails_at_boot` — `MONITOR_EMAIL_API_URL=http://localhost:59998/EmailNotify`

**문서**: ARCHITECTURE.md §Failure Modes ES/Email 행 "silent pass → ping at connect"

**회귀 가드**: unit test 4개

---

#### P0-4. Leader `_do_redistribute()` 재시도 + 회로 차단

**파일**: `src/distributed/partition_manager.py`

**영향**:
- `PartitionManager.__init__` — 신규 필드 `self._redistribute_unhealthy: bool = False`, `self._redistribute_retry_task: asyncio.Task | None = None`
- `PartitionManager._do_redistribute` — 예외 처리 래퍼 추가
- `PartitionManager._retry_redistribute` — 신규 (backoff + 재시도)

**패턴**:
```
async def _do_redistribute(self, instances):
    try:
        # 기존 본체
        ...
        self._redistribute_unhealthy = False  # 성공 시 clear
    except Exception as e:
        attempt = getattr(self, "_redistribute_attempt", 0) + 1
        self._redistribute_attempt = attempt
        logger.error("redistribute_failed_retrying",
                     attempt=attempt, instances=instances, error=str(e))
        if attempt < 5:
            # 기존 _redistribution_task 와 별도의 retry task
            if self._redistribute_retry_task and not self._redistribute_retry_task.done():
                self._redistribute_retry_task.cancel()
            self._redistribute_retry_task = asyncio.create_task(
                self._retry_redistribute(instances, attempt)
            )
        else:
            logger.error("redistribute_giving_up", attempts=attempt)
            self._redistribute_unhealthy = True

async def _retry_redistribute(self, instances, attempt):
    delay = min(30, 2 ** attempt)
    try:
        await asyncio.sleep(delay)
        if self._leader.is_leader():
            await self._do_redistribute(instances)
    except asyncio.CancelledError:
        pass
```

- `src/api/health.py::readiness` 에서 `pm.redistribute_unhealthy` 가 True 면 readiness 503 에 해당 체크 포함
- 성공 시 `self._redistribute_attempt = 0` 으로 리셋

**테스트**:
- `tests/unit/test_partition_manager.py::test_redistribute_mongo_failure_retries_and_recovers` — `_eqp_repo.get_distinct_processes` 를 2회 실패 후 성공으로 mock, 3회 attempt 확인, leader crash 없음
- `tests/unit/test_partition_manager.py::test_redistribute_persistent_failure_flags_unhealthy` — 항상 실패 시 5회 attempt 후 `redistribute_unhealthy=True` 확인
- `tests/unit/test_health.py::test_readiness_503_when_redistribute_unhealthy`

**문서**: ARCHITECTURE.md §Distributed Coordination — "leader redistribution retry policy (5회 exponential, 최종 실패 시 readiness 503)"

**회귀 가드**: `_do_redistribute` 본체 상단에 bright-line 주석: "모든 예외 경로는 retry를 스케줄하거나 `redistribute_unhealthy=True` 로 설정해야 함. silent stall 금지."

---

#### P0-5. infra health Gauge 메트릭

**파일**:
- `src/api/metrics.py` — 신규 Gauge 2개
- `src/api/health.py` — `readiness()` 에서 업데이트
- `src/main.py` — `STARTUP_COMPLETE` lifespan hook

**패턴**:
```python
# src/api/metrics.py
INFRA_UP = Gauge(
    "resource_monitor_infra_up",
    "1 if infra is reachable, 0 otherwise",
    ["infra"],  # values: elasticsearch, mongodb, redis, email_api, zookeeper
)
STARTUP_COMPLETE = Gauge(
    "resource_monitor_startup_complete",
    "1 after lifespan yields",
)
```

- `src/api/health.py::readiness` 에서 `checks` 계산 후:
  ```
  for infra_name, result in checks.items():
      if infra_name == "zookeeper" and debug_mode:
          continue  # skip_debug 는 레이블 업데이트 안 함
      INFRA_UP.labels(infra=infra_name).set(1.0 if result is True else 0.0)
  ```
- `src/main.py` lifespan: `yield` 직전에 `STARTUP_COMPLETE.set(1.0)`, `finally` 에 `STARTUP_COMPLETE.set(0.0)`

**테스트**:
- `tests/unit/test_metrics.py::test_infra_up_gauge_exists` + `test_startup_complete_gauge_exists` — regression tripwire
- `tests/unit/test_health.py::test_readiness_updates_infra_up_gauges` — 5개 infra 모두 1.0 세팅 확인
- `tests/unit/test_health.py::test_readiness_sets_gauge_zero_on_failure` — 한 infra down 시 0.0 확인

**문서**: ARCHITECTURE.md §Observability — 신규 메트릭 목록 + 라벨셋 / CONTRIBUTING.md §테스트 — "infra 추가 시 INFRA_UP 라벨 3곳 업데이트"

**회귀 가드**: metric 존재 assertion test

---

#### P0-6. `tests/integration/test_startup_failure_modes.py` 신설

**파일**: `tests/integration/test_startup_failure_modes.py` (신규)

**패턴**: `tests/integration/test_cooldown_degraded.py:37-71` 의 `docker stop` fixture 패턴 재사용.

```python
@pytest_asyncio.fixture
async def zk_stopped():
    subprocess.run(["docker", "stop", "ars-zookeeper"], check=True)
    try:
        yield
    finally:
        subprocess.run(["docker", "start", "ars-zookeeper"], check=True)
        # 복구 대기
        await _wait_for_zk_healthy()

async def test_zk_down_at_boot_fails_within_budget(zk_stopped, integration_settings):
    integration_settings.zk_startup_budget_sec = 5  # 단축
    app = FastAPI(lifespan=make_lifespan(integration_settings))
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with LifespanManager(app, startup_timeout=30):
            pass
    elapsed = time.monotonic() - start
    assert elapsed < 15  # budget 5 + 여유
```

**테스트 4개**:
- `test_zk_down_at_boot_fails_within_budget` — 핵심 dead-zone 회귀 가드
- `test_redis_down_at_boot_fails_after_retries` — P0-2 검증
- `test_mongo_down_at_boot_fails_after_retries` — 기존 동작 회귀 가드
- `test_es_bad_host_fails_at_boot` + `test_email_bad_url_fails_at_boot` — P0-3 검증

**cleanup 주의**: teardown에서 반드시 `docker start` 수행 (다른 테스트에 영향). asyncio timeout으로 teardown skip 방지.

**문서**: CONTRIBUTING.md §테스트 — "failure-mode integration tests 는 Docker 제어 권한 필요. CI 실행 전 `make dev-up` 로 baseline 복구"

**회귀 가드**: 이 파일 자체

---

### P1 (Phase 0 완성도 향상) — 5개

#### P1-1. Mongo 도메인 예외 변환

**파일**:
- `src/db/repository.py` — 모든 public async 메서드
- `src/db/exceptions.py` (신규) 또는 `src/db/models.py` 에 `MongoUnavailableError` 추가

**패턴**:
```python
_MONGO_UNAVAILABLE = (ServerSelectionTimeoutError, NetworkTimeout, ConnectionFailure)

try:
    result = await self._collection.find_one(...)
except _MONGO_UNAVAILABLE as e:
    raise MongoUnavailableError(f"mongo unavailable: {e}") from e
```

- `ProfileRepository.find_by_scope`, `resolve_profile`, `create`, `upsert`, `EqpInfoRepository.get_distinct_processes`, `count_active_by_process`, `get_active_eqps_by_process` 7개 메서드
- `DuplicateKeyError → ProfileAlreadyExistsError` 변환은 유지

**테스트**: `tests/unit/test_db_repository.py::test_*_translates_mongo_unavailable` — 7개 메서드 각각 mock 예외 주입

**문서**: SCHEMA.md §Exception Contract / CONTRIBUTING.md §DB layer

**회귀 가드**: 변환 테스트 7개

---

#### P1-2. `_job_wrapper` failure reason 레이블

**파일**:
- `src/scheduler/jobs.py` — `_job_wrapper` 예외 branch
- `src/api/metrics.py` — `JOB_TOTAL` labelset 확장

**패턴**:
```python
# 기존: JOB_TOTAL = Counter(..., labels=["process", "status"])
# 신규: labels=["process", "status", "reason"]
#   reason: "" (success), "mongo_unavailable", "es_unavailable",
#           "lock_timeout", "other"

except Exception as e:
    if isinstance(e, MongoUnavailableError):
        reason = "mongo_unavailable"
    elif isinstance(e, LockAcquisitionTimeout):
        reason = "lock_timeout"
    elif "elasticsearch" in type(e).__module__:
        reason = "es_unavailable"
    else:
        reason = "other"
    JOB_TOTAL.labels(process=process, status="failure", reason=reason).inc()
```

- **주의**: Prometheus 레이블 추가는 dashboard breaking change. Step 9 배포 **전에** 반영 필요

**테스트**: `tests/unit/test_scheduler_jobs.py::test_failure_reason_labels` — parametrize로 4가지 예외 주입

**문서**: ARCHITECTURE.md §Observability — `JOB_TOTAL` 레이블 스키마

**회귀 가드**: parametrized test

---

#### P1-3. Email in-memory outbox + admin endpoint

**파일**:
- `src/alert/email_client.py` — `EmailAlertClient.__init__` + `send_alert`
- `src/api/admin.py` — 신규 `GET /admin/email-outbox`

**패턴**:
```python
from collections import deque

class EmailAlertClient:
    def __init__(self, settings):
        ...
        self._outbox: deque = deque(maxlen=1000)

    async def send_alert(self, request):
        ...
        # 모든 False return 분기에서:
        self._outbox.append({
            "ts": time.time(),
            "payload": request.to_payload(),
            "reason": "timeout" | "http_error" | "connect_error" | ...,
        })
        return False

    def get_outbox_snapshot(self) -> list[dict]:
        return list(self._outbox)
```

```python
# src/api/admin.py
@router.get("/email-outbox")
async def email_outbox(email_client = Depends(deps.get_email_client)):
    if email_client is None:
        raise HTTPException(503, "email client not available")
    return {
        "count": len(email_client._outbox),
        "max_size": email_client._outbox.maxlen,
        "entries": email_client.get_outbox_snapshot()[-50:],  # 최근 50건
    }
```

- debug_read_only 모드에서는 outbox 기록 안 함 (debug 기록 오염 방지)

**테스트**:
- `tests/unit/test_email_client.py::test_send_alert_failure_appends_to_outbox` — 각 실패 분기에서 outbox 증가
- `tests/unit/test_email_client.py::test_outbox_bounded_at_maxlen` — 1001건 append 후 첫 건 evicted
- `tests/unit/test_admin.py::test_email_outbox_endpoint` — FastAPI TestClient로 응답 구조 확인

**문서**: ARCHITECTURE.md §Alerting — "Phase 0 in-memory outbox, bounded 1000. Phase 1+ 영구 outbox 검토" / README.md §Admin Endpoints — 신규 경로 문서화

**회귀 가드**: unit test 3개

---

#### P1-4. K8s probe timing invariant 테스트

**파일**: `tests/unit/test_k8s_probe_invariants.py` (신규)

**패턴**:
```python
import yaml
from pathlib import Path
from src.config.settings import AppSettings

def test_liveness_initial_delay_exceeds_zk_budget():
    """liveness probe가 ZK startup budget 완료 전에 fire되지 않도록."""
    manifest = yaml.safe_load(
        Path("k8s/deployment.yaml").read_text()
    )
    liveness = manifest["spec"]["template"]["spec"]["containers"][0]["livenessProbe"]
    initial_delay = liveness["initialDelaySeconds"]

    settings = AppSettings()
    zk_budget = settings.zk_startup_budget_sec

    # 10초 안전 마진
    assert initial_delay >= zk_budget + 10, (
        f"liveness.initialDelaySeconds ({initial_delay}) must be >= "
        f"zk_startup_budget_sec ({zk_budget}) + 10"
    )

def test_readiness_failure_threshold_allows_60s_grace():
    """readiness failureThreshold × periodSeconds >= 60."""
    manifest = yaml.safe_load(Path("k8s/deployment.yaml").read_text())
    readiness = manifest["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert readiness["failureThreshold"] * readiness["periodSeconds"] >= 60
```

**문서**: CONTRIBUTING.md §K8s — "probe 설정 또는 `zk_startup_budget_sec` 변경 시 이 테스트 업데이트 필수"

**회귀 가드**: 이 파일 자체

---

#### P1-5. ES introspect cache TTL

**파일**: `src/es/client.py` — `introspect_field_type`, `__init__`

**패턴**:
```python
from cachetools import TTLCache

class ESClient:
    def __init__(self, settings):
        ...
        self._field_types: TTLCache = TTLCache(maxsize=500, ttl=600)  # 10분
        self._introspect_attempted: TTLCache = TTLCache(maxsize=500, ttl=300)  # 5분
```

- 5분 후 재시도 → 기동 후 만들어진 인덱스도 결국 감지
- 성공 결과는 10분 캐시 (positive cache 더 길게)

**테스트**: `tests/unit/test_es_client.py::test_introspect_retries_after_ttl` — `time-machine` 으로 5분 경과 후 재시도 확인

**문서**: ARCHITECTURE.md §ES introspection — TTL 정책 명시

**회귀 가드**: TTL 만료 test

---

### P2 (Phase 1+ 이월, 명시적 out-of-scope)

다음 항목은 의도적으로 Phase 0에서 제외됨. 사일런트 drop 방지 목적으로 **명시**:

| # | 항목 | 이월 사유 |
|---|------|----------|
| 1 | Circuit breaker (`pybreaker`) | Analyzer 부재로 현재 호출량 적음. APScheduler `coalesce=True`가 자연 throttle 역할 |
| 2 | 영구 email outbox (Redis LIST / 디스크) | In-memory deque(P1-3)로 Phase 0 충분. 운영 상 실제 alert 소실 보고 시 업그레이드 |
| 3 | 멀티 인스턴스 failover 패턴 | PRD Phase 0 = `replicas=1` 고정. Phase 1+에서 검토 |
| 4 | ES version auto-detect / 7.x→8.x fallback | 실패 처리와 무관. `_verify_infra_versions` 경고 로그로 충분 |
| 5 | `/admin/*` 인증 + NetworkPolicy | 이미 `src/api/admin.py:4-7` 에 Phase 1+ TODO |
| 6 | OpenTelemetry tracing | 의존성 surface 증가, orthogonal. Phase 1+ |
| 7 | `MongoClient.connect_with_retry` → shared helper refactor | 동작 중 + 테스트 존재 → grey-field 원칙, 건드리지 않음 |

**재평가 trigger**: (a) 운영 alert 소실 보고, (b) Phase 0 1주 이상 outage, (c) Step 11 Phase 1 kickoff — 셋 중 먼저 발생하는 것

---

### 수정 파일 전체 목록 (Step 8.5)

**신규 파일**:
- `src/db/exceptions.py` (또는 `models.py`에 추가) — `MongoUnavailableError`
- `tests/integration/test_startup_failure_modes.py` — 5개 시나리오
- `tests/unit/test_k8s_probe_invariants.py` — 2개 invariant

**수정 파일**:
- `src/config/settings.py` — `zk_startup_budget_sec: int = 45`
- `src/distributed/zk_client.py` — time-bounded start + ThreadPoolExecutor
- `src/distributed/partition_manager.py` — `_do_redistribute` retry + `redistribute_unhealthy`
- `src/cache/redis_client.py` — `connect_with_retry`
- `src/startup/infra.py` — Redis 호출 변경
- `src/es/client.py` — `connect()` ping + TTLCache
- `src/alert/email_client.py` — `connect()` health_check + outbox deque
- `src/db/repository.py` — 7개 메서드 예외 변환
- `src/api/metrics.py` — INFRA_UP + STARTUP_COMPLETE Gauge + JOB_TOTAL reason label
- `src/api/health.py` — readiness에서 Gauge 업데이트 + `redistribute_unhealthy` 체크
- `src/api/admin.py` — `/admin/email-outbox`
- `src/main.py` — lifespan에 `STARTUP_COMPLETE.set(1.0)` hook
- `src/scheduler/jobs.py` — `_job_wrapper` reason 분기

**확장 테스트**:
- `tests/unit/test_zk_client.py` — timeout test
- `tests/unit/test_redis_client.py` — retry test
- `tests/unit/test_es_client.py` — connect ping + TTL
- `tests/unit/test_email_client.py` — connect health_check + outbox
- `tests/unit/test_partition_manager.py` — redistribute retry
- `tests/unit/test_db_repository.py` — exception translation
- `tests/unit/test_scheduler_jobs.py` — reason labels
- `tests/unit/test_health.py` — Gauge 업데이트 + unhealthy flag
- `tests/unit/test_metrics.py` — Gauge 존재
- `tests/unit/test_admin.py` — email-outbox endpoint

**문서 업데이트**:
- `ARCHITECTURE.md` — §Failure Modes 신설 (또는 §9 확장), §Observability 메트릭 추가, §Distributed Coordination redistribute retry
- `CONTRIBUTING.md` — §K8s probe invariants, §DB exception contract, §INFRA_UP 라벨
- `SCHEMA.md` — §Exception Contract (MongoUnavailableError)
- `README.md` — Admin endpoints에 `/admin/email-outbox`

---

### 검증 절차 (Step 8.5 단독)

#### 전제
```bash
cd /Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer
make dev-up
docker ps --format 'table {{.Names}}\t{{.Status}}'
# 기대: ars-redis, ars-zookeeper, ars-elasticsearch, mongodb-44 모두 Up
```

#### V1 — 기존 회귀 (기준선)
```bash
.venv/bin/python -m pytest tests/unit -q
# 기대: 278 + 신규 테스트 모두 통과
```

#### V2 — ZK down at startup (dead zone 수정 검증)
```bash
docker stop ars-zookeeper
time .venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 2>&1 | tee /tmp/zk-down.log
```
**기대**: 프로세스가 **45~50초** 내 non-zero 종료. 로그에 `zk_startup_timeout` + `startup_phase_failed phase=init_infra`. 이전(v5)의 무한 hang **아님**.
```bash
docker start ars-zookeeper
```

#### V3 — Redis down at startup
```bash
docker stop ars-redis
time .venv/bin/python -m uvicorn src.main:app --port 8000 2>&1 | tee /tmp/redis-down.log
docker start ars-redis
```
**기대**: ~10초 이내 실패. 로그에 `redis_connect_retry attempt=1,2,3` + `startup_phase_failed`.

#### V4 — Mongo down at startup (기존 동작 회귀 가드)
```bash
docker stop mongodb-44
time .venv/bin/python -m uvicorn src.main:app --port 8000 2>&1 | tee /tmp/mongo-down.log
docker start mongodb-44
```
**기대**: ~30~35초 이내 실패. `mongo_connect_retry attempt=1..5`.

#### V5 — ES bad hosts
```bash
MONITOR_ES_HOSTS=http://localhost:59999 .venv/bin/python -m uvicorn src.main:app --port 8000
```
**기대**: 즉시 실패, `es_startup_ping_failed` 로그. 이전(v5)은 silent 통과였음.

#### V6 — Email bad URL
```bash
MONITOR_EMAIL_API_URL=http://localhost:59998/EmailNotify .venv/bin/python -m uvicorn src.main:app --port 8000
```
**기대**: 즉시 실패, `email_startup_health_check_failed` 로그.

#### V7 — 정상 기동 + 메트릭 확인
```bash
.venv/bin/python -m uvicorn src.main:app --port 8000 &
sleep 15
curl -sS http://localhost:8000/healthz/ready | jq .
curl -sS http://localhost:8000/metrics | grep -E 'infra_up|startup_complete'
```
**기대**:
- `checks` 5개 모두 true
- `resource_monitor_infra_up{infra="elasticsearch"} 1.0` (5개 infra)
- `resource_monitor_startup_complete 1.0`

#### V8 — Redis 런타임 중단 + 복구 (degraded mode 기존)
```bash
docker stop ars-redis
curl -sS http://localhost:8000/healthz/ready | jq .
curl -sS http://localhost:8000/metrics | grep 'infra_up{infra="redis"}'
# 기대: checks.redis=false, infra_up{redis}=0.0, scheduler 계속 동작
curl -sS http://localhost:8000/admin/status | jq .scheduler_running
# 기대: true
docker start ars-redis
sleep 15
curl -sS http://localhost:8000/healthz/ready | jq .
# 기대: 200, checks.redis=true, infra_up{redis}=1.0
```

#### V9 — Leader Mongo 실패 시 재시도 (unit test 로 검증)
```bash
.venv/bin/python -m pytest tests/unit/test_partition_manager.py::test_redistribute_mongo_failure_retries_and_recovers -v
.venv/bin/python -m pytest tests/unit/test_partition_manager.py::test_redistribute_persistent_failure_flags_unhealthy -v
```

#### V10 — 전체 통합 테스트
```bash
docker start ars-redis ars-zookeeper mongodb-44 2>/dev/null || true
make dev-status
.venv/bin/python -m pytest tests/integration/test_startup_failure_modes.py -v
# 기대: 5개 시나리오 모두 통과

.venv/bin/python -m pytest -q
# 기대: 278 + 신규 ~30개 테스트 모두 통과
```

---

### 완료 기준 (Step 8.5)

- [ ] P0 6개 모두 구현 + 테스트 통과
- [ ] P1 5개 모두 구현 + 테스트 통과
- [ ] V1~V10 검증 시나리오 10개 모두 manual 실행 + 로그/메트릭 확인
- [ ] ARCHITECTURE.md / CONTRIBUTING.md / SCHEMA.md / README.md 4개 문서 업데이트
- [ ] 전체 테스트 count: 기존 278 + 신규 ~30 = ~308 passing
- [ ] `pytest tests/integration/test_startup_failure_modes.py` 단독 통과 (5/5)
- [ ] 이후 Step 9 (Dockerfile + K8s) 및 Step 10 (OrbStack 통합 테스트) 진행 가능 상태

---

## v5 환경 결정 사항 (2026-04-07)

Step 9/10 상세화 과정에서 사용자와 합의된 dev infra 전략:

### OrbStack 현황
| 서비스 | 컨테이너 | 이미지 | 포트 | 비고 |
|--------|---------|--------|------|------|
| MongoDB | `mongodb-44` | `mongo:4.4.30` | 27017 | 단독 컨테이너 (compose 외부) |
| Redis | `ars-redis` | `redis:7-alpine` | 6379 | `ARS/docker/docker-compose.yml`에 정의됨 |
| Elasticsearch | (없음) | — | — | **신규 추가 필요** |
| Zookeeper | (없음) | — | — | **신규 추가 필요** |
| Akka Email | (없음) | — | — | 테스트는 in-process mock 서버 사용 |

### 결정
1. **테스트 환경 = dev 환경 = OrbStack**: testcontainers 사용 안 함. 이미 떠 있는 인프라를 직접 활용해 통합/E2E를 동일한 기반에서 운영. Phase 1+에서도 같은 환경 재활용.
2. **Redis 7-alpine → 5.0.6-alpine 다운그레이드**: 운영 동일 버전으로. **단, ARS 다른 프로젝트(socks-agent, direct-agent, WebManager)에 영향이 갈 수 있어 사전 검증 필요** (Step 9.1 참조).
3. **ZK 3.5.5 + ES 7.11.9 추가 위치**: `ARS/docker/docker-compose.yml`에 직접 추가. RMS subproject에서 ARS root 파일 수정은 hooks 정책 확인 필요. 차단되면 사용자가 ARS root context에서 직접 수정 (plan에 명시).
4. **MongoDB 4.4.30 그대로**: motor 3.7+ 호환. transactions 미사용이라 무관.
5. **Email API**: pytest 안 in-process aiohttp/FastAPI mock 서버. 실제 Akka 호출 안 함.
6. **테스트 namespace 격리**: 각 test run마다 UUID 기반 prefix 생성. session 종료 시 autouse cleanup으로 모든 리소스 drop.
   - Mongo: `EARS_test_<run_id>` DB
   - Redis: `RESOURCE_ALERT_test_<run_id>:` 키 prefix
   - ES: `test_<run_id>_*` 인덱스 패턴
   - ZK: `/resource-monitor-test-<run_id>` 루트

---

## v3에서 추가된 핵심 변경 사항 (v2 대비)

### P0 (긴급)
1. **kazoo Election.run() 블로킹 해결** — fire-and-forget + threading.Event
2. **liveness/readiness 엔드포인트 분리** — `/healthz/live` + `/healthz/ready`
3. **ZK 3.5.5 LOST → CONNECTED 시 watches/ephemeral 재초기화**
4. **lifespan을 init_infrastructure 헬퍼들로 분해**
5. **clients dict 스코프 + finally 정리 로직 보강**
6. **scheduler shutdown 시 pending Task 강제 cancel**
7. **CancelledError를 health check에서 전파**
8. **introspect_mapping NotFoundError 처리** (당일 첫 시작)
9. **kazoo Transaction 전 ensure_path 선행**
10. **디바운스를 플래그 → Task cancel 패턴으로 변경**
11. **Leader epoch ZK 영속화** (재시작 후 0 리셋 방지)
12. **kazoo SASL 옵션 + Redis AUTH** (Redis 5는 ACL 없음)
13. **버전 핀**: `kazoo>=2.9.0,<2.11.0`, `redis[hiredis]>=4.5.0,<5.1.0`
14. **list[str] env var 파싱** (쉼표 구분 + JSON 둘 다 허용)
15. **CI pipeline + tests/ 계층화** (unit/integration/e2e)
16. **K8s memory limit 1Gi**, PreStop hook, PodDisruptionBudget
17. **`/admin/status` 엔드포인트** (운영 가시성)
18. **Prometheus `/metrics` 엔드포인트**
19. **Self-alert on critical error** (서비스 자체 죽음 감지)
20. **container securityContext**: runAsNonRoot, readOnlyRootFilesystem

### P1 (중요)
- ChildrenWatch/DataWatch 명시적 등록 (코드에 누락되어 있던 부분)
- 자신의 assignment 노드 DataWatch (set_data 변경 감지)
- Stale 방어: epoch + assigned_at timestamp 복합 비교
- Lock acquire는 매번 새 Lock 객체 (kazoo Lock 비재진입) + asyncio.Lock per process
- two-phase logging (settings 전/후)
- testcontainers `zookeeper:3.5.5` + `redis:5.0.6-alpine`
- time-machine for time mocking
- Pydantic to_mongo/from_mongo 왕복 테스트
- mock_<name> fixture 명명 규칙
- Cooldown degraded mode (Redis 다운 시 False 반환)

## v4에서 추가된 핵심 변경 사항 (v3 대비)

### P0 (긴급) — 운영 확정 정보 반영
1. **ES 7.11.9 호환** — `elasticsearch[async]>=7.11.0,<8.0.0`, 쿼리는 `body=` 파라미터, response는 raw dict, `timeout=`(not `request_timeout=`)
2. **Email API 응답 포맷 정정** — `data.get("result") == "success"` (소문자). v3의 `"Success"`는 **항상 False**였던 P0 버그
3. **EQP_INFO 필드명 매핑** — `model → eqpModel`. Pydantic 모델 `Scope.model` 필드의 `to_mongo()`/`from_mongo()`에서 alias 변환
4. **활성 장비 필터** — `get_distinct_processes()`와 장비 수 집계 시 `{onoff:1, webmanagerUse:1}` 필터 적용 (비활성 PC 제외)

### P0 (긴급) — 3차 검증 미반영분
5. **LeaderElection LOST 후 재시작** — v3의 `_reinit_after_loss()`는 watches/ephemeral만 재생성. `Election` 인스턴스는 세션이 죽었으므로 `election.run()`도 종료됨 → 새 `Election` 객체로 재시작 필요
6. **`_register_watches()` 멱등성** — `ChildrenWatch`/`DataWatch`는 내부적으로 listener를 kazoo client에 등록. 재호출 시 누수 → 기존 listener 제거 또는 플래그 가드
7. **DataWatch 빈 노드 처리** — `ensure_path`로 생성된 빈 노드를 `json.loads("")` 하면 `JSONDecodeError`. `if data is None or len(data) == 0: return` 가드
8. **Motor `close()` 동기** — `AsyncIOMotorClient.close()`는 동기 메서드. `await client.close()` 하면 `TypeError`. `client.close()`만 호출
9. **TTL 캐시 bounded** — `resolve_profile` TTL 캐시가 `dict`라서 20K eqpId 각각 다른 scope 요청 시 무한 증가 → `cachetools.TTLCache(maxsize=10000, ttl=300)` 사용
10. **Redis degraded 이메일 폭주 방지** — Redis 다운 시 cooldown False → 매 cycle마다 동일 알림 발송. Redis 다운 감지 시 **로컬 in-memory fallback cooldown** (dict + timestamp) 유지
11. **ZK 4lw whitelist 차단 대응** — `command(b"stat")`이 `Connection reset`/`NotReadOnlyCallException` 실패 가능. try/except 폴백 + 버전 미상 시 `unknown` 반환 + 경고 로그. 설치 가이드에 `4lw.commands.whitelist=stat,ruok,conf` 추가 권장 명시

### P1 (중요)
- **elasticsearch-py 7.x import 경로** — `from elasticsearch import AsyncElasticsearch` + `from elasticsearch.exceptions import NotFoundError, ConnectionError` (8.x는 `elastic_transport` 사용)
- **`ignore_unavailable`는 query string param**이 아닌 7.x 공식 kwarg
- **Pydantic `Scope.model` 필드명 충돌** — `model`은 Pydantic v2 예약 (`model_config`, `model_dump`). Field alias로 해결: `eqp_model: str = Field(alias="model")`
- **settings.py `model` → Pydantic v2 예약어 회피** — `model_config = {"env_prefix": ...}` 이미 v3에 있음 — 충돌 없음

### P2 (낮음)
- 구성 파일(zoo.cfg) 경로 확인 가이드 (ZK 4lw / SASL 체크 명령)

---

## 의존성 그래프

```
pyproject.toml + pytest 설정 + Makefile + tests/ 계층 구조
    |
    v
config/settings.py (list 파싱 validator) + constants.py + logging_config.py (two-phase)
    |
    +----> es/client.py (introspect 옵셔널, NotFoundError 처리) -> es/queries.py
    |
    +----> db/client.py -> db/models.py (AnalysisConfig + to/from_mongo) -> db/repository.py (TTL 캐시)
    |                                         |
    |                                         v
    |                                    analyzer/metric_resolver.py (wildcard + category 경계)
    |
    +----> cache/redis_client.py (protocol=2, AUTH) -> cache/cooldown.py (degraded mode + pipeline)
    |
    +----> alert/models.py -> alert/email_client.py
    |
    +----> distributed/zk_client.py (state machine + 재초기화 + SASL)
    |        |
    |        +-> distributed/leader_election.py (Election fire-and-forget + epoch 영속화)
    |        +-> distributed/lock.py (asyncio.Lock + 새 Lock per acquire)
    |        +-> distributed/partition_manager.py (Transaction + ensure_path + Task cancel debounce)
    |
    v
api/deps.py + api/health.py (live/ready 분리) + api/admin.py + api/metrics.py
    |
    v
scheduler/jobs.py (job 래퍼 + Semaphore + pause/resume + 강제 cancel)
    |
    v
startup/{infra,repos,distributed,scheduler}.py (lifespan 분해)
    |
    v
main.py (lifespan thin orchestrator + RequestId 미들웨어 + 전역 핸들러 + Self-alert)
    |
    v
Dockerfile (USER, healthcheck) + k8s/ (live/ready 분리, PDB, securityContext, Secret)
```

---

## Step 0: 프로젝트 스켈레톤 + pytest + CI

**생성 파일:**
- `pyproject.toml`
- `.gitignore`, `.python-version`
- `src/__init__.py` + 하위 패키지
- `tests/{unit,integration,e2e}/__init__.py`, `tests/conftest.py`, `tests/factories.py`
- `Makefile`
- `.github/workflows/ci.yml` (존재 시) 또는 `scripts/run_tests.sh`

**pyproject.toml:**
```toml
[project]
name = "resource-monitor-server"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.27.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "elasticsearch[async]>=7.11.0,<8.0.0",  # 운영 ES 7.11.9 — 8.x 금지
    "motor>=3.3.0",
    "apscheduler>=3.10.0,<4.0.0",
    "kazoo>=2.9.0,<2.11.0",          # ZK 3.5.5 호환 검증 범위
    "redis[hiredis]>=4.5.0,<5.1.0",  # Redis 5.0.6 호환
    "httpx>=0.27.0",
    "structlog>=24.0.0",
    "prometheus-client>=0.20.0",     # /metrics
    "cachetools>=5.3.0",             # TTLCache bounded (v4)
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=4.0.0",
    "pytest-mock>=3.12.0",
    "pytest-watch>=4.2.0",
    "pytest-xdist>=3.5.0",
    "time-machine>=2.13.0",
    "testcontainers[mongodb,redis,elasticsearch]>=4.0.0",
    "ruff>=0.3.0",
]

[tool.setuptools.packages.find]
where = ["."]
include = ["src*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"  # Motor 이벤트 루프 충돌 방지
pythonpath = ["."]
markers = [
    "unit: mock 기반 단위 테스트",
    "integration: testcontainers 기반",
    "e2e: 다중 인스턴스 시나리오",
    "slow: 10초 이상",
]

[tool.coverage.report]
fail_under = 80
exclude_lines = ["pragma: no cover", "if TYPE_CHECKING:", "raise NotImplementedError"]
```

**Makefile:**
```makefile
test-fast:        # TDD 사이클 (< 5초)
	pytest tests/unit -m unit -x -q

test-integration: # 커밋 전 (< 2분)
	pytest tests/unit tests/integration --ignore=tests/e2e

test-full:        # CI
	pytest tests/

test-watch:
	ptw -- -m unit -x -q

lint:
	ruff check src tests
```

**tests/conftest.py — 동기/비동기 mock 규칙 + clear_settings_cache:**
```python
"""
============================================================
MOCKING 규칙 (강제):
- kazoo (KazooClient): MagicMock — 동기 API
- motor (AsyncIOMotorClient): AsyncMock
- redis.asyncio: AsyncMock
- httpx.AsyncClient: AsyncMock
- AsyncElasticsearch: AsyncMock

FIXTURE 명명: mock_<name> (mock_es, mock_mongo, mock_redis, mock_zk, mock_email)
DATA fixture: sample_<name> (sample_profile, sample_scope)
============================================================
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.config.settings import get_settings

@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

@dataclass
class MockInfraContext:
    es: AsyncMock
    mongo: AsyncMock
    redis: AsyncMock
    zk: MagicMock
    email: AsyncMock

@pytest.fixture
def mock_infra() -> MockInfraContext:
    return MockInfraContext(
        es=AsyncMock(), mongo=AsyncMock(), redis=AsyncMock(),
        zk=MagicMock(), email=AsyncMock(),
    )
```

**검증**: `make test-fast` 실행 → 0개 테스트 통과 (스켈레톤 OK)

---

## Step 1: 설정 + 로깅 (two-phase)

**생성 파일:**
- `src/config/settings.py`
- `src/config/constants.py`
- `src/logging_config.py`
- `tests/unit/test_settings.py`

### settings.py — 핵심 변경 (v2 대비)

```python
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
import json

class AppSettings(BaseSettings):
    # ES (7.11.9)
    es_hosts: list[str] = ["http://es-cluster:9200"]
    es_username: str = ""
    es_password: SecretStr = SecretStr("")
    es_use_ssl: bool = False
    es_request_timeout: int = 30  # 7.x는 `timeout` 파라미터로 전달
    es_max_retries: int = 3

    # MongoDB
    mongo_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongo_db: str = "EARS"

    # Zookeeper (3.5.5)
    zk_hosts: str = "zk1:2181,zk2:2181,zk3:2181"
    zk_root_path: str = "/resource-monitor"
    zk_session_timeout: int = 30  # tickTime(2s) × 15, 4~40초 범위
    zk_sasl_mechanism: str = ""   # "DIGEST-MD5" 등, 빈 문자열이면 미사용
    zk_sasl_username: str = ""
    zk_sasl_password: SecretStr = SecretStr("")

    # Redis (5.0.6 — ACL 없음)
    redis_url: str = "redis://redis:6379/0"
    redis_password: SecretStr = SecretStr("")  # 단순 AUTH password
    redis_key_prefix: str = "RESOURCE_ALERT"

    # Email API
    email_api_url: str = "http://httpwebserver:8080/EmailNotify"
    email_api_timeout: int = 10

    # Grafana
    grafana_base_url: str = "http://grafana:3000"
    grafana_dashboard_uid: str = ""

    # Scheduler / Instance
    scheduler_misfire_grace_time: int = 60
    instance_id: str = ""
    local_tz: str = "Asia/Seoul"  # 인덱스 timezone

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    model_config = {"env_prefix": "MONITOR_", "env_file": ".env"}

    @field_validator("es_hosts", mode="before")
    @classmethod
    def parse_es_hosts(cls, v):
        """JSON 배열과 쉼표 구분 둘 다 지원 (ConfigMap 작성 편의)."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
```

### logging_config.py — two-phase initialization

```python
import logging
import sys
import structlog
from .config.settings import AppSettings

def setup_logging_minimal():
    """settings 로드 전 최소 초기화 (JSON stderr, INFO)."""
    logging.basicConfig(
        level=logging.INFO,
        format='{"event":"%(message)s","level":"%(levelname)s"}',
        stream=sys.stderr,
    )

def setup_logging(settings: AppSettings) -> None:
    """완전 초기화 — uvicorn access 로그도 structlog 통합."""
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    renderer = (structlog.processors.JSONRenderer()
                if settings.log_format == "json"
                else structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # uvicorn 로거를 structlog 파이프라인에 통합
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "kazoo"):
        lg = logging.getLogger(logger_name)
        lg.handlers = [handler]
        lg.propagate = False

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
```

**TDD**: env 오버라이드, 쉼표/JSON 파싱, SecretStr 마스킹, two-phase 호출 검증

---

## Step 2: ES 클라이언트 + 쿼리 빌더

**생성 파일:**
- `src/es/client.py`, `src/es/queries.py`
- `tests/unit/test_es_client.py`, `tests/unit/test_queries.py`
- `tests/integration/test_es_integration.py`

### 핵심 변경 (v4 — ES 7.11.9 호환)

**⚠️ ES 7.x vs 8.x API 차이점**:
- Import: `from elasticsearch import AsyncElasticsearch` + `from elasticsearch.exceptions import NotFoundError, ConnectionError, TransportError`
- Auth: 7.x는 `http_auth=(user, pass)` 사용 (8.x의 `basic_auth`와 다름)
- Search: 7.x는 `body={...}` 파라미터 (8.x는 named params 권장)
- Response: 7.x는 raw dict 반환 (8.x는 `ObjectApiResponse` 래퍼)
- Timeout: 7.x는 `timeout=` 파라미터 (8.x는 `request_timeout=`)
- Retry: `retry_on_timeout=True`, `max_retries=N` (동일)

**1. Client 초기화 — 7.x API:**
```python
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import NotFoundError, ConnectionError, TransportError

class ESClient:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self._client: AsyncElasticsearch | None = None
        self._field_types: dict[str, str] = {}  # 캐시 (bounded 필요 없음, field 수 제한됨)
        self._introspect_attempted: set[str] = set()

    async def connect(self):
        kwargs = {
            "hosts": self._settings.es_hosts,
            "timeout": self._settings.es_request_timeout,   # 7.x: timeout (NOT request_timeout)
            "max_retries": self._settings.es_max_retries,
            "retry_on_timeout": True,
        }
        if self._settings.es_username:
            kwargs["http_auth"] = (                          # 7.x: http_auth (NOT basic_auth)
                self._settings.es_username,
                self._settings.es_password.get_secret_value(),
            )
        if self._settings.es_use_ssl:
            kwargs["verify_certs"] = True
            kwargs["use_ssl"] = True
        self._client = AsyncElasticsearch(**kwargs)

    async def ping(self) -> bool:
        try:
            return await self._client.ping()
        except Exception:
            return False

    async def close(self):
        if self._client:
            await self._client.close()

    async def introspect_field_type(self, index_pattern: str, field: str) -> str:
        """매핑 캐싱 + 인덱스 미존재 시 'unknown' 반환 (서비스 시작 막지 않음)."""
        cache_key = f"{index_pattern}:{field}"
        if cache_key in self._field_types:
            return self._field_types[cache_key]
        if cache_key in self._introspect_attempted:
            return "unknown"
        self._introspect_attempted.add(cache_key)
        try:
            mapping = await self._client.indices.get_mapping(
                index=index_pattern, allow_no_indices=True
            )
            # 7.x: mapping이 raw dict. include_type_name 기본 false(7.x) → properties 직접
            for idx_data in mapping.values():
                props = idx_data.get("mappings", {}).get("properties", {})
                if field in props:
                    field_type = props[field].get("type", "text")
                    self._field_types[cache_key] = field_type
                    return field_type
        except NotFoundError:
            logger.warning("es_index_not_found_for_introspect", pattern=index_pattern)
        except Exception as e:
            logger.warning("es_introspect_failed", error=str(e))
        return "unknown"
```

**2. resolve_index_range — 자정 경계 처리 (v2와 동일, timezone 반영):**
```python
def resolve_index_range(self, process: str, time_range_minutes: int) -> str:
    tz = ZoneInfo(self._settings.local_tz)
    now = datetime.now(tz)
    start = now - timedelta(minutes=time_range_minutes)
    if start.date() == now.date():
        return f"{process.lower()}_all-{now.strftime('%Y.%m.%d')}"
    return (f"{process.lower()}_all-{start.strftime('%Y.%m.%d')},"
            f"{process.lower()}_all-{now.strftime('%Y.%m.%d')}")
```

**3. terms agg + shard_size, max_val 제거, baseline range union (v2 동일)**

**4. search 호출 — 7.x API**:
```python
# 7.x: body= 파라미터, ignore_unavailable은 kwarg, response는 raw dict
result = await self._client.search(
    index=index,
    body=query,  # 7.x는 body=, 8.x는 query/aggs 등 named
    ignore_unavailable=True,
    max_concurrent_shard_requests=5,
)
# result는 dict → result["hits"]["hits"], result["aggregations"] 직접 접근
```

**TDD**: time-machine으로 자정 경계, NotFoundError fallback, .keyword introspect 검증, 7.x response dict 구조 검증

---

## Step 3: MongoDB 클라이언트 + 모델 + 리포지토리

**생성 파일:**
- `src/db/client.py`, `src/db/models.py`, `src/db/repository.py`, `src/db/seed.py`
- `tests/unit/test_models.py`, `tests/unit/test_db_repository.py`
- `tests/integration/test_mongo_integration.py`

### 핵심 변경 (v2 대비)

**1. MongoDB lazy connect with retry + 동기 close (v4: motor close는 동기):**
```python
class MongoClient:
    async def connect_with_retry(self, max_attempts: int = 5, backoff: float = 2.0):
        last_err = None
        for attempt in range(max_attempts):
            try:
                self._client = AsyncIOMotorClient(
                    self._settings.mongo_uri.get_secret_value(),
                    serverSelectionTimeoutMS=5000,
                )
                self._db = self._client[self._settings.mongo_db]
                await self._client.admin.command("ping")
                return
            except Exception as e:
                last_err = e
                logger.warning("mongo_connect_retry", attempt=attempt + 1, error=str(e))
                await asyncio.sleep(backoff * (attempt + 1))
        raise last_err

    async def close(self):
        """AsyncIOMotorClient.close()는 동기 메서드. await 금지."""
        if self._client:
            self._client.close()  # NOT awaited

    async def ping(self) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False
```

**2. Pydantic 모델 + EQP_INFO 필드명 매핑 (v4):**
```python
# Scope.model → EQP_INFO.eqpModel 필드명 매핑
# Pydantic v2: model_* 는 예약어 → eqp_model 사용 + alias
from pydantic import BaseModel, Field, ConfigDict

class Scope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    process: str
    eqp_model: str = Field(default="*", alias="model")  # JSON API에서는 "model"
    eqp_id: str = Field(default="*", alias="eqpId")

    def to_mongo_query(self) -> dict:
        """EQP_INFO 필드명에 맞춰 매핑."""
        q = {"process": self.process}
        if self.eqp_model != "*":
            q["eqpModel"] = self.eqp_model
        if self.eqp_id != "*":
            q["eqpId"] = self.eqp_id
        return q
```

**2-1. EqpInfoRepository.get_distinct_processes() — 활성 필터 (v4):**
```python
class EqpInfoRepository:
    """읽기 전용. EQP_INFO는 Akka 서버가 관리."""

    async def get_distinct_processes(self) -> list[str]:
        """onoff=1, webmanagerUse=1 인 활성 장비의 process 값만."""
        return await self._collection.distinct(
            "process",
            filter={"onoff": 1, "webmanagerUse": 1},
        )

    async def count_active_by_process(self, process: str) -> int:
        return await self._collection.count_documents({
            "process": process,
            "onoff": 1,
            "webmanagerUse": 1,
        })

    async def get_active_eqps_by_process(self, process: str):
        """분석 대상 eqpId 목록 조회 (cursor)."""
        return self._collection.find(
            {"process": process, "onoff": 1, "webmanagerUse": 1},
            projection={"eqpId": 1, "eqpModel": 1, "category": 1, "_id": 0},
        )
```

**3. seed 시 hash 기반 변경 감지:**
```python
async def seed_default_profile(repo: ProfileRepository):
    default = build_default_profile()
    existing = await repo.find_by_scope(Scope(process="*", model="*", eqpId="*"))
    if existing:
        existing_hash = hashlib.md5(
            existing.model_dump_json(exclude={"id", "created_at", "updated_at"}).encode()
        ).hexdigest()
        new_hash = hashlib.md5(
            default.model_dump_json(exclude={"id", "created_at", "updated_at"}).encode()
        ).hexdigest()
        if existing_hash == new_hash:
            logger.info("seed_profile_unchanged_skip")
            return
    await repo.upsert(default)
```

**4. resolve_profile — dot-notation + bounded TTL 캐시 (v4):**

```python
from cachetools import TTLCache

class ProfileRepository:
    def __init__(self, collection):
        self._collection = collection
        # v4: unbounded dict → TTLCache. 20K eqpId × 여러 scope 해석 요청 방어
        self._resolve_cache: TTLCache = TTLCache(maxsize=10000, ttl=300)  # 5분

    async def resolve_profile(self, process, model, eqp_id) -> MonitorProfile:
        cache_key = f"{process}:{model}:{eqp_id}"
        if cache_key in self._resolve_cache:
            return self._resolve_cache[cache_key]
        # Mongo 쿼리 (dot-notation wildcard fallback 순서: exact → model wildcard → 전역)
        profile = await self._find_most_specific(process, model, eqp_id)
        self._resolve_cache[cache_key] = profile
        return profile

    async def create(self, profile: MonitorProfile) -> str:
        try:
            result = await self._collection.insert_one(profile.to_mongo())
            return str(result.inserted_id)
        except DuplicateKeyError:
            raise ProfileAlreadyExistsError(profile.scope)

    async def upsert(self, profile):
        """upsert 시 해당 scope 캐시 invalidate."""
        await self._collection.replace_one(
            profile.scope.to_mongo_query(), profile.to_mongo(), upsert=True
        )
        # 캐시 단순 전체 clear (scope별 정밀 invalidate는 복잡)
        self._resolve_cache.clear()
```

**TDD**: 왕복 (to_mongo↔from_mongo), TTLCache 만료 (time-machine), maxsize 초과 시 LRU eviction, upsert 시 캐시 clear, DuplicateKeyError → 도메인 예외, dot-notation 쿼리, Scope.eqp_model ↔ EQP_INFO.eqpModel 매핑, get_distinct_processes onoff 필터

---

## Step 4: Redis 클라이언트 + Cooldown (Redis 5.0.6 호환)

**생성 파일:**
- `src/cache/redis_client.py`, `src/cache/cooldown.py`
- `tests/unit/test_cooldown.py`
- `tests/integration/test_redis_integration.py` (testcontainers `redis:5.0.6-alpine`)

### 핵심 변경 (v2 대비) — Redis 5.0.6 제약

**1. RedisClient — protocol=2 + 단순 AUTH:**
```python
from redis.asyncio import Redis

class RedisClient:
    def __init__(self, settings: AppSettings):
        self._url = settings.redis_url
        self._password = settings.redis_password.get_secret_value() or None

    async def connect(self):
        # Redis 5.0.6은 RESP3 미지원 → protocol=2 명시
        # ACL 없음 → username 무시, password만 사용
        self._client = Redis.from_url(
            self._url,
            password=self._password,
            decode_responses=True,
            protocol=2,
        )
        await self._client.ping()
```

**2. Cooldown — degraded mode + pipeline + local fallback (v4):**

v3까지의 degraded mode는 Redis 다운 시 `False` 반환 → 매 cycle 동일 알림 발송(이메일 폭주). v4는 **local in-memory fallback cooldown**으로 Redis 복구 시까지 중복 차단.

```python
import time
from cachetools import TTLCache

class AlertCooldownManager:
    """Redis 5.0.6 호환. SETEX, EXISTS, DEL, SCAN, pipeline만 사용.
    v4: Redis 다운 시 local in-memory fallback (이메일 폭주 방지)."""

    def __init__(self, redis_client, default_cooldown_sec: int = 600):
        self._redis = redis_client
        self._default_cooldown = default_cooldown_sec
        # local fallback: 최대 50K 키, 최장 TTL 상한 (실제 TTL은 개별 expire로 판정)
        self._local: TTLCache = TTLCache(maxsize=50000, ttl=3600)

    async def is_cooling_down(self, eqp_id, category, metric) -> bool:
        key = self._make_key(eqp_id, category, metric)
        try:
            return await self._redis.client.exists(key) > 0
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("cooldown_check_redis_unavailable_use_local",
                          key=key, error=str(e))
            # local fallback: TTLCache는 만료된 키 자동 eviction
            return key in self._local

    async def is_cooling_down_batch(self, checks):
        """단일 라운드트립 배치 조회. 실패 시 local fallback."""
        try:
            async with self._redis.client.pipeline(transaction=False) as pipe:
                for eqp_id, cat, met in checks:
                    pipe.exists(self._make_key(eqp_id, cat, met))
                results = await pipe.execute()
            return {c: bool(r) for c, r in zip(checks, results)}
        except (RedisConnectionError, RedisTimeoutError):
            logger.warning("cooldown_batch_redis_unavailable_use_local", count=len(checks))
            return {c: (self._make_key(*c) in self._local) for c in checks}

    async def set_cooldown(self, eqp_id, category, metric, cooldown_minutes):
        key = self._make_key(eqp_id, category, metric)
        ttl_sec = cooldown_minutes * 60
        # v4: Redis 성공/실패 무관하게 local도 기록 (Redis 복구 후에도 단기간 중복 차단)
        self._local[key] = time.monotonic() + ttl_sec
        try:
            await self._redis.client.setex(key, ttl_sec, "1")
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("cooldown_set_redis_unavailable_local_only",
                          key=key, error=str(e))

    async def clear_cooldown(self, eqp_id, category, metric):
        key = self._make_key(eqp_id, category, metric)
        self._local.pop(key, None)
        try:
            await self._redis.client.delete(key)
        except (RedisConnectionError, RedisTimeoutError):
            pass

    def _make_key(self, eqp_id, category, metric) -> str:
        return f"{self._redis.key_prefix}:cooldown:{eqp_id}:{category}:{metric}"
```

**TDD**:
- Redis 정상: Redis 기준 응답
- Redis 다운 + local 있음: True 반환 (폭주 방지)
- Redis 다운 + local 없음: False 반환 (최초 알림 허용)
- set_cooldown 시 Redis/local 둘 다 기록
- TTLCache maxsize 초과 LRU eviction
- pipeline 배치 성공/실패 분기

---

## Step 5: Email Alert 클라이언트

**생성 파일:**
- `src/alert/models.py`, `src/alert/email_client.py`
- `tests/unit/test_email_client.py`

### 핵심 변경 — 응답 포맷 정정 + 모든 HTTP 오류 케이스 처리 (v4)

**🚨 v4 정정**: 실제 Akka 코드 확인 결과
```scala
case class HttpResponse(result: String, message: String)
sender() ! JsonInterface.toJson(HttpResponse("success", "send ok"))
// → {"result":"success","message":"send ok"}   — 소문자 "success"!
```
v3의 `data.get("result") == "Success"`는 **항상 False**였던 P0 버그.

```python
class EmailAlertClient:
    SUCCESS_RESULT = "success"  # 소문자 (Akka HttpResponse 기준)

    async def send_alert(self, request: EmailAlertRequest) -> bool:
        try:
            resp = await self._http_client.post(self._api_url, json=request.model_dump())
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", "")
            message = data.get("message", "")
            if result == self.SUCCESS_RESULT:
                logger.info("email_send_ok", message=message)
                return True
            logger.warning("email_send_app_failure", result=result, message=message)
            return False
        except httpx.TimeoutException as e:
            logger.warning("email_send_timeout", error=str(e))
            return False
        except httpx.HTTPStatusError as e:
            logger.error("email_send_http_error",
                        status=e.response.status_code, body=e.response.text[:500])
            return False
        except httpx.ConnectError as e:
            logger.error("email_send_connect_error", error=str(e))
            return False
        except (ValueError, KeyError) as e:
            # resp.json() 실패 또는 예상 외 포맷
            logger.error("email_send_invalid_response", error=str(e))
            return False

    async def health_check(self) -> bool:
        """Email API는 전용 health endpoint가 없다면 HEAD /EmailNotify 또는 OPTIONS로 대체.
        PRD에 정의되어 있지 않으면 연결 가능성만 테스트."""
        try:
            # HTTPWebServer에 헬스 엔드포인트가 있다면 그것 사용. 없으면 connect만.
            resp = await self._http_client.get(self._api_url.replace("/EmailNotify", "/health"))
            return resp.status_code == 200
        except Exception:
            return False
```

**TDD**: parametrize로 (timeout, 5xx, ConnectError, success 소문자, fail 소문자, 빈 응답, 예상 외 result 값) 7가지 케이스

---

## Step 6: Zookeeper 3.5.5 — 가장 많은 변경

**생성 파일:**
- `src/distributed/zk_client.py`
- `src/distributed/leader_election.py`
- `src/distributed/lock.py`
- `src/distributed/partition_manager.py`
- `tests/unit/test_zk_client.py` (브릿지 테스트, threading.Event)
- `tests/unit/test_leader_election.py`
- `tests/unit/test_partition_manager.py`
- `tests/integration/test_zk_integration.py` (testcontainers `zookeeper:3.5.5`)

### 6.1 ZKClient — state machine + SASL + 루프 가드

```python
import asyncio, threading
from kazoo.client import KazooClient, KazooState
from kazoo.retry import KazooRetry
from kazoo.security import make_digest_acl

class ZKClient:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self._kazoo: KazooClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_handlers: list = []  # async (state) -> None

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        kwargs = {
            "hosts": self._settings.zk_hosts,
            "timeout": self._settings.zk_session_timeout,  # 30s, ZK 3.5.5 4~40 범위
            "connection_retry": KazooRetry(max_tries=-1, delay=1, backoff=2, max_delay=30),
            "command_retry": KazooRetry(max_tries=3, delay=0.5),
        }
        # ZK 3.5.5 SASL 인증
        if self._settings.zk_sasl_mechanism:
            kwargs["sasl_options"] = {
                "mechanism": self._settings.zk_sasl_mechanism,
                "username": self._settings.zk_sasl_username,
                "password": self._settings.zk_sasl_password.get_secret_value(),
            }
        self._kazoo = KazooClient(**kwargs)
        self._kazoo.add_listener(self._state_listener)
        await self._loop.run_in_executor(None, self._kazoo.start)
        await self._loop.run_in_executor(
            None, self._kazoo.ensure_path, self._settings.zk_root_path
        )

    def _state_listener(self, state):
        """kazoo 스레드 → asyncio 브릿지. 루프 종료 가드 + 예외 로깅."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        for handler in self._state_handlers:
            future = asyncio.run_coroutine_threadsafe(handler(state), loop)
            future.add_done_callback(self._log_state_handler_exception)

    @staticmethod
    def _log_state_handler_exception(f):
        if not f.cancelled() and (exc := f.exception()):
            logger.error("zk_state_handler_failed", error=str(exc), exc_info=exc)

    def add_state_handler(self, handler):
        self._state_handlers.append(handler)

    async def close(self):
        if self._kazoo:
            try:
                await self._loop.run_in_executor(None, self._kazoo.stop)
                await self._loop.run_in_executor(None, self._kazoo.close)
            except Exception as e:
                logger.warning("zk_close_failed", error=str(e))

    @property
    def kazoo(self): return self._kazoo
    @property
    def loop(self): return self._loop
    @property
    def root_path(self): return self._settings.zk_root_path

    def is_connected(self):
        return self._kazoo is not None and self._kazoo.connected

    async def get_server_version(self) -> str:
        """4lw 'stat' 명령으로 ZK 서버 버전 반환 (시작 시 호환성 검증용).
        v4: ZK 3.5.0+ 는 기본 4lw.commands.whitelist가 비어있어 차단됨.
        zoo.cfg에 `4lw.commands.whitelist=stat,ruok,conf` 권장.
        차단된 경우 ConnectionResetError/OSError 등 발생 — "unknown" 폴백."""
        try:
            stat_bytes = await asyncio.wait_for(
                self._loop.run_in_executor(None, lambda: self._kazoo.command(b"stat")),
                timeout=3.0,
            )
            if isinstance(stat_bytes, bytes):
                stat_bytes = stat_bytes.decode("utf-8", errors="ignore")
            for line in stat_bytes.split("\n"):
                if line.startswith("Zookeeper version:"):
                    return line.split(":", 1)[1].strip()
        except asyncio.TimeoutError:
            logger.warning("zk_stat_command_timeout")
        except Exception as e:
            # ConnectionResetError = whitelist 차단, OSError 등
            logger.warning("zk_stat_command_unavailable",
                          error_type=type(e).__name__, hint="check 4lw.commands.whitelist")
        return "unknown"
```

### 6.2 LeaderElection — fire-and-forget + epoch 영속화

**v2 핵심 결함**: `await loop.run_in_executor(None, election.run, ...)` 가 무한 블로킹 → lifespan yield 도달 못함.

**해결**:
```python
import threading
from concurrent.futures import ThreadPoolExecutor
from kazoo.recipe.election import Election
from kazoo.exceptions import NoNodeError

class LeaderElection:
    """v4: LOST 후 재시작 지원. Election 인스턴스는 세션별로 생성 (재사용 불가)."""

    def __init__(self, zk_client: ZKClient, instance_id: str):
        self._zk = zk_client
        self._instance_id = instance_id
        self._election_path = f"{zk_client.root_path}/leader-election"
        self._epoch_path = f"{zk_client.root_path}/leader-epoch"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zk-election")
        self._stop_event = threading.Event()
        self._is_leader = False
        self._epoch: int = 0
        self._on_acquired_callbacks: list = []  # async (epoch) -> None
        self._election = None
        self._election_future = None
        self._stopped = False

    async def start(self):
        """non-blocking: election.run을 백그라운드 스레드에서 실행."""
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._epoch_path)
        )
        self._start_election_run()

    def _start_election_run(self):
        """Election 객체를 새로 생성하고 fire-and-forget."""
        # v4: LOST 후 재시작 시 이전 Election 객체는 재사용 불가 — 새 객체 생성
        self._election = Election(
            self._zk.kazoo, self._election_path, self._instance_id
        )
        self._stop_event.clear()  # 재시작 시 이벤트 리셋
        loop = self._zk.loop
        self._election_future = loop.run_in_executor(
            self._executor,
            self._election.run,
            self._on_become_leader_sync,
        )

    def _on_become_leader_sync(self):
        """kazoo 스레드 (election thread)에서 호출됨."""
        try:
            # ZK에서 epoch 읽고 +1, 영속화
            data, _ = self._zk.kazoo.get(self._epoch_path)
            current_epoch = int(data.decode()) if data else 0
            new_epoch = current_epoch + 1
            self._zk.kazoo.set(self._epoch_path, str(new_epoch).encode())
            self._epoch = new_epoch
            self._is_leader = True

            loop = self._zk.loop
            if loop and not loop.is_closed():
                for cb in self._on_acquired_callbacks:
                    asyncio.run_coroutine_threadsafe(cb(new_epoch), loop)

            # stop 신호까지 블로킹 → election.run이 반환하지 않음
            self._stop_event.wait()
        except (SessionExpiredError, ConnectionClosedError) as e:
            # LOST 세션에서는 get/set이 실패. 리더십 포기 후 restart_after_loss가 재시작.
            logger.warning("leader_election_session_lost_in_handler", error=str(e))
        except Exception as e:
            logger.error("leader_election_handler_failed", error=str(e), exc_info=True)
        finally:
            self._is_leader = False

    async def restart_after_loss(self):
        """v4: ZK 세션 LOST 후 CONNECTED 복귀 시 Election을 새 객체로 재시작.
        기존 election_future는 이미 종료되었거나 중단됨."""
        if self._stopped:
            return
        logger.info("leader_election_restarting_after_loss")
        # 기존 future 정리 (이미 완료되었을 가능성 높음)
        self._stop_event.set()
        if self._election_future is not None:
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(self._election_future), timeout=5
                )
            except (asyncio.TimeoutError, Exception):
                pass
        self._is_leader = False
        # 새 Election으로 재시작
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._epoch_path)
        )
        self._start_election_run()

    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def epoch(self) -> int:
        return self._epoch

    def add_on_acquired_callback(self, cb):
        self._on_acquired_callbacks.append(cb)

    async def stop(self):
        self._stopped = True
        self._stop_event.set()
        try:
            if self._election_future is not None:
                await asyncio.wait_for(
                    asyncio.wrap_future(self._election_future), timeout=10
                )
        except asyncio.TimeoutError:
            logger.warning("leader_election_stop_timeout")
        except Exception:
            pass
        self._executor.shutdown(wait=False)
```

### 6.3 ZKAnalysisLock — 매번 새 Lock + asyncio.Lock per process

**v2 핵심 결함**: kazoo Lock 객체는 비재진입. 캐싱하면 동일 객체에 두 번 acquire 시도 시 동작 미정의.

**해결**:
```python
from contextlib import asynccontextmanager
from kazoo.recipe.lock import Lock
from kazoo.exceptions import SessionExpiredError, ConnectionClosedError

class ZKAnalysisLock:
    def __init__(self, zk_client: ZKClient):
        self._zk = zk_client
        self._asyncio_locks: dict[str, asyncio.Lock] = {}

    def _get_asyncio_lock(self, process: str) -> asyncio.Lock:
        if process not in self._asyncio_locks:
            self._asyncio_locks[process] = asyncio.Lock()
        return self._asyncio_locks[process]

    @asynccontextmanager
    async def acquire(self, process: str, timeout_sec: int = 10):
        # 동일 인스턴스 내 재진입/동시 호출 방지
        async with self._get_asyncio_lock(process):
            # 매번 새 Lock 객체 (kazoo Lock 비재진입)
            path = f"{self._zk.root_path}/locks/analysis-{process}"
            lock = self._zk.kazoo.Lock(path)
            acquired = False
            try:
                acquired = await self._zk.loop.run_in_executor(
                    None, lambda: lock.acquire(timeout=timeout_sec)
                )
                if not acquired:
                    raise LockAcquisitionTimeout(process)
                yield
            finally:
                if acquired:
                    try:
                        await self._zk.loop.run_in_executor(None, lock.release)
                    except (SessionExpiredError, ConnectionClosedError) as e:
                        # ZK 세션 만료 시 ephemeral 노드 자동 정리됨 — 락도 해제됨
                        logger.warning("lock_release_skipped_session_lost",
                                      process=process, error=str(e))
                    except Exception as e:
                        logger.error("lock_release_failed", process=process, error=str(e))
```

### 6.4 PartitionManager — Transaction + ensure_path + Task cancel debounce + 재초기화

**v2 핵심 결함들**:
- Transaction set_data NoNodeError (신규 인스턴스)
- 디바운스 플래그 → 이벤트 누락
- ChildrenWatch/DataWatch 등록 코드 누락
- **ZK 3.5.5 LOST 후 watches 재등록 안됨**
- epoch 휘발성 → 재시작 후 stale

**해결**:
```python
from kazoo.recipe.watchers import ChildrenWatch, DataWatch
from kazoo.protocol.states import KazooState

class PartitionManager:
    def __init__(self, zk_client, leader_election, eqp_repo, instance_id, scheduler_provider):
        self._zk = zk_client
        self._leader = leader_election
        self._eqp_repo = eqp_repo
        self._instance_id = instance_id
        self._get_scheduler = scheduler_provider  # callable
        self._members_path = f"{zk_client.root_path}/members"
        self._assignments_path = f"{zk_client.root_path}/assignments"
        self._my_assignment_path = f"{self._assignments_path}/{instance_id}"

        self._known_epoch: int = 0
        self._known_assigned_at: float = 0.0
        self._assigned_processes: list[str] = []
        self._last_known_assignments: list[str] = []
        self._session_lost: bool = False

        self._members_watch: ChildrenWatch | None = None
        self._assignment_watch: DataWatch | None = None
        self._redistribution_task: asyncio.Task | None = None

    async def start(self):
        # 1. members/assignments 부모 경로 보장 (container 노드, ZK 3.5.3+)
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._members_path)
        )
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._assignments_path)
        )
        # 2. 자신을 members에 ephemeral로 등록
        await self._register_member()
        # 3. 자신의 assignment 노드 보장 (set_data 가능하도록)
        await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.ensure_path(self._my_assignment_path)
        )
        # 4. ZK 상태 변화 핸들러 등록
        self._zk.add_state_handler(self.on_zk_state_change)
        # 5. 리더 취득 시 콜백 등록
        self._leader.add_on_acquired_callback(self.on_become_leader)
        # 6. watches 등록
        self._register_watches()
        # 7. 자신의 assignment 강제 조회
        await self._refresh_assignment_from_zk()

    async def _register_member(self):
        path = f"{self._members_path}/{self._instance_id}"
        try:
            await self._zk.loop.run_in_executor(
                None,
                lambda: self._zk.kazoo.create(path, b"", ephemeral=True),
            )
        except NodeExistsError:
            # 이전 세션의 노드가 아직 정리 안됨 — 무시
            pass

    def _register_watches(self):
        """ChildrenWatch + DataWatch 등록.
        ZK 3.5.5: LOST 후 자동 재등록 안되므로 _reinit_after_loss에서 다시 호출.
        v4: 멱등성 — 기존 watcher 객체 참조는 버리되 listener 누수 방지 위해
            epoch counter로 stale 콜백 식별."""
        # v4: 이전 watches의 콜백은 epoch가 달라 무시되도록
        self._watch_epoch = getattr(self, "_watch_epoch", 0) + 1
        current_epoch = self._watch_epoch

        def members_cb(children):
            if current_epoch != self._watch_epoch:
                return False  # stale watcher, kazoo가 재등록 중단
            self._on_members_changed_sync(children)

        def assignment_cb(data, stat, event):
            if current_epoch != self._watch_epoch:
                return False
            self._on_assignment_changed_sync(data, stat, event)

        self._members_watch = ChildrenWatch(
            self._zk.kazoo,
            self._members_path,
            members_cb,
            allow_session_lost=False,
        )
        self._assignment_watch = DataWatch(
            self._zk.kazoo,
            self._my_assignment_path,
            assignment_cb,
        )

    def _on_members_changed_sync(self, children):
        """kazoo 스레드 → asyncio 브릿지."""
        loop = self._zk.loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_membership_change(children), loop
        )

    def _on_assignment_changed_sync(self, data, stat, event):
        """v4: 빈 노드 / 삭제 / JSON 파싱 오류 가드."""
        # ensure_path로 생성된 빈 노드는 data=b"" → json.loads 실패
        if data is None or len(data) == 0:
            return
        loop = self._zk.loop
        if loop is None or loop.is_closed():
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("assignment_invalid_payload", error=str(e))
            return
        asyncio.run_coroutine_threadsafe(self._apply_assignment(payload), loop)

    async def on_become_leader(self, epoch: int):
        """리더가 되었을 때 즉시 1회 재분배."""
        logger.info("became_leader", epoch=epoch, instance=self._instance_id)
        members = await self._zk.loop.run_in_executor(
            None, lambda: self._zk.kazoo.get_children(self._members_path)
        )
        await self._do_redistribute(members)

    async def _handle_membership_change(self, children):
        """디바운스: 기존 task cancel 후 새 task 생성 — 마지막 이벤트 기준."""
        if not self._leader.is_leader():
            return
        if self._redistribution_task and not self._redistribution_task.done():
            self._redistribution_task.cancel()
        self._redistribution_task = asyncio.create_task(
            self._debounced_redistribute(children)
        )

    async def _debounced_redistribute(self, children):
        try:
            await asyncio.sleep(2.0)
            if not self._leader.is_leader():
                return
            # 재확인: 디바운스 동안 멤버십이 또 변경됐을 수 있음
            current = await self._zk.loop.run_in_executor(
                None, lambda: self._zk.kazoo.get_children(self._members_path)
            )
            await self._do_redistribute(current)
        except asyncio.CancelledError:
            pass  # 새 이벤트로 취소됨 — 정상

    async def _do_redistribute(self, instances: list[str]):
        processes = await self._eqp_repo.get_distinct_processes()
        assignments = self._compute_assignments(instances, processes)

        # 1. 모든 assignment 노드 사전 보장 (Transaction set_data 전제)
        for inst_id in assignments:
            path = f"{self._assignments_path}/{inst_id}"
            try:
                await self._zk.loop.run_in_executor(
                    None, lambda p=path: self._zk.kazoo.ensure_path(p)
                )
            except Exception as e:
                logger.warning("ensure_assignment_path_failed", path=path, error=str(e))

        # 2. Transaction으로 원자적 set_data
        transaction = self._zk.kazoo.transaction()
        timestamp = time.time()
        for inst_id, procs in assignments.items():
            data = json.dumps({
                "processes": procs,
                "leader_epoch": self._leader.epoch,
                "assigned_at": timestamp,
            }).encode()
            transaction.set_data(f"{self._assignments_path}/{inst_id}", data)
        try:
            results = await self._zk.loop.run_in_executor(None, transaction.commit)
            for r in results:
                if isinstance(r, Exception):
                    logger.error("transaction_op_failed", error=str(r))
        except Exception as e:
            logger.error("redistribute_transaction_failed", error=str(e))

    def _compute_assignments(self, instances, processes) -> dict[str, list[str]]:
        instances = sorted(instances)
        result = {i: [] for i in instances}
        for idx, proc in enumerate(sorted(processes)):
            result[instances[idx % len(instances)]].append(proc)
        return result

    async def _apply_assignment(self, data: dict):
        """epoch + timestamp 복합 stale 방어."""
        incoming_epoch = data["leader_epoch"]
        incoming_ts = data["assigned_at"]
        if incoming_epoch < self._known_epoch:
            return
        if incoming_epoch == self._known_epoch and incoming_ts <= self._known_assigned_at:
            return
        self._known_epoch = incoming_epoch
        self._known_assigned_at = incoming_ts
        self._assigned_processes = data["processes"]
        self._last_known_assignments = data["processes"]
        scheduler = self._get_scheduler()
        if scheduler:
            await scheduler.reload()

    async def _refresh_assignment_from_zk(self):
        """v4: 빈 노드 / 파싱 오류 가드."""
        try:
            data, _ = await self._zk.loop.run_in_executor(
                None, lambda: self._zk.kazoo.get(self._my_assignment_path)
            )
            if data is None or len(data) == 0:
                return
            try:
                payload = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning("refresh_assignment_invalid_payload", error=str(e))
                return
            await self._apply_assignment(payload)
        except NoNodeError:
            logger.warning("refresh_assignment_no_node")
        except Exception as e:
            logger.warning("refresh_assignment_failed", error=str(e))

    async def on_zk_state_change(self, state):
        """ZK 3.5.5 핵심: LOST 후 새 세션은 watches/ephemeral 모두 사라짐 → 재초기화."""
        scheduler = self._get_scheduler()
        if state == KazooState.SUSPENDED:
            logger.warning("zk_suspended_pausing_jobs")
            if scheduler:
                await scheduler.pause_all_jobs()
        elif state == KazooState.LOST:
            logger.error("zk_session_lost")
            self._session_lost = True
            self._known_epoch = 0  # 새 세션, epoch 재시작
            self._assigned_processes = []
            if scheduler:
                await scheduler.pause_all_jobs()
        elif state == KazooState.CONNECTED:
            if self._session_lost:
                logger.info("zk_reconnected_after_loss_reinit")
                await self._reinit_after_loss()
                self._session_lost = False
            if scheduler:
                await scheduler.resume_jobs_for(self._assigned_processes)

    async def _reinit_after_loss(self):
        """ZK 3.5.5: LOST 후 새 세션 — 모든 ZK 상태 재구성.
        v4: LeaderElection도 반드시 재시작 (기존 Election 객체는 죽은 세션에 귀속)."""
        try:
            # 1. ephemeral member 노드 재생성
            await self._register_member()
            # 2. assignment 노드 재보장
            await self._zk.loop.run_in_executor(
                None, lambda: self._zk.kazoo.ensure_path(self._my_assignment_path)
            )
            # 3. watches 재등록 (ZK 3.5.x 자동 재등록 안됨)
            self._register_watches()
            # 4. 자신의 assignment 강제 재조회
            await self._refresh_assignment_from_zk()
            # 5. v4: LeaderElection 재시작 — 기존 Election은 죽은 세션에 바인딩
            await self._leader.restart_after_loss()
        except Exception as e:
            logger.error("reinit_after_loss_failed", error=str(e), exc_info=True)

    async def stop(self):
        if self._redistribution_task and not self._redistribution_task.done():
            self._redistribution_task.cancel()

    def is_leader(self): return self._leader.is_leader()
    def get_my_processes(self): return list(self._assigned_processes)
    def get_instance_count(self): return -1  # health endpoint용, 정확도 비우선
```

**TDD**:
- ZKClient 브릿지: threading.Event로 콜백 도달 검증
- Election: stop 신호 → run 반환
- Lock: 매번 새 객체, asyncio.Lock 직렬화, 세션 만료 시 release 예외 흡수
- PartitionManager: 균등 분배, 디바운스 cancel, epoch+timestamp stale 방어, NodeExistsError 처리
- 통합 테스트: testcontainers `zookeeper:3.5.5`로 SUSPENDED/LOST/CONNECTED 시뮬레이션

---

## Step 7: Health/Admin/Metrics + Scheduler

**생성 파일:**
- `src/api/deps.py`, `src/api/health.py`, `src/api/admin.py`, `src/api/metrics.py`
- `src/scheduler/jobs.py`
- `tests/unit/test_health.py`, `tests/unit/test_scheduler_jobs.py`

### 7.1 Health 엔드포인트 분리 (P0)

```python
# api/health.py
router = APIRouter()

@router.get("/healthz/live")
async def liveness():
    """프로세스 자체만 확인. 외부 인프라 ping 없음.
    K8s liveness probe용 — false면 pod 재시작."""
    return {"status": "alive"}

@router.get("/healthz/ready")
async def readiness(
    es: ESClient = Depends(get_es_client),
    mongo = Depends(get_mongo_client),
    zk = Depends(get_zk_client),
    redis = Depends(get_redis_client),
    email = Depends(get_email_client),
    pm = Depends(get_partition_manager),
    scheduler = Depends(get_scheduler),
):
    """모든 인프라 ping. K8s readiness probe용 — false면 트래픽 차단만."""
    async def safe_check(coro, timeout=2.0):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.CancelledError:
            raise  # 취소 신호 전파 (삼키지 않음)
        except Exception:
            return False

    checks = {
        "elasticsearch": await safe_check(es.ping()),
        "mongodb": await safe_check(mongo.ping()),
        "zookeeper": zk.is_connected(),
        "redis": await safe_check(redis.ping()),
        "email_api": await safe_check(email.health_check()),
    }
    all_ok = all(checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ready" if all_ok else "not_ready",
            "checks": checks,
            "scheduler_running": scheduler.is_running(),
            "is_leader": pm.is_leader(),
            "version": "0.1.0",
        },
    )

# 하위호환: /health → /healthz/ready로 리다이렉트 또는 동일 응답
```

### 7.2 Admin 엔드포인트 (운영 가시성)

```python
# api/admin.py
router = APIRouter(prefix="/admin")

@router.get("/status")
async def admin_status(
    pm = Depends(get_partition_manager),
    scheduler = Depends(get_scheduler),
    settings = Depends(get_settings),
    zk = Depends(get_zk_client),
):
    return {
        "instance_id": settings.instance_id,
        "is_leader": pm.is_leader(),
        "leader_epoch": pm._leader.epoch if pm.is_leader() else None,
        "assigned_processes": pm.get_my_processes(),
        "scheduled_jobs": [
            {"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
            for j in scheduler._scheduler.get_jobs()
        ],
        "zk_connected": zk.is_connected(),
        "zk_server_version": await zk.get_server_version(),
    }

@router.delete("/cooldowns/{eqp_id}/{category}/{metric}")
async def clear_cooldown(eqp_id: str, category: str, metric: str,
                         cooldown_mgr = Depends(get_cooldown_manager)):
    await cooldown_mgr.clear_cooldown(eqp_id, category, metric)
    return {"cleared": f"{eqp_id}:{category}:{metric}"}

@router.post("/scheduler/reload")
async def reload_scheduler(scheduler = Depends(get_scheduler)):
    await scheduler.reload()
    return {"reloaded": True}
```

### 7.3 Prometheus metrics

```python
# api/metrics.py
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

JOB_DURATION = Histogram(
    "resource_monitor_job_duration_seconds",
    "분석 job 실행 시간",
    ["process", "metric_category"],
)
JOB_TOTAL = Counter(
    "resource_monitor_job_total",
    "분석 job 실행 횟수",
    ["process", "status"],  # success/failure/skip
)
ES_QUERY_DURATION = Histogram("resource_monitor_es_query_duration_seconds", "", ["process"])
ALERTS_SENT = Counter("resource_monitor_alerts_sent_total", "", ["code", "subcode"])
ZK_LEADER = Gauge("resource_monitor_zk_leader", "1 if this instance is leader")
ASSIGNED_PROCESSES = Gauge("resource_monitor_assigned_processes", "현재 할당된 process 수")

router = APIRouter()

@router.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

### 7.4 Scheduler — job 래퍼 + 강제 cancel + Semaphore

```python
class AnalysisScheduler:
    def __init__(self, es, query_builder, profile_repo, zk_lock, partition_mgr_provider, settings):
        self._es = es
        self._qb = query_builder
        self._profile_repo = profile_repo
        self._zk_lock = zk_lock
        self._get_partition_mgr = partition_mgr_provider
        self._scheduler = AsyncIOScheduler(job_defaults={
            "misfire_grace_time": settings.scheduler_misfire_grace_time,
            "coalesce": True,
            "max_instances": 1,
        })
        self._es_semaphore = asyncio.Semaphore(3)
        self._running_jobs: set[asyncio.Task] = set()
        self._paused = False

    async def pause_all_jobs(self):
        self._paused = True
        self._scheduler.pause()

    async def resume_jobs_for(self, processes):
        self._paused = False
        self._scheduler.resume()

    async def _job_wrapper(self, job_fn, *args, **kwargs):
        if self._paused:
            return
        task = asyncio.current_task()
        if task is None:
            logger.error("job_wrapper_no_task_context")
            return
        self._running_jobs.add(task)
        try:
            await job_fn(*args, **kwargs)
            JOB_TOTAL.labels(process=args[0], status="success").inc()
        except Exception as e:
            JOB_TOTAL.labels(process=args[0], status="failure").inc()
            logger.error("scheduled_job_failed",
                        job=job_fn.__name__, error=str(e), exc_info=True)
        finally:
            self._running_jobs.discard(task)

    async def _analysis_job(self, process: str, metric_configs: list):
        async with self._es_semaphore:
            try:
                async with self._zk_lock.acquire(process, timeout_sec=5):
                    for profile, mc in metric_configs:
                        index = self._qb.resolve_index_range(process, mc.schedule.window_minutes)
                        query = self._qb.build_stats_query(...)
                        with ES_QUERY_DURATION.labels(process=process).time():
                            result = await self._es.client.search(
                                index=index, body=query,
                                params={"ignore_unavailable": True,
                                       "max_concurrent_shard_requests": 5},
                            )
                        logger.info("analysis_query_result", process=process, ...)
            except LockAcquisitionTimeout:
                JOB_TOTAL.labels(process=process, status="skip").inc()
                logger.warning("lock_timeout_skip", process=process)

    async def reload(self):
        self._scheduler.remove_all_jobs()
        await self._register_jobs()

    async def shutdown(self, timeout: float = 30.0):
        self._paused = True  # 새 job wrapper 즉시 반환
        self._scheduler.shutdown(wait=False)
        if self._running_jobs:
            done, pending = await asyncio.wait(self._running_jobs, timeout=timeout)
            if pending:
                logger.warning("scheduler_force_cancel", count=len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
```

**TDD**:
- live/ready 분리 응답
- safe_check CancelledError 전파
- _job_wrapper 예외 → metrics counter 증가, 로그
- shutdown timeout 시 강제 cancel
- _analysis_job lock timeout → skip metric

---

## Step 8: Startup 분해 + Main + 미들웨어

**생성 파일:**
- `src/startup/infra.py`, `src/startup/repos.py`, `src/startup/distributed.py`, `src/startup/scheduler_init.py`
- `src/main.py`
- `src/middleware.py`
- `tests/unit/test_startup.py`

### 8.1 startup/ 분해 (lifespan thin orchestrator)

```python
# src/startup/infra.py
from contextlib import asynccontextmanager

@asynccontextmanager
async def startup_phase(name: str):
    logger.info("startup_phase_begin", phase=name)
    try:
        yield
        logger.info("startup_phase_done", phase=name)
    except Exception as e:
        logger.error("startup_phase_failed", phase=name, error=str(e))
        raise

@dataclass
class InfraContext:
    es: ESClient | None = None
    mongo: MongoClient | None = None
    redis: RedisClient | None = None
    email: EmailAlertClient | None = None
    zk: ZKClient | None = None

    async def close_partial(self):
        """역순으로 안전하게 정리."""
        for name, client in [
            ("zk", self.zk), ("email", self.email), ("redis", self.redis),
            ("mongo", self.mongo), ("es", self.es),
        ]:
            if client is not None:
                try:
                    await client.close()
                except Exception as e:
                    logger.warning(f"{name}_close_failed", error=str(e))

async def init_infra(settings) -> InfraContext:
    ctx = InfraContext()
    try:
        async with startup_phase("infra_connect"):
            results = await asyncio.gather(
                ESClient(settings).connect(),
                MongoClient(settings).connect_with_retry(),
                RedisClient(settings).connect(),
                EmailAlertClient(settings).connect(),
                return_exceptions=True,
            )
            # 중요도별 분류 (ES/Mongo 치명, Redis/Email 강등 허용)
            ...
            ctx.es, ctx.mongo, ctx.redis, ctx.email = ...

        async with startup_phase("zk_connect"):
            ctx.zk = ZKClient(settings)
            await ctx.zk.connect()

        return ctx
    except Exception:
        await ctx.close_partial()
        raise
```

### 8.2 main.py — thin lifespan + 미들웨어

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging_minimal()
    settings = get_settings()
    setup_logging(settings)
    instance_id = settings.instance_id or socket.gethostname()
    structlog.contextvars.bind_contextvars(instance_id=instance_id)

    infra: InfraContext | None = None
    try:
        async with startup_phase("init_infra"):
            infra = await init_infra(settings)

        async with startup_phase("verify_versions"):
            await _verify_infra_versions(infra)  # ZK/Redis 버전 경고 로그

        async with startup_phase("init_repos"):
            repos = await init_repos(infra, settings)

        async with startup_phase("seed_default_profile"):
            await seed_default_profile(repos.profile_repo)

        async with startup_phase("init_distributed"):
            distributed = await init_distributed(infra, repos, settings, instance_id)

        async with startup_phase("init_scheduler"):
            scheduler = await init_scheduler(infra, repos, distributed, settings)

        # app.state에 저장
        app.state.settings = settings
        app.state.infra = infra
        app.state.repos = repos
        app.state.distributed = distributed
        app.state.scheduler = scheduler
        app.state.partition_manager = distributed.partition_mgr
        # ... 개별 클라이언트도 deps 편의용으로
        app.state.es_client = infra.es
        app.state.mongo_client = infra.mongo
        app.state.zk_client = infra.zk
        app.state.redis_client = infra.redis
        app.state.email_client = infra.email
        app.state.cooldown_manager = distributed.cooldown_mgr
        app.state.leader_election = distributed.leader_election

        async with startup_phase("scheduler_start"):
            await scheduler.start()

        logger.info("startup_complete")
        yield

    except Exception as e:
        logger.error("startup_failed", error=str(e), exc_info=True)
        await _self_alert_critical(infra, settings, f"startup_failed: {e}")
        raise

    finally:
        logger.info("shutting_down")
        # 엄격한 역순 (lambda late binding 안전)
        try:
            if hasattr(app.state, "scheduler"):
                await app.state.scheduler.shutdown(timeout=30)
            if hasattr(app.state, "partition_manager"):
                await app.state.partition_manager.stop()
            if hasattr(app.state, "leader_election"):
                await app.state.leader_election.stop()
        except Exception as e:
            logger.warning("shutdown_phase_failed", error=str(e))
        if infra is not None:
            await infra.close_partial()
        logger.info("shutdown_complete")


async def _verify_infra_versions(infra: InfraContext):
    """Redis 5.0.6 / ZK 3.5.5 운영 버전 검증 + 경고."""
    try:
        redis_info = await infra.redis.client.info("server")
        if not redis_info.get("redis_version", "").startswith("5."):
            logger.warning("redis_version_mismatch",
                          expected="5.0.x", actual=redis_info.get("redis_version"))
    except Exception:
        pass
    try:
        zk_version = await infra.zk.get_server_version()
        if "3.5." not in zk_version:
            logger.warning("zk_version_mismatch", expected="3.5.x", actual=zk_version)
    except Exception:
        pass


async def _self_alert_critical(infra, settings, msg: str):
    """서비스 자체의 치명적 오류를 운영팀에게 알림."""
    if infra is None or infra.email is None:
        return
    try:
        await infra.email.send_alert(EmailAlertRequest(
            hostname=settings.instance_id,
            ip="self",
            process="ResourceMonitorServer",
            model="self",
            line="self",
            code="RESOURCE_MONITOR_SELF",
            subcode="CRITICAL",
            variables={"MESSAGE": msg},
        ))
    except Exception as e:
        logger.error("self_alert_failed", error=str(e))


# 미들웨어
class RequestIdMiddleware(BaseHTTPMiddleware):
    SKIP_PATHS = frozenset(["/healthz/live", "/healthz/ready", "/metrics"])

    async def dispatch(self, request, call_next):
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)
        request_id = request.headers.get("X-Request-ID", str(uuid4()))
        token = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


app = FastAPI(title="ResourceMonitorServer", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.error("unhandled_exception", path=request.url.path, error=str(exc), exc_info=True)
    return JSONResponse(status_code=500, content={"error": "internal_server_error"})

app.include_router(health.router)
app.include_router(admin.router)
app.include_router(metrics.router)
```

---

## Step 9: Dockerfile + K8s + Dev Infra Bootstrap (v5 상세)

목표: 운영 배포 산출물(Dockerfile, K8s manifests) + dev/integration 테스트가 의존하는 OrbStack 인프라 부트스트랩(ZK 3.5.5, ES 7.11.9 추가, Redis 5.0.6 다운그레이드).

---

### 9.1 Pre-flight: Redis 7→5.0.6 다운그레이드 영향 검증 ★★★

Redis 다운그레이드는 ARS 전체에 영향을 줄 수 있어 **반드시 먼저 수행**.

**검증 대상 코드 위치:**
- `/Users/hyunkyungmin/Developer/ARS/docker/Dockerfile` (socks-agent / direct-agent)
- 위 컨테이너에서 사용하는 Python/Go 코드의 redis 호출 전체
- `/Users/hyunkyungmin/Developer/ARS/WebManager/server/src/**/*.{ts,js}` 의 redis 호출
- 기타 ARS 하위 프로젝트가 Redis를 사용하는지 grep

**확인 항목 (Redis 5.0.6 미지원 명령):**
| 명령 | Redis 5.0.6 | 사용 시 대안 |
|------|-------------|-------------|
| `ACL` | ❌ | password AUTH로 우회 |
| `STREAMS XREAD/XADD` | ⚠️ 5.0+ 있음 | 5.0.6 OK |
| `GETEX` | ❌ (6.2+) | GET + EXPIRE 분리 |
| `GETDEL` | ❌ (6.2+) | GET + DEL 분리 |
| `COPY` | ❌ (6.2+) | DUMP + RESTORE |
| `LMPOP/BLMPOP` | ❌ (7.0+) | LPOP/BLPOP |
| `SMISMEMBER` | ❌ (6.2+) | 여러 SISMEMBER |
| `RESP3 HELLO` | ❌ | `protocol=2` 강제 |

**검증 절차 (Step 9 본 작업 시작 전):**
1. `grep -r "GETEX\|GETDEL\|COPY\|LMPOP\|BLMPOP\|SMISMEMBER\|XADD\|XREAD\|ACL " /Users/hyunkyungmin/Developer/ARS/docker /Users/hyunkyungmin/Developer/ARS/WebManager`
2. 각 매치를 수동 검토 — 운영 코드 vs 단순 문자열 vs 주석 구분
3. 영향 있는 코드 발견 → 사용자에게 보고. 다음 옵션 제시:
   - (a) 해당 코드 수정 (5.0.6 호환 명령으로 교체)
   - (b) 다운그레이드 취소 → RMS 전용 Redis 5.0.6을 6380 포트로 별도 추가
4. 영향 없음 → 9.2로 진행

**산출물:** plan에 검증 결과 한 줄 메모 + 실행 계획 확정.

---

### 9.2 ARS docker-compose.yml 변경 (ZK, ES 추가 + Redis 다운그레이드)

**대상 파일**: `/Users/hyunkyungmin/Developer/ARS/docker/docker-compose.yml`

**주의**: 이 파일은 ARS root에 있고, RMS subproject에 hooks 정책이 걸려 있을 수 있음. **plan 실행 시 이 파일을 RMS context에서 수정 시도 → 차단되면 사용자에게 ARS root context에서 직접 수정 요청**.

**변경 내용:**

```yaml
version: '3.8'

services:
  redis:
    image: redis:5.0.6-alpine            # ★ 7-alpine → 5.0.6-alpine
    container_name: ars-redis
    ports:
      - "6379:6379"
    command: redis-server --databases 16
    networks:
      - ars-net
    restart: unless-stopped

  zookeeper:                              # ★ 신규
    image: zookeeper:3.5.5
    container_name: ars-zookeeper
    ports:
      - "2181:2181"
    environment:
      ZOO_MY_ID: 1
      ZOO_SERVERS: server.1=0.0.0.0:2888:3888;2181
      ZOO_4LW_COMMANDS_WHITELIST: stat,ruok,conf,isro,mntr
      ZOO_TICK_TIME: 2000
    networks:
      - ars-net
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "echo ruok | nc -w 2 localhost 2181 | grep imok"]
      interval: 10s
      timeout: 5s
      retries: 5

  elasticsearch:                          # ★ 신규
    image: docker.elastic.co/elasticsearch/elasticsearch:7.11.2  # 7.11.9 태그 미존재 시 7.11.2
    container_name: ars-elasticsearch
    ports:
      - "9200:9200"
      - "9300:9300"
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - ES_JAVA_OPTS=-Xms512m -Xmx512m
    ulimits:
      memlock:
        soft: -1
        hard: -1
    networks:
      - ars-net
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health | grep -E '\"status\":\"(green|yellow)\"'"]
      interval: 10s
      timeout: 5s
      retries: 10

  socks-agent:
    # ... 기존 그대로
  direct-agent:
    # ... 기존 그대로

networks:
  ars-net:
    driver: bridge
```

**ES 이미지 태그 확인:**
- Elastic 공식 docker registry에서 7.11.9 태그가 실제 존재하는지 확인 (구현 시 `docker pull docker.elastic.co/elasticsearch/elasticsearch:7.11.9`)
- 미존재 시 가장 가까운 패치 (7.11.2 등) + plan에 명시 + 운영팀 통보

**ZK 이미지 태그 확인:**
- `docker pull zookeeper:3.5.5` 가능 여부 확인
- 미존재 시 `zookeeper:3.5.6` 또는 `zookeeper:3.5` 폴백

---

### 9.3 RMS Dockerfile (multi-stage)

**대상 파일**: `/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip wheel --no-deps --wheel-dir=/wheels . \
    && pip wheel --wheel-dir=/wheels \
       fastapi 'uvicorn[standard]' pydantic pydantic-settings \
       'elasticsearch[async]>=7.11.0,<8.0.0' motor \
       'apscheduler>=3.10.0,<4.0.0' \
       'kazoo>=2.9.0,<2.11.0' \
       'redis[hiredis]>=4.5.0,<5.1.0' \
       httpx structlog prometheus-client cachetools

FROM python:3.11-slim AS runtime
WORKDIR /app
RUN adduser --disabled-password --gecos '' --uid 1000 appuser \
    && mkdir -p /tmp \
    && chown -R appuser:appuser /app /tmp
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels
COPY --chown=appuser:appuser src/ ./src/
USER appuser
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz/live').read()" || exit 1
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**.dockerignore** (`/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/.dockerignore`):
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
tests/
docs/
*.md
.git/
.env
.env.*
```

---

### 9.4 K8s manifests

**대상 디렉토리**: `/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/k8s/`

생성 파일:
- `k8s/configmap.yaml`
- `k8s/secret.yaml.example` (커밋, 실제 값 없음)
- `k8s/deployment.yaml`
- `k8s/service.yaml`
- `k8s/pdb.yaml`

#### 9.4.1 configmap.yaml
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: resource-monitor-config
data:
  MONITOR_ES_HOSTS: "http://elasticsearch.observability:9200"
  MONITOR_ES_USERNAME: "elastic"
  MONITOR_ES_USE_SSL: "false"
  MONITOR_ES_REQUEST_TIMEOUT: "30"
  MONITOR_MONGO_DB: "EARS"
  MONITOR_ZK_HOSTS: "zookeeper-0.zookeeper:2181,zookeeper-1.zookeeper:2181,zookeeper-2.zookeeper:2181"
  MONITOR_ZK_ROOT_PATH: "/resource-monitor"
  MONITOR_ZK_SESSION_TIMEOUT: "30"
  MONITOR_REDIS_URL: "redis://redis.cache:6379/0"
  MONITOR_REDIS_KEY_PREFIX: "RESOURCE_ALERT"
  MONITOR_EMAIL_API_URL: "http://httpwebserver.notification:8080/EmailNotify"
  MONITOR_EMAIL_API_TIMEOUT: "10"
  MONITOR_GRAFANA_BASE_URL: "https://grafana.factory.local"
  MONITOR_LOG_LEVEL: "INFO"
  MONITOR_LOG_FORMAT: "json"
  MONITOR_LOCAL_TZ: "Asia/Seoul"
```

#### 9.4.2 secret.yaml.example
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: resource-monitor-secrets
type: Opaque
stringData:
  MONITOR_MONGO_URI: "mongodb://user:CHANGE_ME@mongodb.ears:27017"
  MONITOR_ES_PASSWORD: "CHANGE_ME"
  MONITOR_REDIS_PASSWORD: "CHANGE_ME"
  MONITOR_ZK_SASL_PASSWORD: ""  # 빈 값이면 SASL 미사용
```

#### 9.4.3 deployment.yaml
- replicas: 1 (Phase 0). 멀티 인스턴스는 Phase 1+에서 검증 후 증가
- terminationGracePeriodSeconds: 60
- securityContext: runAsNonRoot, runAsUser 1000, readOnlyRootFilesystem, capabilities drop ALL, allowPrivilegeEscalation false, seccompProfile RuntimeDefault
- envFrom: configmap + secret
- env: MONITOR_INSTANCE_ID ← fieldRef metadata.name
- resources: req mem 512Mi cpu 200m / lim mem 1Gi cpu 500m
- volumeMounts: /tmp emptyDir
- livenessProbe: /healthz/live, initialDelay 60, period 30, failureThreshold 3, timeout 5
- readinessProbe: /healthz/ready, initialDelay 15, period 10, failureThreshold 6, timeout 3
- lifecycle preStop: sleep 5

#### 9.4.4 service.yaml
```yaml
apiVersion: v1
kind: Service
metadata:
  name: resource-monitor-server
  labels: { app: resource-monitor-server }
spec:
  type: ClusterIP
  ports:
    - port: 8000
      targetPort: 8000
      name: http
  selector: { app: resource-monitor-server }
```

#### 9.4.5 pdb.yaml
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: resource-monitor-pdb
spec:
  maxUnavailable: 0
  selector:
    matchLabels: { app: resource-monitor-server }
```

---

### 9.5 Makefile 보강

**대상 파일**: `/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/Makefile` (기존 파일에 target 추가)

```make
ARS_COMPOSE := /Users/hyunkyungmin/Developer/ARS/docker/docker-compose.yml

dev-up:                                  ## OrbStack에 ZK + ES + Redis 5.0.6 기동
	docker-compose -f $(ARS_COMPOSE) up -d redis zookeeper elasticsearch
	@echo "waiting for ZK + ES healthy..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		docker exec ars-zookeeper sh -c 'echo ruok | nc -w 2 localhost 2181' 2>/dev/null | grep -q imok \
		&& curl -sf http://localhost:9200/_cluster/health > /dev/null \
		&& echo "ready" && break; \
		echo "...$$i"; sleep 3; done

dev-down:
	docker-compose -f $(ARS_COMPOSE) stop zookeeper elasticsearch
	# Redis는 다른 프로젝트도 쓰므로 stop 안 함

dev-status:
	@docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' \
		| grep -E 'ars-(redis|zookeeper|elasticsearch)|mongodb-44'

dev-clean-test:                          ## 테스트 namespace 잔재 청소
	@docker exec mongodb-44 mongo --quiet --eval 'db.adminCommand("listDatabases").databases.filter(d => d.name.match(/^EARS_test_/)).forEach(d => db.getSiblingDB(d.name).dropDatabase())'
	@docker exec ars-redis redis-cli --scan --pattern 'RESOURCE_ALERT_test_*' | xargs -r docker exec -i ars-redis redis-cli del
	@docker exec ars-zookeeper zkCli.sh deleteall /resource-monitor-test 2>/dev/null || true
```

---

### 9.6 Verification (Step 9 단독)

```bash
# 1. Pre-flight grep (9.1)
grep -rE "GETEX|GETDEL|COPY|LMPOP|BLMPOP|SMISMEMBER" \
  /Users/hyunkyungmin/Developer/ARS/docker \
  /Users/hyunkyungmin/Developer/ARS/WebManager 2>/dev/null

# 2. ARS compose 변경 후 기동
cd /Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer
make dev-up
make dev-status   # ars-redis(5.0.6) + ars-zookeeper + ars-elasticsearch + mongodb-44 모두 healthy

# 3. 연결 smoke
curl -s http://localhost:9200 | jq .version.number   # 7.11.x
echo ruok | nc -w 2 localhost 2181                   # imok
docker exec ars-redis redis-cli INFO server | grep redis_version  # 5.0.6
docker exec mongodb-44 mongo --eval 'db.version()'   # 4.4.30

# 4. Docker build
docker build -t resource-monitor-server:dev .
docker run --rm resource-monitor-server:dev python -c "import src.main; print('ok')"

# 5. K8s dry-run
kubectl apply --dry-run=client -f k8s/configmap.yaml -f k8s/deployment.yaml -f k8s/service.yaml -f k8s/pdb.yaml

# 6. 기존 unit test 회귀
.venv/bin/python -m pytest tests/unit -q  # 202 passed
```

---

## Step 10: 통합/E2E 테스트 — OrbStack 기반 (v5 상세)

목표: Step 9에서 부트스트랩한 OrbStack 인프라(Mongo/Redis/ZK/ES)에 직접 연결해 통합 시나리오를 검증. testcontainers 사용 안 함. 같은 환경이 Phase 1+ 분석 로직 개발/검증에도 그대로 재사용됨.

### 10.0 설계 원칙

1. **순수성 vs 현실성**: testcontainers 같은 격리된 인스턴스 대신 long-lived OrbStack 사용. 격리는 namespace prefix로 확보.
2. **테스트 = dev = 디버깅 환경**: 통합 테스트를 돌리던 환경 그대로 `uvicorn` 실행 → 디버깅 즉시 가능.
3. **CI 호환**: CI에서도 같은 docker-compose 부트 가능 (GitHub Actions의 services 또는 docker-compose 직접 호출).
4. **실패 격리**: 각 test run은 UUID prefix로 분리. 한 run이 죽어도 다음 run은 영향 없음.
5. **자가 정리**: session 종료 시 모든 namespace 리소스 자동 drop. `make dev-clean-test`로 수동 정리도 제공.

### 10.1 conftest 구조

**대상 파일**: `/Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer/tests/integration/conftest.py`

```python
"""Integration test fixtures — OrbStack 기반.

전제: `make dev-up` 으로 ARS docker-compose의 redis/zookeeper/elasticsearch + 단독
mongodb-44 가 기동되어 있음.

격리 전략:
- session 시작 시 UUID 기반 run_id 생성
- Mongo/Redis/ES/ZK 모두 run_id 포함 namespace
- session 종료 autouse cleanup
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from elasticsearch import AsyncElasticsearch
from kazoo.client import KazooClient
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

# ----- 환경 변수 (개발자 PC default) -----
ES_HOSTS = os.getenv("TEST_ES_HOSTS", "http://localhost:9200")
MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")  # DB 15 = 테스트 전용
ZK_HOSTS = os.getenv("TEST_ZK_HOSTS", "localhost:2181")


# ----- session 스코프: 한 번의 pytest run 동안 유일한 ID -----
@pytest.fixture(scope="session")
def run_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def ns(run_id):
    """Namespace 객체. 모든 prefix를 한 곳에서."""
    class NS:
        mongo_db = f"EARS_test_{run_id}"
        redis_prefix = f"RESOURCE_ALERT_test_{run_id}"
        es_index_prefix = f"test_{run_id}_"
        zk_root = f"/resource-monitor-test-{run_id}"
    return NS


# ----- session 스코프: 실 인프라 클라이언트 -----
@pytest_asyncio.fixture(scope="session")
async def real_es() -> AsyncIterator[AsyncElasticsearch]:
    client = AsyncElasticsearch(hosts=[ES_HOSTS], timeout=10)
    if not await client.ping():
        pytest.skip("Elasticsearch not available — run `make dev-up`")
    yield client
    await client.close()


@pytest_asyncio.fixture(scope="session")
async def real_mongo() -> AsyncIterator[AsyncIOMotorClient]:
    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        await client.admin.command("ping")
    except Exception:
        pytest.skip("MongoDB not available — start mongodb-44 in OrbStack")
    yield client
    client.close()  # ← motor: sync


@pytest_asyncio.fixture(scope="session")
async def real_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis not available — run `make dev-up`")
    yield client
    await client.aclose()


@pytest.fixture(scope="session")
def real_zk() -> KazooClient:
    client = KazooClient(hosts=ZK_HOSTS, timeout=10)
    try:
        client.start(timeout=5)
    except Exception:
        pytest.skip("Zookeeper not available — run `make dev-up`")
    yield client
    client.stop()
    client.close()


# ----- session 스코프: in-process Email mock 서버 -----
@pytest_asyncio.fixture(scope="session")
async def mock_email_server():
    """aiohttp test server with /EmailNotify endpoint."""
    from aiohttp import web
    received: list[dict] = []

    async def handler(request: web.Request):
        received.append(await request.json())
        return web.json_response({"result": "success", "message": "send ok"})

    app = web.Application()
    app.router.add_post("/EmailNotify", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield {"url": f"http://127.0.0.1:{port}/EmailNotify", "received": received}
    await runner.cleanup()


# ----- session autouse: 종료 시 namespace 청소 -----
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _cleanup_namespace(real_es, real_mongo, real_redis, real_zk, ns):
    yield
    # ES 인덱스 삭제
    try:
        await real_es.indices.delete(index=f"{ns.es_index_prefix}*", ignore=[404])
    except Exception:
        pass
    # Mongo DB drop
    try:
        await real_mongo.drop_database(ns.mongo_db)
    except Exception:
        pass
    # Redis 키 삭제
    try:
        async for key in real_redis.scan_iter(f"{ns.redis_prefix}*"):
            await real_redis.delete(key)
    except Exception:
        pass
    # ZK 트리 삭제
    try:
        if real_zk.exists(ns.zk_root):
            real_zk.delete(ns.zk_root, recursive=True)
    except Exception:
        pass


# ----- function 스코프: settings 오버라이드 -----
@pytest.fixture
def integration_settings(ns, mock_email_server):
    """integration test용 AppSettings (real infra + namespace)."""
    from src.config.settings import AppSettings, get_settings
    get_settings.cache_clear()
    s = AppSettings(
        es_hosts=[ES_HOSTS],
        mongo_uri=MONGO_URI,
        mongo_db=ns.mongo_db,
        zk_hosts=ZK_HOSTS,
        zk_root_path=ns.zk_root,
        redis_url=REDIS_URL,
        redis_key_prefix=ns.redis_prefix,
        email_api_url=mock_email_server["url"],
        instance_id=f"test-{ns.zk_root[-8:]}",
        log_format="console",
    )
    yield s
    get_settings.cache_clear()
```

---

### 10.2 통합 테스트 파일별 시나리오

#### tests/integration/test_es_real.py (★ 5개 시나리오)
1. ping + introspect_field_type — 존재하는 필드는 타입 반환
2. introspect_field_type — 존재하지 않는 인덱스에 NotFoundError fallback "unknown"
3. resolve_index_range — 자정 경계 시 콤마 결합 인덱스 반환
4. 실제 인덱스 생성 → 7.11 API `body=` 파라미터로 search → raw dict 응답 검증
5. 인덱스 삭제 후 재호출 — 캐시된 "unknown" 사용

#### tests/integration/test_mongo_real.py (★ 6개 시나리오)
1. connect_with_retry 정상 동작
2. ProfileRepository.create → DuplicateKeyError → ProfileAlreadyExistsError 변환
3. Scope(model="ABC").to_mongo_query() → `{"eqpModel": "ABC"}` 검증 (alias)
4. MonitorProfile to_mongo/from_mongo 왕복 (ObjectId ↔ str)
5. EqpInfoRepository.get_distinct_processes — `onoff=0` 장비 제외 검증 (테스트 데이터로 0/1 섞어 삽입)
6. seed_default_profile 멱등성 — 두 번 호출해도 변경 1번만

#### tests/integration/test_redis_real.py (★ 4개 시나리오)
1. RedisClient connect → ping
2. AlertCooldownManager.set_cooldown / is_cooling_down 사이클 (TTL 만료까지 대기)
3. is_cooling_down_batch — pipeline 동작
4. Redis 5.0.6 protocol=2 강제 — `client.connection_pool.connection_kwargs["protocol"] == 2` 또는 HELLO 실패 검증

#### tests/integration/test_zk_real.py (★ 5개 시나리오)
1. ZKClient connect → ping → close
2. Transaction.set_data atomic — 일부 실패 시 전체 롤백
3. ChildrenWatch — 자식 추가/삭제 시 콜백 호출 (asyncio 브릿지 검증)
4. DataWatch with **빈 노드(`ensure_path`로 만든 b'')** — JSONDecodeError 없이 콜백 정상 종료 (G5 회귀 가드)
5. get_server_version — 4lw whitelist 정상 시 버전 / 차단 시 "unknown" 폴백

#### tests/integration/test_zk_lost_recovery.py (★★★ 핵심 — 3개 시나리오)
1. **LeaderElection LOST 후 재시작**: 인스턴스가 leader 됨 → `kazoo.client._session._session.expire()` 또는 `client.stop()` + `client.start()` 로 세션 강제 만료 → 재연결 → epoch 증가 + leader 재취득 (G4 회귀 가드)
2. **PartitionManager `_reinit_after_loss` 호출**: LOST → CONNECTED 시퀀스에서 ephemeral member 노드 재생성 검증
3. **watch_epoch idempotency**: `_register_watches()` 두 번 호출 후 옛 콜백이 발화해도 state mutation 없음 (G6 회귀 가드)

#### tests/integration/test_partition_real.py (★★ 4개 시나리오)
1. 같은 프로세스에서 2개 ZKClient 인스턴스 (instance_id 다름) 시작 → 둘 다 members에 등록
2. 라운드로빈 분배 검증 — 4 process를 2 instance에 2/2로 나눔
3. 멤버십 변화 디바운스 — 빠른 join/leave 폭탄 후 최종 1회만 redistribution
4. stale assignment 거부 — 옛 epoch 데이터를 직접 set_data 했을 때 무시 (G의 epoch+ts 가드)

#### tests/integration/test_cooldown_degraded.py (★★ 3개 시나리오)
1. 정상 모드: Redis에 SETEX → is_cooling_down True
2. **degraded 모드**: `docker stop ars-redis` (또는 RedisClient를 끊김 상태로 만듦) → set_cooldown은 local TTLCache에 기록 → is_cooling_down도 local에서 True 반환 → **이메일 폭주 없음**
3. Redis 복구: `docker start ars-redis` → 새 set_cooldown은 Redis에 저장. 단, **2번에서 만들어진 local 키는 그대로 cooling 상태 유지** (정확성 > 청소)
   - `ars-redis` 컨테이너 stop/start는 fixture가 아닌 테스트 함수 안에서 직접 subprocess 호출 (다른 테스트에 영향 안 가도록 finalize)

#### tests/integration/test_lifespan_real.py (★★ 2개 시나리오)
1. **FastAPI lifespan 11단계 startup**: `httpx.AsyncClient(app=app)` 로 TestClient 띄움 → /healthz/live 200 → /healthz/ready 200 (모든 5개 ping 통과)
2. **/admin/status**: leader_epoch + assigned_processes 표시 + scheduled_jobs

#### tests/integration/test_email_mock.py (★ 3개 시나리오)
1. mock 서버가 `{"result":"success"}` → `EmailClient.send_alert()` True
2. mock 서버가 `{"result":"fail"}` → False
3. mock 서버가 의도적으로 timeout → False (EmailClient의 5가지 예외 분기 중 하나)

---

### 10.3 (선택) tests/e2e/ 멀티 인스턴스 페일오버

**대상 파일**: `tests/e2e/test_failover.py`

Phase 0 필수는 아님. 시간 여유 시 추가:

1. subprocess.Popen 으로 RMS 인스턴스 2개 띄움 (instance_id 다름, 같은 ZK root)
2. 30초 안정화 대기
3. 각 인스턴스의 /admin/status 호출 → leader 1개, 파티션 분배 확인
4. leader 인스턴스 SIGTERM → 60초 안에 다른 쪽이 leader 됨 + 모든 process 흡수
5. 죽인 인스턴스 재시작 → 다시 일부 process 분배받음
6. 모든 subprocess kill + 청소

**왜 선택**: 이 시나리오는 분산 모듈 단위 테스트(test_partition_real.py, test_zk_lost_recovery.py)로 충분히 커버됨. 멀티 프로세스 검증은 운영에 가까운 추가 보장.

---

### 10.4 Makefile 보강

```make
test-integration:
	$(MAKE) dev-up
	.venv/bin/python -m pytest tests/integration -m integration -v

test-e2e:
	$(MAKE) dev-up
	.venv/bin/python -m pytest tests/e2e -m e2e -v

test-full: test-fast test-integration
```

`pyproject.toml` 의 markers는 이미 `unit / integration / e2e / slow` 정의됨.

---

### 10.5 Verification (Step 10 단독)

```bash
# 1. dev infra 기동
cd /Users/hyunkyungmin/Developer/ARS/ResourceMonitorServer
make dev-up
make dev-status

# 2. 전체 통합 실행
make test-integration   # 30~60s 예상

# 3. 카테고리별 격리 확인
.venv/bin/python -m pytest tests/integration/test_zk_lost_recovery.py -v
.venv/bin/python -m pytest tests/integration/test_cooldown_degraded.py -v  # Redis stop/start 포함

# 4. namespace 잔재 청소 확인
make dev-clean-test
docker exec mongodb-44 mongo --quiet --eval 'db.adminCommand("listDatabases").databases.filter(d => d.name.startsWith("EARS_test_"))'  # []

# 5. unit 회귀
make test-fast   # 202 + integration 추가 = 230~240
```

**완료 기준**:
- 통합 시나리오 30+ 케이스 모두 통과
- LOST recovery / cooldown degraded / lifespan 11-phase 셋이 핵심 (통과 시 분산 코드 신뢰도 ↑)
- namespace cleanup 검증 (잔재 0건)
- unit + integration 합계 5분 이내

---

## 사전 확인 사항 (운영팀 체크리스트)

구현 착수 전에 운영팀과 확인이 필요한 항목들. 각 항목은 `init_infra()` startup 시 경고 로그로도 감지되지만, 사전 확인이 배포 실패를 막는다.

### 1. ZK 4lw 명령어 whitelist
**왜 필요한가**: `/admin/status` 의 `get_server_version()` + startup `_verify_infra_versions()` 가 `stat` 명령을 사용.

**확인 방법** (개발 PC에서):
```bash
# stat 응답이 오면 허용됨
echo stat | nc <zk_host> 2181
echo ruok | nc <zk_host> 2181  # 응답: imok
echo conf | nc <zk_host> 2181

# 또는 zoo.cfg 직접 확인
grep 4lw /path/to/zoo.cfg
# 기대: 4lw.commands.whitelist=stat,ruok,conf,isro  또는  4lw.commands.whitelist=*
```

**결과별 대응**:
- **허용**: 그대로 진행. `/admin/status`에 `zk_server_version` 표시됨
- **차단**: `"unknown"` 반환 + 경고 로그. 기능은 정상 동작. 운영팀에 whitelist 추가 요청 권장

### 2. ZK SASL 인증 사용 여부
**왜 필요한가**: ZK가 SASL로 보호되어 있으면 kazoo `sasl_options` 없이 연결 실패.

**확인 방법**:
```bash
# authProvider 라인이 있으면 인증 사용 중
grep -E "authProvider|sasl" /path/to/zoo.cfg
# 예: authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider

# JAAS 파일 확인 (SASL 사용 시)
grep -E "requireClientAuthScheme" /path/to/zoo.cfg
ls -la /path/to/jaas.conf 2>/dev/null

# ZK 실행 환경 확인
ps -ef | grep zookeeper | grep -oE "java.security.auth.login.config=[^ ]+"
```

**결과별 대응**:
- **SASL 미사용 (일반적)**: `MONITOR_ZK_SASL_MECHANISM=""` 빈 값 유지. 플레인 TCP 연결
- **SASL 사용**: 운영팀에서 credential(username/password + mechanism) 발급받아 K8s Secret에 저장

### 3. EARS DB 컬렉션 충돌
**확인 완료** (SCHEMA.md): 기존 7개 컬렉션에 `RESOURCE_MONITOR_*` 없음 → 신규 생성 가능.

**확인 명령**:
```bash
mongosh --eval "use EARS; db.getCollectionNames().filter(n => n.startsWith('RESOURCE_MONITOR'))"
# 기대: []
```

### 4. EQP_INFO 필드 존재 여부
**확인 완료** (SCHEMA.md): `process, eqpModel, eqpId, onoff, webmanagerUse` 모두 존재.

**확인 명령** (운영 DB 직접):
```bash
mongosh --eval '
use EARS;
db.EQP_INFO.findOne({}, {process:1, eqpModel:1, eqpId:1, onoff:1, webmanagerUse:1, _id:0})
'
# 기대: 모든 필드 값 있음. 없으면 기존 장비 데이터에 누락 존재 → 시드/마이그레이션 필요
```

### 5. ES 7.11.9 쿼리 호환성
**확인 필요**: 운영 ES 인덱스 템플릿에 실제 메트릭 필드들이 어떤 타입인지.

**확인 명령**:
```bash
# 인덱스 패턴 샘플 매핑 조회
curl -X GET "http://<es_host>:9200/<process>_all-2026.04.06/_mapping?pretty" \
  -u "${ES_USER}:${ES_PASS}"

# 핵심 확인: category, proc, metric, value 필드 타입
# category, proc, metric이 "keyword" 또는 "text"+"keyword" subfield 인지
# value가 "long"/"double"/"float" 인지
```

**결과별 대응**:
- `text` 타입: `.keyword` subfield로 집계 쿼리 (introspect가 자동 감지)
- `keyword` 타입: 필드명 그대로 사용
- `value`가 `text`: 집계 불가 → 매핑 변경 요청 필요

### 6. Email API 실제 엔드포인트
**확인 완료** (소스 코드): 응답 `{"result":"success"|"fail", "message":"..."}`

**확인 명령** (엔드포인트 동작):
```bash
curl -v -X POST "http://<httpwebserver>:<port>/EmailNotify" \
  -H "Content-Type: application/json" \
  -d '{"hostname":"test","ip":"1.1.1.1","process":"TEST","model":"TEST","line":"TEST",
       "code":"RESOURCE_MONITOR_SELF","subcode":"HEALTH_CHECK","variables":{}}'
# 기대: 200 + {"result":"success","message":"send ok"}
```

### 7. Redis 5.0.6 password 설정 여부
**확인 명령**:
```bash
redis-cli -h <redis_host> -p 6379 ping
# NOAUTH 오류 → password 사용 중
# 또는 redis.conf 확인
grep ^requirepass /path/to/redis.conf
```

**결과별 대응**:
- password 사용: `MONITOR_REDIS_PASSWORD` 설정
- password 미사용: 빈 값 유지

### 8. MongoDB 버전 + 인덱스
```bash
mongosh --eval "db.version()"
mongosh --eval "use EARS; db.EQP_INFO.getIndexes()"
# 기대: { process:1 }, { process:1, eqpModel:1 }, { eqpId:1 } 존재
```

---

## 구현 순서 + 복잡도

| # | 단계 | 복잡도 | v4 추가/변경 |
|---|------|--------|-------------|
| 0 | 스켈레톤 + pytest + Makefile + CI | Low | cachetools 의존성 |
| 1 | 설정 + 로깅 (two-phase) + SASL/AUTH | Low | - |
| 2 | ES + queries + introspect | Medium | **ES 7.11.9 API (http_auth, timeout, body=, dict response)** |
| 3 | MongoDB + 모델 + 리포지토리 | Medium | **eqpModel alias, onoff 필터, motor close 동기, TTLCache bounded** |
| 4 | Redis + Cooldown (5.0.6) | Low | **local fallback cooldown (이메일 폭주 방지)** |
| 5 | Email | Low | **응답 `"success"` 소문자 정정 + message 로깅** |
| 6 | ZK + 리더 + 락 + 파티션 (3.5.5) | **Very High** | **LeaderElection restart_after_loss, watches 멱등성, DataWatch 빈 노드 가드, 4lw 폴백** |
| 7 | Health(live/ready) + Admin + Metrics + Scheduler | Medium | - |
| 8 | startup/ 분해 + main + 미들웨어 + self-alert | Medium | - |
| 9 | Dockerfile + K8s (PDB, securityContext, Secret) | Low | - |
| 10 | 통합 + E2E | Medium | **ES 7.11.9 컨테이너, local fallback 테스트, LOST 후 재선출** |

---

## 리스크 매트릭스 (v3 누적)

| 카테고리 | 리스크 | v3 대응 |
|---------|------|--------|
| ZK 3.5.5 | LOST 후 watches/ephemeral 자동 재등록 안됨 | `_reinit_after_loss()` 명시적 재구성 |
| ZK 3.5.5 | session_timeout 30s ≤ 20×tickTime 제약 | 운영 ZK tickTime 검증 + 문서화 |
| Redis 5.0.6 | ACL 없음, RESP3 없음 | 단순 password AUTH + protocol=2 |
| Redis 5.0.6 | EOL — 보안 패치 없음 | runbook에 명시 |
| kazoo | Election.run() 블로킹 | 별도 ThreadPoolExecutor + threading.Event |
| kazoo | Lock 비재진입 | 매번 새 Lock 객체 + asyncio.Lock per process |
| kazoo | Transaction set_data NoNodeError | ensure_path 사전 보장 |
| asyncio | 이벤트 루프 종료 중 콜백 | loop.is_closed() 가드 |
| asyncio | gather 부분 실패 누수 | return_exceptions + 중요도 분류 + finally close_partial |
| FastAPI | lifespan 거대 try/finally | startup/ 분해 + startup_phase context |
| FastAPI | health probe 인프라 ping 시간 | 2초 timeout + CancelledError 전파 |
| K8s | liveness/readiness 동일 → 재시작 루프 | /healthz/live + /healthz/ready 분리 |
| K8s | terminationGracePeriod 30s 부족 | 60s + PreStop sleep 5s |
| K8s | 단일 replica drain | PodDisruptionBudget maxUnavailable=0 |
| K8s | OOMKilled (20K bucket agg) | memory limit 1Gi |
| K8s | root 컨테이너 | runAsNonRoot + readOnlyRootFilesystem |
| 운영 | 자기 자신 죽음 미감지 | _self_alert_critical |
| 운영 | 파티션/job 외부 가시성 | /admin/status + /metrics |
| 운영 | cooldown 수동 해제 | DELETE /admin/cooldowns/{...} |
| 운영 | ConfigMap 평문 시크릿 | Secret 분리 |
| 분산 | 디바운스 이벤트 누락 | Task cancel/recreate 패턴 |
| 분산 | epoch 재시작 0 리셋 | ZK persistent 노드에 영속화 |
| 분산 | stale assignment | epoch + assigned_at 복합 비교 |
| MongoDB | resolve_profile N+1 부하 | dot-notation + TTL 캐시 (5분) |
| MongoDB | seed 매번 upsert false change | hash 비교 후 조건부 upsert |
| MongoDB | _id ObjectId 변환 버그 | to_mongo/from_mongo 왕복 테스트 |
| ES | .keyword 매핑 함정 | introspect lazy + 캐싱 |
| ES | 자정 경계 인덱스 누락 | resolve_index_range 2개 인덱스 |
| ES | shard_size 미지정 → 누락 | size + shard_size 명시 |
| ES | 5분 주기 부하 | Semaphore(3) + max_concurrent_shard_requests |
| 테스트 | TDD 사이클 깨짐 (컨테이너 기동 30s+) | unit/integration/e2e 분리 + Makefile test-fast |
| 테스트 | kazoo 브릿지 검증 부재 | threading.Event 패턴 |
| **v4: ES** | **ES 8.x API 코드 → 7.11.9 연결 실패** | **http_auth, timeout, body=, raw dict — pyproject 버전 핀 7.x** |
| **v4: Email** | **응답 "Success" 비교 항상 False → 발송 성공 감지 불가** | **`"success"` 소문자 비교로 정정** |
| **v4: Scope** | **Pydantic `model` 예약어 + EQP_INFO 필드명 불일치** | **`eqp_model = Field(alias="model")` + 매핑** |
| **v4: Mongo** | **비활성 장비(onoff=0) 분석 → 오탐** | **get_distinct_processes에 `{onoff:1, webmanagerUse:1}` 필터** |
| **v4: Mongo** | **motor close() 동기인데 await → TypeError** | **`self._client.close()` (await 제거)** |
| **v4: Mongo** | **TTL 캐시 unbounded → OOMKilled** | **`cachetools.TTLCache(maxsize=10000, ttl=300)`** |
| **v4: Redis** | **다운 시 degraded False → 매 cycle 이메일 폭주** | **local in-memory fallback cooldown (TTLCache)** |
| **v4: ZK** | **LOST 후 Election 객체 재사용 불가 → 리더 재선출 안됨** | **`LeaderElection.restart_after_loss()` 새 Election 객체 생성** |
| **v4: ZK** | **`_register_watches()` 재호출 시 listener 누수** | **watch_epoch 카운터로 stale 콜백 False 반환 → kazoo 재등록 중단** |
| **v4: ZK** | **DataWatch 빈 노드(ensure_path 직후) → JSONDecodeError** | **`if data is None or len(data) == 0: return` 가드** |
| **v4: ZK** | **`get_server_version()` 4lw whitelist 차단 시 예외 누수** | **try/except + timeout + "unknown" 폴백** |
| **v4: 운영** | **배포 후 첫 실행에서 ES 매핑/컬렉션 명/SASL 불일치** | **사전 확인 사항 체크리스트 (운영팀 협업)** |

---

## 검증 방법

### 단위
```bash
make test-fast                                    # < 5초
pytest tests/unit -m unit --cov=src --cov-fail-under=80
```

### 통합 (OrbStack — v5)
```bash
make dev-up                                        # ARS docker-compose의 redis(5.0.6)+zookeeper(3.5.5)+elasticsearch(7.11.x) + mongodb-44 (단독)
make dev-status                                    # 4개 서비스 healthy 확인
make test-integration                              # 30~60s, namespace 격리
```

### E2E (선택, v5)
```bash
make test-e2e                                      # subprocess 멀티 인스턴스 페일오버
```

### namespace 잔재 청소
```bash
make dev-clean-test                                # EARS_test_*, RESOURCE_ALERT_test_*, /resource-monitor-test-* 제거
```

### Docker 빌드
```bash
docker build -t resource-monitor-server:latest .
docker run --rm resource-monitor-server:latest python -c "import src.main"
```

### K8s 배포
```bash
kubectl apply -f k8s/configmap.yaml -f k8s/secret.yaml -f k8s/pdb.yaml -f k8s/deployment.yaml -f k8s/service.yaml
kubectl rollout status deployment/resource-monitor-server
kubectl exec deploy/resource-monitor-server -- curl -s localhost:8000/healthz/ready
kubectl exec deploy/resource-monitor-server -- curl -s localhost:8000/admin/status
kubectl exec deploy/resource-monitor-server -- curl -s localhost:8000/metrics | head
```

### PRD 완료 기준 10개 + 추가 검증
1. PRD §13의 1~10번 항목
2. `/healthz/live` 200 + `/healthz/ready` 200
3. `/admin/status`에 leader_epoch + assigned_processes 표시
4. `/metrics`에 prometheus 포맷 출력
5. ZK 3.5.5 컨테이너 강제 재시작 → SUSPENDED → CONNECTED 복구 + 분석 재개
6. ZK 세션 강제 만료 (kazoo `_session.expire`) → LOST → 재초기화 후 정상 동작
7. Redis 5.0.6 컨테이너 강제 재시작 → degraded mode → 복구
8. K8s pod kill → PreStop 5초 + scheduler shutdown 30초 → 정상 종료 (Task 강제 cancel 없음)
9. **v4: ES 7.11.9 컨테이너 연결 + `body=` 파라미터 search 성공**
10. **v4: Email API mock이 `{"result":"success"}` 반환 시 `send_alert() == True`**
11. **v4: Redis 다운 중 `set_cooldown` → `is_cooling_down` True (local fallback) — 이메일 폭주 없음**
12. **v4: ZK 세션 강제 만료 후 LeaderElection 재시작 확인 (is_leader 재취득 or 타 인스턴스 당선)**
13. **v4: `get_distinct_processes` 결과가 onoff=0 장비 제외**
14. **v4: `Scope(model="ABC123").to_mongo_query()` → `{"process":..., "eqpModel":"ABC123"}`**

---

## 핵심 파일 + 책임

| 파일 | v4 핵심 책임 |
|------|------------|
| `src/main.py` | thin lifespan + startup_phase 오케스트레이션 + 미들웨어 + 전역 핸들러 |
| `src/startup/{infra,repos,distributed,scheduler_init}.py` | lifespan 분해 (테스트 가능 단위) |
| `src/distributed/zk_client.py` | kazoo-asyncio 브릿지 + state machine + SASL + 루프 가드 + **4lw 폴백** |
| `src/distributed/leader_election.py` | fire-and-forget Election + threading.Event + epoch 영속화 + **`restart_after_loss()` 새 Election 객체 생성** |
| `src/distributed/lock.py` | 매번 새 Lock + asyncio.Lock per process + 세션 만료 흡수 |
| `src/distributed/partition_manager.py` | Transaction + ensure_path + Task cancel debounce + `_reinit_after_loss` + **watch_epoch 멱등성 + DataWatch 빈 노드 가드 + LeaderElection.restart_after_loss 호출** |
| `src/cache/cooldown.py` | Redis 5.0.6 호환 + **local in-memory fallback cooldown (이메일 폭주 방지)** + pipeline 배치 |
| `src/cache/redis_client.py` | protocol=2 + 단순 password AUTH |
| `src/db/client.py` | lazy connect + retry + **동기 `close()` (motor API)** + `ping()` |
| `src/db/repository.py` | dot-notation + **`cachetools.TTLCache(maxsize=10000)` bounded** + DuplicateKeyError → 도메인 예외 + **`EqpInfoRepository.get_distinct_processes()` onoff 필터** |
| `src/db/models.py` | AnalysisConfig 분리 + to_mongo/from_mongo + **`Scope.eqp_model = Field(alias="model")` → EQP_INFO.eqpModel 매핑** |
| `src/db/seed.py` | hash 비교 후 조건부 upsert |
| `src/scheduler/jobs.py` | job 래퍼 + Semaphore(3) + 강제 cancel + metrics 통합 |
| `src/alert/email_client.py` | **응답 `"success"` 소문자 비교 + message 필드 로깅 + 5가지 오류 케이스** |
| `src/api/health.py` | live/ready 분리 + safe_check (CancelledError 전파) |
| `src/api/admin.py` | /admin/status + /admin/cooldowns + /admin/scheduler/reload |
| `src/api/metrics.py` | Prometheus exposition |
| `src/es/queries.py` | resolve_index_range + range union baseline + shard_size |
| `src/es/client.py` | **ES 7.x API (http_auth, timeout, body=, raw dict, NotFoundError)** + lazy introspect_field_type |
| `src/config/settings.py` | env list 파싱 validator + SASL/AUTH 필드 + SecretStr |
| `src/logging_config.py` | two-phase + uvicorn 통합 |
| `pyproject.toml` | **`elasticsearch[async]>=7.11.0,<8.0.0`** + kazoo<2.11 + redis<5.1 + **`cachetools>=5.3.0`** |
| `Dockerfile` | non-root + multi-stage + healthcheck + PYTHONPATH |
| `.dockerignore` | venv/캐시/테스트/문서 제외 |
| `k8s/*.yaml` | configmap + secret 템플릿 + deployment(live/ready/security) + service + PDB |
| `Makefile` | **v5: dev-up/dev-down/dev-status/dev-clean-test + test-integration/test-e2e** |
| `ARS/docker/docker-compose.yml` | **v5 변경: redis 5.0.6-alpine 다운그레이드 + zookeeper 3.5.5 + elasticsearch 7.11.x 추가** (RMS 외부 파일) |
| `tests/integration/conftest.py` | **v5 신규: real_es/mongo/redis/zk + run_id namespace + autouse cleanup + mock_email_server** |
| `tests/integration/test_*.py` | **v5 신규: 9개 시나리오 파일 — es_real, mongo_real, redis_real, zk_real, zk_lost_recovery★, partition_real, cooldown_degraded★, lifespan_real, email_mock** |
| `tests/e2e/test_failover.py` | **v5 선택: subprocess 멀티 인스턴스 페일오버** |
