"""Validate the HA integration's pure-Python math two ways:

1. Synthetic data with known physics (as tests/synthetic_check.py does for
   the offline toolkit).
2. The real cached winter data, cross-checked against the offline toolkit's
   published results (HLC ~356 W/K, loft ratio ~0.05).

Run: .venv/bin/python tests/integration_math_check.py
"""

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

# ---------- 2. real cached winter data ----------
import os
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
}
real_stats = {}
for spec in real_conf["rooms"].values():
    real_stats[spec["temperature"]] = cached_stats(spec["temperature"], "mean")
    real_stats[spec["heating_power"]] = cached_stats(spec["heating_power"], "mean")
for key in ("outdoor", "loft"):
    real_stats[real_conf[key]] = cached_stats(real_conf[key], "mean")
real_stats[real_conf["gas_meter"]] = cached_stats(real_conf["gas_meter"], "sum")

now_real = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
real = tm.compute_all(real_stats, real_conf, TZ, now_real, (60, 120, 365))

hlc = real["hlc"]
assert hlc, "no HLC fit on real data"
print(f"\nreal hlc: {hlc['hlc_w_per_k']:.0f} W/K, R² {hlc['r_squared']:.2f}, "
      f"{hlc['days_used']} days, window {hlc['window_days']}d, "
      f"dhw baseline {hlc['dhw_baseline_kwh_per_day']:.1f} kWh/d, "
      f"recent {hlc.get('recent_hlc_w_per_k', float('nan')):.0f} W/K "
      f"over {hlc.get('recent_window_days')}d")
assert abs(hlc["hlc_w_per_k"] - 356) < 30, "diverges from offline toolkit result"

loft_fit = real["loft"]
assert loft_fit, "no loft fit on real data"
print(f"real loft: ratio {loft_fit['ratio']:.2f}, {loft_fit['hours_used']} hours, "
      f"window {loft_fit['window_days']}d")
assert abs(loft_fit["ratio"]) < 0.2, "diverges from offline toolkit result"

print("real taus:")
for room_name, fit in sorted(real["rooms"].items(),
                             key=lambda kv: kv[1]["tau_median_h"] if kv[1] else 999):
    if fit:
        print(f"  {room_name:12s} {fit['tau_median_h']:6.1f} h  "
              f"({fit['nights_fitted']} nights, window {fit['window_days']}d)")
    else:
        print(f"  {room_name:12s} no fit")

print("\nall integration math checks passed")
