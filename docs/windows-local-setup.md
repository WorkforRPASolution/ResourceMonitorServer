# Windows 로컬 실행 가이드

> RMS(ResourceMonitorServer)를 Windows 11 개발 PC에서 실행하여 인프라(ES/MongoDB/Redis) 연결을 검증하는 가이드.
> **Debug Read-Only 모드**로 실행하므로 인프라에 쓰기가 일어나지 않습니다.

## 사전 요구사항

| 항목 | 요구 | 비고 |
|------|------|------|
| OS | Windows 10/11 x64 | arm64 Windows 미검증 |
| Python | **3.11 이상** | 3.10 이하 불가 (`pyproject.toml` 제약) |
| 네트워크 | ES/MongoDB 포트 접근 가능 | 방화벽/VPN 확인 |
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

# ─── Debug Read-Only 모드 ON ───
MONITOR_DEBUG_READ_ONLY=true

# 특정 process 만 분석 (선택, 비우면 전체)
MONITOR_DEBUG_PROCESSES=ETCH,CVD

# ─── 로그 (console = 사람이 읽기 편한 형태) ───
MONITOR_LOG_FORMAT=console
MONITOR_LOG_LEVEL=INFO

# ─── 아래는 debug 모드에서 무시되므로 기본값 유지 ───
# MONITOR_ZK_HOSTS=...       ← 연결 안 함
# MONITOR_REDIS_URL=...      ← local TTLCache 사용
# MONITOR_EMAIL_API_URL=...  ← 발송 안 함
```

### Debug Read-Only 모드에서 동작 요약

| 구성 요소 | 동작 |
|----------|------|
| MongoDB | **읽기만** (index 생성, seed 스킵) |
| Elasticsearch | **읽기만** (정상 모드와 동일) |
| Zookeeper | **연결 안 함** (리더 선출/파티션 전부 스킵) |
| Redis | **로컬 캐시만** (Redis 서버 불필요) |
| Email API | **로그만** (실제 발송 X) |
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

**성공 (인프라 연결 OK):**
```json
{
  "status": "healthy",
  "debug_read_only": true,
  "checks": {
    "elasticsearch": "ok",
    "mongodb": "ok",
    "redis": "ok",
    "email_api": "ok",
    "zookeeper": "skipped_debug"
  }
}
```

**실패 예시 (ES 연결 불가):**
```json
{
  "status": "unhealthy",
  "checks": {
    "elasticsearch": "timeout after 2s",
    "mongodb": "ok",
    ...
  }
}
```

→ 실패한 항목의 호스트/포트/방화벽 확인.

### `curl` 이 없는 경우

PowerShell 내장 명령 사용:
```powershell
Invoke-RestMethod http://localhost:8080/healthz/ready | ConvertTo-Json
Invoke-RestMethod http://localhost:8080/admin/status | ConvertTo-Json
```

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

### ES 연결 타임아웃
```
elasticsearch: "timeout after 2s"
```
→ 네트워크 확인:
```powershell
Test-NetConnection -ComputerName <es-host> -Port 9200
```
`TcpTestSucceeded: True` 가 아니면 방화벽/VPN 문제.

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
