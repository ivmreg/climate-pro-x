from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import thermal_math
from .const import (
    CONF_GAS_METER,
    CONF_HEATING_POWER,
    CONF_LOFT,
    CONF_MAX_WINDOW_DAYS,
    CONF_OUTDOOR,
    CONF_ROOMS,
    CONF_TEMPERATURE,
    DOMAIN,
    EXPANDING_WINDOWS_DAYS,
    UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)


class ThermalCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self.conf = conf

    def _statistic_ids(self) -> set[str]:
        ids = {self.conf[CONF_OUTDOOR]}
        if self.conf.get(CONF_GAS_METER):
            ids.add(self.conf[CONF_GAS_METER])
        if self.conf.get(CONF_LOFT):
            ids.add(self.conf[CONF_LOFT])
        for room in self.conf[CONF_ROOMS].values():
            ids.add(room[CONF_TEMPERATURE])
            if room.get(CONF_HEATING_POWER):
                ids.add(room[CONF_HEATING_POWER])
        return ids

    async def _async_update_data(self) -> dict:
        max_days = self.conf[CONF_MAX_WINDOW_DAYS]
        windows = tuple(d for d in EXPANDING_WINDOWS_DAYS if d < max_days) + (max_days,)
        now = dt_util.utcnow()
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            now - timedelta(days=max_days),
            now,
            self._statistic_ids(),
            "hour",
            None,
            {"mean", "sum"},
        )
        return thermal_math.compute_all(
            stats,
            {
                "rooms": self.conf[CONF_ROOMS],
                "outdoor": self.conf[CONF_OUTDOOR],
                "gas_meter": self.conf.get(CONF_GAS_METER),
                "loft": self.conf.get(CONF_LOFT),
            },
            dt_util.get_default_time_zone(),
            now,
            windows,
        )
