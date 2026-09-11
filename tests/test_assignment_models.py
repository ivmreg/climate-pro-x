"""Missing heating and transition dates cannot leak into fallback models."""
from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.parametrize("missing", [False, True])
def test_dated_heating_quality_blocks_fallback(thermal_math, monkeypatch, missing):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    stats = {s: [] for s in ("sensor.room", "sensor.out", "sensor.heat", "sensor.gas")}
    for hour in range(24 * 5):
        ts = (start + timedelta(hours=hour)).timestamp()
        stats["sensor.room"].append({"start": ts, "mean": 20})
        stats["sensor.out"].append({"start": ts, "mean": 19})
        stats["sensor.gas"].append({"start": ts, "sum": hour})
        if not missing:
            stats["sensor.heat"].append({"start": ts, "mean": 0})
    captured = {}
    original = thermal_math.heating_off_days
    def capture(delta, heat):
        captured.update(delta=delta, heat=heat)
        return original(delta, heat)
    monkeypatch.setattr(thermal_math, "heating_off_days", capture)
    thermal_math.compute_all(stats, {
        "outdoor": "sensor.out", "gas_meter": "sensor.gas",
        "rooms": {"a": {"temperature": "sensor.room", "heating_power": "sensor.heat",
                           "heating_expected_intervals": [{"start": (start + timedelta(days=2)).timestamp(), "end": None}]}},
        "excluded_model_days": ["2026-01-02"],
    }, UTC, start + timedelta(days=5), (30,))
    assert start.date() in captured["delta"]  # before heating was expected
    assert (start + timedelta(days=1)).date() not in captured["delta"]
    if missing:
        assert set(captured["delta"]) == {start.date()}
        assert not captured["heat"]
    else:
        assert len(captured["heat"]) == 3
