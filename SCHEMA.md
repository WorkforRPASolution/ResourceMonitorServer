# SCHEMA — ResourceMonitorServer (Phase 0)

ResourceMonitorServer가 EARS DB(MongoDB)에서 직접 다루는 모든 컬렉션의 스키마 레퍼런스. 본 문서는 **현재 코드에 박혀 있는 사실**을 기준으로 합니다 — 설계 의도는 [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md), 결정 배경은 [ARCHITECTURE.md](ARCHITECTURE.md), 외부 컬렉션의 풀 스키마는 `~/Developer/ARS/WebManager/docs/SCHEMA.md` 참고.

---

## 0. 데이터베이스 + 컬렉션 위치

**모든 컬렉션은 단일 `EARS` 데이터베이스 안에 공존합니다.** RMS는 별도 DB를 만들지 않고 Akka 서버와 동일 DB를 공유합니다.

### 코드 근거
| 위치 | 설정 |
|------|------|
| `src/config/settings.py:38` | `mongo_db: str = "EARS"` (default) |
| `src/db/client.py:53` | `self._db = client[self._settings.mongo_db]` |
| 환경 변수 (override) | `MONITOR_MONGO_DB` |
| K8s ConfigMap | `MONITOR_MONGO_DB: "EARS"` (`k8s/configmap.yaml`) |

### EARS DB의 컬렉션 전체 (Akka + RMS 공존)

```
EARS (database)
├── ARS_USER_INFO                  ← Akka 소유 (기존)
├── EQP_INFO                       ← Akka 소유 (기존), RMS는 read-only
├── EMAIL_TEMPLATE_REPOSITORY      ← Akka 소유 (기존)
├── EMAILINFO                      ← Akka 소유 (기존)
├── EMAIL_RECIPIENTS               ← Akka 소유 (기존)
├── EMAIL_IMAGE_REPOSITORY         ← Akka 소유 (기존)
├── POPUP_TEMPLATE_REPOSITORY      ← Akka 소유 (기존)
├── RESOURCE_MONITOR_PROFILE       ← ★ RMS 소유 (Phase 0 신규)
└── RESOURCE_MONITOR_RULE          ← ★ RMS 소유 (Phase 3+ 예약, 미사용)
```

### 왜 단일 DB인가 (설계 의도)
1. **EARS = 단일 사실 원천**. Akka 서버와 RMS가 같은 장비/사용자/이메일 템플릿을 공유해야 하므로 DB 분리 시 cross-DB join 불가.
2. **`EQP_INFO`를 RMS가 read-only로 사용** — Akka가 master, 같은 DB라야 일관된 view.
3. **이메일 발송 시 `EMAIL_TEMPLATE_REPOSITORY` 참조** (Phase 1+ alert body 빌더). 역시 같은 DB가 자연스러움.
4. **컬렉션 충돌은 prefix로 격리**: 신규 컬렉션은 모두 `RESOURCE_MONITOR_*` prefix → 기존 7개 컬렉션과 충돌 없음 (PRD §5에서 확인 완료).

### 환경별 매핑
| 환경 | DB 인스턴스 | DB 이름 |
|------|----------|--------|
| 운영 | 운영 MongoDB 클러스터 | `EARS` |
| 개발 (OrbStack) | `mongodb-44` 컨테이너 (`mongo:4.4.30`) | `EARS` (Akka/WebManager와 동일 인스턴스 공유) |
| 통합 테스트 | `mongodb-44` 컨테이너 | `EARS_test_<run_id>` (run마다 격리, 끝나면 drop) |

---

### RMS가 직접 다루는 컬렉션 — 한 줄 요약

| 컬렉션 | 소유자 | Phase 0 상태 | 액세스 |
|--------|--------|-------------|--------|
| [`RESOURCE_MONITOR_PROFILE`](#1-resource_monitor_profile) | RMS (신규 생성) | **활성 사용** | read/write (seed + resolve) |
| [`RESOURCE_MONITOR_RULE`](#2-resource_monitor_rule) | RMS (신규 생성) | 상수만 정의, **미사용** | — (Phase 3+) |
| [`EQP_INFO`](#3-eqp_info-외부) | Akka 서버 | **활성 사용** | read-only |

상수 정의: `src/config/constants.py:22-24`

```python
COLL_PROFILE  = "RESOURCE_MONITOR_PROFILE"
COLL_RULE     = "RESOURCE_MONITOR_RULE"
COLL_EQP_INFO = "EQP_INFO"
```

EARS DB의 기존 7개 컬렉션(`EQP_INFO, ARS_USER_INFO, EMAIL_TEMPLATE_REPOSITORY, POPUP_TEMPLATE_REPOSITORY, EMAILINFO, EMAIL_RECIPIENTS, EMAIL_IMAGE_REPOSITORY`)과 **충돌 없음** — 신규 `RESOURCE_MONITOR_*` prefix 사용.

---

## 1. `RESOURCE_MONITOR_PROFILE`

**역할**: 메트릭 분석 규칙(threshold + schedule)을 장비 계층 스코프에 묶어 저장.
**Pydantic 모델**: `MonitorProfile` (`src/db/models.py:113`)
**리포지토리**: `ProfileRepository` (`src/db/repository.py:37`)
**시드**: `seed_default_profile()` (`src/db/seed.py:69`) — 부팅 시 idempotent upsert

### 1.1 문서 구조

```jsonc
{
  "_id": ObjectId("..."),                    // Mongo 자동 부여
  "scope": {
    "process": "*",                           // 필수. EQP_INFO.process와 1:1, "*" = wildcard
    "eqpModel": "*",                          // ★ "model" 아님 (alias 처리)
    "eqpId": "*"
  },
  "analysis_configs": [
    {
      "metric_pattern": "total_used_pct",    // 와일드카드 가능 (예: "*_core_load")
      "threshold": {
        "warning": 80.0,
        "critical": 95.0,
        "cooldown_minutes": 30
      },
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 10
      }
    }
  ]
}
```

### 1.2 필드 레퍼런스

| 경로 | 타입 | 필수 | 기본값 | 비고 |
|------|------|------|------|------|
| `_id` | ObjectId | auto | — | Pydantic의 `id: str | None`로 변환 (`from_mongo`) |
| `scope` | object | ✓ | — | 임베디드 객체, 별도 컬렉션 아님 |
| `scope.process` | string | ✓ | — | EQP_INFO.process와 동일 키. `"*"` = 모든 process |
| `scope.eqpModel` | string |  | `"*"` | **★ 카멜케이스**. Pydantic alias chain: `eqp_model` ↔ JSON `model` ↔ Mongo `eqpModel` |
| `scope.eqpId` | string |  | `"*"` | 단일 장비. EQP_INFO.eqpId와 동일 키 |
| `analysis_configs` | array | ✓ | `[]` | 여러 메트릭에 대한 룰 묶음 |
| `analysis_configs[].metric_pattern` | string | ✓ | — | 메트릭 이름 또는 와일드카드 패턴 (예: `"*_core_load"`) |
| `analysis_configs[].threshold` | object | ✓ | — | |
| `analysis_configs[].threshold.warning` | float | ✓ | — | 경고 임계값 (단위는 metric 의존) |
| `analysis_configs[].threshold.critical` | float | ✓ | — | 심각 임계값 |
| `analysis_configs[].threshold.cooldown_minutes` | int | ✓ | — | Redis cooldown TTL (이메일 폭주 방지) |
| `analysis_configs[].schedule` | object | ✓ | — | |
| `analysis_configs[].schedule.interval_minutes` | int | ✓ | — | APScheduler job 주기 |
| `analysis_configs[].schedule.window_minutes` | int | ✓ | — | ES query 시계열 윈도우 |

### 1.3 Pydantic ↔ Mongo 매핑

```python
class Scope(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    process: str
    eqp_model: str = Field(default="*", alias="model")    # Python 명: eqp_model
    eqp_id:   str = Field(default="*", alias="eqpId")     # Python 명: eqp_id
```

`Scope.to_mongo()`는 alias가 아닌 **실제 EQP_INFO 필드명**(`eqpModel`, `eqpId`)으로 직렬화하므로, JSON 입출력의 alias(`model`, `eqpId`)와 Mongo 저장 키가 다를 수 있음. 정리:

| 위치 | `eqp_model`을 부르는 이름 |
|------|-------|
| Python 코드 | `scope.eqp_model` |
| JSON API 입력/출력 (Pydantic alias) | `model` |
| **Mongo 저장 키** | `eqpModel` |

자세한 함정은 [ARCHITECTURE.md G10](ARCHITECTURE.md#g10-pydantic-v2는-model_-식별자-예약) 참고.

### 1.4 스코프 해석 우선순위

`ProfileRepository.resolve_profile(process, eqp_model, eqp_id)` (`src/db/repository.py:72`)는 **가장 구체적 → 가장 일반적** 순으로 첫 매치를 반환합니다.

| 우선순위 | 검색 scope |
|------|-----------|
| 1 | `(process, eqp_model, eqp_id)` 정확 매칭 |
| 2 | `(process, eqp_model, "*")` |
| 3 | `(process, "*", "*")` |
| 4 | `("*", "*", "*")` (global wildcard) |

캐시: `ProfileRepository._resolve_cache` — `cachetools.TTLCache(maxsize=10000, ttl=300)`. 키 = `f"{process}:{eqp_model}:{eqp_id}"`. `upsert()` 호출 시 cache.clear()로 일관성 유지.

### 1.5 Default seed

`seed_default_profile()`이 부팅 시 적용. 변경 시에만 upsert (SHA256 hash 비교) → 운영자 hand-edit 보존.

```jsonc
{
  "scope": {"process": "*", "eqpModel": "*", "eqpId": "*"},
  "analysis_configs": [
    {
      "metric_pattern": "total_used_pct",
      "threshold": {"warning": 80.0, "critical": 95.0, "cooldown_minutes": 30},
      "schedule": {"interval_minutes": 5, "window_minutes": 10}
    },
    {
      "metric_pattern": "*_core_load",
      "threshold": {"warning": 85.0, "critical": 97.0, "cooldown_minutes": 30},
      "schedule": {"interval_minutes": 5, "window_minutes": 10}
    }
  ]
}
```

정의 위치: `src/db/seed.py:26` `build_default_profile()`.

### 1.6 인덱스

#### `uniq_scope` (unique)

```js
db.RESOURCE_MONITOR_PROFILE.createIndex(
  { "scope.process": 1, "scope.eqpModel": 1, "scope.eqpId": 1 },
  { unique: true, name: "uniq_scope" }
)
```

- 한 프로파일 = 정확히 한 (process, eqpModel, eqpId) 조합. 이 불변조건이 유지되어야 `ProfileRepository.create()`의 `DuplicateKeyError → ProfileAlreadyExistsError` 변환 경로가 동작함.
- **자동 생성**: `src/startup/repos.py:init_repos()`가 startup 시점에 `create_index(..., unique=True, name="uniq_scope")`를 호출. MongoDB가 컬렉션을 암묵적으로 생성하므로 fresh EARS DB에서도 안전.
- **멱등성**: 같은 spec으로 재호출해도 MongoDB는 "all indexes already exist"로 no-op 처리 — 매 재시작마다 호출해도 부작용 없음.
- **Debug Read-Only 모드에서 스킵**: `settings.debug_read_only=True` 일 때 `create_index` 호출은 건너뜁니다. Debug 인스턴스는 운영 중인 production DB 에 연결된다는 전제이므로 인덱스가 이미 존재하며, debug 가 prod 스키마를 변경해선 안 됩니다. 자세한 가드 구조는 [ARCHITECTURE.md §9](ARCHITECTURE.md#9-debug-read-only-모드) 참고.
- **회귀 가드**:
  - `tests/unit/test_startup.py::TestInitRepos::test_init_repos_creates_unique_scope_index_on_profile`
  - `tests/unit/test_startup.py::TestInitRepos::test_init_repos_skips_create_index_in_debug_mode` (debug 가드)
  - `tests/integration/test_mongo_real.py::test_init_repos_creates_unique_scope_index_on_fresh_db` (fresh motor db 대상)
  - `tests/integration/test_mongo_real.py::test_init_repos_is_idempotent` (두 번 호출)
  - `tests/integration/test_mongo_real.py::test_create_duplicate_raises_domain_error` (end-to-end)
  - `tests/integration/test_lifespan_real.py::test_debug_lifespan_does_not_create_profile_index` (debug 모드 전체 lifespan)

### 1.7 도메인 예외

| 예외 | 발생 시점 | HTTP 매핑 (Phase 1+) |
|------|----------|--------------------|
| `ProfileAlreadyExistsError` | `create()` 시 동일 scope가 이미 존재 | 409 Conflict |
| `ProfileNotFoundError` | `find_by_scope()` 결과가 None일 때 호출자가 raise | 404 Not Found |

정의: `src/db/models.py:23-32`.

### 1.8 리포지토리 연산 요약

| 메서드 | 동작 | 캐시 영향 |
|--------|------|----------|
| `create(profile)` | `insert_one`. DuplicateKey → 도메인 예외 변환 | (영향 없음) |
| `upsert(profile)` | `replace_one` filter=`scope.*`, `upsert=True` | `_resolve_cache.clear()` |
| `find_by_scope(scope)` | `find_one` by `scope.*` (와일드카드 필드는 필터 omit) | (read-through 아님) |
| `resolve_profile(p, m, e)` | 1.4의 4단계 lookup, TTL 캐시 | hit 시 DB 우회 |

---

## 2. `RESOURCE_MONITOR_RULE`

**상태**: 상수만 정의됨, Phase 0 코드에 **모델/리포지토리/사용처 0건**.

```python
COLL_RULE = "RESOURCE_MONITOR_RULE"
```

PRD §3 기준으로는 임계값 단순 비교를 넘어 **지표 조합 rule engine**(Phase 3)을 위한 컬렉션 후보입니다. 실제 스키마는 Phase 3 설계 시 확정되므로 본 문서는 **자리 표시만** 합니다.

### Phase 3 설계 시 결정해야 할 항목 (참고)
- rule 식별자 / 활성 플래그
- trigger 표현식 형식 (DSL? JSON expression tree?)
- rule ↔ profile 관계 (1:N, M:N?)
- 우선순위 / suppression 규칙
- audit (생성자, 변경 이력)

---

## 3. `EQP_INFO` (외부)

**소유자**: Akka 서버 (WebManager 측에서 master/CRUD).
**RMS 액세스**: **read-only**. 어떤 경우에도 RMS가 write 하지 않음.
**Pydantic 모델**: 없음 — RMS는 distinct/count 결과만 사용.
**리포지토리**: `EqpInfoRepository` (`src/db/repository.py:120`)

### 3.1 RMS가 사용하는 필드만

| 필드 | 타입 | 사용 위치 |
|------|------|----------|
| `eqpId` | string | 장비 식별자 (Phase 1+ 알림 본문) |
| `process` | string | `get_distinct_processes()` — process 단위 파티셔닝의 키 |
| `eqpModel` | string | `Scope` 매핑 (Phase 1+ resolve_profile) |
| `onoff` | int (0/1) | **활성 필터** |
| `webmanagerUse` | int (0/1) | **활성 필터** |

> 풀 스키마(line, category, ipAddr, localpc 등 추가 필드)는 `~/Developer/ARS/WebManager/docs/SCHEMA.md` 참고.

### 3.2 활성 필터

```python
EqpInfoRepository._ACTIVE_FILTER = {"onoff": 1, "webmanagerUse": 1}
```

이 필터는 **모든 read 경로에 자동 적용**됩니다:

| 메서드 | 적용 |
|--------|------|
| `get_distinct_processes()` | `coll.distinct("process", filter=_ACTIVE_FILTER)` |
| `count_active_by_process(p)` | `coll.count_documents({"process": p, **_ACTIVE_FILTER})` |

→ **decommissioned PC(`onoff=0`)나 webmanager 미관리(`webmanagerUse=0`) 장비는 절대로 분석 대상이 되지 않음**. 직접 EQP_INFO에 쿼리하면 이 필터가 빠질 수 있으므로, **반드시 `EqpInfoRepository`를 통해서** 접근.

### 3.3 권장 인덱스 (운영팀 확인 필요)

운영팀 사전 확인 체크리스트(plan §사전 확인 사항 4):

```js
db.EQP_INFO.getIndexes()
// 기대: { process: 1 }, { process: 1, eqpModel: 1 }, { eqpId: 1 } 존재
```

`get_distinct_processes()`는 `{process: 1}` 인덱스가 있으면 covered query로 전체 스캔을 피할 수 있습니다.

---

## 4. 핵심 함정 (Pitfalls)

다음 실수가 반복되지 않도록 박제합니다.

### P1. `eqpModel` 카멜케이스
- ❌ `{"model": "..."}`, `{"eqp_model": "..."}` — Mongo에 저장될 키가 아님
- ✅ Mongo 직접 쿼리 시에는 `{"scope.eqpModel": "..."}` 또는 `{"eqpModel": "..."}`
- Python 코드에서는 `Scope` 객체로만 다루기 — 매핑은 모델이 알아서

### P2. `eqpId` 카멜케이스
- 같은 함정. snake_case `eqp_id` 아님.

### P3. 활성 필터 누락
- ❌ `coll.distinct("process")` 직접 호출
- ✅ `EqpInfoRepository.get_distinct_processes()`만 사용

### P4. unique index 생성 시점 (§1.6)
- `init_repos()`가 매 startup마다 `create_index(..., unique=True, name="uniq_scope")`를 호출
- MongoDB의 `createIndex` 는 컬렉션 미존재 시 자동 생성 + 기존 인덱스에는 no-op → **매번 호출해도 안전**
- 운영팀이 수동으로 만들 필요 없음. `init_repos()` 호출 전에 생긴 legacy 중복 데이터는 build 실패 사유가 됨 — 그 경우 중복 해결 후 재시작

### P5. `populate_by_name=True` 의존
- `Scope` 모델은 `populate_by_name=True`라 alias `model` 또는 Python 이름 `eqp_model` 둘 다 입력 허용
- 하지만 `extra="forbid"`라 다른 키는 거부 — 잘못된 키로 생성하면 `ValidationError`

### P6. ObjectId ↔ str 변환
- Pydantic의 `MonitorProfile.id`는 `str | None`
- `from_mongo()`에서 `_id`를 `str(_id)`로 변환
- `to_mongo()`는 `_id` 출력하지 않음 (Mongo가 자동 부여하도록)

---

## 4.1 Exception Contract (v6 P1-1)

`src/db/repository.py` 의 모든 public async 메서드는 raw `pymongo.errors.*` 를 호출자에게 누출하지 않습니다. boundary 에서 다음과 같이 도메인 예외로 변환:

| 원본 예외 (pymongo) | 변환된 예외 (`src/db/models.py`) | 의미 |
|---------------------|--------------------------------|------|
| `ServerSelectionTimeoutError` | `MongoUnavailableError` | Mongo 연결 불가 |
| `NetworkTimeout` | `MongoUnavailableError` | replicate set 와 통신 끊김 |
| `ConnectionFailure` | `MongoUnavailableError` | TCP/소켓 끊김 |
| `DuplicateKeyError` (`ProfileRepository.create` 만) | `ProfileAlreadyExistsError` | unique index 충돌 — 비즈니스 로직 |

`MongoUnavailableError` 는 `_job_wrapper` (`src/scheduler/jobs.py`) 가 `JOB_TOTAL{reason="mongo_unavailable"}` 라벨로 분류하기 위해 필요합니다 (P1-2). 새 repository 메서드를 추가할 때는 반드시 같은 패턴으로 wrapping 하세요:

```python
try:
    return await self._collection.find_one(...)
except _MONGO_UNAVAILABLE_EXC as e:
    raise MongoUnavailableError(f"...: {e}") from e
```

회귀 가드: `tests/unit/test_db_repository.py::TestMongoUnavailableTranslation` (parametrized).

---

## 5. Phase 0 알려진 갭 / 후속 작업

| # | 갭 | 영향 | 해결 위치 | 우선순위 |
|---|----|------|----------|---------|
| 1 | ~~`RESOURCE_MONITOR_PROFILE`에 unique index 자동 생성 없음~~ | ~~`create()`의 중복 검출 미동작~~ | `src/startup/repos.py:init_repos()` | **Done** (2026-04-07) |
| 2 | `RESOURCE_MONITOR_RULE` 스키마 미정의 | Phase 3 시작 전 결정 필요 | Phase 3 설계 | Low (Phase 3) |
| 3 | EQP_INFO 인덱스 운영 검증 미완료 | distinct 쿼리 성능 저하 가능 | 운영팀 확인 + 필요 시 추가 | Medium |
| 4 | 프로파일 변경 audit 없음 | 누가 언제 변경했는지 추적 불가 | Phase 1 admin API에서 history 컬렉션 추가 검토 | Low |

---

## 6. 관련 문서

| 문서 | 내용 |
|------|------|
| [README.md](README.md) | 진입점, 디렉토리 맵 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 시스템 설계 + Gotchas (G10: Pydantic `model_*` 예약어) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | TDD / 개발 워크플로우 |
| [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md) | Phase 0 요구사항 (§5 EARS DB 스키마) |
| `~/Developer/ARS/WebManager/docs/SCHEMA.md` | EARS DB 외부 컬렉션 풀 스키마 (EQP_INFO 포함) |
| `src/db/models.py` | 본 문서의 Pydantic 모델 정의 (single source of truth) |
| `src/db/repository.py` | 리포지토리 로직 |
| `src/db/seed.py` | 기본 시드 정의 |
| `src/config/constants.py` | 컬렉션 이름 상수 |
