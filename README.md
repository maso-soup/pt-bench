# PT Bench

A modular benchmark harness for **agentic penetration-testing agents**. It measures how
well an autonomous security agent does against a deliberately-vulnerable target,
and lets you compare **different agent designs** or
**different models**.

Currently supports 3 scenarios: 

1) **SPRKL**, a custom, scratch built web scenario with 95 unique vulnerabilites covering 
OWASP and CWE families for web and API vulnerabilities. (see [SPRKL scenario](#sprkl-scenario-target-verified-uncontaminated) below). 
2) **OWASP Juice Shop**, the well-known vulnerable web application by OWASP
3) **HTB**, runs an agent against a machine IP you supply (see [HTB scenario](#htb-scenario-black-box-self-reported) below). 

The design is modular so more environments can drop in later without touching the runner.

## Two boundaries that keep it modular

```
            ┌─────────────┐   run_spec.json    ┌───────────────┐
   scenario │   RUNNER    │ ─────────────────▶ │    ADAPTER    │ ─▶ agent-under-test
 (target) ◀─┤ (agnostic)  │ ◀───────────────── │ (per agent)   │
   oracle   └─────────────┘     artifacts      └───────────────┘
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
bench/core/            scenario contract, grader, results, resources, adapter invocation
bench/scenarios/       one dir per environment (juice_shop/, sprkl/, htb/)
bench/adapters/        PROTOCOL.md, JSON schemas, one dir per agent wrapper
  example-python/      minimal no-op reference adapter (validates the harness)
  claude-code/         drives any Claude Code repo (e.g. pt-agent) headless
bench/arms/            shipped default (adapter x model) configs; add your own in
                       ~/.config/pt-bench/arms (or --arms-dir / $PTBENCH_ARMS_DIR)
bench/dashboard/       optional local web UI (the pt-bench-dashboard command)
bench/cli.py           the runner; installs as the `pt-bench` console command
run.py                 back-compat shim for `python run.py ...`
# results are written outside the repo (XDG data dir) — see Results storage below
```

## Setup

```bash
# 1. System packages (one time)
sudo apt update && sudo apt install -y docker.io python3-venv python3-full
sudo systemctl enable --now docker

# 2. Install pt-bench inside a virtualenv
git clone https://github.com/maso-soup/pt-bench.git
cd pt-bench
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. Check it works
pt-bench --arm example__noop --scenario juice-shop --repeats 1
```

## Running benchmarks

```bash
# Agent needs the `claude` (or equivalent) CLI installed + logged in, and agent directory specified in the arm YAML

pt-bench --arm pt-agent__opus-4.8    --scenario sprkl
pt-bench --arm flat-prompt__opus-4.8 --scenario sprkl
```

**Portable `repo` paths.** An arm's `adapter_config.repo` may be absolute, relative
a materialized baseline dir (`baselines/<name>`, used by the flat-prompt control),
or use `~` and `${VAR:-default}` expansion (e.g. `${PT_AGENT_REPO:-~/pt-agent}`).
The runner resolves it at load time, so the shipped arms work unchanged on any host.

Each run provisions a fresh container, drives the agent, reads the
solved-challenge state, grades, and tears the container down, except for HTB. 

**Results storage.** Results are kept **outside the repo** so they survive
re-clones: `--results-dir` > `$PTBENCH_RESULTS_DIR` > the XDG default
`~/.local/share/pt-bench/results`. Each cell writes a batch dir
`<results>/<arm>/<scenario>/<stamp>/` containing `manifest.json` (provenance:
mode, model, budget, and the git SHA of pt-bench and the agent repo),
`summary.json` (the aggregate), and one `r<i>/` per repetition with its
`result.json`, `progress.jsonl`, and per-iteration `iter-NN/` artifacts.

**Live progress.** During a run the harness polls the scenario's oracle and prints
each challenge as it's solved, plus an in-place running counter — so you can watch
a long engagement instead of staring at `running adapter ...`. 

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
pt-bench --arm pt-agent__opus-4.8 --scenario sprkl --mode autonomous
pt-bench --arm pt-agent__opus-4.8 --scenario sprkl --mode max-coverage
```

## SPRKL scenario

The `sprkl` scenario runs an agent against [SPRKL](https://github.com/maso-soup/sprkl),
a homegrown vulnerable storefront with **95 live findings** across 11 families at
difficulty 1–6. It grades exactly like Juice Shop — first-party, server-side
solved-state, `verified: true` — but SPRKL is not a famous training app, so none
of its solutions exist in a model's training data. Juice Shop measures how well an
agent reproduces published walkthroughs; SPRKL measures whether it can find things
nobody has written up. 

```bash
pt-bench --arm pt-agent__opus-4.8 --scenario sprkl
```

As of **SPRKL v2.0.2** the
target ships as two images: the attackable storefront (`sprkl-app`) and a
`sprkl-scorer` that fronts it as an ingress proxy and owns the rules, catalog,
scoring key and solve store. The agent talks to the scorer's proxy (which forwards
to the app); the score API is read-only, `X-Oracle-Key`-gated, and published to
loopback on a random per-run port it is never told. A finding is recorded only when
a vulnerable code path fires and the scorer's rule agrees — there is nothing to
self-report into. The split is also the contamination guard the image used to need
overlays for: the attackable container carries no cheat sheet, no `findings.yaml`,
no rules and no key.

Three integrity checks run at provision time, each aborting rather than producing a
quietly meaningless result:

- the oracle's paths must not be reachable through the agent-facing URL,
- the oracle must reject an unkeyed read (expects 401),
- the **app image must not carry an answer key**. SPRKL has live path-traversal,
  file-inclusion and RCE findings, so a cheat sheet, `findings.yaml`, or the
  `scorer/` rules at `/app` would turn one solve into a walkthrough for the other
  94. Under the v2 split the app image carries none of these (they live only in the
  scorer, which the agent cannot reach); the check verifies that against the running
  app container.

**Contamination guard.** SPRKL's repo is public, and its `findings.yaml` +
`scorer/rules.py` carry every finding's location and detection logic — so an agent
with web access can look up the answers instead of finding them. After each iteration the runner scans the agent's
tool calls (name, arguments and results, so a `WebFetch` url, a `git clone` inside
a Bash command, and an echoed search result all count) for the scenario's declared
markers. A hit ends the run immediately with stop code **`contamination`**, in both
modes — unlike a safety refusal this cannot be recovered from by continuing, since
every later iteration is tainted too. Coverage is still graded and written; the
stop code is what marks the run unusable as a discovery measurement, and the
dashboard badges it. Scenarios declare their own markers via
`contamination_markers` on `Scenario` (empty for juice-shop and htb, which have
nothing fetchable to guard).

Knobs live in `bench/scenarios/sprkl/config.yaml` — `app_image` and `scorer_image`
(pin exact tags), `port`, `ready_timeout_s`, and `category_field`, which selects
whether the grader
groups by `family` (11 coarse buckets, the default) or `category` (~40
fine-grained). Override any of them per run with `--scenario-config KEY=VALUE`.

## HTB scenario (black-box, self-reported)

The `htb` scenario runs an agent against a boot2root machine **you** stand up and
manage yourself (a Hack The Box box, a local VM, wherever) — you pass the target
in by hand. It's a deliberately less-modular counterpart to Juice Shop: there is
**no first-party oracle**, so the harness cannot confirm a flag on its own.
Instead it **trusts the agent's self-report** — it grades the `user.txt` /
`root.txt` values the agent writes to a `flags.json` artifact. Such runs are
marked `verified: false` in the manifest and badged **self-reported** in the
dashboard, so they're never confused with Juice Shop's target-verified coverage
(you can manually confirm them later — see Dashboard below).

```bash
# start the machine + connect your VPN yourself first, then:
pt-bench --arm pt-agent__opus-4.8 --scenario htb \
  --target 10.10.11.42 --machine-name Blazorized --difficulty 4 --repeats 3
```

- `--target` (required) host/IP, `--machine-name` and `--difficulty` label the run
  and weight the two flags. Pass any other scenario knob with
  `--scenario-config KEY=VALUE` (e.g. `reachability_port=22` to fail fast if the
  box/VPN is down). These merge over the arm's own `scenario_config`.
- **Black-box by construction:** what the agent sees never reveals the platform,
  the machine name, or the difficulty — only the target host and a generic
  full-scope pentest brief. Those labels live only in the manifest, for grading
  and the dashboard.
- Use `--mode autonomous` (the default): the run ends when the agent stops itself
  or a budget cap trips. `--repeats N` re-runs the same machine. The usual
  time / token / USD budgets from the arm apply unchanged, and the dashboard shows
  total time / tokens / cost per run.

## Adding things

- **A new agent** → new dir under `bench/adapters/` satisfying `PROTOCOL.md`, plus
  an arm file (in `bench/arms/` or `~/.config/pt-bench/arms`). For any Claude Code
  repo, reuse the `claude-code` adapter and just point `extra.repo` at it.
- **A new target** → new dir under `bench/scenarios/` implementing `Scenario`,
  and one line in `bench/core/registry.py`.

## Dashboard

A local web UI to compare any two runs side by side (coverage, coverage-at-budget
curves, efficiency, by-category / by-difficulty, and git-SHA provenance). It ships
with the benchmark — no extra install — and reads the same results dir.

**It auto-starts with every run** (detached, on http://127.0.0.1:8008) and stays up
afterward, so results are always viewable.

You can also start it on its own, without running a benchmark:

```bash
pt-bench --dashboard
```

**Scenario menu.** When results span more than one scenario a segmented control
(All / Juice Shop / HTB) appears above the pickers and filters which runs you can
select, so you can flip between environments instead of scrolling one mixed list.

**Manual verification (HTB).** Self-reported runs carry a red **self-reported**
badge. Once you've submitted those flags to the platform and confirmed they're
legitimate, click **Mark verified** on the run's card — the badge turns green
(**verified (manual)**) and the confirmation (with a timestamp) is persisted to
the run's `manifest.json`. This is recorded separately from the scenario's own
`verified` flag, so a manual confirmation never masquerades as automated
target-side truth; click **Unverify** to undo it.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

This is tooling for **authorized** security testing only. Run it against targets
you own or have explicit written permission to test; the license disclaims all
warranty and liability.
