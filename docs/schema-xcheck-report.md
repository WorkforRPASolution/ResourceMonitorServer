# SCHEMA §5 검증 3-way 대조 리포트

> 자동 생성 — `python -m tests.xcheck_build_report`. 정답(expected)은 SCHEMA.md를 인용해 라벨링한 값.

## 요약

- 전체 케이스: **70**
- 백엔드 ↔ SCHEMA 불일치: **0** (없음 ✓)
- playground ↔ SCHEMA 불일치(지원 39건 중): **0** (없음 ✓)
- playground 커버리지 GAP: **31** (dup_measure_id, count_no_min, count_min_zero, count_min_valid, proc_mismatch, spike_no_over, spike_no_direction, opm_last_ge, opm_last_gt, opm_last_le, opm_last_lt, opm_last_eq, opm_last_ne, opm_p50_ge, opm_p50_gt, opm_p50_le, opm_p50_lt, opm_p50_eq, opm_p50_ne, opm_p90_ge, opm_p90_gt, opm_p90_le, opm_p90_lt, opm_p90_eq, opm_p90_ne, opm_p99_ge, opm_p99_gt, opm_p99_le, opm_p99_lt, opm_p99_eq, opm_p99_ne)

> GAP = playground가 아직 검증하지 않는 규칙/타입(다중 measure·count_min·last/p50/p90/p99·spike param). 버그가 아니라 커버리지 한계이며, playground 확장 시 줄어드는 회귀 지표.

## 케이스별 대조 (expected / backend / playground)

| id | ref | expected | backend | playground |
|----|-----|----------|---------|------------|
| normal_max_ge | 정상 baseline | valid | valid | valid |
| normal_spike_count | 정상 baseline | valid | valid | valid |
| ref_measure_missing | §5.5 | invalid | invalid | invalid |
| ref_fact_undeclared | §5.5 | invalid | invalid | invalid |
| interval_lt_window | §5.6 | valid | valid | valid |
| interval_eq_window | §5.6 | valid | valid | valid |
| interval_gt_window | §5.6 | invalid | invalid | invalid |
| dup_fact_type | §5.1 | invalid | invalid | invalid |
| dup_measure_id | §5.1 | invalid | invalid | —(GAP) |
| notify_missing | §5.7 | invalid | invalid | invalid |
| count_no_min | §5.7 | invalid | invalid | —(GAP) |
| count_min_zero | §5.7 | invalid | invalid | —(GAP) |
| count_min_valid | §5.7 | valid | valid | —(GAP) |
| proc_mismatch | §5.8 | invalid | invalid | —(GAP) |
| spike_no_over | §1.3 (Fact 필수 param) | invalid | invalid | —(GAP) |
| spike_no_direction | §1.3 (Fact 필수 param) | invalid | invalid | —(GAP) |
| opm_max_ge | §5.5 | valid | valid | valid |
| opm_max_gt | §5.5 | valid | valid | valid |
| opm_max_le | §5.5 | invalid | invalid | invalid |
| opm_max_lt | §5.5 | invalid | invalid | invalid |
| opm_max_eq | §5.5 | invalid | invalid | invalid |
| opm_max_ne | §5.5 | invalid | invalid | invalid |
| opm_min_ge | §5.5 | invalid | invalid | invalid |
| opm_min_gt | §5.5 | invalid | invalid | invalid |
| opm_min_le | §5.5 | valid | valid | valid |
| opm_min_lt | §5.5 | valid | valid | valid |
| opm_min_eq | §5.5 | valid | valid | valid |
| opm_min_ne | §5.5 | invalid | invalid | invalid |
| opm_avg_ge | §5.5 | valid | valid | valid |
| opm_avg_gt | §5.5 | valid | valid | valid |
| opm_avg_le | §5.5 | valid | valid | valid |
| opm_avg_lt | §5.5 | valid | valid | valid |
| opm_avg_eq | §5.5 | invalid | invalid | invalid |
| opm_avg_ne | §5.5 | invalid | invalid | invalid |
| opm_last_ge | §5.5 | valid | valid | —(GAP) |
| opm_last_gt | §5.5 | valid | valid | —(GAP) |
| opm_last_le | §5.5 | valid | valid | —(GAP) |
| opm_last_lt | §5.5 | valid | valid | —(GAP) |
| opm_last_eq | §5.5 | valid | valid | —(GAP) |
| opm_last_ne | §5.5 | valid | valid | —(GAP) |
| opm_p50_ge | §5.5 | valid | valid | —(GAP) |
| opm_p50_gt | §5.5 | valid | valid | —(GAP) |
| opm_p50_le | §5.5 | valid | valid | —(GAP) |
| opm_p50_lt | §5.5 | valid | valid | —(GAP) |
| opm_p50_eq | §5.5 | invalid | invalid | —(GAP) |
| opm_p50_ne | §5.5 | invalid | invalid | —(GAP) |
| opm_p90_ge | §5.5 | valid | valid | —(GAP) |
| opm_p90_gt | §5.5 | valid | valid | —(GAP) |
| opm_p90_le | §5.5 | valid | valid | —(GAP) |
| opm_p90_lt | §5.5 | valid | valid | —(GAP) |
| opm_p90_eq | §5.5 | invalid | invalid | —(GAP) |
| opm_p90_ne | §5.5 | invalid | invalid | —(GAP) |
| opm_p95_ge | §5.5 | valid | valid | valid |
| opm_p95_gt | §5.5 | valid | valid | valid |
| opm_p95_le | §5.5 | valid | valid | valid |
| opm_p95_lt | §5.5 | valid | valid | valid |
| opm_p95_eq | §5.5 | invalid | invalid | invalid |
| opm_p95_ne | §5.5 | invalid | invalid | invalid |
| opm_p99_ge | §5.5 | valid | valid | —(GAP) |
| opm_p99_gt | §5.5 | valid | valid | —(GAP) |
| opm_p99_le | §5.5 | valid | valid | —(GAP) |
| opm_p99_lt | §5.5 | valid | valid | —(GAP) |
| opm_p99_eq | §5.5 | invalid | invalid | —(GAP) |
| opm_p99_ne | §5.5 | invalid | invalid | —(GAP) |
| opm_spike_count_ge | §5.5 | valid | valid | valid |
| opm_spike_count_gt | §5.5 | valid | valid | valid |
| opm_spike_count_le | §5.5 | invalid | invalid | invalid |
| opm_spike_count_lt | §5.5 | invalid | invalid | invalid |
| opm_spike_count_eq | §5.5 | invalid | invalid | invalid |
| opm_spike_count_ne | §5.5 | invalid | invalid | invalid |
