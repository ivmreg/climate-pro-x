"""Resumable copy and read-back verification of retained recorder history.

Only public recorder queries and external-statistics imports are used. Source
records are never changed. Immutable Store chunks retain raw observations and
fields not representable by the hourly import API as an inspectable archive.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from copy import deepcopy
from functools import partial
from hashlib import sha256
import json

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, get_metadata, statistics_during_period,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .assignments import GLOBAL_INPUTS, ROLES, configured_inputs, identity, timestamp
from .const import DOMAIN
from .thermal_math import compute_all

FIELDS = {"mean", "min", "max", "sum", "state", "last_reset", "mean_weight"}
ARCHIVE_FIELDS = set(FIELDS)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


async def committed(hass):
    """Queue an explicit barrier, including an import currently being processed.

    async_block_till_done can return early when the worker has removed the
    import from its queue but has not yet marked pending writes.
    """
    future = hass.loop.create_future()
    get_instance(hass).queue_task(SynchronizeTask(future))
    await future


def serializable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def digest(value) -> str:
    return sha256(json.dumps(serializable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def canonical(rows):
    """Compare every timestamp and statistic field, including nulls."""
    return [[timestamp(r["start"])] + [
        timestamp(r[k]) if k == "last_reset" and r.get(k) is not None else r.get(k)
        for k in sorted(ARCHIVE_FIELDS)
    ] for r in rows]


def fingerprint(rows, kind):
    """Return an order-independent inventory of distinct recorder rows.

    Raw daily queries can both include a state at their shared boundary. Treating
    exact duplicates as one observation lets the pre-copy range query be compared
    with the union of the immutable daily chunks without masking changed values.
    """
    values = serializable(rows) if kind == "raw" else canonical(rows)
    encoded = sorted({
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for value in values
    })
    times = [
        timestamp(row["last_updated"] if kind == "raw" else row["start"])
        for row in rows
    ]
    return {
        "count": len(encoded),
        "digest": digest(encoded),
        "first_observation": min(times) if times else None,
        "last_observation": max(times) if times else None,
    }


def import_rows(rows):
    result = []
    for row in rows:
        item = {"start": datetime.fromtimestamp(timestamp(row["start"]), UTC)}
        for field in ARCHIVE_FIELDS:
            if field in row:
                value = row[field]
                if field == "last_reset" and value is not None:
                    value = datetime.fromtimestamp(timestamp(value), UTC)
                item[field] = value
        result.append(item)
    return result


class HistoryMigrator:
    def __init__(self, hass, entry, data, save, on_status=None):
        self.hass, self.entry, self.data, self.save = hass, entry, data, save
        self.on_status = on_status
        self.prefix = f"{DOMAIN}.archive.{entry.entry_id}"

    def _chunk_summary(self, rows, kind):
        times = [timestamp(row["last_updated"] if kind == "raw" else row["start"]) for row in rows]
        return {"count": len(rows), "digest": digest(rows), "kind": kind,
                "first_observation": min(times) if times else None,
                "last_observation": max(times) if times else None}

    def _has_uninterrupted_live_capture(self, cutoff_ts: float) -> bool:
        streams = self.data.get("streams", {})
        active_streams = [
            s for s in streams.values()
            if any(
                v.get("stream") == s["id"] and v.get("end") is None
                for r in self.data.get("rooms", {}).values()
                for v in r.get("visits", [])
            )
        ]
        if not active_streams:
            return True
        for stream in active_streams:
            coverage = stream.get("coverage", [])
            if not coverage:
                continue
            covered = any(
                c["start"] <= cutoff_ts and c.get("end") is None
                for c in coverage
            )
            if not covered:
                return False
        return True

    async def query(self, source, start, end, period, metadata=None):
        units = None
        if metadata and metadata.get("unit_class") and metadata.get("unit_of_measurement"):
            units = {metadata["unit_class"]: metadata["unit_of_measurement"]}
        return (await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, {source}, period, units, FIELDS
        )).get(source, [])

    async def _raw_query(self, source, start, end):
        def read_states():
            states = get_significant_states(
                self.hass, start - timedelta(microseconds=1), end, [source],
                include_start_time_state=False, significant_changes_only=False,
                minimal_response=False, no_attributes=False,
            ).get(source, [])
            return [state.as_dict() if hasattr(state, "as_dict") else state for state in states]

        return await get_instance(self.hass).async_add_executor_job(read_states)

    async def _archive(self, source, kind, start, end, metadata):
        key = f"{kind}_{int(start.timestamp())}_{int(end.timestamp())}"
        record = self.data["migration"]["sources"][source]
        store = Store(self.hass, 1, f"{self.prefix}.{identity(source)}.{key}")
        stored = await store.async_load()
        if stored is not None:
            if digest(stored["rows"]) != stored["digest"]:
                raise ValueError("Archive integrity verification failed")
            # The chunk may have been committed before a crash lost its manifest entry.
            if key not in record["chunks"]:
                record["chunks"][key] = self._chunk_summary(stored["rows"], kind)
                await self.save()
            return stored["rows"], key
        if key in record["chunks"]:
            raise ValueError("A recorded migration archive chunk is missing")
        if kind == "raw":
            rows = await self._raw_query(source, start, end)
        else:
            rows = await self.query(source, start, end, kind, metadata)
        rows = serializable(rows)
        chunk = {"source": source, "start": start.timestamp(), "end": end.timestamp(),
                 "metadata": serializable(metadata), "rows": rows, "digest": digest(rows)}
        await store.async_save(chunk)
        check = await store.async_load()
        if check != chunk:
            raise ValueError("Archive write/read verification failed")
        record["chunks"][key] = self._chunk_summary(rows, kind)
        await self.save()
        return rows, key

    async def _snapshot_inventory(self, metadata, raw_start, initial_end):
        """Snapshot every retained source row before the first copy is accepted."""
        inventory = {}
        for source in self.data["migration"]["sources"]:
            meta = metadata[source][1] if source in metadata else None
            raw_rows = (
                await self._raw_query(source, raw_start, initial_end)
                if ":" not in source else []
            )
            five_rows = (
                await self.query(source, raw_start, initial_end, "5minute", meta)
                if meta else []
            )
            hour_rows = (
                await self.query(source, EPOCH, initial_end, "hour", meta)
                if meta else []
            )
            inventory[source] = {
                "raw": fingerprint(raw_rows, "raw"),
                "5minute": fingerprint(five_rows, "5minute"),
                "hour": fingerprint(hour_rows, "hour"),
            }
        return inventory

    async def _verify_snapshot_inventory(self, initial_end):
        """Prove immutable chunks and imported hourly rows match the snapshot."""
        migration = self.data["migration"]
        for source, expected in migration["inventory"].items():
            record = migration["sources"][source]
            archived = {"raw": [], "5minute": [], "hour": []}
            for key, summary in record["chunks"].items():
                kind, _start, end = key.rsplit("_", 2)
                if int(end) > int(initial_end.timestamp()):
                    continue
                store = Store(
                    self.hass, 1,
                    f"{self.prefix}.{identity(source)}.{key}",
                )
                chunk = await store.async_load()
                if chunk is None or digest(chunk["rows"]) != chunk["digest"]:
                    raise ValueError("Migration archive inventory is missing or corrupt")
                if summary["digest"] != digest(chunk["rows"]):
                    raise ValueError("Migration archive manifest does not match its chunk")
                archived[kind].extend(chunk["rows"])

            for kind in ("raw", "5minute", "hour"):
                if fingerprint(archived[kind], kind) != expected[kind]:
                    raise ValueError(
                        f"Source {source} changed or disappeared during {kind} preservation"
                    )

            meta = record.get("metadata")
            copied = (
                await self.query(
                    record["statistic_id"], EPOCH, initial_end, "hour", meta
                )
                if meta else []
            )
            if fingerprint(copied, "hour") != expected["hour"]:
                raise ValueError("Imported history does not match the source inventory")

    async def run(self):
        migration = self.data["migration"]
        if migration["status"] == "complete":
            return
        migration["status"] = "copying"
        migration.pop("error", None)
        if self.on_status:
            self.on_status()
        instance = get_instance(self.hass)
        await committed(self.hass)
        initial_end = datetime.fromtimestamp(migration["snapshot_end"], UTC)
        cutoff = datetime.fromtimestamp(migration["cutoff"], UTC)
        metadata = await instance.async_add_executor_job(partial(
            get_metadata, self.hass, statistic_ids=set(migration["sources"])
        ))
        # Recorder's oldest retained run bounds the retained raw state history.
        raw_start = dt_util.as_utc(instance.recorder_runs_manager.first.start)
        raw_start = raw_start.replace(hour=0, minute=0, second=0, microsecond=0)
        migration.setdefault("raw_start", raw_start.timestamp())
        if migration.get("inventory_version") != 2:
            if any(record.get("copied") for record in migration["sources"].values()):
                raise ValueError("Migration inventory format changed after copying began")
            migration["inventory"] = await self._snapshot_inventory(
                metadata, raw_start, initial_end
            )
            migration["inventory_version"] = 2
        await self.save()
        for source, record in migration["sources"].items():
            if record.get("copied"):
                continue
            if "metadata" not in record:
                record["metadata"] = dict(metadata[source][1]) if source in metadata else None
                await self.save()
            meta = record["metadata"]
            raw_base = datetime.fromtimestamp(migration["raw_start"], UTC)

            # Use the barrier inventory's bounds. This avoids another mutable
            # source query and skips empty days/years before the first record.
            raw_start_day = None
            raw_first = migration["inventory"][source]["raw"]["first_observation"]
            if raw_first is not None:
                first_dt = datetime.fromtimestamp(raw_first, UTC)
                raw_start_day = max(
                    raw_base,
                    first_dt.replace(hour=0, minute=0, second=0, microsecond=0),
                )

            fivemin_start_day = None
            fivemin_first = migration["inventory"][source]["5minute"]["first_observation"]
            if fivemin_first is not None:
                first_dt = datetime.fromtimestamp(fivemin_first, UTC)
                fivemin_start_day = max(
                    raw_base,
                    first_dt.replace(hour=0, minute=0, second=0, microsecond=0),
                )

            if raw_start_day is not None or fivemin_start_day is not None:
                start_candidates = [d for d in (raw_start_day, fivemin_start_day) if d is not None]
                day = min(start_candidates)
                while day < initial_end:
                    end = min(day + timedelta(days=1), initial_end)
                    if raw_start_day is not None and day >= raw_start_day and ":" not in source:
                        await self._archive(source, "raw", day, end, meta)
                    if fivemin_start_day is not None and day >= fivemin_start_day and meta:
                        await self._archive(source, "5minute", day, end, meta)
                    day = end
            if meta:
                # All retained years, not merely the model lookback; at most one
                # year's hourly rows in memory.
                hour_first = migration["inventory"][source]["hour"]["first_observation"]
                if hour_first is None:
                    record["copied"] = True
                    await self.save()
                    continue
                first_dt = datetime.fromtimestamp(hour_first, UTC)
                start = datetime(first_dt.year, 1, 1, tzinfo=UTC)
                while start < initial_end:
                    end = min(datetime(start.year + 1, 1, 1, tzinfo=UTC), initial_end)
                    rows, key = await self._archive(source, "hour", start, end, meta)
                    await self._import_verify(source, rows, key, start, end)
                    start = end
            record["copied"] = True
            await self.save()
        if not migration.get("snapshot_verified"):
            await self._verify_snapshot_inventory(initial_end)
            migration["snapshot_verified"] = True
            await self.save()
        # Live capture started before this top-of-hour boundary. Wait for the
        # recorder to finalize the bridge bucket instead of creating an upgrade gap.
        if not self._has_uninterrupted_live_capture(cutoff.timestamp()):
            now_dt = dt_util.utcnow()
            now_ts = now_dt.timestamp()
            current_hour_floor = float((int(now_ts) // 3600) * 3600)
            if (
                current_hour_floor > cutoff.timestamp()
                and self._has_uninterrupted_live_capture(current_hour_floor)
                and now_ts >= current_hour_floor + 360
            ):
                new_cutoff_ts = current_hour_floor
            else:
                new_cutoff_ts = float(((int(now_ts) // 3600) + 1) * 3600)

            if new_cutoff_ts > cutoff.timestamp():
                migration["cutoff"] = new_cutoff_ts
                cutoff = datetime.fromtimestamp(new_cutoff_ts, UTC)
                for record in migration["sources"].values():
                    record["tail_done"] = False
                await self.save()

        if dt_util.utcnow() < cutoff + timedelta(minutes=6):
            migration["status"] = "finalizing"
            await self.save()
            if self.on_status:
                self.on_status()
            return
        for source, record in migration["sources"].items():
            meta = record["metadata"]
            if not record.get("tail_done"):
                if ":" not in source:
                    await self._archive(source, "raw", initial_end, cutoff, meta)
                if meta:
                    await self._archive(source, "5minute", initial_end, cutoff, meta)
                    rows, key = await self._archive(source, "hour", initial_end, cutoff, meta)
                    await self._import_verify(source, rows, key, initial_end, cutoff)
                record["tail_done"] = True
                await self.save()
        await self._verify_model_parity(cutoff)
        migration["status"] = "complete"
        migration["completed_at"] = dt_util.utcnow().timestamp()
        await self.save()
        if self.on_status:
            self.on_status()

    async def _verify_model_parity(self, cutoff):
        """Evaluate both copies on the same date, mappings, units and window."""
        migration = self.data["migration"]
        config = deepcopy(migration["configuration"])
        config["gas_unit_rate"] = config["electricity_unit_rate"] = None
        if config.get("loft_since"):
            config["loft_since"] = dt_util.parse_date(config["loft_since"])
        days = int(config.get("max_window_days", 365))
        start = cutoff - timedelta(days=days * 2)
        units = {"temperature": "°C", "energy": "kWh", "volume": "L"}
        sources = migration["sources"]
        ids = set(sources) | {record["statistic_id"] for record in sources.values()}
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, cutoff, ids, "hour", units, {"mean", "sum"}
        )
        original = {sid: stats.get(sid, []) for sid in sources}
        owned = {record["statistic_id"]: stats.get(record["statistic_id"], []) for record in sources.values()}
        for sid, record in sources.items():
            if canonical(original[sid]) != canonical(owned[record["statistic_id"]]):
                raise ValueError("Source changed or disappeared during preservation")
        baseline = await self.hass.async_add_executor_job(
            compute_all, original, config, dt_util.get_default_time_zone(), cutoff, (days,)
        )
        owned_config = deepcopy(config)
        for key in GLOBAL_INPUTS:
            val = owned_config.get(key)
            if isinstance(val, str) and val in sources:
                owned_config[key] = sources[val]["statistic_id"]
            elif isinstance(val, list):
                owned_config[key] = [sources.get(x, {}).get("statistic_id", x) for x in val]
        for room in owned_config.get("rooms", {}).values():
            for role in ROLES:
                val = room.get(role)
                if val and val in sources:
                    room[role] = sources[val]["statistic_id"]
        preserved = await self.hass.async_add_executor_job(
            compute_all, owned, owned_config, dt_util.get_default_time_zone(), cutoff, (days,)
        )
        if baseline != preserved:
            raise ValueError("Preserved history changed fixed-date analysis")
        migration["parity_verified"] = True
        await self.save()

    async def _import_verify(self, source, rows, key, start, end):
        record = self.data["migration"]["sources"][source]
        if record["chunks"][key].get("verified"):
            return
        if rows:
            metadata = dict(record["metadata"])
            metadata.update(source=DOMAIN, statistic_id=record["statistic_id"], name="Climate-pro-x preserved history")
            async_add_external_statistics(self.hass, metadata, import_rows(rows))
            await committed(self.hass)
            copied = await self.query(record["statistic_id"], start, end, "hour", metadata)
            if canonical(rows) != canonical(copied):
                raise ValueError("Imported history did not match the archived observations")
        record["chunks"][key]["verified"] = True
        await self.save()
        if self.on_status:
            self.on_status()


def migration_manifest(config, entry_id, now):
    floor = int(now // 3600) * 3600
    return {
        "status": "pending", "snapshot_end": floor, "cutoff": floor + 3600,
        "configuration": deepcopy(config),
        "sources": {
            source: {"statistic_id": f"{DOMAIN}:legacy_{identity(entry_id, source)}", "chunks": {}}
            for source in sorted(configured_inputs(config))
        },
    }
