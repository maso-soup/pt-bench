"""Live progress: poll a scenario's oracle mid-run and report solves as they land.

Scenario-agnostic. It consumes an `OracleResult` plus the ground-truth
`ItemSpec` list, so it knows nothing about any specific target. Only scenarios
that set `supports_live_progress = True` are polled — their oracle must be cheap
and safe to call repeatedly during a run. This reuses `scenario.oracle()`, which
is already defined as "what has actually been solved right now", so progress is
still measured from target-side truth, never the agent's self-report.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from .scenario import ItemSpec, OracleResult, Scenario, TargetHandle

_CLEAR_LINE = "\r\033[K"  # carriage return + erase to end of line


class ProgressReporter:
    """Formats live solve events. On a TTY it keeps an in-place counter and
    prints a permanent line per newly solved item; off a TTY it prints plain
    append lines on change only, so redirected logs stay clean."""

    def __init__(self, ground_truth: list[ItemSpec], *,
                 stream: TextIO | None = None, lock: threading.Lock | None = None):
        self._by_key = {it.key: it for it in ground_truth}
        self.total = len(ground_truth)
        self._weight_total = sum(it.effective_weight() for it in ground_truth) or 1.0
        self._seen: set[str] = set()
        self._stream = stream or sys.stdout
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._lock = lock or threading.Lock()

    @property
    def solved(self) -> int:
        return len(self._seen)

    @property
    def weighted(self) -> float:
        solved_weight = sum(self._by_key[k].effective_weight() for k in self._seen)
        return solved_weight / self._weight_total

    def update(self, result: OracleResult) -> list[str]:
        """Register an oracle sample: print any newly solved items, refresh the
        counter, and return the newly solved keys (stable order)."""
        solved_now = {k for k in result.solved if k in self._by_key}
        new_keys = sorted(solved_now - self._seen)
        with self._lock:
            for k in new_keys:
                self._seen.add(k)  # add before printing so the count increments per line
                self._print_solved(k)
            self._seen = solved_now  # exact resync (harmless when already equal)
            self._refresh_counter()
        return new_keys

    def finish(self) -> None:
        """Move the cursor off the in-place counter line at the end of a run."""
        if self._tty:
            with self._lock:
                self._stream.write(_CLEAR_LINE)
                self._stream.flush()

    def _print_solved(self, key: str) -> None:
        it = self._by_key[key]
        if self._tty:
            self._stream.write(_CLEAR_LINE)  # clear counter before a permanent line
        self._stream.write(
            f"  [+] solved  {it.name or key}   "
            f"[{it.category}, difficulty {it.difficulty}]   "
            f"{self.solved}/{self.total}\n"
        )
        self._stream.flush()

    def _refresh_counter(self) -> None:
        if not self._tty:
            return
        # In-place line: carriage return, no newline, so the next refresh overwrites.
        self._stream.write(
            f"\r      {self.solved}/{self.total} solved "
            f"(weighted {self.weighted:.2f}) ..."
        )
        self._stream.flush()


class ProgressPoller:
    """Polls `scenario.oracle(handle)` on an interval in a daemon thread while the
    agent runs, feeding a `ProgressReporter` and appending a solve curve to
    `progress.jsonl` in the run's workdir. A no-op when disabled."""

    def __init__(self, scenario: Scenario, handle: TargetHandle, *,
                 ground_truth: list[ItemSpec], workdir: Path,
                 interval: float = 5.0, enabled: bool = True,
                 stream: TextIO | None = None):
        self._scenario = scenario
        self._handle = handle
        self._interval = max(0.1, float(interval))
        self._enabled = enabled
        self._workdir = Path(workdir)
        self._reporter = ProgressReporter(ground_truth, stream=stream)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._first_solve_s: float | None = None
        self._last_solve_s: float | None = None
        self._samples = 0
        self._fh: TextIO | None = None

    def start(self) -> None:
        if not self._enabled:
            return
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._fh = (self._workdir / "progress.jsonl").open("w")
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="progress-poller",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._enabled or self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=self._interval + 5)
        self._reporter.finish()
        if self._fh:
            self._fh.close()
            self._fh = None

    def summary(self) -> dict:
        return {
            "first_solve_s": self._first_solve_s,
            "last_solve_s": self._last_solve_s,
            "samples": self._samples,
            "solved": self._reporter.solved,
            "total": self._reporter.total,
        }

    # -- internals -----------------------------------------------------------
    def _loop(self) -> None:
        # Poll immediately, then every interval; one last poll after stop to catch
        # solves that landed during the final interval.
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self._interval)
        self._poll_once()

    def _poll_once(self) -> None:
        try:
            result = self._scenario.oracle(self._handle)
        except Exception:  # noqa: BLE001 - a transient blip must never crash a run
            return
        new_keys = self._reporter.update(result)
        self._samples += 1
        if new_keys:
            elapsed = round(time.time() - self._started_at, 1)
            if self._first_solve_s is None:
                self._first_solve_s = elapsed
            self._last_solve_s = elapsed
            self._write_sample(elapsed, new_keys)

    def _write_sample(self, elapsed: float, new_keys: list[str]) -> None:
        if not self._fh:
            return
        self._fh.write(json.dumps({
            "elapsed_s": elapsed,
            "solved": self._reporter.solved,
            "total": self._reporter.total,
            "weighted": round(self._reporter.weighted, 4),
            "newly_solved": new_keys,
        }) + "\n")
        self._fh.flush()
