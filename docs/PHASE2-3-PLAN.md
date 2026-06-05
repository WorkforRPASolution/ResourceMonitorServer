# Phase 2/3 구현 계획 — 시계열·이력 fact

> **버전: 1.0 (2026-06-05)** · 상태: 계획(미착수)
>
> Phase 1(`max`/`min`/`avg`/`last`/`pNN`/`spike_count`)은 [SCHEMA.md §13](../SCHEMA.md) 기준 **구현 완료**입니다. 이 문서는 **스키마·검증은 이미 수용하나 엔진이 skip+경고**하는 Phase 2/3 fact를 실제로 동작시키기 위한 fact별 작업 분해·ES 전략·파서 계약·검증·테스트·리스크를 정의합니다. 권위 스펙은 SCHEMA.md, 이 문서는 그 위의 **구현 계획**입니다.

---

## 0. 현재 상태 — "예약"과 "구현"의 경계

| 레이어 | Phase 1 | Phase 2/3 현황 |
|--------|---------|----------------|
| 스키마/검증 (`models.py`, `fact_catalog.py`) | ✅ | ✅ **수용** — 닫힌 `FactType` enum에 13종 전부 존재, `Fact`/`Measure` 검증자가 파라미터(over/direction/mode/unit/bucketing/baseline)를 강제 |
| 전략 매핑 (`fact_catalog.AGG_STRATEGY`) | ✅ | ✅ **예약** — `DATE_HISTOGRAM`/`EXTENDED_STATS`/`BASELINE_QUERY` enum + 매핑까지 정의됨 |
| 쿼리 빌더 (`queries.build_fact_sub_aggs`) | ✅ | ❌ Phase-1 전략만 emit, 나머지 생략 |
| 파서 (`es_parser._read_fact`) | ✅ | ❌ 미구현 |
| 엔진 (`engine._compute_measure`) | ✅ | ❌ `is_implemented(f.type)` 게이트로 **skip + `fact_phase_not_implemented` 경고** |
| 게이트 (`fact_catalog.IMPLEMENTED_PHASES`) | `{1}` | Phase 2 완료 시 `{1,2}`, Phase 3 완료 시 `{1,2,3}` |

> 핵심: **산출 로직·테스트는 0**. enum 자리만 예약돼 있어 "계획"으로 오인하면 안 됩니다. `git grep "fact_phase_not_implemented"`로 skip 지점을 확인할 수 있습니다.

### 공통 변경 패턴 (모든 fact 공통)

각 fact 구현은 동일한 4(+1) 지점을 건드립니다:

1. **`src/es/queries.py` `build_fact_sub_aggs`** — 해당 `AggStrategy` 분기를 추가해 leaf sub-agg(키 = fact type명)를 emit.
2. **`src/analyzer/es_parser.py` `_read_fact`** — 그 sub-agg 응답을 파싱해 값(또는 quantifier 인스턴스 리스트)으로 환원.
3. **`src/analyzer/engine.py` `_compute_measure`** — 클라이언트측 후처리(streak/OLS/moving_fn 등)가 필요한 fact는 여기서 계산. `is_implemented` 게이트는 Phase 단위로 해제.
4. **`src/analyzer/fact_catalog.py` `IMPLEMENTED_PHASES`** — Phase 전체가 green이 된 뒤에만 해당 Phase 번호 추가.
5. **테스트** — `test_queries.py`(sub-agg 형태), `test_es_parser.py`(파싱), `test_analysis_engine.py`(end-to-end), `test_es_real.py`/`test_phase1_analysis_e2e.py`(실 ES). 전부 **TDD(RED→GREEN)**.

> 쿼리↔파서 계약: sub-agg 키 = fact type명(`"duration"`,`"zscore"`…). 중첩(`by_eqp→by_proc→by_metric→leaf`)은 Phase 1과 동일하게 유지하고, leaf만 fact별 전략으로 바뀝니다.

---

## 1. Phase 2 — 시계열·통계 fact

ES 7.11.9 전제: `date_histogram` + `moving_fn`(pipeline), `extended_stats`, `top_hits`. `scripted_metric`은 보안/성능 리스크가 있어 가능하면 클라이언트측 계산을 우선합니다.

### 1.1 `delta` — 변화량 (`AggStrategy.TOP_HITS`)

- **의미**: `mode="last_minus_first"`(기본) 또는 `max_minus_min`. counter 메트릭(예: SMART media_errors 증가) 감지.
- **쿼리**: `last_minus_first`는 `top_hits` 2개가 필요 — 가장 오래된 1건(EARS_TIMESTAMP asc)과 최신 1건(desc). 하나의 leaf에 두 `top_hits`를 두거나, leaf에 `min`/`max` + asc/desc top_hits를 조합. `max_minus_min`은 `max`-`min` 단일 패스(STAT 2개)로 더 저렴.
- **파서**: 두 hit의 `EARS_VALUE` 차(또는 max-min).
- **엔진**: 단순 산술, 후처리 경량.
- **검증(기존)**: `op ∈ {>,>=,<,<=,!=}`. counter 권장(`lint_effective`가 gauge+delta 경고).
- **테스트**: 증가/감소/동일, 표본 1개(차 0 또는 DATA_MISSING) 경계.

### 1.2 `duration` — 최대 연속 지속(초) (`AggStrategy.DATE_HISTOGRAM`)

- **의미**: `over`/`direction` 임계를 **연속으로** 초과한 최장 구간(초). measure에 `bucketing.seconds` 필수(`NEEDS_BUCKETING`).
- **쿼리**: `date_histogram(fixed_interval=bucketing.seconds)` + 버킷별 `max`(또는 avg) sub-agg.
- **파서**: 버킷 배열을 `[(ts, value)]`로 환원.
- **엔진**: **클라이언트측 streak** 계산 — `over`/`direction` 충족 버킷의 최장 연속 길이 × `bucketing.seconds`. 빈 버킷(데이터 공백) 처리 규칙 명시(끊김으로 볼지/이어붙일지 — 기본: 끊김).
- **검증(기존)**: `bucketing` 필수, `op ∈ {>,>=}`.
- **테스트**: 단일 streak, 다중 streak 중 최댓값, 경계(딱 1버킷), 공백 버킷 분리.

### 1.3 `growth_rate` — 단위시간당 증가 (`AggStrategy.DATE_HISTOGRAM`)

- **의미**: `unit`(per_hour/per_day) 당 증가량. 메모리 누수·디스크 증가 추세.
- **쿼리**: `date_histogram` + 버킷별 `avg`.
- **엔진**: 버킷 (ts, value)에 **OLS 선형회귀 기울기** → unit으로 환산. (ES `scripted_metric` OLS도 가능하나 클라이언트 계산 권장 — 보안/디버깅 용이.)
- **검증(기존)**: `bucketing` 필수, `op ∈ {>,>=,<,<=}`.
- **테스트**: 일정 증가 기울기, 평탄(0), 감소(음수), 표본<2 가드(DATA_MISSING).

### 1.4 `moving_avg` / `trend` (`AggStrategy.DATE_HISTOGRAM`)

- **의미**: `moving_avg`=이동평균 마지막값, `trend`=추세 enum(`increasing`/`decreasing`/`flat`). measure에 `bucketing.seconds + points` 필수(`NEEDS_POINTS`).
- **쿼리**: `date_histogram` + `moving_fn`(pipeline agg; **classic `moving_avg` agg는 deprecated — `moving_fn` 사용**) 또는 클라이언트측 윈도우 평균.
- **파서/엔진**: `moving_avg`는 마지막 윈도우 평균값. `trend`는 기울기 부호 → enum(`op == "trend=="`, value=문자열).
- **검증(기존)**: `bucketing.points` 필수, `seconds×points ≤ window`. `trend`는 `trend==`만, value는 문자열.
- **테스트**: 증가/감소/평탄 분류, points 경계, 윈도우 부족.

### 1.5 `zscore` — 창 내 표준화 이상도 (`AggStrategy.EXTENDED_STATS`)

- **의미**: `(max-avg)/std`(또는 direction별). 창 내부 통계만 사용(이력 불필요).
- **쿼리**: leaf에 `extended_stats(EARS_VALUE)` → `avg` + `std_deviation`.
- **엔진**: `(대표값-avg)/std`. **std≈0·표본<2 가드 필수**(0 분모 → DATA_MISSING 또는 스킵).
- **검증(기존)**: `op ∈ {>,>=}`, `direction` 기본 high.
- **테스트**: 명확한 이상치, 균일분포(std≈0) 가드, 표본 부족.

---

## 2. Phase 3 — 이력 기반 fact

### 2.1 `baseline_dev` — 과거 동일시간대 대비 편차% (`AggStrategy.BASELINE_QUERY`)

- **의미**: 현재 창 대표값을 과거 N일 같은 시간대 baseline과 비교한 편차%. measure에 `baseline{days, same_hour, min_points}` 필수(`NEEDS_BASELINE`).
- **쿼리**: **현재 창 + 과거 N일 일별 인덱스 별도 쿼리**(`{process_lower}_all-{과거날짜}`). same_hour면 각 과거일의 동일 시각 range를 `should/OR`로 묶음.
- **엔진**: baseline 평균 산출 → `(현재-baseline)/baseline × 100`. **표본<`min_points` 가드** → DATA_MISSING.
- **인덱스 전제**([SCHEMA §8.2-3](../SCHEMA.md)): 과거 N일 일별 인덱스가 보존돼 있어야 함. 누락일은 건너뛰되 min_points로 신뢰도 보장.
- **escalation/수신자 라우팅 확장**도 Phase 3 범위(notify 다중 채널·심각도별 라우팅).
- **테스트**: 정상 baseline 대비 편차, 과거 인덱스 누락, min_points 미달, same_hour on/off.

---

## 3. 횡단 관심사 — `DATA_MISSING` (데이터 미수신 감지)

> 현재 어느 문서도 자료구조/평가 위치를 정의하지 않은 **신규 능력**입니다. fact 카탈로그(닫힌 enum) 밖의 "제8의 신호"이므로 별도 설계가 필요합니다.

- **트리거**: measure가 기대하는 EARS row가 창 내에 0건(terms 버킷 미생성) → 장비·proc·metric 단위로 "데이터 미수신".
- **설계 결정 필요**:
  1. **표현** — 별도 `Rule.severity`/전용 알림 타입? 아니면 measure 단위 플래그? (권장: 엔진이 measure 계산 시 빈 버킷을 감지해 전용 `DataMissingBreach` 생성, notify는 기존 채널 재사용)
  2. **임계** — 몇 분 연속 미수신부터 알릴지(measure.window 또는 별도 grace).
  3. **cooldown** — 기존 5-dim 키에 `notify=data_missing` 같은 차원으로 흡수.
  4. **per-eqp** — 활성 장비(`EqpInfoRepository`)인데 데이터가 없는 경우만(decommissioned 제외).
- **위치**: `engine._evaluate_bucket`에서 measure별 빈 결과를 1급 신호로 승격. Phase 2와 함께 또는 직후 구현.

---

## 4. 구현 순서 (권장)

각 단계 TDD, 단계 끝마다 `make test-fast` green:

1. **Phase 2-a (저위험, 단일 패스)**: `delta`(max_minus_min/top_hits), `zscore`(extended_stats). date_histogram 불필요.
2. **Phase 2-b (date_histogram 인프라)**: `duration`(streak) → `growth_rate`(OLS) → `moving_avg`/`trend`(moving_fn). 공통 date_histogram 빌더 1벌을 먼저 만들고 후처리만 분기.
3. **Phase 2-c**: `DATA_MISSING` 설계·구현.
4. **게이트 해제**: `IMPLEMENTED_PHASES = {1, 2}` + 통합 테스트(E9~) 추가.
5. **Phase 3**: `baseline_dev`(과거 인덱스 쿼리) + escalation/라우팅. 게이트 `{1,2,3}`.

---

## 5. 리스크 / ES 7.11.9 제약

- **`moving_fn` vs deprecated `moving_avg` agg**: 반드시 pipeline `moving_fn` 사용.
- **`scripted_metric`(OLS)**: 클러스터 스크립트 정책·성능 리스크 → 클라이언트측 회귀 우선.
- **표본 부족/0 분모**: `growth_rate`(점<2)·`zscore`(std≈0)·`baseline_dev`(min_points 미달) 전부 가드 → `DATA_MISSING`로 강등하거나 스킵(스킵 시 silent 방지 위해 로그).
- **`top_hits` 비용**: `delta`(asc+desc)·`last`는 버킷당 hit 수집이라 비쌈 — eqp 수 × 버킷 폭 주의.
- **baseline 인덱스 보존**([SCHEMA §8.2](../SCHEMA.md)): 운영 인덱스 TTL이 N일보다 짧으면 `baseline_dev` 불가 — 인프라 전제 확인 필수.
- **샘플 emit 주기**([SCHEMA §8.2](../SCHEMA.md)): `duration`(bucket_seconds)·`growth_rate`(점 간격)·percentile(표본 충분성) 의미에 직결.
- **회귀 위험**: 게이트(`IMPLEMENTED_PHASES`)를 Phase 전체 green 이전에 올리면 미완 fact가 실알림으로 샘. **단계별 게이트** 엄수.

---

## 6. 검증 계획

- **단위**: fact별 `test_queries.py`(sub-agg 형태) + `test_es_parser.py`(파싱) + `test_analysis_engine.py`(measure→fact→rule). 표본 경계·0 분모·빈 버킷 케이스 필수.
- **통합(실 ES)**: `test_es_real.py`에 date_histogram/extended_stats/baseline 쿼리 회귀, `test_phase1_analysis_e2e.py`(또는 신규 `test_phase2_*`)에 EARS_* 색인 → fact 산출 → rule 발화 → 알림 시나리오(E9~). `baseline_dev`는 과거일 인덱스 시드.
- **게이트 가드**: `IMPLEMENTED_PHASES`가 올라간 뒤 SCHEMA §12 목표 예시의 Phase 2/3 fact가 실제로 산출되는지 E2E로 확인.

---

## 7. 관련 문서

| 문서 | 내용 |
|------|------|
| [SCHEMA.md](../SCHEMA.md) | v2 스키마 권위 스펙 — §2 fact 카탈로그, §4.3 집계 전략, §5 검증, §8 EARS 전제, §14 Phase 계획 |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | 분석 흐름·모듈 구조 |
| `src/analyzer/fact_catalog.py` | `FactType`/`AGG_STRATEGY`/`PHASE_OF_FACT`/`IMPLEMENTED_PHASES` — 구현 게이트의 단일 진실 소스 |
| `src/es/queries.py` · `src/analyzer/es_parser.py` | 쿼리↔파서 계약(확장 지점) |
| `src/analyzer/engine.py` | `_compute_measure` skip 게이트(`fact_phase_not_implemented`) |
