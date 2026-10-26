"""Daily evidence, freshness, and privacy contracts for the pure math core."""

from datetime import date, datetime, timedelta, timezone
import json

import pytest


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
ROOMS = {
    # Deliberately reverse alphabetical order; aliases must use sorted room IDs.
    "z_Private Bedroom": {
        "temperature": "sensor.z_room_temp",
        "heating_power": "sensor.z_heating",
        "heating_expected_intervals": [{"start": int(START.timestamp()), "end": None}],
    },
    "a_Private Lounge": {
        "temperature": "sensor.a_room_temp",
        "heating_power": "sensor.a_heating",
        "heating_expected_intervals": [{"start": int(START.timestamp()), "end": None}],
    },
}


def _expected_heating_gap_data(thermal_math, days=50, heating_through=33):
    stats = {
        key: []
        for key in (
            "sensor.gas_meter", "sensor.water_meter", "sensor.outside",
            "sensor.z_room_temp", "sensor.a_room_temp",
            "sensor.z_heating", "sensor.a_heating",
        )
    }
    gas_total = 1000.0
    water_total = 10000.0
    start_ts = int(START.timestamp())
    for hour in range(-1, days * 24):
        day_index = max(hour // 24, 0)
        outside = 2.0 + day_index % 13
        heating = day_index < heating_through
        daily_gas = 8.0 + (7.2 * (20.0 - outside) if heating else 0.0)
        if hour >= 0:
            gas_total += daily_gas / 24.0
            water_total += 10.0
        ts = start_ts + hour * 3600
        for key, value in (
            ("sensor.outside", outside),
            ("sensor.z_room_temp", 20.0),
            ("sensor.a_room_temp", 20.0),
        ):
            stats[key].append({"start": ts, "mean": value})
        if heating and hour >= 0:
            stats["sensor.z_heating"].append({"start": ts, "mean": 30.0})
            stats["sensor.a_heating"].append({"start": ts, "mean": 30.0})
        stats["sensor.gas_meter"].append({"start": ts, "sum": gas_total})
        stats["sensor.water_meter"].append({"start": ts, "sum": water_total})
    conf = {
        "rooms": ROOMS,
        "outdoor": "sensor.outside",
        "gas_meter": "sensor.gas_meter",
        "water": "sensor.water_meter",
        "boiler_efficiency": 0.88,
        "history_revision": 42,
    }
    now = START + timedelta(days=days)
    result = thermal_math.compute_all(stats, conf, UTC, now, (120,))
    return result, conf, stats, now


def test_complete_gas_is_retained_when_expected_heating_is_missing(thermal_math):
    result, conf, _, now = _expected_heating_gap_data(thermal_math)
    assert result["hlc"] is not None

    status = result["analysis_status"]["hlc"]
    expected_latest = (START.date() + timedelta(days=49)).isoformat()
    model_anchor = (START.date() + timedelta(days=32)).isoformat()
    assert status["status"] == "source_problem"
    assert "expected heating coverage is incomplete" in status["reason"]
    assert status["latest_source_day"] == expected_latest
    assert status["raw_latest_source_day"] == expected_latest
    assert status["latest_eligible_day"] == model_anchor
    assert status["eligible_source_lag_days"] == 17
    assert status["model_data_through"] == model_anchor
    assert status["raw_complete_gas_days"] == 50
    assert status["raw_complete_temperature_days"] == 50

    evidence_by_day = {
        date.fromisoformat(row["date"]): row
        for row in result["analysis_evidence"]["daily"]
    }
    missing_heating_day = START.date() + timedelta(days=49)
    record = evidence_by_day[missing_heating_day]
    assert record["gas_kwh"] is not None
    assert record["delta_t_k"] is not None
    assert record["eligible"]["hlc"] is False
    assert {
        (item["code"], item.get("room")) for item in record["exclusions"]
    } >= {
        ("expected_heating_gap", "room_2"),
        ("hlc_expected_heating_gap", None),
    }

    # A dependent split may retain its numeric result while readiness inherits
    # the stale HLC evidence state.
    raw_gas = {
        START.date() + timedelta(days=offset): 20.0
        for offset in range(50)
    }
    raw_delta = {
        START.date() + timedelta(days=offset): 10.0
        for offset in range(50)
    }
    eligible = {
        START.date() + timedelta(days=offset): 20.0
        for offset in range(33)
    }
    dependent = {
        "hlc": {"status": "valid", "days_used": 33},
        "air_change_rate": {"windows": 12, "sensor_count": 1},
        "losses": {"windows": 12},
    }
    dependent_conf = {
        **conf,
        "co2": "sensor.co2",
        "floor_area_m2": 80.0,
        "ceiling_height_m": 2.5,
        "experimental_whole_home_ventilation": True,
    }
    readiness = thermal_math.build_analysis_status(
        dependent,
        dependent_conf,
        UTC,
        now,
        raw_gas,
        {},
        START.date() + timedelta(days=32),
        heating_off=set(),
        dt_by_day=raw_delta,
        eligible_q_by_day=eligible,
        eligible_dt_by_day=eligible,
        eligible_hlc_days=set(eligible),
        eligibility_exclusion_days={
            "expected_heating_gap": {
                START.date() + timedelta(days=offset)
                for offset in range(33, 50)
            },
        },
    )
    assert readiness["hlc"]["status"] == "source_problem"
    assert readiness["losses"]["status"] == "source_problem"


def test_evidence_bundle_is_json_safe_private_and_alias_stable(thermal_math):
    result, _, _, _ = _expected_heating_gap_data(thermal_math)
    evidence = json.loads(json.dumps(result["analysis_evidence"], allow_nan=False))

    assert set(evidence) == {
        "schema_version", "generated_at", "timezone", "model_version",
        "history_revision", "configuration", "daily", "metrics",
        "uncertainty", "models",
    }
    assert evidence["schema_version"] == 1
    assert evidence["model_version"] == "1"
    assert evidence["history_revision"] == 42
    assert evidence["configuration"]["boiler_efficiency_used"] == 0.88
    assert set(evidence["configuration"]["rooms"]) == {"room_1", "room_2"}
    assert set(evidence["metrics"]["rooms"]) == {"room_1", "room_2"}
    assert "conditional regression confidence interval" in evidence["uncertainty"]["hlc"]
    assert evidence["models"]["hlc"]["days_used"] == result["hlc"]["days_used"]

    serialized = json.dumps(evidence, sort_keys=True)
    for private_value in (
        "sensor.gas_meter", "sensor.water_meter", "sensor.outside",
        "sensor.z_room_temp", "sensor.a_room_temp", "sensor.z_heating",
        "sensor.a_heating", "z_Private Bedroom", "a_Private Lounge",
    ):
        assert private_value not in serialized

    sensitivity = result["analysis_status"]["hlc"]["boiler_efficiency_sensitivity"]
    assert sensitivity["low_efficiency"] == pytest.approx(0.83)
    assert sensitivity["high_efficiency"] == pytest.approx(0.93)
    assert sensitivity["low_hlc_w_per_k"] < result["hlc"]["hlc_w_per_k"]
    assert sensitivity["high_hlc_w_per_k"] > result["hlc"]["hlc_w_per_k"]


def test_clamped_dhw_days_are_counted(thermal_math):
    day = date(2026, 1, 1)
    attributed, quality = thermal_math.attribute_dhw_by_day(
        {day: 1.0},
        {day: 5.0},
        set(),
        {"kwh_per_day": 20.0, "outdoor_mean": 15.0},
    )
    assert attributed[day] == 1.0
    assert quality["dhw_clamp_days"] == 1
