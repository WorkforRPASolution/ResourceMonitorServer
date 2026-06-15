# syntax=docker/dockerfile:1.6
# ----------------------------------------------------------------------
# ResourceMonitorServer — Phase 0
# Multi-stage build: builder produces wheels, runtime is slim + non-root
# ----------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# ──────────────────────────────────────────────────────────────────────
# 폐쇄망 빌드 옵션 — scripts/build-image.{sh,ps1} 가 --build-arg 로 주입.
# 모두 비어 있으면 공용 인터넷 + pypi.org 로 빌드된다(개발 PC 기본값).
#   PIP_INDEX_URL    : 사내 PyPI 미러(Nexus) simple 인덱스 URL
#   PIP_TRUSTED_HOST : 그 미러 호스트(자체서명 인증서 대응 = pip --trusted-host)
#   HTTP(S)_PROXY    : 사내 프록시 (pip 다운로드 경유)
#   NO_PROXY         : 프록시를 거치지 않을 호스트(보통 사내 미러 호스트)
# ──────────────────────────────────────────────────────────────────────
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ENV http_proxy=${HTTP_PROXY} https_proxy=${HTTPS_PROXY} no_proxy=${NO_PROXY} \
    HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}

# 컴파일러/apt 불필요: 런타임 의존성은 전부 manylinux 바이너리 휠(cp311)로 설치된다
# (uvloop/httptools/websockets/watchfiles/aiohttp/pymongo/hiredis 포함). slim 이미지엔
# python3-dev 헤더도 없어 어차피 소스 빌드가 불가하므로 gcc 설치(apt)는 무의미하고,
# 폐쇄망 프록시가 deb.debian.org 를 403 으로 막으면 빌드만 깨뜨린다. 향후 소스 빌드가
# 필요한 의존성이 생기면 사내 Debian 미러로 apt 를 돌릴 것. (DOCKER_BUILD.md §6)
COPY pyproject.toml ./
COPY src/ ./src/

# Pre-build wheels for the whole dependency closure into /wheels.
# Pinning ranges match pyproject.toml — kept explicit so this layer
# does not invisibly drift past the version pins documented in plan v4.
# ${VAR:+flag $VAR} → 인자가 비어 있으면 플래그 자체가 빠져 공용 pypi.org 로 동작.
RUN pip wheel --no-deps --wheel-dir=/wheels \
       ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} \
       ${PIP_TRUSTED_HOST:+--trusted-host $PIP_TRUSTED_HOST} \
       . \
    && pip wheel --wheel-dir=/wheels \
       ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} \
       ${PIP_TRUSTED_HOST:+--trusted-host $PIP_TRUSTED_HOST} \
       'fastapi>=0.110.0' \
       'uvicorn[standard]>=0.27.0' \
       'pydantic>=2.0.0' \
       'pydantic-settings>=2.0.0' \
       'elasticsearch[async]>=7.11.0,<8.0.0' \
       'motor>=3.3.0' \
       'apscheduler>=3.10.0,<4.0.0' \
       'kazoo>=2.9.0,<2.11.0' \
       'redis[hiredis]>=4.5.0,<5.1.0' \
       'httpx>=0.27.0' \
       'structlog>=24.0.0' \
       'prometheus-client>=0.20.0' \
       'cachetools>=5.3.0'

# ----------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

# non-root user. K8s securityContext also enforces this, but having it
# in the image makes `docker run` safe by default too.
RUN adduser --disabled-password --gecos '' --uid 1000 appuser \
    && mkdir -p /tmp \
    && chown -R appuser:appuser /app /tmp

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

COPY --chown=appuser:appuser src/ ./src/

USER appuser
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# /healthz/live deliberately does NOT touch infra — safe for liveness.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz/live', timeout=3).status == 200 else 1)" \
    || exit 1

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
