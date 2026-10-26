"""History schema, recovery privacy, and registry revision contracts."""
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Event
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.const import (
    DOMAIN,
    HISTORY_DATA_VERSION,
)
from custom_components.thermal_efficiency.history import (
    HistoryDataError,
    RoomHistoryManager,
    _migration_retry_allowed,
    migrate_history_data,
)
from custom_components.thermal_efficiency.history_migration import (
    HistoryMigrator,
    MigrationFailure,
    migration_manifest,
)


def _history_document(version_marker=True):
    config = {"rooms": {}}
    migration = migration_manifest(config, "entry-test", 1_800_000_000.0)
    migration["sources"]["private.sensor"] = {
        "statistic_id": "thermal_efficiency:private",
        "chunks": {"hour_100_200": {"digest": "immutable-marker"}},
    }
    data = {
        "revision": 8,
        "rooms": {"room": {"name": "Room", "area_id": "area", "visits": []}},
        "sources": {"private-source-key": {"entity_id": "sensor.private"}},
        "streams": {"private-stream-key": {"coverage": []}},
        "configured": {},
        "original_config": config,
        "migration": migration,
    }
    if version_marker:
        data["version"] = 1
    return data


@pytest.mark.parametrize("version_marker", [False, True])
def test_known_legacy_schema_migration_preserves_all_evidence(version_marker):
    original = _history_document(version_marker)
    before = deepcopy(original)

    migrated = migrate_history_data(original)

    assert migrated["version"] == HISTORY_DATA_VERSION
    assert migrated is not original
    assert migrated["migration"] == before["migration"]
    assert migrated["sources"] == before["sources"]
    assert migrated["streams"] == before["streams"]
    assert original == before
    assert original.get("version") == (1 if version_marker else None)


@pytest.mark.parametrize(
    ("version", "code"),
    [
        (3, "newer_history_version"),
        (0, "unsupported_history_version"),
        (True, "invalid_history_version"),
        (2.0, "invalid_history_version"),
        ("2", "invalid_history_version"),
        (None, "invalid_history_version"),
    ],
)
def test_unknown_or_malformed_schema_versions_are_rejected(version, code):
    stored = _history_document()
    stored["version"] = version
    before = deepcopy(stored)

    with pytest.raises(HistoryDataError) as exc_info:
        migrate_history_data(stored)

    assert exc_info.value.code == code
    assert stored == before


async def test_runtime_initialize_migrates_legacy_version_without_losing_archive_refs(hass):
    config = {"rooms": {}}
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    stored = _history_document()
    stored["sources"] = {}
    stored["streams"] = {}
    stored["migration"] = migration_manifest(
        config, entry.entry_id, manager._now()
    )
    stored["migration"]["sources"]["private.sensor"] = {
        "statistic_id": "thermal_efficiency:private",
        "chunks": {"hour_100_200": {"digest": "immutable-marker"}},
    }
    await manager.store.async_save(stored)

    await manager.async_initialize()

    assert manager.data["version"] == HISTORY_DATA_VERSION
    assert manager.data["migration"]["sources"]["private.sensor"] == stored[
        "migration"
    ]["sources"]["private.sensor"]
    persisted = await manager.store.async_load()
    assert persisted["migration"]["sources"]["private.sensor"] == stored[
        "migration"
    ]["sources"]["private.sensor"]
    await manager.async_shutdown()


async def test_runtime_initialize_refuses_newer_version_without_saving(hass):
    config = {"rooms": {}}
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    stored = _history_document()
    stored["version"] = HISTORY_DATA_VERSION + 1
    await manager.store.async_save(stored)

    with pytest.raises(HistoryDataError) as exc_info:
        await manager.async_initialize()

    assert exc_info.value.code == "newer_history_version"
    assert manager.data == {}
    assert await manager.store.async_load() == stored


def test_retry_policy_pauses_conflicts_but_allows_manual_retry():
    failed_conflict = {
        "status": "failed",
        "failure": {"retryable": False},
    }
    failed_transient = {
        "status": "failed",
        "failure": {"retryable": True},
        "next_retry": 500,
    }

    assert not _migration_retry_allowed(failed_conflict, 1000)
    assert _migration_retry_allowed(failed_conflict, 1000, force=True)
    assert not _migration_retry_allowed(failed_transient, 499)
    assert _migration_retry_allowed(failed_transient, 500)


@pytest.fixture
async def history_manager(hass):
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)
    rooms = {}
    for room_id in ("one", "two"):
        area = area_registry.async_create(room_id.title())
        entity = entity_registry.async_get_or_create(
            "sensor", "test", room_id, suggested_object_id=room_id
        )
        entity_registry.async_update_entity(entity.entity_id, area_id=area.id)
        rooms[room_id] = {"temperature": entity.entity_id}
    config = {"rooms": rooms}
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    yield manager
    await manager.async_shutdown()


async def test_runtime_migration_failure_is_structured_private_and_manually_retryable(
    history_manager, monkeypatch
):
    manager = history_manager
    conflict = MigrationFailure(
        "source_inventory_mismatch",
        "verification",
        "Source changed or disappeared during hour preservation.",
        retryable=False,
        source_index=0,
        chunk="hour",
    )
    run = AsyncMock(side_effect=[conflict, conflict])
    monkeypatch.setattr(HistoryMigrator, "run", run)

    await manager.async_migrate_history()

    migration = manager.data["migration"]
    assert migration["status"] == "failed"
    assert migration["error"] == "MigrationFailure"  # legacy field remains
    failure = migration["failure"]
    assert failure["stage"] == "verification"
    assert failure["code"] == "source_inventory_mismatch"
    assert failure["type"] == "MigrationFailure"
    assert failure["reason"] == "Source changed or disappeared during hour preservation."
    assert failure["retryable"] is False
    assert "manual retry" in failure["guidance"].casefold()
    assert failure["source_index"] == 0
    assert failure["chunk"] == "hour"
    assert "sensor.private" not in repr(failure)
    assert "private-source-key" not in repr(failure)
    assert "private" not in failure["guidance"].casefold()
    assert "next_retry" not in migration

    manager._start_migration()
    assert manager._migration_task is None

    await manager.async_retry_migration()
    assert manager._migration_task is not None
    await manager._migration_task
    assert run.await_count == 2
    assert manager.data["migration"]["status"] == "failed"
    assert manager.data["migration"]["failure"]["retryable"] is False


async def test_migrator_archive_conflict_reports_only_anonymous_context(hass):
    config = {"rooms": {"office": {"temperature": "sensor.private"}}}
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manifest = migration_manifest(config, entry.entry_id, 1_800_000_000.0)
    manifest["sources"]["sensor.private"]["chunks"]["hour_100_200"] = {
        "kind": "hour",
        "digest": "immutable-marker",
    }
    data = {"migration": manifest}
    archived_marker = deepcopy(manifest["sources"]["sensor.private"]["chunks"])
    migrator = HistoryMigrator(hass, entry, data, AsyncMock())

    with pytest.raises(MigrationFailure) as exc_info:
        await migrator._archive(
            "sensor.private",
            "hour",
            datetime.fromtimestamp(100, UTC),
            datetime.fromtimestamp(200, UTC),
            None,
        )

    failure = migrator.failure_details(exc_info.value)
    assert failure["stage"] == "archive"
    assert failure["code"] == "archive_chunk_missing"
    assert failure["retryable"] is False
    assert failure["source_index"] == 0
    assert failure["chunk"] == "hour_100_200"
    assert "sensor.private" not in repr(failure)
    assert "private" not in failure["guidance"].casefold()
    assert data["migration"]["sources"]["sensor.private"]["chunks"] == archived_marker


async def test_registry_revision_invalidates_stale_form_synchronously(
    hass, history_manager
):
    manager = history_manager
    old_revision = manager.data["revision"]
    registry = er.async_get(hass)
    destination_area = manager.data["rooms"]["two"]["area_id"]
    registry.async_update_entity("sensor.one", area_id=destination_area)

    event = Event(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        {},
        time_fired_timestamp=dt_util.utcnow().timestamp(),
    )
    manager._registry_changed(event)

    assert manager.data["revision"] == old_revision + 1
    with pytest.raises(ValueError, match="stale_revision"):
        await manager.async_edit_visit("not-used", 0, 0, False, old_revision)
    await hass.async_block_till_done()
    assert manager.data["revision"] == old_revision + 1


async def test_prepared_history_reports_revision_and_schema_from_snapshot(history_manager):
    manager = history_manager
    revision = manager.data["revision"]
    version = manager.data["version"]

    _stats, prepared = await manager.async_prepare({}, manager.config, UTC)

    assert prepared["history_revision"] == revision
    assert prepared["history_schema_version"] == version == HISTORY_DATA_VERSION
