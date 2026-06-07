<#
.SYNOPSIS
    RESOURCE_MONITOR_PROFILE 컬렉션을 (비어 있게) 생성한다.

.DESCRIPTION
    MONITOR_DEBUG_READ_ONLY=true 로 서버를 띄우면 서버는 스키마를 절대
    건드리지 않으므로(컬렉션/인덱스 생성 스킵) 컬렉션이 만들어지지 않는다.
    이 스크립트로 컬렉션 + uniq_scope 인덱스를 수동으로 한 번 만들어 둔다.
    (서버가 non-debug 로 하던 것과 동일 — 데이터는 넣지 않음. 프로파일은
     이후 JSON 으로 직접 insert)

    연결 정보는 .env 의 MONITOR_MONGO_URI / MONITOR_MONGO_DB 를 사용한다.
    venv 의 Python(motor) 로 동작하므로 mongosh 설치가 필요 없다.

.PARAMETER Yes
    실제로 생성한다. 생략하면 dry-run(대상/계획만 출력).

.EXAMPLE
    .\scripts\create-profile-collection.ps1            # dry-run
    .\scripts\create-profile-collection.ps1 -Yes       # 실제 생성
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
        & $py -m tools.create_collection --yes
    } else {
        & $py -m tools.create_collection
        Write-Host ""
        Write-Host "DRY-RUN 입니다. 실제로 만들려면: .\scripts\create-profile-collection.ps1 -Yes" -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}
