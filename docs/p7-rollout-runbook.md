# P7 — RMS 커스텀 메일 본문(Option C) 롤아웃 런북

> 대상: 운영/배포 담당. 전제: P1~P6 구현 완료(적대 리뷰 통과).
> 전략: **다크런치** — 코드는 먼저 `off`로 배포하고(현행 100% 동일), 검증·실측 후
> 플래그를 `on`으로 전환한다. 설계: [resource-monitor-email-template-architecture.md](resource-monitor-email-template-architecture.md), 계획: [resource-monitor-email-template-tdd-plan.md](resource-monitor-email-template-tdd-plan.md).

## 0. 개요

운영자가 작성한 HTML 템플릿을 RMS(Python)가 **완성된 본문/제목으로 렌더**해
`renderedBody`/`title` 두 **추가 필드**로 Akka에 전달하고, Akka는 그대로 발송한다
(`@HttpWebServerAddress`만 치환 — D2). 플래그 `rms_custom_body_enabled`가 **off**면
이 두 필드를 페이로드에서 생략 → Akka는 **기존 9필드 그대로** 수신(회귀 0).

파이프라인: RMS `EmailAlertClient` → Akka `/EmailNotify`(`EmailWorker.SendEmail`)
→ Redis pub/sub `SendEmails-<proj>:<cat>` → EmailingAgent → ESB(HTML 메일).

## 1. 사전 점검 (코드/설정)

- [ ] RMS: `src/alert/{tokens,body_renderer}.py`, `src/analyzer/alert_builder.py`(플래그 분기), `src/alert/models.py`(`renderedBody`/`title` 조건부 직렬화), `src/db/repository.py`(5-tier 폴백 accessor) 머지.
- [ ] Akka(HttpWebServer): `JsonInterfaces.scala`(`renderedBody`/`title: Option[String]`), `EmailBodyResolver.scala`, `EmailWorker.scala`(SendEmail renderedBody 분기 + legacy `else` byte-unchanged) 머지.
- [ ] WebManager: `server/features/rms-email-template/`(CRUD), `client/src/features/rms-email-template/`(편집기·렌더러·lint) 머지.
- [ ] 플래그 **OFF 확인**: `MONITOR_RMS_CUSTOM_BODY_ENABLED` 미설정 또는 `false`(기본 False).
- [ ] 사이즈 가드 기본값: `MONITOR_RMS_ERB_ROW_LIMIT=50`, `MONITOR_RMS_BODY_BYTE_CAP=256000`.

## 2. 시드 (플래그 on 전 게이트)

### 2-1. catch-all 템플릿 1행 (RMS 스크립트)
전용 템플릿이 없는 process/model도 기본 렌더 메일이 나가도록 만능 행
`(app=ARS, process=_, model=_, code=RESOURCE_MONITOR, subcode=_)`을 넣는다.
`html`/`title`은 `body_renderer.DEFAULT_BODY/DEFAULT_TITLE`를 import해 byte-동일.

```powershell
# ResourceMonitorServer 루트, venv 활성, .env에 MONITOR_MONGO_URI/MONITOR_MONGO_DB 설정
.\scripts\seed-template-catchall.ps1            # dry-run (대상·행만 출력)
.\scripts\seed-template-catchall.ps1 -Yes       # 실제 upsert (멱등)
# 또는: python -m tools.seed_template_catchall [--yes]
```
검증(mongosh 또는 동등):
```
db.RESOURCE_MONITOR_EMAIL_TEMPLATE.findOne({app:"ARS",process:"_",model:"_",code:"RESOURCE_MONITOR",subcode:"_"})
```
→ `title`/`html`이 RMS 상수와 일치하면 OK. (컬렉션·인덱스는 WebManager가 소유 — 스크립트는 행만 upsert)

### 2-2. 권한 시드 (WebManager)
신규 배포 시 `FEATURE_PERMISSIONS`에 `rmsEmailTemplate` 기본값이 있어야 권한 UI에 노출된다.
- WebManager 서버 **기동 시 자동 동기화**됨(`initializeDefaultPermissions()`). 즉 일반 배포면 자동.
- 수동 시드: `cd server && npm run seed:permissions`.
- 역할 권한(`WEBMANAGER_ROLE_PERMISSIONS`)의 `rmsEmailTemplate` 플래그도 기동 시 `initializeRolePermissions()`로 자동 동기화(Admin=true, 그 외=false).
검증: `db.FEATURE_PERMISSIONS.findOne({feature:"rmsEmailTemplate"})` 존재.

## 3. Phase 1 — off 배포 (다크런치)

플래그 OFF로 전 코드 배포. 페이로드가 **정확히 9필드**(renderedBody/title 없음)인지 로그로 확인.
회귀 가드(§7) 1~5 통과 확인. 이 단계는 현행과 100% 동일해야 한다.

## 4. Phase 2 — 검증 (플래그 on 전)

### 4-1. 자동 테스트 (코드베이스별)

RMS (`ResourceMonitorServer/`, venv):
```bash
python -m pytest tests/ -q            # 전체 그린(현재 798 passed 기준) — 회귀 가드 #1/#2
python -m pytest tests/unit/test_akka_contract.py -q          # 와이어 계약(rendered/legacy)
python -m pytest tests/unit/test_body_renderer.py -q          # 렌더러(escape/ERB/캡/제목 콜론)
python -m pytest tests/unit/test_alert_builder.py -q          # 플래그 on/off, 템플릿 미스→DEFAULT_BODY 폴백
python -m pytest tests/integration/test_template_repository_it.py -q   # 5-tier catch-all
python -m pytest "tests/integration/test_phase1_analysis_e2e.py" -q    # e2e: off→9필드, 그룹 N행, 미스→기본본문
```
(통합/e2e는 실 ES/Mongo/Redis 필요 — `make dev-up`)

Akka (`HttpWebServer/`, **JDK 8 필수**):
```bash
export JAVA_HOME=/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home   # 또는 배포 환경의 JDK8
mvn test    # EmailHttpDataFormatSpec(legacy→None/None, rendered→Some/Some), EmailBodyResolverSpec(4분기)
```
+ 코드리뷰 가드 #3/#5: `EmailWorker.scala` legacy 블록 `else{}` byte-unchanged, 두 분기 모두 `getEmailCategory` 호출.

WebManager (`WebManager/`):
```bash
cd server && npm test    # 서버 전체(rms-email-template 포함, 현재 1243 passed 기준)
cd ../client && npm test # 클라 — rms-email-template 35개 + golden byte-동일 드리프트 가드
```
> 주의: 클라에 **기존·무관 실패 13건**(`features/clients/components/config-form` ResourceAgent)이 있음 — 이 기능과 무관. `src/features/rms-email-template`·`src/features/permissions`가 그린이면 OK.

### 4-2. D3 사이즈 실측 (⚠ 운영 핸드오프 — 코드 아님)
Redis pub/sub payload 허용·ESB 본문 cap은 **외부/미상**. 플래그 on 전 실 클러스터에서 측정해
`MONITOR_RMS_ERB_ROW_LIMIT`/`MONITOR_RMS_BODY_BYTE_CAP` 보정:
1. **행 캡**: 동일 그룹 80+대 + 긴 metric/GrafanaUrl로 대형 그룹 알림 유발 → 본문 byte/행수 로깅, Akka `Success`·Redis publish·ESB 배달 확인.
2. **byte 캡**: 대형 정적 HTML + ERB로 cap 근처 본문 → 절단 마커(`<!-- truncated -->`) 동작 확인.
3. **피크**: 1시간 정상 부하로 최대 본문 크기/행수/실패 관측.
- 수용 기준: 모두 cap의 ≤95%, 사이즈로 인한 ESB 실패 0. 거부 시 가드 하향 후 재측정.

## 5. Phase 3 — on 전환

k8s ConfigMap 등에서 `MONITOR_RMS_CUSTOM_BODY_ENABLED=true` 설정 후 배포.
배포 직후: Akka 요청 로그에 `renderedBody`/`title`이 보이고 응답이 `Success`인지, 실 알림 1건을
end-to-end(Akka→Redis→EmailingAgent→ESB) 확인.

## 6. 롤백

`MONITOR_RMS_CUSTOM_BODY_ENABLED`를 unset/`false`로 되돌리고 재배포.
legacy 경로는 byte-unchanged라 **현행 100% 복귀**(페이로드 9필드). catch-all 시드 행은 남겨도 무해.

## 7. 회귀 가드 (전환 전 필수)

| # | 가드 | 확인 |
|---|---|---|
| 1 | off → 페이로드 정확히 9필드 | `test_alert_models.py`/e2e `test_flag_off_legacy_payload` |
| 2 | RMS 전체 스위트 그린 | `pytest tests/ -q` |
| 3 | Akka legacy 경로 불변 | `EmailWorker.scala` `else{}` byte-unchanged(코드리뷰) |
| 4 | 공유 컬렉션 불가침 | EQP_INFO/EMAILINFO/EMAIL_RECIPIENTS/EMAIL_TEMPLATE_REPOSITORY 무변경 |
| 5 | 수신자 라우팅 불변 | 두 분기 모두 `getEmailCategory(process,model,hostname,code,line)` |

## 8. 환경 변수 (env_prefix `MONITOR_`)

| 변수 | 필드/기본값 | 용도 |
|---|---|---|
| `MONITOR_RMS_CUSTOM_BODY_ENABLED` | `rms_custom_body_enabled` / `False` | 다크런치 플래그(off→on) |
| `MONITOR_RMS_ERB_ROW_LIMIT` | `rms_erb_row_limit` / `50` | ERB 행 캡(D3 보정) |
| `MONITOR_RMS_BODY_BYTE_CAP` | `rms_body_byte_cap` / `256000` | 본문 byte 캡(D3 보정) |
| `MONITOR_MONGO_URI` / `MONITOR_MONGO_DB` | / `EARS` | 시드 스크립트 연결 |

## 9. 운영 핸드오프 요약 (이 세션에서 불가)

- **D3 실측**(§4-2): 실 Redis/ESB 한계 측정 → 가드 보정.
- **catch-all 시드 prod 실행**(§2-1 `-Yes`): 스크립트는 작성·검증 완료, prod 실행은 운영.
- **플래그 flip + 배포**(§3) 및 실 배달 확인.
- **TinyMCE 편집기 대화형 검증**: 주석 마커 보존, 토큰/ERB 클릭 삽입, 탭 전환(브라우저 수동).
