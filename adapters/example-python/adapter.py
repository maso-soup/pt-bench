#!/usr/bin/env python3
"""Minimal reference adapter.

This is NOT a real agent. It is the smallest thing that satisfies the protocol:
it reads a run_spec, makes a few harmless HTTP requests against the target
(recording each as a tool call), and writes the three output artifacts. It exists
to validate the harness end-to-end without spending money, and to show exactly
what any adapter must produce. A couple of the requests happen to solve trivial
Juice Shop challenges, so the oracle has something real to detect.
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path

# Harmless GETs; a couple correspond to easy Juice-Shop challenges (confidential
# document, exposed metrics). Best-effort — zero solves is still a valid result.
PROBES = ["/", "/ftp/acquisitions.md", "/metrics", "/robots.txt"]


def get(url: str, timeout: int = 10):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pt-bench-example/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(4096)
            code = r.status
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", -1)
    return code, round(time.time() - t0, 3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    spec = json.loads(Path(ap.parse_args().spec).read_text())

    workdir = Path(spec["workdir"])
    base = spec["target_url"].rstrip("/")
    started = time.time()

    tool_log = (workdir / "tool_calls.jsonl").open("w")
    for path in PROBES:
        url = base + path
        code, dur = get(url)
        tool_log.write(json.dumps({
            "ts": time.time(), "tool": "http_get",
            "args": {"url": url}, "result": {"status": code}, "duration_s": dur,
        }) + "\n")
    tool_log.close()

    (workdir / "usage.json").write_text(json.dumps({
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
        "wall_time_s": round(time.time() - started, 3),
    }, indent=2))
    (workdir / "status.json").write_text(json.dumps({
        "status": "completed", "reason": "reference adapter finished probes",
    }, indent=2))


if __name__ == "__main__":
    main()
