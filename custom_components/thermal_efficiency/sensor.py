from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ThermalCoordinator

HLC_BANDS = (
    (100, "excellent"),
    (180, "good"),
    (280, "typical solid-wall"),
    (400, "poor"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ThermalCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [HlcSensor(coordinator), LoftSensor(coordinator)]
    entities += [
        RoomTauSensor(coordinator, room) for room in coordinator.conf["rooms"]
    ]
    async_add_entities(entities)


class ThermalSensor(CoordinatorEntity[ThermalCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ThermalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "home")},
            name="Thermal Efficiency",
            manufacturer="climate-pro-x",
        )


class HlcSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_hlc"
    _attr_name = "Heat loss coefficient"
    _attr_native_unit_of_measurement = "W/K"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        fit = self.coordinator.data.get("hlc")
        return round(fit["hlc_w_per_k"], 1) if fit else None

    @property
    def extra_state_attributes(self) -> dict:
        fit = self.coordinator.data.get("hlc")
        if not fit:
            return {"note": "not enough heating days yet"}
        value = fit["hlc_w_per_k"]
        rating = next((label for limit, label in HLC_BANDS if value < limit), "very poor")
        return {
            "rating": rating,
            "r_squared": round(fit["r_squared"], 3),
            "days_used": fit["days_used"],
            "window_days": fit["window_days"],
            "free_gains_kwh_per_day": round(fit["free_gains_kwh_per_day"], 1),
            "hlc_w_per_k_per_m2": (
                round(fit["hlc_w_per_k_per_m2"], 2)
                if "hlc_w_per_k_per_m2" in fit
                else None
            ),
            "dhw_baseline_kwh_per_day": (
                round(fit["dhw_baseline_kwh_per_day"], 1)
                if fit["dhw_baseline_kwh_per_day"] is not None
                else None
            ),
            "recent_hlc_w_per_k": (
                round(fit["recent_hlc_w_per_k"], 1)
                if "recent_hlc_w_per_k" in fit
                else None
            ),
            "recent_window_days": fit.get("recent_window_days"),
            "recent_days_used": fit.get("recent_days_used"),
        }


class LoftSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_loft_ratio"
    _attr_name = "Loft ratio"
    _attr_icon = "mdi:home-roof"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        fit = self.coordinator.data.get("loft")
        return round(fit["ratio"], 3) if fit else None

    @property
    def extra_state_attributes(self) -> dict:
        fit = self.coordinator.data.get("loft")
        if not fit:
            return {"note": "not enough cold night hours yet"}
        ratio = fit["ratio"]
        if ratio > 0.5:
            verdict = "ceiling is the weak link - loft insulation pays off"
        elif ratio > 0.25:
            verdict = "moderate ceiling loss"
        else:
            verdict = "loft tracks outdoor - ceiling insulated or loft well ventilated"
        return {
            "verdict": verdict,
            "hours_used": fit["hours_used"],
            "window_days": fit["window_days"],
            "humidity_pct": (
                round(fit["humidity_pct"], 1) if "humidity_pct" in fit else None
            ),
        }


class RoomTauSensor(ThermalSensor):
    _attr_native_unit_of_measurement = "h"
    _attr_icon = "mdi:thermometer-chevron-down"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ThermalCoordinator, room: str) -> None:
        super().__init__(coordinator)
        self._room = room
        self._attr_unique_id = f"{DOMAIN}_{room}_tau"
        self._attr_name = f"{room.replace('_', ' ').title()} time constant"

    @property
    def native_value(self) -> float | None:
        fit = self.coordinator.data["rooms"].get(self._room)
        return round(fit["tau_median_h"], 1) if fit else None

    @property
    def extra_state_attributes(self) -> dict:
        fit = self.coordinator.data["rooms"].get(self._room)
        if not fit:
            return {"note": "no usable cooling nights yet"}
        return {
            "nights_fitted": fit["nights_fitted"],
            "last_night": fit["last_night"],
            "window_days": fit["window_days"],
        }
