# CONTRIBUTING

> 본 프로젝트는 **TDD를 기본 워크플로우**로 합니다. 테스트 없이 프로덕션 코드를 추가하지 마세요.

설계/구현 배경은 [ARCHITECTURE.md](ARCHITECTURE.md), 빠른 시작은 [README.md](README.md) 참고.

---

## 1. 개발 환경 셋업

### 1.1 Python

Python **3.11+** 필요. 3.14에서도 검증됨.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
make install                # pip install -e ".[dev]"
```

### 1.2 환경 변수

테스트는 mock 기반이라 인프라 없이 돌아갑니다. 실제 실행은 [README.md](README.md#3-환경-변수-env) 참고.

`.env` 파일을 쓸 경우 `MONITOR_*` 접두사 필수.

#### 환경 분리 (중요)

- `.env.example` — 템플릿. **localhost/OrbStack 값만** 포함. PR 로 유지 보수.
- `.env` — 로컬 개발 전용. `.gitignore` 에 등록됨. 개발자가 `.env.example` 을 복사해서 만듦.
- `k8s/secret.yaml.example` — 운영 Secret 템플릿. `CHANGE_ME` 자리표시자만 있음.
- `k8s/secret.yaml` — 실제 운영 값. 개발 PC 에 **없어야 함**. 운영팀이 K8s 에 직접 `kubectl create secret` 또는 sealed-secrets/ExternalSecrets 로 주입.

**절대 금지**:
- `.env.example` 에 production 주소/credential 의 일부라도 힌트로 적지 말 것 (`prod`, 사내 도메인 조각 등)
- `.env` 에 production credential 을 넣지 말 것
- 디버깅 PC 에서 `src.main:app` 을 production MongoDB URI 로 실행하지 말 것 — [Debug Read-Only 모드](#debug-read-only-모드) 참고

PR 리뷰 시 체크:
- [ ] `.env` 파일이 `git status` 에 나타나지 않는가
- [ ] 새 환경 변수 추가 시 `.env.example` + `k8s/configmap.yaml` / `secret.yaml.example` 업데이트했는가
- [ ] Dockerfile 에 `.env` 복사 구문이 없는가 (`.dockerignore` 가 `.env` 제외하는지)

### 1.3 IDE

- 루트 디렉토리를 프로젝트 루트로 설정 (`pyproject.toml` 의 `pythonpath = ["."]`)
- ruff를 포매터/린터로 활성화

---

## 2. TDD 워크플로우

상위 ARS 정책: `/Users/hyunkyungmin/Developer/ARS/CLAUDE.md` 의 "개발 워크플로우: TDD 기본" 참고.

### 2.1 사이클

1. **RED** — 실패하는 테스트를 먼저 작성
2. **GREEN** — 테스트를 통과하는 **최소 코드**만 구현
3. **REFACTOR** — 테스트 통과 상태를 유지하며 개선

### 2.2 규칙

- 테스트가 실패하는 것을 **눈으로 확인한 뒤** 구현 시작
- 각 사이클 후 **모든 기존 테스트도 통과** 확인 (`make test-fast`)
- 버그 수정도 동일: 버그를 재현하는 테스트 → 수정 → 통과

### 2.3 예외

- 단순 typo, 주석, 문서 변경
- 외부 인프라 동작 검증 (e.g. ZK 4lw 응답) — 이건 integration/e2e 영역

---

## 3. 테스트 실행

```bash
make test-fast           # tests/unit, mock 기반, <5s — 평소 개발 시
make test-integration    # unit + integration (testcontainers, ZK/Redis/ES/Mongo)
make test-full           # unit + integration + e2e (멀티 인스턴스)
make test-watch          # ptw (파일 변경 감지, 자동 재실행)
```

### 3.1 특정 테스트만

```bash
.venv/bin/python -m pytest tests/unit/test_partition_manager.py -v
.venv/bin/python -m pytest tests/unit/test_partition_manager.py::test_apply_assignment_stale -v
.venv/bin/python -m pytest -k "leader and not lost" -v
```

### 3.2 마커

`pyproject.toml` 에 정의:

| 마커 | 의미 |
|------|------|
| `unit` | mock 기반 빠른 테스트 |
| `integration` | testcontainers 기반 |
| `e2e` | 멀티 인스턴스 시나리오 |
| `slow` | 10초 초과 |

`pytest -m "unit and not slow"` 식으로 필터링 가능.

### 3.3 Failure-mode 통합 테스트

`tests/integration/test_startup_failure_modes.py` 는 **`docker stop` 으로 OrbStack 컨테이너를 죽이고** RMS lifespan 이 어떻게 실패하는지 검증합니다. 실행 전제:

1. `make dev-up` 으로 baseline (ARS docker-compose) 가 healthy 한 상태
2. fixture teardown 이 `docker start` 로 복구하지만, **테스트가 timeout 으로 죽으면 컨테이너가 stopped 인 채 남을 수 있음** — CI 또는 다음 테스트 run 전에 항상 `make dev-status` 로 확인
3. CI 에서는 이 파일을 다른 통합 테스트보다 먼저 실행하지 말 것 (인프라 toggling 이 다른 테스트에 영향)

핵심 회귀 가드: `test_zk_down_at_boot_fails_within_budget` — v6 P0-1 의 ZK dead-zone 수정이 살아있는지 확인. 이 테스트가 깨지면 production 이 CrashLoopBackoff 로 들어갈 위험이 있음.

### 3.4 E2E 테스트 (`tests/e2e/`)

E2E 는 integration 과 달리 **실제 `uvicorn src.main:app` 을 subprocess 로 띄우고 HTTP 로 인터랙션**합니다. 단일 인스턴스 wall-clock 검증 + **다중 인스턴스 ZK 협조** 가 주 목적. In-process `asgi_lifespan` 으로는 여러 Python 프로세스가 같은 ZK root 를 경합하는 시나리오를 재현할 수 없어서 필요했음.

| 파일 | 시나리오 | 소요 |
|------|--------|-----|
| `test_single_instance.py::test_normal_boot_reports_all_infra_up_and_metrics` | V7 — 정상 boot + `/healthz/ready` 200 + 5개 `infra_up=1` + `startup_complete=1` | ~40s |
| `test_single_instance.py::test_runtime_redis_stop_flips_ready_503_then_recovers` | V8 — runtime Redis 중단 → ready 503 + scheduler 유지 → 복구 | ~40s |
| `test_multi_instance.py::test_two_instances_elect_exactly_one_leader` | 2 uvicorn → 정확히 1 leader | ~50s |
| `test_multi_instance.py::test_new_instance_triggers_redistribute` | A 단독 (3/0) → B 추가 → (2,1) 재분배 | ~50s |
| `test_multi_instance.py::test_leader_failover_on_sigterm` | A(leader)+B → A SIGTERM → B 가 새 leader + 새 epoch + 모든 process 인수 | ~40s |

**실행**:
```bash
make test-e2e    # dev-up 의존 — 5개 시나리오, ~4분
```

**전제**:
- OrbStack 에 `ars-zookeeper`, `ars-redis`, `ars-elasticsearch`, `mongodb-44` 모두 healthy (`make dev-up` → `make dev-status` 확인)
- `MONITOR_ZK_SESSION_TIMEOUT=10` 로 단축되어 failover 시나리오가 1분 이내에 완료. kazoo 최소값(tickTime 2s × 4 = 8s) 위라서 안전

**격리**: 각 test run 은 UUID namespace (`EARS_test_{run_id}_e2e_*` Mongo DB, `RESOURCE_ALERT_test_{run_id}_*` Redis 키, `/resource-monitor-test-{run_id}/e2e-*` ZK 경로). integration 과 동일한 session finalizer 로 cleanup.

**핵심 회귀 가드**: `test_leader_failover_on_sigterm` — ZK 3.5.5 세션 만료 → ephemeral 해제 → kazoo Election 콜백 fire → 새 leader 의 redistribute → epoch 증가 의 **전체 체인** 을 진짜 2개 프로세스로 검증. ARCHITECTURE.md G1~G10 의 G3/G4/G6 가 살아있는지 확인하는 유일한 자동 테스트.

**Mock email**: stdlib `http.server.HTTPServer` 를 daemon thread 로 띄움 (integration 의 aiohttp fixture 는 async 라 subprocess 에서 접근 불가). `tests/e2e/conftest.py::email_mock_url` 참고.

**Subprocess 수명**: `Popen` 으로 띄우고 SIGTERM → `wait(timeout=15)` → SIGKILL. 각 subprocess 의 stdout/stderr 는 `tmp_path/<instance_id>.log` 로 캡처되어 assertion 실패 시 `dump_log_tail()` 로 출력됨.

### 3.3 커버리지

```bash
.venv/bin/python -m pytest tests/unit --cov=src --cov-report=term-missing
```

`pyproject.toml` 의 `fail_under = 80` — 80% 미만이면 CI fail.

---

## 4. 코딩 컨벤션

### 4.1 ruff

```bash
make lint               # ruff check src tests
make fmt                # ruff format src tests
```

설정은 `pyproject.toml`:

- `line-length = 100`
- `target-version = "py311"`
- 활성 룰: `E, F, W, I, N, UP, B, C4, SIM`
- 무시 룰: `E501` (긴 줄, 100자 + 약간의 여유)

### 4.2 스타일

- `from __future__ import annotations` 모든 모듈 첫 줄
- 타입 힌트는 PEP 604 (`str | None`) 우선
- async function은 명시적으로 `async def`
- 모듈 docstring으로 **무엇을** + **왜** 둘 다 적기 (특히 분산 모듈)
- 매직 넘버 금지 → `src/config/constants.py` 로 빼기

### 4.3 로깅

- `print` 금지
- `structlog.get_logger(__name__)` 만 사용
- 로그 메시지는 **snake_case 이벤트 이름** + 키워드 인자:
  ```python
  logger.info("became_leader", instance=self._instance_id, epoch=new_epoch)
  ```
- ERROR 로그에는 `exc_info=True` 포함

---

## 5. 테스트 작성 가이드

### 5.1 픽스처 명명 (`tests/conftest.py`)

| 픽스처 | 타입 | 용도 |
|--------|------|------|
| `mock_es` | `AsyncMock` | AsyncElasticsearch |
| `mock_mongo` | `AsyncMock` | AsyncIOMotorClient |
| `mock_redis` | `AsyncMock` | redis.asyncio |
| `mock_zk` | `MagicMock` | KazooClient (sync API) |
| `mock_email` | `AsyncMock` | httpx.AsyncClient 래퍼 |
| `mock_infra` | `MockInfraContext` | 위 5개를 묶은 dataclass |

데이터 픽스처는 `sample_<name>` (예: `sample_profile`).

### 5.2 절대 mocking 금지

- ❌ `kazoo.client.KazooClient` 전체 monkey-patch — `MagicMock` 인스턴스를 주입
- ❌ `motor`/`redis.asyncio` 의 internal — `AsyncMock` 으로 외부 표면만 모킹
- ✅ 인터페이스 단위로 모킹 (e.g. `EqpInfoRepository` 메서드)

### 5.3 비동기 테스트

- `pyproject.toml` 의 `asyncio_mode = "auto"` 덕분에 `@pytest.mark.asyncio` 불필요
- `asyncio_default_fixture_loop_scope = "function"` — 매 테스트마다 새 루프
- Python 3.14에서는 `asyncio.get_event_loop()` deprecated → fixture를 `async def` 로 만들고 `asyncio.get_running_loop()` 사용

### 5.4 회귀 가드

특정 버그 수정 후에는 그 버그를 잡는 테스트에 **회귀 가드** 주석을 남기세요:

```python
def test_returns_false_on_capital_success():
    """Regression: Akka returns lowercase 'success'. Don't accept 'Success'."""
    ...
```

`tests/unit/test_email_client.py::test_returns_false_on_capital_success` 가 좋은 예시.

---

## 6. 새 모듈 추가 시 체크리스트

- [ ] `src/<area>/<module>.py` 작성 (`from __future__ import annotations` 첫 줄)
- [ ] 모듈 docstring에 **무엇을** + **왜** 작성
- [ ] `tests/unit/test_<module>.py` 에 테스트 작성 (RED → GREEN)
- [ ] 외부 의존성은 `tests/conftest.py` 의 mock fixture 사용
- [ ] 새 환경 변수가 있으면 `src/config/settings.py` + README의 환경 변수 표 갱신
- [ ] 새 ZK 경로/캐시 값은 `src/config/constants.py` 에 상수로
- [ ] 새 메트릭은 `src/api/metrics.py` 에 등록
- [ ] **새 인프라 추가 시 `INFRA_LABELS` (`src/api/metrics.py`) + `readiness()` (`src/api/health.py`) + ARCHITECTURE.md §8/§8.5 표 모두 갱신** (v6 P0-5)
- [ ] **K8s probe 또는 `zk_startup_budget_sec` 변경 시 `tests/unit/test_k8s_probe_invariants.py` 의 단언이 여전히 유효한지 확인** — 이 invariant 가 dead-zone 회귀 가드 (v6 P1-4)
- [ ] **새 Mongo repository 메서드는 boundary 에서 `MongoUnavailableError` 로 예외 변환** (`src/db/repository.py` 패턴 참고, v6 P1-1)
- [ ] `make test-fast` 통과 확인
- [ ] `make lint` 통과 확인
- [ ] 분산/락/세션 관련 변경이면 [ARCHITECTURE.md](ARCHITECTURE.md#4-critical-gotchas--pitfalls) 의 Gotchas 갱신

---

## 7. 커밋 메시지

상위 ARS의 hooks 정책상 ResourceMonitorServer 폴더에서만 커밋합니다.

### 형식

```
<type>(<scope>): <subject>

<body>
```

### type

- `feat` — 새 기능
- `fix` — 버그 수정
- `refactor` — 동작 변화 없는 리팩터링
- `test` — 테스트만 추가/수정
- `docs` — 문서만
- `chore` — 빌드/설정/의존성

### scope

`config`, `es`, `db`, `cache`, `alert`, `distributed`, `scheduler`, `api`, `startup`, `tests`, `docs` 중 하나.

### 예시

```
feat(distributed): add LeaderElection.restart_after_loss

LOST 후 옛 Election 객체는 죽은 세션에 묶여 재사용 불가.
새 Election을 만들어 fire-and-forget으로 다시 schedule.

회귀 가드: tests/unit/test_leader_election.py::test_restart_after_loss
```

```
fix(alert): Akka /EmailNotify result는 lowercase 'success'

대문자 'Success' 비교는 항상 False 반환 → 알림 누락.
회귀 가드: test_email_client.py::test_returns_false_on_capital_success
```

---

## 8. 디버깅 팁

### 8.1 분산 모듈

- ZK 관련 버그는 거의 다 **scheduler thread ↔ asyncio loop bridge** 문제. `asyncio.run_coroutine_threadsafe(coro, loop)` 가 빠지지 않았는지 먼저 확인.
- LOST 시나리오는 `tests/unit/test_partition_manager.py` 의 `test_lost_then_reconnect_*` 를 참고해서 재현.
- 락 hang은 **kazoo Lock 재사용** 의심. [ARCHITECTURE.md G2](ARCHITECTURE.md#g2-kazoorecipelocklock-은-비재진입--재사용-금지) 참고.

### 8.2 Pydantic v2

- `model_*` 식별자 예약 — 필드명에 `model` 들어가면 alias로 우회
- `BaseSettings` 의 `list[str]` 자동 JSON 디코드 → `Annotated[..., NoDecode]` 필요 (`src/config/settings.py` 참고)

### 8.3 ES 7.x

- ES 8.x 문서 보지 마세요. `http_auth=`, `timeout=`, raw dict 응답 — 모두 7.x 전용.
- `NotFoundError` 는 `client.indices.get_field_mapping` 에서 자주 발생 → "unknown" 캐싱.

### 8.4 Debug Read-Only 모드

`MONITOR_DEBUG_READ_ONLY=true` 는 "production 데이터를 관찰하지만 절대 변경하지 않는" 전용 부팅 모드입니다. 설계 목적은 두 가지:

1. 개발자가 실수로 production 에 쓰기를 하지 못하게 차단
2. Phase 1+ 분석 코드가 실제 prod 데이터에 대해 어떻게 반응하는지 관찰 (staging 데이터로 재현 불가능한 버그 디버깅)

**쓰기 경로 차단 목록** (전수):

| 위치 | 차단 내용 | 가드 |
|------|----------|------|
| `src/startup/repos.py` | `create_index(uniq_scope)` | `if not settings.debug_read_only` |
| `src/main.py` lifespan | `seed_default_profile` 스킵 | phase skip |
| `src/main.py` lifespan | `init_distributed` (ZK 참여) 스킵 | phase skip |
| `src/main.py` lifespan | `leader_election.start` / `partition_manager.start` 스킵 | phase skip |
| `src/startup/infra.py` | `ZKClient.connect()` 스킵 — `infra.zk = None` 유지 | `if settings.debug_read_only` |
| `src/alert/email_client.py` | `send_alert` HTTP POST 차단 | 가드 → `True` 반환 + `debug_would_send_email` 로그 |
| `src/cache/cooldown.py` | `set_cooldown` / `clear_cooldown` / `is_cooling_down` Redis 쓰기 차단 | 가드 → local TTLCache 만 사용 |

**보존되는 것** (정상 동작):
- ES / Mongo 읽기
- Scheduler start (분석 흐름 관찰 가능)
- HTTP 엔드포인트 (`/healthz/live`, `/healthz/ready`, `/metrics`, `/admin/status`)
- `/healthz/ready` 는 `debug_read_only: true` + `checks.zookeeper: "skipped_debug"` 로 표시
- `/admin/status` 는 분산 필드 모두 `null` 로 표시

**Scheduler partition 결정** (ZK 없이):
- `settings.debug_processes` 가 비어있지 않으면 그 목록 사용
- 비어있으면 `EqpInfoRepository.get_distinct_processes()` 결과 전체 사용
- Phase 1+ 의 `AnalysisScheduler.reload()` 가 `resolve_processes_for_debug()` 를 호출해야 함

**절대 금지**:
- Production K8s manifests (`deployment.yaml`, ConfigMap, Secret) 에 `MONITOR_DEBUG_READ_ONLY` 를 넣지 말 것
- 운영팀이 장애 대응 중에 production pod 에서 이 플래그를 켜지 말 것 — analysis job 이 돌지 않아 감지 공백 발생
- `.env.example` 에 기본값으로 `true` 를 두지 말 것 (false 로 유지)

**회귀 가드** (debug 모드 관련 테스트):
- `tests/unit/test_settings.py::TestDebugReadOnly` — 설정 파싱
- `tests/unit/test_startup.py::TestInitInfra::test_init_infra_skips_zk_in_debug_mode`
- `tests/unit/test_startup.py::TestInitRepos::test_init_repos_skips_create_index_in_debug_mode`
- `tests/unit/test_lock.py::TestNoOpZKLock`
- `tests/unit/test_cooldown.py::TestDebugReadOnlyGuard`
- `tests/unit/test_email_client.py::TestDebugReadOnlyGuard`
- `tests/unit/test_scheduler_jobs.py::TestDebugProcessesResolution`
- `tests/integration/test_lifespan_real.py::test_debug_lifespan_*` (6 tests)

---

## 9. 코드 리뷰 자가 점검

PR 보내기 전에:

- [ ] `make test-fast` 통과 (227 tests baseline — unit only)
- [ ] `make lint` 통과
- [ ] 새 코드의 모든 분기에 테스트 있음
- [ ] 분산 변경은 LOST/SUSPENDED 경로도 테스트했는가?
- [ ] 새 환경 변수는 `.env.example` (있으면) + README 갱신했는가?
- [ ] 새 메트릭은 [ARCHITECTURE.md 8장](ARCHITECTURE.md#8-메트릭--관측) 에도 추가했는가?
- [ ] critical gotcha를 새로 발견했다면 [ARCHITECTURE.md G section](ARCHITECTURE.md#4-critical-gotchas--pitfalls) 갱신했는가?

---

## 10. 참고 문서

| 문서 | 내용 |
|------|------|
| [README.md](README.md) | 개요, 빠른 시작, 디렉토리 맵 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 시스템 설계, 분산 조정, **Gotchas** |
| [SCHEMA.md](SCHEMA.md) | EARS DB 컬렉션 스키마 (PROFILE, EQP_INFO 등) |
| [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md) | Phase 0 요구사항 |
| [docs/archive/phase0-plan-v6.md](docs/archive/phase0-plan-v6.md) | Phase 0 v6 구현 계획 (완료 보관용) |
| `/Users/hyunkyungmin/Developer/ARS/CLAUDE.md` | 상위 워크플로우 정책 |
| `/Users/hyunkyungmin/Developer/ARS/.claude/PLANNING.md` | 상위 ARS 통합 설계 |
