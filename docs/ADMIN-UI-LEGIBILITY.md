# 모니터링 기준정보 관리 UI — 시인성(Legibility) 설계

> **문서 개정 v1.0 (2026-06-05) — as-built 동기화** (문서 개정번호이며 데이터 스키마 v1이 아님; 데이터 스키마는 v2)
>
> 🟢 운영자가 `RESOURCE_MONITOR_PROFILE` 기준정보를 편집할 **관리 UI**의 시인성 설계입니다. 떠받치는 **프로파일 CRUD API는 구현 완료**(`src/api/profiles.py` — overlay CRUD + `effective`(provenance) + measure/rule/notify 항목 엔드포인트, `governance.version` 낙관락). **관리 UI만 후속(미구현)** 입니다. 데이터 스키마는 [SCHEMA.md](../SCHEMA.md) 참고.
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
| **API 형태** | **항목별 REST CRUD** (통째 PUT도 별도 제공) | rule 하나만 원자 수정 — 저장을 안 쪼개도 가능(내부는 단일 문서 통째 replace, [§6](#6-이를-떠받치는-api)) |
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
> ⚠️ "단일 컬렉션이라 상속 가시화가 공짜"는 **과장**이었다 — provenance read-model은 별도 구현이 필요했고, 지금은 `GET /profiles/effective?withProvenance=1`로 **구현 완료**되었다([§7](#7-구현-현황-as-built)).

---

## 5. 시인성 해결책 (구체)

### 5.1 층 (a) — 항목 수 시인성

순수 화면 표현으로 해결. 단일 문서 데이터를 클라이언트에서 재구성한다.

- **리소스별 탭**: `[Measures] [Rules] [Notify]` 로 분리 표시 (저장은 한 문서).
- **정렬 / 텍스트 검색 / 필터**: `category` · `severity` 별 필터.
- **그룹핑·접기**: category별 그룹, measure 기준 그룹("이 measure를 쓰는 rule 묶어보기").
- **역참조 뷰**: "이 measure를 참조하는 rule" / "이 notify를 쓰는 rule" → 삭제 안전성([§5.3](#53-대규모-안전-blast-radius)).
- **rule 폼의 fact 드롭다운**: 로드된 measure+fact(`measureId.type`)로 후보 구성 → 존재하지 않는 참조·오타를 **입력 단계에서 차단**.
- **Phase 2/3 fact 배지(권고)**: `duration`/`delta`/`growth_rate`/`moving_avg`/`trend`/`zscore`/`baseline_dev`는 스키마·검증은 수용하지만 **엔진이 아직 skip+경고**한다(`src/analyzer/fact_catalog.py`의 `IMPLEMENTED_PHASES={1}`). 이런 fact를 쓴 rule에는 "평가 미적용(후속 Phase)" 배지를 붙여 운영자가 저장은 되나 알림은 안 난다는 점을 알게 한다.

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
- **삭제 인터록 (UI 즉답)**: measure 삭제 시 같은 문서 `rules[]`만 스캔해 "이 measure를 `cpu_warn`/`cpu_crit`이 쓰고 있어 삭제 불가" 경고(추가 쿼리 0회). 단일 문서 한정의 빠른 화면 가드.
- **서버 권위 검증 (effective-wide)**: 단, 최종 무결성은 단일 문서 스캔이 아니라 effective 기준이다 — API의 `_validate_composed`(`src/api/profiles.py`)가 `collect_scope_docs`로 상위 scope까지 모은 뒤 `fold_profiles`로 합성하고 `validate_effective`(`src/db/models.py`)로 참조 무결성을 검사해 422를 낸다. 전역 measure 삭제가 하위 overlay의 rule을 런타임에 깨뜨리는지는 이 effective-wide 검증이 잡으며, 화면의 단일 문서 인터록은 그 앞단의 즉답 가드일 뿐이다.

---

## 6. 이를 떠받치는 API

저장은 단일 문서이며, **통째 PUT 외에 항목별 REST CRUD도 얹는다**(rule 하나만 원자 편집). 단, 항목별 저장도 내부적으로는 **단일 문서를 통째 read-modify-write replace** 한다 — 부분 연산자(`$push`/positional `$set`/`$pull`)가 아니라 overlay 문서를 읽어 메모리에서 항목을 추가/수정/삭제한 뒤 통째 교체한다. 운영자에겐 항목 단위로 보이되 저장은 한 문서 replace다. (SCHEMA §13 "repo엔 item CRUD 없음 — 항목 편집은 API 레이어가 read-modify-write replace로 처리"와 일치.)

| 엔드포인트 | 동작 | 구현 |
|-----------|------|------|
| `GET /profiles?process&model&eqpId` | scope 문서 1개 로드 (overlay) | `find_by_scope` |
| `GET /profiles/effective?process&model&eqpId&withProvenance=1` | cascade 합성된 effective profile + 항목별 출처/override 메타 | `collect_scope_docs`(`$or` 4-scope) → `fold_profiles` + `_provenance` 부착 |
| `POST /profiles/measures\|rules` (id는 body) | 항목 추가 | overlay 로드 → 리스트 append → `replace_with_version` (통째 replace) |
| `PATCH/DELETE /profiles/measures\|rules/{id}` | 항목 수정/삭제 | overlay 로드 → 리스트에서 항목 교체/제거 → `replace_with_version` (통째 replace) |
| `PATCH /profiles/notify/{name}` | 채널 수정 | overlay 로드 → `notify[name]` 교체 → `replace_with_version` |

- **동시성**: 모든 쓰기에 `governance.version` 옵티미스틱 락 — `replace_with_version`이 `updateOne({scope, 'governance.version': expected}, {$set: doc})`로 통째 교체하며 새 버전 = `expected+1`을 문서에 박는다(별도 `$inc` 아님). `matchedCount==0`이면 문서 부재(**404**) / 버전 불일치(**409**)를 `find_by_scope` 재조회로 구분한다(`_raise_write_conflict`). 항목 부재(예: 없는 rule patch)는 commit 이전에 **404**로 끊는다.
- **검증**: 저장 전 **합성된 effective profile** 기준으로 [SCHEMA.md §5](../SCHEMA.md) 규칙(`validate_effective`)을 실행 → **422 + 필드 경로**(`rules[3].when[0].fact`)를 반환해 폼 인라인 에러로 매핑. Mongo 다운 시 **503**.
- ⚠️ 항목 쓰기는 "원자적 1명령"이 아니라 **"overlay 로드 → 항목 수정 → base+overlay fold → effective 무결성 재검증 → version-locked replace"** 다단계다(overlay 단독엔 measure가 없을 수 있으므로 `collect_scope_docs`로 상위 scope를 함께 읽어 합성). 단일 문서 통째 replace라 한 overlay 내 동시 편집은 `governance.version` 락이 직렬화한다.

---

## 7. 구현 현황 (as-built)

단일 컬렉션 위 시인성 전제는 **모두 구현 완료**되었고, 남은 것은 **관리 UI 한 가지**다. (3컬렉션은 이 위에 다중 문서 트랜잭션 위험까지 얹었을 것이나 채택하지 않았다.)

| 상태 | 작업 | 근거 |
|------|------|------|
| ✅ | **엔진 per-eqp 해석** (구 dead path) | 구 엔진은 process 레벨만 resolve → override가 알림에 반영 안 됨. 현재는 장비별 resolve + effective_signature 버킷팅으로 **수정 완료**(통합 테스트 E7 회귀 가드). `src/analyzer/engine.py` ([SCHEMA.md §6.5](../SCHEMA.md)) |
| ✅ | `resolve_profile` → cascade fold | `$or` 4-scope 수집(`collect_scope_docs`) → base→specific `fold_profiles` → validate(로그) → effective 캐시. `src/db/repository.py` |
| ✅ | `governance.version` 낙관 락 | `replace_with_version`/`delete_by_scope`가 version-locked, 409/404 구분. `src/db/repository.py` |
| ✅ | 프로파일 CRUD API | overlay GET/POST/PUT/DELETE + 항목 엔드포인트. `src/api/profiles.py` |
| ✅ | **provenance 붙은 effective API** | `GET /profiles/effective?withProvenance=1` — 항목별 출처(scope label) 부착(`_provenance`). 층 (b) 출처 배지·diff의 데이터 공급. `src/api/profiles.py` |
| 🟡 미구현 | 관리 UI (위 §5) | 위 API가 데이터를 공급하므로 착수 가능. **남은 유일한 작업.** |

---

## 8. 안 하는 것 (의도적 배제)

| 항목 | 이유 |
|------|------|
| **컬렉션 3분할** | 시인성 이득 0 + 무결성·합성·원자성 손실. 다중 문서 ACID 위험(단일 호스트 Mongo) |
| **notify 단독 분리** | scope 넘는 재사용 성격이라 분리 명분이 *유일하게* 있는 후보이나, 지금 빼면 항목별 쓰기·effective 합성이 다중 문서가 되어 dangling 위험 부활. **재사용 입증 시 점진 분리**(YAGNI). 그 전엔 "이 채널 쓰는 rule" 역참조 뷰로 충분 |
| **tombstone(상속 삭제) / 4단 fold / 전역검색 read-model** | 추후. 실제 요구 확정 시 도입 (cross-scope 전역검색이 필요해지면 서버 aggregation/검색 인덱스 별도 추가) |

---

## 9. 한 줄 요약

> **저장은 1개 컬렉션, 시인성은 UI(리소스 탭 + 필터 + 출처 배지)로 푼다.** 쪼개려던 직감은 "화면을 나눠 보여주자"로 실현한다. 진짜 시인성 승부처는 cascade 결과(왜 이 값인가)이고, 그건 provenance effective API + 전용 뷰로 푼다 — 그 전제인 엔진 per-eqp 해석·cascade fold·낙관락·프로파일 CRUD API·provenance effective는 모두 **구현 완료**되었고, 남은 것은 이 데이터를 그리는 **관리 UI**뿐이다.

---

## 10. 관련 문서

| 문서 | 내용 |
|------|------|
| [SCHEMA.md](../SCHEMA.md) | 데이터 스키마 (scope/measures/rules/notify, §6 cascade 상속, §5 검증) |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | 설계 배경 |
| [PRD_Phase0_Foundation.md](../PRD_Phase0_Foundation.md) | 원본 요구사항 |
