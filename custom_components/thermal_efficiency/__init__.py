"""Thermal Efficiency: HLC, per-room time constants and loft analysis
computed from the recorder's long-term statistics."""

from __future__ import annotations

from dataclasses import dataclass
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_ASSIGNMENT_SINCE,
    CONF_CEILING_HEIGHT,
    CONF_CO2,
    CONF_ELECTRICITY_METER,
    CONF_ELECTRICITY_UNIT_RATE,
    CONF_FLOOR_AREA,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_HEATING_POWER,
    CONF_HUMIDITY,
    CONF_LOFT,
    CONF_LOFT_HUMIDITY,
    CONF_LOFT_SINCE,
    CONF_MAX_WINDOW_DAYS,
    CONF_MIN_DHW_WATER_L,
    CONF_OUTDOOR,
    CONF_OUTDOOR_CO2,
    CONF_OUTDOOR_CO2_SENSOR,
    CONF_ROOMS,
    CONF_ROOM_TYPE,
    CONF_TEMPERATURE,
    CONF_WATER,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_MAX_WINDOW_DAYS,
    DEFAULT_MIN_DHW_WATER_L,
    DOMAIN,
    ROOM_TYPE_CONDITIONED,
    ROOM_TYPE_LOFT,
)
from .coordinator import ThermalCoordinator
from .config_migration import migrate_legacy_loft_config
from .assignments import analysis_configuration
from .history import RoomHistoryManager
from .thermal_math import compute_all
from .validation import _loft_since

PLATFORMS = ["sensor"]


def _validate_loft_exclusivity(conf: dict) -> dict:
    rooms = conf.get(CONF_ROOMS, {})
    has_loft_room = any(
        room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
        for room in rooms.values()
    )
    if has_loft_room and CONF_LOFT in conf:
        raise vol.Invalid(
            "Cannot configure top-level loft and a loft room under rooms simultaneously"
        )
    return conf


def _validate_room_roles(room: dict) -> dict:
    """Reject room measurements that would otherwise be silently ignored."""
    if room[CONF_ROOM_TYPE] == ROOM_TYPE_LOFT:
        if CONF_HEATING_POWER in room:
            raise vol.Invalid("A loft room cannot have heating power")
    elif CONF_HUMIDITY in room or CONF_ASSIGNMENT_SINCE in room:
        raise vol.Invalid("Humidity and assignment_since are loft-only settings")
    return room


ROOM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_TEMPERATURE): cv.entity_id,
            vol.Optional(CONF_HEATING_POWER): cv.entity_id,
            vol.Optional(CONF_HUMIDITY): cv.entity_id,
            vol.Optional(CONF_ROOM_TYPE, default=ROOM_TYPE_CONDITIONED): vol.In(
                (ROOM_TYPE_CONDITIONED, ROOM_TYPE_LOFT)
            ),
            vol.Optional(CONF_ASSIGNMENT_SINCE): _loft_since,
            vol.Optional("name"): cv.string,
        }
    ),
    _validate_room_roles,
)


def _validate_rooms(rooms: dict) -> dict:
    """The model supports one loft alongside one or more conditioned rooms."""
    kinds = [
        room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) for room in rooms.values()
    ]
    if kinds.count(ROOM_TYPE_LOFT) > 1:
        raise vol.Invalid("Only one loft room is supported")
    if ROOM_TYPE_CONDITIONED not in kinds:
        raise vol.Invalid("At least one conditioned room is required")
    return rooms


def _bounded_float(minimum: float, maximum: float):
    """Coerce a numeric configuration value and enforce physical bounds."""
    return vol.All(vol.Coerce(float), vol.Range(min=minimum, max=maximum))

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            vol.Schema(
                {
                    vol.Required(CONF_OUTDOOR): cv.entity_id,
                    vol.Required(CONF_ROOMS): vol.All(
                        {cv.slug: ROOM_SCHEMA}, vol.Length(min=1), _validate_rooms
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
                    # Heating-off days with less metered water than this are
                    # treated as away days for the hot-water baseline.
                    vol.Optional(
                        CONF_MIN_DHW_WATER_L, default=DEFAULT_MIN_DHW_WATER_L
                    ): _bounded_float(0.0, 2000.0),
                    vol.Optional(CONF_GAS_UNIT_RATE): cv.entity_id,
                    vol.Optional(CONF_ELECTRICITY_METER): cv.entity_id,
                    vol.Optional(CONF_ELECTRICITY_UNIT_RATE): cv.entity_id,
                    vol.Optional(
                        CONF_BOILER_EFFICIENCY, default=DEFAULT_BOILER_EFFICIENCY
                    ): _bounded_float(0.5, 1.0),
                    vol.Optional(
                        CONF_MAX_WINDOW_DAYS, default=DEFAULT_MAX_WINDOW_DAYS
                    ): vol.All(cv.positive_int, vol.Range(min=30, max=730)),
                }
            ),
            _validate_loft_exclusivity,
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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move legacy top-level loft inputs into the normal room collection."""
    if entry.version >= 2:
        return True
    data = migrate_legacy_loft_config(hass, dict(entry.data))
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


@dataclass(slots=True)
class ThermalRuntime:
    coordinator: ThermalCoordinator
    history: RoomHistoryManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    history = RoomHistoryManager(hass, entry, dict(entry.data))
    await history.async_initialize()
    coordinator = ThermalCoordinator(hass, dict(entry.data), history)
    # Recorder availability or a slow archive must not delay live capture.
    initial_config = analysis_configuration(dict(entry.data))
    coordinator.async_set_updated_data(compute_all(
        {}, initial_config, dt_util.get_default_time_zone(), dt_util.utcnow(), (365,)
    ))
    entry.runtime_data = ThermalRuntime(coordinator, history)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.history.async_shutdown()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
