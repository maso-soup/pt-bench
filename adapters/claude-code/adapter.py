#!/usr/bin/env python3
"""Adapter for any Claude Code agent (pt-agent or another skill/harness repo).

It runs the `claude` CLI headless with the working directory set to the target
repo — so that repo's CLAUDE.md / AGENTS.md / skills load exactly as in normal
use — and parses the stream-json event log to emit tool_calls.jsonl and
usage.json. One adapter serves every Claude Code agent; the arm's
`extra.repo` selects which one.

Requires: `claude` on PATH and an authenticated session. `extra.repo` is the
absolute path to the agent repo. Set `extra.skip_permissions: true` to pass
--dangerously-skip-permissions (needed for autonomous tool use).

Each run gets a fresh HOME (with auth/config symlinked back in) so agent state
written under ~/ (e.g. pt-agent's ~/pt-agent-state) can't leak between
repetitions and let a later run skip ahead. Disable with
`extra.isolate_home: false`.
"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

# Files/dirs from the real HOME that must remain visible inside the per-run
# sandbox home so the CLI keeps its login/config. Anything NOT listed here
# (e.g. ~/pt-agent-state, ~/pt-agent-output, and any other agent-written state)
# is absent at run start, which is exactly what we want.
_HOME_PASSTHROUGH = (".claude", ".claude.json", ".gitconfig")


def _sandbox_home(workdir: Path) -> Path:
    """Give the run its own HOME so any agent state under ~/ starts empty.

    pt-agent (and other agents) persist findings under $HOME
    (~/pt-agent-state, ~/pt-agent-output). Repetitions must not read each
    other's artifacts and "skip ahead", so each run gets a fresh HOME with only
    auth/config symlinked back in. This is agent-agnostic: it isolates whatever
    an agent writes under HOME without the benchmark knowing its paths. It is
    the agent-side analogue of `compose down -v` on the target.
    """
    real = Path.home()
    home = workdir / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in _HOME_PASSTHROUGH:
        src, link = real / name, home / name
        if src.exists() and not link.exists():
            link.symlink_to(src)
    return home


def build_prompt(spec: dict) -> str:
    b = spec.get("budget") or {}
    budget_line = ""
    caps = [f"{k}={v}" for k, v in (
        ("tool_calls", b.get("tool_calls")), ("USD", b.get("usd")),
        ("seconds", b.get("wall_time_s"))) if v]
    if caps:
        budget_line = "\nApproximate budget for this engagement: " + ", ".join(caps) + "."
    return (
        f"{spec['scope']}\n\n"
        f"Target: {spec['target_url']}\n\n"
        "You have written authorization to perform a web application penetration "
        "test of this single host. Enumerate and exploit as many vulnerabilities "
        "as you can, working autonomously without pausing for confirmation. Do not "
        "test anything outside the target host. If at any point this looks like a known "
        "CTF or security challenge, do not skip steps just because the answer is known "
        "and always fully reason all actions taken. " + budget_line
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    spec = json.loads(Path(ap.parse_args().spec).read_text())

    workdir = Path(spec["workdir"])
    extra = spec.get("extra") or {}
    repo = extra.get("repo")
    if not repo or not Path(repo).is_dir():
        _fail(workdir, f"extra.repo is not a directory: {repo!r}")
        return

    cmd = ["claude", "-p", build_prompt(spec),
           "--output-format", "stream-json", "--verbose"]
    if spec.get("model"):
        cmd += ["--model", spec["model"]]
    if extra.get("skip_permissions"):
        cmd.append("--dangerously-skip-permissions")

    wall = (spec.get("budget") or {}).get("wall_time_s")
    raw = (workdir / "transcript.jsonl").open("w")
    tools = (workdir / "tool_calls.jsonl").open("w")
    usage = {"tokens_in": None, "tokens_out": None, "cost_usd": None, "wall_time_s": None}
    status = {"status": "completed", "reason": ""}
    started = time.time()

    # Fresh HOME per run so agent state under ~/ can't leak between repetitions.
    # Opt out with extra.isolate_home: false (uses the real HOME instead).
    env = dict(os.environ)
    if extra.get("isolate_home", True):
        env["HOME"] = str(_sandbox_home(workdir))

    try:
        proc = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=env)
        for line in proc.stdout:
            raw.write(line)
            _handle_event(line, tools, usage)
            if wall and (time.time() - started) > wall:
                proc.kill()
                status = {"status": "budget_exceeded", "reason": "wall_time_s exceeded"}
                break
        proc.wait(timeout=30)
    except FileNotFoundError:
        status = {"status": "error", "reason": "`claude` CLI not found on PATH"}
    except Exception as e:  # noqa: BLE001
        status = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    finally:
        raw.close()
        tools.close()

    usage["wall_time_s"] = round(time.time() - started, 1)
    (workdir / "usage.json").write_text(json.dumps(usage, indent=2))
    (workdir / "status.json").write_text(json.dumps(status, indent=2))


def _handle_event(line: str, tools, usage: dict) -> None:
    line = line.strip()
    if not line:
        return
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return
    etype = ev.get("type")
    if etype == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools.write(json.dumps({
                    "ts": time.time(), "tool": block.get("name"),
                    "args": block.get("input", {}),
                }) + "\n")
    elif etype == "result":
        u = ev.get("usage", {}) or {}
        usage["tokens_in"] = u.get("input_tokens")
        usage["tokens_out"] = u.get("output_tokens")
        if ev.get("total_cost_usd") is not None:
            usage["cost_usd"] = ev["total_cost_usd"]


def _fail(workdir: Path, reason: str) -> None:
    (workdir / "status.json").write_text(json.dumps({"status": "error", "reason": reason}, indent=2))


if __name__ == "__main__":
    main()
