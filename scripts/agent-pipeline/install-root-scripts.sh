#!/usr/bin/env bash
# Install the pipeline scripts that root-run systemd units execute. Run as root on the VPS.
#
#   sudo ./install-root-scripts.sh          show what would change, ask, then install
#   sudo ./install-root-scripts.sh --yes    install without asking
#        ./install-root-scripts.sh --list   print the root-run file set (repo-relative); no root needed
#
# Most of the pipeline runs as `lem` and is deployed into /home/lem/agent-pipeline by install.sh and
# sync.sh. A few scripts are executed by units that run as root (lem-gh-token.service mints the App
# token from a root-only key; lem-agentd-watchdog.service restarts a system unit). Those scripts are
# installed root-owned in $LEM_ROOT_LIB instead, and install.sh does not ship them into the deploy
# tree. An update to one of them therefore reaches the box only through this deliberate root step,
# never through the unattended sync. sync.sh logs a line when the repo copy and the installed copy
# differ, so a pending update is visible.
#
# The diff of every changed file is printed before anything is written. Read it: this is the review
# point for code that will run as root.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
ROOT_LIB="${LEM_ROOT_LIB:-/usr/local/lib/lem}"

# Repo-relative paths (under scripts/agent-pipeline/). install.sh reads this list to exclude them.
ROOT_RUN=(
  lib/gh_app_token.sh
  v2/watchdog.sh
)

YES=0
for a in "$@"; do
  case "$a" in
    --list) printf '%s\n' "${ROOT_RUN[@]}"; exit 0 ;;
    --yes)  YES=1 ;;
    -h|--help) sed -n '2,/^set -/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != "0" ]; then
  echo "refusing: must run as root (it installs root-owned files into $ROOT_LIB)." >&2
  exit 1
fi

changed=()
for rel in "${ROOT_RUN[@]}"; do
  dest="$ROOT_LIB/$(basename "$rel")"
  [ -f "$SRC/$rel" ] || { echo "missing in repo: $SRC/$rel" >&2; exit 1; }
  if ! cmp -s "$SRC/$rel" "$dest"; then
    changed+=("$rel")
    diff -uN --label "$dest" --label "$rel" "$dest" "$SRC/$rel" 2>/dev/null || true
  fi
done

if [ "${#changed[@]}" -eq 0 ]; then
  echo "root-run scripts already current in $ROOT_LIB."
  exit 0
fi

if [ "$YES" != 1 ]; then
  read -r -p "Install ${#changed[@]} file(s) into $ROOT_LIB? [y/N] " ans
  case "$ans" in y|Y|yes) ;; *) echo "nothing installed."; exit 1 ;; esac
fi

install -d -m 755 -o root -g root "$ROOT_LIB"
for rel in "${changed[@]}"; do
  install -m 755 -o root -g root "$SRC/$rel" "$ROOT_LIB/$(basename "$rel")"
  echo "installed $ROOT_LIB/$(basename "$rel")"
done
