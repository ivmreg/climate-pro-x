"""Configuration migration helpers."""

from copy import deepcopy

from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.util import slugify

from .const import (
    CONF_ASSIGNMENT_SINCE,
    CONF_HEATING_POWER,
    CONF_HUMIDITY,
    CONF_LOFT,
    CONF_LOFT_HUMIDITY,
    CONF_LOFT_SINCE,
    CONF_ROOMS,
    CONF_ROOM_TYPE,
    CONF_TEMPERATURE,
    ROOM_TYPE_CONDITIONED,
    ROOM_TYPE_LOFT,
)


def migrate_legacy_loft_config(hass, config: dict) -> dict:
    """Return version-2 config with legacy loft inputs represented as a room."""
    data = dict(config)
    rooms = deepcopy(data.get(CONF_ROOMS, {}))
    for room in rooms.values():
        room.setdefault(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED)

    loft_entity = data.pop(CONF_LOFT, None)
    loft_humidity = data.pop(CONF_LOFT_HUMIDITY, None)
    loft_since = data.pop(CONF_LOFT_SINCE, None)
    if not loft_entity:
        data[CONF_ROOMS] = rooms
        return data

    entity = er.async_get(hass).async_get(loft_entity)
    device = (
        dr.async_get(hass).async_get(entity.device_id)
        if entity and entity.device_id else None
    )
    area_id = entity.area_id if entity else None
    area_id = area_id or (device.area_id if device else None)
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    name = area.name if area else "Loft"
    room_id = next(
        (
            candidate
            for candidate, room in rooms.items()
            if room.get(CONF_TEMPERATURE) == loft_entity
        ),
        None,
    )
    if room_id is None:
        base_id = area_id or slugify(name) or "loft"
        room_id = base_id
        suffix = 2
        while room_id in rooms:
            room_id = f"{base_id}_{suffix}"
            suffix += 1
        rooms[room_id] = {
            "name": name,
            CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
            CONF_TEMPERATURE: loft_entity,
        }
    else:
        rooms[room_id].update(
            {
                "name": rooms[room_id].get("name") or name,
                CONF_ROOM_TYPE: ROOM_TYPE_LOFT,
                CONF_TEMPERATURE: loft_entity,
            }
        )
        rooms[room_id].pop(CONF_HEATING_POWER, None)

    if loft_humidity:
        rooms[room_id][CONF_HUMIDITY] = loft_humidity
    if loft_since:
        rooms[room_id][CONF_ASSIGNMENT_SINCE] = str(loft_since)
    data[CONF_ROOMS] = rooms
    return data
