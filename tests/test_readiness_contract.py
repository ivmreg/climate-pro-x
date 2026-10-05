"""Boundary contracts for actionable readiness and rejected source evidence."""
from datetime import date, datetime, timedelta, timezone

import pytest

UTC = timezone.utc
NOW = datetime(2026, 7, 1, tzinfo=UTC)
DAY = date(2026, 6, 30)
CONF = {
    "gas_meter": "sensor.gas", "outdoor": "sensor.out", "water": "external:water",
    "electricity_meter": "sensor.electricity", "co2": ["sensor.co2"], "loft": "sensor.loft",
    "rooms": {"bed": {"temperature": "sensor.bed", "heating_power": "sensor.heat"}},
    "experimental_whole_home_ventilation": True, "floor_area_m2": 100, "ceiling_height_m": 2.5,
}


def status(math, result=None, conf=None, q=None, dt=None, anchor=DAY, diagnostics=None):
    return math.build_analysis_status(
        result or {}, CONF if conf is None else conf, UTC, NOW,
        {DAY: 40} if q is None else q, {}, anchor,
        hlc_diagnostics=diagnostics, dt_by_day={DAY: 10} if dt is None else dt,
    )


@pytest.mark.parametrize("role,metrics", [
    ("gas_meter", ("hlc", "dhw", "usage")), ("electricity_meter", ("electricity",)),
    ("water", ("water_usage",)), ("co2", ("air_change_rate",)), ("loft", ("loft",)),
])
def test_source_problem_is_actionable_and_cannot_look_valid(thermal_math, role, metrics):
    conf = {**CONF, "source_issues": {role: "unit changed after sensor replacement"}}
    states = status(thermal_math, conf=conf)
    for metric in metrics:
        assert states[metric]["status"] == "source_problem"
        assert states[metric]["reason"] == "unit changed after sensor replacement"
        assert states[metric]["next_action"]
    if role != "gas_meter":
        assert states["usage"]["status"] == "collecting"


@pytest.mark.parametrize("diagnostic,expected", [
    ({"status": "collecting", "reason": "fewer than 21 usable heating days", "usable_days": 9}, "collecting"),
    ({"status": "rejected", "reason": "insufficient temperature spread", "usable_days": 30}, "rejected"),
    (None, "rejected"),
])
def test_failed_hlc_keeps_reason_count_and_anchor(thermal_math, diagnostic, expected):
    state = status(thermal_math, diagnostics=diagnostic)["hlc"]
    assert state["status"] == expected
    assert state["model_data_through"] == DAY.isoformat()
    assert state["usable_observations"] == (diagnostic or {}).get("usable_days", 0)
    assert state["next_action"]


@pytest.mark.parametrize("q,dt,action", [
    ({}, {}, "gas meter"), ({DAY: 40}, {}, "temperature history"),
    ({DAY: 40}, {DAY: 10}, "heating activity"),
])
def test_missing_anchor_explains_which_evidence_is_needed(thermal_math, q, dt, action):
    state = status(thermal_math, q=q, dt=dt, anchor=None)["hlc"]
    assert state["status"] == "collecting"
    assert action in state["next_action"]
    assert state["usable_observations"] == len(set(q) & set(dt))


def test_stale_temperature_prevents_ready_heating_model(thermal_math):
    result = {"hlc": {"status": "valid", "days_used": 40}}
    state = status(thermal_math, result, dt={DAY - timedelta(days=20): 10})["hlc"]
    assert state["status"] == "source_problem"
    assert "temperature" in state["reason"]
    assert state["temperature_source_lag_days"] == 20
    assert state["source_lag_days"] == 0


def test_all_independent_models_have_evidence_and_scope(thermal_math):
    result = {
        "hlc": {"status": "provisional", "days_used": 25},
        "dhw": {"status": "provisional", "days_used": 10, "latest_complete_gas_day": DAY},
        "usage": {"heating_off_days": 10, "modelled_days": 15, "latest_complete_gas_day": DAY, "source_lag_days": 0},
        "electricity": {"current_period_days_used": 1, "days_used": 20, "latest_complete_day": DAY, "source_lag_days": 0},
        "water_usage": {"days_used": 20, "latest_complete_day": DAY, "source_lag_days": 0},
        "air_change_rate": {"windows": 15, "sensor_count": 2},
        "loft": {"hours_used": 150},
        "losses": {"windows": 15},
        "rooms": {"bed": {"nights_fitted": 3, "tau_median_h": 12, "last_night": DAY.isoformat()}},
    }
    states = status(thermal_math, result)
    for metric in ("hlc", "dhw", "usage", "electricity"):
        assert states[metric]["status"] == "provisional"
        assert states[metric]["usable_observations"] > 0
    for metric in ("water_usage", "air_change_rate", "loft", "losses"):
        assert states[metric]["status"] == "valid"
        assert states[metric]["usable_observations"] > 0
    assert states["rooms"]["bed"]["status"] == "provisional"
    # A physically impossible split must not inherit the upstream valid state.
    result.pop("losses")
    assert status(thermal_math, result)["losses"]["status"] == "rejected"
    result["losses_status"] = {"diagnostic_note": "ventilation exceeds total loss"}
    assert status(thermal_math, result)["losses"]["reason"] == "ventilation exceeds total loss"


def test_volume_and_temperature_configuration_are_required(thermal_math):
    assert status(thermal_math, conf={**CONF, "floor_area_m2": None})["losses"]["status"] == "not_configured"
    conf = {**CONF, "rooms": {"bed": {}}}
    assert status(thermal_math, conf=conf)["rooms"]["bed"]["status"] == "not_configured"
    conf = {**CONF, "source_issues": {"sensor.gas": "bad cumulative unit"}}
    assert status(thermal_math, conf=conf)["hlc"]["status"] == "source_problem"


def test_recorder_numeric_formats_and_invalid_values(thermal_math):
    ts = int(NOW.timestamp())
    rows = [
        {"start": NOW, "mean": "20"}, {"start": (ts + 3600) * 1000, "mean": 21},
        {"start": ts + 7200, "mean": None}, {"start": ts + 10800, "mean": float("nan")},
        {"start": ts + 14400, "mean": float("inf")}, {"start": ts + 18000, "mean": "broken"},
    ]
    assert thermal_math.series_from_stats(rows, "mean") == {ts: 20.0, ts + 3600: 21.0}


@pytest.mark.parametrize("gas,expected", [(8, "valid"), (100, "rejected")])
def test_positive_water_rate_reports_physical_rejection(thermal_math, gas, expected):
    days = [DAY - timedelta(days=n) for n in range(12)]
    diagnostics = {}
    fit = thermal_math.fit_dhw_water_rate(
        {d: gas for d in days}, {d: 200 for d in days}, {d: 18 for d in days},
        set(days), min(days), min_water_l=0, diagnostics=diagnostics,
    )
    assert diagnostics["status"] == expected
    assert diagnostics["usable_observations"] == 12
    assert (fit is not None) == (expected == "valid")


@pytest.mark.parametrize("mode", ["none", "sparse", "bad_fit", "future"])
def test_hourly_water_regression_rejection_diagnostics(thermal_math, mode):
    start = int(NOW.timestamp())
    gas, water = {start - 3600: 0.0}, {start - 3600: 0.0}
    g = w = 0.0
    for hour in range(250):
        increment = 5 + hour % 35
        w += increment
        g += 1 if mode == "bad_fit" else increment * 0.01
        gas[start + hour * 3600] = g
        water[start + hour * 3600] = w
    if mode == "none":
        water = {}
    elif mode == "sparse":
        water = dict(list(water.items())[:15])
    diag = {}
    fit = thermal_math.fit_water_gas(
        gas, water, tz=UTC, since=DAY + timedelta(days=40) if mode == "future" else None,
        diagnostics=diag,
    )
    assert fit is None
    assert diag["status"] == ("rejected" if mode == "bad_fit" else "collecting")
    assert diag["reason"]
    assert diag["eligible_observations"] < 200 or mode == "bad_fit"
