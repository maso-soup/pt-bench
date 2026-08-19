"""Locate packaged resources and user config/data — so pt-bench works whether it
runs from a checkout (editable install) or from site-packages (real install).

Everything the runtime needs (adapters, scenario data, default arms, the dashboard
static files, the baseline template) ships inside the `bench` package and is found
relative to it via importlib.resources. User-editable things (custom arms, results,
the writable flat-prompt baseline) live in XDG config/data dirs, mirroring
`resolve_results_dir` in results.py.

Note: assumes a normally-installed (unzipped) package, so resource paths are real
filesystem paths — required for exec'ing adapter scripts and running Docker from
the scenario's compose file. Zip-safe installs are not supported.
"""
from __future__ import annotations

import importlib.resources as _ir
import os
import shutil
from pathlib import Path


def package_root() -> Path:
    """Filesystem path to the installed `bench` package."""
    return Path(str(_ir.files("bench")))


def adapters_dir() -> Path:
    return package_root() / "adapters"


def dashboard_static() -> Path:
    return package_root() / "dashboard" / "static"


def default_arms_dir() -> Path:
    return package_root() / "arms"


def _xdg(env: str, *default_sub: str) -> Path:
    base = os.environ.get(env) or os.path.join(os.path.expanduser("~"), *default_sub)
    return Path(base) / "pt-bench"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local", "share")


def arms_search_dirs(cli_arg: str | None = None) -> list[Path]:
    """Where to look for an arm, first match wins: explicit dir > $PTBENCH_ARMS_DIR
    > user config (~/.config/pt-bench/arms) > shipped defaults in the package."""
    dirs: list[Path] = []
    if cli_arg:
        dirs.append(Path(os.path.expanduser(cli_arg)))
    if os.environ.get("PTBENCH_ARMS_DIR"):
        dirs.append(Path(os.path.expanduser(os.environ["PTBENCH_ARMS_DIR"])))
    dirs.append(config_dir() / "arms")
    dirs.append(default_arms_dir())
    return dirs


def find_arm(name: str, cli_arg: str | None = None) -> Path | None:
    for d in arms_search_dirs(cli_arg):
        p = d / f"{name}.yaml"
        if p.exists():
            return p
    return None


def baseline_dir(name: str) -> Path:
    """A WRITABLE per-user baseline dir the agent can use as its cwd (Claude Code
    writes there), materialized from the packaged template on first use. The
    package copy is read-only in a real install, so it can't be the cwd directly."""
    dest = data_dir() / "baselines" / name
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
        tmpl = package_root() / "baselines" / name
        if tmpl.is_dir():
            for f in tmpl.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
    return dest
