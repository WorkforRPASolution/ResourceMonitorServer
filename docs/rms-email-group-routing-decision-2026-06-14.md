# [결정] RMS 그룹 경보 메일 수신자 라우팅 — `email_group` 조립 + 그룹 헤드라인 분리

| 항목 | 내용 |
|------|------|
| 문서 종류 | **최종 결정서** (구현 착수 기준) |
| 작성 | RMS 팀 |
| 작성일 | 2026-06-14 |
| 출처 | WebManager RFC `WebManager/docs/plans/2026-06-13-rms-email-direct-category-proposal.md` 를 RMS·WebManager 논의로 수렴·확정한 결과. **본 문서가 단일 진실원천(SoT)** 이며, 위 RFC의 폐기된 대안(representatives 확장, 단일 `email_category` 전체 지정 등)은 인용하지 않는다. |
| 대상 레포 | `ResourceMonitorServer`(Python), `HttpWebServer`(Scala/Akka), `WebManager`(UI) |
| 코드 기준 | RMS `4ba0cde`, HttpWebServer 현행 `main` (인용 file:line은 작성 시점 기준) |

> **결정 한 줄.** 그룹 경보 메일에서 수신자를 정하려고 *대표 장비(eqpId)를 골라 역산*하던 구조를 폐기한다. RMS가 수신자 카테고리를 **`EMAIL-{process}-{model}-{email_group}` 형식으로 직접 조립**해 payload에 실어 보내고(EMAIL_RECIPIENTS 미사용), 제목 헤드라인은 **그룹 식별자(`displayId`)** 로 바꾼다. 운영자는 카테고리 전체가 아니라 **접미사 `email_group` 한 조각만** 설정한다.

---

## 0. 용어

| 용어 | 정의 |
|------|------|
| **eqpId** | 개별 장비 식별자(예: `EQP001`). payload `hostname` 필드에 실림 |
| **group_by** | NotifyChannel의 묶음 단위 — `eqp` / `model` / `process` |
| **group_value** | `group_by`로 정해진 그룹의 실제 값 — `model`→eqpModel, `process`→공정명 (RMS `resolve_group_value()` 산출) |
| **email_group** | **운영자가 NotifyChannel에 설정하는 카테고리 접미사**(팀/타입 토큰). 예: `TEAM1`, `ALERT`. 라우팅에서 운영자가 정하는 *유일한* 값 |
| **emailCategory** | RMS가 조립해 payload로 보내는 **완성된 수신자 카테고리 문자열**. 형식 `EMAIL-{process}-{model}-{email_group}`. EMAILINFO에서 `(project+category)`로 수신자 주소를 찾는 키 |
| **displayId** | 메일 제목 `[...]` 칸에 표시되는 그룹 식별자(= group_value) |
| **rep_eqp** | 그룹 메일 1통의 대표 보고 장비. `breaching_eqps[0]`(최소 eqpId) 결정값. 표시·컨텍스트 전용이며 **수신자 라우팅과 무관** |

---

## 1. 문제 정의

개별 장비 경보는 "그 장비(eqpId)로 보낸다"가 자연스럽다. 그러나 **그룹 단위(`group_by = model`/`process`)** 경보에서 현행 구조는 두 가지를 강제한다.

1. **수신자**를 정하려고 **대표 장비 1대를 골라** 그 장비로 카테고리를 *역산*한다. 보내려는 대상은 "그 그룹 담당 조직"인데, 조직을 직접 지정할 길이 없다.
2. **메일 제목**에 그 대표 장비의 eqpId가 박힌다(`[EQP001]`). 그룹 메일인데 임의의 한 대를 가리켜 "이건 한 대 문제인가?"로 오해를 부른다.

본 결정은 이 두 결합을 끊는다.

---

## 2. 현행 동작 (코드 근거 — 결정의 전제)

### 2.1 와이어 계약에 수신자 카테고리 자리가 없다
```scala
// HttpWebServer  data/JsonInterfaces.scala:14
case class EmailHttpDataFormat(
  hostname: String, ip: String, app: String, process: String, model: String,
  line: String, code: String, subcode: String, variables: Map[String,String],
  renderedBody: Option[String] = None, title: Option[String] = None)
```
```python
# ResourceMonitorServer  src/alert/models.py:50  to_payload — 9필드 + renderedBody/title만 선택 추가
```
수신자는 Akka가 `getEmailCategory()`로 **역산**한다(데이터 조회, `EmailWorker.scala:178-222`):
EMAIL_RECIPIENTS를 `(app=ARS, code)`로 찾은 뒤 `(process, model, line)` **4단계 폴백**(`:188-192`)으로 매칭 행의 `emailCategory` 필드를 **읽는다**(`:195/198/201/204`). 4단계 모두 실패하면 EQP_INFO를 eqpId로 조회해 그 장비의 `emailcategory`로 폴백(`:206-214`). → **이 EQP_INFO 폴백이 "대표 장비 종속"의 근원**이다.

### 2.2 제목 `[hostname]`은 Akka가 하드코딩
```scala
// EmailWorker.scala:574 (renderedBody 분기) / :624 (legacy 분기)
EmailFormat(conv.app, _emailCategory, s"[${project}][${emailtitle}][${conv.hostname}][${codeForRedis}]:${retString}")
```
제목 3번째 칸이 무조건 `conv.hostname`(=대표 eqpId)으로 채워진다.

### 2.3 group_value는 RMS가 이미 산출, 대표는 "걸린 장비 중 최소 eqpId"
```python
# src/analyzer/alert_builder.py:58  resolve_group_value
#   model→eqp_info["eqpModel"], process→process, eqp→breach.eqp_id   (:67-71)

# src/analyzer/engine.py:271-286  _dispatch — group_value별로 메일 1통씩
channel = members[0].channel               # :278  그룹의 채널
group_value = group_value_by_key[key]      # :279
breaching_eqps = sorted({m.breach.eqp_id for m in members})   # :280
rep_eqp = channel.representatives.get(group_value)            # :282  (← 삭제 대상)
if rep_eqp not in breaching_eqps: rep_eqp = breaching_eqps[0] # :283-284  미지정 시 최소 eqpId
```
- `_dispatch`는 **공정 단위로** 돌고(엔진이 process별 분석), 그 안에서 **group_value별로 쿨다운 키가 갈려 메일이 쪼개진다**(`make_cooldown_key(..., group_value=gv)`, `:262`).
- 대표 eqp 핀(`representatives`)은 "이번에 실제로 걸린 장비 중에서만" 유효하고 안 걸린 tick엔 min으로 폴백되어 **원래도 불안정**하다.

### 2.4 Option C(renderedBody) 분기에서 payload `model`은 라우팅 전용
```scala
// EmailWorker.scala:560  if (conv.renderedBody.isDefined)  → 본문/제목은 RMS 렌더값 그대로
// :570  val _emailCategory = getEmailCategory(conv.process, conv.model, conv.hostname, conv.code, conv.line)
```
renderedBody 분기는 `getEmailBody`(DB 템플릿)·`@Model` 치환을 **호출하지 않는다**(그 코드는 legacy `:582+` 분기). 따라서 Option C에서 `conv.model`은 **수신자 라우팅에만** 쓰인다.

RMS는 `rms_custom_body_enabled` ON일 때 renderedBody를 **항상** 싣는다 — 템플릿이 없어도 `DEFAULT_BODY`로 폴백하므로 `rendered_body`는 절대 None이 아니다(`alert_builder.py:132`가 무조건 실행, `_render_custom_body`(`:223-248`)가 `tuple[str,str]` 반환, 누락/에러 모두 `DEFAULT_BODY`). → 플래그 ON이면 Akka는 **항상** Option C 분기를 탄다.

---

## 3. 결정 — "세 갈래 분리"

대표 eqpId 한 개가 떠안던 역할을 셋으로 나눈다.

| 역할 | 현행 | **결정** |
|------|------|---------|
| **수신자 결정** | 대표 eqpId → 역산(EMAIL_RECIPIENTS/EQP_INFO) | **RMS가 `EMAIL-{process}-{model}-{email_group}` 조립** → payload `emailCategory`로 직접 전송 (Akka는 그대로 사용, EMAIL_RECIPIENTS 미사용) |
| **제목 헤드라인** | 대표 eqpId `[EQP001]` | **그룹 식별자** `displayId`(= group_value) → `[MODEL-A]` / `[PHOTO]` |
| **표시/컨텍스트**(hostname/스냅샷/그래프/본문) | 대표 eqpId(representatives 또는 min) | **`breaching_eqps[0]` 결정값** — 운영자 저장 핀 없음 |

### 3.1 카테고리 조립 규칙 (핵심)
RMS가 그룹별로 다음을 조립한다:
```
emailCategory = "EMAIL-" + process + "-" + model_token + "-" + email_group
```
| 부분 | 값 |
|------|-----|
| `process` | 발송 공정(엔진이 공정 단위로 돌므로 항상 구체값) |
| `model_token` | `group_by == "process"` → **`"ALL"`**(대문자) / 그 외(`model`·`eqp`) → **rep_eqp의 eqpModel** |
| `email_group` | NotifyChannel의 `email_group`(운영자 설정) |

> **왜 이게 splitting을 해결하나**: `email_group`은 그룹 불변(팀/타입)이고 `process`·`model_token`은 그룹별 자동 산출이다. `*/*/*`+`model`에서 단일 `email_group="TEAM1"` 하나로도 ModelA→`EMAIL-P-ModelA-TEAM1`, ModelB→`EMAIL-P-ModelB-TEAM1`처럼 **그룹마다 다른 카테고리**가 나온다. → `email_group`은 **어느 scope에 둬도 안전**하다(앞선 "단일 카테고리 전체 지정"의 scope↔group_by 제약이 사라진다).

> **왜 `model_token="ALL"`인가(process 묶음)**: 공정 묶음은 모델 혼재라 단일 모델이 없다. EMAILINFO의 공정 전체 카테고리(`EMAIL-{process}-ALL-{email_group}`)로 보낸다. `(a)` 방식은 Akka가 `conv.emailCategory`를 직접 쓰므로 **payload `model`은 rep 실모델 그대로 두고** `ALL`은 조립 문자열의 model 칸에만 들어간다.

### 3.2 전제조건 / 플래그
- **`rms_custom_body_enabled` 기본값 `True`** (env 미설정 시 ON). (a) 방식은 `payload.model`을 건드리지 않고(`ALL`은 조립 문자열에만), Akka가 `conv.emailCategory`를 직접 쓰므로 **수신자 라우팅은 플래그 ON/OFF와 무관하게 정상**이다. 플래그는 **본문 품질에만** 영향(ON=RMS 렌더 본문이 영향 장비를 표로 나열 / OFF=Akka legacy 템플릿).
- 따라서 하드 가드는 두지 않는다. 다만 `email_group`이 설정됐는데 플래그가 명시적으로 OFF면 엔진이 1회 `email_group_without_custom_body` warning 로그를 남긴다(본문 부실 안내).
- (정정 이력: 초기 안에서 "OFF 미지원"이라 적었으나 그건 폐기된 B 방식(payload.model="ALL")의 제약이었다. (a)에선 위와 같다.)

---

## 4. 변경 ① HttpWebServer (Scala/Akka) — "조립은 RMS, Akka는 그대로 사용"

### 4-1. `EmailHttpDataFormat`에 선택 필드 2개 추가
```scala
// data/JsonInterfaces.scala:14 — 변경 후
case class EmailHttpDataFormat(
  hostname: String, ip: String, app: String, process: String, model: String,
  line: String, code: String, subcode: String, variables: Map[String,String],
  renderedBody: Option[String] = None, title: Option[String] = None,
  emailCategory: Option[String] = None,   // [신규] 있으면 수신자 라우팅에 그대로 사용
  displayId:     Option[String] = None)   // [신규] 있으면 제목 헤드라인에 hostname 대신 사용
```
`Option[String] = None` 기본값이라, 이 필드를 안 보내는 모든 기존 발신자는 json4s에서 `None`이 되어 기존 경로를 그대로 탄다(renderedBody/title 도입 때 검증된 하위호환 패턴).

### 4-2. 호출부 분기 — `emailCategory`가 있으면 역산 생략
```scala
// EmailWorker.scala:570 (renderedBody 분기) — 변경 후
val _emailCategory = conv.emailCategory.filter(_.nonEmpty).getOrElse(
  getEmailCategory(conv.process, conv.model, conv.hostname, conv.code, conv.line))
```
- `emailCategory` 비어있지 않으면 → **그대로 사용**, EMAIL_RECIPIENTS 4단계·EQP_INFO 폴백 **모두 건너뜀**.
- 비어있으면(None) → 기존 `getEmailCategory()` 역산(다른 발신자·미설정 채널 하위호환).
- legacy 분기(`:619`)에도 동일 한 줄을 넣어 일관성을 두되, **RMS는 전제조건상 이 분기를 타지 않는다**.

### 4-3. 제목 헤드라인 — `displayId`가 있으면 hostname 대신
```scala
// EmailWorker.scala:574 (renderedBody 분기) — 변경 후
val headline = conv.displayId.filter(_.nonEmpty).getOrElse(conv.hostname)
EmailFormat(conv.app, _emailCategory, s"[${project}][${emailtitle}][${headline}][${codeForRedis}]:${retString}")
```
본문 토큰(`@Hostname`/`@Sdwt`/스냅샷)은 건드리지 않는다(여전히 `conv.hostname`). 제목 헤드라인만 분리한다.

### 4-4. 0명 발송 가드
조립 카테고리가 EMAILINFO에 없으면 수신자 0명이 된다. 현행 가드 `"There is no email category"`(`:571/:621` if → `:579/:629`)는 **빈 문자열만** 잡으므로, "비어있지 않으나 EMAILINFO 미존재"는 못 잡는다. → **카테고리→주소 해석부(EMAILINFO 조회 다운스트림)에서 수신자 0명이면 명시 로그**를 남긴다(2차 방어). 1차 방어는 WebManager UI 실존 검증(§6).

---

## 5. 변경 ② RMS (ResourceMonitorServer, Python)

### 5-1. NotifyChannel — `representatives` 삭제, `email_group` 추가
```python
# src/db/models.py  NotifyChannel  (model_config extra="forbid", :342)
#   representatives: dict[str, str]   ← 삭제 (group_value → eqpId)
email_group: str | None = None
#   ↑ 신규: 이 채널의 카테고리 접미사(팀/타입). None이면 조립 안 함 → derivation 폴백.
#     '-' 금지(구분자). 어느 scope/group_by에 둬도 안전(process/model은 그룹별 자동 산출).
```

### 5-2. EmailAlertRequest — payload 필드 2개 추가
```python
# src/alert/models.py  EmailAlertRequest
email_category: str | None = Field(default=None, alias="emailCategory")
display_id:     str | None = Field(default=None, alias="displayId")
# to_payload(): 값이 있을 때만 포함 (renderedBody/title과 동일 패턴)
if self.email_category is not None: payload["emailCategory"] = self.email_category
if self.display_id   is not None: payload["displayId"]     = self.display_id
```

### 5-3. 엔진 — 대표 단순화 + 조립 + displayId
```python
# src/analyzer/engine.py  _dispatch  (그룹 발송 조립부)
rep_eqp = breaching_eqps[0]                       # 대표 컨텍스트(결정적, 저장 핀 없음)

# 카테고리 조립 (email_group 설정 시에만; 미설정이면 None → Akka derivation 폴백)
model_token = "ALL" if channel.group_by == "process" else eqp_lookup[rep_eqp].get("eqpModel", "")
email_category = (
    f"EMAIL-{process}-{model_token}-{channel.email_group}"
    if channel.email_group else None
)
display_id = group_value if channel.group_by != "eqp" else None
# → build_alert_request 결과 alert 에 email_category / display_id 주입
```
- `group_by == "eqp"` + `email_group` 미설정 → 둘 다 None → payload 미포함 → **현행과 동일**.
- payload `model`은 **rep 실모델 그대로**(조립의 `ALL`은 문자열에만, payload 미변경).

### 5-4. 와이어 계약 픽스처
`tests/data/akka_email_contract.json` 에 **`emailCategory`(조립값, 예 `EMAIL-PHOTO-ALL-TEAM1`) + `displayId`(예 `PHOTO`)를 포함하고 eqpId 핀은 없는** `grouped` 케이스 추가. RMS `tests/unit/test_akka_contract.py` + Akka `EmailHttpDataFormatSpec` 양쪽 검증.

---

## 6. 변경 ③ WebManager (UI)

> 합성(fold)·검증 로직은 representatives를 건드리지 않으므로(whole-object 상속) **변경 범위는 UI + 픽스처 + 테스트 + playground**에 한정된다.

| 영역 | 작업 |
|------|------|
| `NotifyForm.vue` | representatives(묶음값→대표 eqpId) 편집 블록 **제거** → **`email_group` 단일 입력(optional)** 으로 교체 |
| 후보/검증 | EMAILINFO category를 scope의 process/model로 필터해 **존재하는 `email_group`(4번째 토큰) 후보를 제시**, 저장 시 **조립 결과 `EMAIL-{process}-{model}-{email_group}`(공정 묶음은 `-ALL-`)가 EMAILINFO에 실재하는지 검증·차단**(§4-4 1차 방어). 자유 조립 금지 |
| group_by↔scope 가드 | `group_by`의 묶음 단위는 scope 범위보다 넓으면 안 됨(`model`→`(P,M,*)` 이상, `process`→`(P,*,*)` 이상). 코드 미검증이므로 **UI가 원천 차단** |
| 픽스처/playground/Help | `seed_default_profile.json`·`fold_golden.json`·`rmsPlaygroundPresets.js`·Help 매뉴얼의 representatives 표현 → `email_group`으로 정리(RMS 정리 단계와 동기) |

---

## 7. 하위호환

| 발신 주체 / 시나리오 | 신규 필드 | 동작 |
|----------------------|-----------|------|
| 기존 Akka 내부 발신자 | 미전송 → `None` | 기존 경로 그대로 |
| RMS 개별 경보(`group_by=eqp`, `email_group` 미설정) | 둘 다 `None` | 현행과 동일(역산 + `[hostname]` 제목) |
| RMS 그룹 경보, `email_group` 설정 | 둘 다 채움 | **조립 카테고리 직접 라우팅 + `[group_value]` 제목** |
| RMS 그룹 경보, `email_group` 미설정 | `displayId`만 채움 | 제목만 그룹 식별자, 수신자는 기존 역산(이행 안전망) |

- **계약 깨짐 없음**: 두 payload 필드 Optional·기본 None. json4s `extract`는 누락을 `None` 처리(검증된 패턴).
- `email_group` 미설정 채널은 기존 역산으로 폴백되어 **이행기 안전**. 목표 운영 상태는 RMS 채널에 `email_group`을 설정해 **EMAIL_RECIPIENTS 의존을 제거**하는 것.

---

## 8. 데이터 전제 (운영)

조립 카테고리가 **EMAILINFO에 실재**해야 한다:
- `model`/`eqp` 묶음 → `EMAIL-{process}-{eqpModel}-{email_group}`
- `process` 묶음 → `EMAIL-{process}-ALL-{email_group}` (**대문자 `ALL`**)

토큰은 **글자 단위로 일치**해야 한다 — RMS `process`명 == EMAILINFO 토큰, `eqpModel` == 토큰(**대소문자 포함**). 없는 카테고리를 조립하면 수신자 0명(§4-4 가드로 탐지).

---

## 9. 정직한 트레이드오프

- **opaque 불변식 역전(의도된 선택)**: 기존에는 RMS/Akka가 카테고리를 *읽기만* 했으나, 본 결정은 RMS가 *조립*한다. 대가로 **EQP_INFO(대표 장비) 무음 폴백이 제거**된다 — 실패 양상이 "엉뚱한 수신자(무음)"에서 **"수신자 0명(탐지 가능)"** 으로 바뀐다(개선).
- **의존성 축소**: RMS 경로에서 `EMAIL_RECIPIENTS`(라우팅 테이블)가 빠지고, 불가피한 `EMAILINFO`(주소 카탈로그)만 남는다(2개→1개).
- **토큰/구분자 결합**: 조립이므로 토큰 정확 일치·`email_group`에 `-` 금지가 필요(§8). 관습이 바뀌면 RMS도 영향 — 단 EMAILINFO 자체가 이미 `EMAIL-{process}-{model}-{type}` 관습을 따른다(WebManager `parseCategoryParts`가 의존).
- **잃는 것**: 그룹 메일 "대표 표시 장비"를 운영자가 특정 장비로 고정하는 능력 → `breaching_eqps[0]`로 자동. 원래도 불안정했고 Option C가 영향 장비 전체를 본문에 나열하므로 영향 미미. *(운영팀 확인 포인트: 대표 표시 장비 고정의 실제 요구가 있는가? 없으면 확정.)*

---

## 10. 작업 분담

| 주체 | 작업 |
|------|------|
| **HttpWebServer** | §4 — `EmailHttpDataFormat` 필드 2개, `:570` emailCategory 우회, `:574` displayId 헤드라인(legacy `:619/:624` 동일), 다운스트림 0명 로그 |
| **RMS** | §5 — `representatives` 삭제 + `email_group` 필드, payload 2필드, 엔진 조립(`rep=min`, `model_token` 규칙, displayId), 계약 픽스처, SCHEMA/문서 갱신 |
| **WebManager** | §6 — representatives UI 제거 + `email_group` 입력·EMAILINFO 검증·group_by↔scope 가드, 픽스처/playground/Help 정리 |
| **운영(데이터)** | §8 — EMAILINFO에 조립 카테고리(`-ALL-` 포함) 실재 보장 |

---

## 11. 배포 순서

1. **Akka**: `emailCategory`/`displayId` Optional 수용 + `:570` 우회 + `:574` 헤드라인 + 0명 로그. (RMS가 안 보내면 무영향)
2. **EMAILINFO 데이터**: 조립 카테고리(`EMAIL-{process}-{model}-{email_group}`, 공정은 `-ALL-`) 준비.
3. **RMS**: `email_group` 필드 + 조립 + displayId + `rep=breaching_eqps[0]`. *(`representatives` 필드 제거는 `extra="forbid"`라 seed/`fold_golden`·SCHEMA·테스트 정리와 **동시**에 — 잔존 문서가 로드 실패하지 않도록 조율.)*
4. **WebManager UI**: RMS 계약 배포 후 착수.

---

## 부록. 핵심 코드 인용 (검증 완료)

| 인용 | 파일:라인 |
|------|-----------|
| 발송 URL `/EmailNotify` 단일 (RTM 경로 무관, 범위 밖) | `src/config/settings.py:68`, `src/alert/email_client.py:47` |
| 와이어 계약 case class | `HttpWebServer .../data/JsonInterfaces.scala:14` |
| getEmailCategory 4단계 + EQP_INFO 폴백 | `EmailWorker.scala:178-222` (`:188-192`, `:206-214`) |
| 제목 `[hostname]` 하드코딩 | `EmailWorker.scala:574`(renderedBody) / `:624`(legacy) |
| 빈문자열 가드 | `EmailWorker.scala:571/579`(renderedBody) / `:621/629`(legacy) |
| Option C 분기·model 라우팅 전용 | `EmailWorker.scala:560-577` |
| renderedBody 항상 존재(플래그 ON) | `alert_builder.py:132`, `_render_custom_body:223-248` |
| resolve_group_value | `alert_builder.py:58` (`:67-71`) |
| `_dispatch` group_value별 분리·대표 선정 | `engine.py:262, 271, 278-286` |
| to_payload 9필드 | `src/alert/models.py:50` |
| NotifyChannel(`extra=forbid`/`group_by`/`representatives`) | `src/db/models.py:342, 347, 348` |
