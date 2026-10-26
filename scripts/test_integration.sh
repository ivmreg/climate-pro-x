#!/usr/bin/env bash
# Run in Linux/WSL with uv. No Home Assistant connection or credentials needed.
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:-3.14}"
case "$version" in
  3.14) ha_version=2026.9.1 ;;
  3.13) ha_version=2026.2.3 ;;
  *) echo 'Supported test Python versions: 3.13 or 3.14' >&2; exit 2 ;;
esac
env_dir=".venv-test-$version"
uv_bin="${UV_BIN:-uv}"
if [[ ! -x "$env_dir/bin/python" ]]; then
  "$uv_bin" venv --python "$version" "$env_dir"
fi
"$uv_bin" pip install --python "$env_dir/bin/python" -r requirements-dev.txt "homeassistant==$ha_version"
"$env_dir/bin/python" -m pytest --cov-report=json:coverage.json --junitxml=pytest.xml
"$env_dir/bin/python" scripts/check_coverage.py --report coverage.json
node --test tests/test_glance_card.mjs
