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
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone, tzinfo
import inspect
from math import exp, isfinite, log, sqrt
import re
from statistics import median
from typing import Literal, TypedDict

Series = dict[int, float]


class EvidenceExclusion(TypedDict, total=False):
    """One structured reason a daily source record was not model eligible."""

    code: str
    room: str


class DailyEligibility(TypedDict):
    hlc: bool
    dhw: bool


class DailyAnalysisEvidence(TypedDict):
    date: str
    gas_kwh: float | None
    delta_t_k: float | None
    strict_delta_t_k: float | None
    outdoor_c: float | None
    water_litres: float | None
    heating_pct: float | None
    heating_pct_lower_bound: float | None
    temperature_short_gap_hours: int
    temperature_interpolated_hours: int
    heating_missing_hours: int
    eligible: DailyEligibility
    exclusions: list[EvidenceExclusion]
    hlc_dhw_kwh: float | None
    hlc_adjusted_kwh: float | None
    hlc_included: bool


class AnalysisEvidenceEnvelope(TypedDict):
    schema_version: Literal[1]
    generated_at: str
    timezone: str
    model_version: str
    history_revision: int | str | None
    configuration: dict[str, object]
    daily: list[DailyAnalysisEvidence]
    metrics: dict[str, object]
    uncertainty: dict[str, str]
    models: dict[str, object]


ANALYSIS_EVIDENCE_SCHEMA_VERSION = 1
ANALYSIS_MODEL_VERSION = "2"

HLC_MAX_TEMPERATURE_GAP_SECONDS = 120
HLC_MAX_TEMPERATURE_GAP_HOURS_PER_DAY = 2
HLC_MAX_INTERPOLATION_GAP_SECONDS = 3 * 3600
HLC_MAX_INTERPOLATED_HOURS_PER_DAY = 2
HLC_MAX_HEATING_MISSING_HOURS_PER_DAY = 2

GAS_MAX_STEP_KWH = 40.0  # bigger hourly steps are meter/statistics artifacts
HLC_MIN_DT = 4.0
HLC_MIN_Q = 0.5
HLC_MIN_DAYS = 30  # for the shorter-window "recent" estimate
HLC_FALLBACK_MIN_DAYS = 20
HLC_SOURCE_STALE_DAYS = 14
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
# Broad CO2 analysis acceptance bounds (analytics suitability policy, not universal physical/safety limits)
CO2_MIN_PPM = 300.0
CO2_MAX_PPM = 10000.0
CO2_MIN_WINDOW_H = 4.0
CO2_MIN_DROP_PPM = 120.0
CO2_MIN_R2 = 0.9
# Role-aware historical input sanitization bounds
TEMP_MIN_C = -90.0
TEMP_MAX_C = 80.0
PERCENT_MIN = 0.0
PERCENT_MAX = 100.0
TARIFF_MIN_GBP_PER_KWH = 0.0
TARIFF_MAX_GBP_PER_KWH = 10.0
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
DHW_RATE_MAX_IQR_WH_PER_L_PER_K = 0.4
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
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isfinite(value):
            continue
        start = row["start"]
        if isinstance(start, datetime):
            ts = start.timestamp()
        else:
            ts = float(start)
            if ts > 1e12:  # milliseconds (websocket-style payloads)
                ts /= 1000.0
        out[int(ts)] = value
    return out


def is_valid_numeric_value(val: object, min_val: float, max_val: float) -> bool:
    """Validate that val is a finite numeric float within [min_val, max_val] inclusive.

    Rejects None, non-numeric strings, NaN, Inf, and out-of-bounds values.
    """
    if val is None:
        return False
    try:
        v = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
    return isfinite(v) and min_val <= v <= max_val


def is_valid_temperature_value(val: object) -> bool:
    """Accept broad physical/analytics temperature bounds -90..80 °C inclusive."""
    return is_valid_numeric_value(val, TEMP_MIN_C, TEMP_MAX_C)


def is_valid_humidity_value(val: object) -> bool:
    """Accept relative humidity bounds 0..100 % inclusive."""
    return is_valid_numeric_value(val, PERCENT_MIN, PERCENT_MAX)


def is_valid_heating_demand_value(val: object) -> bool:
    """Accept heating power/demand bounds 0..100 % inclusive."""
    return is_valid_numeric_value(val, PERCENT_MIN, PERCENT_MAX)


def is_valid_co2_value(val: object) -> bool:
    """Accept broad analytics suitability bounds 300..10000 ppm inclusive.

    Analytics suitability policy, NOT universal physical or safety limits.
    Rejects non-finite, NaN, Inf, negative, zero, and out-of-bounds sentinels.
    """
    return is_valid_numeric_value(val, CO2_MIN_PPM, CO2_MAX_PPM)


def coerce_tariff_rate(val: object) -> float | None:
    """Safely coerce and validate a tariff rate in normalized GBP/kWh (0..10 inclusive).

    Accepts normalized GBP/kWh 0..10 inclusive (broad analytics policy).
    Rejects nonfinite, malformed, negative, or excessive values rather than crash.
    Zero is valid. Returns float or None.
    """
    if val is None:
        return None
    try:
        v = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if isfinite(v) and TARIFF_MIN_GBP_PER_KWH <= v <= TARIFF_MAX_GBP_PER_KWH:
        return v
    return None


def clean_series_from_stats(
    rows: list[dict],
    validator: Callable[[object], bool],
    kind: str = "mean",
) -> tuple[Series, int]:
    """Convert recorder statistics rows to {epoch_seconds: value}.

    Validates target kind (default 'mean') AND any supplied extrema (min, max)
    against validator. Rejects the entire hourly bucket if target value or any
    supplied extrema are outside bounds or non-finite.
    Missing extrema are handled compatibly for legacy/synthetic/mean-only data.

    Returns:
        tuple of (Series, excluded_bucket_count)
    """
    out: Series = {}
    excluded_count = 0
    for row in rows:
        target_val = row.get(kind)
        min_val = row.get("min")
        max_val = row.get("max")

        if target_val is None and min_val is None and max_val is None:
            continue

        if not validator(target_val):
            excluded_count += 1
            continue

        if min_val is not None and not validator(min_val):
            excluded_count += 1
            continue

        if max_val is not None and not validator(max_val):
            excluded_count += 1
            continue

        start = row.get("start")
        if start is None:
            excluded_count += 1
            continue
        if isinstance(start, datetime):
            ts = start.timestamp()
        else:
            try:
                ts = float(start)
            except (TypeError, ValueError, OverflowError):
                excluded_count += 1
                continue
            if not isfinite(ts):
                excluded_count += 1
                continue
            if ts > 1e12:  # milliseconds (websocket-style payloads)
                ts /= 1000.0
        out[int(ts)] = float(target_val)
    return out, excluded_count


def co2_series_from_stats(rows: list[dict]) -> tuple[Series, int]:
    """Convert recorder statistics rows to {epoch_seconds: mean_value} for CO2.

    Validates the hourly bucket against broad analytics suitability bounds (300..10000 ppm).
    Rejects the entire hourly bucket if mean or any supplied extrema (min, max) are outside
    these bounds or non-finite (e.g. brief 99999 sentinel diluted into an otherwise acceptable mean).
    Missing extrema are handled compatibly for legacy/synthetic/mean-only data.

    Returns:
        tuple of (Series, excluded_bucket_count)
    """
    return clean_series_from_stats(rows, is_valid_co2_value, kind="mean")


def temperature_series_from_stats(rows: list[dict]) -> tuple[Series, int]:
    """Convert recorder statistics rows to {epoch_seconds: mean_value} for temperature (-90..80 °C)."""
    return clean_series_from_stats(rows, is_valid_temperature_value, kind="mean")


def humidity_series_from_stats(rows: list[dict]) -> tuple[Series, int]:
    """Convert recorder statistics rows to {epoch_seconds: mean_value} for humidity (0..100%)."""
    return clean_series_from_stats(rows, is_valid_humidity_value, kind="mean")


def heating_power_series_from_stats(rows: list[dict]) -> tuple[Series, int]:
    """Convert recorder statistics rows to {epoch_seconds: mean_value} for heating power (0..100%)."""
    return clean_series_from_stats(rows, is_valid_heating_demand_value, kind="mean")


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
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return round((end.timestamp() - start.timestamp()) / 3600)


def _daily_meter_steps(cumulative: Series, tz: tzinfo, max_step: float) -> dict:
    """Hourly deltas grouped by complete local dates on the exact 3600s grid.

    HA statistics rows are timestamped at row.start (00:00, 01:00, ..., 23:00).
    For each complete local date:
      - Deltas are computed at current row.start 00..23: t1 in [daystart, dayend - 3600].
      - Baseline t0 for the daystart (00:00) delta is previousday 23:00 (daystart - 3600).
      - Exact 3600s cadence requirement: t1 - t0 == 3600.
      - Deltas group directly under _local(t1, tz).date().
    """
    if not cumulative or len(cumulative) < 2:
        return {}

    dates = {
        _local(ts, tz).date()
        for ts in cumulative
    }

    complete_days = {}
    for day in sorted(dates):
        daystart = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        dayend = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        expected_hours = round((dayend.timestamp() - daystart.timestamp()) / 3600)
        start_ts = int(daystart.timestamp())

        day_steps = []
        is_complete = True
        for h in range(expected_hours):
            t1 = start_ts + h * 3600
            t0 = t1 - 3600
            if t0 not in cumulative or t1 not in cumulative:
                is_complete = False
                break
            step = cumulative[t1] - cumulative[t0]
            if not (0 <= step <= max_step):
                is_complete = False
                break
            day_steps.append(step)

        if is_complete and len(day_steps) == expected_hours:
            complete_days[day] = day_steps

    return complete_days


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
    """Local-date mean dT with a fixed, complete room population on the exact 3600s grid."""
    if not outdoor or not rooms:
        return {}
    dates = {_local(ts, tz).date() for ts in outdoor}
    result = {}
    for day in sorted(dates):
        daystart = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        dayend = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        expected_hours = round((dayend.timestamp() - daystart.timestamp()) / 3600)
        start_ts = int(daystart.timestamp())

        day_dts = []
        is_complete = True
        for h in range(expected_hours):
            ts = start_ts + h * 3600
            if ts not in outdoor:
                is_complete = False
                break
            temps = [room[ts] for room in rooms if ts in room]
            if len(temps) != len(rooms):
                is_complete = False
                break
            day_dts.append(sum(temps) / len(temps) - outdoor[ts])

        if is_complete and len(day_dts) == expected_hours:
            result[day] = sum(day_dts) / expected_hours
    return result


def daily_mean(series: Series, tz: tzinfo) -> dict:
    """Local-date mean of an hourly series over complete 23/24/25-hour days on the exact 3600s grid."""
    if not series:
        return {}
    dates = {_local(ts, tz).date() for ts in series}
    result = {}
    for day in sorted(dates):
        daystart = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        dayend = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        expected_hours = round((dayend.timestamp() - daystart.timestamp()) / 3600)
        start_ts = int(daystart.timestamp())

        vals = []
        is_complete = True
        for h in range(expected_hours):
            ts = start_ts + h * 3600
            if ts not in series:
                is_complete = False
                break
            vals.append(series[ts])

        if is_complete and len(vals) == expected_hours:
            result[day] = sum(vals) / expected_hours
    return result


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
    heating_off: set | None = None,
    diagnostics: dict | None = None,
    tz: tzinfo | None = None,
) -> dict | None:
    days = sorted(
        d
        for d in q_by_day
        if d in dt_by_day and d >= since and (until is None or d <= until)
    )
    pairs = []
    for d in days:
        if heating_off is not None and d in heating_off:
            continue
        if dt_by_day[d] <= HLC_MIN_DT or q_by_day[d] <= HLC_MIN_Q:
            continue
        adjusted = q_by_day[d] - (dhw_by_day.get(d, 0.0) if dhw_by_day else 0.0)
        if adjusted <= 0:
            continue
        if tz is not None:
            day_h = _expected_local_day_hours(d, tz)
            adjusted_24h = adjusted * (24.0 / day_h)
        else:
            adjusted_24h = adjusted
        pairs.append((d, dt_by_day[d], adjusted_24h))

    if diagnostics is not None:
        diagnostics["usable_days"] = len(pairs)
        diagnostics["required_days"] = HLC_FALLBACK_MIN_DAYS
        diagnostics["fit_rows"] = [
            {
                "date": day.isoformat(),
                "delta_t_k": delta_t,
                "adjusted_kwh": adjusted_24h,
            }
            for day, delta_t, adjusted_24h in pairs
        ]

    if len(pairs) < HLC_FALLBACK_MIN_DAYS:
        if diagnostics is not None:
            diagnostics["status"] = "collecting"
            diagnostics["reason"] = (
                f"fewer than {HLC_FALLBACK_MIN_DAYS} usable heating days (have {len(pairs)})"
            )
        return None
    fit_days = [p[0] for p in pairs]
    xs = [p[1] for p in pairs]
    ys = [p[2] for p in pairs]
    dt_spread = max(xs) - min(xs)
    if diagnostics is not None:
        diagnostics["dt_spread"] = dt_spread
    if dt_spread < HLC_MIN_DT_SPREAD:
        if diagnostics is not None:
            diagnostics["status"] = "rejected"
            diagnostics["reason"] = (
                f"insufficient indoor-outdoor temperature spread ({dt_spread:.1f}K < {HLC_MIN_DT_SPREAD}K)"
            )
        return None
    slope, intercept, r2 = linear_fit(xs, ys)
    hlc = slope * 1000 / 24
    if slope <= 0 or r2 < HLC_MIN_R2 or not HLC_MIN_W_PER_K <= hlc <= HLC_MAX_W_PER_K:
        if diagnostics is not None:
            diagnostics["status"] = "rejected"
            diagnostics["reason"] = "HLC fit failed physical or statistical quality gates"
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
        if diagnostics is not None:
            diagnostics["status"] = "rejected"
            diagnostics["reason"] = "confidence interval lower bound is non-positive"
        return None
    baseline_days = [
        q_by_day[d] for d in q_by_day
        if d >= since
        and (until is None or d <= until)
        and d in dt_by_day
        and dt_by_day[d] < DHW_BASELINE_MAX_DT
    ]
    res = {
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
    if diagnostics is not None:
        diagnostics["status"] = res["status"]
        diagnostics["reason"] = "heat loss coefficient model successfully fitted"
    return res


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
    diagnostics: dict | None = None,
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
            or not isfinite(litres)
            or litres <= 0
            or litres < min_water_l
            or (max_water_l is not None and litres > max_water_l)
        ):
            continue
        rise = MAINS_TANK_TEMP_C - mains_temp_c(outdoor_by_day[d])
        if rise <= 0:
            continue
        samples.append(q * 1000 / litres / rise)
    if diagnostics is not None:
        diagnostics["usable_observations"] = len(samples)
        diagnostics["required_observations"] = DHW_WATER_MIN_DAYS
    if len(samples) < DHW_WATER_MIN_DAYS:
        if diagnostics is not None:
            diagnostics.update(
                status="collecting",
                reason=f"fewer than {DHW_WATER_MIN_DAYS} positive-water heating-off days",
            )
        return None
    rate = median(samples)
    if not DHW_RATE_MIN_WH_PER_L_PER_K <= rate <= DHW_RATE_MAX_WH_PER_L_PER_K:
        if diagnostics is not None:
            diagnostics.update(
                status="rejected",
                reason="water energy-per-litre estimate failed physical limits",
            )
        return None
    ordered = sorted(samples)
    iqr = ordered[(3 * len(ordered)) // 4] - ordered[len(ordered) // 4]
    if iqr > DHW_RATE_MAX_IQR_WH_PER_L_PER_K:
        if diagnostics is not None:
            diagnostics.update(
                status="rejected",
                reason="water energy-per-litre estimates vary too widely between days",
            )
        return None
    if diagnostics is not None:
        diagnostics.update(status="valid", reason="water energy-per-litre estimate fitted")
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
    clamp_days = 0
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
            estimate = dhw_kwh_from_water(litres, outdoor_by_day[d], water_rate)
            if estimate > q:
                clamp_days += 1
            attributed[d] = min(estimate, q)
            quality["water_attribution_days"] += 1
        elif d in outdoor_by_day:
            estimate = dhw_daily_kwh(outdoor_by_day[d], baseline)
            if estimate > q:
                clamp_days += 1
            attributed[d] = min(estimate, q)
            quality["baseline_fallback_days"] += 1
    if clamp_days:
        quality["dhw_clamp_days"] = clamp_days
    return attributed, quality


def fit_water_gas(
    gas_sum: Series,
    water_sum: Series,
    boiler_efficiency: float = DEFAULT_BOILER_EFFICIENCY,
    heating_off: set | None = None,
    tz: tzinfo | None = None,
    since=None,
    until=None,
    diagnostics: dict | None = None,
) -> dict | None:
    """Informational only: hourly gas-vs-water regression on heating-off intervals,
    giving a rough Wh-per-litre rate and the implied hot fraction of metered water. Noisy
    (household draws are bursty and mix hot/cold) - not the basis for the
    DHW cost figure, which comes from dhw_baseline instead.
    """
    gas_hourly = hourly_change(gas_sum, GAS_MAX_STEP_KWH)
    water_hourly = hourly_change(water_sum, WATER_MAX_STEP_L)
    common = sorted(set(gas_hourly) & set(water_hourly))
    if not common:
        if diagnostics is not None:
            diagnostics.update(
                status="collecting",
                reason="no overlapping hourly gas and water readings",
                eligible_observations=0,
                required_observations=DHW_REGRESSION_MIN_HOURS,
                rejected_heating_on_observations=0,
                rejected_outside_window_observations=0,
            )
        return None

    eligible = []
    rejected_heating_on = 0
    rejected_outside_window = 0
    for ts in common:
        if tz is not None and (heating_off is not None or since is not None or until is not None):
            day = _local(ts, tz).date()
            if since is not None and day < since:
                rejected_outside_window += 1
                continue
            if until is not None and day > until:
                rejected_outside_window += 1
                continue
            if heating_off is not None and day not in heating_off:
                rejected_heating_on += 1
                continue
        eligible.append(ts)

    if len(eligible) < DHW_REGRESSION_MIN_HOURS:
        if diagnostics is not None:
            diagnostics.update(
                status="collecting",
                reason=f"fewer than {DHW_REGRESSION_MIN_HOURS} eligible overlapping hours",
                eligible_observations=len(eligible),
                required_observations=DHW_REGRESSION_MIN_HOURS,
                rejected_heating_on_observations=rejected_heating_on,
                rejected_outside_window_observations=rejected_outside_window,
                total_common_observations=len(common),
            )
        return None
    slope, _, r2 = linear_fit(
        [water_hourly[ts] for ts in eligible], [gas_hourly[ts] for ts in eligible]
    )
    if slope <= 0 or r2 < MIN_WATER_REGRESSION_R2:
        if diagnostics is not None:
            diagnostics.update(
                status="rejected",
                reason="hourly gas/water regression failed slope or fit-quality limits",
                eligible_observations=len(eligible),
                required_observations=DHW_REGRESSION_MIN_HOURS,
                rejected_heating_on_observations=rejected_heating_on,
                rejected_outside_window_observations=rejected_outside_window,
                total_common_observations=len(common),
            )
        return None
    wh_per_litre = slope * 1000
    hot_fraction_pct = (
        wh_per_litre * boiler_efficiency / DHW_THEORETICAL_WH_PER_L * 100
    )
    # Reject physically inconsistent hot fractions instead of clipping to 100
    if hot_fraction_pct > 100.0 or hot_fraction_pct < 0.0:
        if diagnostics is not None:
            diagnostics.update(
                status="rejected",
                reason=(
                    f"implied hot-water fraction {hot_fraction_pct:.1f}% is outside 0-100%"
                ),
                eligible_observations=len(eligible),
                required_observations=DHW_REGRESSION_MIN_HOURS,
                rejected_heating_on_observations=rejected_heating_on,
                rejected_outside_window_observations=rejected_outside_window,
                total_common_observations=len(common),
            )
        return None

    if diagnostics is not None:
        diagnostics.update(
            status="valid",
            reason="hourly gas/water regression passed physical and fit-quality checks",
            eligible_observations=len(eligible),
            required_observations=DHW_REGRESSION_MIN_HOURS,
            rejected_heating_on_observations=rejected_heating_on,
            rejected_outside_window_observations=rejected_outside_window,
            total_common_observations=len(common),
        )

    return {
        "wh_per_litre": wh_per_litre,
        "fuel_input_wh_per_litre": wh_per_litre,
        "hot_fraction_pct": hot_fraction_pct,
        "regression_r_squared": r2,
        "regression_hours": len(eligible),
        "eligible_hours": len(eligible),
        "rejected_heating_on_hours": rejected_heating_on,
        "rejected_outside_window_hours": rejected_outside_window,
        "total_common_hours": len(common),
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
    room: Series, outdoor: Series, heating: Series | None, tz: tzinfo, since,
    expected_intervals: list[dict] | None = None,
    diagnostics: dict | None = None,
) -> list[dict]:
    """One exponential-decay fit per usable night."""
    nights: dict = defaultdict(list)
    for ts in sorted(room):
        local = _local(ts, tz)
        if local.hour in TAU_NIGHT_HOURS and local.date() >= since:
            nights[local.date()].append(ts)

    fits = []
    rejection_counts: dict[str, int] = defaultdict(int)

    def reject(code: str) -> None:
        rejection_counts[code] += 1

    for date, hours in nights.items():
        if len(hours) < TAU_MIN_POINTS:
            reject("too_few_points")
            continue
        if any(b - a > TAU_MAX_GAP_S for a, b in zip(hours, hours[1:])):
            reject("excessive_gap")
            continue
        if expected_intervals is None:
            expected_hours = hours
        else:
            expected_hours = [
                ts for ts in hours
                if any(
                    (v.get("start") is not None and ts >= v["start"])
                    and (v.get("end") is None or ts + 3600 <= v["end"])
                    for v in expected_intervals
                )
            ]
        if heating is not None and expected_hours:
            h_vals = [heating[ts] for ts in expected_hours if ts in heating]
            # Missing/rejected demand cannot establish a free cooldown.
            if len(h_vals) != len(expected_hours) or max(h_vals) > TAU_MAX_HEATING_PCT:
                reject("heating_not_confirmed_off")
                continue
        if any(ts not in outdoor for ts in hours):
            reject("outdoor_coverage")
            continue
        temps = [room[ts] for ts in hours]
        excess = [room[ts] - outdoor[ts] for ts in hours]
        if min(excess) < TAU_MIN_DT:
            reject("insufficient_temperature_difference")
            continue
        if temps[0] - temps[-1] < TAU_MIN_DROP:
            reject("insufficient_temperature_drop")
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
            reject("search_bound")
            continue
        mean_temp = sum(temps) / len(temps)
        total = sum((value - mean_temp) ** 2 for value in temps)
        r2 = 1 - best_residual / total if total > 0 else 0.0
        if r2 < TAU_MIN_R2:
            reject("poor_fit")
            continue
        fits.append({"date": str(date), "tau_hours": best_tau, "r_squared": r2})
    if diagnostics is not None:
        diagnostics.update({
            "candidate_nights": len(nights),
            "accepted_nights": len(fits),
            "rejected_nights": sum(rejection_counts.values()),
            "rejection_counts": dict(rejection_counts),
        })
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
        temps = [room[ts] for room in rooms if ts in room and room[ts] is not None]
        if t_out is None or not rooms or len(temps) != len(rooms):
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
    if outdoor_baseline_ppm is not None:
        if not is_valid_co2_value(outdoor_baseline_ppm):
            return None
        baseline = float(outdoor_baseline_ppm)
    else:
        valid_values = [
            float(v)
            for ts, v in co2.items()
            if _local(ts, tz).date() >= since and is_valid_co2_value(v)
        ]
        if len(valid_values) < 2:
            return None
        baseline = _percentile(valid_values, CO2_BASELINE_PCT)
        if not is_valid_co2_value(baseline):
            return None

    raw_items = sorted((ts, v) for ts, v in co2.items() if _local(ts, tz).date() >= since)
    if len(raw_items) < 2:
        return None

    # Coerce valid entries to float, preserving invalid items as barriers (None)
    items: list[tuple[int, float | None]] = [
        (ts, float(v) if is_valid_co2_value(v) else None)
        for ts, v in raw_items
    ]

    fits = []
    i, n = 0, len(items)
    while i < n - 1:
        if items[i][1] is None:
            i += 1
            continue
        j = i
        while (
            j + 1 < n
            and items[j + 1][1] is not None
            and items[j + 1][0] - items[j][0] <= CO2_MAX_GAP_S
            and items[j + 1][1] <= items[j][1] + 2  # tolerate small sensor noise
        ):
            j += 1
        window: list[tuple[int, float]] = [
            (ts, v) for ts, v in items[i : j + 1] if v is not None
        ]
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
    heating_power_issues = dict(
        conf.get("invalid_heating_power_entities") or {}
    )
    invalid_heating_power = set(heating_power_issues)
    room_temp: dict[str, Series] = {}
    hlc_room_temp: dict[str, Series] = {}
    room_heat: dict[str, Series] = {}
    numeric_issues: dict[str, str] = {}

    def ingest(source_id, validator, label, kind="mean") -> Series:
        if not source_id:
            return {}
        values, rejected = clean_series_from_stats(
            stats.get(source_id, []), validator, kind
        )
        if rejected:
            numeric_issues[source_id] = (
                f"{rejected} historical {label} bucket(s) were invalid or "
                "outside the analysis range and were ignored"
            )
        return values

    for name, spec in room_confs.items():
        temp_entity = spec.get("temperature")
        room_temp[name] = ingest(temp_entity, is_valid_temperature_value, "temperature")
        hlc_source = spec.get("hlc_temperature") if conf.get("hlc_allow_short_gaps") else None
        hlc_room_temp[name] = (
            ingest(hlc_source, is_valid_temperature_value, "temperature")
            if hlc_source and hlc_source != temp_entity else room_temp[name]
        )
        # Humidity is currently contextual rather than a model regressor, but
        # expose source quality even when no loft ratio can be calculated.
        ingest(spec.get("humidity"), is_valid_humidity_value, "humidity")
        heating_entity = spec.get("heating_power")
        if heating_entity:
            if heating_entity in invalid_heating_power:
                room_heat[name] = {}
                continue
            room_heat[name], invalid_points = heating_power_series_from_stats(
                stats.get(heating_entity, [])
            )
            if invalid_points:
                heating_power_issues[heating_entity] = (
                    f"{invalid_points} historical observation(s) were outside "
                    "the finite 0-100% range and were ignored"
                )
                numeric_issues[heating_entity] = heating_power_issues[heating_entity]
    outdoor = ingest(conf.get("outdoor"), is_valid_temperature_value, "temperature")
    loft_values = ingest(conf.get("loft"), is_valid_temperature_value, "temperature")
    loft_humidity = ingest(conf.get("loft_humidity"), is_valid_humidity_value, "humidity")
    all_rooms = list(room_temp.values())

    result: dict = {
        "rooms": {},
        "source_issues": numeric_issues,
        "heating_power_issues": heating_power_issues,
        "invalid_heating_power_entities": dict(
            conf.get("invalid_heating_power_entities") or {}
        ),
    }

    # Recorder sums can have large or negative baseline offsets. Validate
    # finiteness here; physical plausibility belongs to the existing delta gates.
    finite_meter = lambda value: is_valid_numeric_value(value, -float("inf"), float("inf"))
    gas = ingest(conf.get("gas_meter"), finite_meter, "gas", "sum")
    water = ingest(conf.get("water"), finite_meter, "water", "sum")
    for role in ("gas_unit_rate", "electricity_unit_rate"):
        if conf.get(role) is not None and coerce_tariff_rate(conf[role]) is None:
            numeric_issues[role] = "Tariff must be finite and within 0-10 GBP/kWh"
    raw_q_by_day = daily_gas_kwh(gas, tz)
    raw_dt_by_day = daily_delta_t(list(hlc_room_temp.values()), outdoor, tz)
    strict_dt_by_day = daily_delta_t(all_rooms, outdoor, tz)
    room_source_issues = conf.get("room_source_issues") or {}
    has_room_temperature_issue = any(
        issues.get("temperature")
        for issues in room_source_issues.values()
        if isinstance(issues, dict)
    )
    if has_room_temperature_issue:
        # Keep the configured room population fixed. An invalid room source
        # cannot be silently dropped to make a smaller home appear complete.
        raw_dt_by_day = {}
        strict_dt_by_day = {}
    raw_outdoor_by_day = daily_mean(outdoor, tz)
    raw_water_by_day = daily_water_litres(water, tz)
    raw_room_temp_by_day = {
        name: daily_mean(temps, tz) for name, temps in hlc_room_temp.items()
    }
    raw_heat_pct_by_day = daily_heating_pct(list(room_heat.values()), tz)

    # Keep source-complete dates immutable. Transition and coverage exclusions
    # are applied only to the per-model maps below, so gas freshness and daily
    # evidence still describe the meter itself.
    raw_observed_days = set(raw_q_by_day) | set(raw_dt_by_day) | set(raw_outdoor_by_day) | set(raw_water_by_day) | set(raw_heat_pct_by_day)
    for source in (gas, water, outdoor, *room_temp.values(), *room_heat.values()):
        raw_observed_days.update(_local(ts, tz).date() for ts in source)

    expected_heating_gap_days: set[date] = set()
    expected_heating_gap_rooms: dict[date, set[str]] = defaultdict(set)
    partial_heating_on: dict[date, int] = {}
    has_expected_heating = any(
        "heating_expected_intervals" in spec for spec in room_confs.values()
    )
    if has_expected_heating:
        # Dated expectations distinguish a missing heating bucket from evidence
        # that the system was off. Build this independently of gas and
        # temperature availability so each missing source keeps its own reason.
        expected_dates = set(raw_observed_days)
        earliest_observed = min(expected_dates) if expected_dates else None
        latest_observed = max(expected_dates) if expected_dates else None
        for spec in room_confs.values():
            for interval in spec.get("heating_expected_intervals", []):
                start = interval.get("start")
                if start is None:
                    continue
                first_day = _local(int(start), tz).date()
                end = interval.get("end")
                last_day = (
                    _local(int(end) - 1, tz).date()
                    if end is not None
                    else latest_observed
                )
                if (
                    earliest_observed is not None
                    and last_day is not None
                    and last_day >= first_day
                ):
                    first_day = max(first_day, earliest_observed)
                    last_day = min(last_day, latest_observed)
                if last_day is not None and last_day >= first_day:
                    expected_dates.update(
                        first_day + timedelta(days=offset)
                        for offset in range((last_day - first_day).days + 1)
                    )
        raw_observed_days.update(expected_dates)

        raw_heat_pct_by_day = {}
        for day in sorted(expected_dates):
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            expected_hours = round((day_end.timestamp() - day_start.timestamp()) / 3600)
            required_slots: list[tuple[int, list[str]]] = []
            for hour in range(expected_hours):
                ts = int(day_start.timestamp()) + hour * 3600
                expected_rooms = [
                    name
                    for name, spec in room_confs.items()
                    if any(
                        interval.get("start") is not None
                        and ts >= interval["start"]
                        and (interval.get("end") is None or ts + 3600 <= interval["end"])
                        for interval in spec.get("heating_expected_intervals", [])
                    )
                ]
                if expected_rooms:
                    required_slots.append((ts, expected_rooms))
            if not required_slots:
                continue
            observations = []
            missing_hours = 0
            for ts, expected_rooms in required_slots:
                missing_rooms = [
                    name for name in expected_rooms
                    if ts not in room_heat.get(name, {})
                ]
                if missing_rooms:
                    expected_heating_gap_rooms[day].update(missing_rooms)
                    missing_hours += 1
                    continue
                observations.append(max(room_heat[name][ts] for name in expected_rooms))
            if expected_heating_gap_rooms.get(day) or len(observations) != len(required_slots):
                # Missing demand is never interpreted as heating off. When at
                # most two hours are unknown, observed non-negative demand can
                # nevertheless prove that the full-day mean exceeds the off
                # threshold even if every missing hour were zero.
                lower_bound = sum(observations) / expected_hours
                if (
                    conf.get("hlc_allow_short_gaps")
                    and 0 < missing_hours <= HLC_MAX_HEATING_MISSING_HOURS_PER_DAY
                    and lower_bound > HEATING_OFF_MAX_PCT
                ):
                    raw_heat_pct_by_day[day] = lower_bound
                    partial_heating_on[day] = missing_hours
                    expected_heating_gap_rooms.pop(day, None)
                else:
                    expected_heating_gap_days.add(day)
            else:
                raw_heat_pct_by_day[day] = sum(observations) / len(observations)

    excluded_dates: set[date] = set()
    for value in conf.get("excluded_model_days", []):
        try:
            excluded_dates.add(
                value.date() if isinstance(value, datetime)
                else value if isinstance(value, date)
                else date.fromisoformat(str(value))
            )
        except (TypeError, ValueError):
            continue
    excluded_rooms_by_day: dict[date, set[str]] = defaultdict(set)
    for name, spec in room_confs.items():
        for value in spec.get("excluded_model_days", []):
            try:
                day = (
                    value.date() if isinstance(value, datetime)
                    else value if isinstance(value, date)
                    else date.fromisoformat(str(value))
                )
                excluded_rooms_by_day[day].add(name)
            except (TypeError, ValueError):
                continue

    # The current local day is incomplete by definition and remains outside
    # every complete-day source map and metric window.
    current_day = now.astimezone(tz).date()
    yesterday = current_day - timedelta(days=1)
    for by_day in (
        raw_q_by_day,
        raw_dt_by_day,
        strict_dt_by_day,
        raw_outdoor_by_day,
        raw_water_by_day,
        raw_heat_pct_by_day,
    ):
        by_day.pop(current_day, None)
    raw_observed_days.discard(current_day)
    expected_heating_gap_days.discard(current_day)
    excluded_dates.discard(current_day)

    model_excluded_dates = excluded_dates | expected_heating_gap_days
    # Room cooling fits keep their prior per-room transition exclusions.
    excluded_days = {day.isoformat() for day in model_excluded_dates}
    q_by_day = {d: value for d, value in raw_q_by_day.items() if d not in model_excluded_dates}
    dt_by_day = {d: value for d, value in raw_dt_by_day.items() if d not in model_excluded_dates}
    strict_model_dt_by_day = {d: value for d, value in strict_dt_by_day.items() if d not in model_excluded_dates}
    outdoor_by_day = dict(raw_outdoor_by_day)
    water_by_day = dict(raw_water_by_day)
    heat_pct_by_day = {d: value for d, value in raw_heat_pct_by_day.items() if d not in model_excluded_dates}
    # General rolling window for usage/context metrics. The heating model gets
    # a separate window below, anchored to the latest evidenced heating day so
    # summer data cannot move it.
    result["hlc"] = None
    since_full = (now - timedelta(days=windows_days[-1])).astimezone(tz).date()

    heating_off = heating_off_days(strict_model_dt_by_day, heat_pct_by_day)
    if heating_power_issues:
        # An invalid configured demand source cannot be replaced by the dT
        # proxy to claim heating was off.
        heating_off.intersection_update(heat_pct_by_day)
    configured_min_water_l = conf.get("min_dhw_water_litres")
    min_water_l = (
        DHW_OCCUPIED_MIN_WATER_L
        if configured_min_water_l is None
        else float(configured_min_water_l)
    )

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
        "dhw_clamp_days": 0,
    }
    water_latest_day = max(water_by_day) if water_by_day else None
    water_limit_l = (
        water_outlier_limit_litres(water_by_day, water_latest_day)
        if water_latest_day
        else None
    )
    baseline = dhw_baseline(
        q_by_day, strict_model_dt_by_day, outdoor_by_day, since_full,
        heating_off, water_by_day, min_water_l,
    )
    water_rate_diagnostics: dict = {}
    water_rate = fit_dhw_water_rate(
        q_by_day,
        water_by_day,
        outdoor_by_day,
        heating_off,
        since_full,
        min_water_l,
        water_limit_l,
        diagnostics=water_rate_diagnostics,
    ) if conf.get("water") else None
    if not conf.get("water"):
        water_rate_diagnostics = {
            "status": "not_configured",
            "reason": "water meter not configured; DHW attribution uses the gas baseline",
            "usable_observations": 0,
            "required_observations": DHW_WATER_MIN_DAYS,
        }
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
        dhw_quality = {"dhw_clamp_days": 0, **dhw_quality}
        result["dhw"] = {
            "kwh_per_day": baseline["kwh_per_day"],
            "days_used": baseline["days_used"],
            "outdoor_mean": baseline["outdoor_mean"],
            "low_water_days_excluded": baseline["low_water_days_excluded"],
            "min_occupied_water_litres": min_water_l,
            "status": "valid" if baseline["days_used"] >= 14 else "provisional",
            "latest_complete_gas_day": max(raw_q_by_day) if raw_q_by_day else None,
            "latest_eligible_gas_day": max(dhw_by_day) if dhw_by_day else None,
            "latest_complete_water_day": max(raw_water_by_day) if raw_water_by_day else None,
            "water_source_lag_days": (
                (yesterday - max(raw_water_by_day)).days if raw_water_by_day else None
            ),
            "water_outlier_limit_litres": water_limit_l,
            "water_rate_status": water_rate_diagnostics,
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
        modelled_daily = list(dhw_by_day.values())
        annual_kwh = (
            sum(modelled_daily) / len(modelled_daily) * 365
            if modelled_daily
            else baseline["kwh_per_day"] * 365
        )
        result["dhw"]["modelled_annual_kwh"] = annual_kwh
        gas_rate = coerce_tariff_rate(conf.get("gas_unit_rate"))
        if gas_rate is not None:
            result["dhw"]["cost_per_day_gbp"] = baseline["kwh_per_day"] * gas_rate
            result["dhw"]["cost_per_year_gbp"] = annual_kwh * gas_rate
        regression_diagnostics: dict = {}
        if conf.get("water"):
            water_fit = fit_water_gas(
                gas,
                water,
                conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY,
                heating_off=heating_off,
                tz=tz,
                since=since_full,
                until=yesterday,
                diagnostics=regression_diagnostics,
            )
            if water_fit:
                result["dhw"].update(water_fit)
        else:
            regression_diagnostics = {
                "status": "not_configured",
                "reason": "water meter not configured; hourly gas/water regression unavailable",
                "eligible_observations": 0,
                "required_observations": DHW_REGRESSION_MIN_HOURS,
            }
        result["dhw"]["water_gas_regression_status"] = regression_diagnostics

    # Rolling attributed usage: recent per-day gas split into hot water and
    # space heating, so both can be tracked over time as sensor history.
    result["usage"] = None
    if dhw_by_day:
        space_by_day = {
            d: max(q_by_day[d] - dhw, 0.0)
            for d, dhw in dhw_by_day.items()
            if d in q_by_day
        }
        dhw_latest_day = max(dhw_by_day) if dhw_by_day else None
        space_latest_day = max(space_by_day) if space_by_day else None
        usage = {
            "dhw_kwh_per_day_7d": (
                recent_daily_mean(dhw_by_day, dhw_latest_day, 7, RECENT_7D_MIN_DAYS)
                if dhw_latest_day
                else None
            ),
            "dhw_kwh_per_day_30d": (
                recent_daily_mean(dhw_by_day, dhw_latest_day, 30, RECENT_30D_MIN_DAYS)
                if dhw_latest_day
                else None
            ),
            "space_heating_kwh_per_day_7d": (
                recent_daily_mean(
                    space_by_day, space_latest_day, 7, RECENT_7D_MIN_DAYS
                )
                if space_latest_day
                else None
            ),
            "space_heating_kwh_per_day_30d": (
                recent_daily_mean(
                    space_by_day, space_latest_day, 30, RECENT_30D_MIN_DAYS
                )
                if space_latest_day
                else None
            ),
            "heating_off_days": sum(1 for d in dhw_by_day if d in heating_off),
            "modelled_days": sum(1 for d in dhw_by_day if d not in heating_off),
            "heating_off_from_power_days": sum(
                1 for d in heating_off if d in heat_pct_by_day
            ),
            **dhw_quality,
            "latest_complete_gas_day": max(raw_q_by_day) if raw_q_by_day else None,
            "latest_eligible_gas_day": max(dhw_by_day) if dhw_by_day else None,
            "latest_complete_water_day": max(raw_water_by_day) if raw_water_by_day else None,
            "source_lag_days": (
                (yesterday - dhw_latest_day).days if dhw_latest_day else None
            ),
            "raw_source_lag_days": (
                (yesterday - max(raw_q_by_day)).days if raw_q_by_day else None
            ),
        }
        gas_rate = coerce_tariff_rate(conf.get("gas_unit_rate"))
        if gas_rate is not None:
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
        elec = ingest(conf.get("electricity_meter"), finite_meter, "electricity", "sum")
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
            elec_rate = coerce_tariff_rate(conf.get("electricity_unit_rate"))
            if elec_rate is not None:
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
        locked_dt = {d: v for d, v in strict_model_dt_by_day.items() if d <= model_day}
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

    fit = None
    selected_hlc_fit_rows: list[dict] = []
    selected_hlc_dhw_by_day: dict = {}
    hlc_diagnostics: dict = {}
    if model_day:
        fit = fit_hlc(
            q_by_day,
            dt_by_day,
            hlc_since_full,
            until=model_day,
            heating_off=locked_off,
            diagnostics=hlc_diagnostics,
            tz=tz,
        )
    else:
        hlc_diagnostics["status"] = "collecting"
        if not q_by_day:
            hlc_diagnostics["reason"] = (
                "no complete daily gas meter observations in analysis window"
            )
        elif not dt_by_day:
            hlc_diagnostics["reason"] = (
                "no complete aligned room and outdoor temperature days in analysis window"
            )
        else:
            hlc_diagnostics["reason"] = (
                "no qualifying heating days observed in analysis window"
            )
    if fit:
        boiler_eff = conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY
        gas_side_fit = fit
        corrected_diagnostics: dict = {}
        corrected = (
            fit_hlc(
                q_by_day,
                dt_by_day,
                hlc_since_full,
                hlc_dhw_by_day,
                model_day,
                heating_off=locked_off,
                diagnostics=corrected_diagnostics,
                tz=tz,
            )
            if hlc_dhw_by_day
            else None
        )
        space_heating_fit = corrected or gas_side_fit
        selected_fit_rows = (
            corrected_diagnostics.get("fit_rows", [])
            if corrected
            else hlc_diagnostics.get("fit_rows", [])
        )
        selected_hlc_fit_rows = selected_fit_rows
        selected_hlc_dhw_by_day = hlc_dhw_by_day if corrected else {}
        selected_fit_days = [date.fromisoformat(row["date"]) for row in selected_fit_rows]
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
            "model_data_from": min(selected_fit_days) if selected_fit_days else None,
            "model_data_through": model_day,
            "fit_data_through": max(selected_fit_days) if selected_fit_days else None,
            "summer_stability": "held until a new qualifying heating day",
            "dhw_baseline_kwh_per_day": (
                hlc_baseline["kwh_per_day"] if hlc_baseline else None
            ),
            "dhw_baseline_days_used": (
                hlc_baseline["days_used"] if hlc_baseline else None
            ),
        }
        sensitivity_low_eff = max(0.5, boiler_eff - 0.05)
        sensitivity_high_eff = min(1.0, boiler_eff + 0.05)
        sensitivity_fuel_hlc = space_heating_fit["hlc_w_per_k"]
        sensitivity = {
            "efficiency_bounds": [0.5, 1.0],
            "bounds": [0.5, 1.0],
            "low_efficiency": sensitivity_low_eff,
            "high_efficiency": sensitivity_high_eff,
            "low_hlc_w_per_k": sensitivity_fuel_hlc * sensitivity_low_eff,
            "high_hlc_w_per_k": sensitivity_fuel_hlc * sensitivity_high_eff,
            "delta_low_w_per_k": sensitivity_fuel_hlc * sensitivity_low_eff - delivered_hlc,
            "delta_high_w_per_k": sensitivity_fuel_hlc * sensitivity_high_eff - delivered_hlc,
        }
        result["hlc"]["boiler_efficiency_sensitivity"] = sensitivity
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
                    heating_off=locked_off,
                    tz=tz,
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
    result["air_change_rate"] = None
    result["losses"] = None
    result["losses_status"] = None
    ach_fit = None
    if conf.get("co2"):
        configured_co2 = conf["co2"]
        co2_ids = [configured_co2] if isinstance(configured_co2, str) else configured_co2
        outdoor_baseline = conf.get("outdoor_co2_ppm")
        if outdoor_baseline is not None and not is_valid_co2_value(outdoor_baseline):
            outdoor_baseline = None
        outdoor_sensor_used = False
        if conf.get("outdoor_co2_sensor"):
            outdoor_sensor_id = conf["outdoor_co2_sensor"]
            outdoor_co2, outdoor_rejected = co2_series_from_stats(
                stats.get(outdoor_sensor_id, [])
            )
            if outdoor_rejected:
                msg = (
                    f"{outdoor_rejected} historical observation(s) were outside "
                    "the 300-10000 ppm range and were ignored"
                )
                result.setdefault("source_issues", {})[outdoor_sensor_id] = msg
            outdoor_values = [
                value
                for ts, value in outdoor_co2.items()
                if _local(ts, tz).date() >= since_full
            ]
            if outdoor_values:
                cand_baseline = median(outdoor_values)
                if is_valid_co2_value(cand_baseline):
                    outdoor_baseline = cand_baseline
                    outdoor_sensor_used = True
        ach_fits = []
        for co2_id in co2_ids:
            co2, co2_rejected = co2_series_from_stats(stats.get(co2_id, []))
            if co2_rejected:
                msg = (
                    f"{co2_rejected} historical observation(s) were outside "
                    "the 300-10000 ppm range and were ignored"
                )
                result.setdefault("source_issues", {})[co2_id] = msg
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
                and is_valid_co2_value(conf.get("outdoor_co2_ppm"))
                else "indoor low-percentile fallback"
            )
            ach_fit["co2_sensors_used"] = ach_fit["sensor_count"]
            ach_fit["co2_baseline_source"] = ach_fit["baseline_source"]
            ach_fit["scope"] = (
                f"median of {ach_fit['sensor_count']} room-derived ACH proxies "
                "scaled to configured home volume"
            ) if conf.get("floor_area_m2") and conf.get("ceiling_height_m") else (
                f"median of {ach_fit['sensor_count']} room-derived ACH proxies"
            )
            result["air_change_rate"] = ach_fit

    opt_in_losses = bool(conf.get("experimental_whole_home_ventilation", False))
    if (
        opt_in_losses
        and ach_fit
        and conf.get("floor_area_m2")
        and conf.get("ceiling_height_m")
        and result["hlc"]
    ):
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
                    "experimental whole-home estimate assuming uniform infiltration "
                    "scaled to configured home volume"
                ),
                "assumptions": (
                    "single-zone uniform air change rate derived from room CO2 proxy; "
                    "actual infiltration varies by room and wind conditions; "
                    "not a categorical retrofit recommendation"
                ),
            }
            result["losses_status"] = "consistent"
        else:
            result["losses"] = None
            result["losses_status"] = {
                "status": "inconsistent",
                "calculated_ventilation_w_per_k": ventilation_w_per_k,
                "hlc_delivered_w_per_k": hlc_delivered,
                "diagnostic_note": (
                    "physically inconsistent ventilation/fabric split: "
                    f"calculated ventilation loss ({ventilation_w_per_k:.1f} W/K) "
                    f"exceeds delivered HLC ({hlc_delivered:.1f} W/K)"
                ),
            }
    else:
        result["losses"] = None
        result["losses_status"] = {"status": "experimental_disabled"} if not opt_in_losses else None

    result["room_fit_counts"] = {}
    result["room_fit_quality"] = {}
    for name, temps in room_temp.items():
        result["rooms"][name] = None
        room_issues = room_source_issues.get(name) or {}
        if room_issues.get("temperature"):
            continue
        room_excluded = set(room_confs[name].get("excluded_model_days") if "excluded_model_days" in room_confs[name] else excluded_days)
        tau_diagnostics: dict = {}
        for window in windows_days:
            since = (now - timedelta(days=window)).astimezone(tz).date()
            safe_temps = {ts: value for ts, value in temps.items()
                          if _local(ts, tz).date().isoformat() not in room_excluded}
            tau_args = (
                safe_temps,
                outdoor,
                room_heat.get(name),
                tz,
                since,
                room_confs[name].get("heating_expected_intervals"),
            )
            if "diagnostics" in inspect.signature(night_taus).parameters:
                fits = night_taus(*tau_args, diagnostics=tau_diagnostics)
            else:
                # A small number of callers replace the pure fitter with a
                # six-argument test double. Keep that seam compatible while
                # reporting the observations it explicitly returned.
                fits = night_taus(*tau_args)
                tau_diagnostics.update({
                    "candidate_nights": len(fits),
                    "accepted_nights": len(fits),
                    "rejected_nights": 0,
                    "rejection_counts": {},
                })
            result["room_fit_counts"][name] = len(fits)
            if len(fits) >= TAU_MIN_NIGHTS:
                taus = sorted(f["tau_hours"] for f in fits)
                first_night = fits[0]["date"]
                last_night = fits[-1]["date"]
                result["rooms"][name] = {
                    "tau_median_h": median(taus),
                    "tau_q1_h": _percentile(taus, 0.25),
                    "tau_q3_h": _percentile(taus, 0.75),
                    "nights_fitted": len(fits),
                    "first_night": first_night,
                    "last_night": last_night,
                    "model_data_through": last_night,
                    "usable_observations": len(fits),
                    "required_observations": TAU_MIN_NIGHTS,
                    "sensor_count": 1,
                    "temperature_sensor_count": 1,
                    "heating_sensor_count": int(bool(room_confs[name].get("heating_power"))),
                    "candidate_nights": tau_diagnostics.get("candidate_nights", 0),
                    "rejected_nights": tau_diagnostics.get("rejected_nights", 0),
                    "rejection_counts": tau_diagnostics.get("rejection_counts", {}),
                    "latest_source_day": max(
                        (_local(ts, tz).date() for ts in temps), default=None
                    ),
                    "latest_temperature_day": max(
                        (_local(ts, tz).date() for ts in temps), default=None
                    ),
                    "latest_heating_day": max(
                        (_local(ts, tz).date() for ts in room_heat.get(name, {})),
                        default=None,
                    ),
                    "latest_demand_day": max(
                        (_local(ts, tz).date() for ts in room_heat.get(name, {})),
                        default=None,
                    ),
                    "window_days": window,
                }
                break
        result["room_fit_quality"][name] = {
            "candidate_nights": tau_diagnostics.get("candidate_nights", 0),
            "rejected_nights": tau_diagnostics.get("rejected_nights", 0),
            "rejection_counts": tau_diagnostics.get("rejection_counts", {}),
            "latest_source_day": max(
                (_local(ts, tz).date() for ts in temps), default=None
            ),
            "latest_temperature_day": max(
                (_local(ts, tz).date() for ts in temps), default=None
            ),
            "latest_heating_day": max(
                (_local(ts, tz).date() for ts in room_heat.get(name, {})),
                default=None,
            ),
            "latest_demand_day": max(
                (_local(ts, tz).date() for ts in room_heat.get(name, {})),
                default=None,
            ),
        }

    # Loft ratio needs cold nights, so use the full window; drop_flatlines
    # keeps a dead sensor's frozen value from poisoning the median. loft_since
    # additionally guards against a sensor that was relocated into the loft -
    # its history from before the move belongs to wherever it used to live,
    # not the loft, and won't necessarily flatline so drop_flatlines alone
    # can't catch it.
    result["loft"] = None
    if conf.get("loft") and not room_source_issues.get("loft", {}).get("temperature"):
        loft = drop_flatlines(loft_values)
        loft_since = conf.get("loft_since")
        loft_cutoff = max(since_full, loft_since) if loft_since else since_full
        ratio = loft_ratio(all_rooms, loft, outdoor, tz, loft_cutoff)
        if ratio:
            result["loft"] = ratio | {"window_days": windows_days[-1]}
            if conf.get("loft_humidity"):
                humidity = {
                    ts: v for ts, v in loft_humidity.items()
                    if _local(ts, tz).date() >= loft_cutoff
                }
                if humidity:
                    result["loft"]["humidity_pct"] = humidity[max(humidity)]

    result["analysis_status"] = build_analysis_status(
        result,
        conf,
        tz,
        now,
        raw_q_by_day,
        raw_water_by_day,
        model_day,
        hlc_diagnostics,
        since_full=since_full,
        heating_off=heating_off,
        dt_by_day=raw_dt_by_day,
        strict_dt_by_day=strict_dt_by_day,
        eligible_q_by_day=q_by_day,
        eligible_dt_by_day=dt_by_day,
        eligible_hlc_days={
            day for day in set(q_by_day) & set(dt_by_day)
            if day >= hlc_since_full
        },
        eligible_dhw_by_day={
            day for day in q_by_day
            if day >= since_full
            and (day in heating_off or day in outdoor_by_day)
            and (day in heat_pct_by_day or day in dt_by_day)
        },
        eligibility_exclusion_days={
            "expected_heating_gap": expected_heating_gap_days,
            "assignment_transition": excluded_dates,
        },
    )

    room_aliases = {
        name: f"room_{index}"
        for index, name in enumerate(sorted(room_confs), start=1)
    }
    fit_rows_by_day = {
        date.fromisoformat(row["date"]): row
        for row in selected_hlc_fit_rows
    }
    temperature_gap_hours_by_day: dict[date, int] = defaultdict(int)
    temperature_interpolated_hours_by_day: dict[date, int] = defaultdict(int)
    if conf.get("hlc_allow_short_gaps"):
        for name, spec in room_confs.items():
            for ts in spec.get("hlc_tolerated_temperature_hours", []):
                if ts in hlc_room_temp.get(name, {}) and ts not in room_temp.get(name, {}):
                    temperature_gap_hours_by_day[_local(ts, tz).date()] += 1
            for ts in spec.get("hlc_interpolated_temperature_hours", []):
                if ts in hlc_room_temp.get(name, {}) and ts not in room_temp.get(name, {}):
                    temperature_interpolated_hours_by_day[_local(ts, tz).date()] += 1
    evidence_dates = sorted(
        raw_observed_days
        | set(raw_q_by_day)
        | set(raw_dt_by_day)
        | set(raw_outdoor_by_day)
        | set(raw_water_by_day)
        | set(raw_heat_pct_by_day)
        | expected_heating_gap_days
        | excluded_dates
    )
    daily_evidence: list[DailyAnalysisEvidence] = []
    hlc_window_start = hlc_since_full if model_day is not None else since_full
    for day in evidence_dates:
        exclusions: list[EvidenceExclusion] = []
        exclusion_keys: set[tuple[str, str | None]] = set()

        def add_exclusion(code: str, room_name: str | None = None) -> None:
            room_alias = room_aliases.get(room_name) if room_name else None
            key = (code, room_alias)
            if key in exclusion_keys:
                return
            exclusion_keys.add(key)
            record: EvidenceExclusion = {"code": code}
            if room_alias is not None:
                record["room"] = room_alias
            exclusions.append(record)

        if day not in raw_q_by_day:
            add_exclusion("meter_gap")
        if day not in raw_dt_by_day or day not in raw_outdoor_by_day:
            missing_rooms = [
                name for name in room_confs
                if day not in raw_room_temp_by_day.get(name, {})
                or (room_source_issues.get(name) or {}).get("temperature")
            ]
            if missing_rooms:
                for name in missing_rooms:
                    add_exclusion("temperature_coverage", name)
            else:
                add_exclusion("temperature_coverage")
        for name in sorted(expected_heating_gap_rooms.get(day, set())):
            add_exclusion("expected_heating_gap", name)
        if day in expected_heating_gap_days and not expected_heating_gap_rooms.get(day):
            add_exclusion("expected_heating_gap")
        for name in sorted(excluded_rooms_by_day.get(day, set())):
            add_exclusion("assignment_transition", name)
        if day in excluded_dates and not excluded_rooms_by_day.get(day):
            add_exclusion("assignment_transition")

        day_model_sources_complete = (
            day in q_by_day
            and day in dt_by_day
            and day not in expected_heating_gap_days
            and day not in excluded_dates
        )
        dhw_day_eligible = (
            day >= since_full
            and day in q_by_day
            and (day in heating_off or day in outdoor_by_day)
            and (day in heat_pct_by_day or day in dt_by_day)
        )
        hlc_day_eligible = (
            day_model_sources_complete
            and day >= hlc_window_start
        )

        included_row = fit_rows_by_day.get(day)
        hlc_dhw_kwh = None
        if (
            model_day is not None
            and hlc_window_start <= day <= model_day
            and day in q_by_day
            and day in dt_by_day
        ):
            hlc_dhw_kwh = selected_hlc_dhw_by_day.get(day, 0.0)
        adjusted_kwh = included_row["adjusted_kwh"] if included_row else None
        if not included_row:
            if day in excluded_dates:
                add_exclusion("hlc_assignment_transition")
            elif day in expected_heating_gap_days:
                add_exclusion("hlc_expected_heating_gap")
            elif day not in raw_q_by_day:
                add_exclusion("hlc_meter_gap")
            elif day not in raw_dt_by_day:
                add_exclusion("hlc_temperature_coverage")
            elif model_day is None:
                add_exclusion("hlc_no_model_anchor")
            elif day < hlc_window_start:
                add_exclusion("hlc_outside_model_window")
            elif day > model_day:
                add_exclusion("hlc_after_model_anchor")
            elif day in heating_off:
                add_exclusion("hlc_heating_off")
            elif raw_dt_by_day.get(day, 0.0) <= HLC_MIN_DT:
                add_exclusion("hlc_min_dt")
            elif raw_q_by_day.get(day, 0.0) <= HLC_MIN_Q:
                add_exclusion("hlc_min_q")
            elif raw_q_by_day[day] - (hlc_dhw_kwh or 0.0) <= 0:
                add_exclusion("hlc_negative_adjusted")
            elif day in q_by_day and day in dt_by_day:
                add_exclusion("hlc_not_selected")

        daily_evidence.append({
            "date": day.isoformat(),
            "gas_kwh": raw_q_by_day.get(day),
            "delta_t_k": raw_dt_by_day.get(day),
            "strict_delta_t_k": strict_dt_by_day.get(day),
            "outdoor_c": raw_outdoor_by_day.get(day),
            "water_litres": raw_water_by_day.get(day),
            "heating_pct": None if day in partial_heating_on else raw_heat_pct_by_day.get(day),
            "heating_pct_lower_bound": raw_heat_pct_by_day.get(day) if day in partial_heating_on else None,
            "temperature_short_gap_hours": temperature_gap_hours_by_day.get(day, 0),
            "temperature_interpolated_hours": temperature_interpolated_hours_by_day.get(day, 0),
            "heating_missing_hours": partial_heating_on.get(day, 0),
            "eligible": {"hlc": bool(hlc_day_eligible), "dhw": bool(dhw_day_eligible)},
            "exclusions": exclusions,
            "hlc_dhw_kwh": hlc_dhw_kwh,
            "hlc_adjusted_kwh": adjusted_kwh,
            "hlc_included": included_row is not None,
        })

    exclusion_counts: dict[str, int] = defaultdict(int)
    for daily_record in daily_evidence:
        for exclusion in daily_record["exclusions"]:
            exclusion_counts[exclusion["code"]] += 1
    result["analysis_status"]["hlc"]["exclusion_counts"] = dict(exclusion_counts)
    result["analysis_status"]["hlc"]["eligible_days"] = sum(
        1 for record in daily_evidence if record["eligible"]["hlc"]
    )
    included_days = set(fit_rows_by_day)
    result["analysis_status"]["hlc"]["temperature_short_gap_days"] = len(
        included_days & set(temperature_gap_hours_by_day)
    )
    result["analysis_status"]["hlc"]["temperature_interpolated_days"] = len(
        included_days & set(temperature_interpolated_hours_by_day)
    )
    result["analysis_status"]["hlc"]["heating_on_with_missing_hours_days"] = len(
        included_days & set(partial_heating_on)
    )
    result["analysis_status"]["hlc"]["coverage_note"] = (
        "Short temperature gaps use the recorded partial-hour mean; limited "
        "longer gaps are interpolated. Heating with missing hours is accepted "
        "only when observed demand proves it was on."
    )
    result["analysis_status"]["dhw"]["eligible_days"] = sum(
        1 for record in daily_evidence if record["eligible"]["dhw"]
    )

    analysis_configuration = _analysis_evidence_configuration(
        conf, windows_days, model_day, hlc_window_start
    )
    result["analysis_evidence"] = _make_analysis_evidence(
        result, conf, tz, now, analysis_configuration, daily_evidence
    )
    return result


def _iso_date_str(d: date | datetime | str | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def _check_source_issue(conf: dict, *keys: str | None) -> str | None:
    source_issues = conf.get("source_issues") or {}
    for k in keys:
        if not k:
            continue
        if k in source_issues:
            return str(source_issues[k])
        entity = conf.get(k)
        if isinstance(entity, str) and entity in source_issues:
            return str(source_issues[entity])
    return None


def _room_source_issue(conf: dict, room: str, *roles: str) -> str | None:
    issues = (conf.get("room_source_issues") or {}).get(room, {})
    if not isinstance(issues, dict):
        return None
    for role in roles:
        issue = issues.get(role)
        if issue:
            return str(issue)
    return None


def _json_safe(value):
    """Convert calculation values to a JSON-compatible tree."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            _json_safe(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, float) and not isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _entity_ids_in_config(conf: dict) -> list[str]:
    """Return configured source identifiers for evidence-text redaction."""
    identifiers: set[str] = set()
    for key in (
        "gas_meter", "outdoor", "water", "electricity_meter", "loft",
        "loft_humidity", "outdoor_co2_sensor",
    ):
        value = conf.get(key)
        if isinstance(value, str) and value:
            identifiers.add(value)
    co2 = conf.get("co2")
    if isinstance(co2, str):
        identifiers.add(co2)
    elif isinstance(co2, (list, tuple)):
        identifiers.update(item for item in co2 if isinstance(item, str) and item)
    for spec in (conf.get("rooms") or {}).values():
        if isinstance(spec, dict):
            identifiers.update(
                value for key, value in spec.items()
                if key in ("temperature", "heating_power", "humidity")
                and isinstance(value, str) and value
            )
    for key in (conf.get("source_issues") or {}):
        if isinstance(key, str) and ("." in key or ":" in key):
            identifiers.add(key)
    for key in (conf.get("invalid_heating_power_entities") or {}):
        if isinstance(key, str):
            identifiers.add(key)
    return sorted(identifiers, key=lambda item: (-len(item), item))


def _redact_evidence_metrics(metrics: dict, conf: dict) -> dict:
    """Copy status evidence while replacing configured source and room names."""
    safe_metrics = deepcopy(metrics)
    room_names = sorted((conf.get("rooms") or {}).keys())
    room_aliases = {
        name: f"room_{index}"
        for index, name in enumerate(room_names, start=1)
    }
    room_text_aliases = dict(room_aliases)
    for room_id, spec in (conf.get("rooms") or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("name"), str):
            room_text_aliases[spec["name"]] = room_aliases[room_id]
    room_status = safe_metrics.get("rooms")
    if isinstance(room_status, dict):
        safe_metrics["rooms"] = {
            room_aliases.get(name, f"room_{index}"): item
            for index, (name, item) in enumerate(room_status.items(), start=1)
        }

    source_aliases = {
        identifier: f"source_{index}"
        for index, identifier in enumerate(_entity_ids_in_config(conf), start=1)
    }
    replacements = sorted(
        [*source_aliases.items(), *room_text_aliases.items()],
        key=lambda pair: (-len(pair[0]), pair[0]),
    )

    human_text_fields = {"reason", "next_action", "note", "summary", "diagnostic_note", "guidance"}

    def redact(value, field=None):
        if isinstance(value, str):
            # Statuses, dates and other machine values are not user prose. A
            # room called 'valid' must not change the meaning of a status enum.
            if field not in human_text_fields:
                return value
            text = value
            for original, alias in replacements:
                if not original:
                    continue
                text = re.sub(
                    rf"(?<![\w]){re.escape(original)}(?![\w])",
                    alias,
                    text,
                )
            return text
        if isinstance(value, dict):
            return {key: redact(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item, field) for item in value]
        return value

    return redact(_json_safe(safe_metrics))


def _analysis_evidence_configuration(
    conf: dict,
    windows_days: tuple[int, ...],
    model_day: date | None,
    hlc_since: date,
) -> dict[str, object]:
    room_aliases = {
        name: f"room_{index}"
        for index, name in enumerate(sorted((conf.get("rooms") or {})), start=1)
    }
    boiler_efficiency = conf.get("boiler_efficiency") or DEFAULT_BOILER_EFFICIENCY
    min_water_l = conf.get("min_dhw_water_litres")
    return {
        "boiler_efficiency": boiler_efficiency,
        "boiler_efficiency_used": boiler_efficiency,
        "minimum_occupied_water_litres": (
            DHW_OCCUPIED_MIN_WATER_L if min_water_l is None else min_water_l
        ),
        "floor_area_m2": conf.get("floor_area_m2"),
        "ceiling_height_m": conf.get("ceiling_height_m"),
        "model_window_days": windows_days[-1],
        "model_window_from": hlc_since.isoformat(),
        "model_anchor_day": _iso_date_str(model_day),
        "coverage_policy": {
            "enabled": bool(conf.get("hlc_allow_short_gaps")),
            "max_partial_temperature_gap_seconds": HLC_MAX_TEMPERATURE_GAP_SECONDS,
            "max_partial_temperature_hours_per_room_day": HLC_MAX_TEMPERATURE_GAP_HOURS_PER_DAY,
            "max_temperature_interpolation_gap_seconds": HLC_MAX_INTERPOLATION_GAP_SECONDS,
            "max_interpolated_hours_per_room_day": HLC_MAX_INTERPOLATED_HOURS_PER_DAY,
            "max_missing_heating_hours_per_day": HLC_MAX_HEATING_MISSING_HOURS_PER_DAY,
            "heating_off_requires_complete_expected_coverage": True,
        },
        "rooms": {
            room_aliases[name]: {
                "temperature_configured": bool(spec.get("temperature")),
                "heating_power_configured": bool(spec.get("heating_power")),
                "heating_expectations_configured": (
                    "heating_expected_intervals" in spec
                ),
            }
            for name, spec in (conf.get("rooms") or {}).items()
        },
        "hlc_fit_gates": {
            "minimum_dt_k": HLC_MIN_DT,
            "minimum_gas_kwh_per_day": HLC_MIN_Q,
            "minimum_fit_days": HLC_FALLBACK_MIN_DAYS,
            "valid_days_for_full_status": HLC_MIN_DAYS,
            "minimum_r_squared": HLC_MIN_R2,
            "minimum_dt_spread_k": HLC_MIN_DT_SPREAD,
            "minimum_hlc_w_per_k": HLC_MIN_W_PER_K,
            "maximum_hlc_w_per_k": HLC_MAX_W_PER_K,
        },
    }


def _make_analysis_evidence(
    result: dict,
    conf: dict,
    tz: tzinfo,
    now: datetime,
    configuration: dict[str, object],
    daily: list[DailyAnalysisEvidence],
) -> AnalysisEvidenceEnvelope:
    return {
        "schema_version": ANALYSIS_EVIDENCE_SCHEMA_VERSION,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        # Dates and targets already use HA's local-day/DST convention. Replay
        # does not need its location-bearing IANA zone identifier.
        "timezone": "local",
        "model_version": ANALYSIS_MODEL_VERSION,
        "history_revision": _json_safe(conf.get("history_revision")),
        "configuration": _json_safe(configuration),
        "daily": daily,
        "metrics": _redact_evidence_metrics(result.get("analysis_status", {}), conf),
        "uncertainty": {
            "hlc": (
                "conditional regression confidence interval; excludes boiler-efficiency, "
                "DHW attribution and sensor systematic uncertainty"
            ),
        },
        "models": {"hlc": _json_safe(result.get("hlc"))},
    }


def build_analysis_status(
    result: dict,
    conf: dict,
    tz: tzinfo,
    now: datetime,
    q_by_day: dict,
    water_by_day: dict,
    model_day: date | None,
    hlc_diagnostics: dict | None = None,
    since_full: date | None = None,
    heating_off: set | None = None,
    dt_by_day: dict | None = None,
    strict_dt_by_day: dict | None = None,
    eligible_q_by_day: dict | None = None,
    eligible_dt_by_day: dict | None = None,
    eligible_hlc_days: set | None = None,
    eligible_dhw_by_day: dict | None = None,
    eligibility_exclusion_days: dict[str, set] | None = None,
) -> dict:
    yesterday = now.astimezone(tz).date() - timedelta(days=1)
    status: dict = {}

    # --- HLC ---
    room_temp_issue = next(
        (
            issue
            for issues in (conf.get("room_source_issues") or {}).values()
            if isinstance(issues, dict)
            for issue in [issues.get("temperature")]
            if issue
        ),
        None,
    )
    hlc_issue = _check_source_issue(conf, "gas_meter", conf.get("gas_meter"), "outdoor", conf.get("outdoor")) or room_temp_issue
    latest_gas_day = max(q_by_day) if q_by_day else None
    gas_lag = (yesterday - latest_gas_day).days if latest_gas_day else None
    latest_temperature_day = max(dt_by_day) if dt_by_day else None
    strict_temperature = dt_by_day if strict_dt_by_day is None else strict_dt_by_day
    latest_strict_temperature_day = max(strict_temperature) if strict_temperature else None
    temperature_lag = (
        (yesterday - latest_temperature_day).days
        if latest_temperature_day
        else None
    )
    eligible_q = q_by_day if eligible_q_by_day is None else eligible_q_by_day
    eligible_dt = dt_by_day if eligible_dt_by_day is None else eligible_dt_by_day
    eligible_days = (
        set(eligible_hlc_days)
        if eligible_hlc_days is not None
        else set(eligible_q) & set(eligible_dt or {})
    )
    latest_eligible_day = max(eligible_days) if eligible_days else None
    eligible_lag = (
        (yesterday - latest_eligible_day).days
        if latest_eligible_day is not None
        else None
    )

    eligibility_exclusion_days = eligibility_exclusion_days or {}
    recent_expected_gaps = {
        day for day in eligibility_exclusion_days.get("expected_heating_gap", set())
        if day <= yesterday and (since_full is None or day >= since_full)
    }
    recent_assignment_gaps = {
        day for day in eligibility_exclusion_days.get("assignment_transition", set())
        if day <= yesterday and (since_full is None or day >= since_full)
    }
    latest_expected_gap = max(recent_expected_gaps) if recent_expected_gaps else None
    latest_assignment_gap = max(recent_assignment_gaps) if recent_assignment_gaps else None
    if latest_expected_gap is not None and (
        latest_assignment_gap is None or latest_expected_gap >= latest_assignment_gap
    ):
        eligibility_gap_reason = (
            "expected heating coverage is incomplete; aligned HLC model evidence "
            f"has not been eligible since {_iso_date_str(latest_eligible_day)}"
        )
        eligibility_gap_action = "restore expected heating-power coverage and aligned daily model inputs"
    elif latest_assignment_gap is not None:
        eligibility_gap_reason = (
            "room assignment transitions have interrupted aligned HLC model coverage; "
            f"the latest eligible day was {_iso_date_str(latest_eligible_day)}"
        )
        eligibility_gap_action = "accumulate complete aligned days after the room assignment transition"
    else:
        eligibility_gap_reason = (
            "aligned HLC model coverage is incomplete; "
            f"the latest eligible day was {_iso_date_str(latest_eligible_day)}"
        )
        eligibility_gap_action = "restore complete aligned gas, room temperature, outdoor, and expected heating data"
    if not eligible_days and not q_by_day:
        eligibility_gap_reason = "no complete daily gas evidence is eligible for the HLC model"
        eligibility_gap_action = "restore complete gas meter days and aligned model inputs"
    elif not eligible_days and not (dt_by_day or {}):
        eligibility_gap_reason = "no complete aligned room and outdoor temperature days are eligible for the HLC model"
        eligibility_gap_action = "restore complete aligned room and outdoor temperature history"

    if hlc_issue:
        hlc_stat = {
            "status": "source_problem",
            "reason": hlc_issue,
            "next_action": "resolve gas meter or outdoor temperature sensor issue",
        }
        if latest_gas_day:
            hlc_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            hlc_stat["source_lag_days"] = gas_lag
    elif not (conf.get("gas_meter") and conf.get("outdoor") and conf.get("rooms")):
        hlc_stat = {
            "status": "not_configured",
            "reason": "gas meter, outdoor, or room temperature sensors not configured",
            "next_action": "configure gas meter and temperature sensors to calculate heat loss coefficient",
        }
    elif (
        gas_lag is not None and gas_lag >= HLC_SOURCE_STALE_DAYS
    ) or (
        temperature_lag is not None
        and temperature_lag >= HLC_SOURCE_STALE_DAYS
    ) or (
        eligible_lag is not None
        and eligible_lag >= HLC_SOURCE_STALE_DAYS
    ):
        stale_source = (
            "gas meter"
            if gas_lag is not None and gas_lag >= HLC_SOURCE_STALE_DAYS
            else "room/outdoor temperature"
            if temperature_lag is not None
            and temperature_lag >= HLC_SOURCE_STALE_DAYS
            else "expected heating/aligned model coverage"
        )
        hlc_stat = {
            "status": "source_problem",
            "reason": (
                eligibility_gap_reason
                if stale_source == "expected heating/aligned model coverage"
                else f"{stale_source} history is stale; source data is at least "
                f"{HLC_SOURCE_STALE_DAYS} days old"
            ),
            "next_action": (
                eligibility_gap_action
                if stale_source == "expected heating/aligned model coverage"
                else f"restore recent {stale_source} history before interpreting the HLC status"
            ),
        }
        if latest_gas_day is not None:
            hlc_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            hlc_stat["source_lag_days"] = gas_lag
        if latest_temperature_day is not None:
            hlc_stat["latest_temperature_day"] = _iso_date_str(latest_temperature_day)
            hlc_stat["temperature_source_lag_days"] = temperature_lag
        if result.get("hlc") is not None:
            hlc_stat["model_data_through"] = _iso_date_str(model_day)
            hlc_stat["usable_observations"] = result["hlc"].get("days_used")
            hlc_stat["required_observations"] = HLC_MIN_DAYS
    elif model_day is None:
        diag = hlc_diagnostics or {}
        reason = (
            eligibility_gap_reason
            if not eligible_days
            else diag.get("reason", "no qualifying heating days observed in analysis window")
        )
        if not q_by_day:
            next_action = "restore complete gas meter history for full local days"
        elif not dt_by_day:
            next_action = "restore complete aligned room and outdoor temperature history"
        elif not eligible_days:
            next_action = eligibility_gap_action
        else:
            next_action = "wait for colder weather or measured heating activity"
        hlc_stat = {
            "status": "collecting",
            "reason": reason,
            "next_action": next_action,
            "usable_observations": len(set(eligible_q) & set(eligible_dt or {})),
            "required_observations": HLC_FALLBACK_MIN_DAYS,
        }
        if latest_gas_day:
            hlc_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            hlc_stat["source_lag_days"] = gas_lag
        if latest_temperature_day:
            hlc_stat["latest_temperature_day"] = _iso_date_str(latest_temperature_day)
            hlc_stat["temperature_source_lag_days"] = temperature_lag
    elif result.get("hlc") is not None:
        hlc_res = result["hlc"]
        held_start = yesterday - timedelta(days=HLC_SOURCE_STALE_DAYS - 1)
        held_dates = {
            held_start + timedelta(days=offset)
            for offset in range(HLC_SOURCE_STALE_DAYS)
        }
        common_days = set(eligible_q) & set(eligible_dt or {})
        is_held = (
            yesterday > model_day
            and (yesterday - model_day).days >= HLC_SOURCE_STALE_DAYS
            and held_dates.issubset(common_days)
            and held_dates.issubset(heating_off or set())
        )
        if is_held:
            hlc_stat = {
                "status": "historical_baseline_held",
                "reason": (
                    f"heating season baseline held from {_iso_date_str(model_day)}; "
                    f"no qualifying heating observed for {(yesterday - model_day).days} days"
                ),
                "next_action": "model will update when a new qualifying heating day occurs",
            }
        else:
            hlc_stat = {
                "status": hlc_res["status"],
                "reason": "heat loss coefficient model successfully fitted",
                "next_action": "continue monitoring daily energy balance",
            }
        hlc_stat.update({
            "usable_observations": hlc_res["days_used"],
            "required_observations": HLC_MIN_DAYS,
            "model_data_through": _iso_date_str(model_day),
        })
        if latest_gas_day:
            hlc_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            hlc_stat["source_lag_days"] = gas_lag
        if latest_temperature_day:
            hlc_stat["latest_temperature_day"] = _iso_date_str(latest_temperature_day)
            hlc_stat["temperature_source_lag_days"] = temperature_lag
    else:
        diag = hlc_diagnostics or {}
        reason = diag.get("reason", "HLC fit failed physical or statistical quality gates")
        is_collecting = diag.get("status") == "collecting" or "fewer than" in reason
        hlc_stat = {
            "status": "collecting" if is_collecting else "rejected",
            "reason": reason,
            "next_action": (
                "wait for more heating days"
                if is_collecting
                else "check heating and temperature sensor placement and calibration"
            ),
            "usable_observations": diag.get("usable_days", 0),
            "required_observations": HLC_FALLBACK_MIN_DAYS,
            "model_data_through": _iso_date_str(model_day),
        }
        if latest_gas_day:
            hlc_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            hlc_stat["source_lag_days"] = gas_lag
    # Preserve legacy dates while making their source and model meanings
    # explicit. Source dates always come from complete raw maps; eligibility
    # bounds come from the non-destructive model mask.
    if latest_gas_day is not None:
        hlc_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
        hlc_stat["raw_latest_source_day"] = _iso_date_str(latest_gas_day)
        hlc_stat["source_lag_days"] = gas_lag
    if latest_temperature_day is not None:
        hlc_stat["latest_temperature_day"] = _iso_date_str(latest_temperature_day)
        hlc_stat["temperature_source_lag_days"] = temperature_lag
    if latest_strict_temperature_day is not None:
        hlc_stat["raw_latest_temperature_day"] = _iso_date_str(latest_strict_temperature_day)
    hlc_stat["latest_eligible_day"] = _iso_date_str(latest_eligible_day)
    hlc_stat["eligible_source_lag_days"] = (
        (yesterday - latest_eligible_day).days
        if latest_eligible_day is not None
        else None
    )
    hlc_stat["raw_complete_gas_days"] = sum(
        1 for day in q_by_day
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    hlc_stat["raw_complete_temperature_days"] = sum(
        1 for day in (strict_temperature or {})
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    hlc_stat["qualified_complete_temperature_days"] = sum(
        1 for day in (dt_by_day or {})
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    hlc_stat["eligible_observations"] = sum(
        1 for day in eligible_days
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    if model_day is not None:
        hlc_stat.setdefault("model_data_through", _iso_date_str(model_day))
    if result.get("hlc"):
        hlc_stat["model_data_from"] = _iso_date_str(
            result["hlc"].get("model_data_from")
        )
        hlc_stat["model_data_through"] = _iso_date_str(
            result["hlc"].get("model_data_through") or model_day
        )
        if result["hlc"].get("boiler_efficiency_sensitivity"):
            hlc_stat["boiler_efficiency_sensitivity"] = dict(
                result["hlc"]["boiler_efficiency_sensitivity"]
            )
    status["hlc"] = hlc_stat

    # --- DHW ---
    dhw_issue = _check_source_issue(conf, "gas_meter", conf.get("gas_meter")) or room_temp_issue
    dhw_eligible = eligible_dhw_by_day or {}
    dhw_latest_eligible = (
        max(dhw_eligible)
        if dhw_eligible
        else result.get("dhw", {}).get("latest_eligible_gas_day")
        if isinstance(result.get("dhw"), dict)
        else None
    )
    dhw_eligibility_lag = (
        (yesterday - dhw_latest_eligible).days
        if dhw_latest_eligible is not None
        else None
    )
    dhw_stale_source = (
        "gas meter"
        if gas_lag is not None and gas_lag >= HLC_SOURCE_STALE_DAYS
        else "room/outdoor temperature"
        if temperature_lag is not None and temperature_lag >= HLC_SOURCE_STALE_DAYS
        else "eligible DHW classification"
        if dhw_eligibility_lag is not None
        and dhw_eligibility_lag >= HLC_SOURCE_STALE_DAYS
        else None
    )
    if dhw_issue:
        dhw_stat = {
            "status": "source_problem",
            "reason": dhw_issue,
            "next_action": "resolve gas meter sensor issue",
        }
        if latest_gas_day:
            dhw_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            dhw_stat["source_lag_days"] = gas_lag
    elif not conf.get("gas_meter"):
        dhw_stat = {
            "status": "not_configured",
            "reason": "gas meter not configured",
            "next_action": "configure a gas meter sensor to calculate hot water baseline",
        }
    elif dhw_stale_source:
        dhw_stat = {
            "status": "source_problem",
            "reason": (
                f"{dhw_stale_source} evidence is stale; the latest usable data is at least "
                f"{HLC_SOURCE_STALE_DAYS} days old"
            ),
            "next_action": f"restore recent {dhw_stale_source} evidence before interpreting DHW attribution",
        }
    elif result.get("dhw") is not None:
        dhw_res = result["dhw"]
        dhw_stat = {
            "status": dhw_res["status"],
            "reason": f"hot water baseline established using {dhw_res['days_used']} heating-off days",
            "next_action": "continue monitoring non-heating gas consumption",
            "usable_observations": dhw_res["days_used"],
            "required_observations": 14,
        }
    else:
        since_cutoff = since_full or (yesterday - timedelta(days=365))
        usable_dhw = sum(
            1 for d in q_by_day
            if d >= since_cutoff and (heating_off is None or d in heating_off)
        )
        dhw_stat = {
            "status": "collecting",
            "reason": f"fewer than {DHW_BASELINE_MIN_DAYS} heating-off days with gas data (have {usable_dhw})",
            "next_action": "wait for warm weather or heating-off days",
            "usable_observations": usable_dhw,
            "required_observations": DHW_BASELINE_MIN_DAYS,
        }
        if latest_gas_day:
            dhw_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
            dhw_stat["source_lag_days"] = gas_lag
    status["dhw"] = dhw_stat
    if latest_gas_day is not None:
        dhw_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
        dhw_stat["raw_latest_source_day"] = _iso_date_str(latest_gas_day)
        dhw_stat["source_lag_days"] = gas_lag
    dhw_stat["raw_complete_gas_days"] = sum(
        1 for day in q_by_day
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    dhw_stat["raw_complete_temperature_days"] = sum(
        1 for day in (strict_temperature or {})
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    dhw_stat["eligible_observations"] = len(dhw_eligible)
    if dhw_latest_eligible is not None:
        dhw_stat["latest_eligible_day"] = _iso_date_str(dhw_latest_eligible)
        dhw_stat["eligibility_lag_days"] = dhw_eligibility_lag
        dhw_stat["eligible_source_lag_days"] = dhw_eligibility_lag
        dhw_stat["model_data_through"] = _iso_date_str(dhw_latest_eligible)

    # --- USAGE ---
    usage_issue = _check_source_issue(conf, "gas_meter", conf.get("gas_meter"))
    usage_res = result.get("usage")
    usage_latest_eligible = (
        usage_res.get("latest_eligible_gas_day")
        if isinstance(usage_res, dict)
        else dhw_latest_eligible
    )
    usage_eligibility_lag = (
        (yesterday - usage_latest_eligible).days
        if usage_latest_eligible is not None
        else None
    )
    usage_stale_source = (
        "gas meter"
        if gas_lag is not None and gas_lag >= HLC_SOURCE_STALE_DAYS
        else "room/outdoor temperature"
        if temperature_lag is not None and temperature_lag >= HLC_SOURCE_STALE_DAYS
        else "eligible usage attribution"
        if usage_eligibility_lag is not None
        and usage_eligibility_lag >= HLC_SOURCE_STALE_DAYS
        else None
    )
    if usage_issue:
        usage_stat = {
            "status": "source_problem",
            "reason": usage_issue,
            "next_action": "resolve gas meter sensor issue",
        }
    elif not conf.get("gas_meter"):
        usage_stat = {
            "status": "not_configured",
            "reason": "gas meter not configured",
            "next_action": "configure gas meter to calculate attributed usage",
        }
    elif usage_stale_source:
        usage_stat = {
            "status": "source_problem",
            "reason": (
                f"{usage_stale_source} evidence is stale; the latest usable data is at least "
                f"{HLC_SOURCE_STALE_DAYS} days old"
            ),
            "next_action": f"restore recent {usage_stale_source} evidence before interpreting attributed usage",
        }
    elif result.get("usage") is not None:
        u = result["usage"]
        usage_stat = {
            "status": "valid" if u.get("dhw_kwh_per_day_30d") is not None else "provisional",
            "reason": "gas consumption successfully attributed between hot water and space heating",
            "next_action": "continue tracking seasonal usage",
            "usable_observations": u["heating_off_days"] + u["modelled_days"],
            "required_observations": RECENT_7D_MIN_DAYS,
        }
    else:
        usage_stat = {
            "status": "collecting",
            "reason": "insufficient history to attribute daily usage",
            "next_action": "accumulate more gas meter readings",
        }
    status["usage"] = usage_stat
    if latest_gas_day is not None:
        usage_stat["latest_source_day"] = _iso_date_str(latest_gas_day)
        usage_stat["raw_latest_source_day"] = _iso_date_str(latest_gas_day)
        usage_stat["source_lag_days"] = gas_lag
    usage_stat["raw_complete_gas_days"] = sum(
        1 for day in q_by_day
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    usage_stat["raw_complete_temperature_days"] = sum(
        1 for day in (strict_temperature or {})
        if (since_full is None or day >= since_full) and day <= yesterday
    )
    usage_stat["eligible_observations"] = len(dhw_eligible)
    if usage_latest_eligible is not None:
        usage_stat["latest_eligible_day"] = _iso_date_str(usage_latest_eligible)
        usage_stat["eligibility_lag_days"] = usage_eligibility_lag
        usage_stat["eligible_source_lag_days"] = usage_eligibility_lag
        usage_stat["model_data_through"] = _iso_date_str(usage_latest_eligible)

    # --- ELECTRICITY ---
    elec_issue = _check_source_issue(conf, "electricity_meter", conf.get("electricity_meter"))
    if elec_issue:
        elec_stat = {
            "status": "source_problem",
            "reason": elec_issue,
            "next_action": "resolve electricity meter sensor issue",
        }
    elif not conf.get("electricity_meter"):
        elec_stat = {
            "status": "not_configured",
            "reason": "electricity meter not configured",
            "next_action": "configure an electricity meter sensor to calculate baseload and usage",
        }
    elif result.get("electricity") is not None:
        elec = result["electricity"]
        elec_stat = {
            "status": "valid" if elec["current_period_days_used"] >= ELEC_MIN_DAYS else "provisional",
            "reason": f"electricity usage and baseload established from {elec['days_used']} days",
            "next_action": "continue monitoring electrical baseload",
            "usable_observations": elec["days_used"],
            "required_observations": ELEC_MIN_DAYS,
            "latest_source_day": _iso_date_str(elec["latest_complete_day"]),
            "source_lag_days": elec["source_lag_days"],
        }
    else:
        usable_elec = result.get("electricity_status", {}).get("usable_days", 0)
        elec_stat = {
            "status": "collecting",
            "reason": f"fewer than {ELEC_MIN_DAYS} usable days of electricity meter data",
            "next_action": "accumulate more electricity meter readings",
            "usable_observations": usable_elec,
            "required_observations": ELEC_MIN_DAYS,
        }
    status["electricity"] = elec_stat

    # --- WATER USAGE ---
    water_issue = _check_source_issue(conf, "water", conf.get("water"))
    if water_issue:
        wu_stat = {
            "status": "source_problem",
            "reason": water_issue,
            "next_action": "resolve water meter sensor issue",
        }
    elif not conf.get("water"):
        wu_stat = {
            "status": "not_configured",
            "reason": "water meter not configured",
            "next_action": "configure a water meter sensor to track total water consumption",
        }
    elif result.get("water_usage") is not None:
        wu = result["water_usage"]
        wu_stat = {
            "status": "valid",
            "reason": f"water consumption trends established over {wu['days_used']} days",
            "next_action": "continue monitoring water usage",
            "usable_observations": wu["days_used"],
            "required_observations": WATER_USAGE_MIN_DAYS,
            "latest_source_day": _iso_date_str(wu["latest_complete_day"]),
            "source_lag_days": wu["source_lag_days"],
        }
    else:
        usable_water = result.get("water_status", {}).get("usable_days", 0)
        wu_stat = {
            "status": "collecting",
            "reason": f"fewer than {WATER_USAGE_MIN_DAYS} usable days of water meter data",
            "next_action": "accumulate more water meter readings",
            "usable_observations": usable_water,
            "required_observations": WATER_USAGE_MIN_DAYS,
        }
    status["water_usage"] = wu_stat

    # --- AIR CHANGE RATE ---
    co2_conf = conf.get("co2")
    co2_entities = [co2_conf] if isinstance(co2_conf, str) else (co2_conf or [])
    co2_issue = _check_source_issue(conf, "co2", *co2_entities)
    if co2_issue and result.get("air_change_rate") is None:
        ach_stat = {
            "status": "source_problem",
            "reason": co2_issue,
            "next_action": "resolve CO2 sensor issue",
        }
    elif not conf.get("co2"):
        ach_stat = {
            "status": "not_configured",
            "reason": "CO2 sensor not configured",
            "next_action": "configure indoor CO2 sensor(s) to calculate air change rate",
        }
    elif result.get("air_change_rate") is not None:
        ach = result["air_change_rate"]
        ach_stat = {
            "status": "valid",
            "reason": f"air change rate established from {ach['windows']} decay windows across {ach['sensor_count']} sensor(s)",
            "next_action": "maintain indoor air quality monitoring",
            "usable_observations": ach["windows"],
            "required_observations": CO2_MIN_WINDOWS,
        }
    else:
        ach_stat = {
            "status": "collecting",
            "reason": f"insufficient clean CO2 decay windows ({CO2_MIN_WINDOWS} required)",
            "next_action": "ensure periods of vacancy or quiet decay to observe natural CO2 relaxation",
            "required_observations": CO2_MIN_WINDOWS,
        }
    status["air_change_rate"] = ach_stat

    # --- LOSSES (Whole-home split) ---
    opt_in = bool(conf.get("experimental_whole_home_ventilation", False))
    if not opt_in:
        losses_stat = {
            "status": "not_configured",
            "reason": "experimental whole-home ventilation split is not enabled",
            "next_action": "set experimental_whole_home_ventilation to true to enable whole-home loss split",
        }
    elif not (conf.get("floor_area_m2") and conf.get("ceiling_height_m")):
        losses_stat = {
            "status": "not_configured",
            "reason": "floor area or ceiling height not configured",
            "next_action": "configure floor area and ceiling height",
        }
    elif status.get("hlc", {}).get("status") == "source_problem":
        losses_stat = {
            "status": "source_problem",
            "reason": (
                "HLC source evidence is stale or invalid; dependent fabric and ventilation "
                "loss values are not current"
            ),
            "next_action": status["hlc"].get(
                "next_action", "restore current gas and temperature history"
            ),
        }
    elif result.get("air_change_rate") is None or result.get("hlc") is None:
        losses_stat = {
            "status": "collecting",
            "reason": "waiting for valid air change rate and HLC models",
            "next_action": "wait for CO2 decay curves and space heating models to complete",
        }
    elif result.get("losses") is not None:
        losses_stat = {
            "status": "valid",
            "reason": "whole-home ventilation and fabric loss split successfully calculated",
            "next_action": "review fabric and ventilation loss shares",
            "usable_observations": result["losses"]["windows"],
            "required_observations": CO2_MIN_WINDOWS,
        }
    else:
        note = (
            result.get("losses_status", {}).get("diagnostic_note")
            if isinstance(result.get("losses_status"), dict)
            else "physically inconsistent ventilation/fabric split: calculated ventilation loss exceeds delivered HLC"
        )
        losses_stat = {
            "status": "rejected",
            "reason": note,
            "next_action": "verify building volume and CO2 sensor placement",
            "usable_observations": result["air_change_rate"]["windows"] if result.get("air_change_rate") else 0,
            "required_observations": CO2_MIN_WINDOWS,
        }
    status["losses"] = losses_stat

    # --- LOFT ---
    loft_issue = _check_source_issue(conf, "loft", conf.get("loft"))
    if loft_issue:
        loft_stat = {
            "status": "source_problem",
            "reason": loft_issue,
            "next_action": "resolve loft temperature sensor issue",
        }
    elif not conf.get("loft"):
        loft_stat = {
            "status": "not_configured",
            "reason": "loft temperature sensor not configured",
            "next_action": "configure a loft temperature sensor to calculate loft resistance ratio",
        }
    elif result.get("loft") is not None:
        loft_res = result["loft"]
        loft_stat = {
            "status": "valid",
            "reason": f"loft resistance ratio established from {loft_res['hours_used']} cold night hours",
            "next_action": "maintain thermal monitoring",
            "usable_observations": loft_res["hours_used"],
            "required_observations": LOFT_MIN_HOURS,
        }
    else:
        loft_stat = {
            "status": "collecting",
            "reason": f"fewer than {LOFT_MIN_HOURS} cold night hours with dT > {LOFT_MIN_DT}K",
            "next_action": "wait for colder night temperatures to observe loft resistance",
            "required_observations": LOFT_MIN_HOURS,
        }
    status["loft"] = loft_stat

    # --- ROOMS ---
    rooms_status = {}
    for name, spec in conf.get("rooms", {}).items():
        temp_eid = spec.get("temperature")
        heat_eid = spec.get("heating_power")
        r_issue = (
            _check_source_issue(conf, temp_eid, heat_eid)
            or _room_source_issue(conf, name, "temperature", "heating_power")
        )
        if not r_issue and heat_eid:
            r_issue = (result.get("heating_power_issues") or {}).get(heat_eid)
        if r_issue:
            rooms_status[name] = {
                "status": "source_problem",
                "reason": r_issue,
                "next_action": "resolve sensor issue for room",
            }
        elif not temp_eid:
            rooms_status[name] = {
                "status": "not_configured",
                "reason": "room temperature sensor not configured",
                "next_action": "configure a temperature sensor for this room",
            }
        elif result.get("rooms", {}).get(name) is not None:
            r_fit = result["rooms"][name]
            rooms_status[name] = {
                "status": "valid" if r_fit["nights_fitted"] >= 5 else "provisional",
                "reason": (
                    f"cooling rate estimated at tau={r_fit['tau_median_h']:.1f}h from "
                    f"{r_fit['nights_fitted']} night fits ({r_fit.get('first_night')} to "
                    f"{r_fit.get('last_night')})"
                ),
                "next_action": "continue monitoring overnight room cooling",
                "usable_observations": r_fit["nights_fitted"],
                "required_observations": TAU_MIN_NIGHTS,
                "model_data_through": r_fit.get("last_night"),
                "latest_source_day": _iso_date_str(r_fit.get("latest_source_day")),
                "latest_temperature_day": _iso_date_str(r_fit.get("latest_temperature_day")),
                "latest_heating_day": _iso_date_str(r_fit.get("latest_heating_day")),
                "latest_demand_day": _iso_date_str(r_fit.get("latest_demand_day")),
                "first_night": r_fit.get("first_night"),
                "last_night": r_fit.get("last_night"),
                "tau_q1_h": r_fit.get("tau_q1_h"),
                "tau_q3_h": r_fit.get("tau_q3_h"),
                "sensor_count": r_fit.get("sensor_count", 1),
                "temperature_sensor_count": r_fit.get("temperature_sensor_count", 1),
                "heating_sensor_count": r_fit.get("heating_sensor_count", 0),
                "candidate_nights": r_fit.get("candidate_nights", r_fit["nights_fitted"]),
                "rejected_nights": r_fit.get("rejected_nights", 0),
                "rejection_counts": r_fit.get("rejection_counts", {}),
            }
        else:
            quality = result.get("room_fit_quality", {}).get(name, {})
            rooms_status[name] = {
                "status": "collecting",
                "reason": (
                    "fewer than 3 clean overnight free-cooling periods with heating confirmed off"
                    if spec.get("heating_power") else
                    "fewer than 3 clean overnight free-cooling periods; heating-off is assumed without a heating source"
                ),
                "next_action": "ensure heating is off overnight with steady unheated cooling periods",
                "usable_observations": result.get("room_fit_counts", {}).get(name, 0),
                "required_observations": TAU_MIN_NIGHTS,
                "latest_source_day": _iso_date_str(quality.get("latest_source_day")),
                "latest_temperature_day": _iso_date_str(quality.get("latest_temperature_day")),
                "latest_heating_day": _iso_date_str(quality.get("latest_heating_day")),
                "latest_demand_day": _iso_date_str(quality.get("latest_demand_day")),
                "sensor_count": 1,
                "temperature_sensor_count": int(bool(temp_eid)),
                "heating_sensor_count": int(bool(heat_eid)),
                "candidate_nights": quality.get("candidate_nights", 0),
                "rejected_nights": quality.get("rejected_nights", 0),
                "rejection_counts": quality.get("rejection_counts", {}),
            }
    status["rooms"] = rooms_status

    return status
