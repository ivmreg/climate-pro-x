"""Non-heating gas (hot water, plus cooking/pilot only if those burn gas): a
robust summer baseline, scaled by a coarse mains-water-temperature model to
correct the winter HLC fit for DHW.

Mirrors the dhw_baseline/mains_temp_c/dhw_daily_kwh/fit_water_gas cluster in
the HA integration (custom_components/thermal_efficiency/thermal_math.py) -
this pandas version is for offline exploration/CLI use and cross-validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DHW_BASELINE_MAX_DT = 3.0
DHW_BASELINE_MIN_DAYS = 14
DHW_OCCUPIED_MIN_WATER_L = 50.0
DHW_WATER_MIN_DAYS = 10
DHW_RATE_MIN_WH_PER_L_PER_K = 0.05
DHW_RATE_MAX_WH_PER_L_PER_K = 1.5
DHW_REGRESSION_MIN_HOURS = 200
DHW_REGRESSION_MIN_R2 = 0.5
DHW_THEORETICAL_WH_PER_L = 34.8  # Wh to raise 1L by 30K, a combi's typical DHW rise
MAINS_TANK_TEMP_C = 55.0
MAINS_TEMP_MIN_C = 4.0
MAINS_TEMP_MAX_C = 16.0
MAINS_OUTDOOR_MIN_C = 2.0
MAINS_OUTDOOR_MAX_C = 18.0
GAS_MAX_STEP_KWH = 40.0
WATER_MAX_STEP_L = 1000.0
MAX_METER_GAP = pd.Timedelta("1.5h")


def hourly_change(cumulative: pd.Series, max_step: float) -> pd.Series:
    """Hourly deltas from a cumulative statistics series, dropping resets
    (negative steps) and implausible artifacts (> max_step)."""
    cumulative = cumulative.sort_index()
    diffs = cumulative.diff()
    gaps = cumulative.index.to_series().diff()
    valid = (
        (gaps > pd.Timedelta(0))
        & (gaps <= MAX_METER_GAP)
        & (diffs >= 0)
        & (diffs <= max_step)
    )
    return diffs.where(valid).dropna()


def dhw_baseline(
    q_daily: pd.Series, dt_daily: pd.Series, outdoor_daily: pd.Series
) -> dict | None:
    """Robust non-heating gas estimate: median daily gas on days
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
    """Scale the measured summer non-heating baseline for a colder mains
    supply on another day. See thermal_math.dhw_daily_kwh for the rationale
    behind applying the ratio to the whole baseline (exact when the home
    cooks on electricity and the baseline is pure DHW)."""
    ref_dt = MAINS_TANK_TEMP_C - mains_temp_c(baseline["outdoor_mean"])
    day_dt = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_c)
    ratio = day_dt / ref_dt if ref_dt > 0 else 1.0
    return baseline["kwh_per_day"] * ratio


def fit_dhw_water_rate(
    q_daily: pd.Series,
    water_daily: pd.Series,
    outdoor_daily: pd.Series,
    heating_off: set,
    min_water_l: float = DHW_OCCUPIED_MIN_WATER_L,
) -> dict | None:
    """Mirror of thermal_math.fit_dhw_water_rate: daily gas-per-litre-per-K
    rate from heating-off days with enough metered water that someone was
    home (all gas on such days is hot water in an electric-cooking combi
    home). The median rate predicts DHW gas on heating days from actual
    litres instead of a constant mains-scaled baseline."""
    df = pd.DataFrame(
        {"q": q_daily, "water": water_daily, "outdoor": outdoor_daily}
    ).dropna()
    df = df[df.index.isin(list(heating_off)) & (df.water >= min_water_l) & (df.q > 0)]
    rise = MAINS_TANK_TEMP_C - df.outdoor.map(mains_temp_c)
    df, rise = df[rise > 0], rise[rise > 0]
    if len(df) < DHW_WATER_MIN_DAYS:
        return None
    rates = df.q * 1000 / df.water / rise
    rate = float(rates.median())
    if not DHW_RATE_MIN_WH_PER_L_PER_K <= rate <= DHW_RATE_MAX_WH_PER_L_PER_K:
        return None
    return {"wh_per_litre_per_k": rate, "days_used": int(len(rates))}


def dhw_kwh_from_water(litres: float, outdoor_c: float, water_rate: dict) -> float:
    """DHW gas for a day, from its metered litres and the fitted daily rate."""
    rise = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_c)
    return litres * water_rate["wh_per_litre_per_k"] * rise / 1000


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
    if df.empty:
        return None
    dhw_kwh = df.outdoor.apply(lambda t: dhw_daily_kwh(t, baseline))
    q_adjusted = df.q - dhw_kwh
    valid = q_adjusted > 0
    if not valid.any():
        return None
    from . import hlc

    fitted = hlc.fit_hlc(q_adjusted[valid], df.loc[valid, "dt"])
    if "note" in fitted:
        return None
    fitted["baseline"] = baseline
    return fitted


def fit_water_gas(
    gas_kwh_hourly: pd.Series,
    water_l_hourly: pd.Series,
    boiler_efficiency: float = 0.88,
) -> dict | None:
    """Informational only: hourly gas-vs-water regression, giving a rough
    Wh-per-litre rate and the implied hot fraction of metered water. Noisy
    (household draws are bursty and mix hot/cold) - not the basis for the
    DHW cost figure, which comes from dhw_baseline instead."""
    df = pd.concat(
        [gas_kwh_hourly.rename("gas"), water_l_hourly.rename("water")], axis=1
    ).dropna()
    if len(df) < DHW_REGRESSION_MIN_HOURS or df.water.nunique() < 10:
        return None
    slope, intercept = np.polyfit(df.water, df.gas, 1)
    if slope <= 0:
        return None
    pred = slope * df.water + intercept
    ss_res = float(np.sum((df.gas - pred) ** 2))
    ss_tot = float(np.sum((df.gas - df.gas.mean()) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if r_squared < DHW_REGRESSION_MIN_R2:
        return None
    wh_per_litre = slope * 1000
    return {
        "wh_per_litre": wh_per_litre,
        "fuel_input_wh_per_litre": wh_per_litre,
        "hot_fraction_pct": min(
            100.0,
            wh_per_litre * boiler_efficiency / DHW_THEORETICAL_WH_PER_L * 100,
        ),
        "regression_r_squared": r_squared,
        "regression_hours": len(df),
    }
