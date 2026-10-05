# Climate Pro X 0.8.0

Version 0.8.0 strengthens the reliability boundary for Home Assistant
statistics and makes model readiness visible to users. It is a semantic
release: results that do not meet the new validation rules can become
unavailable until enough complete, compatible history is collected.

## What changed

- Meter daily values use local-day coverage and reject incomplete or
  misaligned intervals. Daylight-saving days are accepted at their correct
  23-, 24- or 25-hour length; partial days are not treated as complete.
- Source metadata and units are validated before fitting. Source problems and
  readiness reasons, actions and observation counts are exposed in the
  diagnostic readiness sensor.
- The HLC displays its model-data anchor and freshness. A held historical
  baseline is identified explicitly when no newer qualifying heating data is
  available.
- Dashboard files are generated from `lovelace/dashboard_mapping.json`.
  Room names are serialized safely, optional live-power and daily-water cards
  are absent unless mapped, and a zero-room configuration shows setup guidance.
- Whole-home fabric/ventilation loss is labeled experimental and remains
  disabled unless the user opts in through integration options.
- The release documentation distinguishes room proxies, household totals,
  non-space-heating gas and delivered heat loss from direct measurements.

## Compatibility and upgrade

The integration manifest reports version `0.8.0`. Existing entity unique IDs
and configuration schema version 2 are retained. New readiness diagnostics
add an entity; users may need to enable diagnostic entities in Home Assistant
to see it. Previously visible estimates can be suppressed when complete-day,
source-unit, model-fit or physical-bound checks fail. That is expected until
the source is corrected or more qualifying history is recorded.

Before upgrade, make and verify a Home Assistant backup covering configuration,
recorder data and `.storage`, which contains entity and config-entry state. If
the database lives outside that backup, back it up separately; protect and
verify access to any encryption key in a separate location. For rollback,
restore the matching pre-upgrade integration files and data together through
Home Assistant's supported workflow. See [reliability and rollback](reliability.md)
for the recovery boundary.

## Dashboard setup

Confirm entity IDs in your own entity registry, update
`lovelace/dashboard_mapping.json`, and run:

```sh
python scripts/generate_dashboard.py
```

The generator updates the two checked-in YAML examples. `live_electricity_power`
and `daily_water_meter` are optional and default to `null`. The dashboards use
the existing ApexCharts and Plotly custom cards; the generator adds no frontend
dependencies. Readiness, HLC model anchor, source lag, experimental labels and
observation scope are visible in both dashboards.

## Validation status

The generated YAML is parsed in release tests, and samples exercise Jinja and
JavaScript handling of empty state values and room names containing ordinary
punctuation, quotes, newlines and Unicode. Synthetic performance measurements
are produced by `scripts/benchmark_analysis.py` for complete and gappy
small, normal and scaling workloads. Numbers are measured when the benchmark
is run and are not treated as fixed release claims.

No household data is included in benchmark fixtures. Final acceptance also
depends on the project CI jobs for the supported Home Assistant/Linux runtime;
see the release review for the actual CI result.
