"""Contract tests for trustworthy heat-loss regression results."""

from __future__ import annotations

import math


def _fit(thermal_math, days, dt_values, q_values, dhw_values=None):
    q_by_day = dict(zip(days, q_values))
    dt_by_day = dict(zip(days, dt_values))
    dhw_by_day = dict(zip(days, dhw_values)) if dhw_values is not None else None
    return thermal_math.fit_hlc(q_by_day, dt_by_day, days[0], dhw_by_day)


def test_hlc_recovers_known_positive_slope(thermal_math, forty_days):
    expected_w_per_k = 240.0
    slope_kwh_per_day_k = expected_w_per_k * 24 / 1000
    delta_t = [5.0 + offset % 10 for offset in range(len(forty_days))]
    energy = [2.0 + slope_kwh_per_day_k * dt for dt in delta_t]

    result = _fit(thermal_math, forty_days, delta_t, energy)

    assert result is not None
    assert math.isfinite(result["hlc_w_per_k"])
    assert result["hlc_w_per_k"] > 0
    assert result["hlc_w_per_k"] == pytest.approx(expected_w_per_k, rel=1e-9)
    assert result["r_squared"] == pytest.approx(1.0)
    assert result["days_used"] == len(forty_days)


def test_hlc_normalizes_23_and_25_hour_energy_to_24_hours():
    import pandas as pd

    from ha_efficiency import hlc

    index = pd.date_range("2026-02-20", periods=50, freq="1D", tz="Europe/London")
    delta_t = [5.0 + (i % 12) for i in range(len(index))]
    hours = [hlc._expected_day_hours(day) for day in index]
    slope = 240.0 * 24 / 1000
    q = [
        (2.0 + slope * dt) * day_hours / 24
        for dt, day_hours in zip(delta_t, hours)
    ]

    result = hlc.fit_hlc(pd.Series(q, index=index), pd.Series(delta_t, index=index))

    assert result["hlc_w_per_k"] == pytest.approx(240.0, rel=1e-9)


def test_cli_hlc_applies_measured_heating_off_days(monkeypatch):
    import pandas as pd

    import ha_efficiency.__main__ as cli

    expected_off_days = {pd.Timestamp("2026-01-01")}
    monkeypatch.setattr(cli, "load_config", lambda: {
        "outdoor_entity": "sensor.outdoor",
        "gas_kwh_entity": "sensor.gas",
    })
    def load_temperature(_entity, *, validator):
        assert validator(20) and validator(-20) and not validator(99999)
        return pd.Series(dtype=float)

    monkeypatch.setattr(cli.store, "load_resampled", load_temperature)
    monkeypatch.setattr(cli.store, "load", lambda _entity: pd.Series(dtype=float))
    monkeypatch.setattr(cli, "_room_series", lambda _cfg, _role: {"room": pd.Series(dtype=float)})
    monkeypatch.setattr(cli.hlc, "daily_delta_t", lambda _rooms, _outdoor: pd.Series(dtype=float))
    monkeypatch.setattr(cli.hlc, "daily_heat_input_from_meter", lambda _gas: pd.Series(dtype=float))
    monkeypatch.setattr(cli, "_dhw_context", lambda _cfg, _dt: {"heating_off": expected_off_days})
    seen = {}

    def fit(_q, _dt, *, heating_off=None):
        seen["heating_off"] = heating_off
        return {"days": 0, "note": "insufficient data"}

    monkeypatch.setattr(cli.hlc, "fit_hlc", fit)
    cli.cmd_hlc(None)

    assert seen["heating_off"] == expected_off_days


def test_hlc_rejects_negative_slope(thermal_math, forty_days):
    delta_t = [5.0 + offset % 10 for offset in range(len(forty_days))]
    energy = [80.0 - 2.0 * dt for dt in delta_t]

    assert _fit(thermal_math, forty_days, delta_t, energy) is None


def test_hlc_rejects_positive_but_uninformative_fit(thermal_math, forty_days):
    delta_t = [5.0 + offset % 10 for offset in range(len(forty_days))]
    # A small positive signal buried under balanced day-level variation.
    energy = [30.0 + 0.1 * dt + (10.0 if (i // 10) % 2 else -10.0)
              for i, dt in enumerate(delta_t)]

    assert _fit(thermal_math, forty_days, delta_t, energy) is None


def test_hlc_rejects_too_few_days_even_when_fit_is_perfect(thermal_math, forty_days):
    days = forty_days[:19]
    delta_t = [5.0 + offset for offset in range(len(days))]
    energy = [3.0 + 5.0 * dt for dt in delta_t]

    assert _fit(thermal_math, days, delta_t, energy) is None


def test_hlc_widens_interval_for_serially_correlated_residuals(
    thermal_math, forty_days
):
    """Two fits with identical scatter about the same line, differing only in
    how that scatter is arranged in time. The one whose errors persist across
    consecutive days carries less independent evidence, so it must report the
    wider interval - the point estimate is unchanged either way."""
    slope = 240.0 * 24 / 1000
    delta_t = [5.0 + offset % 10 for offset in range(len(forty_days))]
    # Same residual magnitude in both, alternating (uncorrelated) vs persisting
    # in week-long runs (correlated).
    alternating = [1.5 if i % 2 else -1.5 for i in range(len(forty_days))]
    persistent = [1.5 if (i // 7) % 2 else -1.5 for i in range(len(forty_days))]

    independent = _fit(
        thermal_math,
        forty_days,
        delta_t,
        [2.0 + slope * dt + e for dt, e in zip(delta_t, alternating)],
    )
    correlated = _fit(
        thermal_math,
        forty_days,
        delta_t,
        [2.0 + slope * dt + e for dt, e in zip(delta_t, persistent)],
    )

    assert independent is not None and correlated is not None
    assert correlated["residual_autocorrelation"] > 0.4
    assert independent["residual_autocorrelation"] <= 0.0
    # Negative autocorrelation must not be credited as extra precision.
    assert independent["effective_independent_days"] == pytest.approx(
        independent["days_used"]
    )
    assert correlated["effective_independent_days"] < correlated["days_used"]

    correlated_width = (
        correlated["hlc_ci_high_w_per_k"] - correlated["hlc_ci_low_w_per_k"]
    )
    independent_width = (
        independent["hlc_ci_high_w_per_k"] - independent["hlc_ci_low_w_per_k"]
    )
    assert correlated_width > independent_width


def test_hlc_ignores_autocorrelation_across_non_adjacent_days(
    thermal_math, forty_days
):
    """A gap of days carries no lag-1 information: residuals either side of it
    are not a lagged pair and must not be treated as one."""
    slope = 240.0 * 24 / 1000
    days = forty_days[::2]  # every other calendar day - no adjacent pairs at all
    delta_t = [5.0 + offset % 10 for offset in range(len(days))]
    energy = [
        2.0 + slope * dt + (1.5 if (i // 7) % 2 else -1.5)
        for i, dt in enumerate(delta_t)
    ]

    result = _fit(thermal_math, days, delta_t, energy)

    assert result is not None
    assert result["residual_autocorrelation"] == 0.0
    assert result["effective_independent_days"] == pytest.approx(result["days_used"])


def test_hlc_rejects_negative_dhw_adjusted_energy(thermal_math, forty_days):
    delta_t = [5.0 + offset % 10 for offset in range(len(forty_days))]
    energy = [3.0 + 5.0 * dt for dt in delta_t]
    dhw = [q + 1.0 for q in energy]

    assert _fit(thermal_math, forty_days, delta_t, energy, dhw) is None


def test_clear_sky_solar_proxy_and_two_var_hlc_removal_of_solar_confounding(thermal_math):
    from datetime import date, timedelta

    assert (
        thermal_math.clear_sky_solar_proxy(date(2026, 6, 21), 51.5)
        > thermal_math.clear_sky_solar_proxy(date(2026, 3, 21), 51.5)
        > thermal_math.clear_sky_solar_proxy(date(2026, 12, 21), 51.5)
    )

    # Full Nov-May heating season with true slope 250 W/K and solar gain -2.5 kWh/day/(kWh/m2)
    start = date(2025, 11, 1)
    days = [start + timedelta(days=i) for i in range(180)]
    true_hlc = 250.0
    true_slope = true_hlc * 24 / 1000
    true_solar_coeff = -2.5
    dt_by_day = {}
    q_by_day = {}
    for i, d in enumerate(days):
        solar = thermal_math.clear_sky_solar_proxy(d, 51.5)
        # Seasonal correlation: colder in winter (lower solar), milder in spring (higher solar)
        dt = max(4.5, 14.0 - 0.9 * solar + (i % 7) - 3.0)
        dt_by_day[d] = dt
        q_by_day[d] = 5.0 + true_slope * dt + true_solar_coeff * solar

    # Without latitude: pure-function 1-var OLS is upward-biased by solar confounding
    unadj = thermal_math.fit_hlc(q_by_day, dt_by_day, start)
    assert unadj is not None
    assert unadj["solar_adjusted"] is False
    assert unadj["solar_gain_coefficient_kwh_per_day"] is None
    assert unadj["hlc_w_per_k"] == pytest.approx(unadj["unadjusted_hlc_w_per_k"])
    assert unadj["hlc_w_per_k"] > true_hlc + 15.0
    assert unadj["winter_hlc_w_per_k"] is not None

    # With latitude: 2-var OLS recovers exact 250 W/K and negative solar coefficient
    adj = thermal_math.fit_hlc(q_by_day, dt_by_day, start, latitude_deg=51.5)
    assert adj is not None
    assert adj["solar_adjusted"] is True
    assert adj["hlc_w_per_k"] == pytest.approx(true_hlc, rel=1e-6)
    assert adj["solar_gain_coefficient_kwh_per_day"] == pytest.approx(true_solar_coeff, rel=1e-6)
    assert adj["unadjusted_hlc_w_per_k"] == pytest.approx(unadj["hlc_w_per_k"], rel=1e-6)
    assert adj["r_squared"] > unadj["r_squared"]
    assert adj["winter_hlc_w_per_k"] is not None


def test_hlc_falls_back_to_one_var_when_solar_slope_non_negative(thermal_math):
    from datetime import date, timedelta

    start = date(2025, 11, 1)
    days = [start + timedelta(days=i) for i in range(120)]
    slope = 220.0 * 24 / 1000
    dt_by_day = {d: 5.0 + (i % 10) for i, d in enumerate(days)}
    # Positive solar coefficient (+1.0) violates physical solar gain -> fall back to 1-var
    q_by_day = {
        d: 4.0 + slope * dt_by_day[d] + 1.0 * thermal_math.clear_sky_solar_proxy(d, 51.5)
        for d in days
    }
    res = thermal_math.fit_hlc(q_by_day, dt_by_day, start, latitude_deg=51.5)
    assert res is not None
    assert res["solar_adjusted"] is False
    assert res["solar_gain_coefficient_kwh_per_day"] is None
    assert res["hlc_w_per_k"] == pytest.approx(res["unadjusted_hlc_w_per_k"])


# pytest is imported last to keep the test data and physics readable above.
import pytest
