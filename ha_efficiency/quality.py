"""Broad input suitability bounds shared by offline analyses.

Invalid samples remain NaN at their original timestamps; callers must not
interpolate them into model fits. These are analytics limits, not safety limits.
"""
from __future__ import annotations

import math
import pandas as pd

TEMP_MIN_C = -90.0
TEMP_MAX_C = 80.0


def _in_range(value, lower, upper):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and lower <= number <= upper


def is_valid_temperature(value):
    return _in_range(value, TEMP_MIN_C, TEMP_MAX_C)


def is_valid_percentage(value):
    return _in_range(value, 0.0, 100.0)


def clean_numeric(series, validator=None):
    """Coerce to finite numbers without deleting invalid timestamps."""
    validator = validator or (lambda value: _in_range(value, -math.inf, math.inf))
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.where(numeric.map(validator))
