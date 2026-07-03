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
    CONF_FLOOR_AREA,
    CONF_GAS_METER,
    CONF_HEATING_POWER,
    CONF_LOFT,
    CONF_LOFT_HUMIDITY,
    CONF_LOFT_SINCE,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
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

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_OUTDOOR): cv.entity_id,
                vol.Required(CONF_ROOMS): {cv.slug: ROOM_SCHEMA},
                vol.Optional(CONF_GAS_METER): cv.entity_id,
                vol.Optional(CONF_LOFT): cv.entity_id,
                # Loft sensor history before this date is ignored - protects
                # against a sensor that was relocated into the loft (its
                # earlier readings are from wherever it used to live).
                vol.Optional(CONF_LOFT_SINCE): _loft_since,
                vol.Optional(CONF_LOFT_HUMIDITY): cv.entity_id,
                vol.Optional(CONF_FLOOR_AREA): vol.Coerce(float),
                vol.Optional(
                    CONF_MAX_WINDOW_DAYS, default=DEFAULT_MAX_WINDOW_DAYS
                ): cv.positive_int,
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
