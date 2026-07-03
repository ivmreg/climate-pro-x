"""Self-check: recover known parameters from synthetic data.

Simulates a home with known physics, then verifies each analysis gets the
right answer back. Run: .venv/bin/python tests/synthetic_check.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from ha_efficiency import cooling, hlc, loft

rng = np.random.default_rng(42)
idx = pd.date_range("2026-01-01", "2026-01-15", freq="5min", tz="Europe/London")
hours = np.arange(len(idx)) * 5 / 60

# Outdoor: 5 degC mean, +-4 daily swing, slow weather drift
outdoor = pd.Series(
    5 + 4 * np.sin(2 * np.pi * (hours - 15) / 24)
    + np.cumsum(rng.normal(0, 0.01, len(idx))),
    index=idx,
)

# Room: held at 20 degC while heating (07:00-23:30), free exponential decay
# towards outdoor with TAU_TRUE overnight.
TAU_TRUE = 15.0
temp = np.empty(len(idx))
heat_on = np.array([(t.hour, t.minute) >= (7, 0) and (t.hour, t.minute) < (23, 30) for t in idx])
temp[0] = 20.0
step_h = 5 / 60
for i in range(1, len(idx)):
    if heat_on[i]:
        temp[i] = 20.0
    else:
        temp[i] = outdoor.iloc[i] + (temp[i - 1] - outdoor.iloc[i]) * np.exp(-step_h / TAU_TRUE)
room = pd.Series(temp + rng.normal(0, 0.03, len(idx)), index=idx)
heating_pct = pd.Series(np.where(heat_on, 40.0, 0.0), index=idx)

# --- cooling ---
fits = cooling.analyse_room(room, outdoor, heating_pct, "23:30", "06:30")
taus = [f.tau_hours for f in fits]
tau_med = float(np.median(taus))
assert len(fits) >= 8, f"expected >=8 nights fitted, got {len(fits)}"
assert abs(tau_med - TAU_TRUE) / TAU_TRUE < 0.15, f"tau {tau_med:.1f} vs true {TAU_TRUE}"
print(f"cooling ok: {len(fits)} nights, tau median {tau_med:.1f} h (true {TAU_TRUE})")

# --- hlc ---
# Daily heat input consistent with HLC_TRUE: q = HLC*dT*24/1000 - gains + noise
HLC_TRUE, GAINS = 250.0, 4.0
dt_daily = hlc.daily_delta_t({"room": room}, outdoor)
q_daily = (HLC_TRUE * dt_daily * 24 / 1000 - GAINS
           + pd.Series(rng.normal(0, 2, len(dt_daily)), index=dt_daily.index))
result = hlc.fit_hlc(q_daily, dt_daily)
got = result["hlc_w_per_k"]
assert abs(got - HLC_TRUE) / HLC_TRUE < 0.25, f"hlc {got:.0f} vs true {HLC_TRUE}"
print(f"hlc ok: {got:.0f} W/K (true {HLC_TRUE:.0f}), gains "
      f"{result['free_gains_kwh_per_day']:.1f} kWh/d (true {GAINS}), "
      f"R² {result['r_squared']:.2f}, {result['days']} days")

# --- loft ---
# Loft sits at known resistance ratio r=0.4 between indoor and outdoor
R_TRUE = 0.4
loft_series = outdoor + R_TRUE * (room - outdoor) + rng.normal(0, 0.1, len(idx))
res = loft.loft_ratio({"room": room}, loft_series, outdoor)
assert abs(res["ratio"] - R_TRUE) < 0.05, f"ratio {res['ratio']:.2f} vs true {R_TRUE}"
print(f"loft ok: ratio {res['ratio']:.2f} (true {R_TRUE}), {res['hours_used']} hours")

# --- meter input path ---
cumulative = q_daily.fillna(0).clip(lower=0).cumsum()
daily_from_meter = hlc.daily_heat_input_from_meter(cumulative)
assert (daily_from_meter.dropna() >= 0).all()
print("meter path ok")

print("\nall checks passed")
