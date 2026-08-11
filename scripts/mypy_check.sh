#!/usr/bin/env bash
# The ONE way to run the Optional-typing guard (issue #1221).
#
# ADVISORY BY CONSTRUCTION: this script always exits 0, whatever mypy reports. That is not a
# convenience — the guard is prevention against a bug the #1154 audit measured ZERO instances of,
# and it is only worth having while it stays cheap. Making it a gate would buy a blocked pipeline
# for a class of defect nobody has hit. Scope + rationale: `[tool.mypy]` in pyproject.toml and
# docs/typing-guard.md.
#
# Usage:
#   scripts/mypy_check.sh                 # the configured scope
#   scripts/mypy_check.sh path/to/file.py # one module, e.g. to see whether it is ready to join
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

set +e
poetry run mypy "$@" > /tmp/mypy-check.txt 2>&1
rc=$?
set -e

cat /tmp/mypy-check.txt
if [ "$rc" -eq 0 ]; then
  exit 0
fi

# The lint group is optional, so "no mypy" is the common first run — say so instead of reporting it
# as a clean sheet, which is what a plain finding count would do here.
if grep -q 'Command not found: mypy' /tmp/mypy-check.txt; then
  echo
  echo "mypy is not installed in this environment — run: poetry install --with lint"
  exit 0
fi

findings=$(grep -cE '^[^ ].*:[0-9]+: error: ' /tmp/mypy-check.txt || true)
echo
echo "mypy reported ${findings} finding(s) — ADVISORY, this never fails a build."
echo "Fix them in the module, or drop the module from [tool.mypy] files in pyproject.toml."
exit 0
