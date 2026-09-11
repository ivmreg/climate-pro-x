"""Missing heating and transition dates cannot leak into fallback models."""
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.thermal_efficiency.assignments import compose


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


def test_room_isolated_transition_exclusions(thermal_math, monkeypatch):
    from custom_components.thermal_efficiency.assignments import compose
    start = datetime(2026, 1, 1, tzinfo=UTC)
    t_trans = (start + timedelta(days=1)).timestamp()  # 2026-01-02
    data = {
        "migration": {
            "cutoff": (start + timedelta(days=10)).timestamp(),
            "status": "complete",
            "sources": {
                "sensor.a": {"statistic_id": "sensor.a"},
                "sensor.b": {"statistic_id": "sensor.b"},
            },
        },
        "streams": {
            "s_a1": {"entity_id": "s_a1", "original_entity_id": "sensor.a", "coverage": [{"start": 0, "end": None}]},
            "s_a2": {"entity_id": "s_a2", "original_entity_id": "sensor.a", "coverage": [{"start": 0, "end": None}]},
            "s_b": {"entity_id": "s_b", "original_entity_id": "sensor.b", "coverage": [{"start": 0, "end": None}]},
        },
        "rooms": {
            "a": {
                "visits": [
                    {"role": "temperature", "start": None, "end": t_trans, "cause": "legacy", "legacy": True, "stream": "s_a1"},
                    {"role": "temperature", "start": t_trans, "end": None, "cause": "replacement", "legacy": False, "stream": "s_a2"},
                ]
            },
            "b": {
                "visits": [
                    {"role": "temperature", "start": None, "end": None, "cause": "legacy", "legacy": True, "stream": "s_b"},
                ]
            },
        },
    }
    config = {
        "outdoor": "sensor.out",
        "rooms": {
            "a": {"temperature": "sensor.a"},
            "b": {"temperature": "sensor.b"},
        },
    }
    stats = {
        "sensor.out": [], "sensor.a": [], "sensor.b": [],
        "s_a1": [], "s_a2": [], "s_b": [],
    }
    for hour in range(24 * 4):
        ts = (start + timedelta(hours=hour)).timestamp()
        stats["sensor.out"].append({"start": ts, "mean": 5})
        stats["sensor.a"].append({"start": ts, "mean": 20})
        stats["sensor.b"].append({"start": ts, "mean": 20})
        stats["s_a1"].append({"start": ts, "mean": 20})
        stats["s_a2"].append({"start": ts, "mean": 20})
        stats["s_b"].append({"start": ts, "mean": 20})

    composed_stats, composed_conf = compose(stats, config, data, UTC)
    assert composed_conf["rooms"]["a"]["excluded_model_days"] == ["2026-01-02"]
    assert composed_conf["rooms"]["b"]["excluded_model_days"] == []
    assert composed_conf["excluded_model_days"] == ["2026-01-02"]

    captured_room_safe_temps = {}
    def mock_night_taus(room, outdoor, heating, tz, since, expected_intervals=None):
        captured_room_safe_temps[len(captured_room_safe_temps)] = list(room.keys())
        return [{"date": "2026-01-01", "tau_hours": 15.0, "r_squared": 0.9}]

    monkeypatch.setattr(thermal_math, "night_taus", mock_night_taus)
    thermal_math.compute_all(composed_stats, composed_conf, UTC, start + timedelta(days=4), (30,))

    # Room A has hours from 2026-01-02 filtered out
    # Room B retains hours from 2026-01-02
    ts_jan2 = int(t_trans)
    assert ts_jan2 not in captured_room_safe_temps[0]  # room a
    assert ts_jan2 in captured_room_safe_temps[1]      # room b


def test_compose_preserves_non_legacy_stream_prior_to_cutoff():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cutoff = base + timedelta(hours=10)
    t_replace = base + timedelta(hours=5)

    data = {
        "revision": 1,
        "migration": {
            "status": "complete",
            "cutoff": cutoff.timestamp(),
            "sources": {},
        },
        "rooms": {
            "living_room": {
                "visits": [
                    {
                        "role": "temperature",
                        "stream": "s_legacy",
                        "legacy": True,
                        "start": None,
                        "end": t_replace.timestamp(),
                        "cause": "legacy",
                    },
                    {
                        "role": "temperature",
                        "stream": "s_new",
                        "legacy": False,
                        "start": t_replace.timestamp(),
                        "end": None,
                        "cause": "replacement",
                    },
                ]
            }
        },
        "streams": {
            "s_legacy": {
                "id": "s_legacy",
                "entity_id": "sensor.legacy_stream",
                "original_entity_id": "sensor.orig",
                "role": "temperature",
                "coverage": [{"start": 0, "end": t_replace.timestamp()}],
            },
            "s_new": {
                "id": "s_new",
                "entity_id": "sensor.new_stream",
                "original_entity_id": "sensor.repl",
                "role": "temperature",
                "coverage": [{"start": t_replace.timestamp(), "end": None}],
            },
        },
    }
    config = {"rooms": {"living_room": {"name": "Living Room"}}}

    # Populate stats: legacy has hours 0..4, new has hours 5..12
    stats = {
        "sensor.orig": [{"start": (base + timedelta(hours=h)).timestamp(), "mean": 19.0} for h in range(5)],
        "sensor.new_stream": [{"start": (base + timedelta(hours=h)).timestamp(), "mean": 21.0} for h in range(5, 13)],
    }

    result_stats, result_conf = compose(stats, config, data, UTC)
    room_key = result_conf["rooms"]["living_room"]["temperature"]
    composed_hours = [int(r["start"]) for r in result_stats[room_key]]

    # Ensure all hours from 0 through 12 are present in room history,
    # specifically hours 5, 6, 7, 8, 9 from the non-legacy stream before cutoff
    expected_hours = [int((base + timedelta(hours=h)).timestamp()) for h in range(13)]
    assert composed_hours == expected_hours


def test_compose_heating_power_without_metadata_preserves_rows():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cutoff = base + timedelta(hours=10)

    data = {
        "revision": 1,
        "migration": {
            "status": "pending",
            "cutoff": cutoff.timestamp(),
            "sources": {
                "sensor.heating": {
                    "statistic_id": "sensor.heating",
                    "metadata": None,  # Metadata not yet loaded during early startup
                }
            },
        },
        "rooms": {
            "bedroom": {
                "visits": [
                    {
                        "role": "heating_power",
                        "stream": "s_heat",
                        "legacy": True,
                        "start": None,
                        "end": None,
                        "cause": "legacy",
                    }
                ]
            }
        },
        "streams": {
            "s_heat": {
                "id": "s_heat",
                "entity_id": "sensor.heat_stream",
                "original_entity_id": "sensor.heating",
                "role": "heating_power",
                "coverage": [{"start": 0, "end": None}],
            }
        },
    }
    config = {"rooms": {"bedroom": {"name": "Bedroom"}}}
    stats = {
        "sensor.heating": [{"start": (base + timedelta(hours=h)).timestamp(), "mean": 60.0} for h in range(5)],
    }

    result_stats, result_conf = compose(stats, config, data, UTC)
    heat_key = result_conf["rooms"]["bedroom"]["heating_power"]
    # Rows should NOT be dropped just because metadata is None at startup
    assert len(result_stats[heat_key]) == 5


def test_compose_pre_cutover_move_preserves_archived_history():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cutoff = base + timedelta(hours=10)
    t_move = base + timedelta(hours=4)

    data = {
        "revision": 1,
        "migration": {
            "status": "complete",
            "cutoff": cutoff.timestamp(),
            "sources": {
                "sensor.temp": {
                    "statistic_id": "sensor.archived_temp",
                    "metadata": {"unit_of_measurement": "°C"},
                }
            },
        },
        "rooms": {
            "old_room": {
                "visits": [
                    {
                        "role": "temperature",
                        "stream": "s_old",
                        "legacy": True,
                        "start": None,
                        "end": t_move.timestamp(),
                        "cause": "legacy",
                    }
                ]
            },
            "new_room": {
                "visits": [
                    {
                        "role": "temperature",
                        "stream": "s_new",
                        "legacy": False,
                        "start": t_move.timestamp(),
                        "end": None,
                        "cause": "area_change",  # Sensor moved before cutoff
                    }
                ]
            },
        },
        "streams": {
            "s_old": {
                "id": "s_old",
                "entity_id": "sensor.stream_old",
                "original_entity_id": "sensor.temp",
                "role": "temperature",
                "coverage": [{"start": 0, "end": t_move.timestamp()}],
            },
            "s_new": {
                "id": "s_new",
                "entity_id": "sensor.stream_new",
                "original_entity_id": "sensor.temp",
                "role": "temperature",
                "coverage": [{"start": t_move.timestamp(), "end": None}],
            },
        },
    }
    config = {
        "rooms": {
            "old_room": {"name": "Old Room"},
            "new_room": {"name": "New Room"},
        }
    }

    # Archived stats has all hours 0..9 (pre-cutoff)
    # Live stats has hours 4..12
    stats = {
        "sensor.archived_temp": [{"start": (base + timedelta(hours=h)).timestamp(), "mean": 20.0} for h in range(10)],
        "sensor.stream_new": [{"start": (base + timedelta(hours=h)).timestamp(), "mean": 21.0} for h in range(4, 13)],
    }

    result_stats, result_conf = compose(stats, config, data, UTC)
    new_room_key = result_conf["rooms"]["new_room"]["temperature"]
    new_room_hours = [int(r["start"]) for r in result_stats[new_room_key]]

    # New room must contain hours 4..12 (including hours 4..9 between move and cutoff claimed from archive)
    expected_new_hours = [int((base + timedelta(hours=h)).timestamp()) for h in range(4, 13)]
    assert new_room_hours == expected_new_hours
