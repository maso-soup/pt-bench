# pt-bench dashboard

A small, dependency-light local web UI to compare benchmark runs side by side.
Optional, isolated component — it reads the flat-JSON results tree directly (no
database) and imports only the shared result schema from `bench.core.results`.

## Run it

```bash
pip install -e .            # Flask ships with the benchmark; no extra step
pt-bench-dashboard          # http://127.0.0.1:8008
# options: --results-dir <path>  --port <n>  --host <addr>
```

`pt-bench-dashboard` is a console command installed with the package (equivalent
to `python -m bench.dashboard.app`).

It resolves the results location the same way the runner does
(`--results-dir` > `$PTBENCH_RESULTS_DIR` > `~/.local/share/pt-bench/results`),
so it shows whatever you've already run.

## What you get

- Pick any two runs (a run = one `<arm>/<scenario>/<stamp>/` batch). They can be
  different arms, or the same arm in different modes / at different times.
- An overlaid **coverage-at-budget** curve for A vs B, plus per-run cards with
  coverage (± CI), weighted coverage, iterations, time-to-first-solve, cost per
  run, tool calls, and **by-category / by-difficulty** breakdowns.
- Batch **totals** on each card — total wall time, total tokens (in + out), and
  total USD, summed over the batch's completed repetitions.
- **Findings per X**, with X switchable between time, tokens, and USD: findings
  (solved objectives) per hour, per 1M tokens, or per dollar. The toggle applies
  to both cards at once, so A and B are always read on the same denominator.
  Totals and rates cover completed repetitions only — the same runs the coverage
  numbers come from — and the tile says so when a batch had failures. A run whose
  adapter reported no usage shows `–` rather than a divide-by-zero rate.
- **Why it stopped** — a per-run breakdown of how the repetitions ended, counted
  by reason: the agent finishing on its own, solving everything, plateauing (no
  new solves), hitting the max-iteration cap, hitting a configured limit (wall
  time / tokens / USD), or erroring. Colour-coded, with a `×n` count when a batch
  mixed reasons across repetitions.
- Provenance (git SHA of pt-bench and the agent repo, dirty flag) from each run's
  `manifest.json`, so a comparison is anchored to the code that produced it.
- **Delete** a run from either card (with a confirm prompt) — removes its batch dir
  from disk so you don't have to clean up the results directory by hand.

## Endpoints (if you want the raw data)

- `GET /api/runs` — every run batch with headline metrics.
- `GET /api/runs/<arm>/<scenario>/<stamp>` — manifest + summary + per-repetition rows.
