from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import thermal_math
from .assignments import analysis_configuration
from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_CEILING_HEIGHT,
    CONF_CO2,
    CONF_ELECTRICITY_METER,
    CONF_ELECTRICITY_UNIT_RATE,
    CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION,
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
    CONF_TEMPERATURE,
    CONF_WATER,
    DOMAIN,
    EXPANDING_WINDOWS_DAYS,
    HLC_STATISTICS_LOOKBACK_MULTIPLIER,
    UPDATE_INTERVAL_HOURS,
)
from .validation import (
    async_fetch_recorder_metadata,
    heating_power_issue,
    validate_source_metadata_and_state,
)
from .history import RoomHistoryManager

_LOGGER = logging.getLogger(__name__)


class ThermalCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self,
        hass: HomeAssistant,
        conf: dict,
        history: RoomHistoryManager | None = None,
        entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry or (history.entry if history else None),
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self.conf = conf
        self.history = history

    def _statistic_ids(self) -> set[str]:
        base_conf = (
            analysis_configuration(self.conf) if not self.history else self.conf
        )
        ids = {base_conf[CONF_OUTDOOR]}
        if base_conf.get(CONF_GAS_METER):
            ids.add(base_conf[CONF_GAS_METER])
        if base_conf.get(CONF_LOFT):
            ids.add(base_conf[CONF_LOFT])
        if base_conf.get(CONF_LOFT_HUMIDITY):
            ids.add(base_conf[CONF_LOFT_HUMIDITY])
        co2 = base_conf.get(CONF_CO2)
        if isinstance(co2, str):
            ids.add(co2)
        elif co2:
            ids.update(co2)
        if base_conf.get(CONF_OUTDOOR_CO2_SENSOR):
            ids.add(base_conf[CONF_OUTDOOR_CO2_SENSOR])
        if base_conf.get(CONF_WATER):
            ids.add(base_conf[CONF_WATER])
        if base_conf.get(CONF_ELECTRICITY_METER):
            ids.add(base_conf[CONF_ELECTRICITY_METER])
        if self.history:
            ids.update(self.history.statistic_ids())
        else:
            for room in base_conf[CONF_ROOMS].values():
                ids.add(room[CONF_TEMPERATURE])
                if room.get(CONF_HEATING_POWER):
                    ids.add(room[CONF_HEATING_POWER])
        return ids

    def _unit_rate(self, conf_key: str, fuel: str) -> float | None:
        """Return the latest tariff normalized to GBP/kWh.

        Tariff integrations commonly expose either pounds or pence per kWh.
        Silently treating the latter as pounds creates a 100x cost error, so an
        unknown or missing unit is rejected instead of guessed.
        """
        entity_id = self.conf.get(conf_key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        from math import isfinite
        if not isfinite(value) or value < 0.0:
            return None

        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if not isinstance(unit, str):
            _LOGGER.warning(
                "%s tariff %s has no unit; expected GBP/kWh or p/kWh",
                fuel,
                entity_id,
            )
            return None

        normalized = unit.casefold().replace(" ", "")
        if normalized in {"gbp/kwh", "£/kwh"}:
            return value
        if normalized in {"p/kwh", "pence/kwh"}:
            return value / 100.0
        if normalized in {"gbp/mwh", "£/mwh"}:
            return value / 1000.0

        _LOGGER.warning(
            "%s tariff %s uses unsupported unit %s; expected GBP/kWh or p/kWh",
            fuel,
            entity_id,
            unit,
        )
        return None

    async def _async_update_data(self) -> dict:
        max_days = self.conf[CONF_MAX_WINDOW_DAYS]
        windows = (
            tuple(d for d in EXPANDING_WINDOWS_DAYS if d < max_days)
            + (max_days,)
        )
        now = dt_util.utcnow()
        all_statistic_ids = self._statistic_ids()

        metadata = await async_fetch_recorder_metadata(self.hass, all_statistic_ids)
        source_issues: dict[str, str] = {}
        source_health: dict[str, str] = {}
        unsuitable_ids: set[str] = set()
        room_source_issues: dict[str, dict[str, str]] = {}
        invalid_heating_power: dict[str, str] = {}
        source_roles: dict[str, list[tuple[str, str | None]]] = {}

        def register(source_id: str | None, role: str, room_id: str | None = None) -> None:
            if source_id:
                source_roles.setdefault(source_id, []).append((role, room_id))

        register(self.conf.get(CONF_OUTDOOR), "outdoor")
        register(self.conf.get(CONF_GAS_METER), "gas_meter")
        register(self.conf.get(CONF_ELECTRICITY_METER), "electricity_meter")
        register(self.conf.get(CONF_WATER), "water")
        register(self.conf.get(CONF_OUTDOOR_CO2_SENSOR), "outdoor_co2_sensor")
        for source_id in (
            self.conf.get(CONF_CO2)
            if isinstance(self.conf.get(CONF_CO2), list)
            else [self.conf.get(CONF_CO2)]
        ):
            register(source_id, "co2")
        for room_id, room in self.conf.get(CONF_ROOMS, {}).items():
            register(room.get(CONF_TEMPERATURE), "temperature", room_id)
            register(room.get(CONF_HEATING_POWER), "heating_power", room_id)
            register(room.get(CONF_HUMIDITY), "humidity", room_id)
        register(self.conf.get(CONF_LOFT), "loft_temperature")
        register(self.conf.get(CONF_LOFT_HUMIDITY), "loft_humidity")

        global_issue_role = {
            "outdoor": "outdoor",
            "gas_meter": "gas_meter",
            "electricity_meter": "electricity_meter",
            "water": "water",
            "outdoor_co2_sensor": "outdoor_co2_sensor",
            "loft_temperature": "loft",
            "loft_humidity": "loft_humidity",
        }
        for source_id, refs in source_roles.items():
            for role_key, room_id in refs:
                role = "temperature" if role_key == "loft_temperature" else role_key
                status_role = role_key
                if room_id and self.conf.get(CONF_ROOMS, {}).get(room_id, {}).get("room_type") == "loft":
                    status_role = "loft_humidity" if role == "humidity" else "loft_temperature"
                if metadata is None:
                    error_key = "recorder_unavailable"
                    issue = "Recorder metadata could not be read; retry after recorder access is restored"
                else:
                    error_key, issue = validate_source_metadata_and_state(
                        self.hass, source_id, role, metadata.get(source_id)
                    )
                if not issue:
                    health_key = status_role if status_role in global_issue_role else role
                    source_health[health_key] = (
                        "valid" if metadata is not None and metadata.get(source_id) else "pending"
                    )
                    if room_id:
                        source_health[f"room:{room_id}:{role}"] = source_health[health_key]
                    continue
                source_issues[source_id] = issue
                if status_role in global_issue_role:
                    source_issues.setdefault(global_issue_role[status_role], issue)
                    source_health[global_issue_role[status_role]] = "source_problem"
                elif role == "co2":
                    source_issues.setdefault(source_id, issue)
                if room_id:
                    room_source_issues.setdefault(room_id, {})[role] = issue
                    source_health[f"room:{room_id}:{role}"] = "source_problem"
                if role == "heating_power":
                    invalid_heating_power[source_id] = issue
                # Unsupported or unverifiable sources are removed before the
                # recorder converter sees the complete statistics request.
                unsuitable_ids.add(source_id)

        valid_co2 = [
            source_id for source_id in (
                self.conf.get(CONF_CO2)
                if isinstance(self.conf.get(CONF_CO2), list)
                else [self.conf.get(CONF_CO2)]
            ) if source_id and source_id not in unsuitable_ids
        ]
        configured_co2 = self.conf.get(CONF_CO2)
        configured_co2 = configured_co2 if isinstance(configured_co2, list) else [configured_co2]
        if any(configured_co2) and not valid_co2:
            bad_co2 = next((source_issues.get(source_id) for source_id in configured_co2 if source_issues.get(source_id)), None)
            if bad_co2:
                source_issues["co2"] = bad_co2
                source_health["co2"] = "source_problem"

        # Tariffs use live state, not recorder statistics.
        gas_rate_id = self.conf.get(CONF_GAS_UNIT_RATE)
        if gas_rate_id:
            _, issue = validate_source_metadata_and_state(
                self.hass, gas_rate_id, "gas_unit_rate"
            )
            if issue:
                source_issues["gas_unit_rate"] = issue
                source_health["gas_unit_rate"] = "source_problem"
            else:
                source_health["gas_unit_rate"] = "valid"

        elec_rate_id = self.conf.get(CONF_ELECTRICITY_UNIT_RATE)
        if elec_rate_id:
            _, issue = validate_source_metadata_and_state(
                self.hass, elec_rate_id, "electricity_unit_rate"
            )
            if issue:
                source_issues["electricity_unit_rate"] = issue
                source_health["electricity_unit_rate"] = "source_problem"
            else:
                source_health["electricity_unit_rate"] = "valid"

        # Issue Repairs for user-fixable source problems
        entry_id = self.config_entry.entry_id if self.config_entry else "default"
        for role in ("gas_meter", "water", "electricity_meter", "gas_unit_rate", "electricity_unit_rate"):
            issue_id = f"source_issue_{entry_id}_{role}"
            if role in source_issues:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="incompatible_source",
                    translation_placeholders={
                        "source": role.replace("_", " "),
                        "issue": source_issues[role],
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

        # Query only sources that passed role-specific validation. This protects
        # the shared recorder converter from one malformed optional source.
        valid_statistic_ids = all_statistic_ids - unsuitable_ids

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            # Keep a complete model window before the latest heating day while
            # that day remains inside the configured lookback.
            now - timedelta(days=max_days * HLC_STATISTICS_LOOKBACK_MULTIPLIER),
            now,
            valid_statistic_ids,
            "hour",
            {
                "energy": UnitOfEnergy.KILO_WATT_HOUR,
                "temperature": UnitOfTemperature.CELSIUS,
                "volume": UnitOfVolume.LITERS,
            },
            {"mean", "sum"},
        )
        # compute_all is pure CPU and takes ~1s on a full season of hourly
        # statistics, so it must not run on the event loop. Tariffs are read
        computation_stats = stats
        if self.history:
            computation_stats, prepared = await self.history.async_prepare(
                stats, self.conf, dt_util.get_default_time_zone()
            )
            room_conf = prepared[CONF_ROOMS]
        else:
            prepared = analysis_configuration(self.conf)
            room_conf = prepared[CONF_ROOMS]

        # history.prepare composes archived and synthetic aliases after the
        # recorder query. Remove aliases for invalid sources again here so an
        # old stream cannot reintroduce a rejected source into the model.
        for room_id, role_issues in room_source_issues.items():
            spec = room_conf.get(room_id, {})
            for role, issue in role_issues.items():
                alias = spec.get(role)
                if alias:
                    computation_stats.pop(alias, None)
                if role == "heating_power":
                    if alias:
                        invalid_heating_power[alias] = issue
        if "loft" in source_issues:
            computation_stats.pop(prepared.get(CONF_LOFT, self.conf.get(CONF_LOFT)), None)
        if "loft_humidity" in source_issues:
            computation_stats.pop(
                prepared.get(CONF_LOFT_HUMIDITY, self.conf.get(CONF_LOFT_HUMIDITY)),
                None,
            )

        for entity_id in list(invalid_heating_power):
            issue = invalid_heating_power[entity_id]
            _LOGGER.warning("Ignoring invalid heating-power source: %s", issue)
        conf = {
            "rooms": room_conf,
            "excluded_model_days": prepared.get("excluded_model_days", []),
            "outdoor": self.conf[CONF_OUTDOOR],
            "gas_meter": self.conf.get(CONF_GAS_METER) if "gas_meter" not in source_issues else None,
            "loft": prepared.get(CONF_LOFT, self.conf.get(CONF_LOFT)) if "loft" not in source_issues else None,
            "loft_since": (
                prepared.get(CONF_LOFT_SINCE)
                if CONF_LOFT_SINCE in prepared
                else dt_util.parse_date(self.conf[CONF_LOFT_SINCE])
                if self.conf.get(CONF_LOFT_SINCE)
                else None
            ),
            "loft_humidity": prepared.get(
                CONF_LOFT_HUMIDITY, self.conf.get(CONF_LOFT_HUMIDITY)
            ) if "loft_humidity" not in source_issues else None,
            "floor_area_m2": self.conf.get(CONF_FLOOR_AREA),
            "co2": valid_co2 if valid_co2 else None,
            "outdoor_co2_ppm": self.conf.get(CONF_OUTDOOR_CO2),
            "outdoor_co2_sensor": self.conf.get(CONF_OUTDOOR_CO2_SENSOR) if "outdoor_co2_sensor" not in source_issues else None,
            "ceiling_height_m": self.conf.get(CONF_CEILING_HEIGHT),
            "water": self.conf.get(CONF_WATER) if "water" not in source_issues else None,
            "min_dhw_water_litres": self.conf.get(CONF_MIN_DHW_WATER_L),
            "gas_unit_rate": self._unit_rate(CONF_GAS_UNIT_RATE, "Gas") if "gas_unit_rate" not in source_issues else None,
            "boiler_efficiency": self.conf.get(CONF_BOILER_EFFICIENCY),
            "electricity_meter": self.conf.get(CONF_ELECTRICITY_METER) if "electricity_meter" not in source_issues else None,
            "electricity_unit_rate": self._unit_rate(
                CONF_ELECTRICITY_UNIT_RATE, "Electricity"
            ) if "electricity_unit_rate" not in source_issues else None,
            "experimental_whole_home_ventilation": self.conf.get(
                CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION, False
            ),
            "invalid_heating_power_entities": invalid_heating_power,
            "room_source_issues": room_source_issues,
            "source_issues": source_issues,
        }
        result = await self.hass.async_add_executor_job(
            thermal_math.compute_all,
            computation_stats,
            conf,
            dt_util.get_default_time_zone(),
            now,
            windows,
        )

        result["source_issues"] = source_issues
        result["source_health"] = source_health

        return result
