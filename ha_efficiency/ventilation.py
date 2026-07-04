"""Whole-home air-change rate from CO2 decay curves, and the resulting split
of the delivered space-heating HLC into ventilation vs fabric losses.

Model: with no fresh CO2 source (room unoccupied / no combustion), indoor
CO2 relaxes toward an outdoor baseline as C(t) = C_out + (C0-C_out)e^(-ACH t),
so ln(C-C_out) is linear in t with slope -ACH. Falling stretches of the
series are auto-detected and fit individually; the median ACH over clean
fits is reported. Mirrors thermal_math.air_change_rate in the HA
integration (custom_components/thermal_efficiency/thermal_math.py) - this
pandas version is for offline exploration/CLI use and cross-validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CO2_MIN_WINDOW_H = 4.0
CO2_MIN_DROP_PPM = 120.0
CO2_MIN_R2 = 0.9
CO2_BASELINE_PCT = 0.02  # outdoor CO2 estimated as this low percentile of the series
CO2_MIN_WINDOWS = 10
CO2_MAX_GAP = pd.Timedelta("1.5h")  # bigger gaps break the exponential-decay assumption
AIR_HEAT_CAPACITY = 0.335  # Wh/(m3*K), volumetric heat capacity of air


def air_change_rate(co2: pd.Series) -> dict | None:
    """Median air-change rate (1/h) from CO2 decay curves; see module
    docstring for the model."""
    co2 = co2.dropna().sort_index()
    if len(co2) < 2:
        return None
    baseline = float(co2.quantile(CO2_BASELINE_PCT))

    times, values = co2.index, co2.values
    n = len(co2)
    fits = []
    i = 0
    while i < n - 1:
        j = i
        while (
            j + 1 < n
            and times[j + 1] - times[j] <= CO2_MAX_GAP
            and values[j + 1] <= values[j] + 2  # tolerate small sensor noise
        ):
            j += 1
        window_hours = (times[j] - times[i]).total_seconds() / 3600
        if window_hours >= CO2_MIN_WINDOW_H and values[i] - baseline >= CO2_MIN_DROP_PPM:
            excess = values[i : j + 1] - baseline
            if (excess > 0).all():
                t = (times[i : j + 1] - times[i]).total_seconds() / 3600
                y = np.log(excess)
                slope, intercept = np.polyfit(t, y, 1)
                pred = slope * t + intercept
                ss_res = float(np.sum((y - pred) ** 2))
                ss_tot = float(np.sum((y - y.mean()) ** 2))
                r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
                if slope < 0 and r2 >= CO2_MIN_R2:
                    fits.append(-slope)
        i = j if j > i else i + 1

    if len(fits) < CO2_MIN_WINDOWS:
        return None
    return {"ach": float(np.median(fits)), "windows": len(fits), "baseline_ppm": baseline}


def split_losses(
    ach: float,
    floor_area_m2: float,
    ceiling_height_m: float,
    space_heating_hlc_w_per_k: float,
    boiler_efficiency: float = 0.88,
) -> dict:
    """Ventilation vs fabric split of the delivered space-heating HLC."""
    volume = floor_area_m2 * ceiling_height_m
    ventilation_w_per_k = AIR_HEAT_CAPACITY * ach * volume
    hlc_delivered = space_heating_hlc_w_per_k * boiler_efficiency
    fabric_w_per_k = max(0.0, hlc_delivered - ventilation_w_per_k)
    return {
        "ventilation_w_per_k": ventilation_w_per_k,
        "fabric_w_per_k": fabric_w_per_k,
        "hlc_delivered_w_per_k": hlc_delivered,
        "ventilation_share_pct": (
            ventilation_w_per_k / hlc_delivered * 100 if hlc_delivered > 0 else None
        ),
    }
