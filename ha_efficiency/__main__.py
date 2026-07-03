from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

from . import cooling, discover, hlc, loft, store
from .client import HAClient


def load_config() -> dict:
    path = Path("config.yaml")
    if not path.exists():
        sys.exit("config.yaml not found — run `python -m ha_efficiency discover` first.")
    return yaml.safe_load(path.read_text())


def config_entities(cfg: dict) -> list[str]:
    entities = [cfg["outdoor_entity"], cfg.get("loft_entity")]
    if cfg.get("gas_kwh_entity"):
        entities.append(cfg["gas_kwh_entity"])
    for room in cfg["rooms"].values():
        entities.append(room["temperature"])
        if room.get("heating_power"):
            entities.append(room["heating_power"])
    return [e for e in entities if e and e != "FILL_ME_IN"]


def cmd_pull(args) -> None:
    cfg = load_config()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    entities = config_entities(cfg)
    if args.lts:
        from . import lts
        print(f"Pulling hourly long-term statistics, {len(entities)} entities, {args.days} days …")
        series = lts.fetch(entities, start)
    else:
        client = HAClient()
        print(f"Pulling {len(entities)} entities, {args.days} days …")
        series = client.history_chunked(entities, start, end)
    store.save(series)
    for eid in entities:
        got = series.get(eid)
        span = f"{got.index[0]:%Y-%m-%d} → {got.index[-1]:%Y-%m-%d} ({len(got)} pts)" \
            if got is not None and len(got) else "NO DATA"
        print(f"  {eid:55s} {span}")
    print("Cached in data/ (additive — re-run any time to extend).")


def _room_series(cfg: dict, key: str) -> dict[str, pd.Series]:
    out = {}
    for room, spec in cfg["rooms"].items():
        eid = spec.get(key)
        if eid:
            s = store.load_resampled(eid)
            if s is not None:
                out[room] = s
    return out


def cmd_cooling(args) -> None:
    cfg = load_config()
    outdoor = store.load_resampled(cfg["outdoor_entity"])
    if outdoor is None:
        sys.exit("No outdoor data cached — run `pull` first.")
    temps = _room_series(cfg, "temperature")
    heating = _room_series(cfg, "heating_power")
    fits = {
        room: cooling.analyse_room(
            series, outdoor, heating.get(room), cfg["night_start"], cfg["night_end"]
        )
        for room, series in temps.items()
    }
    summary = cooling.summarise(fits)
    print("\nPer-room thermal time constants (low tau = fast-cooling = leaky):\n")
    print(summary.to_string(index=False))
    total = int(summary["nights_fitted"].sum())
    if total == 0:
        print("\nNo usable cooling windows found. Common causes: heating runs "
              "overnight (no free cooldown), mild weather (dT < 3 K), or "
              "less history than one full night — pull more days.")
    else:
        print("\nRule of thumb: tau > 20 h is good for a solid-wall flat, "
              "10–20 h typical, < 10 h suggests draughts/poor glazing in that room.")


def cmd_hlc(args) -> None:
    cfg = load_config()
    outdoor = store.load_resampled(cfg["outdoor_entity"])
    temps = _room_series(cfg, "temperature")
    if outdoor is None or not temps:
        sys.exit("Missing cached data — run `pull` first.")
    dt_daily = hlc.daily_delta_t(temps, outdoor)

    gas_entity = cfg.get("gas_kwh_entity")
    if gas_entity:
        gas = store.load(gas_entity)
        q_daily = hlc.daily_heat_input_from_meter(gas)
        source = f"gas meter ({gas_entity})"
    else:
        heating = _room_series(cfg, "heating_power")
        if not heating:
            sys.exit("No gas_kwh_entity and no heating_power sensors configured — "
                     "need at least one heat-input source for HLC.")
        q_daily = hlc.daily_heat_input_from_tado(heating, cfg["boiler_output_kw"])
        source = f"Tado heating power x {cfg['boiler_output_kw']} kW boiler"

    result = hlc.fit_hlc(q_daily, dt_daily)
    print(f"\nHeat input source: {source}")
    print(f"Usable heating days: {result['days']}")
    if "note" in result:
        print(result["note"])
        return
    value = result["hlc_w_per_k"]
    print(f"Heat Loss Coefficient: {value:.0f} W/K  (R² {result['r_squared']:.2f})")
    print(f"Free gains: ~{result['free_gains_kwh_per_day']:.1f} kWh/day")
    print(f"Benchmark: {hlc.benchmark(value)}")
    _plot_hlc(result)


def _plot_hlc(result: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = result["data"]
    Path("output").mkdir(exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(df.dt, df.q, alpha=0.7)
    xs = pd.Series([df.dt.min(), df.dt.max()])
    slope = result["hlc_w_per_k"] * 24 / 1000
    ax.plot(xs, slope * xs - result["free_gains_kwh_per_day"], "r--")
    ax.set_xlabel("Daily mean indoor-outdoor ΔT (K)")
    ax.set_ylabel("Daily heat input (kWh)")
    ax.set_title(f"HLC fit: {result['hlc_w_per_k']:.0f} W/K over {result['days']} days")
    fig.tight_layout()
    fig.savefig("output/hlc_fit.png", dpi=120)
    print("Plot: output/hlc_fit.png")


def cmd_loft(args) -> None:
    cfg = load_config()
    outdoor = store.load_resampled(cfg["outdoor_entity"])
    loft_series = store.load_resampled(cfg["loft_entity"])
    temps = _room_series(cfg, "temperature")
    if outdoor is None or loft_series is None or not temps:
        sys.exit("Missing cached data — run `pull` first (and set loft_entity).")
    result = loft.loft_ratio(temps, loft_series, outdoor)
    print(f"\nCold-night hours used: {result['hours_used']}")
    if "note" in result:
        print(result["note"])
        return
    print(f"Loft ratio (T_loft-T_out)/(T_in-T_out): {result['ratio']:.2f}")
    print(f"Verdict: {result['verdict']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ha_efficiency")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="list HA entities, draft config.yaml")
    p_pull = sub.add_parser("pull", help="cache history from HA")
    p_pull.add_argument("--days", type=int, default=10)
    p_pull.add_argument("--lts", action="store_true",
                        help="hourly long-term statistics (reaches back past "
                             "recorder retention; use for past heating seasons)")
    sub.add_parser("cooling", help="per-room thermal time constants")
    sub.add_parser("hlc", help="whole-home heat loss coefficient")
    sub.add_parser("loft", help="ceiling vs roof loss analysis")
    args = parser.parse_args()
    {
        "discover": lambda a: discover.run(),
        "pull": cmd_pull,
        "cooling": cmd_cooling,
        "hlc": cmd_hlc,
        "loft": cmd_loft,
    }[args.command](args)


if __name__ == "__main__":
    main()
