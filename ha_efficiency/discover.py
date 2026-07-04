"""Scan HA entities and draft a config.yaml mapping rooms to sensors."""

from __future__ import annotations

import yaml

from .client import HAClient

OUTDOOR_HINTS = ("outdoor", "outside", "external", "garden", "balcony")
LOFT_HINTS = ("loft", "attic")


GAS_RATE_HINTS = ("gas",)


def categorise(states: list[dict]) -> dict[str, list[dict]]:
    cats: dict[str, list[dict]] = {
        "indoor_temperature": [],
        "outdoor_temperature": [],
        "loft_temperature": [],
        "heating_power": [],
        "climate": [],
        "weather": [],
        "energy": [],
        "co2": [],
        "gas_rate": [],
    }
    for s in states:
        eid = s["entity_id"]
        attrs = s.get("attributes", {})
        domain = eid.split(".")[0]
        name = (attrs.get("friendly_name") or eid).lower()
        if domain == "climate":
            cats["climate"].append(s)
        elif domain == "weather":
            cats["weather"].append(s)
        elif domain == "sensor":
            device_class = attrs.get("device_class")
            unit = attrs.get("unit_of_measurement", "")
            if device_class == "temperature" or unit in ("°C", "°F"):
                if any(h in name or h in eid for h in LOFT_HINTS):
                    cats["loft_temperature"].append(s)
                elif any(h in name or h in eid for h in OUTDOOR_HINTS):
                    cats["outdoor_temperature"].append(s)
                else:
                    cats["indoor_temperature"].append(s)
            elif unit == "%" and ("heating" in eid or "heating" in name):
                cats["heating_power"].append(s)
            elif device_class in ("energy", "gas") or unit in ("kWh", "m³"):
                cats["energy"].append(s)
            elif device_class == "carbon_dioxide":
                cats["co2"].append(s)
            elif (
                device_class == "monetary"
                and "kwh" in unit.lower()
                and any(h in name or h in eid for h in GAS_RATE_HINTS)
            ):
                cats["gas_rate"].append(s)
    return cats


def draft_config(cats: dict[str, list[dict]]) -> dict:
    def eid(s):
        return s["entity_id"]

    heating_by_zone = {}
    for s in cats["heating_power"]:
        zone = eid(s).removeprefix("sensor.").removesuffix("_heating")
        heating_by_zone[zone] = eid(s)

    rooms = {}
    for s in cats["indoor_temperature"]:
        room = eid(s).removeprefix("sensor.")
        for suffix in ("_temperature", "_temp", "_thermometer"):
            room = room.removesuffix(suffix)
        rooms[room] = {
            "temperature": eid(s),
            "heating_power": heating_by_zone.get(room),
        }

    return {
        "boiler_output_kw": 28,
        "boiler_efficiency": 0.88,
        "gas_kwh_entity": eid(cats["energy"][0]) if cats["energy"] else None,
        "gas_unit_rate_entity": eid(cats["gas_rate"][0]) if cats["gas_rate"] else None,
        "outdoor_entity": eid(cats["outdoor_temperature"][0]) if cats["outdoor_temperature"] else "FILL_ME_IN",
        "loft_entity": eid(cats["loft_temperature"][0]) if cats["loft_temperature"] else "FILL_ME_IN",
        "co2_entity": eid(cats["co2"][0]) if cats["co2"] else None,
        # External statistic (e.g. from a water utility integration), not a
        # sensor.* entity - not visible here since this only scans
        # /api/states. Find it in HA's Developer Tools > Statistics, e.g.
        # "thames_water:thameswater_consumption".
        "water_stat": None,
        "ceiling_height_m": None,
        "weather_entity": eid(cats["weather"][0]) if cats["weather"] else None,
        "night_start": "23:30",
        "night_end": "06:30",
        "rooms": rooms,
    }


def run(config_path: str = "config.yaml") -> None:
    client = HAClient()
    print(f"Connected: {client.ping()} ({client.url})")
    cats = categorise(client.states())

    for cat, items in cats.items():
        print(f"\n{cat} ({len(items)}):")
        for s in items:
            name = s.get("attributes", {}).get("friendly_name", "")
            print(f"  {s['entity_id']:55s} {s['state']:>10s}  {name}")

    cfg = draft_config(cats)
    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    print(f"\nDraft written to {config_path} — review it before running analyses:")
    print("  * check the outdoor/loft guesses (matched by name)")
    print("  * remove rooms that aren't rooms (e.g. fridge/boiler sensors)")
    print("  * set boiler_output_kw from your Worcester Bosch model plate")
    print("  * fill in co2_entity + floor_area_m2 + ceiling_height_m to unlock "
          "the `ventilation` command (ventilation vs fabric loss split)")
    print("  * fill in water_stat (an external statistic id, not a sensor.* "
          "entity - see Developer Tools > Statistics) to unlock the "
          "informational hot-water-fraction regression in `dhw`")
