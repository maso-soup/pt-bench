#!/usr/bin/env python3
"""pt-bench runner CLI: execute one (arm x scenario) cell for N repetitions.

    pt-bench --arm example__noop --scenario juice-shop --repeats 1

Flow per repetition, all through scenario-agnostic seams:
    provision -> build run_spec -> invoke adapter -> oracle -> grade -> teardown
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

from bench.core import adapter as adapter_mod
from bench.core import grader, progress, registry, resources, results
from bench.core.scenario import Budget, OracleResult

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


def resolve_repo(value: str) -> str:
    """Make an arm's `repo` portable: expand ${VARS} and ~. A `baselines/<name>`
    value maps to a writable materialized baseline dir (works from an installed
    package); absolute paths pass through; other relatives resolve against cwd."""
    expanded = os.path.expanduser(_expand_vars(str(value)))
    parts = Path(expanded).parts
    if len(parts) >= 2 and parts[0] == "baselines":
        return str(resources.baseline_dir(parts[1]))
    p = Path(expanded)
    return str(p if p.is_absolute() else (Path.cwd() / p))


def _is_baseline_repo(repo: str | None) -> bool:
    """True if `repo` is a materialized per-user baseline dir (see
    resources.baseline_dir), i.e. it lives directly under the baselines root."""
    if not repo:
        return False
    try:
        return Path(repo).resolve().parent == resources.baselines_root().resolve()
    except OSError:
        return False


def load_arm(name: str, arms_dir: str | None = None) -> dict:
    path = resources.find_arm(name, arms_dir)
    if path is None:
        searched = "\n  ".join(str(d) for d in resources.arms_search_dirs(arms_dir))
        sys.exit(f"no such arm: {name}.yaml\nsearched:\n  {searched}")
    arm = yaml.safe_load(path.read_text())
    cfg = arm.get("adapter_config") or {}
    if cfg.get("repo"):  # normalize to an absolute host path once, at load time
        cfg["repo"] = resolve_repo(cfg["repo"])
        arm["adapter_config"] = cfg
    return arm


def adapter_cmd(adapter_name: str) -> list[str]:
    entry = resources.adapters_dir() / adapter_name / "adapter.py"
    if not entry.exists():
        sys.exit(f"adapter has no adapter.py: {entry}")
    return [sys.executable, str(entry)]


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def ensure_dashboard(results_dir: Path, port: int, host: str = "127.0.0.1") -> str:
    """Start the results dashboard as a DETACHED background process so it outlives
    this run — you can keep reading results after the benchmark finishes. Idempotent:
    if something is already listening on the port, reuse it instead of spawning a
    duplicate. Stop it later with `pkill -f bench.dashboard.app`."""
    url = f"http://{host}:{port}"
    if _port_open(host, port):
        print(f"  dashboard already up: {url}")
        return url
    results_dir.mkdir(parents=True, exist_ok=True)
    log = (results_dir / "dashboard.log").open("a")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "bench.dashboard.app",
             "--results-dir", str(results_dir), "--host", host, "--port", str(port)],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)  # detach: survives this process exiting
    finally:
        log.close()  # the child keeps its own inherited fd
    for _ in range(30):  # wait up to ~3s for it to bind
        if _port_open(host, port):
            break
        time.sleep(0.1)
    print(f"  dashboard started (detached): {url}")
    return url


def _budget_code(reason: str) -> str:
    """Map a budget_exceeded reason string to which configured limit was hit."""
    r = (reason or "").lower()
    if "wall" in r:
        return "budget_wall"
    if "token" in r:
        return "budget_tokens"
    if "usd" in r or "$" in (reason or ""):
        return "budget_usd"
    return "budget"


def _find_contamination(tool_calls: list[dict], markers) -> dict | None:
    """The first tool call referencing one of the scenario's contamination markers,
    or None.

    Matches against the whole serialized call — name, arguments and any recorded
    result — because the reference can arrive in any of them: a WebFetch url, a
    WebSearch query, a `git clone` or `curl` in a Bash command, or a search result
    echoed back. Substring matching over the raw JSON is deliberately blunt; a
    false positive costs one re-run, a false negative silently turns a retrieval
    run into a discovery result."""
    if not markers:
        return None
    for i, call in enumerate(tool_calls):
        blob = json.dumps(call, default=str).lower()
        for marker in markers:
            if marker.lower() in blob:
                return {"marker": marker, "tool": call.get("tool") or "unknown",
                        "call_index": i}
    return None


def _classify_stop(status: dict) -> dict:
    """Turn an adapter terminal status into a structured stop reason
    {code, detail}. Used for autonomous runs and for adapter-side limit hits."""
    st = status.get("status")
    reason = status.get("reason", "") or ""
    if st == "completed":
        return {"code": "agent_completed", "detail": "agent stopped on its own"}
    if st == "refused":
        return {"code": "refused", "detail": reason or "safety refusal"}
    if st == "error":
        return {"code": "error", "detail": reason}
    if st == "budget_exceeded":
        return {"code": _budget_code(reason), "detail": reason}
    return {"code": "unknown", "detail": reason}


def _drive(arm: dict, scenario, handle, gt, cmd, budget: Budget, workdir: Path,
           mode: str) -> tuple[dict, list[Path], dict]:
    """Run the adapter once (autonomous) or in a continuation loop (max-coverage)
    until the agent solves everything, plateaus (an iteration adds no new solves),
    errors, hits a budget limit, or reaches the iteration cap. State is preserved
    across iterations so the agent resumes; the harness reads the oracle to decide
    when to stop while the agent itself stays blind to the score.

    A safety refusal is terminal in autonomous mode but NOT in max-coverage: there
    a refused iteration is followed by a fresh continuation attempt, so one guarded
    turn can't end a coverage run."""
    total_wall = budget.wall_time_s
    started = time.time()
    max_iters = MAX_CONTINUATION_ITERS if mode == "max-coverage" else 1
    iter_dirs: list[Path] = []
    status: dict = {"status": "error", "reason": "no iteration ran"}
    stop: dict = {"code": "error", "detail": "no iteration ran"}
    prev_solved = -1
    # Budgets are TOTALS across the whole cell, so continuation iterations spend
    # from one shared pool. Track what earlier iterations used and hand each
    # iteration only what's left — otherwise every iteration would get the full
    # token/USD budget again and a max-coverage run could spend N times the cap.
    spent_tokens = 0.0
    spent_usd = 0.0
    hit_max = True  # cleared whenever we break out for a more specific reason

    for it in range(max_iters):
        remaining = None
        if total_wall is not None:
            remaining = int(total_wall - (time.time() - started))
            if remaining <= 0:
                status = {"status": "budget_exceeded", "reason": "wall_time_s exhausted"}
                stop = {"code": "budget_wall",
                        "detail": "wall-time budget exhausted between iterations"}
                hit_max = False
                break
        rem_tokens = None
        if budget.tokens is not None:
            rem_tokens = int(budget.tokens - spent_tokens)
            if rem_tokens <= 0:
                status = {"status": "budget_exceeded", "reason": "token budget exhausted"}
                stop = {"code": "budget_tokens",
                        "detail": "token budget exhausted between iterations"}
                hit_max = False
                break
        rem_usd = None
        if budget.usd is not None:
            rem_usd = round(budget.usd - spent_usd, 4)
            if rem_usd <= 0:
                status = {"status": "budget_exceeded", "reason": "USD budget exhausted"}
                stop = {"code": "budget_usd",
                        "detail": "USD budget exhausted between iterations"}
                hit_max = False
                break

        iter_dir = workdir / f"iter-{it:02d}"
        iter_dirs.append(iter_dir)
        it_budget = Budget(usd=rem_usd, tokens=rem_tokens, wall_time_s=remaining)
        spec = adapter_mod.build_run_spec(
            handle=handle, model=arm.get("model"), budget=it_budget, workdir=iter_dir,
            adapter_config=arm.get("adapter_config", {}), continuation=(it > 0))
        backstop = (remaining + TIMEOUT_GRACE_S) if remaining is not None else None

        print(f"  {'running adapter' if it == 0 else f'continuing (iter {it})'} ...")
        status = adapter_mod.run_adapter(
            adapter_cmd=cmd, run_spec=spec, workdir=iter_dir, wall_time_s=backstop)
        print(f"  adapter status: {status.get('status')}")

        # Draw this iteration's spend from the shared pool for the next one.
        it_usage, it_calls = adapter_mod.read_artifacts(iter_dir)
        spent_tokens += (it_usage.get("tokens_in") or 0) + (it_usage.get("tokens_out") or 0)
        spent_usd += it_usage.get("cost_usd") or 0

        # Did the agent go and read the answer key? Checked before any mode branch,
        # and terminal in BOTH modes: unlike a safety refusal, this cannot be
        # recovered from by continuing — once the agent has the solutions, every
        # later iteration is contaminated too. Coverage is still graded and written;
        # the stop code is what marks the run unusable as a discovery measurement.
        contam = _find_contamination(it_calls, getattr(scenario, "contamination_markers", ()))
        if contam:
            print(f"  CONTAMINATION: iter {it} tool call #{contam['call_index']} "
                  f"({contam['tool']}) referenced {contam['marker']} — the agent "
                  f"fetched the answer key. Ending the run; this result measures "
                  f"retrieval, not discovery.")
            stop = {"code": "contamination",
                    "detail": (f"tool call #{contam['call_index']} ({contam['tool']}) "
                               f"referenced {contam['marker']} in iteration {it}")}
            hit_max = False
            break

        if mode != "max-coverage":
            # Autonomous: the single run's terminal state is the whole story —
            # the agent stopped itself, or the adapter hit a configured limit.
            stop = _classify_stop(status)
            hit_max = False
            break
        # max-coverage: a hard error or a budget/limit stop ends the cell —
        # continuing would just re-hit it.
        if status.get("status") in ("error", "budget_exceeded"):
            stop = _classify_stop(status)
            hit_max = False
            break
        solved = len(scenario.oracle(handle).solved)
        # A safety refusal does NOT end a max-coverage cell: give the agent a
        # fresh continuation iteration to make progress a different way instead of
        # letting one refused turn terminate the whole run. (Autonomous mode still
        # treats a refusal as terminal — the `mode != max-coverage` branch breaks
        # first, before reaching here.) A
        # refusal makes no new solves, so it must not advance prev_solved or count
        # as a plateau; the shared budget and the MAX_CONTINUATION_ITERS cap bound
        # how long a persistently-refusing agent can keep retrying.
        if status.get("status") == "refused":
            print(f"  iter {it} ended on a safety refusal "
                  f"({solved}/{len(gt)} solved) — continuing to the next iteration")
            continue
        print(f"  progress after iter {it}: {solved}/{len(gt)} solved")
        if solved >= len(gt):
            stop = {"code": "all_solved", "detail": f"solved all {len(gt)} findings"}
            hit_max = False
            break
        if it > 0 and solved <= prev_solved:  # plateau: no new solves this iteration
            print(f"  plateau: iter {it} added no new solves — stopping")
            stop = {"code": "plateau", "detail": f"iter {it} added no new solves"}
            hit_max = False
            break
        prev_solved = solved

    if hit_max:  # loop ran the full continuation cap without another stop firing
        stop = {"code": "max_iterations",
                "detail": f"reached the {max_iters}-iteration cap"}

    return status, iter_dirs, stop


def run_cell(arm: dict, scenario_id: str, repeats: int, keep_up: bool, *,
             mode: str, results_dir: Path, progress_enabled: bool,
             progress_interval: float | None,
             scenario_config: dict | None = None) -> tuple[list[dict], Path]:
    budget = Budget.from_dict(arm.get("budget"))
    cmd = adapter_cmd(arm["adapter"])
    started_at = dt.datetime.now()
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    # CLI-supplied scenario config (e.g. the HTB machine) overrides the arm's.
    merged_cfg = {**(arm.get("scenario_config") or {}), **(scenario_config or {})}
    # A probe instance just reads scenario-level facts for the manifest (whether
    # coverage is target-verified); each repetition still gets its own instance.
    verified = registry.get_scenario(scenario_id, merged_cfg).verified
    # One batch dir per cell groups its repetitions and holds provenance.
    batch_dir = results_dir / arm["name"] / scenario_id / stamp
    results.write_json(results.make_manifest(
        arm=arm, scenario=scenario_id, mode=mode, repeats=repeats,
        started_at=started_at.isoformat(timespec="seconds"), budget=budget.to_dict(),
        pt_bench_dir=resources.package_root(),
        agent_repo=(arm.get("adapter_config") or {}).get("repo"),
        scenario_config=merged_cfg, verified=verified),
        batch_dir / "manifest.json")
    rows: list[dict] = []

    for i in range(repeats):
        workdir = batch_dir / f"r{i}"
        print(f"\n=== {arm['name']} x {scenario_id} [{mode}] : repeat {i} ===")
        scenario = registry.get_scenario(scenario_id, merged_cfg)

        handle = None
        try:
            print("  provisioning target ...")
            handle = scenario.provision()
            # Some scenarios (e.g. HTB) read the agent's self-reported artifacts
            # from this repetition's output dir; hand them the path.
            handle.meta["run_workdir"] = str(workdir)
            gt = scenario.ground_truth(handle)
            print(f"  target up: {handle.target_url}  ({len(gt)} challenges)")

            # Clear this agent's prior on-disk state once per repetition (before
            # the first iteration) so repetitions never read each other's
            # artifacts, while continuation iterations keep state to resume.
            reset_paths = (arm.get("adapter_config") or {}).get("reset_paths") or []
            for src, dest in adapter_mod.reset_agent_state(reset_paths=reset_paths):
                print(f"  reset: {src} -> trash ({dest.parent.name})")

            # A baseline arm's cwd IS a managed baseline dir — reset it to the
            # pristine template here rather than via reset_paths (which would move
            # the cwd to trash and break the run). Gives each repetition a clean cwd.
            repo = (arm.get("adapter_config") or {}).get("repo")
            if _is_baseline_repo(repo):
                resources.baseline_dir(Path(repo).name, refresh=True)
                print(f"  baseline cwd reset to template: {repo}")

            interval = (progress_interval if progress_interval is not None
                        else getattr(scenario, "progress_interval_s", 5.0))
            poller = progress.ProgressPoller(
                scenario, handle, ground_truth=gt, workdir=workdir, interval=interval,
                enabled=progress_enabled and scenario.supports_live_progress)

            poller.start()
            try:
                status, iter_dirs, stop_reason = _drive(arm, scenario, handle, gt,
                                                        cmd, budget, workdir, mode)
            finally:
                poller.stop()

            oracle = scenario.oracle(handle)
            usage, tool_calls = adapter_mod.read_artifacts_multi(iter_dirs)
            # Coverage is the UNION of everything solved during the run (the live
            # poller's history) with the final oracle read — so a target that
            # transiently drops solved-state between max-coverage iterations (e.g.
            # the app restarting and re-seeding) can't erase earlier progress. The
            # final read alone would report only what happens to be solved right now.
            solved_union = set(oracle.solved) | poller.solved_keys
            cov = grader.coverage(gt, OracleResult(solved=solved_union))
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
                workdir=str(workdir), stop_reason=stop_reason)
            results.write_row(row, workdir / "result.json")
            rows.append(row)
            print(f"  solved {cov['solved']}/{cov['total']}  "
                  f"(weighted {cov['coverage_weighted']})  iters={len(iter_dirs)}  "
                  f"stop={stop_reason['code']}  cost={eff['cost_usd']}")
        finally:
            if handle and not keep_up:
                print("  tearing down ...")
                scenario.teardown(handle)

    results.write_json(results.aggregate(rows), batch_dir / "summary.json")
    return rows, batch_dir


def _scenario_config_from_args(args) -> dict:
    """Collect per-run scenario config from the CLI: the convenience flags plus
    any --scenario-config KEY=VALUE pairs. Later sources win on key collisions."""
    cfg: dict = {}
    if args.target is not None:
        cfg["target"] = args.target
    if args.machine_name is not None:
        cfg["machine_name"] = args.machine_name
    if args.difficulty is not None:
        cfg["difficulty"] = args.difficulty
    for item in args.scenario_config or []:
        if "=" not in item:
            raise SystemExit(f"--scenario-config expects KEY=VALUE, got: {item!r}")
        k, v = item.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(prog="pt-bench", description="Run one pt-bench cell.")
    ap.add_argument("--arm", help="arm to run (omit only when using --dashboard alone)")
    ap.add_argument("--scenario",
                    help="scenario to run (omit only when using --dashboard alone)")
    ap.add_argument("--repeats", type=int, default=1)
    # Per-run scenario inputs. The three convenience flags are for the htb
    # scenario (a manually-supplied box); --scenario-config is the generic
    # escape hatch for any scenario. All merge into the scenario config and
    # override the arm's own scenario_config.
    ap.add_argument("--target", default=None,
                    help="target host/IP for scenarios that take one (htb)")
    ap.add_argument("--machine-name", default=None,
                    help="machine name for the htb scenario (labels results)")
    ap.add_argument("--difficulty", type=int, default=None,
                    help="difficulty rating for the htb scenario (weights flags)")
    ap.add_argument("--scenario-config", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="extra scenario config, repeatable (e.g. reachability_port=22)")
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
    ap.add_argument("--arms-dir", default=None,
                    help="extra arms dir to search first (else $PTBENCH_ARMS_DIR, "
                         "~/.config/pt-bench/arms, then the packaged defaults)")
    ap.add_argument("--results-dir", default=None,
                    help="where to store results (default: $PTBENCH_RESULTS_DIR or "
                         "~/.local/share/pt-bench/results; kept out of the repo)")
    ap.add_argument("--dashboard", action="store_true",
                    help="run alone (no --arm/--scenario) to just start the dashboard "
                         "and exit; note the dashboard also auto-starts with every run")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="do not auto-start the dashboard for this run")
    ap.add_argument("--dashboard-port", type=int, default=8008)
    args = ap.parse_args()

    results_dir = results.resolve_results_dir(args.results_dir)

    # `pt-bench --dashboard` on its own just starts the dashboard (no benchmark).
    if args.dashboard and not (args.arm and args.scenario):
        ensure_dashboard(results_dir, args.dashboard_port)
        return
    if not (args.arm and args.scenario):
        ap.error("--arm and --scenario are required "
                 "(or run `pt-bench --dashboard` alone to just start the dashboard)")

    progress_enabled = args.progress if args.progress is not None else sys.stdout.isatty()

    # The dashboard auto-starts with every run so results are always viewable
    # (detached; stays up afterward). Opt out with --no-dashboard.
    dashboard_url = None
    if not args.no_dashboard:
        dashboard_url = ensure_dashboard(results_dir, args.dashboard_port)

    scenario_config = _scenario_config_from_args(args)

    arm = load_arm(args.arm, args.arms_dir)
    rows, batch_dir = run_cell(arm, args.scenario, args.repeats, args.keep_up,
                               mode=args.mode, results_dir=results_dir,
                               progress_enabled=progress_enabled,
                               progress_interval=args.progress_interval,
                               scenario_config=scenario_config)

    print("\n=== aggregate ===")
    for k, v in results.aggregate(rows).items():
        print(f"  {k}: {v}")
    print(f"\nresults: {batch_dir}")
    if dashboard_url:
        print(f"dashboard (still up): {dashboard_url}")


if __name__ == "__main__":
    main()
