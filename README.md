# climate-pro-x — thermal efficiency analysis for a solid-brick flat

Estimates how thermally efficient your home is from data you already have in
Home Assistant: per-room thermometers, Tado TRVs + boiler control, an outdoor
sensor, a loft sensor and a weather integration.

## What it measures

| Metric | What it tells you | Needs |
|---|---|---|
| **Effective overnight cooling time constant τ (hours), per room** | How quickly a room cooled under the observed conditions. It combines fabric, draughts, thermal mass and heat exchange with adjacent rooms. | Temperatures; heating-power coverage strongly recommended |
| **Delivered Heat Loss Coefficient HLC (W/K), whole home** | Estimated heat delivered to replace each watt lost per degree of indoor/outdoor difference, after the DHW and boiler-efficiency corrections available from the data. | Temperatures + real gas kWh; the offline Tado proxy is trend-only |
| **Loft ratio** | Directional evidence about how closely loft temperature follows indoors versus outdoors; not an insulation payback calculation. | Loft + indoor + outdoor temperatures |
| **Ventilation vs fabric split (W/K)** | An exploratory split based on a room-derived CO2 decay proxy. A single room is not assumed to be a direct whole-home ACH measurement. | CO2 sensor, floor area, ceiling height, and valid HLC |
| **Non-space-heating gas baseline (kWh/day, £/day, £/yr)** | Summer gas not explained by space heating. It includes hot water and any gas cooking/pilot load, and is used to de-bias HLC. | Gas meter and enough complete low-heating days |

Treat these as household diagnostics rather than a substitute for a calibrated
co-heating test or professional retrofit survey. The integration suppresses
results that fail coverage, fit-quality, confidence or physical-bound checks.

## Setup

1. In Home Assistant: your profile → **Security** → **Long-lived access
   tokens** → create one.
2. `cp .env.example .env` and fill in `HA_URL` and `HA_TOKEN`.
3. Discover your entities and generate a config skeleton:

   ```bash
   .venv/bin/python -m ha_efficiency discover
   ```

   This prints every temperature/climate/weather entity it finds and writes a
   draft `config.yaml`. Edit it: map each room to its thermometer (and
   optionally its Tado heating-power sensor), set the outdoor and loft
   entities, and set `boiler_output_kw` (your Worcester Bosch's rated output —
   check the model plate; typically 24–30 kW).

4. Pull history into a local cache, then analyse:

   ```bash
   .venv/bin/python -m ha_efficiency pull --days 10
   .venv/bin/python -m ha_efficiency cooling      # per-room time constants
   .venv/bin/python -m ha_efficiency hlc          # heat loss coefficient
   .venv/bin/python -m ha_efficiency loft         # ceiling vs roof analysis
   .venv/bin/python -m ha_efficiency ventilation  # ventilation vs fabric loss split
   .venv/bin/python -m ha_efficiency dhw          # hot-water gas cost + DHW-corrected HLC
   ```

Plots land in `output/`, cached history in `data/`.

`ventilation` needs `co2_entity`, `floor_area_m2` and `ceiling_height_m` in
`config.yaml`, plus a gas meter for the HLC it splits. `dhw` needs a gas
meter and enough cached summer (heating-off) days; `gas_unit_rate_entity`
adds the £/day figure (fetched live — see the gotcha below), and `water_stat`
adds an informational Wh/L regression. Both are pulled via `pull --lts`,
same as the loft/HLC winter analyses.

**Gotcha:** a household water meter integration may expose *two* things that
look similar but aren't: a recorder-tracked `sensor.*` entity that only
updates once a day (diffing it gives one big daily spike, not real hourly
usage) and a separate *external statistic* (not a `sensor.*` entity — check
Developer Tools → Statistics) with genuine backfilled hourly readings. Use
the external statistic for `water_stat`; the CSV filename ends up with a
colon in it (e.g. `thames_water:thameswater_consumption.csv`), which is
expected. Similarly, a gas/electricity *tariff* sensor is usually
`state_class: total` but isn't a real meter, so its long-term "sum"
statistics are meaningless noise (recorder computes a running delta as if it
were a meter) — `gas_unit_rate_entity` is read from its **live** state, not
cached history, for both the offline `dhw` command and the live integration.

## Thermal Storyboard dashboard card

[`lovelace/thermal-storyboard-card.js`](lovelace/thermal-storyboard-card.js) is
a dependency-free custom Lovelace card that combines the integration's three
main visual stories:

- HLC evidence: fit status, R², heating days, confidence interval and the
  full-versus-recent estimate.
- Heat-loss flow: delivered HLC split into fabric and ventilation W/K, with
  non-space-heating gas kept separate because it is measured in kWh/day.
- Room thermal fingerprints: effective cooling time constants ranked from
  fastest to slowest cooling, with fit counts and observation windows.

To install it, copy the JavaScript file to
`/config/www/thermal-storyboard-card.js`, then add `/local/thermal-storyboard-card.js`
as a **JavaScript module** under **Settings → Dashboards → Resources**. Paste
[`lovelace/thermal_efficiency_dashboard.yaml`](lovelace/thermal_efficiency_dashboard.yaml)
into a dashboard's raw configuration editor. The example entity IDs match the
development instance; edit the `metahome_` variants if Home Assistant assigned
different IDs on your installation.

The full card configuration is explicit and therefore stable across entity
renames:

```yaml
type: custom:thermal-storyboard-card
title: Thermal efficiency
hlc: sensor.thermal_efficiency_heat_loss_coefficient
fabric: sensor.metahome_thermal_efficiency_fabric_heat_loss
ventilation: sensor.metahome_thermal_efficiency_ventilation_heat_loss
ach: sensor.metahome_thermal_efficiency_air_change_rate
hot_water: sensor.metahome_thermal_efficiency_hot_water_gas
loft: sensor.thermal_efficiency_loft_ratio
rooms:
  - sensor.thermal_efficiency_living_room_time_constant
  - sensor.thermal_efficiency_bedroom_time_constant
```

`rooms` may be omitted: the card then discovers available time-constant
entities that expose `nights_fitted`. Clicking any metric or room opens Home
Assistant's normal entity detail dialog. The evidence panel intentionally does
not fabricate a regression scatter plot from summary attributes; daily points
remain a future diagnostics-data enhancement.

## Notes on data depth

The `pull` command uses the recorder history API, which by default keeps ~10
days. That's plenty for cooling curves; for a robust HLC you ideally want
weeks of heating-season data. If your recorder retention is short, run `pull`
periodically — the cache is additive — or extend `purge_keep_days`. REST and
long-term-statistics pulls are now stored separately under `data/_sources/` so
different cumulative-meter baselines cannot be blindly interleaved. Run
`python -m ha_efficiency cache-audit --cumulative` to inspect or safely
canonicalise older cache files.

## The Home Assistant integration (Phase 2)

`custom_components/thermal_efficiency/` is a custom integration running the
same (validated) maths live inside HA, straight from the recorder's
long-term statistics — no tokens, no polling, no pip requirements.

Entities (updated every 6 h, all under one "Thermal Efficiency" device):

- `sensor.thermal_efficiency_heat_loss_coefficient` — delivered W/K over the
  full window after available DHW and boiler-efficiency corrections, with
  attributes for the fuel-input slopes, confidence interval, R² and days used,
  DHW baseline, a shorter-window `recent_hlc_w_per_k` for spotting
  improvements after e.g. draught-proofing, `hlc_w_per_k_per_m2` (set
  `floor_area_m2` to get this — the normalised benchmark assessors use), and
  `space_heating_hlc_w_per_k` — the same fit with hot-water gas subtracted
  day-by-day (needs a gas meter and enough summer history for a DHW
  baseline; see `hot_water_gas` below).
- `sensor.thermal_efficiency_<room>_time_constant` — median effective overnight
  cooling τ in hours; nights require continuous temperature data and at least
  80% heating-power coverage when that source is configured.
- `sensor.thermal_efficiency_loft_ratio` — ceiling-vs-roof loss split from
  cold nights; robust to flatlined (dead-battery) sensors, plus a
  `humidity_pct` attribute if `loft_humidity` is configured. If your loft
  sensor was ever relocated (moved into the loft from somewhere else), set
  `loft_since` to the move date so its earlier, non-loft readings are
  ignored — a sensor that merely sat somewhere else warm won't necessarily
  flatline, so this isn't caught automatically otherwise.
- `sensor.thermal_efficiency_hot_water_gas` — non-space-heating gas (hot water,
  plus any gas cooking/pilot), kWh/day,
  from a robust summer (heating-off) baseline; attributes include
  `cost_per_day_gbp`/`cost_per_year_gbp` (needs `gas_unit_rate`) and,
  once enough hourly water history has accrued, an informational
  `wh_per_litre`/`hot_fraction_of_metered_water_pct` from a gas-vs-water
  regression (needs `water`).
- `sensor.thermal_efficiency_air_change_rate` — median room-derived air-change
  proxy (1/h) from independently fitted CO2 sensors (needs `co2`; configure an
  `outdoor_co2_sensor` or `outdoor_co2_ppm` rather than relying on the
  low-percentile indoor fallback when possible), plus
  `sensor.thermal_efficiency_ventilation_heat_loss` and
  `..._fabric_heat_loss` (W/K) splitting the delivered space-heating HLC
  between draughts and fabric (needs `co2`, `floor_area_m2` and
  `ceiling_height_m`) — the number that actually decides draught-proofing
  vs wall/window insulation.

### Install

1. Copy `custom_components/thermal_efficiency/` into your HA config
   directory: `/config/custom_components/thermal_efficiency/` (via the
   Samba/SSH add-on, or the File editor add-on).
2. Restart Home Assistant so it picks up the new integration.
3. **Settings → Devices & Services → Add Integration → "Thermal
   Efficiency".** The setup wizard is:
   - One form for whole-home sensors: outdoor, gas meter, loft (+
     `loft_since`/`loft_humidity`), floor area, ceiling height, a CO2
     sensor, a water statistic, a gas tariff sensor and boiler efficiency
     (the last five are all optional — see the metrics table above for
     what each unlocks).
   - Then, per room: pick an existing **Versatile Thermostat climate
     entity** to auto-fill that room's name (from its Area) and
     temperature sensor (its EMA sensor — same device as the climate
     entity), or leave it blank to enter everything by hand. A
     heating-power sensor is auto-suggested too, but only when there's
     exactly one unambiguous candidate in that area. Check "Add another
     room" to keep going, uncheck it once you've added the last one.

   Sensors appear within a minute of finishing the wizard (first
   computation runs over up to a year of statistics). To change anything
   later, use the integration's **Configure** button — existing rooms are
   replayed one at a time so you can fix an entity ID or drop a room,
   then you're offered the chance to add new ones.

Only one instance is allowed (it represents one home). If you're on an
older version of this repo that used YAML, the `thermal_efficiency:` block
in `configuration.yaml` still works — it's automatically imported into a
config entry on startup, exactly as if you'd used the wizard, and can be
removed from `configuration.yaml` afterwards. For reference, the YAML shape
matches `config.yaml` in this repo:

```yaml
thermal_efficiency:
  gas_meter: sensor.smart_meter_gas_import
  outdoor: sensor.sonoff_outdoor_sensor_temperature
  loft: sensor.portable_sensor_temperature
  loft_since: "2026-07-03"  # ignore this sensor's history before its move into the loft
  loft_humidity: sensor.portable_sensor_humidity
  floor_area_m2: 105
  ceiling_height_m: 2.45
  co2:
    - sensor.bedroom_co2
    - sensor.living_room_co2
  outdoor_co2_sensor: sensor.garden_co2
  # Used only when no valid outdoor sensor history is available:
  outdoor_co2_ppm: 420
  # An external statistic id (not a sensor.* entity - see the gotcha above),
  # e.g. from a water-utility integration with genuine hourly usage.
  water: thames_water:thameswater_consumption
  gas_unit_rate: sensor.smart_meter_gas_import_unit_rate
  boiler_efficiency: 0.88
  rooms:
    living_room:
      temperature: sensor.living_room_vtrv_ema_temperature
      heating_power: sensor.living_room_heating_power
    bedroom:
      temperature: sensor.bedroom_vtrv_ema_temperature
      heating_power: sensor.bedroom_heating_power
    kids_room:
      temperature: sensor.kids_room_vtrv_ema_temperature
      heating_power: sensor.kids_room_heating_power
    kitchen:
      temperature: sensor.kitchen_vtrv_ema_temperature
      heating_power: sensor.kitchen_heating_power
    bathroom:
      temperature: sensor.bathroom_vtrv_ema_temperature
      heating_power: sensor.bathroom_heating_power
    office:
      temperature: sensor.office_vtrv_ema_temperature
      heating_power: sensor.office_heating_power
```

### Version 0.4 migration notes

- Existing entity unique IDs are retained, but the headline HLC value is now
  delivered heat loss after boiler efficiency and any valid DHW correction.
  It will normally be lower than the old gas-input slope.
- Gas-side diagnostics remain available as attributes named
  `fuel_input_hlc_w_per_k` and `space_heating_fuel_input_hlc_w_per_k`.
- HLC now needs at least 20 complete, statistically useful heating days. A
  previously visible value may become unavailable until that evidence exists.
- Weak water regressions, impossible loft ratios and ventilation estimates
  larger than total delivered loss are suppressed.
- Old mixed caches are not rewritten automatically. Audit them with
  `python -m ha_efficiency cache-audit --cumulative`, then pull clean LTS data
  to populate the source-separated cache.

Tests: install `requirements-dev.txt` and run `pytest`. The suite covers nominal
and adversarial HLC, meter gaps, partial and DST days, cache backfills, Home
Assistant config/coordinator behavior, multiple CO2 sensors, loft bounds,
ventilation reconciliation, weak water regressions, and an immutable sanitized
heating-season fixture. CI enforces 80% coverage across trust-sensitive modules
and at least 90% branch coverage for the live pure-math core. The executable
cross-checks `tests/synthetic_check.py` and `tests/integration_math_check.py`
remain as broader known-physics and cached-data validations.
