"""Fetch HA long-term statistics (hourly, kept forever) via the websocket API.

The REST history endpoint only reaches back as far as recorder retention
(days). LTS gives hourly means for temperature sensors and hourly cumulative
sums for energy sensors, going back to when each sensor was created — which
is what lets us analyse a past heating season.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv


def fetch(entity_ids: list[str], start: datetime) -> dict[str, pd.Series]:
    """Hourly LTS series per entity: mean for measurements, sum for meters."""
    return asyncio.run(_fetch(entity_ids, start))


async def _fetch(entity_ids: list[str], start: datetime) -> dict[str, pd.Series]:
    load_dotenv()
    url = os.environ["HA_URL"].rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    import websockets

    async with websockets.connect(url, max_size=100 * 1024 * 1024) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": os.environ["HA_TOKEN"]}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"websocket auth failed: {auth}")

        await ws.send(json.dumps({
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start.astimezone(timezone.utc).isoformat(),
            "statistic_ids": entity_ids,
            "period": "hour",
            "types": ["mean", "sum"],
        }))
        result = json.loads(await ws.recv())["result"]

    out: dict[str, pd.Series] = {}
    for eid, rows in result.items():
        idx = pd.to_datetime([r["start"] for r in rows], unit="ms", utc=True)
        # Meters (gas kWh) carry 'sum' (cumulative); measurements carry 'mean'.
        if rows and rows[0].get("sum") is not None:
            values = [r["sum"] for r in rows]
        else:
            values = [r.get("mean") for r in rows]
        series = pd.Series(values, index=idx, name=eid, dtype=float).dropna()
        if not series.empty:
            out[eid] = series[~series.index.duplicated()].sort_index()
    return out
