# Analytical evidence and reproducibility

Observed meter days are retained separately from thermal eligibility. A
complete gas day can remain available while missing room temperatures, expected
heating data or an assignment transition exclude that date from HLC and DHW
classification. Source freshness and model eligibility are separate dates.
Meter days and heating-off classification still require complete coverage.

For the HLC only, a recorder temperature mean can be used when it is bracketed
by observed coverage across one interruption of at most 120 seconds, limited to
two such hours per room per local day. A temperature gap of at most three hours
may instead be interpolated between valid fully covered hourly observations,
with at most two interpolated hours per room per local day. Neither method
crosses assignment boundaries or quarantined periods; unbounded, contaminated,
or longer gaps remain excluded. Room-cooling and loft fits retain strict inputs.

Heating-off days still need complete expected heating coverage. An HLC day
with at most two missing heating hours is accepted as heating-on only when the
sum of observed hourly demand proves its full-day mean exceeds the off
threshold even if all missing hours were zero. The exported daily evidence
labels this as a lower bound, not a measured daily mean. Missing demand is never
filled with zero for the baseline or cooling fits.

The bundle's model version 2 records partial and interpolated temperature
hours and missing heating hours. When qualified days are present, HLC diagnostics
include a strict-policy comparison on the same input snapshot. Its change can
also reflect a different model window or hot-water training period, not just the
interpolation. The conditional regression interval does not account for gap
estimation. Earlier model-version-1 bundles remain supported by offline replay.

Download the integration's Home Assistant diagnostics for its versioned
`analysis_evidence` bundle. It contains anonymous model configuration, daily
source summaries, eligibility/exclusion reasons, included HLC targets, final
readiness and the reported HLC result. Source identifiers and room display
names are omitted. Dated consumption is still household information; review
diagnostics before sharing them.

Calendar dates use Home Assistant's configured local-day convention, including
clock changes. The location-bearing IANA zone is redacted as `local`; included
HLC targets are already normalized for the actual day length, so replay does
not need that location identifier. Generation timestamps use UTC.

The bundle identifies the history revision used by the resolver, configuration
fingerprint and evidence identifier. Recorder-query, composition and calculation
timings/counts describe that execution, not hardware sizing guarantees. Timing
fields are excluded from the evidence identifier. This identifier is not an
authenticity signature.

## Replay a frozen HLC fit

No Home Assistant connection or credentials are needed:

```bash
python -m ha_efficiency.evidence downloaded-diagnostics.json
# Equivalent with the ordinary CLI dependencies installed:
python -m ha_efficiency replay-evidence downloaded-diagnostics.json
```

Accepts the diagnostics document or its `analysis_evidence` object. Independent
closed-form OLS replays the included, 24-hour-normalized daily targets and
compares delivered HLC, intercept, R-squared and day count against unrounded
reported values. A mismatch exits 1; malformed or unsupported input exits 2.

This does not reconstruct hourly eligibility, retrain DHW, reproduce confidence
intervals, or replay room/loft/CO2 fits. With sufficient dates it also reports a
temporal training/test split and MAE/RMSE of the already attributed target.
Because DHW and eligibility are not independently retrained per block, that is
a diagnostic, not end-to-end out-of-sample validation or causal evidence of an
intervention. It does not change live results or quality thresholds.

The existing pandas CLI commands remain exploratory. Their cache selection and
preprocessing do not automatically reproduce the live dated room resolver. Use
the frozen evidence route for explicit live-HLC replay.

## Interpretation and presentation

`analysis_status` is the canonical final readiness consumed by entity adapters.
Numeric historical estimates can coexist with `source_problem` or
`historical_baseline_held`. Dates and reasons accompany those values. Dependent
ventilation/fabric results inherit HLC problems instead of claiming validity
merely because numeric fits exist.

The daily evidence's `delta_t_k` may use qualified temperatures, while
`strict_delta_t_k` contains only fully covered room-temperature days. Status
attributes distinguish raw complete days from qualified complete days. DHW
baseline training continues to use strict temperatures.

HLC confidence intervals describe conditional regression uncertainty. They
exclude uncertainty in temperature gap estimation, boiler efficiency, DHW attribution, calibration and room
representativeness. A separate sensitivity range varies configured efficiency
by five percentage points within supported bounds; it is a scenario rather
than a confidence interval or an estimate of actual efficiency.

DHW allocation remains modelled attribution. Clamp counts identify estimates
capped at measured gas; a nonnegative split does not establish correctness.
Cooling summaries include accepted-night spread and rejection context where
available, which must be considered when comparing observation periods.

The portable dashboards include readiness and dates. Interpretation text avoids
current loss-share claims when the split is unready. The optional compact
overview's source is `lovelace/climate-x-glance-card.js`; its mappings are supplied
by card configuration. It shows status beside retained HLC and gas-baseline
values, source/model dates and conditional-interval scope. Updating an installed
dashboard resource is a separate deployment step.

## History recovery

Known legacy documents migrate without removing visits, observations or archive
references. Unknown newer versions are rejected before reinterpretation.
Migration diagnostics expose a safe stage/code, anonymous source index/chunk,
retryability and recovery guidance. Transient failures retain bounded retries;
immutable evidence conflicts pause automatic retries. Explicit retry revalidates
the same chunks and does not overwrite them. Recovery may require the matching
recorder/storage backup; no automatic archive reset is performed.

## Development verification

Use Linux or WSL with `uv` and Node.js:

```bash
bash scripts/test_integration.sh 3.14
bash scripts/test_integration.sh 3.13
```

Each creates an ignored isolated environment using CI's Python/HA combination,
runs pytest, enforces combined/live-core coverage gates, and runs the Node
card-state checks. The tests use fixtures, not a live home.
