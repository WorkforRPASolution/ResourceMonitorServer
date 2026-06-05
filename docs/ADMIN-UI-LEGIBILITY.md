# 모니터링 기준정보 관리 UI — 시인성(Legibility) 설계

> **버전: v1.0 (2026-06-05) — 설계 문서 (미구현)**
>
> 🟡 운영자가 `RESOURCE_MONITOR_PROFILE` 기준정보를 편집할 **관리 UI/API**의 설계 방향입니다. 아직 admin CRUD API·UI 모두 미구현. 데이터 스키마는 [SCHEMA.md](../SCHEMA.md) 참고.
>
> 근거: UI 엔지니어 / UX 디자이너 / 백엔드 아키텍트 / 정보구조(IA) 전문가 4인 패널 토의(각 적대 검증 통과) **만장일치** 결론.

---

## 1. 풀어야 할 문제

scope당 단일 문서 안에 `measures[]`·`rules[]`가 쌓이면(예: 전역 프로파일 measure ~15, rule ~18) **시인성(legibility)이 떨어진다**는 우려.

제기된 질문: **컬렉션을 `measure`/`rule`/`notify` 3개로 쪼개면 시인성이 좋아지는가?**

---

## 2. 결론

> ### 저장은 단일 컬렉션을 유지한다. 쪼개지 않는다.
> ### 시인성은 **저장 구조가 아니라 UI 표현**으로 푼다.

컬렉션을 3개로 쪼개도:
- 운영자 눈에 보이는 **항목 수는 동일**하다(rule 18개는 그대로 18개).
- "이 장비가 무엇을 **잰다 → 판단한다 → 알린다**"의 전체 그림이 **3개 화면으로 흩어져** 오히려 시인성이 악화된다.
- 참조 무결성(rule→measure)·상속 합성(cascade)·원자성이 cross-collection으로 흩어져 **만들기 어렵고 덜 안전**해진다.

→ "쪼개면 깔끔할 것"이라는 직관은 **세 개의 다른 축을 하나로 묶어 본** 범주 오류다([§3](#3-핵심-프레임--3축-분리)).

---

## 3. 핵심 프레임 — 3축 분리

저장 / API / 화면은 **독립적으로** 선택할 수 있다. 시인성은 이 중 "화면" 축의 문제다.

| 축 | 결정 | 이유 |
|----|------|------|
| **저장 구조** | **단일 컬렉션** `RESOURCE_MONITOR_PROFILE` | 참조 무결성·cascade 합성·원자적 scope 저장 |
| **API 형태** | **항목별 REST CRUD** (통째 PUT 아님) | rule 하나만 원자 수정 — 저장을 안 쪼개도 가능 |
| **화면(UI)** | **리소스별 탭 + 필터/검색/그룹 + 출처 배지** | ← **시인성은 여기서 해결** |

즉 **"저장은 안 쪼개되, API와 화면은 measure/rule/notify로 나눠 보여준다."** 3컬렉션 분리가 줄 편집 편의(항목 단위 CRUD)는 저장을 안 쪼개도 그대로 얻는다([§6](#6-이를-떠받치는-api)).

---

## 4. 시인성은 두 층이다

| 층 | 무엇 | 푸는 방법 | 컬렉션 분리 효과 |
|----|------|----------|-----------------|
| **(a) 항목 수** | rule 18개를 어떻게 한눈에 보나 | 리스트 + 필터/검색 + 그룹/접기 ([§5.1](#51-층-a--항목-수-시인성)) | **0 (무효)**, 오히려 악화 |
| **(b) cascade 결과** | "왜 이 장비가 이 값인가" (전역 상속? 이 scope 재정의?) | provenance 배지 + effective 합성 뷰 + blast-radius ([§5.2](#52-층-b--cascade-상속-시인성)) | **0** (별도 read-model 필요) |

> **(b)가 2만대 규모의 진짜 시인성 승부처다.** 이건 저장 분리로도, 단순 UI 필터로도 안 풀리고 **"출처(provenance)가 붙은 effective profile"** 이라는 별도 기능으로 푼다. 단일 컬렉션이면 `$or` 한 방 fold로 이걸 만들기가 쉽다(3컬렉션이면 cross-collection join).
>
> ⚠️ "단일 컬렉션이라 상속 가시화가 공짜"는 **과장**이다 — provenance read-model은 반드시 **새로 만들어야** 한다([§7](#7-만들어야-할-것-공짜-아님)).

---

## 5. 시인성 해결책 (구체)

### 5.1 층 (a) — 항목 수 시인성

순수 화면 표현으로 해결. 단일 문서 데이터를 클라이언트에서 재구성한다.

- **리소스별 탭**: `[Measures] [Rules] [Notify]` 로 분리 표시 (저장은 한 문서).
- **정렬 / 텍스트 검색 / 필터**: `category` · `severity` 별 필터.
- **그룹핑·접기**: category별 그룹, measure 기준 그룹("이 measure를 쓰는 rule 묶어보기").
- **역참조 뷰**: "이 measure를 참조하는 rule" / "이 notify를 쓰는 rule" → 삭제 안전성([§5.3](#53-대규모-안전-blast-radius)).
- **rule 폼의 fact 드롭다운**: 로드된 measure+fact(`measureId.type`)로 후보 구성 → 존재하지 않는 참조·오타를 **입력 단계에서 차단**.

### 5.2 층 (b) — cascade 상속 시인성

scope 계층 상속([SCHEMA.md §6](../SCHEMA.md))의 결과를 운영자가 이해하게 만드는 것. **시인성의 핵심.**

- **3색 출처 배지** (Zabbix inherited 패턴):
  - `상속됨(inherited)` — 더 넓은 scope에서 옴. **회색·읽기전용**.
  - `재정의(overridden)` — 이 scope에서 덮음. **강조**.
  - `로컬(local)` — 이 scope에만 있음.
- **provenance를 필터/그룹 차원으로**: "상속만 보기 / 이 scope에서 바꾼 것만 보기".
- **overlay vs effective 분리 표시**: "이 문서에 적은 것(overlay)"과 "실제 적용값(effective, 합성 결과)"을 **항상 구분**해 보여준다. **overlay만 보여주는 화면 금지**(빈 overlay를 전체 설정으로 착각 방지).
- **명시적 재정의(pin) 액션**: 상속 항목은 회색 읽기전용. **"이 scope에서 재정의"** 버튼을 눌러야 sparse overlay로 편집 가능 → "통째 교체" 의미([SCHEMA.md §6.2](../SCHEMA.md))를 화면에서 명확히, **전역 통째 복사([SCHEMA.md P9](../SCHEMA.md)) 구조적 차단**.
- **drift lint**: "재정의됐는데 전역 원본과 값이 동일(불필요한 pin)" 항목을 표시하고 **"상속으로 되돌리기"**(해당 overlay 항목 `$pull`) 버튼 제공 → 드리프트 정리.

### 5.3 대규모 안전 (blast-radius)

2만대에서 가장 위험한 과업은 단일 장비 조회가 아니라 **전역 변경의 파급**이다.

- **blast-radius 미리보기**: "전역 X를 바꾸면 영향받는 장비 수 + override로 빠지는 장비 목록".
- **삭제 인터록**: measure 삭제 시 같은 문서 `rules[]`를 스캔해 "이 measure를 `cpu_warn`/`cpu_crit`이 쓰고 있어 삭제 불가" 경고(추가 쿼리 0회).
- **전역 measure 삭제 영향 분석**: 하위 overlay의 rule이 런타임에 깨지지 않도록 effective 기준 사용처 분석 + 주기적 effective-wide 무결성 스캔(깨진 참조 리포트).

---

## 6. 이를 떠받치는 API

저장은 단일 문서지만, **통째 PUT이 아니라 항목별 REST CRUD**를 얹는다 (Mongo가 단일 문서 항목별 원자 수정을 지원).

| 엔드포인트 | 동작 | 구현 |
|-----------|------|------|
| `GET /profiles/{scope}` | scope 문서 1개 로드 (overlay) | `find_one` |
| `GET /profiles/effective?process&model&eqpId&withProvenance=1` | cascade 합성된 effective profile + 항목별 출처/override 메타 | `$or` 4-scope fold + provenance 부착 |
| `POST/PATCH/DELETE /profiles/{scope}/measures\|rules/{id}` | 항목 추가/수정/삭제 | `$push` / positional `$set:'rules.$'` / `$pull` (중첩 `facts[]`만 `arrayFilters`) |
| `PATCH /profiles/{scope}/notify/{name}` | 채널 수정 | 맵 점경로 `$set` |

- **동시성**: 모든 쓰기에 `governance.version` 옵티미스틱 락 — `updateOne({scope, 'governance.version': v}, {$set, $inc:{'governance.version':1}})`, `matchedCount==0`이면 **409**. 항목 부재는 **404**로 구분(2단 식별).
- **검증**: 저장 시 **합성된 effective profile** 기준으로 [SCHEMA.md §5](../SCHEMA.md) 규칙 실행 → **422 + 필드 경로**(`rules[3].when[0].fact`)를 반환해 폼 인라인 에러로 매핑.
- ⚠️ 항목 쓰기는 "원자적 1명령"이 아니라 **"쓰기 → base+overlay fold → 참조 무결성 재검증"** 다단계다(overlay 단독엔 measure가 없을 수 있으므로 상위 scope를 함께 읽어 합성). 전역 measure 변경 중 하위 overlay rule 저장 같은 cross-scope 진부화는 상위 scope `governance.version`까지 확인해 방지.

---

## 7. 만들어야 할 것 (공짜 아님)

단일 컬렉션을 택해도 아래는 **신규 구현**이다. (3컬렉션은 이 위에 다중 문서 트랜잭션 위험까지 얹는다.)

| 우선 | 작업 | 비고 |
|------|------|------|
| 🔴 **#0** | **엔진 per-eqp 해석** (dead path 수정) | 지금은 process 레벨만 resolve → override가 알림에 **반영 안 됨**. 화면은 override를 보여주는데 알림은 무시 → 신뢰 붕괴. **UI보다 먼저.** ([SCHEMA.md §6.5](../SCHEMA.md)) |
| 1 | `resolve_profile` → cascade fold | 첫 매치 replace → `$or` 수집·base→specific fold |
| 1 | `governance.version` 낙관 락 | 현재 모델에 version 필드 없음 |
| 1 | 프로파일 CRUD API | 현재 admin에 0개 |
| 1 | **provenance 붙은 effective API** | 층 (b) 시인성의 전제 — 없으면 출처 배지·diff 못 그림 |
| 2 | 관리 UI (위 §5) | 1순위 API가 데이터 공급 |

---

## 8. 안 하는 것 (의도적 배제)

| 항목 | 이유 |
|------|------|
| **컬렉션 3분할** | 시인성 이득 0 + 무결성·합성·원자성 손실. 다중 문서 ACID 위험(단일 호스트 Mongo) |
| **notify 단독 분리** | scope 넘는 재사용 성격이라 분리 명분이 *유일하게* 있는 후보이나, 지금 빼면 항목별 쓰기·effective 합성이 다중 문서가 되어 dangling 위험 부활. **재사용 입증 시 점진 분리**(YAGNI). 그 전엔 "이 채널 쓰는 rule" 역참조 뷰로 충분 |
| **tombstone(상속 삭제) / 4단 fold / 전역검색 read-model** | 추후. 실제 요구 확정 시 도입 (cross-scope 전역검색이 필요해지면 서버 aggregation/검색 인덱스 별도 추가) |

---

## 9. 한 줄 요약

> **저장은 1개 컬렉션, 시인성은 UI(리소스 탭 + 필터 + 출처 배지)로 푼다.** 쪼개려던 직감은 "화면을 나눠 보여주자"로 실현한다. 진짜 시인성 승부처는 cascade 결과(왜 이 값인가)이고, 그건 provenance effective API + 전용 뷰로 푼다 — 단, 그 전에 엔진 per-eqp dead path(#0)부터 고쳐야 한다.

---

## 10. 관련 문서

| 문서 | 내용 |
|------|------|
| [SCHEMA.md](../SCHEMA.md) | 데이터 스키마 (scope/measures/rules/notify, §6 cascade 상속, §5 검증) |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | 설계 배경 |
| [PRD_Phase0_Foundation.md](../PRD_Phase0_Foundation.md) | 원본 요구사항 |
