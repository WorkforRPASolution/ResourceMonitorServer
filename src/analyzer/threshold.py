"""v2 rule evaluation — operator/quantifier/combine over computed facts.

A *measure* produces facts; a *rule* compares ``measureId.type`` facts against
values via an operator, reduces over metric *instances* with a quantifier
(any/all/count), and combines its conditions with AND/OR. ``state_check`` is not
a separate concept — it is an ordinary ``min == 0`` / ``max > 0`` condition
(SCHEMA.md §2). The two-tier WARNING/CRITICAL model is expressed as two rules,
each owning its own severity, not as a single warning/critical pair.
"""
from __future__ import annotations

import operator
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.db.models import Rule, Severity

_NUMERIC_OPS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


class ThresholdBreach(BaseModel):
    """One equipment/proc breaching one rule."""

    model_config = ConfigDict(extra="forbid")

    eqp_id: str
    proc: str = "@system"
    rule_id: str
    fact: str  # the triggering "measureId.type"
    category: str
    op: str
    current_value: float | None
    threshold_value: float | str
    severity: Severity


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process: str
    rule_ids: list[str] = []
    breaches: list[ThresholdBreach]
    total_evaluated: int
    timestamp: datetime


def op_compare(value: float | str | None, op: str, threshold: float | str) -> bool:
    """Apply one operator. ``trend==`` is string equality; numeric ops treat a
    ``None`` value (empty bucket) as non-breaching."""
    if op == "trend==":
        return value == threshold
    if value is None:
        return False
    return _NUMERIC_OPS[op](value, threshold)


def _worst(values: list[float], op: str) -> float:
    """Representative breaching value for the email: the most extreme one in the
    operator's direction (max for high-side ops, min for low-side)."""
    if op in (">=", ">"):
        return max(values)
    if op in ("<=", "<"):
        return min(values)
    return values[0]


def evaluate_condition(
    cond, values: list[float | None]
) -> tuple[bool, float | None]:
    """Reduce one condition over a fact's instance values via its quantifier.

    Returns ``(passed, representative_value)``. ``all`` over an empty value list
    is False (no data cannot satisfy "all").
    """
    present = [v for v in values if v is not None]
    if cond.op == "trend==":
        passing = [v for v in values if op_compare(v, cond.op, cond.value)]
    else:
        passing = [v for v in present if op_compare(v, cond.op, cond.value)]
    n = len(passing)
    if cond.quantifier == "any":
        passed = n >= 1
    elif cond.quantifier == "all":
        passed = len(present) > 0 and n == len(present)
    else:  # count
        passed = n >= (cond.count_min or 0)
    if not passed:
        rep = present[0] if present else None
        return False, rep
    numeric = [v for v in passing if isinstance(v, (int, float))]
    rep = _worst(numeric, cond.op) if numeric else (passing[0] if passing else None)
    return True, rep


def evaluate_rule(
    rule: Rule,
    facts_by_ref: dict[str, list[float | None]],
    *,
    eqp_id: str,
    proc: str,
    measure_category: dict[str, str],
) -> ThresholdBreach | None:
    """Evaluate one rule for one (eqp, proc) target.

    ``facts_by_ref`` maps ``"measureId.type"`` to the list of instance values for
    this target. ``measure_category`` maps measure id → its EARS category (used
    to label the breach). Returns a :class:`ThresholdBreach` when the rule fires,
    else ``None``.
    """
    results: list[tuple[object, bool, float | None]] = []
    for cond in rule.when:
        values = facts_by_ref.get(cond.fact, [])
        passed, rep = evaluate_condition(cond, values)
        results.append((cond, passed, rep))

    flags = [p for _, p, _ in results]
    breached = all(flags) if rule.combine == "AND" else any(flags)
    if not breached or not results:
        return None

    trigger = next((r for r in results if r[1]), results[0])
    cond, _, rep = trigger
    measure_id = cond.fact.partition(".")[0]
    return ThresholdBreach(
        eqp_id=eqp_id,
        proc=proc,
        rule_id=rule.id,
        fact=cond.fact,
        category=measure_category.get(measure_id, ""),
        op=cond.op,
        current_value=rep,
        threshold_value=cond.value,
        severity=rule.severity,
    )
