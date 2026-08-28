"""Contamination detection: a run where the agent fetched the published answer key
measures retrieval, not discovery, and must be distinguishable from one where it
did the work.

The detector is scenario-agnostic — the runner matches whatever markers a scenario
declares — so these tests cover the matching itself plus the sprkl wiring.
"""
import json

from bench.cli import _find_contamination
from bench.core import registry
from bench.core.results import stop_code
from bench.core.scenario import Scenario

MARKERS = ("github.com/maso-soup",)


def test_no_markers_never_flags():
    """Scenarios with nothing published to guard (juice-shop, htb) opt out by
    leaving the tuple empty, and must never pay a false positive."""
    calls = [{"tool": "WebFetch", "args": {"url": "https://github.com/maso-soup/sprkl"}}]
    assert _find_contamination(calls, ()) is None


def test_clean_run_is_not_flagged():
    calls = [
        {"tool": "Bash", "args": {"command": "curl -s http://localhost:8080/products"}},
        {"tool": "WebSearch", "args": {"query": "flask session cookie forgery"}},
        {"tool": "WebFetch", "args": {"url": "https://owasp.org/Top10/"}},
    ]
    assert _find_contamination(calls, MARKERS) is None


def test_flags_a_webfetch_of_the_repo():
    calls = [
        {"tool": "Bash", "args": {"command": "curl http://localhost:8080/"}},
        {"tool": "WebFetch",
         "args": {"url": "https://github.com/maso-soup/sprkl/blob/main/findings.yaml"}},
    ]
    hit = _find_contamination(calls, MARKERS)
    assert hit == {"marker": "github.com/maso-soup", "tool": "WebFetch", "call_index": 1}


def test_flags_a_clone_hidden_in_a_bash_command():
    """The reference can arrive as a shell command rather than a fetch tool."""
    calls = [{"tool": "Bash",
              "args": {"command": "git clone https://github.com/maso-soup/sprkl /tmp/s"}}]
    assert _find_contamination(calls, MARKERS)["tool"] == "Bash"


def test_flags_a_result_echoing_the_repo():
    """A search result can carry the URL back even when the query looked innocent."""
    calls = [{"tool": "WebSearch", "args": {"query": "SPRKL sparkling water storefront"},
              "result": {"urls": ["https://github.com/maso-soup/sprkl"]}}]
    assert _find_contamination(calls, MARKERS)["tool"] == "WebSearch"


def test_match_is_case_insensitive():
    calls = [{"tool": "WebFetch", "args": {"url": "HTTPS://GitHub.com/Maso-Soup/sprkl"}}]
    assert _find_contamination(calls, MARKERS) is not None


def test_reports_the_first_hit():
    calls = [
        {"tool": "Bash", "args": {"command": "curl http://localhost:8080/"}},
        {"tool": "WebFetch", "args": {"url": "https://github.com/maso-soup/sprkl"}},
        {"tool": "WebFetch", "args": {"url": "https://github.com/maso-soup/pt-bench"}},
    ]
    assert _find_contamination(calls, MARKERS)["call_index"] == 1


def test_survives_unserializable_args():
    """Tool calls are read from JSONL, but the detector must not crash the whole
    run on an odd value — it is a guard, not a critical path."""
    calls = [{"tool": "Weird", "args": {"blob": {1, 2, 3}}},
             {"tool": "WebFetch", "args": {"url": "https://github.com/maso-soup/sprkl"}}]
    assert _find_contamination(calls, MARKERS)["call_index"] == 1


# -- wiring ------------------------------------------------------------------

def test_sprkl_declares_the_marker():
    assert registry.get_scenario("sprkl").contamination_markers == MARKERS


def test_other_scenarios_declare_none():
    """Juice Shop's solutions are in the training data, not fetched at runtime, and
    HTB has no first-party answer key at all — neither has anything to guard."""
    assert registry.get_scenario("juice-shop").contamination_markers == ()
    assert registry.get_scenario("htb", {"target": "10.0.0.1"}).contamination_markers == ()


def test_contract_default_is_empty():
    assert Scenario.contamination_markers == ()


def test_stop_code_round_trips():
    """The runner writes the code into the row; the grader and dashboard read it
    back through stop_code()."""
    row = {"status": "completed",
           "stop_reason": {"code": "contamination", "detail": "tool call #3"}}
    assert stop_code(row) == "contamination"


def test_dashboard_labels_the_code():
    """A code with no STOP_META entry renders as a bare slug in the UI."""
    from bench.core import resources
    html = (resources.dashboard_static() / "index.html").read_text()
    assert "contamination:" in html


# -- integration: the real _drive loop ---------------------------------------

class _FakeScenario:
    contamination_markers = MARKERS

    def __init__(self, solved=()):
        self._solved = set(solved)

    def oracle(self, handle):
        from bench.core.scenario import OracleResult
        return OracleResult(solved=set(self._solved))


def _drive_with(monkeypatch, tmp_path, calls_per_iter, mode, status=None):
    """Run cli._drive with a stubbed adapter that emits the given tool calls."""
    from bench import cli
    from bench.core.scenario import Budget, ItemSpec, TargetHandle

    seen = {"iters": 0}

    def fake_run_adapter(**kwargs):
        seen["iters"] += 1
        return status or {"status": "completed", "reason": "done"}

    def fake_read_artifacts(workdir):
        i = min(seen["iters"] - 1, len(calls_per_iter) - 1)
        return {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0}, calls_per_iter[i]

    monkeypatch.setattr(cli.adapter_mod, "build_run_spec",
                        lambda **kw: {"target_url": "http://t"})
    monkeypatch.setattr(cli.adapter_mod, "run_adapter", fake_run_adapter)
    monkeypatch.setattr(cli.adapter_mod, "read_artifacts", fake_read_artifacts)

    handle = TargetHandle(target_url="http://t", scope="s")
    # Two unsolved items, so a max-coverage loop keeps going: with an empty answer
    # key the "solved everything" branch fires on iteration 0 and nothing else runs.
    gt = [ItemSpec(key="a", category="c"), ItemSpec(key="b", category="c")]
    return cli._drive(arm={"model": None, "adapter_config": {}},
                      scenario=_FakeScenario(), handle=handle, gt=gt,
                      cmd=["true"], budget=Budget(), workdir=tmp_path, mode=mode)


CLEAN = [{"tool": "Bash", "args": {"command": "curl http://localhost:8080/"}}]
DIRTY = [{"tool": "WebFetch", "args": {"url": "https://github.com/maso-soup/sprkl"}}]


def test_drive_ends_autonomous_run_on_contamination(monkeypatch, tmp_path):
    _status, _dirs, stop = _drive_with(monkeypatch, tmp_path, [DIRTY], "autonomous")
    assert stop["code"] == "contamination"
    assert "WebFetch" in stop["detail"] and "iteration 0" in stop["detail"]


def test_drive_leaves_a_clean_autonomous_run_alone(monkeypatch, tmp_path):
    _status, _dirs, stop = _drive_with(monkeypatch, tmp_path, [CLEAN], "autonomous")
    assert stop["code"] == "agent_completed"


def test_contamination_is_terminal_in_max_coverage(monkeypatch, tmp_path):
    """Unlike a safety refusal, contamination cannot be recovered from by
    continuing: once the agent has the solutions every later iteration is tainted.
    The clean first iteration must not be allowed to continue past the dirty one."""
    _status, iter_dirs, stop = _drive_with(
        monkeypatch, tmp_path, [CLEAN, DIRTY, CLEAN], "max-coverage")
    assert stop["code"] == "contamination"
    assert "iteration 1" in stop["detail"]
    assert len(iter_dirs) == 2, "must not run a third iteration after detection"


def test_contamination_beats_a_refusal(monkeypatch, tmp_path):
    """A refused iteration normally continues in max-coverage. If that same
    iteration also fetched the answer key, the contamination stop wins."""
    _status, _dirs, stop = _drive_with(
        monkeypatch, tmp_path, [DIRTY], "max-coverage",
        status={"status": "refused", "reason": "safety"})
    assert stop["code"] == "contamination"
