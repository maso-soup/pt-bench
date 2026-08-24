# claude-code adapter

Drives any Claude Code agent repo headless and reports tool calls + cost.

## What it does

Runs `claude -p "<task>" --output-format stream-json --verbose` with the working
directory set to `extra.repo`, so that repo's `CLAUDE.md` / `AGENTS.md` / skills
load exactly as in interactive use. It parses the stream-json events to produce
`tool_calls.jsonl` (one line per `tool_use`) and `usage.json` (tokens + cost from
the final `result` event).

## Requirements

- `claude` CLI on `PATH`, with an authenticated session.
- `extra.repo` — absolute path to the agent repo under test.

## Unattended runs: `skip_permissions`

By default Claude Code prompts before running tools, which stalls a headless run.
To let the agent use tools autonomously, add to the arm's `adapter_config`:

```yaml
adapter_config:
  repo: /path/to/agent
  skip_permissions: true
```

This passes `--dangerously-skip-permissions` to the CLI. Only enable it when the
agent is confined to an authorized, isolated target (as the benchmark scenarios
are) — it removes the human-in-the-loop guard, so never point such an arm at a
target you are not authorized to attack. It is intentionally left off in the
shipped arm files so you opt in deliberately.

## Run isolation: `reset_paths`

Agents persist state on disk to survive context compression — pt-agent writes
findings to `~/pt-agent-state/<target>/` and raw output to
`~/pt-agent-output/<target>/`. Across repetitions against the same target those
paths would collide, and a later run could read the earlier run's state and skip
ahead — contaminating the "each run starts fresh" assumption the benchmark
depends on.

Isolation is handled by the **runner**, not by redirecting `$HOME` here. A
`$HOME` sandbox only catches shell-expanded (`~`) writes; the model resolves `~`
itself for absolute paths it passes to the Write/Edit tools, so some state
escapes to the real home regardless. Instead, each arm declares the dirs to
clear before every run:

```yaml
adapter_config:
  repo: /path/to/agent
  reset_paths:
    - ~/pt-agent-state
    - ~/pt-agent-output
    - ~/.claude/projects    # Claude Code's per-project memory (see below)
```

Each entry is a directory (or file) cleared before every run; `~` expands
against the real home. To stay reversible, matching paths are **moved to a
timestamped trash dir** (`~/.pt-bench-trash/<stamp>/`) rather than hard-deleted;
prune it yourself when you're sure. Omit `reset_paths` (or leave it empty) to
disable.

**Claude Code memory.** Beyond the agent's own state dirs, Claude Code persists
per-project memory under `~/.claude/projects/<cwd-hash>/memory/`, keyed by the
working directory — so repetitions of the same arm share it, and the agent reads
it back on a fresh run, leaking findings between runs. Include `~/.claude/projects`
in `reset_paths` to clear it. Because that path spans **all** Claude Code
projects/sessions on the host (not just this arm's), run the benchmark on a
dedicated host and not alongside other `claude` sessions.

> These are whole directories, so on a machine where you also run the agent
> manually, point `reset_paths` at a dedicated benchmark location or run the
> benchmark on its own host — a run will move *all* of `~/pt-agent-state` to
> trash, not just the benchmark target's subdir.

The **agent under test is not modified** — its resume-from-state behavior is a
real feature (it's how progress survives context compression); the benchmark
just controls the environment so that feature has nothing stale to resume from.

## Budget

`budget.wall_time_s`, `budget.usd`, and `budget.tokens` are all hard limits: the
adapter tracks the streamed usage and kills the run when any is exceeded, writing
`status: budget_exceeded`. The runner also enforces `wall_time_s` as a subprocess
backstop and totals every cap across max-coverage continuation iterations.

USD can only preempt mid-run if the model is priceable — the CLI's authoritative
`total_cost_usd` arrives only in the terminal `result` event, so the adapter
derives a running cost estimate from token usage using a built-in price table
(override per-arm via `adapter_config.prices`: `input_per_mtok`, `output_per_mtok`,
optional `cache_write_per_mtok` / `cache_read_per_mtok`). For a model with no known
price, USD is enforced post-hoc from the final cost, while `tokens` and
`wall_time_s` still bound the run.

## Safety refusals

If a safeguard declines a turn (the API's `stop_reason: "refusal"`, e.g. category
`cyber`), the `claude` run ends but streams like a normal completion — so without
handling it the run would be recorded as `completed`, indistinguishable from the
agent finishing on its own. The adapter watches the stream for a refusal and
promotes the terminal status to `refused` (with the refusal `category`), so
results record why the run stopped. In max-coverage mode the runner treats
`refused` as terminal and does not re-invoke the agent back into the same refusal.
