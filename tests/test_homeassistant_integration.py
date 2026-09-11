"""Home Assistant fixture tests for setup boundaries and coordinator inputs."""

from __future__ import annotations

from datetime import timedelta

import pytest

homeassistant = pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import UnitOfEnergy
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.config_flow import (
    ThermalEfficiencyConfigFlow,
    ThermalEfficiencyOptionsFlow,
)
from custom_components.thermal_efficiency.const import (
    CONF_BOILER_EFFICIENCY,
    CONF_CO2,
    CONF_ELECTRICITY_METER,
    CONF_ELECTRICITY_UNIT_RATE,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_HEATING_POWER,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_OUTDOOR_CO2_SENSOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
    DEFAULT_MAX_WINDOW_DAYS,
    DOMAIN,
)
from custom_components.thermal_efficiency.coordinator import ThermalCoordinator
from custom_components.thermal_efficiency.validation import heating_power_issue


@pytest.fixture(autouse=True)
def _enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Allow Home Assistant to discover this repository's custom integration."""


def _config() -> dict:
    return {
        CONF_OUTDOOR: "sensor.outdoor_temperature",
        CONF_GAS_METER: "sensor.gas_energy",
        CONF_GAS_UNIT_RATE: "sensor.gas_rate",
        CONF_CO2: ["sensor.bedroom_co2", "sensor.living_room_co2"],
        CONF_OUTDOOR_CO2_SENSOR: "sensor.outdoor_co2",
        CONF_BOILER_EFFICIENCY: 0.88,
        CONF_MAX_WINDOW_DAYS: DEFAULT_MAX_WINDOW_DAYS,
        CONF_ROOMS: {
            "living_room": {CONF_TEMPERATURE: "sensor.living_temperature"}
        },
    }


async def test_config_flow_accepts_multiple_co2_sensors(hass):
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user(
        {
            CONF_OUTDOOR: "sensor.outdoor_temperature",
            CONF_CO2: ["sensor.bedroom_co2", "sensor.living_room_co2"],
            CONF_OUTDOOR_CO2_SENSOR: "sensor.outdoor_co2",
            CONF_BOILER_EFFICIENCY: 0.88,
            CONF_MAX_WINDOW_DAYS: 365,
        }
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "room"

    result = await flow.async_step_room({})
    assert result["step_id"] == "room_details"

    result = await flow.async_step_room_details(
        {
            "name": "Living room",
            CONF_TEMPERATURE: "sensor.living_temperature",
            "add_another": False,
        }
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CO2] == [
        "sensor.bedroom_co2",
        "sensor.living_room_co2",
    ]


async def test_config_flow_rejects_non_percent_heating_source(hass):
    hass.states.async_set(
        "sensor.radiator_watts",
        "350",
        {"unit_of_measurement": "W"},
    )
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass
    await flow.async_step_user({CONF_OUTDOOR: "sensor.outdoor_temperature"})
    await flow.async_step_room({})

    result = await flow.async_step_room_details(
        {
            "name": "Living room",
            CONF_TEMPERATURE: "sensor.living_temperature",
            CONF_HEATING_POWER: "sensor.radiator_watts",
            "add_another": False,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_HEATING_POWER: "heating_power_must_be_percent"
    }


async def test_options_flow_cannot_finish_without_a_room(hass):
    flow = ThermalEfficiencyOptionsFlow()
    flow.hass = hass
    flow._rooms = {}

    result = await flow.async_step_new_room({"finish": True})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "new_room"
    assert result["errors"] == {"base": "at_least_one_room"}


async def test_heating_power_runtime_validation(hass):
    hass.states.async_set("sensor.valid_demand", "42", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.watts", "42", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.out_of_range", "142", {"unit_of_measurement": "%"})

    assert heating_power_issue(hass, "sensor.valid_demand") is None
    assert "unit must be %" in heating_power_issue(hass, "sensor.watts")
    assert (
        heating_power_issue(
            hass, "sensor.temporarily_missing", allow_missing=True
        )
        is None
    )
    assert heating_power_issue(hass, "sensor.temporarily_missing") is not None
    assert "between 0 and 100" in heating_power_issue(
        hass, "sensor.out_of_range"
    )


async def test_coordinator_collects_multi_co2_and_outdoor_ids(hass):
    coordinator = ThermalCoordinator(hass, _config())

    assert coordinator._statistic_ids() == {
        "sensor.outdoor_temperature",
        "sensor.gas_energy",
        "sensor.bedroom_co2",
        "sensor.living_room_co2",
        "sensor.outdoor_co2",
        "sensor.living_temperature",
    }


async def test_tariff_is_normalized_from_pence_per_kwh(hass):
    coordinator = ThermalCoordinator(hass, _config())
    hass.states.async_set(
        "sensor.gas_rate",
        "5.25",
        {"unit_of_measurement": "p/kWh"},
    )

    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") == pytest.approx(0.0525)


async def test_tariff_with_unknown_unit_is_suppressed(hass):
    coordinator = ThermalCoordinator(hass, _config())
    hass.states.async_set(
        "sensor.gas_rate",
        "5.25",
        {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
    )

    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") is None


async def test_electricity_meter_and_tariff_are_wired(hass):
    config = _config() | {
        CONF_ELECTRICITY_METER: "sensor.electricity_energy",
        CONF_ELECTRICITY_UNIT_RATE: "sensor.electricity_rate",
    }
    coordinator = ThermalCoordinator(hass, config)
    hass.states.async_set(
        "sensor.electricity_rate",
        "18.4",
        {"unit_of_measurement": "p/kWh"},
    )

    assert "sensor.electricity_energy" in coordinator._statistic_ids()
    assert coordinator._unit_rate(
        CONF_ELECTRICITY_UNIT_RATE, "Electricity"
    ) == pytest.approx(0.184)


async def test_analysis_does_not_run_on_the_event_loop(hass, monkeypatch):
    """compute_all is ~1s of pure CPU on a full season of hourly statistics.
    Running it on the event loop stalls all of Home Assistant for that long,
    so it must be handed to the executor."""
    import threading

    from custom_components.thermal_efficiency import coordinator as coordinator_module

    loop_thread = threading.get_ident()
    ran_on: dict = {}

    def _fake_statistics(*args, **kwargs):
        ran_on["lookback"] = args[2] - args[1]
        return {}

    def _fake_compute_all(stats, conf, tz, now, windows):
        ran_on["thread"] = threading.get_ident()
        return {"rooms": {}}

    # No recorder is running in this fixture; borrow hass's own executor so the
    # statistics fetch resolves and the compute hand-off is what gets tested.
    monkeypatch.setattr(coordinator_module, "get_instance", lambda hass_: hass_)
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", _fake_statistics
    )
    monkeypatch.setattr(
        coordinator_module.thermal_math, "compute_all", _fake_compute_all
    )

    coordinator = ThermalCoordinator(hass, _config())
    await coordinator._async_update_data()

    assert ran_on["thread"] != loop_thread
    assert ran_on["lookback"] == timedelta(
        days=DEFAULT_MAX_WINDOW_DAYS * 2
    )


async def test_options_entry_can_hold_legacy_scalar_co2(hass):
    config = _config()
    config[CONF_CO2] = "sensor.bedroom_co2"
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)

    coordinator = ThermalCoordinator(hass, dict(entry.data))
    assert "sensor.bedroom_co2" in coordinator._statistic_ids()


async def test_complete_entry_setup_captures_and_unloads(recorder_mock, hass, monkeypatch):
    from unittest.mock import AsyncMock
    from homeassistant.helpers import entity_registry as er
    from custom_components.thermal_efficiency.history_migration import HistoryMigrator
    from custom_components.thermal_efficiency.diagnostics import async_get_config_entry_diagnostics

    monkeypatch.setattr(HistoryMigrator, "run", AsyncMock())
    entry = MockConfigEntry(domain=DOMAIN, data=_config(), version=1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 1  # public config remains compatible with rollback
    manager = entry.runtime_data.history
    await manager._migration_task
    binding = manager.bindings()[0]
    hass.states.async_set(binding.source_entity_id, "20", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert hass.states.get(binding.entity_id).state == "20.0"
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 14
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["archive_rows"] == {"raw": 0, "5minute": 0, "hour": 0}
    assert binding.source_entity_id not in str(diagnostics)
    # Compile a real hour of unchanged reports on the owned SensorEntity.
    from freezegun import freeze_time
    from homeassistant.util import dt as dt_util
    from homeassistant.components.recorder.tasks import StatisticsTask, ClearStatisticsTask
    from custom_components.thermal_efficiency.history_migration import HistoryMigrator, committed
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for index in range(12):
        beginning = base + timedelta(minutes=5 * index)
        with freeze_time(beginning):
            hass.states.async_set(binding.source_entity_id, "22", {"unit_of_measurement": "°C"})
            await hass.async_block_till_done()
            await committed(hass)
        with freeze_time(beginning + timedelta(minutes=5)):
            recorder_mock.queue_task(StatisticsTask(beginning, False))
            await committed(hass)
    migrator = HistoryMigrator(hass, entry, manager.data, manager._save)
    rows = await migrator.query(binding.entity_id, base, base + timedelta(hours=1), "hour")
    assert len(rows) == 1
    assert rows[0]["mean"] == 22
    hass.states.async_remove(binding.source_entity_id)
    recorder_mock.queue_task(ClearStatisticsTask(None, [binding.source_entity_id]))
    await committed(hass)
    assert await migrator.query(binding.entity_id, base, base + timedelta(hours=1), "hour") == rows
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert manager._stopping


async def test_history_options_preserve_room_identity_and_reject_stale_form(hass):
    from types import SimpleNamespace
    from custom_components.thermal_efficiency.history import RoomHistoryManager

    config = _config()
    entry = MockConfigEntry(domain=DOMAIN, data=config, version=2)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    entry.runtime_data = SimpleNamespace(history=manager)
    flow = ThermalEfficiencyOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    menu = await flow.async_step_init()
    assert "history" in menu["menu_options"]
    history = await flow.async_step_history()
    assert "sensor.living_temperature" in history["description_placeholders"]["history"]
    assert (await flow.async_step_resolve())["reason"] == "no_pending_moves"
    await flow.async_step_settings(config)
    await flow.async_step_room({"name": "Renamed living room", "temperature": "sensor.replacement"})
    assert "living_room" in flow._rooms
    assert flow._rooms["living_room"]["name"] == "Renamed living room"
    manager.data["revision"] += 1
    assert flow._async_finish()["reason"] == "assignments_changed"


async def test_replacement_options_preview_before_write(recorder_mock, hass):
    from types import SimpleNamespace
    from custom_components.thermal_efficiency.history import RoomHistoryManager
    config = _config()
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    entry.runtime_data = SimpleNamespace(history=manager)
    flow = ThermalEfficiencyOptionsFlow()
    flow.hass, flow.handler = hass, entry.entry_id
    await flow.async_step_replace()
    revision = manager.data["revision"]
    hass.states.async_set("sensor.new", "19", {"unit_of_measurement": "°C"})
    preview = await flow.async_step_replace({"room": "living_room", "role": "temperature", "source": "sensor.new"})
    assert preview["step_id"] == "confirm"
    assert "sensor.new" in preview["description_placeholders"]["change"]
    assert manager.data["revision"] == revision
    assert (await flow.async_step_confirm({}))["type"] is FlowResultType.CREATE_ENTRY
    assert manager.current_rooms()["living_room"]["temperature"] == "sensor.new"
    await manager.async_shutdown()


async def test_replacement_rejects_owned_entities(hass):
    from types import SimpleNamespace
    from custom_components.thermal_efficiency.history import RoomHistoryManager
    config = _config()
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    entry.runtime_data = SimpleNamespace(history=manager)

    binding = manager.bindings()[0]
    # Set valid state on the owned entity to prove the rejection is due to ownership, not observation
    hass.states.async_set(binding.entity_id, "20", {"unit_of_measurement": "°C"})

    flow = ThermalEfficiencyOptionsFlow()
    flow.hass, flow.handler = hass, entry.entry_id
    form = await flow.async_step_replace({"room": "living_room", "role": "temperature", "source": binding.entity_id})
    assert form["type"] is FlowResultType.FORM
    assert form["errors"]["base"] == "invalid_source"

    with pytest.raises(ValueError, match="invalid_source"):
        await manager.async_replace("living_room", "temperature", binding.entity_id, manager.data["revision"])

    await manager.async_shutdown()


async def test_history_status_sensor_live_updates(hass):
    from unittest.mock import MagicMock
    from custom_components.thermal_efficiency.history import RoomHistoryManager
    from custom_components.thermal_efficiency.room_sensor import HistoryStatusSensor
    config = _config()
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    coordinator = MagicMock()
    sensor = HistoryStatusSensor(coordinator, manager)
    sensor.hass = hass
    sensor.entity_id = "sensor.history_status"

    await sensor.async_added_to_hass()
    assert sensor in manager._status_sensors

    manager.data["migration"]["status"] = "copying"
    manager._publish_migration_status()
    assert sensor.native_value == "copying"

    manager.data["migration"]["status"] = "complete"
    manager._publish_migration_status()
    assert sensor.native_value == "complete"

    await sensor.async_will_remove_from_hass()
    assert sensor not in manager._status_sensors
    await manager.async_shutdown()


def test_room_source_sensor_has_force_update():
    from unittest.mock import MagicMock
    from custom_components.thermal_efficiency.room_sensor import RoomSourceSensor

    binding = MagicMock(id="s123456", entity_id="sensor.s", role="temperature", room_id="r1")
    manager = MagicMock(
        data={"streams": {"s123456": {"unique_id": "u1"}}, "rooms": {"r1": {"name": "Room 1"}}},
        entry=MagicMock(entry_id="e1"),
    )
    sensor = RoomSourceSensor(manager, binding)
    assert sensor.force_update is True


async def test_config_and_options_flows_reject_owned_entities(recorder_mock, hass):
    from types import SimpleNamespace
    from homeassistant import config_entries
    from homeassistant.helpers import entity_registry as er
    from custom_components.thermal_efficiency.history import RoomHistoryManager

    # Register an owned entity with platform == DOMAIN
    ent_reg = er.async_get(hass)
    owned = ent_reg.async_get_or_create("sensor", DOMAIN, "owned_sensor_unique", suggested_object_id="owned_sensor")

    # 1. Config flow room details
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"outdoor": "sensor.outdoor"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Room 1", "temperature": owned.entity_id, "add_another": False},
    )
    assert result["type"] == "form"
    assert result["errors"]["temperature"] == "invalid_source"

    # 2. Options flow room editing
    config = _config()
    entry = MockConfigEntry(domain=DOMAIN, data=config, version=2)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    entry.runtime_data = SimpleNamespace(history=manager)

    flow = ThermalEfficiencyOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    await flow.async_step_init()
    await flow.async_step_settings(config)
    res = await flow.async_step_room({"name": "Living Room", "temperature": owned.entity_id})
    assert res["type"] == "form"
    assert res["errors"]["temperature"] == "invalid_source"
    await manager.async_shutdown()


async def test_options_flow_rejects_duplicate_display_name_when_renaming(hass):
    from types import SimpleNamespace
    from custom_components.thermal_efficiency.history import RoomHistoryManager

    # Two rooms: room_1 (Living Room) and room_2 (Bedroom)
    config = {
        "outdoor": "sensor.outdoor",
        "rooms": {
            "room_1": {"name": "Living Room", "temperature": "sensor.living"},
            "room_2": {"name": "Bedroom", "temperature": "sensor.bed"},
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, version=2)
    entry.add_to_hass(hass)
    manager = RoomHistoryManager(hass, entry, config)
    await manager.async_initialize()
    entry.runtime_data = SimpleNamespace(history=manager)

    flow = ThermalEfficiencyOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    await flow.async_step_init()
    await flow.async_step_settings(config)

    # Renaming room_1 to "Bedroom" (which collides with room_2) must fail
    res = await flow.async_step_room({"name": "Bedroom", "temperature": "sensor.living"})
    assert res["type"] == "form"
    assert res["errors"]["name"] == "duplicate_room"

    # Keeping the same name "Living Room" succeeds
    res_ok = await flow.async_step_room({"name": "Living Room", "temperature": "sensor.living"})
    assert res_ok["type"] == "form"
    assert res_ok["step_id"] == "room"  # advances to next room (room_2)
    await manager.async_shutdown()


async def test_diagnostics_verified_hourly_chunks_counts_only_hour_kind(hass):
    from types import SimpleNamespace
    from custom_components.thermal_efficiency.diagnostics import async_get_config_entry_diagnostics

    entry = MockConfigEntry(domain=DOMAIN, data=_config())
    history_data = {
        "migration": {
            "sources": {
                "sensor.a": {
                    "statistic_id": "sensor.a",
                    "chunks": {
                        "raw_1": {"kind": "raw", "verified": True, "count": 10},
                        "5m_1": {"kind": "5minute", "verified": True, "count": 12},
                        "hour_1": {"kind": "hour", "verified": True, "count": 24},
                        "hour_2": {"kind": "hour", "verified": False, "count": 24},
                    }
                }
            }
        }
    }
    entry.runtime_data = SimpleNamespace(history=SimpleNamespace(data=history_data))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    # Even though raw_1 and 5m_1 are verified, only hour_1 is verified of kind 'hour'
    assert diag["verified_hourly_chunks"] == 1



