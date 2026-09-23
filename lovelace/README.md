# Lovelace Dashboards for Thermal Efficiency

This directory provides dashboard configurations for visualizing the thermal efficiency models:

- `thermal_efficiency_live.yaml`: Sections-view dashboard with real-time gauges, status fallback tiles, and trend charts.
- `thermal_efficiency_dashboard.yaml`: Comprehensive Thermal Storyboard dashboard.

## Installation-Specific Entity Mapping

The dashboard YAML files are installation-specific. Entity IDs in these files reflect the local Home Assistant entity registry where integration unique IDs may map to custom object IDs (for example, with a `metahome_` prefix).

Before deploying these dashboard configurations on another Home Assistant instance, map the dashboard entities to your instance's corresponding entity IDs using the table below:

| Measurement | Integration Unique ID | Default Entity ID | Example Dashboard Entity ID |
|---|---|---|---|
| Delivered HLC | `thermal_efficiency_hlc` | `sensor.thermal_efficiency_heat_loss_coefficient` | `sensor.thermal_efficiency_heat_loss_coefficient` |
| Loft Coupling | `thermal_efficiency_loft_ratio` | `sensor.thermal_efficiency_loft_ratio` | `sensor.thermal_efficiency_loft_ratio` |
| Air Infiltration | `thermal_efficiency_air_change_rate` | `sensor.thermal_efficiency_air_change_rate` | `sensor.metahome_thermal_efficiency_air_change_rate` |
| Fabric Heat Loss | `thermal_efficiency_fabric_loss` | `sensor.thermal_efficiency_fabric_heat_loss` | `sensor.metahome_thermal_efficiency_fabric_heat_loss` |
| Ventilation Loss | `thermal_efficiency_ventilation_loss` | `sensor.thermal_efficiency_ventilation_heat_loss` | `sensor.metahome_thermal_efficiency_ventilation_heat_loss` |
| Hot Water Gas (7d) | `thermal_efficiency_hot_water_gas_7d` | `sensor.thermal_efficiency_hot_water_gas_7_day_average` | `sensor.metahome_thermal_efficiency_hot_water_gas_7_day_average` |
| Space Heating (7d) | `thermal_efficiency_space_heating_gas_7d` | `sensor.thermal_efficiency_space_heating_gas_7_day_average` | `sensor.metahome_thermal_efficiency_space_heating_gas_7_day_average` |
| Baseload Electricity | `thermal_efficiency_electricity_baseload` | `sensor.thermal_efficiency_electricity_baseload` | `sensor.metahome_thermal_efficiency_electricity_baseload` |
| Electricity (7d) | `thermal_efficiency_electricity_7d` | `sensor.thermal_efficiency_electricity_use_7_day_average` | `sensor.metahome_thermal_efficiency_electricity_use_7_day_average` |
| Water Use (7d) | `thermal_efficiency_water_7d` | `sensor.thermal_efficiency_total_water_use_7_day_average` | `sensor.metahome_thermal_efficiency_total_water_use_7_day_average` |

You can find and adjust your instance's entity IDs under **Settings → Devices & Services → Entities** by filtering for the **Thermal Efficiency** integration.

## Required External Dependencies

### Custom Frontend Cards
Ensure the following Lovelace cards are installed (via HACS or registered under dashboard resources):
- `apexcharts-card` (`type: custom:apexcharts-card`)
- `lovelace-plotly-graph-card` (`type: custom:plotly-graph`)

### External Household Entities
The dashboards reference the following non-integration household entities that should be updated to match your local installation:
- **Live electricity demand**: `sensor.smart_meter_electricity_power` (or your smart meter's instantaneous power sensor)
- **Daily water consumption**: `sensor.thames_water_sensor` (or your water utility/meter sensor)
- **Room time constants**: `sensor.thermal_efficiency_<room>_time_constant` (configured per conditioned room in the Plotly fingerprints card)

> [!NOTE]
> Do not commit private tokens, access URLs, or installation-specific private credentials when sharing dashboards.
