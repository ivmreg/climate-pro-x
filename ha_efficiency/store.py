"""Local cache of pulled Home Assistant history.

Legacy versions wrote every source into ``data/<entity_id>.csv``.  That is
safe for ordinary measurements, but not for cumulative statistics: recorder
history and long-term statistics can use different cumulative baselines.  A
blind merge then invents very large positive and negative meter changes.

New callers can identify the source (``rest`` or ``lts``).  Those observations
are kept in separate files and merged only when that is demonstrably safe.
The legacy file remains readable so existing installations and scripts keep
working.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DATA_DIR = Path("data")
SOURCE_DIR_NAME = "_sources"
KNOWN_SOURCES = ("lts", "rest")


def _filename(entity_id: str) -> str:
    return f"{entity_id.replace('.', '__')}.csv"


def _path(entity_id: str) -> Path:
    """Path used by the original, source-agnostic cache."""
    return DATA_DIR / _filename(entity_id)


def _safe_source(source: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", source.strip().lower())
    if not value:
        raise ValueError("cache source must not be empty")
    return value


def _source_path(entity_id: str, source: str) -> Path:
    return DATA_DIR / SOURCE_DIR_NAME / _safe_source(source) / _filename(entity_id)


def _metadata_path(path: Path) -> Path:
    return path.with_suffix(".json")


def _normalise(series: pd.Series, entity_id: str) -> pd.Series:
    """Return a numeric, UTC-indexed, sorted series with newest duplicates."""
    if not isinstance(series.index, pd.DatetimeIndex):
        index = pd.to_datetime(series.index, utc=True, format="ISO8601")
    elif series.index.tz is None:
        index = series.index.tz_localize("UTC")
    else:
        index = series.index.tz_convert("UTC")
    values = pd.to_numeric(series, errors="coerce")
    out = pd.Series(values.to_numpy(dtype=float), index=index, name=entity_id)
    out = out[np.isfinite(out.to_numpy())]
    # keep='last' is deliberate: a refresh/backfill must be able to correct a
    # previously cached value at the same timestamp.
    return out[~out.index.duplicated(keep="last")].sort_index()


def _read(path: Path, entity_id: str) -> pd.Series | None:
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0)
    if "value" not in df.columns:
        return None
    raw = pd.Series(df["value"].to_numpy(), index=df.index, name=entity_id)
    return _normalise(raw, entity_id)


def _read_metadata(path: Path) -> dict[str, Any]:
    meta_path = _metadata_path(path)
    if not meta_path.exists():
        return {}
    try:
        value = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(series: pd.Series, path: Path, metadata: dict[str, Any]) -> None:
    """Atomically replace a cache file and its small provenance sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    series.to_csv(tmp, header=["value"])
    tmp.replace(path)

    meta_path = _metadata_path(path)
    meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    meta_tmp.replace(meta_path)


def save(
    series_by_entity: dict[str, pd.Series],
    *,
    source: str | None = None,
    kind_by_entity: dict[str, str] | None = None,
    metadata_by_entity: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Save observations, allowing refreshed values to replace old values.

    ``source`` should be supplied by ingestion code.  Source-specific files
    prevent incompatible cumulative baselines from being interleaved.  Calls
    which omit it continue to use the legacy path for API compatibility.

    ``kind_by_entity`` values are normally ``measurement`` or ``cumulative``.
    They are used by :func:`load` to decide whether cross-source merging is
    physically safe.
    """
    DATA_DIR.mkdir(exist_ok=True)
    safe_source = _safe_source(source) if source else None
    kind_by_entity = kind_by_entity or {}
    metadata_by_entity = metadata_by_entity or {}

    for entity_id, incoming in series_by_entity.items():
        path = _source_path(entity_id, safe_source) if safe_source else _path(entity_id)
        new = _normalise(incoming, entity_id)
        if new.empty:
            continue
        existing = _read(path, entity_id)
        if existing is not None and not existing.empty:
            # Existing first, incoming last: keep='last' makes the new pull win.
            new = pd.concat([existing, new])
            new = new[~new.index.duplicated(keep="last")].sort_index()

        kind = kind_by_entity.get(entity_id) or incoming.attrs.get("kind")
        unit = metadata_by_entity.get(entity_id, {}).get("unit") or incoming.attrs.get("unit")
        meta: dict[str, Any] = {
            "entity_id": entity_id,
            "source": safe_source or "legacy",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "rows": len(new),
            "start": new.index[0].isoformat(),
            "end": new.index[-1].isoformat(),
        }
        if kind:
            meta["kind"] = kind
        if unit:
            meta["unit"] = unit
        meta.update(metadata_by_entity.get(entity_id, {}))
        _write(new, path, meta)


def available_sources(entity_id: str) -> list[str]:
    """Return source-specific cache names available for an entity."""
    root = DATA_DIR / SOURCE_DIR_NAME
    if not root.exists():
        return []
    return sorted(
        directory.name
        for directory in root.iterdir()
        if directory.is_dir() and _source_path(entity_id, directory.name).exists()
    )


def _cadence(series: pd.Series) -> pd.Timedelta | None:
    if len(series) < 2:
        return None
    gaps = series.index.to_series().diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    if gaps.empty:
        return None
    # Median is resistant to occasional recorder gaps.
    return pd.Timedelta(gaps.median())


def _baseline_offset(anchor: pd.Series, other: pd.Series) -> tuple[float, int] | None:
    """Estimate a constant cumulative-baseline offset from close overlaps."""
    if anchor.empty or other.empty:
        return None
    cadence = _cadence(anchor) or _cadence(other) or pd.Timedelta("1h")
    tolerance = min(max(cadence / 2, pd.Timedelta("5min")), pd.Timedelta("45min"))
    left = other.rename("other").sort_index().to_frame().reset_index(names="timestamp")
    right = anchor.rename("anchor").sort_index().to_frame().reset_index(names="timestamp")
    paired = pd.merge_asof(
        left,
        right,
        on="timestamp",
        direction="nearest",
        tolerance=tolerance,
    ).dropna()
    if len(paired) < 3:
        return None
    delta = paired["other"] - paired["anchor"]
    offset = float(delta.median())
    mad = float((delta - offset).abs().median())
    # Nearby meter readings can differ by genuine consumption.  A stable
    # baseline offset should nevertheless have very little dispersion.
    allowed_mad = max(0.25, min(2.0, abs(offset) * 0.001))
    if not math.isfinite(offset) or mad > allowed_mad:
        return None
    return offset, len(paired)


def _merge_measurements(series_by_source: dict[str, pd.Series]) -> pd.Series:
    # LTS is placed first and higher-resolution REST observations last, so a
    # matching REST timestamp wins without discarding long-term history.
    order = sorted(series_by_source, key=lambda s: (s == "rest", s))
    merged = pd.concat([series_by_source[s] for s in order])
    return merged[~merged.index.duplicated(keep="last")].sort_index()


def _merge_cumulative(series_by_source: dict[str, pd.Series]) -> pd.Series:
    """Merge cumulative sources only after proving a constant offset."""
    if len(series_by_source) == 1:
        source, only = next(iter(series_by_source.items()))
        only = only.copy()
        only.attrs.update({"sources_used": [source], "source": source})
        return only

    # LTS is the best long-history anchor.  Otherwise choose the series with
    # the widest span, then the most observations.
    if "lts" in series_by_source:
        anchor_name = "lts"
    else:
        anchor_name = max(
            series_by_source,
            key=lambda name: (
                series_by_source[name].index[-1] - series_by_source[name].index[0],
                len(series_by_source[name]),
            ),
        )
    merged = series_by_source[anchor_name].copy()
    used = [anchor_name]
    rejected: list[str] = []
    offsets: dict[str, float] = {}

    for source, candidate in series_by_source.items():
        if source == anchor_name:
            continue
        estimate = _baseline_offset(series_by_source[anchor_name], candidate)
        if estimate is None:
            rejected.append(source)
            continue
        offset, _pairs = estimate
        normalised = candidate - offset
        merged = pd.concat([merged, normalised])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        offsets[source] = offset
        used.append(source)

    merged.name = series_by_source[anchor_name].name
    merged.attrs.update(
        {
            "source": "normalised_merge" if len(used) > 1 else anchor_name,
            "sources_used": used,
            "sources_rejected": rejected,
            "baseline_offsets": offsets,
        }
    )
    if rejected:
        merged.attrs["quality_warning"] = (
            "Cumulative cache sources without a reliable overlap were not merged: "
            + ", ".join(rejected)
        )
    return merged


def load(entity_id: str, *, source: str | None = None) -> pd.Series | None:
    """Load cached observations.

    Supplying ``source`` reads exactly that source.  The default combines
    measurement sources, but cumulative sources are combined only after a
    stable constant offset has been established from overlapping readings.
    Source-specific data takes precedence over the old mixed legacy cache.
    """
    if source:
        safe_source = _safe_source(source)
        path = _path(entity_id) if safe_source == "legacy" else _source_path(entity_id, safe_source)
        value = _read(path, entity_id)
        if value is not None:
            value.attrs.update(_read_metadata(path))
        return value

    sources = available_sources(entity_id)
    if not sources:
        value = _read(_path(entity_id), entity_id)
        if value is not None:
            value.attrs.update(_read_metadata(_path(entity_id)))
        return value

    values: dict[str, pd.Series] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for name in sources:
        path = _source_path(entity_id, name)
        value = _read(path, entity_id)
        if value is not None and not value.empty:
            values[name] = value
            metadata[name] = _read_metadata(path)
    if not values:
        return None

    kinds = {meta.get("kind") for meta in metadata.values() if meta.get("kind")}
    if kinds == {"cumulative"}:
        result = _merge_cumulative(values)
    elif "cumulative" in kinds:
        # Conflicting/missing metadata is not enough evidence to combine a
        # cumulative meter.  Select one coherent source and expose the issue.
        preferred = "lts" if "lts" in values else max(values, key=lambda s: len(values[s]))
        result = values[preferred].copy()
        result.attrs.update(
            {
                "source": preferred,
                "sources_used": [preferred],
                "sources_rejected": [s for s in values if s != preferred],
                "quality_warning": "Conflicting cache-kind metadata; cumulative sources were not merged.",
            }
        )
    else:
        result = _merge_measurements(values)
        result.attrs.update({"source": "merged", "sources_used": list(values)})
    if len(kinds) == 1:
        result.attrs["kind"] = next(iter(kinds))
    return result


def load_resampled(entity_id: str, freq: str = "5min") -> pd.Series | None:
    """Load and put on a regular grid with a bounded forward fill."""
    series = load(entity_id)
    if series is None or series.empty:
        return None
    attrs = dict(series.attrs)
    result = series.resample(freq).mean().ffill(
        limit=int(pd.Timedelta("2h") / pd.Timedelta(freq))
    )
    result.attrs.update(attrs)
    return result


def audit(
    entity_id: str,
    *,
    cumulative: bool = False,
    max_step: float = 40.0,
) -> dict[str, Any]:
    """Inspect each physical cache file without merging its sources."""
    names = available_sources(entity_id)
    if _path(entity_id).exists():
        names = ["legacy", *names]
    report: dict[str, Any] = {"entity_id": entity_id, "sources": {}, "warnings": []}
    for name in names:
        path = _path(entity_id) if name == "legacy" else _source_path(entity_id, name)
        series = _read(path, entity_id)
        if series is None or series.empty:
            continue
        gaps = series.index.to_series().diff().dropna()
        cadence = _cadence(series)
        source_report: dict[str, Any] = {
            "rows": len(series),
            "start": series.index[0],
            "end": series.index[-1],
            "cadence": cadence,
            "large_gaps": int((gaps > cadence * 1.5).sum()) if cadence else 0,
        }
        if cumulative:
            diffs = series.diff().dropna()
            negative = diffs[diffs < 0]
            large = diffs[diffs > max_step]
            source_report.update(
                {
                    "negative_steps": len(negative),
                    "over_limit_steps": len(large),
                }
            )
            if len(negative) >= 2 and len(large) >= 2:
                positive_size = float(large.median())
                negative_size = float((-negative).median())
                similar = abs(positive_size - negative_size) <= max(
                    max_step, 0.1 * max(positive_size, negative_size)
                )
                if similar:
                    source_report["mixed_baseline_likely"] = True
                    report["warnings"].append(
                        f"{name}: alternating cumulative baselines are likely"
                    )
        report["sources"][name] = source_report
    return report


def repair(entity_id: str, *, source: str = "legacy", cumulative: bool = False) -> dict[str, Any]:
    """Safely canonicalise a cache file, refusing baseline reconstruction.

    Sorting, numeric cleanup and duplicate replacement are deterministic.
    Mixed cumulative baselines cannot be repaired without source provenance,
    so this function deliberately refuses to guess and directs the caller to
    re-pull a clean source instead.
    """
    report = audit(entity_id, cumulative=cumulative)
    source_report = report["sources"].get(source)
    if not source_report:
        return {"entity_id": entity_id, "source": source, "status": "missing"}
    if source_report.get("mixed_baseline_likely"):
        return {
            "entity_id": entity_id,
            "source": source,
            "status": "refused",
            "reason": "mixed cumulative baselines require a clean source-specific re-pull",
        }
    path = _path(entity_id) if source == "legacy" else _source_path(entity_id, source)
    series = _read(path, entity_id)
    assert series is not None
    metadata = _read_metadata(path)
    metadata.update({"repaired_at": datetime.now(timezone.utc).isoformat()})
    _write(series, path, metadata)
    return {"entity_id": entity_id, "source": source, "status": "rewritten", "rows": len(series)}
