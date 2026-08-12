#!/usr/bin/env python3
"""pt-bench runner: execute one (arm x scenario) cell for N repetitions.

    python run.py --arm example__noop --scenario juice-shop --repeats 1

Flow per repetition, all through scenario-agnostic seams:
    provision -> build run_spec -> invoke adapter -> oracle -> grade -> teardown
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bench.core import adapter as adapter_mod  # noqa: E402
from bench.core import grader, registry, results  # noqa: E402
from bench.core.scenario import Budget  # noqa: E402

ADAPTERS_DIR = ROOT / "adapters"
ARMS_DIR = ROOT / "arms"
RESULTS_DIR = ROOT / "results"
TIMEOUT_GRACE_S = 120  # runner backstop beyond the adapter's own wall limit


def load_arm(name: str) -> dict:
    path = ARMS_DIR / f"{name}.yaml"
    if not path.exists():
        sys.exit(f"no such arm: {path}")
    return yaml.safe_load(path.read_text())


def adapter_cmd(adapter_name: str) -> list[str]:
    entry = ADAPTERS_DIR / adapter_name / "adapter.py"
    if not entry.exists():
        sys.exit(f"adapter has no adapter.py: {entry}")
    return [sys.executable, str(entry)]


def run_cell(arm: dict, scenario_id: str, repeats: int, keep_up: bool) -> list[dict]:
    budget = Budget.from_dict(arm.get("budget"))
    cmd = adapter_cmd(arm["adapter"])
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rows: list[dict] = []

    for i in range(repeats):
        workdir = RESULTS_DIR / arm["name"] / scenario_id / f"{stamp}-r{i}"
        print(f"\n=== {arm['name']} x {scenario_id} : repeat {i} ===")
        scenario = registry.get_scenario(scenario_id, arm.get("scenario_config"))

        handle = None
        try:
            print("  provisioning target ...")
            handle = scenario.provision()
            gt = scenario.ground_truth(handle)
            print(f"  target up: {handle.target_url}  ({len(gt)} challenges)")

            spec = adapter_mod.build_run_spec(
                handle=handle, model=arm.get("model"), budget=budget,
                workdir=workdir, adapter_config=arm.get("adapter_config", {}))
            backstop = (budget.wall_time_s + TIMEOUT_GRACE_S) if budget.wall_time_s else None

            print("  running adapter ...")
            status = adapter_mod.run_adapter(
                adapter_cmd=cmd, run_spec=spec, workdir=workdir, wall_time_s=backstop)
            print(f"  adapter status: {status.get('status')}")

            oracle = scenario.oracle(handle)
            usage, tool_calls = adapter_mod.read_artifacts(workdir)
            cov = grader.coverage(gt, oracle)
            eff = grader.efficiency(usage, tool_calls)
            derived = grader.cost_per_solved(cov, eff)

            row = results.make_row(
                arm=arm["name"], scenario=scenario_id, repeat=i,
                model=arm.get("model"), status=status.get("status", "error"),
                coverage=cov, efficiency=eff, derived=derived, workdir=str(workdir))
            results.write_row(row, workdir / "result.json")
            rows.append(row)
            print(f"  solved {cov['solved']}/{cov['total']}  "
                  f"(weighted {cov['coverage_weighted']})  "
                  f"tool_calls={eff['tool_calls']}  cost={eff['cost_usd']}")
        finally:
            if handle and not keep_up:
                print("  tearing down ...")
                scenario.teardown(handle)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one pt-bench cell.")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--keep-up", action="store_true",
                    help="do not tear down the target (debugging)")
    args = ap.parse_args()

    arm = load_arm(args.arm)
    rows = run_cell(arm, args.scenario, args.repeats, args.keep_up)

    agg = results.aggregate(rows)
    print("\n=== aggregate ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
