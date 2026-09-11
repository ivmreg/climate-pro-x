"""Pure time attribution for owned source/room statistics.

Storage contains observations and provenance independently. Correcting a visit
changes analytical attribution without changing any archived observation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite

ROLES = ("temperature", "heating_power")
GLOBAL_INPUTS = (
    "outdoor", "gas_meter", "loft", "loft_humidity", "co2",
    "outdoor_co2_sensor", "water", "electricity_meter",
)


def identity(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()[:24]


def timestamp(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("An explicit UTC offset is required")
        return parsed.timestamp()
    return float(value)


def inside(start: float, intervals: list[dict], duration: float = 3600) -> bool:
    return any(
        (v.get("start") is None or start >= v["start"])
        and (v.get("end") is None or start + duration <= v["end"])
        for v in intervals
    )


def configured_inputs(config: dict) -> set[str]:
    values = set()
    for key in GLOBAL_INPUTS:
        value = config.get(key)
        candidates = value if isinstance(value, list) else [value]
        values.update(item for item in candidates if isinstance(item, str) and item)
    for room in config["rooms"].values():
        values.update(
            room[role] for role in ROLES
            if isinstance(room.get(role), str) and room[role]
        )
    return values


def validate_visits(data: dict) -> None:
    """Reject overlaps before committing a manual correction."""
    intervals = {}
    stream_intervals = {}
    source_intervals = {}
    for room_id, room in data["rooms"].items():
        for visit in room["visits"]:
            start, end = visit.get("start"), visit.get("end")
            if start is not None and (not isfinite(start) or end is not None and start >= end):
                raise ValueError("Visit end must be after start")
            if end is not None and not isfinite(end):
                raise ValueError("Invalid visit end")
            period = (float("-inf") if start is None else start, float("inf") if end is None else end)
            key = (room_id, visit["role"])
            intervals.setdefault(key, []).append(period)
            stream_id = visit.get("stream")
            if stream_id:
                stream_intervals.setdefault(stream_id, []).append(period)
                source_id = data.get("streams", {}).get(stream_id, {}).get("source_id")
                if source_id:
                    source_intervals.setdefault(source_id, []).append(period)
    for group in (intervals, stream_intervals, source_intervals):
        for periods in group.values():
            periods.sort()
            if any(a[1] > b[0] for a, b in zip(periods, periods[1:])):
                raise ValueError("Assignments overlap")


def compose(stats: dict, config: dict, data: dict, tz) -> tuple[dict, dict]:
    """Select by visit, capture coverage and cutover, never by today's Area."""
    result = dict(stats)
    conf = deepcopy(config)
    cutoff = data["migration"]["cutoff"]
    verified = data["migration"].get("status") == "complete"
    excluded_days = set()
    for room_id, room in data["rooms"].items():
        if room_id not in conf["rooms"]:
            continue
        room_conf = {}
        room_excluded_days = set()
        for visit in room["visits"]:
            if visit.get("cause") != "legacy":
                for boundary in (visit.get("start"), visit.get("end")):
                    if boundary is not None:
                        day_str = datetime.fromtimestamp(boundary, tz).date().isoformat()
                        excluded_days.add(day_str)
                        room_excluded_days.add(day_str)
            elif visit.get("end") is not None:
                day_str = datetime.fromtimestamp(visit["end"], tz).date().isoformat()
                excluded_days.add(day_str)
                room_excluded_days.add(day_str)
        room_conf["excluded_model_days"] = sorted(room_excluded_days)
        for role in ROLES:
            key = f"thermal_efficiency:room_{identity(room_id, role)}"
            by_time = {}
            relevant = [v for v in room["visits"] if v["role"] == role]
            expected = [v for v in relevant if v.get("expected", True)]
            for visit in relevant:
                stream = data.get("streams", {}).get(visit.get("stream"))
                if stream is None:
                    continue
                archive = data.get("migration", {}).get("sources", {}).get(stream["original_entity_id"])
                archived_id = archive["statistic_id"] if archive and verified else stream["original_entity_id"]
                can_claim_archive = visit.get("legacy") or visit.get("cause") in ("area_change", "correction")
                historical = [(archived_id, True)] if can_claim_archive else []
                for stat_id, legacy in historical + [(s, False) for s in [stream.get("entity_id"), *stream.get("aliases", [])]]:
                    for row in stats.get(stat_id, []):
                        start = timestamp(row["start"])
                        if legacy and start + 3600 > cutoff:
                            continue
                        if can_claim_archive and not legacy and start < cutoff and archive:
                            continue
                        if not inside(start, [visit]):
                            continue
                        if not legacy and not inside(start, stream["coverage"]):
                            continue
                        if any(start + 3600 > q["start"] and (q.get("end") is None or start < q["end"]) for q in stream.get("quarantine", [])):
                            continue
                        if stream.get("role") == "heating_power" and legacy and archive and archive.get("metadata") is not None:
                            unit = (archive.get("metadata") or {}).get("unit_of_measurement")
                            norm_unit = unit.strip().casefold() if isinstance(unit, str) else None
                            if norm_unit not in {"%", "percent", "percentage"}:
                                continue
                        if start in by_time:
                            if by_time[start] == row:
                                continue
                            raise ValueError("Two sources contribute to one room/hour")
                        by_time[start] = row
            result[key] = [by_time[t] for t in sorted(by_time)]
            if role == "temperature" or expected:
                room_conf[role] = key
            if role == "heating_power":
                room_conf["heating_expected_intervals"] = [
                    {"start": v.get("start"), "end": v.get("end")}
                    for v in expected
                ]
        conf["rooms"][room_id] = room_conf
    # Preserve all other inputs' old statistics too, including cumulative meters.
    for original, archive in data["migration"]["sources"].items():
        if not verified:
            continue
        old = [r for r in stats.get(archive["statistic_id"], []) if timestamp(r["start"]) < cutoff]
        recent = [r for r in stats.get(original, []) if timestamp(r["start"]) >= cutoff]
        result[original] = old + recent
    conf["excluded_model_days"] = sorted(excluded_days)
    return result, conf
