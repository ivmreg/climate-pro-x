"""Privacy-conscious diagnostics for room-history migration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    runtime = entry.runtime_data
    history = runtime.history.data
    configured_rooms = getattr(runtime.history, "config", entry.data).get("rooms", {})
    migration = history.get("migration", {})
    return {
        "configuration_schema_version": entry.version,
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
            {"statistic_id": source["statistic_id"],
             "unit": (source.get("metadata") or {}).get("unit_of_measurement"),
             "resolutions": {kind: {
                 "rows": sum(c["count"] for c in source["chunks"].values() if c["kind"] == kind),
                 "first_observation": min((c["first_observation"] for c in source["chunks"].values()
                                           if c["kind"] == kind and c.get("first_observation") is not None), default=None),
                 "last_observation": max((c["last_observation"] for c in source["chunks"].values()
                                          if c["kind"] == kind and c.get("last_observation") is not None), default=None),
             } for kind in ("raw", "5minute", "hour")}}
            for source in migration.get("sources", {}).values()
        ],
    }
