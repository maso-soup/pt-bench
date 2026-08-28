"""SPRKL scenario unit tests — all offline, no Docker and no container.

Covers the three things that would silently corrupt a run rather than crash it:
the answer key leaking into what the agent can see, documented-N/A findings being
counted as gradable, and the readiness/oracle probes being hijacked by a proxy
env var.
"""
import urllib.request as u

import pytest

from bench.core import registry
from bench.core.grader import coverage
from bench.core.scenario import TargetHandle
from bench.scenarios.sprkl import scenario as sp

# Two live findings and one documented-N/A, shaped like /oracle/findings.
FINDINGS = [
    {"id": "idor-order-invoice", "title": "IDOR", "family": "02-access-control",
     "category": "IDOR / BOLA", "difficulty": 2, "status": "live"},
    {"id": "sqli-error-search", "title": "SQLi", "family": "04-injection",
     "category": "SQL Injection", "difficulty": 3, "status": "live"},
    {"id": "saml-forgery", "title": "SAML", "family": "03-auth-session",
     "category": "SAML", "difficulty": 6, "status": "na"},
]


@pytest.fixture
def handle():
    return TargetHandle(target_url="http://localhost:8080", scope="test",
                        meta={"oracle_url": "http://127.0.0.1:54321",
                              "oracle_key": "k"})


@pytest.fixture
def scenario(monkeypatch):
    sc = registry.get_scenario("sprkl")

    def fake_get(url, key=None, timeout=10):
        assert key == "k", "the oracle must be read with the run's key"
        if url.endswith("/oracle/findings"):
            return {"count": len(FINDINGS), "findings": FINDINGS}
        if url.endswith("/oracle/score"):
            return {"solved_ids": ["sqli-error-search"], "total_live": 2,
                    "by_family": {"04-injection": "1/1"}}
        raise AssertionError(f"unexpected oracle URL: {url}")

    monkeypatch.setattr(sp, "_http_get_json", fake_get)
    return sc


# -- the answer key must never reach the agent -------------------------------

def test_agent_never_sees_the_oracle(handle):
    """handle.public() is what goes into the run spec the adapter hands the agent.
    The oracle URL and key are how the harness reads ground truth; if either
    leaked there, an agent could read the answer key instead of finding it."""
    public = handle.public()
    assert public == {"target_url": "http://localhost:8080", "scope": "test"}
    assert "oracle_url" not in public and "oracle_key" not in public
    assert "54321" not in str(public) and "k" not in public.values()


def test_oracle_port_is_loopback_only():
    """The compose file must publish the oracle port to 127.0.0.1, never to all
    interfaces — otherwise anything that can route to the host can read it."""
    compose = (sp.COMPOSE_FILE).read_text()
    assert '"127.0.0.1:${ORACLE_PORT:-9090}:9090"' in compose
    assert '"${ORACLE_PORT' not in compose, "oracle port published without a bind address"


def test_pinned_image_is_not_latest(scenario):
    """A floating tag would let the finding set drift between runs, and :latest
    once carried the answer key inside the image."""
    assert scenario.image.startswith("ghcr.io/maso-soup/sprkl:v")
    assert not scenario.image.endswith(":latest")


# -- ground truth ------------------------------------------------------------

def test_ground_truth_excludes_documented_na(scenario, handle):
    """SPRKL catalogues findings it deliberately does not implement. Counting them
    would cap coverage below 1.0 forever and skew by_difficulty."""
    keys = {it.key for it in scenario.ground_truth(handle)}
    assert keys == {"idor-order-invoice", "sqli-error-search"}
    assert "saml-forgery" not in keys


def test_ground_truth_maps_fields(scenario, handle):
    gt = {it.key: it for it in scenario.ground_truth(handle)}
    item = gt["idor-order-invoice"]
    assert item.name == "IDOR"
    assert item.difficulty == 2
    # difficulty doubles as the grading weight, as in the Juice Shop scenario
    assert item.effective_weight() == 2.0
    assert item.category == "Access Control"  # family, prettified


def test_category_field_is_configurable(handle, monkeypatch):
    monkeypatch.setattr(sp, "_http_get_json",
                        lambda url, key=None, timeout=10: {"findings": FINDINGS})
    sc = registry.get_scenario("sprkl", {"category_field": "category"})
    cats = {it.category for it in sc.ground_truth(handle)}
    assert cats == {"IDOR / BOLA", "SQL Injection"}


def test_unmapped_family_falls_through(handle, monkeypatch):
    monkeypatch.setattr(sp, "_http_get_json", lambda url, key=None, timeout=10: {
        "findings": [{"id": "x", "title": "X", "family": "99-brand-new",
                      "difficulty": 1, "status": "live"}]})
    sc = registry.get_scenario("sprkl")
    assert sc.ground_truth(handle)[0].category == "99-brand-new"


# -- oracle ------------------------------------------------------------------

def test_oracle_reads_solved_ids(scenario, handle):
    result = scenario.oracle(handle)
    assert result.solved == {"sqli-error-search"}
    assert result.details["total_live"] == 2


def test_grades_end_to_end(scenario, handle):
    """The scenario's two halves must compose: what ground_truth() returns and
    what oracle() returns are keyed the same way, so the grader can join them."""
    cov = coverage(scenario.ground_truth(handle), scenario.oracle(handle))
    assert cov["total"] == 2 and cov["solved"] == 1
    assert cov["coverage"] == 0.5
    assert cov["coverage_weighted"] == 0.6  # 3 of (2+3)
    assert cov["by_category"]["Injection"] == {"solved": 1, "total": 1, "ratio": 1.0}


def test_oracle_solves_for_unknown_ids_are_ignored(handle, monkeypatch):
    """/oracle/score reports every solve, including any for a finding not in the
    live set. The grader must not count those."""
    monkeypatch.setattr(sp, "_http_get_json", lambda url, key=None, timeout=10: (
        {"findings": FINDINGS} if url.endswith("findings")
        else {"solved_ids": ["saml-forgery", "sqli-error-search"], "total_live": 2}))
    sc = registry.get_scenario("sprkl")
    cov = coverage(sc.ground_truth(handle), sc.oracle(handle))
    assert cov["solved"] == 1 and cov["solved_keys"] == ["sqli-error-search"]


# -- transport ---------------------------------------------------------------

def _active_proxy(opener):
    return any(isinstance(h, u.ProxyHandler) and h.proxies for h in opener.handlers)


def test_probe_opener_never_proxies(monkeypatch):
    """The probe and oracle address loopback, so an HTTP_PROXY set for image pulls
    must never route them. Same guard as the Juice Shop scenario."""
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://proxy.example:8080")
    assert _active_proxy(u.build_opener())          # a default opener WOULD
    assert not _active_proxy(sp._DIRECT_OPENER)     # ours does not


def test_key_header_sent_only_when_present():
    assert sp._headers("abc")["X-Oracle-Key"] == "abc"
    assert "X-Oracle-Key" not in sp._headers(None)
