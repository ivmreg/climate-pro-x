"""Pure-Python thermal analysis on hourly long-term-statistics rows.

No numpy/pandas so the integration needs no pip requirements. Series are
plain dicts of {unix_hour_start_seconds: value}; all date/hour bucketing is
done in the home's local timezone (passed in, keeping these functions pure).

Models (validated offline against a full heating season in this repo):
  HLC:    daily gas kWh ~ HLC * daily mean (T_in - T_out); slope in
          kWh/day/K -> x1000/24 = W/K (gas-input side, includes boiler eff).
  tau:    overnight free cooling T(t) = T_out + (T0-T_out) e^(-t/tau);
          log-linear fit per night, median over nights.
  loft:   r = (T_loft-T_out)/(T_in-T_out) on cold nights = share of the
          ceiling+roof resistance that sits in the roof.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, tzinfo
from math import log
from statistics import median

Series = dict[int, float]

GAS_MAX_STEP_KWH = 40.0  # bigger hourly steps are meter/statistics artifacts
HLC_MIN_DT = 4.0
HLC_MIN_Q = 0.5
HLC_MIN_DAYS = 30  # for the shorter-window "recent" estimate
HLC_FALLBACK_MIN_DAYS = 5
DHW_BASELINE_MAX_DT = 3.0
TAU_NIGHT_HOURS = range(0, 6)  # local hour starts used for the night fit
TAU_MIN_POINTS = 5
TAU_MIN_DT = 3.0
TAU_MIN_DROP = 0.3
TAU_MIN_R2 = 0.8
TAU_MAX_HEATING_PCT = 1.0
TAU_MIN_NIGHTS = 3
LOFT_NIGHT_HOURS = range(1, 6)
LOFT_MIN_DT = 6.0
LOFT_MIN_HOURS = 12


def series_from_stats(rows: list[dict], kind: str) -> Series:
    """Convert recorder statistics rows to {epoch_seconds: value}."""
    out: Series = {}
    for row in rows:
        value = row.get(kind)
        if value is None:
            continue
        start = row["start"]
        if isinstance(start, datetime):
            ts = start.timestamp()
        else:
            ts = float(start)
            if ts > 1e12:  # milliseconds (websocket-style payloads)
                ts /= 1000.0
        out[int(ts)] = float(value)
    return out


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares -> (slope, intercept, r_squared)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


def _local(ts: int, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def drop_flatlines(series: Series, window: int = 24, eps: float = 0.05) -> Series:
    """Drop hours where the value hasn't moved for `window` hours.

    A dead battery sensor keeps its last state, and recorder statistics keep
    emitting that frozen value hourly — which poisons ratio analyses. A real
    temperature never sits within eps for a full day.
    """
    out: Series = {}
    recent: deque[float] = deque(maxlen=window)
    for ts, value in sorted(series.items()):
        recent.append(value)
        if len(recent) == window and max(recent) - min(recent) < eps:
            continue
        out[ts] = value
    return out


def daily_gas_kwh(gas_sum: Series, tz: tzinfo) -> dict:
    """Local-date daily kWh from a cumulative statistics 'sum' series."""
    days: dict = defaultdict(float)
    items = sorted(gas_sum.items())
    for (_, v0), (t1, v1) in zip(items, items[1:]):
        step = v1 - v0
        if 0 < step <= GAS_MAX_STEP_KWH:
            days[_local(t1, tz).date()] += step
    return dict(days)


def daily_delta_t(rooms: list[Series], outdoor: Series, tz: tzinfo) -> dict:
    """Local-date mean of (mean-room - outdoor), needing >=12 hours/day."""
    per_day: dict = defaultdict(list)
    for ts, t_out in outdoor.items():
        temps = [room[ts] for room in rooms if ts in room]
        if temps:
            per_day[_local(ts, tz).date()].append(sum(temps) / len(temps) - t_out)
    return {d: sum(v) / len(v) for d, v in per_day.items() if len(v) >= 12}


def fit_hlc(q_by_day: dict, dt_by_day: dict, since) -> dict | None:
    days = sorted(d for d in q_by_day if d in dt_by_day and d >= since)
    pairs = [
        (dt_by_day[d], q_by_day[d])
        for d in days
        if dt_by_day[d] > HLC_MIN_DT and q_by_day[d] > HLC_MIN_Q
    ]
    if len(pairs) < HLC_FALLBACK_MIN_DAYS:
        return None
    slope, intercept, r2 = linear_fit([p[0] for p in pairs], [p[1] for p in pairs])
    baseline_days = [
        q_by_day[d] for d in q_by_day
        if d >= since and d in dt_by_day and dt_by_day[d] < DHW_BASELINE_MAX_DT
    ]
    return {
        "hlc_w_per_k": slope * 1000 / 24,
        "r_squared": r2,
        "days_used": len(pairs),
        "free_gains_kwh_per_day": -intercept,
        "dhw_baseline_kwh_per_day": (
            median(baseline_days) if len(baseline_days) >= 7 else None
        ),
    }


def night_taus(
    room: Series, outdoor: Series, heating: Series | None, tz: tzinfo, since
) -> list[dict]:
    """One exponential-decay fit per usable night."""
    nights: dict = defaultdict(list)
    for ts in sorted(room):
        local = _local(ts, tz)
        if local.hour in TAU_NIGHT_HOURS and local.date() >= since:
            nights[local.date()].append(ts)

    fits = []
    for date, hours in nights.items():
        if len(hours) < TAU_MIN_POINTS:
            continue
        if heating:
            h_vals = [heating[ts] for ts in hours if ts in heating]
            # Filter only when the night is actually covered by heating data;
            # otherwise fall back to the quality gates below (r2, drop, dT).
            if len(h_vals) >= len(hours) // 2 and max(h_vals) > TAU_MAX_HEATING_PCT:
                continue
        outs = [outdoor[ts] for ts in hours if ts in outdoor]
        if len(outs) < TAU_MIN_POINTS:
            continue
        t_out = sum(outs) / len(outs)
        temps = [room[ts] for ts in hours]
        if min(temps) - t_out < TAU_MIN_DT:
            continue
        if temps[0] - temps[-1] < TAU_MIN_DROP:
            continue
        xs = [(ts - hours[0]) / 3600 for ts in hours]
        ys = [log(t - t_out) for t in temps]
        slope, _, r2 = linear_fit(xs, ys)
        if slope >= 0 or r2 < TAU_MIN_R2:
            continue
        fits.append({"date": str(date), "tau_hours": -1 / slope, "r_squared": r2})
    return fits


def loft_ratio(
    rooms: list[Series], loft: Series, outdoor: Series, tz: tzinfo, since
) -> dict | None:
    ratios = []
    for ts, t_loft in loft.items():
        local = _local(ts, tz)
        if local.hour not in LOFT_NIGHT_HOURS or local.date() < since:
            continue
        t_out = outdoor.get(ts)
        temps = [room[ts] for room in rooms if ts in room]
        if t_out is None or not temps:
            continue
        dt = sum(temps) / len(temps) - t_out
        if dt > LOFT_MIN_DT:
            ratios.append((t_loft - t_out) / dt)
    if len(ratios) < LOFT_MIN_HOURS:
        return None
    return {"ratio": median(ratios), "hours_used": len(ratios)}


def compute_all(
    stats: dict[str, list[dict]],
    conf: dict,
    tz: tzinfo,
    now: datetime,
    windows_days: tuple[int, ...],
) -> dict:
    """Run every analysis, widening the lookback window until data suffices."""
    room_confs = conf["rooms"]
    room_temp: dict[str, Series] = {}
    room_heat: dict[str, Series] = {}
    for name, spec in room_confs.items():
        room_temp[name] = series_from_stats(stats.get(spec["temperature"], []), "mean")
        if spec.get("heating_power"):
            room_heat[name] = series_from_stats(stats.get(spec["heating_power"], []), "mean")
    outdoor = series_from_stats(stats.get(conf["outdoor"], []), "mean")
    all_rooms = list(room_temp.values())

    result: dict = {"rooms": {}}

    gas = series_from_stats(stats.get(conf["gas_meter"], []), "sum") if conf.get("gas_meter") else {}
    q_by_day = daily_gas_kwh(gas, tz)
    dt_by_day = daily_delta_t(all_rooms, outdoor, tz)
    # Primary HLC over the full window: season-blended and stable. A
    # shorter-window "recent" estimate rides along as extra data — useful for
    # seeing improvements (draught-proofing etc.) without destabilising the
    # main sensor value in shoulder seasons.
    result["hlc"] = None
    since_full = (now - timedelta(days=windows_days[-1])).astimezone(tz).date()
    fit = fit_hlc(q_by_day, dt_by_day, since_full)
    if fit:
        result["hlc"] = fit | {"window_days": windows_days[-1]}
        for window in windows_days[:-1]:
            since = (now - timedelta(days=window)).astimezone(tz).date()
            recent = fit_hlc(q_by_day, dt_by_day, since)
            if recent and recent["days_used"] >= HLC_MIN_DAYS:
                result["hlc"]["recent_hlc_w_per_k"] = recent["hlc_w_per_k"]
                result["hlc"]["recent_window_days"] = window
                result["hlc"]["recent_days_used"] = recent["days_used"]
                break

    for name, temps in room_temp.items():
        result["rooms"][name] = None
        for window in windows_days:
            since = (now - timedelta(days=window)).astimezone(tz).date()
            fits = night_taus(temps, outdoor, room_heat.get(name), tz, since)
            if len(fits) >= TAU_MIN_NIGHTS or window == windows_days[-1]:
                if fits:
                    taus = sorted(f["tau_hours"] for f in fits)
                    result["rooms"][name] = {
                        "tau_median_h": median(taus),
                        "nights_fitted": len(fits),
                        "last_night": fits[-1]["date"],
                        "window_days": window,
                    }
                break

    # Loft ratio needs cold nights, so use the full window; drop_flatlines
    # keeps a dead sensor's frozen value from poisoning the median.
    result["loft"] = None
    if conf.get("loft"):
        loft = drop_flatlines(series_from_stats(stats.get(conf["loft"], []), "mean"))
        ratio = loft_ratio(all_rooms, loft, outdoor, tz, since_full)
        if ratio:
            result["loft"] = ratio | {"window_days": windows_days[-1]}

    return result
