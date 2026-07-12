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
from math import exp, log, sqrt
from statistics import median

Series = dict[int, float]

GAS_MAX_STEP_KWH = 40.0  # bigger hourly steps are meter/statistics artifacts
HLC_MIN_DT = 4.0
HLC_MIN_Q = 0.5
HLC_MIN_DAYS = 30  # for the shorter-window "recent" estimate
HLC_FALLBACK_MIN_DAYS = 20
HLC_MIN_R2 = 0.5
HLC_MIN_DT_SPREAD = 3.0
HLC_MIN_W_PER_K = 10.0
HLC_MAX_W_PER_K = 1500.0
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
WATER_MAX_STEP_L = 1000.0  # bigger hourly steps are meter/statistics artifacts
CO2_MIN_WINDOW_H = 4.0
CO2_MIN_DROP_PPM = 120.0
CO2_MIN_R2 = 0.9
CO2_BASELINE_PCT = 0.02  # outdoor CO2 estimated as this low percentile of the series
CO2_MIN_WINDOWS = 10
CO2_MAX_GAP_S = 5400  # 1.5h; bigger gaps break the exponential-decay assumption
METER_MAX_GAP_S = 5400
MIN_DAILY_METER_COVERAGE = 0.9
MIN_DAILY_TEMPERATURE_HOURS = 18
MIN_WATER_REGRESSION_R2 = 0.5
MIN_ACH = 0.05
MAX_ACH = 3.0
AIR_HEAT_CAPACITY = 0.335  # Wh/(m3*K), volumetric heat capacity of air
DEFAULT_BOILER_EFFICIENCY = 0.88
DHW_BASELINE_MIN_DAYS = 7
DHW_REGRESSION_MIN_HOURS = 200
DHW_THEORETICAL_WH_PER_L = 34.8  # Wh to raise 1L by 30K, a combi's typical DHW rise
MAINS_TANK_TEMP_C = 55.0
MAINS_TEMP_MIN_C = 4.0
MAINS_TEMP_MAX_C = 16.0
MAINS_OUTDOOR_MIN_C = 2.0
MAINS_OUTDOOR_MAX_C = 18.0


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


def hourly_change(cumulative: Series, max_step: float) -> Series:
    """Hourly deltas from a cumulative statistics 'sum' series, dropping
    resets (negative steps) and implausible artifacts (> max_step). Unlike
    daily_gas_kwh's own accumulation, zero-valued hours are kept - they
    matter for lining a fine-grained series up against another (e.g. gas
    against water for the DHW regression).
    """
    out: Series = {}
    items = sorted(cumulative.items())
    for (t0, v0), (t1, v1) in zip(items, items[1:]):
        step = v1 - v0
        if 0 < t1 - t0 <= METER_MAX_GAP_S and 0 <= step <= max_step:
            out[t1] = step
    return out


def _expected_local_day_hours(day, tz: tzinfo) -> int:
    """Number of real hours in a local day, including DST transitions."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=1)
    return round((end.timestamp() - start.timestamp()) / 3600)


def daily_gas_kwh(gas_sum: Series, tz: tzinfo) -> dict:
    """Complete local-date kWh totals from a cumulative statistics series."""
    days: dict = defaultdict(list)
    for ts, step in hourly_change(gas_sum, GAS_MAX_STEP_KWH).items():
        days[_local(ts, tz).date()].append(step)
    return {
        day: sum(steps)
        for day, steps in days.items()
        if len(steps)
        >= (_expected_local_day_hours(day, tz) - 1) * MIN_DAILY_METER_COVERAGE
    }


def daily_delta_t(rooms: list[Series], outdoor: Series, tz: tzinfo) -> dict:
    """Local-date mean dT with a fixed, complete room population."""
    per_day: dict = defaultdict(list)
    for ts, t_out in outdoor.items():
        temps = [room[ts] for room in rooms if ts in room]
        if rooms and len(temps) == len(rooms):
            per_day[_local(ts, tz).date()].append(sum(temps) / len(temps) - t_out)
    return {
        d: sum(v) / len(v)
        for d, v in per_day.items()
        if len(v) >= MIN_DAILY_TEMPERATURE_HOURS
    }


def daily_mean(series: Series, tz: tzinfo) -> dict:
    """Local-date mean of any hourly series."""
    per_day: dict = defaultdict(list)
    for ts, v in series.items():
        per_day[_local(ts, tz).date()].append(v)
    return {d: sum(v) / len(v) for d, v in per_day.items()}


def fit_hlc(
    q_by_day: dict, dt_by_day: dict, since, dhw_by_day: dict | None = None
) -> dict | None:
    days = sorted(d for d in q_by_day if d in dt_by_day and d >= since)
    pairs = []
    for d in days:
        if dt_by_day[d] <= HLC_MIN_DT or q_by_day[d] <= HLC_MIN_Q:
            continue
        adjusted = q_by_day[d] - (dhw_by_day.get(d, 0.0) if dhw_by_day else 0.0)
        if adjusted <= 0:
            continue
        pairs.append((dt_by_day[d], adjusted))
    if len(pairs) < HLC_FALLBACK_MIN_DAYS:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if max(xs) - min(xs) < HLC_MIN_DT_SPREAD:
        return None
    slope, intercept, r2 = linear_fit(xs, ys)
    hlc = slope * 1000 / 24
    if slope <= 0 or r2 < HLC_MIN_R2 or not HLC_MIN_W_PER_K <= hlc <= HLC_MAX_W_PER_K:
        return None

    mx = sum(xs) / len(xs)
    sxx = sum((x - mx) ** 2 for x in xs)
    residual_sum = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)
    )
    slope_se = sqrt(residual_sum / (len(xs) - 2) / sxx) if len(xs) > 2 and sxx else 0.0
    lower_slope = slope - 1.96 * slope_se
    upper_slope = slope + 1.96 * slope_se
    if lower_slope <= 0:
        return None
    baseline_days = [
        q_by_day[d] for d in q_by_day
        if d >= since and d in dt_by_day and dt_by_day[d] < DHW_BASELINE_MAX_DT
    ]
    return {
        "hlc_w_per_k": hlc,
        "hlc_ci_low_w_per_k": lower_slope * 1000 / 24,
        "hlc_ci_high_w_per_k": upper_slope * 1000 / 24,
        "r_squared": r2,
        "days_used": len(pairs),
        "free_gains_kwh_per_day": -intercept,
        "regression_intercept_kwh_per_day": intercept,
        "status": "valid" if len(pairs) >= HLC_MIN_DAYS else "provisional",
        "dhw_baseline_kwh_per_day": (
            median(baseline_days) if len(baseline_days) >= DHW_BASELINE_MIN_DAYS else None
        ),
    }


def dhw_baseline(
    q_by_day: dict, dt_by_day: dict, outdoor_by_day: dict, since
) -> dict | None:
    """Robust non-heating gas estimate (hot water, plus cooking/pilot only if
    those burn gas): median daily gas on days
    with negligible heating demand (mean dT < DHW_BASELINE_MAX_DT), plus the
    mean outdoor temperature on those days (the mains-water-temperature
    reference point `dhw_daily_kwh` scales from). Distinct from fit_hlc's own
    `dhw_baseline_kwh_per_day` attribute, which is the same median as a cheap
    sanity figure - this version additionally tracks the outdoor reference
    needed for the winter mains-temperature correction below.
    """
    days = [
        d for d in q_by_day
        if d >= since and d in dt_by_day and dt_by_day[d] < DHW_BASELINE_MAX_DT
        and d in outdoor_by_day
    ]
    if len(days) < DHW_BASELINE_MIN_DAYS:
        return None
    return {
        "kwh_per_day": median([q_by_day[d] for d in days]),
        "outdoor_mean": sum(outdoor_by_day[d] for d in days) / len(days),
        "days_used": len(days),
    }


def mains_temp_c(outdoor_c: float) -> float:
    """Coarse UK mains-water-temperature model (cold in winter, warmer in
    summer, tracking outdoor air with damping). Only used as a scaling ratio
    between the summer DHW baseline and other days, not as an absolute
    physical claim.
    """
    if outdoor_c <= MAINS_OUTDOOR_MIN_C:
        return MAINS_TEMP_MIN_C
    if outdoor_c >= MAINS_OUTDOOR_MAX_C:
        return MAINS_TEMP_MAX_C
    frac = (outdoor_c - MAINS_OUTDOOR_MIN_C) / (MAINS_OUTDOOR_MAX_C - MAINS_OUTDOOR_MIN_C)
    return MAINS_TEMP_MIN_C + frac * (MAINS_TEMP_MAX_C - MAINS_TEMP_MIN_C)


def dhw_daily_kwh(outdoor_c: float, baseline: dict) -> float:
    """Scale the measured summer non-heating baseline for a colder mains
    supply on another day. Applies the (tank-mains) delta-T ratio to the
    whole baseline rather than splitting out any gas cooking/pilot share - a
    simplification when those exist (they don't depend on mains temperature),
    and exact in an electric-cooking home where the baseline is pure DHW. A
    reliable data-driven split needs more water history than a single summer
    provides (see fit_water_gas's `hot_fraction_pct` once more accrues).
    """
    ref_dt = MAINS_TANK_TEMP_C - mains_temp_c(baseline["outdoor_mean"])
    day_dt = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_c)
    ratio = day_dt / ref_dt if ref_dt > 0 else 1.0
    return baseline["kwh_per_day"] * ratio


def fit_water_gas(
    gas_sum: Series,
    water_sum: Series,
    boiler_efficiency: float = DEFAULT_BOILER_EFFICIENCY,
) -> dict | None:
    """Informational only: hourly gas-vs-water regression, giving a rough
    Wh-per-litre rate and the implied hot fraction of metered water. Noisy
    (household draws are bursty and mix hot/cold) - not the basis for the
    DHW cost figure, which comes from dhw_baseline instead.
    """
    gas_hourly = hourly_change(gas_sum, GAS_MAX_STEP_KWH)
    water_hourly = hourly_change(water_sum, WATER_MAX_STEP_L)
    common = sorted(set(gas_hourly) & set(water_hourly))
    if len(common) < DHW_REGRESSION_MIN_HOURS:
        return None
    slope, _, r2 = linear_fit(
        [water_hourly[ts] for ts in common], [gas_hourly[ts] for ts in common]
    )
    if slope <= 0 or r2 < MIN_WATER_REGRESSION_R2:
        return None
    wh_per_litre = slope * 1000
    return {
        "wh_per_litre": wh_per_litre,
        "fuel_input_wh_per_litre": wh_per_litre,
        "hot_fraction_pct": min(
            100.0,
            wh_per_litre * boiler_efficiency / DHW_THEORETICAL_WH_PER_L * 100,
        ),
        "regression_r_squared": r2,
        "regression_hours": len(common),
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
        if any(b - a > CO2_MAX_GAP_S for a, b in zip(hours, hours[1:])):
            continue
        if heating is not None:
            h_vals = [heating[ts] for ts in hours if ts in heating]
            # Missing heating observations are not evidence that the radiator
            # stayed off. Require at least 80% coverage when configured.
            if len(h_vals) / len(hours) < 0.8 or max(h_vals) > TAU_MAX_HEATING_PCT:
                continue
        if any(ts not in outdoor for ts in hours):
            continue
        temps = [room[ts] for ts in hours]
        excess = [room[ts] - outdoor[ts] for ts in hours]
        if min(excess) < TAU_MIN_DT:
            continue
        if temps[0] - temps[-1] < TAU_MIN_DROP:
            continue
        best_tau = None
        best_residual = float("inf")
        for quarter_hours in range(4, 801):  # 1h to 200h in 0.25h steps
            tau = quarter_hours / 4
            predicted = [temps[0]]
            for index in range(1, len(hours)):
                elapsed = (hours[index] - hours[index - 1]) / 3600
                boundary = (outdoor[hours[index - 1]] + outdoor[hours[index]]) / 2
                predicted.append(
                    boundary + (predicted[-1] - boundary) * exp(-elapsed / tau)
                )
            residual = sum((actual - model) ** 2 for actual, model in zip(temps, predicted))
            if residual < best_residual:
                best_residual = residual
                best_tau = tau
        mean_temp = sum(temps) / len(temps)
        total = sum((value - mean_temp) ** 2 for value in temps)
        r2 = 1 - best_residual / total if total > 0 else 0.0
        if best_tau is None or r2 < TAU_MIN_R2:
            continue
        fits.append({"date": str(date), "tau_hours": best_tau, "r_squared": r2})
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
    ratio = median(ratios)
    if not 0.0 <= ratio <= 1.0:
        return None
    ordered = sorted(ratios)
    q1 = ordered[len(ordered) // 4]
    q3 = ordered[(3 * len(ordered)) // 4]
    out_of_range_pct = sum(not 0 <= value <= 1 for value in ratios) / len(ratios) * 100
    if q3 - q1 > 0.5 or out_of_range_pct > 20:
        return None
    return {
        "ratio": ratio,
        "hours_used": len(ratios),
        "iqr": q3 - q1,
        "out_of_range_pct": out_of_range_pct,
    }


def _percentile(values: list[float], pct: float) -> float:
    s = sorted(values)
    return s[int(pct * (len(s) - 1))]


def air_change_rate(
    co2: Series, tz: tzinfo, since, outdoor_baseline_ppm: float | None = None
) -> dict | None:
    """Whole-home air-change rate (1/h) from CO2 decay curves.

    With no fresh CO2 source (room unoccupied / no combustion), indoor CO2
    relaxes toward an outdoor baseline as C(t) = C_out + (C0-C_out)e^(-ACH t),
    so ln(C-C_out) is linear in t with slope -ACH. Falling stretches of the
    series are auto-detected (occupants leaving/sleeping does this several
    times a day) and fit individually; the median ACH across clean fits is
    reported. Measured in one room and used as a whole-home infiltration
    proxy - a reasonable ballpark, not an exact whole-flat measurement.
    """
    items = sorted((ts, v) for ts, v in co2.items() if _local(ts, tz).date() >= since)
    if len(items) < 2:
        return None
    baseline = (
        outdoor_baseline_ppm
        if outdoor_baseline_ppm is not None
        else _percentile([v for _, v in items], CO2_BASELINE_PCT)
    )

    fits = []
    i, n = 0, len(items)
    while i < n - 1:
        j = i
        while (
            j + 1 < n
            and items[j + 1][0] - items[j][0] <= CO2_MAX_GAP_S
            and items[j + 1][1] <= items[j][1] + 2  # tolerate small sensor noise
        ):
            j += 1
        window = items[i : j + 1]
        i = j if j > i else i + 1
        if (window[-1][0] - window[0][0]) / 3600 < CO2_MIN_WINDOW_H:
            continue
        if window[0][1] - baseline < CO2_MIN_DROP_PPM:
            continue
        excess = [v - baseline for _, v in window]
        if min(excess) <= 0:
            continue
        t0 = window[0][0]
        xs = [(ts - t0) / 3600 for ts, _ in window]
        ys = [log(e) for e in excess]
        slope, _, r2 = linear_fit(xs, ys)
        if slope >= 0 or r2 < CO2_MIN_R2:
            continue
        fits.append(-slope)

    if len(fits) < CO2_MIN_WINDOWS:
        return None
    ach = median(fits)
    if not MIN_ACH <= ach <= MAX_ACH:
        return None
    return {"ach": ach, "windows": len(fits), "baseline_ppm": baseline}


def combine_air_change_rates(fits: list[dict]) -> dict | None:
    """Combine independently fitted room proxies without pooling raw CO2."""
    if not fits:
        return None
    return {
        "ach": median([fit["ach"] for fit in fits]),
        "windows": sum(fit["windows"] for fit in fits),
        "baseline_ppm": median([fit["baseline_ppm"] for fit in fits]),
        "sensor_count": len(fits),
    }


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
    outdoor_by_day = daily_mean(outdoor, tz)
    # Primary HLC over the full window: season-blended and stable. A
    # shorter-window "recent" estimate rides along as extra data — useful for
    # seeing improvements (draught-proofing etc.) without destabilising the
    # main sensor value in shoulder seasons.
    result["hlc"] = None
    since_full = (now - timedelta(days=windows_days[-1])).astimezone(tz).date()
    current_day = now.astimezone(tz).date()
    # Hourly statistics for the current day are necessarily incomplete.
    q_by_day.pop(current_day, None)
    dt_by_day.pop(current_day, None)
    outdoor_by_day.pop(current_day, None)

    # Hot water: a robust summer (heating-off) gas baseline, used both as the
    # headline "hot water gas" cost figure and - scaled for colder winter
    # mains water via dhw_daily_kwh - to strip DHW out of the winter HLC fit
    # below via the space_heating_hlc_w_per_k attribute.
    result["dhw"] = None
    dhw_by_day: dict = {}
    baseline = dhw_baseline(q_by_day, dt_by_day, outdoor_by_day, since_full)
    if baseline:
        dhw_by_day = {
            d: dhw_daily_kwh(outdoor_by_day[d], baseline)
            for d in q_by_day
            if d in outdoor_by_day
        }
        result["dhw"] = {
            "kwh_per_day": baseline["kwh_per_day"],
            "days_used": baseline["days_used"],
            "outdoor_mean": baseline["outdoor_mean"],
            "status": "valid" if baseline["days_used"] >= 14 else "provisional",
        }
        gas_rate = conf.get("gas_unit_rate")
        if gas_rate:
            result["dhw"]["cost_per_day_gbp"] = baseline["kwh_per_day"] * gas_rate
            modelled_daily = list(dhw_by_day.values())
            annual_kwh = (
                sum(modelled_daily) / len(modelled_daily) * 365
                if modelled_daily
                else baseline["kwh_per_day"] * 365
            )
            result["dhw"]["modelled_annual_kwh"] = annual_kwh
            result["dhw"]["cost_per_year_gbp"] = annual_kwh * gas_rate
        if conf.get("water"):
            water = series_from_stats(stats.get(conf["water"], []), "sum")
            water_fit = fit_water_gas(
                gas,
                water,
                conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY,
            )
            if water_fit:
                result["dhw"].update(water_fit)

    fit = fit_hlc(q_by_day, dt_by_day, since_full)
    if fit:
        boiler_eff = conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY
        gas_side_fit = fit
        corrected = (
            fit_hlc(q_by_day, dt_by_day, since_full, dhw_by_day)
            if dhw_by_day
            else None
        )
        space_heating_fit = corrected or gas_side_fit
        delivered_hlc = space_heating_fit["hlc_w_per_k"] * boiler_eff
        result["hlc"] = gas_side_fit | {
            "hlc_w_per_k": delivered_hlc,
            "hlc_ci_low_w_per_k": space_heating_fit["hlc_ci_low_w_per_k"] * boiler_eff,
            "hlc_ci_high_w_per_k": space_heating_fit["hlc_ci_high_w_per_k"] * boiler_eff,
            "r_squared": space_heating_fit["r_squared"],
            "days_used": space_heating_fit["days_used"],
            "free_gains_kwh_per_day": space_heating_fit["free_gains_kwh_per_day"],
            "regression_intercept_kwh_per_day": space_heating_fit[
                "regression_intercept_kwh_per_day"
            ],
            "fuel_input_hlc_w_per_k": gas_side_fit["hlc_w_per_k"],
            "fuel_input_r_squared": gas_side_fit["r_squared"],
            "space_heating_fuel_input_hlc_w_per_k": space_heating_fit["hlc_w_per_k"],
            "delivered_hlc_w_per_k": delivered_hlc,
            "boiler_efficiency_used": boiler_eff,
            "window_days": windows_days[-1],
            "status": space_heating_fit["status"],
        }
        floor_area = conf.get("floor_area_m2")
        if floor_area:
            result["hlc"]["hlc_w_per_k_per_m2"] = delivered_hlc / floor_area
        if corrected:
            result["hlc"]["space_heating_hlc_w_per_k"] = delivered_hlc
            result["hlc"]["space_heating_r_squared"] = corrected["r_squared"]
        for window in windows_days[:-1]:
            since = (now - timedelta(days=window)).astimezone(tz).date()
            recent = fit_hlc(
                q_by_day, dt_by_day, since, dhw_by_day if dhw_by_day else None
            )
            if recent and recent["days_used"] >= HLC_MIN_DAYS:
                result["hlc"]["recent_hlc_w_per_k"] = (
                    recent["hlc_w_per_k"] * boiler_eff
                )
                result["hlc"]["recent_window_days"] = window
                result["hlc"]["recent_days_used"] = recent["days_used"]
                break

    # Ventilation/fabric split: air-change rate from CO2 decay curves, times
    # the flat's volume, gives ventilation W/K; the rest of the delivered
    # space-heating HLC is fabric (walls/windows/roof).
    result["losses"] = None
    if conf.get("co2") and conf.get("floor_area_m2") and conf.get("ceiling_height_m") and result["hlc"]:
        configured_co2 = conf["co2"]
        co2_ids = [configured_co2] if isinstance(configured_co2, str) else configured_co2
        outdoor_baseline = conf.get("outdoor_co2_ppm")
        outdoor_sensor_used = False
        if conf.get("outdoor_co2_sensor"):
            outdoor_co2 = series_from_stats(
                stats.get(conf["outdoor_co2_sensor"], []), "mean"
            )
            outdoor_values = [
                value
                for ts, value in outdoor_co2.items()
                if _local(ts, tz).date() >= since_full
            ]
            if outdoor_values:
                outdoor_baseline = median(outdoor_values)
                outdoor_sensor_used = True
        ach_fits = []
        for co2_id in co2_ids:
            co2 = series_from_stats(stats.get(co2_id, []), "mean")
            fit_for_sensor = air_change_rate(
                co2, tz, since_full, outdoor_baseline
            )
            if fit_for_sensor:
                ach_fits.append(fit_for_sensor)
        ach_fit = combine_air_change_rates(ach_fits)
        if ach_fit:
            ach_fit["baseline_source"] = (
                "outdoor sensor"
                if outdoor_sensor_used
                else "configured value"
                if conf.get("outdoor_co2_ppm") is not None
                else "indoor low-percentile fallback"
            )
            volume = conf["floor_area_m2"] * conf["ceiling_height_m"]
            ventilation_w_per_k = AIR_HEAT_CAPACITY * ach_fit["ach"] * volume
            hlc_delivered = result["hlc"]["delivered_hlc_w_per_k"]
            if 0 <= ventilation_w_per_k <= hlc_delivered:
                fabric_w_per_k = hlc_delivered - ventilation_w_per_k
                result["losses"] = {
                    "ach": ach_fit["ach"],
                    "windows": ach_fit["windows"],
                    "baseline_ppm": ach_fit["baseline_ppm"],
                    "co2_sensors_used": ach_fit["sensor_count"],
                    "co2_baseline_source": ach_fit["baseline_source"],
                    "ventilation_w_per_k": ventilation_w_per_k,
                    "fabric_w_per_k": fabric_w_per_k,
                    "hlc_delivered_w_per_k": hlc_delivered,
                    "ventilation_share_pct": ventilation_w_per_k / hlc_delivered * 100,
                    "boiler_efficiency_used": result["hlc"]["boiler_efficiency_used"],
                    "scope": (
                        f"median of {ach_fit['sensor_count']} room-derived ACH proxies "
                        "scaled to configured home volume"
                    ),
                }

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
    # keeps a dead sensor's frozen value from poisoning the median. loft_since
    # additionally guards against a sensor that was relocated into the loft -
    # its history from before the move belongs to wherever it used to live,
    # not the loft, and won't necessarily flatline so drop_flatlines alone
    # can't catch it.
    result["loft"] = None
    if conf.get("loft"):
        loft = drop_flatlines(series_from_stats(stats.get(conf["loft"], []), "mean"))
        loft_since = conf.get("loft_since")
        loft_cutoff = max(since_full, loft_since) if loft_since else since_full
        ratio = loft_ratio(all_rooms, loft, outdoor, tz, loft_cutoff)
        if ratio:
            result["loft"] = ratio | {"window_days": windows_days[-1]}
            if conf.get("loft_humidity"):
                humidity = series_from_stats(
                    stats.get(conf["loft_humidity"], []), "mean"
                )
                humidity = {
                    ts: v for ts, v in humidity.items()
                    if _local(ts, tz).date() >= loft_cutoff
                }
                if humidity:
                    result["loft"]["humidity_pct"] = humidity[max(humidity)]

    return result
