"""Shared fixtures for calculation and ingestion regression tests."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def thermal_math():
    """Load the HA integration's pure maths without importing Home Assistant."""
    module_name = "thermal_math_under_test"
    module_path = ROOT / "custom_components" / "thermal_efficiency" / "thermal_math.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def forty_days() -> list[date]:
    start = date(2026, 1, 1)
    return [start + timedelta(days=offset) for offset in range(40)]
