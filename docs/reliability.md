# Reliability and data interpretation

Climate Pro X surfaces measured source quality alongside model outputs. A
number is only useful when its units, time window, completeness and physical
meaning remain clear.

## Heat-loss model

The delivered heat-loss coefficient (HLC, W/K) is the slope of daily delivered
space-heating energy against indoor/outdoor temperature difference. Gas input
is corrected for the configured boiler efficiency, and an established
non-space-heating gas baseline is removed before the fit. The model requires
complete local days and sustained temperature difference; partial days, meter
gaps and physically or statistically unsupported fits do not count as valid
evidence. Local days can contain 23, 24 or 25 hours at daylight-saving
transitions. The model's `days_used`, `window_days`, `model_data_through`,
`source_lag_days`, confidence interval and effective independent-day estimate
describe its actual scope.

When a new qualifying heating day is unavailable, the integration may report
`historical_baseline_held`. This means the latest qualifying heating model is
retained; it does not mean recent conditions were measured by that model.
Readiness is exposed by `sensor.thermal_efficiency_data_readiness`, including
per-analysis status, source issues, usable/required observations, reasons and
next actions. The dashboards show the readiness summary and HLC anchor.

## Other estimates and limits

- Room cooling time constants summarize observed overnight cooling under the
  recorded heating, weather, thermal-mass and adjacent-room conditions. They
  are room-level diagnostics, not a whole-home survey.
- Loft coupling is directional evidence from the measured temperature series;
  it does not classify insulation or estimate payback.
- Indoor CO₂ decay produces a room-derived air-change proxy. The whole-home
  fabric/ventilation split is experimental because it extrapolates that proxy
  across the configured building volume. It is disabled by default and only
  runs after explicit `experimental_whole_home_ventilation: true` opt-in.
- Non-space-heating gas includes any cooking or pilot load unless a separate
  source distinguishes it. Total water is reported as household water and is
  not converted into hot-water litres without appropriate evidence.
- Tariff costs omit fixed standing charges unless explicitly stated by a
  separate source. Electricity internal gains are context and are not
  subtracted from gas fits.

## Source readiness

Choose sources from the Home Assistant entity registry and check their device
class, state class, unit and recorder history. Cumulative energy must convert
to kWh; temperatures must convert to Celsius; CO₂ must be a concentration; and
water needs a supported cumulative volume source. A tariff source is a live
price, not a meter. Source errors are reported with the affected role and an
action to take. A missing source or unavailable history is not represented as
zero consumption.

## Upgrade backup and rollback

Before upgrading, create and verify a Home Assistant backup that includes the
configuration directory, recorder database and `.storage` data. These hold
the YAML settings, historical statistics and entity/config-entry identities
that must stay together for a reliable rollback. If recorder data is stored
outside Home Assistant's backup scope, back up that database separately and
verify it can be read. Keep any encrypted backup key in a separate protected
location and confirm it is available before relying on the archive. Record
the installed integration version and keep the prior integration files
available.

If rollback is required, restore the prior integration files and the matching
configuration, recorder and `.storage` data from the same pre-upgrade backup
as one recovery set, using Home Assistant's supported restore workflow. Restore
the separate recorder database too when applicable. For a dashboard saved in
Home Assistant, roll it back through the dashboard's supported editor or API;
for a YAML dashboard, restore its mapping and generated YAML together. Do not
edit `.storage` files directly or combine a prior integration with newer
recorder/config-entry state. This document describes the recovery plan only;
it does not perform a backup or restore.
