#!/usr/bin/env python3
"""pt-bench runner: execute one (arm x scenario) cell for N repetitions.

    python run.py --arm example__noop --scenario juice-shop --repeats 1

Flow per repetition, all through scenario-agnostic seams:
    provision -> build run_spec -> invoke adapter -> oracle -> grade -> teardown
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bench.core import adapter as adapter_mod  # noqa: E402
from bench.core import grader, progress, registry, results  # noqa: E402
from bench.core.scenario import Budget  # noqa: E402

ADAPTERS_DIR = ROOT / "adapters"
ARMS_DIR = ROOT / "arms"
RESULTS_DIR = ROOT / "results"
TIMEOUT_GRACE_S = 120  # runner backstop beyond the adapter's own wall limit


# ${VAR} and ${VAR:-default} expansion (os.path.expandvars lacks :- defaults).
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_vars(s: str) -> str:
    def repl(m: re.Match) -> str:
        val = os.environ.get(m.group(1))
        if val:  # set and non-empty wins
            return val
        return m.group(2) or ""  # else the :- default, or empty
    return _VAR_RE.sub(repl, s)


def resolve_repo(value: str, root: Path) -> str:
    """Make an arm's `repo` portable: expand ${VARS} and ~, and resolve a
    relative path against the pt-bench root. Absolute paths pass through."""
    p = Path(os.path.expanduser(_expand_vars(str(value))))
    return str(p if p.is_absolute() else (root / p))


def load_arm(name: str) -> dict:
    path = ARMS_DIR / f"{name}.yaml"
    if not path.exists():
        sys.exit(f"no such arm: {path}")
    arm = yaml.safe_load(path.read_text())
    cfg = arm.get("adapter_config") or {}
    if cfg.get("repo"):  # normalize to an absolute host path once, at load time
        cfg["repo"] = resolve_repo(cfg["repo"], ROOT)
        arm["adapter_config"] = cfg
    return arm


def adapter_cmd(adapter_name: str) -> list[str]:
    entry = ADAPTERS_DIR / adapter_name / "adapter.py"
    if not entry.exists():
        sys.exit(f"adapter has no adapter.py: {entry}")
    return [sys.executable, str(entry)]


def run_cell(arm: dict, scenario_id: str, repeats: int, keep_up: bool, *,
             progress_enabled: bool, progress_interval: float | None) -> list[dict]:
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

            # Clear this agent's prior on-disk state (scoped to this target) so
            # repetitions never read each other's artifacts. Moved to trash, not
            # deleted. See adapter_config.reset_paths in the arm file.
            reset_paths = (arm.get("adapter_config") or {}).get("reset_paths") or []
            moved = adapter_mod.reset_agent_state(
                reset_paths=reset_paths, target_url=handle.target_url)
            for src, dest in moved:
                print(f"  reset: {src} -> trash ({dest.parent.name})")

            interval = (progress_interval if progress_interval is not None
                        else getattr(scenario, "progress_interval_s", 5.0))
            poller = progress.ProgressPoller(
                scenario, handle, ground_truth=gt, workdir=workdir, interval=interval,
                enabled=progress_enabled and scenario.supports_live_progress)

            print("  running adapter ...")
            poller.start()
            try:
                status = adapter_mod.run_adapter(
                    adapter_cmd=cmd, run_spec=spec, workdir=workdir, wall_time_s=backstop)
            finally:
                poller.stop()
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
    ap.add_argument("--progress", action=argparse.BooleanOptionalAction, default=None,
                    help="live solve progress in the terminal (default: on when a TTY)")
    ap.add_argument("--progress-interval", type=float, default=None,
                    help="seconds between solve-state polls (default: scenario's own)")
    args = ap.parse_args()

    progress_enabled = args.progress if args.progress is not None else sys.stdout.isatty()

    arm = load_arm(args.arm)
    rows = run_cell(arm, args.scenario, args.repeats, args.keep_up,
                    progress_enabled=progress_enabled,
                    progress_interval=args.progress_interval)

    agg = results.aggregate(rows)
    print("\n=== aggregate ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
