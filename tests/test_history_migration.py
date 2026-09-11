"""Real recorder imports: all fields survive removal of original statistics."""
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from freezegun import freeze_time
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics, clear_statistics
from homeassistant.components.recorder.tasks import ClearStatisticsTask
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.history_migration import (
    HistoryMigrator, canonical, committed, digest, migration_manifest,
)


async def test_actual_recorder_preservation_and_source_deletion(recorder_mock, hass):
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    config = {"outdoor": "sensor.temperature", "rooms": {"office": {"temperature": "sensor.temperature"}},
              "gas_meter": "sensor.gas"}
    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    entry.add_to_hass(hass)
    meta = {
        "source": "recorder", "statistic_id": "sensor.temperature", "name": "Old temperature",
        "unit_of_measurement": "°F", "unit_class": "temperature",
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False,
    }
    temperature = [{"start": now - timedelta(days=400), "mean": 68., "min": 60., "max": 72.},
                   {"start": now - timedelta(hours=3), "mean": 70., "min": 69., "max": 71.}]
    gas_meta = {**meta, "statistic_id": "sensor.gas", "unit_of_measurement": "kWh",
                "unit_class": "energy", "has_sum": True, "mean_type": StatisticMeanType.NONE}
    gas = [{"start": now - timedelta(days=400), "state": 1200., "sum": 22., "last_reset": now - timedelta(days=500)},
           {"start": now - timedelta(hours=3), "state": 1400., "sum": 222., "last_reset": now - timedelta(days=500)}]
    async_import_statistics(hass, meta, temperature)
    async_import_statistics(hass, gas_meta, gas)
    await committed(hass)
    data = {"migration": migration_manifest(config, entry.entry_id, (now - timedelta(hours=3)).timestamp())}
    # Mirror an upgrade whose cutover has already been finalized by recorder.
    manifest_store = Store(hass, 1, "test.history_manifest")
    async def save():
        await manifest_store.async_save(data)
    migrator = HistoryMigrator(hass, entry, data, save)
    before = {s: await migrator.query(s, datetime(1970, 1, 1, tzinfo=UTC), now, "hour", m)
              for s, m in (("sensor.temperature", meta), ("sensor.gas", gas_meta))}
    await migrator.run()
    assert data["migration"]["status"] == "complete"
    for source, record in data["migration"]["sources"].items():
        copied = await migrator.query(record["statistic_id"], datetime(1970, 1, 1, tzinfo=UTC), now, "hour", record["metadata"])
        assert canonical(copied) == canonical(before[source])
        assert sum(c["count"] for c in record["chunks"].values() if c["kind"] == "hour") == 2
    # Delete ONLY test-fixture originals to prove independent ownership.
    recorder_mock.queue_task(ClearStatisticsTask(None, list(before)))
    await committed(hass)
    for source, record in data["migration"]["sources"].items():
        assert await migrator.query(source, datetime(1970, 1, 1, tzinfo=UTC), now, "hour") == []
        assert canonical(await migrator.query(record["statistic_id"], datetime(1970, 1, 1, tzinfo=UTC), now, "hour", record["metadata"])) == canonical(before[source])
    restored = await manifest_store.async_load()
    assert restored == data
    await HistoryMigrator(hass, entry, restored, save).run()


async def test_archive_retry_does_not_overwrite_after_source_loss(recorder_mock, hass):
    config = {"rooms": {"a": {"temperature": "sensor.a"}}}
    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    data = {"migration": migration_manifest(config, entry.entry_id, dt_util.utcnow().timestamp())}
    async def save():
        pass
    migrator = HistoryMigrator(hass, entry, data, save)
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    # Exercise raw archive even with no state history; a valid empty snapshot
    # is distinct from a missing chunk after it has been recorded.
    rows, key = await migrator._archive("sensor.a", "raw", start, end, None)
    assert rows == []
    data["migration"]["sources"]["sensor.a"]["chunks"].pop(key)
    assert await migrator._archive("sensor.a", "raw", start, end, None) == (rows, key)
    assert data["migration"]["sources"]["sensor.a"]["chunks"][key]["digest"] == digest(rows)


async def test_raw_boundary_attributes_and_short_term_preserved(recorder_mock, hass):
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    config = {"rooms": {"a": {"temperature": "sensor.a"}}}
    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    data = {"migration": migration_manifest(config, entry.entry_id, start.timestamp())}
    async def save():
        pass
    migrator = HistoryMigrator(hass, entry, data, save)
    with freeze_time(start):
        hass.states.async_set("sensor.a", "20.125", {"unit_of_measurement": "°C", "calibration": 0.125})
        await hass.async_block_till_done()
        await committed(hass)
    meta = {"source": "recorder", "statistic_id": "sensor.a", "name": "Temperature",
            "unit_of_measurement": "°C", "unit_class": "temperature", "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC}
    recorder_mock.async_import_statistics(meta, [{"start": start, "mean": 20.125, "min": 20., "max": 20.25}], StatisticsShortTerm)
    await committed(hass)
    raw, _ = await migrator._archive("sensor.a", "raw", start, start + timedelta(hours=1), meta)
    assert len(raw) == 1
    assert raw[0]["state"] == "20.125"
    assert raw[0]["attributes"]["calibration"] == 0.125
    assert raw[0]["last_updated"] == start.isoformat()
    short, _ = await migrator._archive("sensor.a", "5minute", start, start + timedelta(hours=1), meta)
    assert len(short) == 1
    assert short[0]["mean"] == 20.125
    assert short[0]["min"] == 20.
    assert short[0]["max"] == 20.25
    hass.states.async_remove("sensor.a")
    recorder_mock.queue_task(ClearStatisticsTask(None, ["sensor.a"]))
    await committed(hass)
    assert (await migrator._archive("sensor.a", "raw", start, start + timedelta(hours=1), meta))[0] == raw
    assert (await migrator._archive("sensor.a", "5minute", start, start + timedelta(hours=1), meta))[0] == short
