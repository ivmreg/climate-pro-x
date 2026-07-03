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
    diffs = gas_kwh.diff()
    diffs[(diffs < 0) | (diffs > max_step_kwh)] = 0.0
    return diffs.resample("1D").sum()


def daily_delta_t(
    indoor_by_room: dict[str, pd.Series], outdoor: pd.Series
) -> pd.Series:
    indoor_mean = pd.DataFrame(indoor_by_room).mean(axis=1)
    dt = indoor_mean - outdoor.reindex(indoor_mean.index).interpolate(limit=24)
    return dt.resample("1D").mean()


def fit_hlc(q_daily: pd.Series, dt_daily: pd.Series) -> dict:
    df = pd.DataFrame({"q": q_daily, "dt": dt_daily}).dropna()
    # Heating-season days only: meaningful dT and some heat actually delivered
    df = df[(df.dt > 4) & (df.q > 0.5)]
    if len(df) < 5:
        return {"days": len(df), "hlc_w_per_k": float("nan"),
                "note": "Fewer than 5 usable heating days — pull more history "
                        "or wait for colder weather."}
    slope, intercept = np.polyfit(df.dt, df.q, 1)
    pred = slope * df.dt + intercept
    ss_res = float(np.sum((df.q - pred) ** 2))
    ss_tot = float(np.sum((df.q - df.q.mean()) ** 2))
    return {
        "days": len(df),
        "hlc_w_per_k": slope * 1000 / 24,
        "free_gains_kwh_per_day": -intercept,
        "r_squared": 1 - ss_res / ss_tot if ss_tot > 0 else 0.0,
        "data": df,
    }


def benchmark(hlc: float) -> str:
    if np.isnan(hlc):
        return "insufficient data"
    bands = [
        (100, "excellent — like a modern insulated build"),
        (180, "good — better than most solid-wall homes"),
        (280, "typical for an unimproved solid-brick flat"),
        (400, "poor — significant losses; check draughts, glazing, loft"),
    ]
    for limit, label in bands:
        if hlc < limit:
            return label
    return "very poor — worth a professional survey"
