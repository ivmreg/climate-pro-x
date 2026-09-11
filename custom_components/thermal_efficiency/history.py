"""Durable, dated assignments and recorder-backed source/room streams."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import isfinite

from homeassistant.components.recorder import get_instance
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import (
    async_track_state_change_event, async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .assignments import (
    GLOBAL_INPUTS, ROLES, compose, identity, room_roles, validate_visits,
)
from .const import (
    CONF_ASSIGNMENT_SINCE,
    CONF_LOFT,
    CONF_LOFT_HUMIDITY,
    CONF_ROOM_TYPE,
    DOMAIN,
    HISTORY_DATA_VERSION,
    HISTORY_STORE_KEY,
    HISTORY_STORE_VERSION,
    ROOM_TYPE_CONDITIONED,
    ROOM_TYPE_LOFT,
)
from .history_migration import HistoryMigrator, migration_manifest


@dataclass(slots=True)
class StreamBinding:
    id: str
    room_id: str
    role: str
    source_id: str
    source_entity_id: str
    entity_id: str


def observation(state, role):
    """Reject invalid units and non-finite readings rather than guessing."""
    if state is None:
        return None
    try:
        value = float(state.state)
        unit = state.attributes.get("unit_of_measurement")
        if role == "temperature":
            value = TemperatureConverter.convert(value, unit, "°C")
        else:
            norm_unit = unit.strip().casefold() if isinstance(unit, str) else None
            if norm_unit not in {"%", "percent", "percentage"} or not 0 <= value <= 100:
                return None
        return value if isfinite(value) else None
    except (ValueError, TypeError, HomeAssistantError):
        return None


def assignment_timestamp(value) -> float | None:
    """Convert a configured local calendar date to its UTC timestamp."""
    if not value:
        return None
    day = dt_util.parse_date(str(value))
    if day is None:
        raise ValueError("Invalid assignment start date")
    return datetime.combine(
        day, time.min, tzinfo=dt_util.get_default_time_zone()
    ).timestamp()


from types import MappingProxyType


def _to_dict(val):
    if isinstance(val, (dict, MappingProxyType)):
        return {k: _to_dict(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_to_dict(v) for v in val]
    return val


class RoomHistoryManager:
    def __init__(self, hass, entry, config):
        self.hass, self.entry, self.config = hass, entry, deepcopy(_to_dict(config))
        self.store = Store(hass, HISTORY_STORE_VERSION, f"{HISTORY_STORE_KEY}.{entry.entry_id}")
        self.data = {}
        self.entities = {}
        self.values = {}
        self._unsubs = []
        self._state_unsubs = []
        self._tasks = set()
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._add_entities = None
        self._coordinator = None
        self._migration_task = None
        self._stopping = False
        self._status_sensors = set()

    def register_status_sensor(self, sensor):
        self._status_sensors.add(sensor)

    def unregister_status_sensor(self, sensor):
        self._status_sensors.discard(sensor)

    @callback
    def _publish_migration_status(self):
        for sensor in list(self._status_sensors):
            if sensor.hass:
                sensor.async_write_ha_state()

    def _is_owned_entity(self, entity_id: str) -> bool:
        if not entity_id:
            return False
        reg_entry = er.async_get(self.hass).async_get(entity_id)
        if reg_entry and reg_entry.platform == DOMAIN:
            return True
        for stream in self.data.get("streams", {}).values():
            if entity_id == stream.get("entity_id") or entity_id in stream.get("aliases", []):
                return True
        return False

    @property
    def ready(self):
        return self.data.get("migration", {}).get("status") == "complete"

    def _now(self):
        return dt_util.utcnow().timestamp()

    async def _save(self):
        async with self._save_lock:
            await self.store.async_save(deepcopy(self.data))

    def _area(self, entity_id):
        entity = er.async_get(self.hass).async_get(entity_id)
        if not entity:
            return None
        device = dr.async_get(self.hass).async_get(entity.device_id) if entity.device_id else None
        return entity.area_id or (device.area_id if device else None)

    def _source(self, entity_id, role):
        entity = er.async_get(self.hass).async_get(entity_id)
        for known in self.data["sources"].values():
            if not known.get("missing") and not known.get("retired") and known["role"] == role and (
                entity and known["registry_id"] == entity.id
                or not entity and known["registry_id"] is None and known["entity_id"] == entity_id
            ):
                return known
        # Registry-row identity survives entity_id rename but not delete/recreate.
        key = identity(entity.id if entity else f"unregistered:{entity_id}", role)
        if any(self.data["sources"].get(key, {}).get(flag) for flag in ("missing", "retired")):
            # HA may resurrect a deleted registry row. Explicit adoption starts
            # a new generation and cannot silently resume its retired history.
            key = identity(key, str(self.data["revision"]), "replacement")
        return self.data["sources"].setdefault(key, {
            "id": key, "registry_id": entity.id if entity else None,
            "entity_id": entity_id, "role": role, "area_id": self._area(entity_id),
        })

    def _stream(self, source, room_id, data=None):
        data = data if data is not None else self.data
        sid = identity(self.entry.entry_id, source["id"], room_id, source["role"])
        if sid not in data["streams"]:
            unique = f"{DOMAIN}_room_stream_{sid}"
            entity = er.async_get(self.hass).async_get_or_create(
                "sensor", DOMAIN, unique, config_entry=self.entry,
                suggested_object_id=f"thermal_efficiency_{room_id}_{source['role']}_{sid[:6]}",
            )
            data["streams"][sid] = {
                "id": sid, "unique_id": unique, "entity_id": entity.entity_id,
                "room_id": room_id, "role": source["role"], "source_id": source["id"],
                "original_entity_id": source["entity_id"], "coverage": [], "quarantine": [],
            }
        return sid

    def _close(self, visit, when, data=None):
        visit["end"] = max(when, visit.get("start") or when)
        self._gap(visit.get("stream"), when, data=data)

    def _gap(self, sid, when, data=None):
        data = data if data is not None else self.data
        stream = data["streams"].get(sid)
        if not stream:
            return
        closed = False
        for interval in stream["coverage"]:
            if interval.get("end") is None:
                interval["end"] = max(interval["start"], when)
                closed = True
        if data is self.data:
            self.values.pop(sid, None)
            if closed and not self._stopping:
                self._spawn(self._save())
            if not self._stopping and (entity := self.entities.get(sid)) and entity.hass:
                entity.async_write_ha_state()

    def _assign(self, source, room_id, when, cause="area_change", legacy=False, data=None):
        data = data if data is not None else self.data
        role = source["role"]
        vacated = set()
        for rid, room in data["rooms"].items():
            for visit in room["visits"]:
                stream = data["streams"].get(visit.get("stream"), {})
                if visit.get("end") is None and (
                    stream.get("source_id") == source["id"] or rid == room_id and visit["role"] == role
                ):
                    self._close(visit, when, data=data)
                    if rid != room_id:
                        vacated.add(rid)
        for rid in vacated:
            data["rooms"][rid]["visits"].append({
                "id": identity(rid, role, str(when), "gap"), "stream": None,
                "role": role, "start": when, "end": None, "cause": "gap",
                "legacy": False, "expected": True,
                "source_id": source["id"],
            })
        if room_id is not None:
            sid = self._stream(source, room_id, data=data)
            data["rooms"][room_id]["visits"].append({
                "id": identity(sid, str(when), str(data["revision"])),
                "stream": sid, "role": role, "start": None if legacy else when,
                "end": None, "cause": cause, "legacy": legacy, "expected": True,
                "provenance": "legacy_mapping_unverified" if legacy else cause,
            })

    async def async_initialize(self):
        now = self._now()
        self._started_at = now
        stored = await self.store.async_load()
        self.data = stored or {
            "version": HISTORY_DATA_VERSION, "revision": 0, "rooms": {},
            "sources": {}, "streams": {}, "configured": {},
            "original_config": deepcopy(self.config),
            "migration": migration_manifest(self.config, self.entry.entry_id, now),
        }
        self.data["version"] = HISTORY_DATA_VERSION
        last = self.data.get("last_verified", now)
        for sid in self.data["streams"]:
            self._gap(sid, last)
        # Reconcile only explicit config deltas. An unchanged saved config must
        # never undo a move already observed through the HA registry.
        previous = self.data.get("configured", {})
        area_reg = ar.async_get(self.hass)
        for rid, spec in self.config["rooms"].items():
            existing = rid in self.data["rooms"]
            room = self.data["rooms"].setdefault(rid, {
                "name": spec.get("name", rid.replace("_", " ").title()),
                "area_id": None, "visits": [],
            })
            room["name"] = spec.get("name", room["name"])
            if not existing or not room.get("area_id"):
                room_area_id = None
                if rid in area_reg.areas:
                    room_area_id = rid
                else:
                    room_name = spec.get("name", rid.replace("_", " ").title()).lower()
                    for a in area_reg.areas.values():
                        if a.name.lower() == room_name or a.id.lower() == rid.lower():
                            room_area_id = a.id
                            break
                room["area_id"] = room_area_id or self._area(spec["temperature"])
            allowed_roles = room_roles(spec)
            is_loft = (
                spec.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED)
                == ROOM_TYPE_LOFT
            )
            for role in ROLES:
                current = spec.get(role) if role in allowed_roles else None
                previous_spec = previous.get(rid, {})
                if current == previous_spec.get(role):
                    if (
                        is_loft
                        and current
                        and spec.get(CONF_ASSIGNMENT_SINCE)
                        != previous_spec.get(CONF_ASSIGNMENT_SINCE)
                    ):
                        effective = assignment_timestamp(
                            spec.get(CONF_ASSIGNMENT_SINCE)
                        )
                        for visit in room["visits"]:
                            if visit["role"] == role and visit.get("end") is None:
                                visit["start"] = effective
                                visit["legacy"] = effective is None
                                if effective is not None:
                                    visit["cause"] = "loft_migration"
                                    visit["provenance"] = "loft_migration"
                                else:
                                    visit["cause"] = "legacy"
                                    visit["provenance"] = "legacy_mapping_unverified"
                                if visit.get("stream") and (
                                    effective is not None
                                    or current in self.data.get("migration", {}).get("sources", {})
                                ):
                                    self.data["streams"][visit["stream"]]["legacy_bridge_end"] = now
                    continue
                if current:
                    if self._is_owned_entity(current):
                        continue
                    source = self._source(current, role)
                    if any(v.get("end") is None and self.data["streams"].get(v.get("stream"), {}).get("source_id") == source["id"]
                           for v in room["visits"]):
                        continue
                    if is_loft:
                        is_migrated_source = current in self.data.get("migration", {}).get("sources", {})
                        since_changed = (
                            spec.get(CONF_ASSIGNMENT_SINCE) != previous_spec.get(CONF_ASSIGNMENT_SINCE)
                        )
                        is_new_loft = previous_spec.get(CONF_ROOM_TYPE) != ROOM_TYPE_LOFT
                        has_since = bool(spec.get(CONF_ASSIGNMENT_SINCE))

                        use_since = has_since and (not stored or is_new_loft or since_changed)
                        effective = assignment_timestamp(spec.get(CONF_ASSIGNMENT_SINCE)) if use_since else None

                        should_bridge = is_migrated_source or effective is not None
                        if should_bridge or not stored:
                            cause = "loft_migration"
                        else:
                            cause = "replacement"

                        legacy = (effective is None and not stored)
                        when = effective if effective is not None else now
                        self._assign(source, rid, when, cause, legacy=legacy)

                        if should_bridge:
                            active = next(
                                v for v in room["visits"]
                                if v.get("end") is None and v.get("stream")
                                and self.data["streams"][v["stream"]]["source_id"] == source["id"]
                            )
                            self.data["streams"][active["stream"]]["legacy_bridge_end"] = now
                        continue
                    sensor_area = self._area(current)
                    if not stored and room.get("area_id") is not None and sensor_area != room.get("area_id"):
                        self._assign(source, rid, now, "legacy", legacy=True)
                        visit = room["visits"][-1]
                        self._close(visit, now)
                        room["visits"].append({
                            "id": identity(rid, role, str(now), "gap"), "stream": None,
                            "role": role, "start": now, "end": None, "cause": "gap",
                            "legacy": False, "expected": True,
                        })
                        source["pending"] = {"since": 0, "observed": now, "initial": True}
                    else:
                        self._assign(source, rid, now,
                                     "legacy" if not stored else "replacement", legacy=not stored)
                else:
                    for visit in room["visits"]:
                        if visit["role"] == role and visit.get("end") is None:
                            self._close(visit, now)
        for rid in previous.keys() - self.config["rooms"].keys():
            for visit in self.data["rooms"][rid]["visits"]:
                if visit.get("end") is None:
                    self._close(visit, now)
        self.data["configured"] = deepcopy(self.config["rooms"])
        if stored:
            self._reconcile_registry(now, offline_since=last)
        self.data["last_verified"] = now
        self.data["revision"] += 1
        await self._save()

    def bindings(self):
        return [StreamBinding(s["id"], s["room_id"], s["role"], s["source_id"],
                              self.data["sources"][s["source_id"]]["entity_id"], s["entity_id"])
                for s in self.data["streams"].values()]

    def _stream_active(self, sid):
        return any(v.get("stream") == sid and v.get("end") is None
                   for r in self.data["rooms"].values() for v in r["visits"])

    def register_entity(self, binding, entity):
        self.entities[binding.id] = entity

    def _publish_new(self):
        if self._add_entities:
            from .room_sensor import RoomSourceSensor
            new = [RoomSourceSensor(self, b) for b in self.bindings() if b.id not in self.entities]
            # Reserve synchronously to avoid duplicate entities on rapid moves.
            for entity in new:
                self.entities[entity.binding.id] = entity
            if new:
                self._add_entities(new)

    def _spawn(self, coro):
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def async_setup_platform(self, add_entities, coordinator):
        self._add_entities, self._coordinator = add_entities, coordinator
        self._publish_new()
        self._subscribe_states()
        self._unsubs = [
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed),
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry_changed),
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)),
        ]
        # Completed migrations still need an immediate analytical refresh after
        # restart; do not leave the startup placeholders until the six-hour tick.
        self._spawn(coordinator.async_request_refresh())
        self._start_migration()

    def _subscribe_states(self):
        for unsub in self._state_unsubs:
            unsub()
        ids = {s["entity_id"] for s in self.data["sources"].values()}
        self._state_unsubs = [
            async_track_state_change_event(self.hass, ids, self._state_changed),
            async_track_state_report_event(self.hass, ids, self._state_changed),
        ]

    @callback
    def _state_changed(self, event):
        if self._stopping:
            return
        state = event.data["new_state"] if "new_state" in event.data else self.hass.states.get(event.data["entity_id"])
        when = event.time_fired.timestamp()
        for binding in self.bindings():
            if binding.source_entity_id != event.data["entity_id"] or not self._stream_active(binding.id):
                continue
            source = self.data["sources"][binding.source_id]
            stream = self.data["streams"][binding.id]
            visit = next(v for r in self.data["rooms"].values() for v in r["visits"]
                         if v.get("stream") == binding.id and v.get("end") is None)
            if state is not None and (
                state.attributes.get("restored")
                or state.last_reported.timestamp() < max(visit.get("start") or 0, getattr(self, "_started_at", 0))
                or when < stream.get("last_report", 0)
            ):
                continue
            entry = er.async_get(self.hass).async_get(source["entity_id"])
            owned_entry = er.async_get(self.hass).async_get(binding.entity_id)
            value = observation(state, binding.role)
            recorder = get_instance(self.hass)
            included = (recorder.entity_filter is None or recorder.entity_filter(binding.entity_id)) and not (owned_entry and owned_entry.disabled)
            if value is None or source.get("pending") or not included or entry and entry.disabled:
                stream["quality"] = "excluded" if not included else "unavailable"
                self._gap(binding.id, when)
                continue
            if not stream["coverage"] or stream["coverage"][-1].get("end") is not None:
                stream["coverage"].append({"start": when, "end": None})
                self._spawn(self._save())
            stream["last_report"] = when
            stream["quality"] = "capturing"
            self.values[binding.id] = value
            if (entity := self.entities.get(binding.id)) and entity.hass:
                entity.async_write_ha_state()

    @callback
    def _registry_changed(self, event):
        # Mutate synchronously at the event boundary, before any subsequent
        # reading can be attributed to yesterday's registry snapshot.
        if self._reconcile_registry(event.time_fired.timestamp()):
            self._spawn(self._commit_registry())

    def _reconcile_registry(self, when, offline_since=None):
        registry = er.async_get(self.hass)
        rows = {e.id: e for e in registry.entities.values()}
        by_area = {}
        for rid, room in self.data["rooms"].items():
            if rid in self.config["rooms"] and room.get("area_id"):
                by_area.setdefault(room["area_id"], []).append(rid)
        changed = False
        for stream in self.data["streams"].values():
            current = registry.async_get_entity_id("sensor", DOMAIN, stream["unique_id"])
            if current and current != stream["entity_id"]:
                stream.setdefault("aliases", []).append(stream["entity_id"])
                stream["entity_id"] = current
                changed = True
        for source in self.data["sources"].values():
            if source.get("missing") or source.get("retired"):
                continue
            row = rows.get(source["registry_id"])
            if row and source["entity_id"] != row.entity_id:
                source["entity_id"] = row.entity_id
                changed = True
            missing = source["registry_id"] is not None and row is None
            area = self._area(source["entity_id"])
            if area == source["area_id"] and not missing:
                continue
            if source.get("missing") == missing and source.get("pending") and area == source["area_id"]:
                continue
            start = when if offline_since is None else offline_since
            dest = [
                rid for rid in by_area.get(area, [])
                if source["role"] in room_roles(self.config["rooms"][rid])
            ]
            source.update(area_id=area, missing=missing)
            pending = offline_since is not None or missing or len(dest) != 1
            source["pending"] = {"since": start, "observed": when} if pending else None
            self._assign(source, None if pending else dest[0], start, "uncertain" if pending else "area_change")
            changed = True
        return changed

    async def _commit_registry(self):
        async with self._lock:
            self.data["revision"] += 1
            await self._save()
            self._publish_new()
            self._subscribe_states()

    @callback
    def _tick(self, now):
        self._spawn(self._checkpoint(now.timestamp()))
        self._start_migration()

    async def _checkpoint(self, when):
        async with self._lock:
            # Silent inputs become gaps, even if HA retains their last state.
            for sid, stream in self.data["streams"].items():
                if when - stream.get("last_report", when) > 86400:
                    stream["quality"] = "stale"
                    self._gap(sid, stream["last_report"] + 86400)
            self.data["last_verified"] = when
            await self._save()

    def _start_migration(self, force=False):
        if self.ready or self._stopping or self._migration_task and not self._migration_task.done():
            return
        migration = self.data.get("migration", {})
        if not force and migration.get("status") == "failed":
            next_retry = migration.get("next_retry", 0)
            if self._now() < next_retry:
                return
        self._migration_task = self.hass.async_create_background_task(
            self.async_migrate_history(), f"{DOMAIN} preserve retained history"
        )

    async def async_migrate_history(self):
        try:
            await HistoryMigrator(
                self.hass, self.entry, self.data, self._save, self._publish_migration_status
            ).run()
            if self.ready:
                self.data["migration"].pop("retries", None)
                self.data["migration"].pop("next_retry", None)
                await self._save()
        except Exception as err:
            retries = self.data["migration"].get("retries", 0)
            backoff = min(3600, 60 * (2 ** retries))
            now = self._now()
            self.data["migration"].update(
                status="failed",
                error=type(err).__name__,
                retries=retries + 1,
                next_retry=now + backoff,
            )
            await self._save()
        self._publish_migration_status()
        if self._coordinator:
            await self._coordinator.async_request_refresh()

    async def async_retry_migration(self):
        """Immediately retry migration, resetting backoff."""
        if self.ready or self._stopping:
            return
        if "migration" in self.data:
            self.data["migration"]["next_retry"] = 0
        self._start_migration(force=True)

    def statistic_ids(self):
        global_ids = set()
        for key in GLOBAL_INPUTS:
            val = self.config.get(key)
            if val:
                global_ids.update(val if isinstance(val, list) else [val])
        return {s["entity_id"] for s in self.data["streams"].values()} | {
            s["statistic_id"] for s in self.data["migration"]["sources"].values()
        } | global_ids | (set() if self.ready else set(self.data["migration"]["sources"])) | {
            alias for stream in self.data["streams"].values() for alias in stream.get("aliases", [])
        } | {
            stream["original_entity_id"]
            for stream in self.data["streams"].values()
            if stream.get("legacy_bridge_end") is not None
        }

    def prepare(self, stats, conf, tz):
        return compose(stats, conf, self.data, tz)

    def current_rooms(self):
        rooms = deepcopy(self.config["rooms"])
        for rid, spec in rooms.items():
            spec["name"] = self.data["rooms"][rid]["name"]
            for role in room_roles(spec):
                active = [v for v in self.data["rooms"][rid]["visits"] if v["role"] == role and v.get("end") is None]
                if active:
                    stream = self.data["streams"].get(active[0].get("stream"))
                    if stream:
                        spec[role] = self.data["sources"][stream["source_id"]]["entity_id"]
                    else:
                        # Explicit gap: preserve the configured role if the previous source is pending resolution
                        prev_visits = [v for v in self.data["rooms"][rid]["visits"] if v["role"] == role and v.get("stream")]
                        last_source_id = self.data["streams"][prev_visits[-1]["stream"]]["source_id"] if prev_visits else None
                        last_source = self.data["sources"].get(last_source_id) if last_source_id else None
                        if last_source and last_source.get("pending"):
                            pass
                        else:
                            spec.pop(role, None)
                else:
                    spec.pop(role, None)
        return rooms

    async def async_correct(self, source_id, room_id, effective, revision):
        """Resolve an offline move from a user-supplied effective timestamp.

        Unobserved hours remain gaps. This edits provenance, never sensor data.
        """
        async with self._lock:
            if revision != self.data["revision"]:
                raise ValueError("stale_revision")
            source = self.data["sources"].get(source_id)
            if not source:
                raise ValueError("invalid_source")
            pending = source.get("pending")
            if room_id not in self.config["rooms"]:
                raise ValueError("invalid_time")
            if source["role"] not in room_roles(self.config["rooms"][room_id]):
                raise ValueError("role_not_supported")
            if (
                source.get("missing") or not pending or not isfinite(effective)
                or not pending["since"] <= effective <= self._now()
            ):
                raise ValueError("invalid_time")
            if any(v.get("end") is None and v["role"] == source["role"] and v.get("start") is not None and v["start"] > effective
                   for v in self.data["rooms"][room_id]["visits"]):
                raise ValueError("invalid_time")

            staged = deepcopy(self.data)
            staged_source = staged["sources"][source_id]
            self._assign(staged_source, room_id, effective, "correction", data=staged)
            for room in staged["rooms"].values():
                for v in room["visits"]:
                    if v.get("stream") is None and v["role"] == staged_source["role"] and v.get("end") is None:
                        if v.get("source_id") == staged_source["id"] or v.get("start") == pending["since"]:
                            v["end"] = max(v.get("start") or effective, effective)
                    stream = staged["streams"].get(v.get("stream"), {})
                    if stream.get("source_id") == staged_source["id"] and v.get("end") is not None and v["end"] > effective:
                        v["end"] = effective
            for room in staged["rooms"].values():
                room["visits"] = [v for v in room["visits"] if v.get("start") is None or v.get("start") != v.get("end")]
            staged_source["pending"] = None
            validate_visits(staged)
            self.data["rooms"] = staged["rooms"]
            self.data["streams"] = staged["streams"]
            source["pending"] = None
            self.data["revision"] += 1
            await self._save()
            self._publish_new()
            if self._coordinator:
                await self._coordinator.async_request_refresh()

    async def async_replace(self, room_id, role, entity_id, revision):
        """Explicitly adopt a replacement, including same-registry-ID hardware."""
        async with self._lock:
            if revision != self.data["revision"]:
                raise ValueError("stale_revision")
            if (
                room_id not in self.config["rooms"]
                or role not in room_roles(self.config["rooms"][room_id])
            ):
                raise ValueError("invalid_selection")
            if self._is_owned_entity(entity_id) or observation(self.hass.states.get(entity_id), role) is None:
                raise ValueError("invalid_source")
            source = self._source(entity_id, role)
            active_here = any(v.get("end") is None and self.data["streams"].get(v.get("stream"), {}).get("source_id") == source["id"]
                              for v in self.data["rooms"][room_id]["visits"])
            now = self._now()
            room_area = self.data["rooms"][room_id].get("area_id") or self._area(entity_id)
            if active_here:
                source["retired"] = True
                source["pending"] = None
                source["area_id"] = room_area
                source = self._source(entity_id, role)
            source["pending"] = None
            source["area_id"] = room_area
            is_loft = (
                self.config["rooms"][room_id].get(CONF_ROOM_TYPE) == ROOM_TYPE_LOFT
            )
            is_migrated_source = (
                is_loft
                and entity_id in self.data.get("migration", {}).get("sources", {})
            )
            cause = "loft_migration" if is_migrated_source else "replacement"
            self._assign(source, room_id, now, cause)
            if is_migrated_source:
                active = next(
                    v for v in self.data["rooms"][room_id]["visits"]
                    if v.get("end") is None and v.get("stream")
                    and self.data["streams"][v["stream"]]["source_id"] == source["id"]
                )
                self.data["streams"][active["stream"]]["legacy_bridge_end"] = now
            self.data["revision"] += 1
            await self._save()
            self._publish_new()
            self._subscribe_states()
            if self._coordinator:
                await self._coordinator.async_request_refresh()

    async def async_edit_visit(self, visit_id, start, end, exclude, revision):
        """Correct a completed visit's analytical bounds, preserving its audit."""
        async with self._lock:
            if revision != self.data["revision"]:
                raise ValueError("stale_revision")
            if not isfinite(start) or not isfinite(end) or end > self._now():
                raise ValueError("invalid_time")
            staged = deepcopy(self.data)
            visit = next(v for room in staged["rooms"].values() for v in room["visits"] if v["id"] == visit_id)
            if visit.get("end") is None:
                raise ValueError("active_visit")
            visit.setdefault("as_recorded", {k: visit.get(k) for k in ("start", "end", "stream", "cause")})
            visit.update(start=start, end=end, cause="corrected")
            if exclude:
                visit["stream"] = None
            else:
                visit["stream"] = visit["as_recorded"]["stream"]
            validate_visits(staged)
            self.data["rooms"] = staged["rooms"]
            self.data["revision"] += 1
            await self._save()
            if self._coordinator:
                await self._coordinator.async_request_refresh()

    async def async_shutdown(self):
        self._stopping = True
        for unsub in self._unsubs + self._state_unsubs:
            unsub()
        tasks = list(self._tasks)
        if self._migration_task:
            tasks.append(self._migration_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        now = self._now()
        for sid in self.data["streams"]:
            self._gap(sid, now)
        self.data["last_verified"] = now
        await self._save()
