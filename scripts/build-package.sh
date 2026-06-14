#!/bin/bash
set -e
# ──────────────────────────────────────────────────────────────────────
# build-package.sh — RMS 소스 패키징 (Linux/Mac)
#
#   개발 PC 에서 실행 → ResourceMonitorServer-{날짜}.zip 생성.
#   zip 안에는 이미지 빌드에 필요한 소스만 담는다(.venv/__pycache__/.git 제외).
#   이후 빌드 서버로 옮겨 build-image.{sh,ps1} 로 docker 이미지를 만든다.
#
#   Windows 에서는 scripts/build-package.ps1 을 쓴다(같은 zip 생성).
# ──────────────────────────────────────────────────────────────────────

# ─── 색상 ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# ─── 프로젝트 루트 이동 ───
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
log "프로젝트 루트: $PROJECT_ROOT"

command -v zip >/dev/null 2>&1 || fail "zip 명령이 없습니다. (Ubuntu: apt-get install zip / Mac: 기본 포함)"
[ -f "Dockerfile" ] || fail "Dockerfile 이 없습니다. RMS 루트에서 실행하세요."

# ─── 버전 추출 (pyproject.toml) ───
VERSION=$(grep -E '^[[:space:]]*version[[:space:]]*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
[ -z "$VERSION" ] && VERSION="0.0.0"

DATE=$(date +%Y-%m-%d)
OUTPUT="ResourceMonitorServer-${DATE}.zip"

log "버전: ${VERSION}"
log "패키징 중... → ${OUTPUT}"

rm -f "$OUTPUT"

# 이미지 빌드 + k8s 배포에 필요한 것만 포함.
zip -r -q "$OUTPUT" \
  src/ \
  k8s/ \
  scripts/ \
  pyproject.toml \
  Dockerfile \
  .dockerignore \
  -x "*__pycache__*" \
  -x "*.pyc" \
  -x "*.pyo" \
  -x "*.egg-info*" \
  -x "*.venv/*" \
  -x "*venv/*" \
  -x "*.pytest_cache/*" \
  -x "*.ruff_cache/*" \
  -x "*.git/*" \
  -x "*.zip" \
  -x "*.tar"

SIZE=$(du -h "$OUTPUT" | cut -f1)
ok "패키징 완료: ${OUTPUT} (${SIZE})"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} ${OUTPUT} 생성 완료${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN} 다음 단계 (빌드 서버에서):${NC}"
echo -e "${GREEN}   1. zip 을 빌드 서버로 전송 후 압축 해제${NC}"
echo -e "${GREEN}   2. Linux:   ./scripts/build-image.sh${NC}"
echo -e "${GREEN}      Windows: powershell -ExecutionPolicy Bypass -File scripts/build-image.ps1${NC}"
echo -e "${GREEN}========================================${NC}"
