"""Whole-home Heat Loss Coefficient (W/K) by daily energy balance.

For each day: heat input Q (kWh) vs mean indoor-outdoor difference dT.
In steady state Q ~ HLC * dT * 24h - gains, so a linear regression of daily Q
on daily dT has slope HLC (in kWh/day/K -> x1000/24 = W/K). The negative
x-intercept reflects free gains (sun, people, appliances).

Heat input source, in order of preference:
  1. gas_kwh_entity — a real cumulative/daily gas energy sensor
  2. Tado heating power %% averaged across zones x boiler_output_kw
     (a proxy: assumes output scales with demand %%; good for trends,
      +-20-30%% on absolute level)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_HLC_DAYS = 20
MIN_HLC_R2 = 0.5
MIN_DT_SPREAD = 3.0
MIN_HLC_W_PER_K = 10.0
MAX_HLC_W_PER_K = 1500.0
MAX_METER_GAP = pd.Timedelta("1.5h")
MIN_DAILY_COVERAGE = 0.9
# Consecutive days share a weather system, so daily regression residuals are
# serially correlated and the naive OLS standard error understates the real
# uncertainty. Mirrors thermal_math's AR(1) effective-sample-size correction.
AC_MIN_PAIRS = 10
AC_MAX_R1 = 0.9


def _lag1_autocorrelation(days: pd.DatetimeIndex, residuals: np.ndarray) -> float:
    """Lag-1 autocorrelation over adjacent calendar days only: the days that
    survive the fit's filters are not contiguous, and a week-long gap carries
    no AR(1) information."""
    adjacent = np.diff(days.to_numpy()) == np.timedelta64(1, "D")
    if adjacent.sum() < AC_MIN_PAIRS:
        return 0.0
    centred = residuals - residuals.mean()
    variance = float(np.mean(centred**2))
    if variance <= 0:
        return 0.0
    covariance = float(np.mean((centred[:-1] * centred[1:])[adjacent]))
    return covariance / variance


def _variance_inflation(r1: float) -> float:
    """AR(1) variance inflation for a slope standard error. Negative r1 is not
    credited: claiming more precision than the independent-sample case on this
    evidence is not a trade worth making."""
    return (1 + min(max(r1, 0.0), AC_MAX_R1)) / (1 - min(max(r1, 0.0), AC_MAX_R1))


def _expected_day_hours(day: pd.Timestamp) -> int:
    """Return the real number of hours in a local day, including DST."""
    start = day.normalize()
    end = start + pd.DateOffset(days=1)
    return round((end.tz_convert("UTC") - start.tz_convert("UTC")).total_seconds() / 3600)


def daily_heat_input_from_tado(
    heating_by_room: dict[str, pd.Series], boiler_output_kw: float
) -> pd.Series:
    """kWh/day estimated from mean Tado heating power across zones."""
    df = pd.DataFrame(heating_by_room)
    mean_pct = df.mean(axis=1)  # simple mean across zones
    return (mean_pct / 100.0 * boiler_output_kw).resample("1D").mean() * 24


def daily_heat_input_from_meter(
    gas_kwh: pd.Series, max_step_kwh: float = 40.0
) -> pd.Series:
    """kWh/day from a cumulative gas energy sensor.

    Negative steps (meter resets) and implausibly large positive steps
    (statistics-baseline offsets when LTS and recorder data interleave; a
    domestic boiler can't burn more than ~max_step_kwh between readings)
    are treated as artifacts, not consumption.
    """
    gas_kwh = gas_kwh.sort_index()
    diffs = gas_kwh.diff()
    gaps = gas_kwh.index.to_series().diff()
    valid = (
        (gaps > pd.Timedelta(0))
        & (gaps <= MAX_METER_GAP)
        & (diffs >= 0)
        & (diffs <= max_step_kwh)
    )
    diffs = diffs.where(valid)
    totals = diffs.resample("1D").agg(["sum", "count"])
    complete = [
        count >= (_expected_day_hours(day) - 1) * MIN_DAILY_COVERAGE
        for day, count in totals["count"].items()
    ]
    result = totals.loc[complete, "sum"]
    result.name = gas_kwh.name
    return result


def daily_delta_t(
    indoor_by_room: dict[str, pd.Series], outdoor: pd.Series
) -> pd.Series:
    if not indoor_by_room:
        return pd.Series(dtype=float)
    rooms = pd.DataFrame(indoor_by_room).dropna(how="any")
    indoor_mean = rooms.mean(axis=1)
    aligned_outdoor = outdoor.reindex(indoor_mean.index).interpolate(limit=1)
    dt = (indoor_mean - aligned_outdoor).dropna()
    daily = dt.resample("1D").agg(["mean", "count"])
    gaps = dt.index.to_series().diff().dropna()
    cadence = gaps[gaps > pd.Timedelta(0)].median() if not gaps.empty else pd.Timedelta("1h")
    complete = [
        count >= _expected_day_hours(day) * pd.Timedelta("1h") / cadence * MIN_DAILY_COVERAGE
        for day, count in daily["count"].items()
    ]
    return daily.loc[complete, "mean"]


def fit_hlc(q_daily: pd.Series, dt_daily: pd.Series) -> dict:
    df = pd.DataFrame({"q": q_daily, "dt": dt_daily}).dropna()
    # Heating-season days only: meaningful dT and some heat actually delivered
    df = df[(df.dt > 4) & (df.q > 0.5)]
    if len(df) < MIN_HLC_DAYS:
        return {"days": len(df), "hlc_w_per_k": float("nan"),
                "note": f"Fewer than {MIN_HLC_DAYS} usable heating days — pull more history "
                        "or wait for colder weather."}
    if df.dt.max() - df.dt.min() < MIN_DT_SPREAD:
        return {"days": len(df), "hlc_w_per_k": float("nan"),
                "note": "Usable days do not span enough indoor-outdoor temperature variation."}
    slope, intercept = np.polyfit(df.dt, df.q, 1)
    pred = slope * df.dt + intercept
    residuals = (df.q - pred).to_numpy(dtype=float)
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((df.q - df.q.mean()) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    hlc_w_per_k = slope * 1000 / 24
    sxx = float(np.sum((df.dt - df.dt.mean()) ** 2))
    slope_se = float(np.sqrt(ss_res / (len(df) - 2) / sxx)) if sxx > 0 else float("inf")
    # Widen for serially correlated residuals: the fit has fewer independent
    # observations than it has days.
    r1 = _lag1_autocorrelation(df.index, residuals)
    vif = _variance_inflation(r1)
    slope_se *= np.sqrt(vif)
    ci_low = (slope - 1.96 * slope_se) * 1000 / 24
    ci_high = (slope + 1.96 * slope_se) * 1000 / 24
    if (
        slope <= 0
        or r_squared < MIN_HLC_R2
        or ci_low <= 0
        or not MIN_HLC_W_PER_K <= hlc_w_per_k <= MAX_HLC_W_PER_K
    ):
        return {"days": len(df), "hlc_w_per_k": float("nan"),
                "r_squared": r_squared,
                "note": "HLC fit failed physical or statistical quality gates."}
    return {
        "days": len(df),
        "hlc_w_per_k": hlc_w_per_k,
        "hlc_ci_low_w_per_k": ci_low,
        "hlc_ci_high_w_per_k": ci_high,
        "free_gains_kwh_per_day": -intercept,
        "regression_intercept_kwh_per_day": intercept,
        "r_squared": r_squared,
        "residual_autocorrelation": r1,
        "effective_independent_days": len(df) / vif,
        "status": "valid" if len(df) >= 30 else "provisional",
        "data": df,
    }


def benchmark(hlc: float) -> str:
    if np.isnan(hlc):
        return "insufficient data"
    return "not benchmarked — compare qualified trends or use a building-specific standard"
