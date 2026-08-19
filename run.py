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
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bench.core import adapter as adapter_mod  # noqa: E402
from bench.core import grader, progress, registry, results  # noqa: E402
from bench.core.scenario import Budget  # noqa: E402

ADAPTERS_DIR = ROOT / "adapters"
ARMS_DIR = ROOT / "arms"
TIMEOUT_GRACE_S = 120  # runner backstop beyond the adapter's own wall limit
MAX_CONTINUATION_ITERS = 10  # safety cap on max-coverage re-invocations
MODES = ("autonomous", "max-coverage")


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


def _drive(arm: dict, scenario, handle, gt, cmd, budget: Budget, workdir: Path,
           mode: str) -> tuple[dict, list[Path]]:
    """Run the adapter once (autonomous) or in a continuation loop (max-coverage)
    until the agent solves everything, plateaus (an iteration adds no new solves),
    errors, or the total wall budget runs out. State is preserved across
    iterations so the agent resumes; the harness reads the oracle to decide when
    to stop while the agent itself stays blind to the score."""
    total_wall = budget.wall_time_s
    started = time.time()
    max_iters = MAX_CONTINUATION_ITERS if mode == "max-coverage" else 1
    iter_dirs: list[Path] = []
    status: dict = {"status": "error", "reason": "no iteration ran"}
    prev_solved = -1

    for it in range(max_iters):
        remaining = None
        if total_wall is not None:
            remaining = int(total_wall - (time.time() - started))
            if remaining <= 0:
                status = {"status": "budget_exceeded", "reason": "wall_time_s exhausted"}
                break

        iter_dir = workdir / f"iter-{it:02d}"
        iter_dirs.append(iter_dir)
        it_budget = Budget(tool_calls=budget.tool_calls, usd=budget.usd,
                           wall_time_s=remaining)
        spec = adapter_mod.build_run_spec(
            handle=handle, model=arm.get("model"), budget=it_budget, workdir=iter_dir,
            adapter_config=arm.get("adapter_config", {}), continuation=(it > 0))
        backstop = (remaining + TIMEOUT_GRACE_S) if remaining is not None else None

        print(f"  {'running adapter' if it == 0 else f'continuing (iter {it})'} ...")
        status = adapter_mod.run_adapter(
            adapter_cmd=cmd, run_spec=spec, workdir=iter_dir, wall_time_s=backstop)
        print(f"  adapter status: {status.get('status')}")

        if mode != "max-coverage":
            break
        solved = len(scenario.oracle(handle).solved)
        print(f"  progress after iter {it}: {solved}/{len(gt)} solved")
        if solved >= len(gt) or status.get("status") == "error":
            break
        if it > 0 and solved <= prev_solved:  # plateau: no new solves this iteration
            print(f"  plateau: iter {it} added no new solves — stopping")
            break
        prev_solved = solved

    return status, iter_dirs


def run_cell(arm: dict, scenario_id: str, repeats: int, keep_up: bool, *,
             mode: str, results_dir: Path, progress_enabled: bool,
             progress_interval: float | None) -> tuple[list[dict], Path]:
    budget = Budget.from_dict(arm.get("budget"))
    cmd = adapter_cmd(arm["adapter"])
    started_at = dt.datetime.now()
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    # One batch dir per cell groups its repetitions and holds provenance.
    batch_dir = results_dir / arm["name"] / scenario_id / stamp
    results.write_json(results.make_manifest(
        arm=arm, scenario=scenario_id, mode=mode, repeats=repeats,
        started_at=started_at.isoformat(timespec="seconds"), budget=budget.to_dict(),
        pt_bench_dir=ROOT, agent_repo=(arm.get("adapter_config") or {}).get("repo")),
        batch_dir / "manifest.json")
    rows: list[dict] = []

    for i in range(repeats):
        workdir = batch_dir / f"r{i}"
        print(f"\n=== {arm['name']} x {scenario_id} [{mode}] : repeat {i} ===")
        scenario = registry.get_scenario(scenario_id, arm.get("scenario_config"))

        handle = None
        try:
            print("  provisioning target ...")
            handle = scenario.provision()
            gt = scenario.ground_truth(handle)
            print(f"  target up: {handle.target_url}  ({len(gt)} challenges)")

            # Clear this agent's prior on-disk state once per repetition (before
            # the first iteration) so repetitions never read each other's
            # artifacts, while continuation iterations keep state to resume.
            reset_paths = (arm.get("adapter_config") or {}).get("reset_paths") or []
            for src, dest in adapter_mod.reset_agent_state(reset_paths=reset_paths):
                print(f"  reset: {src} -> trash ({dest.parent.name})")

            interval = (progress_interval if progress_interval is not None
                        else getattr(scenario, "progress_interval_s", 5.0))
            poller = progress.ProgressPoller(
                scenario, handle, ground_truth=gt, workdir=workdir, interval=interval,
                enabled=progress_enabled and scenario.supports_live_progress)

            poller.start()
            try:
                status, iter_dirs = _drive(arm, scenario, handle, gt, cmd, budget,
                                           workdir, mode)
            finally:
                poller.stop()

            oracle = scenario.oracle(handle)
            usage, tool_calls = adapter_mod.read_artifacts_multi(iter_dirs)
            cov = grader.coverage(gt, oracle)
            eff = grader.efficiency(usage, tool_calls)
            derived = grader.cost_per_solved(cov, eff)
            cab = grader.coverage_at_budget(poller.curve, cov["total"], budget.wall_time_s)
            summ = poller.summary()
            curve = {"first_solve_s": summ["first_solve_s"],
                     "last_solve_s": summ["last_solve_s"], **cab}

            row = results.make_row(
                arm=arm["name"], scenario=scenario_id, repeat=i, model=arm.get("model"),
                mode=mode, iterations=len(iter_dirs), status=status.get("status", "error"),
                coverage=cov, curve=curve, efficiency=eff, derived=derived,
                workdir=str(workdir))
            results.write_row(row, workdir / "result.json")
            rows.append(row)
            print(f"  solved {cov['solved']}/{cov['total']}  "
                  f"(weighted {cov['coverage_weighted']})  iters={len(iter_dirs)}  "
                  f"tool_calls={eff['tool_calls']}  cost={eff['cost_usd']}")
        finally:
            if handle and not keep_up:
                print("  tearing down ...")
                scenario.teardown(handle)

    results.write_json(results.aggregate(rows), batch_dir / "summary.json")
    return rows, batch_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one pt-bench cell.")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--mode", choices=MODES, default="autonomous",
                    help="autonomous: one run, agent stops when it decides (honest "
                         "baseline). max-coverage: re-invoke to resume until it "
                         "plateaus or the wall budget runs out (capability ceiling).")
    ap.add_argument("--keep-up", action="store_true",
                    help="do not tear down the target (debugging)")
    ap.add_argument("--progress", action=argparse.BooleanOptionalAction, default=None,
                    help="live solve progress in the terminal (default: on when a TTY)")
    ap.add_argument("--progress-interval", type=float, default=None,
                    help="seconds between solve-state polls (default: scenario's own)")
    ap.add_argument("--results-dir", default=None,
                    help="where to store results (default: $PTBENCH_RESULTS_DIR or "
                         "~/.local/share/pt-bench/results; kept out of the repo)")
    args = ap.parse_args()

    progress_enabled = args.progress if args.progress is not None else sys.stdout.isatty()
    results_dir = results.resolve_results_dir(args.results_dir)

    arm = load_arm(args.arm)
    rows, batch_dir = run_cell(arm, args.scenario, args.repeats, args.keep_up,
                               mode=args.mode, results_dir=results_dir,
                               progress_enabled=progress_enabled,
                               progress_interval=args.progress_interval)

    print("\n=== aggregate ===")
    for k, v in results.aggregate(rows).items():
        print(f"  {k}: {v}")
    print(f"\nresults: {batch_dir}")


if __name__ == "__main__":
    main()
