"""Adapter invocation: the process boundary between the benchmark and any
agent-under-test.

The contract (see adapters/PROTOCOL.md) is intentionally a subprocess + files,
not a Python import, so an adapter can be written in any language. The runner:
  1. writes run_spec.json into the workdir,
  2. execs `<adapter> --spec <path>`,
  3. reads back tool_calls.jsonl, usage.json, status.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .scenario import Budget, TargetHandle


def build_run_spec(*, handle: TargetHandle, model: str | None, budget: Budget,
                   workdir: Path, adapter_config: dict[str, Any]) -> dict[str, Any]:
    spec = handle.public()
    spec.update({
        "model": model,
        "budget": budget.to_dict(),
        "workdir": str(workdir),
        "extra": adapter_config or {},
    })
    return spec


def reset_agent_state(*, reset_paths: list[str],
                      trash_root: Path | None = None) -> list[tuple[Path, Path]]:
    """Clear an agent's on-disk state before a run so repetitions never read each
    other's artifacts — the agent-side analogue of `compose down -v`.

    Each entry is a directory (or file) to clear, with `~` expanded against the
    real HOME. To stay reversible, matching paths are *moved* to a timestamped
    trash dir (default ~/.pt-bench-trash/<stamp>/), not hard-deleted. Returns the
    (source, destination) pairs actually moved."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    trash = (trash_root or Path.home() / ".pt-bench-trash") / stamp
    moved: list[tuple[Path, Path]] = []
    for entry in reset_paths or []:
        src = Path(os.path.expanduser(entry))
        if not src.exists():
            continue
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / str(src).lstrip("/").replace("/", "__")
        shutil.move(str(src), str(dest))
        moved.append((src, dest))
    return moved


def run_adapter(*, adapter_cmd: list[str], run_spec: dict[str, Any],
                workdir: Path, wall_time_s: int | None) -> dict[str, Any]:
    """Invoke the adapter and return its status dict. Enforces wall_time as a
    hard subprocess timeout; on timeout, synthesizes a budget_exceeded status."""
    workdir.mkdir(parents=True, exist_ok=True)
    spec_path = workdir / "run_spec.json"
    spec_path.write_text(json.dumps(run_spec, indent=2))

    cmd = list(adapter_cmd) + ["--spec", str(spec_path)]
    started = time.time()
    log = (workdir / "adapter.log").open("w")
    try:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                       timeout=wall_time_s, check=False)
    except subprocess.TimeoutExpired:
        _write_if_absent(workdir / "status.json",
                         {"status": "budget_exceeded", "reason": "wall_time_s exceeded"})
    finally:
        log.close()
    elapsed = time.time() - started

    status = _read_json(workdir / "status.json",
                        default={"status": "error", "reason": "adapter wrote no status.json"})
    status.setdefault("wall_time_s", round(elapsed, 1))
    return status


def read_artifacts(workdir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    usage = _read_json(workdir / "usage.json", default={})
    if "wall_time_s" not in usage:
        st = _read_json(workdir / "status.json", default={})
        if "wall_time_s" in st:
            usage["wall_time_s"] = st["wall_time_s"]
    tool_calls = _read_jsonl(workdir / "tool_calls.jsonl")
    return usage, tool_calls


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_if_absent(path: Path, data: dict[str, Any]) -> None:
    if not path.exists():
        path.write_text(json.dumps(data, indent=2))
