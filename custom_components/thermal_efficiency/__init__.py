"""Thermal Efficiency: HLC, per-room time constants and loft analysis
computed from the recorder's long-term statistics."""

from __future__ import annotations

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_CEILING_HEIGHT,
    CONF_CO2,
    CONF_FLOOR_AREA,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_HEATING_POWER,
    CONF_LOFT,
    CONF_LOFT_HUMIDITY,
    CONF_LOFT_SINCE,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_OUTDOOR_CO2,
    CONF_OUTDOOR_CO2_SENSOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
    CONF_WATER,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_MAX_WINDOW_DAYS,
    DOMAIN,
)
from .coordinator import ThermalCoordinator

PLATFORMS = ["sensor"]


def _loft_since(value: object) -> str:
    """Validate an ISO date string, keeping it a plain string (not a `date`
    object) so it survives being stored as config-entry data, which is
    persisted to storage as JSON."""
    parsed = dt_util.parse_date(str(value))
    if parsed is None:
        raise vol.Invalid("loft_since must be an ISO date (YYYY-MM-DD)")
    return parsed.isoformat()


ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEMPERATURE): cv.entity_id,
        vol.Optional(CONF_HEATING_POWER): cv.entity_id,
    }
)


def _bounded_float(minimum: float, maximum: float):
    """Coerce a numeric configuration value and enforce physical bounds."""
    return vol.All(vol.Coerce(float), vol.Range(min=minimum, max=maximum))

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_OUTDOOR): cv.entity_id,
                vol.Required(CONF_ROOMS): vol.All(
                    {cv.slug: ROOM_SCHEMA}, vol.Length(min=1)
                ),
                vol.Optional(CONF_GAS_METER): cv.entity_id,
                vol.Optional(CONF_LOFT): cv.entity_id,
                # Loft sensor history before this date is ignored - protects
                # against a sensor that was relocated into the loft (its
                # earlier readings are from wherever it used to live).
                vol.Optional(CONF_LOFT_SINCE): _loft_since,
                vol.Optional(CONF_LOFT_HUMIDITY): cv.entity_id,
                vol.Optional(CONF_FLOOR_AREA): _bounded_float(1.0, 2000.0),
                vol.Optional(CONF_CEILING_HEIGHT): _bounded_float(1.8, 10.0),
                vol.Optional(CONF_CO2): vol.Any(cv.entity_id, [cv.entity_id]),
                vol.Optional(CONF_OUTDOOR_CO2): _bounded_float(350.0, 550.0),
                vol.Optional(CONF_OUTDOOR_CO2_SENSOR): cv.entity_id,
                # A statistic id, not an entity - the water history is an
                # external statistic (e.g. thames_water:thameswater_consumption)
                # rather than a sensor.* entity.
                vol.Optional(CONF_WATER): cv.string,
                vol.Optional(CONF_GAS_UNIT_RATE): cv.entity_id,
                vol.Optional(
                    CONF_BOILER_EFFICIENCY, default=DEFAULT_BOILER_EFFICIENCY
                ): _bounded_float(0.5, 1.0),
                vol.Optional(
                    CONF_MAX_WINDOW_DAYS, default=DEFAULT_MAX_WINDOW_DAYS
                ): vol.All(cv.positive_int, vol.Range(min=30, max=730)),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Hand a `thermal_efficiency:` YAML block to the config-entry import
    flow. All entity creation goes through config entries from here on -
    the UI flow (Settings > Devices & Services) is the normal path, YAML is
    just migrated into one automatically."""
    conf = config.get(DOMAIN)
    if conf is None:
        return True
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=conf
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = ThermalCoordinator(hass, dict(entry.data))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
