# pt-bench

A modular benchmark for **agentic penetration-testing agents**. It measures how
well an autonomous security agent does against a deliberately-vulnerable target,
and lets you compare **different agent designs** (hold the model fixed) or
**different models** (hold the design fixed).

v1 ships one scenario — **OWASP Juice Shop in CTF mode** — and grades three
dimensions: **coverage**, **exploitation** (both from the target's own solved
state), and **efficiency** (tool calls, tokens, cost, time). Safety/scope and
report-quality scoring are deliberately out of scope for v1.

The design is modular so more environments (AD ranges, cloud accounts, forensics
images, …) drop in later without touching the runner.

## Two boundaries that keep it modular

```
            ┌─────────────┐   run_spec.json    ┌───────────────┐
   scenario │   RUNNER    │ ─────────────────▶ │    ADAPTER    │ ─▶ agent-under-test
 (target) ◀─┤ (agnostic)  │ ◀───────────────── │ (per agent)   │
   oracle   └─────────────┘   artifacts (files) └───────────────┘
```

- **Scenario boundary** (`bench/core/scenario.py`): every target implements
  `provision / ground_truth / oracle / teardown`. The runner knows nothing else.
  Coverage is read from the target's oracle — *target-side truth*.
- **Adapter boundary** (`adapters/PROTOCOL.md`): every agent is wrapped by an
  adapter that takes a `run_spec.json` and writes `status.json`, `usage.json`,
  and `tool_calls.jsonl`. Efficiency is read from those — *agent-side cost*.

Neither boundary references a specific agent or target, so adding a scenario or
an agent never changes core code.

## Layout

```
bench/core/            scenario contract, grader, results, adapter invocation
bench/scenarios/       one dir per environment (juice_shop/ ships in v1)
adapters/              PROTOCOL.md, JSON schemas, one dir per agent wrapper
  example-python/      minimal no-op reference adapter (validates the harness)
  claude-code/         drives any Claude Code repo (e.g. pt-agent) headless
arms/                  (adapter x model) configs — what you actually run
results/               one result.json per (arm x scenario x repetition)
run.py                 execute one cell for N repetitions
```

## Quick start

```bash
cd ~/pt-bench
python3 -m pip install -e .        # installs PyYAML

# 1) Validate the whole harness with the free no-op adapter:
python3 run.py --arm example__noop --scenario juice-shop --repeats 1

# 2) Benchmark pt-agent (needs the claude CLI; see adapters/claude-code/README.md
#    and enable skip_permissions for unattended runs):
python3 run.py --arm pt-agent__opus-4.8 --scenario juice-shop --repeats 5
```

Each run provisions a fresh Juice Shop container, drives the agent, reads the
solved-challenge state, grades, and tears the container down. Results land under
`results/<arm>/<scenario>/`.

## Adding things

- **A new agent** → new dir under `adapters/` satisfying `PROTOCOL.md`, plus an
  arm file. (For any Claude Code repo, reuse the `claude-code` adapter and just
  point `extra.repo` at it.)
- **A new target** → new dir under `bench/scenarios/` implementing `Scenario`,
  and one line in `bench/core/registry.py`.

## v1 caveats

- Juice Shop is white-box and well-documented, so v1 measures agentic execution,
  not novel discovery. Add a less-documented scenario to separate the two.
- `tool_calls` / `usd` budgets are advisory in v1; only `wall_time_s` is enforced.
- Concurrent runs on one host need distinct `port`s (compose project names are
  already unique per run).
