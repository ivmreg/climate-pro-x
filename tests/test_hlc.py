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


# pytest is imported last to keep the test data and physics readable above.
import pytest
