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

from .quality import is_valid_percentage, is_valid_temperature

MIN_WINDOW_HOURS = 3.0
MIN_DELTA_T = 3.0  # K between room and outdoor; below this the fit is noise
MIN_DROP = 0.3  # room must actually cool by this much (degC)
MAX_HEATING_PCT = 1.0  # tado heating power must stay <= this during window
MIN_NIGHTS = 3
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
    if room.empty or outdoor.empty or len(room) < 2 or len(outdoor) < 2:
        return None

    # Align on combined index preserving all timestamps; do not dropna and bridge
    room_sorted = room.sort_index()
    outdoor_sorted = outdoor.sort_index()
    combined_index = room_sorted.index.union(outdoor_sorted.index)

    r_series = pd.to_numeric(room_sorted.reindex(combined_index), errors="coerce")
    o_series = pd.to_numeric(outdoor_sorted.reindex(combined_index), errors="coerce")

    # Role-aware rejection: -90..80 C
    r_valid = r_series.apply(is_valid_temperature)
    o_valid = o_series.apply(is_valid_temperature)
    valid_row = r_valid & o_valid

    # Partition into contiguous valid segments using invalid timestamps and gaps as barriers
    segments: list[pd.DataFrame] = []
    current_rows: list[dict] = []
    prev_ts: pd.Timestamp | None = None

    for ts, is_valid in valid_row.items():
        if not is_valid:
            # Barrier! Closes current segment without bridging
            if current_rows:
                segments.append(pd.DataFrame(current_rows).set_index("ts"))
                current_rows = []
            prev_ts = None
            continue

        if prev_ts is not None:
            gap = ts - prev_ts
            if gap <= pd.Timedelta(0) or gap > pd.Timedelta("10min"):
                # Temporal gap exceeded: close segment
                if current_rows:
                    segments.append(pd.DataFrame(current_rows).set_index("ts"))
                    current_rows = []

        current_rows.append({"ts": ts, "room": float(r_series[ts]), "outdoor": float(o_series[ts])})
        prev_ts = ts

    if current_rows:
        segments.append(pd.DataFrame(current_rows).set_index("ts"))

    candidate_fits: list[tuple[float, float, NightFit]] = []
    for seg in segments:
        if len(seg) < 2:
            continue
        seg_room = seg["room"]
        seg_outdoor = seg["outdoor"]
        hours = (seg_room.index[-1] - seg_room.index[0]).total_seconds() / 3600
        if hours < MIN_WINDOW_HOURS:
            continue
        excess = seg_room - seg_outdoor
        if excess.min() < MIN_DELTA_T:
            continue
        if seg_room.iloc[0] - seg_room.iloc[-1] < MIN_DROP:
            continue

        elapsed = seg_room.index.to_series().diff().dt.total_seconds().div(3600).to_numpy()
        room_values = seg_room.to_numpy(dtype=float)
        outdoor_values = seg_outdoor.to_numpy(dtype=float)
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
            continue
        ss_res = best_residual
        ss_tot = float(np.sum((room_values - room_values.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        if r2 < 0.8:
            continue

        candidate_fits.append((
            hours,
            r2,
            NightFit(
                date=str(seg_room.index[0].date()),
                tau_hours=best_tau,
                r_squared=r2,
                t_start=float(seg_room.iloc[0]),
                t_end=float(seg_room.iloc[-1]),
                outdoor_mean=float(seg_outdoor.mean()),
            )
        ))

    if not candidate_fits:
        return None
    # Longest qualifying valid segment, tie-breaking on R^2
    candidate_fits.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidate_fits[0][2]


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
            h = heating[start:end].reindex(window.index)
            h_valid = h.apply(is_valid_percentage)
            h_clean = h[h_valid].astype(float)
            if not h_valid.all() or h_clean.empty or h_clean.max() > MAX_HEATING_PCT:
                continue  # heating ran during the window or insufficient valid heating data
        fit = fit_window(window, outdoor[start:end])
        if fit:
            fits.append(fit)
    return fits


def summarise(fits_by_room: dict[str, list[NightFit]]) -> pd.DataFrame:
    rows = []
    for room, fits in fits_by_room.items():
        taus = [f.tau_hours for f in fits]
        publishable = len(taus) >= MIN_NIGHTS
        rows.append(
            {
                "room": room,
                "nights_fitted": len(fits),
                "tau_median_h": float(np.median(taus)) if publishable else float("nan"),
                "tau_min_h": min(taus) if publishable else float("nan"),
                "tau_max_h": max(taus) if publishable else float("nan"),
            }
        )
    return pd.DataFrame(rows).sort_values("tau_median_h")
