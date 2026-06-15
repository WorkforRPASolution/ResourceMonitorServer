# 결정: 프로파일 `enabled`를 "규칙(rule) 단위" 판정으로 (v3)

> **상태**: 결정·구현 완료 (RMS, 2026-06-15). 이 문서가 `enabled` 의미론의 **단일 진실 소스(SoT)**.
> **supersedes**: v2 doc-레벨 last-wins (커밋 `ee861c2`, 2026-06-12). WebManager 핸드오프 스크래치 문서 `rms-enabled-lastwins-webmanager-ui-handoff-2026-06-12.md`(커밋 안 함)도 무효.
> **권위 스펙 반영**: [SCHEMA.md §6.2](../SCHEMA.md), [ARCHITECTURE.md §2.2](../ARCHITECTURE.md).

---

## 1. 배경 (사고)

production에서 운영자가 전역 `(*,*,*)` 문서를 `enabled:false`로 두고 `TESTPROCESS/*/*`만 켜서
**mem 규칙 하나만** 테스트하려 했는데, 설정한 적 없는 `RESOURCE_MONITOR-DISK_CRITICAL`
메일이 10분 간격으로 발생했다.

원인은 직전(2026-06-12, 커밋 `ee861c2`)에 도입한 **doc 레벨 last-wins(v2)** 의미론이었다:
- `fold_profiles`가 `enabled`를 "가장 구체적 scope의 한 비트"로 두고, 여러 scope에서
  **누적된 전체 규칙**을 그 한 비트로 일괄 게이트했다.
- 전역(off)의 `disk_full`(rule.enabled=true) 규칙이 `TESTPROCESS`(on)로 **상속**되고,
  folded `enabled`가 TESTPROCESS의 `true`로 덮여 **disk_full이 살아남아 발송**됐다.
- `get_scheduling_intervals`는 doc-level `enabled`를 의도적으로 무시해 비활성 문서의
  interval(여기선 disk 규칙의 주기)까지 스케줄했다.

즉 v2 last-wins의 약점은 **"규칙은 여러 scope에서 누적되는데 `enabled`는 한 scope에서만
가져온다"는 불일치**였다.

## 2. 결정 (v3)

`enabled`를 **규칙(rule) 단위**로 판정한다.

> 규칙 R의 동작 = **(R을 가진 *가장 구체적인* scope 문서의 `scope.enabled`) AND (그 문서에서의 `rule.enabled`)**
> - 상속/override는 **`rule.id` 정확 일치**로만 판단(같은 id → whole-object 교체=가장 구체 scope 승자, 다른 id → 둘 다 누적).
> - `scope.enabled=false`는 **그 문서가 직접 선언한 규칙만** 끈다. 상속 규칙을 끄려면 같은 id로 `rule.enabled:false` 재선언(soft tombstone).
> - folded `profile.enabled` = "활성 규칙이 하나라도 있는가"(엔진의 값싼 per-equipment skip용).
> - `measures`/`notify`는 `enabled`로 게이팅하지 않는다(활성 규칙이 비활성 조상의 measure를 참조할 수 있으므로 항상 cascade).

이는 last-wins(v2)를 rule 단위로 정교화한 것이며, "가장 구체적인 scope가 이긴다"는 overlay
철학을 규칙 단위로 일관 적용한다.

## 3. 진리표

G=`*/*/*`, P=구체 overlay(예 `TESTPROCESS/*/*`), 장비는 P 소속, 규칙 R:

| # | G.scope / G의 R(rule_en) | P.scope / P의 R(rule_en) | 승자 | 유효 | 의미 |
|---|---|---|---|---|---|
| a | off / 있음(true) | on / 없음 | G | **off** | 사고(disk_full) 해결 — 광역 off의 규칙은 상속돼도 꺼짐 |
| b | off / 있음(true) | on / 있음(true) | P | **on** | 켜진 overlay가 같은 id로 재선언 |
| c | on / 있음(true) | off / 없음 | G | **on** | P가 그 규칙 미선언 → G 따름 |
| d | on / 있음(true) | off / 있음(false) | P | **off** | tombstone |
| e | on / 있음(false) | on / 없음 | G | **off** | rule_en 자체 off |

**사고 설정 결과**: `*/*/*`(off)에만 있는 `disk_full` 등은 케이스 a → OFF. `TESTPROCESS`가 같은
id로 가진 규칙만 ON. DISK_CRITICAL 소멸.

## 4. 구현 (RMS)

- `src/db/models.py` `fold_profiles`: 규칙 누적 시 `rules[r.id] = r.model_copy(update={"enabled": prof.enabled and r.enabled})`(승자 scope.enabled를 baked). 반환 `profile.enabled = any(r.enabled for r in rules.values())`. measures/notify는 무게이팅.
- `src/db/repository.py` `get_scheduling_intervals`: Mongo 쿼리에 `"enabled": {"$ne": False}` 추가(비활성 문서 제외; 레거시 enabled 부재 문서는 포함). override는 별도 enabled 문서에서 interval이 잡혀 누락 없음.
- `src/analyzer/engine.py`: **무변경**. `:90` `if not profile.enabled`는 이제 "활성 규칙 0개면 skip", `:114` `r.enabled` 필터는 baked 값으로 동작.
- **DB 마이그레이션 없음** — 스키마 동일, fold 해석만 변경.

테스트: 단위 fold 케이스 a~e + any-active + measure 유지, 스케줄 제외, E2E 사고 가드
(`test_inherited_rule_from_disabled_global_does_not_fire`).

## 5. 운영 가이드

- **광역으로 끄기**: 전역 문서를 `enabled:false`로 두면 그 문서가 직접 가진 규칙은 전부 꺼진다.
  단, 더 구체적인 enabled 문서가 같은 id로 재선언한 규칙은 그 문서가 지배하므로 계속 동작한다.
- **특정 scope만 통째로 끄기**: 그 scope `enabled:false`만으로는 **상속 규칙이 안 꺼진다**.
  끄려는 상속 규칙을 같은 `rule.id`로 `rule.enabled:false` 재선언(soft tombstone)해야 한다.
- **테스트 격리(이번 사고 패턴)**: 전역을 끄고 특정 process만 테스트하려면, 그 process 문서에
  **테스트할 규칙을 직접 선언**하면 된다 — 전역의 다른 규칙은 상속돼도 꺼진 채 남는다.

## 6. 교차 레포 영향 (WebManager — 별도 세션)

- `GET /profiles/effective`의 folded `enabled` 의미가 "가장 구체적 doc의 비트"에서
  **"활성 규칙 존재 여부"**로 바뀐다. 비활성 배너/판정 로직 재검토 필요.
- WebManager의 JS fold 미러(`utils/foldProfiles.js`)와 골든은 v2 last-wins로 맞춰져 있다 →
  **v3 rule 단위로 다시** 맞춰야 한다(규칙별 `scope.enabled AND rule.enabled`). 위 v2 핸드오프의
  "foldProfiles를 last-wins로" 지시는 **무효**.
- UI 문구: "넓은 범위를 꺼도 자체 설정이 켜진 하위는 동작"(v2 표현)은 부분적으로만 맞다 —
  v3에선 "광역 off는 그 문서가 직접 가진 규칙만 끄고, 상속 규칙은 같은 id로만 끌 수 있다"로 정정.
