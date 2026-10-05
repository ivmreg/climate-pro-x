#!/usr/bin/env python3
"""Coverage gate script enforcing F6 requirements:

1. Math module (custom_components/thermal_efficiency/thermal_math.py) branch coverage:
   covered_branches / num_branches >= 90%
2. Combined trust modules coverage:
   percent_covered >= 80%
3. Fail immediately on:
   - Missing or malformed report
   - Non-finite or out-of-range percentages or thresholds (including NaN / Inf)
   - Non-integral, boolean, negative, or overflowing count values
   - Inconsistent totals (e.g. covered > total, reported percent inconsistent with counters)
   - Malformed file keys
   - Zero-denominator situations (no denominator 0 pass)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

MAX_REASONABLE_COUNT = 100_000_000


def _validate_threshold(val: Any, name: str) -> float:
    """Validate that a threshold is a finite float between 0.0 and 100.0."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Threshold '{name}' must be a float or integer, got {type(val).__name__}.")
    try:
        fval = float(val)
    except OverflowError as exc:
        raise ValueError(f"Threshold '{name}' must be finite.") from exc
    if not math.isfinite(fval):
        raise ValueError(f"Threshold '{name}' must be finite, got {fval}.")
    if not 0.0 <= fval <= 100.0:
        raise ValueError(f"Threshold '{name}' must be between 0.0 and 100.0, got {fval}.")
    return fval


def _validate_count(val: Any, name: str) -> int:
    """Validate that a count is a real non-negative integer (not bool, float, or string)."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"Count '{name}' must be an exact integer, got {type(val).__name__} ({val!r}).")
    if val < 0:
        raise ValueError(f"Count '{name}' cannot be negative, got {val}.")
    if val > MAX_REASONABLE_COUNT:
        raise ValueError(f"Count '{name}' exceeds reasonable bound ({val} > {MAX_REASONABLE_COUNT}).")
    return val


def _validate_percent(val: Any, name: str) -> float:
    """Validate that a percentage is a finite float between 0.0 and 100.0."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Percentage '{name}' must be a numeric value, got {type(val).__name__}.")
    try:
        fval = float(val)
    except OverflowError as exc:
        raise ValueError(f"Percentage '{name}' must be finite.") from exc
    if not math.isfinite(fval):
        raise ValueError(f"Percentage '{name}' must be finite (not NaN or Inf), got {fval}.")
    if not 0.0 <= fval <= 100.0:
        raise ValueError(f"Percentage '{name}' must be between 0.0 and 100.0, got {fval}.")
    return fval


def evaluate_coverage(
    report_path: str | Path,
    math_module_path: str = "custom_components/thermal_efficiency/thermal_math.py",
    math_min_branch_pct: float = 90.0,
    combined_min_pct: float = 80.0,
) -> tuple[bool, str]:
    """Evaluate coverage report against F6 quality gates.

    Returns (passed: bool, message: str).
    """
    # 1. Validate threshold arguments
    try:
        math_min_branch_pct = _validate_threshold(math_min_branch_pct, "math_min_branch_pct")
        combined_min_pct = _validate_threshold(combined_min_pct, "combined_min_pct")
    except ValueError as exc:
        return False, f"Invalid threshold configuration: {exc}"

    # 2. Validate file existence and readability
    path = Path(report_path)
    if not path.is_file():
        return False, f"Coverage report file not found: {report_path}"

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return False, f"Coverage report file is empty: {report_path}"
        data = json.loads(content)
    except (OSError, ValueError, UnicodeError) as exc:
        return False, f"Malformed coverage JSON report: {exc}"

    if not isinstance(data, dict):
        return False, "Coverage report JSON root must be an object."

    files = data.get("files")
    totals = data.get("totals")

    if not isinstance(files, dict):
        return False, "Coverage report missing 'files' dictionary."
    if not isinstance(totals, dict):
        return False, "Coverage report missing 'totals' dictionary."

    # 3. Validate file keys (no empty strings, null bytes, or non-strings)
    for k in files:
        if not isinstance(k, str) or not k.strip() or "\x00" in k:
            return False, f"Malformed file key in coverage report: {k!r}"

    # 4. Locate math module
    norm_math = math_module_path.replace("\\", "/").lower()
    math_key = None
    for k in files:
        if k.replace("\\", "/").lower().endswith(norm_math):
            math_key = k
            break

    if not math_key:
        return False, f"Math module '{math_module_path}' not found in coverage report files."

    math_entry = files[math_key]
    if not isinstance(math_entry, dict) or "summary" not in math_entry:
        return False, f"Math module '{math_key}' missing 'summary' dictionary."

    math_summary = math_entry["summary"]
    if not isinstance(math_summary, dict):
        return False, f"Math module '{math_key}' summary is not a dictionary."

    # Validate math branch counters
    try:
        num_branches = _validate_count(math_summary.get("num_branches"), "math.num_branches")
        covered_branches = _validate_count(math_summary.get("covered_branches"), "math.covered_branches")
        if "num_statements" in math_summary:
            num_stmts = _validate_count(math_summary["num_statements"], "math.num_statements")
        if "covered_lines" in math_summary:
            covered_lines = _validate_count(math_summary["covered_lines"], "math.covered_lines")
            if "num_statements" in math_summary and covered_lines > num_stmts:
                return False, f"Math module covered_lines ({covered_lines}) > num_statements ({num_stmts})."
    except ValueError as exc:
        return False, f"Invalid counter in math module summary: {exc}"

    if num_branches <= 0:
        return False, f"Math module '{math_key}' has {num_branches} branches (no denominator-0 pass)."

    if covered_branches > num_branches:
        return (
            False,
            f"Math module '{math_key}' inconsistent branch counts: {covered_branches} > {num_branches}.",
        )

    math_branch_pct = (covered_branches / num_branches) * 100.0

    # 5. Validate totals and combined coverage
    raw_combined_pct = totals.get("percent_covered")
    if raw_combined_pct is None:
        return False, "Totals section missing 'percent_covered'."

    try:
        combined_pct = _validate_percent(raw_combined_pct, "totals.percent_covered")
        total_stmts = _validate_count(totals.get("num_statements"), "totals.num_statements")
        total_branches = _validate_count(totals.get("num_branches"), "totals.num_branches")
        total_covered_lines = _validate_count(totals.get("covered_lines"), "totals.covered_lines")
        total_cov_branches = _validate_count(totals.get("covered_branches"), "totals.covered_branches")
        if total_covered_lines > total_stmts:
            return False, f"Totals covered_lines ({total_covered_lines}) > num_statements ({total_stmts})."
        if total_cov_branches > total_branches:
            return False, f"Totals covered_branches ({total_cov_branches}) > num_branches ({total_branches})."
    except ValueError as exc:
        return False, f"Invalid value in coverage report 'totals': {exc}"

    total_denom = total_stmts + total_branches
    if total_denom <= 0:
        return False, "Combined coverage has no statements or branches (denominator 0)."

    # The gate uses actual counts, never a rounded/display percentage.
    calc_pct = ((total_covered_lines + total_cov_branches) / total_denom) * 100.0
    if abs(calc_pct - combined_pct) > 1.5:
        return (
            False,
            f"Reported combined coverage ({combined_pct:.2f}%) is inconsistent with "
            f"counters ({calc_pct:.2f}% calculated from {total_covered_lines}+{total_cov_branches}/{total_denom}).",
        )
    combined_pct = calc_pct

    # 6. Evaluate against thresholds
    details = (
        f"Coverage Gate Results:\n"
        f"  - Math Module: {math_key}\n"
        f"    Branches: {covered_branches} / {num_branches} ({math_branch_pct:.2f}%)\n"
        f"    Requirement: >= {math_min_branch_pct:.2f}%\n"
        f"  - Combined Trust Modules: {combined_pct:.2f}%\n"
        f"    Requirement: >= {combined_min_pct:.2f}%"
    )

    failures = []
    if math_branch_pct < math_min_branch_pct:
        failures.append(
            f"Math branch coverage {math_branch_pct:.2f}% is below required {math_min_branch_pct:.2f}%."
        )

    if combined_pct < combined_min_pct:
        failures.append(
            f"Combined trust coverage {combined_pct:.2f}% is below required {combined_min_pct:.2f}%."
        )

    if failures:
        return False, f"{details}\nFAILED:\n  * " + "\n  * ".join(failures)

    return True, f"{details}\nPASSED: All coverage quality gates met."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify coverage JSON against F6 branch and combined coverage gates."
    )
    parser.add_argument(
        "--report",
        "-r",
        default="coverage.json",
        help="Path to coverage.json report (default: coverage.json)",
    )
    parser.add_argument(
        "--math-module",
        default="custom_components/thermal_efficiency/thermal_math.py",
        help="Path or suffix of thermal math module",
    )
    parser.add_argument(
        "--math-min-branch",
        type=float,
        default=90.0,
        help="Minimum branch coverage percentage for math module (default: 90.0)",
    )
    parser.add_argument(
        "--combined-min",
        type=float,
        default=80.0,
        help="Minimum combined coverage percentage across trust modules (default: 80.0)",
    )

    args = parser.parse_args()

    passed, message = evaluate_coverage(
        report_path=args.report,
        math_module_path=args.math_module,
        math_min_branch_pct=args.math_min_branch,
        combined_min_pct=args.combined_min,
    )

    print(message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
