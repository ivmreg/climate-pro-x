"""Discovery chooses compatible meters and maps heating-demand room suffixes."""

from __future__ import annotations

from ha_efficiency.discover import categorise, draft_config


def _state(
    entity_id: str,
    *,
    device_class: str | None = None,
    unit: str | None = None,
    friendly_name: str | None = None,
) -> dict:
    attributes = {}
    if device_class is not None:
        attributes["device_class"] = device_class
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if friendly_name is not None:
        attributes["friendly_name"] = friendly_name
    return {"entity_id": entity_id, "state": "0", "attributes": attributes}


def test_draft_maps_heating_power_suffix_and_selects_gas_kwh():
    states = [
        _state(
            "sensor.grid_energy",
            device_class="energy",
            unit="kWh",
            friendly_name="Electricity import",
        ),
        _state(
            "sensor.gas_volume",
            device_class="gas",
            unit="m³",
            friendly_name="Gas meter volume",
        ),
        _state(
            "sensor.gas_energy",
            device_class="energy",
            unit="kWh",
            friendly_name="Gas meter energy",
        ),
        _state(
            "sensor.lounge_temperature",
            device_class="temperature",
            unit="°C",
        ),
        _state(
            "sensor.lounge_heating_power",
            unit="%",
            friendly_name="Lounge heating demand",
        ),
    ]

    config = draft_config(categorise(states))

    assert config["gas_kwh_entity"] == "sensor.gas_energy"
    assert config["rooms"]["lounge"]["heating_power"] == (
        "sensor.lounge_heating_power"
    )


def test_draft_leaves_gas_blank_without_gas_labelled_kwh_meter():
    states = [
        _state(
            "sensor.grid_energy",
            device_class="energy",
            unit="kWh",
            friendly_name="Electricity import",
        ),
        _state(
            "sensor.gas_volume",
            device_class="gas",
            unit="m³",
            friendly_name="Gas meter volume",
        ),
    ]

    config = draft_config(categorise(states))

    assert config["gas_kwh_entity"] is None
