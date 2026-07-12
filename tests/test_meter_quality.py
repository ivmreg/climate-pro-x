"""Meter continuity, completeness, and timezone-boundary tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ha_efficiency import dhw, hlc


def test_live_hourly_change_does_not_invent_consumption_across_gap(thermal_math):
    cumulative = {
        0: 100.0,
        3600: 101.0,
        25 * 3600: 121.0,
        26 * 3600: 122.0,
    }

    result = thermal_math.hourly_change(cumulative, max_step=40.0)

    assert result == {3600: 1.0, 26 * 3600: 1.0}


@pytest.mark.parametrize("converter", [dhw.hourly_change])
def test_offline_hourly_change_does_not_invent_consumption_across_gap(converter):
    index = pd.to_datetime(
        ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
         "2026-01-02T01:00:00Z", "2026-01-02T02:00:00Z"]
    )
    cumulative = pd.Series([100.0, 101.0, 121.0, 122.0], index=index)

    result = converter(cumulative, max_step=40.0)

    assert list(result.index) == [index[1], index[3]]
    assert result.tolist() == [1.0, 1.0]


def test_offline_daily_meter_excludes_partial_days():
    index = pd.date_range("2026-01-01T00:00:00Z", periods=24 + 12, freq="1h")
    cumulative = pd.Series(range(len(index)), index=index, dtype=float)

    result = hlc.daily_heat_input_from_meter(cumulative)

    assert list(result.index) == [pd.Timestamp("2026-01-01T00:00:00Z")]
    assert result.iloc[0] == pytest.approx(23.0)


@pytest.mark.parametrize(
    ("local_day", "expected_hours"),
    [
        (datetime(2026, 3, 29), 23),
        (datetime(2026, 10, 25), 25),
    ],
)
def test_dst_days_keep_real_consecutive_hours(thermal_math, local_day, expected_hours):
    tz = ZoneInfo("Europe/London")
    start = local_day.replace(tzinfo=tz)
    end = (local_day + timedelta(days=1)).replace(tzinfo=tz)
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    timestamps = []
    cursor = start_utc
    while cursor <= end_utc:
        timestamps.append(int(cursor.timestamp()))
        cursor += timedelta(hours=1)
    cumulative = {ts: float(i) for i, ts in enumerate(timestamps)}

    changes = thermal_math.hourly_change(cumulative, max_step=40.0)

    assert len(changes) == expected_hours
    assert sum(changes.values()) == pytest.approx(float(expected_hours))
