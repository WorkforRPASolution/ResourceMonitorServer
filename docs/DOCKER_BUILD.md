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

> **⚠️ Windows 에서 패키징한 zip 을 Linux 빌드서버에서 풀었다면 — 줄바꿈(LF) 변환 먼저**
> Windows 체크아웃(git `core.autocrlf=true`)이나 편집기로 인해 `.sh` 가 **CRLF** 가 되면
> Linux `bash` 가 `\r` 까지 명령으로 읽어 `$'\r': command not found` / `bad interpreter` 로 실패한다.
> 빌드 전 한 번 LF 로 변환한다(빌드서버, RMS 루트):
> ```bash
> # dos2unix 가 있으면(권장)
> dos2unix scripts/*.sh Dockerfile .dockerignore
> # 없으면 sed (GNU sed; CR 제거)
> sed -i 's/\r$//' scripts/*.sh Dockerfile .dockerignore
> # 실행권한도 zip 에서 유실됐을 수 있으니 함께
> chmod +x scripts/*.sh
> ```
> 변환 확인: `file scripts/build-image.sh` 결과에 `with CRLF` 가 없어야 한다(또는 `grep -c $'\r' scripts/build-image.sh` 가 `0`).

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
kubectl apply -f secret.yaml # 먼저: secret.yaml.example 채워서 생성(§5.2). 안 하면 Pod 기동 실패
kubectl apply -f k8s/        # configmap, deployment, service, pdb (.example 은 자동 제외)
kubectl get pods -l app=resource-monitor-server
kubectl logs  -l app=resource-monitor-server
```
- `k8s/deployment.yaml` 은 `resource-monitor-server:latest` + `imagePullPolicy: IfNotPresent` →
  방금 load 한 로컬 이미지를 그대로 쓴다(레지스트리 불필요).
- 환경변수는 `k8s/configmap.yaml`(`secret.yaml.example` 참고해 secret 생성)로 주입 → **매니페스트 전문·환경변수 표는 §5**.
- 버전 고정 롤아웃을 원하면 `deployment.yaml` 의 image 를 `:{버전}` 으로 바꿔도 된다(두 태그 모두 load 됨).

**단일 서버(K8s 없이)**
```bash
docker load -i ResourceMonitorServer@{버전}.tar
docker run -d -p 8000:8000 --env-file .env resource-monitor-server:latest
```

헬스체크: `GET /healthz/live`(인프라 무관·항상 200), `GET /healthz/ready`(ES·Mongo·Redis·ZK + **Email API** 5종 ping; 하나라도 실패하면 503). probe 설정 전문은 §5.3.

---

## 5. K8s 매니페스트 상세 (configmap · secret · deployment · service · pdb)

`k8s/` 디렉터리에 운영 매니페스트 5종이 들어 있고, 이미지를 `docker load` 한 뒤 `kubectl apply -f k8s/` 한 번이면 배포된다. 아래는 그 5개 파일의 **전문과 설명**이다 — 원본 SoT 는 `k8s/*.yaml` 파일이며, 이 문서는 운영자가 바로 보고 이해하도록 옮긴 것이다(파일을 고치면 이 표·예시도 함께 갱신할 것).

> **적용 순서** — ConfigMap·Secret 이 먼저 존재해야 Deployment 의 `envFrom` 이 해석된다. `kubectl apply -f k8s/` 는 파일명 알파벳 순(configmap→deployment→pdb→secret→service)으로 적용하지만 Pod 는 스케줄 시점에 env 를 읽으므로 한 번에 apply 해도 무방하다. 불안하면 `kubectl apply -f k8s/configmap.yaml -f k8s/secret.yaml` 을 먼저 실행한다.
>
> `secret.yaml` 은 실제 자격증명이 들어가므로 **커밋 금지**(`secret.yaml.example` 만 커밋, `secret.yaml` 은 `.gitignore`).

### 5.1 ConfigMap — 비밀 아닌 설정 (`k8s/configmap.yaml`)

모든 설정은 `MONITOR_` 접두사 환경변수다(`src/config/settings.py` 의 `env_prefix="MONITOR_"`). 자격증명(비밀번호·URI)은 여기 두지 말고 Secret(§5.2)으로.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: resource-monitor-config
  labels:
    app: resource-monitor-server
data:
  # ----- Elasticsearch (운영 7.11.x) -----
  MONITOR_ES_HOSTS: "http://elasticsearch.observability:9200"
  MONITOR_ES_USERNAME: "elastic"
  MONITOR_ES_USE_SSL: "false"
  MONITOR_ES_REQUEST_TIMEOUT: "30"
  MONITOR_ES_MAX_RETRIES: "3"

  # ----- MongoDB (EARS DB) -----
  # URI는 Secret에서 주입 (자격증명 포함)
  MONITOR_MONGO_DB: "EARS"

  # ----- Zookeeper 3.5.5 -----
  MONITOR_ZK_HOSTS: "zookeeper-0.zookeeper:2181,zookeeper-1.zookeeper:2181,zookeeper-2.zookeeper:2181"
  MONITOR_ZK_ROOT_PATH: "/resource-monitor"
  MONITOR_ZK_SESSION_TIMEOUT: "30"
  # SASL 미사용 시 빈 값 (Secret이 빈 mechanism을 주입)

  # ----- Redis 5.0.6 -----
  # DB 격리:
  #   DB 0  = WebManager
  #   DB 5  = ResourceMonitorServer (이 서비스)
  #   DB 10 = socks-agent / direct-agent (docker-compose)
  #   DB 15 = RMS integration/e2e 테스트 (localhost only)
  # prefix 'RESOURCE_ALERT:' 는 belt-and-suspenders 이중 가드
  MONITOR_REDIS_URL: "redis://redis.cache:6379/5"
  MONITOR_REDIS_KEY_PREFIX: "RESOURCE_ALERT"

  # ----- Email API (Akka HttpWebServer) -----
  MONITOR_EMAIL_API_URL: "http://httpwebserver.notification:8080/EmailNotify"
  MONITOR_EMAIL_API_TIMEOUT: "10"

  # ----- Grafana (alert body 링크용) -----
  MONITOR_GRAFANA_BASE_URL: "https://grafana.factory.local"
  MONITOR_GRAFANA_DASHBOARD_UID: ""

  # ----- Scheduler / instance -----
  MONITOR_SCHEDULER_MISFIRE_GRACE_TIME: "60"
  MONITOR_LOCAL_TZ: "Asia/Seoul"

  # ----- Logging -----
  MONITOR_LOG_LEVEL: "INFO"
  MONITOR_LOG_FORMAT: "json"
```

코드에는 있으나 위 ConfigMap 에는 **생략(=기본값 사용)된 선택 키**들이 있다. 운영 중 조정이 필요할 때만 `data:` 에 추가한다(아래 값은 모두 코드 기본값):

```yaml
  # ----- 선택(미설정 시 코드 기본값) -----
  MONITOR_ES_KEYWORD_SUFFIX: ".keyword"            # ES text+.keyword 매핑 대응. bare keyword면 ""
  MONITOR_EMAIL_APP_NAME: "ARS"                    # Akka EmailWorker 템플릿/카테고리 조회 키
  MONITOR_RMS_CUSTOM_BODY_ENABLED: "true"          # Option C 커스텀 본문. false면 Akka 레거시 템플릿
  MONITOR_RMS_ERB_ROW_LIMIT: "50"                  # 메일 표 최대 행
  MONITOR_RMS_BODY_BYTE_CAP: "256000"              # 메일 본문 바이트 상한(Redis/ESB 가드)
  MONITOR_SCHEDULER_RECONCILE_INTERVAL_SEC: "60"   # cadence 자동 reconcile 주기(0=비활성)
```

- 리스트형 값(`MONITOR_ES_HOSTS`)은 `a,b,c` 콤마구분 또는 `["a","b"]` JSON 둘 다 허용된다.
- ⚠️ `MONITOR_ZK_STARTUP_BUDGET_SEC`(기본 45)도 선택 키지만 **livenessProbe.initialDelaySeconds(60)보다 반드시 작아야** 한다(§5.3 타이밍 불변식, `tests/unit/test_k8s_probe_invariants.py` 가 강제). 꼭 필요할 때만 60 미만으로 조정.
- ⚠️ `MONITOR_DEBUG_READ_ONLY` / `MONITOR_DEBUG_PROCESSES` 는 **운영 매니페스트에 절대 넣지 말 것**(분산 조정 비활성화 → 감지 공백). 개발 PC 전용.

### 5.2 Secret — 자격증명 (`k8s/secret.yaml.example`)

`.example` 만 커밋하고, placeholder 를 실제 값으로 채운 `secret.yaml` 로 저장한 뒤 `kubectl apply` 한다(`secret.yaml` 은 `.gitignore`).

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: resource-monitor-secrets
  labels:
    app: resource-monitor-server
type: Opaque
stringData:
  # Mongo 자격증명 — URI에 user:password 포함
  MONITOR_MONGO_URI: "mongodb://USER:CHANGE_ME@mongodb.ears:27017"

  # ES basic auth password
  MONITOR_ES_PASSWORD: "CHANGE_ME"

  # Redis AUTH (5.0.6은 ACL 없음 — 단일 password)
  MONITOR_REDIS_PASSWORD: "CHANGE_ME"

  # ZK SASL — 미사용 시 둘 다 빈 문자열
  MONITOR_ZK_SASL_MECHANISM: ""
  MONITOR_ZK_SASL_USERNAME: ""
  MONITOR_ZK_SASL_PASSWORD: ""
```

- `stringData` 라 평문으로 적으면 K8s 가 자동으로 base64 인코딩해 저장한다.
- ES 는 username(공개)은 ConfigMap, password(비밀)은 Secret 으로 분리한다.
- ZK SASL 미사용이면 세 값 모두 빈 문자열(코드가 빈 mechanism 을 unauthenticated 로 해석).

### 5.3 Deployment (`k8s/deployment.yaml`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: resource-monitor-server
  labels:
    app: resource-monitor-server
spec:
  # Phase 0: 단일 인스턴스. Phase 1+에서 멀티 인스턴스 페일오버 검증 후 증가.
  replicas: 1
  selector:
    matchLabels:
      app: resource-monitor-server
  strategy:
    type: RollingUpdate
    rollingUpdate:
      # PDB(maxUnavailable=0)와 짝. 새 pod ready 후 옛 pod 정리.
      maxUnavailable: 0
      maxSurge: 1
  template:
    metadata:
      labels:
        app: resource-monitor-server
    spec:
      # PreStop sleep 5s + scheduler graceful shutdown 30s + 여유 25s
      terminationGracePeriodSeconds: 60
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: monitoring
          image: resource-monitor-server:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
              name: http
          envFrom:
            - configMapRef:
                name: resource-monitor-config
            - secretRef:
                name: resource-monitor-secrets
          env:
            # 인스턴스 ID는 pod 이름으로 — leader election + partition assignment 키
            - name: MONITOR_INSTANCE_ID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            # ⚠️ MONITOR_DEBUG_READ_ONLY 는 여기에 절대 설정하지 말 것.
            # 개발 PC 전용 플래그이며, prod 에서 켜지면 분산 조정이 비활성화되어
            # 감지 공백이 발생한다. ARCHITECTURE.md §9 참고.
          resources:
            requests:
              memory: "512Mi"
              cpu: "200m"
            limits:
              # 20K 장비 bucket aggregation 대응 (PRD §13)
              memory: "1Gi"
              cpu: "500m"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            # readOnlyRootFilesystem이라 임시 파일용
            - name: tmp
              mountPath: /tmp
          # liveness는 인프라 무관. 죽은 프로세스 감지만.
          livenessProbe:
            httpGet:
              path: /healthz/live
              port: http
            initialDelaySeconds: 60   # ZK 세션 + seed + watch 등록 여유
            periodSeconds: 30
            failureThreshold: 3
            timeoutSeconds: 5
          # readiness는 5개 인프라 ping. 일시 장애 시 트래픽 차단만, 재시작 X.
          readinessProbe:
            httpGet:
              path: /healthz/ready
              port: http
            initialDelaySeconds: 15
            periodSeconds: 10
            failureThreshold: 6       # ~60s 동안 계속 실패해야 not-ready
            timeoutSeconds: 3
          lifecycle:
            preStop:
              exec:
                # iptables 전파 + endpoint 제거 시간 확보
                command: ["sh", "-c", "sleep 5"]
      volumes:
        - name: tmp
          emptyDir: {}
```

핵심 설계 포인트:

- **단일 인스턴스(`replicas: 1`, Phase 0)** + `maxUnavailable: 0`/`maxSurge: 1` 롤링업데이트 → 새 Pod 가 Ready 된 뒤 옛 Pod 를 정리(PDB 와 짝, §5.5).
- **보안 컨텍스트**: 비루트(`runAsUser: 1000`, 이미지의 `appuser` uid 와 일치) · `readOnlyRootFilesystem: true` · 모든 capability `drop: ["ALL"]` · `seccompProfile: RuntimeDefault`. 루트FS 가 읽기전용이라 임시쓰기용 `/tmp` emptyDir 만 마운트한다 — 앱은 로그를 stdout/stderr 로만 내보내고(structlog) 캐시는 전부 메모리(`cachetools.TTLCache`), 스케줄러 jobstore 도 in-memory 라 **영속 볼륨이 필요 없는 stateless 설계**(상태는 ES/Mongo/Redis/ZK 에 있음).
- **`MONITOR_INSTANCE_ID` = Pod 이름**(`fieldRef: metadata.name`) → leader election + 파티션 할당 키. 매니페스트에 직접 쓰지 않고 런타임에 주입한다.
- **resources**: request `512Mi`/`200m`, limit `1Gi`/`500m`(20K 장비 bucket aggregation 대비, PRD §13).
- **probe** (실제 동작은 `src/api/health.py`):
  - liveness `GET /healthz/live` — 인프라를 전혀 건드리지 않고 항상 `200 {"status":"alive"}`. 죽은 프로세스만 감지(일시적 ES/Mongo 장애로 재시작 루프에 빠지지 않게).
  - readiness `GET /healthz/ready` — ES·Mongo·Redis·ZK + **Email API 5종**을 각 2s timeout 으로 ping, 하나라도 실패하면 `503`. leader 가 redistribute 재시도를 소진해 일을 못 하는 상태도 `503`. `failureThreshold: 6 × periodSeconds: 10 ≈ 60s` 동안 계속 실패해야 not-ready 가 되어 트래픽이 빠진다(재시작은 안 함).
- **graceful shutdown / 기동 타이밍 불변식**(`tests/unit/test_k8s_probe_invariants.py`·`test_settings.py` 가 회귀 방지로 고정):
  - `zk_startup_budget_sec(45)` **<** liveness `initialDelaySeconds(60)` — ZK 장애가 lifespan 기동을 무한정 잡지 못하게(여유 15s).
  - preStop `sleep 5` + 스케줄러 graceful shutdown `timeout=30` = 35s **<** `terminationGracePeriodSeconds(60)`(여유 25s).

### 5.4 Service (`k8s/service.yaml`)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: resource-monitor-server
  labels:
    app: resource-monitor-server
spec:
  type: ClusterIP
  selector:
    app: resource-monitor-server
  ports:
    - name: http
      port: 8000
      targetPort: http
      protocol: TCP
```

- 클러스터 내부용 `ClusterIP`, 포트 8000(`http`). probe·메트릭(`/metrics`, §5.7)도 모두 같은 포트를 쓴다.

### 5.5 PodDisruptionBudget (`k8s/pdb.yaml`)

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: resource-monitor-pdb
  labels:
    app: resource-monitor-server
spec:
  # Phase 0 단일 인스턴스 보호. 노드 drain 시 PreStop이 끝날 때까지 evict 차단.
  # Phase 1+ 멀티 인스턴스로 가면 minAvailable: 1 또는 maxUnavailable: 1로 변경.
  maxUnavailable: 0
  selector:
    matchLabels:
      app: resource-monitor-server
```

- Phase 0 단일 인스턴스에서는 `maxUnavailable: 0` 으로 노드 drain 시 PreStop 이 끝날 때까지 evict 를 막는다.
- Phase 1+ 멀티 인스턴스로 가면 `minAvailable: 1`(또는 `maxUnavailable: 1`)로 완화한다.

### 5.6 환경변수 레퍼런스

전체 표면은 `src/config/settings.py` 의 `AppSettings`(MONITOR_* 접두사). `위치` 열은 운영 배포 시 그 값을 어디에 두는지를 뜻한다.

| 변수 | 위치 | 기본값 | 설명 |
|---|---|---|---|
| `MONITOR_ES_HOSTS` | ConfigMap | `http://es-cluster:9200` | ES 호스트(콤마/JSON 리스트) |
| `MONITOR_ES_USERNAME` | ConfigMap | `""` | ES basic auth user(공개) |
| `MONITOR_ES_PASSWORD` | **Secret** | `""` | ES basic auth password |
| `MONITOR_ES_USE_SSL` | ConfigMap | `false` | ES SSL 사용 여부 |
| `MONITOR_ES_REQUEST_TIMEOUT` | ConfigMap | `30` | ES 요청 timeout(s) |
| `MONITOR_ES_MAX_RETRIES` | ConfigMap | `3` | ES 재시도 횟수 |
| `MONITOR_ES_KEYWORD_SUFFIX` | 선택 | `.keyword` | text+.keyword 매핑 대응. bare keyword면 `""` |
| `MONITOR_MONGO_URI` | **Secret** | `mongodb://localhost:27017` | Mongo 접속 URI(자격증명 포함) |
| `MONITOR_MONGO_DB` | ConfigMap | `EARS` | Mongo DB 이름 |
| `MONITOR_ZK_HOSTS` | ConfigMap | `zk1:2181,zk2:2181,zk3:2181` | ZK 앙상블 |
| `MONITOR_ZK_ROOT_PATH` | ConfigMap | `/resource-monitor` | ZK znode 루트 |
| `MONITOR_ZK_SESSION_TIMEOUT` | ConfigMap | `30` | ZK 세션 timeout(s, 4–40 범위) |
| `MONITOR_ZK_STARTUP_BUDGET_SEC` | 선택 | `45` | kazoo.start() 상한. **< liveness 60 필수** |
| `MONITOR_ZK_SASL_MECHANISM` | **Secret** | `""` | 예: `DIGEST-MD5`. 빈 값=미인증 |
| `MONITOR_ZK_SASL_USERNAME` | **Secret** | `""` | ZK SASL user |
| `MONITOR_ZK_SASL_PASSWORD` | **Secret** | `""` | ZK SASL password |
| `MONITOR_REDIS_URL` | ConfigMap | `redis://redis:6379/5` | Redis URL(DB 5 = RMS 전용) |
| `MONITOR_REDIS_PASSWORD` | **Secret** | `""` | Redis AUTH(단일 password) |
| `MONITOR_REDIS_KEY_PREFIX` | ConfigMap | `RESOURCE_ALERT` | Redis 키 접두사 |
| `MONITOR_EMAIL_API_URL` | ConfigMap | `http://httpwebserver:8080/EmailNotify` | Akka 메일 API |
| `MONITOR_EMAIL_API_TIMEOUT` | ConfigMap | `10` | 메일 API timeout(s) |
| `MONITOR_EMAIL_APP_NAME` | 선택 | `ARS` | Akka EmailWorker 템플릿/카테고리 조회 키 |
| `MONITOR_GRAFANA_BASE_URL` | ConfigMap | `http://grafana:3000` | 메일 본문 Grafana 링크 base |
| `MONITOR_GRAFANA_DASHBOARD_UID` | ConfigMap | `""` | Grafana 대시보드 UID(빈 값=링크 생략) |
| `MONITOR_RMS_CUSTOM_BODY_ENABLED` | 선택 | `true` | Option C 커스텀 본문. false=Akka 레거시 |
| `MONITOR_RMS_ERB_ROW_LIMIT` | 선택 | `50` | 메일 표 최대 행 |
| `MONITOR_RMS_BODY_BYTE_CAP` | 선택 | `256000` | 메일 본문 바이트 상한 |
| `MONITOR_SCHEDULER_MISFIRE_GRACE_TIME` | ConfigMap | `60` | APScheduler misfire grace(s) |
| `MONITOR_SCHEDULER_RECONCILE_INTERVAL_SEC` | 선택 | `60` | cadence reconcile 주기(0=비활성) |
| `MONITOR_INSTANCE_ID` | Deployment(fieldRef) | (Pod 이름) | leader/partition 키. 런타임 주입 |
| `MONITOR_LOCAL_TZ` | ConfigMap | `Asia/Seoul` | 로컬 타임존 |
| `MONITOR_LOG_LEVEL` | ConfigMap | `INFO` | 로그 레벨 |
| `MONITOR_LOG_FORMAT` | ConfigMap | `json` | 로그 포맷(json/console) |
| `MONITOR_DEBUG_READ_ONLY` | **설정 금지** | `false` | 개발 전용. 운영 매니페스트 금지 |
| `MONITOR_DEBUG_PROCESSES` | **설정 금지** | `[]` | 개발 전용. 운영 매니페스트 금지 |

### 5.7 (선택) Prometheus 메트릭 수집

RMS 는 `/metrics`(포트 8000, 메인 앱과 동일·무인증)에서 Prometheus 텍스트 포맷 메트릭을 노출한다(`src/api/metrics.py`). 노출 지표:

| 종류 | 이름 | 라벨 |
|---|---|---|
| Counter | `resource_monitor_job_total` | process, status, reason |
| Counter | `resource_monitor_alerts_sent_total` | code, subcode |
| Counter | `resource_monitor_threshold_breaches_total` | process, metric, severity |
| Counter | `resource_monitor_alerts_suppressed_by_cooldown_total` | process, metric, severity |
| Histogram | `resource_monitor_job_duration_seconds` | process, metric_category |
| Histogram | `resource_monitor_es_query_duration_seconds` | process |
| Gauge | `resource_monitor_zk_leader` | — (1=이 인스턴스가 leader) |
| Gauge | `resource_monitor_assigned_processes` | — (담당 process 수) |
| Gauge | `resource_monitor_infra_up` | infra (`infra_up == 0` 으로 인프라 단절 알람) |
| Gauge | `resource_monitor_startup_complete` | — (1=lifespan 기동 완료) |

기본 `k8s/` 매니페스트에는 스크랩 설정이 **없다**(엔드포인트만 떠 있음). Prometheus 로 수집하려면 둘 중 하나:

**(A) Pod 어노테이션 방식** — `deployment.yaml` 의 `spec.template.metadata` 에 추가:

```yaml
  template:
    metadata:
      labels:
        app: resource-monitor-server
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/path: "/metrics"
        prometheus.io/port: "8000"
```

**(B) ServiceMonitor 방식**(Prometheus Operator 사용 시) — `k8s/servicemonitor.yaml` 신규:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: resource-monitor-server
  labels:
    app: resource-monitor-server
spec:
  selector:
    matchLabels:
      app: resource-monitor-server
  endpoints:
    - port: http        # service.yaml 의 포트 이름
      path: /metrics
      interval: 30s
```

### 5.8 적용 & 검증

```bash
docker load -i ResourceMonitorServer@{버전}.tar

# 1) Secret 먼저 — secret.yaml.example 은 확장자가 .yaml 이 아니라 `kubectl apply -f k8s/` 가
#    건너뛴다. 실제 값으로 채운 secret.yaml 을 만들어 적용한다.
#    (Deployment 가 secretRef 를 요구하므로 secret 이 없으면 Pod 가 기동 실패한다.)
cp k8s/secret.yaml.example secret.yaml      # 편집기로 CHANGE_ME 값 채운 뒤
kubectl apply -f secret.yaml

# 2) 나머지 — 디렉터리 적용은 .yaml/.yml/.json 만 처리(.example 은 자동 제외)
kubectl apply -f k8s/                       # configmap, deployment, service, pdb

kubectl rollout status deploy/resource-monitor-server
kubectl get pods -l app=resource-monitor-server
kubectl logs  -l app=resource-monitor-server

# probe 수동 확인(포트포워드)
kubectl port-forward deploy/resource-monitor-server 8000:8000 &
curl -fsS localhost:8000/healthz/live      # {"status":"alive"}
curl -s    localhost:8000/healthz/ready    # 5종 ping 결과(JSON); 모두 OK면 HTTP 200, 아니면 503
curl -s    localhost:8000/metrics | head   # Prometheus 텍스트
```

> ⚠️ `secret.yaml` 을 먼저 적용하지 않으면 Deployment 의 `envFrom.secretRef` 가 풀리지 않아 Pod 가 `CreateContainerConfigError` 로 기동하지 못한다. ConfigMap 만 있고 Secret 이 없는 상태를 주의.

---

## 6. 트러블슈팅 / 주의

- **`.sh` 실행 시 `$'\r': command not found` / `bad interpreter: /bin/bash^M`** — 스크립트가
  CRLF(Windows 줄바꿈)다. Linux 빌드서버에서 LF 로 변환 후 재실행:
  `dos2unix scripts/*.sh Dockerfile .dockerignore` (없으면 `sed -i 's/\r$//' scripts/*.sh Dockerfile .dockerignore`).
  근본 차단은 repo 에 `.gitattributes`(`*.sh text eol=lf`, `Dockerfile text eol=lf`)를 두는 것.
- **`Permission denied` (`./scripts/build-image.sh`)** — zip 이 실행권한을 잃었다.
  `chmod +x scripts/*.sh` 후 재실행하거나 `bash scripts/build-image.sh` 로 실행.
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

## 7. 참고
- Dockerfile: 멀티스테이지(builder=wheel 생성 / runtime=비루트 `appuser`, 오프라인 설치).
  builder 의 `ARG PIP_INDEX_URL/PIP_TRUSTED_HOST/HTTP(S)_PROXY` 가 미러/프록시 주입점.
- `make docker-build` 은 로컬 단발 빌드용(`resource-monitor-server:dev`, 공용 pypi). 운영 배포는 위 스크립트 사용.
- WebManager 동등 문서: `../WebManager/docs/DEPLOYMENT.md`, `WINDOWS_SERVER_DEPLOY.md`.
