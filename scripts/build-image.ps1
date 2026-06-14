#Requires -Version 5.0
param(
    [string]$Proxy = "",
    [string]$Registry = "",
    [switch]$Public
)
$ErrorActionPreference = "Stop"
# ----------------------------------------------------------------------
# build-image.ps1 - RMS Docker 이미지 빌드 + tar 저장 (Windows / Docker Desktop)
#
#   패키징(zip)을 푼 RMS 루트에서 실행. Docker Desktop(WSL2 백엔드)이
#   linux 컨테이너를 빌드하므로 결과 이미지는 Linux 빌드와 동일하다.
#
#   사용법:
#     powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1
#       [-Proxy http://ip:port] [-Registry https://nexus/pypi-all/simple/] [-Public]
#
#   ⚠️ 기본 미러 URL 은 WebManager Nexus 호스트에서 유추한 값.
#      실제 사내 pypi repo 경로가 다르면 -Registry 로 지정하거나 아래 기본값 수정.
# ----------------------------------------------------------------------

# ⚠️ 확인 필요: 사내 PyPI(Nexus) simple 인덱스
$DefaultPipIndexUrl = "https://scpnexus.itplatform.samsungdisplay.net:8081/nexus/repository/pypi-all/simple/"

# --- 의존성 출처 결정 ---
$PipIndexUrl = ""
if ($Public) {
    Write-Host "[INFO] 의존성 출처: 공용 pypi.org" -ForegroundColor Blue
} else {
    $PipIndexUrl = if ($Registry) { $Registry } else { $DefaultPipIndexUrl }
    Write-Host "[INFO] 의존성 출처(사내 미러): $PipIndexUrl" -ForegroundColor Blue
}

if (-not (Test-Path "Dockerfile")) { throw "Dockerfile 이 없습니다. RMS 루트에서 실행하세요." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker 명령이 없습니다(Docker Desktop 확인)." }

# --- 버전 추출 (pyproject.toml) ---
$pyproject = Get-Content "pyproject.toml" -Raw
$m = [regex]::Match($pyproject, '(?m)^\s*version\s*=\s*"([^"]+)"')
$Version = if ($m.Success) { $m.Groups[1].Value } else { "0.0.0" }

$ImageName = "resource-monitor-server"
$ImageTag  = "${ImageName}:${Version}"
$TarName   = "ResourceMonitorServer@${Version}.tar"

Write-Host "[INFO] 이미지: $ImageTag (+ ${ImageName}:latest)" -ForegroundColor Blue
Write-Host "[INFO] 출력: $TarName" -ForegroundColor Blue

# --- build-arg 구성 ---
$BuildArgs = @()
$TrustedHost = ""
if ($PipIndexUrl) {
    $TrustedHost = ([regex]::Match($PipIndexUrl, '^https?://([^:/]+)')).Groups[1].Value
    $BuildArgs += @("--build-arg", "PIP_INDEX_URL=$PipIndexUrl")
    $BuildArgs += @("--build-arg", "PIP_TRUSTED_HOST=$TrustedHost")
}
if ($Proxy) {
    Write-Host "[INFO] 프록시: $Proxy" -ForegroundColor Blue
    $BuildArgs += @("--build-arg", "HTTP_PROXY=$Proxy", "--build-arg", "HTTPS_PROXY=$Proxy")
    if ($TrustedHost) {
        $BuildArgs += @("--build-arg", "NO_PROXY=$TrustedHost,localhost,127.0.0.1")
        Write-Host "[INFO] no_proxy: $TrustedHost" -ForegroundColor Blue
    }
}

# --- Docker Build ---
Write-Host "[INFO] Docker 빌드 시작..." -ForegroundColor Blue
docker build @BuildArgs --no-cache=true --tag $ImageTag .
if ($LASTEXITCODE -ne 0) { throw "docker build 실패 (exit $LASTEXITCODE)" }
docker tag $ImageTag "${ImageName}:latest"
Write-Host "[OK] Docker 빌드 완료: $ImageTag (+ :latest)" -ForegroundColor Green

# --- Docker Save (버전 + latest) ---
Write-Host "[INFO] 이미지 저장 중... -> $TarName" -ForegroundColor Blue
docker save -o $TarName $ImageTag "${ImageName}:latest"
if ($LASTEXITCODE -ne 0) { throw "docker save 실패 (exit $LASTEXITCODE)" }
$Size = "{0:N1} MB" -f ((Get-Item $TarName).Length / 1MB)
Write-Host "[OK] 저장 완료: $TarName ($Size)" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " $TarName 생성 완료" -ForegroundColor Green
Write-Host "" -ForegroundColor Green
Write-Host " K8s 배포 (노드/마스터에서):" -ForegroundColor Green
Write-Host "   1. docker load -i $TarName" -ForegroundColor Green
Write-Host "   2. kubectl apply -f k8s/" -ForegroundColor Green
Write-Host " 단일 서버 실행:" -ForegroundColor Green
Write-Host "   docker run -d -p 8000:8000 --env-file .env ${ImageName}:latest" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
