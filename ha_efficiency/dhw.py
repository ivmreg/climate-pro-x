"""Hot-water (+hob/pilot) gas: a robust summer baseline, scaled by a coarse
mains-water-temperature model to correct the winter HLC fit for DHW.

Mirrors the dhw_baseline/mains_temp_c/dhw_daily_kwh/fit_water_gas cluster in
the HA integration (custom_components/thermal_efficiency/thermal_math.py) -
this pandas version is for offline exploration/CLI use and cross-validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DHW_BASELINE_MAX_DT = 3.0
DHW_BASELINE_MIN_DAYS = 7
DHW_REGRESSION_MIN_HOURS = 200
DHW_THEORETICAL_WH_PER_L = 34.8  # Wh to raise 1L by 30K, a combi's typical DHW rise
MAINS_TANK_TEMP_C = 55.0
MAINS_TEMP_MIN_C = 4.0
MAINS_TEMP_MAX_C = 16.0
MAINS_OUTDOOR_MIN_C = 2.0
MAINS_OUTDOOR_MAX_C = 18.0
GAS_MAX_STEP_KWH = 40.0
WATER_MAX_STEP_L = 1000.0


def hourly_change(cumulative: pd.Series, max_step: float) -> pd.Series:
    """Hourly deltas from a cumulative statistics series, dropping resets
    (negative steps) and implausible artifacts (> max_step)."""
    diffs = cumulative.sort_index().diff()
    diffs[(diffs < 0) | (diffs > max_step)] = np.nan
    return diffs.dropna()


def dhw_baseline(
    q_daily: pd.Series, dt_daily: pd.Series, outdoor_daily: pd.Series
) -> dict | None:
    """Robust hot-water+hob+pilot gas estimate: median daily gas on days
    with negligible heating demand (mean dT < DHW_BASELINE_MAX_DT), plus the
    mean outdoor temperature on those days (the mains-water-temperature
    reference point `dhw_daily_kwh` scales from)."""
    df = pd.DataFrame(
        {"q": q_daily, "dt": dt_daily, "outdoor": outdoor_daily}
    ).dropna()
    df = df[df.dt < DHW_BASELINE_MAX_DT]
    if len(df) < DHW_BASELINE_MIN_DAYS:
        return None
    return {
        "kwh_per_day": float(df.q.median()),
        "outdoor_mean": float(df.outdoor.mean()),
        "days_used": len(df),
    }


def mains_temp_c(outdoor_c: float) -> float:
    """Coarse UK mains-water-temperature model (cold in winter, warmer in
    summer, tracking outdoor air with damping). Only used as a scaling ratio
    between the summer DHW baseline and other days, not as an absolute
    physical claim."""
    if outdoor_c <= MAINS_OUTDOOR_MIN_C:
        return MAINS_TEMP_MIN_C
    if outdoor_c >= MAINS_OUTDOOR_MAX_C:
        return MAINS_TEMP_MAX_C
    frac = (outdoor_c - MAINS_OUTDOOR_MIN_C) / (MAINS_OUTDOOR_MAX_C - MAINS_OUTDOOR_MIN_C)
    return MAINS_TEMP_MIN_C + frac * (MAINS_TEMP_MAX_C - MAINS_TEMP_MIN_C)


def dhw_daily_kwh(outdoor_c: float, baseline: dict) -> float:
    """Scale the measured summer DHW+hob+pilot baseline for a colder mains
    supply on another day. See thermal_math.dhw_daily_kwh for the rationale
    behind applying the ratio to the whole baseline rather than splitting
    out hob/pilot."""
    ref_dt = MAINS_TANK_TEMP_C - mains_temp_c(baseline["outdoor_mean"])
    day_dt = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_c)
    ratio = day_dt / ref_dt if ref_dt > 0 else 1.0
    return baseline["kwh_per_day"] * ratio


def corrected_hlc(
    q_daily: pd.Series, dt_daily: pd.Series, outdoor_daily: pd.Series
) -> dict | None:
    """Re-fit HLC after subtracting the modelled DHW gas from each day's gas
    input - filtering on the raw (pre-subtraction) daily totals, matching
    thermal_math.fit_hlc's dhw_by_day semantics in the live integration."""
    baseline = dhw_baseline(q_daily, dt_daily, outdoor_daily)
    if not baseline:
        return None
    df = pd.DataFrame(
        {"q": q_daily, "dt": dt_daily, "outdoor": outdoor_daily}
    ).dropna()
    df = df[(df.dt > 4) & (df.q > 0.5)]
    if len(df) < 5:
        return None
    dhw_kwh = df.outdoor.apply(lambda t: dhw_daily_kwh(t, baseline))
    q_adjusted = df.q - dhw_kwh
    slope, intercept = np.polyfit(df.dt, q_adjusted, 1)
    pred = slope * df.dt + intercept
    ss_res = float(np.sum((q_adjusted - pred) ** 2))
    ss_tot = float(np.sum((q_adjusted - q_adjusted.mean()) ** 2))
    return {
        "days": len(df),
        "hlc_w_per_k": slope * 1000 / 24,
        "r_squared": 1 - ss_res / ss_tot if ss_tot > 0 else 0.0,
        "baseline": baseline,
    }


def fit_water_gas(gas_kwh_hourly: pd.Series, water_l_hourly: pd.Series) -> dict | None:
    """Informational only: hourly gas-vs-water regression, giving a rough
    Wh-per-litre rate and the implied hot fraction of metered water. Noisy
    (household draws are bursty and mix hot/cold) - not the basis for the
    DHW cost figure, which comes from dhw_baseline instead."""
    df = pd.concat(
        [gas_kwh_hourly.rename("gas"), water_l_hourly.rename("water")], axis=1
    ).dropna()
    if len(df) < DHW_REGRESSION_MIN_HOURS:
        return None
    slope, intercept = np.polyfit(df.water, df.gas, 1)
    if slope <= 0:
        return None
    pred = slope * df.water + intercept
    ss_res = float(np.sum((df.gas - pred) ** 2))
    ss_tot = float(np.sum((df.gas - df.gas.mean()) ** 2))
    wh_per_litre = slope * 1000
    return {
        "wh_per_litre": wh_per_litre,
        "hot_fraction_pct": min(100.0, wh_per_litre / DHW_THEORETICAL_WH_PER_L * 100),
        "regression_r_squared": 1 - ss_res / ss_tot if ss_tot > 0 else 0.0,
        "regression_hours": len(df),
    }
