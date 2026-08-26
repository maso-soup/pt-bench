"""In max-coverage mode a safety refusal must NOT end the cell — the loop starts a
fresh continuation iteration instead. Autonomous mode still treats a refusal as
terminal. Drives bench.cli._drive with faked adapter/scenario so no real agent or
target is needed."""
from types import SimpleNamespace

import pytest

from bench import cli
from bench.core.scenario import Budget, ItemSpec, OracleResult


class _FakeScenario:
    """oracle() returns a scripted solved-set, one entry per iteration."""
    def __init__(self, solved_per_iter):
        self._script = solved_per_iter
        self.i = 0
    def oracle(self, handle):
        s = self._script[min(self.i, len(self._script) - 1)]
        self.i += 1
        return OracleResult(solved=set(s))


@pytest.fixture
def patched(monkeypatch):
    """Fake the three adapter entry points _drive calls; run_adapter yields a
    scripted list of terminal statuses, one per iteration."""
    statuses = []
    monkeypatch.setattr(cli.adapter_mod, "build_run_spec", lambda **k: None)
    monkeypatch.setattr(cli.adapter_mod, "read_artifacts", lambda d: ({}, []))
    def fake_run(**kwargs):
        return statuses.pop(0)
    monkeypatch.setattr(cli.adapter_mod, "run_adapter", fake_run)
    return statuses


def _gt(n):
    return [ItemSpec(key=str(i), category="c", difficulty=1) for i in range(n)]


def _drive(scenario, mode):
    return cli._drive(arm={"name": "a", "model": "m", "adapter_config": {}},
                      scenario=scenario, handle=None, gt=_gt(5), cmd=["x"],
                      budget=Budget(usd=None, tokens=None, wall_time_s=None),
                      workdir=cli.Path("/tmp/xdrive"), mode="max-coverage" if mode=="mc" else mode)


def test_maxcov_refusal_continues_then_solves(patched):
    # iter0 refuses (no solves), iter1 refuses (no solves), iter2 solves all 5.
    patched[:] = [{"status": "refused", "reason": "cyber"},
                  {"status": "refused", "reason": "cyber"},
                  {"status": "completed"}]
    sc = _FakeScenario([set(), set(), {"0","1","2","3","4"}])
    status, iter_dirs, stop = _drive(sc, "mc")
    assert len(iter_dirs) == 3            # both refusals were retried, not terminal
    assert stop["code"] == "all_solved"   # ended on real progress, not the refusal
    assert status["status"] == "completed"


def test_maxcov_refusal_is_not_counted_as_plateau(patched):
    # A refusal at iter1 (no new solves) must not trip the plateau stop; iter2
    # makes progress, iter3 plateaus for real.
    patched[:] = [{"status": "completed"},                 # iter0: 2 solved
                  {"status": "refused", "reason": "cyber"},  # iter1: refusal, 2 solved
                  {"status": "completed"},                 # iter2: 3 solved (progress)
                  {"status": "completed"}]                 # iter3: 3 solved -> plateau
    sc = _FakeScenario([{"0","1"}, {"0","1"}, {"0","1","2"}, {"0","1","2"}])
    status, iter_dirs, stop = _drive(sc, "mc")
    assert len(iter_dirs) == 4
    assert stop["code"] == "plateau"      # the real plateau, at iter3 — not iter1


def test_autonomous_refusal_is_terminal(patched):
    patched[:] = [{"status": "refused", "reason": "cyber"}]
    sc = _FakeScenario([set()])
    status, iter_dirs, stop = _drive(sc, "autonomous")
    assert len(iter_dirs) == 1            # single run, no continuation
    assert stop["code"] == "refused"
    assert status["status"] == "refused"
