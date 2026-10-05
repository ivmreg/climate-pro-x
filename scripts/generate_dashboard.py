#!/usr/bin/env python3
"""Dashboard generator for Climate Pro X.

Generates portable Home Assistant Lovelace dashboards from a single JSON mapping source:
- lovelace/thermal_efficiency_dashboard.yaml (Thermal Storyboard)
- lovelace/thermal_efficiency_live.yaml (Live Overview)

Features:
- Validates entity IDs against 'domain.entity_id' format.
- Uses proper JSON serialization for room names and entity IDs to safely support
  apostrophes, colons, double quotes, and Unicode names without template injection.
- Normalizes display whitespace while preserving safely serialized room labels.
- Ensures empty string/null states are not falsely converted to 0 in JavaScript.
- Includes data_readiness sensor and readiness reporting cards.
- Prominently displays HLC date scope (model_data_through, lag, historical_baseline_held).
- Labels whole-home loss split as [EXPERIMENTAL] and notes prerequisite flag.
- Truly optional live_power and daily_water: omitted when absent or null.
- Gracefully handles empty room rosters with informative guidance.
- Removes unsupported categorical retrofit or payback claims.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

REQUIRED_HOME_METRICS = {
    "data_readiness": "Data Readiness Diagnostic",
    "hlc": "Delivered HLC",
    "loft_ratio": "Loft Coupling Ratio",
    "air_change_rate": "Air Change Rate",
    "fabric_loss": "Fabric Heat Loss",
    "ventilation_loss": "Ventilation Heat Loss",
    "hot_water_gas": "Hot Water Gas Baseline",
    "hot_water_gas_7d": "Hot Water Gas (7-Day Average)",
    "space_heating_gas_7d": "Space Heating Gas (7-Day Average)",
    "electricity_baseload": "Baseload Electricity",
    "electricity_use_7d": "Electricity Use (7-Day Average)",
    "water_use_7d": "Water Use (7-Day Average)",
}

OPTIONAL_EXTERNAL_METRICS = {
    "live_electricity_power",
    "daily_water_meter",
}


def validate_entity_id(entity_id: str, field_name: str) -> None:
    if not isinstance(entity_id, str) or not ENTITY_ID_RE.match(entity_id):
        raise ValueError(
            f"Invalid entity ID for {field_name}: '{entity_id}'. Must match 'domain.entity_id'."
        )


def validate_room_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Room name must be a non-empty string.")
    # Names are data, not source: the generator serializes them as JSON for
    # JavaScript/Jinja and escapes apostrophes for YAML single-quoted scalars.


def validate_mapping(data: Any) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    if not isinstance(data, dict):
        raise ValueError("Mapping configuration must be a JSON object.")

    home = data.get("home")
    if not isinstance(home, dict):
        raise ValueError("Mapping configuration missing 'home' object.")

    validated_home: dict[str, str | None] = {}
    for key, label in REQUIRED_HOME_METRICS.items():
        if key not in home or home[key] is None:
            raise ValueError(f"Mapping 'home' missing required metric: '{key}' ({label}).")
        entity_id = home[key]
        validate_entity_id(entity_id, f"home.{key}")
        validated_home[key] = entity_id

    for key in OPTIONAL_EXTERNAL_METRICS:
        entity_id = home.get(key)
        if entity_id is not None:
            validate_entity_id(entity_id, f"home.{key}")
            validated_home[key] = entity_id
        else:
            validated_home[key] = None

    raw_rooms = data.get("rooms", [])
    if not isinstance(raw_rooms, list):
        raise ValueError("Mapping configuration 'rooms' must be a list of room objects.")

    validated_rooms: list[dict[str, str]] = []
    seen_entities = set()
    for idx, room in enumerate(raw_rooms):
        if not isinstance(room, dict):
            raise ValueError(f"Room at index {idx} must be a dictionary.")
        name = room.get("name")
        entity_id = room.get("entity_id")
        validate_room_name(name)
        validate_entity_id(entity_id, f"rooms[{idx}].entity_id")
        if entity_id in seen_entities:
            raise ValueError(f"Duplicate room entity_id in mapping: '{entity_id}'.")
        seen_entities.add(entity_id)
        validated_rooms.append({"name": name.strip(), "entity_id": entity_id})

    return validated_home, validated_rooms


def generate_storyboard_yaml(home: dict[str, str | None], rooms: list[dict[str, str]]) -> str:
    readiness_id = home["data_readiness"]
    hlc_id = home["hlc"]
    fabric_id = home["fabric_loss"]
    vent_id = home["ventilation_loss"]
    ach_id = home["air_change_rate"]
    hw_id = home["hot_water_gas"]

    # JS room roster serialization
    js_pairs = [[r["name"], r["entity_id"]] for r in rooms]
    js_rooms_repr = json.dumps(js_pairs, ensure_ascii=True).replace("'", "\\u0027")

    # Jinja room roster serialization
    if rooms:
        jinja_elements = [
            f'[{json.dumps(r["name"].replace(chr(10), " "))}, states({json.dumps(r["entity_id"])}) | float(-1)]'
            for r in rooms
        ]
        jinja_rooms_repr = "[\n                " + ",\n                ".join(jinja_elements) + "\n              ]"
        first_room_entity = rooms[0]["entity_id"]
    else:
        jinja_rooms_repr = "[]"
        first_room_entity = hlc_id

    # Interpretation card content
    if rooms:
        interpretation_content = f"""{{% set hlc = states('{hlc_id}') | float(0) %}}
              {{% set fabric = states('{fabric_id}') | float(0) %}}
              {{% set vent = states('{vent_id}') | float(0) %}}
              {{% set co2_count = state_attr('{ach_id}', 'co2_sensors_used') | int(0) %}}
              {{% set status = state_attr('{hlc_id}', 'status') %}}
              {{% set raw_rooms = {jinja_rooms_repr} %}}
              {{% set valid_rooms = raw_rooms | selectattr(1, 'gt', 0) | sort(attribute=1) | list %}}

              {{% if fabric and vent and fabric > vent * 2 %}}
              **Observed loss signal is predominantly fabric heat transfer**, rather than ventilation.
              {{% elif fabric and vent %}}
              Ventilation is a material part of total building heat loss.
              {{% else %}}
              A physically consistent fabric/ventilation split is not currently available.
              {{% endif %}}
              {{% if valid_rooms | length >= 2 %}}
              Observed fastest-cooling areas: **{{{{ valid_rooms[0][0] }}}}** and **{{{{ valid_rooms[1][0] }}}}**.
              {{% elif valid_rooms | length == 1 %}}
              Observed fastest-cooling area: **{{{{ valid_rooms[0][0] }}}}**.
              {{% endif %}}

              *Diagnostic notice:* Effective τ reflects overnight temperature decline under observed conditions (combining fabric, draughts, thermal mass, and adjacent-room heat exchange). It is a room diagnostic, not a building survey or intervention recommendation."""
    else:
        interpretation_content = """**No conditioned rooms configured.**
              Add at least one conditioned room in `dashboard_mapping.json` to enable room cooling time constant (τ) analysis."""

    yaml_content = f"""# Thermal Storyboard dashboard
# Generated from portable mapping configuration.
# Required custom cards:
#   - apexcharts-card
#   - lovelace-plotly-graph-card

views:
  - title: Thermal efficiency
    icon: mdi:home-thermometer
    type: sections
    max_columns: 4
    sections:
      # Evidence: readiness and headline heat loss
      - type: grid
        column_span: 4
        cards:
          - type: heading
            heading: Can I trust the heat-loss number?
            heading_style: title
            icon: mdi:chart-scatter-plot

          - type: markdown
            title: System Data Readiness
            grid_options:
              columns: 12
              rows: auto
            content: >-
              {{% set s = '{readiness_id}' %}}
              {{% set state = states(s) %}}
              {{% set summary = state_attr(s, 'summary') %}}
              {{% set hlc_status = state_attr(s, 'hlc_status') %}}
              {{% set hlc_reason = state_attr(s, 'hlc_reason') %}}
              {{% set hlc_action = state_attr(s, 'hlc_next_action') %}}
              {{% set metrics = state_attr(s, 'metrics') or {{}} %}}
              {{% set hlc_metrics = metrics.get('hlc', {{}}) %}}

              {{% if states(s) in ['unknown', 'unavailable'] %}}
              **Readiness source unavailable.** Check integration setup and recorder access.
              {{% else %}}
              **Readiness State:** `{{{{ state | replace('_', ' ') | title }}}}`
              {{% if hlc_status %}} · HLC: **{{{{ hlc_status | replace('_', ' ') | title }}}}**{{% endif %}}
              {{% if hlc_reason %}} · **Reason:** {{{{ hlc_reason }}}}{{% endif %}}
              {{% if hlc_action and hlc_action != 'None' %}} · **Next Action:** {{{{ hlc_action }}}}{{% endif %}}
              {{% if summary %}} · {{{{ summary }}}}{{% endif %}}
              {{% if hlc_metrics.get('usable_observations') is not none %}} · HLC evidence: **{{{{ hlc_metrics.get('usable_observations') }}}}/{{{{ hlc_metrics.get('required_observations', '—') }}}}** usable/required observations.{{% endif %}}
              {{% for metric, info in metrics.items() if metric != 'rooms' %}}

              **{{{{ metric | replace('_', ' ') | title }}}}:** {{{{ info.get('status', 'collecting') | replace('_', ' ') }}}}. {{{{ info.get('reason', '') }}}} {{{{ info.get('next_action', '') }}}}
              {{% if info.get('usable_observations') is not none %}} Evidence: {{{{ info.get('usable_observations') }}}}/{{{{ info.get('required_observations', '—') }}}}.{{% endif %}}
              {{% if info.get('model_data_through') %}} Model through {{{{ info.get('model_data_through') }}}}.{{% endif %}}
              {{% if info.get('source_lag_days') is not none %}} Source lag: {{{{ info.get('source_lag_days') }}}} days.{{% endif %}}
              {{% endfor %}}
              {{% for room, info in metrics.get('rooms', {{}}).items() %}}

              **{{{{ room | replace('_', ' ') | title }}}}:** {{{{ info.get('status', 'collecting') | replace('_', ' ') }}}}. {{{{ info.get('reason', '') }}}} {{{{ info.get('next_action', '') }}}}
              {{% if info.get('usable_observations') is not none %}} Nights: {{{{ info.get('usable_observations') }}}}/{{{{ info.get('required_observations', '—') }}}}.{{% endif %}}
              {{% endfor %}}
              {{% endif %}}

          - type: markdown
            title: Delivered heat loss
            grid_options:
              columns: 6
              rows: 4
            content: >-
              {{% set s = '{hlc_id}' %}}
              {{% set value = states(s) | float(0) %}}
              {{% set status = state_attr(s, 'status') %}}
              {{% set days = state_attr(s, 'days_used') | int(0) %}}
              {{% set r2 = state_attr(s, 'r_squared') | float(0) %}}
              {{% set low = state_attr(s, 'confidence_interval_low_w_per_k') | float(0) %}}
              {{% set high = state_attr(s, 'confidence_interval_high_w_per_k') | float(0) %}}
              {{% set recent = state_attr(s, 'recent_hlc_w_per_k') | float(0) %}}
              {{% set recent_window = state_attr(s, 'recent_window_days') | int(0) %}}
              {{% set intercept = state_attr(s, 'regression_intercept_kwh_per_day') | float(0) %}}
              {{% set effective = state_attr(s, 'effective_independent_days') | int(0) %}}
              {{% set model_through = state_attr(s, 'model_data_through') %}}
              {{% set lag = state_attr(s, 'source_lag_days') %}}
              {{% set change = ((recent - value) / value * 100) if value and recent else none %}}
              {{% set note = state_attr(s, 'note') %}}

              {{% if states(s) in ['unknown', 'unavailable'] or not days %}}
              # Not yet available

              {{% if status and status != 'unknown' %}}**Status: {{{{ status | replace('_', ' ') | title }}}}** · {{% endif %}}{{{{ note or 'Heating season data required.' }}}}
              {{% else %}}
              # {{{{ value | round(0) }}}} W/K

              **{{{{ (status or 'Provisional') | replace('_', ' ') | title }}}}** · {{{{ days }}}} heating days · R² {{{{ r2 | round(3) }}}}
              {{% if model_through %}} · Data through: **{{{{ model_through }}}}**{{% if lag is not none %}} (lag: {{{{ lag }}}}d){{% endif %}}{{% endif %}}
              {{% if status == 'historical_baseline_held' %}} · *Heating model held from latest qualifying cold season*{{% endif %}}

              *Scope:* {{{{ state_attr(s, 'window_days') or days }}}} complete local heating days (23, 24 or 25 hours on daylight-saving transitions); partial days are excluded. This model remains anchored to its latest qualifying heating day.

              Approximate 95% interval: **{{{{ low | round(0) }}}}–{{{{ high | round(0) }}}} W/K**
              {{% if effective %}}(consecutive days share weather; {{{{ days }}}} days carry about {{{{ effective }}}} days of independent evidence){{% endif %}}

              {{% if recent %}}
              Recent {{{{ recent_window }}}}-day estimate: **{{{{ recent | round(0) }}}} W/K**
              {{% if change is not none %}}({{{{ '%+.0f' | format(change) }}}}% versus full window){{% endif %}}
              {{% endif %}}

              Regression intercept: **{{{{ intercept | round(1) }}}} kWh/day**.
              {{% if state_attr(s, 'dhw_correction') %}}{{{{ state_attr(s, 'dhw_correction') }}}}{{% endif %}}
              {{% endif %}}

          - type: custom:apexcharts-card
            section_mode: true
            grid_options:
              columns: 6
              rows: 4
            graph_span: 365d
            header:
              show: true
              title: HLC history — weekly mean
              show_states: true
              colorize_states: true
            yaxis:
              - min: 0
                decimals: 0
                apex_config:
                  title:
                    text: W/K
            apex_config:
              chart:
                height: 260
              stroke:
                width: 3
              annotations:
                yaxis:
                  - "y": 300
                    borderColor: "#fb8c00"
                    strokeDashArray: 4
                    label:
                      text: 300 W/K context line
            series:
              - entity: {hlc_id}
                name: Delivered HLC
                color: "#ef6c00"
                type: area
                curve: smooth
                opacity: 0.22
                stroke_width: 3
                statistics:
                  type: mean
                  period: week
                  align: middle

      # Diagnosis: Sankey loss split and DHW baseline
      - type: grid
        column_span: 4
        cards:
          - type: heading
            heading: Where does the heat go?
            heading_style: title
            icon: mdi:chart-sankey-variant

          - type: custom:plotly-graph
            title: "[EXPERIMENTAL] Delivered space-heating loss (W/K)"
            visibility:
              - condition: numeric_state
                entity: {fabric_id}
                above: 0
              - condition: numeric_state
                entity: {vent_id}
                above: 0
            grid_options:
              columns: 8
              rows: 5
            raw_plotly_config: true
            refresh_interval: auto
            entities:
              - entity: {hlc_id}
                type: sankey
                orientation: h
                arrangement: fixed
                node:
                  pad: 30
                  thickness: 28
                  line:
                    color: rgba(0,0,0,0.22)
                    width: 1
                  label:
                    - Delivered HLC
                    - Fabric
                    - Ventilation
                  color:
                    - "#ff8a65"
                    - "#ef6c00"
                    - "#1e88e5"
                  x:
                    - 0.05
                    - 0.78
                    - 0.78
                  "y":
                    - 0.5
                    - 0.22
                    - 0.76
                  hovertemplate: "%{{label}}<extra></extra>"
                link:
                  source:
                    - 0
                    - 0
                  target:
                    - 1
                    - 2
                  value: >-
                    $ex {{
                      const parseNum = (v) => {{
                        const raw = hass.states[v]?.state;
                        return (raw !== undefined && raw !== null && String(raw).trim() !== '' && !['unknown', 'unavailable', 'none'].includes(String(raw).toLowerCase())) ? Number(raw) : NaN;
                      }};
                      const f = parseNum('{fabric_id}');
                      const v = parseNum('{vent_id}');
                      const fVal = Number.isFinite(f) && f > 0 ? f : 0;
                      const vVal = Number.isFinite(v) && v > 0 ? v : 0;
                      return (fVal > 0 && vVal > 0) ? [fVal, vVal] : [];
                    }}
                  color:
                    - rgba(239,108,0,0.58)
                    - rgba(30,136,229,0.58)
                  customdata: >-
                    $ex {{
                      const parseNum = (v) => {{
                        const raw = hass.states[v]?.state;
                        return (raw !== undefined && raw !== null && String(raw).trim() !== '' && !['unknown', 'unavailable', 'none'].includes(String(raw).toLowerCase())) ? Number(raw) : NaN;
                      }};
                      const f = parseNum('{fabric_id}');
                      const v = parseNum('{vent_id}');
                      return [
                        Number.isFinite(f) && f > 0 ? `${{f.toFixed(1)}} W/K` : 'Not enabled / insufficient data',
                        Number.isFinite(v) && v > 0 ? `${{v.toFixed(1)}} W/K` : 'Not enabled / insufficient data'
                      ];
                    }}
                  hovertemplate: "%{{target.label}}: %{{customdata}}<extra></extra>"
            layout:
              height: 330
              margin:
                l: 20
                r: 20
                t: 35
                b: 20
              font:
                color: $ex css_vars['primary-text-color']
              paper_bgcolor: rgba(0,0,0,0)
              plot_bgcolor: rgba(0,0,0,0)
            config:
              displayModeBar: false
              responsive: true

          - type: vertical-stack
            grid_options:
              columns: 4
              rows: 5
            cards:
              - type: tile
                entity: {ach_id}
                name: "[EXPERIMENTAL] Air Infiltration"
                icon: mdi:weather-windy
                color: blue
                vertical: false
                features_position: bottom

              - type: markdown
                title: Ventilation evidence
                content: >-
                  {{% set s = '{ach_id}' %}}
                  {{% set note = state_attr(s, 'note') %}}
                  {{% set co2_sensors = state_attr(s, 'co2_sensors_used') %}}
                  {{% set decay_windows = state_attr(s, 'decay_windows_used') %}}
                  {{% set baseline = state_attr(s, 'outdoor_co2_baseline_ppm') %}}
                  {{% set source = state_attr(s, 'co2_baseline_source') %}}

                  {{% if states(s) in ['unknown', 'unavailable'] or not decay_windows %}}
                  # Not yet available

                  {{{{ note or 'Room-derived air change rate proxy requires clean CO₂ decay windows.' }}}}
                  {{% else %}}
                  **{{{{ states(s) }}}}/h** from
                  **{{{{ co2_sensors }}}} CO₂ sensors** and
                  **{{{{ decay_windows }}}} decay windows**.

                  Baseline: **{{{{ baseline | float(0) | round(0) }}}} ppm**
                  {{% if source %}}({{{{ source }}}}).{{% endif %}}
                  {{% endif %}}

                  *Scope & experimental limits:* Room-derived ACH proxy. Whole-home loss split is experimental and requires `experimental_whole_home_ventilation: true` in integration options.

              - type: markdown
                title: Non-space-heating gas
                content: >-
                  {{% set s = '{hw_id}' %}}
                  {{% if states(s) in ['unknown', 'unavailable'] %}}
                  # Not yet available

                  {{{{ state_attr(s, 'note') or 'Non-space-heating gas baseline requires gas consumption history.' }}}}
                  {{% else %}}
                  # {{{{ states(s) }}}} kWh/day

                  {{% set yearly = state_attr(s, 'cost_per_year_gbp') %}}
                  {{% if yearly is not none %}}**£{{{{ yearly | round(0) }}}}/modelled year** · {{% endif %}}
                  {{{{ state_attr(s, 'days_used') | int(0) }}}} low-heating days.
                  {{% endif %}}

                  Kept separate from the Sankey because this is energy per day,
                  while the space-heating branches are W/K.

      # Action: room ranking with fit evidence
      - type: grid
        column_span: 4
        cards:
          - type: heading
            heading: Which rooms cool fastest?
            heading_style: title
            icon: mdi:home-thermometer-outline

          - type: custom:plotly-graph
            title: Room thermal fingerprints — effective τ
            grid_options:
              columns: 12
              rows: 6
            raw_plotly_config: true
            refresh_interval: auto
            entities:
              - entity: {first_room_entity}
                type: bar
                orientation: h
                x: >-
                  $ex {{
                    const rawRooms = {js_rooms_repr};
                    const getRows = () => rawRooms.map(([name, id]) => {{
                      const raw = hass.states[id]?.state;
                      const isValid = raw !== undefined && raw !== null && String(raw).trim() !== '' && !['unknown', 'unavailable', 'none'].includes(String(raw).toLowerCase());
                      const nightsRaw = hass.states[id]?.attributes?.nights_fitted;
                      const nightsValid = nightsRaw !== undefined && nightsRaw !== null && String(nightsRaw).trim() !== '';
                      return {{
                        name,
                        value: isValid ? Number(raw) : NaN,
                        nights: nightsValid ? (Number(nightsRaw) || 0) : 0
                      }};
                    }}).filter(room => Number.isFinite(room.value) && room.value > 0)
                       .sort((a, b) => b.value - a.value);
                    return getRows().map(room => room.value);
                  }}
                "y": >-
                  $ex {{
                    const rawRooms = {js_rooms_repr};
                    const getRows = () => rawRooms.map(([name, id]) => {{
                      const raw = hass.states[id]?.state;
                      const isValid = raw !== undefined && raw !== null && String(raw).trim() !== '' && !['unknown', 'unavailable', 'none'].includes(String(raw).toLowerCase());
                      const nightsRaw = hass.states[id]?.attributes?.nights_fitted;
                      const nightsValid = nightsRaw !== undefined && nightsRaw !== null && String(nightsRaw).trim() !== '';
                      return {{
                        name,
                        value: isValid ? Number(raw) : NaN,
                        nights: nightsValid ? (Number(nightsRaw) || 0) : 0
                      }};
                    }}).filter(room => Number.isFinite(room.value) && room.value > 0)
                       .sort((a, b) => b.value - a.value);
                    return getRows().map(room => room.name);
                  }}
                customdata: >-
                  $ex {{
                    const rawRooms = {js_rooms_repr};
                    const getRows = () => rawRooms.map(([name, id]) => {{
                      const raw = hass.states[id]?.state;
                      const isValid = raw !== undefined && raw !== null && String(raw).trim() !== '' && !['unknown', 'unavailable', 'none'].includes(String(raw).toLowerCase());
                      const nightsRaw = hass.states[id]?.attributes?.nights_fitted;
                      const nightsValid = nightsRaw !== undefined && nightsRaw !== null && String(nightsRaw).trim() !== '';
                      return {{
                        name,
                        value: isValid ? Number(raw) : NaN,
                        nights: nightsValid ? (Number(nightsRaw) || 0) : 0
                      }};
                    }}).filter(room => Number.isFinite(room.value) && room.value > 0)
                       .sort((a, b) => b.value - a.value);
                    return getRows().map(room => room.nights);
                  }}
                marker:
                  color: >-
                    $ex {{
                      const rawRooms = {js_rooms_repr};
                      const getRows = () => rawRooms.map(([name, id]) => {{
                        const raw = hass.states[id]?.state;
                        const isValid = raw !== undefined && raw !== null && String(raw).trim() !== '' && !['unknown', 'unavailable', 'none'].includes(String(raw).toLowerCase());
                        return {{
                          name,
                          value: isValid ? Number(raw) : NaN
                        }};
                      }}).filter(room => Number.isFinite(room.value) && room.value > 0)
                         .sort((a, b) => b.value - a.value);
                      return getRows().map((room, index) =>
                        ['#2e7d32', '#7cb342', '#f9a825', '#ef6c00', '#d84315', '#c62828'][index % 6]
                      );
                    }}
                texttemplate: "%{{x:.1f}} h"
                textposition: auto
                hovertemplate: "%{{y}}<br>τ %{{x:.1f}} h<br>%{{customdata}} fitted nights<extra></extra>"
            layout:
              height: 390
              margin:
                l: 105
                r: 25
                t: 40
                b: 55
              bargap: 0.28
              xaxis:
                title: Effective overnight cooling time constant τ (hours)
                rangemode: tozero
                gridcolor: rgba(127,127,127,0.18)
              "yaxis":
                automargin: true
              font:
                color: $ex css_vars['primary-text-color']
              paper_bgcolor: rgba(0,0,0,0)
              plot_bgcolor: rgba(0,0,0,0)
            config:
              displayModeBar: false
              responsive: true

          - type: markdown
            title: Interpretation
            grid_options:
              columns: 12
              rows: 3
            content: >-
              {interpretation_content}
"""
    return yaml_content


def generate_live_yaml(home: dict[str, str | None], rooms: list[dict[str, str]]) -> str:
    readiness_id = home["data_readiness"]
    hlc_id = home["hlc"]
    loft_id = home["loft_ratio"]
    ach_id = home["air_change_rate"]
    fabric_id = home["fabric_loss"]
    vent_id = home["ventilation_loss"]
    hw_7d_id = home["hot_water_gas_7d"]
    sh_7d_id = home["space_heating_gas_7d"]
    baseload_id = home["electricity_baseload"]
    elec_7d_id = home["electricity_use_7d"]
    water_7d_id = home["water_use_7d"]
    live_power_id = home.get("live_electricity_power")
    daily_water_id = home.get("daily_water_meter")

    js_pairs = [[r["name"], r["entity_id"]] for r in rooms]
    js_rooms_repr = json.dumps(js_pairs, ensure_ascii=True).replace("'", "\\u0027")
    first_room_entity = rooms[0]["entity_id"] if rooms else hlc_id

    # Optional badges
    badges = [
        f"""  - color: blue
    entity: {readiness_id}
    icon: mdi:clipboard-check-outline
    name: Readiness
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {readiness_id}
      state_not: unknown
    - condition: state
      entity: {readiness_id}
      state_not: unavailable""",
        f"""  - color: purple
    entity: {hlc_id}
    icon: mdi:home-thermometer-outline
    name: Delivered HLC
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {hlc_id}
      state_not: unknown
    - condition: state
      entity: {hlc_id}
      state_not: unavailable""",
        f"""  - color: teal
    entity: {ach_id}
    icon: mdi:weather-windy
    name: Air Changes
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {ach_id}
      state_not: unknown
    - condition: state
      entity: {ach_id}
      state_not: unavailable""",
        f"""  - color: amber
    entity: {loft_id}
    icon: mdi:home-roof
    name: Loft Coupling
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {loft_id}
      state_not: unknown
    - condition: state
      entity: {loft_id}
      state_not: unavailable""",
        f"""  - color: blue
    entity: {baseload_id}
    icon: mdi:power-plug-outline
    name: Baseload
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {baseload_id}
      state_not: unknown
    - condition: state
      entity: {baseload_id}
      state_not: unavailable""",
        f"""  - color: deep-orange
    entity: {sh_7d_id}
    icon: mdi:radiator
    name: Space Heating
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {sh_7d_id}
      state_not: unknown
    - condition: state
      entity: {sh_7d_id}
      state_not: unavailable""",
        f"""  - color: light-blue
    entity: {hw_7d_id}
    icon: mdi:shower-head
    name: Hot Water
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {hw_7d_id}
      state_not: unknown
    - condition: state
      entity: {hw_7d_id}
      state_not: unavailable""",
    ]

    if daily_water_id:
        badges.append(
            f"""  - color: cyan
    entity: {daily_water_id}
    icon: mdi:water
    name: Daily Water
    show_icon: true
    show_name: true
    show_state: true
    visibility:
    - condition: state
      entity: {daily_water_id}
      state_not: unknown
    - condition: state
      entity: {daily_water_id}
      state_not: unavailable"""
        )

    badges_yaml = "\n".join(badges)

    # Optional live power card badge
    if live_power_id:
        elec_header_badges = f"""      badges:
      - entity: {live_power_id}
        name: Live Demand
        show_state: true
        type: entity
"""
    else:
        elec_header_badges = ""

    # Optional daily water tile
    if daily_water_id:
        daily_water_card = f"""    - color: cyan
      entity: {daily_water_id}
      grid_options:
        columns: 6
        rows: 2
      icon: mdi:water
      name: Metered Water (Daily)
      tap_action:
        action: more-info
      type: tile
      vertical: true
"""
        water_tile_cols = 6
    else:
        daily_water_card = ""
        water_tile_cols = 12

    yaml_content = f"""# Sections-view dashboard with real-time gauges, status fallback tiles, and trend charts.
# Generated from portable mapping configuration.

views:
- badges:
{badges_yaml}
  dense_section_placement: true
  icon: mdi:home-thermometer
  max_columns: 3
  path: thermal-efficiency
  sections:
  - cards:
    - heading: Building Thermal Envelope
      heading_style: title
      icon: mdi:home-thermometer
      type: heading
    - type: markdown
      title: Data readiness and model scope
      content: >-
        {{% set s = '{readiness_id}' %}}
        {{% set summary = state_attr(s, 'summary') %}}
        {{% set status = state_attr(s, 'hlc_status') %}}
        {{% set reason = state_attr(s, 'hlc_reason') %}}
        {{% set action = state_attr(s, 'hlc_next_action') %}}
        **Readiness:** {{{{ states(s) | replace('_', ' ') | title }}}} · **HLC:** {{{{ status | default('collecting', true) | replace('_', ' ') | title }}}}
        {{% if reason %}} · **Reason:** {{{{ reason }}}}{{% endif %}}
        {{% if action and action != 'None' %}} · **Next:** {{{{ action }}}}{{% endif %}}
              {{% if summary %}} · {{{{ summary }}}}{{% endif %}}
        {{% set hlc = state_attr(s, 'metrics') or {{}} %}}{{% set fit = hlc.get('hlc', {{}}) %}}
        {{% for metric, info in hlc.items() if metric != 'rooms' %}}

        **{{{{ metric | replace('_', ' ') | title }}}}:** {{{{ info.get('status', 'collecting') | replace('_', ' ') }}}}. {{{{ info.get('reason', '') }}}} {{{{ info.get('next_action', '') }}}}
        {{% if info.get('usable_observations') is not none %}} Evidence: {{{{ info.get('usable_observations') }}}}/{{{{ info.get('required_observations', '—') }}}}.{{% endif %}}
        {{% if info.get('model_data_through') %}} Model through {{{{ info.get('model_data_through') }}}}.{{% endif %}}
        {{% if info.get('source_lag_days') is not none %}} Source lag: {{{{ info.get('source_lag_days') }}}} days.{{% endif %}}
        {{% endfor %}}
        {{% for room, info in hlc.get('rooms', {{}}).items() %}}

        **{{{{ room | replace('_', ' ') | title }}}}:** {{{{ info.get('status', 'collecting') | replace('_', ' ') }}}}. {{{{ info.get('reason', '') }}}} {{{{ info.get('next_action', '') }}}}
        {{% if info.get('usable_observations') is not none %}} Nights: {{{{ info.get('usable_observations') }}}}/{{{{ info.get('required_observations', '—') }}}}.{{% endif %}}
        {{% endfor %}}
        {{% if fit.get('usable_observations') is not none %}} · HLC observations: **{{{{ fit.get('usable_observations') }}}}/{{{{ fit.get('required_observations', '—') }}}}** usable/required.{{% endif %}}
        {{% set through = state_attr('{hlc_id}', 'model_data_through') %}}
        {{% set lag = state_attr('{hlc_id}', 'source_lag_days') %}}
        {{% set model_status = state_attr('{hlc_id}', 'status') %}}
        {{% if through %}} · Model data through **{{{{ through }}}}**{{% if lag is not none %}} (source lag {{{{ lag }}}} days){{% endif %}}.{{% endif %}}
        {{% if model_status == 'historical_baseline_held' %}} Historical heating baseline held from its latest qualifying cold-season day.{{% endif %}}
        · Only complete local days are used; daylight-saving days may contain 23, 24 or 25 hours, and partial days are excluded.
    - type: conditional
      conditions:
      - condition: state
        entity: {hlc_id}
        state_not: unknown
      - condition: state
        entity: {hlc_id}
        state_not: unavailable
      card:
        type: gauge
        entity: {hlc_id}
        name: Delivered HLC
        min: 0
        max: 500
        needle: true
        segments:
        - from: 0
          color: '#2e7d32'
        - from: 100
          color: '#7cb342'
        - from: 180
          color: '#f9a825'
        - from: 280
          color: '#ef6c00'
        - from: 400
          color: '#c62828'
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: or
        conditions:
        - condition: state
          entity: {hlc_id}
          state: unknown
        - condition: state
          entity: {hlc_id}
          state: unavailable
      card:
        type: tile
        entity: {hlc_id}
        name: Delivered HLC
        icon: mdi:home-thermometer-outline
        color: purple
        vertical: true
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: state
        entity: {ach_id}
        state_not: unknown
      - condition: state
        entity: {ach_id}
        state_not: unavailable
      card:
        type: gauge
        entity: {ach_id}
        name: "[EXPERIMENTAL] Air Infiltration"
        min: 0
        max: 1.5
        needle: true
        segments:
        - from: 0
          color: '#ef6c00'
        - from: 0.35
          color: '#2e7d32'
        - from: 0.7
          color: '#f9a825'
        - from: 1
          color: '#c62828'
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: or
        conditions:
        - condition: state
          entity: {ach_id}
          state: unknown
        - condition: state
          entity: {ach_id}
          state: unavailable
      card:
        type: tile
        entity: {ach_id}
        name: "[EXPERIMENTAL] Air Infiltration"
        icon: mdi:weather-windy
        color: teal
        vertical: true
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: state
        entity: {fabric_id}
        state_not: unknown
      - condition: state
        entity: {fabric_id}
        state_not: unavailable
      card:
        type: gauge
        entity: {fabric_id}
        name: "[EXPERIMENTAL] Fabric Loss"
        min: 0
        max: 450
        needle: true
        segments:
        - from: 0
          color: '#2e7d32'
        - from: 100
          color: '#7cb342'
        - from: 180
          color: '#f9a825'
        - from: 280
          color: '#ef6c00'
        - from: 380
          color: '#c62828'
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: or
        conditions:
        - condition: state
          entity: {fabric_id}
          state: unknown
        - condition: state
          entity: {fabric_id}
          state: unavailable
      card:
        type: tile
        entity: {fabric_id}
        name: "[EXPERIMENTAL] Fabric Loss"
        icon: mdi:wall
        color: deep-orange
        vertical: true
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: numeric_state
        entity: {loft_id}
        above: -1
        below: 2
      card:
        type: gauge
        entity: {loft_id}
        name: Loft Coupling
        min: 0
        max: 1
        needle: true
        segments:
        - from: 0
          color: '#2e7d32'
        - from: 0.2
          color: '#f9a825'
        - from: 0.45
          color: '#c62828'
      grid_options:
        columns: 6
        rows: 3
    - type: conditional
      conditions:
      - condition: or
        conditions:
        - condition: state
          entity: {loft_id}
          state: unknown
        - condition: state
          entity: {loft_id}
          state: unavailable
      card:
        type: tile
        entity: {loft_id}
        name: Loft Coupling
        icon: mdi:home-roof
        color: amber
        vertical: true
      grid_options:
        columns: 6
        rows: 3
    column_span: 1
    type: grid
  - cards:
    - heading: Room Cooling Fingerprints
      heading_style: title
      icon: mdi:fingerprint
      type: heading
    - config:
        displayModeBar: false
        responsive: true
      entities:
      - customdata: '$ex {{ const rawRooms = {js_rooms_repr}; const getRows = () => rawRooms.map(([n, id]) => {{ const raw = hass.states[id]?.state; const isValid = raw !== undefined && raw !== null && String(raw).trim() !== "" && !["unknown", "unavailable", "none"].includes(String(raw).toLowerCase()); const nRaw = hass.states[id]?.attributes?.nights_fitted; const nValid = nRaw !== undefined && nRaw !== null && String(nRaw).trim() !== ""; return {{ name: n, value: isValid ? Number(raw) : NaN, nights: nValid ? (Number(nRaw) || 0) : 0 }}; }}).filter(r => Number.isFinite(r.value) && r.value > 0).sort((a, b) => b.value - a.value); return getRows().map(r => r.nights); }}'
        entity: {first_room_entity}
        hovertemplate: '<b>%{{y}}</b><br>Cooling time: <b>%{{x:.1f}} h</b><br>Fitted: %{{customdata}} nights<extra></extra>'
        marker:
          color: '$ex {{ const rawRooms = {js_rooms_repr}; const getRows = () => rawRooms.map(([n, id]) => {{ const raw = hass.states[id]?.state; const isValid = raw !== undefined && raw !== null && String(raw).trim() !== "" && !["unknown", "unavailable", "none"].includes(String(raw).toLowerCase()); return {{ name: n, value: isValid ? Number(raw) : NaN }}; }}).filter(r => Number.isFinite(r.value) && r.value > 0).sort((a, b) => b.value - a.value); return getRows().map((r, i) => ["#2e7d32", "#689f38", "#fbc02d", "#f57c00", "#e64a19", "#d32f2f"][i % 6]); }}'
          opacity: 0.88
        orientation: h
        textposition: auto
        texttemplate: '%{{x:.1f}} h'
        type: bar
        x: '$ex {{ const rawRooms = {js_rooms_repr}; const getRows = () => rawRooms.map(([n, id]) => {{ const raw = hass.states[id]?.state; const isValid = raw !== undefined && raw !== null && String(raw).trim() !== "" && !["unknown", "unavailable", "none"].includes(String(raw).toLowerCase()); return {{ name: n, value: isValid ? Number(raw) : NaN }}; }}).filter(r => Number.isFinite(r.value) && r.value > 0).sort((a, b) => b.value - a.value); return getRows().map(r => r.value); }}'
        y: '$ex {{ const rawRooms = {js_rooms_repr}; const getRows = () => rawRooms.map(([n, id]) => {{ const raw = hass.states[id]?.state; const isValid = raw !== undefined && raw !== null && String(raw).trim() !== "" && !["unknown", "unavailable", "none"].includes(String(raw).toLowerCase()); return {{ name: n, value: isValid ? Number(raw) : NaN }}; }}).filter(r => Number.isFinite(r.value) && r.value > 0).sort((a, b) => b.value - a.value); return getRows().map(r => r.name); }}'
      layout:
        bargap: 0.28
        font:
          color: $ex css_vars['primary-text-color']
        height: 390
        margin:
          b: 65
          l: 100
          r: 25
          t: 15
        paper_bgcolor: rgba(0,0,0,0)
        plot_bgcolor: rgba(0,0,0,0)
        xaxis:
          gridcolor: rgba(127,127,127,0.16)
          rangemode: tozero
          title:
            text: Cooling Time Constant τ (hours)
            standoff: 12
        yaxis:
          automargin: true
          autorange: reversed
      raw_plotly_config: true
      refresh_interval: auto
      type: custom:plotly-graph
    type: grid
  - cards:
    - heading: Long-Term Heat Loss Model
      heading_style: title
      icon: mdi:chart-bell-curve-cumulative
      type: heading
    - apex_config:
        chart:
          height: 310
        legend:
          fontSize: 12px
          position: top
        stroke:
          curve: smooth
          dashArray:
          - 0
          - 0
          - 4
        yaxis:
          decimalsInFloat: 0
          min: 0
          title:
            text: W/K
      graph_span: 365d
      header:
        colorize_states: true
        show: true
        show_states: true
        title: Delivered HLC & [EXPERIMENTAL] Loss Split (365 Days)
      series:
      - color: '#7e57c2'
        entity: {hlc_id}
        group_by:
          duration: 7d
          func: avg
        name: Total HLC
        opacity: 0.12
        stroke_width: 3
        type: area
        statistics:
          type: mean
          period: week
          align: middle
      - color: '#ef5350'
        entity: {fabric_id}
        group_by:
          duration: 7d
          func: avg
        name: Fabric Loss (Experimental)
        stroke_width: 2.5
        type: line
        statistics:
          type: mean
          period: week
          align: middle
      - color: '#42a5f5'
        entity: {vent_id}
        group_by:
          duration: 7d
          func: avg
        name: Ventilation Loss (Experimental)
        stroke_width: 2
        type: line
        statistics:
          type: mean
          period: week
          align: middle
      span:
        end: day
      type: custom:apexcharts-card
    column_span: 1
    type: grid
  - cards:
    - heading: Gas Consumption
      heading_style: title
      icon: mdi:meter-gas
      type: heading
    - color: blue
      entity: {hw_7d_id}
      grid_options:
        columns: 6
        rows: 2
      icon: mdi:shower-head
      name: Hot Water (7d)
      tap_action:
        action: more-info
      type: tile
      vertical: true
    - color: deep-orange
      entity: {sh_7d_id}
      grid_options:
        columns: 6
        rows: 2
      icon: mdi:radiator
      name: Space Heating (7d)
      tap_action:
        action: more-info
      type: tile
      vertical: true
    - apex_config:
        chart:
          height: 225
        legend:
          position: top
        stroke:
          curve: smooth
        yaxis:
          decimalsInFloat: 1
          min: 0
          title:
            text: kWh/day
      graph_span: 90d
      header:
        colorize_states: true
        show: true
        show_states: true
        title: Attributed Gas Use (90 Days)
      series:
      - color: '#42a5f5'
        entity: {hw_7d_id}
        group_by:
          duration: 1d
          func: avg
        name: Hot Water
        opacity: 0.18
        stroke_width: 2
        type: area
        statistics:
          type: mean
          period: day
      - color: '#ef6c00'
        entity: {sh_7d_id}
        group_by:
          duration: 1d
          func: avg
        name: Space Heating
        opacity: 0.14
        stroke_width: 2
        type: area
        statistics:
          type: mean
          period: day
      span:
        end: day
      type: custom:apexcharts-card
    type: grid
  - cards:
    - heading: Electricity & Baseload
      heading_style: title
      icon: mdi:transmission-tower-import
      type: heading
{elec_header_badges}    - color: amber
      entity: {elec_7d_id}
      grid_options:
        columns: 6
        rows: 2
      icon: mdi:flash
      name: Electricity (7d)
      tap_action:
        action: more-info
      type: tile
      vertical: true
    - color: blue
      entity: {baseload_id}
      grid_options:
        columns: 6
        rows: 2
      icon: mdi:power-plug-outline
      name: Baseload (Always-on)
      tap_action:
        action: more-info
      type: tile
      vertical: true
    - apex_config:
        chart:
          height: 225
        stroke:
          curve: smooth
        yaxis:
          decimalsInFloat: 1
          min: 0
          title:
            text: kWh/day
      graph_span: 90d
      header:
        colorize_states: true
        show: true
        show_states: true
        title: Electricity Consumption (90 Days)
      series:
      - color: '#f9a825'
        entity: {elec_7d_id}
        group_by:
          duration: 1d
          func: avg
        name: Daily Electricity
        opacity: 0.2
        stroke_width: 2
        type: area
        statistics:
          type: mean
          period: day
      span:
        end: day
      type: custom:apexcharts-card
    type: grid
  - cards:
    - heading: Water Consumption
      heading_style: title
      icon: mdi:water
      type: heading
{daily_water_card}    - color: teal
      entity: {water_7d_id}
      grid_options:
        columns: {water_tile_cols}
        rows: 2
      icon: mdi:water-outline
      name: Water Avg (7d)
      tap_action:
        action: more-info
      type: tile
      vertical: true
    - apex_config:
        chart:
          height: 225
        stroke:
          curve: smooth
        yaxis:
          decimalsInFloat: 0
          min: 0
          title:
            text: L/day
      graph_span: 90d
      header:
        colorize_states: true
        show: true
        show_states: true
        title: Total Water Trend (90 Days)
      series:
      - color: '#00acc1'
        entity: {water_7d_id}
        group_by:
          duration: 1d
          func: avg
        name: Water (7d avg)
        opacity: 0.18
        stroke_width: 2
        type: area
        statistics:
          type: mean
          period: day
      span:
        end: day
      type: custom:apexcharts-card
    type: grid
  title: Thermal efficiency
"""
    return yaml_content


def generate_dashboards(
    mapping_path: str | Path,
    storyboard_out: str | Path | None = None,
    live_out: str | Path | None = None,
) -> tuple[str, str]:
    mapping_p = Path(mapping_path)
    if not mapping_p.is_file():
        raise FileNotFoundError(f"Mapping file not found: {mapping_path}")

    raw_data = json.loads(mapping_p.read_text(encoding="utf-8"))
    home, rooms = validate_mapping(raw_data)

    storyboard_yaml = generate_storyboard_yaml(home, rooms)
    live_yaml = generate_live_yaml(home, rooms)

    if storyboard_out:
        out_p = Path(storyboard_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(storyboard_yaml, encoding="utf-8")

    if live_out:
        out_p = Path(live_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(live_yaml, encoding="utf-8")

    return storyboard_yaml, live_yaml


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate portable Lovelace dashboards from entity/room mapping JSON."
    )
    parser.add_argument(
        "--mapping",
        "-m",
        default="lovelace/dashboard_mapping.json",
        help="Path to mapping JSON (default: lovelace/dashboard_mapping.json)",
    )
    parser.add_argument(
        "--storyboard",
        "-s",
        default="lovelace/thermal_efficiency_dashboard.yaml",
        help="Output path for storyboard dashboard YAML",
    )
    parser.add_argument(
        "--live",
        "-l",
        default="lovelace/thermal_efficiency_live.yaml",
        help="Output path for live overview dashboard YAML",
    )

    args = parser.parse_args()

    try:
        generate_dashboards(
            mapping_path=args.mapping,
            storyboard_out=args.storyboard,
            live_out=args.live,
        )
        print(f"Successfully generated dashboards from {args.mapping}:")
        print(f"  - Storyboard: {args.storyboard}")
        print(f"  - Live overview: {args.live}")
        return 0
    except Exception as exc:
        print(f"Error generating dashboards: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
