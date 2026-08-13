"""Core contracts shared by every scenario and the runner.

Nothing in this module knows about any specific agent, harness, or target.
A scenario provisions a target, exposes an answer key (`ground_truth`) and a
live oracle (`oracle`) for what has actually been solved, and tears down.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class ItemSpec:
    """One gradable unit of the answer key (a planted vuln / CTF challenge)."""
    key: str                 # stable identifier, unique within a scenario
    category: str            # e.g. "Injection", "XSS", "Broken Access Control"
    difficulty: int = 1      # 1..6 for Juice Shop; higher = harder
    name: str = ""
    weight: float | None = None  # defaults to difficulty when None

    def effective_weight(self) -> float:
        return float(self.difficulty if self.weight is None else self.weight)


@dataclass
class OracleResult:
    """Ground-truth solved state, read live from the target — never from the
    agent's self-report."""
    solved: set[str] = field(default_factory=set)   # keys of solved items
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TargetHandle:
    """What the runner hands to an adapter, plus internal bookkeeping the
    scenario needs to run its oracle and teardown."""
    target_url: str
    scope: str
    meta: dict[str, Any] = field(default_factory=dict)  # scenario-private

    def public(self) -> dict[str, Any]:
        """The subset an agent is allowed to see (goes into the run spec)."""
        return {"target_url": self.target_url, "scope": self.scope}


@dataclass
class Budget:
    tool_calls: int | None = None
    usd: float | None = None
    wall_time_s: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Budget":
        d = d or {}
        return cls(
            tool_calls=d.get("tool_calls"),
            usd=d.get("usd"),
            wall_time_s=d.get("wall_time_s"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Scenario(abc.ABC):
    """Every environment (Juice Shop, an AD range, a cloud account) implements
    this and nothing more. The runner only ever touches this interface."""

    id: str = "scenario"
    category: str = "generic"

    # Opt in when the oracle is cheap and safe to poll repeatedly during a run;
    # the runner then reports solves live (see bench/core/progress.py).
    supports_live_progress: bool = False

    @abc.abstractmethod
    def provision(self) -> TargetHandle:
        """Bring the target to a known-good state and return a handle."""

    @abc.abstractmethod
    def ground_truth(self, handle: TargetHandle) -> list[ItemSpec]:
        """The answer key. May be derived from the live target at provision
        time so it can never drift from what is actually running."""

    @abc.abstractmethod
    def oracle(self, handle: TargetHandle) -> OracleResult:
        """Query the target for what has actually been solved right now."""

    @abc.abstractmethod
    def teardown(self, handle: TargetHandle) -> None:
        """Destroy the target so no state leaks into the next run."""
