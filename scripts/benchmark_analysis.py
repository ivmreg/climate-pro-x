#!/usr/bin/env python3
"""Benchmark analysis tool for Climate Pro X core thermal models.

Measures wall-clock execution time and peak memory consumption for representative
room rosters and historical observation windows using synthetic data (no household
data or live credentials).

Workloads are measured both with complete hourly histories and with 1% synthetic
gaps. Timing and peak memory include deterministic input generation and analysis.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
import time
import tracemalloc
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Load pure-Python thermal_math directly without loading Home Assistant packages
REPO_ROOT = Path(__file__).resolve().parent.parent
MATH_FILE = (
    REPO_ROOT / "custom_components" / "thermal_efficiency" / "thermal_math.py"
)

spec = importlib.util.spec_from_file_location("thermal_math", MATH_FILE)
if spec is None or spec.loader is None:
    raise ImportError(f"Could not load thermal_math from {MATH_FILE}")
thermal_math = importlib.util.module_from_spec(spec)
spec.loader.exec_module(thermal_math)

TZ_LONDON = ZoneInfo("Europe/London")


def generate_synthetic_data(
    num_rooms: int,
    num_days: int,
    seed: int = 42,
    gap_rate: float = 0.01,
) -> dict:
    """Generate deterministic synthetic sensor history with realistic gaps."""
    rng = random.Random(seed)
    start_day = date(2025, 10, 1)

    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=TZ_LONDON)
    start_ts = int(start_dt.timestamp())
    total_hours = num_days * 24

    outdoor_series = {}
    room_series = [{} for _ in range(num_rooms)]
    heating_power_series = [{} for _ in range(num_rooms)]
    co2_series = {}

    cum_gas_kwh = 1000.0
    gas_sum_series = {start_ts - 3600: cum_gas_kwh}
    cum_water_l = 50000.0
    water_sum_series = {start_ts - 3600: cum_water_l}

    for h in range(total_hours):
        ts = start_ts + h * 3600
        day_offset = h // 24
        hour_of_day = h % 24

        # Occasional 1% data drop to simulate real packet loss
        dropped = rng.random() < gap_rate

        # Outdoor temperature: seasonal swing + daily diurnal cycle
        season_phase = (day_offset / 365.0) * 2 * math.pi
        daily_phase = (hour_of_day / 24.0) * 2 * math.pi
        t_out = (
            7.0
            - 5.0 * math.cos(season_phase)
            + 3.0 * math.sin(daily_phase - 1.5)
            + rng.uniform(-0.5, 0.5)
        )
        outdoor_series[ts] = round(t_out, 2)

        # Room temperatures: thermostat setpoint ~20C with slight setback and room variance
        for r_idx in range(num_rooms):
            base_setpoint = 19.5 + (r_idx % 3) * 0.8
            if hour_of_day < 6 or hour_of_day > 23:
                base_setpoint -= 1.5  # Night setback
            t_room = base_setpoint + rng.uniform(-0.3, 0.3)
            room_series[r_idx][ts] = round(t_room, 2)

            # Heating power %
            if t_room < base_setpoint and t_out < 15.0 and (6 <= hour_of_day <= 22):
                pct = min(
                    100.0,
                    max(10.0, (base_setpoint - t_room) * 50.0 + rng.uniform(0, 20)),
                )
            else:
                pct = 0.0
            heating_power_series[r_idx][ts] = round(pct, 1)

        # CO2 decay curve in room 0 during night hours (01:00 - 05:00)
        if 1 <= hour_of_day <= 5:
            decay_factor = math.exp(-0.4 * (hour_of_day - 1))
            co2_val = 420.0 + 650.0 * decay_factor + rng.uniform(-10.0, 10.0)
        else:
            co2_val = 420.0 + rng.uniform(200.0, 800.0)
        co2_series[ts] = round(co2_val, 1)

        # Gas meter: heating + DHW draws
        gas_hour = 0.0
        if t_out < 14.0 and (6 <= hour_of_day <= 22):
            gas_hour += rng.uniform(0.8, 2.5)  # Space heating
        if hour_of_day in (7, 8, 19, 20, 21):
            gas_hour += rng.uniform(0.5, 1.8)  # Hot water demand
        cum_gas_kwh += gas_hour
        gas_sum_series[ts] = round(cum_gas_kwh, 3)

        # Water meter: morning/evening draws
        water_hour = rng.uniform(1.0, 5.0)
        if hour_of_day in (7, 8, 19, 20):
            water_hour += rng.uniform(15.0, 45.0)
        cum_water_l += water_hour
        water_sum_series[ts] = round(cum_water_l, 1)
        if dropped:
            # Missing observations do not erase real consumption.
            for series in [outdoor_series, co2_series, gas_sum_series,
                           water_sum_series, *room_series, *heating_power_series]:
                series.pop(ts, None)

    return {
        "start_day": start_day,
        "num_days": num_days,
        "num_rooms": num_rooms,
        "outdoor": outdoor_series,
        "rooms": room_series,
        "heating_power": heating_power_series,
        "co2": co2_series,
        "gas_sum": gas_sum_series,
        "water_sum": water_sum_series,
    }


def benchmark_configuration(
    config_name: str, num_rooms: int, num_days: int, gap_rate: float
) -> dict:
    """Run full thermal calculations for a configuration, measuring time and memory."""
    tracemalloc.start()
    start_time = time.perf_counter()
    synthetic = generate_synthetic_data(
        num_rooms=num_rooms, num_days=num_days, gap_rate=gap_rate
    )

    def rows(series, metric):
        return [{"start": ts, metric: value} for ts, value in series.items()]

    stats = {
        "sensor.outdoor": rows(synthetic["outdoor"], "mean"),
        "sensor.gas": rows(synthetic["gas_sum"], "sum"),
        "sensor.water": rows(synthetic["water_sum"], "sum"),
        "sensor.co2": rows(synthetic["co2"], "mean"),
    }
    rooms = {}
    for idx in range(num_rooms):
        temperature = f"sensor.room_{idx}_temperature"
        heating = f"sensor.room_{idx}_heating"
        stats[temperature] = rows(synthetic["rooms"][idx], "mean")
        stats[heating] = rows(synthetic["heating_power"][idx], "mean")
        rooms[f"room_{idx}"] = {"temperature": temperature, "heating_power": heating}
    conf = {
        "outdoor": "sensor.outdoor", "gas_meter": "sensor.gas",
        "water": "sensor.water", "co2": "sensor.co2", "rooms": rooms,
        "outdoor_co2_ppm": 420.0,
    }
    start = datetime.combine(synthetic["start_day"], datetime.min.time(), tzinfo=TZ_LONDON)
    now = datetime.fromtimestamp(start.timestamp() + num_days * 86400, TZ_LONDON)
    result = thermal_math.compute_all(stats, conf, TZ_LONDON, now, (num_days,))

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    peak_mem_kb = peak_mem / 1024.0

    return {
        "configuration": config_name,
        "rooms": num_rooms,
        "history_days": num_days,
        "gap_rate": gap_rate,
        "observed_hourly_points": (
            len(synthetic["outdoor"])
            + sum(len(series) for series in synthetic["rooms"])
            + sum(len(series) for series in synthetic["heating_power"])
            + len(synthetic["co2"])
            + len(synthetic["gas_sum"])
            + len(synthetic["water_sum"])
        ),
        "elapsed_ms": round(elapsed_ms, 2),
        "peak_memory_kb": round(peak_mem_kb, 1),
        "pipeline": "thermal_math.compute_all",
        "metric_status": {key: value["status"] for key, value in result["analysis_status"].items() if key != "rooms"},
        "complete_gas_days": result["analysis_status"]["usage"].get("usable_observations"),
        "hlc_fitted": result["hlc"] is not None,
        "taus_fitted": sum(1 for fit in result["rooms"].values() if fit),
        "co2_fitted": result["air_change_rate"] is not None,
    }


def run_benchmarks() -> list[dict]:
    configs = [
        ("Small", 2, 30),
        ("Normal", 5, 90),
        ("Scaling", 12, 365),
    ]

    results = []
    for name, rooms, days in configs:
        for workload, gap_rate in (("complete", 0.0), ("1% gaps", 0.01)):
            res = benchmark_configuration(
                f"{name} ({workload})", rooms, days, gap_rate
            )
            results.append(res)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run thermal model performance benchmarks."
    )
    parser.add_argument(
        "--json", action="store_true", help="Output results in JSON format"
    )
    parser.add_argument(
        "--output", "-o", help="Write benchmark results to specified file"
    )

    args = parser.parse_args()

    results = run_benchmarks()

    if args.json:
        out_text = json.dumps(results, indent=2)
    else:
        lines = [
            "=" * 78,
            f"{'Workload':<22} | {'Rooms':<5} | {'Days':<5} | {'Points':<8} | {'Time (ms)':<10} | {'Peak Mem (KB)':<13}",
            "-" * 90,
        ]
        for r in results:
            lines.append(
                f"{r['configuration']:<22} | {r['rooms']:<5} | {r['history_days']:<5} | "
                f"{r['observed_hourly_points']:<8} | {r['elapsed_ms']:<10.2f} | {r['peak_memory_kb']:<13.1f}"
            )
        lines.append("=" * 90)
        out_text = "\n".join(lines)

    print(out_text)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text, encoding="utf-8")
        print(f"Benchmark results saved to: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
