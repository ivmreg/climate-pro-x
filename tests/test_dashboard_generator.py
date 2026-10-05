"""Portable dashboard generation and escaping checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

from scripts.generate_dashboard import generate_dashboards, validate_entity_id, validate_mapping, validate_room_name


ROOT = Path(__file__).resolve().parents[1]


def _make_valid_mapping(room_count: int = 3) -> dict:
    rooms = [
        {"name": f"Room {i}", "entity_id": f"sensor.room_{i}_tau"}
        for i in range(1, room_count + 1)
    ]
    return {
        "home": {
            "data_readiness": "sensor.thermal_efficiency_data_readiness",
            "hlc": "sensor.heat_loss_coefficient",
            "loft_ratio": "sensor.loft_ratio",
            "air_change_rate": "sensor.air_change_rate",
            "fabric_loss": "sensor.fabric_heat_loss",
            "ventilation_loss": "sensor.ventilation_heat_loss",
            "hot_water_gas": "sensor.hot_water_gas",
            "hot_water_gas_7d": "sensor.hot_water_gas_7d",
            "space_heating_gas_7d": "sensor.space_heating_gas_7d",
            "electricity_baseload": "sensor.electricity_baseload",
            "electricity_use_7d": "sensor.electricity_use_7d",
            "water_use_7d": "sensor.water_use_7d",
            "live_electricity_power": None,
            "daily_water_meter": None,
        },
        "rooms": rooms,
    }


def _write_mapping(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _find_card(value, *, title: str):
    if isinstance(value, dict):
        if value.get("title") == title:
            return value
        for child in value.values():
            found = _find_card(child, title=title)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_card(child, title=title)
            if found is not None:
                return found
    return None


def test_yaml_jinja_js_escape_and_stale_state_smoke(tmp_path: Path):
    data = _make_valid_mapping(1)
    room_name = "Children's room: \"East\"\n温度 — & $ `"
    data["rooms"][0]["name"] = room_name
    mapping = _write_mapping(tmp_path, data)
    storyboard_text, live_text = generate_dashboards(mapping)

    storyboard = yaml.safe_load(storyboard_text)
    live = yaml.safe_load(live_text)
    assert isinstance(storyboard, dict) and isinstance(live, dict)

    interpretation = _find_card(storyboard, title="Interpretation")
    template = Environment().from_string(interpretation["content"])
    rendered = template.render(
        states=lambda entity: {"sensor.room_1_tau": "12.5"}.get(entity, "unknown"),
        state_attr=lambda *_args: None,
    )
    assert room_name.replace("\n", " ") in rendered

    room_chart = _find_card(storyboard, title="Room thermal fingerprints — effective τ")
    expression = room_chart["entities"][0]["x"].removeprefix("$ex ").strip()
    node = shutil.which("node")
    if node:
        js = (
            "const f = new Function('hass', " + json.dumps(expression) + ");"
            "const result = f({states:{'sensor.room_1_tau':{state:''}}});"
            "if (result.length !== 0) process.exit(9);"
            "const valid = f({states:{'sensor.room_1_tau':{state:'12.5'}}});"
            "if (JSON.stringify(valid) !== '[12.5]') process.exit(10);"
        )
        run = subprocess.run([node, "-e", js], capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr

    assert json.dumps(room_name, ensure_ascii=True)[1:-1].replace("'", "\\u0027") in live_text
    assert "Number(raw)" in live_text
    assert "daily_water_meter" not in live_text
    assert "live_electricity_power" not in live_text


def test_empty_rooms_and_missing_optional_metrics_are_supported(tmp_path: Path):
    data = _make_valid_mapping(0)
    data["home"].pop("live_electricity_power")
    data["home"].pop("daily_water_meter")
    mapping = _write_mapping(tmp_path, data)
    storyboard_text, live_text = generate_dashboards(mapping)
    storyboard = yaml.safe_load(storyboard_text)
    live = yaml.safe_load(live_text)

    assert "No conditioned rooms configured" in storyboard_text
    badges = live["views"][0]["badges"]
    assert all(badge.get("name") != "Daily Water" for badge in badges)
    assert _find_card(live, title="Metered Water (Daily)") is None
    assert "Room Cooling Fingerprints" in str(live)


def test_model_scope_readiness_and_experimental_labels(tmp_path: Path):
    mapping = _write_mapping(tmp_path, _make_valid_mapping(2))
    storyboard, live = generate_dashboards(mapping)
    for text in (storyboard, live):
        assert "model_data_through" in text
        assert "historical_baseline_held" in text or "historical heating baseline held" in text
        assert "23, 24 or 25 hours" in text
        assert "Data readiness" in text or "System Data Readiness" in text
    assert "[EXPERIMENTAL]" in storyboard
    assert "experimental_whole_home_ventilation: true" in storyboard
    assert "[EXPERIMENTAL]" in live
    assert "payback" not in storyboard.lower()


def test_checked_in_dashboards_match_mapping_byte_for_byte():
    storyboard, live = generate_dashboards(ROOT / "lovelace" / "dashboard_mapping.json")
    assert storyboard == (ROOT / "lovelace" / "thermal_efficiency_dashboard.yaml").read_text(encoding="utf-8")
    assert live == (ROOT / "lovelace" / "thermal_efficiency_live.yaml").read_text(encoding="utf-8")
    yaml.safe_load(storyboard)
    yaml.safe_load(live)


def test_entity_mapping_and_name_validation():
    validate_entity_id("sensor.room_temperature", "test")
    with pytest.raises(ValueError, match="Invalid entity ID"):
        validate_entity_id("sensor.room {{ 7*7 }}", "test")
    validate_room_name('Children\'s room: "East"\n温度')
    with pytest.raises(ValueError, match="non-empty"):
        validate_room_name("  ")


def test_required_readiness_metric_is_validated():
    mapping = _make_valid_mapping()
    del mapping["home"]["data_readiness"]
    with pytest.raises(ValueError, match="missing required metric: 'data_readiness'"):
        validate_mapping(mapping)
