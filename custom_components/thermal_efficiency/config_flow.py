"""UI config flow: one form for whole-home sensors, then rooms added one at a
time. Adding a room can piggyback on an existing Versatile Thermostat climate
entity (room name from its Area, temperature from its EMA sensor - both live
on the same device) instead of hand-picking every entity. The options flow
re-runs the same steps, replaying existing rooms first (so they can be
reviewed/edited) before offering to add new ones."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import slugify

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


def _entity_selector(**kwargs: Any) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", **kwargs))


def _suggest(value: Any) -> dict:
    return {"suggested_value": value} if value is not None else {}


def _suggest_list(value: Any) -> dict:
    """Normalize legacy scalar values for a multiple-entity selector."""
    if value is None:
        return {}
    return {"suggested_value": value if isinstance(value, list) else [value]}


def _global_schema(defaults: dict | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_OUTDOOR, description=_suggest(defaults.get(CONF_OUTDOOR))
            ): _entity_selector(device_class="temperature"),
            vol.Optional(
                CONF_GAS_METER, description=_suggest(defaults.get(CONF_GAS_METER))
            ): _entity_selector(device_class="energy"),
            vol.Optional(
                CONF_LOFT, description=_suggest(defaults.get(CONF_LOFT))
            ): _entity_selector(device_class="temperature"),
            vol.Optional(
                CONF_LOFT_SINCE, description=_suggest(defaults.get(CONF_LOFT_SINCE))
            ): selector.DateSelector(),
            vol.Optional(
                CONF_LOFT_HUMIDITY,
                description=_suggest(defaults.get(CONF_LOFT_HUMIDITY)),
            ): _entity_selector(device_class="humidity"),
            vol.Optional(
                CONF_FLOOR_AREA, description=_suggest(defaults.get(CONF_FLOOR_AREA))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    step=0.5,
                    unit_of_measurement="m2",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_CEILING_HEIGHT,
                description=_suggest(defaults.get(CONF_CEILING_HEIGHT)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1.8,
                    max=5,
                    step=0.05,
                    unit_of_measurement="m",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_CO2, description=_suggest_list(defaults.get(CONF_CO2))
            ): _entity_selector(device_class="carbon_dioxide", multiple=True),
            vol.Optional(
                CONF_OUTDOOR_CO2,
                description=_suggest(defaults.get(CONF_OUTDOOR_CO2)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=350,
                    max=550,
                    step=1,
                    unit_of_measurement="ppm",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_OUTDOOR_CO2_SENSOR,
                description=_suggest(defaults.get(CONF_OUTDOOR_CO2_SENSOR)),
            ): _entity_selector(device_class="carbon_dioxide"),
            # Water history lives in an external statistic (e.g. from the
            # Thames Water integration), not a sensor.* entity - a plain
            # entity selector can't reach it.
            vol.Optional(
                CONF_WATER, description=_suggest(defaults.get(CONF_WATER))
            ): selector.StatisticSelector(),
            vol.Optional(
                CONF_GAS_UNIT_RATE,
                description=_suggest(defaults.get(CONF_GAS_UNIT_RATE)),
            ): _entity_selector(device_class="monetary"),
            vol.Optional(
                CONF_BOILER_EFFICIENCY,
                description=_suggest(
                    defaults.get(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY)
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5, max=1.0, step=0.01, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_MAX_WINDOW_DAYS,
                default=defaults.get(CONF_MAX_WINDOW_DAYS, DEFAULT_MAX_WINDOW_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=30, max=730, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _normalize_global(user_input: dict) -> dict:
    data = dict(user_input)
    if CONF_MAX_WINDOW_DAYS in data:
        data[CONF_MAX_WINDOW_DAYS] = int(data[CONF_MAX_WINDOW_DAYS])
    if CONF_FLOOR_AREA in data:
        data[CONF_FLOOR_AREA] = float(data[CONF_FLOOR_AREA])
    if CONF_CEILING_HEIGHT in data:
        data[CONF_CEILING_HEIGHT] = float(data[CONF_CEILING_HEIGHT])
    if CONF_BOILER_EFFICIENCY in data:
        data[CONF_BOILER_EFFICIENCY] = float(data[CONF_BOILER_EFFICIENCY])
    if CONF_OUTDOOR_CO2 in data:
        data[CONF_OUTDOOR_CO2] = float(data[CONF_OUTDOOR_CO2])
    return data


def _vtrv_picker_schema(allow_finish: bool = False) -> vol.Schema:
    schema: dict = {
        vol.Optional("vtrv_climate"): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="climate", integration="versatile_thermostat"
            )
        ),
    }
    if allow_finish:
        schema[vol.Optional("finish", default=False)] = selector.BooleanSelector()
    return vol.Schema(schema)


def _derive_from_vtrv(
    hass: HomeAssistant, climate_entity_id: str | None
) -> tuple[str | None, str | None, str | None]:
    """Suggested (name, temperature, heating_power) from a Versatile
    Thermostat climate entity: the room name comes from its Area, and the
    EMA temperature sensor lives on the same device. A heating-power sensor
    is typically a separate entity (e.g. from the Tado side) rather than on
    the VTRV's own device, so it's only suggested when exactly one candidate
    shares the same area - otherwise it's left for the user to pick."""
    if not climate_entity_id:
        return None, None, None
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(climate_entity_id)
    if entry is None:
        return None, None, None

    device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
    area_id = entry.area_id or (device.area_id if device else None)

    name = None
    if area_id:
        area = ar.async_get(hass).async_get_area(area_id)
        name = area.name if area else None

    temperature = None
    if entry.device_id:
        for sibling in er.async_entries_for_device(ent_reg, entry.device_id):
            if sibling.entity_id.endswith("_ema_temperature"):
                temperature = sibling.entity_id
                break

    heating_power = None
    if area_id:
        candidates = [
            e.entity_id
            for e in er.async_entries_for_area(ent_reg, area_id)
            if e.entity_id.endswith("_heating_power")
        ]
        if len(candidates) == 1:
            heating_power = candidates[0]

    return name, temperature, heating_power


def _room_details_schema(
    name: str | None,
    room: dict | None,
    ask_add_another: bool,
    default_add_another: bool = True,
    allow_remove: bool = False,
) -> vol.Schema:
    room = room or {}
    schema: dict = {
        vol.Required("name", description=_suggest(name)): selector.TextSelector(),
        vol.Required(
            CONF_TEMPERATURE, description=_suggest(room.get(CONF_TEMPERATURE))
        ): _entity_selector(device_class="temperature"),
        vol.Optional(
            CONF_HEATING_POWER, description=_suggest(room.get(CONF_HEATING_POWER))
        ): _entity_selector(),
    }
    if allow_remove:
        schema[vol.Optional("remove_room", default=False)] = selector.BooleanSelector()
    if ask_add_another:
        schema[vol.Required("add_another", default=default_add_another)] = (
            selector.BooleanSelector()
        )
    return vol.Schema(schema)


def _room_from_input(user_input: dict) -> dict:
    room = {CONF_TEMPERATURE: user_input[CONF_TEMPERATURE]}
    if user_input.get(CONF_HEATING_POWER):
        room[CONF_HEATING_POWER] = user_input[CONF_HEATING_POWER]
    return room


def _validate_room_name(name: str, taken: dict) -> tuple[str | None, dict[str, str]]:
    slug = slugify(name)
    if not slug:
        return None, {"name": "invalid_name"}
    if slug in taken:
        return None, {"name": "duplicate_room"}
    return slug, {}


class ThermalEfficiencyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Home settings, then rooms one at a time, each optionally piggybacking
    on a Versatile Thermostat climate entity."""

    VERSION = 1

    def __init__(self) -> None:
        self._global: dict = {}
        self._rooms: dict[str, dict] = {}
        self._pending_vtrv: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._global = _normalize_global(user_input)
            return await self.async_step_room()
        return self.async_show_form(step_id="user", data_schema=_global_schema())

    async def async_step_room(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._pending_vtrv = user_input.get("vtrv_climate")
            return await self.async_step_room_details()
        return self.async_show_form(step_id="room", data_schema=_vtrv_picker_schema())

    async def async_step_room_details(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            slug, errors = _validate_room_name(user_input["name"], self._rooms)
            if slug:
                self._rooms[slug] = _room_from_input(user_input)
                if user_input.get("add_another"):
                    return await self.async_step_room()
                return self.async_create_entry(
                    title="Thermal Efficiency",
                    data={**self._global, CONF_ROOMS: self._rooms},
                )
        name, temperature, heating_power = _derive_from_vtrv(
            self.hass, self._pending_vtrv
        )
        return self.async_show_form(
            step_id="room_details",
            data_schema=_room_details_schema(
                name,
                {CONF_TEMPERATURE: temperature, CONF_HEATING_POWER: heating_power},
                ask_add_another=True,
            ),
            errors=errors,
        )

    async def async_step_import(
        self, import_config: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Migrate an existing `thermal_efficiency:` YAML block."""
        return self.async_create_entry(
            title="Thermal Efficiency (from YAML)", data=import_config
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ThermalEfficiencyOptionsFlow:
        return ThermalEfficiencyOptionsFlow()


class ThermalEfficiencyOptionsFlow(config_entries.OptionsFlow):
    """Existing rooms are replayed first, one at a time, so they can be
    reviewed/edited; once they're all through, new rooms can be added the
    same VTRV-assisted way as the initial setup."""

    def __init__(self) -> None:
        self._global: dict = {}
        self._rooms: dict[str, dict] = {}
        self._pending_rooms: list[tuple[str, dict]] = []
        self._current_room: tuple[str | None, dict | None] = (None, None)
        self._pending_vtrv: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._global = _normalize_global(user_input)
            self._pending_rooms = list(
                self.config_entry.data.get(CONF_ROOMS, {}).items()
            )
            return await self._async_advance_room()
        return self.async_show_form(
            step_id="init", data_schema=_global_schema(self.config_entry.data)
        )

    async def _async_advance_room(self) -> config_entries.ConfigFlowResult:
        if self._pending_rooms:
            self._current_room = self._pending_rooms.pop(0)
            return await self.async_step_room()
        return await self.async_step_new_room()

    async def async_step_room(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Review/edit one pre-existing room, or drop it from the config."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("remove_room"):
                return await self._async_advance_room()
            slug, errors = _validate_room_name(user_input["name"], self._rooms)
            if slug:
                self._rooms[slug] = _room_from_input(user_input)
                return await self._async_advance_room()
        name, room = self._current_room
        return self.async_show_form(
            step_id="room",
            data_schema=_room_details_schema(
                name, room, ask_add_another=False, allow_remove=True
            ),
            errors=errors,
        )

    async def async_step_new_room(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            if user_input.get("finish"):
                return self._async_finish()
            self._pending_vtrv = user_input.get("vtrv_climate")
            return await self.async_step_new_room_details()
        return self.async_show_form(
            step_id="new_room", data_schema=_vtrv_picker_schema(allow_finish=True)
        )

    async def async_step_new_room_details(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            slug, errors = _validate_room_name(user_input["name"], self._rooms)
            if slug:
                self._rooms[slug] = _room_from_input(user_input)
                if user_input.get("add_another"):
                    return await self.async_step_new_room()
                return self._async_finish()
        name, temperature, heating_power = _derive_from_vtrv(
            self.hass, self._pending_vtrv
        )
        return self.async_show_form(
            step_id="new_room_details",
            data_schema=_room_details_schema(
                name,
                {CONF_TEMPERATURE: temperature, CONF_HEATING_POWER: heating_power},
                ask_add_another=True,
            ),
            errors=errors,
        )

    def _async_finish(self) -> config_entries.ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self._global, CONF_ROOMS: self._rooms}
        )
        return self.async_create_entry(title="", data={})
