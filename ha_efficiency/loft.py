"""Ceiling-vs-roof analysis from the loft temperature.

Steady state: heat flows indoor -> loft (through ceiling, resistance Rc) and
loft -> outdoor (through roof + ventilation, resistance Rr). Then

    r = (T_loft - T_out) / (T_in - T_out) = Rr / (Rc + Rr)

High r (loft nearly as warm as indoors): the ceiling leaks heat easily
relative to the roof — loft floor insulation would pay off directly.
Low r (loft nearly at outdoor temperature): the ceiling already resists well,
or the loft is heavily ventilated; extra loft insulation gains less.
"""

from __future__ import annotations

import pandas as pd


def loft_ratio(
    indoor_by_room: dict[str, pd.Series],
    loft: pd.Series,
    outdoor: pd.Series,
) -> dict:
    indoor = pd.DataFrame(indoor_by_room).mean(axis=1)
    df = pd.DataFrame(
        {"in": indoor, "loft": loft, "out": outdoor}
    ).interpolate(limit=24).dropna()
    dt = df["in"] - df["out"]
    # Only cold, steady periods: night hours with a real gradient, so sun on
    # the roof and daytime heating transients don't skew the ratio.
    mask = (dt > 6) & df.index.hour.isin([1, 2, 3, 4, 5])
    df, dt = df[mask], dt[mask]
    if len(df) < 12:
        return {"hours_used": len(df), "ratio": float("nan"),
                "note": "Not enough cold night hours in the data yet."}
    ratios = (df["loft"] - df["out"]) / dt
    ratio = ratios.median()
    iqr = ratios.quantile(0.75) - ratios.quantile(0.25)
    out_of_range_pct = float(((ratios < 0) | (ratios > 1)).mean() * 100)
    if not 0 <= ratio <= 1 or iqr > 0.5 or out_of_range_pct > 20:
        return {
            "hours_used": int(len(df)),
            "ratio": float("nan"),
            "note": "Loft observations failed physical bounds or stability checks.",
        }
    if ratio > 0.5:
        verdict = ("loft stays warm: the ceiling is the weak link — "
                   "loft floor insulation should give a direct win")
    elif ratio > 0.25:
        verdict = "moderate ceiling loss — loft insulation would still help"
    else:
        verdict = ("loft tracks outdoor temperature: ceiling already "
                   "insulating well (or loft is very ventilated); "
                   "walls/windows are likely the bigger losses")
    return {
        "hours_used": int(len(df)),
        "ratio": float(ratio),
        "iqr": float(iqr),
        "out_of_range_pct": out_of_range_pct,
        "verdict": verdict,
    }
