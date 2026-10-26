"""Offline, independent HLC replay from an exported analysis evidence bundle.

This checks the fitted daily target with closed-form OLS. It does not recreate
hourly source eligibility, re-estimate DHW, or establish causal retrofit effects.
It needs no Home Assistant connection or optional numerical packages.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from math import isfinite, sqrt
from pathlib import Path
from statistics import mean


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as err:
        raise ValueError(f"{field} must be a finite number") from err
    if not isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _ols(points: list[tuple[date, float, float]]) -> dict:
    if len(points) < 3:
        raise ValueError("At least three included days are required for replay")
    xs, ys = [p[1] for p in points], [p[2] for p in points]
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        raise ValueError("Included daily temperature differences have no variation")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    sse = sum(r * r for r in residuals)
    syy = sum((y - my) ** 2 for y in ys)
    return {
        "days_used": len(points), "slope_kwh_per_day_per_k": slope,
        "regression_intercept_kwh_per_day": intercept,
        "fuel_input_hlc_w_per_k": slope * 1000 / 24,
        "r_squared": 1 - sse / syy if syy > 0 else 0.0,
    }


def replay_hlc_evidence(document: dict) -> dict:
    """Accept a bare bundle or HA diagnostics containing ``analysis_evidence``."""
    if not isinstance(document, dict):
        raise ValueError("Evidence document must be an object")
    # HA's download view wraps integration-specific diagnostics under `data`.
    # Accept that real download format as well as an extracted integration
    # diagnostics object or bare evidence bundle.
    integration_data = document.get("data")
    if isinstance(integration_data, dict) and "analysis_evidence" in integration_data:
        document = integration_data
    bundle = document.get("analysis_evidence", document)
    if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int
            or bundle["schema_version"] != 1):
        raise ValueError("Unsupported analysis evidence schema")
    if bundle.get("model_version") != "1":
        raise ValueError("Unsupported HLC evidence model version")
    configuration = bundle.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("Evidence configuration is missing")
    efficiency = _number(configuration.get("boiler_efficiency_used"), "boiler efficiency")
    if not 0.5 <= efficiency <= 1:
        raise ValueError("Boiler efficiency must be between 0.5 and 1")
    rows = bundle.get("daily")
    if not isinstance(rows, list):
        raise ValueError("Daily evidence must be a list")
    points = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each daily evidence row must be an object")
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("Each daily evidence row needs a valid ISO date") from err
        if day in seen:
            raise ValueError("Duplicate evidence date")
        seen.add(day)
        if row.get("hlc_included") is not True:
            continue
        if not isinstance(row.get("eligible"), dict) or row["eligible"].get("hlc") is not True:
            raise ValueError("An included HLC day must be eligible")
        points.append((day, _number(row.get("delta_t_k"), "delta_t_k"),
                       _number(row.get("hlc_adjusted_kwh"), "hlc_adjusted_kwh")))
    points.sort()
    fit = _ols(points)
    fit["delivered_hlc_w_per_k"] = fit["fuel_input_hlc_w_per_k"] * efficiency
    fit["boiler_efficiency_used"] = efficiency
    fit["first_included_day"], fit["last_included_day"] = points[0][0].isoformat(), points[-1][0].isoformat()
    models = bundle.get("models")
    if models is None:
        models = {}
    if not isinstance(models, dict) or (models.get("hlc") is not None and not isinstance(models["hlc"], dict)):
        raise ValueError("Reported HLC model must be an object or null")
    expected = models.get("hlc") or {}
    comparisons = {}
    for expected_key, actual_key in (
        ("hlc_w_per_k", "delivered_hlc_w_per_k"),
        ("regression_intercept_kwh_per_day", "regression_intercept_kwh_per_day"),
        ("r_squared", "r_squared"), ("days_used", "days_used"),
    ):
        if expected.get(expected_key) is not None:
            reference = _number(expected[expected_key], expected_key)
            actual = fit[actual_key]
            tolerance = max(1e-8, abs(reference) * 1e-9)
            comparisons[expected_key] = {
                "reported": reference, "replayed": actual,
                "matches": abs(actual - reference) <= tolerance,
            }
    # A diagnostic temporal split of the already attributed target, not an
    # independent end-to-end validation of the DHW model or input selection.
    holdout = {"status": "collecting", "reason": "Need at least 25 included days (20 training, 5 test)"}
    if len(points) >= 25:
        split = max(20, min(len(points) - 5, int(len(points) * 0.8)))
        train, test = points[:split], points[split:]
        try:
            trained = _ols(train)
        except ValueError:
            holdout = {"status": "unavailable", "reason": "Training temperature differences have no variation"}
        else:
            errors = [y - (trained["slope_kwh_per_day_per_k"] * x + trained["regression_intercept_kwh_per_day"])
                      for _, x, y in test]
            holdout = {
                "status": "available", "training_days": len(train), "test_days": len(test),
                "first_test_day": test[0][0].isoformat(),
                "mae_kwh_per_24h": mean(abs(e) for e in errors),
                "rmse_kwh_per_24h": sqrt(mean(e * e for e in errors)),
                "scope": "Temporal holdout of the frozen attributed target; DHW and eligibility are not independently retrained",
            }
    return {
        "scope": "Independent OLS replay of included, 24-hour-normalized HLC targets only",
        "evidence_id": bundle.get("evidence_id"), "fit": fit,
        "comparisons": comparisons,
        "matches_reported": all(c["matches"] for c in comparisons.values()) if comparisons else None,
        "temporal_holdout": holdout,
        "limitations": "Does not replay hourly eligibility, DHW training, confidence intervals, room/loft/CO2 fits or establish causal effects",
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Downloaded diagnostics or analysis evidence JSON")
    args = parser.parse_args(argv)
    try:
        result = replay_hlc_evidence(json.loads(args.path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as err:
        parser.exit(2, f"Evidence replay failed: {err}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    if result["matches_reported"] is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
