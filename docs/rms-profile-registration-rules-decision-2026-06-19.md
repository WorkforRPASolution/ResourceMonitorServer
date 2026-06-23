# RMS_MONITOR_PROFILE 등록 규칙 결정 (API ↔ 엔진 정렬)

- **날짜**: 2026-06-19
- **상태**: 결정(확정 v1) — 구현 대기
- **범위**: RMS(이 repo) + WebManager(별도 repo)
- **요약**: 프로파일 등록/수정 API가 **엔진이 실제로 처리 가능한 집합보다 좁게** 막고 있다. stale해진 가드 2개(`_check_interval_scope`, WebManager의 "`*/*/*` 먼저" 제약)를 제거해 **API 등록 가능 집합 = 엔진 처리 가능 집합** 으로 정렬한다. 데이터 무결성 가드는 유지한다.

---

## 1. 배경 / 문제

RMS_MONITOR_PROFILE은 scope별 overlay 문서들의 cascade fold(전역 `*/*/*` → 공정 `P/*/*` → 모델 `P/M/*` → 장비 `P/M/E`)로 effective 프로파일을 만든다. 엔진(스케줄러+분석)은 다음을 **이미 정상 처리**한다(코드/테스트로 확인):

- deep scope(`P/M/*`, `P/M/E`)에 단독으로 존재하는 문서도 스케줄·평가됨
  - `get_scheduling_intervals`(`src/db/repository.py:242`)가 `{"scope.process": {"$in":[process,"*"]}}` 로 deep 문서까지 interval을 수집 → `(process, interval)` job 등록
  - `_cascade_triples`(`src/db/repository.py:297`)가 self 트리플 포함 → `resolve_profile`이 deep 단독 문서를 fold
  - 회귀 가드: `tests/integration/test_mongo_real.py::test_get_scheduling_intervals_eqp_only_doc`(부모 없는 eqp 단독 문서의 interval이 스케줄됨을 검증)
- deep scope에서 같은 `rule.id`로 주기(`interval_minutes`)를 재정의해도, `(process, interval)` 틱이 장비별 fold 결과를 평가하므로 정상 발화함

**그러나 쓰기 API는 이를 막는다.** `_check_interval_scope`(`src/api/profiles.py:130-165`, `_validate_composed:122`에서 호출)가:

1. model/eqp 레벨 overlay가 **전역+공정 레벨에 없는 주기**를 effective에 들이면 → **422**
2. 부모(전역/공정)가 없으면 비교 기준(`process_intervals`)이 **빈 집합**이라, deep 단독 문서의 enabled rule 주기가 **무엇이든 전부 거부**

→ "엔진은 되는데 API는 막힘" 비대칭. 그 결과 *"특정 모델만 다른 주기"* 같은 정당한 요구가 깔끔하게 표현되지 않고(상위에 주기를 먼저 등록해야 하며, 그러면 전 장비에 영향), deep 단독 모니터링 등록도 불가능하다.

## 2. 근본 원인 (git 히스토리로 확인)

- **2026-06-05 `b2ad0de`** — `_check_interval_scope` 도입. *당시* 스케줄러는 `resolve_profile(process,*,*)`로만 cadence를 뽑아 **deep scope 주기를 실제로 못 잡았다** → deep 새 주기 = "저장됐지만 안 도는 유령 규칙(silent lost breach)"이라는 **진짜 버그**였고, 가드는 정당한 방어였다.
- **2026-06-07 `e933f7b`** ("reload가 eqp/model 단독 프로파일도 스케줄") — `get_scheduling_intervals` 도입, deep scope 스케줄링 지원. **이 시점에 가드의 전제가 깨졌다.**
- 이후 가드는 재검토 없이 잔존. → 현재의 비대칭은 **엔진과 가드가 따로 진화하다 reconcile되지 않은 역사적 산물**이며, 가드의 명분("스케줄 안 됨")은 stale이다.

## 3. 결정

**API 등록 가능 집합을 엔진 처리 가능 집합과 일치시킨다.** stale 가드는 제거하고, 엔진이 의존하는 무결성 가드는 유지한다. **순수 정렬**(별도 거버넌스 장치 미도입)으로 진행한다.

---

## 4. 등록 규칙 (확정 v1)

### I. 허용 규칙
- **R1 상속** — 자식 scope는 부모의 measure/rule/notify를 **키 기준 그대로 상속**한다.
- **R2 재정의** — 더 구체적 scope에서 **같은 키**(rule·measure = `id`, notify = 채널 *이름*)로 재선언하면 그 객체를 **통째로 교체**한다.
  - 키를 제외한 **모든 항목 재정의 가능 — 주기(`interval_minutes`) 포함**.
  - *whole-object 교체*다(필드 단위 부분 병합 없음) — 재선언 시 전 필드를 다시 기술해야 한다.
  - 특수 케이스: 상속 rule을 같은 id로 `enabled:false` 재선언 → 그 scope에서 뮤트(soft tombstone).
- **R3 독립 등록** — 부모 레벨 정보 유무와 무관하게 **어느 레벨(`P/*/*`, `P/M/*`, `P/M/E`)이든 단독 등록 가능**하다.

### II. 유지되는 무결성 제약 (제거 대상 아님 — 엔진이 의존)
- **C1 참조 무결성**(`validate_effective`) — rule의 `when` fact → 합성 effective에 measure 존재 / rule의 `notify` → 채널 존재. (위반 422)
  - R3의 "self-contained" 요건이 곧 C1: 부모가 없으면 그 문서가 자기 rule이 참조하는 measure·notify를 직접 정의해야 한다.
- **C2 `interval_minutes ≤ 참조 measure의 window_minutes`** (사건 누락 방지)
- **C3** 구조 검증(Pydantic) · path/body `id` 일치 · 낙관적 버전 락

### III. 제거 대상 (엔진 대비 stale)
- **G1 `_check_interval_scope`** (cadence locality) — 제거. R2의 주기 재정의 + R3의 deep 단독 등록이 열린다.
- **G2 WebManager UI "`*/*/*` 먼저 등록" 요구** — RMS엔 없는 제약(process 레벨은 가드 면제 + self-contained면 validate 통과). 제거.

### 수용하는 트레이드오프 (순수 정렬 선택의 결과)
deep overlay(특히 장비 레벨)가 **공정 전체에 도는 짧은 주기**(예: 1분 틱)를 도입해도 막는 장치가 없다. 의도된 선택 — 필요해지면 별도로 주기 하한선/blast-radius 경고를 추가할 수 있다(이번 범위 밖).

---

## 5. 동작 예시 (변경 전 → 후)

전제: 전역 `*/*/*`에 `cpu_warn@5`(enabled).

| 시나리오 | 변경 전 | 변경 후 |
|---|---|---|
| `P/M/*`에서 `cpu_warn` 주기 `5→10` 재정의 | **422** | **허용** — M 장비만 10분 평가, 나머지 5분 |
| `P/M/*` 단독(부모 없음)에 measure+rule+notify 등록 | **422** | **허용**(self-contained 시) |
| `P/M/E` 단독(부모 없음) 등록 | **422** | **허용**(self-contained 시) |
| `P/M/*`에서 `cpu.max`가 measure 없이 참조 | 422 (C1) | **여전히 422** (C1 유지) |
| rule `interval > 참조 measure window` | 422 (C2) | **여전히 422** (C2 유지) |

## 6. 마이그레이션 영향

- **DB 스키마 변경 없음, 데이터 마이그레이션 불필요.** 가드 제거는 *수락 범위를 넓히기만* 한다.
- **기존 동작 무변경**: 현재 저장돼 동작 중인 프로파일은 더 강한 가드를 통과했으므로 가드 제거 후에도 그대로 유효. 새로 *허용*되는 건 기존에 422로 거부되던 쓰기뿐이다.
- 엔진은 새로 허용되는 구성을 **이미 정상 처리**(§1) → 런타임 변경 없음.

## 7. 변경 범위 & 회귀 테스트 계획

### RMS (이 repo)
- **코드**: `src/api/profiles.py` — `_check_interval_scope` 함수 + `_validate_composed:122`의 호출 제거. (나머지 `validate_effective`/`lint_effective`는 유지)
- **테스트**:
  - `tests/unit/test_profiles.py::test_model_overlay_new_interval_rejected` → **반전**(`..._allowed`: 422 아님, 201/200 확인)
  - `..._existing_interval_ok` → 더 이상 특수 케이스 아님(일반 통과로 유지/병합)
  - **신규**: deep 단독 등록 허용(부모 없는 `P/M/*`/`P/M/E`에 self-contained 문서 → 성공) 테스트
  - **신규/확인**: C1·C2가 deep scope에서도 여전히 거부함을 가드(참조 깨짐·interval>window)
  - 엔진 측 deep-scope 동작은 기존 `test_get_scheduling_intervals_eqp_only_doc` 등으로 이미 커버
- **문서**: `SCHEMA.md §6.4`에서 cadence-locality 제약(현재 line 335) 삭제 + stale 근거 정정. 관련 가드 docstring/메시지 잔재 정리.

### WebManager (별도 repo — 핸드오프)
- 클라 미러 가드 제거: `validateProfileItem.js`의 interval-scope 검사(가드 5, `errors.interval_minutes`) — *WebManager 분석 기준*
- UI의 "`*/*/*` 먼저 등록" 제약 제거 + deep scope에서 주기 입력 허용
- (선택) effective/적용값 화면에서 deep scope cadence가 그대로 보이는지 확인

## 8. 롤백
가드 제거는 단순 되돌리기로 복구 가능(함수+호출 복원, 테스트 원복). 데이터 영향이 없으므로 롤백 리스크 낮음.

## 9. 참조
- 코드: `src/api/profiles.py:122,130-165` · `src/db/repository.py:242,266,297` · `src/analyzer/engine.py` · `src/db/models.py` `fold_profiles`
- 테스트: `tests/unit/test_profiles.py:312,396` · `tests/integration/test_mongo_real.py:170`
- 커밋: `b2ad0de`(가드 도입, 2026-06-05) · `e933f7b`(deep scope 스케줄, 2026-06-07)
- 관련 결정: `docs/rms-enabled-rulelevel-decision-2026-06-15.md`(rule 단위 fold) · `docs/rms-cadence-reconcile-webmanager-ui-handoff-2026-06-11.md`(cadence 자동 reconcile)
