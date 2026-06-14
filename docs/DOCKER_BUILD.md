# RMS Docker 이미지 빌드 & 배포 가이드

ResourceMonitorServer(RMS)를 **소스 패키징 → Docker 이미지 빌드 → 오프라인 전달/배포**
3단계로 만드는 방법. WebManager의 동일 패턴(`docs/DEPLOYMENT.md`)을 RMS(Python/uvicorn)에 맞춘 것이다.

> **핵심 전제**
> - 의존성은 **사내 PyPI 미러(Nexus)** 에서 받는다(폐쇄망). 공용 pypi.org 가 닿으면 `--public` 으로 우회 가능.
> - 이미지는 **레지스트리 없이 `docker save`/`docker load`** 로 옮긴다(`imagePullPolicy: IfNotPresent`).
> - **빌드는 Windows·Linux 양쪽** 에서 가능하다(스크립트 `.sh`/`.ps1` 쌍).

---

## 0. 전체 흐름

```
[개발 PC: Windows 또는 Mac/Linux]
  scripts/build-package.{ps1,sh}
        │   소스만 zip (의존성·.venv·.git 제외, ~120KB)
        ▼   ResourceMonitorServer-{날짜}.zip
        │   (FTP/scp/USB 로 빌드 서버에 전달 → 압축 해제)
        ▼
[빌드 서버: Docker 있는 Windows 또는 Linux]
  scripts/build-image.{ps1,sh}
        │   docker build (사내 PyPI 미러서 의존성 wheel 빌드) → docker save
        ▼   ResourceMonitorServer@{버전}.tar  (resource-monitor-server:{버전} + :latest)
        │   (tar 를 운영 노드에 전달)
        ▼
[운영: K8s 노드]
  docker load -i *.tar
  kubectl apply -f k8s/
```

각 단계는 분리돼 있다 — 패키징 PC, 빌드 서버, 운영 노드가 모두 같은 장비여도 되고 달라도 된다.

---

## 1. 사전 준비

| 역할 | 필요한 것 |
|---|---|
| **개발 PC**(패키징) | Linux/Mac: `zip` / Windows: PowerShell 5.0+. Docker 불필요 |
| **빌드 서버**(이미지) | Docker(또는 Docker Desktop). 사내 PyPI 미러 또는 프록시 접근 |
| **운영 노드**(배포) | Docker + (K8s 쓰면) `kubectl` |

빌드 시 의존성은 **Linux 컨테이너 안에서** 받으므로, 빌드 서버가 Windows(Docker Desktop)든 Linux든 **결과 이미지는 동일**하다(플랫폼 차이 없음).

---

## 2. 1단계 — 소스 패키징 (개발 PC)

**Linux / Mac**
```bash
./scripts/build-package.sh
# → ResourceMonitorServer-{날짜}.zip
```

**Windows**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-package.ps1
# → ResourceMonitorServer-{날짜}.zip
```

- 버전은 `pyproject.toml` 의 `version` 에서 자동 추출.
- zip 포함: `src/`, `k8s/`, `scripts/`, `pyproject.toml`, `Dockerfile`, `.dockerignore`.
- zip 제외: `__pycache__`, `*.pyc`, `.venv/`, `.git/`, `*.egg-info`, `.pytest_cache`, `*.zip`, `*.tar`.
- 의존성(휠)은 담지 않는다 → zip 이 매우 작다(~120KB). 의존성은 **빌드 때** 미러에서 받는다.

생성된 zip 을 빌드 서버로 전송하고 압축을 푼다.

---

## 3. 2단계 — 이미지 빌드 (빌드 서버)

압축을 푼 RMS 루트에서 실행한다.

**Linux / Mac**
```bash
# (a) 사내 미러 기본값 사용
./scripts/build-image.sh

# (b) 프록시 경유 + 미러 명시
./scripts/build-image.sh --proxy http://10.x.x.x:8080 \
                         --registry https://<nexus>/repository/pypi-all/simple/

# (c) 인터넷 되는 곳에서 공용 pypi 로 빌드(개발/검증용)
./scripts/build-image.sh --public
```

**Windows (Docker Desktop)**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1
powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1 -Proxy http://10.x.x.x:8080 -Registry https://<nexus>/repository/pypi-all/simple/
powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1 -Public
```

결과:
- 이미지 `resource-monitor-server:{버전}` 과 `resource-monitor-server:latest` (두 태그).
- `ResourceMonitorServer@{버전}.tar` (두 태그를 한 tar 에 저장).

### 사내 PyPI 미러 URL — ⚠️ 확인 필요
스크립트의 기본 미러 URL 은 WebManager 의 Nexus 호스트(`scpnexus.itplatform.samsungdisplay.net:8081`)에서 **유추한 값**이다:

```
https://scpnexus.itplatform.samsungdisplay.net:8081/nexus/repository/pypi-all/simple/
```

실제 사내 PyPI(pypi proxy) repo 경로가 다르면 둘 중 하나로 맞춘다:
1. 매번 `--registry <정확한 URL>` (Windows: `-Registry`) 로 지정, 또는
2. `scripts/build-image.sh` / `.ps1` 상단의 기본값(`DEFAULT_PIP_INDEX_URL` / `$DefaultPipIndexUrl`) 을 수정.

미러가 자체서명 인증서면 스크립트가 URL 호스트를 `pip --trusted-host` 로 자동 등록한다.

---

## 4. 3단계 — 전달 & 배포 (운영 노드)

tar 를 운영 노드로 옮긴 뒤:

**K8s**
```bash
docker load -i ResourceMonitorServer@{버전}.tar
kubectl apply -f k8s/        # configmap, secret, deployment, service, pdb
kubectl get pods -l app=resource-monitor-server
kubectl logs  -l app=resource-monitor-server
```
- `k8s/deployment.yaml` 은 `resource-monitor-server:latest` + `imagePullPolicy: IfNotPresent` →
  방금 load 한 로컬 이미지를 그대로 쓴다(레지스트리 불필요).
- 환경변수는 `k8s/configmap.yaml`(`secret.yaml.example` 참고해 secret 생성)로 주입.
- 버전 고정 롤아웃을 원하면 `deployment.yaml` 의 image 를 `:{버전}` 으로 바꿔도 된다(두 태그 모두 load 됨).

**단일 서버(K8s 없이)**
```bash
docker load -i ResourceMonitorServer@{버전}.tar
docker run -d -p 8000:8000 --env-file .env resource-monitor-server:latest
```

헬스체크: `GET /healthz/live`(인프라 무관), `GET /healthz/ready`(ES/ZK/Redis/Mongo ping).

---

## 5. 트러블슈팅 / 주의

- **`apt-get`(gcc) 실패** — builder 단계가 `gcc` 를 설치한다. 폐쇄망에서 데비안 mirror 가
  안 닿으면 `--proxy` 로 인터넷 게이트웨이를 거치게 한다. (대부분의 의존성은 manylinux
  바이너리 휠이라 실제 컴파일은 거의 없지만, gcc 설치 단계 자체는 네트워크가 필요하다.)
- **미러에 패키지/버전 없음** — `pip ... could not find a version`. Nexus pypi proxy 가
  대상 패키지를 캐시하도록 한 번 인터넷 경유로 받아두거나, 정확한 미러 URL 인지 확인.
- **태그 불일치** — 배포 노드의 이미지 태그가 `deployment.yaml` 의 `image:` 와 정확히
  같아야 한다. 스크립트는 `:{버전}` 과 `:latest` 둘 다 만들므로 기본 배포(`:latest`)는 그대로 동작.
- **Apple Silicon 등에서 빌드** — 개발 PC 검증 빌드는 호스트 아키텍처(arm64)로 만들어진다.
  운영 노드가 x86_64 면 **운영과 같은 아키텍처의 빌드 서버**에서 만들 것(또는
  `docker build --platform linux/amd64`).
- **`MONITOR_DEBUG_READ_ONLY`** — 운영 configmap 에 절대 넣지 말 것(분산 조정 비활성화).

---

## 6. 참고
- Dockerfile: 멀티스테이지(builder=wheel 생성 / runtime=비루트 `appuser`, 오프라인 설치).
  builder 의 `ARG PIP_INDEX_URL/PIP_TRUSTED_HOST/HTTP(S)_PROXY` 가 미러/프록시 주입점.
- `make docker-build` 은 로컬 단발 빌드용(`resource-monitor-server:dev`, 공용 pypi). 운영 배포는 위 스크립트 사용.
- WebManager 동등 문서: `../WebManager/docs/DEPLOYMENT.md`, `WINDOWS_SERVER_DEPLOY.md`.
