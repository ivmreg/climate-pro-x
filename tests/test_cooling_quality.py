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


def test_configured_but_missing_heating_history_suppresses_tau(thermal_math):
    room, outdoor, _heating = _cooling_series()

    fits = thermal_math.night_taus(
        room, outdoor, {}, timezone.utc, date(2026, 1, 1)
    )

    assert fits == []
