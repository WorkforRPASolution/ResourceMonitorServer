# RMS 알림 메일 템플릿 (Option C) — TDD 구현 계획 (rev2)

> 상태: **구현 계획 — 코드 작성 전**
> 작성일: 2026-06-09 (rev2: 멀티에이전트 적대 리뷰 28개 확정 지적 반영)
> 대상: [resource-monitor-email-template-architecture.md](resource-monitor-email-template-architecture.md) Option C. UI는 [editor-ui 문서](resource-monitor-email-template-editor-ui.md).
> 원칙: **TDD(RED→GREEN→REFACTOR)**, 테스트 없이 프로덕션 코드 금지(CLAUDE.md).

> **rev2 주요 변경(리뷰 반영):** ① Akka는 테스트 하네스가 **아예 없음** → P5-0 부트스트랩 신설, "기존 스펙" 전제 폐기. ② EmailWorker 액터 단위테스트 불가 → 순수 `EmailBodyResolver` 추출 + 파싱 스펙 분리. ③ 제목 폴백 모순 해소(RMS가 title 항상 전송). ④ async 조회는 `_dispatch`, `build_alert_request`는 sync 유지. ⑤ `@Metric` v1 제외(@Fact), 숫자=`str` 확정, `@Timestamp` 포맷 확정. ⑥ 신규 repo 배선·테스트 파일 경로·권한 키·vitest 스크립트·크로스언어 golden·TinyMCE 주석 보존 등 보강.

---

## 0. 가이드 원칙 & 확정 상수

- **Iron law**: 모든 신규 함수/분기에 먼저 실패하는 테스트. 통합/e2e 전 단위부터.
- **회귀 가드(최우선)**: 전역 플래그 `rms_custom_body_enabled` **off → RMS 페이로드는 현행 9필드와 100% 동일**(renderedBody/title 키 없음). 기존 717 그린 유지.
- **렌더러 정본화**: RMS `body_renderer`가 **canonical**. P1이 **language-neutral golden JSON**(`tests/data/email_template_golden.json`)을 산출하고 pytest가 이를 소비. WebManager 미리보기는 그 사본 + byte-equality 가드로 동기화(P6-3).
- **확정 상수(리뷰 반영, 더 이상 "결정사항" 아님)**:
  - 숫자 렌더 = `str(value)` 그대로(재포맷 없음). 비-float(str/trend) verbatim. None→`-`. **렌더러가 원시 값을 받아** 1곳에서 포맷(사전 `str()` 금지).
  - `@Timestamp` 포맷 = `%Y-%m-%d %H:%M KST`(Asia/Seoul).
  - 토큰명 = `renderedBody`(페이로드 필드, ≠`@contents` 토큰). 권한/feature 키 = **`rmsEmailTemplate`**, 라우트 `/api/rms-email-template`, feature 폴더 `rms-email-template`, 컬렉션 `RESOURCE_MONITOR_EMAIL_TEMPLATE` (전 문서 통일).
  - `@Metric`은 **v1 미포함**(데이터 소스 없음) → 지표는 `@Fact`(=`breach.fact`).
  - **내장 기본 본문 상수** + **내장 기본 제목 상수**는 P1에서 확정(폴백/제목 단일 소스).

### 테스트 도구 (현실 반영)
| 컴포넌트 | 도구 | 비고 |
|---|---|---|
| RMS | pytest (`unit`/`integration`/`e2e`), venv `source .venv/bin/activate` | 정상 구성 |
| Akka HttpWebServer | scalatest + akka-testkit | ⚠️ **현재 테스트 인프라 전무**(pom.xml에 test dep/plugin 없음, scala-maven-plugin이 testCompile 미바인딩, `src/test/scala/samples/*`는 미선언 specs2/junit import) → **P5-0에서 부트스트랩 필요** |
| WebManager client | vitest + Playwright | ⚠️ `client/package.json`에 `test` 스크립트 없음 → P6에서 `"test":"vitest run"` 추가(현재는 `npx vitest run`) |
| WebManager server | vitest (`test`: `vitest run` 존재) | 정상 |

---

## 1. 단계 개요 (의존 순서)

| Phase | 범위 | 코드베이스 | 산출물 | 선행 |
|---|---|---|---|---|
| **P1** | 토큰 카탈로그 + **순수 렌더러** + **golden JSON** + 기본 본문/제목 상수 | RMS | `src/alert/tokens.py`,`body_renderer.py`, `tests/data/email_template_golden.json` | — |
| **P2** | 템플릿 accessor(5-tier 폴백, read-only) + **deps 배선** | RMS | repo + RepositoryContext/engine/scheduler 배선 | — (P1과 병행) |
| **P3** | 배선: settings·model·`_dispatch`(async 조회)·`build_alert_request`(sync) | RMS | 페이로드 `renderedBody`/`title` | P1,P2 |
| **P4** | RMS 페이로드 통합(Akka 목) + **계약 fixture 라운드트립** | RMS | 그룹→N행/단일→1행/fallback + 와이어 계약 | P3 |
| **P5-0** | **Akka 테스트 하네스 부트스트랩** | HttpWebServer | pom test dep/plugin, 샘플 정리, 1==1 Spec 그린 | Phase 0 계약 |
| **P5** | Akka 수신·본문 투입(파싱 스펙 + 순수 resolver) | HttpWebServer | `EmailHttpDataFormatSpec`+`EmailBodyResolverSpec`+코드 | P5-0 |
| **P6** | WebManager 신규 feature + 편집기 UI(golden 사본·lint·미리보기) | WebManager | 클론 CRUD + 팔레트/ERB/미리보기/lint | P1(golden/규칙) |
| **P7** | 시드·문서 정합·롤아웃(플래그 off→on) | RMS/WebManager | catch-all 시드, D3 실측, 문서 정합 | P3~P6 |

- **계약(Phase 0 freeze, P1과 함께 확정)**: 필드명(`renderedBody`/`title`)·토큰 카탈로그·ERB 문법·**숫자/타임스탬프 포맷**·**기본 본문/제목 상수**·**golden JSON**. 이 freeze 후 P5(Akka, 순수 pass-through)·P6(WebManager)는 독립 진행.
- **P4 라벨 주의**: ES/Mongo/Redis는 실 인프라지만 **Akka는 목**(aiohttp `mock_email_server`). 진짜 end-to-end 배달(RMS→Akka→Redis→EmailingAgent)은 테스트 안 함 → **계약 fixture 라운드트립**(P4-계약)으로 와이어 호환만 보증, 실배달은 P7 수기/ops.

---

## P1. 토큰 카탈로그 + 순수 렌더러 + golden (RMS) — **핵심**

순수함수. (§7.2, §7.3, D6) **렌더러는 원시 breach 값을 받는다**(사전 `str()` 금지 — None→`-` 분기 생존).

### P1-0. 토큰 카탈로그 — `src/alert/tokens.py`
- **RED** `test_tokens.py`: 스칼라/행 토큰 집합·컨텍스트(`text`/`url`) 맵 존재, `@GrafanaUrl`만 `url`, **`@Metric` 부재**(v1 제외), `@Fact` 포함. architecture §7.2-A/B와 개수·이름 일치.
- **GREEN**: `SCALAR_TOKENS`/`ROW_TOKENS`/`TOKEN_CONTEXT`.

### P1-1. 텍스트 escape (D6)
- **RED** `test_body_renderer.py::test_text_token_html_escaped`: `a<b&c"'` → `a&lt;b&amp;c&quot;&#x27;`.
- **GREEN** `_escape_text` = `html.escape(v, quote=True)`.

### P1-2. URL escape (D6)
- **RED** `test_url_token_keeps_query_amp`(쿼리 `&` 보존, 따옴표만 escape) / `test_url_token_rejects_non_http`(javascript: 차단).
- **GREEN** `_escape_url`(scheme 검증 + 따옴표 escape).

### P1-3. None/숫자/문자열·trend 포맷 (확정 규칙)
- **RED** `test_none_current_value_renders_dash`(None→`-`); `test_number_str_verbatim`(91.2→`91.2`, **재포맷 없음**); `test_str_threshold_verbatim`(trend 라벨 등 str `threshold_value` verbatim); `test_renderer_receives_raw_values`(렌더 컨텍스트가 `str()` 사전변환이 아니라 원시 값을 받음 → None 분기 생존).
- **GREEN** 포맷터(isinstance 가드: float만 str화, str/None은 규칙대로).

### P1-4. 스칼라 치환 + prefix 충돌 회피 (§5-③)
- **RED** `test_scalar_substitution_all`; `test_no_prefix_collision`(`@Threshold @ThresholdX` → 앞만 치환).
- **GREEN** 토큰 경계 인식 단일 패스(naive replaceAll 아님).

### P1-5. 미지/누락 토큰
- **RED** `test_unknown_token_left_literal`; `test_missing_value_blank`.
- **GREEN** 정책 구현.

### P1-6. ERB 펼치기 — 단일/N행 + 행 None (§7.3)
- **RED** `test_erb_single_row`(1행); `test_erb_n_rows`(3행 `@Row.*`); `test_erb_outside_tokens_untouched`; `test_erb_row_none_value_dash`(`@Row.CurrentValue` None→`-`).
- **GREEN** `<!--@EachEquipment-->…<!--@EndEachEquipment-->` 추출·복제·치환·splice.

### P1-7. ERB 정렬·멀티플리시티 (§7.4)
- **RED** `test_erb_default_sort`(심각도 desc→현재값 worst→eqpId asc); `test_erb_breach_per_row`(동일 eqp 2 fact→2행); 스칼라는 worst 멤버.
- **GREEN** 정렬 키 + 행=breach.

### P1-8. 크기 가드 (D3)
- **RED** `test_erb_row_cap`(>`rms_erb_row_limit`→cap + `@RemainingCount`); `test_body_byte_cap`(초과 절단 + 경고).
- **GREEN** cap/overflow/byte guard(한계값 settings 주입).

### P1-9. 제목 렌더 + 콜론 제거 + 기본 제목 (D1)
- **RED** `test_title_render_strips_colon`(결과에 `:` 없음); `test_title_falls_back_to_default_const`(템플릿 title 비면 **내장 기본 제목 상수** 사용 — RMS가 title 항상 산출).
- **GREEN** `render_title()` + 기본 제목 상수.

### P1-10. 리터럴 `@HttpWebServerAddress` 무력화 (D2)
- **RED** `test_data_value_neutralizes_reserved_token`.
- **GREEN** 데이터 값 내 예약 토큰 escape.

### P1-11. 기본 본문 상수 + golden JSON 산출 (이동: 구 P7)
- **RED** `test_builtin_default_body_renders`(내장 기본 본문 상수가 컨텍스트로 렌더됨 — P3-4 fallback이 의존); `test_golden_cases_match`: `tests/data/email_template_golden.json`(케이스 배열 `{name, template_html, title_template, context{scalars,rows}, expected_body, expected_title}`)를 로드해 렌더 결과 == expected.
- **GREEN** 기본 본문/제목 상수 + golden 파일 + 파라미터라이즈 로더. (선례: `tests/data/schema_cases.json` + `test_schema_cases_xcheck.py` 패턴 재사용)

> **REFACTOR**: escape→ERB→스칼라 순서 보장 단일 패스. golden은 이후 모든 단계(P6 포함)의 정본.

---

## P2. 템플릿 accessor + deps 배선 (RMS) — 5-tier 폴백, read-only (§7.1, D5)

### P2-1. 폴백 순서
- **RED** `test_template_repository.py`(목 컬렉션): exact → `subcode="_"` → `model="_"` → `process="_"` → `code="_"`, 전부 미스 시 `None`.
- **GREEN** `RmsEmailTemplateRepository.find_template(...)` (async motor).

### P2-2. read-only 가드 (DoD)
- **RED** `test_accessor_is_read_only`: 목 컬렉션에 `find_one`/`find`만 await, `insert_one`/`update_one`/`replace_one`/`delete_one`은 `.assert_not_awaited()` (선례 `tests/unit/test_db_seed.py`, `test_db_repository.py`).
- **GREEN** find 전용 구현.

### P2-3. deps 배선 (숨은 의존 — 리뷰 지적)
- **RED** `test_repos_builds_template_repo`: `startup/repos.py`의 `RepositoryContext`가 `template_repo`를 만든다; 통합 `_make_engine` deps(`test_phase1_analysis_e2e.py`)에 `template_repo` 포함.
- **GREEN** `RmsEmailTemplateRepository`를 **RepositoryContext(`startup/repos.py`)·`AnalysisEngine` deps·`SchedulerDeps`(scheduler_init.py)·`main.py` 생성지점·통합 `_make_engine`**에 배선. Mongo 핸들은 `src/db/client.py:46-53`(실 핸들; `settings.py:43`은 DB명 문자열일 뿐).

### P2-4. 통합(실 Mongo)
- **RED** `test_template_repository_it.py`(`integration`): catch-all `(_,_,RESOURCE_MONITOR,_)` 시드 1행 → 임의 process/model 조회 시 catch-all 매칭(§7.1 핵심 이점).
- **GREEN** 컬렉션 연결.

---

## P3. 배선: settings·model·dispatch·builder (RMS)

> **핵심 정정(리뷰):** async 템플릿 조회·행 컨텍스트 조립은 **`_dispatch`(이미 async)**에서 하고, **`build_alert_request`는 sync 유지**(또는 별도 async 래퍼). 기존 sync `TestBuildAlertRequest` 호출부 불변.

### P3-1. settings (D3, D7)
- **RED** `test_settings_defaults`: `rms_custom_body_enabled`=**False**, `rms_erb_row_limit`=50, byte cap 기본.
- **GREEN** 필드 추가.

### P3-2. EmailAlertRequest 모델 + to_payload (§5-④) — **올바른 파일**
- **RED** `tests/unit/test_alert_models.py::TestEmailAlertRequest`(← `test_models.py` 아님): `renderedBody`/`title` 옵셔널, **None이면 to_payload 키 미포함**(기존 `test_payload_keys_match_akka_schema` 9키 세트와 **공존**), 값 있으면 포함.
- **GREEN** 모델 필드 + 조건부 직렬화.

### P3-3. dispatch: 멤버 리스트 + timestamp 전달 (§2, §7.4)
- **RED** `test_analysis_engine.py::TestRenderedBody`: `_dispatch`가 그룹/단일 모두 **멤버 breach 리스트 전체** + `AnalysisResult.timestamp`를 빌더 경로로 전달. (`now`는 `_do_analysis`(engine.py:70)에서 `_dispatch`로 스레딩 — 호출부 engine.py:116 변경, P3-3 범위.)
- **GREEN** `_dispatch` 시그니처/호출부 + 비동기 `find_template`·행 컨텍스트 조립. **회귀**: 기존 `TestGroupSend` 그린.

### P3-4. 렌더 통합 (D5, D7) — builder는 sync, 조회는 dispatch에서
- **RED** `tests/unit/test_alert_builder.py`:
  - `test_existing_sync_calls_unchanged`: 기존 `TestBuildAlertRequest` 동기 호출 그대로 통과(빌더 sync 유지 가드).
  - `test_flag_off_no_rendered_body`(off→9필드).
  - `test_flag_on_template_found_renders`(on+템플릿→renderedBody/title 세팅).
  - `test_template_miss_uses_builtin_default`(미스→내장 기본 본문, P1-11 상수).
  - `test_render_error_falls_back`(예외→내장 기본).
  - `test_operator_groupby_groupvalue_timestamp_fact_bound`: `@Operator`=`breach.op`, `@GroupBy`=`notify.group_by`, `@GroupValue`=`resolve_group_value(notify.group_by, breach, eqp_info, process)`(**기존 인자로 산출 — 신규 파라미터 불필요**), `@Timestamp`(신규 `timestamp` 파라미터, **키워드 기본값** → 기존 호출부 불변), `@Fact`(@Metric 아님).
- **GREEN** dispatch가 조회한 템플릿+rows를 sync 빌더/렌더러에 주입; try/except 폴백. `build_alert_request`에 `timestamp: datetime | None = None` 추가.

### P3-5. email_client — **가드(특성화), RED 아님**
- 재분류: `email_client`는 `to_payload()` 순수 pass-through(email_client.py:140)라 신규 분기 없음 → P3-2가 키셋 소유. P3-5는 "pass-through 유지" **회귀 가드**로 표기(RED 라벨 제거) 또는 P3-2에 흡수.

> **REFACTOR**: render-context 빌더 분리. 전체 단위 그린.

---

## P4. RMS 페이로드 통합 + 계약 fixture (실 인프라: ES/Mongo/Redis, Akka는 목)

- **RED** `tests/integration/test_phase1_analysis_e2e.py` 확장:
  - `test_group_model_renders_n_row_table`(모델 3대→1통, renderedBody 3행, escape).
  - `test_single_mode_one_row`(eqp 모드→1행).
  - `test_flag_off_legacy_payload`(off→9필드, 회귀).
  - `test_template_miss_builtin`(시드 없이→내장 기본 본문 발송, "no template" 실패 0).
- **RED (계약 라운드트립)** `test_payload_roundtrips_akka_contract`: 실 `EmailAlertRequest.to_payload()`(renderedBody/title 포함)를 **golden 계약 fixture**로 저장 → P5의 `EmailHttpDataFormatSpec`이 **동일 fixture**를 `extract()`해 필드명/형 일치 보증(키 오타·casing 불일치 방지). (Akka 라이브 불필요)
- **GREEN** P1~P3 통합. **전체 스위트 그린**. P4 라벨을 "RMS payload integration (Akka mocked)"로 명시.

---

## P5-0. Akka 테스트 하네스 부트스트랩 (HttpWebServer) — **신설, 선행 차단**

> 현재 HttpWebServer는 테스트 프레임워크가 **전혀 없음** → 이 단계 전엔 어떤 Akka RED도 fail-first 불가.
- **RED-enabling** (이 단계 자체의 DoD = "1==1 scalatest Spec이 `mvn test`로 실행·그린"):
  - `pom.xml`에 test-scoped 의존 추가: `org.scalatest:scalatest_2.11:3.0.x`, `com.typesafe.akka:akka-testkit_2.11:2.4.16`(Scala 2.11.8 호환), junit.
  - 테스트 러너 등록: `scalatest-maven-plugin`(또는 `scala-maven-plugin`에 `testCompile` goal + `maven-surefire-plugin`).
  - `src/test/scala/samples/{specs,junit,scalatest}.scala` **삭제/수정**(미선언 `org.specs2`/`org.junit` import → testCompile 깨짐).
- 메모: `EmailWorker(conf: Config, cassandraConnection: Cluster)`(EmailWorker.scala:37), Redis는 `actorSelection("/user/Master/RedisActor")`(고정) — 액터 통합 테스트는 이 경로에 TestProbe 등록 + Config/Cassandra 필요(무거움 → P5는 가능한 순수 단위로).

---

## P5. Akka 수신·본문 투입 (D1, D2) — 순수 단위 위주

### P5-1. 계약 파싱 — `EmailHttpDataFormatSpec` (순수 json4s, 액터/Mongo/Redis 불필요)
- **RED**:
  - renderedBody/title 있는 JSON → 둘 다 `Some(value)`.
  - **레거시 9필드 JSON(필드 없음) → 둘 다 `None` + 나머지 9필드 정상 추출**(진짜 하위호환 가드).
  - `Option[String]` 선언이라 생략 시 `MappingException` 없음(§5-④).
- **GREEN** `EmailHttpDataFormat`에 `renderedBody: Option[String]`·`title: Option[String]` 추가(`JsonInterfaces.scala`).

### P5-2. 본문/제목 선택 — 순수 helper `EmailBodyResolver` (액터에서 추출)
- **RED** `EmailBodyResolverSpec`(순수, 4분기): `resolve(renderedBody, title, legacyBody:=>(String,String), publicAddr)`:
  - renderedBody=`Some` → 본문=renderedBody, **`@HttpWebServerAddress`만 치환**, `legacyBody` **강제 평가 안 함**(getEmailBody 미호출), 제목=`title`(RMS가 항상 제공).
  - renderedBody=`None` → `legacyBody`(기존 경로).
  - **renderedBody=`Some` + 템플릿 행 없음 → 정상 반환**(line 608 "There is no email template" 미발생).
  - 제목: renderedBody 모드에서 `title`이 단일 소스(레거시 getEmailBody 제목 폴백 없음 — D1 정정 일치).
- **GREEN** `SendEmail` case **최상단에서 분기**: `if(conv.renderedBody.isDefined) { retString = ...replaceAll("@HttpWebServerAddress", addr); emailtitle = conv.title.get; getEmailCategory; publish } else { 기존 getEmailBody 경로 }`. 선택 로직은 `EmailBodyResolver`로 추출해 그 helper만 순수 단위 테스트.
- **데모트**: `getEmailCategory`/Redis 발행 불변은 (액터 통합 또는 P4 계약)으로 — 순수 단위에서 제외.

### P5-3. 특성화/회귀
- **기존 스펙 없음** → renderedBody=None 회귀 baseline은 **resolver 레벨**에서 확보(`EmailBodyResolverSpec`의 None→legacy 분기 + `EmailHttpDataFormatSpec`의 legacy 9필드→None/None 파싱). "기존 SendEmail/RTM/Recovery 스펙 그린"은 삭제(존재하지 않음).
- **액터(EmailWorker.SendEmail) 레벨 특성화는 inspection-only**(§10: 액터 통합 테스트는 무거워 P7 수기로 데모트). 근거: renderedBody=None일 때 타는 legacy `else` 블록은 **원본 코드 byte-unchanged**로 `else {}`에만 감쌌으므로 회귀 위험이 구조적으로 0에 수렴(코드 리뷰로 확인). 회귀 가드 #3/#5는 이 inspection + resolver 스펙으로 보증.

---

## P6. WebManager 신규 feature + 편집기 UI (vitest + Playwright)

> **단일 정본 이름**: `rmsEmailTemplate` / `/api/rms-email-template` / `rms-email-template` / `RESOURCE_MONITOR_EMAIL_TEMPLATE` (전부 동일 문자열).

> **구현 상태(2026-06-09):** P6-0~P6-5 **완료**(적대 리뷰 통과). 서버 feature 클론(8 vitest), 권한 단일 문자열 배선(전 위치), 클라 JS 렌더러+golden 벤더+드리프트 가드+lint, 편집기 UI(컴포넌트 클론 + 토큰 팔레트/ERB 삽입/미리보기/ERB 경고·가드, editorHelpers 8 vitest). rms feature 클라 **35 vitest** 그린, 서버 1243 그린, `vite build` 성공. **타입값 계약(MF-1)**: 프리뷰는 스칼라/행 값을 문자열로 공급해야 Python canon과 byte-패리티(JS는 float `.0` 복구 불가). seedPermissions는 신규 배포 시 실행 필요(P7). **P6-5 대화형 부분(TinyMCE 주석 제거·클릭 삽입·탭 전환)은 브라우저 수동검증 필요**.

### P6-0. 실행환경 (리뷰 지적)
- `client/package.json`에 `"test":"vitest run"`(+`test:watch`) 추가(서버와 동일). 그 전까진 `npx vitest run`.

### P6-1. 서버 feature 클론 (vitest)
- **RED** `server/features/rms-email-template/__tests__`: base-7 CRUD, **`requiredFields`에 `title` 포함**(기본값엔 title 없음 → 명시 전달해야 "title 누락→에러" RED가 fail-first), 중복키.
- **GREEN** `model/service/controller/routes.js` 복제 + `requiredFields:[...,'title','html']`. `app.js` 마운트.

### P6-2. 권한·라우터 배선 + **이름 일관성 체크리스트** (DoD)
- **GREEN**: 아래 **모든 위치에 동일 문자열** `rmsEmailTemplate` 사용 — `router/index.js`의 `meta.permission`·`meta.menu.permission`; `users/model.js` permissions 서브스키마 + `DEFAULT_ROLE_PERMISSIONS`; `permissions/model.js` feature enum + `DEFAULT_FEATURE_PERMISSIONS` + `FEATURE_NAMES`; `permissionUtils.js` `permissionNames`/`menuPermissionGroups`/`featurePermissionGroups`; `app.js` 마운트. (메뉴는 `menu.js`가 `meta.menu`에서 자동 도출)

### P6-3. 클라 렌더러 golden — **벤더링 + drift 가드** (정본 동기화)
- **RED** `features/rms-email-template/__tests__/renderer.spec.js`: RMS의 `tests/data/email_template_golden.json`을 **WebManager로 벤더링한 사본**을 로드해 JS 렌더러 결과 == expected. + `golden_copy_in_sync.spec.js`: 벤더 사본이 RMS 정본과 **byte-identical**(아니면 RED). (RMS·WebManager는 별도 git repo + CLAUDE.md 훅으로 cross-subproject 편집 차단 → 복사 + 동기 가드 방식.)
- **GREEN** 클라 렌더러(escape/ERB/콜론 규칙 = P1과 동일).

### P6-4. lint (vitest)
- **RED** 미지 `@토큰` 경고(허용집합=카탈로그, `@Metric` 없음), ERB 펜스 불균형 차단, `@HttpWebServerAddress` 예외.
- **GREEN** 저장 훅(`validation.js`) 룰.

### P6-5. 편집기 UI + **TinyMCE 주석 보존** (Playwright/component)
- **RED**:
  - 팔레트 클릭→커서 삽입, [ERB 삽입]→스켈레톤 삽입, 미리보기 단일/그룹 토글 렌더, 저장. 콘솔 0.
  - **`<!--@EachEquipment-->` 포함 시 비주얼 탭 진입/저장 → ERB 경고 다이얼로그**(showCssWarning 패턴 확장; 현재는 full-HTML만 감지).
  - **Monaco(HTML) 탭에서 저장 시 `<!--@EachEquipment-->`/`<!--@EndEachEquipment-->` verbatim 보존**(handleSave visual 분기 vs code 분기, HtmlEditorModal.vue:538-548).
- **GREEN** 팔레트/ERB 버튼/미리보기 토글/ERB 경고.

---

## P7. 시드·문서 정합·롤아웃 (D5, D7)

- catch-all 시드 1행 `(ARS,_,_,RESOURCE_MONITOR,_)` + 표준 토큰·ERB 기본 HTML(죽은 `{{handlebars}}` 미사용). (기본 본문/제목 **상수는 P1**에서 이미 확정 — 여기선 DB 시드만.)
- 롤아웃: `rms_custom_body_enabled` **off 배포** → P4/P5 검증 → on. (off=현행 100%)
- 🔎 **D3 실측**: 실제 Redis payload·ESB 본문 cap 측정 → `rms_erb_row_limit`/byte cap 보정.
- **문서 정합**: architecture/editor-ui "구현 완료" 표기; architecture §8 playground-parity는 **비대상**으로 이미 정정됨(editor-ui §10).

---

## 8. 회귀 가드 (필수 통과)

1. `rms_custom_body_enabled=off` → RMS 페이로드 **정확히 9필드**(renderedBody/title 없음). 기존 e2e 동일.
2. 기존 RMS 전체 스위트(현 717) 그린. `build_alert_request` **sync 유지**(기존 동기 호출부 불변).
3. Akka: `renderedBody=None` → 기존 SendEmail 동작 불변(특성화 테스트로 baseline).
4. **공유 컬렉션 불가침(측정 가능)**: RMS accessor는 **read-only**(write 메서드 `assert_not_awaited`, P2-2). WebManager CRUD는 Mongoose 모델이 컬렉션명을 **정적 바인딩**(`RESOURCE_MONITOR_EMAIL_TEMPLATE`)하여 `EMAIL_TEMPLATE_REPOSITORY`를 물리적으로 못 건드림. EQP_INFO/EMAILINFO/EMAIL_RECIPIENTS/POPUP_TEMPLATE_REPOSITORY·Redis·EmailingAgent **무변경**.
5. **수신자 라우팅 불변(P5)**: renderedBody=Some에서도 `getEmailCategory(process,model,hostname,code,**line**)` 입력 동일(D2). (off→on 시 RMS 페이로드의 `hostname/process/model/code/line` 동일도 통합에서 확인 가능.)

---

## 9. Definition of Done

- [x] P1~P6 각 RED→GREEN 완료(실패 선확인). **P5-0이 P5보다 먼저 그린**.
- [x] RMS `pytest tests/ -q` 그린(798 passed), 회귀 가드 1~5 통과, **golden JSON이 단일 정본**.
- [x] Akka: 하네스 부트스트랩(P5-0, JDK8) 후 `EmailHttpDataFormatSpec`(파싱) + `EmailBodyResolverSpec`(4분기) 그린(`mvn test` 7). (monolithic EmailWorkerSpec 아님)
- [x] WebManager: 서버 `npm test`(1243) + 클라 rms-email-template 35 vitest, **golden 벤더 사본 byte-equality 가드 그린**, ERB 경고/저장 가드 + editorHelpers 단위 그린. ⏳ Playwright(콘솔 0)·TinyMCE 주석 보존 등 **대화형은 브라우저 수동검증 대기**(runbook §9).
- [x] 권한/라우트 **`rmsEmailTemplate` 단일 문자열** 전 위치 일치(P6-2 체크리스트).
- [x] catch-all 시드(`tools/seed_template_catchall.py`, 멱등 upsert·dry-run 검증) + 내장 기본 본문/제목 상수 동작.
- [ ] **플래그 off 배포 → 검증 → on 절차** — 절차 문서화 완료([p7-rollout-runbook.md](p7-rollout-runbook.md)); 실제 배포/플래그 flip·D3 실측은 **운영 핸드오프**.

---

## 10. 미결/주의 (계획 외 의존)

- **D3 사이즈 실측**(인프라 cap) — ops/부하테스트.
- **렌더러 정합성** — golden JSON(정본) + 벤더 사본 byte-equality. 완전 일치 필요 시 서버 canonical render 엔드포인트(후속).
- **Akka 액터 통합 테스트**(getEmailCategory/Redis 실경로)는 P5-0 하네스 위에서 무거움 → 순수 resolver/파싱으로 대체, 실경로는 P4 계약 + P7 수기.
- **CLAUDE.md hooks**: 구현은 각 하위 프로젝트 컨텍스트에서. RMS↔WebManager는 별도 repo → golden은 복사+가드.

---

## 11. 관련 문서
| 문서 | 내용 |
|---|---|
| [resource-monitor-email-template-architecture.md](resource-monitor-email-template-architecture.md) | 설계·스키마·토큰·결정(D1~D7) |
| [resource-monitor-email-template-editor-ui.md](resource-monitor-email-template-editor-ui.md) | WebManager 편집기 UI 목업 |
| [SCHEMA.md](../SCHEMA.md) | RESOURCE_MONITOR_PROFILE 스키마 |
