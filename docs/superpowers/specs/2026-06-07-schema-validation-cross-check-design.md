# 설계: SCHEMA §5 검증 3-way 대조 테스트 (Phase 1)

> 작성일: 2026-06-07 · 상태: 설계 승인 대기 · 대상 브랜치: feat/monitoring-v2-phase0

## 1. 목적

SCHEMA.md는 RESOURCE_MONITOR_PROFILE의 **단일 명세(single source of truth)**이고, 그 §5 검증 규칙을 두 곳이 각각 구현한다:

- **백엔드**: `src/db/models.py`의 Pydantic validator(구조 검증 §5.1~5.4·5.6) + `validate_effective`(참조 무결성 §5.5·5.7·5.8)
- **playground**: `docs/resource-monitor-profile-playground.html`의 `validate(p)` (JS, §5.1·5.5·5.6·5.7 일부)

같은 명세를 두 번 구현했으므로, **동일 케이스를 양쪽에 돌려 SCHEMA가 정한 정답과 대조**하면 어느 쪽이든 명세와 어긋난 곳이 드러난다(differential testing). 이 문서는 그 대조 테스트의 설계를 정의한다.

## 2. 범위

| 포함 | 제외 |
|------|------|
| Phase 1 fact 전수: `max`/`min`/`avg`/`last`/`p50`·`p90`·`p95`·`p99`/`spike_count` (+ state = min/max) | Phase 2/3 fact(`duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`) |
| §5 **저장 시점 검증 판정**(valid/invalid + 위반 규칙) | 런타임 fact 계산(ES 집계) — playground에 없으므로 대조 불가 |
| 생성 프로파일 JSON의 백엔드 수용성 | 알림 발송·cooldown·스케줄 동작 |

## 3. 확정된 설계 결정

1. **대상(SUT)**: playground ↔ 백엔드 **3-way 대조** (expected/backend/playground)
2. **fact 범위**: Phase 1 전수 먼저 (그 이상은 playground 확장 동반 — 후속)
3. **정답(oracle)**: **SCHEMA 기준 라벨링** — 케이스마다 사람이 검수한 `expected` + `expected_violations(§5.x)`
4. **아키텍처**: 단일 JSON 케이스 매트릭스 + 백엔드 pytest 러너 + playground Playwright 러너 + 대조 리포트

## 4. 아키텍처 & 데이터 흐름

```
tests/data/schema_cases.json   (단일 진실 — SCHEMA에서 라벨링한 케이스)
        │
        ├──▶ 백엔드 러너 (pytest)         : MonitorProfile(**p) + validate_effective → 판정 → expected 대조 (전 케이스)
        │
        └──▶ playground 러너 (Playwright) : window.__rmp.validateCase(p) → 판정 → expected 대조
        │                                   (playground_supports=true 케이스만; false는 GAP 집계)
        ▼
   대조 리포트 (case별 expected / backend / playground 3열 + 불일치·갭 목록)
```

## 5. 케이스 매트릭스

### 5.1 케이스 스키마

```jsonc
{
  "id": "op_max_le",                 // 유일 식별자
  "ref": "§5.5",                     // 근거 규칙
  "desc": "max에 <= 연산자 (type↔op 부적합)",
  "profile": { /* 완전한 프로파일 JSON */ },
  "expected": "invalid",             // valid | invalid
  "expected_violations": ["§5.5"],   // invalid일 때 위반 규칙 목록
  "playground_supports": true        // playground가 이 규칙을 검증하는가
}
```

### 5.2 규칙별 도출 기법

| 규칙 | 기법 | 케이스 개요 |
|------|------|------------|
| §5.5 type↔op 적합성 | **결정 테이블** | Phase1 type × op(`>=`,`>`,`<=`,`<`,`==`,`!=`) 전수 → 허용/거부. ALLOWED_OPS: max=`>`,`>=` / min=`<`,`<=`,`==` / avg=`>`,`>=`,`<`,`<=` / last=비교·등치 / pNN=`>`,`>=`,`<`,`<=` / spike_count=`>`,`>=` |
| §5.5 참조 무결성 | 동등 분할 | M 미존재 / T 미선언 / 정상 |
| §5.6 interval ≤ window | **경계값** | window=15 기준 interval = 14(valid)·15(valid, ==경계)·16(invalid) |
| §5.1 유일성 | 동등 분할 | 한 measure 내 type 중복(invalid) / measure id 중복(invalid, **GAP**) |
| §5.7 | 동등 분할 | notify 미존재(invalid) / quantifier=count인데 count_min<1(invalid, **GAP**) |
| §5.8 proc 일관성 | 조합 | 한 rule이 서로 다른 proc measure 참조(invalid, **GAP**) |
| spike_count 필수 params | 결정 테이블 | over 누락 / direction 누락 (invalid, 구조) |
| 정상 baseline | — | 각 Phase1 type 정상 프로파일 (valid) |

예상 케이스 수: **60~90개** (대부분 §5.5 결정 테이블).

### 5.3 정답 라벨 검수 (사람)

`expected`/`expected_violations`는 **사람이 SCHEMA를 근거로 검수·승인**한다. 자동 생성된 정답지로 자기검증하면 "가짜 그린" 위험이 있으므로, 이 라벨만은 사람 게이트를 둔다.

## 6. playground 변경 — 최소 침습

단일 HTML 원칙 유지. 검증 진입점만 노출:

```js
window.__rmp = {
  validateCase(profile) { /* 임의 profile JSON → { valid:boolean, violations:[§5.x] } */ },
  FACT_TYPES, ALL_OPS
};
```

- 현재 `validate(p)`는 이미 profile을 인자로 받으므로, **각 위반 메시지에 머신 판독용 규칙 코드(`§5.x`)를 병기**하면 된다(사용자 UI의 평문 메시지는 그대로). UI 동작·외관 변화 없음.

## 7. 백엔드 러너

`tests/unit/test_schema_cases_xcheck.py`:

1. `MonitorProfile(**case.profile)` 생성 → `pydantic.ValidationError`면 invalid(구조 §5.1~5.4·5.6)
2. 통과하면 `validate_effective(profile)` → 반환 리스트 비어있지 않으면 invalid(참조 §5.5·5.7·5.8)
3. 예외/메시지를 `§5.x`로 매핑 → `expected` 및 `expected_violations`와 대조(assert)

## 8. 갭 처리 & 회귀 가드

- `playground_supports:false` 케이스(measure id 중복·count_min·proc 일관성)는 playground 러너에서 **GAP으로 집계**(실패 아님).
- → "playground가 백엔드 대비 못 잡는 규칙"이 숫자로 남아, 후속에 playground를 확장하면 갭이 줄어드는 **회귀 지표**가 된다.

## 9. 메타 self-test (테스트의 테스트)

일부러 틀린 케이스 1개(예: `expected`를 거짓 라벨)를 주입해 **러너가 불일치를 실제로 검출**하는지 확인한다. 러너가 "항상 통과"하는 가짜 그린이 아님을 보증.

## 10. 완료 조건 (Definition of Done) — /goal 입력용

다음이 모두 충족되면 완료:

1. `tests/data/schema_cases.json`에 §5.2 표의 모든 규칙을 커버하는 케이스가 존재하고, 각 케이스에 `expected`/`expected_violations`/`playground_supports`가 채워져 있다.
2. **백엔드 러너**(`pytest tests/unit/test_schema_cases_xcheck.py`)가 **전 케이스에서 expected와 일치**하여 green.
3. **playground 러너**(Playwright)가 `playground_supports:true` 케이스에서 **전부 expected와 일치**하고, `false` 케이스는 GAP으로 분류해 개수를 보고한다.
4. **메타 self-test**가 의도적 불일치를 검출함을 보인다.
5. 대조 리포트(`docs/`에 마크다운)에 케이스별 `expected/backend/playground` 3열 표 + 불일치 0건 + GAP 목록이 출력된다.
6. 위 1~5의 실행 결과(명령어 + 출력 요약)를 대화에 출력했다.

## 11. 산출물

- `tests/data/schema_cases.json` — 라벨링된 케이스 매트릭스
- `tests/unit/test_schema_cases_xcheck.py` — 백엔드 러너 + 메타 self-test
- playground 러너 스크립트(Playwright) — `window.__rmp` 주입·전수 평가
- `docs/resource-monitor-profile-playground.html` — `window.__rmp` 훅 + 규칙 코드 병기(최소 변경)
- 대조 리포트 마크다운 — `docs/schema-xcheck-report.md`
