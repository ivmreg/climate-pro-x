"""Regression checks against an immutable, rounded and de-identified extract."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ha_efficiency import hlc

FIXTURE = Path(__file__).parent / "fixtures" / "sanitized_heating_days.csv"


def test_sanitized_heating_fixture_matches_independent_ols_reference():
    data = pd.read_csv(FIXTURE)
    index = pd.date_range("2000-01-01", periods=len(data), freq="1D", tz="UTC")
    gas = pd.Series(data.gas_kwh.to_numpy(), index=index)
    delta_t = pd.Series(data.delta_t_k.to_numpy(), index=index)

    # Independent closed-form reference, intentionally separate from fit_hlc.
    covariance = float(np.sum((delta_t - delta_t.mean()) * (gas - gas.mean())))
    variance = float(np.sum((delta_t - delta_t.mean()) ** 2))
    reference_hlc = covariance / variance * 1000 / 24

    result = hlc.fit_hlc(gas, delta_t)

    assert "note" not in result
    assert result["days"] == 60
    assert result["hlc_w_per_k"] == pytest.approx(reference_hlc, rel=1e-12)
    assert result["hlc_w_per_k"] == pytest.approx(324, abs=2)
    assert result["r_squared"] == pytest.approx(0.657, abs=0.01)
    assert result["hlc_ci_low_w_per_k"] > 250
    assert result["hlc_ci_high_w_per_k"] < 400
