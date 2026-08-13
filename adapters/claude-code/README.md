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

## Run isolation: fresh `$HOME` per run

Agents persist state on disk to survive context compression — pt-agent writes
findings to `~/pt-agent-state/<target>/` and raw output to
`~/pt-agent-output/<target>/`. Across repetitions against the same target those
paths would collide, and a later run could read the earlier run's state and skip
ahead — contaminating the "each run starts fresh" assumption the benchmark
depends on.

To prevent that, the adapter gives every run its own `HOME` (a `home/` dir inside
the run's workdir) and symlinks only auth/config back in (`.claude`,
`.claude.json`, `.gitconfig`). So `~/pt-agent-state` resolves *inside the
sandbox*, empty at the start of every run. This is the agent-side analogue of
`compose down -v` on the target, and it's agent-agnostic: it isolates whatever
any agent writes under `$HOME` without the benchmark needing to know its paths.

The **agent under test is not modified** — its resume-from-state behavior is a
real feature (it's how progress survives context compression); the benchmark
just controls the environment so that feature has nothing stale to resume from.

Opt out (use the real `$HOME`) with:

```yaml
adapter_config:
  repo: /path/to/agent
  isolate_home: false
```

## Budget

`budget.wall_time_s` is enforced by the adapter (it kills the run when exceeded)
and again by the runner as a backstop. `tool_calls` and `usd` are advisory in v1
— passed into the task prompt but not hard-enforced.
