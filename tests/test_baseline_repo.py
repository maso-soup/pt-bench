"""The flat-prompt baseline cwd is materialized to a writable per-user dir and
reset to its template each repetition — it must never be deleted out from under
the run (the "extra.repo is not a directory" failure)."""
from pathlib import Path

import yaml

from bench import cli
from bench.core import resources

ROOT = Path(__file__).resolve().parent.parent


def test_flat_prompt_does_not_reset_its_own_cwd():
    """Regression guard: the arm must not list its own baseline cwd in
    reset_paths (reset_agent_state would move it to trash before the run)."""
    arm = yaml.safe_load((ROOT / "bench/arms/flat-prompt__opus-4.8.yaml").read_text())
    resets = (arm.get("adapter_config") or {}).get("reset_paths") or []
    assert not any("baselines/flat-prompt" in p for p in resets), resets


def test_resolve_repo_materializes_existing_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    repo = cli.resolve_repo("baselines/flat-prompt")
    assert Path(repo).is_dir()                      # exists, ready to be the cwd
    assert Path(repo).parent == resources.baselines_root()


def test_is_baseline_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    base = resources.baselines_root() / "flat-prompt"
    assert cli._is_baseline_repo(str(base))
    assert not cli._is_baseline_repo("/home/kali/pt-agent")
    assert not cli._is_baseline_repo(None)


def test_baseline_dir_refresh_resets_to_template(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    d = resources.baseline_dir("flat-prompt")
    assert d.is_dir()
    junk = d / "agent-notes.txt"
    junk.write_text("a prior repetition's leftover")
    d2 = resources.baseline_dir("flat-prompt", refresh=True)
    assert d2 == d and d2.is_dir()
    assert not junk.exists()                        # prior-rep files wiped
    assert (d2 / "README.md").exists()              # template restored
