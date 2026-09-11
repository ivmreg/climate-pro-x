"""Validation shared by setup and runtime source checks."""

from __future__ import annotations

from math import isfinite

import voluptuous as vol

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


def heating_power_issue(
    hass: HomeAssistant, entity_id: str, *, allow_missing: bool = False
) -> str | None:
    """Return why a heating-demand source is unsafe, or None when valid."""
    state = hass.states.get(entity_id)
    if state is None:
        return None if allow_missing else "entity is not currently available"
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if not isinstance(unit, str) or unit.strip().casefold() not in {
        "%",
        "percent",
        "percentage",
    }:
        return "unit must be % (0-100 heating demand)"
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return "state is not numeric"
    if not isfinite(value) or not 0.0 <= value <= 100.0:
        return "state must be between 0 and 100%"
    return None


def _loft_since(value: object) -> str:
    """Validate an ISO date string, keeping it a plain string (not a `date`
    object) so it survives being stored as config-entry data, which is
    persisted to storage as JSON."""
    parsed = dt_util.parse_date(str(value))
    if parsed is None:
        raise vol.Invalid("loft_since must be an ISO date (YYYY-MM-DD)")
    return parsed.isoformat()

