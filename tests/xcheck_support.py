"""SCHEMA §5 검증 3-way 대조 — 공유 지원 모듈.

`tests/data/schema_cases.json`(SCHEMA를 인용한 정답지)을 로드하고, op 결정테이블
(type × op)을 전개해 전체 케이스 목록을 만든다. 백엔드 판정(`backend_verdict`)도
여기 둬서 백엔드 러너(test_schema_cases_xcheck.py)와 리포트 빌더가 함께 쓴다.

설계: docs/superpowers/specs/2026-06-07-schema-validation-cross-check-design.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).parent / "data" / "schema_cases.json"

_OP_SLUG = {">=": "ge", ">": "gt", "<=": "le", "<": "lt", "==": "eq", "!=": "ne"}


def load_raw() -> dict[str, Any]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def _make_fact(ftype: str) -> dict[str, Any]:
    """op 매트릭스용 fact 한 개. spike_count는 필수 param(over/direction)을 채운다."""
    if ftype == "spike_count":
        return {"type": "spike_count", "over": 90, "direction": "above"}
    return {"type": ftype}


def _op_matrix_cases(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """§5.5 type↔op 적합성 결정테이블 — phase1 fact × ops_tested 전수 전개.

    expected는 백엔드 ALLOWED_OPS가 아니라 meta.allowed_by_schema(SCHEMA §2 인용)에서
    독립적으로 계산한다 — 그래야 백엔드와 별개 oracle이 된다.
    """
    allowed = meta["allowed_by_schema"]
    ops = meta["ops_tested"]
    pg = set(meta["playground_fact_support"])
    out: list[dict[str, Any]] = []
    for ftype in meta["phase1_fact_types"]:
        allow = set(allowed[ftype])
        for op in ops:
            value = 5 if ftype == "spike_count" else 80
            expected = "valid" if op in allow else "invalid"
            out.append(
                {
                    "id": f"opm_{ftype}_{_OP_SLUG[op]}",
                    "ref": "§5.5",
                    "desc": f"{ftype} 에 비교 연산자 '{op}'",
                    "expected": expected,
                    "expected_violations": [] if expected == "valid" else ["§5.5"],
                    "playground_supports": ftype in pg,
                    "profile": {
                        "measures": [
                            {
                                "id": "m",
                                "category": "cpu",
                                "metric": "total_used_pct",
                                "proc": "@system",
                                "window_minutes": 15,
                                "facts": [_make_fact(ftype)],
                            }
                        ],
                        "rules": [
                            {
                                "id": "r",
                                "interval_minutes": 5,
                                "severity": "WARNING",
                                "when": [{"fact": f"m.{ftype}", "op": op, "value": value}],
                                "notify": "default",
                            }
                        ],
                    },
                }
            )
    return out


def expand_cases() -> list[dict[str, Any]]:
    """명시적 cases + op 매트릭스 전개를 합친 전체 케이스 목록."""
    raw = load_raw()
    cases = list(raw["cases"])
    cases.extend(_op_matrix_cases(raw["_meta"]))
    return cases


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """케이스 profile에 scope/enabled/notify 기본값을 주입(케이스는 measures/rules에 집중).

    scope는 Scope 모델의 alias 형식(model/eqpId)을 쓴다. notify를 명시한 케이스는
    그대로 두므로 notify_missing 케이스(rule.notify='ghost')는 default만 주입돼 §5.7로 잡힌다.
    """
    p = json.loads(json.dumps(profile))  # deep copy
    p.setdefault("scope", {"process": "*", "model": "*", "eqpId": "*"})
    p.setdefault("enabled", True)
    p.setdefault(
        "notify", {"default": {"cooldown_minutes": 30, "email_code": "RESOURCE_MONITOR"}}
    )
    return p


def backend_verdict(profile: dict[str, Any]) -> str:
    """백엔드(Pydantic 구조 검증 + validate_effective) 판정 → 'valid' | 'invalid'.

    1) MonitorProfile(**p) 생성 시 예외 → 구조 검증 거부(§5.1~5.4·Fact·Condition param)
    2) validate_effective 결과가 비어있지 않으면 → 참조 무결성 거부(§5.5·5.6·5.7·5.8)
    """
    from src.db.models import MonitorProfile, validate_effective

    p = normalize_profile(profile)
    try:
        prof = MonitorProfile(**p)
    except Exception:
        return "invalid"
    return "invalid" if validate_effective(prof) else "valid"
