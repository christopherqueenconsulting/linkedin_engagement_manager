#!/usr/bin/env bash
# The ONE way to count lint findings for `.ruff-baseline`.
#
# `ruff check ... | wc -l` and `grep -c '^'` are both WRONG: ruff prints two trailing summary lines
# ("Found N errors." and "[*] N fixable ..."), so those count exactly 2 too many. Ratcheting the
# baseline with an inflated number leaves that much silent slack in the gate — measured at 2 on
# 2026-08-09, which was enough to let a deliberate 2-violation probe through unnoticed.
#
# The regex below is the SAME one `.github/workflows/docstring-lint.yml` grades with.
# `tests/unit/test_ruff_count_matches_the_gate.py` fails the build if the two ever diverge.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
set +e
poetry run ruff check src/ tests/ --output-format=concise > /tmp/ruff-count.txt 2>&1
rc=$?
set -e
if [ "$rc" -gt 1 ]; then
  echo "ruff exited $rc — cannot measure this tree." >&2
  sed -n '1,20p' /tmp/ruff-count.txt >&2
  exit 1
fi
count="$(grep -cE '^[^ ].*:[0-9]+:[0-9]+: ' /tmp/ruff-count.txt || true)"
: "${count:=0}"
# Exit 1 is ALSO what `poetry run` returns when ruff is not installed ("Command not found: ruff"),
# so rc<=1 alone does not prove ruff ran: that shape printed 0 on every nightly for weeks (#2111).
# A dirty exit with zero findings, or a clean one with some, is the same refusal the gate makes.
if { [ "$rc" -eq 0 ] && [ "$count" -ne 0 ]; } || { [ "$rc" -eq 1 ] && [ "$count" -eq 0 ]; }; then
  echo "ruff exit $rc disagrees with $count parsed finding(s) — cannot measure this tree." >&2
  sed -n '1,20p' /tmp/ruff-count.txt >&2
  exit 1
fi
echo "$count"
