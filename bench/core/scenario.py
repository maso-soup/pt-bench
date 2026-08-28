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
    objective: str | None = None  # optional task text the adapter renders into
                                  # the agent's prompt; None => adapter default

    def public(self) -> dict[str, Any]:
        """The subset an agent is allowed to see (goes into the run spec)."""
        out = {"target_url": self.target_url, "scope": self.scope}
        if self.objective is not None:
            out["objective"] = self.objective
        return out


@dataclass
class Budget:
    usd: float | None = None
    tokens: int | None = None          # total tokens (input + output)
    wall_time_s: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Budget":
        d = d or {}
        return cls(
            usd=d.get("usd"),
            tokens=d.get("tokens"),
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

    # Substrings that must not appear in the agent's tool calls: a scenario whose
    # solutions are published somewhere declares where, and the runner ends the run
    # with stop code "contamination" if the agent fetches it. A run where the agent
    # read the answer key measures retrieval, not discovery, so it has to be
    # distinguishable from one where it did the work. Empty = nothing to guard.
    contamination_markers: tuple[str, ...] = ()

    # True when the oracle reads target-side truth (e.g. Juice Shop's server-
    # tracked solved-state). Set False for scenarios that TRUST the agent's own
    # self-report (e.g. the HTB scenario, which grades the flags the agent claims
    # to have found). The runner records this in the manifest so unverified
    # results are never mistaken for verified ones.
    verified: bool = True

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
