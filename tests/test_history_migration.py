"""Real recorder imports: all fields survive removal of original statistics."""
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, async_import_statistics, clear_statistics,
)
from homeassistant.components.recorder.tasks import ClearStatisticsTask
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.history_migration import (
    HistoryMigrator, canonical, committed, digest, fingerprint, migration_manifest,
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


def test_fields_contains_mean_weight():
    from custom_components.thermal_efficiency.history_migration import FIELDS, ARCHIVE_FIELDS
    assert "mean_weight" in FIELDS
    assert ARCHIVE_FIELDS == set(FIELDS)


async def test_interrupted_migration_cutoff_rebasing(recorder_mock, hass):
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    t0 = base                      # 10:00
    t1 = base + timedelta(hours=1) # 11:00 (original cutoff)
    t2 = base + timedelta(hours=2) # 12:00 (rebased cutoff)

    config = {"outdoor": "sensor.a", "rooms": {"office": {"temperature": "sensor.a"}}}
    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    entry.add_to_hass(hass)

    meta = {
        "source": "recorder", "statistic_id": "sensor.a", "name": "Temperature",
        "unit_of_measurement": "°C", "unit_class": "temperature",
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False,
    }
    stats = [
        {"start": t0 - timedelta(days=1), "mean": 20.0},
        {"start": t0, "mean": 20.5},
        {"start": t1, "mean": 21.0},
    ]
    async_import_statistics(hass, meta, stats)
    await committed(hass)

    # Simulate an interruption between 10:45 and 11:30 across original cutoff 11:00
    t_interrupted = t0 + timedelta(minutes=45)
    t_restart = t1 + timedelta(minutes=30)
    data = {
        "migration": migration_manifest(config, entry.entry_id, t0.timestamp()),
        "streams": {
            "s_a": {
                "id": "s_a",
                "coverage": [
                    {"start": t0.timestamp(), "end": t_interrupted.timestamp()},
                    {"start": t_restart.timestamp(), "end": None},
                ],
            }
        },
        "rooms": {
            "office": {
                "visits": [
                    {"stream": "s_a", "end": None, "role": "temperature", "expected": True}
                ]
            }
        },
    }

    async def save():
        pass

    migrator = HistoryMigrator(hass, entry, data, save)

    # Run at 11:35 (past original cutoff + 6 min, but interrupted)
    with freeze_time(t_restart + timedelta(minutes=5)):
        await migrator.run()

    # Cutoff must be rebased to 12:00 (t2) and status must be finalizing
    assert data["migration"]["cutoff"] == t2.timestamp()
    assert data["migration"]["status"] == "finalizing"
    assert not data["migration"]["sources"]["sensor.a"].get("tail_done")

    # Fast forward to 12:10 (past rebased cutoff + 6 min, active uninterrupted capture at 12:00)
    with freeze_time(t2 + timedelta(minutes=10)):
        await migrator.run()

    assert data["migration"]["status"] == "complete"
    assert data["migration"]["sources"]["sensor.a"]["tail_done"]
    # Verify rows through 12:00 were imported
    source_record = data["migration"]["sources"]["sensor.a"]
    copied = await migrator.query(source_record["statistic_id"], t0 - timedelta(days=2), t2, "hour", source_record["metadata"])
    assert len(copied) == 3


def test_unobserved_stream_has_uninterrupted_live_capture(hass):
    data = {
        "migration": {"cutoff": 1000.0},
        "rooms": {"office": {"visits": [{"stream": "s_silent", "role": "temperature", "end": None}]}},
        "streams": {"s_silent": {"id": "s_silent", "coverage": []}},
    }
    migrator = HistoryMigrator(hass, MagicMock(), data, AsyncMock())
    # Unobserved stream with coverage=[] must not fail live capture check
    assert migrator._has_uninterrupted_live_capture(1000.0) is True


async def test_bounded_short_term_preservation_skips_empty_years(recorder_mock, hass):
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    config = {"outdoor": "sensor.empty_history", "rooms": {}}
    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    entry.add_to_hass(hass)

    # Set raw_start to 3 years ago in migration manifest
    three_years_ago = (base - timedelta(days=365 * 3)).timestamp()
    data = {
        "migration": migration_manifest(config, entry.entry_id, base.timestamp()),
        "streams": {},
        "rooms": {},
    }
    data["migration"]["raw_start"] = three_years_ago
    migrator = HistoryMigrator(hass, entry, data, AsyncMock())

    with freeze_time(base + timedelta(minutes=10)):
        await migrator.run()

    # Source has no history in recorder, so raw and 5minute chunks must not be created for 1000+ days
    record = data["migration"]["sources"]["sensor.empty_history"]
    raw_and_5m_chunks = [c for c in record["chunks"].values() if c["kind"] in ("raw", "5minute")]
    assert len(raw_and_5m_chunks) == 0


async def test_verify_model_parity_rewrites_config_to_owned_ids(recorder_mock, hass, monkeypatch):
    from copy import deepcopy
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    cutoff = base
    config = {
        "outdoor": "sensor.out",
        "gas_meter": "sensor.gas",
        "rooms": {
            "office": {
                "temperature": "sensor.room",
                "heating_power": "sensor.heat",
            },
            "loft": {
                "room_type": "loft",
                "temperature": "sensor.loft",
                "humidity": "sensor.loft_humidity",
                "assignment_since": base.date().isoformat(),
            },
        },
    }
    meta_out = {"source": "recorder", "statistic_id": "sensor.out", "name": "Out", "unit_of_measurement": "°C", "has_sum": False, "mean_type": StatisticMeanType.ARITHMETIC, "unit_class": "temperature"}
    meta_gas = {"source": "recorder", "statistic_id": "sensor.gas", "name": "Gas", "unit_of_measurement": "kWh", "has_sum": True, "mean_type": StatisticMeanType.ARITHMETIC, "unit_class": "energy"}
    meta_room = {"source": "recorder", "statistic_id": "sensor.room", "name": "Room", "unit_of_measurement": "°C", "has_sum": False, "mean_type": StatisticMeanType.ARITHMETIC, "unit_class": "temperature"}
    meta_heat = {"source": "recorder", "statistic_id": "sensor.heat", "name": "Heat", "unit_of_measurement": "%", "has_sum": False, "mean_type": StatisticMeanType.ARITHMETIC, "unit_class": None}
    meta_loft = {**meta_room, "statistic_id": "sensor.loft", "name": "Loft"}
    meta_humidity = {"source": "recorder", "statistic_id": "sensor.loft_humidity", "name": "Loft humidity", "unit_of_measurement": "%", "has_sum": False, "mean_type": StatisticMeanType.ARITHMETIC, "unit_class": None}

    stats = [{"start": cutoff - timedelta(hours=1), "mean": 10.0, "sum": 5.0}]
    async_import_statistics(hass, meta_out, stats)
    async_import_statistics(hass, meta_gas, stats)
    async_import_statistics(hass, meta_room, stats)
    async_import_statistics(hass, meta_heat, stats)
    async_import_statistics(hass, meta_loft, stats)
    async_import_statistics(hass, meta_humidity, stats)

    # Import identical stats for the owned external statistics
    async_add_external_statistics(hass, {**meta_out, "source": "thermal_efficiency", "statistic_id": "thermal_efficiency:out"}, stats)
    async_add_external_statistics(hass, {**meta_gas, "source": "thermal_efficiency", "statistic_id": "thermal_efficiency:gas"}, stats)
    async_add_external_statistics(hass, {**meta_room, "source": "thermal_efficiency", "statistic_id": "thermal_efficiency:room"}, stats)
    async_add_external_statistics(hass, {**meta_heat, "source": "thermal_efficiency", "statistic_id": "thermal_efficiency:heat"}, stats)
    async_add_external_statistics(hass, {**meta_loft, "source": "thermal_efficiency", "statistic_id": "thermal_efficiency:loft"}, stats)
    async_add_external_statistics(hass, {**meta_humidity, "source": "thermal_efficiency", "statistic_id": "thermal_efficiency:loft_humidity"}, stats)
    await committed(hass)

    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    entry.add_to_hass(hass)
    manifest = migration_manifest(config, entry.entry_id, cutoff.timestamp())
    manifest["sources"]["sensor.out"]["statistic_id"] = "thermal_efficiency:out"
    manifest["sources"]["sensor.gas"]["statistic_id"] = "thermal_efficiency:gas"
    manifest["sources"]["sensor.room"]["statistic_id"] = "thermal_efficiency:room"
    manifest["sources"]["sensor.heat"]["statistic_id"] = "thermal_efficiency:heat"
    manifest["sources"]["sensor.loft"]["statistic_id"] = "thermal_efficiency:loft"
    manifest["sources"]["sensor.loft_humidity"]["statistic_id"] = "thermal_efficiency:loft_humidity"

    data = {"migration": manifest, "streams": {}, "rooms": {}}
    migrator = HistoryMigrator(hass, entry, data, AsyncMock())

    captured_configs = []
    from custom_components.thermal_efficiency import history_migration as hm
    def mock_compute(st, cfg, tz, now, windows):
        captured_configs.append(deepcopy(cfg))
        return {"result": 1}
    monkeypatch.setattr(hm, "compute_all", mock_compute)

    await migrator._verify_model_parity(cutoff)
    assert data["migration"]["parity_verified"] is True
    assert len(captured_configs) == 2
    # Baseline used original config
    assert captured_configs[0]["outdoor"] == "sensor.out"
    assert captured_configs[0]["rooms"]["office"]["temperature"] == "sensor.room"
    assert set(captured_configs[0]["rooms"]) == {"office"}
    assert captured_configs[0]["loft"] == "sensor.loft"
    assert captured_configs[0]["loft_humidity"] == "sensor.loft_humidity"
    assert captured_configs[0]["loft_since"] == base.date()
    # Preserved run used rewritten config with owned statistic IDs
    assert captured_configs[1]["outdoor"] == "thermal_efficiency:out"
    assert captured_configs[1]["rooms"]["office"]["temperature"] == "thermal_efficiency:room"
    assert captured_configs[1]["rooms"]["office"]["heating_power"] == "thermal_efficiency:heat"
    assert captured_configs[1]["gas_meter"] == "thermal_efficiency:gas"
    assert captured_configs[1]["loft"] == "thermal_efficiency:loft"
    assert captured_configs[1]["loft_humidity"] == "thermal_efficiency:loft_humidity"


async def test_snapshot_inventory_fails_if_source_is_lost_before_copy(recorder_mock, hass):
    cutoff = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    config = {
        "outdoor": "sensor.out",
        "rooms": {"office": {"name": "Office", "temperature": "sensor.room"}},
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=config)
    entry.add_to_hass(hass)
    manifest = migration_manifest(config, entry.entry_id, cutoff.timestamp())
    missing_row = [{"start": cutoff - timedelta(hours=1), "mean": 10.0}]
    empty = fingerprint([], "hour")
    manifest["inventory_version"] = 2
    manifest["inventory"] = {
        source: {
            "raw": fingerprint([], "raw"),
            "5minute": empty,
            "hour": fingerprint(missing_row, "hour"),
        }
        for source in manifest["sources"]
    }

    data = {"migration": manifest, "streams": {}, "rooms": {}}
    migrator = HistoryMigrator(hass, entry, data, AsyncMock())

    # No archive chunks exist: the source vanished after inventory and before
    # the first yearly archive query. Empty must not be accepted as success.
    with pytest.raises(ValueError, match="changed or disappeared during hour preservation"):
        await migrator._verify_snapshot_inventory(cutoff)
