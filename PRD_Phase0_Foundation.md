# ResourceMonitorServer — Phase 0 PRD

## 기반 구축 (Foundation)

> 버전: 2.0
> 작성일: 2026-04-06 (최종 갱신: 2026-06-05)
> 상태: Draft
> 변경 이력:
> - v2.0: 기준정보 스키마 §5 재설계 — **단일 컬렉션**(measures/rules/notify 3계층) + scope **계층 상속(cascade)**. `RESOURCE_MONITOR_RULE` 별도 컬렉션 폐기. 권위 스펙은 [SCHEMA.md](SCHEMA.md). (§5.2/§5.3의 v1 JSON은 보존용)
> - v1.1: 스케일아웃 전략 추가 (Zookeeper, Redis, process 파티셔닝)
> - v1.0: 초안 작성

---

## 1. 개요

### 1.1 목적

공장 내 PC(최대 20,000대)에서 ResourceAgent가 수집하여 Elasticsearch에 저장된 리소스 메트릭 데이터를 주기적으로 분석하고, 이상 발생 시 기존 Email REST API를 통해 알림을 발송하는 모니터링 서비스의 기반을 구축한다.

### 1.2 배경

현재 파이프라인:

```
ResourceAgent (Go, 10,000~20,000대)
  → Kafka
    → Elasticsearch ({process}_all-yyyy.MM.dd)
      → Grafana (시각화)
```

데이터 수집과 시각화는 갖춰져 있으나, **자동 이상탐지 및 알림** 체계가 없다. 현재는 Grafana 대시보드를 사람이 직접 확인해야 이상을 인지할 수 있는 상태이다.

### 1.3 Phase 0 범위

Phase 0은 서비스의 골격을 만드는 단계이다. 이상탐지 로직이나 알림 발송은 Phase 1부터 구현하며, Phase 0에서는 다음을 확립한다:

- 프로젝트 구조 및 기술 스택 셋업
- Elasticsearch 연동 및 쿼리 빌더
- 메트릭 카탈로그 정의
- MongoDB 기준정보 스키마 설계 및 연동
- 설정 체계 (Pydantic + MongoDB)
- APScheduler 기반 스케줄러
- Email REST API 클라이언트 모듈
- 스케일아웃 기반 (Zookeeper 분산 락, Redis alert cooldown)
- K8s 배포 manifest 초안

### 1.4 Phase 전체 로드맵 (참고)

| Phase | 목표 | 핵심 가치 |
|-------|------|----------|
| **Phase 0** | **기반 구축** | 프로젝트 골격, ES 연동, 기준정보 스키마, 메트릭 카탈로그, 스케일아웃 기반 |
| Phase 1 | 기본 임계값 알림 | 즉시 가치: threshold 초과 → 이메일 + Grafana 링크 |
| Phase 2 | 통계 기반 이상탐지 | 정확도 향상: P95, spike, duration, z-score |
| Phase 3 | 복합 패턴 탐지 | 핵심 목표: 지표 조합 rule engine + baseline |
| Phase 4 | 운영 고도화 | 대규모 운영 안정성 + 리포트 자동화 |

> ⚠️ **v2 정정**: 위 로드맵의 "복합 판단=Phase3"은 v1 계획이다. **복합 조건(여러 condition을 `combine: AND/OR`로 묶는 rule)은 Phase 1에 이미 구현**되어 있다(`src/analyzer/threshold.py::evaluate_rule`). Phase 3가 추가하는 것은 baseline_dev fact 등 통계 알고리즘이지 "복합 vs 단순"의 구분이 아니다.

---

## 2. 기술 스택

| 영역 | 선택 | 이유 |
|------|------|------|
| 언어 | Python 3.11+ | scipy/numpy 통계 분석, ES 클라이언트 성숙도 |
| 프레임워크 | FastAPI | 비동기 지원, 향후 관리 API 확장 |
| ES 클라이언트 | elasticsearch-py | 공식 클라이언트, aggregation DSL |
| MongoDB 클라이언트 | motor (async) | 비동기 MongoDB 드라이버 |
| 스케줄러 | APScheduler | 주기적 분석 job (cron-like) |
| 분산 조정 | Zookeeper (kazoo) | 분산 락, 리더 선출, process 파티셔닝 (기존 인프라 활용) |
| 캐시 / 상태 | Redis (redis-py) | Alert cooldown TTL, 분석 상태 캐시 (기존 인프라 활용) |
| 설정 | Pydantic + MongoDB | 앱 설정은 Pydantic, 모니터링 기준정보는 MongoDB |
| 로깅 | structlog | 구조화 로깅 (JSON) |
| 배포 | K8s (Docker) | 기존 서버 인프라 통일 |

### 2.1 인프라 의존성

| 인프라 | 용도 | 비고 |
|--------|------|------|
| Elasticsearch | 메트릭 데이터 저장/조회 | 기존 운영 중 |
| MongoDB (EARS DB) | 기준정보 관리 | 기존 운영 중 |
| Kafka | 메트릭 데이터 수집 파이프라인 | 기존 운영 중 (본 서비스는 직접 사용 안 함) |
| Zookeeper | 분산 락 / 리더 선출 | 기존 운영 중 (Kafka용) |
| Redis | Alert cooldown / 상태 캐시 | 기존 운영 중 |
| Grafana | 대시보드 시각화 | 기존 운영 중 |
| HttpWebServer | Email REST API | 기존 운영 중 |

---

## 3. 프로젝트 구조

> ⚠️ **v2 정정**: 아래 트리의 Phase 표기는 v1 계획이다. as-built에서는 `analyzer/`(engine.py·threshold.py·fact_catalog.py·es_parser.py·alert_builder.py·metric_resolver.py, **복합 판단 포함**)와 `api/`(profiles.py·admin.py·metrics.py·health.py)가 **이미 구현완료**다. `rules/` 별도 디렉터리는 없다 — 단순·복합 판단은 모두 `analyzer/threshold.py::evaluate_rule`이 담당한다. 권위 있는 모듈 구성은 `src/` 실제 트리를 따른다.

```
resource-monitor-server/
├── src/
│   ├── main.py                        # FastAPI 앱 진입점
│   ├── config/
│   │   ├── settings.py                # Pydantic 앱 설정 (ES, MongoDB, ZK, Redis)
│   │   └── constants.py               # 메트릭 카탈로그 상수
│   ├── es/
│   │   ├── client.py                  # ES 연결 관리
│   │   └── queries.py                 # 재사용 가능한 쿼리 빌더
│   ├── db/
│   │   ├── client.py                  # MongoDB 연결 관리
│   │   ├── models.py                  # 기준정보 Pydantic 모델
│   │   └── repository.py             # 기준정보 CRUD
│   ├── distributed/
│   │   ├── zk_client.py               # Zookeeper 연결 및 래퍼
│   │   ├── leader_election.py         # 리더 선출
│   │   ├── partition_manager.py       # process 파티셔닝 관리
│   │   └── lock.py                    # 분산 락 유틸
│   ├── cache/
│   │   ├── redis_client.py            # Redis 연결 관리
│   │   └── cooldown.py                # Alert cooldown 관리
│   ├── analyzer/                      # Phase 1~2에서 구현
│   │   └── __init__.py
│   ├── rules/                         # Phase 3에서 구현
│   │   └── __init__.py
│   ├── alert/
│   │   ├── email_client.py            # Email REST API 호출
│   │   └── models.py                  # 알림 데이터 모델
│   ├── scheduler/
│   │   └── jobs.py                    # APScheduler job 정의
│   └── api/                           # Phase 4에서 확장
│       └── health.py                  # GET /health
├── tests/
│   ├── test_es_client.py
│   ├── test_queries.py
│   ├── test_db_repository.py
│   ├── test_email_client.py
│   ├── test_partition_manager.py
│   └── test_cooldown.py
├── k8s/
│   ├── deployment.yaml
│   ├── configmap.yaml
│   └── service.yaml
├── Dockerfile
└── pyproject.toml
```

---

## 4. 메트릭 카탈로그

EARS-METRICS-REFERENCE 기반으로 ES에 저장되는 전체 메트릭을 분석 대상 Tier로 분류한다.

### 4.1 Tier 분류 기준

| Tier | 정의 | 분석 시작 시점 |
|------|------|---------------|
| Tier 1 | 장비 영향이 직접적인 핵심 지표 | Phase 1 |
| Tier 2 | 패턴 분석이 의미 있는 보조 지표 | Phase 2 |
| Tier 3 | 장기 트렌드 / 자산 관리 성격 | Phase 3~4 |

### 4.2 시스템 메트릭 (proc=@system)

| Category | Metric | Tier | 설명 | 단위 | 분석 알고리즘 |
|----------|--------|------|------|------|--------------|
| cpu | total_used_pct | 1 | 전체 CPU 사용률 | % | P95, spike, duration, delta, z-score, baseline |
| cpu | core_{N}_used_pct | 3 | 코어별 사용률 | % | 특정 코어 과부하 시 참조 |
| memory | total_used_pct | 1 | 메모리 사용률 | % | moving avg, delta, z-score, baseline |
| memory | total_free_pct | 3 | 메모리 여유률 | % | total_used_pct의 보완 (별도 분석 불필요) |
| memory | total_used_size | 2 | 사용 메모리 크기 | bytes | growth rate (leak 탐지) |
| disk | {mountpoint} | 1 | 파티션 사용률 | % | 임계값, growth rate |
| network | all_inbound | 2 | 인바운드 TCP 커넥션 수 | 개 | spike, z-score |
| network | all_outbound | 2 | 아웃바운드 TCP 커넥션 수 | 개 | spike, z-score |
| network | recv_rate | 2 | NIC 수신 속도 | bytes/s | spike, baseline |
| network | sent_rate | 2 | NIC 송신 속도 | bytes/s | spike, baseline |
| temperature | {센서명} | 2 | CPU 온도 | °C | 임계값, 추세 |
| fan | {팬명} | 3 | 팬 속도 | RPM | 임계값 (저RPM 경고) |
| voltage | {센서명} | 3 | 전압 | V | 범위 이탈 |
| motherboard_temp | {센서명} | 3 | 메인보드 온도 | °C | 임계값 |
| gpu | {Name}_core_load | 2 | GPU 코어 사용률 | % | P95, spike |
| gpu | {Name}_memory_load | 2 | GPU 메모리 사용률 | % | 임계값 |
| gpu | {Name}_temperature | 2 | GPU 온도 | °C | 임계값 |
| gpu | {Name}_fan_speed | 3 | GPU 팬 속도 | RPM | 임계값 |
| gpu | {Name}_power | 3 | GPU 전력 | W | 참조 |
| gpu | {Name}_core_clock | 3 | GPU 코어 클럭 | MHz | 참조 |
| gpu | {Name}_memory_clock | 3 | GPU 메모리 클럭 | MHz | 참조 |
| storage_smart | {Name}_remaining_life | 2 | 잔여 수명 | % | 임계값 (낮으면 교체) |
| storage_smart | {Name}_temperature | 3 | 디스크 온도 | °C | 임계값 |
| storage_smart | {Name}_media_errors | 2 | 미디어 에러 수 | 개 | 증가 추세 |
| storage_smart | {Name}_power_cycles | 3 | 전원 사이클 | 회 | 참조 |
| storage_smart | {Name}_unsafe_shutdowns | 2 | 비정상 종료 | 회 | 증가 추세 |
| storage_smart | {Name}_power_on_hours | 3 | 총 가동 시간 | 시간 | 참조 |
| storage_smart | {Name}_total_bytes_written | 3 | 총 기록량 | bytes | 참조 |
| uptime | boot_time_unix | 3 | 부팅 시각 | unix ts | 참조 |
| uptime | uptime_minutes | 3 | 가동 시간 | 분 | 재부팅 탐지 |
| process_watch | required | 1 | 필수 프로세스 실행 여부 | 0/1 | value=0 시 즉시 알림 |
| process_watch | forbidden | 1 | 금지 프로세스 실행 여부 | 0/1 | value=1 시 즉시 알림 |

### 4.3 프로세스 메트릭 (proc={프로세스명})

| Category | Metric | Tier | 설명 | 단위 | 분석 알고리즘 |
|----------|--------|------|------|------|--------------|
| cpu | used_pct | 2 | 프로세스 CPU 사용률 | % | 특정 프로세스 과점유 탐지 |
| memory | used | 2 | 프로세스 RSS | bytes | growth rate (leak 탐지) |

### 4.4 분석 알고리즘 카탈로그

> ⚠️ **v2 정정**: 아래 v1 알고리즘 카탈로그(`threshold`/`percentile`/`state_check` 등 10종 + 임의 파라미터)는 **폐기**되었다. as-built의 권위 카탈로그는 **닫힌 `FactType` enum**(`src/analyzer/fact_catalog.py`, [SCHEMA.md §2](SCHEMA.md))이다 — Phase 1 구현완료: `max`/`min`/`avg`/`last`/`p50`·`p90`·`p95`·`p99`/`spike_count`. `state_check`은 별도 type가 없고 `min`/`max`+op 조건으로 흡수한다(required down=`min==0`, forbidden=`max>0`, health=`max>=2`). 아래 표는 v1 원안(보존용)이다.

각 메트릭에 적용 가능한 분석 알고리즘과 파라미터 정의:

| 알고리즘 | 코드명 | 설명 | 파라미터 | 적용 대상 |
|---------|--------|------|---------|----------|
| 단순 임계값 | `threshold` | 값이 기준 초과/미만 | warning, critical | 모든 % 기반 지표 |
| Percentile | `percentile` | P95/P99 상위 부하 | p95_warning, p99_warning | cpu, gpu load |
| Spike Count | `spike_count` | 급상승 횟수 | spike_threshold, spike_count_warning | cpu, network |
| Duration | `duration` | 고부하 지속시간 | threshold, duration_warning_sec | cpu, memory |
| Delta | `delta` | 변화량 | delta_warning | cpu, memory |
| Rate of Change | `growth_rate` | 증가 속도 | growth_rate_warning (단위/hour) | memory size, disk |
| Moving Average | `moving_avg` | 추세 분석 | moving_avg_window (데이터포인트 수) | memory (leak) |
| Z-score | `zscore` | 통계적 이상 | zscore_warning | cpu, network |
| Baseline Deviation | `baseline` | 정상 대비 이탈 | baseline_days, baseline_deviation_pct | 모든 지표 |
| 상태 체크 | `state_check` | 0/1 상태 확인 | expected_value | process_watch |

---

## 5. 기준정보 스키마 (MongoDB)

> 🟡 **v2 갱신 (2026-06-05)**: 모니터링 기준정보는 **단일 컬렉션 `RESOURCE_MONITOR_PROFILE`** 하나로 관리한다(measure·rule·notify 3계층). v1의 `RESOURCE_MONITOR_RULE` 별도 컬렉션은 **폐기**(단순/복합 판단을 한 컬렉션의 rule로 통합). **권위 있는 필드 스펙·type 카탈로그·검증 규칙·전체 예시는 [SCHEMA.md](SCHEMA.md)** 이며, 아래 §5.2/§5.3의 상세 JSON은 **v1 원안(보존용, 현행 설계 아님)** 이다. 결정 배경은 [SCHEMA.md §0](SCHEMA.md), 관리 UI/시인성 설계는 [docs/ADMIN-UI-LEGIBILITY.md](docs/ADMIN-UI-LEGIBILITY.md) 참고.

기존 EARS DB(MongoDB)에 `RESOURCE_MONITOR_PROFILE` 컬렉션 하나를 추가한다. (v1은 2개 컬렉션이었으나 v2에서 1개로 통합.)

### 5.1 적용 범위 (scope) 모델

모든 기준정보는 scope로 적용 대상을 지정한다. EQP_INFO의 계층 구조를 따른다:

```
전체 기본값 (process="*", model="*", eqpId="*")
  └─ process 단위 (process="PHOTO", model="*", eqpId="*")
      └─ model 단위 (process="PHOTO", model="MODEL_A", eqpId="*")
          └─ 개별 장비 (process="PHOTO", model="MODEL_A", eqpId="EQP001")
```

**우선순위**: eqpId 지정 > model 지정 > process 지정 > 전체 기본값 (가장 구체적인 scope가 이김)

> 🟡 **v2**: scope 해석은 v1의 "첫 매치 1개만 사용(replace)"이 아니라 **계층 상속(cascade)** 이다 — 넓은 scope를 base로 깔고 구체적 scope가 **바꿀 항목(measure/rule/notify)만 덮어쓰는** sparse overlay. 전역을 통째 복사할 필요가 없고, 전역 변경이 예외 장비에도 자동 전파된다. 상세 합성 규칙은 [SCHEMA.md §6](SCHEMA.md).

특정 장비 EQP001(process=PHOTO, model=MODEL_A)의 유효 설정은 아래를 **넓은 → 좁은 순으로 합성(fold)** 한 결과다:

1. `scope: { process: "*", model: "*", eqpId: "*" }` — 전역 base
2. `scope: { process: "PHOTO", model: "*", eqpId: "*" }` — process 레벨 덮어쓰기
3. `scope: { process: "PHOTO", model: "MODEL_A", eqpId: "*" }` — model 레벨 덮어쓰기
4. `scope: { process: "PHOTO", model: "MODEL_A", eqpId: "EQP001" }` — 장비 레벨 덮어쓰기(최우선)

### 5.2 RESOURCE_MONITOR_PROFILE

> 🟡 **v2 설계**: 문서 1개 = scope 1개. 내부는 3계층 — `measures[]`(잰다: category/metric/proc/window/facts), `rules[]`(판단: `measureId.type` 참조 + interval + severity + when), `notify{}`(알린다: cooldown/email_code). 단순 임계값은 조건 1개짜리 rule, 복합 조건은 조건 여러 개짜리 rule로 **통합** 표현한다. **전체 필드 레퍼런스·type 카탈로그·검증 규칙·예시는 [SCHEMA.md](SCHEMA.md)** 가 권위 문서다.
>
> ⚠️ **아래 JSON은 v1 원안(SUPERSEDED, 보존용)** 이다. v1은 한 문서에 `metrics[]` 배열 + 각 metric에 `analysis{}` 알고리즘 블록을 두는 형태였다. v2는 이를 measure(잰다)/rule(판단)로 분리하고 알고리즘을 `facts[].type`(`max`/`min`/`p95`/`spike_count`/`duration`/`zscore`/`baseline_dev` 등)으로 평탄화했다. **신규 설계는 아래 v1 JSON을 따르지 않는다.**

_(v1 원안 — 메트릭별 모니터링 설정. Phase 1~2.)_

```json
{
  "_id": ObjectId,
  "name": "PHOTO_MODEL_A_default",
  "description": "PHOTO 공정 MODEL_A 기본 모니터링 프로파일",
  
  "scope": {
    "process": "PHOTO",
    "model": "MODEL_A",
    "eqpId": "*"
  },
  
  "enabled": true,
  
  "metrics": [
    {
      "category": "cpu",
      "metric": "total_used_pct",
      "proc": "@system",
      "enabled": true,
      "tier": 1,
      
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 15
      },
      
      "thresholds": {
        "warning": 80,
        "critical": 95
      },
      
      "analysis": {
        "percentile": {
          "enabled": true,
          "p95_warning": 80,
          "p99_warning": 90
        },
        "spike_count": {
          "enabled": true,
          "spike_threshold": 90,
          "spike_count_warning": 5
        },
        "duration": {
          "enabled": true,
          "threshold": 80,
          "duration_warning_sec": 180
        },
        "delta": {
          "enabled": true,
          "delta_warning": 30
        },
        "zscore": {
          "enabled": true,
          "zscore_warning": 2.5
        },
        "moving_avg": {
          "enabled": false,
          "window": 6
        },
        "growth_rate": {
          "enabled": false
        },
        "baseline": {
          "enabled": true,
          "baseline_days": 7,
          "baseline_deviation_pct": 20
        }
      }
    },
    
    {
      "category": "memory",
      "metric": "total_used_pct",
      "proc": "@system",
      "enabled": true,
      "tier": 1,
      
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 15
      },
      
      "thresholds": {
        "warning": 85,
        "critical": 95
      },
      
      "analysis": {
        "percentile": {
          "enabled": false
        },
        "spike_count": {
          "enabled": false
        },
        "duration": {
          "enabled": true,
          "threshold": 85,
          "duration_warning_sec": 300
        },
        "delta": {
          "enabled": true,
          "delta_warning": 10
        },
        "zscore": {
          "enabled": true,
          "zscore_warning": 2.5
        },
        "moving_avg": {
          "enabled": true,
          "window": 6
        },
        "growth_rate": {
          "enabled": true,
          "rate_warning_mb_hour": 5
        },
        "baseline": {
          "enabled": true,
          "baseline_days": 7,
          "baseline_deviation_pct": 20
        }
      }
    },
    
    {
      "category": "memory",
      "metric": "total_used_size",
      "proc": "@system",
      "enabled": true,
      "tier": 2,
      
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 60
      },
      
      "thresholds": null,
      
      "analysis": {
        "growth_rate": {
          "enabled": true,
          "rate_warning_mb_hour": 50
        },
        "moving_avg": {
          "enabled": true,
          "window": 12
        }
      }
    },
    
    {
      "category": "disk",
      "metric": "*",
      "proc": "@system",
      "enabled": true,
      "tier": 1,
      
      "schedule": {
        "interval_minutes": 30,
        "window_minutes": 60
      },
      
      "thresholds": {
        "warning": 85,
        "critical": 95
      },
      
      "analysis": {
        "growth_rate": {
          "enabled": true,
          "rate_warning_pct_day": 5
        }
      }
    },
    
    {
      "category": "process_watch",
      "metric": "required",
      "proc": "*",
      "enabled": true,
      "tier": 1,
      
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 5
      },
      
      "thresholds": null,
      
      "analysis": {
        "state_check": {
          "enabled": true,
          "expected_value": 1,
          "alert_on_mismatch": true
        }
      }
    },
    
    {
      "category": "process_watch",
      "metric": "forbidden",
      "proc": "*",
      "enabled": true,
      "tier": 1,
      
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 5
      },
      
      "thresholds": null,
      
      "analysis": {
        "state_check": {
          "enabled": true,
          "expected_value": 0,
          "alert_on_mismatch": true
        }
      }
    },
    
    {
      "category": "temperature",
      "metric": "*",
      "proc": "@system",
      "enabled": true,
      "tier": 2,
      
      "schedule": {
        "interval_minutes": 10,
        "window_minutes": 30
      },
      
      "thresholds": {
        "warning": 80,
        "critical": 95
      },
      
      "analysis": {
        "moving_avg": {
          "enabled": true,
          "window": 6
        }
      }
    },
    
    {
      "category": "gpu",
      "metric": "*_core_load",
      "proc": "@system",
      "enabled": true,
      "tier": 2,
      
      "schedule": {
        "interval_minutes": 10,
        "window_minutes": 30
      },
      
      "thresholds": {
        "warning": 85,
        "critical": 95
      },
      
      "analysis": {
        "percentile": {
          "enabled": true,
          "p95_warning": 85
        },
        "spike_count": {
          "enabled": true,
          "spike_threshold": 95,
          "spike_count_warning": 3
        }
      }
    },
    
    {
      "category": "storage_smart",
      "metric": "*_remaining_life",
      "proc": "@system",
      "enabled": true,
      "tier": 2,
      
      "schedule": {
        "interval_minutes": 60,
        "window_minutes": 1440
      },
      
      "thresholds": {
        "warning": 20,
        "critical": 10
      },
      
      "analysis": null
    },
    
    {
      "category": "network",
      "metric": "recv_rate",
      "proc": "*",
      "enabled": true,
      "tier": 2,
      
      "schedule": {
        "interval_minutes": 5,
        "window_minutes": 15
      },
      
      "thresholds": null,
      
      "analysis": {
        "spike_count": {
          "enabled": true,
          "spike_threshold_bytes_sec": 100000000,
          "spike_count_warning": 5
        },
        "zscore": {
          "enabled": true,
          "zscore_warning": 3.0
        },
        "baseline": {
          "enabled": true,
          "baseline_days": 7,
          "baseline_deviation_pct": 50
        }
      }
    }
  ],
  
  "alert": {
    "cooldown_minutes": 30,
    "email_code": "RESOURCE_MONITOR",
    "email_subcode": "_"
  },
  
  "created_at": ISODate("2026-04-06T00:00:00Z"),
  "updated_at": ISODate("2026-04-06T00:00:00Z")
}
```

**인덱스**:

```javascript
db.RESOURCE_MONITOR_PROFILE.createIndex(
  { "scope.process": 1, "scope.model": 1, "scope.eqpId": 1 },
  { unique: true }
)
db.RESOURCE_MONITOR_PROFILE.createIndex({ "enabled": 1 })
```

**metric 항목의 wildcard 규칙**:
- `metric: "*"` — 해당 category의 모든 metric에 동일 설정 적용 (예: disk의 모든 마운트포인트)
- `metric: "*_core_load"` — 패턴 매칭 (예: GPU 이름 불특정)
- `proc: "*"` — 모든 proc에 적용 (예: process_watch의 모든 프로세스)

### 5.3 RESOURCE_MONITOR_RULE (v1, 폐기됨)

> ⚠️ **v2에서 이 별도 컬렉션은 폐기**되었다. 복합 조건 판단은 `RESOURCE_MONITOR_PROFILE` 문서 안의 `rules[]`(조건 여러 개 + `combine: AND/OR`)로 **통합**한다 — "단순 vs 복합"으로 컬렉션을 가르지 않는다. 폐기 배경은 [SCHEMA.md §0.1](SCHEMA.md).
>
> 아래 JSON은 **v1 원안(SUPERSEDED, 보존용)** 이다. 여기 `conditions[]`의 `analysis_type`/`analysis_field` 참조는 v2에서 `when[].fact = "measureId.type"` 로 대체되었다.

_(v1 원안 — 복합 조건 판단 규칙. Phase 3.)_

```json
{
  "_id": ObjectId,
  "name": "cpu_anomaly",
  "description": "CPU 복합 이상 — P95 과다 + 빈번한 spike + 장시간 지속",
  
  "scope": {
    "process": "*",
    "model": "*",
    "eqpId": "*"
  },
  
  "enabled": true,
  "severity": "CRITICAL",
  
  "conditions": [
    {
      "category": "cpu",
      "metric": "total_used_pct",
      "analysis_type": "percentile",
      "analysis_field": "p95",
      "operator": ">",
      "value": 80
    },
    {
      "category": "cpu",
      "metric": "total_used_pct",
      "analysis_type": "spike_count",
      "analysis_field": "count",
      "operator": ">",
      "value": 5
    },
    {
      "category": "cpu",
      "metric": "total_used_pct",
      "analysis_type": "duration",
      "analysis_field": "max_duration_sec",
      "operator": ">",
      "value": 180
    }
  ],
  "combine": "AND",
  
  "alert": {
    "cooldown_minutes": 60,
    "email_code": "RESOURCE_ANOMALY",
    "email_subcode": "CPU",
    "escalation": {
      "enabled": true,
      "repeat_count_to_escalate": 3,
      "escalate_to_severity": "CRITICAL"
    }
  },
  
  "created_at": ISODate("2026-04-06T00:00:00Z"),
  "updated_at": ISODate("2026-04-06T00:00:00Z")
}
```

**추가 Rule 예시 — Memory Leak**:

```json
{
  "name": "memory_leak",
  "description": "Memory Leak 의심 — 추세 증가 + 양수 delta + baseline 이탈",
  "scope": { "process": "*", "model": "*", "eqpId": "*" },
  "enabled": true,
  "severity": "CRITICAL",
  
  "conditions": [
    {
      "category": "memory",
      "metric": "total_used_pct",
      "analysis_type": "moving_avg",
      "analysis_field": "trend",
      "operator": "==",
      "value": "increasing"
    },
    {
      "category": "memory",
      "metric": "total_used_pct",
      "analysis_type": "delta",
      "analysis_field": "value",
      "operator": ">",
      "value": 0
    },
    {
      "category": "memory",
      "metric": "total_used_pct",
      "analysis_type": "baseline",
      "analysis_field": "deviation_pct",
      "operator": ">",
      "value": 20
    }
  ],
  "combine": "AND",
  
  "alert": {
    "cooldown_minutes": 60,
    "email_code": "RESOURCE_ANOMALY",
    "email_subcode": "MEMORY"
  }
}
```

**추가 Rule 예시 — Disk 문제**:

```json
{
  "name": "disk_issue",
  "description": "Disk 문제 — 용량 부족 또는 증가 추세",
  "scope": { "process": "*", "model": "*", "eqpId": "*" },
  "enabled": true,
  "severity": "WARNING",
  
  "conditions": [
    {
      "category": "disk",
      "metric": "*",
      "analysis_type": "threshold",
      "analysis_field": "max",
      "operator": ">",
      "value": 85
    },
    {
      "category": "disk",
      "metric": "*",
      "analysis_type": "growth_rate",
      "analysis_field": "rate_pct_day",
      "operator": ">",
      "value": 5
    }
  ],
  "combine": "OR",
  
  "alert": {
    "cooldown_minutes": 120,
    "email_code": "RESOURCE_ANOMALY",
    "email_subcode": "DISK"
  }
}
```

**인덱스**:

```javascript
db.RESOURCE_MONITOR_RULE.createIndex(
  { "scope.process": 1, "scope.model": 1, "scope.eqpId": 1 }
)
db.RESOURCE_MONITOR_RULE.createIndex({ "enabled": 1, "severity": 1 })
```

### 5.4 기존 컬렉션 활용

| 컬렉션 | 용도 | Phase |
|--------|------|-------|
| EQP_INFO | 장비 메타데이터 조회 (process, model, line, eqpId, ipAddr, emailcategory) | Phase 0 |
| EMAIL_TEMPLATE_REPOSITORY | 리소스 모니터링 알림 이메일 템플릿 등록 | Phase 1 |
| EMAIL_RECIPIENTS | 알림 수신 그룹 매핑 | Phase 1 |
| EMAILINFO | 이메일 수신자 목록 | Phase 1 |

---

## 6. 스케일아웃 전략

### 6.1 설계 원칙

- **Phase 0~1에서는 단일 인스턴스로 운영**하되, 다중 인스턴스 배포가 가능한 구조로 설계한다.
- 20,000대 규모 확장 시 replicas를 늘리는 것만으로 수평 확장이 가능해야 한다.
- 기존 인프라(Zookeeper, Redis, MongoDB)를 활용하여 추가 인프라 구축 없이 분산 처리를 지원한다.

### 6.2 Process 기반 파티셔닝

ES 인덱스가 process별로 분리되어 있으므로(`{process}_all-yyyy.MM.dd`), process가 자연스러운 파티션 단위이다.

```
인스턴스 A: process = [photo, cvd]      → photo_all-*, cvd_all-* 분석
인스턴스 B: process = [etch, diff]      → etch_all-*, diff_all-* 분석
인스턴스 C: process = [imp, metal]      → imp_all-*, metal_all-* 분석
```

**파티션 할당 방식**:

1. 모든 인스턴스가 시작 시 Zookeeper에 ephemeral sequential node를 생성한다.
2. 리더(가장 낮은 sequence)가 EQP_INFO에서 전체 process 목록을 조회한다.
3. 리더가 process 목록을 활성 인스턴스 수로 균등 분배하여 Zookeeper에 기록한다.
4. 각 인스턴스는 자신에게 할당된 process만 분석한다.
5. 인스턴스 추가/제거 시 Zookeeper watcher가 감지하여 자동 재분배한다.

**Zookeeper 노드 구조**:

```
/resource-monitor/
├── leader-election/           # 리더 선출용 ephemeral sequential
│   ├── instance-0000000001    # 인스턴스 A (리더)
│   ├── instance-0000000002    # 인스턴스 B
│   └── instance-0000000003    # 인스턴스 C
├── assignments/               # process 할당 정보 (리더가 기록)
│   ├── instance-A             # {"processes": ["photo", "cvd"]}
│   ├── instance-B             # {"processes": ["etch", "diff"]}
│   └── instance-C             # {"processes": ["imp", "metal"]}
└── locks/                     # 분석 job 실행 분산 락
    └── analysis-{process}     # process별 락
```

### 6.3 분산 락 (Zookeeper)

동일 process에 대한 분석 job이 여러 인스턴스에서 중복 실행되지 않도록 Zookeeper 분산 락을 사용한다.

```python
class ZKAnalysisLock:
    """Zookeeper 기반 분석 job 분산 락."""
    
    async def acquire(self, process: str, timeout_sec: int = 30) -> bool:
        """process별 분석 락을 획득한다.
        
        파티셔닝이 정상 동작하면 충돌이 드물다.
        이 락은 파티션 재분배 중간의 race condition 방어용이다.
        """
    
    async def release(self, process: str) -> None:
        """분석 완료 후 락을 해제한다."""
```

**사용 시나리오**:
- 정상 상황: 파티셔닝에 의해 각 인스턴스가 다른 process를 담당하므로 락 충돌 없음
- 재분배 중: 인스턴스 추가/제거로 파티션이 재분배되는 과도기에 중복 방지
- Failover: 인스턴스 장애 시 ephemeral node 삭제 → 다른 인스턴스가 해당 process 인수

### 6.4 Alert Cooldown (Redis)

> ⚠️ **v2 정정**: 아래 v1 키(`RESOURCE_ALERT:{eqpId}:{category}:{metric}`)와 `AlertCooldownManager(eqp_id, category, metric)` 시그니처는 **폐기**되었다. as-built cooldown 정체성은 **5차원 `(process, eqpId, proc, notify, severity)`** 이며 키는 `{prefix}:cooldown:{process}:{eqpId}:{proc}:{notify}:{severity}` 다(`src/cache/cooldown.py::AlertCooldownManager._make_key(process, eqp_id, proc, notify, severity)`, [SCHEMA.md §1.5](SCHEMA.md)). 억제 단위는 장비×EARS proc×notify 채널×severity라 같은 notify를 쓰는 rule은 한 incident로 합쳐지고 WARNING→CRITICAL 승급은 별도로 알린다. 아래 v1 내용은 보존용이다.

동일 장비 + 동일 지표에 대한 반복 알림을 방지하기 위해 Redis TTL 기반 cooldown을 사용한다.

**Key 형식**:

```
RESOURCE_ALERT:{eqpId}:{category}:{metric}
```

**예시**:

```
RESOURCE_ALERT:EQP001:cpu:total_used_pct → TTL 1800 (30분)
RESOURCE_ALERT:EQP002:disk:C: → TTL 7200 (2시간)
RESOURCE_ALERT:EQP003:process_watch:required:mes_client.exe → TTL 1800
```

**인터페이스**:

```python
class AlertCooldownManager:
    """Redis 기반 Alert cooldown 관리."""
    
    async def is_cooling_down(self, eqp_id: str, category: str, metric: str) -> bool:
        """해당 장비+지표가 cooldown 중인지 확인한다."""
    
    async def set_cooldown(self, eqp_id: str, category: str, metric: str, 
                           cooldown_minutes: int) -> None:
        """cooldown을 설정한다. TTL로 자동 만료."""
    
    async def clear_cooldown(self, eqp_id: str, category: str, metric: str) -> None:
        """수동으로 cooldown을 해제한다."""
    
    async def get_active_cooldowns(self, eqp_id: str | None = None) -> list[dict]:
        """활성 cooldown 목록을 조회한다. (관리 API용)"""
```

**Redis를 선택한 이유**:
- TTL 기반 key 만료가 정확함 (MongoDB TTL index는 60초 간격 체크로 부정확)
- 다중 인스턴스 간 공유 가능 (인메모리 dict는 인스턴스별 격리)
- 조회 성능이 빠름 (분석 주기마다 20,000대 × 다수 메트릭에 대해 cooldown 확인)

### 6.5 분석 상태 캐시 (Redis)

> ⚠️ **v2 정정**: 상태 캐시는 **Phase 2 항목**(아직 미구현)이다. 아래 v1 키(`RESOURCE_STATE:{eqpId}:{category}:{metric}`)는 cooldown과 동일하게 v2 식별 단위 `(process, eqpId, proc, ...)` 기준으로 재설계 예정이며, `{eqpId}:{category}:{metric}` 형식은 채택하지 않는다(§6.4 참조).

분석 결과의 중간 상태를 Redis에 캐싱하여 인스턴스 간 공유 및 재시작 시 복구에 활용한다.

**Key 형식**:

```
RESOURCE_STATE:{eqpId}:{category}:{metric}
```

**저장 데이터 (Phase 2~)**:

```json
{
  "last_analysis_at": "2026-04-06T10:30:00Z",
  "last_value": 85.5,
  "moving_avg": 78.2,
  "trend": "increasing",
  "consecutive_warnings": 3
}
```

Phase 0에서는 Redis 연결 및 기본 get/set만 구현하고, 실제 상태 저장은 Phase 2에서 구현한다.

### 6.6 단일 인스턴스 동작 모드

replicas=1일 때는 파티셔닝 없이 모든 process를 하나의 인스턴스가 처리한다. Zookeeper 리더 선출에서 자동으로 유일한 리더가 되어 전체 process를 할당받는다. 코드 경로는 다중 인스턴스와 동일하여 별도 분기가 없다.

### 6.7 스케일아웃 시나리오

**시나리오 1: 10,000대 → 20,000대 확장**

```
기존: replicas=1, 전체 process 처리
확장: replicas=2~3으로 변경
→ 새 인스턴스가 ZK에 등록
→ 리더가 process 재분배
→ 각 인스턴스가 할당된 process만 분석
```

**시나리오 2: 인스턴스 장애**

```
인스턴스 B 장애 → ZK ephemeral node 삭제
→ 리더가 감지, process 재분배
→ 인스턴스 A, C가 B의 process를 인수
→ K8s가 B를 재시작 → 다시 ZK 등록 → 재분배
```

**시나리오 3: 리더 장애**

```
인스턴스 A(리더) 장애 → ZK ephemeral node 삭제
→ 다음 sequence의 인스턴스 B가 자동으로 리더 승계
→ B가 process 재분배 수행
```

---

## 7. Elasticsearch 연동

### 7.1 인덱스 패턴

```
{process}_all-yyyy.MM.dd
```

- `process`: EQP_INFO의 process 필드 (소문자)
- 예: `photo_all-2026.04.06`, `cvd_all-2026.04.06`
- 분석 대상 process 목록은 Zookeeper 파티셔닝으로 결정

### 7.2 Document 필드 매핑

| ES 필드 | 타입 | 역할 | 쿼리 활용 |
|---------|------|------|----------|
| EARS_CATEGORY | keyword | 리소스 종류 | filter |
| EARS_METRIC | keyword | 세부 지표명 | filter |
| EARS_VALUE | float | 수치값 | aggregation 대상 |
| EARS_EQPID | keyword | 장비 ID | group by |
| EARS_MODEL | keyword | 장비 모델 | group by (baseline) |
| EARS_LINE | keyword | 라인 | group by (알림 그룹) |
| EARS_PROCNAME | keyword | @system 또는 프로세스명 | filter |
| EARS_PID | long | 프로세스 ID | filter |
| EARS_FILENAME | keyword | 데이터 소스 | filter (resource) |
| EARS_TIMESTAMP | date | 에이전트 수집 시각 | time range |

### 7.3 쿼리 빌더 설계

> ⚠️ **v2 정정**: 실제 EARS row의 문자열 필드(`EARS_EQPID`/`EARS_METRIC`/`EARS_MODEL` 등)는 전부 **bare keyword** 로 매핑되어 있어 `.keyword` 서브필드가 **없다**. term 필터·terms 집계는 모두 **bare 필드명**을 쓴다([SCHEMA.md §8.1](SCHEMA.md), `src/es/queries.py`). 아래 패턴의 `EARS_*.keyword`는 v1 오기이며, as-built 빌더는 `.keyword` 없이 동작한다. (정정 반영: 아래 JSON에서 `.keyword` 제거)

Phase 1~2에서 사용할 쿼리 패턴을 미리 정의하고, 재사용 가능한 빌더로 추상화한다.

**기본 필터 조합**:

```python
def build_base_filter(
    category: str,
    metric: str,
    proc: str,
    time_range_minutes: int,
    eqp_ids: list[str] | None = None
) -> dict:
    """모든 분석 쿼리의 공통 필터를 생성한다."""
```

**패턴 1: 장비별 기본 통계** (Phase 1)

```json
{
  "query": { "bool": { "filter": [/* base filters */] } },
  "size": 0,
  "aggs": {
    "by_eqp": {
      "terms": { "field": "EARS_EQPID", "size": 20000 },
      "aggs": {
        "stats": { "extended_stats": { "field": "EARS_VALUE" } },
        "max_val": { "max": { "field": "EARS_VALUE" } }
      }
    }
  }
}
```

**패턴 2: 장비별 통계 + percentile + spike count** (Phase 2)

```json
{
  "query": { "bool": { "filter": [/* base filters */] } },
  "size": 0,
  "aggs": {
    "by_eqp": {
      "terms": { "field": "EARS_EQPID", "size": 20000 },
      "aggs": {
        "p95": { "percentiles": { "field": "EARS_VALUE", "percents": [95, 99] } },
        "stats": { "extended_stats": { "field": "EARS_VALUE" } },
        "max_val": { "max": { "field": "EARS_VALUE" } },
        "spike_count": {
          "filter": { "range": { "EARS_VALUE": { "gte": 90 } } }
        }
      }
    }
  }
}
```

**패턴 3: 시계열 데이터 (duration/delta 분석용)** (Phase 2)

```json
{
  "query": { "bool": { "filter": [/* base filters + 특정 eqpId */] } },
  "size": 1000,
  "sort": [{ "EARS_TIMESTAMP": "asc" }],
  "_source": ["EARS_VALUE", "EARS_TIMESTAMP", "EARS_EQPID"]
}
```

**패턴 4: Baseline 계산 (과거 동일 시간대)** (Phase 3)

```json
{
  "query": {
    "bool": {
      "filter": [
        /* base filters */,
        { "range": { "EARS_TIMESTAMP": { "gte": "now-7d" } } },
        { "script": {
            "script": "doc['EARS_TIMESTAMP'].value.getHour() >= params.hour_start && doc['EARS_TIMESTAMP'].value.getHour() < params.hour_end",
            "params": { "hour_start": 9, "hour_end": 10 }
        }}
      ]
    }
  },
  "size": 0,
  "aggs": {
    "by_model": {
      "terms": { "field": "EARS_MODEL" },
      "aggs": {
        "stats": { "extended_stats": { "field": "EARS_VALUE" } },
        "percentiles": { "percentiles": { "field": "EARS_VALUE", "percents": [50, 95] } }
      }
    }
  }
}
```

### 7.4 쿼리 빌더 인터페이스

```python
class ESQueryBuilder:
    """ES 집계 쿼리를 생성하는 빌더."""
    
    def build_stats_query(
        self,
        category: str,
        metric: str,
        proc: str,
        window_minutes: int,
        include_percentiles: bool = False,
        spike_threshold: float | None = None,
    ) -> dict:
        """장비별 통계 집계 쿼리를 생성한다. (패턴 1, 2)"""
    
    def build_timeseries_query(
        self,
        category: str,
        metric: str,
        proc: str,
        window_minutes: int,
        eqp_ids: list[str],
        max_docs: int = 1000,
    ) -> dict:
        """시계열 raw 데이터 조회 쿼리를 생성한다. (패턴 3)"""
    
    def build_baseline_query(
        self,
        category: str,
        metric: str,
        proc: str,
        baseline_days: int,
        hour_of_day: int,
    ) -> dict:
        """baseline 통계 집계 쿼리를 생성한다. (패턴 4)"""
    
    def resolve_index(
        self,
        process: str,
        date: datetime | None = None,
    ) -> str:
        """process로 ES 인덱스 이름을 결정한다."""
```

---

## 8. Email REST API 클라이언트

### 8.1 기존 API 스펙 요약

```
POST /EmailNotify
Host: <HttpWebServerAddress>
Content-Type: application/json
```

Request Body (`EmailHttpDataFormat`):

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| hostname | String | Yes | 장비 ID |
| ip | String | Yes | 장비 IP |
| app | String | Yes | 애플리케이션 (ARS) |
| process | String | Yes | 공정명 |
| model | String | Yes | 장비 모델 |
| line | String | Yes | 라인 |
| code | String | Yes | 이메일 트리거 코드 |
| subcode | String | Yes | 서브코드 (없으면 "_") |
| variables | Map | No | 템플릿 변수 치환용 |

### 8.2 리소스 모니터링용 코드 체계

> ⚠️ **v2 정정**: 아래 표의 "복합 이상=별도 코드(`RESOURCE_ANOMALY`)/Phase 3" 구분은 v1 계획이다. as-built에서는 email `code`/`subcode`가 rule이 참조하는 **notify 채널**에서 나온다 — `code = notify.email_code`(기본 `RESOURCE_MONITOR`), `subcode = notify.email_subcode or "{CATEGORY}_{SEVERITY}"`(`src/analyzer/alert_builder.py`). 단순/복합을 코드로 가르지 않으며, 복합 rule도 동일 `RESOURCE_MONITOR` 경로로 발송된다.

| code | subcode | 용도 | Phase |
|------|---------|------|-------|
| RESOURCE_MONITOR | CPU | CPU 임계값 알림 | Phase 1 |
| RESOURCE_MONITOR | MEMORY | Memory 임계값 알림 | Phase 1 |
| RESOURCE_MONITOR | DISK | Disk 임계값 알림 | Phase 1 |
| RESOURCE_MONITOR | PROCESS | 프로세스 상태 알림 | Phase 1 |
| RESOURCE_ANOMALY | CPU | CPU 복합 이상 | Phase 3 |
| RESOURCE_ANOMALY | MEMORY | Memory Leak | Phase 3 |
| RESOURCE_ANOMALY | DISK | Disk 복합 이상 | Phase 3 |
| RESOURCE_REPORT | DAILY | 일간 리포트 | Phase 4 |
| RESOURCE_REPORT | WEEKLY | 주간 리포트 | Phase 4 |

### 8.3 variables 정의 (템플릿 치환용)

Phase 1에서 사용할 기본 variables:

| Key | 설명 | 예시 |
|-----|------|------|
| METRIC_CATEGORY | 리소스 종류 | CPU |
| METRIC_NAME | 지표명 | total_used_pct |
| CURRENT_VALUE | 현재값 | 92.5 |
| THRESHOLD_WARNING | 경고 기준 | 80 |
| THRESHOLD_CRITICAL | 위험 기준 | 95 |
| SEVERITY | 알림 등급 | WARNING |
| GRAFANA_LINK | Grafana 대시보드 딥링크 | https://grafana.../d/xxx?var-eqpId=EQP001&from=... |
| ANALYSIS_WINDOW | 분석 기간 | 최근 15분 |
| ALERT_TIME | 알림 발생 시각 | 2026-04-06 10:30:00 |

### 8.4 클라이언트 인터페이스

```python
class EmailAlertClient:
    """기존 Email REST API를 호출하는 클라이언트."""
    
    async def send_alert(
        self,
        eqp_id: str,
        ip: str,
        process: str,
        model: str,
        line: str,
        code: str,
        subcode: str,
        variables: dict[str, str],
    ) -> bool:
        """이메일 알림을 전송한다. 성공 시 True."""
    
    async def health_check(self) -> bool:
        """Email API 서버 연결을 확인한다."""
```

---

## 9. 스케줄러

> ⚠️ **v2 정정** (§9.1~9.3 전체): as-built 스케줄러는 **metric-centric이 아니라 `(process, interval)` 단위**다(`src/scheduler/jobs.py`). job 키는 `analysis-{process}-{interval}m`이고, **interval은 rule이 소유**한다(measure는 window만 가짐) — `metrics`를 순회하며 `interval_minutes`로 그룹핑하지 않고, process의 effective 프로파일에서 `{r.interval_minutes for r in profile.rules}`를 모아 interval별 job을 등록한다. interval override는 **process 레벨까지만** 허용된다. 또한 §9.3의 "Phase 0 no-op stub"은 옛 상태이며, 실제 분석 엔진(`AnalysisEngine.run_analysis(process, interval_minutes)`, 장비별 effective resolve + signature 버킷팅 + measure→fact→rule→cooldown→notify)이 **Phase 1에 구현완료**다. 아래 §9.1~9.3 본문은 v1 원안(보존용)이다.

### 9.1 설계

APScheduler를 사용하여 메트릭별 다른 주기로 분석 job을 실행한다. 각 인스턴스는 Zookeeper 파티셔닝으로 할당받은 process에 대해서만 job을 실행한다.

```python
class AnalysisScheduler:
    """MongoDB RESOURCE_MONITOR_PROFILE 기반으로 분석 job을 스케줄링한다."""
    
    def __init__(self, partition_manager: PartitionManager):
        """파티션 매니저를 주입받아 담당 process 범위를 결정한다."""
    
    async def start(self):
        """프로파일을 로드하고, 담당 process의 메트릭별 주기에 따라 job을 등록한다."""
    
    async def reload(self):
        """프로파일 변경 또는 파티션 재분배 시 job을 재등록한다."""
    
    async def stop(self):
        """스케줄러를 종료한다."""
```

### 9.2 Job 등록 로직

1. Zookeeper에서 자신에게 할당된 process 목록을 확인한다.
2. MongoDB에서 해당 process에 매칭되는 enabled=true인 RESOURCE_MONITOR_PROFILE을 로드한다.
3. 각 프로파일의 metrics를 순회하며 `interval_minutes` 별로 그룹핑한다.
4. 동일 interval의 메트릭은 하나의 job으로 묶어 ES 쿼리 횟수를 최적화한다.
5. APScheduler IntervalTrigger로 job을 등록한다.
6. job 실행 시 Zookeeper 분산 락을 획득한 후 진행한다.

### 9.3 Phase 0에서의 동작

Phase 0에서는 분석 로직 없이 **ES 쿼리 실행 → 결과 로그 출력**만 수행한다.

```python
async def analysis_job(profile, metrics, partition_manager, zk_lock):
    """Phase 0: ES 쿼리 실행 후 결과를 로그로 출력한다."""
    process = profile.scope.process
    
    if not await zk_lock.acquire(process):
        logger.warn("lock_failed", process=process)
        return
    
    try:
        for metric_config in metrics:
            index = query_builder.resolve_index(process)
            query = query_builder.build_stats_query(...)
            result = await es_client.search(index=index, body=query)
            logger.info("query_result", 
                        process=process,
                        category=metric_config.category, 
                        metric=metric_config.metric, 
                        hit_count=..., 
                        eqp_count=...)
    finally:
        await zk_lock.release(process)
```

### 9.4 파티션 변경 시 스케줄러 재로드

Zookeeper watcher가 파티션 할당 변경을 감지하면, 스케줄러의 `reload()`를 호출하여 job을 재등록한다.

```python
class PartitionManager:
    """Zookeeper 기반 process 파티셔닝 관리."""
    
    def __init__(self, zk_client, on_partition_change: Callable):
        """파티션 변경 시 호출될 콜백(스케줄러 reload)을 등록한다."""
    
    async def get_my_processes(self) -> list[str]:
        """현재 인스턴스에 할당된 process 목록을 반환한다."""
    
    async def start(self):
        """ZK에 등록하고 리더 선출 / 파티션 할당을 시작한다."""
    
    async def stop(self):
        """ZK에서 등록 해제한다."""
```

---

## 10. 앱 설정 (settings.py)

MongoDB에 저장하지 않는 인프라/연결 설정은 환경변수 + Pydantic Settings로 관리한다.

```python
class AppSettings(BaseSettings):
    """앱 인프라 설정. 환경변수 또는 .env에서 로드."""
    
    # Elasticsearch
    es_hosts: list[str] = ["http://es-cluster:9200"]
    es_request_timeout: int = 30
    es_max_retries: int = 3
    
    # MongoDB
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "EARS"
    
    # Zookeeper
    zk_hosts: str = "zk1:2181,zk2:2181,zk3:2181"
    zk_root_path: str = "/resource-monitor"
    zk_session_timeout: int = 30
    
    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_key_prefix: str = "RESOURCE_ALERT"
    
    # Email API
    email_api_url: str = "http://httpwebserver:8080/EmailNotify"
    email_api_timeout: int = 10
    
    # Grafana
    grafana_base_url: str = "http://grafana:3000"
    grafana_dashboard_uid: str = ""
    
    # Scheduler
    scheduler_misfire_grace_time: int = 60
    
    # Instance
    instance_id: str = ""  # 비어있으면 hostname 사용
    
    # 로깅
    log_level: str = "INFO"
    log_format: str = "json"
    
    class Config:
        env_prefix = "MONITOR_"
```

> **참고**: 기존 v1.0에서 `target_processes`를 설정으로 관리했으나, v1.1에서 Zookeeper 파티셔닝으로 대체되었다. 분석 대상 process 목록은 EQP_INFO에서 동적으로 조회하며, 파티셔닝을 통해 각 인스턴스에 자동 할당된다.

---

## 11. K8s 배포

### 11.1 deployment.yaml

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: resource-monitor-server
  labels:
    app: resource-monitor-server
spec:
  replicas: 1    # 초기 1대, 스케일아웃 시 2~3으로 조정
  selector:
    matchLabels:
      app: resource-monitor-server
  template:
    metadata:
      labels:
        app: resource-monitor-server
    spec:
      containers:
        - name: monitoring
          image: resource-monitor-server:latest
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: resource-monitor-config
          env:
            - name: MONITOR_INSTANCE_ID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name    # Pod 이름을 인스턴스 ID로 사용
          resources:
            requests:
              memory: "256Mi"
              cpu: "100m"
            limits:
              memory: "512Mi"
              cpu: "500m"
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
```

### 11.2 configmap.yaml

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: resource-monitor-config
data:
  MONITOR_ES_HOSTS: '["http://es-cluster:9200"]'
  MONITOR_MONGO_URI: "mongodb://mongodb:27017"
  MONITOR_MONGO_DB: "EARS"
  MONITOR_ZK_HOSTS: "zk1:2181,zk2:2181,zk3:2181"
  MONITOR_ZK_ROOT_PATH: "/resource-monitor"
  MONITOR_REDIS_URL: "redis://redis:6379/0"
  MONITOR_EMAIL_API_URL: "http://httpwebserver:8080/EmailNotify"
  MONITOR_GRAFANA_BASE_URL: "http://grafana:3000"
  MONITOR_GRAFANA_DASHBOARD_UID: ""
  MONITOR_LOG_LEVEL: "INFO"
```

### 11.3 service.yaml

```yaml
apiVersion: v1
kind: Service
metadata:
  name: resource-monitor-server
spec:
  selector:
    app: resource-monitor-server
  ports:
    - port: 8000
      targetPort: 8000
  type: ClusterIP
```

---

## 12. API 엔드포인트 (Phase 0)

Phase 0에서는 최소한의 API만 노출한다.

| Method | Path | 설명 |
|--------|------|------|
| GET | /health | 서비스 상태 (모든 인프라 연결 + 파티션 정보 포함) |

```python
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "instance_id": settings.instance_id,
        "elasticsearch": await es_client.ping(),
        "mongodb": await db_client.ping(),
        "zookeeper": zk_client.is_connected(),
        "redis": await redis_client.ping(),
        "email_api": await email_client.health_check(),
        "scheduler": scheduler.is_running(),
        "partition": {
            "is_leader": partition_manager.is_leader(),
            "assigned_processes": await partition_manager.get_my_processes(),
            "total_instances": partition_manager.get_instance_count(),
        },
        "version": "0.1.0"
    }
```

---

## 13. 완료 기준

| # | 항목 | 검증 방법 |
|---|------|----------|
| 1 | 프로젝트 빌드 및 Docker 이미지 생성 | `docker build` 성공 |
| 2 | ES 연결 및 쿼리 실행 | `/health` 에서 elasticsearch: true, 쿼리 결과 로그 출력 |
| 3 | MongoDB 연결 및 기준정보 CRUD | RESOURCE_MONITOR_PROFILE 생성/조회/수정/삭제 |
| 4 | 메트릭 카탈로그 기본 프로파일 등록 | 전체 기본값("*") 프로파일 1건 이상 MongoDB에 등록 |
| 5 | Zookeeper 연결 및 리더 선출 | `/health` 에서 zookeeper: true, is_leader: true (단일 인스턴스) |
| 6 | Redis 연결 및 cooldown 동작 | `/health` 에서 redis: true, cooldown set/get 테스트 |
| 7 | 스케줄러 동작 | 설정된 주기로 ES 쿼리 실행 → 결과 로그 확인 |
| 8 | Email API 클라이언트 연결 확인 | `/health` 에서 email_api: true |
| 9 | K8s 배포 | deployment 적용 → pod running → /health 200 |
| 10 | 파티셔닝 동작 (선택) | replicas=2로 변경 → 각 인스턴스의 assigned_processes가 분배됨 확인 |

---

## 14. Phase 1 선행 조건 (Phase 0 산출물 → Phase 1 입력)

Phase 0 완료 후, Phase 1 착수 전에 추가로 필요한 작업:

| 항목 | 담당 | 설명 |
|------|------|------|
| EMAIL_TEMPLATE_REPOSITORY 등록 | 운영팀/개발팀 | RESOURCE_MONITOR 코드에 대한 HTML 템플릿 작성 |
| EMAIL_RECIPIENTS 등록 | 운영팀 | 리소스 모니터링 알림 수신 그룹 매핑 |
| Grafana 대시보드 UID 확정 | 인프라팀 | 이메일 딥링크용 대시보드 식별자 |
| 기본 프로파일 임계값 튜닝 | 개발팀/운영팀 | 실 데이터 기반 warning/critical 값 조정 |

---

## 15. 리스크 및 고려사항

| 리스크 | 영향 | 대응 |
|--------|------|------|
| ES 쿼리 부하 | 20,000대 × 다수 메트릭 → ES 부하 | process 파티셔닝으로 분산, 동일 interval 메트릭을 하나의 쿼리로 묶기, 분석 시간 분산 |
| MongoDB 기준정보 동기화 | 프로파일 변경 시 스케줄러 반영 지연 | 변경 감지 주기 설정 (1분), 수동 reload API 제공 |
| scope 우선순위 복잡도 | 장비별 프로파일 결정 로직이 복잡해질 수 있음 | 우선순위 규칙 명확히 문서화, 캐싱으로 반복 계산 방지 |
| metric wildcard 매칭 | `*_core_load` 같은 패턴 매칭 구현 복잡도 | Python fnmatch 활용, 단순 패턴만 지원 |
| Zookeeper 세션 만료 | 네트워크 불안정 시 파티션 재분배 빈발 | session timeout 충분히 설정 (30초), 재연결 로직 |
| Redis 장애 | cooldown 기능 불가 → 중복 알림 가능 | Redis 장애 시 인메모리 fallback (degraded mode) |
| 파티션 재분배 중 분석 갭 | 재분배 동안 일부 process의 분석이 지연 | 짧은 재분배 시간, 다음 주기에 자동 복구 |
| 분석 대상 process 누락 | 신규 process 추가 시 파티셔닝 미반영 | EQP_INFO의 process 목록을 주기적 동기화 (리더가 수행) |
