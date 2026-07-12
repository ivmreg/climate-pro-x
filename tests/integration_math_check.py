"""Validate the HA integration's pure-Python math two ways:

1. Synthetic data with known physics (as tests/synthetic_check.py does for
   the offline toolkit).
2. The real cached winter data, cross-checked against the offline toolkit's
   published results (HLC ~356 W/K, loft ratio ~0.05).

Run: .venv/bin/python tests/integration_math_check.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

# Load thermal_math directly by path: importing the package would pull in
# homeassistant, which isn't installed here.
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "thermal_math",
    ROOT / "custom_components" / "thermal_efficiency" / "thermal_math.py",
)
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

TZ = ZoneInfo("Europe/London")


def to_stats_rows(series: pd.Series, kind: str) -> list[dict]:
    return [
        {"start": ts.timestamp(), kind: float(v)}
        for ts, v in series.items()
        if not pd.isna(v)
    ]


# ---------- 1. synthetic, known physics ----------
rng = np.random.default_rng(7)
idx = pd.date_range("2026-01-01", "2026-02-15", freq="1h", tz="UTC")
hours = np.arange(len(idx))
# daily cycle + slow multi-day weather swings (regression needs day-to-day
# dT variance to have anything to fit)
outdoor = pd.Series(
    4 + 3 * np.sin(2 * np.pi * (hours - 15) / 24)
    + 5 * np.sin(2 * np.pi * hours / (24 * 11)),
    index=idx,
)

TAU_TRUE = 18.0
temp = np.empty(len(idx))
temp[0] = 20.0
heat_on = np.array([7 <= t.astimezone(TZ).hour < 23 for t in idx])
for i in range(1, len(idx)):
    temp[i] = 20.0 if heat_on[i] else (
        outdoor.iloc[i] + (temp[i - 1] - outdoor.iloc[i]) * np.exp(-1 / TAU_TRUE)
    )
room = pd.Series(temp + rng.normal(0, 0.02, len(idx)), index=idx)
heating = pd.Series(np.where(heat_on, 35.0, 0.0), index=idx)

conf = {
    "rooms": {"room": {"temperature": "sensor.room", "heating_power": "sensor.heat"}},
    "outdoor": "sensor.out",
    "gas_meter": "sensor.gas",
    "loft": "sensor.loft",
    # The synthetic gas input below is already expressed as delivered heat.
    "boiler_efficiency": 1.0,
}

HLC_TRUE, GAINS = 300.0, 5.0
dt_hourly = (room - outdoor).clip(lower=0)
gas_hourly = (HLC_TRUE * dt_hourly / 1000 - GAINS / 24
              + rng.normal(0, 0.05, len(idx))).clip(lower=0)
gas_cum = gas_hourly.cumsum() + 3570.136  # replicate the Glow sum offset

R_TRUE = 0.35
loft_series = outdoor + R_TRUE * (room - outdoor)

stats = {
    "sensor.room": to_stats_rows(room, "mean"),
    "sensor.out": to_stats_rows(outdoor, "mean"),
    "sensor.heat": to_stats_rows(heating, "mean"),
    "sensor.gas": to_stats_rows(gas_cum, "sum"),
    "sensor.loft": to_stats_rows(loft_series, "mean"),
}
now = idx[-1].to_pydatetime()
result = tm.compute_all(stats, conf, TZ, now, (60,))

hlc = result["hlc"]
assert hlc, "no HLC fit on synthetic data"
assert abs(hlc["hlc_w_per_k"] - HLC_TRUE) / HLC_TRUE < 0.10, hlc
print(f"synthetic hlc ok: {hlc['hlc_w_per_k']:.0f} W/K (true {HLC_TRUE:.0f}), "
      f"R² {hlc['r_squared']:.2f}, {hlc['days_used']} days")

tau = result["rooms"]["room"]
assert tau, "no tau fit on synthetic data"
assert abs(tau["tau_median_h"] - TAU_TRUE) / TAU_TRUE < 0.20, tau
print(f"synthetic tau ok: {tau['tau_median_h']:.1f} h (true {TAU_TRUE}), "
      f"{tau['nights_fitted']} nights")

loft_fit = result["loft"]
assert loft_fit and abs(loft_fit["ratio"] - R_TRUE) < 0.05, loft_fit
print(f"synthetic loft ok: ratio {loft_fit['ratio']:.2f} (true {R_TRUE})")

# Heating filter: with the radiator running all night, no tau should fit
always_on = pd.Series(np.full(len(idx), 50.0), index=idx)
stats_on = stats | {"sensor.heat": to_stats_rows(always_on, "mean")}
result_on = tm.compute_all(stats_on, conf, TZ, now, (60,))
assert result_on["rooms"]["room"] is None or \
    result_on["rooms"]["room"]["nights_fitted"] == 0
print("heating filter ok: no nights fitted when heating runs overnight")

# floor_area_m2: HLC normalised to W/K/m2
conf_area = conf | {"floor_area_m2": 105.0}
result_area = tm.compute_all(stats, conf_area, TZ, now, (60,))
hlc_area = result_area["hlc"]
assert "hlc_w_per_k_per_m2" in hlc_area
assert abs(hlc_area["hlc_w_per_k_per_m2"] - hlc_area["hlc_w_per_k"] / 105.0) < 1e-9
print(f"floor_area_m2 ok: {hlc_area['hlc_w_per_k_per_m2']:.2f} W/K/m2")

# loft_since: a relocated sensor's pre-move history sat somewhere warm indoors
# (fluctuating, not flat, so drop_flatlines can't catch it) before it started
# tracking the loft. Move it near the end of the window - mirrors the real
# case, where most of the analysis window predates a recent relocation, so
# the contaminated data is the majority and would otherwise dominate the
# median. Cutting it off at the move date should recover ~R_TRUE; without the
# cutoff the contaminated data should skew it noticeably.
move_date = idx[int(len(idx) * 0.9)].astimezone(TZ).date()
moved = np.array([ts.astimezone(TZ).date() >= move_date for ts in idx])
contaminated_indoor = room + 12.0 + rng.normal(0, 0.3, len(idx))
loft_true = outdoor + R_TRUE * (room - outdoor)
loft_relocated = pd.Series(
    np.where(moved, loft_true.values, contaminated_indoor.values), index=idx
)
stats_relocated = stats | {"sensor.loft": to_stats_rows(loft_relocated, "mean")}

result_no_cutoff = tm.compute_all(stats_relocated, conf, TZ, now, (60,))
assert result_no_cutoff["loft"] is None, \
    "physically impossible contaminated loft history should be suppressed"

conf_since = conf | {"loft_since": move_date}
result_cutoff = tm.compute_all(stats_relocated, conf_since, TZ, now, (60,))
assert abs(result_cutoff["loft"]["ratio"] - R_TRUE) < 0.05, result_cutoff["loft"]
print(f"loft_since ok: contaminated uncut history suppressed "
      f"-> cut ratio {result_cutoff['loft']['ratio']:.2f}")

# loft_humidity: informational attribute alongside the ratio
humidity = pd.Series(55 + 5 * np.sin(2 * np.pi * hours / 24), index=idx)
conf_hum = conf_since | {"loft_humidity": "sensor.loft_humidity"}
stats_hum = stats_relocated | {
    "sensor.loft_humidity": to_stats_rows(humidity, "mean")
}
result_hum = tm.compute_all(stats_hum, conf_hum, TZ, now, (60,))
assert result_hum["loft"] and "humidity_pct" in result_hum["loft"]
print(f"loft_humidity ok: {result_hum['loft']['humidity_pct']:.1f}%")

# ventilation: CO2 decay -> air-change rate -> ventilation/fabric HLC split.
# Every ~5th day is a fully unoccupied "quiet" day (e.g. away/weekend), so
# CO2 genuinely reaches the true outdoor baseline sometimes - without that,
# short nightly-only decay windows never get close enough to the asymptote
# for the low-percentile baseline estimate to be unbiased, which then biases
# the fitted ACH itself (a real property of this method, not a test quirk).
ACH_TRUE = 0.22
CO2_BASELINE_TRUE = 420.0
local_idx = idx.tz_convert(TZ)
quiet_day = local_idx.dayofyear % 5 == 0
co2_vals = np.empty(len(idx))
co2_vals[0] = CO2_BASELINE_TRUE + 300
for i in range(1, len(idx)):
    occupied = (9 <= local_idx[i].hour < 18) and not quiet_day[i]
    if occupied:
        target = 1100 + rng.normal(0, 50)
        co2_vals[i] = co2_vals[i - 1] + 0.3 * (target - co2_vals[i - 1]) + rng.normal(0, 5)
    else:
        co2_vals[i] = CO2_BASELINE_TRUE + (co2_vals[i - 1] - CO2_BASELINE_TRUE) * np.exp(-ACH_TRUE)
co2_series = pd.Series(co2_vals + rng.normal(0, 2, len(idx)), index=idx)

stats_co2 = stats | {"sensor.co2": to_stats_rows(co2_series, "mean")}
conf_co2 = conf | {
    "co2": "sensor.co2", "floor_area_m2": 105.0, "ceiling_height_m": 2.45,
    "boiler_efficiency": 1.0,
}
result_co2 = tm.compute_all(stats_co2, conf_co2, TZ, now, (60,))
losses = result_co2["losses"]
assert losses, "no ventilation/fabric split on synthetic data"
assert abs(losses["ach"] - ACH_TRUE) / ACH_TRUE < 0.2, losses
expected_vent = tm.AIR_HEAT_CAPACITY * ACH_TRUE * 105.0 * 2.45
assert abs(losses["ventilation_w_per_k"] - expected_vent) / expected_vent < 0.3, losses
print(f"synthetic ventilation ok: ACH {losses['ach']:.2f}/h (true {ACH_TRUE}), "
      f"ventilation {losses['ventilation_w_per_k']:.0f} W/K, "
      f"fabric {losses['fabric_w_per_k']:.0f} W/K, {losses['windows']} windows")

# dhw: mains-temperature-scaled baseline corrects a seasonally-biased HLC.
# Uses thermal_math's dict-keyed-by-date API directly (compute_all's own
# daily aggregation from hourly stats is already exercised by the HLC/tau
# checks above; this isolates the DHW baseline/correction math itself).
FABRIC_HLC_TRUE2, HOB_PILOT_TRUE2 = 300.0, 2.0
# Exaggerated on purpose so the correction's effect is unambiguous against
# noise - the real effect on a typical UK combi is a much smaller, documented
# refinement (a few W/K), not this synthetic test's ~50 kWh/day.
DHW_SUMMER_KWH_TRUE = 50.0

import datetime as _dt
days2 = [_dt.date(2025, 6, 1) + _dt.timedelta(days=d) for d in range(365)]
rng2 = np.random.default_rng(11)
day_of_year2 = np.array([d.timetuple().tm_yday for d in days2])
outdoor_daily_true2 = 10 + 8 * np.cos(2 * np.pi * (day_of_year2 - 15) / 365)
dt_daily_true2 = np.clip(18 - outdoor_daily_true2, 0, None) + rng2.normal(0, 0.3, len(days2))
summer_mask2 = dt_daily_true2 < tm.DHW_BASELINE_MAX_DT
outdoor_summer_mean_true2 = float(outdoor_daily_true2[summer_mask2].mean())
baseline_true2 = {"kwh_per_day": DHW_SUMMER_KWH_TRUE, "outdoor_mean": outdoor_summer_mean_true2}
dhw_true2 = np.array([tm.dhw_daily_kwh(o, baseline_true2) for o in outdoor_daily_true2])
heating_true2 = FABRIC_HLC_TRUE2 * dt_daily_true2 * 24 / 1000
q_true2 = (
    HOB_PILOT_TRUE2 + dhw_true2 + heating_true2 + rng2.normal(0, 0.5, len(days2))
).clip(min=0)

q_by_day2 = {d: float(q) for d, q in zip(days2, q_true2)}
dt_by_day2 = {d: float(v) for d, v in zip(days2, dt_daily_true2)}
outdoor_by_day2 = {d: float(v) for d, v in zip(days2, outdoor_daily_true2)}
since2 = days2[0]

baseline_est2 = tm.dhw_baseline(q_by_day2, dt_by_day2, outdoor_by_day2, since2)
assert baseline_est2, "no DHW baseline recovered (thermal_math)"
# The "summer" band (dt < 3K) spans outdoor temps over which the mains model
# varies, and still lets a little real heating leak in (dt up to just under
# 3K, not exactly 0) - both genuine properties of the dT-threshold
# approximation, not test quirks. Compute the expected median the same way.
expected_summer_median2 = float(
    np.median(HOB_PILOT_TRUE2 + dhw_true2[summer_mask2] + heating_true2[summer_mask2])
)
assert abs(baseline_est2["kwh_per_day"] - expected_summer_median2) < 3, baseline_est2

raw_fit2 = tm.fit_hlc(q_by_day2, dt_by_day2, since2)
assert raw_fit2, "no raw HLC fit (thermal_math)"
raw_error2 = abs(raw_fit2["hlc_w_per_k"] - FABRIC_HLC_TRUE2)

dhw_by_day2 = {d: tm.dhw_daily_kwh(outdoor_by_day2[d], baseline_est2) for d in q_by_day2}
corrected_fit2 = tm.fit_hlc(q_by_day2, dt_by_day2, since2, dhw_by_day2)
assert corrected_fit2, "no corrected HLC fit (thermal_math)"
corrected_error2 = abs(corrected_fit2["hlc_w_per_k"] - FABRIC_HLC_TRUE2)

assert raw_error2 > 15, f"expected the uncorrected fit visibly biased, got {raw_error2:.1f} W/K off"
assert corrected_error2 < 10, f"expected the DHW correction to recover the true fabric HLC, got {corrected_error2:.1f} W/K off"
assert corrected_error2 < raw_error2, "DHW correction should reduce, not increase, the HLC bias"
print(f"thermal_math dhw ok: baseline {baseline_est2['kwh_per_day']:.1f} kWh/day "
      f"(expected ~{expected_summer_median2:.1f}); raw HLC {raw_fit2['hlc_w_per_k']:.0f} W/K "
      f"(off {raw_error2:.0f}) -> corrected {corrected_fit2['hlc_w_per_k']:.0f} W/K "
      f"(off {corrected_error2:.0f}), true fabric HLC {FABRIC_HLC_TRUE2:.0f} W/K")

# fit_water_gas: hourly gas-vs-water regression recovers a known Wh/L rate
WH_PER_L_TRUE2, HOB_PILOT_HOURLY_TRUE2 = 18.0, 0.1
n_hours2 = 24 * 40
rng3 = np.random.default_rng(13)
start_ts = int(pd.Timestamp("2026-02-01", tz="UTC").timestamp())
water_hourly_true2 = np.clip(rng3.exponential(3.0, n_hours2) - 2.0, 0, None)
gas_hourly_true2 = (
    HOB_PILOT_HOURLY_TRUE2 + WH_PER_L_TRUE2 / 1000 * water_hourly_true2
    + rng3.normal(0, 0.02, n_hours2)
).clip(min=0)
gas_cum2 = np.cumsum(gas_hourly_true2) + 500.0
water_cum2 = np.cumsum(water_hourly_true2) + 1000.0
gas_sum_dict = {start_ts + i * 3600: float(v) for i, v in enumerate(gas_cum2)}
water_sum_dict = {start_ts + i * 3600: float(v) for i, v in enumerate(water_cum2)}

water_fit2 = tm.fit_water_gas(gas_sum_dict, water_sum_dict)
assert water_fit2, "no water regression fit (thermal_math)"
assert abs(water_fit2["wh_per_litre"] - WH_PER_L_TRUE2) / WH_PER_L_TRUE2 < 0.15, water_fit2
print(f"thermal_math fit_water_gas ok: {water_fit2['wh_per_litre']:.1f} Wh/L "
      f"(true {WH_PER_L_TRUE2}), R² {water_fit2['regression_r_squared']:.2f}, "
      f"{water_fit2['regression_hours']} hours")

# ---------- 2. real cached winter data ----------
if os.environ.get("SKIP_REAL_CACHE_CHECK") == "1":
    print("real-cache cross-check skipped by environment")
    raise SystemExit(0)

os.chdir(ROOT)
from ha_efficiency import store

def cached_stats(eid: str, kind: str) -> list[dict]:
    series = store.load(eid)
    return to_stats_rows(series, kind) if series is not None else []

real_conf = {
    "rooms": {
        room: {"temperature": f"sensor.{room}_vtrv_ema_temperature",
               "heating_power": f"sensor.{room}_heating_power"}
        for room in ["living_room", "bedroom", "kids_room", "kitchen", "bathroom", "office"]
    },
    "outdoor": "sensor.sonoff_outdoor_sensor_temperature",
    "gas_meter": "sensor.smart_meter_gas_import",
    "loft": "sensor.aqara_loft_sensor_temperature",
    "co2": "sensor.qp_sensor_co2",
    "water": "thames_water:thameswater_consumption",
    "floor_area_m2": 105.0,
    "ceiling_height_m": 2.45,
    "boiler_efficiency": 0.88,
    # Live tariff snapshot - its own LTS history is unusable (a price sensor
    # isn't a real meter, so the recorder's long-term "sum" for it is noise
    # around 0 GBP/kWh; see the offline `dhw` command, which fetches this
    # live rather than from the cache for the same reason).
    "gas_unit_rate": 0.05024,
}
real_stats = {}
for spec in real_conf["rooms"].values():
    real_stats[spec["temperature"]] = cached_stats(spec["temperature"], "mean")
    real_stats[spec["heating_power"]] = cached_stats(spec["heating_power"], "mean")
for key in ("outdoor", "loft", "co2"):
    real_stats[real_conf[key]] = cached_stats(real_conf[key], "mean")
real_stats[real_conf["gas_meter"]] = cached_stats(real_conf["gas_meter"], "sum")
real_stats[real_conf["water"]] = cached_stats(real_conf["water"], "sum")

now_real = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
real = tm.compute_all(real_stats, real_conf, TZ, now_real, (60, 120, 365))

hlc = real["hlc"]
assert hlc, "no HLC fit on real data"
print(f"\nreal hlc: {hlc['hlc_w_per_k']:.0f} W/K, R² {hlc['r_squared']:.2f}, "
      f"{hlc['days_used']} days, window {hlc['window_days']}d, "
      f"dhw baseline {hlc['dhw_baseline_kwh_per_day']:.1f} kWh/d, "
      f"recent {hlc.get('recent_hlc_w_per_k', float('nan')):.0f} W/K "
      f"over {hlc.get('recent_window_days')}d")
assert abs(hlc["fuel_input_hlc_w_per_k"] - 356) < 30, \
    "gas-side diagnostic diverges from the earlier result"
assert abs(hlc["hlc_w_per_k"] - 303) < 35, \
    "delivered HLC diverges from the efficiency-adjusted result"

loft_fit = real["loft"]
assert loft_fit, "no loft fit on real data"
print(f"real loft: ratio {loft_fit['ratio']:.2f}, {loft_fit['hours_used']} hours, "
      f"window {loft_fit['window_days']}d")
assert abs(loft_fit["ratio"]) < 0.2, "diverges from offline toolkit result"

dhw_real = real["dhw"]
assert dhw_real, "no DHW baseline on real data"
print(f"real dhw: {dhw_real['kwh_per_day']:.1f} kWh/day, {dhw_real['days_used']} days"
      + (f", £{dhw_real['cost_per_day_gbp']:.2f}/day (£{dhw_real['cost_per_year_gbp']:.0f}/yr)"
         if "cost_per_day_gbp" in dhw_real else ""))
assert 3 < dhw_real["kwh_per_day"] < 25, "diverges from the earlier probe (~10-14 kWh/day)"
if "wh_per_litre" in dhw_real:
    print(f"  water regression: {dhw_real['wh_per_litre']:.1f} Wh/L, "
          f"~{dhw_real['hot_fraction_pct']:.0f}% hot, R² {dhw_real['regression_r_squared']:.2f}, "
          f"{dhw_real['regression_hours']} hours")
if "space_heating_hlc_w_per_k" in hlc:
    print(f"real space-heating HLC (DHW-corrected): "
          f"{hlc['space_heating_hlc_w_per_k']:.0f} W/K (raw {hlc['hlc_w_per_k']:.0f} W/K)")

losses_real = real["losses"]
assert losses_real, "no ventilation/fabric split on real data"
print(f"real losses: ACH {losses_real['ach']:.2f}/h, ventilation "
      f"{losses_real['ventilation_w_per_k']:.0f} W/K, "
      f"fabric {losses_real['fabric_w_per_k']:.0f} W/K ({losses_real['windows']} windows)")
assert 0.05 < losses_real["ach"] < 0.4, "diverges from the earlier probe (~0.15-0.2 /h)"
assert 5 < losses_real["ventilation_w_per_k"] < 40, "diverges from the earlier probe (~13-15 W/K)"

print("real taus:")
for room_name, fit in sorted(real["rooms"].items(),
                             key=lambda kv: kv[1]["tau_median_h"] if kv[1] else 999):
    if fit:
        print(f"  {room_name:12s} {fit['tau_median_h']:6.1f} h  "
              f"({fit['nights_fitted']} nights, window {fit['window_days']}d)")
    else:
        print(f"  {room_name:12s} no fit")

print("\nall integration math checks passed")
