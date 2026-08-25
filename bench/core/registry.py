"""Scenario registry. A scenario registers itself by id; the runner resolves
scenarios by id and never imports a concrete scenario module directly."""
from __future__ import annotations

import importlib
from typing import Callable

from .scenario import Scenario

_FACTORIES: dict[str, Callable[[dict], Scenario]] = {}

# id -> module path providing `register()`. New scenarios add one line here.
_KNOWN_MODULES = {
    "juice-shop": "bench.scenarios.juice_shop.scenario",
    "htb": "bench.scenarios.htb.scenario",
}


def register(scenario_id: str, factory: Callable[[dict], Scenario]) -> None:
    _FACTORIES[scenario_id] = factory


def get_scenario(scenario_id: str, config: dict | None = None) -> Scenario:
    if scenario_id not in _FACTORIES:
        module = _KNOWN_MODULES.get(scenario_id)
        if not module:
            raise KeyError(f"unknown scenario id: {scenario_id!r} "
                           f"(known: {sorted(_KNOWN_MODULES)})")
        importlib.import_module(module)  # triggers self-registration
    return _FACTORIES[scenario_id](config or {})
