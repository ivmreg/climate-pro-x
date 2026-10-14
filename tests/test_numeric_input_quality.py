"""Role bounds must reject corrupted data without suppressing real edge cases."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from math import exp
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ha_efficiency import cooling, dhw, hlc, loft, quality, store

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("converter,valid", [
    ("temperature_series_from_stats", [-90, -25, 0, 55, 80]),
    ("humidity_series_from_stats", [0, 50, 100]),
    ("heating_power_series_from_stats", [0, 50, 100]),
])
def test_bounded_buckets_preserve_edges_and_reject_diluted_spikes(thermal_math, converter, valid):
    clean = [{"start": i * 3600, "mean": value} for i, value in enumerate(valid)]
    convert = getattr(thermal_math, converter)
    good, rejected = convert(clean)
    assert list(good.values()) == valid
    assert rejected == 0
    for field, bad in [("mean", "bad"), ("mean", float("nan")),
                       ("max", 99999), ("min", -999), ("max", float("inf"))]:
        broken = [dict(row) for row in clean]
        broken[1][field] = bad
        filtered, rejected = convert(broken)
        assert rejected == 1
        assert 3600 not in filtered
        assert filtered[0] == valid[0]


def test_compute_all_uses_bounds_and_reports_every_source(thermal_math, monkeypatch):
    captured = {}
    original = thermal_math.daily_delta_t

    def capture(rooms, outdoor, tz):
        captured.update(rooms=rooms, outdoor=outdoor)
        return original(rooms, outdoor, tz)

    monkeypatch.setattr(thermal_math, "daily_delta_t", capture)
    stats = {
        "room": [{"start": 0, "mean": 20}, {"start": 3600, "mean": 21, "max": 99999}],
        "outside": [{"start": 0, "mean": -20}, {"start": 3600, "mean": -999}],
        "humidity": [{"start": 0, "mean": 50, "max": 101}],
        "heat": [{"start": 0, "mean": 0}, {"start": 3600, "mean": 0, "max": 99999}],
        "loft": [{"start": 0, "mean": 55}, {"start": 3600, "mean": 99999}],
        "loft_rh": [{"start": 0, "mean": 99999}],
    }
    conf = {"rooms": {"test": {"temperature": "room", "humidity": "humidity", "heating_power": "heat"}},
            "outdoor": "outside", "loft": "loft", "loft_humidity": "loft_rh",
            "gas_unit_rate": "broken", "electricity_unit_rate": float("inf")}
    result = thermal_math.compute_all(stats, conf, timezone.utc, START, (30,))
    assert captured["rooms"] == [{0: 20.0}]
    assert captured["outdoor"] == {0: -20.0}
    assert set(result["source_issues"]) == set(stats) | {"gas_unit_rate", "electricity_unit_rate"}
    assert "1 historical" in result["source_issues"]["room"]
    assert result["loft"] is None


def test_temperature_and_heating_gaps_prevent_cooling_fit(thermal_math):
    room = {int((START + timedelta(hours=h)).timestamp()): 5 + 15 * exp(-h / 15) for h in range(6)}
    outside = {ts: 5 for ts in room}
    heat = {ts: 0 for ts in room}
    assert thermal_math.night_taus(room, outside, heat, timezone.utc, START.date())
    missing = list(room)[3]
    # Even one rejected demand sample cannot establish that heating was off.
    assert thermal_math.night_taus(room, outside, {ts: v for ts, v in heat.items() if ts != missing}, timezone.utc, START.date()) == []
    assert thermal_math.night_taus({ts: v for ts, v in room.items() if ts != missing}, outside, heat, timezone.utc, START.date()) == []


def test_meter_large_totals_offsets_spikes_and_recovery(thermal_math):
    values = {0: 99999, 3600: 100000, 7200: 99999999, 10800: 100002, 14400: 100003}
    assert thermal_math.hourly_change(values, 40) == {3600: 1, 14400: 1}
    offset = {ts: value - 200000 for ts, value in values.items()}
    assert thermal_math.hourly_change(offset, 40) == {3600: 1, 14400: 1}
    index = pd.date_range("2025-12-31T23:00Z", periods=73, freq="1h")
    meter = pd.Series([99999 + h for h in range(73)], index=index, dtype=object)
    assert hlc.daily_heat_input_from_meter(meter).tolist() == [24, 24, 24]
    meter.iloc[12] = "broken"
    # First day is unusable; later complete days recover with no fabricated usage.
    assert hlc.daily_heat_input_from_meter(meter).tolist() == [24, 24]
    assert dhw.hourly_change(meter, 40).max() == 1


def test_offline_temperature_percentages_and_recovery():
    index = pd.date_range(START, periods=48, freq="1h")
    room = pd.Series(20.0, index=index)
    outside = pd.Series(-20.0, index=index)
    assert hlc.daily_delta_t({"room": room}, outside).tolist() == [40, 40]
    bad_room = room.copy()
    bad_room.iloc[10] = 99999
    assert hlc.daily_delta_t({"room": bad_room}, outside).tolist() == [40]
    heat = pd.Series(50.0, index=index)
    assert hlc.daily_heat_input_from_tado({"room": heat}, 20).tolist() == [240, 240]
    heat.iloc[10] = 99999
    output = hlc.daily_heat_input_from_tado({"room": heat}, 20)
    assert pd.isna(output.iloc[0]) and output.iloc[1] == 240
    assert dhw.daily_heating_pct({"room": heat}).max() == 50
    loft_index = pd.date_range(START, periods=72, freq="1h")
    loft_room = pd.Series(20.0, index=loft_index)
    loft_outside = pd.Series(-20.0, index=loft_index)
    loft_values = pd.Series(0.0, index=loft_index)
    assert loft.loft_ratio({"room": loft_room}, loft_values, loft_outside)["ratio"] == pytest.approx(0.5)
    assert pd.isna(loft.loft_ratio({"room": loft_room}, pd.Series(99999, index=loft_index), loft_outside)["ratio"])


def test_offline_cooling_keeps_bad_temperature_as_a_barrier():
    index = pd.date_range(START, periods=61, freq="5min")
    room = pd.Series([5 + 15 * exp(-i / 12 / 15) for i in range(61)], index=index)
    outside = pd.Series(5.0, index=index)
    assert cooling.fit_window(room, outside) is not None
    room.iloc[30] = 99999
    assert cooling.fit_window(room, outside) is None
    assert quality.is_valid_temperature(55)
    assert not quality.is_valid_percentage(101)


def test_resampling_detects_raw_spike_before_averaging_and_never_fills(monkeypatch):
    raw = pd.Series([20, 99999, 21, 22], index=pd.to_datetime([
        "2026-01-01T00:00Z", "2026-01-01T00:01Z", "2026-01-01T00:02Z", "2026-01-01T00:15Z"]))
    monkeypatch.setattr(store, "load", lambda _: raw)
    result = store.load_resampled("test", validator=quality.is_valid_temperature)
    assert result.iloc[:3].isna().all()
    assert result.iloc[3] == 22


def _load_functions(filename, names, namespace):
    """Execute production functions in isolation from unavailable HA fixtures."""
    tree = ast.parse((ROOT / "custom_components/thermal_efficiency" / filename).read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


def test_live_observation_converts_before_checking_bounds(thermal_math):
    from math import isfinite
    # Isolate HA's dependency tree. This tests conversion-before-validation
    # in the actual sampler, rather than retesting HA's conversion library.
    class TemperatureConverter:
        @staticmethod
        def convert(value, unit, target):
            assert target == "°C"
            return {"°C": value, "°F": (value - 32) * 5 / 9, "K": value - 273.15}[unit]

    ns = _load_functions("history.py", {"observation"}, {
        "TemperatureConverter": TemperatureConverter, "HomeAssistantError": RuntimeError,
        "isfinite": isfinite, "is_valid_temperature_value": thermal_math.is_valid_temperature_value,
    })
    for value, unit, expected in [(68, "°F", 20), (328.15, "K", 55), (-25, "°C", -25),
                                  (99999, "°C", None), (-999, "°C", None), (float("nan"), "°C", None)]:
        state = SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})
        assert ns["observation"](state, "temperature") == expected
    assert ns["observation"](SimpleNamespace(state="99999", attributes={"unit_of_measurement": "%"}), "humidity") is None


def test_tariff_normalization_precedes_bounds(thermal_math):
    ns = _load_functions("validation.py", {"is_tariff_unit", "normalized_tariff_rate"}, {
        "Any": object, "coerce_tariff_rate": thermal_math.coerce_tariff_rate,
        "TARIFF_UNITS": {"gbp/kwh", "£/kwh", "p/kwh", "pence/kwh", "gbp/mwh", "£/mwh"},
    })
    normalize = ns["normalized_tariff_rate"]
    assert normalize("25", "p/kWh") == 0.25
    assert normalize("250", "GBP/MWh") == 0.25
    assert normalize("0", "GBP/kWh") == 0
    assert normalize("1000", "p/kWh") == 10
    for bad in ["bad", "nan", "inf", "99999", "-1"]:
        assert normalize(bad, "GBP/kWh") is None
    assert normalize("1", "unknown") is None


def test_archival_parity_keeps_outlier_diagnostics_but_normalizes_identity():
    compare = _load_functions("history_migration.py", {"comparable_analysis"}, {})["comparable_analysis"]
    original = {"hlc": {"value": 100}, "source_issues": {"sensor.test": "2 invalid buckets"},
                "heating_power_issues": {"sensor.test": "2 invalid buckets"}}
    preserved = {"hlc": {"value": 100}, "source_issues": {"archive:test": "2 invalid buckets"},
                 "heating_power_issues": {"archive:test": "2 invalid buckets"}}
    assert compare(preserved, {"archive:test": "sensor.test"}) == original
    assert "archive:test" in preserved["source_issues"]  # input remains intact
    preserved["hlc"] = {"value": 101}
    assert compare(preserved, {"archive:test": "sensor.test"}) != original
