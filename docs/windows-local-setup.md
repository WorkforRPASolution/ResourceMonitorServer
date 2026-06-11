# Windows 로컬 실행 가이드

> RMS(ResourceMonitorServer)를 Windows 11 개발 PC에서 실행하여 인프라(ES/MongoDB/Redis) 연결을 검증하는 가이드.
> **Debug Read-Only 모드**로 실행하므로 인프라에 쓰기가 일어나지 않습니다.

## 사전 요구사항

| 항목 | 요구 | 비고 |
|------|------|------|
| OS | Windows 10/11 x64 | arm64 Windows 미검증 |
| Python | **3.11 이상** | 3.10 이하 불가 (`pyproject.toml` 제약) |
| 네트워크 | ES/MongoDB/**Redis** 포트 접근 가능 | 방화벽/VPN 확인 (Redis 도 부팅에 필요) |
| Git | 선택 | 없으면 zip 복사 |

> **Python 3.10 이하가 설치되어 있어도 3.11 과 공존 가능합니다.** `py -3.11` 런처로 명시 호출하면 됩니다.

---

## 1단계: Python 3.11 설치

기존 Python 3.10 이 있어도 충돌 없이 병렬 설치됩니다.

### 방법 A — winget (추천)
```powershell
winget install Python.Python.3.11
```

### 방법 B — 수동 설치
1. https://www.python.org/downloads/ 에서 **Python 3.11.x Windows installer (64-bit)** 다운로드
2. 설치 시 **"Add python.exe to PATH"** 반드시 체크
3. "Customize installation" → **pip 포함** 확인

### 설치 확인
```powershell
py -3.11 --version
# Python 3.11.x
```

---

## 2단계: 소스 코드 준비

### Git 사용
```powershell
git clone <repo-url> C:\rms
cd C:\rms
```

### Git 없이
소스를 zip 으로 압축 → Windows PC 에 복사 → `C:\rms` 에 풀기.

필요한 디렉토리 구조:
```
C:\rms\
├── src\          ← 필수
├── pyproject.toml ← 필수
├── .env.example   ← 필수
└── (나머지)
```

---

## 3단계: 가상환경 생성 및 의존성 설치

```powershell
cd C:\rms
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

> **PowerShell 실행 정책 오류 발생 시:**
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```
> 이후 `.\.venv\Scripts\Activate.ps1` 재시도.

> **`hiredis` 설치 실패 시** (C 컴파일러 부재):
> ```powershell
> pip install -e . --no-deps
> pip install fastapi "uvicorn[standard]" pydantic pydantic-settings "elasticsearch[async]>=7.11,<8" motor "apscheduler>=3.10,<4" "kazoo>=2.9,<2.11" "redis>=4.5,<5.1" httpx structlog prometheus-client cachetools
> ```
> `hiredis` 없이도 정상 동작합니다 (Redis 파서가 pure-Python fallback 사용, 성능만 약간 하락).

설치 확인:
```powershell
python -c "import fastapi, uvicorn, elasticsearch, motor, kazoo; print('OK')"
```

---

## 4단계: 환경 변수 설정

```powershell
copy .env.example .env
notepad .env
```

`.env` 에서 아래 항목을 수정합니다:

```bash
# ─── 인프라 주소 (실제 값으로 교체) ───
MONITOR_MONGO_URI=mongodb://<mongo-host>:27017
MONITOR_MONGO_DB=EARS
MONITOR_ES_HOSTS=http://<es-host>:9200
# ES 인증이 필요하면
MONITOR_ES_USERNAME=<username>
MONITOR_ES_PASSWORD=<password>
# ⚠️ Redis 는 debug 모드에서도 부팅 시 connect_with_retry 로 연결한다.
#    기본값(redis:6379)으론 startup 이 실패하므로 반드시 실제 서버로 지정할 것.
#    (debug 에서 로컬 캐시로 우회되는 건 cooldown '쓰기'뿐, 연결 자체는 필수)
MONITOR_REDIS_URL=redis://<redis-host>:6379/5

# ─── Debug Read-Only 모드 ON ───
MONITOR_DEBUG_READ_ONLY=true

# 특정 process 만 분석 (선택, 비우면 전체)
MONITOR_DEBUG_PROCESSES=ETCH,CVD

# ─── 로그 (console = 사람이 읽기 편한 형태) ───
MONITOR_LOG_FORMAT=console
MONITOR_LOG_LEVEL=INFO

# ─── ZooKeeper: debug 에선 연결 안 함 (기본값 유지 가능) ───
# MONITOR_ZK_HOSTS=...

# ─── Akka Email: 메일 발송은 suppress(로그만) 되어 부팅엔 영향 없음.
#     단 /healthz/ready 의 email_api 체크가 이 URL 로 HEAD 를 보낸다 —
#     닿으면 ready=200, 안 닿으면 email_api:false + 503 (분석은 정상, 6단계 참고).
# MONITOR_EMAIL_API_URL=http://<akka-host>:8080/EmailNotify
```

### Debug Read-Only 모드에서 동작 요약

| 구성 요소 | 동작 |
|----------|------|
| MongoDB | **읽기만** (index 생성, seed 스킵) |
| Elasticsearch | **읽기만** (정상 모드와 동일) |
| Zookeeper | **연결 안 함** (리더 선출/파티션 전부 스킵) |
| Redis | **연결 필수** (부팅 시 `connect_with_retry`) — cooldown *쓰기*만 로컬 TTLCache 로 우회 |
| Email API | **발송 suppress**(로그만) — 단 `/healthz/ready` 의 email_api 체크는 이 URL 로 HEAD |
| Scheduler | **정상 기동** (분석 흐름 관찰 가능) |

---

## 5단계: 실행

```powershell
cd C:\rms
.\.venv\Scripts\Activate.ps1
uvicorn src.main:app --host 0.0.0.0 --port 8080
```

정상 기동 시 콘솔 출력 예시:
```
INFO     Started server process [12345]
INFO     Waiting for application startup.
...
INFO     Application startup complete.
INFO     Uvicorn running on http://0.0.0.0:8080
```

---

## 6단계: 인프라 연결 확인

**새 터미널 창** 을 열고:

```powershell
# 기본 생존 확인
curl http://localhost:8080/healthz/live

# 인프라 연결 상태 확인 (핵심)
curl http://localhost:8080/healthz/ready

# 인스턴스/파티션 상태
curl http://localhost:8080/admin/status
```

### `/healthz/ready` 응답 해석

> 각 인프라 체크 값은 **boolean `true`/`false`** 입니다(`"ok"` 문자열 아님).
> `zookeeper` 만 debug 모드에서 `"skipped_debug"` 문자열입니다.
> HTTP 상태코드: ready 면 **200**, not_ready 면 **503**.

**성공 (인프라 연결 OK) — HTTP 200:**
```json
{
  "status": "ready",
  "debug_read_only": true,
  "checks": {
    "elasticsearch": true,
    "mongodb": true,
    "redis": true,
    "email_api": true,
    "zookeeper": "skipped_debug"
  },
  "scheduler_running": true,
  "is_leader": null,
  "redistribute_unhealthy": false,
  "version": "0.1.0"
}
```

**실패 예시 (ES 연결 불가) — HTTP 503:**
```json
{
  "status": "not_ready",
  "debug_read_only": true,
  "checks": {
    "elasticsearch": false,
    "mongodb": true,
    "redis": true,
    "email_api": true,
    "zookeeper": "skipped_debug"
  },
  "scheduler_running": true,
  "is_leader": null,
  "redistribute_unhealthy": false,
  "version": "0.1.0"
}
```

→ `false` 인 항목의 호스트/포트/방화벽 확인.

> **⚠️ debug 모드라도 `/healthz/ready` 는 `email_api` 를 검사한다.**
> Akka(`MONITOR_EMAIL_API_URL`) 에 닿지 않으면 `email_api: false` + **503** 이 된다.
> 하지만 이때도 **ES 조회·분석·메일 suppress 는 정상 동작**한다(부팅엔 영향 없음).
> Akka 를 띄울 수 없다면 ready 503 은 무시하고, 대신 **앱 로그의 `debug_would_send_email`**
> 줄과 **`/admin/status`** 로 분석 흐름을 검증하면 된다.

### `curl` 이 없는 경우

PowerShell 내장 명령 사용:
```powershell
Invoke-RestMethod http://localhost:8080/healthz/ready | ConvertTo-Json
Invoke-RestMethod http://localhost:8080/admin/status | ConvertTo-Json
```

---

## 7단계: 프로파일 컬렉션 준비 + 분석 트리거 (debug 모드)

debug 모드는 스키마를 건드리지 않으므로 **`RESOURCE_MONITOR_PROFILE` 컬렉션이 자동 생성되지 않습니다.** 또 파티션 매니저가 없어 **분석 잡도 자동 등록되지 않습니다.** 아래 순서로 직접 준비합니다.

> ⚠️ 7-1/7-2 는 대상 Mongo에 **쓰기**(컬렉션 생성 + 프로파일 insert)를 합니다. **운영 Mongo 무변경**을 원하면 `.env` 의 `MONITOR_MONGO_URI` 를 로컬 Mongo로 두세요.

### 7-1. 빈 컬렉션 생성
서버(debug)가 만들지 않으므로 스크립트로 생성합니다(빈 컬렉션 + `uniq_scope` 인덱스, 데이터 없음):
```powershell
.\scripts\create-profile-collection.ps1            # dry-run (대상/계획만 출력)
.\scripts\create-profile-collection.ps1 -Yes       # 실제 생성
```

### 7-2. 프로파일 JSON 수동 입력
`mongosh` / Compass 등으로 `RESOURCE_MONITOR_PROFILE` 에 프로파일 문서를 직접 insert 합니다. scope `(process, eqpModel, eqpId)` 는 유일해야 합니다(uniq_scope). 스키마는 [SCHEMA.md](../SCHEMA.md) 참고.
> 분석이 실제로 돌려면 해당 process 에 **활성 EQP_INFO 장비**(`onoff=1, webmanagerUse=1`)도 있어야 합니다. 로컬 Mongo면 EQP_INFO 문서도 같이 넣어야 합니다.

### 7-3. 분석 트리거 + 확인
debug 모드는 주기 reconcile 루프가 돌지 않으므로(관찰자 계약) 수동 트리거합니다. 이 엔드포인트는 이제 **cadence reconcile**을 수행해 소유 process 의 job 을 (재)등록합니다(`{"reconciled": true|false}` 반환):
```powershell
Invoke-RestMethod -Method Post http://localhost:8080/admin/scheduler/reload
Invoke-RestMethod http://localhost:8080/admin/status | ConvertTo-Json -Depth 5   # scheduled_jobs 확인
```
- 앱 로그 `scheduler_reconciled` 의 `added`/`removed`/`job_count` 로 반영 결과 확인. 잡이 안 생기면 → 해당 process 에 활성 rule 프로파일이 없는 것.
- (참고) 정상 모드에선 프로파일을 편집하면 위 호출 없이도 쓰기 직후 자동 reconcile + 주기 루프(`MONITOR_SCHEDULER_RECONCILE_INTERVAL_SEC`, 기본 60초)로 반영됩니다.
- 잡은 rule 의 `interval_minutes` 마다 틱 → 첫 분석은 그 간격 후(`/admin/status` 의 `next_run` 확인).
- 임계 초과 시 로그에 **`debug_would_send_email`**(발송 suppress)이 뜨면 **조회→분석→알림 전 구간이 운영 데이터로 검증된 것**입니다.

---

## 종료

실행 중인 터미널에서 `Ctrl+C` 를 누르면 graceful shutdown 됩니다.

---

## 트러블슈팅

### Python 버전 오류
```
ERROR: Package 'resource-monitor-server' requires a different Python: 3.10.9 not in '>=3.11'
```
→ Python 3.11 이 설치되지 않았거나, venv 가 3.10 으로 생성됨. `py -3.11 -m venv .venv` 로 재생성.

### PowerShell 실행 정책
```
.\.venv\Scripts\Activate.ps1 : 이 시스템에서 스크립트를 실행할 수 없습니다
```
→ `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 실행 후 재시도.

### ES 연결 실패 (`/healthz/ready` 의 checks 에서 `"elasticsearch": false`)
→ 네트워크 확인:
```powershell
Test-NetConnection -ComputerName <es-host> -Port 9200
```
`TcpTestSucceeded: True` 가 아니면 방화벽/VPN 문제.

### ES: `not Elasticsearch ... unknown product` / `GET /` 가 302
원인: `MONITOR_ES_HOSTS` 가 **ES API(9200)가 아니라 Kibana/포털**을 가리키거나 앞단 프록시가 로그인으로 302 함 → elasticsearch-py 가 "정품 ES 아님"으로 판정해 부팅 실패(`es_startup_ping_failed`).
- `curl -i http://<host>:9200/` 로 확인: 정상 ES 면 `tagline: "You Know, for Search"` JSON. `Location: /login...` 302 면 Kibana/게이트웨이임.
- 운영 ES 는 보통 **클러스터 내부 DNS**(예 `elasticsearch.observability:9200`, 인증 `elastic`)라 PC 에서 직접 안 닿습니다. `kubectl -n observability port-forward svc/elasticsearch 9200:9200` 후 `MONITOR_ES_HOSTS=http://localhost:9200` + `MONITOR_ES_USERNAME=elastic` / `MONITOR_ES_PASSWORD=<비번>` 설정. (실제 주소는 `k8s/configmap.yaml` 참고)

### MongoDB 인증 실패
```
mongodb: "Authentication failed"
```
→ `.env` 의 `MONITOR_MONGO_URI` 에 인증 정보 포함:
```
MONITOR_MONGO_URI=mongodb://<user>:<password>@<host>:27017
```

### `ModuleNotFoundError`
→ venv 가 활성화 상태인지 확인. 프롬프트 앞에 `(.venv)` 가 보여야 합니다:
```powershell
(.venv) PS C:\rms>
```

### Windows 방화벽 경고
RMS 가 포트 8080 을 열 때 Windows 방화벽 팝업이 뜰 수 있습니다.
"프라이빗 네트워크" 에 대해 허용하면 됩니다.

---

## 참고

| 문서 | 내용 |
|------|------|
| [README.md](../README.md) | 전체 개요, 아키텍처, 엔드포인트 목록 |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Debug Read-Only 모드 상세 설계 (8.4절) |
| [.env.example](../.env.example) | 환경 변수 전체 목록 및 설명 |
