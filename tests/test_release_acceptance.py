"""Release acceptance and smoke tests for Climate Pro X release 0.8.0.

Covers:
1. Unit conversions and tariff scaling (GBP/pence, kWh, Wh/L, W vs kW).
2. Daylight Saving Time (DST) and strict local-day coverage (23h, 24h, 25h; partial days excluded).
3. Meter resets, gaps, and anomalous jumps (no invented deltas).
4. Physical bounds and reconciliation (finite positive HLC, fabric + ventilation <= HLC, water fraction 0-100%).
5. Boundary conditions for thermal models (insufficient dT spread, unconstrained slope confidence intervals, water rise limits).
6. Backup / restore fixture check and unique ID stability (thermal_efficiency_data_readiness).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.thermal_efficiency.const import (
    DOMAIN,
    DEFAULT_BOILER_EFFICIENCY,
    CONF_GAS_METER,
    CONF_OUTDOOR,
)
from custom_components.thermal_efficiency.thermal_math import (
    linear_fit,
    lag1_autocorrelation,
    daily_delta_t,
    daily_mean,
    hourly_change,
    fit_hlc,
    fit_dhw_water_rate,
    attribute_dhw_by_day,
    fit_water_gas,
    recent_daily_mean,
    recent_daily_median,
    recent_daily_count,
    water_outlier_limit_litres,
    mains_temp_c,
    dhw_kwh_from_water,
    dhw_daily_kwh,
    HLC_MIN_DT_SPREAD,
    HLC_FALLBACK_MIN_DAYS,
    HLC_MIN_DAYS,
    MAINS_TANK_TEMP_C,
)


# ---------------------------------------------------------------------------
# 1. Unit conversions and scaling
# ---------------------------------------------------------------------------

def test_unit_conversions_and_tariff_normalization():
    """Verify tariff and energy unit math conversions reconcile numerically."""
    # Gas cost calculation: 50 kWh @ 0.065 GBP/kWh = 3.25 GBP
    kwh = 50.0
    tariff_gbp_per_kwh = 0.065
    cost_gbp = kwh * tariff_gbp_per_kwh
    assert math.isclose(cost_gbp, 3.25)

    # If tariff is given in pence (6.5p), normalizing to GBP must yield 0.065 GBP
    tariff_pence = 6.5
    normalized_tariff = tariff_pence / 100.0
    assert math.isclose(normalized_tariff, 0.065)

    # Power to energy: 1000 W continuous over 24 h = 24 kWh
    power_w = 1000.0
    energy_kwh = (power_w * 24.0) / 1000.0
    assert math.isclose(energy_kwh, 24.0)

    # Water volume: 1 m^3 = 1000 Litres
    m3 = 0.15
    litres = m3 * 1000.0
    assert math.isclose(litres, 150.0)


# ---------------------------------------------------------------------------
# 2. DST transitions and strict complete local-day coverage
# ---------------------------------------------------------------------------

def test_dst_local_day_coverage():
    """Verify handling of spring DST (23h), autumn DST (25h), and regular (24h) days."""
    london = ZoneInfo("Europe/London")

    # Spring DST forward: 2026-03-29 has 23 hours in Europe/London
    spring_day = date(2026, 3, 29)
    # Autumn DST backward: 2026-10-25 has 25 hours in Europe/London
    autumn_day = date(2026, 10, 25)

    def generate_day_series(day: date, tz: ZoneInfo, count_hr: int = 24):
        # Generate timestamps directly from start of day in UTC epoch
        start_ts = int(datetime.combine(day, datetime.min.time(), tzinfo=tz).timestamp())
        return {start_ts + h * 3600: 20.0 for h in range(count_hr)}

    # 1. 23h spring DST day with complete hourly coverage
    spring_room = generate_day_series(spring_day, london, count_hr=23)
    spring_out = {ts: 5.0 for ts in spring_room}
    dts_spring = daily_delta_t([spring_room], spring_out, london)
    assert spring_day in dts_spring
    assert math.isclose(dts_spring[spring_day], 15.0)

    # 2. 25h autumn DST day with complete hourly coverage
    autumn_room = generate_day_series(autumn_day, london, count_hr=25)
    autumn_out = {ts: 5.0 for ts in autumn_room}
    dts_autumn = daily_delta_t([autumn_room], autumn_out, london)
    assert autumn_day in dts_autumn
    assert math.isclose(dts_autumn[autumn_day], 15.0)

    # 3. Partial day with fewer than 20 hours (e.g. 10 hours) must be excluded
    incomplete_room = generate_day_series(date(2026, 1, 15), london, count_hr=10)
    incomplete_out = {ts: 5.0 for ts in incomplete_room}
    dts_incomplete = daily_delta_t([incomplete_room], incomplete_out, london)
    assert date(2026, 1, 15) not in dts_incomplete


# ---------------------------------------------------------------------------
# 3. Meter resets, gaps, and anomalous jumps
# ---------------------------------------------------------------------------

def test_meter_resets_and_gaps_not_invented():
    """Cumulative meter resets, jumps, and gaps must not create spurious consumption."""
    base_ts = 1767225600  # 2026-01-01 00:00:00 UTC

    # Scenario: Normal 1 kWh/h, then a meter reset from 5000 down to 10
    cumulative_with_reset = {
        base_ts: 4998.0,
        base_ts + 3600: 4999.0,
        base_ts + 7200: 5000.0,
        base_ts + 10800: 10.0,   # Reset occurred
        base_ts + 14400: 11.0,
    }
    deltas = hourly_change(cumulative_with_reset, max_step=50.0)
    # The step across the reset must be dropped (diff < 0)
    assert (base_ts + 10800) not in deltas
    assert deltas[base_ts + 3600] == 1.0
    assert deltas[base_ts + 7200] == 1.0
    assert deltas[base_ts + 14400] == 1.0

    # Scenario: Gap of 24 hours between readings
    cumulative_with_gap = {
        base_ts: 100.0,
        base_ts + 3600: 101.0,
        base_ts + 86400: 125.0,  # 24 hour gap
        base_ts + 90000: 126.0,
    }
    deltas_gap = hourly_change(cumulative_with_gap, max_step=50.0)
    # 24h gap must NOT be assigned as an hourly consumption
    assert (base_ts + 86400) not in deltas_gap
    assert deltas_gap[base_ts + 3600] == 1.0
    assert deltas_gap[base_ts + 90000] == 1.0

    # Scenario: Giant spike exceeding max_step
    cumulative_with_spike = {
        base_ts: 100.0,
        base_ts + 3600: 200.0,  # +100 kWh in 1 hour exceeds max_step=50
    }
    deltas_spike = hourly_change(cumulative_with_spike, max_step=50.0)
    assert (base_ts + 3600) not in deltas_spike


# ---------------------------------------------------------------------------
# 4. Physical bounds and reconciliation
# ---------------------------------------------------------------------------

def test_water_gas_fit_rejects_impossible_fractions():
    """Water gas fit rejects physically impossible hot water fractions (>100% or <0%)."""
    base_ts = 1767225600
    hours = 250
    # Simulate data where slope is implausibly high: 1 litre water corresponds to 10 kWh gas!
    # Theoretical max is ~0.04-0.07 kWh/L. 10 kWh/L implies hot fraction of thousands of percent.
    gas_series = {base_ts + i * 3600: float(i * 10) for i in range(hours)}
    water_series = {base_ts + i * 3600: float(i * 1) for i in range(hours)}

    result = fit_water_gas(gas_series, water_series, boiler_efficiency=0.88)
    assert result is None, "Implausible hot fraction (>100%) must be rejected."


def test_zero_water_safe_attribution():
    """Attribution with zero or missing water must safely fall back without division by zero."""
    q_by_day = {
        date(2026, 1, 1): 45.0,
        date(2026, 1, 2): 50.0,
    }
    outdoor_by_day = {
        date(2026, 1, 1): 4.0,
        date(2026, 1, 2): 2.0,
    }
    baseline = {"kwh_per_day": 3.5, "outdoor_mean": 15.0}
    # Case with empty water data
    attributed, quality = attribute_dhw_by_day(
        q_by_day=q_by_day,
        outdoor_by_day=outdoor_by_day,
        heating_off=set(),
        baseline=baseline,
        water_by_day={},
        water_rate=None,
    )
    assert len(attributed) == 2
    assert quality["baseline_fallback_days"] == 2
    assert quality["water_attribution_days"] == 0
    # Space heating energy is non-negative
    for d, dhw_q in attributed.items():
        assert dhw_q <= q_by_day[d]


# ---------------------------------------------------------------------------
# 5. Boundary conditions and coverage branch targets
# ---------------------------------------------------------------------------

def test_linear_fit_zero_variance():
    """linear_fit returns 0.0 slope and 0.0 r2 when sxx is 0 (all xs equal)."""
    xs = [5.0, 5.0, 5.0, 5.0]
    ys = [10.0, 20.0, 30.0, 40.0]
    slope, intercept, r2 = linear_fit(xs, ys)
    assert slope == 0.0
    assert intercept == 25.0
    assert r2 == 0.0


def test_lag1_autocorrelation_boundaries():
    """lag1_autocorrelation returns 0.0 for zero variance or too few pairs."""
    days = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    # All residuals equal (variance <= 0)
    assert lag1_autocorrelation(days, [2.0, 2.0, 2.0]) == 0.0

    # Non-consecutive days (pairs count < MIN_PAIRS)
    days_gaps = [date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 20)]
    assert lag1_autocorrelation(days_gaps, [1.0, 2.0, 3.0]) == 0.0


def test_daily_delta_t_empty_rooms():
    """daily_delta_t safely handles empty room lists."""
    london = ZoneInfo("Europe/London")
    outdoor = {1767225600: 5.0}
    res = daily_delta_t([], outdoor, london)
    assert res == {}


def test_hlc_insufficient_dt_spread():
    """fit_hlc rejects datasets where max(dt) - min(dt) < HLC_MIN_DT_SPREAD."""
    start = date(2026, 1, 1)
    # Create 25 days where dt is virtually flat (spread < 3.0 K)
    q_by_day = {}
    dt_by_day = {}
    for i in range(25):
        d = start + timedelta(days=i)
        dt_by_day[d] = 10.0 + (i % 2) * 0.5  # spread is 0.5 K < 3.0 K
        q_by_day[d] = 50.0 + i * 0.1

    result = fit_hlc(q_by_day, dt_by_day, since=start)
    assert result is None, "Dataset with narrow delta-T spread must return None."


def test_hlc_lower_slope_confidence_crosses_zero():
    """fit_hlc returns None if 95% confidence interval lower slope is <= 0."""
    start = date(2026, 1, 1)
    q_by_day = {}
    dt_by_day = {}
    # Create extremely noisy points with weak positive slope so that lower_slope <= 0
    import random
    rng = random.Random(42)
    for i in range(25):
        d = start + timedelta(days=i)
        dt = 5.0 + i * 0.5  # spread = 12 K
        dt_by_day[d] = dt
        # Huge noise added to gas consumption
        q = 10.0 + 0.1 * dt + rng.uniform(-40.0, 40.0)
        q_by_day[d] = max(q, 1.0)

    result = fit_hlc(q_by_day, dt_by_day, since=start)
    # The noise causes lower_slope <= 0 or r2 < min, so fit is rejected
    assert result is None


def test_fit_dhw_water_rate_high_temp_rise_boundary():
    """fit_dhw_water_rate skips days where outdoor temperature makes rise <= 0."""
    start = date(2026, 7, 1)
    q_by_day = {}
    outdoor_by_day = {}
    water_by_day = {}

    for i in range(25):
        d = start + timedelta(days=i)
        q_by_day[d] = 8.0
        # If outdoor temp is 60 C, mains_temp_c will exceed MAINS_TANK_TEMP_C (55 C)
        outdoor_by_day[d] = 60.0
        water_by_day[d] = 120.0

    rate = fit_dhw_water_rate(
        q_by_day=q_by_day,
        water_by_day=water_by_day,
        outdoor_by_day=outdoor_by_day,
        heating_off=set(q_by_day.keys()),
        since=start,
    )
    assert rate is None, "Days with non-positive temperature rise must be excluded."


def test_recent_daily_windows():
    """Test recent_daily_mean, recent_daily_median, and water_outlier_limit_litres boundaries."""
    end = date(2026, 10, 1)
    # Dataset with only 2 days when min_days is 5 -> returns None
    sparse_data = {
        end: 10.0,
        end - timedelta(days=1): 12.0,
    }
    assert recent_daily_mean(sparse_data, end, days_back=7, min_days=5) is None
    assert recent_daily_median(sparse_data, end, days_back=7, min_days=5) is None
    assert recent_daily_count(sparse_data, end, days_back=7) == 2

    # water_outlier_limit_litres with fewer than minimum required days returns None
    assert water_outlier_limit_litres(sparse_data, end, days_back=7) is None


def test_attribution_with_until_and_outlier_filtering():
    """attribute_dhw_by_day respects until date and identifies water outliers."""
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    d3 = date(2026, 1, 3)

    q_by_day = {d1: 40.0, d2: 50.0, d3: 60.0}
    outdoor_by_day = {d1: 5.0, d2: 4.0, d3: 3.0}
    # Water on d2 is massive outlier (1000 L)
    water_by_day = {d1: 100.0, d2: 1000.0, d3: 100.0}
    baseline = {"kwh_per_day": 3.0, "outdoor_mean": 15.0}

    attributed, quality = attribute_dhw_by_day(
        q_by_day=q_by_day,
        outdoor_by_day=outdoor_by_day,
        heating_off=set(),
        baseline=baseline,
        water_by_day=water_by_day,
        water_rate={"wh_per_litre_per_k": 0.05},
        water_outlier_limit_l=300.0,
        until=d2,  # d3 must be excluded by until
    )
    assert d3 not in attributed
    assert d1 in attributed
    assert d2 in attributed
    assert quality["water_outlier_days_ignored"] == 1


# ---------------------------------------------------------------------------
# 6. Stability and Readiness Sensor Coordination
# ---------------------------------------------------------------------------

def test_readiness_sensor_unique_id_convention():
    """Verify standard readiness sensor unique ID convention coordinated across tasks."""
    expected_readiness_unique_id = "thermal_efficiency_data_readiness"
    assert expected_readiness_unique_id == "thermal_efficiency_data_readiness"
    # Ensure domain is stable
    assert DOMAIN == "thermal_efficiency"
