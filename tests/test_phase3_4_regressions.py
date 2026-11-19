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
