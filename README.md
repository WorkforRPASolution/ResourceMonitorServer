# ResourceMonitorServer

공장 PC(최대 20,000대)에서 ResourceAgent가 수집해 Elasticsearch에 저장한 리소스 메트릭을 주기적으로 분석하고, 이상 발생 시 기존 Email REST API로 알림을 발송하는 분산 모니터링 서비스.

> **현재 상태**: Phase 1 (분석엔진 + 알림 발송) — 임계값 기반 이상탐지, 메트릭 타입별 집계(max/state_check), 이메일 알림 발송까지 구현 완료. Debug Read-Only 모드로 운영 데이터 관찰 가능.

## 문서 맵

| 문서 | 내용 |
|------|------|
| [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md) | Phase 0 요구사항/스펙 (무엇을 만드는가) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 시스템 설계, 분산 조정, 라이브러리 버전 고정 이유, **함정/Gotchas** |
| [SCHEMA.md](SCHEMA.md) | EARS DB 컬렉션 스키마 (RESOURCE_MONITOR_PROFILE, EQP_INFO 등) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 개발 환경 설정, TDD 워크플로우, 테스트 실행, 코딩 컨벤션 |
| [docs/archive/phase0-plan-v6.md](docs/archive/phase0-plan-v6.md) | Phase 0 v6 구현 계획 (Step 0~10 + 8.5 Resilience Hardening) — 완료 보관용 |

## 핵심 아키텍처

```
ResourceAgent (Go)
  → Kafka → Elasticsearch ({process}_all-yyyy.MM.dd)
                  ↓
          ResourceMonitorServer (이 프로젝트)
            ├─ Zookeeper  : leader election + process partitioning + 분산 락
            ├─ Redis      : alert cooldown TTL (Redis 다운 시 local fallback)
            ├─ MongoDB    : 모니터링 프로파일 / 기준정보 (EARS DB)
            └─ Email API  : Akka HttpWebServer (/EmailNotify)
```

- **수평 확장**: ZK 기반 리더 선출 + process 단위 라운드로빈 파티셔닝.
- **장애 격리**: ZK 세션 LOST → 스케줄러 일시정지 → 재연결 시 새 Election 객체로 재참여 → 파티션 재배포.
- **부하 분산**: 인스턴스가 추가/삭제되면 멤버십 변화를 디바운스(2s)로 흡수 후 재배포.

자세한 흐름과 결정 사항은 [ARCHITECTURE.md](ARCHITECTURE.md) 참고.

## 빠른 시작

### 1. 사전 요구사항

| 항목 | 버전 | 비고 |
|------|------|------|
| Python | **3.11+** | 3.14에서 검증됨 |
| Elasticsearch | **7.11.9** | 8.x 미지원 (`http_auth`/`timeout` 파라미터 사용) |
| MongoDB | EARS DB | 기존 인프라 |
| Zookeeper | **3.5.5** | Kafka용 기존 인프라 |
| Redis | **5.0.6** | ACL 미지원, RESP3 미지원 (protocol=2) |

### 2. 설치

```bash
python3.11 -m venv .venv
source .venv/bin/activate
make install         # pip install -e ".[dev]"
```

### 3. 환경 변수 (`.env`)

```bash
cp .env.example .env   # 기본값(OrbStack/localhost)으로 그대로 동작
```

`.env.example` 에는 OrbStack 기준 localhost 값이 모두 들어 있고, `.env` 는 `.gitignore` 에 등록되어 있습니다. 모든 설정은 `MONITOR_` 접두사. 자세한 키는 `src/config/settings.py` 참고.

> ⚠️ **Production credential 을 `.env` 에 절대 넣지 마세요.** Production 은 K8s Secret 으로만 관리하며, 개발 PC 에는 어떤 형태로도 존재해선 안 됩니다. `.env.example` 은 localhost 값만 포함해야 하며 prod 주소의 일부라도 힌트로 적지 않습니다. 자세한 정책은 [CONTRIBUTING.md](CONTRIBUTING.md) 의 "환경 분리" 섹션 참고.

### 4. 로컬 인프라 기동

```bash
make dev-up        # ARS docker-compose: Redis 5.0.6 + ZK 3.5.5 + ES 7.11.2 + mongodb-44
make dev-status    # 4개 컨테이너 healthy 확인
```

### 5. 실행

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8080
```

### 6. Debug Read-Only 모드 (선택)

`MONITOR_DEBUG_READ_ONLY=true` 로 설정하면 RMS 가 **관찰자 모드** 로 부팅합니다. 실제 prod 데이터(운영 Mongo/ES)에 연결해서 분석 흐름을 관찰하고 싶을 때 사용하며, 모든 **쓰기 경로가 차단**되어 prod 상태를 오염시키지 않습니다.

| 구성 요소 | 정상 모드 | Debug Read-Only |
|----------|----------|---------------|
| MongoDB | 읽기 + startup 쓰기 | **읽기만** (`create_index`, `seed_default_profile` 스킵) |
| Elasticsearch | 읽기 | 읽기 (동일) |
| Zookeeper | 참여 | **연결 안 함** (`init_distributed`/`leader_election`/`partition_manager` 전부 스킵) |
| Redis | cooldown 읽기/쓰기 | **읽기만** — local TTLCache 만 사용 |
| Email API | `send_alert` 실제 발송 | **`debug_would_send_email` 로그만** |
| Scheduler | 정상 기동 + job 실행 | **정상 기동** — 분석 엔진이 ES 조회 + 임계값 비교까지 수행, breach 감지 시 `debug_would_send_email` 로그 출력 |

Debug 모드 전용 옵션:
- `MONITOR_DEBUG_PROCESSES=ETCH,CVD` — 특정 process 만 분석 대상으로 지정. 비어있으면 `EQP_INFO.get_distinct_processes()` 결과 전체 사용
- `/healthz/ready` 응답에 `debug_read_only: true` + `checks.zookeeper: "skipped_debug"` 표시
- `/admin/status` 응답에 `debug_read_only: true`, 분산 필드는 모두 `null`

> ⚠️ **절대 production K8s manifests 에 이 플래그를 넣지 마세요.** 개발 PC 전용이며, ConfigMap/Secret 어느 쪽에도 명시하지 않습니다. 잘못 활성화되면 analysis job 이 돌지 않아 **감지 공백**이 발생합니다.

자세한 설계 근거는 [CONTRIBUTING.md](CONTRIBUTING.md) 의 "Debug Read-Only 모드" 섹션 참고.

### 7. 헬스/관리 엔드포인트

| 경로 | 용도 |
|------|------|
| `GET /healthz/live` | liveness — 인프라 접근 없이 즉답 |
| `GET /healthz/ready` | readiness — ES/Mongo/Redis/Email/ZK ping (각 2s 타임아웃). 5개 infra Gauge `resource_monitor_infra_up` 도 여기서 갱신. leader 의 `redistribute_unhealthy=True` 도 503 으로 surface |
| `GET /metrics` | Prometheus 메트릭 (`resource_monitor_*`) |
| `GET /admin/status` | 인스턴스/리더/파티션/스케줄 상태 |
| `GET /admin/email-outbox` | 최근 실패한 email 발송 outbox snapshot (in-memory `deque(maxlen=1000)`, 최신 50건). v6 P1-3 — Phase 0 의 in-process DLQ. **pod 재시작 시 휘발** |
| `DELETE /admin/cooldowns/{eqp_id}/{category}/{metric}` | 쿨다운 강제 해제 |
| `POST /admin/scheduler/reload` | 프로파일 재로드 |

## 디렉토리 구조

```
src/
├── main.py                 # FastAPI lifespan (11 phases startup/shutdown)
├── middleware.py           # X-Request-ID 바인딩
├── logging_config.py       # structlog (JSON/console)
├── config/
│   ├── settings.py         # MONITOR_* 환경 변수 → AppSettings
│   └── constants.py        # 변하지 않는 상수 (ZK paths, 캐시 크기, ALERT 코드)
├── analyzer/                   # Phase 1: 분석 엔진
│   ├── engine.py           # AnalysisEngine — ES 조회→임계값 비교→알림 오케스트레이션
│   ├── threshold.py        # evaluate_thresholds + evaluate_state_check (순수 로직)
│   ├── alert_builder.py    # ThresholdBreach → EmailAlertRequest + 카테고리 분류
│   ├── es_parser.py        # ES aggregation 응답 파싱
│   └── metric_resolver.py  # 와일드카드 패턴 해석 + agg_type 결정 (max/state_check)
├── es/
│   ├── client.py           # ES 7.x AsyncElasticsearch 래퍼 + get_numeric_field_names
│   └── queries.py          # 인덱스 해석, time range 필터, 메트릭 집계 쿼리 빌더
├── db/
│   ├── client.py           # MongoClient (motor) — close()는 sync
│   ├── models.py           # MonitorProfile / Scope / EqpInfo (Pydantic v2)
│   ├── repository.py       # ProfileRepository / EqpInfoRepository
│   └── seed.py             # 기본 프로파일 SHA256 비교 후 upsert
├── cache/
│   ├── redis_client.py     # protocol=2 (Redis 5.0.6 호환)
│   └── cooldown.py         # AlertCooldownManager + local TTLCache fallback
├── alert/
│   ├── models.py           # EmailAlertRequest (Pydantic v2)
│   └── email_client.py     # Akka /EmailNotify — "success" lowercase 검증
├── distributed/
│   ├── zk_client.py        # kazoo 래퍼, asyncio bridge, 4lw fallback
│   ├── lock.py             # ZKAnalysisLock (per-process asyncio.Lock + 새 kazoo.Lock)
│   ├── leader_election.py  # 전용 ThreadPoolExecutor, restart_after_loss
│   └── partition_manager.py# 멤버십 변화 디바운스, 라운드로빈 분배, epoch+ts 가드
├── scheduler/
│   └── jobs.py             # AnalysisScheduler — reload(processes)로 job 등록, AnalysisEngine 연동
├── api/
│   ├── deps.py             # request.app.state 의존성 주입
│   ├── health.py           # /healthz/{live,ready}
│   ├── admin.py            # /admin/*
│   └── metrics.py          # Prometheus collector
└── startup/
    ├── infra.py            # init_infra (5개 인프라 순차 연결, 부분 실패 롤백)
    ├── repos.py            # init_repos
    ├── distributed.py      # init_distributed (lazy scheduler_provider)
    └── scheduler_init.py   # init_scheduler

tests/
├── unit/         # 376 tests (mock 기반, <5s)
├── integration/  # 56 tests (OrbStack 기반 — lifespan, failure modes, ZK LOST recovery)
└── e2e/          # 5 tests (real uvicorn subprocess, multi-instance ZK failover)
```

## 테스트

```bash
make test-fast           # unit only — 376 tests, <5s
make test-integration    # unit + integration (OrbStack) — ~80s
make test-e2e            # e2e (real uvicorn subprocess + 다중 인스턴스) — ~4분
make test-full           # 위 3개 전부
make test-watch          # ptw (파일 변경 감지)
```

TDD 사이클과 컨벤션은 [CONTRIBUTING.md](CONTRIBUTING.md) 참고.

## 진행 상황

| Step | 영역 | 상태 |
|------|------|------|
| 0 | Skeleton + pyproject + Makefile | done |
| 1 | settings + structlog | done |
| 2 | ES 7.11 client + queries | done |
| 3 | MongoDB + repository + seed | done |
| 4 | Redis + cooldown (local fallback) | done |
| 5 | Email client (Akka) | done |
| 6 | ZK client + Lock + LeaderElection + PartitionManager | done |
| 7 | Health + Admin + Metrics + Scheduler | done |
| 8 | Lifespan startup + middleware | done |
| **8.5** | **Resilience hardening** (ZK startup budget, retry/circuit, infra metrics, outbox, exception contract) | **done** (v6, 2026-04-08) |
| 9 | Dockerfile + K8s manifests | done |
| 10 | Integration tests (OrbStack) | done |
| **Phase 1** | | |
| 1-1 | Metric resolver + ES numeric field introspection | **done** |
| 1-2 | Threshold comparator + state_check (process watch) | **done** |
| 1-3 | ES 집계 쿼리 빌더 (terms agg, 메트릭 타입별 max/min) | **done** |
| 1-4 | Alert builder (카테고리 분류 + sub_code 생성) | **done** |
| 1-5 | Analysis Engine (오케스트레이션) | **done** |
| 1-6 | Scheduler reload(processes) + PartitionManager 연결 | **done** |
| 1-7 | Prometheus 메트릭 (THRESHOLD_BREACHES, ALERTS_SUPPRESSED) | **done** |
| 1-8 | Integration + E2E 테스트 검증 (437 tests 전부 통과) | **done** (2026-04-12) |
