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


def test_loft_ratio_requires_all_rooms_and_prevents_bias(thermal_math):
    # Two conditioned rooms: living (warm, 22C) and bedroom (cold, 16C)
    # Mean is 19C. Outdoor is 5C. dT = 14C. Loft is 12C -> ratio = (12 - 5) / 14 = 0.5.
    # On day 2, bedroom is missing. If bedroom was ignored, average would be 22C,
    # dT = 17C, biasing ratio to (12 - 5) / 17 = 0.41.
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    living = {}
    bedroom = {}
    loft = {}
    outdoor = {}
    for day in range(4):
        for hour in range(1, 6):
            ts = int((start + timedelta(days=day, hours=hour)).timestamp())
            outdoor[ts] = 5.0
            loft[ts] = 12.0
            living[ts] = 22.0
            if day != 2:
                bedroom[ts] = 16.0
    rooms = [living, bedroom]
    result = thermal_math.loft_ratio(
        rooms, loft, outdoor, ZoneInfo("Europe/London"), date(2026, 1, 1)
    )
    assert result is not None
    # 4 days * 5 hours = 20 total hours, minus 5 hours on day 2 = 15 hours used
    assert result["hours_used"] == 15
    assert result["ratio"] == pytest.approx(0.5)

    # Crosscheck offline
    from ha_efficiency import loft as offline_loft
    all_ts = sorted(living.keys())
    idx = pd.to_datetime(all_ts, unit="s", utc=True)
    s_living = pd.Series([living[t] for t in all_ts], index=idx)
    s_bedroom = pd.Series([bedroom.get(t, float("nan")) for t in all_ts], index=idx)
    s_loft = pd.Series([loft[t] for t in all_ts], index=idx)
    s_outdoor = pd.Series([outdoor[t] for t in all_ts], index=idx)
    offline_res = offline_loft.loft_ratio(
        {"living": s_living, "bedroom": s_bedroom}, s_loft, s_outdoor
    )
    assert offline_res["hours_used"] == 15
    assert offline_res["ratio"] == pytest.approx(0.5)



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


def test_compute_all_separates_ach_from_inconsistent_loss_split(thermal_math):
    start_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    co2_rows = []
    # 12 windows, each 5 hours long, starting at 12:00 every day
    # C(t) = 400 + 400 * exp(-0.5 * t)
    for day in range(15):
        day_base = start_ts + day * 86400 + 12 * 3600
        for h in range(5):
            val = 400.0 + 400.0 * (2.718281828459045 ** (-0.5 * h))
            co2_rows.append({"start": day_base + h * 3600, "mean": val})

    # Prepare gas and indoor/outdoor temp that yield a small delivered HLC, e.g. 20 W/K
    # Vary outdoor temp between 2C (dT=18) and 8C (dT=12) so dT spread >= 3.0
    # Volume: 200 m2 * 3.0 m = 600 m3
    # ACH ~ 0.5 -> ventilation = 0.335 * 0.5 * 600 = 100.5 W/K
    # Since 100.5 W/K > 20 W/K, this is physically inconsistent!
    outdoor_rows = []
    temp_rows = []
    gas_rows = []
    cum_gas = 0.0
    for day in range(40):
        ts = start_ts + day * 86400
        t_out = 2.0 if (day % 2 == 0) else 8.0
        dt = 20.0 - t_out  # 18 or 12
        day_gas = 20.0 * dt * 24.0 / 1000.0 / 0.88
        for h in range(24):
            hour_ts = ts + h * 3600
            outdoor_rows.append({"start": hour_ts, "mean": t_out})
            temp_rows.append({"start": hour_ts, "mean": 20.0})
            cum_gas += day_gas / 24.0
            gas_rows.append({"start": hour_ts, "sum": cum_gas})

    stats = {
        "sensor.co2": co2_rows,
        "sensor.outdoor": outdoor_rows,
        "sensor.temp": temp_rows,
        "sensor.gas": gas_rows,
    }
    conf = {
        "rooms": {
            "living": {"temperature": "sensor.temp"}
        },
        "outdoor": "sensor.outdoor",
        "gas_meter": "sensor.gas",
        "co2": "sensor.co2",
        "outdoor_co2_ppm": 400.0,
        "floor_area_m2": 200.0,
        "ceiling_height_m": 3.0,
        "boiler_efficiency": 0.88,
        "experimental_whole_home_ventilation": True,
    }
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 2, 15, tzinfo=timezone.utc)
    res = thermal_math.compute_all(stats, conf, tz, now, (30, 60))

    assert res["air_change_rate"] is not None
    assert res["air_change_rate"]["ach"] == pytest.approx(0.5, abs=0.05)
    assert res["air_change_rate"]["windows"] >= 10

    # Losses split must be None because ventilation exceeds delivered HLC
    assert res["losses"] is None
    assert res["losses_status"] is not None
    assert res["losses_status"]["status"] == "inconsistent"
    assert "exceeds delivered HLC" in res["losses_status"]["diagnostic_note"]


def test_loss_sensors_handle_inconsistent_split_vs_insufficient_co2():
    from custom_components.thermal_efficiency.sensor import (
        AirChangeRateSensor,
        VentilationLossSensor,
        FabricLossSensor,
    )
    from unittest.mock import MagicMock

    # Case 1: Inconsistent split (ACH valid, but ventilation > HLC)
    coordinator = MagicMock()
    coordinator.data = {
        "air_change_rate": {
            "ach": 0.456,
            "windows": 14,
            "baseline_ppm": 415.0,
            "co2_sensors_used": 1,
            "co2_baseline_source": "outdoor sensor",
            "scope": "home volume",
        },
        "losses": None,
        "losses_status": {
            "status": "inconsistent",
            "calculated_ventilation_w_per_k": 85.0,
            "hlc_delivered_w_per_k": 60.0,
            "diagnostic_note": (
                "physically inconsistent ventilation/fabric split: "
                "calculated ventilation loss (85.0 W/K) exceeds delivered HLC (60.0 W/K)"
            ),
        },
    }

    ach_sensor = AirChangeRateSensor(coordinator)
    vent_sensor = VentilationLossSensor(coordinator)
    fabric_sensor = FabricLossSensor(coordinator)

    # Air change rate sensor retains true ACH and evidence
    assert ach_sensor.native_value == 0.456
    attrs = ach_sensor.extra_state_attributes
    assert attrs["decay_windows_used"] == 14
    assert attrs["outdoor_co2_baseline_ppm"] == 415.0
    assert attrs["co2_sensors_used"] == 1

    # Ventilation and fabric sensors are unavailable
    assert vent_sensor.native_value is None
    assert fabric_sensor.native_value is None

    # Diagnostic note distinguishes physical inconsistency
    vent_note = vent_sensor.extra_state_attributes["note"]
    fabric_note = fabric_sensor.extra_state_attributes["note"]
    assert "exceeds delivered HLC" in vent_note
    assert "physically inconsistent" in vent_note
    assert "exceeds delivered HLC" in fabric_note
    assert "physically inconsistent" in fabric_note

    # Case 2: Insufficient CO2 decay windows (no ACH)
    coordinator.data = {
        "air_change_rate": None,
        "losses": None,
        "losses_status": None,
    }
    assert ach_sensor.native_value is None
    assert "not enough clean CO2 decay windows yet" in ach_sensor.extra_state_attributes["note"]
    assert vent_sensor.native_value is None
    assert vent_sensor.extra_state_attributes["note"] == "not enough data yet"
    assert fabric_sensor.native_value is None
    assert fabric_sensor.extra_state_attributes["note"] == "not enough data yet"


@pytest.mark.parametrize("status,expected", [
    ("source_problem", "source_problem"), ("rejected", "rejected"),
    ("provisional", "provisional"), ("valid", "ready"),
    ("historical_baseline_held", "historical_baseline_held"),
])
def test_readiness_identity_state_and_privacy(status, expected):
    from unittest.mock import MagicMock
    from custom_components.thermal_efficiency.sensor import DataReadinessSensor

    coordinator = MagicMock()
    coordinator.conf = {"rooms": {"bed": {"name": "Private Bedroom", "temperature": "sensor.private_temperature"}}}
    coordinator.data = {
        "analysis_status": {
            "hlc": {"status": status, "reason": "sensor.private_temperature in Private Bedroom", "next_action": "check sensor.private_temperature", "usable_observations": 21},
            "rooms": {"bed": {"status": status, "reason": "Private Bedroom source failed", "required_observations": 3}},
        },
        "source_issues": {"external:private_water": "external:private_water has missing unit"},
    }
    sensor = DataReadinessSensor(coordinator)
    assert sensor.unique_id == "thermal_efficiency_data_readiness"
    assert sensor.state_class is None
    assert sensor.native_unit_of_measurement is None
    assert sensor.native_value == expected
    attrs = sensor.extra_state_attributes
    assert attrs["metrics"]["hlc"]["usable_observations"] == 21
    assert attrs["metrics"]["rooms"]["room_1"]["required_observations"] == 3
    assert "private_temperature" not in str(attrs)
    assert "Private Bedroom" not in str(attrs)
    assert "private_water" not in str(attrs)
