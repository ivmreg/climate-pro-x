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
        HotWaterUsageSensor(coordinator),
        SpaceHeatingUsageSensor(coordinator),
        ElectricityBaseloadSensor(coordinator),
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
            "day_classification": "heating-off from heating-power sensors "
                                  "where available, dT proxy otherwise; "
                                  "low-water (away) days excluded",
            "days_used": dhw["days_used"],
            "low_water_days_excluded": dhw.get("low_water_days_excluded"),
            "min_occupied_water_litres": dhw.get("min_occupied_water_litres"),
            "idle_gas_kwh_per_day": (
                round(dhw["idle_gas_kwh_per_day"], 2)
                if "idle_gas_kwh_per_day" in dhw
                else None
            ),
            "water_rate_wh_per_litre_per_k": (
                round(dhw["water_rate_wh_per_litre_per_k"], 3)
                if "water_rate_wh_per_litre_per_k" in dhw
                else None
            ),
            "water_rate_days_used": dhw.get("water_rate_days_used"),
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


class HotWaterUsageSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_hot_water_usage_7d"
    _attr_name = "Hot water gas 7-day average"
    _attr_native_unit_of_measurement = "kWh/d"
    _attr_icon = "mdi:shower-head"
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        usage = self.coordinator.data.get("usage")
        if not usage or usage["dhw_kwh_per_day_7d"] is None:
            return None
        return round(usage["dhw_kwh_per_day_7d"], 2)

    @property
    def extra_state_attributes(self) -> dict:
        usage = self.coordinator.data.get("usage")
        if not usage:
            return {"note": "needs a gas meter and a hot-water baseline first"}
        return {
            "method": "heating-off days: all gas counted as hot water; "
                      "heating days: modelled from that day's metered litres "
                      "(or the mains-scaled baseline without water data)",
            "kwh_per_day_30d": (
                round(usage["dhw_kwh_per_day_30d"], 2)
                if usage["dhw_kwh_per_day_30d"] is not None
                else None
            ),
            "cost_per_day_gbp_7d": (
                round(usage["dhw_cost_per_day_gbp_7d"], 2)
                if "dhw_cost_per_day_gbp_7d" in usage
                else None
            ),
            "heating_off_days_in_window": usage["heating_off_days"],
            "modelled_days_in_window": usage["modelled_days"],
            "heating_off_days_from_power_sensors": usage[
                "heating_off_from_power_days"
            ],
        }


class SpaceHeatingUsageSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_space_heating_usage_7d"
    _attr_name = "Space heating gas 7-day average"
    _attr_native_unit_of_measurement = "kWh/d"
    _attr_icon = "mdi:radiator"
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        usage = self.coordinator.data.get("usage")
        if not usage or usage["space_heating_kwh_per_day_7d"] is None:
            return None
        return round(usage["space_heating_kwh_per_day_7d"], 2)

    @property
    def extra_state_attributes(self) -> dict:
        usage = self.coordinator.data.get("usage")
        if not usage:
            return {"note": "needs a gas meter and a hot-water baseline first"}
        return {
            "method": "daily gas minus the attributed hot-water share "
                      "(zero on heating-off days by construction)",
            "kwh_per_day_30d": (
                round(usage["space_heating_kwh_per_day_30d"], 2)
                if usage["space_heating_kwh_per_day_30d"] is not None
                else None
            ),
            "cost_per_day_gbp_7d": (
                round(usage["space_heating_cost_per_day_gbp_7d"], 2)
                if "space_heating_cost_per_day_gbp_7d" in usage
                else None
            ),
        }


class ElectricityBaseloadSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_electricity_baseload"
    _attr_name = "Electricity baseload"
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:power-plug"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        elec = self.coordinator.data.get("electricity")
        return round(elec["baseload_w"], 0) if elec else None

    @property
    def extra_state_attributes(self) -> dict:
        elec = self.coordinator.data.get("electricity")
        if not elec:
            return {"note": "configure an electricity meter; needs a couple "
                            "of weeks of hourly statistics"}
        return {
            "method": "median across days of the cheapest hour - the "
                      "always-on load (fridges, standby, network gear)",
            "kwh_per_day": round(elec["kwh_per_day"], 2),
            "last_7d_kwh_per_day": (
                round(elec["last_7d_kwh_per_day"], 2)
                if elec["last_7d_kwh_per_day"] is not None
                else None
            ),
            "last_30d_kwh_per_day": (
                round(elec["last_30d_kwh_per_day"], 2)
                if elec["last_30d_kwh_per_day"] is not None
                else None
            ),
            "baseload_kwh_per_day": round(elec["baseload_kwh_per_day"], 2),
            "baseload_share_pct": (
                round(elec["baseload_share_pct"], 1)
                if "baseload_share_pct" in elec
                else None
            ),
            "cost_per_year_gbp": (
                round(elec["cost_per_year_gbp"], 0)
                if "cost_per_year_gbp" in elec
                else None
            ),
            "baseload_cost_per_year_gbp": (
                round(elec["baseload_cost_per_year_gbp"], 0)
                if "baseload_cost_per_year_gbp" in elec
                else None
            ),
            "implied_internal_gains_w": round(elec["implied_internal_gains_w"], 0),
            "internal_gains_note": "average electrical draw ends up as heat "
                                   "indoors; context for the HLC free-gains "
                                   "intercept, deliberately not subtracted "
                                   "from the gas fits",
            "days_used": elec["days_used"],
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
