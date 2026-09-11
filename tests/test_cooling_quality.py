"""Cooling fits require continuous observations and evidence heating stayed off."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from math import exp, isnan

from ha_efficiency import cooling


def _cooling_series(days: int = 4, tau: float = 15.0):
    room = {}
    outdoor = {}
    heating = {}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for day in range(days):
        for hour in range(6):
            ts = int((start + timedelta(days=day, hours=hour)).timestamp())
            outdoor[ts] = 5.0
            room[ts] = 5.0 + 15.0 * exp(-hour / tau)
            heating[ts] = 0.0
    return room, outdoor, heating


def test_tau_fit_recovers_dynamic_cooling_parameter(thermal_math):
    room, outdoor, heating = _cooling_series()

    fits = thermal_math.night_taus(
        room, outdoor, heating, timezone.utc, date(2026, 1, 1)
    )

    assert len(fits) == 4
    assert all(abs(fit["tau_hours"] - 15.0) <= 0.25 for fit in fits)


def test_heating_added_later_preserves_earlier_cooling(thermal_math):
    room, outdoor, _ = _cooling_series()
    added = datetime(2026, 1, 3, tzinfo=timezone.utc).timestamp()
    fits = thermal_math.night_taus(room, outdoor, {}, timezone.utc, date(2026, 1, 1),
                                   [{"start": added, "end": None}])
    assert [f["date"] for f in fits] == ["2026-01-01", "2026-01-02"]


def test_tau_pinned_at_the_search_bound_is_not_reported(thermal_math):
    """A room that barely cools fits best at the top of the tau search range.
    That is the range talking, not the building: the night bounds tau from
    below rather than measuring it, so reporting 200h would let the search
    bound leak into the median as if it were a result."""
    # tau=230h is past the 200h search bound but still cools enough over the
    # night to clear the minimum-drop gate, so the ceiling is what rejects it
    # rather than the room simply looking static.
    room, outdoor, heating = _cooling_series(tau=230.0)

    fits = thermal_math.night_taus(
        room, outdoor, heating, timezone.utc, date(2026, 1, 1)
    )

    assert fits == []

    # Control: just inside the bound, the same shape still fits and reports.
    room, outdoor, heating = _cooling_series(tau=190.0)

    fits = thermal_math.night_taus(
        room, outdoor, heating, timezone.utc, date(2026, 1, 1)
    )

    assert len(fits) == 4
    assert all(fit["tau_hours"] < thermal_math.TAU_MAX_HOURS for fit in fits)


def test_configured_but_missing_heating_history_suppresses_tau(thermal_math):
    room, outdoor, _heating = _cooling_series()

    fits = thermal_math.night_taus(
        room, outdoor, {}, timezone.utc, date(2026, 1, 1)
    )

    assert fits == []


def test_compute_all_withholds_tau_with_only_two_nights(thermal_math, monkeypatch):
    monkeypatch.setattr(
        thermal_math,
        "night_taus",
        lambda *args: [
            {"date": "2026-01-01", "tau_hours": 12.0, "r_squared": 0.9},
            {"date": "2026-01-02", "tau_hours": 14.0, "r_squared": 0.9},
        ],
    )

    result = thermal_math.compute_all(
        {},
        {
            "rooms": {"room": {"temperature": "sensor.room"}},
            "outdoor": "sensor.outdoor",
        },
        timezone.utc,
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        (60, 120, 365),
    )

    assert result["rooms"]["room"] is None


def test_out_of_range_heating_history_is_filtered(thermal_math, monkeypatch):
    captured = {}

    def _capture_heating(room, outdoor, heating, tz, since, expected_intervals=None):
        captured["heating"] = heating
        return []

    monkeypatch.setattr(thermal_math, "night_taus", _capture_heating)
    valid_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    invalid_ts = valid_ts + 3600
    result = thermal_math.compute_all(
        {
            "sensor.heat": [
                {"start": valid_ts, "mean": 25.0},
                {"start": invalid_ts, "mean": 250.0},
            ]
        },
        {
            "rooms": {
                "room": {
                    "temperature": "sensor.room",
                    "heating_power": "sensor.heat",
                }
            },
            "outdoor": "sensor.outdoor",
        },
        timezone.utc,
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        (60,),
    )

    assert captured["heating"] == {valid_ts: 25.0}
    assert "sensor.heat" in result["heating_power_issues"]


def test_offline_summary_also_requires_three_nights():
    fits = [
        cooling.NightFit(
            date=f"2026-01-0{day}",
            tau_hours=10.0 + day,
            r_squared=0.9,
            t_start=20.0,
            t_end=18.0,
            outdoor_mean=5.0,
        )
        for day in range(1, 4)
    ]

    two_nights = cooling.summarise({"room": fits[:2]}).iloc[0]
    three_nights = cooling.summarise({"room": fits}).iloc[0]

    assert two_nights["nights_fitted"] == 2
    assert isnan(two_nights["tau_median_h"])
    assert three_nights["tau_median_h"] == 12.0
