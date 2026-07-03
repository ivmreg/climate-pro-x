"""Local additive cache of pulled history (data/<entity_id>.csv)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")


def _path(entity_id: str) -> Path:
    return DATA_DIR / f"{entity_id.replace('.', '__')}.csv"


def save(series_by_entity: dict[str, pd.Series]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    for eid, new in series_by_entity.items():
        existing = load(eid)
        if existing is not None:
            new = pd.concat([existing, new])
            new = new[~new.index.duplicated()].sort_index()
        new.to_csv(_path(eid), header=["value"])


def load(entity_id: str) -> pd.Series | None:
    path = _path(entity_id)
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0)
    # utc=True: cached rows may mix +00:00 (LTS) and +01:00 (BST recorder) offsets
    df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
    series = df["value"]
    series = series[~series.index.duplicated()].sort_index()
    series.name = entity_id
    return series


def load_resampled(entity_id: str, freq: str = "5min") -> pd.Series | None:
    """Load and put on a regular grid (time-weighted forward fill)."""
    series = load(entity_id)
    if series is None or series.empty:
        return None
    return series.resample(freq).mean().ffill(limit=int(pd.Timedelta("2h") / pd.Timedelta(freq)))
