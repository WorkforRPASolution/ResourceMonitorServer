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
        ▼   ResourceMonitorServer@{버전}.tar  (resource-monitor-server:{버전})
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
                         --registry https://<nexus>/repository/pypi/simple/

# (c) 인터넷 되는 곳에서 공용 pypi 로 빌드(개발/검증용)
./scripts/build-image.sh --public
```

**Windows (Docker Desktop)**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1
powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1 -Proxy http://10.x.x.x:8080 -Registry https://<nexus>/repository/pypi/simple/
powershell -ExecutionPolicy Bypass -File scripts\build-image.ps1 -Public
```

결과:
- 이미지 `resource-monitor-server:{버전}` (버전 태그 단독, `:latest` 없음).
- `ResourceMonitorServer@{버전}.tar` (이 이미지를 tar 에 저장).

### 사내 PyPI 미러 URL — ⚠️ 확인 필요
스크립트의 기본 미러 URL 은 WebManager 의 Nexus 호스트(`scpnexus.itplatform.samsungdisplay.net:8081`)에서 **유추한 값**이다:

```
https://scpnexus.itplatform.samsungdisplay.net:8081/nexus/repository/pypi/simple/
```

실제 사내 PyPI(pypi proxy) repo 경로가 다르면 둘 중 하나로 맞춘다:
1. 매번 `--registry <정확한 URL>` (Windows: `-Registry`) 로 지정, 또는
2. `scripts/build-image.sh` / `.ps1` 상단의 기본값(`DEFAULT_PIP_INDEX_URL` / `$DefaultPipIndexUrl`) 을 수정.

> **⚠️ URL 은 반드시 `/simple/` 로 끝나야 한다(끝 슬래시 포함).** pip 은 PEP 503 *Simple
> Repository API* 로 패키지를 찾으며, `--index-url` 뒤에 `/<패키지명>/` 을 붙여 파일 목록을
> 읽는다. Nexus 에서 그 엔드포인트가 `.../repository/<repo>/simple/` 다(`.../repository/<repo>/`
> 는 repo 루트일 뿐 pip 용 API 가 아니다). `/simple/` 을 빠뜨리면 pip 이 엉뚱한 경로를 쳐서
> **모든 패키지가 `Could not find a version ... (from versions: none)`** 로 실패한다(§6 참고).
> 스크립트 기본값은 이미 `/simple/` 로 끝나니, `--registry` 로 덮을 때 빠뜨리지 말 것.

미러가 자체서명 인증서면 스크립트가 URL 호스트를 `pip --trusted-host` 로 자동 등록한다.

---

## 4. 3단계 — 전달 & 배포 (운영 노드)

tar 를 운영 노드로 옮긴 뒤:

**K8s**
```bash
docker load -i ResourceMonitorServer@{버전}.tar
kubectl apply -f secret.yaml # 먼저: secret.yaml.example 채워서 생성(§5.2). 안 하면 Pod 기동 실패
kubectl apply -f k8s/        # configmap, deployment, service, pdb (.example 은 자동 제외)
# ⚠️ k8s <1.21 이면 pdb.yaml(policy/v1) 미지원 → 제외하고 개별 적용(자세히는 §5 호환 노트):
#    kubectl apply -f k8s/configmap.yaml -f k8s/deployment.yaml -f k8s/service.yaml
kubectl get pods -l app=resource-monitor-server
kubectl logs  -l app=resource-monitor-server
```
- `k8s/deployment.yaml` 은 `resource-monitor-server:{버전}`(버전 핀) + `imagePullPolicy: IfNotPresent` →
  방금 load 한 로컬 이미지를 그대로 쓴다(레지스트리 불필요). 빌드 스크립트는 **버전 태그 단독**으로만
  이미지를 만드므로(`:latest` 없음), **`deployment.yaml` 의 image 태그를 빌드한 버전과 정확히 일치**시켜야 한다.
- 새 버전 배포 시: `pyproject.toml` 의 version 을 올려 빌드 → `deployment.yaml` 의 `image:` 태그도 같은 버전으로 갱신.
- 환경변수는 `k8s/configmap.yaml`(`secret.yaml.example` 참고해 secret 생성)로 주입 → **매니페스트 전문·환경변수 표는 §5**.

**단일 서버(K8s 없이)**
```bash
docker load -i ResourceMonitorServer@{버전}.tar
docker run -d -p 8000:8000 --env-file .env resource-monitor-server:{버전}
```

헬스체크: `GET /healthz/live`(인프라 무관·항상 200), `GET /healthz/ready`(ES·Mongo·Redis·ZK + **Email API** 5종 ping; 하나라도 실패하면 503). probe 설정 전문은 §5.3.

---

## 5. K8s 매니페스트 상세 (configmap · secret · deployment · service · pdb)

`k8s/` 디렉터리에 운영 매니페스트 5종이 들어 있고, 이미지를 `docker load` 한 뒤 `kubectl apply -f k8s/` 한 번이면 배포된다. 아래는 그 5개 파일의 **전문과 설명**이다 — 원본 SoT 는 `k8s/*.yaml` 파일이며, 이 문서는 운영자가 바로 보고 이해하도록 옮긴 것이다(파일을 고치면 이 표·예시도 함께 갱신할 것).

> **적용 순서** — ConfigMap·Secret 이 먼저 존재해야 Deployment 의 `envFrom` 이 해석된다. `kubectl apply -f k8s/` 는 파일명 알파벳 순(configmap→deployment→pdb→secret→service)으로 적용하지만 Pod 는 스케줄 시점에 env 를 읽으므로 한 번에 apply 해도 무방하다. 불안하면 `kubectl apply -f k8s/configmap.yaml -f k8s/secret.yaml` 을 먼저 실행한다.
>
> `secret.yaml` 은 실제 자격증명이 들어가므로 **커밋 금지**(`secret.yaml.example` 만 커밋, `secret.yaml` 은 `.gitignore`).

> **⚠️ k8s 버전 호환 — `pdb.yaml`(policy/v1)=1.21+, `seccompProfile`=1.19+**
> 이 묶음은 비교적 최신 필드를 일부 쓴다. **타깃이 k8s 1.21 미만(예: 1.14)이면 그대로 두고 적용만 제외**한다(매니페스트 수정 불필요, **나중에 k8s 업그레이드 시 도입**):
> - **`pdb.yaml` 은 적용하지 말 것** — `policy/v1` PodDisruptionBudget 은 **k8s 1.21 에서 GA** 라 그 미만에선 `kubectl apply` 가 `no matches for kind "PodDisruptionBudget" in version "policy/v1"` 로 실패한다. 지금은 **단일 인스턴스라 PDB 없이도 무방**하며, **k8s 1.21+ 로 마이그레이션할 때** 그대로 적용하면 된다(1.21 미만에서 꼭 쓰려면 `apiVersion: policy/v1beta1` 로만 바꿔도 동작).
> - **`deployment.yaml` 의 `securityContext.seccompProfile`(RuntimeDefault) 은 k8s 1.19+ 필드** — 1.14 에선 미지원이라 kubectl 검증에서 거부되거나 서버에서 드롭된다. 적용이 막히면 deployment 의 `seccompProfile:` 두 줄을 임시로 제거/주석(**1.19+ 로 올라가면 복원**). 1.19+ 에선 그대로 정상 적용.
> - 그 외(Deployment `apps/v1`, Service/ConfigMap/Secret `v1`, probe, `runAsNonRoot`/`readOnlyRootFilesystem`/`capabilities`/`fsGroup`/`preStop`)는 1.14 에서도 동작한다.

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
  # DB 격리: 0=WebManager / 5=ResourceMonitorServer(이 서비스) / 10=agent / 15=테스트
  # prefix 'RESOURCE_ALERT:' 는 belt-and-suspenders 이중 가드
  MONITOR_REDIS_KEY_PREFIX: "RESOURCE_ALERT"
  # (A) HA / Sentinel 모드 (운영 기본) — announce 파드 sentinel(26379) 목록 + master 그룹명.
  #     ⚠️ DB 는 URL 의 /N 이 아니라 MONITOR_REDIS_DB 로 지정한다(RMS 예약=5).
  MONITOR_REDIS_SENTINELS: "mdb-redis-ha-announce-0.ears-base.svc.cluster.local:26379,mdb-redis-ha-announce-1.ears-base.svc.cluster.local:26379,mdb-redis-ha-announce-2.ears-base.svc.cluster.local:26379"
  MONITOR_REDIS_SENTINEL_MASTER: "mymaster"
  MONITOR_REDIS_DB: "5"
  # (B) 단일 Redis(비-HA) — 위 Sentinel 키를 비우고 이 URL 만 쓴다. DB 는 URL 끝의 /5.
  # MONITOR_REDIS_URL: "redis://redis.cache:6379/5"

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
  # (선택) Sentinel 인증이 데이터 노드와 다를 때만. 비우면 REDIS_PASSWORD 재사용.
  # MONITOR_REDIS_SENTINEL_PASSWORD: ""

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
          # 버전 핀 — pyproject.toml 의 version(=빌드 산출 태그)과 일치시킬 것. 릴리스마다 갱신.
          # 빌드 스크립트는 :latest 를 만들지 않으므로 여기 태그가 load 한 이미지와 정확히 같아야 한다.
          image: resource-monitor-server:0.1.0
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

- 클러스터 내부용 `ClusterIP`, 포트 8000(`http`) 하나로 API·`/metrics`(§5.7)를 모두 노출한다.
  - probe(liveness/readiness)는 **kubelet이 Pod 에 직접** 접속하므로 이 Service 를 거치지 않는다(같은 8000 포트 사용).
- **WebManager 프록시 진입점**: WebManager 의 `RESOURCE_MONITOR_PROFILE`(모니터링 기준정보) 관리 기능은 **모든 쓰기와 `effective` 조회·readiness 체크를 이 Service 를 통해 RMS HTTP API 로 프록시**한다(`WebManager/server/features/rms-monitor-profile/rmsProfileClient.js`). RMS 의 Pydantic 검증(단일 진실)·`governance.version` 낙관락·TTLCache 무효화를 재사용하기 위함이며, WebManager 는 프로파일 컬렉션에 **직접 쓰지 않는다**(목록/scope옵션/inspect/blast-radius 등 일부 조회만 Mongo read-only).
  - WebManager 측 환경변수(**같은 네임스페이스**): `RMS_API_URL=http://resource-monitor-server:8000` (+ 선택 `RMS_API_TIMEOUT_MS=10000`). 크로스 네임스페이스면 `http://resource-monitor-server.<ns>.svc.cluster.local:8000`. ⚠️ 스킴 `http://` 필수. 미설정 시 클라이언트가 `503 RMS_NOT_CONFIGURED` 반환.
  - 호출 엔드포인트 — 읽기: `GET /profiles`, `GET /profiles/effective`, `GET /healthz/ready` · 쓰기: `POST/PUT/DELETE /profiles`, `.../measures*`, `.../rules*`, `PATCH /profiles/notify/{name}`.
- ⚠️ **무인증 → ClusterIP 유지**: RMS API 에는 인증이 없고 **WebManager 가 유일한 권한 게이트**다. 따라서 이 Service 는 `ClusterIP`(클러스터 내부 전용)로 유지하고 **외부(Ingress/LoadBalancer/NodePort)로 노출하지 말 것**. 또한 짧은 이름(`resource-monitor-server`) resolve 를 위해 WebManager 와 **같은 네임스페이스에 배포**한다.

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
- ⚠️ **k8s 버전**: `apiVersion: policy/v1` 은 **k8s 1.21+ 전용**이다. 1.21 미만(1.14 등)에서는 **적용하지 말 것**(§5 머리 호환 노트 참고) — 단일 인스턴스라 없어도 무방하며, **1.21+ 마이그레이션 시 도입**한다. (참고: `maxUnavailable: 0` 은 자발적 eviction 을 0 건 허용 = 노드 drain 시 이 파드 축출을 거부하므로, 점검 시 `kubectl drain --disable-eviction` 등으로 강제해야 한다.)

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
| `MONITOR_REDIS_SENTINELS` | 선택 | `[]` | 설정 시 **Sentinel 모드**. `host:26379` 콤마/JSON 목록(URL 대체) |
| `MONITOR_REDIS_SENTINEL_MASTER` | 선택 | `mymaster` | Sentinel master 그룹명 |
| `MONITOR_REDIS_SENTINEL_PASSWORD` | **Secret** | `""` | sentinel 인증(비우면 `REDIS_PASSWORD` 재사용) |
| `MONITOR_REDIS_DB` | 선택 | `0` | Sentinel 모드 DB 번호(RMS 예약=5) |
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
#    ⚠️ k8s <1.21 이면 pdb.yaml(policy/v1) 제외:
#       kubectl apply -f k8s/configmap.yaml -f k8s/deployment.yaml -f k8s/service.yaml
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
- **`[builder 1/N] FROM python:3.11-slim` 에서 멈춤** — 첫 단계는 베이스 이미지를
  **Docker Hub(`docker.io`)** 에서 받는 단계다. 폐쇄망이면 여기서 hang 된다. ⚠️ 주의:
  `--proxy`(build-arg)는 **컨테이너 안 RUN(apt·pip)** 에만 적용되고, `FROM` 의 베이스
  이미지 pull 은 **Docker 데몬**이 하므로 영향을 주지 않는다. 해결:
  ① (권장·오프라인) 인터넷 PC 에서 `docker pull python:3.11-slim && docker save python:3.11-slim -o python-3.11-slim.tar`
  → 빌드서버로 옮겨 `docker load -i python-3.11-slim.tar`, 그 후 `DOCKER_BUILDKIT=0 ./scripts/build-image.sh`
  (BuildKit 이 로컬 이미지를 두고도 레지스트리 메타데이터를 조회해 멈추면 레거시 빌더로 강제).
  ② 데몬 프록시: `/etc/systemd/system/docker.service.d/http-proxy.conf` 에 `HTTP(S)_PROXY` 설정 후 `systemctl daemon-reload && systemctl restart docker`.
  ③ 사내 Docker 레지스트리 미러(`/etc/docker/daemon.json` 의 `registry-mirrors`) 또는 `FROM <사내레지스트리>/python:3.11-slim` 로 변경.
- **`[builder N/M] RUN apt-get update` 가 `403 Forbidden [deb.debian.org]`** — 사내 프록시가
  공개 Debian 저장소를 막은 것. **현재 Dockerfile 은 apt/gcc 단계를 쓰지 않는다**(런타임
  의존성이 전부 cp311 manylinux 바이너리 휠 + slim 이미지엔 `python3-dev` 헤더도 없어 소스
  빌드 자체가 불가 → gcc 불필요). 구버전 Dockerfile 을 쓰고 있다면 해당 `RUN apt-get ...`
  단계를 삭제하면 된다. 정말 소스 컴파일이 필요한 의존성이 새로 생기면, **사내 Debian(apt)
  미러**로 `sources` 를 교체하고 `gcc`+`python3-dev` 를 설치하도록 단계를 되살릴 것.
- **`Could not find a version that satisfies ... (from versions: none)`** — pip 이 인덱스에서
  패키지를 못 찾음. 원인을 순서대로 확인한다(실제로 자주 겪는 순서):
  1. **URL 에 `/simple/` 누락** — pip 로그의 `Looking in indexes:` 줄을 보라. `.../repository/<repo>/`
     처럼 `/simple/` 없이 끝나면 잘못된 것(§3 참고). `--registry .../repository/<repo>/simple/` 로 수정.
  2. **프록시가 사내 Nexus 를 가로채 403** — `--proxy` 를 주면 http(s)_proxy 가 설정되는데,
     사내 Nexus 는 프록시를 타면 안 된다(외부 프록시가 내부 호스트를 `403 Forbidden` 으로 거부).
     **베이스 이미지는 오프라인 load, apt 는 제거된 지금, 빌드가 필요로 하는 네트워크는 사내
     Nexus(내부망 직결)뿐이므로 보통 `--proxy` 를 빼고 `--registry` 만 주면 된다.** 꼭 필요하면
     no_proxy 에 Nexus 호스트가 정확히 들어가야 한다. 빌드서버에서 누가 403 을 주는지 판별:
     `curl -ksS --noproxy '*' -o /dev/null -w '%{http_code}\n' "<index>/simple/setuptools/"`
     (이게 200 이면 범인은 프록시).
  3. **Nexus 가 인증 요구** — 위 `--noproxy` 직접 호출도 `401/403` 이고 `WWW-Authenticate: ... Nexus`
     가 보이면 익명 접근 비활성. `--registry https://USER:PASS@host/.../simple/` 또는 관리자에게
     anonymous read 허용 요청.
  4. **Nexus 가 해당 휠 미보유** — 위가 다 OK 인데 특정 패키지(특히 바이너리 휠
     `uvloop`/`hiredis`/`pymongo`/`aiohttp`/`watchfiles`/`httptools`)만 none → repo 가 hosted
     단독이거나 proxy 의 업스트림(pypi.org)이 막힌 것. **pypi proxy/group** repo 를 쓰고, proxy 면
     한 번 인터넷 경유로 캐시되게 한다.
- **`No matching distribution found for setuptools`** (RMS 본체 휠 빌드 중) — PEP 517 빌드 격리가
  `setuptools` 를 인덱스에서 받으려다 실패. **현재 Dockerfile 은 `pip wheel --no-build-isolation`**
  으로 베이스 이미지 번들 setuptools/wheel 을 써서 이 문제를 피한다. 구버전 Dockerfile 이면
  해당 플래그를 추가하거나 Nexus 에 `setuptools`/`wheel`/`pip` 가 있는지 확인.
- **태그 불일치** — 배포 노드의 이미지 태그가 `deployment.yaml` 의 `image:` 와 정확히
  같아야 한다. 스크립트는 `:{버전}` 단일 태그만 만들므로(`:latest` 없음), `deployment.yaml` 의
  `image:` 를 빌드한 버전과 정확히 일치시킬 것(불일치 시 `ImagePullBackOff`/`ErrImageNeverPull`).
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
