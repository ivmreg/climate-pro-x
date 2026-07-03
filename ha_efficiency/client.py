"""Minimal Home Assistant REST API client."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from dotenv import load_dotenv


class HAClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        load_dotenv()
        self.url = (url or os.environ.get("HA_URL", "")).rstrip("/")
        self.token = token or os.environ.get("HA_TOKEN", "")
        if not self.url or not self.token:
            raise SystemExit(
                "HA_URL / HA_TOKEN not set. Copy .env.example to .env and fill it in."
            )
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {self.token}"

    def _get(self, path: str, **params) -> requests.Response:
        resp = self._session.get(f"{self.url}{path}", params=params, timeout=60)
        resp.raise_for_status()
        return resp

    def ping(self) -> str:
        return self._get("/api/").json().get("message", "")

    def states(self) -> list[dict]:
        return self._get("/api/states").json()

    def history(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, pd.Series]:
        """Numeric state history per entity as tz-aware pandas Series."""
        data = self._get(
            f"/api/history/period/{start.astimezone(timezone.utc).isoformat()}",
            filter_entity_id=",".join(entity_ids),
            end_time=end.astimezone(timezone.utc).isoformat(),
            minimal_response="",
            no_attributes="",
        ).json()

        out: dict[str, pd.Series] = {}
        for entity_states in data:
            if not entity_states:
                continue
            entity_id = entity_states[0]["entity_id"]
            times, values = [], []
            for s in entity_states:
                try:
                    values.append(float(s["state"]))
                except (ValueError, TypeError):
                    continue  # 'unavailable', 'unknown', ...
                times.append(pd.Timestamp(s["last_changed"] if "last_changed" in s else s["last_updated"]))
            if times:
                series = pd.Series(values, index=pd.DatetimeIndex(times), name=entity_id)
                out[entity_id] = series[~series.index.duplicated()].sort_index()
        return out

    def history_chunked(
        self, entity_ids: list[str], start: datetime, end: datetime, chunk_days: int = 2
    ) -> dict[str, pd.Series]:
        """Fetch history in chunks so large ranges don't time out."""
        parts: dict[str, list[pd.Series]] = {}
        t = start
        while t < end:
            t2 = min(t + timedelta(days=chunk_days), end)
            for eid, series in self.history(entity_ids, t, t2).items():
                parts.setdefault(eid, []).append(series)
            t = t2
        return {
            eid: pd.concat(chunks).pipe(lambda s: s[~s.index.duplicated()]).sort_index()
            for eid, chunks in parts.items()
        }
