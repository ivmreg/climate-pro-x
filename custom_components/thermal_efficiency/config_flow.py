"""UI config flow: one form for whole-home sensors, then a repeating form to
add rooms one at a time. The options flow re-runs the same two steps,
pre-filled from the current entry so existing rooms can be reviewed/edited
rather than re-typed from scratch."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

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


def _entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _suggest(value: Any) -> dict:
    return {"suggested_value": value} if value is not None else {}


def _global_schema(defaults: dict | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_OUTDOOR, description=_suggest(defaults.get(CONF_OUTDOOR))
            ): _entity_selector(),
            vol.Optional(
                CONF_GAS_METER, description=_suggest(defaults.get(CONF_GAS_METER))
            ): _entity_selector(),
            vol.Optional(
                CONF_LOFT, description=_suggest(defaults.get(CONF_LOFT))
            ): _entity_selector(),
            vol.Optional(
                CONF_LOFT_SINCE, description=_suggest(defaults.get(CONF_LOFT_SINCE))
            ): selector.DateSelector(),
            vol.Optional(
                CONF_LOFT_HUMIDITY,
                description=_suggest(defaults.get(CONF_LOFT_HUMIDITY)),
            ): _entity_selector(),
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
    return data


def _room_schema(
    name: str | None, room: dict | None, ask_add_another: bool
) -> vol.Schema:
    room = room or {}
    schema: dict = {
        vol.Required("name", description=_suggest(name)): selector.TextSelector(),
        vol.Required(
            CONF_TEMPERATURE, description=_suggest(room.get(CONF_TEMPERATURE))
        ): _entity_selector(),
        vol.Optional(
            CONF_HEATING_POWER, description=_suggest(room.get(CONF_HEATING_POWER))
        ): _entity_selector(),
    }
    if ask_add_another:
        schema[vol.Required("add_another", default=not room)] = (
            selector.BooleanSelector()
        )
    return vol.Schema(schema)


def _room_from_input(user_input: dict) -> dict:
    room = {CONF_TEMPERATURE: user_input[CONF_TEMPERATURE]}
    if user_input.get(CONF_HEATING_POWER):
        room[CONF_HEATING_POWER] = user_input[CONF_HEATING_POWER]
    return room


class ThermalEfficiencyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Home settings, then rooms one at a time."""

    VERSION = 1

    def __init__(self) -> None:
        self._global: dict = {}
        self._rooms: dict[str, dict] = {}

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
        errors: dict[str, str] = {}
        if user_input is not None:
            slug = slugify(user_input["name"])
            if not slug:
                errors["name"] = "invalid_name"
            elif slug in self._rooms:
                errors["name"] = "duplicate_room"
            else:
                self._rooms[slug] = _room_from_input(user_input)
                if user_input.get("add_another"):
                    return await self.async_step_room()
                return self.async_create_entry(
                    title="Thermal Efficiency",
                    data={**self._global, CONF_ROOMS: self._rooms},
                )
        return self.async_show_form(
            step_id="room",
            data_schema=_room_schema(None, None, ask_add_another=True),
            errors=errors,
            description_placeholders={
                "rooms_so_far": ", ".join(self._rooms) or "none yet"
            },
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
    """Re-runs the same two steps. Existing rooms are replayed first (so they
    can be reviewed/edited one at a time) before offering to add new ones."""

    def __init__(self) -> None:
        self._global: dict = {}
        self._rooms: dict[str, dict] = {}
        self._pending_rooms: list[tuple[str, dict]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._global = _normalize_global(user_input)
            self._pending_rooms = list(
                self.config_entry.data.get(CONF_ROOMS, {}).items()
            )
            return await self.async_step_room()
        return self.async_show_form(
            step_id="init", data_schema=_global_schema(self.config_entry.data)
        )

    async def async_step_room(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            slug = slugify(user_input["name"])
            if not slug:
                errors["name"] = "invalid_name"
            elif slug in self._rooms:
                errors["name"] = "duplicate_room"
            else:
                self._rooms[slug] = _room_from_input(user_input)
                # Still replaying pre-existing rooms: keep going regardless
                # of "add_another" (it isn't even shown until they're done).
                if self._pending_rooms or user_input.get("add_another"):
                    return await self.async_step_room()
                return self._async_finish()
        name, room = (None, None)
        if self._pending_rooms:
            name, room = self._pending_rooms.pop(0)
        return self.async_show_form(
            step_id="room",
            data_schema=_room_schema(
                name, room, ask_add_another=not self._pending_rooms
            ),
            errors=errors,
        )

    def _async_finish(self) -> config_entries.ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self._global, CONF_ROOMS: self._rooms}
        )
        return self.async_create_entry(title="", data={})
