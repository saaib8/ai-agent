# Baseline: main before the multi-agent rebuild

Run on 2026-10-21 against `main` at 813ed83, store 50, live models (`.env`),
with `python -m evals.conversations.run main=http://127.0.0.1:8801 --repeat=2`.

| Measure | main |
|---|---|
| Conversations run | 526 (263 cases x 2) |
| Passed | 459 (87.3%) |
| Failed | 67 |
| Error responses | 4 |
| Time per conversation, p50 | 13.0 s |
| Time per conversation, p95 | 26.8 s |

The full report is `baseline-main-report.txt` and the raw run is
`baseline-main-run.json`. The multi-agent branch is compared against this run
only on cases that test outcomes; cases that test a scripted step are set
aside (plan update 2026-10-21).
