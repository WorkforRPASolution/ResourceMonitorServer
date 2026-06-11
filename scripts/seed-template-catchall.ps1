<#
.SYNOPSIS
    catch-all RESOURCE_MONITOR_EMAIL_TEMPLATE 행을 1건 upsert 한다 (Option C, P7).

.DESCRIPTION
    5-tier 폴백의 최종 단계인 만능 행
    (app=<email_app_name>, process="_", model="_", code="RESOURCE_MONITOR", subcode="_")
    을 넣어, 전용 템플릿이 없는 process/model 도 운영자가 편집 가능한 기본
    렌더 메일을 받게 한다. html/title 은 RMS 코드 상수(DEFAULT_BODY/DEFAULT_TITLE)
    를 import 해 byte-동일하게 채운다(드리프트 0).

    컬렉션/인덱스는 WebManager 가 소유하므로 이 스크립트는 행만 upsert 한다.
    연결 정보는 .env 의 MONITOR_MONGO_URI / MONITOR_MONGO_DB 를 사용한다.
    venv 의 Python(motor) 로 동작하므로 mongosh 설치가 필요 없다.

    멱등: 복합키 upsert 라 재실행해도 중복이 생기지 않고 html/title 을 최신
    코드 상수로 갱신한다.

.PARAMETER Yes
    실제로 upsert 한다. 생략하면 dry-run(대상/행만 출력).

.EXAMPLE
    .\scripts\seed-template-catchall.ps1            # dry-run
    .\scripts\seed-template-catchall.ps1 -Yes       # 실제 upsert
#>
param([switch]$Yes)

$ErrorActionPreference = "Stop"

# repo 루트 = 이 스크립트(scripts/)의 상위
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Error "venv 의 python 을 찾을 수 없습니다: $py`n먼저 'py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e .' 를 실행하세요."
    exit 1
}

Push-Location $root
try {
    if ($Yes) {
        & $py -m tools.seed_template_catchall --yes
    } else {
        & $py -m tools.seed_template_catchall
        Write-Host ""
        Write-Host "DRY-RUN 입니다. 실제로 넣으려면: .\scripts\seed-template-catchall.ps1 -Yes" -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}
