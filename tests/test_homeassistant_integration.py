"""Home Assistant fixture tests for setup boundaries and coordinator inputs."""

from __future__ import annotations

import pytest

homeassistant = pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import UnitOfEnergy
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermal_efficiency.config_flow import ThermalEfficiencyConfigFlow
from custom_components.thermal_efficiency.const import (
    CONF_BOILER_EFFICIENCY,
    CONF_CO2,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_OUTDOOR_CO2_SENSOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
    DEFAULT_MAX_WINDOW_DAYS,
    DOMAIN,
)
from custom_components.thermal_efficiency.coordinator import ThermalCoordinator


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

    assert coordinator._gas_unit_rate() == pytest.approx(0.0525)


async def test_tariff_with_unknown_unit_is_suppressed(hass):
    coordinator = ThermalCoordinator(hass, _config())
    hass.states.async_set(
        "sensor.gas_rate",
        "5.25",
        {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
    )

    assert coordinator._gas_unit_rate() is None


async def test_options_entry_can_hold_legacy_scalar_co2(hass):
    config = _config()
    config[CONF_CO2] = "sensor.bedroom_co2"
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)

    coordinator = ThermalCoordinator(hass, dict(entry.data))
    assert "sensor.bedroom_co2" in coordinator._statistic_ids()
