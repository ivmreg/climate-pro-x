"""Focused tests for CO2 outlier robustness, acceptance bounds, and non-bridging."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pytest

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "thermal_math_under_test",
    ROOT / "custom_components" / "thermal_efficiency" / "thermal_math.py",
)
assert _spec and _spec.loader
thermal_math = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(thermal_math)

from ha_efficiency import ventilation

TZ = ZoneInfo("Europe/London")
DAY = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
T0 = int(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp())


def test_co2_series_filters_max99999_even_with_valid_mean():
    rows = [
        {"start": T0, "mean": 9000.0, "max": 99999.0, "min": 850.0},
        {"start": T0 + 3600, "mean": 1500.0, "max": 99999.0, "min": 600.0},
    ]
    series, excluded = thermal_math.co2_series_from_stats(rows)
    assert series == {}
    assert excluded == 2


def test_co2_series_filters_invalid_min0():
    rows = [
        {"start": T0, "mean": 600.0, "min": 0.0, "max": 700.0},
        {"start": T0 + 3600, "mean": 600.0, "min": -10.0, "max": 700.0},
        {"start": T0 + 7200, "mean": 600.0, "min": 250.0, "max": 700.0},
    ]
    series, excluded = thermal_math.co2_series_from_stats(rows)
    assert series == {}
    assert excluded == 3


def test_co2_series_missing_extrema_compatibility():
    rows = [
        {"start": T0, "mean": 800.0},
        {"start": T0 + 3600, "mean": 850.0},
    ]
    series, excluded = thermal_math.co2_series_from_stats(rows)
    assert series == {T0: 800.0, T0 + 3600: 850.0}
    assert excluded == 0


def test_co2_series_nan_inf_sentinels():
    rows = [
        {"start": T0, "mean": float("nan")},
        {"start": T0 + 3600, "mean": float("inf")},
        {"start": T0 + 7200, "mean": 500.0, "max": float("inf")},
        {"start": T0 + 10800, "mean": 32767.0},
        {"start": T0 + 14400, "mean": 290.0},  # Below analysis range (< 300 ppm)
    ]
    series, excluded = thermal_math.co2_series_from_stats(rows)
    assert series == {}
    assert excluded == 5


def test_co2_series_all_invalid():
    rows = [
        {"start": T0 + i * 3600, "mean": 99999.0}
        for i in range(10)
    ]
    series, excluded = thermal_math.co2_series_from_stats(rows)
    assert series == {}
    assert excluded == 10


@pytest.mark.parametrize(
    "val,expected",
    [
        (300.0, True),
        (10000.0, True),
        (300, True),
        (10000, True),
        (299.99, False),
        (10000.01, False),
        (0.0, False),
        (-10.0, False),
        (99999.0, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
def test_co2_acceptance_boundary_validation(val, expected):
    assert thermal_math.is_valid_co2_value(val) is expected
    assert bool(ventilation.is_valid_co2_value(val)) is expected


def test_air_change_rate_coerces_numeric_strings(monkeypatch):
    monkeypatch.setattr(thermal_math, "CO2_MIN_WINDOWS", 1)
    monkeypatch.setattr(ventilation, "CO2_MIN_WINDOWS", 1)

    ach_true = 0.20
    base = 420.0
    co2_str = {
        T0 + h * 3600: f"{base + 580.0 * np.exp(-ach_true * h):.2f}"
        for h in range(6)
    }
    res = thermal_math.air_change_rate(co2_str, TZ, DAY, outdoor_baseline_ppm=base)
    assert res is not None
    assert res["ach"] == pytest.approx(ach_true, abs=0.03)

    pd_series = pd.Series({pd.Timestamp(ts, unit="s", tz="UTC"): v for ts, v in co2_str.items()})
    res_offline = ventilation.air_change_rate(pd_series, outdoor_baseline_ppm=base)
    assert res_offline is not None
    assert res_offline["ach"] == pytest.approx(ach_true, abs=0.03)


def test_air_change_rate_accepts_legitimate_high_curves_5000_to_10000():
    # 12 clean decay windows starting at 8000 ppm and decaying to ~5510 ppm over 5 hours
    # C(t) = 450 + 7550 * exp(-0.08 * h) -> all 6 samples stay within 5000..10000 ppm
    co2: thermal_math.Series = {}
    pd_records = {}
    for day in range(12):
        base = T0 + day * 86400
        for h in range(6):
            ts = base + h * 3600
            val = 450.0 + 7550.0 * np.exp(-0.08 * h)
            co2[ts] = float(val)
            pd_records[pd.Timestamp(ts, unit="s", tz="UTC")] = float(val)

    assert all(5000.0 <= v <= 10000.0 for v in co2.values())

    # Pure Python
    res = thermal_math.air_change_rate(co2, TZ, DAY, outdoor_baseline_ppm=450.0)
    assert res is not None
    assert res["ach"] == pytest.approx(0.08, abs=0.02)
    assert res["windows"] >= 10

    # Offline pandas
    pd_series = pd.Series(pd_records)
    res_offline = ventilation.air_change_rate(pd_series, outdoor_baseline_ppm=450.0)
    assert res_offline is not None
    assert res_offline["ach"] == pytest.approx(0.08, abs=0.02)
    assert res_offline["windows"] >= 10


@pytest.mark.parametrize("barrier_val", [float("nan"), float("inf"), 99999.0, 0.0, None])
def test_direct_invalid_barriers_and_no_bridging(monkeypatch, barrier_val):
    monkeypatch.setattr(thermal_math, "CO2_MIN_WINDOWS", 1)
    monkeypatch.setattr(ventilation, "CO2_MIN_WINDOWS", 1)

    # 30-minute intervals over 5 hours (11 points, hours 0.0 to 5.0, span 5h > 4h min window)
    # C(t) = 420 + 580 * exp(-0.25 * t)
    ach_true = 0.25
    base_co2 = 420.0
    times = [T0 + int(i * 1800) for i in range(11)]
    clean_co2: dict[int, object] = {
        t: base_co2 + 580.0 * np.exp(-ach_true * (i * 0.5))
        for i, t in enumerate(times)
    }

    # 1. Unbroken control curve must yield a valid fit
    ctrl_res = thermal_math.air_change_rate(clean_co2, TZ, DAY, outdoor_baseline_ppm=base_co2)
    assert ctrl_res is not None
    assert ctrl_res["ach"] == pytest.approx(ach_true, abs=0.03)

    ctrl_pd = pd.Series({pd.Timestamp(t, unit="s", tz="UTC"): v for t, v in clean_co2.items()})
    ctrl_offline = ventilation.air_change_rate(ctrl_pd, outdoor_baseline_ppm=base_co2)
    assert ctrl_offline is not None
    assert ctrl_offline["ach"] == pytest.approx(ach_true, abs=0.03)

    # 2. Insert barrier at index 5 (2.5h)
    # Piece before: indices 0..4 (0.0h..2.0h -> span 2.0h < 4.0h)
    # Piece after: indices 6..10 (3.0h..5.0h -> span 2.0h < 4.0h)
    # If the barrier is bridged across the 3600s gap (<= CO2_MAX_GAP_S 5400s), it would fit.
    # Because invalid values act as barriers, both pieces are < 4h and fit must be None.
    broken_co2 = dict(clean_co2)
    broken_co2[times[5]] = barrier_val

    res = thermal_math.air_change_rate(broken_co2, TZ, DAY, outdoor_baseline_ppm=base_co2)
    assert res is None

    broken_pd = pd.Series({pd.Timestamp(t, unit="s", tz="UTC"): v for t, v in broken_co2.items()})
    res_offline = ventilation.air_change_rate(broken_pd, outdoor_baseline_ppm=base_co2)
    assert res_offline is None


def test_recovery_after_excluded_hourly_bucket_breaks_window(monkeypatch):
    monkeypatch.setattr(thermal_math, "CO2_MIN_WINDOWS", 1)
    monkeypatch.setattr(ventilation, "CO2_MIN_WINDOWS", 1)

    ach_true = 0.20
    base_co2 = 420.0
    # Hourly data over 6 hours (span 5.0h > 4.0h min window)
    clean_rows = [
        {"start": T0 + h * 3600, "mean": base_co2 + 580.0 * np.exp(-ach_true * h)}
        for h in range(6)
    ]

    # 1. Complete clean rows must fit
    clean_series, clean_excluded = thermal_math.co2_series_from_stats(clean_rows)
    assert clean_excluded == 0
    clean_res = thermal_math.air_change_rate(clean_series, TZ, DAY, outdoor_baseline_ppm=base_co2)
    assert clean_res is not None
    assert clean_res["ach"] == pytest.approx(ach_true, abs=0.03)

    # 2. Same rows with hour 2 contaminated with max=99999
    # Drops hour 2 leaving a 2h gap (7200s > 5400s CO2_MAX_GAP_S).
    # Piece 1: hours 0..1 (span 1.0h < 4.0h).
    # Piece 2: hours 3..5 (span 2.0h < 4.0h).
    broken_rows = [dict(r) for r in clean_rows]
    broken_rows[2]["max"] = 99999.0

    broken_series, broken_excluded = thermal_math.co2_series_from_stats(broken_rows)
    assert broken_excluded == 1
    assert (T0 + 2 * 3600) not in broken_series
    broken_res = thermal_math.air_change_rate(broken_series, TZ, DAY, outdoor_baseline_ppm=base_co2)
    assert broken_res is None


def test_bad_explicit_or_outdoor_baseline_rejected(monkeypatch):
    monkeypatch.setattr(thermal_math, "CO2_MIN_WINDOWS", 1)
    monkeypatch.setattr(ventilation, "CO2_MIN_WINDOWS", 1)

    co2 = {T0 + h * 3600: 420.0 + 580.0 * np.exp(-0.2 * h) for h in range(6)}
    pd_series = pd.Series({pd.Timestamp(ts, unit="s", tz="UTC"): v for ts, v in co2.items()})

    assert thermal_math.air_change_rate(co2, TZ, DAY, outdoor_baseline_ppm=420.0) is not None
    assert ventilation.air_change_rate(pd_series, outdoor_baseline_ppm=420.0) is not None

    for bad_baseline in [99999.0, float("nan"), float("inf"), 0.0, -50.0, 150.0, 15000.0]:
        assert thermal_math.air_change_rate(co2, TZ, DAY, outdoor_baseline_ppm=bad_baseline) is None
        pd_series = pd.Series({pd.Timestamp(ts, unit="s", tz="UTC"): v for ts, v in co2.items()})
        assert ventilation.air_change_rate(pd_series, outdoor_baseline_ppm=bad_baseline) is None


def test_compute_all_co2_diagnostics_and_unaffected_non_co2_series():
    start_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    co2_rows = []
    # 12 clean decay windows, but with 2 contaminated rows inserted on day 0 and day 1
    for day in range(25):
        day_base = start_ts + day * 86400 + 12 * 3600
        for h in range(5):
            val = 420.0 + 380.0 * np.exp(-0.25 * h)
            row = {"start": day_base + h * 3600, "mean": val}
            if day == 0 and h == 2:
                row["max"] = 99999.0  # Diluted sentinel
            elif day == 1 and h == 3:
                row["min"] = 0.0  # Invalid low min
            co2_rows.append(row)

    outdoor_rows = []
    temp_rows = []
    gas_rows = []
    heat_rows = []
    cum_gas = 1000.0
    hlc_true = 120.0
    for day in range(25):
        ts = start_ts + day * 86400
        t_out = 0.0 + 8.0 * (day / 25)
        dt = 20.0 - t_out
        day_gas = hlc_true * dt * 24.0 / 1000.0
        for h in range(24):
            hour_ts = ts + h * 3600
            outdoor_rows.append({"start": hour_ts, "mean": t_out})
            temp_rows.append({"start": hour_ts, "mean": 20.0})
            heat_rows.append({"start": hour_ts, "mean": 50.0})
            cum_gas += day_gas / 24.0
            gas_rows.append({"start": hour_ts, "sum": cum_gas})

    stats = {
        "sensor.test_indoor_co2": co2_rows,
        "sensor.test_outdoor": outdoor_rows,
        "sensor.test_temp": temp_rows,
        "sensor.test_heat": heat_rows,
        "sensor.test_gas": gas_rows,
    }
    conf = {
        "rooms": {
            "test_room": {
                "temperature": "sensor.test_temp",
                "heating_power": "sensor.test_heat",
            }
        },
        "outdoor": "sensor.test_outdoor",
        "gas_meter": "sensor.test_gas",
        "boiler_efficiency": 1.0,
        "co2": "sensor.test_indoor_co2",
        "outdoor_co2_ppm": 420.0,
        "floor_area_m2": 100.0,
        "ceiling_height_m": 2.5,
    }
    now = datetime(2026, 1, 26, tzinfo=timezone.utc)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    # 1. CO2 excluded count recorded in source_issues
    assert "sensor.test_indoor_co2" in res.get("source_issues", {})
    assert "2 historical observation(s) were outside the 300-10000 ppm range and were ignored" in res["source_issues"]["sensor.test_indoor_co2"]

    # 2. ACH is successfully computed from remaining clean windows
    assert res["air_change_rate"] is not None
    assert res["air_change_rate"]["ach"] > 0
    assert res["air_change_rate"]["windows"] >= 10
    assert res["analysis_status"]["air_change_rate"]["status"] == "valid"

    # 3. Unrelated series (HLC, room temp, gas) are valid and unaffected
    assert res["hlc"] is not None
    assert res["hlc"]["delivered_hlc_w_per_k"] == pytest.approx(120.0, rel=0.05)


def test_compute_all_outdoor_co2_fallback_when_all_invalid():
    start_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    co2_rows = [
        {"start": start_ts + day * 86400 + h * 3600, "mean": 420.0 + 380.0 * np.exp(-0.25 * h)}
        for day in range(12)
        for h in range(5)
    ]
    # Outdoor CO2 sensor with only bad data
    outdoor_co2_rows = [
        {"start": start_ts + h * 3600, "mean": 99999.0}
        for h in range(24)
    ]

    stats = {
        "sensor.indoor_co2": co2_rows,
        "sensor.outdoor_co2": outdoor_co2_rows,
    }
    conf = {
        "rooms": {},
        "co2": "sensor.indoor_co2",
        "outdoor_co2_sensor": "sensor.outdoor_co2",
        "outdoor_co2_ppm": 410.0,  # Fallback configured value
    }
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    # Outdoor exclusion issue recorded
    assert "sensor.outdoor_co2" in res.get("source_issues", {})
    # ACH still computed using fallback configured outdoor baseline
    assert res["air_change_rate"] is not None
    assert res["air_change_rate"]["baseline_source"] == "configured value"
    assert res["air_change_rate"]["baseline_ppm"] == 410.0
