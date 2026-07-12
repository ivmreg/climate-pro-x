# Climate Pro X Improvement Plan

## Objective

Make Climate Pro X safe for household decision support by ensuring every
published metric has validated inputs, sound physical semantics, adequate data
coverage, and an explicit quality assessment.

## Implementation status

Implemented in version 0.4:

- P0 trust gates, delivered-HLC semantics, confidence intervals, complete-day
  filtering, gap-aware meter deltas, and source-separated caches;
- bounded loft, ACH and loss-split outputs, stronger heating-data coverage,
  and time-varying-outdoor cooling fits;
- tariff and recorder-unit normalization, bounded configuration, an optional
  outdoor CO2 baseline, seasonal non-space-heating gas costing, and weak water
  regression suppression;
- pytest-discoverable adversarial tests, CI configuration, cache audit/repair,
  updated documentation, and migration guidance;
- Home Assistant fixture-based config-flow/coordinator tests, multiple indoor
  CO2 sensors, and a measured outdoor CO2 entity with scalar fallback;
- an immutable rounded and de-identified heating-season fixture, 80% enforced
  coverage across trust-sensitive modules, and 90% enforced branch coverage
  for the dependency-free live calculation core.

CO2 occupancy is not guessed without occupancy evidence. Instead, the decay
classifier rejects rising/fresh-source periods, discontinuous windows, poor
exponential fits, and implausibly fast decays consistent with an open window.
The resulting value remains explicitly labelled as a room-derived proxy.

## Milestone 1: Trust gates and test foundation

**Priority:** P0
**Estimate:** 2-3 days

- Convert the existing check scripts into conventional `pytest` tests.
- Add continuous integration, coverage reporting, reusable fixtures, and
  synthetic-data builders.
- Introduce a standard calculation result containing:
  - value;
  - validity status (`valid`, `provisional`, or `invalid`);
  - confidence or quality indicators;
  - observations used and rejected;
  - rejection reasons.
- Suppress results that are non-finite, negative where physically impossible,
  outside physical bounds, or based on insufficient data.

### Acceptance criteria

- Negative HLC values and fits with negligible explanatory power are rejected.
- A loft ratio of 1.67 is rejected rather than assigned an insulation verdict.
- Ventilation shares above 100% cannot be published.
- `pytest` discovers and runs the complete suite automatically.
- Existing valid synthetic cases continue to pass.

## Milestone 2: Correct HLC semantics

**Priority:** P0
**Estimate:** 2-3 days

- Exclude partial local days and days with inadequate hourly coverage.
- Use a stable set of eligible rooms throughout each fit.
- Require enough heating days, sufficient delta-T variation, a positive slope,
  acceptable fit quality, and uncertainty bounds.
- Subtract non-space-heating gas before fitting when a valid baseline exists.
- Apply seasonal boiler efficiency to produce delivered building HLC.
- Rename supporting values to make their boundaries explicit:
  - `fuel_input_hlc_w_per_k`;
  - `space_heating_fuel_input_hlc_w_per_k`;
  - `delivered_hlc_w_per_k`;
  - `regression_intercept_kwh_per_day`.
- Make delivered HLC the headline sensor and the input to building ratings.
- Mark short-window HLC as provisional unless it independently passes every
  validation gate.

### Acceptance criteria

- A result cannot be published from five poor-quality heating days.
- The headline HLC represents delivered building heat loss, not gas input.
- HLC units, components, and efficiency conversions reconcile numerically.
- Negative or statistically unsupported slopes produce an unavailable sensor
  with an explanatory reason.

## Milestone 3: Fix meter ingestion and the offline cache

**Priority:** P0
**Estimate:** 2-4 days

- Difference cumulative meters only across expected consecutive intervals.
- Treat resets, gaps, baseline changes, and large jumps as missing data rather
  than zero consumption.
- Require near-complete daily meter coverage before using a day in a model.
- Keep REST history and long-term-statistics data separate, or normalize them
  explicitly before merging.
- Store source provenance and unit metadata alongside cached values.
- Allow refreshed and backfilled values to replace stale cached observations.
- Add a cache audit and repair command.
- Rebuild the currently contaminated gas period from clean long-term
  statistics.

### Acceptance criteria

- A cumulative change across a 24-hour gap cannot become an hourly delta.
- Mixed cumulative baselines are detected automatically.
- Incomplete days are excluded and counted in result diagnostics.
- Repeated pulls are idempotent and can correct historical data.

## Milestone 4: Validate configured sources

**Priority:** P1
**Estimate:** 2 days

Validate source metadata during configuration and calculation:

- Gas must be cumulative energy convertible to kWh.
- Temperature sources must have the expected device class and convertible unit.
- CO2 must be a concentration source convertible to ppm.
- Water must be cumulative volume with a known unit.
- Tariffs must be normalized to GBP/kWh, including explicit conversion from
  pence per kWh.
- Boiler efficiency must be greater than zero and no greater than one.
- Floor area and ceiling height must be positive and within plausible ranges.

Expose the following diagnostic attributes:

- data start and end timestamps;
- freshness;
- valid and rejected day counts;
- per-room coverage and excluded rooms;
- normalized source units;
- calculation status and rejection reasons.

### Acceptance criteria

- Wrong-unit and wrong-state-class sources are rejected with actionable errors.
- Tariff unit mistakes cannot create 100x cost errors.
- Missing or stale required sources cannot silently produce a valid result.

## Milestone 5: Repair DHW and water metrics

**Priority:** P1
**Estimate:** 3-4 days

- Rename the headline value to "non-space-heating gas baseline."
- Require complete days and explicit heating-off evidence when heating-power
  data is available.
- Separate cooking and pilot consumption when evidence permits; otherwise keep
  the limitation visible.
- Calculate annual cost from seasonally modeled demand instead of multiplying a
  summer baseline by 365.
- Restrict gas-versus-water regression to non-heating periods.
- Detect and test plausible reporting lags between gas and water sources.
- Require adequate water variation, fit quality, and confidence intervals.
- Suppress hot-water fraction when those checks fail.

### Acceptance criteria

- The current real-data water fit with R-squared of approximately 0.13 is
  suppressed.
- DHW subtraction cannot create silently accepted negative heating energy.
- Annual cost reflects the modeled seasonal demand and states what charges it
  excludes.

## Milestone 6: Make secondary metrics appropriately cautious

**Priority:** P1
**Estimate:** 4-6 days

### Effective cooling time constant

- Rename "thermal time constant" to "effective overnight cooling time
  constant."
- Require continuous, aligned room, outdoor, and heating observations.
- Require strong heating-sensor coverage throughout each cooling window.
- Account for changing outdoor temperature instead of using one nightly mean.
- Report the night-level distribution, median fit quality, and uncertainty.

### Loft ratio

- Reject implausible ratios and unstable periods.
- Report the median, interquartile range, and outlier share.
- Add steady-period criteria beyond time-of-night and temperature difference.
- Replace deterministic payback verdicts with directional observations and
  visible limitations.

### CO2 and ventilation

- Allow a configured outdoor CO2 concentration or a dedicated outdoor sensor.
- Prefer higher-frequency observations over hourly means.
- Reject occupied, fresh-source, and likely open-window periods.
- Label a single-room estimate as a room-derived ACH proxy.
- Do not extrapolate one room's ACH to whole-home volume without an explicit,
  validated mixing assumption.
- Suppress inconsistent fabric/ventilation splits rather than clipping them.

### Acceptance criteria

- Secondary sensors clearly distinguish estimates and proxies from whole-home
  measurements.
- No categorical retrofit recommendation is based on one proxy alone.
- Each result exposes variability and the number of accepted observations.

## Milestone 7: Home Assistant integration tests and release

**Priority:** P1
**Estimate:** 3-4 days

Add Home Assistant fixture tests for:

- configuration and options flows;
- unit and source validation;
- recorder statistics retrieval;
- missing, unavailable, and stale states;
- coordinator failure and recovery;
- sensor availability, values, and attributes;
- YAML import and migration;
- local-day and daylight-saving-time boundaries.

Update the documentation with:

- exact metric definitions and formulas;
- required source types and units;
- minimum data-quality requirements;
- confidence and validity interpretation;
- known model limitations;
- migration notes for renamed sensors and attributes.

Release the semantic changes as a version that clearly signals compatibility
impact, preferably `0.4.0`.

### Acceptance criteria

- Integration behavior is tested without requiring a live Home Assistant
  instance.
- Renamed sensors and attributes have documented migration behavior.
- The release notes distinguish corrected semantics from ordinary renaming.

## Automated test matrix

The suite should include nominal, boundary, missing-data, and adversarial cases
for:

- negative, zero, and weak HLC slopes;
- low R-squared and narrow delta-T ranges;
- partial days and missing hours;
- meter resets and mixed cumulative baselines;
- daylight-saving-time transitions;
- changing or missing room populations;
- negative DHW-adjusted energy;
- implausible loft and ACH values;
- ventilation shares above 100%;
- low-quality, lagged, and heating-confounded water regressions;
- invalid units and unavailable entities;
- cache refreshes and historical backfills.

Add property-based invariants where practical:

- an accepted HLC is finite and positive;
- loss components are non-negative and reconcile to delivered HLC;
- percentages remain within 0-100%;
- incomplete data cannot turn an invalid result into a trusted result;
- consumption is never invented across meter gaps.

Replace mutable real-cache assertions with sanitized, immutable fixtures and
independently calculated expected outputs.

## Delivery order

1. Trust gates and physical bounds.
2. Delivered-HLC correction and metric renaming.
3. Gap-aware meter processing and cache repair.
4. `pytest`, CI, and adversarial regression tests.
5. Source and unit validation.
6. DHW and water-regression improvements.
7. CO2, cooling, and loft model refinements.
8. Documentation, migration, and the `0.4.0` release.

## Definition of done

- Every headline metric has a documented formula, boundary, and unit.
- Invalid or low-confidence results become unavailable instead of misleading.
- No calculation uses incomplete days or unvalidated meter gaps.
- Delivered HLC is explicitly distinct from fuel-input HLC.
- Percentages remain within 0-100%, and heat-loss components reconcile.
- Tests cover nominal, boundary, missing-data, and adversarial cases.
- Continuous integration passes with at least 90% branch coverage for the core
  calculation modules.
