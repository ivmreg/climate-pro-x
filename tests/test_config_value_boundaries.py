"""Non-finite physical inputs are rejected through YAML and flow preflight."""
import pytest
import voluptuous as vol

from custom_components.thermal_efficiency import CONFIG_SCHEMA
from custom_components.thermal_efficiency.validation import validate_global_sources


@pytest.mark.parametrize("field", [
    "floor_area_m2", "ceiling_height_m", "boiler_efficiency",
    "outdoor_co2_ppm", "min_dhw_water_litres", "max_window_days",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_setting_cannot_enter_a_model(field, value):
    config = {
        "thermal_efficiency": {
            "outdoor": "sensor.outdoor",
            "rooms": {"main": {"temperature": "sensor.temperature"}},
            field: value,
        },
    }
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)
    assert validate_global_sources(None, {field: value}, {}) == {field: "invalid_value"}


@pytest.mark.parametrize("value", ["bad", None, 29, 731, 30.5])
def test_invalid_history_window_has_an_actionable_error(value):
    assert validate_global_sources(None, {"max_window_days": value}, {}) == {"max_window_days": "invalid_value"}


def test_zero_occupancy_threshold_and_finite_physical_boundaries_are_accepted():
    assert validate_global_sources(None, {
        "min_dhw_water_litres": 0, "boiler_efficiency": 1,
        "floor_area_m2": 1, "ceiling_height_m": 1.8,
        "outdoor_co2_ppm": 350, "max_window_days": 730,
    }, {}) == {}
