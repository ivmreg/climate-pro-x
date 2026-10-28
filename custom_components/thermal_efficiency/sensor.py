from __future__ import annotations

import re

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION,
    CONF_HEATING_POWER,
    CONF_ROOMS,
    CONF_ROOM_TYPE,
    DOMAIN,
    ROOM_TYPE_CONDITIONED,
)
from .coordinator import ThermalCoordinator
from .thermal_math import TAU_MIN_NIGHTS


def _analysis_status(coordinator: ThermalCoordinator, metric: str) -> dict:
    """Return the canonical calculated readiness for one metric."""
    status_map = coordinator.data.get("analysis_status", {})
    status = status_map.get(metric, {}) if isinstance(status_map, dict) else {}
    return status if isinstance(status, dict) else {}


def _status_attributes(status_info: dict, fallback_status: str | None = None) -> dict:
    """Expose compact readiness beside a value without exporting model evidence."""
    attrs = {
        "status": status_info.get("status", fallback_status),
        "reason": status_info.get("reason"),
        "next_action": status_info.get("next_action"),
    }
    for key in (
        "latest_source_day",
        "raw_latest_source_day",
        "latest_temperature_day",
        "raw_latest_temperature_day",
        "latest_heating_day",
        "latest_demand_day",
        "temperature_source_lag_days",
        "latest_eligible_day",
        "source_lag_days",
        "eligible_source_lag_days",
        "model_data_through",
        "model_data_from",
        "raw_complete_gas_days",
        "raw_complete_temperature_days",
        "qualified_complete_temperature_days",
        "eligible_observations",
        "temperature_short_gap_days",
        "temperature_interpolated_days",
        "heating_on_with_missing_hours_days",
        "coverage_note",
        "coverage_sensitivity",
        "exclusion_counts",
        "excluded_day_count",
        "excluded_observation_count",
        "excluded_dates_count",
        "excluded_observations_count",
        "excluded_dates_by_reason",
        "sensor_count",
        "temperature_sensor_count",
        "heating_sensor_count",
        "sensor_counts",
        "candidate_nights",
        "rejected_nights",
        "rejection_counts",
        "first_night",
        "last_night",
        "tau_q1_h",
        "tau_q3_h",
        "usable_observations",
        "required_observations",
        "dependencies",
        "dependency_status",
    ):
        if key in status_info:
            attrs[key] = status_info[key]
    return attrs


def _electricity_unavailable_attributes(coordinator: ThermalCoordinator) -> dict:
    status = coordinator.data.get("electricity_status", {})
    analysis = _analysis_status(coordinator, "electricity")
    if not status.get("configured"):
        return {
            "note": "electricity meter is not configured",
            **_status_attributes(analysis, "not_configured"),
        }
    return {
        "note": "not enough complete hourly electricity statistics",
        "usable_days": status.get("usable_days", 0),
        "required_days": status.get("required_days", 14),
        **_status_attributes(analysis, "collecting"),
    }

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: ThermalCoordinator = runtime.coordinator
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
        ElectricityUsageSensor(coordinator),
        WaterUsageSensor(coordinator),
        DataReadinessSensor(coordinator),
    ]
    entities += [
        RoomTauSensor(coordinator, room)
        for room, spec in coordinator.conf["rooms"].items()
        if spec.get(CONF_ROOM_TYPE, ROOM_TYPE_CONDITIONED) == ROOM_TYPE_CONDITIONED
    ]
    from .room_sensor import HistoryStatusSensor
    entities.append(HistoryStatusSensor(coordinator, runtime.history))
    async_add_entities(entities)
    await runtime.history.async_setup_platform(async_add_entities, coordinator)


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
        status_info = _analysis_status(self.coordinator, "hlc")
        if not fit:
            return {
                "scope": "whole_home",
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or (
                    "not enough qualifying heating days; the fit needs complete "
                    "gas and indoor/outdoor temperature data with sustained temperature "
                    "difference and a positive, credible regression"
                ),
            }
        attrs = {
            "scope": "whole_home",
            "rating": "not benchmarked; building type and floor area context required",
            **_status_attributes(status_info, fit.get("status", "provisional")),
            "latest_source_day": status_info.get("latest_source_day"),
            "source_lag_days": status_info.get("source_lag_days"),
            "r_squared": round(fit["r_squared"], 3),
            "days_used": fit["days_used"],
            "window_days": fit["window_days"],
            "confidence_interval_low_w_per_k": round(fit["hlc_ci_low_w_per_k"], 1),
            "confidence_interval_high_w_per_k": round(fit["hlc_ci_high_w_per_k"], 1),
            # Consecutive days share weather, so the interval above is widened
            # for serial correlation; these two explain how much.
            "residual_autocorrelation": round(fit["residual_autocorrelation"], 2),
            "effective_independent_days": round(fit["effective_independent_days"]),
            # Delivered-heat frame, directly comparable with the electricity
            # sensor's implied_internal_gains_w.
            "free_gains_w": round(fit["free_gains_w"]),
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
            "dhw_baseline_days_used": fit.get("dhw_baseline_days_used"),
            "model_data_through": (
                fit["model_data_through"].isoformat()
                if fit.get("model_data_through")
                else None
            ),
            "summer_stability": fit.get("summer_stability"),
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
            "confidence_interval_note": (
                "conditional regression interval; it does not include uncertainty "
                "in boiler efficiency, meter or sensor calibration, DHW attribution, "
                "temperature gap estimation, or how representative the selected rooms are"
            ),
        }
        sensitivity = status_info.get("boiler_efficiency_sensitivity") or fit.get(
            "boiler_efficiency_sensitivity"
        )
        if not sensitivity:
            evidence = self.coordinator.data.get("analysis_evidence", {})
            metrics = evidence.get("metrics", {}) if isinstance(evidence, dict) else {}
            evidence_hlc = metrics.get("hlc", {}) if isinstance(metrics, dict) else {}
            sensitivity = (
                evidence_hlc.get("boiler_efficiency_sensitivity")
                if isinstance(evidence_hlc, dict)
                else None
            )
        if isinstance(sensitivity, dict):
            attrs["boiler_efficiency_sensitivity"] = {
                key: sensitivity.get(key)
                for key in (
                    "low_efficiency",
                    "high_efficiency",
                    "low_hlc_w_per_k",
                    "high_hlc_w_per_k",
                    "delta_low_w_per_k",
                    "delta_high_w_per_k",
                    "bounds",
                    "efficiency_bounds",
                )
                if key in sensitivity
            }
        return attrs


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
        status_info = _analysis_status(self.coordinator, "loft")
        if not fit:
            return {
                **_status_attributes(status_info, "collecting"),
                "note": "not enough valid cold-night overlap; indoor, loft and "
                "outdoor sensors must all report while the indoor-outdoor "
                "temperature difference is large enough",
            }
        ratio = fit["ratio"]
        if ratio > 0.5:
            verdict = "loft stayed relatively warm; ceiling heat transfer may be significant"
        elif ratio > 0.25:
            verdict = "loft temperature was moderately coupled to indoors"
        else:
            verdict = "loft tracked outdoors; this can indicate insulation or strong ventilation"
        return {
            **_status_attributes(status_info, "valid"),
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
        ach = self.coordinator.data.get("air_change_rate") or self.coordinator.data.get("losses")
        return round(ach["ach"], 3) if ach and "ach" in ach else None

    @property
    def extra_state_attributes(self) -> dict:
        ach = self.coordinator.data.get("air_change_rate") or self.coordinator.data.get("losses")
        status_info = _analysis_status(self.coordinator, "air_change_rate")
        if not ach or "ach" not in ach:
            return {
                "scope": "room",
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or (
                    "not enough clean CO2 decay windows yet - configure a CO2 sensor "
                    "(floor area and ceiling height are only required for the ventilation/fabric loss split)"
                ),
            }
        attrs = {
            "scope": ach.get("scope", "room"),
            **_status_attributes(status_info, "valid"),
            "decay_windows_used": ach["windows"],
            "outdoor_co2_baseline_ppm": round(ach["baseline_ppm"], 0),
            "co2_sensors_used": ach.get("co2_sensors_used", 1),
            "co2_baseline_source": ach.get("co2_baseline_source"),
        }
        if status_info.get("latest_source_day"):
            attrs["latest_source_day"] = status_info["latest_source_day"]
        return attrs


class VentilationLossSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_ventilation_loss"
    _attr_name = "Estimated ventilation heat loss"
    _attr_native_unit_of_measurement = "W/K"
    _attr_icon = "mdi:door-open"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.conf.get(CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION, False):
            return None
        losses = self.coordinator.data.get("losses")
        return round(losses["ventilation_w_per_k"], 1) if losses and losses.get("ventilation_w_per_k") is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        opt_in = self.coordinator.conf.get(CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION, False)
        status_info = _analysis_status(self.coordinator, "losses")
        base_attrs = {
            "scope": "whole_home",
            "experimental": True,
            "experimental_assumptions": "Assumes measured room air change rate applies uniformly across whole-home volume",
        }
        if not opt_in:
            return {
                **base_attrs,
                "status": "not_configured",
                "note": "Whole-home ventilation and fabric loss estimates require enabling experimental whole-home ventilation in settings",
                "next_action": "Enable experimental whole-home ventilation in integration settings",
            }
        losses = self.coordinator.data.get("losses")
        if not losses or losses.get("ventilation_w_per_k") is None:
            status_data = self.coordinator.data.get("losses_status")
            diagnostic_note = status_info.get("reason") or "not enough data yet"
            if isinstance(status_data, dict) and status_data.get("diagnostic_note"):
                diagnostic_note = status_data["diagnostic_note"]
            elif isinstance(losses, dict) and losses.get("diagnostic_note"):
                diagnostic_note = losses["diagnostic_note"]
            return {
                **base_attrs,
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or diagnostic_note,
            }
        return {
            **base_attrs,
            **_status_attributes(status_info, "valid"),
            "note": status_info.get("reason"),
            "share_of_delivered_hlc_pct": (
                round(losses["ventilation_share_pct"], 1)
                if losses.get("ventilation_share_pct") is not None
                else None
            ),
            "air_change_rate": round(losses["ach"], 3) if losses.get("ach") is not None else None,
        }


class FabricLossSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_fabric_loss"
    _attr_name = "Estimated fabric heat loss"
    _attr_native_unit_of_measurement = "W/K"
    _attr_icon = "mdi:wall"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.conf.get(CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION, False):
            return None
        losses = self.coordinator.data.get("losses")
        return round(losses["fabric_w_per_k"], 1) if losses and losses.get("fabric_w_per_k") is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        opt_in = self.coordinator.conf.get(CONF_EXPERIMENTAL_WHOLE_HOME_VENTILATION, False)
        status_info = _analysis_status(self.coordinator, "losses")
        base_attrs = {
            "scope": "whole_home",
            "experimental": True,
            "experimental_assumptions": "Assumes measured room air change rate applies uniformly across whole-home volume",
        }
        if not opt_in:
            return {
                **base_attrs,
                "status": "not_configured",
                "note": "Whole-home ventilation and fabric loss estimates require enabling experimental whole-home ventilation in settings",
                "next_action": "Enable experimental whole-home ventilation in integration settings",
            }
        losses = self.coordinator.data.get("losses")
        if not losses or losses.get("fabric_w_per_k") is None:
            status_data = self.coordinator.data.get("losses_status")
            diagnostic_note = status_info.get("reason") or "not enough data yet"
            if isinstance(status_data, dict) and status_data.get("diagnostic_note"):
                diagnostic_note = status_data["diagnostic_note"]
            elif isinstance(losses, dict) and losses.get("diagnostic_note"):
                diagnostic_note = losses["diagnostic_note"]
            return {
                **base_attrs,
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or diagnostic_note,
            }
        return {
            **base_attrs,
            **_status_attributes(status_info, "valid"),
            "note": status_info.get("reason"),
            "hlc_delivered_w_per_k": round(losses["hlc_delivered_w_per_k"], 1) if losses.get("hlc_delivered_w_per_k") is not None else None,
            "boiler_efficiency_used": losses.get("boiler_efficiency_used"),
        }


class HotWaterGasSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_hot_water_gas"
    _attr_name = "Typical hot-water gas baseline"
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
        status_info = _analysis_status(self.coordinator, "dhw")
        if not dhw:
            return {
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or "not enough summer (heating-off) days yet",
            }
        return {
            **_status_attributes(status_info, dhw.get("status", "provisional")),
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
            "water_attribution_days": dhw.get("water_attribution_days"),
            "baseline_fallback_days": dhw.get("baseline_fallback_days"),
            "water_outlier_days_ignored": dhw.get("water_outlier_days_ignored"),
            "water_outlier_limit_litres": (
                round(dhw["water_outlier_limit_litres"], 0)
                if dhw.get("water_outlier_limit_litres") is not None
                else None
            ),
            "latest_complete_gas_day": (
                dhw["latest_complete_gas_day"].isoformat()
                if dhw.get("latest_complete_gas_day")
                else None
            ),
            "latest_complete_water_day": (
                dhw["latest_complete_water_day"].isoformat()
                if dhw.get("latest_complete_water_day")
                else None
            ),
            "water_source_lag_days": dhw.get("water_source_lag_days"),
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
        status_info = _analysis_status(self.coordinator, "usage")
        if not usage:
            return {
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or "needs a gas meter and a hot-water baseline first",
            }
        return {
            **_status_attributes(status_info, "provisional"),
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
            "water_attribution_days": usage.get("water_attribution_days"),
            "baseline_fallback_days": usage.get("baseline_fallback_days"),
            "water_outlier_days_ignored": usage.get("water_outlier_days_ignored"),
            "latest_complete_gas_day": (
                usage["latest_complete_gas_day"].isoformat()
                if usage.get("latest_complete_gas_day")
                else None
            ),
            "latest_complete_water_day": (
                usage["latest_complete_water_day"].isoformat()
                if usage.get("latest_complete_water_day")
                else None
            ),
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
        status_info = _analysis_status(self.coordinator, "usage")
        if not usage:
            return {
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or "needs a gas meter and a hot-water baseline first",
            }
        return {
            **_status_attributes(status_info, "provisional"),
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
            return _electricity_unavailable_attributes(self.coordinator)
        status_info = _analysis_status(self.coordinator, "electricity")
        return {
            **_status_attributes(status_info, "valid"),
            "method": "median across up to the latest 30 days of each day's "
                      "cheapest hour - the "
                      "always-on load (fridges, standby, network gear)",
            "kwh_per_day": round(elec["kwh_per_day"], 2),
            "consumption_period": "latest 30 complete days",
            "historical_kwh_per_day": round(elec["historical_kwh_per_day"], 2),
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
            "current_period_days_used": elec["current_period_days_used"],
            "latest_complete_day": elec["latest_complete_day"].isoformat(),
            "source_lag_days": elec["source_lag_days"],
            "days_used_7d": elec["days_used_7d"],
            "days_used_30d": elec["days_used_30d"],
            "cost_basis": "variable energy charge only; standing charge excluded",
        }


class ElectricityUsageSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_electricity_usage_7d"
    _attr_name = "Electricity use 7-day average"
    _attr_native_unit_of_measurement = "kWh/d"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        elec = self.coordinator.data.get("electricity")
        if not elec or elec["last_7d_kwh_per_day"] is None:
            return None
        return round(elec["last_7d_kwh_per_day"], 2)

    @property
    def extra_state_attributes(self) -> dict:
        elec = self.coordinator.data.get("electricity")
        if not elec:
            return _electricity_unavailable_attributes(self.coordinator)
        status_info = _analysis_status(self.coordinator, "electricity")
        return {
            **_status_attributes(status_info, "valid"),
            "kwh_per_day_30d": (
                round(elec["last_30d_kwh_per_day"], 2)
                if elec["last_30d_kwh_per_day"] is not None
                else None
            ),
            "previous_30d_kwh_per_day": (
                round(elec["previous_30d_kwh_per_day"], 2)
                if elec["previous_30d_kwh_per_day"] is not None
                else None
            ),
            "change_vs_previous_30d_pct": (
                round(elec["change_vs_previous_30d_pct"], 1)
                if elec["change_vs_previous_30d_pct"] is not None
                else None
            ),
            "variable_cost_per_day_gbp_30d": (
                round(elec["cost_per_day_gbp_30d"], 2)
                if "cost_per_day_gbp_30d" in elec
                else None
            ),
            "variable_cost_per_year_gbp_30d": (
                round(elec["cost_per_year_gbp_30d"], 0)
                if "cost_per_year_gbp_30d" in elec
                else None
            ),
            "latest_complete_day": elec["latest_complete_day"].isoformat(),
            "source_lag_days": elec["source_lag_days"],
            "days_used_7d": elec["days_used_7d"],
            "days_used_30d": elec["days_used_30d"],
            "cost_basis": "variable energy charge only; standing charge excluded",
        }


class WaterUsageSensor(ThermalSensor):
    _attr_unique_id = f"{DOMAIN}_water_usage_7d"
    _attr_name = "Total water use 7-day average"
    _attr_native_unit_of_measurement = "L/d"
    _attr_icon = "mdi:water"
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        water = self.coordinator.data.get("water_usage")
        return round(water["litres_per_day_7d"], 1) if water else None

    @property
    def extra_state_attributes(self) -> dict:
        water = self.coordinator.data.get("water_usage")
        analysis = _analysis_status(self.coordinator, "water_usage")
        if not water:
            status = self.coordinator.data.get("water_status", {})
            if not status.get("configured"):
                return {
                    "note": "water statistic is not configured",
                    **_status_attributes(analysis, "not_configured"),
                }
            return {
                "note": "not enough complete hourly water statistics",
                "usable_days": status.get("usable_days", 0),
                "required_days": status.get("required_days", 5),
                **_status_attributes(analysis, "collecting"),
            }
        return {
            **_status_attributes(analysis, "valid"),
            "scope": "total metered water; not inferred hot-water litres",
            "litres_per_day_30d": (
                round(water["litres_per_day_30d"], 1)
                if water["litres_per_day_30d"] is not None
                else None
            ),
            "typical_day_litres_30d": (
                round(water["typical_day_litres_30d"], 1)
                if water["typical_day_litres_30d"] is not None
                else None
            ),
            "high_usage_days_30d": water["high_usage_days_30d"],
            "high_usage_threshold_litres": (
                round(water["outlier_limit_litres"], 0)
                if water["outlier_limit_litres"] is not None
                else None
            ),
            "latest_complete_day": water["latest_complete_day"].isoformat(),
            "source_lag_days": water["source_lag_days"],
            "days_used_7d": water["days_used_7d"],
            "days_used_30d": water["days_used_30d"],
            "days_used": water["days_used"],
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
        room_status = self.coordinator.data.get("analysis_status", {}).get("rooms", {})
        status_info = room_status.get(self._room, {}) if isinstance(room_status, dict) else {}
        if not isinstance(status_info, dict):
            status_info = {}
        if not fit:
            room_conf = self.coordinator.conf.get(CONF_ROOMS, {}).get(
                self._room, {}
            )
            heating_entity = room_conf.get(CONF_HEATING_POWER)
            heating_issue = self.coordinator.data.get(
                "invalid_heating_power_entities", {}
            ).get(heating_entity)
            if heating_issue:
                return {
                    "scope": "room",
                    **_status_attributes(status_info, "source_problem"),
                    "note": "configured heating-demand source is invalid, so cooling "
                    "fits are suppressed until it reports 0-100%",
                    "source_issue": heating_issue,
                    "required_nights": TAU_MIN_NIGHTS,
                }
            return {
                "scope": "room",
                **_status_attributes(status_info, "collecting"),
                "note": status_info.get("reason") or (
                    f"fewer than {TAU_MIN_NIGHTS} usable unheated cooling nights "
                    "passed the coverage, temperature-drop and fit-quality checks"
                ),
                "required_nights": TAU_MIN_NIGHTS,
            }
        attrs = {
            "scope": "room",
            **_status_attributes(status_info, "valid"),
            "nights_fitted": fit["nights_fitted"],
            "last_night": fit["last_night"],
            "window_days": fit["window_days"],
        }
        if status_info.get("latest_source_day"):
            attrs["latest_source_day"] = status_info["latest_source_day"]
        for key in (
            "sensor_count",
            "temperature_sensor_count",
            "heating_sensor_count",
            "sensor_counts",
            "first_night",
            "candidate_nights",
            "rejected_nights",
            "rejection_counts",
            "tau_q1_h",
            "tau_q3_h",
            "latest_temperature_day",
            "latest_heating_day",
            "latest_demand_day",
            "latest_source_day",
        ):
            if key in fit:
                attrs[key] = fit[key]
            elif key in status_info:
                attrs[key] = status_info[key]
        return attrs


class DataReadinessSensor(CoordinatorEntity[ThermalCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_unique_id = f"{DOMAIN}_data_readiness"
    _attr_name = "Data readiness"
    _attr_icon = "mdi:clipboard-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = None
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: ThermalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "home")},
            name="Thermal Efficiency",
            manufacturer="climate-pro-x",
        )

    @property
    def native_value(self) -> str:
        status_map = self.coordinator.data.get("analysis_status", {})
        if not status_map:
            return "not_configured"
        statuses = []
        for k, v in status_map.items():
            if k == "rooms" and isinstance(v, dict):
                statuses.extend(r.get("status") for r in v.values() if isinstance(r, dict))
            elif isinstance(v, dict):
                statuses.append(v.get("status"))

        # Keep a held primary heating baseline visible in aggregate readiness,
        # even when independent secondary metrics are valid.
        for status, readiness in (
            ("source_problem", "source_problem"),
            ("rejected", "rejected"),
            ("historical_baseline_held", "historical_baseline_held"),
            ("collecting", "collecting"),
            ("provisional", "provisional"),
            ("valid", "ready"),
        ):
            if status in statuses:
                return readiness
        return "not_configured"

    @property
    def extra_state_attributes(self) -> dict:
        status_map = self.coordinator.data.get("analysis_status", {})
        source_issues = self.coordinator.data.get("source_issues", {})
        room_names = {}
        rooms = self.coordinator.conf.get(CONF_ROOMS, {})
        for index, (room_id, room) in enumerate(sorted(rooms.items()), 1):
            room_names[room.get("name") or room_id] = f"room_{index}"
            room_names[room_id] = f"room_{index}"

        def sanitize(value):
            text = str(value) if value is not None else None
            if text is None:
                return None
            for room_name, alias in sorted(room_names.items(), key=lambda item: len(item[0]), reverse=True):
                text = re.sub(
                    rf"(?<![\w]){re.escape(room_name)}(?![\w])",
                    alias,
                    text,
                    flags=re.IGNORECASE,
                )
            text = re.sub(r"\b[a-z_]+(?:\.[a-z0-9_-]+)+\b", "<source>", text, flags=re.IGNORECASE)
            text = re.sub(r"(?<![\w])[a-z0-9_.-]+:[a-z0-9_.:-]+", "<source>", text, flags=re.IGNORECASE)
            return text

        sanitized_issues = {
            role if role in {"outdoor", "gas_meter", "water", "electricity_meter", "co2", "outdoor_co2_sensor", "gas_unit_rate", "electricity_unit_rate"} else "room_source": sanitize(msg)
            for role, msg in source_issues.items()
        }
        summary_parts = []
        sanitized_status = {}
        for metric, info in status_map.items():
            if metric == "rooms":
                if isinstance(info, dict):
                    sanitized_status[metric] = {
                        f"room_{index}": {
                            "status": room.get("status"),
                            "reason": sanitize(room.get("reason")),
                            "next_action": sanitize(room.get("next_action")),
                            **{
                                key: room[key]
                                for key in ("usable_observations", "required_observations", "latest_source_day", "source_lag_days", "model_data_through")
                                if key in room
                            },
                        }
                        for index, (_, room) in enumerate(sorted(info.items()), 1)
                        if isinstance(room, dict)
                    }
                    states = sorted({room.get("status") for room in info.values() if isinstance(room, dict) and room.get("status")})
                    if states:
                        summary_parts.append(f"rooms: {', '.join(states)}")
                continue
            if not isinstance(info, dict):
                continue
            st = info.get("status")
            sanitized_status[metric] = {
                "status": st,
                "reason": sanitize(info.get("reason")),
                "next_action": sanitize(info.get("next_action")),
                **{
                    key: info[key]
                    for key in ("usable_observations", "required_observations", "latest_source_day", "source_lag_days", "model_data_through")
                    if key in info
                },
            }
            if st:
                detail = sanitize(info.get("reason"))
                summary_parts.append(f"{metric}: {st}" + (f" — {detail}" if detail else ""))
        summary = "; ".join(summary_parts) if summary_parts else "No metrics active"

        attrs = {
            "summary": summary,
            "metrics": sanitized_status,
            "source_issues": sanitized_issues,
        }
        for metric, info in sanitized_status.items():
            if metric != "rooms" and isinstance(info, dict):
                attrs[f"{metric}_status"] = info.get("status")
                attrs[f"{metric}_reason"] = info.get("reason")
                attrs[f"{metric}_next_action"] = info.get("next_action")
        return attrs
