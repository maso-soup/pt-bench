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

SCHEMA_VERSION = 1


def make_row(*, arm: str, scenario: str, repeat: int, model: str | None,
             status: str, coverage: dict[str, Any], efficiency: dict[str, Any],
             derived: dict[str, Any], workdir: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "arm": arm,
        "scenario": scenario,
        "repeat": repeat,
        "model": model,
        "status": status,
        "coverage": coverage,
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
    return {
        "n_runs": len(rows),
        "n_completed": len(ok),
        "coverage_mean": round(cov_m, 4),
        "coverage_ci95_halfwidth": round(cov_h, 4),
        "coverage_weighted_mean": round(covw_m, 4),
        "coverage_weighted_ci95_halfwidth": round(covw_h, 4),
        "cost_usd_mean": round(statistics.fmean(cost), 4) if cost else None,
        "tool_calls_mean": round(statistics.fmean(calls), 2) if calls else None,
    }


def load_rows(results_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(results_dir.rglob("result.json"))]
