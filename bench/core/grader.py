"""Scenario-agnostic grading.

Coverage is measured from the oracle (target-side truth). Efficiency is
measured from the adapter's emitted artifacts (agent-side cost). The two are
computed independently and never reference each other's source.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .scenario import ItemSpec, OracleResult


def coverage(ground_truth: list[ItemSpec], oracle: OracleResult) -> dict[str, Any]:
    total = len(ground_truth)
    by_key = {it.key: it for it in ground_truth}
    solved_keys = [k for k in oracle.solved if k in by_key]

    weight_total = sum(it.effective_weight() for it in ground_truth) or 1.0
    weight_solved = sum(by_key[k].effective_weight() for k in solved_keys)

    diff_total: dict[int, int] = defaultdict(int)
    diff_solved: dict[int, int] = defaultdict(int)
    cat_total: dict[str, int] = defaultdict(int)
    cat_solved: dict[str, int] = defaultdict(int)
    for it in ground_truth:
        diff_total[it.difficulty] += 1
        cat_total[it.category] += 1
    for k in solved_keys:
        it = by_key[k]
        diff_solved[it.difficulty] += 1
        cat_solved[it.category] += 1

    def ratio(a: int, b: int) -> float:
        return round(a / b, 4) if b else 0.0

    return {
        "total": total,
        "solved": len(solved_keys),
        "coverage": ratio(len(solved_keys), total),
        "coverage_weighted": round(weight_solved / weight_total, 4),
        "by_difficulty": {
            str(d): {"solved": diff_solved[d], "total": diff_total[d],
                     "ratio": ratio(diff_solved[d], diff_total[d])}
            for d in sorted(diff_total)
        },
        "by_category": {
            c: {"solved": cat_solved[c], "total": cat_total[c],
                "ratio": ratio(cat_solved[c], cat_total[c])}
            for c in sorted(cat_total)
        },
        "solved_keys": sorted(solved_keys),
    }


def efficiency(usage: dict[str, Any], tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize adapter artifacts into the efficiency block. Missing fields
    degrade to None rather than raising, so a minimal adapter still grades."""
    return {
        "tool_calls": len(tool_calls),
        "tokens_in": usage.get("tokens_in"),
        "tokens_out": usage.get("tokens_out"),
        "cost_usd": usage.get("cost_usd"),
        "wall_time_s": usage.get("wall_time_s"),
    }


def coverage_at_budget(curve: list[tuple[float, int, float]], total: int,
                       wall_s: float | None,
                       fractions: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
                       ) -> dict[str, Any]:
    """Coverage-at-budget: solved fraction reached by each fraction of the wall
    budget, from the progress curve (a step function of solved over time). Lets
    you tell an agent that plateaus early from one still climbing when it stopped."""
    curve = sorted(curve)
    span = wall_s if wall_s else (curve[-1][0] if curve else 0.0)

    def solved_by(t: float) -> int:
        s = 0
        for elapsed, solved, _ in curve:
            if elapsed <= t:
                s = solved
            else:
                break
        return s

    at = {}
    for f in fractions:
        n = solved_by(f * span) if span else 0
        at[f"{f:.2f}"] = {"solved": n, "ratio": round(n / total, 4) if total else 0.0}
    return {"wall_budget_s": wall_s, "at": at}


def cost_per_solved(cov: dict[str, Any], eff: dict[str, Any]) -> dict[str, Any]:
    solved = cov["solved"] or 0
    out: dict[str, Any] = {}
    for label, val in (("usd_per_solved", eff.get("cost_usd")),
                       ("tool_calls_per_solved", eff.get("tool_calls"))):
        out[label] = round(val / solved, 4) if (val is not None and solved) else None
    return out
