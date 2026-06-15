# RMS 알림 메일 본문(@contents) 사용자 정의 — 설계 검토

> 상태: **구현 완료(P1~P6) — 운영 롤아웃(P7) 대기.** Option C 채택·구현(RMS 렌더 + `renderedBody`/`title` 추가 필드 + Akka 직접 사용), 적대 리뷰 통과. 플래그 `rms_custom_body_enabled`는 **2026-06-14부터 기본 on**(다크런치 종료, off→on; 그룹 발송 `email_group` 라우팅의 전제 — `docs/rms-email-group-routing-decision-2026-06-14.md`). 롤아웃 절차: [p7-rollout-runbook.md](p7-rollout-runbook.md).
> 작성일: 2026-06-08 (구현 반영: 2026-06-09)
> 범위: ResourceMonitorServer(RMS) 알림 메일의 **본문을 운영자가 자유롭게 구성**하는 방법. 특히 EARS `EMAIL_TEMPLATE_REPOSITORY`처럼 **별도 컬렉션 기반**으로 두고, **rule 감지 동적 데이터**를 본문에 어떻게 바인딩할지.
> 근거: ARS 모노레포(`HttpWebServer`/`WebManager`/`EmailingAgent`/`ResourceMonitorServer`) 코드 11개 에이전트 병렬 조사 + 설계안 3종 적대 검증. 모든 사실은 `file:line`로 확인됨.

---

## 0. 결론 요약 (TL;DR)

- **Akka(HttpWebServer)가 `@contents`를 받아 본문에 넣는 메커니즘은 이미 존재한다.** 단, 지금은 **복구(Recovery) 메일 경로에서만** 동작한다 (`EmailWorker.scala:460`). RMS가 쓰는 메인 경로(`SendEmail`)에는 `@contents`도, 자유 본문 필드도 **없다**.
- **본문을 HTML로 자유롭게 구성하는 것은 가능하다.** 최종 발송단(EmailingAgent)이 이미 HTML 메일을 그대로 보내고(`setBHtmlContentCheck(true)`), 템플릿 편집기(TinyMCE + Monaco + 미리보기)도 이미 있다.
- **진짜 난제는 "가변 길이 리스트(장비별 행)" 바인딩이다.** Akka의 치환 엔진은 `Map[String,String]`에 대한 **단순 `@키`→문자열 치환(loop 불가)**뿐이라, 장비별 표를 Akka/순수 템플릿만으로는 만들 수 없다. → **표 렌더링은 RMS(Python)에서 해야 한다.** (구조화된 breach 데이터를 가진 유일한 컴포넌트)
- **권고안: Option C** — *별도 컬렉션*(`RESOURCE_MONITOR_EMAIL_TEMPLATE`)에 운영자가 본문 템플릿을 작성 → **RMS가 fetch·렌더(이스케이프+루프+스칼라 치환)하여 완성 HTML 생성** → Akka에 **추가(additive) `renderedBody` 필드**로 전달 → Akka는 그대로 본문에 투입. 적대 검증에서 **유일하게 기술적 블로커 없음(4/5)**.
- 하드 제약 충족: **EQP_INFO / EMAILINFO / EMAIL_RECIPIENTS 구조 무변경**, 수신자 라우팅·Redis·EmailingAgent **무변경**.

---

## 1. 현재 메일 파이프라인 (조사로 확정된 사실)

```
RMS (Python)                Akka HttpWebServer            Redis              EmailingAgent           외부 ESB
─────────────               ──────────────────            ─────              ─────────────           ───────
build_alert_request()       POST /EmailNotify
  → variables{} 구성   ─────► SendEmail case
EmailAlertClient.send_alert    getEmailBody(p,m,code,sub)  ← EMAIL_TEMPLATE_REPOSITORY.html (Mongo)
  POST 9필드 JSON              @토큰 replaceAll 치환
                              "[EARS][title][host][code]:" + body
                              EmailFormat ───────────────► publish
                                                          SendEmails-<proj>:<cat> ──► EmailActor
                                                                                       title:body 분리(첫 ':')
                                                                                       EMAILINFO로 수신자 해석
                                                                                       sendMISMail() ──────────► HTML 발송
```

### 1.1 RMS가 보내는 것 (변경 seam)
- `EmailAlertClient.send_alert`가 httpx로 `settings.email_api_url`(기본 `http://httpwebserver:8080/EmailNotify`)에 POST. (`email_client.py:111-141`, `settings.py:68`)
- 페이로드는 **정확히 9필드**: `hostname, ip, app, process, model, line, code, subcode, variables`. **`renderedBody`/`body` 없음.** (`models.py:44-56`)
- `variables`(=치환 토큰 소스)는 `build_alert_request`에서 구성: `Severity, Category, MetricName, CurrentValue, Threshold, WindowMin, GrafanaUrl` (+그룹 발송 시 `AffectedEquipment, AffectedCount`). (`alert_builder.py:81-92`)
- 키 식별: `code`=`notify.email_code`(기본 `RESOURCE_MONITOR`), `subcode`=`notify.email_subcode` 또는 `{CATEGORY}_{SEVERITY}`(예 `CPU_CRITICAL`). (`alert_builder.py:73`, `db/models.py:345`)
- `NotifyChannel` 스키마: `cooldown_minutes, email_code, email_subcode, group_by, email_group` 뿐. `extra="forbid"`. → 본문/템플릿 필드를 추가하려면 스키마 변경 필요. (`db/models.py:325-348`)
- **추가 작업 seam = `build_alert_request`** (breach + eqp_info + notify + window + affected가 모두 모이는 단 하나의 함수). (`alert_builder.py:58-107`)

### 1.2 Akka가 본문을 만드는 방식
- 본문 = `EMAIL_TEMPLATE_REPOSITORY.html`을 `getEmailBody(process, model, code, subcode)`로 조회. **3단계 폴백**: `(p,m,code,sub)` → `sub='_'` → `code='_'`. (`EmailWorker.scala:84-111`)
- ⚠️ **`process`/`model`은 항상 리터럴로 고정** — **와일드카드 불가**. `'_'` 폴백은 **`subcode`와 `code`에만** 적용된다. (검증으로 확정; 아래 §5 위험 참고)
- 치환 = 순수 `String.replaceAll`로 **`@`접두 토큰** 대체. 고정 토큰: `@Hostname, @Process, @Model, @IP, @Line, @CODE, @Sdwt, @HttpWebServerAddress, @__snapshot__`. 동적: `variables`의 각 키 `k` → `@k`. (`EmailWorker.scala:564-593`)
- 값은 `Matcher.quoteReplacement`로 정규식 escape만 함(`$`,`\`) — **HTML escape는 어디에도 없음.** (`EmailWorker.scala:589`)

### 1.3 `@contents`의 현재 위치 (중요)
- `@contents` 토큰은 **오직 `SendRecoveryEmail` 경로 1곳**에만 존재: 래퍼 템플릿(`defaultEmailBodyTemplate`)의 `@contents`를 요청의 `body`로 치환. 래퍼가 비면 `body`를 그대로 본문으로 사용. (`EmailWorker.scala:456-460`, `67-82`)
- 즉 **"운영자/생산자가 만든 자유 본문을 외곽 chrome 템플릿의 슬롯에 끼워넣는다"는 패턴이 이미 구현되어 검증된 셈**(=구현 존재 증명). 다만 RMS가 쓰는 `SendEmail`/`SendEmailForRTM`/`ScriptResult` 경로엔 없음.
- 자유 본문 필드(`body`)도 **`RecoveryEmailHttpDataFormat`에만** 있다. 메인 `EmailHttpDataFormat`엔 없음. (`JsonInterfaces.scala:7,9`)

### 1.4 템플릿 컬렉션 & 편집기 (이미 있는 자산)
- `EMAIL_TEMPLATE_REPOSITORY` 스키마: `app, process, model, code, subcode`(복합키) + `title`(제목) + `html`(본문). category/language 차원 없음, **변수 manifest 개념 없음.** (`WebManager/server/features/email-template/model.js:11-44`)
- 편집: 그리드 + `HtmlEditorModal` — **TinyMCE WYSIWYG / Monaco raw-HTML / iframe 미리보기** 3-뷰. `valid_elements:'*[*]'`, `convert_urls:false`라 **`@토큰`이 비주얼 편집을 거쳐도 보존됨.** (`HtmlEditorModal.vue:411-422`)
- CRUD: `/api/email-template`, JWT + `requireFeaturePermission('emailTemplate', ...)`. **`createTemplateService` 팩토리는 재사용 가능** — `POPUP_TEMPLATE_REPOSITORY`가 이미 같은 팩토리로 복제됨. (별도 컬렉션 추가가 저렴하다는 근거)
- ⚠️ 시드 픽스처(`seedManualData.js:133-141`)는 `{{eqpId}}` 같은 **handlebars 문법**을 쓰는데, **파이프라인 어디서도 치환되지 않는다(죽은 문법, 함정).** 채택할 문법은 **`@토큰` 일원화**여야 함.

### 1.5 동적 치환의 "유일한 작동 엔진"
- `EmailWorker`가 템플릿 fetch + 동적 치환을 하는 **유일한 살아있는 코드**다. EmailingAgent는 **템플릿을 전혀 안 함** — `title:body`를 첫 `:`로 분리해 ESB로 그대로 전송. (`EmailActor.scala:140-142, 189-200`)
- `KafkaToElastic/EmailManager.scala`에 유사 패턴 복제본이 있으나 **컴파일 불가(죽은 코드)** — 무시.
- 최종 발송: 외부 삼성 ESB 게이트웨이. **HTML 활성(`setBHtmlContentCheck(true)`)**, UTF-8/ko_KR. 본문은 **검증·사이즈 제한 없이 그대로** 통과. (`EmailActor.scala:45`)
- `title:body` 분리는 **첫 번째 `:`** 기준이고, 제목 접두부 `[proj][title][host][code]`엔 `:`가 없으므로 **본문 HTML 안의 `:`(예: `style="..."`, `http://`)는 분리를 깨지 않음**(검증됨).

---

## 2. 핵심 난제: rule 감지 동적 데이터 바인딩

사용자가 정확히 지적한 지점. 셋으로 분해된다.

| 데이터 유형 | 예 | 바인딩 난이도 | 해법 |
|---|---|---|---|
| **고정 문구** | "임계치 초과 알림입니다" | 쉬움 | 템플릿 HTML에 그대로 |
| **스칼라 동적값** | 지표명, 현재값, 임계값, 심각도, 시각 | 쉬움 | `@토큰` 치환 (이미 가능) |
| **가변 길이 리스트/표** | 그룹 발송 시 **걸린 장비별 행** | **어려움** | **RMS에서 렌더링** (아래) |

**왜 리스트가 어려운가:** Akka의 치환은 `Map[String,String]`에 대한 평면 `@키`→문자열 대체일 뿐, **반복(loop)·하위 템플릿이 불가**하다. 장비 N대를 `<tr>` N개로 펼치는 일을 Akka도, 순수 템플릿 문법도 할 수 없다.

**누가 데이터를 갖고 있나:** 구조화된 per-breach 데이터(eqp_id, current_value, threshold_value, severity)는 **RMS의 `_dispatch` 안 `members: list[_Pending]`에만** 존재한다 (`engine.py`, `threshold.py:34-42`). Akka엔 평면 문자열만 도착한다. **따라서 표 렌더링의 책임은 RMS에 둘 수밖에 없다.**

> ⚠️ 현재 RMS는 그룹 발송 시 **콤마로 합친 문자열**(`AffectedEquipment`)과 대표 breach 1건만 `build_alert_request`로 넘긴다 (`alert_builder.py:90-92`). per-장비 행을 만들려면 `_dispatch`가 **멤버 breach 리스트 전체**를 넘기도록 시그니처를 바꿔야 한다(작지만 실재하는 변경).

---

## 3. 사용자 질문에 대한 직접 답변

**Q1. Akka가 `@contents`를 받아 본문에 잘 넣을 수 있나?**
→ **그렇다, 그러나 지금은 복구 경로만.** 메커니즘 자체는 검증됨(`EmailWorker.scala:460`). RMS 메인 경로에 적용하려면 **작은 additive 변경**(아래 §4)이 필요. 최종 HTML 발송 능력(EmailingAgent)·colon-safe 분리는 모두 확인됨.

**Q2. 사용자가 `@contents`를 정의해 본문을 구성할 수 있나? 어느 수준까지?**
→ **가능. 수준:**
- ✅ **완전 자유 HTML 본문** — TinyMCE/Monaco 편집기 이미 존재, ESB가 HTML 그대로 발송.
- ✅ **스칼라 동적값** 임의 개수 — `@MetricName @CurrentValue @Threshold @Severity @WindowMin @GrafanaUrl @Timestamp` 등.
- ⚠️ **가변 리스트(장비별 표)** = **「장비 반복 블록(ERB)」**(§7.3) — 가능하지만 **순수 템플릿 문법만으론 불가**, RMS가 행을 펼쳐 렌더해야 함. 운영자는 반복 블록(`<!--@EachEquipment--> … <!--@EndEachEquipment-->`)을 템플릿에 두고, 그 안에 per-행 토큰(`@Row.EqpId @Row.CurrentValue …`, §7.2)을 쓰는 수준까지 가능.
- ❌ **임의 로직(조건/계산/외부조회)** — 불가(템플릿 엔진이 아니라 치환). 필요하면 RMS가 미리 계산해 토큰으로 제공.

**Q3. EARS `EMAIL_TEMPLATE_REPOSITORY`처럼 별도 컬렉션으로 둘 수 있나?**
→ **가능.** `createTemplateService` 팩토리·`HtmlEditorModal`을 복제하면 됨(POPUP_TEMPLATE_REPOSITORY 선례). 별도 컬렉션이면 **RMS가 lookup 키/폴백을 직접 정의**할 수 있어, Akka `getEmailBody`의 process/model 와일드카드 불가 제약(§5)을 피할 수 있다 — 권고안의 핵심 이점.

---

## 4. 설계 옵션 비교 (3안 + 적대 검증 점수)

| | **A. RMS 렌더 → Akka 래퍼 `@contents`** | **B. 기존 컬렉션 재사용 (Akka 치환)** | **C. 별도 컬렉션 + RMS 렌더 → additive `renderedBody` ⭐** |
|---|---|---|---|
| 치환 위치 | RMS | **Akka(기존 엔진)** | RMS |
| 템플릿 저장 | 신규 컬렉션 + 기존 EMAIL_TEMPLATE 래퍼 | **기존 EMAIL_TEMPLATE_REPOSITORY 그대로** | **신규 `RESOURCE_MONITOR_EMAIL_TEMPLATE`** |
| 리스트/표 | RMS `@each` 루프 | RMS가 표를 통째 문자열(`@AffectedTable`)로 | RMS `<!--@EachEquipment-->` 루프 |
| Akka 변경 | 필요(필드+치환) | **0(코어)** ~ 1줄(옵션) | **additive `renderedBody` 필드(권장)** |
| 별도 컬렉션(사용자 이상) | △ | ✗(반대) | **✅** |
| 검증 점수 | **3/5** | **3/5** | **4/5 (블로커 없음)** |
| 치명 약점 | "Akka verbatim 통과" 거짓(치환이 본문 위에 재실행) → `@contents`를 **맨 마지막**에 치환해야; 글로벌 escape가 GrafanaUrl 깨뜨림 | **catch-all 1행 불가**(process/model 와일드카드 안 됨) → (process,model)쌍마다 행 필요; 단일 템플릿 양모드 처리에 Akka "옵션" 1줄이 **사실상 필수** | 최소변형은 두 컬렉션 결합/렌더러 이중화 — **explicit renderedBody 변형으로 해소** |

세 안 모두 공통적으로 **process/model 와일드카드 불가**·**HTML escape 부재**·**naive replaceAll 충돌**이라는 코드 현실에 부딪힌다. C가 RMS-측 렌더 + 자체 컬렉션 + escape 내장으로 이 3가지를 가장 깔끔히 회피한다.

---

## 5. 반드시 알아야 할 코드 현실 (적대 검증이 잡아낸 함정)

1. **`process`/`model`은 와일드카드가 안 된다.** `getEmailBody`의 3단계 폴백은 `subcode`→`code`만 `'_'`로 낮춘다 (`EmailWorker.scala:84-111`). RMS는 실제 process/model을 보내므로(`alert_builder.py:100,102`), **EMAIL_TEMPLATE_REPOSITORY를 직접 재사용(B안)하면 "운영자가 1행만 작성"이 불가**하고 (process,model) 조합마다 행이 필요하다. process/model을 `'_'`로 바꾸면 **수신자 라우팅(`getEmailCategory`)이 깨진다.** → RMS-측 렌더 + 자체 lookup(C안)이 유리.
2. **파이프라인 전 구간에 HTML escape가 없다.** 지금도 `@MetricName`(=`breach.fact` 복합문자열)·`@CurrentValue` 등이 raw로 HTML에 박힌다. 값에 `<`,`&`가 있으면 마크업이 깨진다. → **RMS 렌더 시 `html.escape` 필수.** (단, GrafanaUrl처럼 URL은 컨텍스트가 달라 무차별 escape 금지 — 속성/URL은 따로 처리.)
3. **`replaceAll`은 전역·무앵커**라 토큰 prefix 충돌(`@Threshold` vs `@ThresholdX`)·재치환 위험이 있다. RMS가 완성 HTML을 통째로 넘기고 Akka가 **그 위에 추가 치환을 하지 않게** 해야 안전(→ explicit `renderedBody` 단락 처리 권장, variables-map에 HTML 덩어리를 끼워넣는 B/C-최소변형은 충돌면이 커짐).
4. **`json4s DefaultFormats.extract`는 미지 필드를 무시**한다 (`EmailWorker.scala:38,552`) — 그래서 `renderedBody` 필드 추가는 **하위호환**이지만, 케이스 클래스에 **`String`(필수)로 넣으면 기존 생산자가 누락 시 `MappingException`** 위험 → **`Option[String]`/기본값**으로.
5. **`@__snapshot__`만 잔여 토큰을 빈 문자열로 지운다** (`:592`). 다른 토큰은 안 지움 → 단일 템플릿로 양모드를 처리하려면 잔여 리스트 토큰 blanking 또는 explicit 분기가 필요.

---

## 6. 권고안 — Option C (정제판)

> **"별도 컬렉션 + RMS 렌더 + Akka additive `renderedBody`"**, 적대 검증 권고를 반영해 다듬음.

> ⚠️ **용어 구분(중요):** 이름이 비슷한 둘을 혼동하지 말 것.
> - **`@contents` *토큰*** = 기존 복구 메일 래퍼 템플릿의 치환 자리표시자(`EmailWorker.scala:460`). **Option C는 이 토큰을 쓰지 않는다**(래퍼+토큰 방식 = 거부된 A안). §1.3은 "이 패턴이 작동한다"는 *존재 증명*으로만 인용.
> - **`renderedBody` *페이로드 필드*** = `/EmailNotify` JSON에 새로 추가하는 필드. RMS가 완성한 HTML 본문을 담아 전송하면 Akka가 그대로 본문에 사용. **Option C가 실제로 쓰는 것은 이것.**
> 즉 운영자는 컬렉션의 `html` 컬럼(본문 템플릿)을 작성하고, RMS가 그것을 렌더해 `renderedBody` 필드로 보낸다. `@contents` 토큰 치환 단계는 **없다**.

### 6.1 흐름
1. 운영자가 **신규 컬렉션** `RESOURCE_MONITOR_EMAIL_TEMPLATE`(세부 스키마 §7.1)에 본문 HTML을 WebManager로 작성. `@`스칼라 토큰(§7.2-A) + **장비 반복 블록**(`<!--@EachEquipment--> … <!--@EndEachEquipment-->`, 내부 per-행 토큰 `@Row.*`, §7.2-B/§7.3).
2. **RMS `build_alert_request`**가 (app, code, subcode) + `'_'` 폴백으로 템플릿을 fetch(자체 lookup → process/model 제약 없음).
3. **RMS 렌더러**(신규 순수함수, TDD 대상)가: ① 모든 값 **HTML escape** → ② 반복 블록을 행마다 펼쳐 per-행 토큰 치환 → ③ 스칼라 토큰 치환 → **완성 HTML** 산출. 단일·그룹 모드 모두 **항상 멤버 리스트(1개 또는 N개)**로 통일 입력.
4. RMS가 완성 HTML(본문)과 렌더된 **제목**을 각각 additive `renderedBody`·`title` 필드로 `/EmailNotify`에 전달(D1). 전역 플래그 `rms_custom_body_enabled` off면 미전송(D7).
5. **Akka(SendEmail)**: `renderedBody`가 있으면 **그 값을 본문으로 사용**(getEmailBody·데이터 토큰 치환 우회), 제목은 `title` 사용 → 재치환/충돌 위험 0. **예외: `@HttpWebServerAddress`만 치환**(이미지 URL용, D2). 없으면 기존 동작 그대로(하위호환).
6. 수신자 해석(`getEmailCategory`)·Redis·EmailingAgent **무변경**. (세부 확정은 §9 D1~D7)

### 6.2 왜 C인가
- 적대 검증에서 **유일하게 기술 블로커 없음**(4/5). A/B의 치명 약점(Akka 재치환·process/model 와일드카드 불가)을 구조적으로 회피.
- 사용자 이상(별도 컬렉션) 충족 + RMS가 lookup을 소유 → **운영자가 code/subcode 단위로 1행만 작성 가능**(Akka 제약 우회).
- 리스트 문제를 **데이터를 가진 곳(RMS)**에서 해결. escape도 RMS에서 일원화.
- additive `renderedBody`는 **하위호환**(json4s 미지필드 무시 검증됨), variables-map 오버로드의 `@`충돌·순서 위험 없음.

### 6.3 트레이드오프(수용)
- **렌더러 이중화**: WebManager "샘플 미리보기"가 RMS 렌더러와 동일 동작이어야 함. → RMS 렌더러를 **정본(canonical)**으로 삼고 미리보기 endpoint 호출 또는 golden test로 동기화.
- **신규 WebManager 기능 표면**(모델/서비스/컨트롤러/라우트 + `rmsEmailTemplate` 권한) — 팩토리 복제로 저렴하나 권한/시드 plumbing은 실재.
- 반복 블록은 새 mini-문법 → 편집기에 "행 블록 삽입" 버튼 + 토큰 팔레트 + 미지토큰 lint로 가드.

---

## 7. Option C 상세 설계 — 스키마 · 토큰 · 장비 반복 블록 · 범위

> 채택안(Option C)을 공고히 하기 위한 정밀 설계. 모든 토큰/필드는 RMS가 dispatch 시점에 **실제로 보유한 데이터**(`engine._dispatch`, `ThresholdBreach`, `eqp_lookup`)에 근거하며 출처를 명시한다.

### 7.1 신규 컬렉션 `RESOURCE_MONITOR_EMAIL_TEMPLATE` 세부 스키마

기존 `EMAIL_TEMPLATE_REPOSITORY`의 5-키 형태를 **그대로 유지**하여 WebManager `createTemplateService` 팩토리·`HtmlEditorModal`을 재사용한다. **스키마는 base-7로 최소화**(선택 필드 불채택) — 편집기 UI 크로스체크 결과 팔레트·미리보기·lint가 전부 클라이언트 상수로 가능하고, on/off·ERB 행수 cap은 RMS 전역 설정으로 빼므로 **추가 필드가 필요 없다**(상세: [editor-ui 문서](resource-monitor-email-template-editor-ui.md)).

| 필드 | 타입 | 필수 | 설명 / 제약 |
|---|---|---|---|
| `_id` | ObjectId | 자동 | |
| `app` | string | ✓ | 기본 `"ARS"`. RMS `email_app_name`와 일치. **lookup 키1** |
| `process` | string | ✓ | 공정명 또는 `"_"`(전체). **lookup 키2** |
| `model` | string | ✓ | 모델명 또는 `"_"`. **lookup 키3** |
| `code` | string | ✓ | 기본 `"RESOURCE_MONITOR"` (=`notify.email_code`). **lookup 키4** |
| `subcode` | string | ✓ | `notify.email_subcode` 또는 `{CATEGORY}_{SEVERITY}`(예 `CPU_CRITICAL`), 또는 `"_"`. **lookup 키5** |
| `title` | string | ✓ | 메일 제목(또는 제목 템플릿). `@`스칼라 토큰 사용 가능 |
| `html` | string | ✓ | **운영자 본문 템플릿.** `@`스칼라 토큰 + 장비 반복 블록 포함. RMS가 렌더한 결과가 **`renderedBody` 페이로드 필드** 값으로 전송됨(≠ `@contents` *토큰*, §6 용어 구분). Mongo 컬럼명은 편집기 재사용 위해 `html` 유지 |

> **폐기된 선택 필드(결정: 최소 스키마):** `variablesManifest`(→ 토큰 팔레트/lint는 클라 상수), `rowLimit`·`rowOverflowText`(→ RMS 전역 설정 `rms_erb_row_limit`/overflow 문구), `enabled`(→ 개별 비활성화는 행 삭제 또는 전역 플래그 `rms_custom_body_enabled`), `description`/`updatedAt`/`updatedBy`(→ 불필요/감사 out-of-band). Mongoose strict 기본이라 미선언 필드는 저장 시 자동 폐기되므로 base-7 외 값은 보관되지 않는다.

- **복합 유니크 인덱스**: `{ app:1, process:1, model:1, code:1, subcode:1 }`.
- **lookup 폴백(RMS가 직접 구현)**: exact → `subcode="_"` → `model="_"` → `process="_"` → `code="_"` (순서는 §9 결정사항). **핵심 이점:** RMS가 lookup을 소유하므로 Akka `getEmailBody`가 못 하는 **process/model 와일드카드 폴백을 RMS는 할 수 있다**(§5-①의 제약을 정면 회피) → 운영자가 `(process="_",model="_")` **catch-all 1행만 작성**하고 특정 공정/모델만 override 가능.
- `EMAIL_TEMPLATE_REPOSITORY`(EARS 공용)와 **물리적으로 분리** → EARS 템플릿 무영향, 권한도 별도(`rmsEmailTemplate`).

### 7.2 동적 치환 토큰 완전 목록 + HTML 표시

치환은 **RMS 렌더러가 1회** 수행(escape 포함)하고 Akka는 재치환하지 않는다. 토큰은 **이메일 단위 스칼라**와 **반복 블록 내부 per-행** 두 계층.

#### (A) 이메일 단위 스칼라 토큰 — 대표 breach(=worst 멤버; 단일 모드면 그 1건) 기준

| 토큰 | 소스 (file:line) | 예시 값 | 템플릿 → 렌더 결과(HTML) | 상태 |
|---|---|---|---|---|
| `@Severity` | `breach.severity` (threshold.py:42) | `CRITICAL` | `<b>@Severity</b>` → `<b>CRITICAL</b>` | 기존 |
| `@Category` | `breach.category.upper()` (alert_builder.py:72) | `CPU` | `[@Category]` → `[CPU]` | 기존 |
| ~~`@Metric`~~ | **v1 미포함(후속)** — 정제 지표명의 데이터 소스가 코드에 없음(Measure에 label 없음, breach에 measure id 없음) | — | 지표 식별은 `@Fact` 사용 | 보류 |
| `@Fact` | `breach.fact` 원복합값 (threshold.py:37) | `cpu_usage.total_used_pct` | `@Fact` → `cpu_usage.total_used_pct` | 기존(=현 MetricName). **v1 지표 토큰** |
| `@CurrentValue` | `_round_display(breach.current_value)` (alert_builder.py) | `91.2` | `현재 @CurrentValue%` → `현재 91.2%` | **소수 1자리 반올림**(None→`-`) |
| `@Threshold` | `str(breach.threshold_value)` (alert_builder.py:86) | `85.0` | `임계 @Threshold` → `임계 85.0` | 기존 |
| `@Operator` | `breach.op` (threshold.py:39) | `>=` | `@Operator @Threshold` → `>= 85.0` | **신규(파생)** |
| `@WindowMin` | `window_minutes` (engine.py:52) | `30` | `최근 @WindowMin분` → `최근 30분` | 기존 |
| `@Timestamp` | `AnalysisResult.timestamp` (threshold.py:52) | `2026-06-09 14:05 KST` | `@Timestamp` → `2026-06-09 14:05 KST` | **신규(배선 필요)**. 포맷 확정: `%Y-%m-%d %H:%M KST`(Asia/Seoul) |
| `@Process` | `process` (engine.py:224) | `PHOTO` | `@Process` → `PHOTO` | 기존 |
| `@GroupBy` | `channel.group_by` (db/models.py) | `model` | `@GroupBy` → `model` | **신규(파생)** |
| `@GroupValue` | 그룹 식별자(model명/process/eqpId) (alert_builder.py:42-55) | `MODEL_A` | `@GroupValue` → `MODEL_A` | **신규(파생)** |
| `@AffectedCount` | `len(affected)` (alert_builder.py:92) | `7` | `장비 @AffectedCount대` → `장비 7대` | 기존(그룹) |
| `@AffectedEquipment` | 콤마 목록 (alert_builder.py:91) | `EQP001, EQP002, …` | 표 대신 간단형 | 기존(그룹) |
| `@GrafanaUrl` | 딥링크 (alert_builder.py:74-79) | `https://…?var-eqpId=EQP001` | **속성 컨텍스트**: `<a href="@GrafanaUrl">차트</a>` → `href="https://…"` | 기존 |
| `@Hostname` | 대표 eqpId (alert_builder.py:98) | `EQP001` | `@Hostname` → `EQP001` | 기존(Akka chrome) |
| `@Model` | 대표 `eqp_info.eqpModel` (repository.py:343) | `MODEL_A` | `@Model` → `MODEL_A` | 기존 |
| `@Line` | 대표 `eqp_info.line` (repository.py:347) | `L1` | `@Line` → `L1` | 기존 |
| `@IP` | 대표 `eqp_info.ipAddr` (repository.py:346) | `10.0.0.5` | `@IP` → `10.0.0.5` | 기존 |
| `@CODE` | `code`(`-subcode`) | `RESOURCE_MONITOR-CPU_CRITICAL` | `@CODE` → `…CPU_CRITICAL` | 기존(Akka chrome) |

#### (B) 반복 블록 내부 per-행 토큰 — 그룹 내 breach 멤버마다 (네임스페이스 `@Row.*`)

> `@Row.*` 별도 네임스페이스로 스칼라와 분리 → 토큰 prefix 충돌(`@Threshold` vs `@ThresholdX`) 및 모호성 제거.

| 토큰 | 소스 | 예시 |
|---|---|---|
| `@Row.Index` | 1-기반 행 번호(파생) | `1`, `2`, `3` |
| `@Row.EqpId` | `member.breach.eqp_id` | `EQP002` |
| `@Row.CurrentValue` | `_round_display(member.breach.current_value)` | `88.9` (소수 1자리 반올림, None→`-`) |
| `@Row.Threshold` | `member.breach.threshold_value` | `85.0` |
| `@Row.Severity` | `member.breach.severity` | `WARNING` |
| `@Row.Metric` / `@Row.Fact` | `member.breach.fact` | `mem.total_used_pct` |
| `@Row.Category` | `member.breach.category` | `MEMORY` |
| `@Row.Operator` | `member.breach.op` | `>=` |
| `@Row.Model` | `eqp_lookup[eqp].eqpModel` | `MODEL_A` |
| `@Row.Line` | `eqp_lookup[eqp].line` | `L1` |
| `@Row.IP` | `eqp_lookup[eqp].ipAddr` | `10.0.0.6` |
| `@Row.Proc` | `member.breach.proc` | `@system` |

#### (C) HTML 표시 규칙
- **텍스트 토큰**: `html.escape` 적용 — `& < > " '` 변환. 예: `@Fact`가 `a<b&c`면 → `a&lt;b&amp;c`(마크업 안 깨짐).
- **URL 토큰(`@GrafanaUrl`)**: `href`/`src` 속성에만 사용, **URL/속성 escape**(텍스트 escape와 다름). 무차별 `&`→`&amp;`는 `?var-...=...&var-...` 링크를 깨뜨리므로 컨텍스트 분리 필수(§5-②).
- **None/누락**: `-`(또는 빈 문자열)로 치환 — `str(None)="None"` 노출 방지.
- **숫자**: 렌더러는 받은 값을 `str(value)` **그대로** 출력(렌더러 재포맷 없음). 단 **현재값(`@CurrentValue`/`@Row.CurrentValue`/legacy `CurrentValue`)은 소스(`alert_builder._round_display`)에서 소수 1자리로 반올림**(round-half-up, 예 95.34→95.3·88.96→89.0)해 넣는다(2026-06-15 운영 요청). `threshold_value`는 반올림 안 함(str/trend면 verbatim). None은 `-`.
- **단일 치환**: RMS가 1회만 치환 → Akka는 renderedBody에 대해 **`@HttpWebServerAddress` 1개만** 치환(이미지 URL용), 그 외 재치환 0(충돌 회피, D2).

### 7.3 장비 반복 블록 — 기능 명칭 & 문법

**기능 명칭(채택):** 구성요소 = **「장비 반복 블록(Equipment Repeat Block, ERB)」**, 렌더 결과물 통칭 = **「영향 장비 표」**.

| 후보 | 의미 | 평가 |
|---|---|---|
| **장비 반복 블록 (Equipment Repeat Block)** ⭐ | 템플릿의 반복 영역 자체 | 구조를 정확히 지칭, 마커 `@EachEquipment`와 일관 → **채택** |
| 영향 장비 표 (Affected Equipment Table) | 렌더 결과물 | 결과 통칭으로 병행 |
| 장비 다이제스트 / 위반 장비 명세 | 요약/목록 | 의미 모호하거나 딱딱 → 보류 |

**문법** (마커 = HTML 주석이라 TinyMCE 비주얼에서 안 보이고 보존됨):
```html
<table>
  <tr><th>장비</th><th>현재값</th><th>임계</th><th>심각도</th></tr>
  <!--@EachEquipment-->
  <tr>
    <td>@Row.EqpId</td><td>@Row.CurrentValue</td>
    <td>@Row.Threshold</td><td>@Row.Severity</td>
  </tr>
  <!--@EndEachEquipment-->
</table>
```
**렌더 결과** (장비 3대 걸린 그룹):
```html
<table>
  <tr><th>장비</th>…</tr>
  <tr><td>EQP001</td><td>91.2</td><td>85.0</td><td>CRITICAL</td></tr>
  <tr><td>EQP002</td><td>88.9</td><td>85.0</td><td>WARNING</td></tr>
  <tr><td>EQP003</td><td>86.1</td><td>85.0</td><td>WARNING</td></tr>
</table>
```
**규칙:**
- 블록 0/1/N행 모두 **동일 템플릿**: 단일(`group_by="eqp"`) 모드 = 1행, 그룹 모드 = N행. 운영자는 분기 불필요.
- 블록은 **1개만**(중첩·다중 블록·하위표 비지원 — §7.4).
- 마커 불균형(`Each`만/`End`만)은 **저장 시 lint 차단**.
- ERB 행수 상한(RMS 전역 설정 `rms_erb_row_limit`, 기본 50) 초과 시 잘라내고 전역 overflow 문구(`@RemainingCount` 치환) 추가.
- 반복 블록을 안 쓰고 `@AffectedEquipment`(콤마 목록)만 쓰는 **간단형**도 허용.

### 7.4 RMS의 가변 리스트 처리 범위 — 할 수 있는 것 vs 해야 하는 것

**원칙: RMS = 데이터·안전 담당 / 운영자 템플릿 = 레이아웃·스타일 담당.**
**경계 규칙:** dispatch의 `members`/`eqp_lookup`에 **이미 있으면** RMS가 렌더 가능; **새 쿼리·비즈니스 로직이 필요하면** 범위 밖.

**✅ 할 수 있고, 하는 게 적절(RMS 책임):**
- 한 쿨다운 그룹의 `members`(breach들) 반복, 행마다 `breach`+`eqp_info` 필드 출력
- **결정적 정렬 기본값**: 심각도 desc → 현재값 worst → eqpId asc
- **크기 가드**: RMS 전역 설정 `rms_erb_row_limit`(기본 50행) cap + "외 N대" overflow (Redis/ESB 미상 사이즈 한계 대비, §9)
- 안전 일원화: 셀 escape · None 처리 · 숫자 포맷
- 집계 스칼라 제공: `@AffectedCount`, worst 값 등
- 행 단위 = **breach 기본**(동일 eqp가 다른 지표로 2행 가능; 지표 노출에 유리), eqp당 1행 **dedup은 옵션**으로 문서화

**❌ 하지 말아야(범위 밖 → 운영자 템플릿 / 후속 / 비목표):**
- 추가 DB/ES 조회로 행 보강(트렌드 이력·담당자·최근 복구시각 등) → **핫 패스 I/O, 후속 과제**
- 임의 조건/계산/수식 컬럼 → 템플릿은 **치환기지 로직 엔진이 아님**
- 교차 그룹 병합(여러 쿨다운 그룹을 1메일로) → 대표·쿨다운 모델 붕괴
- CSS/레이아웃 결정 → **운영자 템플릿 몫**
- 중첩 루프·다중 반복 블록·하위표
- 수신자별 개인화

**멀티플리시티 주의:** 한 그룹 안에서 동일 eqp가 여러 rule/fact로 다중 breach될 수 있다. 기본은 **"breach당 1행"**(어떤 지표가 걸렸는지 보임), 이메일 단위 스칼라(`@CurrentValue` 등)는 **worst 멤버** 기준(`_worst`, threshold.py:65-72). eqp당 1행 통합을 원하면 dedup 옵션으로 제공.

---

## 8. 컴포넌트별 변경 지점 (구현 아님 — 범위만)

**RMS (`ResourceMonitorServer`)**
- `RESOURCE_MONITOR_EMAIL_TEMPLATE` Mongo accessor (RMS는 이미 EARS Mongo 연결 보유, `settings.py:43`). lookup = `(app,process,model,code,subcode)` 5-tier `'_'` 폴백(§7.1).
- 신규 `body_renderer` 순수함수: 컨텍스트별 escape(text/url, D6) + 반복블록 expand + 스칼라 치환 + `title` 렌더(`:` 제거, D1) + body 사이즈 가드(D3) + 리터럴 `@HttpWebServerAddress` 무력화(D2). (TDD)
- `build_alert_request` 확장: 렌더 컨텍스트(스칼라 + per-멤버 rows) 구성, 템플릿 미스/렌더 오류 시 **내장 기본 본문 fallback**(D5).
- `engine._dispatch`: **항상** 멤버 breach 리스트 + `AnalysisResult.timestamp`를 `build_alert_request`로 전달(eqp 모드 포함).
- `EmailAlertRequest`/`to_payload`: `renderedBody`·`title` 추가. **`NotifyChannel` 무변경**(옵트인은 전역 `settings.rms_custom_body_enabled`, **기본 on** — 2026-06-14 off→on 전환, D7).
- 데이터 보강: `@Timestamp` 배선(`AnalysisResult.timestamp`→build_alert_request), `@Operator`(=`breach.op`)·`@GroupBy`(=`notify.group_by`)·`@GroupValue`(=`resolve_group_value(...)`) 파생 — 셋 다 build_alert_request의 기존 인자로 산출 가능(신규 파라미터 불필요). `@Metric`은 v1 미포함(@Fact 사용).

**Akka (`HttpWebServer`)**
- `EmailHttpDataFormat`에 `renderedBody: Option[String]`·`title: Option[String]` 추가(additive, 하위호환, json4s 미지필드 무시). (`JsonInterfaces.scala:7`)
- `SendEmail` case: `renderedBody` 존재 시 본문=renderedBody(getEmailBody·데이터 토큰 치환 우회), **`@HttpWebServerAddress`만 치환**(D2); 제목 슬롯은 `title` 사용(D1). 미존재 시 기존 경로.
- RedisActor / `getEmailCategory`(수신자) / EmailingAgent: **무변경.** (제목 합성 슬롯만 `title` 사용하도록 소폭 조정)

**WebManager**
- `features/email-template` 복제 → `RESOURCE_MONITOR_EMAIL_TEMPLATE`용 모델/서비스/컨트롤러/라우트(`/api/rms-email-template`) + `rmsEmailTemplate` 권한.
- `HtmlEditorModal` 재사용 + **토큰 팔레트 / 반복블록 삽입 버튼 / 샘플 데이터 미리보기(단일·그룹 토글) / 미지토큰·블록 lint**.
- 죽은 `{{handlebars}}` 시드 제거, `@토큰`으로 재시드.
- **플레이그라운드 패리티**: **본 기능 비대상** — `RESOURCE_MONITOR_PROFILE`/`NotifyChannel` 필드를 추가하지 않으므로(D7) 메모리 정책 트리거 안 됨. (editor-ui §10 참조)

**데이터/컬렉션**
- 신규 컬렉션만 추가. **EQP_INFO / EMAILINFO / EMAIL_RECIPIENTS / POPUP_TEMPLATE_REPOSITORY 무변경.**

---

## 9. 결정 사항 (확정)

> 이전 "미해결" 6항목을 모두 확정. 각 항목 = **결정 + 근거**. 외부 수치 1건만 "구현 전 검증 액션"으로 분리.

### D1. 제목(title) 처리 — **결정: RMS가 렌더한 `title`을 additive 필드로 전송**
- 신규 컬렉션의 `title`(제목 템플릿)을 RMS가 `@`스칼라 토큰으로 렌더한 뒤, **additive `title: Option[String]`** 필드로 `/EmailNotify`에 함께 전송.
- ⚠️ **renderedBody 모드에서는 RMS가 `title`을 항상 함께 전송**(템플릿 `title`이 비면 RMS **내장 기본 제목 상수** 사용). Akka는 renderedBody 모드에서 `getEmailBody`를 호출하지 않으므로 **레거시 제목 폴백이 없다** — `title`이 곧 단일 소스. renderedBody 미존재(레거시 경로)일 때만 기존 getEmailBody 제목 사용. (D2의 "getEmailBody 건너뜀"과 모순 없음)
- ⚠️ **콜론 불변식 보존**: EmailActor가 `title:body`를 **첫 `:`**로 분리하므로, RMS는 렌더된 제목에서 `:`를 제거/치환한다(대괄호 구조에는 `:` 없음 → split 안전).
- 근거: 복구 경로 선례(`RecoveryEmailHttpDataFormat.title`)와 일관, 운영자 제목 제어 가능.

### D2. Akka의 `renderedBody` 재치환 — **결정: `@HttpWebServerAddress` 1개만 치환, 그 외 전부 미치환**
- renderedBody 모드에서 Akka는 `getEmailBody` 및 데이터 `@`토큰 치환 체인을 **건너뛴다**(RMS가 이미 escape·렌더 완료 → `@`충돌/순서/이중 escape 위험 0, §5-③).
- **예외 단 1개**: `@HttpWebServerAddress`는 치환한다. 운영자가 WebManager 편집기로 삽입한 EmailImage URL(`http://@HttpWebServerAddress/ARS/EmailImage/…`)이 발송 시 실제 주소로 해석되어야 하기 때문(`imageUrl.js`, `EmailWorker.scala:372,593`).
- 보완: RMS 렌더러는 데이터 값에 우연히 들어간 리터럴 `@HttpWebServerAddress` 문자열을 무력화(escape)한다.
- 수신자 해석(`getEmailCategory`, hostname/process/model/code)은 **그대로 수행** — 본문 합성만 우회.

### D3. 본문 사이즈 상한 — **결정: RMS-측 보수적 가드 채택 + 실측은 구현 전 검증**
- 정확한 인프라 한계(Redis pub/sub payload max, ESB `sendMISMail` 본문 cap)는 모노레포 밖이라 **수치 미상** → 설계는 방어 정책으로 확정:
  - ERB 행수 cap: **RMS 전역 설정** `rms_erb_row_limit`(기본 **50행**) + 초과 시 잘라내고 overflow 문구(`@RemainingCount`). *(템플릿별 필드 아님 — 최소 스키마)*
  - 렌더 본문 **전체 바이트 가드**: 전역 설정(보수적 기본 ~256KB) 초과 시 ERB 절단 + overflow 문구 + 경고 로그.
- 🔎 **구현 전 검증 액션(1건)**: 실제 Redis 채널 최대 payload·ESB 본문 cap을 ops/부하테스트로 확인해 위 기본값 보정.

### D4. `SendEmails-` 구독자 위치 — **결정: 해소됨(설계 영향 없음)**
- 조사(영역 5)로 확인: 구독자 = **EmailingAgent** (`RedisActor.scala:92` `psubscribe SendEmails-*` → `EmailActor` → 외부 ESB `sendMISMail`). HTML 발송(`setBHtmlContentCheck(true)`)·verbatim 전달.
- Option C는 EmailingAgent **무변경** → 별도 조치 불필요. (open question 종료)

### D5. 기본 템플릿 & 폴백 — **결정: catch-all 시드 1행 + RMS 내장 기본 본문(이중 안전)**
- 시드: `(app="ARS", process="_", model="_", code="RESOURCE_MONITOR", subcode="_")` 1행에 표준 토큰 + ERB를 쓴 기본 HTML. RMS가 lookup을 소유하므로 와일드카드 catch-all이 실제로 매칭됨(§7.1).
- 폴백: 템플릿 미스/렌더 오류 시 RMS **내장 기본 본문**으로 발송 → "There is no email template"류 실패 0.
- 개별 템플릿 비활성화: 별도 `enabled` 필드 없음(최소 스키마) → **해당 행 삭제**(catch-all/내장 본문으로 폴백) 또는 전역 플래그 `rms_custom_body_enabled` off(전체 비활성)로 처리.

### D6. escape 컨텍스트 — **결정: RMS 렌더러의 컨텍스트별 escape(토큰 manifest로 선언)**
- `text`(기본, 모든 데이터 토큰): `html.escape(quote=True)` → `& < > " '` 변환.
- `url`(`@GrafanaUrl`): RMS가 직접 생성한 URL → scheme(http/https) 검증 + URL 구성요소(eqp_id/process) URL-encode + href 속성 삽입 시 **따옴표만** escape(`"`→`&quot;`), 쿼리 구분자 `&`는 보존(링크 무결, §5-②).
- 토큰별 컨텍스트(`text`/`url`)는 **RMS 렌더러와 클라 편집기가 공유하는 상수 토큰 맵**으로 선언(문서 필드 아님 — `variablesManifest` 폐기). 토큰 집합이 고정이라 클라 상수로 충분.

### D7. 옵트인/롤아웃 — **결정: NotifyChannel 무변경, 전역 settings 플래그로 dark-launch**
- 채널별 `use_custom_body` 플래그는 **불채택**(템플릿 행 존재 여부가 곧 옵트인 → `NotifyChannel` `extra="forbid"` 유지, 스키마 churn 0).
- 안전 롤아웃(초기): 전역 `settings.rms_custom_body_enabled`를 off 배포 → 검증 후 on. off면 RMS가 renderedBody 미전송 → 기존 동작 100% 유지.
  > **갱신(2026-06-14)**: 다크런치 검증 완료로 **기본값을 on으로 전환**했다(Option C가 권장 운영 모드이자 그룹 발송 `email_group` 라우팅의 전제). 이제 off는 명시적 opt-out(`MONITOR_RMS_CUSTOM_BODY_ENABLED=false`)일 때만. 근거: `docs/rms-email-group-routing-decision-2026-06-14.md`.

---

## 10. 부록 — 거부된 대안 요지

- **B안(기존 컬렉션 재사용, Akka 치환, zero 신규컬렉션)**: 가장 적은 신규 표면이지만 — process/model 와일드카드 불가로 **(process,model)쌍마다 템플릿 행 필요**, 단일 템플릿 양모드에 Akka 1줄이 사실상 필수, 스칼라 토큰 escape 누락. 조직이 "신규 컬렉션 금지/최대 일관성"을 강제할 때의 차선책. (3/5)
- **A안(RMS 렌더 → 기존 EMAIL_TEMPLATE 래퍼 `@contents`)**: 래퍼-본문 2곳 관리 + Akka가 본문 위에 토큰 재치환(→ `@contents`를 맨 마지막에 치환해야, 복구 경로 코드와 상충) + 글로벌 escape가 URL 깨뜨림. (3/5)

---

### 근거 파일 인덱스
- Akka: `HttpWebServer/src/main/scala/com/sec/eeg/ars/actor/EmailWorker.scala`, `data/JsonInterfaces.scala`, `endpoint/HttpEndPoint.scala`, `actor/RedisActor.scala`
- EmailingAgent: `EmailingAgent/.../actor/EmailActor.scala`
- WebManager: `server/features/email-template/{model,service,controller,routes}.js`, `shared/utils/createTemplateService.js`, `client/.../email-template/*`, `HtmlEditorModal.vue`, `seedManualData.js`, `docs/SCHEMA.md`
- RMS: `src/analyzer/alert_builder.py`, `src/analyzer/engine.py`, `src/alert/{models,email_client}.py`, `src/db/models.py`, `src/config/settings.py`, `src/analyzer/threshold.py`
