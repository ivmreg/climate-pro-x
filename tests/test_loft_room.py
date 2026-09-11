"""Loft regressions for the unified room and source-history model."""

from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.assignments import (
    analysis_configuration,
    compose,
    configured_inputs,
    identity,
    validate_visits,
)
from custom_components.thermal_efficiency.config_flow import (
    ThermalEfficiencyConfigFlow,
    ThermalEfficiencyOptionsFlow,
)
from custom_components.thermal_efficiency.config_migration import (
    migrate_legacy_loft_config,
)
from custom_components.thermal_efficiency.const import (
    CONF_ASSIGNMENT_SINCE,
    CONF_HUMIDITY,
    CONF_LOFT,
    CONF_LOFT_HUMIDITY,
    CONF_LOFT_SINCE,
    CONF_MAX_WINDOW_DAYS,
    CONF_ROOMS,
    CONF_ROOM_TYPE,
    CONF_TEMPERATURE,
    HISTORY_DATA_VERSION,
    ROOM_TYPE_CONDITIONED,
    ROOM_TYPE_LOFT,
)
from custom_components.thermal_efficiency.coordinator import ThermalCoordinator
from custom_components.thermal_efficiency.history import (
    RoomHistoryManager,
    assignment_timestamp,
)
from custom_components.thermal_efficiency import CONFIG_SCHEMA


def _create_sensor(hass, unique_id: str, area_id: str):
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor", "test", unique_id, suggested_object_id=unique_id
    )
    return registry.async_update_entity(entity.entity_id, area_id=area_id)


async def test_legacy_loft_config_becomes_an_area_backed_typed_room(hass):
    living = ar.async_get(hass).async_create("Living room")
    loft_area = ar.async_get(hass).async_create("Loft")
    _create_sensor(hass, "living_temperature", living.id)
    loft = _create_sensor(hass, "loft_temperature", loft_area.id)
    humidity = _create_sensor(hass, "loft_humidity", loft_area.id)
    old_loft = _create_sensor(hass, "old_loft_temperature", loft_area.id)
    legacy = {
        "outdoor": "sensor.outdoor",
        CONF_LOFT: loft.entity_id,
        CONF_LOFT_HUMIDITY: humidity.entity_id,
        CONF_LOFT_SINCE: "2026-07-03",
        CONF_ROOMS: {
            "living_room": {
                "name": "Living room",
                CONF_TEMPERATURE: "sensor.living_temperature",
            },
            loft_area.id: {
                "name": "Loft",
                CONF_TEMPERATURE: old_loft.entity_id,
                "heating_power": "sensor.old_loft_heating",
            },
        },
    }

    migrated = migrate_legacy_loft_config(hass, legacy)

    assert CONF_LOFT not in migrated
    assert CONF_LOFT_HUMIDITY not in migrated
    assert CONF_LOFT_SINCE not in migrated
    assert migrated[CONF_ROOMS]["living_room"][CONF_ROOM_TYPE] == ROOM_TYPE_CONDITIONED
    assert migrated[CONF_ROOMS][loft_area.id] == {
        "name": "Loft",
        CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
        CONF_TEMPERATURE: old_loft.entity_id,
        "heating_power": "sensor.old_loft_heating",
    }
    loft_room_id = f"{loft_area.id}_2"
    assert migrated[CONF_ROOMS][loft_room_id] == {
        "name": "Loft",
        CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
        CONF_TEMPERATURE: loft.entity_id,
        CONF_HUMIDITY: humidity.entity_id,
        CONF_ASSIGNMENT_SINCE: "2026-07-03",
    }
    prepared = analysis_configuration(migrated)
    assert prepared[CONF_LOFT] == loft.entity_id
    assert prepared[CONF_LOFT_HUMIDITY] == humidity.entity_id
    assert prepared[CONF_LOFT_SINCE] == date(2026, 7, 3)
    assert set(prepared[CONF_ROOMS]) == {"living_room", loft_area.id}
    assert {loft.entity_id, humidity.entity_id, old_loft.entity_id} <= configured_inputs(migrated)


async def test_v06_loft_archive_is_bridged_into_dated_room_history(hass):
    living = ar.async_get(hass).async_create("Living room")
    loft_area = ar.async_get(hass).async_create("Loft")
    living_sensor = _create_sensor(hass, "living_temperature", living.id)
    loft_sensor = _create_sensor(hass, "loft_temperature", loft_area.id)
    humidity_sensor = _create_sensor(hass, "loft_humidity", loft_area.id)
    legacy = {
        "outdoor": "sensor.outdoor",
        CONF_LOFT: loft_sensor.entity_id,
        CONF_LOFT_HUMIDITY: humidity_sensor.entity_id,
        CONF_LOFT_SINCE: "2026-07-03",
        CONF_ROOMS: {
            "living_room": {
                "name": "Living room",
                CONF_TEMPERATURE: living_sensor.entity_id,
            }
        },
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=legacy, version=1)
    entry.add_to_hass(hass)

    old = RoomHistoryManager(hass, entry, legacy)
    old._now = lambda: datetime(2026, 9, 10, tzinfo=UTC).timestamp()
    await old.async_initialize()
    cutoff = datetime(2026, 9, 11, 12, tzinfo=UTC).timestamp()
    old.data["migration"].update(
        status="complete",
        cutoff=cutoff,
        sources={
            loft_sensor.entity_id: {
                "statistic_id": "thermal_efficiency:legacy_loft_temperature",
                "chunks": {},
            },
            humidity_sensor.entity_id: {
                "statistic_id": "thermal_efficiency:legacy_loft_humidity",
                "chunks": {},
            },
        },
    )
    await old._save()
    await old.async_shutdown()

    migrated = migrate_legacy_loft_config(hass, legacy)
    hass.config_entries.async_update_entry(entry, data=migrated, version=2)
    bridge_end = datetime(2026, 9, 11, 14, tzinfo=UTC).timestamp()
    manager = RoomHistoryManager(hass, entry, migrated)
    manager._now = lambda: bridge_end
    await manager.async_initialize()

    loft_id = next(
        rid
        for rid, room in migrated[CONF_ROOMS].items()
        if room[CONF_ROOM_TYPE] == ROOM_TYPE_LOFT
    )
    loft_visits = manager.data["rooms"][loft_id]["visits"]
    assert manager.data["version"] == HISTORY_DATA_VERSION
    assert {visit["role"] for visit in loft_visits} == {
        CONF_TEMPERATURE,
        CONF_HUMIDITY,
    }
    assert all(visit["cause"] == "loft_migration" for visit in loft_visits)
    assert all(
        visit["start"] == assignment_timestamp("2026-07-03")
        for visit in loft_visits
    )
    for visit in loft_visits:
        stream = manager.data["streams"][visit["stream"]]
        assert stream["legacy_bridge_end"] == bridge_end
        assert stream["original_entity_id"] in {
            loft_sensor.entity_id,
            humidity_sensor.entity_id,
        }
    assert {
        loft_sensor.entity_id,
        humidity_sensor.entity_id,
    } <= manager.statistic_ids()

    await manager._save()
    await manager.async_shutdown()

    replacement = _create_sensor(hass, "replacement_loft_temperature", loft_area.id)
    changed = deepcopy(migrated)
    changed[CONF_ROOMS][loft_id][CONF_TEMPERATURE] = replacement.entity_id
    hass.config_entries.async_update_entry(entry, data=changed)
    replacement_time = datetime(2026, 9, 12, 9, tzinfo=UTC).timestamp()
    restarted = RoomHistoryManager(hass, entry, changed)
    restarted._now = lambda: replacement_time
    await restarted.async_initialize()
    active = next(
        visit
        for visit in restarted.data["rooms"][loft_id]["visits"]
        if visit["role"] == CONF_TEMPERATURE and visit["end"] is None
    )
    active_stream = restarted.data["streams"][active["stream"]]
    assert active["cause"] == "replacement"
    assert active["start"] == replacement_time
    assert active_stream["original_entity_id"] == replacement.entity_id
    assert "legacy_bridge_end" not in active_stream
    await restarted.async_shutdown()


def test_compose_routes_loft_visits_through_archive_bridge_and_owned_stream():
    stream_id = "loft-temperature-stream"
    owned_id = "sensor.owned_loft_temperature"
    archive_id = "thermal_efficiency:legacy_loft_temperature"
    room_statistic = f"thermal_efficiency:room_{identity('loft', CONF_TEMPERATURE)}"
    config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            "living": {
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: "sensor.living",
            },
            "loft": {
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: "sensor.loft",
            },
        },
    }
    data = {
        "migration": {
            "status": "complete",
            "cutoff": 10_800,
            "sources": {
                "sensor.loft": {"statistic_id": archive_id},
            },
        },
        "rooms": {
            "living": {"visits": []},
            "loft": {
                "visits": [
                    {
                        "stream": stream_id,
                        "role": CONF_TEMPERATURE,
                        "start": 3_600,
                        "end": None,
                        "cause": "loft_migration",
                    }
                ]
            },
        },
        "streams": {
            stream_id: {
                "original_entity_id": "sensor.loft",
                "entity_id": owned_id,
                "aliases": [],
                "role": CONF_TEMPERATURE,
                "coverage": [{"start": 18_000, "end": None}],
                "quarantine": [],
                "legacy_bridge_end": 18_000,
            }
        },
    }
    row = lambda start, mean: {"start": start, "mean": mean}
    stats = {
        archive_id: [row(0, 9), row(3_600, 10), row(7_200, 11)],
        "sensor.loft": [row(10_800, 12), row(14_400, 13)],
        owned_id: [row(14_400, 99), row(18_000, 14)],
    }

    prepared_stats, prepared_config = compose(stats, config, data, UTC)

    assert prepared_config[CONF_LOFT] == room_statistic
    assert prepared_config[CONF_LOFT_SINCE] is None
    assert set(prepared_config[CONF_ROOMS]) == {"living"}
    assert [row["mean"] for row in prepared_stats[room_statistic]] == [
        10,
        11,
        12,
        13,
        14,
    ]


async def test_temperature_area_move_uses_same_logic_for_loft_and_room(hass):
    living_area = ar.async_get(hass).async_create("Living")
    loft_area = ar.async_get(hass).async_create("Loft")
    living = _create_sensor(hass, "living_temperature", living_area.id)
    loft = _create_sensor(hass, "loft_temperature", loft_area.id)
    config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            living_area.id: {
                "name": "Living",
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: living.entity_id,
            },
            loft_area.id: {
                "name": "Loft",
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft.entity_id,
            },
        },
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=config, version=2)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    original = next(
        binding
        for binding in manager.bindings()
        if binding.source_entity_id == living.entity_id
        and binding.room_id == living_area.id
    )

    er.async_get(hass).async_update_entity(living.entity_id, area_id=loft_area.id)
    assert manager._reconcile_registry(manager._now())
    moved = next(
        binding
        for binding in manager.bindings()
        if binding.source_entity_id == living.entity_id
        and binding.room_id == loft_area.id
    )
    assert manager._stream_active(moved.id)
    assert not manager._stream_active(original.id)

    er.async_get(hass).async_update_entity(living.entity_id, area_id=living_area.id)
    assert manager._reconcile_registry(manager._now() + 1)
    assert manager._stream_active(original.id)
    validate_visits(manager.data)
    await manager.async_shutdown()


async def test_config_flow_allows_one_loft_and_requires_a_conditioned_room(hass):
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass
    await flow.async_step_user({"outdoor": "sensor.outdoor"})
    await flow.async_step_room({CONF_ROOM_TYPE: ROOM_TYPE_LOFT})
    result = await flow.async_step_room_details(
        {
            "name": "Loft",
            CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
            CONF_TEMPERATURE: "sensor.loft_temperature",
            CONF_HUMIDITY: "sensor.loft_humidity",
            "add_another": True,
        }
    )
    assert result["step_id"] == "room"

    await flow.async_step_room({CONF_ROOM_TYPE: ROOM_TYPE_LOFT})
    duplicate = await flow.async_step_room_details(
        {
            "name": "Other loft",
            CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
            CONF_TEMPERATURE: "sensor.other_loft_temperature",
            "add_another": True,
        }
    )
    assert duplicate["errors"] == {CONF_ROOM_TYPE: "duplicate_loft"}

    await flow.async_step_room({CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED})
    completed = await flow.async_step_room_details(
        {
            "name": "Living",
            CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
            CONF_TEMPERATURE: "sensor.living_temperature",
            "add_another": False,
        }
    )
    assert completed["type"] is FlowResultType.CREATE_ENTRY
    assert completed["data"][CONF_ROOMS]["loft"][CONF_HUMIDITY] == (
        "sensor.loft_humidity"
    )


def test_yaml_schema_rejects_incompatible_or_duplicate_loft_rooms():
    base = {
        "thermal_efficiency": {
            "outdoor": "sensor.outdoor",
            CONF_ROOMS: {
                "living": {CONF_TEMPERATURE: "sensor.living"},
                "loft": {
                    CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                    CONF_TEMPERATURE: "sensor.loft",
                },
            },
        }
    }
    assert CONFIG_SCHEMA(base)["thermal_efficiency"][CONF_ROOMS]["loft"][
        CONF_ROOM_TYPE
    ] == ROOM_TYPE_LOFT

    incompatible = deepcopy(base)
    incompatible["thermal_efficiency"][CONF_ROOMS]["loft"]["heating_power"] = (
        "sensor.loft_heating"
    )
    with pytest.raises(vol.Invalid, match="cannot have heating power"):
        CONFIG_SCHEMA(incompatible)

    duplicate = deepcopy(base)
    duplicate["thermal_efficiency"][CONF_ROOMS]["second_loft"] = {
        CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
        CONF_TEMPERATURE: "sensor.second_loft",
    }
    with pytest.raises(vol.Invalid, match="Only one loft"):
        CONFIG_SCHEMA(duplicate)


def test_yaml_schema_rejects_top_level_loft_and_loft_room_simultaneously():
    conf = {
        "thermal_efficiency": {
            "outdoor": "sensor.outdoor",
            CONF_LOFT: "sensor.legacy_loft",
            CONF_ROOMS: {
                "living": {CONF_TEMPERATURE: "sensor.living"},
                "loft": {
                    CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                    CONF_TEMPERATURE: "sensor.loft_room",
                },
            },
        }
    }
    with pytest.raises(vol.Invalid, match="simultaneously"):
        CONFIG_SCHEMA(conf)


def test_unconfigured_loft_humidity_remains_none():
    config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            "living": {
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: "sensor.living",
            },
            "loft": {
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: "sensor.loft",
            },
        },
    }
    prepared = analysis_configuration(config)
    assert prepared[CONF_LOFT] == "sensor.loft"
    assert prepared[CONF_LOFT_HUMIDITY] is None

    stats = {
        "sensor.outdoor": [],
        "sensor.living": [],
        "sensor.loft": [],
    }
    data = {
        "migration": {
            "status": "complete",
            "cutoff": 0,
            "sources": {},
        },
        "streams": {
            "s1": {
                "id": "s1",
                "source_id": "src1",
                "original_entity_id": "sensor.loft",
                "coverage": [],
            }
        },
        "rooms": {
            "loft": {
                "visits": [
                    {
                        "id": "v1",
                        "stream": "s1",
                        "role": CONF_TEMPERATURE,
                        "start": 0,
                        "end": None,
                        "expected": True,
                        "cause": "legacy",
                    }
                ]
            }
        },
    }
    composed_stats, composed_conf = compose(stats, config, data, UTC)
    assert composed_conf["loft"] == f"thermal_efficiency:room_{identity('loft', CONF_TEMPERATURE)}"
    assert composed_conf["loft_humidity"] is None


async def test_late_added_loft_and_updating_assignment_since(hass):
    living_area = ar.async_get(hass).async_create("Living room")
    loft_area = ar.async_get(hass).async_create("Loft")
    living = _create_sensor(hass, "living_temp", living_area.id)
    loft = _create_sensor(hass, "loft_temp", loft_area.id)

    entry = MockConfigEntry(
        domain="thermal_efficiency",
        data={
            "outdoor": "sensor.outdoor",
            CONF_ROOMS: {
                "living": {
                    "name": "Living room",
                    CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                    CONF_TEMPERATURE: living.entity_id,
                }
            },
        },
        version=2,
    )
    entry.add_to_hass(hass)

    start_time = datetime(2026, 9, 1, 10, tzinfo=UTC).timestamp()
    manager = RoomHistoryManager(hass, entry, entry.data)
    manager._now = lambda: start_time
    await manager.async_initialize()
    manager.data["migration"]["sources"][loft.entity_id] = {
        "statistic_id": "thermal_efficiency:legacy_loft",
        "chunks": {},
    }
    await manager._save()
    await manager.async_shutdown()

    # Late-add loft with assignment_since
    loft_since = "2026-08-15"
    late_added_config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            "living": {
                "name": "Living room",
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: living.entity_id,
            },
            "loft": {
                "name": "Loft",
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft.entity_id,
                CONF_ASSIGNMENT_SINCE: loft_since,
            },
        },
    }
    hass.config_entries.async_update_entry(entry, data=late_added_config)
    late_time = datetime(2026, 9, 2, 10, tzinfo=UTC).timestamp()
    manager2 = RoomHistoryManager(hass, entry, late_added_config)
    manager2._now = lambda: late_time
    await manager2.async_initialize()

    loft_visits = manager2.data["rooms"]["loft"]["visits"]
    active_visit = next(v for v in loft_visits if v["end"] is None and v["role"] == CONF_TEMPERATURE)
    assert active_visit["start"] == assignment_timestamp(loft_since)
    assert active_visit["cause"] == "loft_migration"
    active_stream = manager2.data["streams"][active_visit["stream"]]
    assert active_stream["legacy_bridge_end"] == late_time

    await manager2._save()
    await manager2.async_shutdown()

    # Update assignment_since with unchanged entity ID
    updated_since = "2026-07-01"
    updated_config = deepcopy(late_added_config)
    updated_config[CONF_ROOMS]["loft"][CONF_ASSIGNMENT_SINCE] = updated_since
    hass.config_entries.async_update_entry(entry, data=updated_config)

    manager3 = RoomHistoryManager(hass, entry, updated_config)
    manager3._now = lambda: late_time + 100
    await manager3.async_initialize()

    updated_visits = manager3.data["rooms"]["loft"]["visits"]
    updated_active = next(v for v in updated_visits if v["end"] is None and v["role"] == CONF_TEMPERATURE)
    assert updated_active["start"] == assignment_timestamp(updated_since)
    assert updated_active["cause"] == "loft_migration"
    await manager3._save()
    await manager3.async_shutdown()

    # Clear assignment_since
    cleared_config = deepcopy(updated_config)
    cleared_config[CONF_ROOMS]["loft"].pop(CONF_ASSIGNMENT_SINCE)
    hass.config_entries.async_update_entry(entry, data=cleared_config)

    manager4 = RoomHistoryManager(hass, entry, cleared_config)
    manager4._now = lambda: late_time + 200
    await manager4.async_initialize()
    cleared_visits = manager4.data["rooms"]["loft"]["visits"]
    cleared_active = next(v for v in cleared_visits if v["end"] is None and v["role"] == CONF_TEMPERATURE)
    assert cleared_active["start"] is None
    assert cleared_active["legacy"] is True
    assert cleared_active["cause"] == "legacy"
    assert cleared_active["provenance"] == "legacy_mapping_unverified"
    await manager4.async_shutdown()


async def test_late_added_loft_without_migration_source_sets_bridge_and_statistic_ids(hass):
    living_area = ar.async_get(hass).async_create("Living room 2")
    loft_area = ar.async_get(hass).async_create("Loft 2")
    living = _create_sensor(hass, "living_temp_2", living_area.id)
    loft = _create_sensor(hass, "loft_temp_2", loft_area.id)

    entry = MockConfigEntry(
        domain="thermal_efficiency",
        data={
            "outdoor": "sensor.outdoor",
            CONF_ROOMS: {
                "living": {
                    "name": "Living room",
                    CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                    CONF_TEMPERATURE: living.entity_id,
                }
            },
        },
        version=2,
    )
    entry.add_to_hass(hass)

    start_time = datetime(2026, 9, 1, 10, tzinfo=UTC).timestamp()
    manager = RoomHistoryManager(hass, entry, entry.data)
    manager._now = lambda: start_time
    await manager.async_initialize()
    await manager._save()
    await manager.async_shutdown()

    # Late-add loft without it being in migration sources
    loft_since = "2026-08-20"
    late_config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            "living": {
                "name": "Living room",
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: living.entity_id,
            },
            "loft": {
                "name": "Loft",
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft.entity_id,
                CONF_ASSIGNMENT_SINCE: loft_since,
            },
        },
    }
    hass.config_entries.async_update_entry(entry, data=late_config)
    late_time = datetime(2026, 9, 2, 12, tzinfo=UTC).timestamp()
    manager2 = RoomHistoryManager(hass, entry, late_config)
    manager2._now = lambda: late_time
    await manager2.async_initialize()

    loft_visits = manager2.data["rooms"]["loft"]["visits"]
    active_visit = next(v for v in loft_visits if v["end"] is None and v["role"] == CONF_TEMPERATURE)
    assert active_visit["start"] == assignment_timestamp(loft_since)
    assert active_visit["cause"] == "loft_migration"
    active_stream = manager2.data["streams"][active_visit["stream"]]
    assert active_stream["legacy_bridge_end"] == late_time
    assert loft.entity_id in manager2.statistic_ids()
    await manager2.async_shutdown()


async def test_coordinator_without_history_uses_analysis_configuration(hass, monkeypatch):
    import custom_components.thermal_efficiency.coordinator as coord_mod

    loft_area = ar.async_get(hass).async_create("Loft")
    living_area = ar.async_get(hass).async_create("Living")
    loft = _create_sensor(hass, "coord_loft_temp", loft_area.id)
    living = _create_sensor(hass, "coord_living_temp", living_area.id)

    raw_conf = {
        "outdoor": "sensor.outdoor",
        CONF_MAX_WINDOW_DAYS: 30,
        CONF_ROOMS: {
            "living": {
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: living.entity_id,
            },
            "loft": {
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft.entity_id,
            },
        },
    }
    coordinator = ThermalCoordinator(hass, raw_conf, history=None)
    stats_ids = coordinator._statistic_ids()
    assert living.entity_id in stats_ids
    assert loft.entity_id in stats_ids
    assert "sensor.outdoor" in stats_ids

    captured_conf = {}

    def _fake_stats(*args, **kwargs):
        return {s: [] for s in stats_ids}

    def _fake_compute_all(stats, conf, tz, now, windows):
        captured_conf.update(conf)
        return {"rooms": {}, "loft": None}

    monkeypatch.setattr(coord_mod, "get_instance", lambda hass_: hass_)
    monkeypatch.setattr(coord_mod, "statistics_during_period", _fake_stats)
    monkeypatch.setattr(coord_mod.thermal_math, "compute_all", _fake_compute_all)

    await coordinator._async_update_data()
    assert set(captured_conf["rooms"]) == {"living"}
    assert captured_conf["loft"] == loft.entity_id
    assert captured_conf["loft_humidity"] is None



async def test_config_flow_resolve_and_replace_validations(hass):
    living_area = ar.async_get(hass).async_create("Living")
    loft_area = ar.async_get(hass).async_create("Loft")
    living = _create_sensor(hass, "cf_living_temp", living_area.id)
    loft = _create_sensor(hass, "cf_loft_temp", loft_area.id)

    config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            "living": {
                "name": "Living",
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: living.entity_id,
            },
            "loft": {
                "name": "Loft",
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft.entity_id,
            },
        },
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=config, version=2)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()

    class Runtime:
        history = manager

    entry.runtime_data = Runtime()

    humidity_source = manager._source("sensor.humidity", "humidity")
    humidity_source["pending"] = {"since": manager._now() - 100, "observed": manager._now() - 50}

    options_flow = ThermalEfficiencyOptionsFlow()
    options_flow.hass = hass
    options_flow.handler = entry.entry_id

    await options_flow.async_step_init()
    res = await options_flow.async_step_resolve({
        "source": humidity_source["id"],
        "room": "living",
        "effective_time": datetime.now(UTC).isoformat(),
    })
    assert res["type"] is FlowResultType.FORM
    assert res["errors"] == {"base": "role_not_supported"}

    with pytest.raises(ValueError, match="role_not_supported"):
        await manager.async_correct(
            humidity_source["id"], "living", manager._now() - 75, manager.data["revision"]
        )

    hass.states.async_set("sensor.bad_heating", "not_a_number", {"unit_of_measurement": "%"})
    replace_res = await options_flow.async_step_replace({
        "room": "living",
        "role": "heating_power",
        "source": "sensor.bad_heating",
    })
    assert replace_res["type"] is FlowResultType.FORM
    assert replace_res["errors"] == {"base": "heating_power_must_be_percent"}
    await manager.async_shutdown()


def test_loft_since_validation():
    from custom_components.thermal_efficiency.validation import _loft_since

    assert _loft_since("2026-07-03") == "2026-07-03"
    with pytest.raises(vol.Invalid, match="ISO date"):
        _loft_since("invalid-date")


async def test_config_flow_assignment_since_validation(hass):
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass
    await flow.async_step_user({"outdoor": "sensor.outdoor"})
    await flow.async_step_room({CONF_ROOM_TYPE: ROOM_TYPE_LOFT})
    res_bad_date = await flow.async_step_room_details({
        "name": "Loft",
        CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
        CONF_TEMPERATURE: "sensor.loft_temperature",
        CONF_ASSIGNMENT_SINCE: "not-a-date",
        "add_another": True,
    })
    assert res_bad_date["type"] is FlowResultType.FORM
    assert res_bad_date["errors"] == {CONF_ASSIGNMENT_SINCE: "invalid_date"}

    await flow.async_step_room({CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED})
    res_cond_with_since = await flow.async_step_room_details({
        "name": "Living",
        CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
        CONF_TEMPERATURE: "sensor.living_temperature",
        CONF_ASSIGNMENT_SINCE: "2026-07-03",
        "add_another": False,
    })
    assert res_cond_with_since["type"] is FlowResultType.FORM
    assert res_cond_with_since["errors"] == {CONF_ASSIGNMENT_SINCE: "role_not_supported"}


async def test_replaced_loft_with_assignment_since_and_migrated_source(hass):
    loft_area = ar.async_get(hass).async_create("Loft")
    living_area = ar.async_get(hass).async_create("Living")
    living = _create_sensor(hass, "repl_living_temp", living_area.id)
    loft1 = _create_sensor(hass, "repl_loft_temp_1", loft_area.id)
    loft2 = _create_sensor(hass, "repl_loft_temp_2", loft_area.id)
    loft3 = _create_sensor(hass, "repl_loft_temp_3", loft_area.id)

    initial_config = {
        "outdoor": "sensor.outdoor",
        CONF_ROOMS: {
            "living": {
                "name": "Living",
                CONF_ROOM_TYPE: ROOM_TYPE_CONDITIONED,
                CONF_TEMPERATURE: living.entity_id,
            },
            "loft": {
                "name": "Loft",
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft1.entity_id,
                CONF_ASSIGNMENT_SINCE: "2026-07-01",
            },
        },
    }
    entry = MockConfigEntry(domain="thermal_efficiency", data=initial_config, version=2)
    entry.add_to_hass(hass)

    start_time = datetime(2026, 9, 1, 10, tzinfo=UTC).timestamp()
    manager = RoomHistoryManager(hass, entry, initial_config)
    manager._now = lambda: start_time
    await manager.async_initialize()
    manager.data["migration"]["sources"][loft3.entity_id] = {
        "statistic_id": "thermal_efficiency:legacy_loft3",
        "chunks": {},
    }
    await manager._save()
    await manager.async_shutdown()

    # Replace via config with new sensor and updated assignment_since
    t2 = datetime(2026, 9, 2, 10, tzinfo=UTC).timestamp()
    config_v2 = deepcopy(initial_config)
    config_v2[CONF_ROOMS]["loft"][CONF_TEMPERATURE] = loft2.entity_id
    config_v2[CONF_ROOMS]["loft"][CONF_ASSIGNMENT_SINCE] = "2026-08-10"
    hass.config_entries.async_update_entry(entry, data=config_v2)

    manager2 = RoomHistoryManager(hass, entry, config_v2)
    manager2._now = lambda: t2
    await manager2.async_initialize()

    loft_visits = manager2.data["rooms"]["loft"]["visits"]
    active_visit = next(v for v in loft_visits if v["end"] is None and v["role"] == CONF_TEMPERATURE)
    assert active_visit["start"] == assignment_timestamp("2026-08-10")
    assert active_visit["cause"] == "loft_migration"
    active_stream = manager2.data["streams"][active_visit["stream"]]
    assert active_stream["legacy_bridge_end"] == t2

    # Replace via async_replace with loft3 (which is in migration sources)
    t3 = datetime(2026, 9, 3, 10, tzinfo=UTC).timestamp()
    manager2._now = lambda: t3
    hass.states.async_set(loft3.entity_id, "21.5", {"unit_of_measurement": "°C"})
    await manager2.async_replace("loft", "temperature", loft3.entity_id, manager2.data["revision"])

    active_visit3 = next(v for v in manager2.data["rooms"]["loft"]["visits"] if v["end"] is None and v["role"] == CONF_TEMPERATURE)
    assert active_visit3["cause"] == "loft_migration"
    assert active_visit3["provenance"] == "loft_migration"
    assert active_visit3["start"] == t3
    stream3 = manager2.data["streams"][active_visit3["stream"]]
    assert stream3["legacy_bridge_end"] == t3
    await manager2.async_shutdown()


