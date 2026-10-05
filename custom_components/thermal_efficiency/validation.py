"""Validation shared by setup and runtime source checks."""

from __future__ import annotations

import functools
import logging
from math import isfinite
from typing import Any

import voluptuous as vol

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from homeassistant.util.unit_conversion import (
    EnergyConverter,
    TemperatureConverter,
    VolumeConverter,
)

_LOGGER = logging.getLogger(__name__)

CO2_UNITS = {"ppm"}
PERCENT_UNITS = {"%"}
TARIFF_UNITS = {
    "gbp/kwh", "£/kwh", "p/kwh", "pence/kwh", "gbp/mwh", "£/mwh"
}


def is_valid_temperature_unit(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    try:
        TemperatureConverter.convert(0.0, unit, "°C")
    except HomeAssistantError:
        return False
    return True


def is_valid_energy_unit(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    try:
        EnergyConverter.convert(0.0, unit, "kWh")
    except HomeAssistantError:
        return False
    return True


def is_valid_volume_unit(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    try:
        VolumeConverter.convert(0.0, unit, "L")
    except HomeAssistantError:
        return False
    return True


def is_valid_co2_unit(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    return unit.strip().casefold() in CO2_UNITS


def is_valid_percent_unit(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    return unit.strip() in PERCENT_UNITS


def is_tariff_unit(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    norm = unit.strip().casefold().replace(" ", "")
    return norm in TARIFF_UNITS


def _metadata_has_mean(metadata: dict[str, Any]) -> bool | None:
    """Read recorder mean support across HA's has_mean/mean_type schemas."""
    if "mean_type" in metadata:
        mean_type = metadata["mean_type"]
        value = getattr(mean_type, "value", mean_type)
        name = getattr(mean_type, "name", None)
        return (
            str(name).casefold() == "arithmetic"
            if name is not None
            else str(value).casefold() == "arithmetic" or type(value) is int and value == 1
        )
    if "has_mean" in metadata:
        return bool(metadata["has_mean"])
    return None


def heating_power_issue(
    hass: HomeAssistant, entity_id: str, *, allow_missing: bool = False
) -> str | None:
    """Return why a heating-demand source is unsafe, or None when valid."""
    state = hass.states.get(entity_id)
    if state is None:
        return None if allow_missing else "entity is not currently available"
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if not isinstance(unit, str) or not is_valid_percent_unit(unit):
        return "unit must be % (0-100 heating demand)"
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return "state is not numeric"
    if not isfinite(value) or not 0.0 <= value <= 100.0:
        return "state must be between 0 and 100%"
    return None


def _loft_since(value: object) -> str:
    """Validate an ISO date string, keeping it a plain string (not a `date`
    object) so it survives being stored as config-entry data, which is
    persisted to storage as JSON."""
    parsed = dt_util.parse_date(str(value))
    if parsed is None:
        raise vol.Invalid("loft_since must be an ISO date (YYYY-MM-DD)")
    return parsed.isoformat()


def validate_source_metadata_and_state(
    hass: HomeAssistant,
    entity_or_stat_id: str,
    role: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Validate source against recorder metadata and state.

    Returns: (error_key, diagnostic_issue_string) or (None, None) when valid/pending.
    """
    state = hass.states.get(entity_or_stat_id)
    state_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
    state_class = state.attributes.get("state_class") if state else None

    # Check recorder metadata if available
    meta_unit = metadata.get("unit_of_measurement") if metadata else None
    has_mean = _metadata_has_mean(metadata) if metadata is not None else None
    has_sum = metadata.get("has_sum") if metadata else None

    is_external = ":" in entity_or_stat_id or (
        metadata is not None and metadata.get("source") not in (None, "recorder")
    )

    if role in ("outdoor", "temperature"):
        if metadata is not None:
            if has_mean is not True:
                return ("missing_mean", f"Temperature source {entity_or_stat_id} does not record arithmetic mean statistics")
            if meta_unit is not None and not is_valid_temperature_unit(meta_unit):
                return ("incompatible_unit", f"Temperature source {entity_or_stat_id} has incompatible unit '{meta_unit}'")
            if meta_unit is None and is_external:
                return ("incompatible_unit", f"External temperature source {entity_or_stat_id} has missing unit of measurement")
        elif is_external:
            return ("incompatible_unit", f"External temperature source {entity_or_stat_id} has no recorder metadata")
        if state_unit is not None and not is_valid_temperature_unit(state_unit):
            return ("incompatible_unit", f"Temperature source {entity_or_stat_id} has incompatible unit '{state_unit}'")
        return (None, None)

    if role == "gas_meter":
        if metadata is not None:
            if has_sum is not True:
                return ("missing_sum", f"Gas meter source {entity_or_stat_id} is measurement-only without sum statistics")
            if meta_unit is not None and not is_valid_energy_unit(meta_unit):
                return ("incompatible_unit", f"Gas meter source {entity_or_stat_id} has incompatible unit '{meta_unit}'; energy convertible to kWh required")
            if meta_unit is None and is_external:
                return ("incompatible_unit", f"External gas meter source {entity_or_stat_id} has missing unit of measurement")
        elif is_external:
            return ("incompatible_unit", f"External gas meter source {entity_or_stat_id} has no recorder metadata")
        if state is not None:
            if metadata is None and state_class not in ("total", "total_increasing"):
                return ("missing_sum", f"Gas meter source {entity_or_stat_id} must report a cumulative total")
            if state_unit is not None and not is_valid_energy_unit(state_unit):
                return ("incompatible_unit", f"Gas meter source {entity_or_stat_id} has incompatible unit '{state_unit}'; energy convertible to kWh required")
        return (None, None)

    if role == "electricity_meter":
        if metadata is not None:
            if has_sum is not True:
                return ("missing_sum", f"Electricity meter source {entity_or_stat_id} is measurement-only without sum statistics")
            if meta_unit is not None and not is_valid_energy_unit(meta_unit):
                return ("incompatible_unit", f"Electricity meter source {entity_or_stat_id} has incompatible unit '{meta_unit}'")
            if meta_unit is None and is_external:
                return ("incompatible_unit", f"External electricity meter source {entity_or_stat_id} has missing unit of measurement")
        elif is_external:
            return ("incompatible_unit", f"External electricity meter source {entity_or_stat_id} has no recorder metadata")
        if state is not None:
            if metadata is None and state_class not in ("total", "total_increasing"):
                return ("missing_sum", f"Electricity meter source {entity_or_stat_id} must report a cumulative total")
            if state_unit is not None and not is_valid_energy_unit(state_unit):
                return ("incompatible_unit", f"Electricity meter source {entity_or_stat_id} has incompatible unit '{state_unit}'")
        return (None, None)

    if role == "water":
        if metadata is not None:
            if meta_unit is not None:
                if is_valid_energy_unit(meta_unit):
                    return ("incompatible_unit", f"Water source {entity_or_stat_id} has incompatible energy unit '{meta_unit}'")
                if not is_valid_volume_unit(meta_unit):
                    return ("incompatible_unit", f"Water source {entity_or_stat_id} has incompatible non-volume unit '{meta_unit}'")
            elif is_external:
                return ("incompatible_unit", f"External water source {entity_or_stat_id} has missing unit of measurement")
            if has_sum is not True:
                return ("missing_sum", f"Water source {entity_or_stat_id} requires cumulative sum statistics")
        if state is not None:
            if state_unit is not None:
                if is_valid_energy_unit(state_unit):
                    return ("incompatible_unit", f"Water source {entity_or_stat_id} has incompatible energy unit '{state_unit}'")
                if not is_valid_volume_unit(state_unit):
                    return ("incompatible_unit", f"Water source {entity_or_stat_id} has incompatible non-volume unit '{state_unit}'")
            if metadata is None and state_class not in ("total", "total_increasing"):
                return ("missing_sum", f"Water source {entity_or_stat_id} requires a cumulative total")
        elif is_external:
            return ("incompatible_unit", f"External water source {entity_or_stat_id} has no recorder metadata")
        return (None, None)

    if role in ("co2", "outdoor_co2_sensor"):
        if metadata is not None:
            if has_mean is not True:
                return ("missing_mean", f"CO2 source {entity_or_stat_id} does not record arithmetic mean statistics")
            if meta_unit is not None and not is_valid_co2_unit(meta_unit):
                return ("incompatible_unit", f"CO2 source {entity_or_stat_id} has incompatible unit '{meta_unit}' (expected ppm)")
            if meta_unit is None and is_external:
                return ("incompatible_unit", f"External CO2 source {entity_or_stat_id} has missing unit of measurement")
        elif is_external:
            return ("incompatible_unit", f"External CO2 source {entity_or_stat_id} has no recorder metadata")
        if state_unit is not None and not is_valid_co2_unit(state_unit):
            return ("incompatible_unit", f"CO2 source {entity_or_stat_id} has incompatible unit '{state_unit}' (expected ppm)")
        return (None, None)

    if role in ("gas_unit_rate", "electricity_unit_rate"):
        if state is not None:
            if state_unit is not None and not is_tariff_unit(state_unit):
                return ("incompatible_unit", f"Tariff {entity_or_stat_id} has unsupported unit '{state_unit}'")
            if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                try:
                    val = float(state.state)
                except (ValueError, TypeError):
                    return ("invalid_tariff", f"Tariff {entity_or_stat_id} state is not numeric")
                if not isfinite(val) or val < 0.0:
                    return ("invalid_tariff", f"Tariff {entity_or_stat_id} rate must be a finite non-negative number")
        return (None, None)

    if role == "heating_power":
        issue = heating_power_issue(hass, entity_or_stat_id, allow_missing=True)
        if issue:
            return ("heating_power_must_be_percent", f"Heating power source {entity_or_stat_id}: {issue}")
        if metadata is not None and has_mean is not True:
            return ("missing_mean", f"Heating power source {entity_or_stat_id} does not record arithmetic mean statistics")
        if metadata is None and is_external:
            return ("incompatible_unit", f"External heating power source {entity_or_stat_id} has no recorder metadata")
        return (None, None)

    if role in ("humidity", "loft_humidity"):
        if metadata is not None:
            if has_mean is not True:
                return ("missing_mean", f"Humidity source {entity_or_stat_id} does not record arithmetic mean statistics")
            if meta_unit is not None and not is_valid_percent_unit(meta_unit):
                return ("incompatible_unit", f"Humidity source {entity_or_stat_id} has incompatible unit '{meta_unit}'")
            if meta_unit is None and is_external:
                return ("incompatible_unit", f"External humidity source {entity_or_stat_id} has missing unit of measurement")
        elif is_external:
            return ("incompatible_unit", f"External humidity source {entity_or_stat_id} has no recorder metadata")
        if state_unit is not None and not is_valid_percent_unit(state_unit):
            return ("incompatible_unit", f"Humidity source {entity_or_stat_id} has incompatible unit '{state_unit}'")
        return (None, None)

    return (None, None)


async def async_fetch_recorder_metadata(
    hass: HomeAssistant, statistic_ids: set[str]
) -> dict[str, Any] | None:
    """Fetch recorder metadata off the event loop via typed recorder API.

    Returns:
        dict mapping statistic_id to metadata dict if query succeeded.
        None if recorder is unavailable or the query failed (distinguishes DB failure from absent metadata).
    """
    if not statistic_ids:
        return {}
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import get_metadata

        instance = get_instance(hass)
        if instance is None:
            return None
        fetcher = functools.partial(get_metadata, hass, statistic_ids=statistic_ids)
        result = await instance.async_add_executor_job(fetcher)
        if not isinstance(result, dict):
            return None
        return {
            stat_id: meta_tuple[1] if isinstance(meta_tuple, tuple) and len(meta_tuple) > 1 else meta_tuple
            for stat_id, meta_tuple in result.items()
        }
    except Exception as err:
        _LOGGER.warning("Could not fetch recorder metadata: %s", err)
        return None


def validate_global_sources(
    hass: HomeAssistant,
    user_input: dict[str, Any],
    metadata_map: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Preflight validate all global sources in user_input.

    Returns dict mapping field_name -> error_key.
    """
    errors: dict[str, str] = {}
    bounds = {
        "floor_area_m2": (1.0, 2000.0),
        "ceiling_height_m": (1.8, 10.0),
        "boiler_efficiency": (0.5, 1.0),
        "outdoor_co2_ppm": (350.0, 550.0),
        "min_dhw_water_litres": (0.0, 2000.0),
        "max_window_days": (30, 730),
    }
    for field, (minimum, maximum) in bounds.items():
        if field not in user_input:
            continue
        try:
            value = float(user_input[field])
            if not isfinite(value) or not minimum <= value <= maximum:
                errors[field] = "invalid_value"
            elif field == "max_window_days" and value != int(value):
                errors[field] = "invalid_value"
        except (TypeError, ValueError, OverflowError):
            errors[field] = "invalid_value"
    if metadata_map is None:
        errors["base"] = "recorder_unavailable"
        return errors

    metadata = metadata_map

    role_field_map = [
        ("outdoor", "outdoor"),
        ("gas_meter", "gas_meter"),
        ("water", "water"),
        ("electricity_meter", "electricity_meter"),
        ("outdoor_co2_sensor", "outdoor_co2_sensor"),
        ("gas_unit_rate", "gas_unit_rate"),
        ("electricity_unit_rate", "electricity_unit_rate"),
    ]

    for role, field in role_field_map:
        source_id = user_input.get(field)
        if not source_id or not isinstance(source_id, str):
            continue
        err_key, _ = validate_source_metadata_and_state(
            hass, source_id, role, metadata.get(source_id)
        )
        if err_key:
            errors[field] = err_key

    # CO2 can be a string or list of strings
    co2 = user_input.get("co2")
    if co2:
        co2_ids = co2 if isinstance(co2, list) else [co2]
        for s_id in co2_ids:
            if isinstance(s_id, str) and s_id:
                err_key, _ = validate_source_metadata_and_state(
                    hass, s_id, "co2", metadata.get(s_id)
                )
                if err_key:
                    errors["co2"] = err_key
                    break

    return errors
