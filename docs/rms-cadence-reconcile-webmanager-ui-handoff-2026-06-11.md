# RMS cadence 자동 reconcile — WebManager UI 수정 핸드오프 (2026-06-11)

> 대상: **WebManager 세션**. 이 문서만 보고 `rms-monitor-profile` UI를 정정할 수 있도록 작성.
> 작성: ResourceMonitorServer 세션. RMS 측 변경은 이 문서의 §1에 요약(별도 커밋).
> RMS 코드는 **읽기 전용 참조**용이며, 실제 UI 수정은 WebManager 세션에서 진행.

---

## 0. TL;DR

- RMS가 **프로파일 편집 시 평가주기(interval/cadence) 변경을 자동 반영**하도록 바뀌었다(쓰기 직후 + 주기 reconcile). 더 이상 RMS 재시작/파티션 재배치를 기다릴 필요가 없다.
- WebManager가 RMS에 보내는 **요청/응답 계약은 하나도 안 바뀜** → 기능적으로 깨지는 곳 없음. 새 필드·엔드포인트 추가 불필요.
- 단, WebManager UI에 **"점검 주기 변경은 RMS 재시작 또는 파티션 재배치 시 반영"** 이라고 안내하는 배너·도움말·주석이 있는데, 이제 **사실과 다름**(틀린 안내). → **이 문구만 "자동 반영"으로 정정**하면 됨.
- `cadenceChanged` 플래그/서버 로직/관련 테스트는 **그대로 유지**. 의미만 "조치(재시작) 필요" → "자동 반영 안내"로 바뀜.
- **배포 순서 주의**: 이 UI 정정은 **RMS reconcile 변경이 배포된 뒤**에 반영할 것(§5).

---

## 1. RMS에서 무엇이 바뀌었나 (배경)

### 1.1 이전 상태(결함)
- `POST /admin/scheduler/reload`가 **정상(prod) 모드에서 no-op**이었다(`reload(processes=None)`이 경고만 찍고 return). WebManager가 이를 알고 **의도적으로 호출하지 않았다**(`server/features/rms-monitor-profile/writeService.js:13-16` 주석).
- prod는 `replicas: 1`이라 파티션 재배치가 startup 이후 다시 발생하지 않음 → **interval(cadence) 변경이 사실상 RMS 재시작 전까지 반영되지 않았다.** WebManager의 "재시작/재배치 시 반영" 배너는 이 현실을 정확히 반영한 것이었다.
- 단, 임계값·채널 등 **내용** 변경은 엔진이 매 검사 tick마다 Mongo를 재조회하므로 이미 자동 반영되고 있었다(이건 그대로).

### 1.2 변경 후 (이제)
RMS에 cadence **reconcile** 메커니즘 추가:
- 새 primitive `AnalysisScheduler.reconcile()` — 소유 공정의 interval 집합을 Mongo에서 재계산해, 등록된 `(process, interval)` 잡과 **차이(delta)만** 추가/삭제. 전체 재구성(remove-all) 안 함.
- **트리거 3가지**:
  1. **쓰기 직후(자동)** — 모든 프로파일 쓰기(`POST/PUT/DELETE /profiles`, 항목 CRUD) 성공 후 RMS가 best-effort로 reconcile 실행. ← WebManager 편집이 여기에 해당.
  2. **주기 루프** — 각 RMS pod가 `scheduler_reconcile_interval_sec`(기본 60초)마다 자기 소유 공정을 reconcile.
  3. **수동** — `POST /admin/scheduler/reload`가 이제 reconcile을 수행(정상 동작). 응답이 `{"reloaded": true}` → `{"reconciled": <bool>}`로 바뀜. **WebManager는 이 엔드포인트를 호출하지 않으므로 무관.**

### 1.3 반영 타이밍 (UI 문구 기준)
- **현재 prod(replicas: 1)**: 프로파일 쓰기를 받은 RMS pod가 모든 공정을 소유 → 쓰기 직후 reconcile이 **즉시** 적용. → interval 변경이 **저장 즉시 반영**.
- **멀티-pod(향후)**: 쓰기가 그 공정의 소유 pod에 떨어지면 즉시, 아니면 소유 pod의 다음 주기 reconcile까지(최대 `scheduler_reconcile_interval_sec`, 기본 ~60초) 반영.
- 따라서 UI 문구는 **"저장 시 자동 반영(보통 즉시, 최대 약 1분)"** 로 표현하면 정확.

### 1.4 RMS 측 참조 파일(읽기 전용)
`src/scheduler/jobs.py`(reconcile/주기루프/락), `src/api/admin.py`(reconcile+503), `src/api/profiles.py`(쓰기 후 자동 트리거), `src/config/settings.py`(`scheduler_reconcile_interval_sec`). RMS 변경은 별도 커밋 예정(작성 시점 미커밋).

---

## 2. WebManager 영향 분류

| 항목 | 영향 | 조치 |
|------|------|------|
| RMS 프로파일 CRUD 계약(`/profiles*`, `/effective`, `/healthz`) | **변경 없음** | 없음 |
| `POST /admin/scheduler/reload` 응답 형태 변경(`reloaded`→`reconciled`) | WebManager가 **호출 안 함** | 없음 |
| `cadenceChanged` 서버 플래그/로직(`writeService.js`) | 유지 | 없음(주석만 §3.4) |
| **cadence 배너/도움말 문구**("재시작/재배치 시 반영") | **사실과 다름(틀린 안내)** | **정정 필요(§3)** |
| 새 설정 필드/엔드포인트 | 없음 | 없음(playground UI 추가 불필요) |

**결론: 기능 정합성 측면에서 반드시 해야 하는 수정은 없음. 정확성(오안내 제거)을 위해 문구 정정만 권장.**

---

## 3. 수정할 곳 (WebManager, before → after)

> 아래 4곳은 모두 "재시작/재배치 시 반영"이라는 옛 전제를 담고 있다. 문구만 바꾸면 되고, 컴포넌트 구조/상태머신/서버 로직은 그대로 둔다.

### 3.1 cadence 배너 문구 — `client/src/features/rms-monitor-profile/RmsMonitorProfileView.vue` (현재 ~204-205행)

**현재:**
```html
<span>⏱ 점검 주기(interval) 구성이 변경되었습니다 — <b>RMS 재시작 또는 파티션 재배치 시 반영</b>됩니다.
  임계값·채널 변경은 즉시 반영됩니다.</span>
```

**권장(자동 반영):**
```html
<span>⏱ 점검 주기(interval) 구성이 변경되었습니다 — <b>저장 시 자동으로 스케줄에 반영</b>됩니다(보통 즉시, 최대 약 1분).
  임계값·채널 변경은 다음 검사 주기부터 반영됩니다.</span>
```

부가(선택): 이제 "조치 필요" 경고가 아니라 단순 안내이므로, amber 경고 스타일을 중립(info)으로 낮추거나 지속 배너 대신 토스트로 강등해도 된다. 유지해도 무방. `data-testid="rms-cadence-banner"`는 그대로 둘 것(테스트/추적 호환).

### 3.2 하단 도움말 문구 — 같은 파일 (현재 ~405-406행)

**현재:**
```html
<p class="...">
  변경은 즉시 저장되며 임계값·채널은 다음 검사 주기부터 반영됩니다.
  점검 주기(interval) 변경은 RMS 재시작 또는 파티션 재배치 시 반영됩니다.
</p>
```

**권장:**
```html
<p class="...">
  변경은 즉시 저장되며 임계값·채널은 다음 검사 주기부터 반영됩니다.
  점검 주기(interval) 변경도 저장 시 자동 반영됩니다(보통 즉시, 최대 약 1분).
</p>
```

### 3.3 composable 주석 — `client/src/features/rms-monitor-profile/composables/useProfileEditor.js` (현재 7행)

**현재:**
```js
 *  - cadenceChanged 응답 메타 → 지속 배너 (RMS 재시작/재배치 시 반영)
```

**권장:**
```js
 *  - cadenceChanged 응답 메타 → 안내 배너 (interval 변경은 저장 시 자동 반영)
```

> `cadencePending` ref(31행)와 `if (body?.cadenceChanged) cadencePending.value = true`(119행) **로직은 그대로 유지**. 배너의 *의미*만 바뀐다(조치 필요 → 자동 반영 안내). 원한다면 배너를 일정 시간 후 자동 해제하도록 바꿔도 되지만 필수 아님.

### 3.4 서버 주석 — `server/features/rms-monitor-profile/writeService.js` (현재 13-16행)

**현재:**
```js
 * cadenceChanged: enabled rule의 interval 집합을 바꿀 수 있는 쓰기(rule 항목,
 * overlay 통째/삭제) = true → UI가 "RMS 재시작/재배치 시 반영" 배너 표시.
 * RMS의 POST /admin/scheduler/reload는 정상 모드에서 파괴적 no-op이므로 호출하지
 * 않는다 (설계 §7.4).
```

**권장:**
```js
 * cadenceChanged: enabled rule의 interval 집합을 바꿀 수 있는 쓰기(rule 항목,
 * overlay 통째/삭제) = true → UI가 "interval 변경 자동 반영" 안내 배너 표시.
 * RMS는 프로파일 쓰기 직후 자동으로 cadence를 reconcile하므로(2026-06 변경)
 * WebManager가 reload를 호출할 필요는 없다. (설계 §7.4 갱신)
```

> 동작은 변함없다(여전히 reload를 호출하지 않음). 호출하지 않는 **이유**가 "no-op이라서" → "쓰기가 자동 reconcile하므로 불필요"로 바뀐다.

### 3.5 설계 문서 §7.4
"cadence 변경은 RMS 재시작/재배치 시 반영" 가정을 "쓰기 시 RMS가 자동 reconcile(보통 즉시, 최대 ~`scheduler_reconcile_interval_sec`)" 로 갱신. WebManager 설계 문서 위치는 WebManager 세션에서 확인.

---

## 4. 유지할 것 (바꾸지 말 것)

- `cadenceChanged` 서버 계산 로직 전체(`writeService.js`의 rule/overlay/delete = true, measure/notify/threshold = false).
- `useProfileEditor.js`의 `cadencePending` 상태와 set/clear 플러밍.
- `data-testid="rms-cadence-banner"`.
- WebManager는 계속 **`/admin/scheduler/reload`를 호출하지 않는다**(쓰기가 자동 reconcile하므로 불필요). 굳이 호출하도록 바꾸지 말 것.
- 서버 단위 테스트(`writeService.test.js`의 `cadenceChanged: true/false` 단언) — 그대로 통과해야 함(플래그 의미 불변).

---

## 5. 배포 순서 (중요)

UI 문구 정정은 **RMS의 reconcile 변경이 배포된 뒤**에 반영할 것.
- RMS 미배포 상태에서 UI를 "자동 반영"으로 바꾸면, 그 시점엔 여전히 재시작이 필요하므로 **반대 방향으로 틀린 안내**가 된다.
- 권장: RMS reconcile 배포 → 검증(§6) → WebManager UI 문구 정정 배포.
- 안전망: 두 배포 사이 간극이 길면, 그 기간엔 기존(재시작) 문구를 두는 편이 안전.

---

## 6. 검증 체크리스트 (RMS 배포 후, WebManager)

1. WebManager에서 어떤 scope의 **rule을 추가/삭제하거나 overlay를 통째 저장**(= cadence 변경) → 저장 성공.
2. RMS(prod, replicas:1)에서 해당 공정의 분석 잡 주기가 **재시작 없이** 새 interval로 바뀌는지 확인:
   - `GET /admin/status`의 `scheduled_jobs`에 `analysis-<process>-<새interval>m` 잡 등장.
   - 또는 RMS 로그 `scheduler_reconciled`(added/removed) 확인.
3. UI 배너/도움말이 "자동 반영" 문구로 표시되는지 확인.
4. 기존 WebManager 테스트(client/server) 전부 그린. (배너 문구를 단언하는 테스트는 현재 없음 — 텍스트 변경이 테스트를 깨지 않음. 원하면 새 문구를 단언하는 e2e/unit 1건 추가 권장.)

---

## 7. 한 줄 요약

WebManager는 RMS 프로파일 CRUD 계약을 그대로 쓰므로 **깨지는 것 없음**. 단 cadence 배너/도움말/주석의 **"재시작·재배치 시 반영" → "저장 시 자동 반영"** 정정만 하면 되고(§3 4곳), 플래그·로직·테스트는 유지하며, **RMS 배포 후** 반영한다.
