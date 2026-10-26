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

from .assignments import (
    GLOBAL_INPUTS,
    ROLES,
    analysis_configuration,
    configured_inputs,
    identity,
    timestamp,
)
from .const import DOMAIN
from .thermal_math import compute_all

FIELDS = {"mean", "min", "max", "sum", "state", "last_reset", "mean_weight"}
ARCHIVE_FIELDS = set(FIELDS)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_RETRYABLE_GUIDANCE = (
    "This recorder operation can be retried after storage is available; "
    "manual retry is also available."
)
_CONFLICT_GUIDANCE = (
    "Automatic retries are paused because immutable preservation evidence conflicts. "
    "Review recorder history and its matching Home Assistant storage backup together. "
    "Manual retry remains available and will revalidate existing chunks without replacing them."
)
_STAGE_FAILURES = {
    "inventory": (
        "inventory_operation_failed",
        "Source inventory could not be read. Retry after recorder availability is restored.",
    ),
    "archive": (
        "archive_operation_failed",
        "Archive preservation could not complete. Existing chunks are checked before reuse.",
    ),
    "import": (
        "recorder_import_failed",
        "Recorder import or read-back could not complete. Retry after recorder "
        "availability is restored.",
    ),
    "verification": (
        "preservation_verification_failed",
        "Preservation verification could not complete. Retry after recorder "
        "availability is restored.",
    ),
    "parity": (
        "analysis_parity_failed",
        "Preserved history parity could not be checked. Retry after recorder "
        "availability is restored.",
    ),
    "finalization": (
        "migration_finalization_failed",
        "History preservation could not be finalized. Retry after recorder availability is restored.",
    ),
    "startup": (
        "migration_operation_failed",
        "History preservation could not start. Retry after recorder availability "
        "is restored.",
    ),
}


def _safe_chunk_name(value):
    """Keep only known chunk labels and generated numeric boundaries."""
    if not isinstance(value, str):
        return None
    if value in {"raw", "5minute", "hour"}:
        return value
    parts = value.rsplit("_", 2)
    if len(parts) != 3 or parts[0] not in {"raw", "5minute", "hour"}:
        return None
    try:
        start, end = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return f"{parts[0]}_{start}_{end}"


class MigrationFailure(ValueError):
    """Safe, structured failure for a migration operation or immutable conflict."""

    def __init__(
        self, code, stage, reason, *, retryable, source_index=None, chunk=None,
        guidance=None,
    ):
        super().__init__(reason)
        self.code = code
        self.stage = stage
        self.reason = reason
        self.retryable = retryable
        self.source_index = source_index
        self.chunk = _safe_chunk_name(chunk)
        self.guidance = guidance or (
            _RETRYABLE_GUIDANCE if retryable else _CONFLICT_GUIDANCE
        )


async def committed(hass):
    """Queue an explicit barrier, including an import currently being processed.

    async_block_till_done can return early when the worker has removed the
    import from its queue but has not yet marked pending writes.
    """
    future = hass.loop.create_future()
    get_instance(hass).queue_task(SynchronizeTask(future))
    await future


def comparable_analysis(result, original_by_owned):
    """Compare source-quality evidence using original IDs after preservation.

    Only diagnostic keys change with the archive ID; model values and all
    diagnostic messages must still match exactly.
    """
    normalized = dict(result)
    for key in ("source_issues", "heating_power_issues", "invalid_heating_power_entities"):
        if key in normalized:
            normalized[key] = {
                original_by_owned.get(source_id, source_id): issue
                for source_id, issue in normalized[key].items()
            }
    return normalized


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
        sources = data.get("migration", {}).get("sources", {})
        self._source_indexes = {
            source: index for index, source in enumerate(sorted(sources))
        }
        self._failure_stage = "startup"
        self._failure_source_index = None
        self._failure_chunk = None

    def _set_failure_context(self, stage, source=None, chunk=None):
        self._failure_stage = stage
        self._failure_source_index = self._source_indexes.get(source)
        self._failure_chunk = _safe_chunk_name(chunk)

    def _conflict(self, code, reason, stage=None, source=None, chunk=None):
        if stage is None:
            stage = self._failure_stage
        source_index = (
            self._source_indexes.get(source)
            if source is not None else self._failure_source_index
        )
        return MigrationFailure(
            code,
            stage,
            reason,
            retryable=False,
            source_index=source_index,
            chunk=chunk if chunk is not None else self._failure_chunk,
        )

    def failure_details(self, error):
        """Return privacy-safe recovery guidance without serializing exception text."""
        if isinstance(error, MigrationFailure):
            failure = {
                "stage": error.stage,
                "code": error.code,
                "type": type(error).__name__,
                "reason": error.reason,
                "retryable": error.retryable,
                "guidance": error.guidance,
            }
            source_index, chunk = error.source_index, _safe_chunk_name(error.chunk)
        else:
            code, reason = _STAGE_FAILURES.get(
                self._failure_stage, _STAGE_FAILURES["startup"]
            )
            failure = {
                "stage": self._failure_stage,
                "code": code,
                "type": type(error).__name__,
                "reason": reason,
                "retryable": True,
                "guidance": _RETRYABLE_GUIDANCE,
            }
            source_index, chunk = self._failure_source_index, self._failure_chunk
        if source_index is not None:
            failure["source_index"] = source_index
        if chunk is not None:
            failure["chunk"] = chunk
        return failure

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
        self._set_failure_context("archive", source, key)
        record = self.data["migration"]["sources"][source]
        store = Store(self.hass, 1, f"{self.prefix}.{identity(source)}.{key}")
        stored = await store.async_load()
        if stored is not None:
            try:
                stored_rows = stored["rows"]
                stored_digest = stored["digest"]
                valid = digest(stored_rows) == stored_digest
            except (KeyError, TypeError, ValueError):
                valid = False
                stored_rows = None
            if not valid:
                raise self._conflict(
                    "archive_chunk_corrupt",
                    "An immutable archive chunk failed its integrity check.",
                    source=source,
                    chunk=key,
                )
            # The chunk may have been committed before a crash lost its manifest entry.
            if key not in record["chunks"]:
                record["chunks"][key] = self._chunk_summary(stored_rows, kind)
                await self.save()
            return stored_rows, key
        if key in record["chunks"]:
            raise self._conflict(
                "archive_chunk_missing",
                "A manifest-recorded immutable archive chunk is missing.",
                source=source,
                chunk=key,
            )
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
            raise self._conflict(
                "archive_write_verification_failed",
                "A newly written immutable archive chunk failed read-back verification.",
                source=source,
                chunk=key,
            )
        record["chunks"][key] = self._chunk_summary(rows, kind)
        await self.save()
        return rows, key

    async def _snapshot_inventory(self, metadata, raw_start, initial_end):
        """Snapshot every retained source row before the first copy is accepted."""
        inventory = {}
        for source in self.data["migration"]["sources"]:
            self._set_failure_context("inventory", source)
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
            self._set_failure_context("verification", source)
            record = migration["sources"][source]
            archived = {"raw": [], "5minute": [], "hour": []}
            for key, summary in record["chunks"].items():
                self._set_failure_context("verification", source, key)
                try:
                    kind, _start, end = key.rsplit("_", 2)
                    end = int(end)
                except (AttributeError, TypeError, ValueError):
                    raise self._conflict(
                        "archive_manifest_invalid",
                        "The archive manifest contains an invalid chunk reference.",
                        source=source,
                    ) from None
                if kind not in archived:
                    raise self._conflict(
                        "archive_manifest_invalid",
                        "The archive manifest contains an unsupported chunk kind.",
                        source=source,
                        chunk=key,
                    )
                if end > int(initial_end.timestamp()):
                    continue
                store = Store(
                    self.hass, 1,
                    f"{self.prefix}.{identity(source)}.{key}",
                )
                chunk = await store.async_load()
                try:
                    chunk_rows = chunk["rows"] if chunk is not None else None
                    chunk_digest = chunk["digest"] if chunk is not None else None
                    valid_chunk = digest(chunk_rows) == chunk_digest
                    valid_manifest = summary["digest"] == digest(chunk_rows)
                except (KeyError, TypeError, ValueError):
                    valid_chunk = valid_manifest = False
                    chunk_rows = None
                if not valid_chunk:
                    raise self._conflict(
                        "archive_chunk_missing_or_corrupt",
                        "An immutable archive chunk is missing or failed its integrity check.",
                        source=source,
                        chunk=key,
                    )
                if not valid_manifest:
                    raise self._conflict(
                        "archive_manifest_mismatch",
                        "The archive manifest does not match its immutable chunk.",
                        source=source,
                        chunk=key,
                    )
                if not isinstance(chunk_rows, list):
                    raise self._conflict(
                        "archive_chunk_corrupt",
                        "An immutable archive chunk has an invalid row collection.",
                        source=source,
                        chunk=key,
                    )
                archived[kind].extend(chunk_rows)

            for kind in ("raw", "5minute", "hour"):
                if fingerprint(archived[kind], kind) != expected[kind]:
                    raise self._conflict(
                        "source_inventory_mismatch",
                        f"Source changed or disappeared during {kind} preservation.",
                        source=source,
                        chunk=kind,
                    )

            meta = record.get("metadata")
            copied = (
                await self.query(
                    record["statistic_id"], EPOCH, initial_end, "hour", meta
                )
                if meta else []
            )
            if fingerprint(copied, "hour") != expected["hour"]:
                raise self._conflict(
                    "imported_history_mismatch",
                    "Imported history no longer matches the captured source inventory.",
                    source=source,
                    chunk="hour",
                )

    async def run(self):
        migration = self.data["migration"]
        if migration["status"] == "complete":
            return
        self._set_failure_context("inventory")
        migration["status"] = "copying"
        migration.pop("error", None)
        migration.pop("failure", None)
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
                raise self._conflict(
                    "inventory_format_conflict",
                    "The migration inventory format changed after copying began.",
                    stage="inventory",
                )
            migration["inventory"] = await self._snapshot_inventory(
                metadata, raw_start, initial_end
            )
            migration["inventory_version"] = 2
        await self.save()
        for source, record in migration["sources"].items():
            self._set_failure_context("archive", source)
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
            self._set_failure_context("verification")
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
            self._set_failure_context("finalization")
            migration["status"] = "finalizing"
            await self.save()
            if self.on_status:
                self.on_status()
            return
        for source, record in migration["sources"].items():
            self._set_failure_context("finalization", source)
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
        self._set_failure_context("parity")
        await self._verify_model_parity(cutoff)
        self._set_failure_context("finalization")
        migration["status"] = "complete"
        migration["completed_at"] = dt_util.utcnow().timestamp()
        await self.save()
        if self.on_status:
            self.on_status()

    async def _verify_model_parity(self, cutoff):
        """Evaluate both copies on the same date, mappings, units and window."""
        self._set_failure_context("parity")
        migration = self.data["migration"]
        raw_config = deepcopy(migration["configuration"])
        raw_config["gas_unit_rate"] = raw_config["electricity_unit_rate"] = None
        owned_raw_config = deepcopy(raw_config)
        days = int(raw_config.get("max_window_days", 365))
        start = cutoff - timedelta(days=days * 2)
        units = {"temperature": "°C", "energy": "kWh", "volume": "L"}
        sources = migration["sources"]
        ids = set(sources) | {record["statistic_id"] for record in sources.values()}
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, cutoff, ids, "hour", units, {"mean", "sum", "min", "max"}
        )
        original = {sid: stats.get(sid, []) for sid in sources}
        owned = {record["statistic_id"]: stats.get(record["statistic_id"], []) for record in sources.values()}
        for sid, record in sources.items():
            if canonical(original[sid]) != canonical(owned[record["statistic_id"]]):
                raise self._conflict(
                    "source_history_mismatch",
                    "Source history no longer matches the preserved recorder rows.",
                    stage="parity",
                    source=sid,
                )
        for key in (*GLOBAL_INPUTS, "loft", "loft_humidity"):
            val = owned_raw_config.get(key)
            if isinstance(val, str) and val in sources:
                owned_raw_config[key] = sources[val]["statistic_id"]
            elif isinstance(val, list):
                owned_raw_config[key] = [
                    sources.get(x, {}).get("statistic_id", x) for x in val
                ]
        for room in owned_raw_config.get("rooms", {}).values():
            for role in ROLES:
                val = room.get(role)
                if val and val in sources:
                    room[role] = sources[val]["statistic_id"]
        config = analysis_configuration(raw_config)
        owned_config = analysis_configuration(owned_raw_config)
        for prepared in (config, owned_config):
            if prepared.get("loft_since") and isinstance(prepared["loft_since"], str):
                prepared["loft_since"] = dt_util.parse_date(prepared["loft_since"])
        baseline = await self.hass.async_add_executor_job(
            compute_all, original, config, dt_util.get_default_time_zone(), cutoff, (days,)
        )
        preserved = await self.hass.async_add_executor_job(
            compute_all, owned, owned_config, dt_util.get_default_time_zone(), cutoff, (days,)
        )
        original_by_owned = {record["statistic_id"]: sid for sid, record in sources.items()}
        if baseline != comparable_analysis(preserved, original_by_owned):
            raise self._conflict(
                "analysis_parity_mismatch",
                "Preserved history does not match the fixed-date analysis baseline.",
                stage="parity",
            )
        migration["parity_verified"] = True
        await self.save()

    async def _import_verify(self, source, rows, key, start, end):
        self._set_failure_context("import", source, key)
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
                raise self._conflict(
                    "import_readback_mismatch",
                    "Recorder read-back does not match the immutable archived observations.",
                    stage="import",
                    source=source,
                    chunk=key,
                )
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
