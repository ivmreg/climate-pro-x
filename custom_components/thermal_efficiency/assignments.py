"""Pure time attribution for owned source/room statistics.

Storage contains observations and provenance independently. Correcting a visit
changes analytical attribution without changing any archived observation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from hashlib import sha256
from math import isfinite

from .thermal_math import (
    HLC_MAX_INTERPOLATED_HOURS_PER_DAY,
    HLC_MAX_INTERPOLATION_GAP_SECONDS,
    HLC_MAX_TEMPERATURE_GAP_HOURS_PER_DAY,
    HLC_MAX_TEMPERATURE_GAP_SECONDS,
    clean_series_from_stats,
    is_valid_temperature_value,
)

from .const import (
    CONF_ASSIGNMENT_SINCE,
    CONF_HEATING_POWER,
    CONF_HUMIDITY,
    CONF_ROOM_TYPE,
    CONF_TEMPERATURE,
    ROOM_TYPE_CONDITIONED,
    ROOM_TYPE_LOFT,
)

ROLES = (CONF_TEMPERATURE, CONF_HEATING_POWER, CONF_HUMIDITY)
GLOBAL_INPUTS = (
    "outdoor", "gas_meter", "co2", "outdoor_co2_sensor", "water",
    "electricity_meter",
)


def room_roles(room: dict) -> tuple[str, ...]:
    """Return the measurement roles supported by a configured room type."""
    if room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT:
        return (CONF_TEMPERATURE, CONF_HUMIDITY)
    return (CONF_TEMPERATURE, CONF_HEATING_POWER)


def analysis_configuration(config: dict) -> dict:
    """Translate typed rooms to the thermal model's conditioned/loft inputs."""
    conf = deepcopy(config)
    conditioned = {}
    for room_id, room in conf.get("rooms", {}).items():
        if room.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT:
            conf["loft"] = room.get(CONF_TEMPERATURE)
            conf["loft_humidity"] = room.get(CONF_HUMIDITY)
            value = room.get(CONF_ASSIGNMENT_SINCE)
            conf["loft_since"] = date.fromisoformat(str(value)) if value else None
        else:
            conditioned[room_id] = room
    conf["rooms"] = conditioned
    return conf


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


# Only a recorded temperature mean with observed coverage on *both* sides of
# one short interruption can be used as a qualified HLC input. Never bridge a
# visit boundary, quarantine, or a missing recorder statistic.
def _short_internal_gap(start: float, intervals: list[dict]) -> float | None:
    """Return the length of one bounded internal coverage gap, else None."""
    end = start + 3600
    ordered = sorted(intervals, key=lambda v: float("-inf") if v.get("start") is None else v["start"])
    for left, right in zip(ordered, ordered[1:]):
        left_start, left_end = left.get("start"), left.get("end")
        right_start, right_end = right.get("start"), right.get("end")
        if (
            (left_start is None or left_start <= start)
            and left_end is not None and start < left_end
            and right_start is not None and left_end < right_start < end
            and (right_end is None or right_end >= end)
            and right_start - left_end <= HLC_MAX_TEMPERATURE_GAP_SECONDS
        ):
            return right_start - left_end
    return None


def configured_inputs(config: dict) -> set[str]:
    values = set()
    for key in GLOBAL_INPUTS:
        value = config.get(key)
        candidates = value if isinstance(value, list) else [value]
        values.update(item for item in candidates if isinstance(item, str) and item)
    for room in config["rooms"].values():
        values.update(
            room[role] for role in room_roles(room)
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
    configured_rooms = conf.get("rooms", {})
    conditioned_rooms = {}
    cutoff = data["migration"]["cutoff"]
    verified = data["migration"].get("status") == "complete"
    excluded_days = set()
    for room_id, room in data["rooms"].items():
        if room_id not in configured_rooms:
            continue
        spec = configured_rooms[room_id]
        is_loft = spec.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_LOFT
        room_conf = {}
        room_excluded_days = set()
        if not is_loft:
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
        for role in room_roles(spec):
            key = f"thermal_efficiency:room_{identity(room_id, role)}"
            by_time = {}
            supplemental_temperature = {}
            relevant = [v for v in room["visits"] if v["role"] == role]
            expected = [v for v in relevant if v.get("expected", True)]
            for visit in relevant:
                stream = data.get("streams", {}).get(visit.get("stream"))
                if stream is None:
                    continue
                archive = data.get("migration", {}).get("sources", {}).get(stream["original_entity_id"])
                archived_id = archive["statistic_id"] if archive and verified else stream["original_entity_id"]
                can_claim_archive = visit.get("legacy") or visit.get("cause") in (
                    "area_change", "correction", "loft_migration"
                )
                candidates = [(archived_id, "archive")] if can_claim_archive else []
                if stream.get("legacy_bridge_end") is not None:
                    candidates.append((stream["original_entity_id"], "bridge"))
                candidates.extend(
                    (s, "owned")
                    for s in [stream.get("entity_id"), *stream.get("aliases", [])]
                    if s
                )
                for stat_id, source_kind in candidates:
                    for row in stats.get(stat_id, []):
                        start = timestamp(row["start"])
                        if source_kind == "archive" and start + 3600 > cutoff:
                            continue
                        if source_kind == "bridge" and not (
                            start >= cutoff
                            and start + 3600 <= stream["legacy_bridge_end"]
                        ):
                            continue
                        if can_claim_archive and source_kind == "owned" and start < cutoff and archive:
                            continue
                        if not inside(start, [visit]):
                            continue
                        if any(start + 3600 > q["start"] and (q.get("end") is None or start < q["end"]) for q in stream.get("quarantine", [])):
                            continue
                        if source_kind == "owned" and not inside(start, stream["coverage"]):
                            gap = (
                                _short_internal_gap(start, stream["coverage"])
                                if role == CONF_TEMPERATURE and not is_loft else None
                            )
                            if gap is not None:
                                existing = supplemental_temperature.get(start)
                                if existing is not None and existing[0] != row:
                                    raise ValueError("Two sources contribute to one room/hour")
                                supplemental_temperature[start] = (row, gap)
                            continue
                        if stream.get("role") == "heating_power" and source_kind == "archive" and archive and archive.get("metadata") is not None:
                            unit = (archive.get("metadata") or {}).get("unit_of_measurement")
                            norm_unit = unit.strip().casefold() if isinstance(unit, str) else None
                            if norm_unit not in {"%", "percent", "percentage"}:
                                continue
                        if start in by_time:
                            if by_time[start] == row:
                                continue
                            raise ValueError("Two sources contribute to one room/hour")
                        by_time[start] = row
            if role == CONF_HUMIDITY and not (spec.get(CONF_HUMIDITY) or relevant or by_time):
                continue
            result[key] = [by_time[t] for t in sorted(by_time)]
            if role == CONF_TEMPERATURE and not is_loft:
                # Daily cap prevents numerous individually short gaps from
                # silently becoming a poorly observed temperature day.
                candidates = {}
                for ts, (row, gap) in supplemental_temperature.items():
                    if ts not in by_time:
                        day = datetime.fromtimestamp(ts, tz).date()
                        candidates.setdefault(day, []).append((ts, row, gap))
                accepted = {
                    ts: (row, gap)
                    for rows in candidates.values()
                    if len(rows) <= HLC_MAX_TEMPERATURE_GAP_HOURS_PER_DAY
                    for ts, row, gap in rows
                }
                hlc_key = f"{key}_hlc"
                hlc_rows = {**by_time, **{ts: item[0] for ts, item in accepted.items()}}
                interpolated = {}
                for visit in relevant:
                    stream = data.get("streams", {}).get(visit.get("stream"))
                    if not stream:
                        continue
                    intervals = sorted(
                        stream["coverage"],
                        key=lambda v: float("-inf") if v.get("start") is None else v["start"],
                    )
                    for left, right in zip(intervals, intervals[1:]):
                        gap_start, gap_end = left.get("end"), right.get("start")
                        if (gap_start is None or gap_end is None or gap_end <= gap_start
                                or gap_end - gap_start > HLC_MAX_INTERPOLATION_GAP_SECONDS):
                            continue
                        first = int(gap_start // 3600) * 3600
                        last = int((gap_end - 0.000001) // 3600) * 3600
                        missing = [ts for ts in range(first, last + 1, 3600) if ts not in hlc_rows]
                        before, after = first - 3600, last + 3600
                        support = range(before, after + 1, 3600)
                        if (not missing or len(missing) > 4 or before not in by_time or after not in by_time
                                or not all(inside(ts, [visit]) for ts in support)
                                or any(after + 3600 > q["start"] and (q.get("end") is None or before < q["end"])
                                       for q in stream.get("quarantine", []))):
                            continue
                        edge_values, _ = clean_series_from_stats(
                            [by_time[before], by_time[after]], is_valid_temperature_value, "mean"
                        )
                        if before not in edge_values or after not in edge_values:
                            continue
                        for ts in missing:
                            fraction = (ts - before) / (after - before)
                            interpolated[ts] = {
                                "start": ts,
                                "mean": edge_values[before] + fraction * (edge_values[after] - edge_values[before]),
                            }
                interpolated_by_day = {}
                for ts, row in interpolated.items():
                    day = datetime.fromtimestamp(ts, tz).date()
                    interpolated_by_day.setdefault(day, []).append((ts, row))
                accepted_interpolation = {
                    ts: row
                    for rows in interpolated_by_day.values()
                    if len(rows) <= HLC_MAX_INTERPOLATED_HOURS_PER_DAY
                    for ts, row in rows
                }
                hlc_rows.update(accepted_interpolation)
                result[hlc_key] = [hlc_rows[t] for t in sorted(hlc_rows)]
                room_conf["hlc_temperature"] = hlc_key
                room_conf["hlc_tolerated_temperature_hours"] = sorted(accepted)
                room_conf["hlc_interpolated_temperature_hours"] = sorted(accepted_interpolation)
            if role in (CONF_TEMPERATURE, CONF_HUMIDITY) or expected:
                room_conf[role] = key
            if role == CONF_HEATING_POWER:
                first_obs = min(by_time) if by_time else None
                room_conf["heating_expected_intervals"] = [
                    {
                        "start": (
                            v.get("start")
                            if v.get("start") is not None
                            else first_obs
                        ),
                        "end": v.get("end"),
                    }
                    for v in expected
                    if v.get("start") is not None or first_obs is not None
                ]
        if is_loft:
            conf["loft"] = room_conf.get(CONF_TEMPERATURE)
            conf["loft_humidity"] = room_conf.get(CONF_HUMIDITY)
            conf["loft_since"] = None
        else:
            conditioned_rooms[room_id] = room_conf
    # Preserve all other inputs' old statistics too, including cumulative meters.
    for original, archive in data["migration"]["sources"].items():
        if not verified:
            continue
        old = [r for r in stats.get(archive["statistic_id"], []) if timestamp(r["start"]) < cutoff]
        recent = [r for r in stats.get(original, []) if timestamp(r["start"]) >= cutoff]
        result[original] = old + recent
    conf["rooms"] = conditioned_rooms
    conf["excluded_model_days"] = sorted(excluded_days)
    return result, conf
