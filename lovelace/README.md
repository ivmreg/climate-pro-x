# Lovelace Dashboards for Thermal Efficiency

This directory provides portable Home Assistant dashboards generated from one editable mapping file:

- `thermal_efficiency_dashboard.yaml`: Comprehensive Thermal Storyboard dashboard with HLC evidence, loss Sankey flow, and dynamic room cooling fingerprints.
- `thermal_efficiency_live.yaml`: Sections-view dashboard with real-time gauges, status fallback tiles, and trend charts.
- `dashboard_mapping.json`: the single source for entity IDs and arbitrary conditioned-room names.

---

## 1. Portable Dashboard Generator

Home Assistant entity IDs vary across installations depending on naming and registry history. Instead of manually editing hundreds of lines of YAML or repeating a fixed six-room roster:

1. **Edit one mapping file:** Edit `lovelace/dashboard_mapping.json` to map the integration's entity IDs from your own entity registry and list your conditioned rooms. An empty room list is supported and displays a setup note.
2. **Run the token-free generator:**
   ```bash
   python scripts/generate_dashboard.py --mapping lovelace/dashboard_mapping.json
   ```
   This regenerates both `lovelace/thermal_efficiency_dashboard.yaml` and `lovelace/thermal_efficiency_live.yaml`.

### Customizing Paths
You can specify custom mapping and output paths:
```bash
python scripts/generate_dashboard.py \
  --mapping my_home_mapping.json \
  --storyboard my_storyboard.yaml \
  --live my_live.yaml
```

---

## 2. Mapping Format (`dashboard_mapping.json`)

The mapping file defines whole-home metric entities and an arbitrary list of conditioned rooms:

```json
{
  "home": {
    "data_readiness": "sensor.thermal_efficiency_data_readiness",
    "hlc": "sensor.thermal_efficiency_heat_loss_coefficient",
    "loft_ratio": "sensor.thermal_efficiency_loft_ratio",
    "air_change_rate": "sensor.thermal_efficiency_air_change_rate",
    "fabric_loss": "sensor.thermal_efficiency_fabric_heat_loss",
    "ventilation_loss": "sensor.thermal_efficiency_ventilation_heat_loss",
    "hot_water_gas": "sensor.thermal_efficiency_hot_water_gas",
    "hot_water_gas_7d": "sensor.thermal_efficiency_hot_water_gas_7_day_average",
    "space_heating_gas_7d": "sensor.thermal_efficiency_space_heating_gas_7_day_average",
    "electricity_baseload": "sensor.thermal_efficiency_electricity_baseload",
    "electricity_use_7d": "sensor.thermal_efficiency_electricity_use_7_day_average",
    "water_use_7d": "sensor.thermal_efficiency_total_water_use_7_day_average",
    "live_electricity_power": null,
    "daily_water_meter": null
  },
  "rooms": []
}
```

Add objects to `rooms` after confirming each generated room time-constant
entity ID in your Home Assistant registry. Keep arbitrary names as supplied;
do not use this snippet's empty room list as a required format.

### Integration Unique ID Lookup
To map integration entities on your Home Assistant instance, check **Settings → Devices & Services → Entities** and filter by the **Thermal Efficiency** integration:

| Measurement | Integration Unique ID | Standard Entity ID |
|---|---|---|
| Delivered HLC | `thermal_efficiency_hlc` | `sensor.thermal_efficiency_heat_loss_coefficient` |
| Loft Coupling | `thermal_efficiency_loft_ratio` | `sensor.thermal_efficiency_loft_ratio` |
| Air Change Rate | `thermal_efficiency_air_change_rate` | `sensor.thermal_efficiency_air_change_rate` |
| Fabric Heat Loss | `thermal_efficiency_fabric_loss` | `sensor.thermal_efficiency_fabric_heat_loss` |
| Ventilation Loss | `thermal_efficiency_ventilation_loss` | `sensor.thermal_efficiency_ventilation_heat_loss` |
| Hot Water Baseline | `thermal_efficiency_hot_water_gas` | `sensor.thermal_efficiency_hot_water_gas` |
| Hot Water Gas (7d) | `thermal_efficiency_hot_water_usage_7d` | `sensor.thermal_efficiency_hot_water_gas_7_day_average` |
| Space Heating (7d) | `thermal_efficiency_space_heating_usage_7d` | `sensor.thermal_efficiency_space_heating_gas_7_day_average` |
| Baseload Electricity | `thermal_efficiency_electricity_baseload` | `sensor.thermal_efficiency_electricity_baseload` |
| Electricity Use (7d) | `thermal_efficiency_electricity_usage_7d` | `sensor.thermal_efficiency_electricity_use_7_day_average` |
| Water Use (7d) | `thermal_efficiency_water_usage_7d` | `sensor.thermal_efficiency_total_water_use_7_day_average` |
| Room Time Constant | `thermal_efficiency_<room>_tau` | `sensor.thermal_efficiency_<room>_time_constant` |

---

## 3. Dynamic Roster and Safety Features

- **Arbitrary Room Roster:** You can configure any number of rooms. Room names are encoded as JSON data for JavaScript and Jinja and safely support apostrophes, quotes, colons, line breaks and Unicode. An empty roster remains usable.
- **Dynamic Frontend Sorting:** The Plotly chart dynamically sorts all rooms from fastest to slowest cooling (`b.value - a.value`), coloring bars dynamically according to rank.
- **Badge Visibility Conditions:** All optional badges in `thermal_efficiency_live.yaml` include visibility conditions to hide cleanly when entities are `unknown` or `unavailable`.
- **Readiness and model scope:** Both dashboards show readiness status, HLC source reason/action, available observation counts, `model_data_through`, source lag and whether the historical baseline is held. The HLC copy explains that complete local days have 23, 24 or 25 hours across daylight-saving changes; partial days are excluded.
- **Loss Scope & Experimental Limits:** Whole-home fabric/ventilation split cards carry an `[EXPERIMENTAL]` label and explain that they need `experimental_whole_home_ventilation: true`. The integration option is off by default.
- **Optional sources:** `live_electricity_power` and `daily_water_meter` may be absent, null or mapped. Their badges and cards are only generated when mapped.
- **Safe serialization:** Entity IDs are validated; room names are preserved as data through JSON serialization and YAML block/double-quoted scalars instead of being rejected by a punctuation blacklist.

---

## 4. Required Frontend Dependencies

Ensure the following custom cards are installed via HACS (or registered in **Settings → Dashboards → Resources**):
- **`apexcharts-card`** (`type: custom:apexcharts-card`)
- **`lovelace-plotly-graph-card`** (`type: custom:plotly-graph`)

No additional JavaScript resources or external dependencies are required.

`lovelace/dashboard_mapping.json` contains canonical integration entity IDs as an editable starting point; confirm every ID against **Settings → Devices & Services → Entities** before use. The optional sources are null, and no room IDs are assumed. After editing the mapping, regenerate the checked-in examples and run `pytest tests/test_dashboard_generator.py` to validate YAML parsing, template rendering, JavaScript state handling and byte-for-byte generation.

---

## 5. Deployment

1. Open your Home Assistant dashboard.
2. Click the top-right menu (three dots) → **Edit Dashboard**.
3. Click the top-right menu again → **Raw configuration editor**.
4. Paste the generated contents of `thermal_efficiency_dashboard.yaml` or `thermal_efficiency_live.yaml` and click **Save**.
