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

## Budget

`budget.wall_time_s` is enforced by the adapter (it kills the run when exceeded)
and again by the runner as a backstop. `tool_calls` and `usd` are advisory in v1
— passed into the task prompt but not hard-enforced.
