# Synthetic computation benchmark, 2026-10-05

Measured using Python 3.13.15 on Windows. Each workload calls the real
`thermal_math.compute_all` pipeline. Timing and peak memory include deterministic
input creation, statistics-row construction, computation and complete-day counting.
`tracemalloc` is enabled, which materially increases execution time. These are
machine-dependent observations, not hardware sizing promises.

| Rooms / requested days | History | Hourly points | Complete gas days | Time (s) | Peak memory (MiB) |
|---|---|---:|---:|---:|---:|
| 2 / 30 | Complete | 5,762 | 29 | 0.57 | 2.18 |
| 2 / 30 | 1% gaps | 5,706 | 21 | 0.55 | 2.08 |
| 5 / 90 | Complete | 30,242 | 89 | 2.14 | 9.61 |
| 5 / 90 | 1% gaps | 29,906 | 67 | 2.08 | 9.49 |
| 12 / 365 | Complete | 245,282 | 365 | 27.81 | 75.74 |
| 12 / 365 | 1% gaps | 242,762 | 283 | 26.89 | 75.11 |

Dates use Europe/London; fixed elapsed-hour windows may end on a partial local
day at daylight-saving transitions. Gaps remove observations while preserving
the underlying cumulative consumption. They therefore reduce complete-day
evidence instead of making missing consumption appear to be zero.

These workloads intentionally contain noisy heating and room histories: HLC
and room cooling fits are withheld, while water and CO₂ estimates succeed.
They measure processing of these histories, not the maximum cost of every
possible accepted model or household calibration accuracy. Accepted-model
behavior is checked separately in the full-year synthetic pipeline tests.
Database access, archive migration, concurrent Home Assistant load and frontend
rendering are outside this benchmark. The larger workload supports keeping
composition and calculation off Home Assistant's event loop.

Reproduce with `python scripts/benchmark_analysis.py --json --output benchmark.json`.
The JSON records workload scope, complete-day counts and per-metric statuses;
all inputs are synthetic and contain no household data.
