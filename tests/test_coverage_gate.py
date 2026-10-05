"""Tests for the coverage gate script (scripts/check_coverage.py).

Verifies F6 requirements:
- Math branch coverage must be >= 90.0%
- Combined coverage must be >= 80.0%
- Historical case (93.36% combined, 87.33% math branch) must fail
- Valid cases (>=90% math branch, >=80% combined) must pass
- Rejects non-finite/out-of-range percent and thresholds (NaN, Inf, <0, >100)
- Rejects non-integral, boolean, negative, or overflow counts (prevents truncation/bool evasion)
- Rejects malformed file keys
- Enforces consistent totals and counters
- Denominator 0 and missing files must fail
"""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_coverage import evaluate_coverage, main


def _make_report(
    math_branches: int,
    math_covered: int,
    combined_pct: float,
    math_path: str = "custom_components/thermal_efficiency/thermal_math.py",
    math_lines: int = 702,
    math_covered_lines: int = 673,
    total_stmts: int = 2500,
    total_branches: int = 1000,
    total_covered_lines: int | None = None,
    total_covered_branches: int | None = None,
) -> dict:
    """Construct a mock coverage.json structure matching coverage.py schema."""
    if total_covered_lines is None:
        total_covered_lines = int(round(total_stmts * (combined_pct / 100.0)))
    if total_covered_branches is None:
        total_covered_branches = int(round(total_branches * (combined_pct / 100.0)))

    return {
        "meta": {"version": "7.15.4"},
        "files": {
            math_path: {
                "summary": {
                    "covered_lines": math_covered_lines,
                    "num_statements": math_lines,
                    "percent_covered": (
                        (math_covered_lines + math_covered) / (math_lines + math_branches) * 100.0
                        if (math_lines + math_branches) > 0
                        else 0.0
                    ),
                    "num_branches": math_branches,
                    "covered_branches": math_covered,
                    "missing_branches": math_branches - math_covered,
                }
            }
        },
        "totals": {
            "num_statements": total_stmts,
            "num_branches": total_branches,
            "covered_lines": total_covered_lines,
            "covered_branches": total_covered_branches,
            "percent_covered": combined_pct,
        },
    }


# ---------------------------------------------------------------------------
# Core F6 gate requirements
# ---------------------------------------------------------------------------

def test_historical_coverage_fails(tmp_path: Path):
    """Old baseline: 93.36% combined, 87.33% math branches (255/292). Must fail!"""
    report_file = tmp_path / "coverage_old.json"
    data = _make_report(
        math_branches=292,
        math_covered=255,  # 255 / 292 = 87.3287%
        combined_pct=86.74,
    )
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "87.33%" in msg or "below required 90.00%" in msg
    assert "FAILED" in msg


def test_passing_coverage_gate(tmp_path: Path):
    """Target: >=90.0% math branches and >=80.0% combined. Must pass!"""
    report_file = tmp_path / "coverage_pass.json"
    data = _make_report(
        math_branches=292,
        math_covered=265,  # 265 / 292 = 90.75%
        combined_pct=88.5,
    )
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert passed
    assert "PASSED: All coverage quality gates met." in msg


def test_exact_90_percent_branch_passes(tmp_path: Path):
    """Exact boundary: 90.0% math branch, 80.0% combined."""
    report_file = tmp_path / "coverage_boundary.json"
    data = _make_report(
        math_branches=100,
        math_covered=90,  # 90.0%
        combined_pct=80.0,
    )
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert passed
    assert "PASSED" in msg


# ---------------------------------------------------------------------------
# Strict validation: Non-finite, NaN, and out-of-range values
# ---------------------------------------------------------------------------

def test_nan_percent_rejected(tmp_path: Path):
    """Report with NaN percent_covered must fail."""
    report_file = tmp_path / "nan_cov.json"
    # Construct raw string with NaN
    raw_json = '{"files":{"custom_components/thermal_efficiency/thermal_math.py":{"summary":{"num_branches":100,"covered_branches":95,"num_statements":100,"covered_lines":95}}},"totals":{"num_statements":100,"num_branches":100,"percent_covered":NaN}}'
    report_file.write_text(raw_json, encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "finite" in msg.lower() or "malformed" in msg.lower()


def test_infinity_percent_rejected(tmp_path: Path):
    """Report with Infinity percent_covered must fail."""
    report_file = tmp_path / "inf_cov.json"
    raw_json = '{"files":{"custom_components/thermal_efficiency/thermal_math.py":{"summary":{"num_branches":100,"covered_branches":95,"num_statements":100,"covered_lines":95}}},"totals":{"num_statements":100,"num_branches":100,"percent_covered":Infinity}}'
    report_file.write_text(raw_json, encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "finite" in msg.lower() or "malformed" in msg.lower()


def test_out_of_range_percent_rejected(tmp_path: Path):
    """Report with percent > 100 or < 0 must fail."""
    report_file = tmp_path / "out_of_range.json"
    data = _make_report(math_branches=100, math_covered=95, combined_pct=105.0)
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "between 0.0 and 100.0" in msg.lower()


def test_invalid_thresholds_rejected(tmp_path: Path):
    """Non-finite or out-of-range thresholds passed to evaluate_coverage must fail."""
    report_file = tmp_path / "valid.json"
    data = _make_report(math_branches=100, math_covered=95, combined_pct=85.0)
    report_file.write_text(json.dumps(data), encoding="utf-8")

    # NaN threshold
    passed, msg = evaluate_coverage(report_file, math_min_branch_pct=float("nan"))
    assert not passed
    assert "must be finite" in msg

    # Out of range threshold
    passed, msg = evaluate_coverage(report_file, combined_min_pct=150.0)
    assert not passed
    assert "between 0.0 and 100.0" in msg

    # Boolean threshold
    passed, msg = evaluate_coverage(report_file, math_min_branch_pct=True)
    assert not passed
    assert "must be a float or integer" in msg


# ---------------------------------------------------------------------------
# Strict validation: Non-integral, boolean, negative, and overflow counts
# ---------------------------------------------------------------------------

def test_boolean_count_rejected(tmp_path: Path):
    """A boolean (e.g. num_branches: True) must be rejected, not treated as int 1."""
    report_file = tmp_path / "bool_count.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {"num_branches": True, "covered_branches": True}
            }
        },
        "totals": {"num_statements": 100, "num_branches": 50, "percent_covered": 85.0},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "must be an exact integer" in msg.lower() or "invalid" in msg.lower()


def test_float_count_rejected(tmp_path: Path):
    """Non-integral float count (e.g. 292.5) must be rejected, not truncated."""
    report_file = tmp_path / "float_count.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {"num_branches": 292.5, "covered_branches": 265}
            }
        },
        "totals": {"num_statements": 100, "num_branches": 50, "percent_covered": 85.0},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "must be an exact integer" in msg.lower()


def test_negative_count_rejected(tmp_path: Path):
    """Negative branch or statement count must be rejected."""
    report_file = tmp_path / "negative_count.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {"num_branches": -10, "covered_branches": 5}
            }
        },
        "totals": {"num_statements": 100, "num_branches": 50, "percent_covered": 85.0},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "cannot be negative" in msg.lower() or "invalid" in msg.lower()


def test_overflow_count_rejected(tmp_path: Path):
    """Unreasonably large count must be rejected."""
    report_file = tmp_path / "overflow_count.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {"num_branches": 10**10, "covered_branches": 10**10}
            }
        },
        "totals": {"num_statements": 100, "num_branches": 50, "percent_covered": 85.0},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "exceeds reasonable bound" in msg.lower()


# ---------------------------------------------------------------------------
# Strict validation: Malformed file keys and inconsistent totals
# ---------------------------------------------------------------------------

def test_malformed_file_key_rejected(tmp_path: Path):
    """Empty string or non-string file keys must be rejected."""
    report_file = tmp_path / "bad_file_key.json"
    data = {
        "files": {
            "": {"summary": {"num_branches": 10, "covered_branches": 9}},
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {"num_branches": 100, "covered_branches": 95, "num_statements": 100, "covered_lines": 95}
            },
        },
        "totals": {"num_statements": 100, "num_branches": 100, "percent_covered": 95.0},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "malformed file key" in msg.lower()


def test_inconsistent_counters_rejected(tmp_path: Path):
    """Covered lines or branches exceeding total statements/branches must fail."""
    report_file = tmp_path / "inconsistent.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {
                    "num_statements": 100,
                    "covered_lines": 110,  # 110 > 100!
                    "num_branches": 50,
                    "covered_branches": 45,
                }
            }
        },
        "totals": {"num_statements": 100, "num_branches": 50, "percent_covered": 85.0},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "covered_lines" in msg.lower() and "num_statements" in msg.lower()


def test_reported_percent_inconsistent_with_counters(tmp_path: Path):
    """Reported combined percent (95%) falsely conflicting with underlying counters (50%) must fail."""
    report_file = tmp_path / "inconsistent_percent.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/thermal_math.py": {
                "summary": {"num_statements": 100, "covered_lines": 50, "num_branches": 100, "covered_branches": 95}
            }
        },
        "totals": {
            "num_statements": 100,
            "num_branches": 100,
            "covered_lines": 50,
            "covered_branches": 50,  # 100/200 = 50.0%
            "percent_covered": 95.0,  # Fabricated 95.0%
        },
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "inconsistent with counters" in msg.lower()


# ---------------------------------------------------------------------------
# Missing files, missing modules, denominator zero, and CLI tests
# ---------------------------------------------------------------------------

def test_missing_report_file():
    """Non-existent coverage report file must fail."""
    passed, msg = evaluate_coverage("non_existent_coverage_file.json")
    assert not passed
    assert "not found" in msg.lower()


def test_empty_report_file(tmp_path: Path):
    """Empty report file must fail."""
    report_file = tmp_path / "empty.json"
    report_file.write_text("", encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "empty" in msg.lower()


def test_malformed_json_report(tmp_path: Path):
    """Corrupt JSON must fail."""
    report_file = tmp_path / "corrupt.json"
    report_file.write_text("{ incomplete json ...", encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "malformed" in msg.lower()


def test_missing_math_module_in_report(tmp_path: Path):
    """Report without thermal_math.py must fail."""
    report_file = tmp_path / "missing_math.json"
    data = {
        "files": {
            "custom_components/thermal_efficiency/coordinator.py": {
                "summary": {"num_branches": 20, "covered_branches": 19}
            }
        },
        "totals": {"percent_covered": 85.0, "num_statements": 100, "num_branches": 20},
    }
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "not found in coverage report" in msg.lower()


def test_denominator_zero_branches_fails(tmp_path: Path):
    """Denominator 0 branches must fail (no free pass)."""
    report_file = tmp_path / "zero_branches.json"
    data = _make_report(
        math_branches=0,
        math_covered=0,
        combined_pct=85.0,
    )
    report_file.write_text(json.dumps(data), encoding="utf-8")

    passed, msg = evaluate_coverage(report_file)
    assert not passed
    assert "no denominator-0 pass" in msg.lower()


def test_cli_execution_fails_on_old_baseline(tmp_path: Path, monkeypatch):
    """CLI main() returns 1 on failing coverage report."""
    report_file = tmp_path / "cli_report.json"
    data = _make_report(math_branches=292, math_covered=255, combined_pct=86.74)
    report_file.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv", ["check_coverage.py", "--report", str(report_file)]
    )
    assert main() == 1


def test_cli_execution_passes_on_target_coverage(tmp_path: Path, monkeypatch):
    """CLI main() returns 0 on passing coverage report."""
    report_file = tmp_path / "cli_pass.json"
    data = _make_report(math_branches=292, math_covered=268, combined_pct=88.0)
    report_file.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv", ["check_coverage.py", "--report", str(report_file)]
    )
    assert main() == 0


def test_rounded_display_percentage_cannot_pass_threshold(tmp_path: Path):
    data = _make_report(100, 95, 80.0)
    data["totals"].update(covered_lines=1999, covered_branches=800)
    report = tmp_path / "rounded.json"
    report.write_text(json.dumps(data), encoding="utf-8")
    passed, message = evaluate_coverage(report)
    assert not passed
    assert "below required 80.00%" in message


@pytest.mark.parametrize("field", ["covered_lines", "covered_branches", "num_branches"])
def test_missing_totals_counters_fail(tmp_path: Path, field):
    data = _make_report(100, 95, 85.0)
    del data["totals"][field]
    report = tmp_path / "missing.json"
    report.write_text(json.dumps(data), encoding="utf-8")
    passed, _ = evaluate_coverage(report)
    assert not passed


def test_numeric_overflow_is_reported_as_failure(tmp_path: Path):
    data = _make_report(100, 95, 85.0)
    report = tmp_path / "overflow.json"
    report.write_text(json.dumps(data), encoding="utf-8")
    assert not evaluate_coverage(report, math_min_branch_pct=10**1000)[0]
    data["totals"]["percent_covered"] = 10**1000
    report.write_text(json.dumps(data), encoding="utf-8")
    assert not evaluate_coverage(report)[0]
