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
  coverage (± CI), weighted coverage, iterations, time-to-first-solve, cost,
  tool calls, and **by-category / by-difficulty** breakdowns.
- Provenance (git SHA of pt-bench and the agent repo, dirty flag) from each run's
  `manifest.json`, so a comparison is anchored to the code that produced it.
- **Delete** a run from either card (with a confirm prompt) — removes its batch dir
  from disk so you don't have to clean up the results directory by hand.

## Endpoints (if you want the raw data)

- `GET /api/runs` — every run batch with headline metrics.
- `GET /api/runs/<arm>/<scenario>/<stamp>` — manifest + summary + per-repetition rows.
