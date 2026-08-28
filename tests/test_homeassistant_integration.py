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
def _enable_custom_integrations(enable_custom_integrations):
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
