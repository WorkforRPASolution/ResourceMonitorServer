#Requires -Version 5.0
$ErrorActionPreference = "Stop"
# ----------------------------------------------------------------------
# build-package.ps1 - RMS 소스 패키징 (Windows PowerShell)
#
#   개발 PC 에서 실행 -> ResourceMonitorServer-{날짜}.zip 생성.
#   build-package.sh(Linux/Mac)와 동일한 zip 을 만든다.
#
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\build-package.ps1
# ----------------------------------------------------------------------

# --- 프로젝트 루트 이동 ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path "$ScriptDir\..").Path
Set-Location $ProjectRoot
Write-Host "[INFO] 프로젝트 루트: $ProjectRoot" -ForegroundColor Blue

if (-not (Test-Path "Dockerfile")) { throw "Dockerfile 이 없습니다. RMS 루트에서 실행하세요." }

# --- 버전 추출 (pyproject.toml) ---
$pyproject = Get-Content "pyproject.toml" -Raw
$m = [regex]::Match($pyproject, '(?m)^\s*version\s*=\s*"([^"]+)"')
$Version = if ($m.Success) { $m.Groups[1].Value } else { "0.0.0" }

$Date = Get-Date -Format "yyyy-MM-dd"
$Output = "ResourceMonitorServer-${Date}.zip"

Write-Host "[INFO] 버전: $Version" -ForegroundColor Blue
Write-Host "[INFO] 패키징 중... -> $Output" -ForegroundColor Blue

if (Test-Path $Output) { Remove-Item $Output }

# --- 포함 경로 / 제외 패턴 ---
$IncludePaths = @("src", "k8s", "scripts", "pyproject.toml", "Dockerfile", ".dockerignore")
$ExcludePatterns = @(
    "*__pycache__*",
    "*.pyc",
    "*.pyo",
    "*.egg-info*",
    "*\.venv\*",
    "*\venv\*",
    "*\.pytest_cache\*",
    "*\.ruff_cache\*",
    "*\.git\*",
    "*.zip",
    "*.tar"
)

$FilesToInclude = @()
foreach ($path in $IncludePaths) {
    $fullPath = Join-Path $ProjectRoot $path
    if (Test-Path $fullPath -PathType Leaf) {
        $FilesToInclude += $fullPath
    }
    elseif (Test-Path $fullPath -PathType Container) {
        $files = Get-ChildItem -Path $fullPath -Recurse -File
        foreach ($file in $files) {
            $relativePath = $file.FullName.Substring($ProjectRoot.Length + 1)
            $excluded = $false
            foreach ($pattern in $ExcludePatterns) {
                if ($relativePath -like $pattern) { $excluded = $true; break }
            }
            if (-not $excluded) { $FilesToInclude += $file.FullName }
        }
    }
}

Write-Host "[INFO] 파일 수: $($FilesToInclude.Count)개" -ForegroundColor Blue

# --- 임시폴더로 복사 후 ZIP (상대경로 보존) ---
$TempDir = $null
try {
    $TempDir = Join-Path ([System.IO.Path]::GetTempPath()) "RMS-pack-$(Get-Date -Format 'yyyyMMddHHmmss')"
    New-Item -ItemType Directory -Path $TempDir | Out-Null

    foreach ($file in $FilesToInclude) {
        $relativePath = $file.Substring($ProjectRoot.Length + 1)
        $destPath = Join-Path $TempDir $relativePath
        $destDir = Split-Path -Parent $destPath
        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
        Copy-Item -Path $file -Destination $destPath
    }

    $OutputPath = Join-Path $ProjectRoot $Output
    Compress-Archive -Path "$TempDir\*" -DestinationPath $OutputPath -Force
}
finally {
    if ($TempDir -and (Test-Path $TempDir)) {
        Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$Size = "{0:N1} MB" -f ((Get-Item $Output).Length / 1MB)
Write-Host "[OK] 패키징 완료: $Output ($Size)" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " $Output 생성 완료" -ForegroundColor Green
Write-Host "" -ForegroundColor Green
Write-Host " 다음 단계 (빌드 서버에서):" -ForegroundColor Green
Write-Host "   1. zip 을 빌드 서버로 전송 후 압축 해제" -ForegroundColor Green
Write-Host "   2. Windows: powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1" -ForegroundColor Green
Write-Host "      Linux:   ./scripts/build-image.sh" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
