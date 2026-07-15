"""Heating-off day classification and water-corroborated DHW attribution."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ha_efficiency import dhw

TZ = ZoneInfo("Europe/London")
SINCE = date(2026, 1, 1)


def _days(count: int, start: date = SINCE) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(count)]


# --- heating_off_days -------------------------------------------------------


def test_measured_heating_power_overrides_dt_proxy_both_ways(thermal_math):
    d_burst, d_mild, d_cold, d_warm = _days(4)
    dt_by_day = {d_burst: 2.0, d_mild: 5.0, d_cold: 12.0, d_warm: 1.5}
    heat_by_day = {d_burst: 4.0, d_mild: 0.2}  # no power data for the others

    off = thermal_math.heating_off_days(dt_by_day, heat_by_day)

    # A shoulder day with a measured heating burst is not "off" despite dT<3;
    # a mild day where the heating never fired is "off" despite dT>3.
    assert d_burst not in off
    assert d_mild in off
    # Days without power coverage fall back to the dT proxy.
    assert d_cold not in off
    assert d_warm in off


def test_daily_heating_pct_requires_full_sensor_population(thermal_math):
    base = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    room_a = {base + h * 3600: 0.0 for h in range(24)}
    room_b = {base + h * 3600: 10.0 for h in range(24)}
    # Second day: room_b's sensor is silent - not evidence its radiator was off.
    for h in range(24, 48):
        room_a[base + h * 3600] = 0.0

    by_day = thermal_math.daily_heating_pct([room_a, room_b], TZ)

    assert by_day == {date(2026, 6, 1): pytest.approx(10.0)}


# --- dhw_baseline with water corroboration ----------------------------------


def _baseline_inputs(litres_by_offset: dict[int, float]):
    days = _days(20)
    q = {d: 12.0 for d in days}
    dt = {d: 1.0 for d in days}
    outdoor = {d: 18.0 for d in days}
    water = {}
    for offset, litres in litres_by_offset.items():
        d = days[offset]
        water[d] = litres
        if litres < 50.0:
            q[d] = 0.3  # away day: boiler idle, nearly no gas
    return days, q, dt, outdoor, water


def test_baseline_excludes_low_water_away_days(thermal_math):
    litres = {i: (5.0 if i < 8 else 400.0) for i in range(20)}
    days, q, dt, outdoor, water = _baseline_inputs(litres)

    plain = thermal_math.dhw_baseline(q, dt, outdoor, SINCE)
    corroborated = thermal_math.dhw_baseline(
        q, dt, outdoor, SINCE, heating_off=set(days), water_by_day=water
    )

    assert plain is not None and corroborated is not None
    assert corroborated["kwh_per_day"] == pytest.approx(12.0)
    assert corroborated["days_used"] == 12
    assert corroborated["low_water_days_excluded"] == 8
    assert corroborated["idle_gas_kwh_per_day"] == pytest.approx(0.3)
    # Without water data the away days stay in and can only distort the median.
    assert plain["days_used"] == 20


def test_baseline_keeps_days_predating_the_water_meter(thermal_math):
    litres = {i: 400.0 for i in range(10, 20)}  # water history starts mid-window
    days, q, dt, outdoor, water = _baseline_inputs(litres)

    result = thermal_math.dhw_baseline(
        q, dt, outdoor, SINCE, heating_off=set(days), water_by_day=water
    )

    assert result is not None
    assert result["days_used"] == 20
    assert result["low_water_days_excluded"] == 0


# --- fit_dhw_water_rate ------------------------------------------------------


def _rate_fixture(rate_wh_per_l_per_k: float, count: int = 15):
    days = _days(count)
    outdoor = {d: 4.0 + i for i, d in enumerate(days)}
    water = {d: 300.0 + 20 * i for i, d in enumerate(days)}
    q = {}
    for d in days:
        rise = 55.0 - _mains(outdoor[d])
        q[d] = water[d] * rate_wh_per_l_per_k * rise / 1000
    return days, q, water, outdoor


def _mains(outdoor_c: float) -> float:
    if outdoor_c <= 2.0:
        return 4.0
    if outdoor_c >= 18.0:
        return 16.0
    return 4.0 + (outdoor_c - 2.0) / 16.0 * 12.0


def test_water_rate_recovered_across_seasonal_mains_swing(thermal_math):
    days, q, water, outdoor = _rate_fixture(0.6)

    fit = thermal_math.fit_dhw_water_rate(q, water, outdoor, set(days), SINCE)

    assert fit is not None
    assert fit["wh_per_litre_per_k"] == pytest.approx(0.6, rel=1e-6)
    assert fit["days_used"] == 15

    modelled = thermal_math.dhw_kwh_from_water(water[days[3]], outdoor[days[3]], fit)
    assert modelled == pytest.approx(q[days[3]], rel=1e-6)


def test_water_rate_ignores_heating_and_low_water_days(thermal_math):
    days, q, water, outdoor = _rate_fixture(0.6)
    heating_off = set(days[:12])
    q[days[0]] += 80.0  # heating day gas would poison the rate...
    water[days[1]] = 20.0  # ...and so would an away day's trickle

    fit = thermal_math.fit_dhw_water_rate(
        q, water, outdoor, heating_off - {days[0]}, SINCE
    )

    assert fit is not None
    assert fit["wh_per_litre_per_k"] == pytest.approx(0.6, rel=1e-6)
    assert fit["days_used"] == 10


def test_water_rate_needs_enough_days_and_physical_bounds(thermal_math):
    days, q, water, outdoor = _rate_fixture(0.6, count=9)
    assert thermal_math.fit_dhw_water_rate(q, water, outdoor, set(days), SINCE) is None

    days, q, water, outdoor = _rate_fixture(2.5)  # more than pure hot water
    assert thermal_math.fit_dhw_water_rate(q, water, outdoor, set(days), SINCE) is None


def test_offline_water_rate_crosschecks_the_integration_math(thermal_math):
    days, q, water, outdoor = _rate_fixture(0.45)

    live = thermal_math.fit_dhw_water_rate(q, water, outdoor, set(days), SINCE)
    offline = dhw.fit_dhw_water_rate(
        pd.Series(q), pd.Series(water), pd.Series(outdoor), set(days)
    )

    assert live is not None and offline is not None
    assert offline["wh_per_litre_per_k"] == pytest.approx(
        live["wh_per_litre_per_k"], rel=1e-9
    )
    assert offline["days_used"] == live["days_used"]


# --- electricity -------------------------------------------------------------


def test_electricity_summary_baseload_and_daily_use(thermal_math):
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    total = 1000.0
    series = {}
    for hour in range(24 * 20):
        local_hour = hour % 24
        total += 0.15 + (0.5 if 8 <= local_hour < 20 else 0.0)
        series[int(start.timestamp()) + hour * 3600] = total

    result = thermal_math.electricity_summary(
        series, TZ, date(2026, 6, 1), date(2026, 7, 1)
    )

    assert result is not None
    assert result["baseload_w"] == pytest.approx(150.0)
    assert result["kwh_per_day"] == pytest.approx(0.15 * 24 + 0.5 * 12, rel=0.05)
    assert result["implied_internal_gains_w"] == pytest.approx(
        result["kwh_per_day"] * 1000 / 24
    )
    assert 0 < result["baseload_share_pct"] < 100


def test_electricity_summary_needs_two_weeks(thermal_math):
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    series = {
        int(start.timestamp()) + hour * 3600: 1000.0 + 0.2 * hour
        for hour in range(24 * 10)
    }

    assert (
        thermal_math.electricity_summary(
            series, TZ, date(2026, 6, 1), date(2026, 7, 1)
        )
        is None
    )
