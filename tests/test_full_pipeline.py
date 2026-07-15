"""End-to-end pure-math coverage for a full heating and summer season."""

from __future__ import annotations

from datetime import datetime
from math import cos, exp, pi
from zoneinfo import ZoneInfo

import pytest


def _row(timestamp: int, kind: str, value: float) -> dict:
    return {"start": timestamp, kind: value}


def test_compute_all_full_year_multi_sensor_pipeline(thermal_math):
    tz = ZoneInfo("Europe/London")
    start = datetime(2025, 7, 1, tzinfo=tz)
    hours = 366 * 24

    stats = {
        "sensor.room": [],
        "sensor.outdoor": [],
        "sensor.heating": [],
        "sensor.gas": [],
        "sensor.loft": [],
        "sensor.humidity": [],
        "sensor.co2_a": [],
        "sensor.co2_b": [],
        "sensor.outdoor_co2": [],
        "utility:water": [],
        "sensor.electricity": [],
    }
    gas_total = 1000.0
    water_total = 5000.0
    electricity_total = 2000.0
    co2_a = 720.0
    co2_b = 760.0

    for hour_index in range(hours):
        timestamp = int(start.timestamp()) + hour_index * 3600
        day = hour_index // 24
        local_hour = hour_index % 24
        outdoor = 10.0 + 10.0 * cos(2 * pi * day / 365)
        indoor = 20.0
        delta_t = max(0.0, indoor - outdoor)
        # Heating fires only when it is cold enough; hot water burns a flat
        # 12 kWh/day year-round against a steady 500 L/day of metered water.
        heating_on = delta_t > 3.0
        heating_pct = min(100.0, 5.0 * delta_t) if heating_on else 0.0
        daily_gas = 12.0 + (7.2 * delta_t if heating_on else 0.0)
        gas_increment = daily_gas / 24
        gas_total += gas_increment
        water_total += 500.0 / 24
        electricity_total += 0.1 + (0.4 if 9 <= local_hour < 22 else 0.0)

        occupied = 9 <= local_hour < 18 and day % 5 != 0
        if occupied:
            co2_a += 0.3 * (1050.0 - co2_a)
            co2_b += 0.28 * (1100.0 - co2_b)
        else:
            co2_a = 420.0 + (co2_a - 420.0) * exp(-0.22)
            co2_b = 420.0 + (co2_b - 420.0) * exp(-0.30)

        stats["sensor.room"].append(_row(timestamp, "mean", indoor))
        stats["sensor.outdoor"].append(_row(timestamp, "mean", outdoor))
        stats["sensor.heating"].append(_row(timestamp, "mean", heating_pct))
        stats["sensor.gas"].append(_row(timestamp, "sum", gas_total))
        stats["sensor.loft"].append(
            _row(timestamp, "mean", outdoor + 0.35 * (indoor - outdoor))
        )
        stats["sensor.humidity"].append(
            _row(timestamp, "mean", 55.0 + 3.0 * cos(2 * pi * local_hour / 24))
        )
        stats["sensor.co2_a"].append(_row(timestamp, "mean", co2_a))
        stats["sensor.co2_b"].append(_row(timestamp, "mean", co2_b))
        stats["sensor.outdoor_co2"].append(_row(timestamp, "mean", 420.0))
        stats["utility:water"].append(_row(timestamp, "sum", water_total))
        stats["sensor.electricity"].append(_row(timestamp, "sum", electricity_total))

    now = datetime.fromtimestamp(int(start.timestamp()) + (hours - 1) * 3600, tz)
    result = thermal_math.compute_all(
        stats,
        {
            "rooms": {
                "room": {
                    "temperature": "sensor.room",
                    "heating_power": "sensor.heating",
                }
            },
            "outdoor": "sensor.outdoor",
            "gas_meter": "sensor.gas",
            "loft": "sensor.loft",
            "loft_humidity": "sensor.humidity",
            "floor_area_m2": 100.0,
            "ceiling_height_m": 2.4,
            "co2": ["sensor.co2_a", "sensor.co2_b"],
            "outdoor_co2_sensor": "sensor.outdoor_co2",
            "water": "utility:water",
            "gas_unit_rate": 0.05,
            "boiler_efficiency": 0.9,
            "electricity_meter": "sensor.electricity",
            "electricity_unit_rate": 0.18,
        },
        tz,
        now,
        (60, 120, 365),
    )

    assert result["hlc"] is not None
    assert result["hlc"]["delivered_hlc_w_per_k"] > 0
    assert result["hlc"]["fuel_input_hlc_w_per_k"] > 0
    assert result["dhw"] is not None
    assert result["dhw"]["days_used"] >= 14
    assert result["dhw"]["kwh_per_day"] == pytest.approx(12.0, rel=0.05)
    assert result["dhw"]["modelled_annual_kwh"] > 0
    assert result["dhw"]["water_rate_days_used"] >= 10
    assert result["dhw"]["water_rate_wh_per_litre_per_k"] > 0
    assert result["loft"] is not None
    assert result["loft"]["humidity_pct"] > 0
    assert result["losses"] is not None
    assert result["losses"]["co2_sensors_used"] == 2
    assert result["losses"]["co2_baseline_source"] == "outdoor sensor"
    # The window ends in late June: heating off, so all recent gas is hot
    # water and the space-heating share is zero by construction.
    usage = result["usage"]
    assert usage is not None
    assert usage["dhw_kwh_per_day_7d"] == pytest.approx(12.0, rel=0.05)
    assert usage["space_heating_kwh_per_day_7d"] == pytest.approx(0.0, abs=0.01)
    assert usage["dhw_cost_per_day_gbp_7d"] == pytest.approx(0.6, rel=0.05)
    assert usage["heating_off_from_power_days"] > 0
    electricity = result["electricity"]
    assert electricity is not None
    assert electricity["baseload_w"] == pytest.approx(100.0, rel=0.05)
    assert electricity["kwh_per_day"] == pytest.approx(0.1 * 24 + 0.4 * 13, rel=0.05)
    assert electricity["last_7d_kwh_per_day"] == pytest.approx(
        electricity["kwh_per_day"], rel=0.05
    )
    assert electricity["baseload_cost_per_year_gbp"] == pytest.approx(
        0.1 * 24 * 0.18 * 365, rel=0.05
    )
    assert electricity["implied_internal_gains_w"] == pytest.approx(
        electricity["kwh_per_day"] * 1000 / 24, rel=0.01
    )
