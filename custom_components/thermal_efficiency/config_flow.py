"""UI config flow: one form for whole-home sensors, then rooms added one at a
time. Adding a room can piggyback on an existing Versatile Thermostat climate
entity (room name from its Area, temperature from its EMA sensor - both live
on the same device) instead of hand-picking every entity. The options flow
re-runs the same steps, replaying existing rooms first (so they can be
reviewed/edited) before offering to add new ones."""

from __future__ import annotations

from typing import Any
from datetime import UTC, datetime

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import slugify
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ASSIGNMENT_SINCE,
    CONF_BOILER_EFFICIENCY,
    CONF_CEILING_HEIGHT,
    CONF_CO2,
    CONF_ELECTRICITY_METER,
    CONF_ELECTRICITY_UNIT_RATE,
    CONF_FLOOR_AREA,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_HEATING_POWER,
    CONF_HUMIDITY,
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
from .validation import heating_power_issue, _loft_since
from .assignments import room_roles, timestamp
from .config_migration import migrate_legacy_loft_config


def _entity_selector(**kwargs: Any) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", **kwargs))


def _display_time(value):
    return datetime.fromtimestamp(value, UTC).astimezone(dt_util.get_default_time_zone()).isoformat() if value is not None else "Retained legacy history"


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
            # Heating-off days with less metered water than this count as
            # away days and are kept out of the hot-water baseline.
            vol.Optional(
                CONF_MIN_DHW_WATER_L,
                description=_suggest(
                    defaults.get(CONF_MIN_DHW_WATER_L, DEFAULT_MIN_DHW_WATER_L)
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=2000,
                    step=5,
                    unit_of_measurement="L",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_GAS_UNIT_RATE,
                description=_suggest(defaults.get(CONF_GAS_UNIT_RATE)),
            ): _entity_selector(device_class="monetary"),
            vol.Optional(
                CONF_ELECTRICITY_METER,
                description=_suggest(defaults.get(CONF_ELECTRICITY_METER)),
            ): _entity_selector(device_class="energy"),
            vol.Optional(
                CONF_ELECTRICITY_UNIT_RATE,
                description=_suggest(defaults.get(CONF_ELECTRICITY_UNIT_RATE)),
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
    if CONF_MIN_DHW_WATER_L in data:
        data[CONF_MIN_DHW_WATER_L] = float(data[CONF_MIN_DHW_WATER_L])
    return data


def _vtrv_picker_schema(allow_finish: bool = False) -> vol.Schema:
    schema: dict = {
        vol.Required(CONF_ROOM_TYPE, default=ROOM_TYPE_CONDITIONED): (
            selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {"value": ROOM_TYPE_CONDITIONED, "label": "Conditioned room"},
                {"value": ROOM_TYPE_LOFT, "label": "Loft"},
            ]))
        ),
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
            CONF_ROOM_TYPE,
            default=room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED),
        ): selector.SelectSelector(selector.SelectSelectorConfig(options=[
            {"value": ROOM_TYPE_CONDITIONED, "label": "Conditioned room"},
            {"value": ROOM_TYPE_LOFT, "label": "Loft"},
        ])),
        vol.Required(
            CONF_TEMPERATURE, description=_suggest(room.get(CONF_TEMPERATURE))
        ): _entity_selector(device_class="temperature"),
        vol.Optional(
            CONF_HEATING_POWER, description=_suggest(room.get(CONF_HEATING_POWER))
        ): _entity_selector(),
        vol.Optional(
            CONF_HUMIDITY, description=_suggest(room.get(CONF_HUMIDITY))
        ): _entity_selector(device_class="humidity"),
        vol.Optional(
            CONF_ASSIGNMENT_SINCE,
            description=_suggest(room.get(CONF_ASSIGNMENT_SINCE)),
        ): selector.DateSelector(),
    }
    if allow_remove:
        schema[vol.Optional("remove_room", default=False)] = selector.BooleanSelector()
    if ask_add_another:
        schema[vol.Required("add_another", default=default_add_another)] = (
            selector.BooleanSelector()
        )
    return vol.Schema(schema)


def _room_from_input(user_input: dict) -> dict:
    room_type = user_input.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED)
    room = {
        "name": user_input["name"],
        CONF_ROOM_TYPE: room_type,
        CONF_TEMPERATURE: user_input[CONF_TEMPERATURE],
    }
    if room_type == ROOM_TYPE_CONDITIONED and user_input.get(CONF_HEATING_POWER):
        room[CONF_HEATING_POWER] = user_input[CONF_HEATING_POWER]
    if room_type == ROOM_TYPE_LOFT and user_input.get(CONF_HUMIDITY):
        room[CONF_HUMIDITY] = user_input[CONF_HUMIDITY]
    if room_type == ROOM_TYPE_LOFT and user_input.get(CONF_ASSIGNMENT_SINCE):
        room[CONF_ASSIGNMENT_SINCE] = _loft_since(user_input[CONF_ASSIGNMENT_SINCE])
    return room


def _is_owned_entity(hass: HomeAssistant, entity_id: str | None) -> bool:
    if not entity_id or not isinstance(entity_id, str):
        return False
    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry and reg_entry.platform == DOMAIN:
        return True
    if entity_id.startswith(f"sensor.{DOMAIN}_"):
        return True
    if DOMAIN in hass.data:
        for entry_val in hass.data[DOMAIN].values():
            mgr = getattr(entry_val, "history", None)
            if mgr is None and isinstance(entry_val, dict):
                mgr = entry_val.get("history")
            if mgr and hasattr(mgr, "_is_owned_entity") and mgr._is_owned_entity(entity_id):
                return True
    return False


def _other_room_slugs(
    rooms: dict,
    current_id: str | None = None,
    pending_rooms: list | None = None,
) -> set[str]:
    slugs = set()
    for rid, r in rooms.items():
        if current_id is not None and rid == current_id:
            continue
        slugs.add(rid)
        if isinstance(r, dict) and r.get("name"):
            slugs.add(slugify(r["name"]))
    if pending_rooms:
        for item in pending_rooms:
            rid = item[0] if isinstance(item, (tuple, list)) else item
            r = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else {}
            if current_id is not None and rid == current_id:
                continue
            slugs.add(rid)
            if isinstance(r, dict) and r.get("name"):
                slugs.add(slugify(r["name"]))
    return slugs


def _assigned_sources(
    rooms: dict | list,
    exclude_room_id: str | None = None,
    pending_rooms: list | None = None,
) -> set[str]:
    assigned = set()
    items = list(rooms.items()) if isinstance(rooms, dict) else list(rooms)
    if pending_rooms:
        items.extend(pending_rooms)
    for item in items:
        rid = item[0] if isinstance(item, (tuple, list)) else item
        spec = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else {}
        if exclude_room_id is not None and rid == exclude_room_id:
            continue
        if not isinstance(spec, dict):
            continue
        for role in (CONF_TEMPERATURE, CONF_HEATING_POWER, CONF_HUMIDITY):
            val = spec.get(role)
            if isinstance(val, str) and val:
                assigned.add(val)
    return assigned


def _validate_room_name(name: str, taken: set[str] | dict) -> tuple[str | None, dict[str, str]]:
    slug = slugify(name)
    if not slug:
        return None, {"name": "invalid_name"}
    if isinstance(taken, dict):
        taken_slugs = set(taken.keys())
        for r in taken.values():
            if isinstance(r, dict) and r.get("name"):
                taken_slugs.add(slugify(r["name"]))
    else:
        taken_slugs = set(taken)
    if slug in taken_slugs:
        return None, {"name": "duplicate_room"}
    return slug, {}


def _validate_room_input(
    hass: HomeAssistant,
    user_input: dict,
    taken: set[str] | dict,
    other_sources: set[str] | None = None,
) -> tuple[str | None, dict[str, str]]:
    slug, errors = _validate_room_name(user_input["name"], taken)
    temp_sensor = user_input.get(CONF_TEMPERATURE)
    other_sources = other_sources or set()
    if temp_sensor:
        if _is_owned_entity(hass, temp_sensor):
            errors[CONF_TEMPERATURE] = "invalid_source"
        elif temp_sensor in other_sources:
            errors[CONF_TEMPERATURE] = "duplicate_source"
    heating_power = user_input.get(CONF_HEATING_POWER)
    room_type = user_input.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED)
    if room_type not in (ROOM_TYPE_CONDITIONED, ROOM_TYPE_LOFT):
        errors[CONF_ROOM_TYPE] = "invalid_room_type"
    if heating_power:
        if room_type == ROOM_TYPE_LOFT:
            errors[CONF_HEATING_POWER] = "role_not_supported"
        elif _is_owned_entity(hass, heating_power):
            errors[CONF_HEATING_POWER] = "invalid_source"
        elif heating_power in other_sources:
            errors[CONF_HEATING_POWER] = "duplicate_source"
        elif temp_sensor and heating_power == temp_sensor:
            errors[CONF_HEATING_POWER] = "duplicate_source"
        elif heating_power_issue(hass, heating_power):
            errors[CONF_HEATING_POWER] = "heating_power_must_be_percent"
    humidity = user_input.get(CONF_HUMIDITY)
    if humidity:
        if room_type != ROOM_TYPE_LOFT:
            errors[CONF_HUMIDITY] = "role_not_supported"
        elif _is_owned_entity(hass, humidity):
            errors[CONF_HUMIDITY] = "invalid_source"
        elif humidity in other_sources:
            errors[CONF_HUMIDITY] = "duplicate_source"
        elif temp_sensor and humidity == temp_sensor:
            errors[CONF_HUMIDITY] = "duplicate_source"
    assignment_since = user_input.get(CONF_ASSIGNMENT_SINCE)
    if assignment_since:
        if room_type != ROOM_TYPE_LOFT:
            errors[CONF_ASSIGNMENT_SINCE] = "role_not_supported"
        else:
            try:
                _loft_since(assignment_since)
            except vol.Invalid:
                errors[CONF_ASSIGNMENT_SINCE] = "invalid_date"
    return slug, errors


def _has_loft(rooms: dict, current_id: str | None = None) -> bool:
    return any(
        room_id != current_id
        and room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
        for room_id, room in rooms.items()
    )


def _has_conditioned_room(rooms: dict) -> bool:
    return any(
        room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_CONDITIONED
        for room in rooms.values()
    )


class ThermalEfficiencyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Home settings, then rooms one at a time, each optionally piggybacking
    on a Versatile Thermostat climate entity."""

    # Version 2 represents the loft as a typed room. The migration preserves the
    # old top-level values as dated room history before setup continues.
    VERSION = 2

    def __init__(self) -> None:
        self._global: dict = {}
        self._rooms: dict[str, dict] = {}
        self._pending_vtrv: str | None = None
        self._pending_room_type = ROOM_TYPE_CONDITIONED

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
            self._pending_room_type = user_input.get(
                CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED
            )
            return await self.async_step_room_details()
        return self.async_show_form(step_id="room", data_schema=_vtrv_picker_schema())

    async def async_step_room_details(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            slug, errors = _validate_room_input(
                self.hass, user_input, self._rooms, _assigned_sources(self._rooms)
            )
            if (
                user_input.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
                and _has_loft(self._rooms)
            ):
                errors[CONF_ROOM_TYPE] = "duplicate_loft"
            if slug and not errors:
                candidate = {**self._rooms, slug: _room_from_input(user_input)}
                if (
                    not user_input.get("add_another")
                    and not _has_conditioned_room(candidate)
                ):
                    errors["base"] = "conditioned_room_required"
                else:
                    self._rooms = candidate
            if slug and not errors:
                if user_input.get("add_another"):
                    return await self.async_step_room()
                return self.async_create_entry(
                    title="Thermal Efficiency",
                    data={**self._global, CONF_ROOMS: self._rooms},
                )
        name, temperature, heating_power = _derive_from_vtrv(
            self.hass,
            self._pending_vtrv if self._pending_room_type == ROOM_TYPE_CONDITIONED else None,
        )
        return self.async_show_form(
            step_id="room_details",
            data_schema=_room_details_schema(
                name,
                {CONF_ROOM_TYPE: self._pending_room_type,
                 CONF_TEMPERATURE: temperature, CONF_HEATING_POWER: heating_power},
                ask_add_another=True,
            ),
            errors=errors,
        )

    async def async_step_import(
        self, import_config: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Migrate an existing `thermal_efficiency:` YAML block."""
        return self.async_create_entry(
            title="Thermal Efficiency (from YAML)",
            data=migrate_legacy_loft_config(self.hass, import_config),
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
        self._pending_room_type = ROOM_TYPE_CONDITIONED
        self._revision: int | None = None
        self._change = None

    def _history(self):
        runtime = getattr(self.config_entry, "runtime_data", None)
        return getattr(runtime, "history", None)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is None and self._history():
            return self.async_show_menu(step_id="init", menu_options=["settings", "replace", "history", "resolve", "correct_visit"])
        return await self.async_step_settings(user_input)

    async def async_step_settings(self, user_input=None):
        if user_input is not None:
            self._global = _normalize_global(user_input)
            history = self._history()
            self._revision = history.data["revision"] if history else None
            self._pending_rooms = list(
                (history.current_rooms() if history else self.config_entry.data.get(CONF_ROOMS, {})).items()
            )
            return await self._async_advance_room()
        return self.async_show_form(
            step_id="settings", data_schema=_global_schema(self.config_entry.data)
        )

    async def async_step_history(self, user_input=None):
        if user_input is not None:
            return await self.async_step_init()
        history = self._history()
        migration = history.data["migration"]
        lines = [f"History migration: {migration['status']}"]
        for room in history.data["rooms"].values():
            lines.append(f"\n{room['name']}")
            for visit in room["visits"]:
                stream = history.data["streams"].get(visit.get("stream"))
                source = history.data["sources"][stream["source_id"]]["entity_id"] if stream else "No source — gap"
                start = _display_time(visit.get("start"))
                end = _display_time(visit["end"]) if visit.get("end") else "present"
                lines.append(f"{visit['role']}: {source}; {start} → {end}")
        return self.async_show_form(step_id="history", data_schema=vol.Schema({}),
                                    description_placeholders={"history": "\n\n".join(lines)})

    async def async_step_resolve(self, user_input=None):
        history = self._history()
        pending = {sid: s for sid, s in history.data["sources"].items() if s.get("pending") and not s.get("missing")}
        if not pending:
            return self.async_abort(reason="no_pending_moves")
        errors = {}
        if user_input is not None:
            try:
                source = history.data["sources"][user_input["source"]]
                if source["role"] not in room_roles(
                    history.config["rooms"][user_input["room"]]
                ):
                    raise ValueError("role_not_supported")
                self._change = ("async_correct", [user_input["source"], user_input["room"],
                                                 timestamp(user_input["effective_time"])], self._revision)
                return await self.async_step_confirm()
            except ValueError as err:
                if str(err) == "role_not_supported":
                    errors["base"] = "role_not_supported"
                else:
                    errors["base"] = "invalid_correction"
            except KeyError:
                errors["base"] = "invalid_correction"
        self._revision = history.data["revision"]
        return self.async_show_form(step_id="resolve", errors=errors, data_schema=vol.Schema({
            vol.Required("source"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": sid, "label": s["entity_id"]} for sid, s in pending.items()])),
            vol.Required("room"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": rid, "label": history.data["rooms"][rid]["name"]} for rid in history.config["rooms"]])),
            vol.Required("effective_time"): selector.TextSelector(),
        }))

    async def async_step_replace(self, user_input=None):
        history = self._history()
        ent_reg = er.async_get(self.hass)
        owned = {e.entity_id for e in ent_reg.entities.values() if e.platform == DOMAIN}
        if history:
            for s in history.data.get("streams", {}).values():
                if s.get("entity_id"):
                    owned.add(s["entity_id"])
                owned.update(s.get("aliases", []))
        errors = {}
        if user_input is not None:
            other_active = (
                _assigned_sources(
                    history.config["rooms"],
                    exclude_room_id=user_input.get("room"),
                )
                if history
                else set()
            )
            if user_input["source"] in owned:
                errors["base"] = "invalid_source"
            elif user_input["source"] in other_active:
                errors["base"] = "duplicate_source"
            elif (
                user_input["room"] not in history.config["rooms"]
                or user_input["role"]
                not in room_roles(history.config["rooms"][user_input["room"]])
            ):
                errors["base"] = "role_not_supported"
            elif user_input["role"] == CONF_HEATING_POWER and heating_power_issue(
                self.hass, user_input["source"]
            ):
                errors["base"] = "heating_power_must_be_percent"
            else:
                self._change = ("async_replace", [user_input["room"], user_input["role"], user_input["source"]], self._revision)
                return await self.async_step_confirm()
        self._revision = history.data["revision"]
        return self.async_show_form(step_id="replace", errors=errors, data_schema=vol.Schema({
            vol.Required("room"): selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {"value": rid, "label": history.data["rooms"][rid]["name"]} for rid in history.config["rooms"]])),
            vol.Required("role"): selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {"value": "temperature", "label": "Temperature"},
                {"value": "heating_power", "label": "Heating demand (%)"},
                {"value": "humidity", "label": "Humidity"}])),
            vol.Required("source"): _entity_selector(exclude_entities=sorted(owned)),
        }))

    async def async_step_correct_visit(self, user_input=None):
        history = self._history()
        options = [{"value": v["id"], "label": f"{room['name']} / {v['role']} / {_display_time(v.get('start'))} → {_display_time(v['end'])}"}
                   for room in history.data["rooms"].values() for v in room["visits"] if v.get("end") is not None]
        if not options:
            return self.async_abort(reason="no_closed_visits")
        errors = {}
        if user_input is not None:
            try:
                self._change = ("async_edit_visit", [user_input["visit"], timestamp(user_input["start"]),
                    timestamp(user_input["end"]), user_input.get("exclude", False)], self._revision)
                return await self.async_step_confirm()
            except ValueError:
                errors["base"] = "invalid_correction"
        self._revision = history.data["revision"]
        return self.async_show_form(step_id="correct_visit", errors=errors, data_schema=vol.Schema({
            vol.Required("visit"): selector.SelectSelector(selector.SelectSelectorConfig(options=options)),
            vol.Required("start"): selector.TextSelector(), vol.Required("end"): selector.TextSelector(),
            vol.Optional("exclude", default=False): selector.BooleanSelector(),
        }))

    async def async_step_confirm(self, user_input=None):
        method, args, revision = self._change
        if user_input is not None:
            try:
                await getattr(self._history(), method)(*args, revision)
                return self.async_create_entry(title="", data={})
            except (ValueError, KeyError, StopIteration):
                return self.async_abort(reason="change_not_applied")
        # The preview uses the submitted values, never silently refreshed defaults.
        history = self._history()
        if method == "async_correct":
            preview = f"Assign {history.data['sources'][args[0]]['entity_id']} to {history.data['rooms'][args[1]]['name']} from {_display_time(args[2])}. Unobserved hours remain gaps."
        elif method == "async_replace":
            preview = f"Use {args[2]} for {args[1].replace('_', ' ')} in {history.data['rooms'][args[0]]['name']} from confirmation time. The previous assignment ends; its history stays preserved."
        else:
            preview = f"Correct this completed visit to {_display_time(args[1])} → {_display_time(args[2])}. Exclude from analysis: {args[3]}. Recorded observations stay unchanged."
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
            description_placeholders={"change": preview})

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
            other_slugs = _other_room_slugs(
                self._rooms,
                current_id=self._current_room[0],
                pending_rooms=self._pending_rooms,
            )
            other_sources = _assigned_sources(
                self._rooms,
                exclude_room_id=self._current_room[0],
                pending_rooms=self._pending_rooms,
            )
            slug, errors = _validate_room_input(
                self.hass, user_input, other_slugs, other_sources
            )
            other_rooms = dict(self._rooms)
            other_rooms.update(dict(self._pending_rooms))
            if (
                user_input.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
                and _has_loft(other_rooms, self._current_room[0])
            ):
                errors[CONF_ROOM_TYPE] = "duplicate_loft"
            if (
                not errors
                and user_input.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
                and user_input.get(CONF_ASSIGNMENT_SINCE)
                and (history := self._history())
            ):
                from copy import deepcopy
                from .assignments import validate_visits
                from .history import assignment_timestamp
                staged = deepcopy(history.data)
                loft_id = self._current_room[0]
                if loft_id and loft_id in staged.get("rooms", {}):
                    eff = assignment_timestamp(user_input[CONF_ASSIGNMENT_SINCE])
                    for v in staged["rooms"][loft_id]["visits"]:
                        if v["role"] == CONF_TEMPERATURE and v.get("end") is None:
                            v["start"] = eff
                    try:
                        validate_visits(staged)
                    except ValueError:
                        errors[CONF_ASSIGNMENT_SINCE] = "overlapping_visit"
            if slug and not errors:
                # Display names may change; established room identity never does.
                room_id = self._current_room[0] or slug
                updated = {
                    **_room_from_input(user_input),
                    "name": user_input["name"],
                }
                if (
                    updated[CONF_ROOM_TYPE] == ROOM_TYPE_LOFT
                    and CONF_ASSIGNMENT_SINCE not in user_input
                    and self._current_room[1]
                    and self._current_room[1].get(CONF_ASSIGNMENT_SINCE)
                ):
                    updated[CONF_ASSIGNMENT_SINCE] = self._current_room[1][
                        CONF_ASSIGNMENT_SINCE
                    ]
                self._rooms[room_id] = updated
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
                if not self._rooms:
                    return self.async_show_form(
                        step_id="new_room",
                        data_schema=_vtrv_picker_schema(allow_finish=True),
                        errors={"base": "at_least_one_room"},
                    )
                if not _has_conditioned_room(self._rooms):
                    return self.async_show_form(
                        step_id="new_room",
                        data_schema=_vtrv_picker_schema(allow_finish=True),
                        errors={"base": "conditioned_room_required"},
                    )
                return self._async_finish()
            self._pending_vtrv = user_input.get("vtrv_climate")
            self._pending_room_type = user_input.get(
                CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED
            )
            return await self.async_step_new_room_details()
        return self.async_show_form(
            step_id="new_room", data_schema=_vtrv_picker_schema(allow_finish=True)
        )

    async def async_step_new_room_details(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            other_slugs = _other_room_slugs(self._rooms)
            other_sources = _assigned_sources(self._rooms)
            slug, errors = _validate_room_input(
                self.hass, user_input, other_slugs, other_sources
            )
            if (
                user_input.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
                and _has_loft(self._rooms)
            ):
                errors[CONF_ROOM_TYPE] = "duplicate_loft"
            if slug and not errors:
                candidate = {**self._rooms, slug: _room_from_input(user_input)}
                if (
                    not user_input.get("add_another")
                    and not _has_conditioned_room(candidate)
                ):
                    errors["base"] = "conditioned_room_required"
                else:
                    self._rooms = candidate
                    if user_input.get("add_another"):
                        return await self.async_step_new_room()
                    return self._async_finish()
        name, temperature, heating_power = _derive_from_vtrv(
            self.hass,
            self._pending_vtrv if self._pending_room_type == ROOM_TYPE_CONDITIONED else None,
        )
        return self.async_show_form(
            step_id="new_room_details",
            data_schema=_room_details_schema(
                name,
                {CONF_ROOM_TYPE: self._pending_room_type,
                 CONF_TEMPERATURE: temperature, CONF_HEATING_POWER: heating_power},
                ask_add_another=True,
            ),
            errors=errors,
        )

    def _async_finish(self) -> config_entries.ConfigFlowResult:
        history = self._history()
        if history and self._revision != history.data["revision"]:
            return self.async_abort(reason="assignments_changed")
        if not _has_conditioned_room(self._rooms):
            return self.async_abort(reason="conditioned_room_required")
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self._global, CONF_ROOMS: self._rooms}
        )
        return self.async_create_entry(title="", data={})
