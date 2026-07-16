"""Per-room thermal time constants from overnight cooling curves.

Model: with heating off, a room relaxes towards outdoor temperature as
    T(t) = T_out + (T0 - T_out) * exp(-t / tau)
so ln(T - T_out) is linear in t with slope -1/tau. We fit that per night per
room and report the median tau. Bigger tau = slower cooling = better retained
heat (mass + insulation + airtightness combined).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_WINDOW_HOURS = 3.0
MIN_DELTA_T = 3.0  # K between room and outdoor; below this the fit is noise
MIN_DROP = 0.3  # room must actually cool by this much (degC)
MAX_HEATING_PCT = 1.0  # tado heating power must stay <= this during window
MIN_TAU_HOURS = 1.0
MAX_TAU_HOURS = 200.0  # search bound; a fit landing here is censored, not measured
TAU_SEARCH_STEP_H = 0.25


@dataclass
class NightFit:
    date: str
    tau_hours: float
    r_squared: float
    t_start: float
    t_end: float
    outdoor_mean: float


def night_windows(index: pd.DatetimeIndex, night_start: str, night_end: str):
    """Yield (start, end) timestamps of each night window in the data range."""
    if len(index) == 0:
        return
    tz = index.tz
    for day in pd.date_range(index[0].floor("D"), index[-1].ceil("D"), tz=tz):
        start = day + pd.Timedelta(night_start + ":00")
        end = day + pd.Timedelta("1D") + pd.Timedelta(night_end + ":00") \
            if night_end < night_start else day + pd.Timedelta(night_end + ":00")
        if start >= index[0] and end <= index[-1]:
            yield start, end


def fit_window(room: pd.Series, outdoor: pd.Series) -> NightFit | None:
    aligned = pd.concat([room.rename("room"), outdoor.rename("outdoor")], axis=1).dropna()
    if aligned.empty:
        return None
    room, outdoor = aligned.room, aligned.outdoor
    gaps = room.index.to_series().diff().dropna()
    if not gaps.empty and gaps.max() > pd.Timedelta("10min"):
        return None
    hours = (room.index[-1] - room.index[0]).total_seconds() / 3600
    if hours < MIN_WINDOW_HOURS:
        return None
    excess = room - outdoor
    if excess.min() < MIN_DELTA_T:
        return None
    if room.iloc[0] - room.iloc[-1] < MIN_DROP:
        return None  # not cooling (heating on, or fully insulated night)

    elapsed = room.index.to_series().diff().dt.total_seconds().div(3600).to_numpy()
    room_values = room.to_numpy(dtype=float)
    outdoor_values = outdoor.to_numpy(dtype=float)
    # The boundary temperature of each step does not depend on tau, so build it
    # once rather than inside every candidate-tau iteration.
    boundaries = (outdoor_values[:-1] + outdoor_values[1:]) / 2
    best_tau = None
    best_residual = float("inf")
    for tau in np.arange(MIN_TAU_HOURS, MAX_TAU_HOURS + TAU_SEARCH_STEP_H, TAU_SEARCH_STEP_H):
        predicted = np.empty(len(room_values))
        predicted[0] = room_values[0]
        decay = np.exp(-elapsed[1:] / tau)
        for i in range(1, len(room_values)):
            predicted[i] = boundaries[i - 1] + (
                predicted[i - 1] - boundaries[i - 1]
            ) * decay[i - 1]
        residual = float(np.sum((room_values - predicted) ** 2))
        if residual < best_residual:
            best_tau, best_residual = float(tau), residual
    if best_tau is None or best_tau >= MAX_TAU_HOURS:
        return None  # pinned at the search bound: a lower bound, not a measurement
    ss_res = best_residual
    ss_tot = float(np.sum((room_values - room_values.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if r2 < 0.8:
        return None  # non-exponential (door opened, sun, heating blip)

    return NightFit(
        date=str(room.index[0].date()),
        tau_hours=best_tau,
        r_squared=r2,
        t_start=float(room.iloc[0]),
        t_end=float(room.iloc[-1]),
        outdoor_mean=float(outdoor.mean()),
    )


def analyse_room(
    room: pd.Series,
    outdoor: pd.Series,
    heating: pd.Series | None,
    night_start: str,
    night_end: str,
) -> list[NightFit]:
    fits = []
    for start, end in night_windows(room.index, night_start, night_end):
        window = room[start:end]
        if heating is not None:
            h = heating[start:end].dropna()
            if len(h) / max(len(window), 1) < 0.8 or h.max() > MAX_HEATING_PCT:
                continue  # heating ran during the window — not a free cooldown
        fit = fit_window(window, outdoor[start:end])
        if fit:
            fits.append(fit)
    return fits


def summarise(fits_by_room: dict[str, list[NightFit]]) -> pd.DataFrame:
    rows = []
    for room, fits in fits_by_room.items():
        taus = [f.tau_hours for f in fits]
        rows.append(
            {
                "room": room,
                "nights_fitted": len(fits),
                "tau_median_h": float(np.median(taus)) if taus else float("nan"),
                "tau_min_h": min(taus) if taus else float("nan"),
                "tau_max_h": max(taus) if taus else float("nan"),
            }
        )
    return pd.DataFrame(rows).sort_values("tau_median_h")
