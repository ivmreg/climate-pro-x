"""The local history cache must support corrected backfills safely."""

from __future__ import annotations

import pandas as pd

from ha_efficiency import store


def test_save_is_idempotent_and_new_backfill_replaces_old_value(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    index = pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"])
    original = pd.Series([10.0, 11.0], index=index)
    corrected = pd.Series([12.5], index=index[1:])

    store.save({"sensor.gas": original})
    store.save({"sensor.gas": corrected})
    store.save({"sensor.gas": corrected})

    loaded = store.load("sensor.gas")
    assert loaded is not None
    assert loaded.index.is_unique
    assert loaded.tolist() == [10.0, 12.5]


def test_cache_round_trip_normalises_mixed_offsets_to_utc(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    mixed = pd.Series(
        [1.0, 2.0],
        index=pd.Index(["2026-07-01T01:00:00+01:00", "2026-07-01T01:00:00+00:00"]),
    )

    store.save({"sensor.temperature": mixed})
    loaded = store.load("sensor.temperature")

    assert loaded is not None
    assert str(loaded.index.tz) == "UTC"
    assert loaded.index.is_monotonic_increasing


def test_cumulative_sources_are_normalised_before_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    index = pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="1h")
    lts = pd.Series([100.0, 101.0, 102.0, 103.0], index=index)
    rest = pd.Series([1100.0, 1101.0, 1102.0, 1103.0], index=index)

    store.save({"sensor.gas": lts}, source="lts", kind_by_entity={"sensor.gas": "cumulative"})
    store.save({"sensor.gas": rest}, source="rest", kind_by_entity={"sensor.gas": "cumulative"})

    loaded = store.load("sensor.gas")
    assert loaded is not None
    assert loaded.tolist() == [100.0, 101.0, 102.0, 103.0]
    assert set(loaded.attrs["sources_used"]) == {"lts", "rest"}
    assert loaded.attrs["baseline_offsets"]["rest"] == 1000.0
