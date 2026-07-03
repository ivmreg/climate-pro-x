# climate-pro-x — thermal efficiency analysis for a solid-brick flat

Estimates how thermally efficient your home is from data you already have in
Home Assistant: per-room thermometers, Tado TRVs + boiler control, an outdoor
sensor, a loft sensor and a weather integration.

## What it measures

| Metric | What it tells you | Needs |
|---|---|---|
| **Thermal time constant τ (hours), per room** | How fast each room cools when heating is off. Low τ = leaky room. | Temperatures only |
| **Heat Loss Coefficient HLC (W/K), whole home** | Watts lost per degree of indoor/outdoor difference. *The* retrofit benchmark. | Temperatures + a heat-input proxy (Tado heating power %, or real gas kWh) |
| **Loft ratio** | Whether your ceiling or your roof is the weak link — i.e. would loft insulation pay off. | Loft + indoor + outdoor temperatures |

Typical uninsulated solid-wall UK flats sit around **200–350 W/K**; per-room
time constants under ~10 h at night usually indicate significant draughts or
uninsulated external walls.

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
   .venv/bin/python -m ha_efficiency cooling     # per-room time constants
   .venv/bin/python -m ha_efficiency hlc         # heat loss coefficient
   .venv/bin/python -m ha_efficiency loft        # ceiling vs roof analysis
   ```

Plots land in `output/`, cached history in `data/`.

## Notes on data depth

The `pull` command uses the recorder history API, which by default keeps ~10
days. That's plenty for cooling curves; for a robust HLC you ideally want
weeks of heating-season data. If your recorder retention is short, run `pull`
periodically — the cache is additive — or extend `purge_keep_days`.

## The Home Assistant integration (Phase 2)

`custom_components/thermal_efficiency/` is a custom integration running the
same (validated) maths live inside HA, straight from the recorder's
long-term statistics — no tokens, no polling, no pip requirements.

Entities (updated every 6 h, all under one "Thermal Efficiency" device):

- `sensor.thermal_efficiency_heat_loss_coefficient` — W/K over the full
  window (stable, season-blended), with attributes: rating, R², days used,
  DHW baseline, and a shorter-window `recent_hlc_w_per_k` for spotting
  improvements after e.g. draught-proofing.
- `sensor.thermal_efficiency_<room>_time_constant` — median overnight
  cooling τ in hours; nights are skipped when Tado heating power shows the
  radiator ran, and gated on fit quality otherwise.
- `sensor.thermal_efficiency_loft_ratio` — ceiling-vs-roof loss split from
  cold nights; robust to flatlined (dead-battery) sensors.

### Install

1. Copy `custom_components/thermal_efficiency/` into your HA config
   directory: `/config/custom_components/thermal_efficiency/` (via the
   Samba/SSH add-on, or the File editor add-on).
2. Add to `configuration.yaml` (adjust entities to yours — see
   `config.yaml` in this repo for the known-good mapping):

   ```yaml
   thermal_efficiency:
     gas_meter: sensor.smart_meter_gas_import
     outdoor: sensor.sonoff_outdoor_sensor_temperature
     loft: sensor.aqara_loft_sensor_temperature
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

3. Restart Home Assistant. Sensors appear within a minute (first
   computation runs on startup over up to a year of statistics).

Tests: `tests/synthetic_check.py` (offline toolkit) and
`tests/integration_math_check.py` (integration maths, synthetic + real
cached data) — both run with `.venv/bin/python`.
