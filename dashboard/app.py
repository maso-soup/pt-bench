#!/usr/bin/env python3
"""pt-bench dashboard — a small local web UI to compare runs side by side.

Reads the flat-JSON results tree directly (no database) and serves a single-page
compare view. Isolated, optional component:

    pip install -e '.[dashboard]'
    python dashboard/app.py            # or: --results-dir / --port

A "run" is a batch dir (one per cell) containing manifest.json + summary.json and
one r<i>/ per repetition. The results dir is resolved the same way the runner
resolves it (shared bench.core.results.resolve_results_dir).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flask import Flask, abort, jsonify, send_from_directory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from bench.core import results as results_mod  # noqa: E402

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
        items.append({
            "id": d.relative_to(RESULTS_DIR).as_posix(),
            "arm": m.get("arm"), "scenario": m.get("scenario"),
            "mode": m.get("mode"), "model": m.get("model"),
            "started_at": m.get("started_at"), "repeats": m.get("repeats"),
            "coverage_mean": s.get("coverage_mean"),
            "coverage_weighted_mean": s.get("coverage_weighted_mean"),
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
