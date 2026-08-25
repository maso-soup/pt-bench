#!/usr/bin/env python3
"""pt-bench dashboard — a small local web UI to compare runs side by side.

Reads the flat-JSON results tree directly (no database) and serves a single-page
compare view. Runs as `pt-bench-dashboard` (a console command) or
`python -m bench.dashboard.app`; options: --results-dir / --host / --port.

A "run" is a batch dir (one per cell) containing manifest.json + summary.json and
one r<i>/ per repetition. The results dir is resolved the same way the runner
resolves it (shared bench.core.results.resolve_results_dir).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from bench.core import results as results_mod

HERE = Path(__file__).resolve().parent

app = Flask(__name__)
RESULTS_DIR = results_mod.resolve_results_dir()  # overridden in main()


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _safe_batch_dir(rid: str) -> Path:
    """Resolve a run id under RESULTS_DIR, refusing path traversal."""
    root = RESULTS_DIR.resolve()
    d = (RESULTS_DIR / rid).resolve()
    if d != root and root not in d.parents:
        abort(403)
    if not (d / "manifest.json").exists():
        abort(404)
    return d


@app.get("/api/runs")
def api_runs():
    items = []
    for mf in RESULTS_DIR.rglob("manifest.json"):
        d = mf.parent
        m = _read(mf) or {}
        s = _read(d / "summary.json") or {}
        sc = m.get("scenario_config") or {}
        items.append({
            "id": d.relative_to(RESULTS_DIR).as_posix(),
            "arm": m.get("arm"), "scenario": m.get("scenario"),
            "mode": m.get("mode"), "model": m.get("model"),
            "started_at": m.get("started_at"), "repeats": m.get("repeats"),
            "coverage_mean": s.get("coverage_mean"),
            "coverage_weighted_mean": s.get("coverage_weighted_mean"),
            # Scenario-specific facts the UI groups/labels by. `verified` defaults
            # True for older runs written before the field existed.
            "verified": m.get("verified", True),
            "manual_verified": bool((m.get("manual_verification") or {}).get("verified")),
            "machine_name": sc.get("machine_name"),
            "difficulty": sc.get("difficulty"),
        })
    items.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return jsonify(items)


@app.get("/api/runs/<path:rid>")
def api_run(rid: str):
    d = _safe_batch_dir(rid)
    rows = [_read(p) for p in sorted(d.glob("r*/result.json"))]
    return jsonify({
        "id": rid,
        "manifest": _read(d / "manifest.json"),
        "summary": _read(d / "summary.json"),
        "rows": [r for r in rows if r],
    })


@app.post("/api/runs/<path:rid>/verify")
def api_verify_run(rid: str):
    """Manually mark (or unmark) a run as human-verified — i.e. you submitted the
    self-reported flags to the platform and confirmed they're legitimate. This is
    recorded separately from the scenario's own `verified` flag (which reflects
    whether coverage came from target-side truth), so provenance stays honest:
    a manual confirmation never masquerades as automated target verification."""
    d = _safe_batch_dir(rid)
    verified = bool((request.get_json(silent=True) or {}).get("verified"))
    mf = d / "manifest.json"
    m = _read(mf) or {}
    if verified:
        m["manual_verification"] = {
            "verified": True,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    else:
        m.pop("manual_verification", None)
    mf.write_text(json.dumps(m, indent=2, sort_keys=True))
    return jsonify({"ok": True, "manual_verified": verified})


@app.delete("/api/runs/<path:rid>")
def api_delete_run(rid: str):
    """Delete one run batch dir from disk, then prune any now-empty
    arm/scenario parent dirs. Path-traversal-guarded by _safe_batch_dir."""
    d = _safe_batch_dir(rid)
    shutil.rmtree(d)
    root = RESULTS_DIR.resolve()
    p = d.parent
    while p != root and root in p.parents:
        try:
            p.rmdir()  # only removes it if empty
        except OSError:
            break
        p = p.parent
    return jsonify({"ok": True, "deleted": rid})


@app.get("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


def main() -> None:
    global RESULTS_DIR
    ap = argparse.ArgumentParser(description="pt-bench results dashboard")
    ap.add_argument("--results-dir", default=None,
                    help="override results location (default: same as the runner)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8008)
    args = ap.parse_args()
    RESULTS_DIR = results_mod.resolve_results_dir(args.results_dir)
    print(f"pt-bench dashboard | results: {RESULTS_DIR} | http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
