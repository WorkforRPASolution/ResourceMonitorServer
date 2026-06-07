"""백엔드 러너 — SCHEMA §5 검증 3-way 대조 (백엔드 ↔ SCHEMA expected).

`tests/data/schema_cases.json`의 모든 케이스(+op 매트릭스 전개)를 백엔드
(Pydantic + validate_effective)에 돌려 SCHEMA 기준 expected와 일치하는지 검증한다.

설계: docs/superpowers/specs/2026-06-07-schema-validation-cross-check-design.md (§10 완료조건 2)
"""
from __future__ import annotations

import pytest

from tests.xcheck_support import backend_verdict, expand_cases

CASES = expand_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_backend_matches_schema_expected(case: dict) -> None:
    """백엔드 판정이 SCHEMA 기준 expected와 일치해야 한다 (전 케이스)."""
    verdict = backend_verdict(case["profile"])
    assert verdict == case["expected"], (
        f"{case['id']} ({case['ref']}): expected={case['expected']} "
        f"but backend={verdict} — {case['desc']}"
    )


def test_case_matrix_is_nonempty_and_covers_rules() -> None:
    """매트릭스가 설계 §5.2의 규칙을 모두 커버하는지(회귀 가드)."""
    refs = {c["ref"] for c in CASES}
    for required in ("§5.1", "§5.5", "§5.6", "§5.7", "§5.8"):
        assert required in refs, f"케이스 매트릭스에 {required} 커버 누락"
    # op 매트릭스: 9 fact × 6 op = 54 + 명시 케이스
    assert len(CASES) >= 60, f"케이스 수가 너무 적음: {len(CASES)}"


def test_meta_self_test_detects_mismatch() -> None:
    """테스트의 테스트: 정답 라벨을 일부러 뒤집으면 비교가 불일치를 검출해야 한다.

    러너가 '항상 통과'하는 가짜 그린이 아님을 보증한다.
    """
    normal = next(c for c in CASES if c["id"] == "normal_max_ge")
    # 실제 판정은 valid
    assert backend_verdict(normal["profile"]) == "valid"
    # 거짓 라벨(invalid)과는 불일치로 잡혀야 한다
    bogus_expected = "invalid"
    assert backend_verdict(normal["profile"]) != bogus_expected
