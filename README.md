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
run.py                 execute one cell for N repetitions
# results are written outside the repo (XDG data dir) — see Results storage below
```

## Quick start

Prerequisites: Docker running (for the target), and — only for the Claude Code
arms — the `claude` CLI installed and authenticated. Nothing else is host-specific;
arm `repo` paths are portable (see below), so a fresh clone needs no file edits.

```bash
git clone <url> pt-bench && cd pt-bench
python3 -m venv .venv && ./.venv/bin/pip install -e .   # installs PyYAML

# 1) Validate the whole harness with the free no-op adapter (no claude/Docker cost):
./.venv/bin/python run.py --arm example__noop --scenario juice-shop --repeats 1

# 2) Benchmark pt-agent. Point PT_AGENT_REPO at your pt-agent checkout, or drop it
#    at ~/pt-agent (the default). See adapters/claude-code/README.md.
export PT_AGENT_REPO=/path/to/pt-agent
./.venv/bin/python run.py --arm pt-agent__opus-4.8   --scenario juice-shop --repeats 5

# 3) Baseline control (same model, no scaffolding) for the A/B:
./.venv/bin/python run.py --arm flat-prompt__opus-4.8 --scenario juice-shop --repeats 5
```

**Portable `repo` paths.** An arm's `adapter_config.repo` may be absolute, relative
to the pt-bench root (e.g. `baselines/flat-prompt`), or use `~` and
`${VAR:-default}` expansion (e.g. `${PT_AGENT_REPO:-~/pt-agent}`). The runner
resolves it at load time, so the shipped arms work unchanged on any host.

Each run provisions a fresh Juice Shop container, drives the agent, reads the
solved-challenge state, grades, and tears the container down.

**Results storage.** Results are kept **outside the repo** so they survive
re-clones: `--results-dir` > `$PTBENCH_RESULTS_DIR` > the XDG default
`~/.local/share/pt-bench/results`. Each cell writes a batch dir
`<results>/<arm>/<scenario>/<stamp>/` containing `manifest.json` (provenance:
mode, model, budget, and the git SHA of pt-bench and the agent repo),
`summary.json` (the aggregate), and one `r<i>/` per repetition with its
`result.json`, `progress.jsonl`, and per-iteration `iter-NN/` artifacts.

**Live progress.** During a run the harness polls the scenario's oracle and prints
each challenge as it's solved, plus an in-place running counter — so you can watch
a long engagement instead of staring at `running adapter ...`. It's on by default
when stdout is a TTY; control it with `--progress / --no-progress` and
`--progress-interval <seconds>`. Every run also writes a solved-over-time curve to
`progress.jsonl` in its workdir. Only scenarios that set
`supports_live_progress = True` are polled.

**Modes (`--mode`).** Agents tend to wrap up an open-ended pentest and stop long
before they've exhausted a target, so a single number conflates *capability* with
*stopping disposition*. Run both modes and compare:

- `autonomous` (default) — one invocation; the agent stops when it decides. The
  honest real-world baseline.
- `max-coverage` — re-invoke the agent (state preserved, so it resumes) until it
  solves everything, **plateaus** (an iteration adds no new solves), errors, or the
  total `wall_time_s` budget runs out (safety cap: 10 iterations). The agent stays
  blind to the score; the harness reads the oracle to decide when to stop. Measures
  the capability *ceiling*.

The **gap** between the two is how much the agent leaves on the table by quitting
early. Each result row also carries `coverage_at_budget` (solved fraction reached
by 25/50/75/100% of the wall budget) and `first_solve_s`, so you can tell an agent
that plateaued from one still climbing when it stopped. Per-iteration artifacts
land in `workdir/iter-NN/`; efficiency (tokens/cost/tool-calls) is summed across
iterations.

```bash
# baseline vs ceiling for the same arm
./.venv/bin/python run.py --arm pt-agent__opus-4.8 --scenario juice-shop --mode autonomous   --repeats 5
./.venv/bin/python run.py --arm pt-agent__opus-4.8 --scenario juice-shop --mode max-coverage --repeats 5
```

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

## Dashboard

A local web UI to compare any two runs side by side (coverage, coverage-at-budget
curves, efficiency, by-category / by-difficulty, and git-SHA provenance). It ships
with the benchmark — no extra install — and reads the same results dir.

```bash
pt-bench-dashboard            # http://127.0.0.1:8008  (--results-dir / --port to override)
```

A `--repeats N` run appears as one selectable run (the mean of its N repetitions,
with a 95% CI and an `N/N runs` label); the picker shows `×N` so a repeats=1 run is
distinct from a repeats=5 one. See [dashboard/README.md](dashboard/README.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

This is tooling for **authorized** security testing only. Run it against targets
you own or have explicit written permission to test; the license disclaims all
warranty and liability.
