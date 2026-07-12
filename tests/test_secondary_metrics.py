"""Physical-bound and fit-quality tests for secondary estimates."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ha_efficiency import dhw, ventilation


def _night_series(ratio: float):
    room = {}
    loft = {}
    outdoor = {}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for day in range(4):
        for hour in range(1, 6):
            ts = int((start + timedelta(days=day, hours=hour)).timestamp())
            outdoor[ts] = 5.0
            room[ts] = 20.0
            loft[ts] = 5.0 + ratio * 15.0
    return [room], loft, outdoor


def test_loft_ratio_accepts_plausible_observations(thermal_math):
    rooms, loft, outdoor = _night_series(0.35)

    result = thermal_math.loft_ratio(
        rooms, loft, outdoor, ZoneInfo("Europe/London"), date(2026, 1, 1)
    )

    assert result is not None
    assert result["ratio"] == pytest.approx(0.35)
    assert 0 <= result["ratio"] <= 1


@pytest.mark.parametrize("ratio", [-0.2, 1.67])
def test_loft_ratio_rejects_physically_impossible_result(thermal_math, ratio):
    rooms, loft, outdoor = _night_series(ratio)

    result = thermal_math.loft_ratio(
        rooms, loft, outdoor, ZoneInfo("Europe/London"), date(2026, 1, 1)
    )

    assert result is None


def test_nominal_loss_components_reconcile_and_share_is_bounded():
    result = ventilation.split_losses(
        ach=0.3,
        floor_area_m2=100.0,
        ceiling_height_m=2.4,
        space_heating_hlc_w_per_k=300.0,
        boiler_efficiency=0.9,
    )

    assert result is not None
    assert result["ventilation_w_per_k"] >= 0
    assert result["fabric_w_per_k"] >= 0
    assert result["ventilation_w_per_k"] + result["fabric_w_per_k"] == pytest.approx(
        result["hlc_delivered_w_per_k"]
    )
    assert 0 <= result["ventilation_share_pct"] <= 100


def test_loss_split_is_suppressed_when_ventilation_exceeds_total_loss():
    result = ventilation.split_losses(
        ach=5.0,
        floor_area_m2=150.0,
        ceiling_height_m=3.0,
        space_heating_hlc_w_per_k=50.0,
        boiler_efficiency=0.8,
    )

    assert result is None


def test_multiple_room_ach_fits_are_combined_by_median(thermal_math):
    result = thermal_math.combine_air_change_rates(
        [
            {"ach": 0.2, "windows": 12, "baseline_ppm": 420.0},
            {"ach": 0.35, "windows": 18, "baseline_ppm": 421.0},
            {"ach": 1.4, "windows": 10, "baseline_ppm": 419.0},
        ]
    )

    assert result is not None
    assert result["ach"] == pytest.approx(0.35)
    assert result["windows"] == 40
    assert result["sensor_count"] == 3
    assert result["baseline_ppm"] == pytest.approx(420.0)


def _cumulative(increments, start=0.0):
    total = start
    output = {}
    base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    for index, increment in enumerate(increments):
        total += increment
        output[base + index * 3600] = total
    return output


def test_water_fit_accepts_strong_signal(thermal_math):
    water = [1.0 + i % 10 for i in range(260)]
    gas = [0.1 + 0.018 * litres for litres in water]

    result = thermal_math.fit_water_gas(_cumulative(gas, 500), _cumulative(water, 1000))

    assert result is not None
    assert result["wh_per_litre"] == pytest.approx(18.0, rel=0.02)
    assert result["regression_r_squared"] > 0.9
    assert 0 <= result["hot_fraction_pct"] <= 100


def test_water_fit_rejects_low_quality_positive_correlation(thermal_math):
    water = [1.0 + i % 10 for i in range(260)]
    gas = [0.2 + 0.002 * litres + (0.12 if (i // 10) % 2 else -0.12)
           for i, litres in enumerate(water)]

    result = thermal_math.fit_water_gas(_cumulative(gas, 500), _cumulative(water, 1000))

    assert result is None


def test_offline_water_fit_rejects_low_quality_positive_correlation():
    index = pd.date_range("2026-01-01", periods=260, freq="1h", tz="UTC")
    water = pd.Series([1.0 + i % 10 for i in range(260)], index=index)
    gas = pd.Series(
        [
            0.2 + 0.002 * litres + (0.12 if (i // 10) % 2 else -0.12)
            for i, litres in enumerate(water)
        ],
        index=index,
    )

    assert dhw.fit_water_gas(gas, water) is None
