"""Privacy-conscious diagnostics for room-history migration and model status."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CO2,
    CONF_ELECTRICITY_METER,
    CONF_ELECTRICITY_UNIT_RATE,
    CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION,
    CONF_GAS_METER,
    CONF_GAS_UNIT_RATE,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_ROOM_TYPE,
    CONF_WATER,
    DEFAULT_MAX_WINDOW_DAYS,
    ROOM_TYPE_CONDITIONED,
)


def _anonymize(
    text: str | None, room_names: dict[str, str], source_ids: list[str] | None = None
) -> str | None:
    if not text:
        return text
    res = text
    for source_id in sorted(source_ids or [], key=len, reverse=True):
        res = re.sub(re.escape(source_id), "<source>", res, flags=re.IGNORECASE)
    for name, anon in sorted(room_names.items(), key=lambda item: len(item[0]), reverse=True):
        res = re.sub(
            rf"(?<![\w]){re.escape(name)}(?![\w])", anon, res, flags=re.IGNORECASE
        )
    res = re.sub(r"\b[a-z_]+(?:\.[a-z0-9_-]+)+\b", "<source>", res, flags=re.IGNORECASE)
    res = re.sub(r"(?<![\w])[a-z0-9_.-]+:[a-z0-9_.:-]+", "<source>", res, flags=re.IGNORECASE)
    return res


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    history = runtime.history.data if hasattr(runtime, "history") and runtime.history else {}
    coordinator = getattr(runtime, "coordinator", None)
    entry_data = {**entry.data, **entry.options}
    if coordinator and coordinator.conf:
        entry_data.update(coordinator.conf)
    configured_rooms = getattr(runtime.history, "config", entry_data).get("rooms", {}) if hasattr(runtime, "history") and runtime.history else entry_data.get("rooms", {})
    migration = history.get("migration", {})
    source_issues = coordinator.data.get("source_issues", {}) if coordinator and coordinator.data else {}

    # Map room names to anonymous labels
    room_names: dict[str, str] = {}
    for idx, (rid, rspec) in enumerate(sorted(configured_rooms.items())):
        name = rspec.get("name") or rid
        anon = f"room_{idx+1}"
        room_names[name] = anon
        room_names[rid] = anon

    history_sources = history.get("sources", {})
    source_ids = set()
    for field in (
        CONF_OUTDOOR, CONF_GAS_METER, CONF_WATER, CONF_ELECTRICITY_METER,
        CONF_CO2, CONF_GAS_UNIT_RATE, CONF_ELECTRICITY_UNIT_RATE,
    ):
        value = entry_data.get(field)
        source_ids.update(value if isinstance(value, list) else ([value] if isinstance(value, str) else []))
    for room in entry_data.get("rooms", {}).values():
        source_ids.update(value for value in room.values() if isinstance(value, str) and "." in value)
    for source in history_sources.values():
        if isinstance(source, dict):
            source_ids.update(
                value for value in (source.get("entity_id"), source.get("statistic_id"))
                if isinstance(value, str)
            )
    for source in migration.get("sources", {}).values():
        if isinstance(source, dict) and isinstance(source.get("statistic_id"), str):
            source_ids.add(source["statistic_id"])
    source_ids = sorted(source_ids, key=len, reverse=True)
    source_health = coordinator.data.get("source_health", {}) if coordinator and coordinator.data else {}

    def _determine_health(field_key: str, role_key: str) -> str:
        val = entry_data.get(field_key)
        if not val:
            return "not_configured"
        if role_key in source_issues:
            return "source_problem"
        health = source_health.get(role_key)
        if health in {"valid", "source_problem"}:
            return health
        if isinstance(val, list) and any(source_health.get(role_key) == "valid" for _ in val):
            return "valid"
        return "pending"

    unit_health: dict[str, Any] = {
        "outdoor_temperature": {
            "configured": bool(entry_data.get(CONF_OUTDOOR)),
            "normalized_unit": "°C",
            "health": _determine_health(CONF_OUTDOOR, "outdoor"),
            "issue": _anonymize(source_issues.get("outdoor"), room_names, source_ids),
        },
        "gas_meter": {
            "configured": bool(entry_data.get(CONF_GAS_METER)),
            "normalized_unit": "kWh",
            "health": _determine_health(CONF_GAS_METER, "gas_meter"),
            "issue": _anonymize(source_issues.get("gas_meter"), room_names, source_ids),
        },
        "water": {
            "configured": bool(entry_data.get(CONF_WATER)),
            "normalized_unit": "L",
            "health": _determine_health(CONF_WATER, "water"),
            "issue": _anonymize(source_issues.get("water"), room_names, source_ids),
        },
        "electricity_meter": {
            "configured": bool(entry_data.get(CONF_ELECTRICITY_METER)),
            "normalized_unit": "kWh",
            "health": _determine_health(CONF_ELECTRICITY_METER, "electricity_meter"),
            "issue": _anonymize(source_issues.get("electricity_meter"), room_names, source_ids),
        },
        "co2": {
            "configured": bool(entry_data.get(CONF_CO2)),
            "normalized_unit": "ppm",
            "health": _determine_health(CONF_CO2, "co2"),
            "issue": _anonymize(source_issues.get("co2"), room_names, source_ids),
        },
        "gas_unit_rate": {
            "configured": bool(entry_data.get(CONF_GAS_UNIT_RATE)),
            "normalized_unit": "GBP/kWh",
            "health": _determine_health(CONF_GAS_UNIT_RATE, "gas_unit_rate"),
            "issue": _anonymize(source_issues.get("gas_unit_rate"), room_names, source_ids),
        },
        "electricity_unit_rate": {
            "configured": bool(entry_data.get(CONF_ELECTRICITY_UNIT_RATE)),
            "normalized_unit": "GBP/kWh",
            "health": _determine_health(CONF_ELECTRICITY_UNIT_RATE, "electricity_unit_rate"),
            "issue": _anonymize(source_issues.get("electricity_unit_rate"), room_names, source_ids),
        },
    }

    raw_status = coordinator.data.get("analysis_status", {}) if coordinator and coordinator.data else {}
    sanitized_status: dict[str, Any] = {}
    for metric, info in raw_status.items():
        if metric == "rooms":
            if isinstance(info, dict):
                sanitized_rooms = {}
                for idx, (rid, rinfo) in enumerate(sorted(info.items())):
                    if isinstance(rinfo, dict):
                        sanitized_rooms[f"room_{idx+1}"] = {
                            "status": rinfo.get("status"),
                            "reason": _anonymize(rinfo.get("reason"), room_names, source_ids),
                            "next_action": _anonymize(rinfo.get("next_action"), room_names, source_ids),
                            "usable_observations": rinfo.get("usable_observations"),
                            "required_observations": rinfo.get("required_observations"),
                            "latest_source_day": rinfo.get("latest_source_day"),
                            "source_lag_days": rinfo.get("source_lag_days"),
                            "model_data_through": rinfo.get("model_data_through"),
                        }
                sanitized_status["rooms"] = sanitized_rooms
        elif isinstance(info, dict):
            sanitized_status[metric] = {
                "status": info.get("status"),
                "reason": _anonymize(info.get("reason"), room_names, source_ids),
                "next_action": _anonymize(info.get("next_action"), room_names, source_ids),
                "usable_observations": info.get("usable_observations"),
                "required_observations": info.get("required_observations"),
                "latest_source_day": info.get("latest_source_day"),
                "source_lag_days": info.get("source_lag_days"),
                "model_data_through": info.get("model_data_through"),
            }

    coord_data = coordinator.data if coordinator and coordinator.data else {}
    window_evidence = {
        "max_window_days": entry_data.get(CONF_MAX_WINDOW_DAYS, DEFAULT_MAX_WINDOW_DAYS),
        "hlc_days_used": coord_data.get("hlc", {}).get("days_used") if coord_data.get("hlc") else None,
        "dhw_days_used": coord_data.get("dhw", {}).get("days_used") if coord_data.get("dhw") else None,
        "electricity_days_used": coord_data.get("electricity", {}).get("days_used") if coord_data.get("electricity") else None,
        "water_days_used": coord_data.get("water_usage", {}).get("days_used") if coord_data.get("water_usage") else None,
        "decay_windows_used": coord_data.get("air_change_rate", {}).get("windows") if coord_data.get("air_change_rate") else None,
        "room_nights_fitted": {
            f"room_{idx+1}": r.get("nights_fitted")
            for idx, (rid, r) in enumerate(sorted(coord_data.get("rooms", {}).items()))
            if isinstance(r, dict)
        },
    }

    return {
        "model_version": 2,
        "configuration_schema_version": entry.version,
        "experimental_whole_home_ventilation": entry_data.get(
            CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION, False
        ),
        "history_schema_version": history.get("version"),
        "history_revision": history.get("revision"),
        "migration": {
            "status": migration.get("status"),
            "cutoff": migration.get("cutoff"),
            "completed_at": migration.get("completed_at"),
            "error": migration.get("error"),
            "parity_verified": migration.get("parity_verified", False),
        },
        "rooms": len(history.get("rooms", {})),
        "room_types": {
            room_type: sum(
                room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == room_type
                for room in configured_rooms.values()
            )
            for room_type in sorted(
                {
                    room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED)
                    for room in configured_rooms.values()
                }
            )
        },
        "sources": len(history.get("sources", {})),
        "streams": [
            {
                "stream_id": stream["id"],
                "role": stream["role"],
                "quality": stream.get("quality", "awaiting_observation"),
                "coverage_intervals": len(stream.get("coverage", [])),
            }
            for stream in history.get("streams", {}).values()
        ],
        "archive_rows": {
            kind: sum(chunk["count"] for source in migration.get("sources", {}).values()
                      for chunk in source["chunks"].values() if chunk["kind"] == kind)
            for kind in ("raw", "5minute", "hour")
        },
        "verified_hourly_chunks": sum(bool(chunk.get("verified")) for source in migration.get("sources", {}).values()
                                      for chunk in source["chunks"].values() if chunk.get("kind") == "hour"),
        "pending_assignments": sum(bool(s.get("pending")) for s in history.get("sources", {}).values()),
        "preserved_inputs": [
            {"statistic_id": f"source_{idx+1}",
             "unit": (source.get("metadata") or {}).get("unit_of_measurement"),
             "resolutions": {kind: {
                 "rows": sum(c["count"] for c in source["chunks"].values() if c["kind"] == kind),
                 "first_observation": min((c["first_observation"] for c in source["chunks"].values()
                                           if c["kind"] == kind and c.get("first_observation") is not None), default=None),
                 "last_observation": max((c["last_observation"] for c in source["chunks"].values()
                                          if c["kind"] == kind and c.get("last_observation") is not None), default=None),
             } for kind in ("raw", "5minute", "hour")}}
            for idx, source in enumerate(migration.get("sources", {}).values())
        ],
        "unit_normalization_and_health": unit_health,
        "source_issues": {
            key if key in {"outdoor", "gas_meter", "water", "electricity_meter", "co2", "outdoor_co2_sensor", "gas_unit_rate", "electricity_unit_rate", "loft", "loft_humidity"} else f"room_source_{idx+1}": _anonymize(value, room_names, source_ids)
            for idx, (key, value) in enumerate(source_issues.items())
        },
        "sanitized_analysis_status": sanitized_status,
        "window_and_count_evidence": window_evidence,
    }
