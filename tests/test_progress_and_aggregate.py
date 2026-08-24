"""Coverage must be robust to a target that transiently drops solved-state (e.g.
a max-coverage app restart), and a budget/refusal-stopped run must still produce
a populated summary."""
import io
from pathlib import Path

from bench.core.progress import ProgressReporter, ProgressPoller
from bench.core.scenario import ItemSpec, OracleResult
from bench.core import results as R


def _reporter(n):
    gt = [ItemSpec(key=f"k{i}", category="c", difficulty=1) for i in range(n)]
    return ProgressReporter(gt, stream=io.StringIO())


def test_reporter_union_survives_target_reset():
    r = _reporter(107)
    r.update(OracleResult(solved={f"k{i}" for i in range(21)}))   # iter 1: 21 solved
    assert r.solved == 21
    r.update(OracleResult(solved={"k0", "k1", "k2"}))             # target reset -> 3
    assert r.solved == 21                                         # not erased
    r.update(OracleResult(solved={"k50", "k51", "k52"}))         # 3 genuinely new
    assert r.solved == 24                                         # 21 + 3, cumulative
    assert {"k5", "k50"} <= r.solved_keys


def test_reporter_ignores_unknown_keys():
    r = _reporter(3)
    r.update(OracleResult(solved={"k0", "nonexistent"}))
    assert r.solved == 1 and r.solved_keys == {"k0"}


def test_poller_solved_keys_delegates_to_reporter():
    gt = [ItemSpec(key="k0", category="c", difficulty=1)]
    p = ProgressPoller(scenario=None, handle=None, ground_truth=gt,
                       workdir=Path("/tmp"), enabled=False)  # disabled: never polls
    p._reporter.update(OracleResult(solved={"k0"}))
    assert p.solved_keys == {"k0"}


def _row(status, ratio=0.2, solved=21, cost=1.0):
    return R.make_row(
        arm="a", scenario="s", repeat=0, model="m", status=status,
        coverage={"coverage": ratio, "coverage_weighted": ratio,
                  "solved": solved, "total": 107},
        efficiency={"cost_usd": cost, "tool_calls": 10}, derived={}, workdir="/x",
        curve={"first_solve_s": 5.0, "at": {"1.0": {"solved": solved, "ratio": ratio}}},
        stop_reason={"code": "budget_tokens"})


def test_budget_run_populates_summary():
    agg = R.aggregate([_row("budget_exceeded")])
    assert agg["coverage_mean"] == 0.2          # not blank
    assert agg["cost_usd_mean"] == 1.0
    assert agg["tool_calls_mean"] == 10
    assert agg["n_completed"] == 0 and agg["n_graded"] == 1
    assert agg["coverage_at_budget_mean"]["1.0"] == 0.2


def test_error_run_is_not_graded():
    agg = R.aggregate([_row("error")])
    assert agg["n_graded"] == 0
    assert agg["cost_usd_mean"] is None         # nothing to grade


def test_refused_run_is_graded():
    agg = R.aggregate([_row("refused")])
    assert agg["n_graded"] == 1 and agg["coverage_mean"] == 0.2
