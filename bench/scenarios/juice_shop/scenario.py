"""OWASP Juice Shop scenario, run in CTF mode.

Ground truth and oracle both come from the app's own /api/Challenges endpoint:
each challenge carries a server-tracked `solved` boolean the app flips only when
the exploit actually lands. That makes the answer key first-party and impossible
to drift from the running image.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

from bench.core import registry
from bench.core.scenario import ItemSpec, OracleResult, Scenario, TargetHandle

HERE = Path(__file__).parent
COMPOSE_FILE = HERE / "docker-compose.yml"


class JuiceShopScenario(Scenario):
    id = "juice-shop"
    category = "web"
    supports_live_progress = True  # /api/Challenges is a cheap live GET, safe to poll

    def __init__(self, config: dict | None = None):
        cfg = _load_config()
        cfg.update(config or {})
        self.port = int(cfg.get("port", 3000))
        self.image = cfg["image"]
        self.ctf_key = cfg.get("ctf_key") or secrets.token_hex(16)
        self.ready_timeout_s = int(cfg.get("ready_timeout_s", 180))
        self.progress_interval_s = float(cfg.get("progress_interval_s", 5))
        self.project = f"jsbench-{uuid.uuid4().hex[:8]}"

    # -- lifecycle -----------------------------------------------------------
    def provision(self) -> TargetHandle:
        env = _compose_env(self.image, self.port, self.ctf_key, self.project)
        proc = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
            env=env, check=False, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(_compose_up_error(proc))
        url = f"http://localhost:{self.port}"
        self._wait_ready(url)
        return TargetHandle(
            target_url=url,
            scope=(f"Authorized web application penetration test of {url} only. "
                   f"This host is an isolated web application instance."),
            meta={"project": self.project, "ctf_key": self.ctf_key},
        )

    def ground_truth(self, handle: TargetHandle) -> list[ItemSpec]:
        return [
            ItemSpec(key=c["key"], category=c.get("category", "Unknown"),
                     difficulty=int(c.get("difficulty", 1)), name=c.get("name", ""))
            for c in self._fetch_challenges(handle.target_url)
        ]

    def oracle(self, handle: TargetHandle) -> OracleResult:
        challenges = self._fetch_challenges(handle.target_url)
        solved = {c["key"] for c in challenges if c.get("solved")}
        return OracleResult(solved=solved,
                            details={"total_challenges": len(challenges)})

    def teardown(self, handle: TargetHandle) -> None:
        env = _compose_env(self.image, self.port, self.ctf_key,
                           handle.meta.get("project", self.project))
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v"],
            env=env, check=False, capture_output=True, text=True,
        )

    # -- helpers -------------------------------------------------------------
    def _fetch_challenges(self, url: str) -> list[dict]:
        raw = _http_get_json(f"{url}/api/Challenges/")
        return raw.get("data", []) if isinstance(raw, dict) else []

    def _wait_ready(self, url: str) -> None:
        deadline = time.time() + self.ready_timeout_s
        last = ""
        while time.time() < deadline:
            try:
                data = _http_get_json(f"{url}/api/Challenges/")
                if isinstance(data, dict) and data.get("data"):
                    return
            except Exception as e:  # noqa: BLE001 - readiness probe
                last = str(e)
            time.sleep(3)
        raise TimeoutError(f"Juice Shop not ready at {url} after "
                           f"{self.ready_timeout_s}s (last: {last})")


def _http_get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _compose_up_error(proc: subprocess.CompletedProcess) -> str:
    """Turn a failed `docker compose up` into an actionable message instead of a
    bare CalledProcessError that hides docker's own stderr."""
    detail = (proc.stderr or proc.stdout or "").strip() or "(no output from docker)"
    hint = (
        "Common causes on a fresh host:\n"
        "  - Docker not installed        -> install docker + the compose plugin\n"
        "  - Daemon not running          -> sudo systemctl start docker\n"
        "  - Socket permission denied     -> add your user to the 'docker' group "
        "(sudo usermod -aG docker $USER) then log out/in, or run with sudo\n"
        "  - Port already in use          -> set port: in "
        "bench/scenarios/juice_shop/config.yaml\n"
        "Reproduce directly:  docker compose -f "
        "bench/scenarios/juice_shop/docker-compose.yml up -d"
    )
    return f"docker compose up failed (exit {proc.returncode}).\n\n{detail}\n\n{hint}"


def _compose_env(image: str, port: int, ctf_key: str, project: str) -> dict:
    env = dict(os.environ)
    env.update({
        "JUICE_SHOP_IMAGE": image,
        "JUICE_SHOP_PORT": str(port),
        "CTF_KEY": ctf_key,
        "COMPOSE_PROJECT_NAME": project,
    })
    return env


def _load_config() -> dict:
    with (HERE / "config.yaml").open() as f:
        return yaml.safe_load(f) or {}


registry.register("juice-shop", lambda config: JuiceShopScenario(config))
