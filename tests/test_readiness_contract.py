"""Boundary contracts for actionable readiness and rejected source evidence."""
import importlib.util
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

UTC = timezone.utc
NOW = datetime(2026, 7, 1, tzinfo=UTC)
DAY = date(2026, 6, 30)
CONF = {
    "gas_meter": "sensor.gas", "outdoor": "sensor.out", "water": "external:water",
    "electricity_meter": "sensor.electricity", "co2": ["sensor.co2"], "loft": "sensor.loft",
    "rooms": {"bed": {"temperature": "sensor.bed", "heating_power": "sensor.heat"}},
    "experimental_whole_home_ventilation": True, "floor_area_m2": 100, "ceiling_height_m": 2.5,
}


def status(math, result=None, conf=None, q=None, dt=None, anchor=DAY, diagnostics=None):
    return math.build_analysis_status(
        result or {}, CONF if conf is None else conf, UTC, NOW,
        {DAY: 40} if q is None else q, {}, anchor,
        hlc_diagnostics=diagnostics, dt_by_day={DAY: 10} if dt is None else dt,
    )


@pytest.mark.parametrize("role,metrics", [
    ("gas_meter", ("hlc", "dhw", "usage")), ("electricity_meter", ("electricity",)),
    ("water", ("water_usage",)), ("co2", ("air_change_rate",)), ("loft", ("loft",)),
])
def test_source_problem_is_actionable_and_cannot_look_valid(thermal_math, role, metrics):
    conf = {**CONF, "source_issues": {role: "unit changed after sensor replacement"}}
    states = status(thermal_math, conf=conf)
    for metric in metrics:
        assert states[metric]["status"] == "source_problem"
        assert states[metric]["reason"] == "unit changed after sensor replacement"
        assert states[metric]["next_action"]
    if role != "gas_meter":
        assert states["usage"]["status"] == "collecting"


@pytest.mark.parametrize("diagnostic,expected", [
    ({"status": "collecting", "reason": "fewer than 21 usable heating days", "usable_days": 9}, "collecting"),
    ({"status": "rejected", "reason": "insufficient temperature spread", "usable_days": 30}, "rejected"),
    (None, "rejected"),
])
def test_failed_hlc_keeps_reason_count_and_anchor(thermal_math, diagnostic, expected):
    state = status(thermal_math, diagnostics=diagnostic)["hlc"]
    assert state["status"] == expected
    assert state["model_data_through"] == DAY.isoformat()
    assert state["usable_observations"] == (diagnostic or {}).get("usable_days", 0)
    assert state["next_action"]


@pytest.mark.parametrize("q,dt,action", [
    ({}, {}, "gas meter"), ({DAY: 40}, {}, "temperature history"),
    ({DAY: 40}, {DAY: 10}, "heating activity"),
])
def test_missing_anchor_explains_which_evidence_is_needed(thermal_math, q, dt, action):
    state = status(thermal_math, q=q, dt=dt, anchor=None)["hlc"]
    assert state["status"] == "collecting"
    assert action in state["next_action"]
    assert state["usable_observations"] == len(set(q) & set(dt))


def test_stale_temperature_prevents_ready_heating_model(thermal_math):
    result = {"hlc": {"status": "valid", "days_used": 40}}
    state = status(thermal_math, result, dt={DAY - timedelta(days=20): 10})["hlc"]
    assert state["status"] == "source_problem"
    assert "temperature" in state["reason"]
    assert state["temperature_source_lag_days"] == 20
    assert state["source_lag_days"] == 0


def test_all_independent_models_have_evidence_and_scope(thermal_math):
    result = {
        "hlc": {"status": "provisional", "days_used": 25},
        "dhw": {"status": "provisional", "days_used": 10, "latest_complete_gas_day": DAY},
        "usage": {"heating_off_days": 10, "modelled_days": 15, "latest_complete_gas_day": DAY, "source_lag_days": 0},
        "electricity": {"current_period_days_used": 1, "days_used": 20, "latest_complete_day": DAY, "source_lag_days": 0},
        "water_usage": {"days_used": 20, "latest_complete_day": DAY, "source_lag_days": 0},
        "air_change_rate": {"windows": 15, "sensor_count": 2},
        "loft": {"hours_used": 150},
        "losses": {"windows": 15},
        "rooms": {"bed": {"nights_fitted": 3, "tau_median_h": 12, "last_night": DAY.isoformat()}},
    }
    states = status(thermal_math, result)
    for metric in ("hlc", "dhw", "usage", "electricity"):
        assert states[metric]["status"] == "provisional"
        assert states[metric]["usable_observations"] > 0
    for metric in ("water_usage", "air_change_rate", "loft", "losses"):
        assert states[metric]["status"] == "valid"
        assert states[metric]["usable_observations"] > 0
    assert states["rooms"]["bed"]["status"] == "provisional"
    # A physically impossible split must not inherit the upstream valid state.
    result.pop("losses")
    assert status(thermal_math, result)["losses"]["status"] == "rejected"
    result["losses_status"] = {"diagnostic_note": "ventilation exceeds total loss"}
    assert status(thermal_math, result)["losses"]["reason"] == "ventilation exceeds total loss"


def test_volume_and_temperature_configuration_are_required(thermal_math):
    assert status(thermal_math, conf={**CONF, "floor_area_m2": None})["losses"]["status"] == "not_configured"
    conf = {**CONF, "rooms": {"bed": {}}}
    assert status(thermal_math, conf=conf)["rooms"]["bed"]["status"] == "not_configured"
    conf = {**CONF, "source_issues": {"sensor.gas": "bad cumulative unit"}}
    assert status(thermal_math, conf=conf)["hlc"]["status"] == "source_problem"


def test_recorder_numeric_formats_and_invalid_values(thermal_math):
    ts = int(NOW.timestamp())
    rows = [
        {"start": NOW, "mean": "20"}, {"start": (ts + 3600) * 1000, "mean": 21},
        {"start": ts + 7200, "mean": None}, {"start": ts + 10800, "mean": float("nan")},
        {"start": ts + 14400, "mean": float("inf")}, {"start": ts + 18000, "mean": "broken"},
    ]
    assert thermal_math.series_from_stats(rows, "mean") == {ts: 20.0, ts + 3600: 21.0}


@pytest.mark.parametrize("gas,expected", [(8, "valid"), (100, "rejected")])
def test_positive_water_rate_reports_physical_rejection(thermal_math, gas, expected):
    days = [DAY - timedelta(days=n) for n in range(12)]
    diagnostics = {}
    fit = thermal_math.fit_dhw_water_rate(
        {d: gas for d in days}, {d: 200 for d in days}, {d: 18 for d in days},
        set(days), min(days), min_water_l=0, diagnostics=diagnostics,
    )
    assert diagnostics["status"] == expected
    assert diagnostics["usable_observations"] == 12
    assert (fit is not None) == (expected == "valid")


@pytest.mark.parametrize("mode", ["none", "sparse", "bad_fit", "future"])
def test_hourly_water_regression_rejection_diagnostics(thermal_math, mode):
    start = int(NOW.timestamp())
    gas, water = {start - 3600: 0.0}, {start - 3600: 0.0}
    g = w = 0.0
    for hour in range(250):
        increment = 5 + hour % 35
        w += increment
        g += 1 if mode == "bad_fit" else increment * 0.01
        gas[start + hour * 3600] = g
        water[start + hour * 3600] = w
    if mode == "none":
        water = {}
    elif mode == "sparse":
        water = dict(list(water.items())[:15])
    diag = {}
    fit = thermal_math.fit_water_gas(
        gas, water, tz=UTC, since=DAY + timedelta(days=40) if mode == "future" else None,
        diagnostics=diag,
    )
    assert fit is None
    assert diag["status"] == ("rejected" if mode == "bad_fit" else "collecting")
    assert diag["reason"]
    assert diag["eligible_observations"] < 200 or mode == "bad_fit"


@pytest.mark.parametrize("mode,expected", [("sparse", "collecting"), ("flat", "rejected"), ("negative", "rejected")])
def test_hlc_fit_explains_rejection_at_calculation_boundary(thermal_math, mode, expected):
    days = [DAY - timedelta(days=n) for n in range(40 if mode != "sparse" else 5)]
    dt = {d: 10 if mode == "flat" else 5 + n % 12 for n, d in enumerate(days)}
    q = {d: 120 - 3 * dt[d] if mode == "negative" else 5 * dt[d] for d in days}
    diagnostic = {}
    assert thermal_math.fit_hlc(q, dt, min(days), diagnostics=diagnostic) is None
    assert diagnostic["status"] == expected
    assert diagnostic["usable_days"] == len(days)
    assert diagnostic["reason"]


def test_reset_and_missing_room_hour_cannot_form_complete_day(thermal_math):
    ts = int(NOW.timestamp())
    meter = {ts - 3600: 0, **{ts + h * 3600: h + 1 for h in range(24)}}
    meter[ts + 10 * 3600] = 0
    assert thermal_math.daily_gas_kwh(meter, UTC) == {}
    out = {ts + h * 3600: 5 for h in range(24)}
    room = {ts + h * 3600: 20 for h in range(24) if h != 10}
    assert thermal_math.daily_delta_t([room], out, UTC) == {}


def test_empty_and_nonexponential_co2_never_publish_an_air_change_rate(thermal_math):
    assert thermal_math.air_change_rate({}, UTC, DAY) is None
    assert thermal_math.combine_air_change_rates([]) is None
    start = int(NOW.timestamp())
    # Linear decay reaches its baseline; treating it as exponential would be misleading.
    co2 = {start + h * 3600: 1000 - (h % 24) * 25 for h in range(24 * 12)}
    assert thermal_math.air_change_rate(co2, UTC, DAY, 420) is None


def test_invalid_room_source_suppresses_home_fit_without_erasing_usage(thermal_math):
    conf = {**CONF, "room_source_issues": {"bed": {"temperature": "incompatible unit"}}}
    result = thermal_math.compute_all({}, conf, UTC, NOW, (30,))
    assert result["hlc"] is None
    assert result["rooms"]["bed"] is None
    assert result["analysis_status"]["rooms"]["bed"]["status"] == "source_problem"
    assert result["analysis_status"]["usage"]["status"] == "collecting"


def test_invalid_heating_source_cannot_be_assumed_off(thermal_math):
    conf = {**CONF, "invalid_heating_power_entities": {"sensor.heat": "must be percent"}}
    result = thermal_math.compute_all({}, conf, UTC, NOW, (30,))
    assert result["rooms"]["bed"] is None
    assert result["analysis_status"]["rooms"]["bed"]["status"] == "source_problem"


@pytest.fixture
def sensor_module(monkeypatch):
    """Load sensor.py with minimal HA stubs so contract tests run standalone."""
    root = Path(__file__).resolve().parents[1]
    package_name = "custom_components.thermal_efficiency"

    def _stub(name: str, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    class CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

    class SensorEntity:
        pass

    class DeviceInfo:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    class SensorStateClass:
        MEASUREMENT = "measurement"

    pkg_root = types.ModuleType("custom_components")
    pkg_root.__path__ = [str(root / "custom_components")]
    monkeypatch.setitem(sys.modules, "custom_components", pkg_root)
    integration = types.ModuleType(package_name)
    integration.__path__ = [str(root / "custom_components" / "thermal_efficiency")]
    monkeypatch.setitem(sys.modules, package_name, integration)

    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub(
        "homeassistant.components.sensor",
        SensorEntity=SensorEntity,
        SensorStateClass=SensorStateClass,
    )
    _stub("homeassistant.config_entries", ConfigEntry=object)
    _stub("homeassistant.const", EntityCategory=EntityCategory)
    _stub("homeassistant.core", HomeAssistant=object)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.device_registry", DeviceInfo=DeviceInfo)
    _stub("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _stub("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)
    _stub(f"{package_name}.coordinator", ThermalCoordinator=type("ThermalCoordinator", (), {}))
    _stub(f"{package_name}.thermal_math", TAU_MIN_NIGHTS=3)

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.sensor",
        root / "custom_components" / "thermal_efficiency" / "sensor.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_data_readiness_preserves_all_source_issues_with_deterministic_keys(sensor_module):
    coordinator = SimpleNamespace(
        conf={
            "rooms": {
                "bed": {"name": "Master Bedroom", "temperature": "sensor.bed_temp"},
                "office": {"name": "Home Office", "temperature": "sensor.office_temp"},
            }
        },
        data={
            "analysis_status": {"hlc": {"status": "source_problem", "reason": "bad", "next_action": "fix"}},
            "source_issues": {
                "sensor.office_co2": "sensor.office_co2 had 1287 out-of-range CO2 hours in Home Office",
                "co2": "all CO2 sensors had quality warnings",
                "sensor.bed_co2": "sensor.bed_co2 had 2305 out-of-range CO2 hours in Master Bedroom",
                "loft": "sensor.loft_temp has gaps",
                "external:office_heat": "external:office_heat invalid unit in Home Office",
            },
        },
    )
    attrs = sensor_module.DataReadinessSensor(coordinator).extra_state_attributes
    issues = attrs["source_issues"]

    assert set(issues) == {"co2", "loft", "room_source_1", "room_source_2", "room_source_3"}
    assert issues["co2"] == "all CO2 sensors had quality warnings"
    assert issues["loft"] == "<source> has gaps"
    # Sorted non-global keys: external:office_heat -> room_source_1, sensor.bed_co2 -> room_source_2, sensor.office_co2 -> room_source_3
    assert issues["room_source_1"] == "<source> invalid unit in room_2"
    assert "2305" in issues["room_source_2"]
    assert "room_1" in issues["room_source_2"]
    assert "1287" in issues["room_source_3"]
    assert "room_2" in issues["room_source_3"]
    serialized = repr(issues)
    for secret in ("sensor.office_co2", "sensor.bed_co2", "sensor.loft_temp", "external:office_heat", "Master Bedroom", "Home Office"):
        assert secret not in serialized


def test_data_readiness_excludes_loft_from_conditioned_room_aliases(sensor_module):
    coordinator = SimpleNamespace(
        conf={
            "rooms": {
                "bed": {"name": "Bedroom", "room_type": "conditioned"},
                "dining": {"name": "Dining", "room_type": "conditioned"},
                "hall": {"name": "Hall", "room_type": "conditioned"},
                "kitchen": {"name": "Kitchen", "room_type": "conditioned"},
                "living": {"name": "Living", "room_type": "conditioned"},
                "loft": {"name": "Loft", "room_type": "loft"},
                "office": {"name": "Office", "room_type": "conditioned"},
                "attic": {"name": "Secret Attic", "room_type": "loft"},
            }
        },
        data={
            "analysis_status": {
                "loft": {
                    "status": "valid",
                    "reason": "loft resistance ratio established over 500 hours (Secret Attic)",
                    "next_action": "keep monitoring Loft",
                },
                "rooms": {
                    "bed": {"status": "valid"},
                    "dining": {"status": "valid"},
                    "hall": {"status": "valid"},
                    "kitchen": {"status": "valid"},
                    "living": {"status": "valid"},
                    "office": {
                        "status": "source_problem",
                        "reason": "Office sensor sensor.office_temp is stale",
                        "next_action": "check Office",
                    },
                },
            },
            "source_issues": {},
        },
    )
    attrs = sensor_module.DataReadinessSensor(coordinator).extra_state_attributes
    assert attrs["metrics"]["loft"]["reason"] == "loft resistance ratio established over 500 hours (loft)"
    assert attrs["metrics"]["loft"]["next_action"] == "keep monitoring Loft"
    assert "room_6" not in attrs["metrics"]["loft"]["reason"]
    assert attrs["metrics"]["rooms"]["room_6"]["reason"] == "room_6 sensor <source> is stale"
    assert attrs["metrics"]["rooms"]["room_6"]["next_action"] == "check room_6"
    assert "Secret Attic" not in repr(attrs)
    assert "Office" not in repr(attrs)


def test_data_readiness_surfaces_eligible_source_lag_days(sensor_module):
    coordinator = SimpleNamespace(
        conf={"rooms": {}},
        data={
            "analysis_status": {
                "hlc": {
                    "status": "source_problem",
                    "reason": "eligible days lag",
                    "next_action": "restore heating coverage",
                    "source_lag_days": 0,
                    "eligible_source_lag_days": 5,
                    "latest_source_day": "2026-06-30",
                    "model_data_through": "2026-06-25",
                },
                "dhw": {
                    "status": "provisional",
                    "reason": "ok",
                    "next_action": "wait",
                    "source_lag_days": 0,
                    "eligible_source_lag_days": 3,
                },
                "usage": {
                    "status": "valid",
                    "reason": "ok",
                    "next_action": "none",
                    "source_lag_days": 0,
                    "eligible_source_lag_days": 2,
                },
            },
            "source_issues": {},
        },
    )
    attrs = sensor_module.DataReadinessSensor(coordinator).extra_state_attributes
    assert attrs["metrics"]["hlc"]["source_lag_days"] == 0
    assert attrs["metrics"]["hlc"]["eligible_source_lag_days"] == 5
    assert attrs["metrics"]["dhw"]["eligible_source_lag_days"] == 3
    assert attrs["metrics"]["usage"]["eligible_source_lag_days"] == 2


def test_hlc_loft_ach_and_readiness_expose_phase4_and_lag_attributes(sensor_module):
    coordinator = SimpleNamespace(
        conf={
            "experimental_whole_home_ventilation": True,
            "rooms": {"bed": {"name": "Master Bedroom", "room_type": "conditioned"}},
        },
        data={
            "hlc": {
                "hlc_w_per_k": 264.34,
                "r_squared": 0.89,
                "days_used": 60,
                "window_days": 120,
                "hlc_ci_low_w_per_k": 245.1,
                "hlc_ci_high_w_per_k": 283.6,
                "residual_autocorrelation": 0.15,
                "effective_independent_days": 45,
                "free_gains_w": 420,
                "fuel_input_hlc_w_per_k": 300.4,
                "space_heating_fuel_input_hlc_w_per_k": 295.2,
                "boiler_efficiency_used": 0.88,
                "regression_intercept_kwh_per_day": 1.4,
                "dhw_baseline_kwh_per_day": 1.1,
                "solar_adjusted": True,
                "unadjusted_hlc_w_per_k": 306.44,
                "solar_gain_coefficient_kwh_per_day": -8.254,
                "winter_hlc_w_per_k": 261.87,
            },
            "loft": {
                "ratio": 0.095,
                "hours_used": 410,
                "window_days": 120,
                "iqr": 0.042,
                "out_of_range_pct": 0.0,
                "cold_night_gate": True,
            },
            "air_change_rate": {
                "ach": 0.382,
                "windows": 18,
                "baseline_ppm": 495.0,
                "co2_sensors_used": 2,
                "baseline_source": "indoor low-percentile (configured baseline below sensor floor)",
                "cadence": "5minute",
                "baseline_mismatch_note": "Configured outdoor CO2 (400 ppm) is >50 ppm below sensor p02",
            },
            "losses": {
                "ach": 0.382,
                "ventilation_w_per_k": 32.0,
                "fabric_w_per_k": 232.3,
                "ventilation_share_pct": 12.1,
                "hlc_delivered_w_per_k": 264.3,
                "boiler_efficiency_used": 0.88,
                "cadence": "5minute",
                "baseline_mismatch_note": "Configured outdoor CO2 (400 ppm) is >50 ppm below sensor p02",
            },
            "analysis_status": {
                "hlc": {
                    "status": "valid",
                    "reason": "HLC fitted",
                    "next_action": "keep monitoring",
                    "source_lag_days": 0,
                    "eligible_source_lag_days": 4,
                    "eligibility_degraded": True,
                    "eligible_lag_warning": "Master Bedroom sensor.bed_heat expected_heating_gap delayed eligibility by 4 days",
                },
                "dhw": {
                    "status": "valid",
                    "reason": "DHW fitted",
                    "next_action": "keep monitoring",
                    "source_lag_days": 0,
                    "eligible_source_lag_days": 0,
                    "eligibility_degraded": False,
                    "eligible_lag_warning": None,
                },
            },
            "source_issues": {},
        },
    )

    hlc_attrs = sensor_module.HlcSensor(coordinator).extra_state_attributes
    assert hlc_attrs["solar_adjusted"] is True
    assert hlc_attrs["unadjusted_hlc_w_per_k"] == 306.4
    assert hlc_attrs["solar_gain_coefficient_kwh_per_day"] == -8.25
    assert hlc_attrs["winter_hlc_w_per_k"] == 261.9
    assert hlc_attrs["eligibility_degraded"] is True
    assert "expected_heating_gap" in hlc_attrs["eligible_lag_warning"]

    loft_attrs = sensor_module.LoftSensor(coordinator).extra_state_attributes
    assert loft_attrs["cold_night_gate"] is True

    ach_attrs = sensor_module.AirChangeRateSensor(coordinator).extra_state_attributes
    assert ach_attrs["cadence"] == "5minute"
    assert ach_attrs["co2_baseline_source"] == "indoor low-percentile (configured baseline below sensor floor)"
    assert ach_attrs["baseline_mismatch_note"] == "Configured outdoor CO2 (400 ppm) is >50 ppm below sensor p02"

    vent_attrs = sensor_module.VentilationLossSensor(coordinator).extra_state_attributes
    assert vent_attrs["cadence"] == "5minute"
    assert vent_attrs["baseline_mismatch_note"] == "Configured outdoor CO2 (400 ppm) is >50 ppm below sensor p02"

    readiness_attrs = sensor_module.DataReadinessSensor(coordinator).extra_state_attributes
    assert readiness_attrs["metrics"]["hlc"]["eligibility_degraded"] is True
    assert readiness_attrs["metrics"]["hlc"]["eligible_lag_warning"] == "room_1 <source> expected_heating_gap delayed eligibility by 4 days"
    assert readiness_attrs["metrics"]["dhw"]["eligibility_degraded"] is False
    assert readiness_attrs["metrics"]["dhw"]["eligible_lag_warning"] is None
    assert "Master Bedroom" not in repr(readiness_attrs)
    assert "sensor.bed_heat" not in repr(readiness_attrs)


def test_hlc_unadjusted_handles_none_solar_and_winter_fields(sensor_module):
    coordinator = SimpleNamespace(
        conf={"rooms": {}},
        data={
            "hlc": {
                "hlc_w_per_k": 280.0,
                "r_squared": 0.81,
                "days_used": 30,
                "window_days": 60,
                "hlc_ci_low_w_per_k": 250.0,
                "hlc_ci_high_w_per_k": 310.0,
                "residual_autocorrelation": 0.1,
                "effective_independent_days": 25,
                "free_gains_w": 300,
                "fuel_input_hlc_w_per_k": 318.2,
                "space_heating_fuel_input_hlc_w_per_k": 318.2,
                "boiler_efficiency_used": 0.88,
                "regression_intercept_kwh_per_day": 1.0,
                "dhw_baseline_kwh_per_day": None,
                "solar_adjusted": False,
                "unadjusted_hlc_w_per_k": 280.0,
                "solar_gain_coefficient_kwh_per_day": None,
                "winter_hlc_w_per_k": None,
            },
            "analysis_status": {"hlc": {"status": "provisional", "reason": "ok", "next_action": "wait"}},
        },
    )
    hlc_attrs = sensor_module.HlcSensor(coordinator).extra_state_attributes
    assert hlc_attrs["solar_adjusted"] is False
    assert hlc_attrs["unadjusted_hlc_w_per_k"] == 280.0
    assert hlc_attrs["solar_gain_coefficient_kwh_per_day"] is None
    assert hlc_attrs["winter_hlc_w_per_k"] is None

