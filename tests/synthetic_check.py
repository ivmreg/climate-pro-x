"""Self-check: recover known parameters from synthetic data.

Simulates a home with known physics, then verifies each analysis gets the
right answer back. Run: .venv/bin/python tests/synthetic_check.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from ha_efficiency import cooling, dhw, hlc, loft, ventilation

rng = np.random.default_rng(42)
idx = pd.date_range("2026-01-01", "2026-01-15", freq="5min", tz="Europe/London")
hours = np.arange(len(idx)) * 5 / 60

# Outdoor: 5 degC mean, +-4 daily swing, slow weather drift
outdoor = pd.Series(
    5 + 4 * np.sin(2 * np.pi * (hours - 15) / 24)
    + np.cumsum(rng.normal(0, 0.01, len(idx))),
    index=idx,
)

# Room: held at 20 degC while heating (07:00-23:30), free exponential decay
# towards outdoor with TAU_TRUE overnight.
TAU_TRUE = 15.0
temp = np.empty(len(idx))
heat_on = np.array([(t.hour, t.minute) >= (7, 0) and (t.hour, t.minute) < (23, 30) for t in idx])
temp[0] = 20.0
step_h = 5 / 60
for i in range(1, len(idx)):
    if heat_on[i]:
        temp[i] = 20.0
    else:
        temp[i] = outdoor.iloc[i] + (temp[i - 1] - outdoor.iloc[i]) * np.exp(-step_h / TAU_TRUE)
room = pd.Series(temp + rng.normal(0, 0.03, len(idx)), index=idx)
heating_pct = pd.Series(np.where(heat_on, 40.0, 0.0), index=idx)

# --- cooling ---
fits = cooling.analyse_room(room, outdoor, heating_pct, "23:30", "06:30")
taus = [f.tau_hours for f in fits]
tau_med = float(np.median(taus))
assert len(fits) >= 8, f"expected >=8 nights fitted, got {len(fits)}"
assert abs(tau_med - TAU_TRUE) / TAU_TRUE < 0.15, f"tau {tau_med:.1f} vs true {TAU_TRUE}"
print(f"cooling ok: {len(fits)} nights, tau median {tau_med:.1f} h (true {TAU_TRUE})")

# --- hlc ---
# Daily heat input consistent with HLC_TRUE: q = HLC*dT*24/1000 - gains + noise
HLC_TRUE, GAINS = 250.0, 4.0
dt_daily = hlc.daily_delta_t({"room": room}, outdoor)
q_daily = (HLC_TRUE * dt_daily * 24 / 1000 - GAINS
           + pd.Series(rng.normal(0, 2, len(dt_daily)), index=dt_daily.index))
result = hlc.fit_hlc(q_daily, dt_daily)
got = result["hlc_w_per_k"]
assert abs(got - HLC_TRUE) / HLC_TRUE < 0.25, f"hlc {got:.0f} vs true {HLC_TRUE}"
print(f"hlc ok: {got:.0f} W/K (true {HLC_TRUE:.0f}), gains "
      f"{result['free_gains_kwh_per_day']:.1f} kWh/d (true {GAINS}), "
      f"R² {result['r_squared']:.2f}, {result['days']} days")

# --- loft ---
# Loft sits at known resistance ratio r=0.4 between indoor and outdoor
R_TRUE = 0.4
loft_series = outdoor + R_TRUE * (room - outdoor) + rng.normal(0, 0.1, len(idx))
res = loft.loft_ratio({"room": room}, loft_series, outdoor)
assert abs(res["ratio"] - R_TRUE) < 0.05, f"ratio {res['ratio']:.2f} vs true {R_TRUE}"
print(f"loft ok: ratio {res['ratio']:.2f} (true {R_TRUE}), {res['hours_used']} hours")

# --- meter input path ---
cumulative = q_daily.fillna(0).clip(lower=0).cumsum()
daily_from_meter = hlc.daily_heat_input_from_meter(cumulative)
assert (daily_from_meter.dropna() >= 0).all()
print("meter path ok")

# --- ventilation: air-change rate from CO2 decay curves ---
ACH_TRUE = 0.25
CO2_BASELINE_TRUE = 420.0
co2_idx = pd.date_range("2026-01-01", "2026-01-31", freq="1h", tz="Europe/London")
co2_vals = np.empty(len(co2_idx))
co2_vals[0] = CO2_BASELINE_TRUE + 300
# Every ~5th day is a fully unoccupied "quiet" day (e.g. away/weekend), so
# CO2 genuinely reaches the true outdoor baseline sometimes. Without that, a
# short nightly-only decay window never gets close enough to the asymptote
# for the low-percentile baseline estimate to be unbiased - which then
# biases the fitted ACH itself (a real property of this method: any
# constant overestimate of the baseline compresses the tail of every decay
# curve and skews the fitted slope steeper than truth), not a test quirk.
quiet_day = co2_idx.dayofyear % 5 == 0
for i in range(1, len(co2_idx)):
    occupied = (9 <= co2_idx[i].hour < 18) and not quiet_day[i]
    if occupied:  # CO2 rises, breaking any decay window
        target = 1100 + rng.normal(0, 50)
        co2_vals[i] = co2_vals[i - 1] + 0.3 * (target - co2_vals[i - 1]) + rng.normal(0, 5)
    else:  # unoccupied: pure exponential decay at the true ACH
        co2_vals[i] = CO2_BASELINE_TRUE + (co2_vals[i - 1] - CO2_BASELINE_TRUE) * np.exp(-ACH_TRUE)
co2_series = pd.Series(co2_vals + rng.normal(0, 2, len(co2_idx)), index=co2_idx)

vent_fit = ventilation.air_change_rate(co2_series)
assert vent_fit, "no air-change-rate fit on synthetic CO2 data"
assert abs(vent_fit["ach"] - ACH_TRUE) / ACH_TRUE < 0.2, vent_fit
print(f"ventilation ach ok: {vent_fit['ach']:.2f} /h (true {ACH_TRUE}), "
      f"{vent_fit['windows']} windows")

FLOOR_AREA, CEILING = 105.0, 2.45
split = ventilation.split_losses(
    vent_fit["ach"], FLOOR_AREA, CEILING, HLC_TRUE, boiler_efficiency=1.0
)
expected_vent_w_per_k = ventilation.AIR_HEAT_CAPACITY * ACH_TRUE * FLOOR_AREA * CEILING
assert abs(split["ventilation_w_per_k"] - expected_vent_w_per_k) / expected_vent_w_per_k < 0.25
assert abs(split["fabric_w_per_k"] - (HLC_TRUE - split["ventilation_w_per_k"])) < 1e-6
print(f"ventilation split ok: {split['ventilation_w_per_k']:.0f} W/K ventilation, "
      f"{split['fabric_w_per_k']:.0f} W/K fabric")

# --- dhw: mains-temperature-scaled baseline corrects a seasonally-biased HLC ---
FABRIC_HLC_TRUE, HOB_PILOT_TRUE = 300.0, 2.0
# Exaggerated on purpose so the correction's effect is unambiguous against
# noise - the real effect on a typical UK combi is a much smaller, documented
# refinement (a few W/K), not this synthetic test's ~50 kWh/day.
DHW_SUMMER_KWH_TRUE = 50.0

days = pd.date_range("2025-06-01", "2026-05-31", freq="1D", tz="Europe/London")
rng2 = np.random.default_rng(11)
day_of_year = days.dayofyear.values
outdoor_daily_true = 10 + 8 * np.cos(2 * np.pi * (day_of_year - 15) / 365)
dt_daily_true = np.clip(18 - outdoor_daily_true, 0, None) + rng2.normal(0, 0.3, len(days))

outdoor_daily = pd.Series(outdoor_daily_true, index=days)
dt_daily2 = pd.Series(dt_daily_true, index=days)

summer_mask = dt_daily_true < dhw.DHW_BASELINE_MAX_DT
outdoor_summer_mean_true = float(outdoor_daily_true[summer_mask].mean())
baseline_true = {"kwh_per_day": DHW_SUMMER_KWH_TRUE, "outdoor_mean": outdoor_summer_mean_true}
dhw_true = np.array([dhw.dhw_daily_kwh(o, baseline_true) for o in outdoor_daily_true])
heating_true = FABRIC_HLC_TRUE * dt_daily_true * 24 / 1000
q_daily2 = pd.Series(
    (HOB_PILOT_TRUE + dhw_true + heating_true + rng2.normal(0, 0.5, len(days))).clip(min=0),
    index=days,
)

baseline_est = dhw.dhw_baseline(q_daily2, dt_daily2, outdoor_daily)
assert baseline_est, "no DHW baseline recovered"
# The "summer" selection band (dt < 3K) spans a range of outdoor temps over
# which the mains-temperature model varies, and still lets a little real
# heating leak in (dt up to just under 3K, not exactly 0) - both genuine
# properties of dhw_baseline's dT-threshold approximation, not test quirks.
# So the expected median isn't simply DHW_SUMMER_KWH_TRUE + HOB_PILOT_TRUE;
# compute it from the same (noise-free) model components actually summed
# into q_daily2 for those days.
expected_summer_median = float(
    np.median(HOB_PILOT_TRUE + dhw_true[summer_mask] + heating_true[summer_mask])
)
assert abs(baseline_est["kwh_per_day"] - expected_summer_median) < 3, baseline_est
print(f"dhw baseline ok: {baseline_est['kwh_per_day']:.1f} kWh/day "
      f"(expected ~{expected_summer_median:.1f} from the model), {baseline_est['days_used']} days")

raw_fit = hlc.fit_hlc(q_daily2, dt_daily2)
assert "note" not in raw_fit, raw_fit
raw_error = abs(raw_fit["hlc_w_per_k"] - FABRIC_HLC_TRUE)

corrected_fit = dhw.corrected_hlc(q_daily2, dt_daily2, outdoor_daily)
assert corrected_fit, "no DHW-corrected HLC fit"
corrected_error = abs(corrected_fit["hlc_w_per_k"] - FABRIC_HLC_TRUE)

assert raw_error > 15, f"expected the uncorrected fit visibly biased by the synthetic DHW signal, got {raw_error:.1f} W/K off"
assert corrected_error < 10, f"expected the DHW correction to recover the true fabric HLC, got {corrected_error:.1f} W/K off"
assert corrected_error < raw_error, "DHW correction should reduce, not increase, the HLC bias"
print(f"dhw correction ok: raw HLC {raw_fit['hlc_w_per_k']:.0f} W/K (off by {raw_error:.0f}) "
      f"-> corrected {corrected_fit['hlc_w_per_k']:.0f} W/K (off by {corrected_error:.0f}), "
      f"true fabric HLC {FABRIC_HLC_TRUE:.0f} W/K")

# --- fit_water_gas: hourly gas-vs-water regression recovers a known Wh/L rate ---
WH_PER_L_TRUE, HOB_PILOT_HOURLY_TRUE = 18.0, 0.1
water_idx = pd.date_range("2026-02-01", periods=24 * 40, freq="1h", tz="UTC")
rng3 = np.random.default_rng(13)
water_hourly_true = np.clip(rng3.exponential(3.0, len(water_idx)) - 2.0, 0, None)
gas_hourly_true = (
    HOB_PILOT_HOURLY_TRUE + WH_PER_L_TRUE / 1000 * water_hourly_true
    + rng3.normal(0, 0.02, len(water_idx))
).clip(min=0)
gas_cum = pd.Series(gas_hourly_true, index=water_idx).cumsum() + 500.0
water_cum = pd.Series(water_hourly_true, index=water_idx).cumsum() + 1000.0

gas_hourly = dhw.hourly_change(gas_cum, dhw.GAS_MAX_STEP_KWH)
water_hourly = dhw.hourly_change(water_cum, dhw.WATER_MAX_STEP_L)
water_fit = dhw.fit_water_gas(gas_hourly, water_hourly)
assert water_fit, "no water regression fit"
assert abs(water_fit["wh_per_litre"] - WH_PER_L_TRUE) / WH_PER_L_TRUE < 0.15, water_fit
print(f"fit_water_gas ok: {water_fit['wh_per_litre']:.1f} Wh/L (true {WH_PER_L_TRUE}), "
      f"R² {water_fit['regression_r_squared']:.2f}, {water_fit['regression_hours']} hours")

print("\nall checks passed")
