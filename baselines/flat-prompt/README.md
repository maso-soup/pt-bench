# flat-prompt baseline

This directory is **intentionally bare**. It is the working directory the
`claude-code` adapter runs in for the `flat-prompt__*` arms.

Because Claude Code loads project instructions (`CLAUDE.md` / `AGENTS.md`) and
skills from the working directory and its ancestors, running from an empty
directory gives you the model + the standard tool set with **no pentest
scaffolding** — no playbooks, no skills, no risk-gate hooks, no state-file
doctrine.

That makes it the control in an A/B against an agent like pt-agent: same model,
same tools, same task prompt (built by the adapter from the run spec), same
budget — the *only* thing that differs is the scaffolding. The gap in coverage /
efficiency between `pt-agent__<model>` and `flat-prompt__<model>` is what the
scaffolding buys you.

Do not add a `CLAUDE.md`, `AGENTS.md`, or `.claude/` here — that would defeat the
purpose. Keep it empty except for this note.

> Note: user-level config (`~/.claude/CLAUDE.md`) and anything in a shared
> ancestor directory still load, but they load for *both* arms equally, so they
> don't confound the comparison. Only pt-agent's own repo-local scaffolding
> differs between the two.
