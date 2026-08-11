#!/usr/bin/env bash
# The ONE way to run the Optional-typing guard (issue #1221).
#
# ADVISORY BY CONSTRUCTION: this script always exits 0, whatever mypy reports. That is not a
# convenience — the guard is prevention against a bug the #1154 audit measured ZERO instances of,
# and it is only worth having while it stays cheap. Making it a gate would buy a blocked pipeline
# for a class of defect nobody has hit. Scope + rationale: `[tool.mypy]` in pyproject.toml and
# docs/typing-guard.md.
#
# Always exiting 0 means the OUTPUT is the only signal, so it must never read as a clean sheet when
# mypy never graded anything — a missing install, a `files` entry pointing at a moved module, a
# typo in a setting. Those all exit non-zero with zero finding lines, and are reported as such.
#
# Usage:
#   scripts/mypy_check.sh                 # the configured scope
#   scripts/mypy_check.sh path/to/file.py # one module, e.g. to see whether it is ready to join
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

# Per-run temp file: this repo runs several agents across worktrees at once, and a shared fixed
# path would have them reading each other's output.
out=$(mktemp "${TMPDIR:-/tmp}/mypy-check.XXXXXX") || exit 2
trap 'rm -f "$out"' EXIT

set +e
poetry run mypy "$@" > "$out" 2>&1
rc=$?
set -e

cat "$out"
if [ "$rc" -eq 0 ]; then
  exit 0
fi

findings=$(grep -cE '^[^ ].*:[0-9]+: error: ' "$out" || true)
echo
if [ "$findings" -gt 0 ]; then
  echo "mypy reported ${findings} finding(s) — ADVISORY, this never fails a build."
  echo "Fix them in the module, or drop the module from [tool.mypy] files in pyproject.toml."
  exit 0
fi

# Non-zero with no graded findings means mypy did not get as far as grading. Printing a finding
# count here would announce a clean sheet for a run that checked nothing — the one failure mode
# this script must not have.
echo "mypy exited ${rc} without grading anything, so this is NOT a clean result — see the output above."
# The lint group is optional, so "no mypy" is the common first run. Match loosely: the wording is
# Poetry's and it has changed between versions.
if grep -qiE 'command not found|not found: mypy|no module named .?mypy' "$out"; then
  echo "mypy is not installed in this environment — run: poetry install --with lint"
fi
exit 0
