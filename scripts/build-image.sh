#!/bin/bash
set -e
# ──────────────────────────────────────────────────────────────────────
# build-image.sh — RMS Docker 이미지 빌드 + tar 저장 (Linux/Mac)
#
#   패키징(zip)을 푼 RMS 루트에서 실행한다.
#   사내 PyPI 미러(Nexus)에서 의존성을 받아 멀티스테이지 빌드 →
#   resource-monitor-server:{버전} 이미지 생성 →
#   docker save 로 ResourceMonitorServer@{버전}.tar 저장(레지스트리 불필요).
#
#   사용법:
#     ./scripts/build-image.sh [--proxy http://ip:port]
#                              [--registry https://nexus/pypi-all/simple/]
#                              [--public]            # 공용 pypi.org 로 빌드
#
#   기본 미러 URL 은 WebManager 의 Nexus 호스트에서 유추한 값이다.
#   ⚠️ 실제 사내 PyPI(pypi) repo 경로가 다르면 --registry 로 지정하거나
#      아래 DEFAULT_PIP_INDEX_URL 을 수정할 것.
# ──────────────────────────────────────────────────────────────────────

# ─── 색상 ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# ⚠️ 확인 필요: 사내 PyPI(Nexus) simple 인덱스. WebManager npm-all 호스트 기준 유추.
DEFAULT_PIP_INDEX_URL="https://scpnexus.itplatform.samsungdisplay.net:8081/nexus/repository/pypi-all/simple/"

# ─── 옵션 파싱 ───
PROXY=""
PIP_INDEX_URL=""
PUBLIC=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --proxy)    PROXY="$2"; shift 2 ;;
    --registry) PIP_INDEX_URL="$2"; shift 2 ;;
    --public)   PUBLIC=1; shift ;;
    -h|--help)
      echo "사용법: $0 [--proxy http://ip:port] [--registry https://nexus/pypi/simple/] [--public]"
      echo "  --proxy URL      사내 프록시 (apt-get + pip 경유)"
      echo "  --registry URL   사내 PyPI 미러 simple 인덱스 URL"
      echo "  --public         사내 미러 대신 공용 pypi.org 사용"
      exit 0 ;;
    *) fail "알 수 없는 옵션: $1 (--help 참고)" ;;
  esac
done

# 의존성 출처 결정: --public 이면 미러 미사용, 아니면 (지정값 || 기본 미러)
if [ "$PUBLIC" -eq 1 ]; then
  PIP_INDEX_URL=""
  log "의존성 출처: 공용 pypi.org"
else
  [ -z "$PIP_INDEX_URL" ] && PIP_INDEX_URL="$DEFAULT_PIP_INDEX_URL"
  log "의존성 출처(사내 미러): ${PIP_INDEX_URL}"
fi

[ -f "Dockerfile" ] || fail "Dockerfile 이 없습니다. RMS 루트에서 실행하세요."
command -v docker >/dev/null 2>&1 || fail "docker 명령이 없습니다."

# ─── 버전 추출 (pyproject.toml) ───
VERSION=$(grep -E '^[[:space:]]*version[[:space:]]*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
[ -z "$VERSION" ] && VERSION="0.0.0"

IMAGE_NAME="resource-monitor-server"
IMAGE_TAG="${IMAGE_NAME}:${VERSION}"
TAR_NAME="ResourceMonitorServer@${VERSION}.tar"

log "이미지: ${IMAGE_TAG}"
log "출력: ${TAR_NAME}"

# ─── build-arg 구성 ───
BUILD_ARGS=()
TRUSTED_HOST=""
if [ -n "$PIP_INDEX_URL" ]; then
  # index URL 에서 호스트 추출 → trusted-host(자체서명) + no_proxy 대상
  TRUSTED_HOST=$(echo "$PIP_INDEX_URL" | sed -E 's|https?://([^:/]+).*|\1|')
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  BUILD_ARGS+=(--build-arg "PIP_TRUSTED_HOST=${TRUSTED_HOST}")
fi
if [ -n "$PROXY" ]; then
  log "프록시: ${PROXY}"
  BUILD_ARGS+=(--build-arg "HTTP_PROXY=${PROXY}" --build-arg "HTTPS_PROXY=${PROXY}")
  # 사내 미러는 프록시를 거치지 않도록 no_proxy 에 추가
  if [ -n "$TRUSTED_HOST" ]; then
    BUILD_ARGS+=(--build-arg "NO_PROXY=${TRUSTED_HOST},localhost,127.0.0.1")
    log "no_proxy: ${TRUSTED_HOST}"
  fi
fi

# ─── Docker Build ───
log "Docker 빌드 시작..."
docker build "${BUILD_ARGS[@]}" --no-cache=true --tag "${IMAGE_TAG}" .
ok "Docker 빌드 완료: ${IMAGE_TAG}"

# ─── Docker Save (버전 태그 단독) ───
log "이미지 저장 중... → ${TAR_NAME}"
docker save -o "${TAR_NAME}" "${IMAGE_TAG}"
SIZE=$(du -h "${TAR_NAME}" | cut -f1)
ok "저장 완료: ${TAR_NAME} (${SIZE})"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} ${TAR_NAME} 생성 완료${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN} K8s 배포 (노드/마스터에서):${NC}"
echo -e "${GREEN}   1. docker load -i ${TAR_NAME}${NC}"
echo -e "${GREEN}   2. kubectl apply -f k8s/${NC}"
echo -e "${GREEN} 단일 서버 실행:${NC}"
echo -e "${GREEN}   docker run -d -p 8000:8000 --env-file .env ${IMAGE_TAG}${NC}"
echo -e "${GREEN}========================================${NC}"
