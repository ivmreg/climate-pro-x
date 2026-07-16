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
# Consecutive days share a weather system, so regression residuals are serially
# correlated and the naive OLS standard error understates the real uncertainty.
# An AR(1) effective-sample-size correction (measured r1 ~0.4 on a real season,
# i.e. a ~1.5x wider interval) keeps the published CI honest.
HLC_AC_MIN_PAIRS = 10
HLC_AC_MAX_R1 = 0.9  # beyond this the AR(1) correction explodes; clamp instead
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
TAU_MIN_HOURS = 1.0
TAU_MAX_HOURS = 200.0  # search bound; a fit landing here is censored, not measured
TAU_SEARCH_STEP_H = 0.25
TAU_MAX_GAP_S = 5400  # 1.5h; bigger gaps break the exponential-decay assumption
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
DHW_OCCUPIED_MIN_WATER_L = 50.0  # less metered water on a heating-off day = nobody home
DHW_IDLE_MIN_DAYS = 3
DHW_WATER_MIN_DAYS = 10
# Physical bounds on the daily gas-per-litre-per-K rate: pure 55C hot water at
# 88% boiler efficiency costs ~1.32 Wh/L/K of fuel, and metered litres include
# cold draws, so a plausible whole-house rate sits well inside these.
DHW_RATE_MIN_WH_PER_L_PER_K = 0.05
DHW_RATE_MAX_WH_PER_L_PER_K = 1.5
HEATING_OFF_MAX_PCT = 1.0  # daily mean of the busiest room's heating power
ELEC_MAX_STEP_KWH = 20.0  # bigger hourly steps are meter/statistics artifacts
ELEC_MIN_DAYS = 14
CURRENT_BASELINE_DAYS = 30
WATER_USAGE_MIN_DAYS = 5
WATER_OUTLIER_MEDIAN_MULTIPLIER = 3.0
WATER_OUTLIER_IQR_MULTIPLIER = 3.0
RECENT_7D_MIN_DAYS = 5
RECENT_30D_MIN_DAYS = 20
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


def lag1_autocorrelation(days: list, residuals: list[float]) -> float:
    """Lag-1 autocorrelation of `residuals`, paired over adjacent calendar days.

    Days that survive the fit's filters are not contiguous (mild and low-gas
    days are dropped), so only genuinely adjacent dates are treated as a lagged
    pair - a gap of a week carries no AR(1) information.
    """
    pairs = [
        (residuals[i], residuals[i + 1])
        for i in range(len(days) - 1)
        if (days[i + 1] - days[i]).days == 1
    ]
    if len(pairs) < HLC_AC_MIN_PAIRS:
        return 0.0
    mean = sum(residuals) / len(residuals)
    variance = sum((r - mean) ** 2 for r in residuals) / len(residuals)
    if variance <= 0:
        return 0.0
    covariance = sum((a - mean) * (b - mean) for a, b in pairs) / len(pairs)
    return covariance / variance


def _variance_inflation(r1: float) -> float:
    """AR(1) variance inflation factor for a mean/slope standard error.

    Only positive autocorrelation is corrected for: negative r1 would shrink
    the interval, and claiming *more* precision than the independent-sample
    case on this evidence is not a trade worth making.
    """
    r1 = min(max(r1, 0.0), HLC_AC_MAX_R1)
    return (1 + r1) / (1 - r1)


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


def _daily_meter_steps(cumulative: Series, tz: tzinfo, max_step: float) -> dict:
    """Hourly deltas grouped by complete local dates."""
    days: dict = defaultdict(list)
    for ts, step in hourly_change(cumulative, max_step).items():
        days[_local(ts, tz).date()].append(step)
    return {
        day: steps
        for day, steps in days.items()
        if len(steps)
        >= (_expected_local_day_hours(day, tz) - 1) * MIN_DAILY_METER_COVERAGE
    }


def daily_gas_kwh(gas_sum: Series, tz: tzinfo) -> dict:
    """Complete local-date kWh totals from a cumulative statistics series."""
    return {
        day: sum(steps)
        for day, steps in _daily_meter_steps(gas_sum, tz, GAS_MAX_STEP_KWH).items()
    }


def daily_water_litres(water_sum: Series, tz: tzinfo) -> dict:
    """Complete local-date litre totals from a cumulative statistics series."""
    return {
        day: sum(steps)
        for day, steps in _daily_meter_steps(water_sum, tz, WATER_MAX_STEP_L).items()
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


def daily_heating_pct(room_heats: list[Series], tz: tzinfo) -> dict:
    """Local-date mean of the busiest room's heating power, over hours where
    every configured heating-power sensor reports (a missing sensor is not
    evidence its radiator stayed off)."""
    if not room_heats:
        return {}
    common = set(room_heats[0]).intersection(*room_heats[1:])
    per_day: dict = defaultdict(list)
    for ts in common:
        per_day[_local(ts, tz).date()].append(max(heat[ts] for heat in room_heats))
    return {
        d: sum(v) / len(v)
        for d, v in per_day.items()
        if len(v) >= MIN_DAILY_TEMPERATURE_HOURS
    }


def heating_off_days(dt_by_day: dict, heat_pct_by_day: dict) -> set:
    """Days the space heating did not run. Measured heating power decides
    where it exists — in both directions: a warm shoulder day with a burst of
    heating is not "off" just because dT stayed small, and a mild day where
    the heating never fired is "off" even with dT above the proxy threshold.
    Days without heating-power coverage fall back to the dT proxy."""
    off = set()
    for d in set(dt_by_day) | set(heat_pct_by_day):
        pct = heat_pct_by_day.get(d)
        if pct is not None:
            if pct <= HEATING_OFF_MAX_PCT:
                off.add(d)
        elif dt_by_day.get(d, float("inf")) < DHW_BASELINE_MAX_DT:
            off.add(d)
    return off


def fit_hlc(
    q_by_day: dict,
    dt_by_day: dict,
    since,
    dhw_by_day: dict | None = None,
    until=None,
) -> dict | None:
    days = sorted(
        d
        for d in q_by_day
        if d in dt_by_day and d >= since and (until is None or d <= until)
    )
    pairs = []
    for d in days:
        if dt_by_day[d] <= HLC_MIN_DT or q_by_day[d] <= HLC_MIN_Q:
            continue
        adjusted = q_by_day[d] - (dhw_by_day.get(d, 0.0) if dhw_by_day else 0.0)
        if adjusted <= 0:
            continue
        pairs.append((d, dt_by_day[d], adjusted))
    if len(pairs) < HLC_FALLBACK_MIN_DAYS:
        return None
    fit_days = [p[0] for p in pairs]
    xs = [p[1] for p in pairs]
    ys = [p[2] for p in pairs]
    if max(xs) - min(xs) < HLC_MIN_DT_SPREAD:
        return None
    slope, intercept, r2 = linear_fit(xs, ys)
    hlc = slope * 1000 / 24
    if slope <= 0 or r2 < HLC_MIN_R2 or not HLC_MIN_W_PER_K <= hlc <= HLC_MAX_W_PER_K:
        return None

    mx = sum(xs) / len(xs)
    sxx = sum((x - mx) ** 2 for x in xs)
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    residual_sum = sum(r**2 for r in residuals)
    slope_se = sqrt(residual_sum / (len(xs) - 2) / sxx) if len(xs) > 2 and sxx else 0.0
    # Widen for serially correlated residuals: consecutive days share weather,
    # so the fit has fewer independent observations than it has days.
    r1 = lag1_autocorrelation(fit_days, residuals)
    vif = _variance_inflation(r1)
    slope_se *= sqrt(vif)
    effective_days = len(xs) / vif
    lower_slope = slope - 1.96 * slope_se
    upper_slope = slope + 1.96 * slope_se
    if lower_slope <= 0:
        return None
    baseline_days = [
        q_by_day[d] for d in q_by_day
        if d >= since
        and (until is None or d <= until)
        and d in dt_by_day
        and dt_by_day[d] < DHW_BASELINE_MAX_DT
    ]
    return {
        "hlc_w_per_k": hlc,
        "hlc_ci_low_w_per_k": lower_slope * 1000 / 24,
        "hlc_ci_high_w_per_k": upper_slope * 1000 / 24,
        "r_squared": r2,
        "days_used": len(pairs),
        "residual_autocorrelation": r1,
        "effective_independent_days": effective_days,
        "free_gains_kwh_per_day": -intercept,
        "regression_intercept_kwh_per_day": intercept,
        "status": "valid" if len(pairs) >= HLC_MIN_DAYS else "provisional",
        "dhw_baseline_kwh_per_day": (
            median(baseline_days) if len(baseline_days) >= DHW_BASELINE_MIN_DAYS else None
        ),
    }


def dhw_baseline(
    q_by_day: dict,
    dt_by_day: dict,
    outdoor_by_day: dict,
    since,
    heating_off: set | None = None,
    water_by_day: dict | None = None,
    min_water_l: float = DHW_OCCUPIED_MIN_WATER_L,
) -> dict | None:
    """Robust non-heating gas estimate (hot water, plus cooking/pilot only if
    those burn gas): median daily gas on heating-off days (measured heating
    power where available, dT proxy otherwise - or pass a precomputed
    `heating_off` set), plus the mean outdoor temperature on those days (the
    mains-water-temperature reference point `dhw_daily_kwh` scales from).

    When daily water totals exist, heating-off days with less than
    `min_water_l` metered are away days: their near-zero gas would drag the
    typical-day median down, so they are excluded and their median gas is
    reported separately as `idle_gas_kwh_per_day` (boiler standby - should be
    ~0 for a combi with no pilot). Days predating the water meter are kept.

    Distinct from fit_hlc's own `dhw_baseline_kwh_per_day` attribute, which
    is a cheap dT-only sanity figure.
    """
    if heating_off is None:
        heating_off = {d for d, dt in dt_by_day.items() if dt < DHW_BASELINE_MAX_DT}
    candidates = [
        d for d in q_by_day
        if d >= since and d in heating_off and d in outdoor_by_day
    ]
    away = []
    if water_by_day:
        away = [
            d for d in candidates
            if d in water_by_day and water_by_day[d] < min_water_l
        ]
        candidates = [d for d in candidates if d not in set(away)]
    if len(candidates) < DHW_BASELINE_MIN_DAYS:
        return None
    out = {
        "kwh_per_day": median([q_by_day[d] for d in candidates]),
        "outdoor_mean": sum(outdoor_by_day[d] for d in candidates) / len(candidates),
        "days_used": len(candidates),
        "low_water_days_excluded": len(away),
    }
    if len(away) >= DHW_IDLE_MIN_DAYS:
        out["idle_gas_kwh_per_day"] = median([q_by_day[d] for d in away])
    return out


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


def fit_dhw_water_rate(
    q_by_day: dict,
    water_by_day: dict,
    outdoor_by_day: dict,
    heating_off: set,
    since,
    min_water_l: float = DHW_OCCUPIED_MIN_WATER_L,
    max_water_l: float | None = None,
) -> dict | None:
    """Daily gas-per-litre rate from days where all gas is known to be hot
    water: heating off (so no space-heating gas) and enough metered water that
    someone was home. Each day's rate is normalised by the modelled
    tank-minus-mains rise so summer and shoulder days are comparable; the
    median rate then predicts DHW gas on *heating* days from that day's actual
    litres (dhw_kwh_from_water), which tracks real usage swings (guests,
    holidays, laundry) that the constant mains-scaled baseline cannot.
    """
    samples = []
    for d, q in q_by_day.items():
        if d < since or d not in heating_off or d not in outdoor_by_day or q <= 0:
            continue
        litres = water_by_day.get(d)
        if (
            litres is None
            or litres < min_water_l
            or (max_water_l is not None and litres > max_water_l)
        ):
            continue
        rise = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_by_day[d])
        if rise <= 0:
            continue
        samples.append(q * 1000 / litres / rise)
    if len(samples) < DHW_WATER_MIN_DAYS:
        return None
    rate = median(samples)
    if not DHW_RATE_MIN_WH_PER_L_PER_K <= rate <= DHW_RATE_MAX_WH_PER_L_PER_K:
        return None
    ordered = sorted(samples)
    iqr = ordered[(3 * len(ordered)) // 4] - ordered[len(ordered) // 4]
    return {
        "wh_per_litre_per_k": rate,
        "days_used": len(samples),
        "iqr_wh_per_litre_per_k": iqr,
    }


def dhw_kwh_from_water(litres: float, outdoor_c: float, water_rate: dict) -> float:
    """DHW gas for a day, from its metered litres and the fitted daily rate."""
    rise = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_c)
    return litres * water_rate["wh_per_litre_per_k"] * rise / 1000


def attribute_dhw_by_day(
    q_by_day: dict,
    outdoor_by_day: dict,
    heating_off: set,
    baseline: dict,
    water_by_day: dict | None = None,
    water_rate: dict | None = None,
    water_outlier_limit_l: float | None = None,
    until=None,
) -> tuple[dict, dict]:
    """Split daily gas into DHW using measured heating-off days, guarded
    water attribution on heating days, and the weather-scaled baseline as the
    fallback. Extreme whole-house water days are cold-use ambiguous and must
    not erase genuine space-heating gas."""
    attributed = {}
    quality = {
        "water_attribution_days": 0,
        "baseline_fallback_days": 0,
        "water_outlier_days_ignored": 0,
    }
    water_by_day = water_by_day or {}
    for d, q in q_by_day.items():
        if until is not None and d > until:
            continue
        if d in heating_off:
            attributed[d] = q
            continue
        litres = water_by_day.get(d)
        water_is_outlier = (
            litres is not None
            and water_outlier_limit_l is not None
            and litres > water_outlier_limit_l
        )
        if water_is_outlier:
            quality["water_outlier_days_ignored"] += 1
        if (
            water_rate
            and litres is not None
            and not water_is_outlier
            and d in outdoor_by_day
        ):
            attributed[d] = min(
                dhw_kwh_from_water(litres, outdoor_by_day[d], water_rate), q
            )
            quality["water_attribution_days"] += 1
        elif d in outdoor_by_day:
            attributed[d] = min(dhw_daily_kwh(outdoor_by_day[d], baseline), q)
            quality["baseline_fallback_days"] += 1
    return attributed, quality


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


def recent_daily_mean(by_day: dict, end_day, days_back: int, min_days: int) -> float | None:
    """Mean of the last `days_back` calendar days ending at `end_day`,
    or None when too few of those days have data to be representative."""
    window = [
        v for d, v in by_day.items()
        if end_day - timedelta(days=days_back - 1) <= d <= end_day
    ]
    if len(window) < min_days:
        return None
    return sum(window) / len(window)


def recent_daily_median(
    by_day: dict, end_day, days_back: int, min_days: int
) -> float | None:
    """Median over a recent calendar window, with the same coverage rule as
    `recent_daily_mean`. Useful for a typical day when isolated high-use days
    should remain visible in the mean but not redefine normal behaviour."""
    window = [
        v for d, v in by_day.items()
        if end_day - timedelta(days=days_back - 1) <= d <= end_day
    ]
    if len(window) < min_days:
        return None
    return median(window)


def recent_daily_count(by_day: dict, end_day, days_back: int) -> int:
    """Number of usable dates represented in a recent calendar window."""
    return sum(
        1
        for d in by_day
        if end_day - timedelta(days=days_back - 1) <= d <= end_day
    )


def previous_period_mean(
    by_day: dict, end_day, days_back: int, min_days: int
) -> float | None:
    """Mean for the immediately preceding calendar period."""
    return recent_daily_mean(
        by_day, end_day - timedelta(days=days_back), days_back, min_days
    )


def water_outlier_limit_litres(
    water_by_day: dict, end_day, days_back: int = CURRENT_BASELINE_DAYS
) -> float | None:
    """Conservative upper bound for using total water as a DHW predictor.

    Whole-house water includes cold-only events such as garden use. Such a day
    must not be allowed to turn all winter gas into apparent hot water. The
    larger of 3x the median and Tukey's far-out fence keeps ordinary busy days
    while rejecting extreme events.
    """
    values = sorted(
        v
        for d, v in water_by_day.items()
        if end_day - timedelta(days=days_back - 1) <= d <= end_day
    )
    if len(values) < WATER_USAGE_MIN_DAYS:
        return None
    med = median(values)
    q1 = values[len(values) // 4]
    q3 = values[(3 * len(values)) // 4]
    return max(
        med * WATER_OUTLIER_MEDIAN_MULTIPLIER,
        q3 + WATER_OUTLIER_IQR_MULTIPLIER * (q3 - q1),
    )


def latest_heating_day(
    q_by_day: dict, dt_by_day: dict, heat_pct_by_day: dict, since
):
    """Latest HLC-eligible day with evidence that heating ran.

    Measured heating power controls where available. Before that history
    begins, the same cold-day proxy used by the HLC fit is used. Anchoring the
    heating model to this date means subsequent summer days cannot move its
    window or retrain its DHW correction.
    """
    candidates = []
    for d, q in q_by_day.items():
        dt = dt_by_day.get(d)
        if d < since or dt is None or dt <= HLC_MIN_DT or q <= HLC_MIN_Q:
            continue
        pct = heat_pct_by_day.get(d)
        if pct is None or pct > HEATING_OFF_MAX_PCT:
            candidates.append(d)
    return max(candidates) if candidates else None


def electricity_summary(elec_sum: Series, tz: tzinfo, since, until) -> dict | None:
    """Descriptive electricity metrics that need no disaggregation guesswork:
    daily kWh, and an always-on baseload estimate (median over days of the
    cheapest hour - typically 3-5am, when only fridges/standby run).

    `implied_internal_gains_w` is the mean electrical draw expressed in watts:
    almost all household electricity ends up as heat indoors, so it is useful
    context for the HLC regression's free-gains intercept. It is deliberately
    NOT fed into the thermal fits - the gas-only regression already absorbs
    steady internal gains in its intercept, and subtracting a second meter
    would double-count them.
    """
    per_day = _daily_meter_steps(elec_sum, tz, ELEC_MAX_STEP_KWH)
    per_day = {d: steps for d, steps in per_day.items() if since <= d < until}
    if len(per_day) < ELEC_MIN_DAYS:
        return None
    daily_kwh = {d: sum(steps) for d, steps in per_day.items()}
    latest_day = max(daily_kwh)
    recent_days = {
        d: steps
        for d, steps in per_day.items()
        if latest_day - timedelta(days=CURRENT_BASELINE_DAYS - 1) <= d <= latest_day
    }
    if len(recent_days) < ELEC_MIN_DAYS:
        return None
    historical_mean = sum(daily_kwh.values()) / len(daily_kwh)
    recent_daily = {d: sum(steps) for d, steps in recent_days.items()}
    mean_daily = sum(recent_daily.values()) / len(recent_daily)
    baseload_kw = median(min(steps) for steps in recent_days.values())
    out = {
        "kwh_per_day": mean_daily,
        "historical_kwh_per_day": historical_mean,
        "baseload_w": baseload_kw * 1000,
        "baseload_kwh_per_day": baseload_kw * 24,
        "implied_internal_gains_w": mean_daily * 1000 / 24,
        "days_used": len(per_day),
        "current_period_days_used": len(recent_days),
        "latest_complete_day": latest_day,
        "daily_kwh": daily_kwh,
    }
    if mean_daily > 0:
        out["baseload_share_pct"] = min(100.0, baseload_kw * 24 / mean_daily * 100)
    return out


def water_usage_summary(water_sum: Series, tz: tzinfo, since, until) -> dict | None:
    """Rolling total-water context for DHW, without pretending all litres are
    hot. The mean preserves actual consumption; the median describes a typical
    day and an outlier count explains when the two diverge."""
    per_day = _daily_meter_steps(water_sum, tz, WATER_MAX_STEP_L)
    daily_litres = {
        d: sum(steps) for d, steps in per_day.items() if since <= d < until
    }
    if len(daily_litres) < WATER_USAGE_MIN_DAYS:
        return None
    latest_day = max(daily_litres)
    mean_7d = recent_daily_mean(
        daily_litres, latest_day, 7, RECENT_7D_MIN_DAYS
    )
    if mean_7d is None:
        return None
    mean_30d = recent_daily_mean(
        daily_litres, latest_day, 30, RECENT_30D_MIN_DAYS
    )
    median_30d = recent_daily_median(
        daily_litres, latest_day, 30, RECENT_30D_MIN_DAYS
    )
    limit = water_outlier_limit_litres(daily_litres, latest_day)
    return {
        "litres_per_day_7d": mean_7d,
        "days_used_7d": recent_daily_count(daily_litres, latest_day, 7),
        "litres_per_day_30d": mean_30d,
        "days_used_30d": recent_daily_count(daily_litres, latest_day, 30),
        "typical_day_litres_30d": median_30d,
        "high_usage_days_30d": (
            sum(
                1
                for d, value in daily_litres.items()
                if latest_day - timedelta(days=29) <= d <= latest_day
                and limit is not None
                and value > limit
            )
        ),
        "outlier_limit_litres": limit,
        "latest_complete_day": latest_day,
        "days_used": len(daily_litres),
        "daily_litres": daily_litres,
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
        if any(b - a > TAU_MAX_GAP_S for a, b in zip(hours, hours[1:])):
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
        # The step geometry does not depend on tau, so build it once instead of
        # rebuilding it inside every candidate-tau iteration.
        steps = [
            (
                (hours[i] - hours[i - 1]) / 3600,
                (outdoor[hours[i - 1]] + outdoor[hours[i]]) / 2,
            )
            for i in range(1, len(hours))
        ]
        best_tau = None
        best_residual = float("inf")
        for quarter_hours in range(
            round(TAU_MIN_HOURS / TAU_SEARCH_STEP_H),
            round(TAU_MAX_HOURS / TAU_SEARCH_STEP_H) + 1,
        ):
            tau = quarter_hours * TAU_SEARCH_STEP_H
            model = temps[0]
            residual = 0.0
            for (elapsed, boundary), actual in zip(steps, temps[1:]):
                model = boundary + (model - boundary) * exp(-elapsed / tau)
                residual += (actual - model) ** 2
            if residual < best_residual:
                best_residual = residual
                best_tau = tau
        if best_tau is None or best_tau >= TAU_MAX_HOURS:
            # Pinned at the search bound: the room barely cooled, so the night
            # only bounds tau from below rather than measuring it. Letting the
            # bound itself into the median would report the search range as a
            # result.
            continue
        mean_temp = sum(temps) / len(temps)
        total = sum((value - mean_temp) ** 2 for value in temps)
        r2 = 1 - best_residual / total if total > 0 else 0.0
        if r2 < TAU_MIN_R2:
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
    water = series_from_stats(stats.get(conf["water"], []), "sum") if conf.get("water") else {}
    q_by_day = daily_gas_kwh(gas, tz)
    dt_by_day = daily_delta_t(all_rooms, outdoor, tz)
    outdoor_by_day = daily_mean(outdoor, tz)
    water_by_day = daily_water_litres(water, tz)
    heat_pct_by_day = daily_heating_pct(list(room_heat.values()), tz)
    # General rolling window for usage/context metrics. The heating model gets
    # a separate window below, anchored to the latest evidenced heating day so
    # summer data cannot move it.
    result["hlc"] = None
    since_full = (now - timedelta(days=windows_days[-1])).astimezone(tz).date()
    current_day = now.astimezone(tz).date()
    yesterday = current_day - timedelta(days=1)
    # Hourly statistics for the current day are necessarily incomplete.
    for by_day in (q_by_day, dt_by_day, outdoor_by_day, water_by_day, heat_pct_by_day):
        by_day.pop(current_day, None)

    heating_off = heating_off_days(dt_by_day, heat_pct_by_day)
    min_water_l = conf.get("min_dhw_water_litres") or DHW_OCCUPIED_MIN_WATER_L

    # Hot water: a robust heating-off gas baseline, used both as the headline
    # "hot water gas" cost figure and to strip DHW out of the winter HLC fit
    # below via the space_heating_hlc_w_per_k attribute. Per-day attribution:
    # on heating-off days ALL gas is hot water (electric hob, combi with no
    # cylinder); on heating days the water-meter rate models the DHW share
    # from that day's actual litres, falling back to the mains-temperature
    # scaling of the baseline where water history doesn't reach.
    result["dhw"] = None
    dhw_by_day: dict = {}
    dhw_quality = {
        "water_attribution_days": 0,
        "baseline_fallback_days": 0,
        "water_outlier_days_ignored": 0,
    }
    water_latest_day = max(water_by_day) if water_by_day else None
    water_limit_l = (
        water_outlier_limit_litres(water_by_day, water_latest_day)
        if water_latest_day
        else None
    )
    baseline = dhw_baseline(
        q_by_day, dt_by_day, outdoor_by_day, since_full,
        heating_off, water_by_day, min_water_l,
    )
    water_rate = fit_dhw_water_rate(
        q_by_day,
        water_by_day,
        outdoor_by_day,
        heating_off,
        since_full,
        min_water_l,
        water_limit_l,
    )
    if baseline:
        dhw_by_day, dhw_quality = attribute_dhw_by_day(
            q_by_day,
            outdoor_by_day,
            heating_off,
            baseline,
            water_by_day,
            water_rate,
            water_limit_l,
        )
        result["dhw"] = {
            "kwh_per_day": baseline["kwh_per_day"],
            "days_used": baseline["days_used"],
            "outdoor_mean": baseline["outdoor_mean"],
            "low_water_days_excluded": baseline["low_water_days_excluded"],
            "min_occupied_water_litres": min_water_l,
            "status": "valid" if baseline["days_used"] >= 14 else "provisional",
            "latest_complete_gas_day": max(q_by_day) if q_by_day else None,
            "latest_complete_water_day": water_latest_day,
            "water_source_lag_days": (
                (yesterday - water_latest_day).days if water_latest_day else None
            ),
            "water_outlier_limit_litres": water_limit_l,
            **dhw_quality,
        }
        if "idle_gas_kwh_per_day" in baseline:
            result["dhw"]["idle_gas_kwh_per_day"] = baseline["idle_gas_kwh_per_day"]
        if water_rate:
            result["dhw"]["water_rate_wh_per_litre_per_k"] = water_rate[
                "wh_per_litre_per_k"
            ]
            result["dhw"]["water_rate_days_used"] = water_rate["days_used"]
            result["dhw"]["water_rate_iqr_wh_per_litre_per_k"] = water_rate[
                "iqr_wh_per_litre_per_k"
            ]
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
            water_fit = fit_water_gas(
                gas,
                water,
                conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY,
            )
            if water_fit:
                result["dhw"].update(water_fit)

    # Rolling attributed usage: recent per-day gas split into hot water and
    # space heating, so both can be tracked over time as sensor history.
    result["usage"] = None
    if dhw_by_day:
        space_by_day = {
            d: max(q_by_day[d] - dhw, 0.0)
            for d, dhw in dhw_by_day.items()
            if d in q_by_day
        }
        usage = {
            "dhw_kwh_per_day_7d": recent_daily_mean(
                dhw_by_day, yesterday, 7, RECENT_7D_MIN_DAYS
            ),
            "dhw_kwh_per_day_30d": recent_daily_mean(
                dhw_by_day, yesterday, 30, RECENT_30D_MIN_DAYS
            ),
            "space_heating_kwh_per_day_7d": recent_daily_mean(
                space_by_day, yesterday, 7, RECENT_7D_MIN_DAYS
            ),
            "space_heating_kwh_per_day_30d": recent_daily_mean(
                space_by_day, yesterday, 30, RECENT_30D_MIN_DAYS
            ),
            "heating_off_days": sum(1 for d in dhw_by_day if d in heating_off),
            "modelled_days": sum(1 for d in dhw_by_day if d not in heating_off),
            "heating_off_from_power_days": sum(
                1 for d in heating_off if d in heat_pct_by_day
            ),
            **dhw_quality,
            "latest_complete_gas_day": max(q_by_day) if q_by_day else None,
            "latest_complete_water_day": water_latest_day,
        }
        gas_rate = conf.get("gas_unit_rate")
        if gas_rate:
            for key in ("dhw_kwh_per_day_7d", "space_heating_kwh_per_day_7d"):
                if usage[key] is not None:
                    usage[key.replace("kwh_per_day", "cost_per_day_gbp")] = (
                        usage[key] * gas_rate
                    )
        result["usage"] = usage

    # Total-water context: deliberately labelled as total water rather than
    # inferred hot-water litres. The source commonly arrives a few days late.
    result["water_usage"] = None
    result["water_status"] = {"configured": bool(conf.get("water"))}
    if conf.get("water"):
        water_summary = water_usage_summary(water, tz, since_full, current_day)
        if water_summary:
            water_summary.pop("daily_litres")
            water_summary["source_lag_days"] = (
                yesterday - water_summary["latest_complete_day"]
            ).days
            result["water_usage"] = water_summary
        else:
            result["water_status"]["usable_days"] = len(water_by_day)
            result["water_status"]["required_days"] = WATER_USAGE_MIN_DAYS

    # Electricity: descriptive only (daily use, current baseload, implied
    # internal gains) - kept out of the gas-side thermal fits on purpose.
    result["electricity"] = None
    result["electricity_status"] = {
        "configured": bool(conf.get("electricity_meter"))
    }
    if conf.get("electricity_meter"):
        elec = series_from_stats(stats.get(conf["electricity_meter"], []), "sum")
        summary = electricity_summary(elec, tz, since_full, current_day)
        if summary:
            elec_daily = summary.pop("daily_kwh")
            summary["last_7d_kwh_per_day"] = recent_daily_mean(
                elec_daily, summary["latest_complete_day"], 7, RECENT_7D_MIN_DAYS
            )
            summary["last_30d_kwh_per_day"] = recent_daily_mean(
                elec_daily, summary["latest_complete_day"], 30, RECENT_30D_MIN_DAYS
            )
            summary["days_used_7d"] = recent_daily_count(
                elec_daily, summary["latest_complete_day"], 7
            )
            summary["days_used_30d"] = recent_daily_count(
                elec_daily, summary["latest_complete_day"], 30
            )
            summary["previous_30d_kwh_per_day"] = previous_period_mean(
                elec_daily,
                summary["latest_complete_day"],
                30,
                RECENT_30D_MIN_DAYS,
            )
            current_30d = summary["last_30d_kwh_per_day"]
            previous_30d = summary["previous_30d_kwh_per_day"]
            summary["change_vs_previous_30d_pct"] = (
                (current_30d - previous_30d) / previous_30d * 100
                if current_30d is not None and previous_30d
                else None
            )
            summary["source_lag_days"] = (
                yesterday - summary["latest_complete_day"]
            ).days
            elec_rate = conf.get("electricity_unit_rate")
            if elec_rate:
                summary["cost_per_day_gbp"] = summary["kwh_per_day"] * elec_rate
                summary["cost_per_year_gbp"] = summary["kwh_per_day"] * elec_rate * 365
                if summary["last_30d_kwh_per_day"] is not None:
                    summary["cost_per_day_gbp_30d"] = (
                        summary["last_30d_kwh_per_day"] * elec_rate
                    )
                    summary["cost_per_year_gbp_30d"] = (
                        summary["last_30d_kwh_per_day"] * elec_rate * 365
                    )
                summary["baseload_cost_per_year_gbp"] = (
                    summary["baseload_kwh_per_day"] * elec_rate * 365
                )
            result["electricity"] = summary
        else:
            elec_days = _daily_meter_steps(elec, tz, ELEC_MAX_STEP_KWH)
            result["electricity_status"].update(
                {"usable_days": len(elec_days), "required_days": ELEC_MIN_DAYS}
            )

    # Freeze the primary heating model at its latest evidenced heating day.
    # Both ends of the HLC window and the DHW model used to correct it are
    # anchored there. Summer hot-water observations can continue improving
    # the live DHW sensors above without changing the heating baseline.
    model_day = latest_heating_day(q_by_day, dt_by_day, heat_pct_by_day, since_full)
    hlc_since_full = (
        model_day - timedelta(days=windows_days[-1]) if model_day else since_full
    )
    hlc_dhw_by_day: dict = {}
    hlc_baseline = None
    if model_day:
        locked_q = {d: v for d, v in q_by_day.items() if d <= model_day}
        locked_dt = {d: v for d, v in dt_by_day.items() if d <= model_day}
        locked_outdoor = {d: v for d, v in outdoor_by_day.items() if d <= model_day}
        locked_water = {d: v for d, v in water_by_day.items() if d <= model_day}
        locked_off = {d for d in heating_off if d <= model_day}
        locked_water_limit = water_outlier_limit_litres(locked_water, model_day)
        hlc_baseline = dhw_baseline(
            locked_q,
            locked_dt,
            locked_outdoor,
            hlc_since_full,
            locked_off,
            locked_water,
            min_water_l,
        )
        locked_water_rate = fit_dhw_water_rate(
            locked_q,
            locked_water,
            locked_outdoor,
            locked_off,
            hlc_since_full,
            min_water_l,
            locked_water_limit,
        )
        if hlc_baseline:
            hlc_dhw_by_day, _ = attribute_dhw_by_day(
                locked_q,
                locked_outdoor,
                locked_off,
                hlc_baseline,
                locked_water,
                locked_water_rate,
                locked_water_limit,
                model_day,
            )

    fit = fit_hlc(q_by_day, dt_by_day, hlc_since_full, until=model_day)
    if fit:
        boiler_eff = conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY
        gas_side_fit = fit
        corrected = (
            fit_hlc(
                q_by_day,
                dt_by_day,
                hlc_since_full,
                hlc_dhw_by_day,
                model_day,
            )
            if hlc_dhw_by_day
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
            "residual_autocorrelation": space_heating_fit["residual_autocorrelation"],
            "effective_independent_days": space_heating_fit[
                "effective_independent_days"
            ],
            # The regression runs on gas, so its intercept is fuel input. Free
            # gains are a heat flow, and every other heat figure here is
            # delivered heat - so report them in that frame (x boiler
            # efficiency), which is also the frame electricity's
            # implied_internal_gains_w is already in, making the two
            # comparable. The raw fuel-side value stays available alongside.
            "free_gains_kwh_per_day": (
                space_heating_fit["free_gains_kwh_per_day"] * boiler_eff
            ),
            "free_gains_w": (
                space_heating_fit["free_gains_kwh_per_day"] * boiler_eff * 1000 / 24
            ),
            "fuel_input_free_gains_kwh_per_day": space_heating_fit[
                "free_gains_kwh_per_day"
            ],
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
            "model_data_through": model_day,
            "summer_stability": "held until a new qualifying heating day",
            "dhw_baseline_kwh_per_day": (
                hlc_baseline["kwh_per_day"] if hlc_baseline else None
            ),
            "dhw_baseline_days_used": (
                hlc_baseline["days_used"] if hlc_baseline else None
            ),
        }
        floor_area = conf.get("floor_area_m2")
        if floor_area:
            result["hlc"]["hlc_w_per_k_per_m2"] = delivered_hlc / floor_area
        if corrected:
            result["hlc"]["space_heating_hlc_w_per_k"] = delivered_hlc
            result["hlc"]["space_heating_r_squared"] = corrected["r_squared"]
        if model_day:
            for window in windows_days[:-1]:
                since = model_day - timedelta(days=window)
                recent = fit_hlc(
                    q_by_day,
                    dt_by_day,
                    since,
                    hlc_dhw_by_day if hlc_dhw_by_day else None,
                    model_day,
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
