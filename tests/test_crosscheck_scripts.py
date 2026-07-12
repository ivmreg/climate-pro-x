"""Keep the broad deterministic physics scripts inside normal pytest/coverage."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_offline_synthetic_crosscheck(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runpy.run_path(str(ROOT / "tests" / "synthetic_check.py"), run_name="__test__")


def test_live_math_synthetic_crosscheck(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKIP_REAL_CACHE_CHECK", "1")
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(
            str(ROOT / "tests" / "integration_math_check.py"), run_name="__test__"
        )
    assert stopped.value.code == 0
