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
    entities: list[SensorEntity] = [
        HlcSensor(coordinator),
        LoftSensor(coordinator),
        AirChangeRateSensor(coordinator),
        VentilationLossSensor(coordinator),
        FabricLossSensor(coordinator),
        HotWaterGasSensor(coordinator),
    ]
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
            "space_heating_hlc_w_per_k": (
                round(fit["space_heating_hlc_w_per_k"], 1)
                if "space_heating_hlc_w_per_k" in fit
                else None
            ),
            "dhw_correction": (
                "hot-water/hob gas estimated and subtracted before fitting"
                if "space_heating_hlc_w_per_k" in fit
                else "not applied - configure a gas meter and enough summer "
                     "(heating-off) days to estimate a DHW baseline"
            ),
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


class AirChangeRateSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_air_change_rate"
    _attr_name = "Air change rate"
    _attr_native_unit_of_measurement = "1/h"
    _attr_icon = "mdi:weather-windy"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        losses = self.coordinator.data.get("losses")
        return round(losses["ach"], 3) if losses else None

    @property
    def extra_state_attributes(self) -> dict:
        losses = self.coordinator.data.get("losses")
        if not losses:
            return {"note": "not enough clean CO2 decay windows yet - "
                             "configure a CO2 sensor, floor area and ceiling height"}
        return {
            "decay_windows_used": losses["windows"],
            "outdoor_co2_baseline_ppm": round(losses["baseline_ppm"], 0),
        }


class VentilationLossSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_ventilation_loss"
    _attr_name = "Ventilation heat loss"
    _attr_native_unit_of_measurement = "W/K"
    _attr_icon = "mdi:door-open"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        losses = self.coordinator.data.get("losses")
        return round(losses["ventilation_w_per_k"], 1) if losses else None

    @property
    def extra_state_attributes(self) -> dict:
        losses = self.coordinator.data.get("losses")
        if not losses:
            return {"note": "not enough data yet"}
        return {
            "share_of_delivered_hlc_pct": (
                round(losses["ventilation_share_pct"], 1)
                if losses["ventilation_share_pct"] is not None
                else None
            ),
            "air_change_rate": round(losses["ach"], 3),
        }


class FabricLossSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_fabric_loss"
    _attr_name = "Fabric heat loss"
    _attr_native_unit_of_measurement = "W/K"
    _attr_icon = "mdi:wall"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        losses = self.coordinator.data.get("losses")
        return round(losses["fabric_w_per_k"], 1) if losses else None

    @property
    def extra_state_attributes(self) -> dict:
        losses = self.coordinator.data.get("losses")
        if not losses:
            return {"note": "not enough data yet"}
        return {
            "hlc_delivered_w_per_k": round(losses["hlc_delivered_w_per_k"], 1),
            "boiler_efficiency_used": losses["boiler_efficiency_used"],
        }


class HotWaterGasSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_hot_water_gas"
    _attr_name = "Hot water gas"
    _attr_native_unit_of_measurement = "kWh/d"
    _attr_icon = "mdi:water-boiler"
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        dhw = self.coordinator.data.get("dhw")
        return round(dhw["kwh_per_day"], 2) if dhw else None

    @property
    def extra_state_attributes(self) -> dict:
        dhw = self.coordinator.data.get("dhw")
        if not dhw:
            return {"note": "not enough summer (heating-off) days yet"}
        return {
            "includes": "DHW + hob + pilot (no way to isolate hob/pilot "
                        "reliably from gas alone)",
            "days_used": dhw["days_used"],
            "cost_per_day_gbp": (
                round(dhw["cost_per_day_gbp"], 2)
                if "cost_per_day_gbp" in dhw
                else None
            ),
            "cost_per_year_gbp": (
                round(dhw["cost_per_year_gbp"], 0)
                if "cost_per_year_gbp" in dhw
                else None
            ),
            "wh_per_litre": (
                round(dhw["wh_per_litre"], 1) if "wh_per_litre" in dhw else None
            ),
            "hot_fraction_of_metered_water_pct": (
                round(dhw["hot_fraction_pct"], 0)
                if "hot_fraction_pct" in dhw
                else None
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
