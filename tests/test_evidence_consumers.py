"""Consumer checks for compact readiness and private analytical diagnostics."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "custom_components.thermal_efficiency"


def _module(monkeypatch, name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def consumers(monkeypatch):
    """Load the adapters with small HA interface stubs, without HA installed."""
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

    package = types.ModuleType("custom_components")
    package.__path__ = [str(ROOT / "custom_components")]
    monkeypatch.setitem(sys.modules, "custom_components", package)
    integration = types.ModuleType(PACKAGE)
    integration.__path__ = [str(ROOT / "custom_components" / "thermal_efficiency")]
    monkeypatch.setitem(sys.modules, PACKAGE, integration)

    _module(monkeypatch, "homeassistant")
    _module(monkeypatch, "homeassistant.components")
    _module(
        monkeypatch,
        "homeassistant.components.sensor",
        SensorEntity=SensorEntity,
        SensorStateClass=SensorStateClass,
    )
    _module(monkeypatch, "homeassistant.config_entries", ConfigEntry=object)
    _module(monkeypatch, "homeassistant.const", EntityCategory=EntityCategory)
    _module(monkeypatch, "homeassistant.core", HomeAssistant=object)
    _module(monkeypatch, "homeassistant.helpers")
    _module(
        monkeypatch,
        "homeassistant.helpers.device_registry",
        DeviceInfo=DeviceInfo,
    )
    _module(
        monkeypatch,
        "homeassistant.helpers.entity_platform",
        AddEntitiesCallback=object,
    )
    _module(
        monkeypatch,
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
    )
    _module(
        monkeypatch,
        f"{PACKAGE}.coordinator",
        ThermalCoordinator=type("ThermalCoordinator", (), {}),
    )
    _module(monkeypatch, f"{PACKAGE}.thermal_math", TAU_MIN_NIGHTS=3)

    def load(name: str, filename: str):
        spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.{name}", ROOT / "custom_components" / "thermal_efficiency" / filename
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    return SimpleNamespace(sensor=load("sensor", "sensor.py"), diagnostics=load("diagnostics", "diagnostics.py"))


def _coordinator(data, conf=None):
    return SimpleNamespace(data=data, conf=conf or {})


def test_numeric_thermal_values_keep_final_status_and_compact_attributes(consumers):
    evidence = {
        "schema_version": 1,
        "daily": [{"date": "2026-01-01", "gas_kwh": 4}],
        "private_marker": "must remain diagnostics-only",
    }
    coordinator = _coordinator(
        {
            "analysis_evidence": evidence,
            "hlc": {
                "hlc_w_per_k": 310.0,
                "r_squared": 0.8,
                "days_used": 45,
                "window_days": 90,
                "hlc_ci_low_w_per_k": 280.0,
                "hlc_ci_high_w_per_k": 340.0,
                "residual_autocorrelation": 0.2,
                "effective_independent_days": 30,
                "free_gains_w": 500,
                "fuel_input_hlc_w_per_k": 350.0,
                "space_heating_fuel_input_hlc_w_per_k": 360.0,
                "boiler_efficiency_used": 0.88,
                "regression_intercept_kwh_per_day": 2.0,
                "dhw_baseline_kwh_per_day": 1.2,
            },
            "losses": {
                "ventilation_w_per_k": 35.0,
                "fabric_w_per_k": 75.0,
                "ventilation_share_pct": 31.8,
                "ach": 0.5,
                "hlc_delivered_w_per_k": 110.0,
                "boiler_efficiency_used": 0.88,
            },
            "analysis_status": {
                "hlc": {
                    "status": "source_problem",
                    "reason": "latest outdoor temperature history is incomplete",
                    "next_action": "restore recent outdoor temperature readings",
                    "latest_source_day": "2026-06-20",
                    "raw_latest_source_day": "2026-06-20",
                    "latest_eligible_day": "2026-06-19",
                    "eligible_source_lag_days": 3,
                    "raw_complete_gas_days": 40,
                    "raw_complete_temperature_days": 37,
                    "eligible_observations": 28,
                    "exclusion_counts": {"expected_heating_gap": 9},
                    "boiler_efficiency_sensitivity": {
                        "low_efficiency": 0.83,
                        "high_efficiency": 0.93,
                        "low_hlc_w_per_k": 292.0,
                        "high_hlc_w_per_k": 327.0,
                        "delta_low_w_per_k": -18.0,
                        "delta_high_w_per_k": 17.0,
                        "efficiency_bounds": [0.5, 1.0],
                    },
                },
                "losses": {
                    "status": "source_problem",
                    "reason": "dependent HLC source is stale",
                    "next_action": "restore recent aligned temperature history",
                    "dependencies": {"hlc": "source_problem", "air_change_rate": "valid"},
                },
            },
        },
        {"experimental_whole_home_ventilation": True},
    )

    hlc = consumers.sensor.HlcSensor(coordinator)
    hlc_attrs = hlc.extra_state_attributes
    assert hlc.native_value == 310.0
    assert hlc_attrs["status"] == "source_problem"
    assert "incomplete" in hlc_attrs["reason"]
    assert hlc_attrs["next_action"] == "restore recent outdoor temperature readings"
    assert hlc_attrs["raw_latest_source_day"] == "2026-06-20"
    assert hlc_attrs["latest_eligible_day"] == "2026-06-19"
    assert hlc_attrs["eligible_source_lag_days"] == 3
    assert hlc_attrs["exclusion_counts"]["expected_heating_gap"] == 9
    assert "conditional regression interval" in hlc_attrs["confidence_interval_note"]
    assert hlc_attrs["boiler_efficiency_sensitivity"]["low_hlc_w_per_k"] == 292.0
    assert "analysis_evidence" not in hlc_attrs
    assert "must remain diagnostics-only" not in repr(hlc_attrs)

    loss_attrs = consumers.sensor.VentilationLossSensor(coordinator).extra_state_attributes
    assert loss_attrs["status"] == "source_problem"
    assert loss_attrs["note"] == "dependent HLC source is stale"
    assert loss_attrs["next_action"] == "restore recent aligned temperature history"
    assert loss_attrs["dependencies"]["hlc"] == "source_problem"


def test_readiness_keeps_held_thermal_baseline_visible_with_valid_metrics(consumers):
    coordinator = _coordinator(
        {
            "analysis_status": {
                "hlc": {"status": "historical_baseline_held"},
                "dhw": {"status": "valid"},
                "electricity": {"status": "valid"},
                "rooms": {"room_1": {"status": "valid"}},
            }
        }
    )
    readiness = consumers.sensor.DataReadinessSensor(coordinator)
    assert readiness.native_value == "historical_baseline_held"


def test_other_metric_sensors_keep_canonical_status_and_tau_counts(consumers):
    coordinator = _coordinator(
        {
            "analysis_status": {
                "dhw": {"status": "collecting", "reason": "no qualifying days", "next_action": "wait"},
                "usage": {"status": "source_problem", "reason": "gas history gap", "next_action": "restore gas history"},
                "electricity": {"status": "rejected", "reason": "counter reset", "next_action": "repair meter history"},
                "water_usage": {"status": "collecting", "reason": "only two days", "next_action": "wait for more days"},
                "rooms": {
                    "bed": {
                        "status": "provisional",
                        "reason": None,
                        "next_action": "collect more nights",
                        "sensor_count": 2,
                        "temperature_sensor_count": 1,
                        "heating_sensor_count": 1,
                        "latest_temperature_day": "2026-06-29",
                        "latest_heating_day": "2026-06-28",
                        "latest_demand_day": "2026-06-28",
                        "candidate_nights": 8,
                        "rejected_nights": 4,
                        "rejection_counts": {"poor_fit": 2, "heating_not_confirmed_off": 2},
                    }
                },
            },
            "electricity_status": {"configured": True, "usable_days": 4, "required_days": 14},
            "water_status": {"configured": True, "usable_days": 2, "required_days": 5},
            "rooms": {
                "bed": {
                    "tau_median_h": 13.2,
                    "nights_fitted": 4,
                    "first_night": "2026-06-01",
                    "last_night": "2026-06-29",
                    "tau_q1_h": 12.0,
                    "tau_q3_h": 14.0,
                    "window_days": 30,
                }
            },
        },
        {"rooms": {"bed": {}}},
    )

    dhw_attrs = consumers.sensor.HotWaterGasSensor(coordinator).extra_state_attributes
    assert dhw_attrs["status"] == "collecting"
    assert dhw_attrs["next_action"] == "wait"
    usage_attrs = consumers.sensor.HotWaterUsageSensor(coordinator).extra_state_attributes
    assert usage_attrs["status"] == "source_problem"
    assert usage_attrs["reason"] == "gas history gap"
    assert usage_attrs["next_action"] == "restore gas history"
    elec_attrs = consumers.sensor.ElectricityBaseloadSensor(coordinator).extra_state_attributes
    assert elec_attrs["status"] == "rejected"
    assert elec_attrs["next_action"] == "repair meter history"
    water_attrs = consumers.sensor.WaterUsageSensor(coordinator).extra_state_attributes
    assert water_attrs["status"] == "collecting"
    assert water_attrs["next_action"] == "wait for more days"

    tau_attrs = consumers.sensor.RoomTauSensor(coordinator, "bed").extra_state_attributes
    assert tau_attrs["status"] == "provisional"
    assert tau_attrs["sensor_count"] == 2
    assert tau_attrs["temperature_sensor_count"] == 1
    assert tau_attrs["heating_sensor_count"] == 1
    assert tau_attrs["latest_temperature_day"] == "2026-06-29"
    assert tau_attrs["latest_heating_day"] == "2026-06-28"
    assert tau_attrs["latest_demand_day"] == "2026-06-28"
    assert tau_attrs["first_night"] == "2026-06-01"
    assert tau_attrs["tau_q1_h"] == 12.0
    assert tau_attrs["tau_q3_h"] == 14.0
    assert tau_attrs["candidate_nights"] == 8
    assert tau_attrs["rejected_nights"] == 4
    assert tau_attrs["rejection_counts"]["poor_fit"] == 2


async def test_diagnostics_export_sanitized_copy_of_evidence_and_safe_failure(consumers):
    room_name = "Secret Bedroom"
    custom_source = "meter-secret"
    entity_id = "sensor.secret_meter"
    evidence = {
        "schema_version": 1,
        "generated_at": "2026-10-26T12:00:00+00:00",
        "timezone": "Europe/London",
        "model_version": "1",
        "history_revision": 8,
        "configuration": {
            "boiler_efficiency": 0.88,
            "room_alias": room_name,
            "meter": custom_source,
        },
        "daily": [
            {
                "date": "2026-06-01",
                "gas_kwh": 4.2,
                "exclusions": [f"{room_name} missing {entity_id} ({custom_source})"],
            }
        ],
        "metrics": {
            "hlc": {
                "status": "valid",
                "reason": f"valid room named 1: {room_name} ({entity_id})",
            },
            "rooms": {
                "bed": {"status": "provisional"},
                "configuration": {"status": "valid"},
                "valid": {"status": "collecting"},
                "1": {"status": "valid"},
            },
        },
        "uncertainty": {"hlc": "conditional regression interval"},
        entity_id: "private source-key value",
    }
    history = SimpleNamespace(
        data={
            "version": 1,
            "revision": 8,
            "rooms": {"bed": {}},
            "sources": {"source_a": {"entity_id": entity_id}},
            "streams": {},
            "migration": {
                "failure": {
                    "stage": "hour_import",
                    "code": "missing_chunk",
                    "type": "recoverable",
                    "source": entity_id,
                    "source_index": 1,
                    "chunk": f"{room_name}-chunk-1",
                    "retryable": True,
                    "reason": f"unable to read {entity_id} in {room_name}",
                    "guidance": f"retry import for {room_name}",
                    "raw_exception": f"secret {entity_id}",
                }
            },
        },
        config={
            "rooms": {
                "bed": {"name": room_name, "temperature": "sensor.room_temp"},
                "other": {"name": "configuration", "temperature": "sensor.other_room"},
                "valid_room": {"name": "valid", "temperature": "sensor.valid_room"},
                "number_room": {"name": "1", "temperature": "sensor.number_room"},
            }
        },
    )
    coordinator = SimpleNamespace(
        conf={
            "rooms": {
                "bed": {"name": room_name, "temperature": "sensor.room_temp"},
                "other": {"name": "configuration", "temperature": "sensor.other_room"},
                "valid_room": {"name": "valid", "temperature": "sensor.valid_room"},
                "number_room": {"name": "1", "temperature": "sensor.number_room"},
            },
            "gas_meter": entity_id,
        },
        data={"analysis_evidence": evidence},
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(history=history, coordinator=coordinator),
        data={},
        options={},
        version=1,
    )

    result = await consumers.diagnostics.async_get_config_entry_diagnostics(None, entry)
    exported = result["analysis_evidence"]
    serialized = repr(exported)
    for private_value in (
        room_name,
        custom_source,
        entity_id,
        "sensor.room_temp",
        "sensor.other_room",
        "sensor.valid_room",
        "sensor.number_room",
        "Europe/London",
    ):
        assert private_value not in serialized
    assert exported is not evidence
    # A room named like a schema key must not rename the root evidence object.
    assert "configuration" in exported
    assert set(exported["metrics"]["rooms"]) == {
        "room_1",
        "room_2",
        "room_3",
        "room_4",
    }
    assert exported["metrics"]["hlc"]["status"] == "valid"
    assert "valid room" not in exported["metrics"]["hlc"]["reason"]
    assert "named 1" not in exported["metrics"]["hlc"]["reason"]
    assert exported["model_version"] == "1"
    assert exported["timezone"] == "local"
    assert exported["daily"][0]["date"] == "2026-06-01"
    assert json.loads(json.dumps(exported))["schema_version"] == 1
    assert result["migration"]["failure"]["retryable"] is True
    assert result["migration"]["failure"]["source_index"] == 1
    assert result["migration"]["failure"]["code"] == "missing_chunk"
    assert "guidance" in result["migration"]["failure"]
    assert "raw_exception" not in result["migration"]["failure"]
    assert entity_id not in repr(result["migration"]["failure"])

    exported["daily"][0]["date"] = "mutated"
    assert evidence["daily"][0]["date"] == "2026-06-01"
