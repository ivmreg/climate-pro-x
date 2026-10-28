"""Bounded partial coverage must not invent heating-off or sensor readings."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.thermal_efficiency.assignments import (
    _short_internal_gap,
    compose,
)


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        ([{"start": 0, "end": 1000}, {"start": 1120, "end": None}], 120),
        ([{"start": 0, "end": 1000}, {"start": 1121, "end": None}], None),
        ([{"start": 100, "end": 1000}, {"start": 1010, "end": None}], None),
        ([{"start": 0, "end": 1000}, {"start": 1010, "end": 2000},
          {"start": 2010, "end": None}], None),
        ([{"start": 0, "end": 1000}], None),
    ],
)
def test_only_one_short_gap_with_coverage_at_both_ends(intervals, expected):
    assert _short_internal_gap(0, intervals) == expected


def _fixture():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    start_ts = int(start.timestamp())
    gaps = [start_ts + (day * 24 + hour) * 3600 + 1800
            for day, hour in [(41, 8), (42, 8), (43, 6), (43, 10), (43, 14)]]
    coverage = []
    opening = start_ts - 3600
    for ts in gaps:
        coverage.append({"start": opening, "end": ts})
        opening = ts + 60
    coverage.append({"start": opening, "end": None})
    stats = {key: [] for key in ("owned.temp", "owned.heat", "outside", "gas")}
    gas = 1000.0
    for hour in range(-1, 45 * 24):
        day = max(hour // 24, 0)
        outside = 2.0 + day % 13
        if hour >= 0:
            gas += (8 + 7.2 * (20 - outside)) / 24
        ts = start_ts + hour * 3600
        for key, value in (("owned.temp", 20.0), ("owned.heat", 0.0 if day == 42 else 30.0),
                           ("outside", outside)):
            stats[key].append({"start": ts, "mean": value})
        stats["gas"].append({"start": ts, "sum": gas})
    data = {
        "migration": {"cutoff": start_ts, "status": "complete", "sources": {}},
        "rooms": {"r": {"visits": [
            {"role": "temperature", "stream": "temp", "start": None, "end": None, "cause": "legacy"},
            {"role": "heating_power", "stream": "heat", "start": None, "end": None, "cause": "legacy"},
        ]}},
        "streams": {
            "temp": {"entity_id": "owned.temp", "original_entity_id": "source.temp", "coverage": coverage},
            "heat": {"entity_id": "owned.heat", "original_entity_id": "source.heat", "coverage": coverage},
        },
    }
    conf = {"rooms": {"r": {"temperature": "source.temp", "heating_power": "source.heat"}},
            "outdoor": "outside", "gas_meter": "gas", "boiler_efficiency": 0.88}
    return start, stats, data, conf


def test_short_temperature_gap_and_proven_heating_on_are_separate(thermal_math):
    start, stats, data, conf = _fixture()
    composed, prepared = compose(stats, conf, data, UTC)
    room = prepared["rooms"]["r"]
    first_gap = int((start + timedelta(days=41, hours=8)).timestamp())
    strict_times = {int(row["start"]) for row in composed[room["temperature"]]}
    hlc_times = {int(row["start"]) for row in composed[room["hlc_temperature"]]}
    assert first_gap not in strict_times and first_gap in hlc_times
    assert first_gap not in {int(row["start"]) for row in composed[room["heating_power"]]}
    # Three interrupted hours in one day exceed the daily tolerance.
    assert int((start + timedelta(days=43, hours=6)).timestamp()) not in hlc_times

    strict = thermal_math.compute_all(composed, prepared, UTC, start + timedelta(days=45), (120,))
    tolerant = thermal_math.compute_all(composed, {**prepared, "hlc_allow_short_gaps": True}, UTC,
                                       start + timedelta(days=45), (120,))
    strict_days = {r["date"]: r for r in strict["analysis_evidence"]["daily"]}
    tolerant_days = {r["date"]: r for r in tolerant["analysis_evidence"]["daily"]}
    on, uncertain, excessive = [(start + timedelta(days=n)).date().isoformat() for n in (41, 42, 43)]
    assert strict_days[on]["eligible"]["hlc"] is False
    assert tolerant_days[on]["eligible"]["hlc"] is True
    assert tolerant_days[on]["delta_t_k"] is not None
    assert tolerant_days[on]["strict_delta_t_k"] is None
    assert tolerant_days[on]["temperature_short_gap_hours"] == 1
    assert tolerant_days[on]["heating_missing_hours"] == 1
    assert tolerant_days[on]["heating_pct"] is None
    assert tolerant_days[on]["heating_pct_lower_bound"] > 1
    assert tolerant_days[uncertain]["eligible"]["hlc"] is False
    assert tolerant_days[excessive]["eligible"]["hlc"] is False
    assert tolerant["analysis_status"]["hlc"]["heating_on_with_missing_hours_days"] >= 1
    status = tolerant["analysis_status"]["hlc"]
    assert status["qualified_complete_temperature_days"] > status["raw_complete_temperature_days"]
    for metric in ("dhw", "usage"):
        assert tolerant["analysis_status"][metric]["raw_complete_temperature_days"] == status["raw_complete_temperature_days"]


def test_short_gap_cannot_cross_visit_or_quarantine():
    start, stats, data, conf = _fixture()
    boundary = int((start + timedelta(days=41, hours=8, minutes=30)).timestamp())
    data["rooms"]["r"]["visits"][0]["end"] = boundary
    composed, prepared = compose(stats, conf, data, UTC)
    hlc = composed[prepared["rooms"]["r"]["hlc_temperature"]]
    assert boundary - 1800 not in {int(row["start"]) for row in hlc}
    data["rooms"]["r"]["visits"][0]["end"] = None
    data["streams"]["temp"]["quarantine"] = [{"start": boundary, "end": boundary + 1}]
    composed, prepared = compose(stats, conf, data, UTC)
    hlc = composed[prepared["rooms"]["r"]["hlc_temperature"]]
    assert boundary - 1800 not in {int(row["start"]) for row in hlc}


def test_dhw_baselines_do_not_train_on_qualified_temperature_days(thermal_math, monkeypatch):
    start, stats, data, conf = _fixture()
    day = (start + timedelta(days=40)).date()
    gap = int((start + timedelta(days=40, hours=8)).timestamp())
    coverage = data["streams"]["temp"]["coverage"]
    coverage.insert(1, {"start": gap + 60, "end": coverage[0]["end"]})
    coverage[0]["end"] = gap
    for row in stats["owned.heat"]:
        if datetime.fromtimestamp(row["start"], UTC).date() == day:
            row["mean"] = 0
    composed, prepared = compose(stats, conf, data, UTC)
    calls = []
    original = thermal_math.dhw_baseline
    def capture(q, dt, *args, **kwargs):
        calls.append(dt)
        return original(q, dt, *args, **kwargs)
    monkeypatch.setattr(thermal_math, "dhw_baseline", capture)
    result = thermal_math.compute_all(composed, {**prepared, "hlc_allow_short_gaps": True}, UTC,
                                      start + timedelta(days=45), (120,))
    row = next(row for row in result["analysis_evidence"]["daily"] if row["date"] == day.isoformat())
    assert row["delta_t_k"] is not None and row["strict_delta_t_k"] is None
    assert len(calls) == 2 and all(day not in dt for dt in calls)


def test_midnight_gap_is_bounded_interpolated_and_never_added_to_strict_stream(thermal_math):
    start, stats, data, conf = _fixture()
    gap_start = int((start + timedelta(days=40, hours=22, minutes=52)).timestamp())
    gap_end = int((start + timedelta(days=41, hours=1, minutes=7)).timestamp())
    data["streams"]["temp"]["coverage"] = [
        {"start": int(start.timestamp()) - 3600, "end": gap_start},
        {"start": gap_end, "end": None},
    ]
    absent = {int((start + timedelta(days=40, hours=23)).timestamp()),
              int((start + timedelta(days=41)).timestamp())}
    stats["owned.temp"] = [r for r in stats["owned.temp"] if int(r["start"]) not in absent]
    composed, prepared = compose(stats, conf, data, UTC)
    room = prepared["rooms"]["r"]
    expected = {int((start + timedelta(days=40, hours=22 + i)).timestamp()) for i in range(4)}
    assert set(room["hlc_interpolated_temperature_hours"]) == expected
    assert expected.isdisjoint({int(r["start"]) for r in composed[room["temperature"]]})
    assert all(r["mean"] == 20 for r in composed[room["hlc_temperature"]] if r["start"] in expected)
    result = thermal_math.compute_all(composed, {**prepared, "hlc_allow_short_gaps": True}, UTC,
                                      start + timedelta(days=45), (120,))
    rows = {r["date"]: r for r in result["analysis_evidence"]["daily"]}
    assert rows[(start + timedelta(days=40)).date().isoformat()]["temperature_interpolated_hours"] == 2
    assert rows[(start + timedelta(days=41)).date().isoformat()]["temperature_interpolated_hours"] == 2

    # A longer outage is not interpolated, even when endpoint values exist.
    data["streams"]["temp"]["coverage"][1]["start"] = gap_start + 3 * 3600 + 1
    _, prepared = compose(stats, conf, data, UTC)
    assert prepared["rooms"]["r"]["hlc_interpolated_temperature_hours"] == []


def test_interpolation_rejects_quarantine_and_contaminated_endpoints():
    start, stats, data, conf = _fixture()
    gap_start = int((start + timedelta(days=40, hours=22, minutes=52)).timestamp())
    gap_end = int((start + timedelta(days=41, hours=1, minutes=7)).timestamp())
    data["streams"]["temp"]["coverage"] = [
        {"start": int(start.timestamp()) - 3600, "end": gap_start},
        {"start": gap_end, "end": None},
    ]
    data["streams"]["temp"]["quarantine"] = [{"start": gap_start, "end": gap_start + 1}]
    _, prepared = compose(stats, conf, data, UTC)
    assert not prepared["rooms"]["r"]["hlc_interpolated_temperature_hours"]
    data["streams"]["temp"]["quarantine"] = []
    before = int((start + timedelta(days=40, hours=21)).timestamp())
    next(row for row in stats["owned.temp"] if row["start"] == before)["max"] = 999
    _, prepared = compose(stats, conf, data, UTC)
    assert not prepared["rooms"]["r"]["hlc_interpolated_temperature_hours"]


def test_strict_comparison_uses_the_same_snapshot_and_is_exported():
    from custom_components.thermal_efficiency.coordinator import _compute_with_coverage_comparison

    start, stats, data, conf = _fixture()
    composed, prepared = compose(stats, conf, data, UTC)
    result = _compute_with_coverage_comparison(
        composed, {**prepared, "hlc_allow_short_gaps": True}, UTC,
        start + timedelta(days=45), (120,),
    )
    comparison = result["analysis_status"]["hlc"]["coverage_sensitivity"]
    assert comparison["strict_hlc_w_per_k"] is not None
    assert comparison["strict_days_used"] < result["hlc"]["days_used"]
    assert comparison["difference_w_per_k"] == pytest.approx(
        result["hlc"]["hlc_w_per_k"] - comparison["strict_hlc_w_per_k"]
    )
    assert result["analysis_evidence"]["metrics"]["hlc"]["coverage_sensitivity"] == comparison
    assert "interpolated temperatures" in result["analysis_evidence"]["uncertainty"]["coverage"]
