"""Focused pure-Python regressions for Phase 3.1, 3.2, and 4.2 pipeline fixes."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_loft_ratio_cold_night_gate_and_warm_fallback(thermal_math):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rooms = [{}]
    loft = {}
    outdoor = {}
    # 15 cold nights (t_out=4C, indoor=20C, dt=16K, ratio=0.10)
    for d in range(15):
        for h in range(1, 6):
            ts = int((start + timedelta(days=d, hours=h)).timestamp())
            outdoor[ts] = 4.0
            rooms[0][ts] = 20.0
            loft[ts] = 5.6
    # 15 warm nights (t_out=14C, indoor=21C, dt=7K > 6K, ratio=0.80)
    for d in range(15, 30):
        for h in range(1, 6):
            ts = int((start + timedelta(days=d, hours=h)).timestamp())
            outdoor[ts] = 14.0
            rooms[0][ts] = 21.0
            loft[ts] = 19.6

    res = thermal_math.loft_ratio(rooms, loft, outdoor, UTC, date(2026, 1, 1))
    assert res is not None
    assert res["cold_night_gate"] is True
    assert res["ratio"] == pytest.approx(0.10)
    assert res["iqr"] == pytest.approx(0.0)

    # When fewer than LOFT_MIN_HOURS (12) cold-night hours exist, fall back to dt > 6.0 K
    warm_only = thermal_math.loft_ratio(
        rooms,
        {ts: v for ts, v in loft.items() if ts >= int((start + timedelta(days=14)).timestamp())},
        outdoor,
        UTC,
        date(2026, 1, 1),
    )
    assert warm_only is not None
    assert warm_only["cold_night_gate"] is False
    assert warm_only["ratio"] == pytest.approx(0.80)


def test_build_analysis_status_surfaces_expected_heating_gap_lag_warning(thermal_math):
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    yesterday = date(2026, 11, 18)
    eligible_end = date(2026, 11, 16)
    days = [eligible_end - timedelta(days=i) for i in range(40)]
    q_by_day = {d: 50.0 for d in days}
    q_by_day[date(2026, 11, 17)] = 50.0
    q_by_day[yesterday] = 50.0
    dt_by_day = {d: 10.0 for d in q_by_day}
    eligible_days = set(days)
    conf = {
        "gas_meter": "sensor.gas",
        "outdoor": "sensor.out",
        "rooms": {"living": {"temperature": "sensor.living", "heating_power": "sensor.heat"}},
    }
    result = {
        "hlc": {"status": "valid", "days_used": 40, "model_data_from": min(days), "model_data_through": eligible_end},
        "dhw": {"status": "valid", "days_used": 20, "latest_eligible_gas_day": eligible_end},
        "usage": {"heating_off_days": 10, "modelled_days": 30, "dhw_kwh_per_day_30d": 10.0, "latest_eligible_gas_day": eligible_end},
    }
    status = thermal_math.build_analysis_status(
        result,
        conf,
        UTC,
        now,
        q_by_day,
        {},
        eligible_end,
        dt_by_day=dt_by_day,
        eligible_q_by_day={d: q_by_day[d] for d in eligible_days},
        eligible_dt_by_day={d: dt_by_day[d] for d in eligible_days},
        eligible_hlc_days=eligible_days,
        eligible_dhw_by_day={d: q_by_day[d] for d in eligible_days},
        eligibility_exclusion_days={
            "expected_heating_gap": {date(2026, 11, 17), yesterday},
        },
    )
    for metric in ("hlc", "dhw", "usage"):
        assert status[metric]["source_lag_days"] == 0
        assert status[metric]["eligible_source_lag_days"] == 2
        assert status[metric]["eligibility_degraded"] is True
        assert status[metric]["eligible_lag_warning"] is not None
        assert "expected heating-power coverage" in status[metric]["reason"]
        assert status[metric]["status"] == "valid"


def test_assignments_interpolation_rejects_vtrv_ema_decay_tail_edges(monkeypatch):
    pkg = types.ModuleType("custom_components")
    pkg.__path__ = [str(ROOT / "custom_components")]
    sub = types.ModuleType("custom_components.thermal_efficiency")
    sub.__path__ = [str(ROOT / "custom_components" / "thermal_efficiency")]
    monkeypatch.setitem(sys.modules, "custom_components", pkg)
    monkeypatch.setitem(sys.modules, "custom_components.thermal_efficiency", sub)

    from custom_components.thermal_efficiency.assignments import compose

    start = datetime(2026, 1, 1, tzinfo=UTC)
    t0 = int(start.timestamp())
    stats = {
        "owned.temp": [
            {"start": t0 - 3600, "mean": 20.5},
            {"start": t0, "mean": 20.8},
            {"start": t0 + 3600, "mean": 44.03},
            {"start": t0 + 4 * 3600, "mean": 21.0},
            {"start": t0 + 5 * 3600, "mean": 21.1},
        ]
    }
    data = {
        "migration": {"cutoff": t0 - 7200, "status": "complete", "sources": {}},
        "rooms": {"r": {"visits": [{"role": "temperature", "stream": "t", "start": None, "end": None, "cause": "legacy"}]}},
        "streams": {
            "t": {
                "entity_id": "owned.temp",
                "original_entity_id": "s.temp",
                "coverage": [
                    {"start": t0 - 7200, "end": t0 + 7200},
                    {"start": t0 + 4 * 3600, "end": None},
                ],
            }
        },
    }
    conf = {"rooms": {"r": {"temperature": "s.temp"}}, "outdoor": "o"}
    _, prepared = compose(stats, conf, data, UTC)
    assert prepared["rooms"]["r"]["hlc_interpolated_temperature_hours"] == []



def test_reject_indoor_temperature_spikes_edges_and_short_series(thermal_math):
    # Short series (<3) passes through untouched
    short = {0: 20.0, 3600: 44.0}
    cleaned, rej = thermal_math.reject_indoor_temperature_spikes(short)
    assert cleaned == short and rej == 0

    # Spike at start (i == 0) and spike at end (i == n - 1)
    s = {
        0: 44.0,
        3600: 20.5,
        7200: 20.6,
        10800: 20.4,
        14400: 43.0,
    }
    cleaned, rej = thermal_math.reject_indoor_temperature_spikes(s)
    assert rej == 2
    assert set(cleaned) == {3600, 7200, 10800}

    # Drop both edges of a 4-point series so loop terminates when n < 3
    s4 = {0: 44.0, 3600: 20.0, 7200: 20.1, 10800: 44.0}
    cleaned4, rej4 = thermal_math.reject_indoor_temperature_spikes(s4)
    assert rej4 == 2
    assert set(cleaned4) == {3600, 7200}


def test_two_var_ols_degenerate_and_fit_hlc_fallbacks(thermal_math):
    # Degenerate collinear inputs in _two_var_ols
    a, b1, b2, r2, det = thermal_math._two_var_ols([1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [5.0, 7.0, 9.0])
    assert det == 0.0 and b1 == 0.0 and b2 == 0.0

    # Small solar spread (< HLC_MIN_SOLAR_SPREAD): falls back to 1-var OLS
    start = date(2025, 12, 5)
    q_by_day = {}
    dt_by_day = {}
    for i in range(25):
        d = start + timedelta(days=i)
        dt = 6.0 + (i % 10)
        dt_by_day[d] = dt
        q_by_day[d] = 6.0 * dt - 5.0
    res = thermal_math.fit_hlc(q_by_day, dt_by_day, start, latitude_deg=51.46)
    assert res is not None
    assert res["solar_adjusted"] is False
    assert res["winter_hlc_w_per_k"] is not None

    # Winter days with insufficient dT spread (< 3K) produce winter_hlc_w_per_k = None
    q_flat_w = {}
    dt_flat_w = {}
    for i in range(22):
        d = date(2026, 1, 1) + timedelta(days=i)
        dt = 10.0 + (i % 2) * 0.5  # spread 0.5K < 3K
        dt_flat_w[d] = dt
        q_flat_w[d] = 6.0 * dt
    for i in range(15):
        d = date(2026, 4, 1) + timedelta(days=i)
        dt = 5.0 + (i % 4)
        dt_flat_w[d] = dt
        q_flat_w[d] = 6.0 * dt - 8.0
    res_flat_w = thermal_math.fit_hlc(q_flat_w, dt_flat_w, date(2026, 1, 1), latitude_deg=51.46)
    assert res_flat_w is not None
    assert res_flat_w["winter_hlc_w_per_k"] is None


def test_dhw_water_rate_edge_branches_and_idle_clamp(thermal_math):
    start = date(2026, 6, 1)
    days = [start + timedelta(days=i) for i in range(15)]
    off = set(days)
    outdoor = {d: 18.0 for d in days}

    # Constant fixture (zero variance in gas and litres) stays valid
    q_const = {d: 10.0 for d in days}
    w_const = {d: 500.0 for d in days}
    res_const = thermal_math.fit_dhw_water_rate(q_const, w_const, outdoor, off, start)
    assert res_const is not None and "correlation_r" not in res_const

    # Correlated (r >= 0.30) but high-intercept so zero-intercept water RMSE > baseline RMSE
    q_high_ic = {d: 10.0 + 0.0015 * (200.0 + 80.0 * i) for i, d in enumerate(days)}
    w_spread = {d: 200.0 + 80.0 * i for i, d in enumerate(days)}
    diag = {}
    res_bad_rmse = thermal_math.fit_dhw_water_rate(q_high_ic, w_spread, outdoor, off, start, diagnostics=diag)
    assert res_bad_rmse is None
    assert diag["status"] == "rejected"
    assert "RMSE" in diag["reason"]

    # attribute_dhw_by_day: 0 L with idle_gas > q (clamped) and water_rate=None
    d0 = date(2026, 1, 10)
    attributed, quality = thermal_math.attribute_dhw_by_day(
        {d0: 0.5},
        {d0: 5.0},
        set(),
        {"kwh_per_day": 10.0, "outdoor_mean": 18.0, "idle_gas_kwh_per_day": 1.5},
        water_by_day={d0: 0.0},
        water_rate=None,
    )
    assert attributed[d0] == pytest.approx(0.5)
    assert quality["dhw_clamp_days"] == 1
    assert quality["idle_attribution_days"] == 1
    assert quality["baseline_fallback_days"] == 1

    # fit_water_gas daily-batched with diagnostics=None returns None cleanly
    base_ts = int(datetime(2026, 6, 1, 0, 0, tzinfo=UTC).timestamp())
    gas_sum = {}
    water_sum = {}
    g_acc, w_acc = 0.0, 0.0
    for day_i in range(6):
        for h in range(24):
            ts = base_ts + (day_i * 24 + h) * 3600
            if h == 8:
                w_acc += 500.0
                g_acc += 2.0
            gas_sum[ts] = g_acc
            water_sum[ts] = w_acc
    assert thermal_math.fit_water_gas(gas_sum, water_sum, tz=UTC) is None


def test_air_change_rate_autodetect_5min_and_combine_mixed(thermal_math):
    import math

    # Auto-detect 5-minute cadence (cadence=None) across 12 clean decay windows
    # Include quiet floor points (~405 ppm) so sensor floor matches configured 400 ppm.
    t0 = int(datetime(2026, 11, 1, 0, 0, tzinfo=UTC).timestamp())
    co2_5m = {t0 - 3600 - k * 300: 405.0 for k in range(1, 20)}
    for w_idx in range(12):
        w_start = t0 + w_idx * 4 * 3600
        for step in range(24):  # 2 hours at 5-minute intervals
            ts = w_start + step * 300
            t_h = step * 300 / 3600.0
            co2_5m[ts] = 400.0 + 600.0 * math.exp(-0.4 * t_h)
    fit_5m = thermal_math.air_change_rate(co2_5m, UTC, date(2026, 10, 31), outdoor_baseline_ppm=400.0)
    assert fit_5m is not None
    assert fit_5m["cadence"] == "5minute"
    assert fit_5m["ach"] == pytest.approx(0.4, abs=0.02)

    # Fewer than 2 valid points returns None
    assert thermal_math.air_change_rate({t0: 500.0}, UTC, date(2026, 11, 1)) is None

    # Combine mixed cadences (5minute + hour)
    combined = thermal_math.combine_air_change_rates([
        {"ach": 0.4, "windows": 12, "baseline_ppm": 400.0, "cadence": "5minute", "baseline_source": "configured value"},
        {"ach": 0.2, "windows": 15, "baseline_ppm": 500.0, "cadence": "hour", "baseline_source": "indoor low-percentile (configured baseline below sensor floor)", "baseline_mismatch_note": "mismatch"},
    ])
    assert combined is not None
    assert combined["cadence"] == "mixed"
    assert combined["baseline_mismatch_note"] == "mismatch"
    assert "below sensor floor" in combined["baseline_source"]
