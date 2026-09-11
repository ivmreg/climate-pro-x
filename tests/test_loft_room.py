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
    CONF_ROOMS,
    CONF_ROOM_TYPE,
    CONF_TEMPERATURE,
    HISTORY_DATA_VERSION,
    ROOM_TYPE_CONDITIONED,
    ROOM_TYPE_LOFT,
)
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
        CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
        CONF_TEMPERATURE: loft.entity_id,
        CONF_HUMIDITY: humidity.entity_id,
        CONF_ASSIGNMENT_SINCE: "2026-07-03",
    }
    prepared = analysis_configuration(migrated)
    assert prepared[CONF_LOFT] == loft.entity_id
    assert prepared[CONF_LOFT_HUMIDITY] == humidity.entity_id
    assert prepared[CONF_LOFT_SINCE] == date(2026, 7, 3)
    assert set(prepared[CONF_ROOMS]) == {"living_room"}
    assert {loft.entity_id, humidity.entity_id} <= configured_inputs(migrated)


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
