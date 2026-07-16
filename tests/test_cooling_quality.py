"""Cooling fits require continuous observations and evidence heating stayed off."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from math import exp


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
