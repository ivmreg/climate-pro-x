from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ThermalCoordinator

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
    _attr_name = "Delivered heat loss coefficient"
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
        return {
            "rating": "not benchmarked; building type and floor area context required",
            "status": fit.get("status", "provisional"),
            "r_squared": round(fit["r_squared"], 3),
            "days_used": fit["days_used"],
            "window_days": fit["window_days"],
            "confidence_interval_low_w_per_k": round(fit["hlc_ci_low_w_per_k"], 1),
            "confidence_interval_high_w_per_k": round(fit["hlc_ci_high_w_per_k"], 1),
            "fuel_input_hlc_w_per_k": round(fit["fuel_input_hlc_w_per_k"], 1),
            "space_heating_fuel_input_hlc_w_per_k": round(
                fit["space_heating_fuel_input_hlc_w_per_k"], 1
            ),
            "boiler_efficiency_used": fit["boiler_efficiency_used"],
            "regression_intercept_kwh_per_day": round(
                fit["regression_intercept_kwh_per_day"], 1
            ),
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
                "non-heating (hot-water) gas estimated and subtracted before fitting"
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
            verdict = "loft stayed relatively warm; ceiling heat transfer may be significant"
        elif ratio > 0.25:
            verdict = "loft temperature was moderately coupled to indoors"
        else:
            verdict = "loft tracked outdoors; this can indicate insulation or strong ventilation"
        return {
            "verdict": verdict,
            "hours_used": fit["hours_used"],
            "window_days": fit["window_days"],
            "interquartile_range": round(fit.get("iqr", 0.0), 3),
            "out_of_range_observations_pct": round(
                fit.get("out_of_range_pct", 0.0), 1
            ),
            "note": "directional estimate, not an insulation payback assessment",
            "humidity_pct": (
                round(fit["humidity_pct"], 1) if "humidity_pct" in fit else None
            ),
        }


class AirChangeRateSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_air_change_rate"
    _attr_name = "Room-derived air change rate proxy"
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
            "co2_sensors_used": losses.get("co2_sensors_used", 1),
            "co2_baseline_source": losses.get("co2_baseline_source"),
            "scope": losses.get("scope"),
        }


class VentilationLossSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_ventilation_loss"
    _attr_name = "Estimated ventilation heat loss"
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
    _attr_name = "Estimated fabric heat loss"
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
    _attr_name = "Non-space-heating gas baseline"
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
            "status": dhw.get("status", "provisional"),
            "includes": "all non-heating gas: hot water, plus cooking/pilot "
                        "only if those burn gas (pure DHW in an "
                        "electric-cooking home)",
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
            "modelled_annual_kwh": (
                round(dhw["modelled_annual_kwh"], 1)
                if "modelled_annual_kwh" in dhw
                else None
            ),
            "wh_per_litre": (
                round(dhw["wh_per_litre"], 1) if "wh_per_litre" in dhw else None
            ),
            "wh_per_litre_basis": (
                "gas fuel input; hot-fraction estimate applies boiler efficiency"
                if "wh_per_litre" in dhw
                else None
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
        self._attr_name = (
            f"{room.replace('_', ' ').title()} effective overnight cooling time constant"
        )

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
