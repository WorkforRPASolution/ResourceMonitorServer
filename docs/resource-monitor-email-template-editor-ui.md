# RMS 알림 메일 템플릿 — WebManager 편집기 UI (목업)

> 상태: **구현 완료(P6) — 편집기 UI 빌드됨.** 서버 feature 클론 + 클라 편집기(토큰 팔레트·ERB 삽입·미리보기·lint·ERB 경고/저장 가드), rms-email-template 클라 35 vitest 그린, `vite build` 성공. 대화형(TinyMCE 주석 보존·클릭 삽입·탭 전환)은 브라우저 수동검증 필요([p7-rollout-runbook.md](p7-rollout-runbook.md) §9).
> 작성일: 2026-06-09 (구현 반영: 2026-06-09)
> 범위: 신규 컬렉션 `RESOURCE_MONITOR_EMAIL_TEMPLATE`(알림 메일 본문 템플릿)을 운영자가 WebManager에서 작성하는 **편집기 UI** 목업과 구현 방식.
> 짝 문서: [resource-monitor-email-template-architecture.md](resource-monitor-email-template-architecture.md) (파이프라인·스키마·토큰·결정사항). **토큰 전체 목록은 그 문서 §7.2가 1차 진실.**
> 근거: WebManager 실제 코드 크로스체크(file:line). 모든 "재사용/신규" 판정은 코드로 확인됨.

---

## 0. 한 줄 요약

> **`popup-template`이 이미 `email-template` 팩토리를 복제한 선례 그대로** 신규 feature를 복제하고, `HtmlEditorModal`(TinyMCE+Monaco+iframe 미리보기)을 재사용한다. 토큰 팔레트·ERB 삽입·샘플 미리보기·lint는 전부 **클라이언트 상수**로 구동 → **문서 스키마 변경 0**(base-7). 유일한 실구현 주의점은 **TinyMCE의 HTML 주석 제거**(ERB 마커)이며, "ERB는 HTML(Monaco) 탭에서 편집" 동선으로 회피한다.

---

## 1. 구현 토대 (재사용 자산)

| 자산 | 무엇 | 근거(file:line) | 우리 용도 |
|---|---|---|---|
| `HtmlEditorModal.vue` | 3탭 모달: 비주얼(TinyMCE) / HTML(Monaco) / 미리보기(iframe) | `HtmlEditorModal.vue:148-168` | 본문 편집기 그대로 재사용 |
| TinyMCE init | `plugins: table image link lists code fullscreen`, `valid_elements:'*[*]'`, `convert_urls:false` | `:387-428` (`:411`,`:419-422`) | `setup`/post-init 훅으로 커스텀 버튼·팔레트 부착 가능(현재 `setup` 훅 없음, 추가 trivial) |
| `editor.insertContent` | 커서 위치 삽입 | 이미지 삽입에 사용 중 `:555` | **토큰/ERB 커서 삽입의 선례** |
| Monaco 탭 | `language="html"` 평문 소스 편집 | `:125-137` | **ERB(주석) 안전 편집기** |
| iframe 미리보기 | `:srcdoc="previewHtml"`, `previewHtml = toDisplayHtml(html)` **순수 클라 변환** | `:164-168`, `:322` | 주입 전 JS 렌더만 끼우면 "샘플 데이터 미리보기" 완성 |
| CSS 경고 다이얼로그 | 위험 편집 시 사용자 경고 선례 | `:189-229`, `:440-443` | "비주얼 탭은 ERB 제거 위험" 경고에 동일 패턴 |
| `createTemplateService.js` | `app/process/model/code/subcode` 복합키 템플릿 **범용 CRUD 팩토리** | `:20-219` (`requiredFields` `:34-49`) | 신규 컬렉션 서비스로 인스턴스화 |
| `popup-template/*` | 위 팩토리·에디터를 이미 복제한 **선례** | `popup-template/model.js` 등 | 복제 레시피 그대로 |
| `validation.js` | 클라 저장 전 검증 룰 | `:7-38`, 저장 훅 `:538` | lint(미지토큰·ERB 균형) 삽입 지점 |

---

## 2. 화면 ① — 목록 그리드

`EmailTemplateGrid`는 `html` 컬럼을 escape·truncate된 mono 텍스트로 보여주고(`EmailTemplateGrid.vue:182-200`), 셀 더블클릭 시 `edit-html`을 emit해 편집 모달을 연다(`:328-331`). 신규 feature도 동일.

```
┌─ RMS 알림 메일 템플릿  (RESOURCE_MONITOR_EMAIL_TEMPLATE) ───────────────────────┐
│ [+ 새 템플릿]   검색[__________]   app[ARS ▾]  code[RESOURCE_MONITOR ▾]         │
│────────────────────────────────────────────────────────────────────────────────│
│ app │process│model│ code             │ subcode      │ title          │ html      │
│ ARS │  _    │  _  │ RESOURCE_MONITOR │ _            │ [EARS] 자원…   │ <table>…  │◄┐ 더블클릭
│ ARS │ PHOTO │  _  │ RESOURCE_MONITOR │ CPU_CRITICAL │ [긴급] CPU…    │ <div>…    │ │ → 편집 모달
│ ARS │ ETCH  │ M_A │ RESOURCE_MONITOR │ MEM_WARNING  │ 메모리 경고…   │ <p>…      │◄┘
└────────────────────────────────────────────────────────────────────────────────┘
   ▲ 키 5컬럼(app·process·model·code·subcode) + title + html.  process/model="_" = catch-all
```

---

## 3. 화면 ② — 편집기 모달 (토큰 팔레트 + ERB 삽입 + lint)

```
┌─ 템플릿 편집:  ARS / _ / _ / RESOURCE_MONITOR / _ ───────────────────────────────────────┐
│ 제목(title): [ [EARS] @Category @Severity - @Hostname ____________________________ ]      │
│                                                                                            │
│ ┌─ 본문(html) ───────────────────────────────────────────┬─ 토큰 팔레트 ────────────────┐ │
│ │ [ 비주얼 ] [ HTML ] [ 미리보기 ]          [⎘ ERB 블록 삽입] │  ▸ 스칼라 (이메일 단위)     │ │
│ │────────────────────────────────────────────────────────│   @Severity   @Category      │ │
│ │ <h3>@Category 임계 초과 (@Severity)</h3>                 │   @Metric     @CurrentValue   │ │
│ │ <p>@Hostname — 현재 @CurrentValue (임계 @Threshold)</p>  │   @Threshold  @WindowMin      │ │
│ │ <p>최근 @WindowMin분 · @Timestamp</p>                    │   @Timestamp  @GrafanaUrl 🔗  │ │
│ │ <table>                                                  │   @Hostname @Model @Line …   │ │
│ │   <tr><th>장비</th><th>현재값</th><th>임계</th></tr>     │  ▸ 행 (@Row.* — ERB 내부)    │ │
│ │   <!--@EachEquipment-->                                  │   @Row.EqpId  @Row.Current…  │ │
│ │   <tr><td>@Row.EqpId</td><td>@Row.CurrentValue</td>      │   @Row.Threshold @Row.Sev…   │ │
│ │       <td>@Row.Threshold</td></tr>                       │   @Row.Model  @Row.Line …    │ │
│ │   <!--@EndEachEquipment-->                               │                              │ │
│ │ </table>                                                 │  (클릭 → 커서 위치 삽입)     │ │
│ │                                                          │  🔗 = URL 토큰(href에만)     │ │
│ │ ⚠ ERB(<!--@Each…-->)는 [HTML] 탭에서 편집하세요          │                              │ │
│ └────────────────────────────────────────────────────────┴──────────────────────────────┘ │
│ lint:  ✓ 인식된 토큰 8종   ✓ ERB 균형 OK(Each 1 / End 1)            [ 취소 ]  [ 저장 ]      │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

- 제목·본문 필드는 기존 모달 구조 그대로. 우측 **토큰 팔레트**(신규 패널)와 상단 **[ERB 블록 삽입]** 버튼이 추가물.
- 하단 **lint 바**가 저장 전 토큰/ERB 상태를 표시.

---

## 4. 화면 ③ — 미리보기 (단일 ↔ 그룹 토글)

기존 미리보기는 `previewHtml = toDisplayHtml(html)`를 iframe `srcdoc`에 넣는 **순수 클라 변환**(`:164-168`,`:322`)이라, 주입 전에 **샘플 데이터로 렌더(토큰 치환 + ERB 펼치기)**만 끼우면 된다.

```
┌─ 미리보기      모드: ( ● 단일 1대 )  ( ○ 그룹 3대 ) ─────────────────────────────┐
│ ┌── iframe (srcdoc, 샘플 데이터로 렌더된 결과) ─────────────────────────────────┐ │
│ │  CPU 임계 초과 (CRITICAL)                                                      │ │
│ │  EQP001 — 현재 91.2 (임계 85.0)                                                │ │
│ │  최근 30분 · 2026-06-09 14:05 KST                                              │ │
│ │  ┌─────────┬────────┬──────┐                                                  │ │
│ │  │ 장비    │ 현재값 │ 임계 │   ← 단일 모드: ERB가 1행                          │ │
│ │  │ EQP001  │ 91.2   │ 85.0 │                                                   │ │
│ │  └─────────┴────────┴──────┘                                                  │ │
│ └────────────────────────────────────────────────────────────────────────────────┘ │
│  ( ○ 그룹 3대 ) 선택 시 ─► ERB가 3행(EQP001/EQP002/EQP003)으로 펼쳐져 렌더됨        │
└────────────────────────────────────────────────────────────────────────────────────┘
```

> 미리보기 렌더러는 RMS 렌더러(정본)와 **동일 규칙**(escape·ERB 펼치기·토큰 치환)을 따라야 한다. 두 구현(JS/Python)의 동작 일치는 golden test로 보증하거나, 완전 일치가 필요하면 서버 "render" 엔드포인트를 두는 방식(후속, §10) — 현재 코드엔 서버 미리보기 엔드포인트가 없고, 클라 렌더만으로도 유용한 미리보기는 충분.

---

## 5. 기능별 구현 방식

각 기능: **동작 / 재사용 / 신규 / 데이터 출처 / 스키마 영향**.

### 5.1 토큰 팔레트 (커서 삽입)
- **동작**: 우측 패널에 스칼라·`@Row.*` 토큰을 그룹별로 나열. 클릭 시 현재 활성 에디터(TinyMCE 또는 Monaco) 커서에 삽입.
- **재사용**: TinyMCE `editor.insertContent`(이미지 삽입 선례 `:555`), 에디터 핸들 `onTinymceInit`로 캡처(`:430-432`). Monaco는 `executeEdits`(신규, 미사용).
- **신규**: 팔레트 컴포넌트 + 토큰 카탈로그 상수(§8).
- **데이터**: **클라이언트 상수**(토큰 집합 고정).
- **스키마 영향**: **없음.**

### 5.2 ERB 블록 삽입
- **동작**: [ERB 블록 삽입] 버튼 → `<!--@EachEquipment--> …행 스켈레톤… <!--@EndEachEquipment-->`를 커서에 삽입.
- **재사용**: 5.1과 동일 삽입 경로.
- **신규**: 스켈레톤 문자열 상수 1개.
- **데이터**: 클라 상수. **스키마 영향: 없음.**

### 5.3 샘플 데이터 미리보기 (단일/그룹 토글)
- **동작**: 미리보기 탭에서 단일/그룹 토글에 따라 내장 샘플 fixture로 본문을 렌더해 iframe에 표시.
- **재사용**: 기존 iframe `srcdoc` 미리보기(`:164-168`), 순수 클라 변환 지점(`:322`)을 `render(sample)` → `toDisplayHtml` 순으로 확장.
- **신규**: 클라 렌더러(토큰 치환·ERB 펼치기·escape) + 샘플 fixture(§8) + 토글.
- **데이터**: **클라 상수 fixture**(서버 호출 불필요).
- **스키마 영향**: **없음.**
- 주의: iframe `sandbox="allow-same-origin"`(스크립트 불가)라 렌더 결과는 정적 HTML이어야 함(렌더러가 정적 HTML 산출 → 무관).

### 5.4 lint / 가드레일
- **동작**: 저장 전 ① 미지 `@토큰`(허용 집합 외) 경고 ② ERB 펜스 불균형(`Each`/`End` 개수 불일치) 차단.
- **재사용**: 저장 훅(`:538`) / `validation.js` 룰 확장(`:7-38`).
- **신규**: 허용 토큰 집합과 비교 + 정규식 펜스 카운트.
- **데이터**: 클라 상수(허용 토큰 = 팔레트와 동일 카탈로그).
- **스키마 영향**: **없음.**
- 주의: 이미지 URL 자리표시자 `@HttpWebServerAddress`(imageUrl.js `:14-17`)를 본문 토큰으로 오인하지 않도록 lint 예외 처리(URL 접두라 구분 가능).

---

## 6. ⚠️ 실구현 핵심 리스크 — TinyMCE 주석 제거

**TinyMCE는 비주얼 탭 라운드트립에서 HTML 주석 노드를 기본 제거**한다. 현재 init(`:387-428`)에 주석 보존 설정이 없고 `valid_elements:'*[*]'`(`:411`)는 *요소*만 통제(주석 노드 비대상). 따라서 운영자가 **ERB 템플릿을 비주얼 탭에서 저장하면 `<!--@EachEquipment-->`가 사라져 반복 영역이 깨질 위험**이 있다.

**완화 동선(목업 반영):**
1. **ERB는 HTML(Monaco) 탭에서 편집** — Monaco는 평문이라 주석 그대로 보존(`:125-137`). 팔레트의 ERB 삽입 버튼은 비주얼 탭에서 누르면 자동으로 HTML 탭으로 전환하거나 경고.
2. **비주얼 탭 진입/저장 경고** — 본문에 ERB 마커가 있으면 기존 CSS 경고 다이얼로그 패턴(`:189-229`,`:440-443`)으로 "비주얼 편집은 ERB 마커를 제거할 수 있습니다. HTML 탭에서 편집하세요" 안내.
3. (선택) TinyMCE 주석 보존 설정(`custom_elements`/`protect`/`setup` 규칙)으로 마커 유지 — 빌드 시 검증 필요. 1·2만으로도 안전.

> 이 리스크는 **스키마와 무관**(편집 동선 문제)하며, 동선/경고로 해소된다.

---

## 7. 신규 feature 배선 (config 편집 — 스키마 아님)

`popup-template`이 `email-template`를 복제한 패턴 그대로. **모두 기존 파일의 설정 편집**이며 컬렉션 데이터 스키마 변경이 아니다.

| # | 대상 | 작업 | 근거(file:line) | 분류 |
|---|---|---|---|---|
| 1 | `server/app.js` | 라우트 마운트 1줄 | `app.js:59`,`:66` | config |
| 2 | `client/src/router/index.js` | 라우트 1개 추가(메뉴는 `meta.menu`에서 자동 도출) | email-tmpl `:377-397` / popup `:399-419`; `menu.js:12-60` | config |
| 3 | `server/features/permissions/model.js` | feature enum(`:25`) + `DEFAULT_FEATURE_PERMISSIONS`(`:43-125`) + `FEATURE_NAMES`(`:128-138`)에 `rmsEmailTemplate` 추가 | 위 라인 | config(모델 내 상수) |
| 4 | `server/features/users/model.js` | `permissions` 서브스키마(`:136-159`) + `DEFAULT_ROLE_PERMISSIONS`(`:168+`)에 메뉴 권한 키 추가 | 위 라인 | config(모델 내 상수) |
| 5 | `client .../permissionUtils.js` | `permissionNames`(`:10-34`)·`menuPermissionGroups`(`:80-91`)·`featurePermissionGroups`(`:112-124`)에 라벨 추가 | 위 라인 | config |

**신규 생성 파일**(복제): server `features/rms-email-template/{model,service,controller,routes}.js` + client `features/rms-email-template/{View,api,composable,validation,components}` (단, `HtmlEditorModal`은 공용 재사용).
> 참고: Admin(roleLevel 1)은 권한 체크를 우회(`middleware.js:24`, `auth.js:81`)하므로 권한 시드 전에도 관리자에겐 동작.

---

## 8. 클라이언트 상수 (스키마 대신 코드로)

팔레트·lint·미리보기를 구동하는 **단일 상수 카탈로그**(예시 구조 — 구현 아님). 토큰 출처/의미는 architecture §7.2가 1차 진실.

```
TOKEN_CATALOG = {
  scalar: [
    { token: "@Severity",     label: "심각도",   context: "text" },
    { token: "@Category",     label: "카테고리", context: "text" },
    { token: "@Fact",         label: "지표(raw)", context: "text" },   // @Metric은 v1 미포함(데이터 소스 없음)
    { token: "@CurrentValue", label: "현재값",   context: "text" },
    { token: "@Threshold",    label: "임계값",   context: "text" },
    { token: "@WindowMin",    label: "윈도(분)", context: "text" },
    { token: "@Timestamp",    label: "발생시각", context: "text" },
    { token: "@Hostname",     label: "대표장비", context: "text" },
    { token: "@Model",        label: "모델",     context: "text" },
    { token: "@Line",         label: "라인",     context: "text" },
    { token: "@AffectedCount",label: "장비수",   context: "text" },
    { token: "@GrafanaUrl",   label: "차트링크", context: "url"  },   // href에만
    // … architecture §7.2-A 전체
  ],
  row: [   // ERB 내부 전용
    { token: "@Row.EqpId",        label: "장비ID",  context: "text" },
    { token: "@Row.CurrentValue", label: "현재값",  context: "text" },
    { token: "@Row.Threshold",    label: "임계값",  context: "text" },
    { token: "@Row.Severity",     label: "심각도",  context: "text" },
    { token: "@Row.Model",        label: "모델",    context: "text" },
    // … architecture §7.2-B 전체
  ],
}

ERB_SKELETON = "<!--@EachEquipment-->\n  <tr><td>@Row.EqpId</td><td>@Row.CurrentValue</td><td>@Row.Threshold</td></tr>\n<!--@EndEachEquipment-->"

// ⚠ SAMPLE_FIXTURE는 독립 상수가 아니라 RMS의 canonical golden(JSON, tests/data/email_template_golden.json)에서
//   유도/벤더링한 사본이어야 한다(미리보기 렌더러 ↔ RMS 렌더러 동기화). 사본 drift는 byte-equality 가드 테스트로 차단.
SAMPLE_FIXTURE = {
  single: { scalars: {Severity:"CRITICAL", Category:"CPU", CurrentValue:"91.2", Threshold:"85.0",
                      Hostname:"EQP001", WindowMin:"30", Timestamp:"2026-06-09 14:05 KST", …},
            rows: [ {EqpId:"EQP001", CurrentValue:"91.2", Threshold:"85.0", Severity:"CRITICAL"} ] },
  group:  { scalars: { …, AffectedCount:"3" },
            rows: [ {EqpId:"EQP001", CurrentValue:"91.2", …},
                    {EqpId:"EQP002", CurrentValue:"88.9", …},
                    {EqpId:"EQP003", CurrentValue:"86.1", …} ] },
}
```

- `context:"url"`은 D6 escape 정책과 연동(텍스트=html.escape, url=따옴표만+scheme검증).
- 토큰 추가/변경 시 **이 상수 한 곳만** 수정하면 팔레트·lint·미리보기가 동시 반영(문서 필드 불필요 이유).

---

## 9. 스키마 영향 = **ZERO**

- 신규 컬렉션 문서 스키마 = **base-7**(`app·process·model·code·subcode·title·html`). 편집기 5개 기능 어느 것도 추가 필드를 요구하지 않음.
- Mongoose strict 기본이라 미선언 필드는 저장 시 자동 폐기 → base-7만 보관.
- on/off·ERB cap = RMS **전역 설정**(`rms_custom_body_enabled`, `rms_erb_row_limit`), 팔레트/lint/미리보기 = **클라 상수** → 전부 비-스키마.
- §7의 배선은 라우터/권한/마운트 **config 편집**이며 컬렉션 스키마 변경이 아님.

---

## 10. 후속 / 미결

- **미리보기 정합성**: 클라 JS 렌더러 ↔ RMS Python 렌더러 동작 일치(golden test) 또는 서버 canonical "render" 엔드포인트(신규) — fidelity 선택, 블로커 아님.
- **TinyMCE 주석 보존 설정**(§6-3)을 둘지: 동선/경고만으로 갈지, 설정까지 추가할지는 빌드 시 검증 후 결정.
- **플레이그라운드 패리티**: 본 기능은 `RESOURCE_MONITOR_PROFILE` 설정 필드를 늘리지 않으므로(NotifyChannel 무변경, architecture D7) 프로파일 playground 패리티 대상 아님. 단, 메일 템플릿 편집기 자체의 미리보기가 그 역할을 대신.

---

## 11. 관련 문서

| 문서 | 내용 |
|---|---|
| [resource-monitor-email-template-architecture.md](resource-monitor-email-template-architecture.md) | 파이프라인·스키마(§7.1)·토큰 전체(§7.2)·ERB(§7.3)·범위(§7.4)·결정(§9 D1~D7) |
| [ADMIN-UI-LEGIBILITY.md](ADMIN-UI-LEGIBILITY.md) | (별개 기능) `RESOURCE_MONITOR_PROFILE` 기준정보 관리 UI |
| [SCHEMA.md](../SCHEMA.md) | RESOURCE_MONITOR_PROFILE 데이터 스키마 |
