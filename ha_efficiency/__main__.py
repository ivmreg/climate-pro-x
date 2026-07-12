from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import pandas as pd
import yaml

from . import cooling, dhw, discover, hlc, loft, store, ventilation
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
    if cfg.get("co2_entity"):
        entities.append(cfg["co2_entity"])
    entities.extend(cfg.get("co2_entities") or [])
    if cfg.get("outdoor_co2_entity"):
        entities.append(cfg["outdoor_co2_entity"])
    if cfg.get("gas_unit_rate_entity"):
        entities.append(cfg["gas_unit_rate_entity"])
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
        # water_stat is an external statistic, not a sensor.* entity - only
        # reachable via the LTS websocket path, not the REST history API.
        if cfg.get("water_stat"):
            entities = entities + [cfg["water_stat"]]
        print(f"Pulling hourly long-term statistics, {len(entities)} entities, {args.days} days …")
        series = lts.fetch(entities, start)
    else:
        client = HAClient()
        print(f"Pulling {len(entities)} entities, {args.days} days …")
        series = client.history_chunked(entities, start, end)
    cumulative_entities = {
        entity
        for entity in (cfg.get("gas_kwh_entity"), cfg.get("water_stat"))
        if entity
    }
    store.save(
        series,
        source="lts" if args.lts else "rest",
        kind_by_entity={
            entity: "cumulative" if entity in cumulative_entities else "measurement"
            for entity in series
        },
    )
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
    fitted = result
    efficiency = 1.0
    if gas_entity:
        outdoor_daily = outdoor.resample("1D").mean()
        corrected = dhw.corrected_hlc(q_daily, dt_daily, outdoor_daily)
        fitted = corrected or result
        efficiency = cfg.get("boiler_efficiency", 0.88)
    fuel_or_proxy_value = fitted["hlc_w_per_k"]
    value = fuel_or_proxy_value * efficiency
    print(f"Delivered Heat Loss Coefficient: {value:.0f} W/K  "
          f"(R² {fitted['r_squared']:.2f})")
    if gas_entity:
        print(f"Fuel-input slope: {fuel_or_proxy_value:.0f} W/K; "
              f"boiler efficiency assumption: {efficiency:.0%}")
        print("DHW correction: " + ("applied" if fitted is not result else "not available"))
    else:
        print("Heat input is a Tado demand proxy; use this result for trends, not benchmarking.")
    print(f"Regression intercept: {fitted['regression_intercept_kwh_per_day']:.1f} kWh/day")
    print(f"Benchmark: {hlc.benchmark(value)}")
    _plot_hlc(fitted)


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


def _gas_daily_inputs(cfg: dict) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """(q_daily, dt_daily, outdoor_daily, gas) shared by ventilation/dhw."""
    outdoor = store.load_resampled(cfg["outdoor_entity"])
    temps = _room_series(cfg, "temperature")
    gas_entity = cfg.get("gas_kwh_entity")
    if outdoor is None or not temps or not gas_entity:
        sys.exit("Missing cached data — run `pull` first "
                 "(need outdoor, rooms, gas_kwh_entity).")
    dt_daily = hlc.daily_delta_t(temps, outdoor)
    gas = store.load(gas_entity)
    q_daily = hlc.daily_heat_input_from_meter(gas)
    outdoor_daily = outdoor.resample("1D").mean()
    return q_daily, dt_daily, outdoor_daily, gas


def cmd_ventilation(args) -> None:
    cfg = load_config()
    co2_entities = [
        entity
        for entity in [cfg.get("co2_entity"), *(cfg.get("co2_entities") or [])]
        if entity
    ]
    if not co2_entities:
        sys.exit("Set co2_entity or co2_entities in config.yaml first.")
    outdoor_baseline = cfg.get("outdoor_co2_ppm")
    outdoor_co2_entity = cfg.get("outdoor_co2_entity")
    if outdoor_co2_entity:
        outdoor_co2 = store.load(outdoor_co2_entity)
        if outdoor_co2 is not None and not outdoor_co2.empty:
            outdoor_baseline = float(outdoor_co2.median())
    fits = []
    for entity in co2_entities:
        co2 = store.load(entity)
        if co2 is not None:
            fit = ventilation.air_change_rate(co2, outdoor_baseline)
            if fit:
                fits.append(fit)
    if not fits:
        sys.exit("Not enough clean CO2 decay windows yet — pull more history.")
    fit = {
        "ach": median(item["ach"] for item in fits),
        "windows": sum(item["windows"] for item in fits),
        "baseline_ppm": median(item["baseline_ppm"] for item in fits),
    }
    print(f"\nAir-change rate: {fit['ach']:.2f} /h  ({fit['windows']} decay windows, "
          f"{len(fits)} sensor(s), outdoor CO2 baseline {fit['baseline_ppm']:.0f} ppm)")

    floor_area = cfg.get("floor_area_m2")
    ceiling = cfg.get("ceiling_height_m")
    if not (floor_area and ceiling):
        print("Set floor_area_m2 and ceiling_height_m in config.yaml to see the W/K split.")
        return

    q_daily, dt_daily, outdoor_daily, _gas = _gas_daily_inputs(cfg)
    corrected = dhw.corrected_hlc(q_daily, dt_daily, outdoor_daily)
    if corrected:
        space_heating_hlc = corrected["hlc_w_per_k"]
        print(f"Using DHW-corrected space-heating HLC: {space_heating_hlc:.0f} W/K")
    else:
        raw = hlc.fit_hlc(q_daily, dt_daily)
        if "note" in raw:
            sys.exit(raw["note"])
        space_heating_hlc = raw["hlc_w_per_k"]
        print("Using raw HLC (not enough summer data yet for a DHW correction): "
              f"{space_heating_hlc:.0f} W/K")

    boiler_eff = cfg.get("boiler_efficiency", 0.88)
    split = ventilation.split_losses(
        fit["ach"], floor_area, ceiling, space_heating_hlc, boiler_eff
    )
    if split is None:
        sys.exit(
            "Ventilation loss exceeds the delivered HLC or an input is invalid; "
            "the fabric/ventilation split has been suppressed."
        )
    print(f"\nVentilation loss: {split['ventilation_w_per_k']:.0f} W/K "
          f"({split['ventilation_share_pct']:.0f}% of delivered)")
    print(f"Fabric loss:      {split['fabric_w_per_k']:.0f} W/K")
    print(f"(delivered HLC {split['hlc_delivered_w_per_k']:.0f} W/K at "
          f"{boiler_eff * 100:.0f}% boiler efficiency)")


def cmd_dhw(args) -> None:
    cfg = load_config()
    q_daily, dt_daily, outdoor_daily, gas = _gas_daily_inputs(cfg)

    baseline = dhw.dhw_baseline(q_daily, dt_daily, outdoor_daily)
    if not baseline:
        sys.exit("Not enough summer (heating-off) days cached yet for a DHW baseline.")
    print(f"\nNon-space-heating gas baseline: {baseline['kwh_per_day']:.1f} kWh/day "
          f"({baseline['days_used']} summer days used)")

    rate_entity = cfg.get("gas_unit_rate_entity")
    if rate_entity:
        # Live state, not cached LTS: a tariff sensor is state_class "total"
        # but isn't a real meter, so the recorder's long-term "sum" for it is
        # meaningless noise (seen as ~0 GBP/kWh) - only the current state is
        # a sensible rate. Same reasoning as the live integration's
        # coordinator._gas_unit_rate(), which reads hass.states directly.
        rate = None
        try:
            state = next(
                (s for s in HAClient().states() if s["entity_id"] == rate_entity), None
            )
            if state and state["state"] not in ("unknown", "unavailable"):
                rate = float(state["state"])
        except Exception:
            rate = None
        if rate:
            state_unit = (state.get("attributes") or {}).get("unit_of_measurement", "")
            normalized_unit = str(state_unit).casefold().replace(" ", "")
            if normalized_unit in {"p/kwh", "pence/kwh"}:
                rate /= 100
            elif normalized_unit in {"gbp/mwh", "£/mwh"}:
                rate /= 1000
            elif normalized_unit not in {"gbp/kwh", "£/kwh"}:
                rate = None
        if rate:
            per_day = baseline["kwh_per_day"] * rate
            modelled = outdoor_daily.dropna().apply(
                lambda value: dhw.dhw_daily_kwh(value, baseline)
            )
            annual_kwh = (
                float(modelled.mean()) * 365 if not modelled.empty
                else baseline["kwh_per_day"] * 365
            )
            print(f"Cost: £{per_day:.2f}/baseline day (£{annual_kwh * rate:.0f}/modelled year "
                  f"at {rate * 100:.1f}p/kWh)")
        else:
            print("\ngas_unit_rate_entity configured but its live value "
                  "wasn't available (need a live HA connection).")

    water_stat = cfg.get("water_stat")
    if water_stat:
        water = store.load(water_stat)
        if water is None:
            print("\nwater_stat configured but not cached — run `pull --lts` first.")
        else:
            gas_hourly = dhw.hourly_change(gas, dhw.GAS_MAX_STEP_KWH)
            water_hourly = dhw.hourly_change(water, dhw.WATER_MAX_STEP_L)
            wfit = dhw.fit_water_gas(
                gas_hourly,
                water_hourly,
                cfg.get("boiler_efficiency", 0.88),
            )
            if wfit:
                print(f"\n(informational) hourly gas-vs-water regression: "
                      f"{wfit['wh_per_litre']:.1f} Wh/L, ~{wfit['hot_fraction_pct']:.0f}% "
                      f"of metered water is hot (R² {wfit['regression_r_squared']:.2f}, "
                      f"{wfit['regression_hours']} hours)")
            else:
                print("\n(informational) not enough overlapping gas/water hours yet "
                      "for the Wh-per-litre regression.")

    corrected = dhw.corrected_hlc(q_daily, dt_daily, outdoor_daily)
    if corrected:
        efficiency = cfg.get("boiler_efficiency", 0.88)
        print(f"\nDHW-corrected delivered HLC: "
              f"{corrected['hlc_w_per_k'] * efficiency:.0f} W/K "
              f"(fuel-input slope {corrected['hlc_w_per_k']:.0f} W/K, "
              f"R² {corrected['r_squared']:.2f}, {corrected['days']} days)")


def cmd_cache_audit(args) -> None:
    """Report cache provenance and refuse unsafe cumulative reconstruction."""
    cfg = load_config()
    entity_id = args.entity or cfg.get("gas_kwh_entity")
    if not entity_id:
        sys.exit("Pass an entity id or configure gas_kwh_entity first.")
    report = store.audit(
        entity_id, cumulative=args.cumulative, max_step=args.max_step
    )
    print(f"\nCache audit: {entity_id}")
    for source, details in report["sources"].items():
        print(
            f"  {source:8s} {details['rows']:6d} rows  "
            f"{details['start']} -> {details['end']}  "
            f"gaps={details['large_gaps']}"
        )
        if args.cumulative:
            print(
                f"           resets={details.get('negative_steps', 0)} "
                f"large_steps={details.get('over_limit_steps', 0)} "
                f"mixed_baseline={details.get('mixed_baseline_likely', False)}"
            )
    for warning in report["warnings"]:
        print(f"  WARNING: {warning}")
    if args.repair:
        outcome = store.repair(
            entity_id, source=args.source, cumulative=args.cumulative
        )
        print(f"Repair: {outcome}")


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
    sub.add_parser("ventilation", help="ventilation vs fabric heat loss split (needs a CO2 sensor)")
    sub.add_parser("dhw", help="hot-water gas cost + DHW-corrected HLC")
    p_audit = sub.add_parser("cache-audit", help="inspect cached data quality/provenance")
    p_audit.add_argument("entity", nargs="?")
    p_audit.add_argument("--cumulative", action="store_true")
    p_audit.add_argument("--max-step", type=float, default=40.0)
    p_audit.add_argument("--repair", action="store_true")
    p_audit.add_argument("--source", default="legacy")
    args = parser.parse_args()
    {
        "discover": lambda a: discover.run(),
        "pull": cmd_pull,
        "cooling": cmd_cooling,
        "hlc": cmd_hlc,
        "loft": cmd_loft,
        "ventilation": cmd_ventilation,
        "dhw": cmd_dhw,
        "cache-audit": cmd_cache_audit,
    }[args.command](args)


if __name__ == "__main__":
    main()
