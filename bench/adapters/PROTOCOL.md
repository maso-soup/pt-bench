# Agent Adapter Protocol

An **adapter** wraps one agent-under-test so the benchmark can drive it without
knowing anything about how it works. The boundary is a **subprocess + files**,
not a language API — so an adapter may be written in any language.

The benchmark never inspects the agent internally. It measures *coverage* from
the target (the scenario's oracle) and *efficiency* from the artifacts the
adapter writes here. Those are the only two channels.

## Invocation

The runner calls:

```
<adapter executable> --spec <path/to/run_spec.json>
```

`run_spec.json` is written by the runner before invocation. The adapter's
working directory is arbitrary; all paths it needs are inside the spec.

## Input — `run_spec.json`

```json
{
  "target_url": "http://localhost:3000",
  "scope": "Authorized web app pentest of http://localhost:3000 only ...",
  "model": "claude-opus-4-8",
  "budget": { "usd": 15, "tokens": 10000000, "wall_time_s": 3600 },
  "workdir": "/abs/path/to/this/run",
  "extra": { "repo": "/Users/mason/pt-agent" }
}
```

- `target_url` / `scope` — what the agent is told about the target.
- `objective` (optional) — scenario-supplied task text. When present the adapter
  should use it as the agent's task instead of its own default prompt; it may
  contain the literal `{output_dir}`, which the adapter substitutes with
  `workdir` so the agent knows where to write deliverables. The HTB scenario uses
  this to ask the agent to record captured flags in `flags.json` (below).
- `model` — model id the adapter should run the agent on (adapter-specific meaning).
- `budget` — resource caps for the run. `wall_time_s`, `usd`, and `tokens` are
  hard limits: the adapter MUST stop the agent when any is reached and report
  `status: budget_exceeded`. The runner additionally enforces `wall_time_s` as a
  subprocess-timeout backstop and, in max-coverage mode, decrements all three
  across continuation iterations so they cap the whole cell. Any field may be
  null (that dimension is uncapped).
  `usd`/`tokens` enforcement needs the streamed usage, so it lives in the adapter
  (the only component that sees incremental cost); an adapter that emits no usage
  can enforce only `wall_time_s`.
- `workdir` — the adapter MUST write all output artifacts here.
- `extra` — adapter-specific config passed straight through from the arm file.

## Output — written into `workdir`

| File | Required | Contents |
|------|----------|----------|
| `status.json` | **yes** | terminal status of the run |
| `usage.json` | recommended | token / cost / time totals |
| `tool_calls.jsonl` | recommended | one JSON object per tool invocation |
| `transcript.jsonl` | optional | raw agent transcript, for debugging |
| `flags.json` | scenario-specific | flags the agent captured, e.g. `{"user": "...", "root": "..."}` — written by the AGENT (not the adapter) into `workdir` when the scenario's `objective` asks for it. Trust-based scenarios (HTB) grade from this. |

### `status.json`
```json
{ "status": "completed", "reason": "" }
```
`status` ∈ `completed` | `budget_exceeded` | `refused` | `error`. Use `refused`
when a safety safeguard declines the run (the API's `stop_reason: "refusal"`);
add a `category` field (e.g. `"cyber"`) when the refusal carries one. If the
adapter writes no `status.json`, the runner records `error`.

### `usage.json`
```json
{ "tokens_in": 812345, "tokens_out": 41022, "cost_usd": 14.20, "wall_time_s": 2600 }
```
Any field may be omitted (recorded as `null`); grading degrades gracefully.

### `tool_calls.jsonl` — one object per line
```json
{ "ts": 1699999999.1, "tool": "Bash", "args": {"command": "nmap ..."}, "duration_s": 2.3 }
```
Only `tool` is required per line; the length of this file is the tool-call count.

## Contract test

Any adapter must satisfy `run_spec.schema.json`, `status.schema.json`, and
`usage.schema.json` under `_schema/`. `example-python/adapter.py` is the minimal
reference implementation — if you can reproduce its three output files, you can
be benchmarked.
