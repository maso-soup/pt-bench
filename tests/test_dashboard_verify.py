"""Dashboard manual-verification endpoint: a self-reported run can be marked (and
unmarked) human-verified, persisted to its manifest, without pretending to be
target-verified."""
import json
from pathlib import Path

import pytest

from bench.dashboard import app as dash


@pytest.fixture
def client(tmp_path, monkeypatch):
    run = tmp_path / "arm" / "htb" / "20260101-000000"
    (run / "r0").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        "arm": "arm", "scenario": "htb", "verified": False,
        "scenario_config": {"machine_name": "Box", "difficulty": 3}}))
    (run / "summary.json").write_text(json.dumps({"n_runs": 1}))
    monkeypatch.setattr(dash, "RESULTS_DIR", tmp_path)
    return dash.app.test_client(), run, "arm/htb/20260101-000000"


def test_mark_and_unmark_verified(client):
    c, run, rid = client
    assert dash_runs(c)[0]["manual_verified"] is False

    r = c.post(f"/api/runs/{rid}/verify", json={"verified": True})
    assert r.get_json()["manual_verified"] is True
    m = json.loads((run / "manifest.json").read_text())
    assert m["manual_verification"]["verified"] is True and m["manual_verification"]["at"]
    assert m["verified"] is False           # source flag untouched — provenance stays honest
    assert dash_runs(c)[0]["manual_verified"] is True

    c.post(f"/api/runs/{rid}/verify", json={"verified": False})
    assert "manual_verification" not in json.loads((run / "manifest.json").read_text())
    assert dash_runs(c)[0]["manual_verified"] is False


def test_verify_rejects_path_traversal(client):
    c, _, _ = client
    assert c.post("/api/runs/..%2f..%2fetc/verify", json={"verified": True}).status_code in (403, 404)


def dash_runs(c):
    return c.get("/api/runs").get_json()
