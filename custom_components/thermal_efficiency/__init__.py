"""Thermal Efficiency: HLC, per-room time constants and loft analysis
computed from the recorder's long-term statistics."""

from __future__ import annotations

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.helpers import discovery
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_GAS_METER,
    CONF_HEATING_POWER,
    CONF_LOFT,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
    DEFAULT_MAX_WINDOW_DAYS,
    DOMAIN,
)

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
                vol.Optional(
                    CONF_MAX_WINDOW_DAYS, default=DEFAULT_MAX_WINDOW_DAYS
                ): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN)
    if conf is None:
        return True
    hass.data[DOMAIN] = conf
    hass.async_create_task(
        discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )
    return True
