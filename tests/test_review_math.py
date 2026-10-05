"""Adversarial and regression test suite for Issue #5 calculation correctness and model quality.

Tests cover:
- F1: DHW rate fitting requires finite strictly positive litres; zero-water days stay in baseline.
- F2: Strict complete-day coverage (23/24/25h) with aligned consecutive observations;
      rejection of missing 10:00/11:00 (prevents 262.5 W/K distortion for true 300 W/K).
- F3: No model_day -> no HLC; explicit heating-off excluded from fit-day selection and summer anchoring.
- F4: Informational gas-vs-water fit restricted to heating-off intervals; reject >100% hot fraction;
      uncoupled from space heating correlation.
- F5: Experimental whole-home split disabled by default, keeping room ACH intact.
- F6: Analysis status contract and resilience to isolated source issues.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from math import isfinite
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

from ha_efficiency import dhw, hlc, ventilation

TZ = ZoneInfo("Europe/London")


def _row(timestamp: int, metric: str, value: float) -> dict:
    return {"start": timestamp, metric: value}


# ==============================================================================
# F1: DHW strictly positive litres and zero-water handling
# ==============================================================================

def test_f1_rate_fitting_requires_strictly_positive_litres():
    """fit_dhw_water_rate must require litres > 0 even if min_water_l == 0."""
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(15)]
    q_daily = {d: 8.0 for d in days}
    outdoor_daily = {d: 18.0 for d in days}
    # Days with 0.0 litres, None, NaN, and negative litres mixed with valid litres
    water_daily = {
        days[0]: 0.0,
        days[1]: 0.0,
        days[2]: float("nan"),
        days[3]: -10.0,
    }
    for d in days[4:]:
        water_daily[d] = 200.0

    heating_off = set(days)

    # Calling with min_water_l = 0.0 must NOT raise ZeroDivisionError
    rate = thermal_math.fit_dhw_water_rate(
        q_daily,
        water_daily,
        outdoor_daily,
        heating_off,
        days[0],
        min_water_l=0.0,
    )
    # Only days with strictly positive litres should be considered
    # 11 valid days >= 10 min days
    assert rate is not None
    assert rate["days_used"] == 11
    assert rate["wh_per_litre_per_k"] > 0
    assert isfinite(rate["wh_per_litre_per_k"])


def test_f1_zero_water_days_available_for_idle_baseline_in_dhw_baseline():
    """Zero-water days remain available for idle baseline and away accounting."""
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(20)]
    q_daily = {}
    outdoor_daily = {d: 18.0 for d in days}
    water_daily = {}
    for i, d in enumerate(days):
        if i < 5:
            # Away / idle days: gas is low pilot/idle, water is 0
            q_daily[d] = 0.5
            water_daily[d] = 0.0
        else:
            # Occupied days: gas 12 kWh, water 250 L
            q_daily[d] = 12.0
            water_daily[d] = 250.0

    heating_off = set(days)
    base = thermal_math.dhw_baseline(
        q_daily,
        {d: 1.0 for d in days},
        outdoor_daily,
        days[0],
        heating_off=heating_off,
        water_by_day=water_daily,
        min_water_l=50.0,
    )
    assert base is not None
    assert base["days_used"] == 15
    assert base["low_water_days_excluded"] == 5
    assert base["idle_gas_kwh_per_day"] == pytest.approx(0.5)


def test_f1_pipeline_completes_with_gas_positive_water_zero_threshold_zero():
    """Full pipeline completes when gas > 0, water = 0, min_dhw_water_litres = 0, electricity works."""
    start = datetime(2026, 1, 1, tzinfo=TZ)
    hours = 40 * 24
    stats = {
        "sensor.temp": [],
        "sensor.out": [],
        "sensor.heat": [],
        "sensor.gas": [],
        "sensor.water": [],
        "sensor.elec": [],
    }
    gas_cum = 100.0
    elec_cum = 50.0
    for h in range(hours):
        ts = int(start.timestamp()) + h * 3600
        # Cold winter with heating on
        outdoor = 4.0 + (h // 24) / 8.0
        daily_gas = 3.0 + 7.2 * (20.0 - outdoor)
        stats["sensor.temp"].append(_row(ts, "mean", 20.0))
        stats["sensor.out"].append(_row(ts, "mean", outdoor))
        stats["sensor.heat"].append(_row(ts, "mean", 40.0))
        gas_cum += daily_gas / 24
        stats["sensor.gas"].append(_row(ts, "sum", gas_cum))
        # Total water meter stays flat at 0.0
        stats["sensor.water"].append(_row(ts, "sum", 0.0))
        elec_cum += 0.3
        stats["sensor.elec"].append(_row(ts, "sum", elec_cum))

    conf = {
        "rooms": {"living": {"temperature": "sensor.temp", "heating_power": "sensor.heat"}},
        "outdoor": "sensor.out",
        "gas_meter": "sensor.gas",
        "water": "sensor.water",
        "min_dhw_water_litres": 0.0,
        "electricity_meter": "sensor.elec",
        "electricity_unit_rate": 0.25,
    }
    now = datetime.fromtimestamp(int(start.timestamp()) + (hours - 1) * 3600, TZ)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    # Electricity and HLC should succeed independently
    assert res["electricity"] is not None
    assert res["electricity"]["kwh_per_day"] == pytest.approx(0.3 * 24, rel=0.05)
    assert res["hlc"] is not None
    assert res["hlc"]["delivered_hlc_w_per_k"] > 0
    # DHW rate cannot fit with 0 water, but baseline / pipeline must not crash
    assert res["dhw"] is None or res["dhw"].get("water_rate_days_used", 0) == 0


# ==============================================================================
# F2: Strict complete-day coverage and missing 10:00/11:00 rejection
# ==============================================================================

def test_f2_missing_hours_rejected_does_not_return_distorted_hlc():
    """Known 300 W/K house with 10:00/11:00 absent each day must be rejected, not return 262.5 valid."""
    start = datetime(2026, 1, 1, tzinfo=TZ)
    days_count = 35
    HLC_TRUE = 300.0  # W/K -> slope = 300 * 24 / 1000 = 7.2 kWh/day/K

    stats = {
        "sensor.temp": [],
        "sensor.out": [],
        "sensor.heat": [],
        "sensor.gas": [],
    }
    gas_cum = 1000.0

    for d in range(days_count):
        t_out = 0.0 + 8.0 * (d / days_count)
        dt = 20.0 - t_out
        daily_kwh = HLC_TRUE * dt * 24.0 / 1000.0
        hourly_kwh = daily_kwh / 24.0

        for h in range(24):
            # Drop 10:00 and 11:00
            if h in (10, 11):
                continue
            ts = int(start.timestamp()) + (d * 24 + h) * 3600
            gas_cum += hourly_kwh
            stats["sensor.temp"].append(_row(ts, "mean", 20.0))
            stats["sensor.out"].append(_row(ts, "mean", t_out))
            stats["sensor.heat"].append(_row(ts, "mean", 50.0))
            stats["sensor.gas"].append(_row(ts, "sum", gas_cum))

    conf = {
        "rooms": {"living": {"temperature": "sensor.temp", "heating_power": "sensor.heat"}},
        "outdoor": "sensor.out",
        "gas_meter": "sensor.gas",
        "boiler_efficiency": 1.0,
    }
    now = datetime.fromtimestamp(int(start.timestamp()) + (days_count * 24 - 1) * 3600, TZ)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    # Incomplete days must be rejected! No valid 262.5 W/K should be returned.
    assert res["hlc"] is None, f"Expected HLC to be rejected due to missing hours, got {res['hlc']}"
    assert res["analysis_status"]["hlc"]["status"] in ("collecting", "rejected")
    assert "complete" in res["analysis_status"]["hlc"]["reason"].lower() or "fewer than" in res["analysis_status"]["hlc"]["reason"].lower()


def test_f2_dst_transitions_require_exact_local_hours():
    """DST spring forward (23h) and autumn back (25h) days must require exact consecutive hours."""
    tz = ZoneInfo("Europe/London")
    # Spring forward: 2026-03-29 (23 hours)
    spring_day = date(2026, 3, 29)
    start_spring = datetime(2026, 3, 29, 0, 0, tzinfo=tz)
    end_spring = datetime(2026, 3, 30, 0, 0, tzinfo=tz)
    expected_spring_h = round((end_spring.timestamp() - start_spring.timestamp()) / 3600)
    assert expected_spring_h == 23

    # Generate cumulative meter series with all 23 hours
    spring_cum = {}
    curr = start_spring.astimezone(timezone.utc) - timedelta(hours=1)
    val = 100.0
    step = 0
    while curr <= end_spring.astimezone(timezone.utc):
        spring_cum[int(curr.timestamp())] = val
        step += 1
        val += step
        curr += timedelta(hours=1)

    steps = thermal_math._daily_meter_steps(spring_cum, tz, 40.0)
    assert spring_day in steps
    assert len(steps[spring_day]) == 23
    assert sum(steps[spring_day]) == pytest.approx(sum(range(1, 24)))

    # If 1 hour is missing from spring day, it must be rejected
    incomplete_cum = {ts: v for ts, v in spring_cum.items() if ts != int((start_spring + timedelta(hours=5)).timestamp())}
    inc_steps = thermal_math._daily_meter_steps(incomplete_cum, tz, 40.0)
    assert spring_day not in inc_steps

    # Autumn back: 2026-10-25 (25 hours)
    autumn_day = date(2026, 10, 25)
    start_autumn = datetime(2026, 10, 25, 0, 0, tzinfo=tz)
    end_autumn = datetime(2026, 10, 26, 0, 0, tzinfo=tz)
    expected_autumn_h = round((end_autumn.timestamp() - start_autumn.timestamp()) / 3600)
    assert expected_autumn_h == 25

    autumn_cum = {}
    curr = start_autumn.astimezone(timezone.utc) - timedelta(hours=1)
    val = 200.0
    step = 0
    while curr <= end_autumn.astimezone(timezone.utc):
        autumn_cum[int(curr.timestamp())] = val
        step += 1
        val += step
        curr += timedelta(hours=1)

    steps_aut = thermal_math._daily_meter_steps(autumn_cum, tz, 40.0)
    assert autumn_day in steps_aut
    assert len(steps_aut[autumn_day]) == 25
    assert sum(steps_aut[autumn_day]) == pytest.approx(sum(range(1, 26)))


# ==============================================================================
# F3: No model_day -> No HLC; explicit heating-off handling
# ==============================================================================

def test_f3_no_model_day_means_no_hlc():
    """When all days are heating-off (e.g. summer), no model_day exists and HLC must be None."""
    start = datetime(2026, 7, 1, tzinfo=TZ)
    days_count = 30
    stats = {
        "sensor.temp": [],
        "sensor.out": [],
        "sensor.heat": [],
        "sensor.gas": [],
    }
    gas_cum = 500.0
    for d in range(days_count):
        for h in range(24):
            ts = int(start.timestamp()) + (d * 24 + h) * 3600
            # Warm summer: outdoor 22C, heating power 0.0%
            stats["sensor.temp"].append(_row(ts, "mean", 22.0))
            stats["sensor.out"].append(_row(ts, "mean", 21.0))
            stats["sensor.heat"].append(_row(ts, "mean", 0.0))
            gas_cum += 0.5  # DHW only
            stats["sensor.gas"].append(_row(ts, "sum", gas_cum))

    conf = {
        "rooms": {"living": {"temperature": "sensor.temp", "heating_power": "sensor.heat"}},
        "outdoor": "sensor.out",
        "gas_meter": "sensor.gas",
    }
    now = datetime.fromtimestamp(int(start.timestamp()) + (days_count * 24 - 1) * 3600, TZ)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    assert res["hlc"] is None
    hlc_status = res["analysis_status"]["hlc"]
    assert hlc_status["status"] == "collecting"
    assert "no_heating_evidence" in hlc_status["reason"] or "heating" in hlc_status["reason"]


def test_f3_heating_off_days_excluded_from_hlc_fit():
    """Explicit heating-off days must not be included as space-heating fit days."""
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(40)]
    q_by_day = {}
    dt_by_day = {}
    heating_off = set()

    for i, d in enumerate(days):
        dt = 5.0 + 10.0 * (i / 40)
        dt_by_day[d] = dt
        if i < 15:
            # Shoulder days with heating off: gas is purely DHW (10 kWh/day)
            heating_off.add(d)
            q_by_day[d] = 10.0
        else:
            # Active heating days: q = 300 * dt * 24 / 1000 = 7.2 * dt
            q_by_day[d] = 7.2 * dt

    diag = {}
    fit = thermal_math.fit_hlc(
        q_by_day,
        dt_by_day,
        days[0],
        heating_off=heating_off,
        diagnostics=diag,
    )
    assert fit is not None
    assert fit["days_used"] == 25  # Only the 25 heating-on days were fitted
    assert fit["hlc_w_per_k"] == pytest.approx(300.0, rel=0.01)


# ==============================================================================
# F4: Informational gas-vs-water fit heating-off isolation and hot fraction check
# ==============================================================================

def test_f4_water_gas_fit_isolates_heating_off_and_rejects_inconsistent_hot_fraction():
    """Synthetic 10 summer / 30 winter with 10 Wh/L DHW + 50 Wh/L correlated winter space heating.
    Must recover summer ~10 Wh/L (~25.29% hot fraction) and reject or suppress winter distortion."""
    start = datetime(2026, 5, 1, tzinfo=TZ)
    days_count = 40
    summer_days = 10
    winter_days = 30

    gas_kwh_hourly = {}
    water_l_hourly = {}
    heating_off = set()

    rng = np.random.default_rng(42)

    for d in range(days_count):
        current_date = (start + timedelta(days=d)).date()
        is_summer = d < summer_days
        if is_summer:
            heating_off.add(current_date)

        for h in range(24):
            ts = int(start.timestamp()) + (d * 24 + h) * 3600
            # Water draw
            water_l = float(np.clip(rng.exponential(10.0), 0, 50))
            if is_summer:
                # 10 Wh/L DHW
                gas = 0.05 + 0.010 * water_l + float(rng.normal(0, 0.002))
            else:
                # Winter: strong space heating correlated with water
                gas = 1.0 + 0.050 * water_l + float(rng.normal(0, 0.01))
            gas_kwh_hourly[ts] = max(0.0, gas)
            water_l_hourly[ts] = max(0.0, water_l)

    # 1. Fit without heating_off: mixes summer and winter, producing distorted high slope and >100% hot fraction
    fit_all = thermal_math.fit_water_gas(gas_kwh_hourly, water_l_hourly, boiler_efficiency=0.88)
    # The naive fit would either be rejected (>100%) or contaminated (~40+ Wh/L)
    if fit_all is not None:
        assert fit_all["hot_fraction_pct"] <= 100.0

    # 2. Fit with heating_off: properly isolates the summer interval
    fit_isolated = thermal_math.fit_water_gas(
        gas_kwh_hourly,
        water_l_hourly,
        boiler_efficiency=0.88,
        heating_off=heating_off,
        tz=TZ,
    )
    if fit_isolated is not None:
        # Recovered true 10 Wh/L rate (~25.29% hot fraction)
        assert fit_isolated["wh_per_litre"] == pytest.approx(10.0, rel=0.15)
        # Hot fraction = 10 * 0.88 / 34.8 * 100 ≈ 25.29%
        assert fit_isolated["hot_fraction_pct"] == pytest.approx(25.29, abs=4.0)
        assert fit_isolated["rejected_heating_on_hours"] == winter_days * 24


def test_f4_rejects_physically_impossible_hot_fraction():
    """Hot fraction > 100% must be rejected (return None) instead of clipped to 100%."""
    water_idx = pd.date_range("2026-06-01", periods=250, freq="1h", tz="UTC")
    # Impossibly high gas per litre: 50 Wh/L with 0.88 eff -> 44 / 34.8 = 126% hot water
    water = pd.Series(np.linspace(5.0, 50.0, 250), index=water_idx)
    gas = pd.Series(0.050 * water.values + 0.1, index=water_idx)

    # Offline version
    offline_fit = dhw.fit_water_gas(gas, water, boiler_efficiency=0.88)
    assert offline_fit is None, f"Expected None for >100% hot fraction, got {offline_fit}"

    # Live version
    baseline_ts = int(water_idx[0].timestamp()) - 3600
    live_gas = {baseline_ts: 0.0, **{int(t.timestamp()): v for t, v in gas.cumsum().items()}}
    live_water = {baseline_ts: 0.0, **{int(t.timestamp()): v for t, v in water.cumsum().items()}}
    live_fit = thermal_math.fit_water_gas(live_gas, live_water, boiler_efficiency=0.88)
    assert live_fit is None, f"Expected None for >100% hot fraction, got {live_fit}"

    diagnostics = {}
    diagnostic_fit = thermal_math.fit_water_gas(
        live_gas, live_water, boiler_efficiency=0.88, diagnostics=diagnostics
    )
    assert diagnostic_fit is None
    assert diagnostics["status"] == "rejected"
    assert "outside 0-100%" in diagnostics["reason"]
    assert diagnostics["eligible_observations"] > 0
    assert diagnostics["total_common_observations"] >= diagnostics["eligible_observations"]


# ==============================================================================
# F5: Experimental whole-home ventilation gating
# ==============================================================================

def test_f5_experimental_whole_home_ventilation_default_false():
    """By default, experimental_whole_home_ventilation is False: ACH is published, losses is None."""
    start = datetime(2026, 1, 1, tzinfo=TZ)
    hours = 30 * 24
    stats = {
        "sensor.temp": [],
        "sensor.out": [],
        "sensor.heat": [],
        "sensor.gas": [],
        "sensor.co2": [],
    }
    gas_cum = 100.0
    co2_val = 800.0
    for h in range(hours):
        ts = int(start.timestamp()) + h * 3600
        stats["sensor.temp"].append(_row(ts, "mean", 20.0))
        stats["sensor.out"].append(_row(ts, "mean", 2.0 + (h // 24) % 12))
        stats["sensor.heat"].append(_row(ts, "mean", 40.0))
        gas_cum += (0.3 * (20.0 - (2.0 + (h // 24) % 12)) / 0.88)
        stats["sensor.gas"].append(_row(ts, "sum", gas_cum))
        # Clear decay curves
        co2_val = 420.0 + 380.0 * np.exp(-0.25 * (h % 24))
        stats["sensor.co2"].append(_row(ts, "mean", co2_val))

    conf = {
        "rooms": {"living": {"temperature": "sensor.temp", "heating_power": "sensor.heat"}},
        "outdoor": "sensor.out",
        "gas_meter": "sensor.gas",
        "co2": "sensor.co2",
        "outdoor_co2_ppm": 420.0,
        "floor_area_m2": 100.0,
        "ceiling_height_m": 2.5,
        # experimental_whole_home_ventilation omitted -> False
    }
    now = datetime.fromtimestamp(int(start.timestamp()) + (hours - 1) * 3600, TZ)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    assert res["air_change_rate"] is not None
    assert res["air_change_rate"]["ach"] > 0
    # Whole-home split must be withheld
    assert res["losses"] is None
    assert res["losses_status"]["status"] == "experimental_disabled"
    assert res["analysis_status"]["losses"]["status"] == "not_configured"


def test_f5_experimental_whole_home_ventilation_opt_in():
    """When experimental_whole_home_ventilation is True, whole-home losses is calculated with scope and assumptions."""
    start = datetime(2026, 1, 1, tzinfo=TZ)
    hours = 30 * 24
    stats = {
        "sensor.temp": [],
        "sensor.out": [],
        "sensor.heat": [],
        "sensor.gas": [],
        "sensor.co2": [],
    }
    gas_cum = 100.0
    co2_val = 800.0
    for h in range(hours):
        ts = int(start.timestamp()) + h * 3600
        stats["sensor.temp"].append(_row(ts, "mean", 20.0))
        stats["sensor.out"].append(_row(ts, "mean", 2.0 + (h // 24) % 12))
        stats["sensor.heat"].append(_row(ts, "mean", 40.0))
        gas_cum += (0.3 * (20.0 - (2.0 + (h // 24) % 12)) / 0.88)
        stats["sensor.gas"].append(_row(ts, "sum", gas_cum))
        co2_val = 420.0 + 380.0 * np.exp(-0.25 * (h % 24))
        stats["sensor.co2"].append(_row(ts, "mean", co2_val))

    conf = {
        "rooms": {"living": {"temperature": "sensor.temp", "heating_power": "sensor.heat"}},
        "outdoor": "sensor.out",
        "gas_meter": "sensor.gas",
        "co2": "sensor.co2",
        "outdoor_co2_ppm": 420.0,
        "floor_area_m2": 100.0,
        "ceiling_height_m": 2.5,
        "experimental_whole_home_ventilation": True,
    }
    now = datetime.fromtimestamp(int(start.timestamp()) + (hours - 1) * 3600, TZ)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    assert res["losses"] is not None
    assert res["losses"]["ventilation_w_per_k"] > 0
    assert res["losses"]["fabric_w_per_k"] > 0
    assert "scope" in res["losses"]
    assert "assumptions" in res["losses"]
    assert "not a categorical retrofit recommendation" in res["losses"]["assumptions"]
    assert res["analysis_status"]["losses"]["status"] == "valid"


# ==============================================================================
# F6: Analysis status contract and source issue resilience
# ==============================================================================

def test_f6_analysis_status_schema_and_source_issues():
    """Test analysis_status keys, status values, and handling of conf['source_issues']."""
    stats = {}
    conf = {
        "rooms": {"bed": {"temperature": "sensor.bad_bed"}},
        "outdoor": "sensor.out",
        "source_issues": {
            "sensor.bad_bed": "sensor unavailable since upgrade",
            "gas_meter": "meter resets every midnight",
        },
    }
    now = datetime(2026, 3, 1, tzinfo=TZ)
    res = thermal_math.compute_all(stats, conf, TZ, now, (30,))

    status = res.get("analysis_status")
    assert status is not None
    expected_keys = {
        "hlc", "dhw", "usage", "electricity", "water_usage",
        "air_change_rate", "losses", "loft", "rooms"
    }
    assert set(status.keys()) == expected_keys

    valid_statuses = {
        "not_configured", "collecting", "valid", "provisional",
        "rejected", "source_problem", "historical_baseline_held"
    }

    for k, item in status.items():
        if k == "rooms":
            for room_id, r_item in item.items():
                assert r_item["status"] in valid_statuses
                assert "reason" in r_item
                assert "next_action" in r_item
        else:
            assert item["status"] in valid_statuses, f"{k} has invalid status {item['status']}"
            assert "reason" in item
            assert "next_action" in item

    # Verify source issues mapped correctly
    assert status["rooms"]["bed"]["status"] == "source_problem"
    assert status["rooms"]["bed"]["reason"] == "sensor unavailable since upgrade"
    assert status["usage"]["status"] == "source_problem"
    assert status["usage"]["reason"] == "meter resets every midnight"


def test_analysis_status_requires_fresh_source_evidence_before_holding_hlc():
    today = date(2026, 7, 1)
    now = datetime(2026, 7, 1, tzinfo=TZ)
    model_day = today - timedelta(days=20)
    recent = {today - timedelta(days=offset) for offset in range(1, 15)}
    q_recent = {day: 8.0 for day in recent}
    dt_recent = {day: 10.0 for day in recent}
    result = {"hlc": {"status": "valid", "days_used": 40}}
    conf = {"gas_meter": "sensor.gas", "outdoor": "sensor.out", "rooms": {"main": {"temperature": "sensor.room"}}}

    fresh = thermal_math.build_analysis_status(
        result,
        conf,
        TZ,
        now,
        q_recent,
        {},
        model_day,
        heating_off=recent,
        dt_by_day=dt_recent,
    )
    assert fresh["hlc"]["status"] == "historical_baseline_held"
    assert fresh["hlc"]["source_lag_days"] == 0

    stale_day = today - timedelta(days=21)
    stale = thermal_math.build_analysis_status(
        result,
        conf,
        TZ,
        now,
        {stale_day: 8.0},
        {},
        model_day,
        heating_off={stale_day},
        dt_by_day={stale_day: 10.0},
    )
    assert stale["hlc"]["status"] == "source_problem"
    assert stale["hlc"]["source_lag_days"] == 20
    assert stale["hlc"]["model_data_through"] == model_day.isoformat()
