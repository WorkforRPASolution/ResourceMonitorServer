"""SCHEMA §5 검증 3-way 대조 — 데이터 빌드 + 리포트 생성.

실행: python -m tests.xcheck_build_report  (cwd = 프로젝트 루트)

1) docs/_xcheck_data.json 생성: 전개+정규화 케이스 + 백엔드 판정
   (playground 러너 입력 — Playwright가 fetch해 window.__rmp.validateCase에 주입)
2) docs/_xcheck_playground.json 이 있으면 docs/schema-xcheck-report.md 생성
   (case별 expected/backend/playground 3열 표 + 불일치·GAP 목록)

설계: docs/superpowers/specs/2026-06-07-schema-validation-cross-check-design.md (§10 완료조건 5)
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.xcheck_support import backend_verdict, expand_cases, normalize_profile

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DATA_OUT = DOCS / "_xcheck_data.json"
PG_IN = DOCS / "_xcheck_playground.json"
REPORT = DOCS / "schema-xcheck-report.md"


def build_data() -> list[dict]:
    cases = expand_cases()
    out = []
    for c in cases:
        out.append(
            {
                "id": c["id"],
                "ref": c["ref"],
                "desc": c["desc"],
                "expected": c["expected"],
                "expected_violations": c.get("expected_violations", []),
                "playground_supports": c["playground_supports"],
                "backend": backend_verdict(c["profile"]),
                "profile": normalize_profile(c["profile"]),
            }
        )
    DATA_OUT.write_text(
        json.dumps({"cases": out}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


def build_report(data: list[dict], pg: dict) -> dict:
    pg_by_id = {r["id"]: r for r in pg.get("results", [])}
    rows, mism_backend, mism_pg, gaps = [], [], [], []
    for c in data:
        exp, be = c["expected"], c["backend"]
        if be != exp:
            mism_backend.append(c["id"])
        if c["playground_supports"]:
            pv = pg_by_id.get(c["id"], {}).get("verdict", "?")
            if pv != exp:
                mism_pg.append(c["id"])
            pg_disp = pv
        else:
            gaps.append(c["id"])
            pg_disp = "—(GAP)"
        rows.append((c["id"], c["ref"], exp, be, pg_disp))

    lines = []
    lines.append("# SCHEMA §5 검증 3-way 대조 리포트")
    lines.append("")
    lines.append("> 자동 생성 — `python -m tests.xcheck_build_report`. "
                 "정답(expected)은 SCHEMA.md를 인용해 라벨링한 값.")
    lines.append("")
    lines.append("## 요약")
    lines.append("")
    lines.append(f"- 전체 케이스: **{len(data)}**")
    lines.append(f"- 백엔드 ↔ SCHEMA 불일치: **{len(mism_backend)}** "
                 + (f"({', '.join(mism_backend)})" if mism_backend else "(없음 ✓)"))
    pg_total = sum(1 for c in data if c["playground_supports"])
    lines.append(f"- playground ↔ SCHEMA 불일치(지원 {pg_total}건 중): **{len(mism_pg)}** "
                 + (f"({', '.join(mism_pg)})" if mism_pg else "(없음 ✓)"))
    lines.append(f"- playground 커버리지 GAP: **{len(gaps)}** "
                 + (f"({', '.join(gaps)})" if gaps else "(없음)"))
    lines.append("")
    lines.append("> GAP = playground가 아직 검증하지 않는 규칙/타입(다중 measure·count_min·"
                 "last/p50/p90/p99·spike param). 버그가 아니라 커버리지 한계이며, "
                 "playground 확장 시 줄어드는 회귀 지표.")
    lines.append("")
    lines.append("## 케이스별 대조 (expected / backend / playground)")
    lines.append("")
    lines.append("| id | ref | expected | backend | playground |")
    lines.append("|----|-----|----------|---------|------------|")
    for rid, ref, exp, be, pv in rows:
        be_mark = be if be == exp else f"**{be}** ⚠"
        pv_mark = pv
        if pv not in ("—(GAP)", exp):
            pv_mark = f"**{pv}** ⚠"
        lines.append(f"| {rid} | {ref} | {exp} | {be_mark} | {pv_mark} |")
    lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return {
        "total": len(data),
        "mismatch_backend": mism_backend,
        "mismatch_playground": mism_pg,
        "gaps": gaps,
        "pg_supported": pg_total,
    }


if __name__ == "__main__":
    data = build_data()
    print(f"[build] {len(data)} cases → {DATA_OUT.relative_to(ROOT)}")
    if PG_IN.exists():
        pg = json.loads(PG_IN.read_text(encoding="utf-8"))
        summary = build_report(data, pg)
        print(f"[report] → {REPORT.relative_to(ROOT)}")
        print(f"[summary] backend_mismatch={len(summary['mismatch_backend'])} "
              f"playground_mismatch={len(summary['mismatch_playground'])} "
              f"gaps={len(summary['gaps'])}/{summary['total']}")
    else:
        print(f"[report] skip — {PG_IN.name} 없음 (playground 러너를 먼저 실행하세요)")
