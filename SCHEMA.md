# SCHEMA — ResourceMonitorServer (모니터링 기준정보)

> **버전: v2.0 (2026-06-05) — 구현 완료 (Phase 1)**
>
> ✅ **구현 상태**: 이 v2 스키마(단일 컬렉션 `measures`/`rules`/`notify` + scope cascade)는 **구현 완료**되었습니다 — Phase 1 fact가 ES 집계→rule 평가→이메일 알림까지 end-to-end로 동작하며 실 ES·Mongo·Redis 통합 테스트가 green입니다. `src/db/models.py`가 Pydantic 단일 진실 소스입니다. Phase 2/3 fact(`duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`)는 **스키마·검증은 수용하되 엔진은 skip+경고**합니다(후속 구현). 영역별 현황은 [§13 구현 상태](#13-구현-상태)를 보세요.
>
> 설계 결정 배경은 [ARCHITECTURE.md](ARCHITECTURE.md), 원본 요구사항은 [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md), 외부 컬렉션 풀 스키마는 `~/Developer/ARS/WebManager/docs/SCHEMA.md`, 실제 수집 메트릭 정의는 `~/Developer/ARS/ResourceAgent/docs/EARS-METRICS-REFERENCE.md` 참고.

---

## 0. 개요 — 단일 컬렉션 결정

### 0.1 컬렉션은 1개

모니터링 기준정보는 **단일 컬렉션 `RESOURCE_MONITOR_PROFILE`** 하나로 관리합니다. (구설계의 `RESOURCE_MONITOR_RULE` 별도 컬렉션은 **폐기**.)

분리하지 않은 이유(요약):

- "단순 임계값 vs 복합 조건"은 자연스러운 컬렉션 경계가 아니라 **표현식 복잡도의 연속선**입니다. 성숙한 모니터링 시스템(Prometheus·Zabbix·Datadog 등) 중 복잡도를 1차 데이터 경계로 삼는 곳은 없습니다.
- 단순/복합을 두 컬렉션으로 가르면 임계값·알림·스코프 해석이 양쪽에 **중복**되고, 단순 임계값에 "지속시간" 한 줄만 추가해도 컬렉션 간 **강제 이전**이 발생합니다.
- 한 장비 묶음에 필요한 모든 것(잴 것/판단할 것/보낼 것)을 한 문서에 두면 조회 1회·원자적 수정·스코프 해석 1벌로 끝납니다.

→ 단순 임계값은 **조건 1개짜리 rule**, 복합 조건은 **조건 여러 개짜리 rule**로 표현합니다. 같은 구조, 조건 개수 차이뿐.

### 0.2 3계층 구조

| 계층 | 역할 | 비유 |
|------|------|------|
| `scope` | 이 설정을 **어느 장비에** 적용할지 | 누구에게 |
| `measures[]` | **무엇을 어떻게 잴지** (집계/분석 → fact 산출) | 잰다 |
| `rules[]` | 잰 값으로 **언제 경보할지** (조건 + 평가 주기) | 판단한다 |
| `notify{}` | **어떻게 알릴지** (이메일 코드, cooldown) | 알린다 |

데이터 흐름: `scope로 대상 결정 → measures로 잼 → rules로 판단 → notify로 발송`.

### 0.3 데이터베이스 위치

모든 컬렉션은 단일 `EARS` MongoDB에 공존합니다. RMS는 별도 DB를 만들지 않고 Akka 서버와 같은 DB를 공유합니다.

```
EARS (database)
├── EQP_INFO                    ← Akka 소유 (기존), RMS는 read-only
├── EMAIL_TEMPLATE_REPOSITORY   ← Akka 소유 (기존), 알림 발송 시 참조
├── EMAIL_RECIPIENTS / EMAILINFO ← Akka 소유 (기존)
└── RESOURCE_MONITOR_PROFILE    ← ★ RMS 소유 (모니터링 기준정보, 단일 컬렉션)
```

| 위치 | 설정 |
|------|------|
| `src/config/settings.py` | `mongo_db: str = "EARS"` (default) |
| 환경 변수 (override) | `MONITOR_MONGO_DB` |
| 상수 | `COLL_PROFILE = "RESOURCE_MONITOR_PROFILE"` (`src/config/constants.py`) |

> `RESOURCE_MONITOR_*` prefix로 기존 컬렉션과 충돌 없음. 구설계의 `COLL_RULE` 상수는 제거합니다.

---

## 1. 문서 구조

### 1.1 한눈에

```jsonc
{
  "_id": ObjectId,
  "scope":    { "process": "*", "eqpModel": "*", "eqpId": "*" },  // 어느 장비에
  "enabled":  true,
  "governance": { "version": 1, "updated_by": "...", "updated_at": ISODate, "change_reason": "..." },

  "measures": [ /* 잰다: 무엇을·어떻게 → fact 산출 */ ],
  "rules":    [ /* 판단: fact로 경보 결정 + 평가 주기 */ ],
  "notify":   { /* 알린다: 전달 채널 */ }
}
```

### 1.2 `scope` — 적용 대상

EQP_INFO 계층을 따릅니다. 구체적 scope가 더 넓은 scope를 **상속**합니다([§6](#6-스코프-해석--계층-상속-cascade)).

| 경로 | 타입 | 필수 | 기본 | 비고 |
|------|------|------|------|------|
| `scope.process` | string | ✓ | — | EQP_INFO.process. `"*"`=전체. 파티션/인덱스 라우팅 키이기도 함 |
| `scope.eqpModel` | string | | `"*"` | **카멜케이스**. Pydantic alias: `eqp_model` ↔ JSON `model` ↔ Mongo `eqpModel` |
| `scope.eqpId` | string | | `"*"` | 단일 장비 |

> ⚠️ `scope.process`(설정 적용 범위 = ES 인덱스명 파티션 키 `{process_lower}_all-…`)와 measure의 `proc`(EARS row 정체성 = `EARS_PROCNAME` 필드)은 **다른 개념**입니다. 이름이 비슷하니 혼동 금지.
>
> **상속(cascade)**: 구체적 scope 문서는 더 넓은 scope를 상속하고 **바꿀 것만 담는 얇은 overlay**입니다(전체 복사 금지). 합성 규칙은 [§6](#6-스코프-해석--계층-상속-cascade).

### 1.3 `measures[]` — 잰다

각 measure는 "무엇을·어떻게 집계해 어떤 fact를 산출하는가"의 정의입니다. **주기(interval)는 갖지 않습니다** — 주기는 rule이 소유하고, measure는 **집계창(window)** 만 갖습니다.

| 경로 | 타입 | 필수 | 기본 | 비고 |
|------|------|------|------|------|
| `id` | string | ✓ | — | 문서 내 유일. rule이 `"id.type"`으로 참조하는 핸들 |
| `category` | string | ✓ | — | **EARS category** (cpu/memory/disk/…). 충돌 방지에 필수 |
| `metric` | string | ✓ | — | **EARS metric**. 와일드카드 가능(`"*_core_load"`, `"*"`) |
| `proc` | string | | `"@system"` | **EARS proc**. `"*"`이면 proc별로 fact 산출 |
| `window_minutes` | int | ✓ | — | 집계 시간창 (이 measure의 모든 fact가 공유) |
| `group_by` | string[] | | `["eqpId"]` | `proc=="*"`이면 자동 `["eqpId","proc"]` |
| `expand` | enum | | `"scalar"` | `metric`이 와일드카드면 자동 `"instance"` (필드별 fact 집합 산출) |
| `metric_kind` | enum\|null | | `null` | `gauge`/`counter`/`cumulative`. **선택 lint 힌트** (강제 아님) |
| `bucketing` | object\|null | 조건부 | `null` | 시간축 fact(`duration`/`growth_rate`/`moving_avg`/`trend`)가 있으면 **필수** |
| `bucketing.seconds` | int | ✓* | — | date_histogram 버킷 크기 |
| `bucketing.points` | int\|null | 조건부 | `null` | `moving_avg`/`trend`가 있으면 필수 |
| `baseline` | object\|null | 조건부 | `null` | `baseline_dev` fact가 있으면 **필수** |
| `baseline.days` | int | | `7` | 과거 며칠 |
| `baseline.same_hour` | bool | | `true` | 동일 시간대만 |
| `baseline.min_points` | int | | `30` | 표본 부족 시 경보 안 함 |
| `baseline.deviation_floor` | float | | `1.0` | 분모≈0 폭주 방지 |
| `facts[]` | array | ✓ | — | 산출할 fact 목록. 각 항목 = fact 1개 |
| `facts[].type` | enum | ✓ | — | **type 이름이 곧 fact 이름** ([§2 카탈로그](#2-type--fact-카탈로그)). 한 measure 내 **유일** |
| `facts[].over` | float | 조건부 | — | `spike_count`/`duration` 필수 — "사건 경계" |
| `facts[].direction` | enum | 조건부 | — | `spike_count`/`duration`: `above`/`below`, `zscore`: `high`/`low` |
| `facts[].mode` | enum | | `last_minus_first` | `delta`: `last_minus_first`/`max_minus_min` |
| `facts[].unit` | enum | | `per_hour` | `growth_rate`: `per_hour`/`per_day` |

**핵심 원칙: 1 measure 항목 = 1 fact = 1 type.** `bucketing`/`baseline`처럼 *함께 해석되는 통계가 공유하는 파라미터*는 measure 레벨에 한 번만 두어 일관성을 강제합니다(예: `moving_avg`와 `trend`가 같은 평활화 창을 쓰도록).

### 1.4 `rules[]` — 판단 + 평가 주기

| 경로 | 타입 | 필수 | 기본 | 비고 |
|------|------|------|------|------|
| `id` | string | ✓ | — | 문서 내 유일 |
| `interval_minutes` | int | ✓ | — | **평가 주기**. 스케줄 단위 = (process × rule) |
| `severity` | enum | ✓ | — | `WARNING` / `CRITICAL` |
| `combine` | enum | | `AND` | 여러 조건 결합: `AND` / `OR` |
| `when[]` | array | ✓ | — | 조건 목록 |
| `when[].fact` | string | ✓ | — | `"measureId.type"` (예: `"cpu.p95"`) |
| `when[].op` | enum | ✓ | — | `>=` `>` `<=` `<` `==` `!=` `trend==` ([§3](#3-연산자--정량자)) |
| `when[].value` | float\|string | ✓ | — | `trend==`일 때만 문자열(`"increasing"` 등) |
| `when[].quantifier` | enum | | `any` | instance/proc measure 정량화: `any`/`all`/`count` |
| `when[].count_min` | int\|null | 조건부 | — | `quantifier=="count"`일 때 필수 |
| `notify` | string | | `"default"` | `notify` 맵의 채널 이름 |
| `enabled` | bool | | `true` | rule 개별 on/off. `false`면 엔진이 평가 스킵 + 스케줄 cadence에서 제외(문서 레벨 `enabled`와 별개). overlay에서 상속 rule을 `enabled:false`로 덮으면 per-scope 뮤트(소프트 tombstone, [§6.2](#62-합성-규칙-key-기준-병합)) |

> **2단계 임계(WARNING 80 / CRITICAL 95)** 는 같은 measure를 참조하는 **rule 2개**로 표현합니다(`cpu_warn`, `cpu_crit`). 이것이 단일/복합을 통일하고 measure 폭증을 막는 정석입니다.

### 1.5 `notify{}` — 알린다

이름 → 채널 맵. rule이 이름으로 참조해 중복을 제거합니다.

| 경로 | 타입 | 필수 | 기본 | 비고 |
|------|------|------|------|------|
| `notify.<name>.cooldown_minutes` | int | ✓ | — | Redis cooldown TTL (이메일 폭주 방지) |
| `notify.<name>.email_code` | string | | `"RESOURCE_MONITOR"` | `EMAIL_TEMPLATE_REPOSITORY` 매칭 코드 |
| `notify.<name>.email_subcode` | string\|null | | `null` | `null`이면 `"{category}_{severity}"` 자동 |
| `notify.<name>.group_by` | enum | | `"eqp"` | **발송 단위**: `eqp`(장비별, 현행) / `model` / `process`. `eqp` 외엔 같은 그룹의 장비 breach를 **메일 1통으로 집계** |
| `notify.<name>.representatives` | object | | `{}` | 그룹값→대표 eqpId 오버라이드(예 `{"MODEL_A":"EQP001"}`). 그룹 메일의 `hostname`(수신자 해석 기준). 미지정 시 그룹 내 최소 eqpId 자동 |

**cooldown 키** = `{prefix}:cooldown:{process}:{group}:{proc}:{notify}:{severity}` — `group`은 `group_by="eqp"`면 eqpId(현행과 동일), `model`/`process`면 그 값. 같은 (그룹, proc, 알림채널, 심각도) 단위로 억제·집계되며, 같은 notify를 공유하는 rule들은 한 사건으로 묶입니다.

**그룹 발송(`group_by` ≠ `eqp`)**: 한 그룹에 걸린 장비들을 **메일 1통**으로 모읍니다. 1통의 `hostname`=대표 eqpId(`representatives` 지정값 또는 최소 eqpId) → Akka가 **대표의 emailCategory**로 수신자 해석(엔드포인트·EQP_INFO/EMAILINFO 구조 무변경). 걸린 장비 목록은 알림 변수 `AffectedEquipment`/`AffectedCount`로 전달(템플릿에서 사용).

> ⚠️ **라우팅 한계**: 수신자는 *대표 1대*의 emailCategory(`EMAIL-[process]-[model]-[group]`)다. `model` 그룹은 보통 단일 category로 정확하지만, **`process` 그룹은 모델이 섞이면 여러 category**가 되어 대표 모델 담당만 통지되고 타 모델은 누락될 수 있다. 이 경우 `representatives`로 대표를 명시하거나 `model` 단위를 쓴다. (장비별 상세를 본문에 HTML 표로 넣는 `@contents`는 후속 작업.)

---

## 2. type (= fact) 카탈로그

`measures[].facts[].type` 에 넣는 값 = rule에서 `measureId.type`으로 부르는 fact 이름. **닫힌 enum**(자유 텍스트 금지).

| type (=fact) | 산출 의미 | 필수 params | measure 설정 | 허용 op | value 단위 | window 따름 | Phase |
|---|---|---|---|---|---|---|---|
| `max` | 창 내 최댓값 | — | — | `>` `>=` | 메트릭 | ✅ | 1 |
| `min` | 창 내 최솟값 | — | — | `<` `<=` `==` | 메트릭 | ✅ | 1 |
| `avg` | 창 내 평균 | — | — | `>` `>=` `<` `<=` | 메트릭 | ✅ | 1 |
| `last` | 창 내 마지막값 | — | — | 비교·등치(`trend==` 제외) | 메트릭 | ✅ | 1 |
| `p50` `p90` `p95` `p99` | 백분위 | — | — | `>` `>=` `<` `<=` | 메트릭 | ✅ | 1 |
| `spike_count` | `over` 초과 샘플 수 | `over`, `direction` | — | `>` `>=` | 정수(횟수) | ✅ | 1 |
| `duration` | 최대 연속 지속(초) | `over`, `direction` | `bucketing.seconds` | `>` `>=` | 초 | ✅ | 2 |
| `delta` | 변화량 | `mode` | (last_first면 정렬) | `>` `>=` `<` `<=` `!=` | 메트릭 | ✅ | 2 |
| `growth_rate` | 단위시간당 증가 | `unit` | `bucketing.seconds` | `>` `>=` `<` `<=` | 단위/시간 | ✅ | 2 |
| `moving_avg` | 이동평균값 | — | `bucketing.seconds+points` | `>` `>=` `<` `<=` | 메트릭 | ✅ | 2 |
| `trend` | 추세 | — | `bucketing.seconds+points` | `trend==` | enum | ✅ | 2 |
| `zscore` | 표준화 이상도(창 내부) | `direction` | — | `>` `>=` | σ | ✅ | 2 |
| `baseline_dev` | 과거 동일시간대 대비 편차% | — | `baseline{days,same_hour}` | `>` `>=` `<` `<=` | % | ❌ 과거 별도쿼리 | 3 |

> **state_check은 별도 type 없음** — `min`/`max`로 흡수:
> - process_watch `required` 다운 → `min == 0`
> - process_watch `forbidden` 실행 → `max > 0`
> - storage_health `status` 위험 → `max >= 2` (PRED_FAIL 이상)

PRD가 정의한 판단 알고리즘 10종이 이 카탈로그에 1:1로 대응합니다(threshold→max/min, percentile→pNN, spike_count, duration, delta, growth_rate, moving_avg, zscore, baseline→baseline_dev, state_check→min/max).

---

## 3. 연산자 / 정량자

### 3.1 연산자 (`when[].op`)

`>=` `>` `<=` `<` `==` `!=` `trend==`

- **경보 방향**을 op로 표현: 높을때(`>=`)·낮을때(`<=`)·상태(`==`).
- **범위 이탈**(전압 등)은 단일 연산자가 아니라 두 조건 + `combine:"OR"`: `min < 하한` OR `max > 상한`.
- 저장 시점에 **type↔op 적합성**을 검증합니다([§5](#5-검증-규칙-저장-시점)). 예: `max`에 `<=`는 거부, `trend`는 `trend==`만 허용.

### 3.2 정량자 (`when[].quantifier`)

`expand:"instance"` measure(와일드카드 metric, 또는 proc별)는 장비마다 **fact 집합**을 산출합니다(예: 디스크 `C:`/`D:`, 프로세스 여러 개). 정량자로 "어떻게 묶어 판단할지" 지정:

| quantifier | 의미 | 예 |
|---|---|---|
| `any` (기본) | 인스턴스 중 하나라도 조건 충족 | 디스크 중 하나라도 95%↑ |
| `all` | 모든 인스턴스가 충족 | — |
| `count` (+`count_min`) | N개 이상 충족 | 센서 3개 이상 과열 |

---

## 4. 측정 → 판단 평가 모델

### 4.1 스케줄 단위 = (process × rule)

- **measure는 스스로 돌지 않습니다.** rule이 자신의 `interval_minutes`마다 트리거되고, 그때 그 rule이 참조하는 measure만 계산합니다.
- 한 주기 틱에서:
  1. 그 주기의 rule들의 `when[].fact`에서 **점 앞부분(measure id)** 을 모음 (중복 제거)
  2. 해당 measure만 ES에서 계산(가능하면 한 쿼리로 묶음) → fact 산출
  3. fact로 각 rule의 `when` 평가(`combine`/`quantifier`)
  4. 걸린 장비 → cooldown 확인 → notify 발송
- **fact 저장소 없음**: 한 rule 주기 안에서 동기로 계산·판단·발송. (예외: `baseline_dev`만 과거 인덱스를 별도 쿼리)
- **비활성 rule(`enabled:false`)은 제외**: cadence 수집(`get_scheduling_intervals` — 활성 rule의 interval만 job 등록)과 평가(엔진이 틱마다 `rule.enabled`로 필터) 양쪽에서 빠집니다. measure가 비활성 rule에만 참조되면 ES 쿼리도 생략됩니다.
- **interval(평가주기) 변경은 자동 반영(재시작 불필요)**: rule 추가/삭제·enable 토글·overlay 저장 등으로 interval 집합이 바뀌면 스케줄러가 **pod 재시작 없이** 새 cadence를 반영합니다 — 프로파일 쓰기 직후(소유 pod) 또는 주기 reconcile(최대 `MONITOR_SCHEDULER_RECONCILE_INTERVAL_SEC`초, 기본 60). 임계값 등 **내용** 변경은 엔진이 매 틱 Mongo를 재조회하므로 다음 틱에 자동 반영됩니다(reconcile 불필요). 상세는 `ARCHITECTURE.md` 스케줄러 절.

### 4.2 measure가 `window`, rule이 `interval`을 갖는 이유

- `window`(집계창)는 "얼마나 긴 구간을 보고 재나" → **측정의 속성** → measure 소유.
- `interval`(주기)는 "얼마나 자주 판단하나" → **판단의 속성** → rule 소유.
- 같은 measure를 주기가 같은 여러 rule이 참조하면 **한 번만 계산**(엔진 최적화: 같은 (process, interval) rule들을 한 job으로 묶어 공유 measure 1회 계산).

### 4.3 메트릭 타입별 ES 집계 전략 (요약)

| fact | ES 7.11.9 전략 |
|---|---|
| `max`/`min`/`avg` | `terms(eqpId)` → 메트릭 sub-agg. 단일 패스 |
| `last` | 버킷별 `top_hits`(EARS_TIMESTAMP desc, size 1) — 비쌈, 꼭 필요할 때만 |
| `pNN` | `percentiles`(TDigest) sub-agg |
| `spike_count` | `filter(range)` sub-agg의 doc_count |
| `duration` | `date_histogram` + 클라이언트 최대 연속 streak 계산 |
| `delta` | `last_minus_first`: `top_hits` 2개 / `max_minus_min`: max-min |
| `growth_rate` | `scripted_metric`(OLS 기울기) 또는 date_histogram 후 클라이언트 |
| `moving_avg`/`trend` | `date_histogram` + `moving_fn` (classic moving_avg 금지) |
| `zscore` | `extended_stats`(avg+std) → `(max-avg)/std`. std≈0/표본<2 가드 필수 |
| `baseline_dev` | 과거 N일 일별 인덱스 **별도 쿼리** + same_hour range OR |

---

## 5. 검증 규칙 (저장 시점)

프로파일 저장 시 검증 → 위반 시 거부. **구조 검증**(1~4·6)은 Pydantic 모델 생성 시점(`MonitorProfile`/`Measure`/`Condition` 검증자), **참조 무결성**(5·7·8)은 합성된 effective profile에 대해 API 쓰기 경로(`profiles._validate_composed` → `validate_effective`, 위반 **422**)에서 검증:

1. **measure id 유일** / 한 measure 내 **`type` 유일** (1:1 참조 보장)
2. 시간축 fact(`duration`/`growth_rate`/`moving_avg`/`trend`)가 있으면 `bucketing` **필수**, `moving_avg`/`trend`엔 `bucketing.points` 필수
3. `bucketing.seconds × points ≤ window_minutes×60`
4. `baseline_dev`가 있으면 `baseline` **필수**
5. 모든 `rule.when.fact = "M.T"` → **M 존재**, **T가 M의 facts에 선언됨**, **op가 T에 허용**(`ALLOWED_OPS`)
6. `rule.interval_minutes ≤ 참조 measure의 window_minutes` (사건 누락 방지)
7. `quantifier=="count"`엔 `count_min ≥ 1` 필수 / `rule.notify`가 `notify` 맵에 존재
8. 한 rule의 **모든 조건은 동일 proc 차원의 measure를 참조**해야 함 — 서로 다른 `proc`을 섞으면 **거부**(엔진이 `(eqp, proc)`별로 평가하므로 AND 조건이 영영 발화 못 하는 silent lost breach 방지)
9. (lint, **경고**) **dead fact** — 어떤 rule도 참조하지 않는 fact (`lint_effective`)
10. (lint, **경고**) `metric_kind=="gauge"`인데 `delta`/`growth_rate` 사용 (`lint_effective`)

> **합성 후 검증**: 참조 무결성(5·7·8)은 개별 overlay 문서가 아니라 **합성된 effective profile**에 적용합니다(`validate_effective` → 위반 시 422, [§6.4](#64-제약)). overlay 문서 하나만 보면 참조가 깨져 보여도 상위 scope와 합성하면 충족될 수 있기 때문입니다. lint 경고(9·10)는 거부가 아니라 `lint_effective`가 resolve·쓰기 시 **로그**로 surface합니다. 구조 검증(1~4, 6)은 문서 단위(Pydantic 검증).
>
> **비활성 rule(`enabled:false`)도 동일 검증(엄격)**: 위 1~8은 rule의 `enabled` 여부와 무관하게 적용됩니다 — 끈 상태로 깨진 참조를 저장할 수 없으므로 다시 켜는 순간 항상 안전합니다. 단 interval-scope 검사([§6.4](#64-제약))만은 **활성 rule**만 따집니다(비활성 rule이 새 주기를 들여와도 스케줄되지 않으므로 저장 허용 → 그 rule을 켤 때 검사).

---

## 6. 스코프 해석 — 계층 상속 (cascade)

한 장비의 **유효 프로파일(effective profile)** 은 그 장비에 매칭되는 scope 문서들을 **넓은 → 좁은 순으로 합성(fold)** 한 결과입니다. 구체적 scope는 더 넓은 scope를 **상속**하고 바꿀 것만 담습니다(= **sparse overlay**).

> 구설계의 "첫 매치 1개만 쓰고 나머지 무시(**replace**)"는 **폐기**합니다 — 예외 장비마다 전역 설정을 통째 복사해야 하고, 전역을 바꿔도 예외 장비엔 반영되지 않아 드리프트를 낳기 때문입니다. (Zabbix 템플릿 상속·k8s kustomize·Nagios `use`와 같은 cascade 방식.)

### 6.1 합성 순서 (좁은 게 이김)

```
(*,*,*)  →  (process,*,*)  →  (process,model,*)  →  (process,model,eqpId)
```

base에 overlay를 차례로 덮습니다. `uniq_scope` 덕에 각 레벨 최대 1문서라 순서 모호성이 없습니다.

> **현재 도입 범위**: 운영 요구의 대부분은 **전역 + 가장 구체적 1개** 의 2-레이어로 충족됩니다. 중간 레벨(process/model) 예외가 실제로 필요해질 때 4단 fold로 점진 확장합니다(YAGNI). "왜 이 장비가 이 설정인가"가 base+overlay 두 문서로만 설명돼 디버깅이 단순.

### 6.2 합성 규칙 (key 기준 병합)

| 대상 | 병합 키 | 충돌 시 |
|------|---------|---------|
| `measures` | `measure.id` | 구체 문서의 measure가 **통째로 교체** |
| `rules` | `rule.id` | 구체 문서의 rule이 **통째로 교체** |
| `notify` | 맵 이름 | 구체 문서의 채널이 **통째로 교체** |
| `enabled` | — | 한 레벨이라도 `false`면 비활성 (AND, 안전측 실패) |

- 같은 key가 양쪽에 있으면 **구체 문서의 객체가 통째로 이김**(필드 단위 부분 병합 금지 — 결정적·예측가능). 구체 문서에만 있는 key는 추가.
- "임계 하나만 바꾸기" = 그 **rule 하나만** overlay에 다시 적음(작음). 나머지 measures/rules/notify는 전부 상속 → 전역 변경이 자동 전파.
- 출처 추적(provenance)·effective-profile 조회 API는 **구현 완료**(`GET /profiles/effective?withProvenance=1` — 각 항목에 기여 scope 라벨).
- **rule 개별 비활성(소프트 tombstone)은 구현 완료**: overlay에서 같은 `rule.id`를 `enabled:false`로 다시 적으면 통째 교체 규칙에 따라 그 scope에서만 rule이 꺼집니다(전역은 유지). 상속 항목을 목록에서 **완전히 제거**하는 tombstone은 여전히 추후 — 지금은 `enabled:false` 뮤트가 그 역할을 대신합니다.

### 6.3 sparse overlay 예시

전역과 동일하되 EQP001만 CPU critical 임계를 95→85로:

```jsonc
// (*,*,*) 전역 문서는 그대로 두고, 아래 문서만 추가
{
  "scope": { "process": "PHOTO", "model": "MODEL_A", "eqpId": "EQP001" },
  "rules": [
    { "id": "cpu_crit", "interval_minutes": 5, "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "cpu.max", "op": ">=", "value": 85 } ] }   // 이 rule만 override
  ]
  // measures/notify/그 외 rules는 전역 상속. cpu_crit가 참조하는 cpu measure도 전역에서 옴.
}
```

### 6.4 제약

- **schedule/interval override는 process 레벨까지만.** 스케줄러는 process 레벨 effective의 interval 집합으로 `(process, interval)` job을 등록하므로, model/eqp overlay가 **process에 없는 새 interval을 도입하면 API가 422로 거부**합니다(`_check_interval_scope`) — 그러지 않으면 그 rule이 스케줄되지 않아 silent lost breach. 임계/value override는 eqp 레벨 가능. 이 검사는 **활성 rule만** 대상으로 합니다(`enabled:false` rule은 스케줄되지 않으므로 새 interval을 들여와도 저장 허용 → 그 rule을 켜는 쓰기 시점에 재검사됨).
- **참조 무결성은 합성된 effective profile에서 검증**([§5](#5-검증-규칙-저장-시점)) — overlay 문서 하나만 보면 `cpu_crit`이 참조하는 `cpu` measure가 없어 보이지만, 전역과 합성하면 존재하므로 정상.

### 6.5 해석 / 캐시

- `resolve_profile(process, eqp_model, eqp_id)`는 "첫 매치 반환"에서 "매칭 scope 문서 수집 → base→specific fold"로 **변경 완료**. 4개 scope는 단일 `$or` 쿼리로 가져와 N+1 회피.
- effective profile을 `f"{p}:{m}:{e}"` 키로 캐시(`TTLCache(maxsize=10000, ttl=300)`). 2만 eqpId > maxsize이므로 eqpId가 아니라 **고유 effective profile(버킷) 단위 캐시**로 카디널리티를 낮춤. `upsert()` 시 캐시 무효화.

> ✅ **dead-path 수정 완료**: 과거 엔진은 `resolve_profile(process,"*","*")`로 **process 레벨만** 해석해 model/eqp override가 알림에 전혀 반영되지 않았습니다(dead path). 현재는 **per-eqp 해석**(장비별 resolve 후 동일 effective profile 장비를 버킷팅 → ES 쿼리는 버킷당 1회)으로 수정되어 override가 실제 알림에 반영됩니다(통합 테스트 E7이 회귀 가드 — [§13](#13-구현-상태)).

---

## 7. 인덱스

```js
db.RESOURCE_MONITOR_PROFILE.createIndex(
  { "scope.process": 1, "scope.eqpModel": 1, "scope.eqpId": 1 },
  { unique: true, name: "uniq_scope" }
)
db.RESOURCE_MONITOR_PROFILE.createIndex({ "enabled": 1 })
```

- 한 프로파일 = 정확히 한 `(process, eqpModel, eqpId)` 조합. `create()`의 `DuplicateKeyError → ProfileAlreadyExistsError` 변환이 이 불변조건에 의존.
- `init_repos()`가 startup 시 멱등 생성. Debug Read-Only 모드에선 스킵.

---

## 8. ES 인덱스 전제 — EARS 행 형식

### 8.1 확정된 사실

운영 ES 문서는 **EARS row**입니다(PRD §7.2). 메트릭마다 top-level numeric 필드를 두는 게 아니라 **(장비, 메트릭, 샘플)당 한 행**이고, 메트릭 정체성은 필드값(필터)이며 모든 fact는 단일 `EARS_VALUE` 컬럼을 집계합니다. (구현: `src/es/queries.py`)

- 인덱스 패턴: `{process_lower}_all-{YYYY.MM.DD}` (일별, **UTC 캘린더 롤오버** — 운영 확인: 한 인덱스가 `EARS_TIMESTAMP` `00:00:00Z~23:59:59Z`를 담음). 인덱스 날짜는 시간범위 필터와 동일하게 **UTC**로 계산(`resolve_index_range`, UTC `now` 주입). 자정(UTC) 가로지르면 콤마 결합.
- 필드 역할:

  | 필드 | 타입 | 역할 |
  |------|------|------|
  | `EARS_TIMESTAMP` | date | time range 필터 (`build_time_range_filter`) |
  | `EARS_VALUE` | double | **유일한 집계 대상** (max/min/avg/percentiles/range…) |
  | `EARS_CATEGORY` | text + `.keyword` | category term 필터 (cpu/memory/disk…) |
  | `EARS_METRIC` | text + `.keyword` | metric term 필터 / 와일드카드 인스턴스 발견(terms) |
  | `EARS_PROCNAME` | text + `.keyword` | proc 필터(`measure.proc`) 또는 proc 그룹(`proc=="*"`) |
  | `EARS_EQPID` | text + `.keyword` | 장비 group_by (`terms`, size=30000) |

- **모든 문자열 필드는 `text` + `.keyword` 서브필드** (ES 기본 동적 매핑, **운영 확인**) → term/terms 필터와 terms aggregation은 **반드시 `<field>.keyword`** 를 써야 한다(bare text로는 집계 400 실패·필터 토큰 mismatch). suffix는 `settings.es_keyword_suffix`(기본 `.keyword`)로 주입하며, bare keyword 클러스터면 `""`로 override. `EARS_VALUE`(numeric)·`EARS_TIMESTAMP`(date)는 bare.
- 집계 중첩(외→내): `by_eqp(EARS_EQPID)` → *(proc=="\*"면)* `by_proc(EARS_PROCNAME)` → *(expand=="instance"면)* `by_metric(EARS_METRIC)` → fact별 leaf sub-agg(키 = fact type명). `src/analyzer/es_parser.py`가 같은 중첩을 파싱 — sub-agg 키가 쿼리↔파서 계약.

### 8.2 해소된 가정 + 남은 의존

설계 시 미해결이던 blocker들은 운영 ES 확인으로 **해소**되었습니다:

1. ~~EARS `proc`의 ES 착지 필드~~ → **`EARS_PROCNAME`** 으로 색인됨(`@system`/프로세스명/NIC). proc 필터·그룹(`group_by:[eqpId,proc]`)이 이 필드로 동작.
2. ~~`category`의 ES 색인 여부~~ → **`EARS_CATEGORY`** 로 색인됨. cpu/memory의 동일 `total_used_pct`는 `EARS_CATEGORY` term으로 분리 집계 (통합 테스트 E8이 혼입 회귀 가드).
3. ~~인덱스 일자 롤오버 시간대~~ → **UTC 확정**(운영 인덱스의 `EARS_TIMESTAMP` min/max가 `00:00:00Z~23:59:59Z`). 과거 `resolve_index_range`가 `local_tz`(Asia/Seoul)로 인덱스 날짜를 골라 KST 00:00–09:00에 엉뚱/빈 인덱스를 여는 버그가 있었으나, **인덱스 날짜를 UTC로 계산하도록 수정**(test_queries `test_index_date_uses_utc_not_local_tz` 가드).

남은 의존 / 운영 전제:

4. **baseline 인덱스 보존** (Phase 3) — `baseline_dev`는 과거 N일 일별 인덱스가 존재해야 함.
5. **샘플 emit 주기** (Phase 2/3) — `spike_count`(샘플수)·`duration`(bucket_seconds)·percentile(표본 충분성) 의미에 직결.
6. **인입의 UTC 롤오버 유지** (지속 운영 전제) — 위 #3 수정은 인덱스가 **UTC 자정에 롤오버**한다는 데 의존합니다. 수집팀(EARS→ES 인입)이 일별 인덱스를 로컬 자정 기준으로 바꾸면 `resolve_index_range`가 다시 어긋납니다. ① 인입팀과 **"UTC 롤오버 유지"를 합의**하거나, ② 더 견고히 하려면 인덱스 날짜 시간대를 **설정값(예: `es_index_tz`)으로 분리**하세요. (`baseline_dev`의 과거 인덱스 날짜 계산도 같은 UTC 규약을 따라야 함 — [PHASE2-3-PLAN §5](docs/PHASE2-3-PLAN.md).)

---

## 9. `EQP_INFO` (외부, read-only)

**소유자**: Akka 서버. **RMS 액세스**: read-only (절대 write 안 함). **리포지토리**: `EqpInfoRepository`.

### 9.1 RMS가 쓰는 필드

| 필드 | 용도 |
|------|------|
| `eqpId` | 장비 식별자 — **알림 `hostname` 필드** (Akka가 hostname을 eqpId로 취급: 수신자 조회·분임조·`@Hostname`) |
| `process` | `get_distinct_processes()` — 파티셔닝 키 |
| `eqpModel` | scope 매핑 (resolve_profile) + 알림 `model` |
| `ipAddr`(→`ip`), `line`, `category` | 알림 본문 |
| `localpc` | (구) hostname 소스였으나 **더 이상 알림에 사용 안 함** — projection엔 잔존(무해) |
| `onoff`, `webmanagerUse` | **활성 필터** (둘 다 1) |

### 9.2 활성 필터

```python
EqpInfoRepository._ACTIVE_FILTER = {"onoff": 1, "webmanagerUse": 1}
```

모든 read 경로에 자동 적용 → decommissioned(`onoff=0`)·미관리(`webmanagerUse=0`) 장비는 분석 대상에서 제외. **반드시 `EqpInfoRepository`를 통해 접근**(직접 쿼리하면 필터 누락).

---

## 10. 예외 계약

`src/db/repository.py`의 public async 메서드는 raw `pymongo.errors.*`를 누출하지 않고 도메인 예외로 변환:

| 원본 (pymongo) | 변환 | 의미 |
|---|---|---|
| `ServerSelectionTimeoutError`/`NetworkTimeout`/`ConnectionFailure` | `MongoUnavailableError` | 연결 불가 (job이 `reason="mongo_unavailable"` 라벨링) |
| `DuplicateKeyError` (`create`만) | `ProfileAlreadyExistsError` | unique scope 충돌 → 409 |
| `find` 결과 None | `ProfileNotFoundError` (호출자) | 404 |

---

## 11. 함정 (Pitfalls)

| # | 함정 | 올바른 사용 |
|---|------|------------|
| P1 | `scope.eqpModel` 카멜케이스 | Mongo 키는 `eqpModel`(snake `eqp_model` 아님). Python은 `Scope` 객체로만 다루기 |
| P2 | `scope.process` ≠ `measure.proc` | 전자=적용범위/파티션, 후자=EARS row 정체성 |
| P3 | `measureId.type` 점 표기 | 점 앞=measure id, 점 뒤=fact(=type). 둘 다 검증으로 존재 강제 |
| P4 | 한 measure 같은 type 중복 | 금지(참조 모호). 다른 임계/창 필요하면 measure 분리 또는 rule 분리 |
| P5 | 임계가 measure·rule 두 곳 | `spike_count.over`=사건 경계(measure), `rule.value`=경보 기준(rule). 의도된 계층 |
| P6 | 경보 방향을 max로만 | 낮을때는 `min`+`<=`, 범위이탈은 두 조건 OR. type↔op 적합성 검증됨 |
| P7 | category 필터 누락 | ES 쿼리에 `EARS_CATEGORY.keyword` term 필수(문자열 필드는 text+`.keyword`, `settings.es_keyword_suffix`; cpu/mem `total_used_pct` 혼입 방지 — [§8.1](#81-확정된-사실)) |
| P8 | 활성 필터 누락 | `EqpInfoRepository`만 사용 |
| P9 | 특정 장비 override 시 전역 통째 복사 | **금지**. overlay엔 바꿀 measure/rule만(나머지 상속, [§6](#6-스코프-해석--계층-상속-cascade)). 전체 복사는 드리프트 유발 |

---

## 12. 전체 JSON 예시 (목표 전역 프로파일 — 전 Phase)

> 이 예시는 **전 Phase fact를 포함한 목표 설계**입니다. Phase 2/3 fact(`duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`)는 스키마상 유효하지만 **엔진은 아직 skip+경고**합니다. 실제로 시드되는 기본 프로파일(`build_default_profile`)은 **Phase 1 fact만 포함한 축소판**입니다([§13](#13-구현-상태)).

```jsonc
{
  "scope": { "process": "*", "model": "*", "eqpId": "*" },
  "enabled": true,
  "governance": { "version": 1, "updated_by": "system", "change_reason": "initial" },

  "measures": [
    { "id": "cpu", "category": "cpu", "metric": "total_used_pct", "proc": "@system",
      "window_minutes": 15, "metric_kind": "gauge",
      "bucketing": { "seconds": 30 },
      "baseline": { "days": 7, "same_hour": true, "min_points": 30 },
      "facts": [
        { "type": "max" }, { "type": "avg" }, { "type": "p95" },
        { "type": "spike_count", "over": 90, "direction": "above" },
        { "type": "duration",    "over": 80, "direction": "above" },
        { "type": "baseline_dev" }
      ] },

    { "id": "mem_used", "category": "memory", "metric": "total_used_pct", "proc": "@system",
      "window_minutes": 15, "metric_kind": "gauge", "facts": [ { "type": "max" } ] },
    { "id": "mem_free", "category": "memory", "metric": "total_free_pct", "proc": "@system",
      "window_minutes": 15, "metric_kind": "gauge", "facts": [ { "type": "min" } ] },
    { "id": "mem_size", "category": "memory", "metric": "total_used_size", "proc": "@system",
      "window_minutes": 60, "metric_kind": "gauge", "bucketing": { "seconds": 300, "points": 6 },
      "facts": [ { "type": "growth_rate", "unit": "per_hour" }, { "type": "trend" } ] },

    { "id": "disk", "category": "disk", "metric": "*", "proc": "@system",
      "window_minutes": 30, "metric_kind": "gauge", "expand": "instance",
      "bucketing": { "seconds": 3600 },
      "facts": [ { "type": "max" }, { "type": "growth_rate", "unit": "per_day" } ] },

    { "id": "temp", "category": "temperature", "metric": "*", "proc": "@system",
      "window_minutes": 30, "metric_kind": "gauge", "expand": "instance", "facts": [ { "type": "max" } ] },
    { "id": "fan", "category": "fan", "metric": "*", "proc": "@system",
      "window_minutes": 15, "metric_kind": "gauge", "expand": "instance", "facts": [ { "type": "min" } ] },
    { "id": "volt_vcore", "category": "voltage", "metric": "CPU_Vcore", "proc": "@system",
      "window_minutes": 15, "metric_kind": "gauge", "facts": [ { "type": "min" }, { "type": "max" } ] },

    { "id": "gpu_load", "category": "gpu", "metric": "*_core_load", "proc": "@system",
      "window_minutes": 15, "metric_kind": "gauge", "expand": "instance",
      "facts": [ { "type": "p95" }, { "type": "spike_count", "over": 95, "direction": "above" } ] },

    { "id": "ssd_life", "category": "storage_smart", "metric": "*_remaining_life", "proc": "@system",
      "window_minutes": 1440, "metric_kind": "gauge", "expand": "instance", "facts": [ { "type": "min" } ] },
    { "id": "ssd_err", "category": "storage_smart", "metric": "*_media_errors", "proc": "@system",
      "window_minutes": 1440, "metric_kind": "counter", "expand": "instance",
      "facts": [ { "type": "delta", "mode": "last_minus_first" } ] },
    { "id": "disk_health", "category": "storage_health", "metric": "*_status", "proc": "@system",
      "window_minutes": 30, "expand": "instance", "facts": [ { "type": "max" } ] },

    { "id": "proc_required", "category": "process_watch", "metric": "required", "proc": "*",
      "window_minutes": 5, "expand": "instance", "facts": [ { "type": "min" } ] },
    { "id": "proc_forbidden", "category": "process_watch", "metric": "forbidden", "proc": "*",
      "window_minutes": 5, "expand": "instance", "facts": [ { "type": "max" } ] },
    { "id": "proc_mem", "category": "memory", "metric": "used", "proc": "*",
      "window_minutes": 60, "metric_kind": "gauge", "expand": "instance",
      "bucketing": { "seconds": 300 }, "facts": [ { "type": "growth_rate", "unit": "per_hour" } ] },
    { "id": "net_recv", "category": "network", "metric": "recv_rate", "proc": "*",
      "window_minutes": 15, "metric_kind": "gauge", "expand": "instance",
      "facts": [ { "type": "spike_count", "over": 100000000, "direction": "above" } ] }
  ],

  "rules": [
    { "id": "cpu_warn", "interval_minutes": 5, "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "cpu.max", "op": ">=", "value": 80 } ] },
    { "id": "cpu_crit", "interval_minutes": 5, "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "cpu.max", "op": ">=", "value": 95 } ] },
    { "id": "cpu_anomaly", "interval_minutes": 5, "severity": "CRITICAL", "combine": "AND", "notify": "default",
      "when": [
        { "fact": "cpu.p95", "op": ">", "value": 80 },
        { "fact": "cpu.spike_count", "op": ">", "value": 5 },
        { "fact": "cpu.duration", "op": ">", "value": 180 }
      ] },

    { "id": "mem_high",     "interval_minutes": 5, "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "mem_used.max", "op": ">=", "value": 90 } ] },
    { "id": "mem_low_free", "interval_minutes": 5, "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "mem_free.min", "op": "<=", "value": 5 } ] },
    { "id": "mem_leak", "interval_minutes": 5, "severity": "CRITICAL", "combine": "AND", "notify": "default",
      "when": [
        { "fact": "mem_size.trend", "op": "trend==", "value": "increasing" },
        { "fact": "mem_size.growth_rate", "op": ">", "value": 52428800 }
      ] },

    { "id": "disk_full",    "interval_minutes": 30, "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "disk.max", "op": ">=", "value": 95, "quantifier": "any" } ] },
    { "id": "temp_high",    "interval_minutes": 10, "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "temp.max", "op": ">=", "value": 90, "quantifier": "any" } ] },
    { "id": "fan_low",      "interval_minutes": 10, "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "fan.min", "op": "<=", "value": 300, "quantifier": "any" } ] },
    { "id": "volt_out", "interval_minutes": 5, "severity": "WARNING", "combine": "OR", "notify": "default",
      "when": [
        { "fact": "volt_vcore.min", "op": "<", "value": 1.1 },
        { "fact": "volt_vcore.max", "op": ">", "value": 1.4 }
      ] },

    { "id": "gpu_hot",      "interval_minutes": 5,  "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "gpu_load.p95", "op": ">=", "value": 95, "quantifier": "any" } ] },
    { "id": "ssd_life_low", "interval_minutes": 60, "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "ssd_life.min", "op": "<=", "value": 20, "quantifier": "any" } ] },
    { "id": "ssd_err_inc",  "interval_minutes": 60, "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "ssd_err.delta", "op": ">", "value": 0, "quantifier": "any" } ] },
    { "id": "disk_failing", "interval_minutes": 30, "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "disk_health.max", "op": ">=", "value": 2, "quantifier": "any" } ] },

    { "id": "proc_down",          "interval_minutes": 5,  "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "proc_required.min", "op": "==", "value": 0, "quantifier": "any" } ] },
    { "id": "proc_forbidden_run", "interval_minutes": 5,  "severity": "CRITICAL", "notify": "default",
      "when": [ { "fact": "proc_forbidden.max", "op": ">", "value": 0, "quantifier": "any" } ] },
    { "id": "proc_mem_leak",      "interval_minutes": 10, "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "proc_mem.growth_rate", "op": ">", "value": 104857600, "quantifier": "any" } ] },
    { "id": "net_spike",          "interval_minutes": 5,  "severity": "WARNING",  "notify": "default",
      "when": [ { "fact": "net_recv.spike_count", "op": ">", "value": 5, "quantifier": "any" } ] }
  ],

  "notify": {
    "default": { "cooldown_minutes": 30, "email_code": "RESOURCE_MONITOR" }
  }
}
```

---

## 13. 구현 상태

v2 스키마는 **구현 완료**되었습니다 (Phase 1 fact end-to-end, 실 ES·Mongo·Redis 통합 테스트 green). 영역별 현황:

| 영역 | 구현 | 비고 |
|------|------|------|
| `src/db/models.py` | ✅ `MonitorProfile(scope+measures+rules+notify+enabled+governance)` + `Rule.enabled`(개별 on/off, 기본 true) + 문서/effective 검증(`validate_effective`) + lint 경고(`lint_effective`) | Pydantic 단일 진실 소스 |
| `src/db/repository.py` | ✅ 매칭 scope `$or` 수집 → cascade fold → effective 캐시, `governance.version` 낙관락(replace/delete) | 항목 단위 편집은 API 레이어가 read-modify-write replace로 처리(repo엔 item CRUD 없음) |
| `src/db/seed.py` | ✅ Phase-1 fact만 포함한 기본 프로파일, governance 제외 hash, 운영자 편집 보존, race 안전 | |
| `src/es/queries.py` | ✅ EARS_* 행 형식 — `EARS_CATEGORY`/`EARS_METRIC`/`EARS_PROCNAME` 필터 + `EARS_EQPID`(×proc×instance) group_by + `EARS_VALUE` 집계 | |
| `src/analyzer/es_parser.py` | ✅ fact type별 파싱(stat/percentiles/top_hits/filter_range) | 쿼리↔파서 계약 |
| `src/analyzer/engine.py` | ✅ **per-eqp resolve** + effective-signature 버킷팅 + Phase2/3 skip+경고 + 비활성 rule(`enabled:false`) 평가 스킵 | dead-path 수정 |
| `src/analyzer/threshold.py` | ✅ `evaluate_rule`(op/quantifier any·all·count/combine) | state_check은 별도 함수 없이 min/max fact+op 조건으로 평가 |
| `src/analyzer/metric_resolver.py` | ✅ fact_catalog enum 공유, 와일드카드 인스턴스 매칭 | |
| `src/cache/cooldown.py` | ✅ 키 `{prefix}:cooldown:{process}:{eqpId}:{proc}:{notify}:{severity}` (가변 5차원) | |
| `src/scheduler/jobs.py` | ✅ (process, interval) 그룹당 job — `get_scheduling_intervals`가 **활성 rule의 interval만** 수집 | |
| `src/api/profiles.py` | ✅ overlay CRUD + effective(+provenance) + measure/rule/notify item API | 관리 UI는 후속 |
| `src/config/constants.py` | ✅ `COLL_RULE` 제거 | |

> `uniq_scope` 인덱스·`EqpInfoRepository`·예외 계약은 그대로 재사용하며, `resolve_profile`은 "첫 매치 replace"에서 "**cascade fold**"로 전환되었습니다([§6](#6-스코프-해석--계층-상속-cascade)). 과거의 dead-path(엔진이 process 레벨만 resolve)는 **per-eqp 해석으로 수정**되어 model/eqp override가 실제 알림에 반영됩니다(통합 테스트 E7이 회귀 가드).
>
> **남은 작업**: Phase 2/3 fact(`duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`)는 스키마·검증은 수용하나 엔진은 skip+경고 상태 — date_histogram/extended_stats/baseline 인프라 구현이 후속([§14](#14-phase-계획)).

---

## 14. Phase 계획

- **Phase 1 ✅ (완료)**: `max`/`min`/`avg`/`last`/`pNN`/`spike_count` + state(min/max) + `quantifier` + `group_by(proc)` + `EARS_CATEGORY` 필터 + 5-dim cooldown 키 + 저장 시 검증 + per-eqp cascade + **rule 개별 enable/disable**(`enabled:false` 평가·스케줄 제외, overlay 소프트 tombstone). (PRD 단순 임계·process_watch·디스크/온도 와일드카드·CPU 복합 p95+spike까지)
- **Phase 2 (후속)**: `duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore` (date_histogram·scripted_metric·top_hits·extended_stats 인프라) + DATA_MISSING(데이터 미수신 감지).
- **Phase 3 (후속)**: `baseline_dev` (과거 인덱스 쿼리) + escalation/수신자 라우팅 확장.

> Phase 2/3 fact별 작업 분해·ES 전략·파서 계약·`DATA_MISSING` 설계·리스크는 **[docs/PHASE2-3-PLAN.md](docs/PHASE2-3-PLAN.md)** 참조.

---

## 15. 관련 문서

| 문서 | 내용 |
|------|------|
| [docs/PHASE2-3-PLAN.md](docs/PHASE2-3-PLAN.md) | **Phase 2/3 fact 구현 계획** — fact별 ES 전략·파서 계약·`DATA_MISSING` 설계·리스크 |
| [PRD_Phase0_Foundation.md](PRD_Phase0_Foundation.md) | 원본 요구사항 (§5는 v2 배너로 SCHEMA에 권위 위임; §5 본문 JSON은 v1 원안 보존용) |
| [docs/ADMIN-UI-LEGIBILITY.md](docs/ADMIN-UI-LEGIBILITY.md) | 관리 UI/시인성 설계 — 왜 단일 컬렉션 유지인가(UI/UX 근거) + 권장 API/UI 방향 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 설계 배경 + Gotchas (v2 as-built 동기화 완료) |
| [README.md](README.md) / [CONTRIBUTING.md](CONTRIBUTING.md) | 진입점 / 워크플로우 (v2 동기화 완료) |
| `~/Developer/ARS/ResourceAgent/docs/EARS-METRICS-REFERENCE.md` | 실제 수집 메트릭(category/metric/proc/value) 정의 |
| `~/Developer/ARS/WebManager/docs/SCHEMA.md` | EARS DB 외부 컬렉션 풀 스키마 (EQP_INFO) |
| `src/db/models.py` | v2 Pydantic 단일 진실 소스 (구현 완료) |
