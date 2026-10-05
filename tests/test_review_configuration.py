"""Tests for configuration source validation, unit conversion, tariffs, and options."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
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
    CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_OUTDOOR_CO2_SENSOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
    CONF_WATER,
    DEFAULT_MAX_WINDOW_DAYS,
    DOMAIN,
)
from custom_components.thermal_efficiency.coordinator import ThermalCoordinator
from custom_components.thermal_efficiency.validation import (
    validate_global_sources,
    validate_source_metadata_and_state,
)


@pytest.fixture(autouse=True)
def _enable_custom_integrations(recorder_db_url, enable_custom_integrations, recorder_mock):
    """Allow Home Assistant to discover this repository's custom integration."""


def _base_config() -> dict:
    return {
        CONF_OUTDOOR: "sensor.outdoor_temperature",
        CONF_ROOMS: {
            "living_room": {CONF_TEMPERATURE: "sensor.living_temperature"}
        },
    }


async def test_minimal_temperature_only_setup(hass):
    """UI minimal setup with only outdoor and room temperature succeeds."""
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass

    result = await flow.async_step_user({CONF_OUTDOOR: "sensor.outdoor_temperature"})
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
    entry_data = result["data"]
    assert entry_data[CONF_OUTDOOR] == "sensor.outdoor_temperature"
    assert CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION in entry_data
    assert entry_data[CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION] is False


async def test_water_energy_statistic_gets_field_specific_incompatible_unit_error(hass):
    """Water configured with energy-statistic gets field-specific incompatible_unit error."""
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass

    with patch(
        "custom_components.thermal_efficiency.config_flow.async_fetch_recorder_metadata",
        return_value={
            "sensor.water_meter": {
                "unit_of_measurement": "kWh",
                "unit_class": "energy",
                "has_sum": True,
            }
        },
    ):
        result = await flow.async_step_user(
            {
                CONF_OUTDOOR: "sensor.outdoor_temperature",
                CONF_WATER: "sensor.water_meter",
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_WATER: "incompatible_unit"}


async def test_water_volume_statistic_accepted(hass):
    """Water configured with valid cumulative volume statistic is accepted."""
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass

    for vol_unit in ["L", "m³", "gal", "ft³"]:
        with patch(
            "custom_components.thermal_efficiency.config_flow.async_fetch_recorder_metadata",
            return_value={
                f"sensor.water_{vol_unit}": {
                    "unit_of_measurement": vol_unit,
                    "unit_class": "volume",
                    "has_sum": True,
                }
            },
        ):
            result = await flow.async_step_user(
                {
                    CONF_OUTDOOR: "sensor.outdoor_temperature",
                    CONF_WATER: f"sensor.water_{vol_unit}",
                }
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "room"


async def test_gas_meter_measurement_only_rejected(hass):
    """Energy measurement-only source cannot silently become gas meter (missing_sum)."""
    flow = ThermalEfficiencyConfigFlow()
    flow.hass = hass

    with patch(
        "custom_components.thermal_efficiency.config_flow.async_fetch_recorder_metadata",
        return_value={
            "sensor.power_instant": {
                "unit_of_measurement": "kW",
                "has_sum": False,
                "has_mean": True,
            }
        },
    ):
        result = await flow.async_step_user(
            {
                CONF_OUTDOOR: "sensor.outdoor_temperature",
                CONF_GAS_METER: "sensor.power_instant",
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_GAS_METER: "missing_sum"}


async def test_gas_meter_accepts_only_energy_units(hass):
    """Gas meter accepts recorder-convertible energy, never raw gas volume."""
    for unit in ["Wh", "kWh", "MWh"]:
        metadata = {
            "sensor.gas_test": {
                "unit_of_measurement": unit,
                "has_sum": True,
            }
        }
        err_key, issue = validate_source_metadata_and_state(
            hass, "sensor.gas_test", "gas_meter", metadata.get("sensor.gas_test")
        )
        assert err_key is None
        assert issue is None

    for unit in ["m³", "ft³", "m3", "ccf", "L"]:
        metadata = {"sensor.gas_test": {"unit_of_measurement": unit, "has_sum": True}}
        err_key, issue = validate_source_metadata_and_state(
            hass, "sensor.gas_test", "gas_meter", metadata["sensor.gas_test"]
        )
        assert err_key == "incompatible_unit"
        assert issue


async def test_electricity_meter_rejects_non_energy_units(hass):
    """Electricity meter rejects non-energy units (e.g. °C, L, ppm) with incompatible_unit."""
    metadata = {
        "sensor.elec_bad": {
            "unit_of_measurement": "L",
            "has_sum": True,
        }
    }
    err_key, _ = validate_source_metadata_and_state(
        hass, "sensor.elec_bad", "electricity_meter", metadata.get("sensor.elec_bad")
    )
    assert err_key == "incompatible_unit"


async def test_outdoor_temperature_requires_mean_statistics(hass):
    """Outdoor temperature source without mean statistics is rejected with missing_mean."""
    metadata = {
        "sensor.outdoor_no_mean": {
            "unit_of_measurement": "°C",
            "has_mean": False,
        }
    }
    err_key, _ = validate_source_metadata_and_state(
        hass, "sensor.outdoor_no_mean", "outdoor", metadata.get("sensor.outdoor_no_mean")
    )
    assert err_key == "missing_mean"


async def test_convertible_temperature_units_accepted(hass):
    """Only the units HA's temperature converter recognizes are accepted."""
    for temp_unit in ["°C", "°F", "K"]:
        metadata = {
            "sensor.temp": {
                "unit_of_measurement": temp_unit,
                "has_mean": True,
            }
        }
        err_key, _ = validate_source_metadata_and_state(
            hass, "sensor.temp", "outdoor", metadata.get("sensor.temp")
        )
        assert err_key is None

    for temp_unit in ["C", "F", "celsius", "degC"]:
        err_key, _ = validate_source_metadata_and_state(
            hass,
            "sensor.temp",
            "outdoor",
            {"unit_of_measurement": temp_unit, "has_mean": True},
        )
        assert err_key == "incompatible_unit"


async def test_new_sensor_without_history_passes_as_pending(hass):
    """A valid newly created sensor without recorder metadata passes setup instead of failing."""
    # Metadata is None (no stats recorded yet)
    err_key, issue = validate_source_metadata_and_state(
        hass, "sensor.new_sensor", "gas_meter", None
    )
    assert err_key is None
    assert issue is None


async def test_source_state_unavailable_with_valid_metadata_passes(hass):
    """Source state is unavailable but recorder metadata is valid: passes validation as pending/collecting."""
    hass.states.async_set("sensor.outdoor_offline", "unavailable", {"unit_of_measurement": "°C"})
    metadata = {
        "sensor.outdoor_offline": {
            "unit_of_measurement": "°C",
            "has_mean": True,
        }
    }
    err_key, issue = validate_source_metadata_and_state(
        hass, "sensor.outdoor_offline", "outdoor", metadata.get("sensor.outdoor_offline")
    )
    assert err_key is None
    assert issue is None


async def test_tariff_validation_finite_zero_and_rejections(hass):
    """Tariff accepts finite non-negative numbers including 0.0, rejects NaN, inf, negatives."""
    coordinator = ThermalCoordinator(hass, _base_config() | {CONF_GAS_UNIT_RATE: "sensor.gas_rate"})

    # Valid non-zero rate in p/kWh
    hass.states.async_set("sensor.gas_rate", "7.5", {"unit_of_measurement": "p/kWh"})
    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") == pytest.approx(0.075)

    # Valid zero tariff cost (e.g. free energy / solar export / zero rate)
    hass.states.async_set("sensor.gas_rate", "0.0", {"unit_of_measurement": "p/kWh"})
    zero_val = coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas")
    assert zero_val == 0.0
    assert zero_val is not None

    # Valid rate in GBP/kWh
    hass.states.async_set("sensor.gas_rate", "0.28", {"unit_of_measurement": "GBP/kWh"})
    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") == pytest.approx(0.28)

    # Reject negative
    hass.states.async_set("sensor.gas_rate", "-5.0", {"unit_of_measurement": "p/kWh"})
    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") is None

    # Reject NaN
    hass.states.async_set("sensor.gas_rate", "nan", {"unit_of_measurement": "p/kWh"})
    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") is None

    # Reject Inf
    hass.states.async_set("sensor.gas_rate", "inf", {"unit_of_measurement": "p/kWh"})
    assert coordinator._unit_rate(CONF_GAS_UNIT_RATE, "Gas") is None

    # Preflight validation in validation.py
    hass.states.async_set("sensor.gas_rate", "0.0", {"unit_of_measurement": "p/kWh"})
    err, _ = validate_source_metadata_and_state(hass, "sensor.gas_rate", "gas_unit_rate", None)
    assert err is None

    hass.states.async_set("sensor.gas_rate", "-1.0", {"unit_of_measurement": "p/kWh"})
    err, _ = validate_source_metadata_and_state(hass, "sensor.gas_rate", "gas_unit_rate", None)
    assert err == "invalid_tariff"


async def test_options_flow_settings_preflight_and_experimental_toggle(hass):
    """Options flow settings step validates updated sources and experimental toggle."""
    entry = MockConfigEntry(domain=DOMAIN, data=_base_config(), version=2)
    entry.add_to_hass(hass)

    flow = ThermalEfficiencyOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id

    # Initialize options flow
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"

    # Step into settings with invalid water sensor
    with patch(
        "custom_components.thermal_efficiency.config_flow.async_fetch_recorder_metadata",
        return_value={
            "sensor.incompatible_water": {
                "unit_of_measurement": "kWh",
                "unit_class": "energy",
            }
        },
    ):
        result = await flow.async_step_settings(
            {
                CONF_OUTDOOR: "sensor.outdoor_temperature",
                CONF_WATER: "sensor.incompatible_water",
                CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_WATER: "incompatible_unit"}

    # Now step into settings with valid inputs and experimental toggle enabled
    with patch(
        "custom_components.thermal_efficiency.config_flow.async_fetch_recorder_metadata",
        return_value={},
    ):
        result = await flow.async_step_settings(
            {
                CONF_OUTDOOR: "sensor.outdoor_temperature",
                CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "room"
        assert flow._global[CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION] is True


async def test_yaml_config_schema_experimental_ventilation(hass):
    """YAML configuration schema supports experimental_whole_home_ventilation with default False."""
    from custom_components.thermal_efficiency import CONFIG_SCHEMA

    raw_config = {
        DOMAIN: {
            CONF_OUTDOOR: "sensor.outdoor_temperature",
            CONF_ROOMS: {
                "living_room": {CONF_TEMPERATURE: "sensor.living_temperature"}
            },
        }
    }
    validated = CONFIG_SCHEMA(raw_config)
    assert validated[DOMAIN][CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION] is False

    raw_config_opted_in = {
        DOMAIN: {
            CONF_OUTDOOR: "sensor.outdoor_temperature",
            CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION: True,
            CONF_ROOMS: {
                "living_room": {CONF_TEMPERATURE: "sensor.living_temperature"}
            },
        }
    }
    validated_opted_in = CONFIG_SCHEMA(raw_config_opted_in)
    assert validated_opted_in[DOMAIN][CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION] is True
