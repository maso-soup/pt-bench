"""Budget enforcement: wall time, tokens, and USD must all stop a run when hit.

The token/USD accounting lives in the claude-code adapter (the only component
that sees streamed usage), which is executed by path, so we import it via its
file path rather than as a package module.
"""
import importlib.util
from pathlib import Path

import pytest

from bench.core.scenario import Budget
from bench.core import adapter as adapter_mod

ROOT = Path(__file__).resolve().parent.parent
CC = ROOT / "bench" / "adapters" / "claude-code" / "adapter.py"


def _load_cc():
    spec = importlib.util.spec_from_file_location("cc_adapter", CC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load_cc()


# ---- Budget plumbing ----

def test_budget_roundtrip_includes_tokens():
    b = Budget.from_dict({"usd": 15, "tokens": 1000, "wall_time_s": 60})
    assert b.tokens == 1000
    d = b.to_dict()
    assert d["tokens"] == 1000 and d["usd"] == 15 and d["wall_time_s"] == 60
    assert "tool_calls" not in d  # removed as a budget dimension


def test_budget_tokens_defaults_none():
    assert Budget.from_dict({}).tokens is None
    assert Budget().tokens is None


# ---- Token budget ----

def _assistant(inp=0, out=0, cw=0, cr=0):
    return {"type": "assistant", "message": {"usage": {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}}


def test_tokens_breach_accumulates_across_turns():
    t = cc.BudgetTracker({"tokens": 1000}, "claude-opus-4-8")
    t.ingest(_assistant(inp=300, out=100))   # 400
    assert t.breach(0) is None
    t.ingest(_assistant(inp=400, out=200))   # 1000 total, not yet > 1000
    assert t.breach(0) is None
    t.ingest(_assistant(inp=1, out=0))       # 1001 > 1000
    hit = t.breach(0)
    assert hit and hit[0] == "tokens"


def test_tokens_include_cache_input():
    t = cc.BudgetTracker({"tokens": 100}, "claude-opus-4-8")
    t.ingest(_assistant(inp=10, out=10, cw=50, cr=40))  # 110 input-side+out > 100
    hit = t.breach(0)
    assert hit and hit[0] == "tokens"


# ---- USD budget: derived mid-run, then authoritative ----

def test_usd_breach_derived_from_tokens():
    # opus-4-8: $5/Mtok in, $25/Mtok out. 1M in + 1M out = $30 > $10.
    t = cc.BudgetTracker({"usd": 10}, "claude-opus-4-8")
    t.ingest(_assistant(inp=1_000_000, out=1_000_000))
    hit = t.breach(0)
    assert hit and hit[0] == "usd"
    assert t.cost == pytest.approx(30.0, rel=1e-6)


def test_usd_prefers_reported_cost():
    t = cc.BudgetTracker({"usd": 10}, "claude-opus-4-8")
    t.ingest(_assistant(inp=100, out=100))  # derived ~ negligible
    t.ingest({"type": "result", "total_cost_usd": 12.5, "usage": {}})
    assert t.cost == 12.5
    hit = t.breach(0)
    assert hit and hit[0] == "usd"


def test_unpriceable_model_cannot_derive_usd():
    t = cc.BudgetTracker({"usd": 10}, "some-unknown-model")
    assert t.priceable is False
    t.ingest(_assistant(inp=10_000_000, out=10_000_000))
    assert t.cost is None            # no price table -> no mid-run estimate
    assert t.breach(0) is None       # USD can't preempt; wall/tokens still could
    # but the authoritative terminal cost still enforces post-hoc
    t.ingest({"type": "result", "total_cost_usd": 99.0})
    hit = t.breach(0)
    assert hit and hit[0] == "usd"


def test_price_override_via_config():
    t = cc.BudgetTracker({"usd": 1}, "some-unknown-model",
                         prices={"input_per_mtok": 1000, "output_per_mtok": 1000})
    assert t.priceable is True
    t.ingest(_assistant(inp=2000, out=0))  # 2000/1e6 * 1000 = $2 > $1
    hit = t.breach(0)
    assert hit and hit[0] == "usd"


# ---- Wall budget + precedence ----

def test_wall_breach():
    t = cc.BudgetTracker({"wall_time_s": 60}, "claude-opus-4-8")
    assert t.breach(59) is None
    hit = t.breach(61)
    assert hit and hit[0] == "wall_time_s"


def test_no_budget_never_breaches():
    t = cc.BudgetTracker({}, "claude-opus-4-8")
    t.ingest(_assistant(inp=10_000_000, out=10_000_000))
    assert t.breach(10_000) is None


def test_wall_checked_before_tokens():
    t = cc.BudgetTracker({"wall_time_s": 60, "tokens": 100}, "claude-opus-4-8")
    t.ingest(_assistant(inp=1000, out=0))
    hit = t.breach(120)
    assert hit and hit[0] == "wall_time_s"   # wall reported first


# ---- usage_dict reflects accumulated totals ----

def test_usage_dict_records_accumulated():
    t = cc.BudgetTracker({}, "claude-opus-4-8")
    t.ingest(_assistant(inp=100, out=50))
    t.ingest(_assistant(inp=100, out=50))
    u = t.usage_dict(12.34)
    assert u["tokens_in"] == 200 and u["tokens_out"] == 100
    assert u["wall_time_s"] == 12.3


def test_usage_dict_null_when_no_usage_seen():
    t = cc.BudgetTracker({}, "claude-opus-4-8")
    u = t.usage_dict(1.0)
    assert u["tokens_in"] is None and u["tokens_out"] is None


def test_result_seeds_totals_only_when_no_assistant_usage():
    t = cc.BudgetTracker({}, "claude-opus-4-8")
    t.ingest({"type": "result", "usage": {"input_tokens": 500, "output_tokens": 200},
              "total_cost_usd": 1.0})
    assert t.tokens_in == 500 and t.tokens_out == 200

    t2 = cc.BudgetTracker({}, "claude-opus-4-8")
    t2.ingest(_assistant(inp=10, out=10))
    t2.ingest({"type": "result", "usage": {"input_tokens": 9999, "output_tokens": 9999},
               "total_cost_usd": 1.0})
    assert t2.tokens_in == 10 and t2.tokens_out == 10  # result.usage ignored


# ---- run_spec carries tokens through to the adapter ----

def test_run_spec_includes_tokens():
    from bench.core.scenario import TargetHandle
    h = TargetHandle(target_url="http://x", scope="s")
    spec = adapter_mod.build_run_spec(
        handle=h, model="claude-opus-4-8",
        budget=Budget(tokens=1234, usd=5, wall_time_s=60),
        workdir=Path("/tmp/x"), adapter_config={})
    assert spec["budget"]["tokens"] == 1234


# ---- Stop-reason classification & aggregation ----

from bench import cli
from bench.core import results as results_mod


def test_budget_code_maps_dimensions():
    assert cli._budget_code("wall_time_s exceeded (61s > 60s)") == "budget_wall"
    assert cli._budget_code("token budget exceeded (1200 > 1000)") == "budget_tokens"
    assert cli._budget_code("USD budget exceeded ($0.02 > $0.01)") == "budget_usd"
    assert cli._budget_code("something else") == "budget"


def test_classify_stop():
    assert cli._classify_stop({"status": "completed"})["code"] == "agent_completed"
    assert cli._classify_stop({"status": "error", "reason": "boom"})["code"] == "error"
    assert cli._classify_stop(
        {"status": "budget_exceeded", "reason": "token budget exceeded"})["code"] == "budget_tokens"


def test_stop_code_prefers_reason_then_status_fallback():
    assert results_mod.stop_code({"stop_reason": {"code": "plateau"}}) == "plateau"
    # older row with no stop_reason -> inferred from status
    assert results_mod.stop_code({"status": "completed"}) == "agent_completed"
    assert results_mod.stop_code({"status": "budget_exceeded"}) == "budget"
    assert results_mod.stop_code({"status": "error"}) == "error"


def _row(status, code=None):
    sr = {"code": code} if code else {}
    return results_mod.make_row(
        arm="a", scenario="s", repeat=0, model="m", status=status,
        coverage={"coverage": 0.5, "coverage_weighted": 0.5, "solved": 1, "total": 2},
        efficiency={"cost_usd": None, "tool_calls": 0}, derived={}, workdir="/x",
        curve={"at": {}}, stop_reason=sr)


def test_aggregate_counts_stop_reasons():
    rows = [_row("completed", "plateau"), _row("completed", "plateau"),
            _row("budget_exceeded", "budget_wall"), _row("error", "error")]
    agg = results_mod.aggregate(rows)
    assert agg["stop_reasons"] == {"plateau": 2, "budget_wall": 1, "error": 1}


def test_make_row_includes_stop_reason():
    r = _row("completed", "all_solved")
    assert r["stop_reason"] == {"code": "all_solved"}


# ---- Safety refusal detection ----

def test_refusal_detail_from_assistant_message():
    ev = {"type": "assistant", "message": {"stop_reason": "refusal",
          "stop_details": {"type": "refusal", "category": "cyber",
                           "explanation": "declined"}, "content": []}}
    r = cc._refusal_detail(ev)
    assert r == {"category": "cyber", "explanation": "declined"}


def test_refusal_detail_from_result_event():
    ev = {"type": "result", "stop_reason": "refusal",
          "stop_details": {"category": "cyber"}}
    assert cc._refusal_detail(ev)["category"] == "cyber"


def test_refusal_detail_tolerates_subtype_and_missing_details():
    assert cc._refusal_detail({"type": "result", "subtype": "refusal_stop"}) == {
        "category": None, "explanation": None}
    assert cc._refusal_detail({"type": "assistant",
                               "message": {"stop_reason": "end_turn"}}) is None
    assert cc._refusal_detail({"type": "assistant", "message": {"content": []}}) is None


def test_classify_stop_refused():
    d = cli._classify_stop({"status": "refused", "reason": "model refusal (category: cyber)"})
    assert d["code"] == "refused" and "cyber" in d["detail"]


def test_stop_code_refused_fallback():
    assert results_mod.stop_code({"status": "refused"}) == "refused"
    assert results_mod.stop_code({"stop_reason": {"code": "refused"}}) == "refused"


# ---- End-to-end: adapter promotes a streamed refusal to status "refused" ----

import json as _json
import os as _os
import subprocess as _sp
import sys as _sys


def test_adapter_records_refusal_end_to_end(tmp_path):
    """Drive the real adapter with a fake `claude` that emits a refusal turn and
    confirm status.json becomes 'refused' (not 'completed'), with the category,
    while tool calls and usage are still captured."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "claude").write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':200,"
        "'output_tokens':50},'content':[{'type':'tool_use','name':'Bash','input':{}}]}}),flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'stop_reason':'refusal',"
        "'stop_details':{'category':'cyber','explanation':'declined'},'content':[]}}),flush=True)\n"
        "print(json.dumps({'type':'result','total_cost_usd':0.01,'usage':{}}),flush=True)\n")
    (fakebin / "claude").chmod(0o755)

    repo = tmp_path / "repo"; repo.mkdir()
    wd = tmp_path / "wd"; wd.mkdir()
    (wd / "run_spec.json").write_text(_json.dumps({
        "target_url": "http://t", "scope": "s", "model": "claude-opus-4-8",
        "budget": {"wall_time_s": 600}, "workdir": str(wd), "extra": {"repo": str(repo)}}))

    env = dict(_os.environ, PATH=f"{fakebin}{_os.pathsep}{_os.environ['PATH']}")
    _sp.run([_sys.executable, str(CC), "--spec", str(wd / "run_spec.json")],
            check=True, env=env, timeout=60)

    status = _json.loads((wd / "status.json").read_text())
    assert status["status"] == "refused" and status["category"] == "cyber"
    assert (wd / "tool_calls.jsonl").read_text().strip()          # tools still recorded
    assert _json.loads((wd / "usage.json").read_text())["cost_usd"] == 0.01
