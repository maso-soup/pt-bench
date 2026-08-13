"""Result row schema and cross-repetition aggregation.

One JSON row per (arm x scenario x repetition). Aggregation reports mean and a
simple normal-approximation confidence interval so a single run is never
mistaken for a result.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


def make_row(*, arm: str, scenario: str, repeat: int, model: str | None,
             status: str, coverage: dict[str, Any], efficiency: dict[str, Any],
             derived: dict[str, Any], workdir: str,
             mode: str = "autonomous", iterations: int = 1,
             curve: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "arm": arm,
        "scenario": scenario,
        "repeat": repeat,
        "model": model,
        "mode": mode,
        "iterations": iterations,
        "status": status,
        "coverage": coverage,
        "curve": curve or {},
        "efficiency": efficiency,
        "derived": derived,
        "workdir": workdir,
    }


def write_row(row: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2, sort_keys=True))


def _ci95(values: list[float]) -> tuple[float, float]:
    """Mean and half-width of a 95% CI (normal approx). Half-width is 0 for n<2."""
    if not values:
        return (0.0, 0.0)
    mean = statistics.fmean(values)
    if len(values) < 2:
        return (mean, 0.0)
    sd = statistics.stdev(values)
    half = 1.96 * sd / math.sqrt(len(values))
    return (mean, half)


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate rows for a single (arm, scenario) cell across repetitions."""
    ok = [r for r in rows if r.get("status") == "completed"]
    cov = [r["coverage"]["coverage"] for r in ok]
    covw = [r["coverage"]["coverage_weighted"] for r in ok]
    cost = [r["efficiency"]["cost_usd"] for r in ok if r["efficiency"].get("cost_usd") is not None]
    calls = [r["efficiency"]["tool_calls"] for r in ok if r["efficiency"].get("tool_calls") is not None]

    cov_m, cov_h = _ci95(cov)
    covw_m, covw_h = _ci95(covw)
    iters = [r.get("iterations", 1) for r in ok]
    firsts = [r["curve"]["first_solve_s"] for r in ok
              if r.get("curve", {}).get("first_solve_s") is not None]
    out = {
        "mode": (ok[0].get("mode") if ok else None),
        "n_runs": len(rows),
        "n_completed": len(ok),
        "iterations_mean": round(statistics.fmean(iters), 2) if iters else None,
        "coverage_mean": round(cov_m, 4),
        "coverage_ci95_halfwidth": round(cov_h, 4),
        "coverage_weighted_mean": round(covw_m, 4),
        "coverage_weighted_ci95_halfwidth": round(covw_h, 4),
        "first_solve_s_mean": round(statistics.fmean(firsts), 1) if firsts else None,
        "cost_usd_mean": round(statistics.fmean(cost), 4) if cost else None,
        "tool_calls_mean": round(statistics.fmean(calls), 2) if calls else None,
    }
    # Mean coverage reached by each fraction of the wall budget (coverage-at-budget).
    frac_keys = sorted({k for r in ok for k in r.get("curve", {}).get("at", {})})
    at = {}
    for fk in frac_keys:
        ratios = [r["curve"]["at"][fk]["ratio"] for r in ok if fk in r.get("curve", {}).get("at", {})]
        if ratios:
            at[fk] = round(statistics.fmean(ratios), 4)
    if at:
        out["coverage_at_budget_mean"] = at
    return out


def load_rows(results_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(results_dir.rglob("result.json"))]
