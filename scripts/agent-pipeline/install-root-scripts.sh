#!/usr/bin/env bash
# Install root-owned copies of the scripts that the root-run pipeline units execute.
#
#   ./install-root-scripts.sh                install or refresh /usr/local/lib/lem (root only)
#   ./install-root-scripts.sh --check        compare the repo's rendered scripts with the installed
#                                            copies: exit 0 = current, 3 = differs. Writes nothing.
#   ./install-root-scripts.sh --render NAME  print what NAME would be installed as. Writes nothing.
#
# Two units run as root: lem-gh-token.service (mints the GitHub App installation token) and
# lem-agentd-watchdog.service (restarts a system unit). Each executes a root-owned copy of
# its script from /usr/local/lib/lem/, and this installer is the only thing that writes there.
# install.sh and sync.sh run as the pipeline user and never touch these copies; sync.sh runs
# --check after each sync and logs a WARNING when the repo has moved on, asking for a re-run.
#
# Each copy is the repo file with its source paths rewritten to other root-owned files, so a copy
# never reads code from the pipeline tree. A rewrite that no longer matches the repo text exactly
# once is a hard failure, not a silent pass-through.
#
# Run it from a checkout only root can write, for example a fresh `git clone` made as root. Install
# mode refuses a source file, or any directory above one, that another user can modify. It is
# idempotent: a copy whose content, owner and mode already match is left alone, and every run
# prints the sha256 of each installed file.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${LEM_ROOT_LIB_DIR:-/usr/local/lib/lem}"
OWNER=0
GROUP=0

# What gets installed, in install order. A unit may only reference names in this list.
ROOT_SCRIPTS=(gh_app_token.sh posthog.sh watchdog.sh)

src_of() {
  case "$1" in
    gh_app_token.sh) echo "lib/gh_app_token.sh" ;;
    posthog.sh)      echo "lib/posthog.sh" ;;
    watchdog.sh)     echo "v2/watchdog.sh" ;;
    *) return 1 ;;
  esac
}

# Exact whole-line rewrites, as FROM/TO line pairs. The values are literal text: `$BASE` here is the
# characters in the script, not an expansion.
# shellcheck disable=SC2016
rewrites_of() {
  case "$1" in
    watchdog.sh)
      printf '%s\n' 'POSTHOG_LIB="$BASE/lib/posthog.sh"' \
                    "POSTHOG_LIB=\"$DEST/posthog.sh\""
      printf '%s\n' 'LOG="$BASE/logs/watchdog.log"' \
                    'LOG=/dev/null   # root copy: log() still prints, so every line reaches the journal'
      ;;
    posthog.sh)
      # The root copy reads its PostHog key from a root-owned file; without one, telemetry is a
      # no-op.
      printf '%s\n' '[ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env"' \
                    '[ -f /etc/lem/posthog.env ] && . /etc/lem/posthog.env'
      ;;
  esac
}

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1 || true; }   # "" when the file is missing

# stdin -> stdout with the line equal to $1 replaced by $2. Fails unless exactly one line matched.
_replace_line() {
  FROM="$1" TO="$2" awk '
    $0 == ENVIRON["FROM"] { print ENVIRON["TO"]; n++; next }
    { print }
    END { if (n != 1) { printf "rewrite matched %d lines, expected 1: %s\n", n, ENVIRON["FROM"] > "/dev/stderr"; exit 3 } }'
}

render() {  # <name> -> the file as it is installed, on stdout
  local name="$1" rel text from to
  rel="$(src_of "$name")" || { echo "unknown root script: $name" >&2; return 2; }
  [ -f "$SRC/$rel" ] || { echo "missing source: $SRC/$rel" >&2; return 2; }
  text="$(cat "$SRC/$rel")"
  while IFS= read -r from && IFS= read -r to; do
    text="$(printf '%s\n' "$text" | _replace_line "$from" "$to")" || return 3
  done < <(rewrites_of "$name")
  # Nothing a root copy runs may come from the pipeline tree.
  if printf '%s\n' "$text" | grep -nE '(^|[;&|({[:space:]])(\.|source)[[:space:]]+"?\$\{?BASE|\$\{?BASE\}?/(lib/|secrets\.env)' >&2; then
    echo "refusing: $name still reads code from \$BASE (lines above)" >&2
    return 3
  fi
  printf '%s\n' "$text" | awk -v rel="$rel" 'NR == 1 { print; print "# Root-owned copy of scripts/agent-pipeline/" rel ", written by install-root-scripts.sh. Edit the repo, then re-run it."; next } { print }'
}

# Every source file, and every directory above it, must be owned by root (or $OWNER) and not
# writable by group or other. A root-owned sticky directory such as /tmp is fine.
assert_trusted_source() {
  local name p u m bad=0
  for name in "${ROOT_SCRIPTS[@]}"; do
    p="$(realpath -e "$SRC/$(src_of "$name")")" || { echo "missing source for $name" >&2; return 2; }
    while :; do
      read -r u m < <(stat -c '%u %a' "$p")
      if { [ "$u" != 0 ] && [ "$u" != "$OWNER" ]; } \
         || { (( 8#$m & 8#022 )) && ! { [ -d "$p" ] && [ "$u" = 0 ] && (( 8#$m & 8#1000 )); }; }; then
        echo "untrusted source path: $p (uid $u, mode $m)" >&2
        bad=1
      fi
      [ "$p" = / ] && break
      p="$(dirname "$p")"
    done
  done
  if [ "$bad" = 1 ]; then
    echo "refusing: install from a checkout only root can write, e.g. a fresh 'git clone' made as root." >&2
    return 4
  fi
}

install_all() {
  local name tmp new old state
  assert_trusted_source
  [ -L "$DEST" ] && { echo "refusing: $DEST is a symlink" >&2; return 4; }
  install -d -o "$OWNER" -g "$GROUP" -m 0755 "$DEST"
  for name in "${ROOT_SCRIPTS[@]}"; do
    tmp="$(mktemp "$DEST/.$name.XXXXXX")"
    if ! render "$name" > "$tmp"; then rm -f "$tmp"; return 3; fi
    chown "$OWNER:$GROUP" "$tmp"
    chmod 0755 "$tmp"
    new="$(sha "$tmp")"
    old="$(sha "$DEST/$name")"
    if [ "$new" = "$old" ] && [ ! -L "$DEST/$name" ] \
       && [ "$(stat -c '%u:%g:%a' "$DEST/$name")" = "$OWNER:$GROUP:755" ]; then
      rm -f "$tmp"; state=unchanged
    else
      mv -f "$tmp" "$DEST/$name"; state=installed
    fi
    printf '%s  %s  (%s)\n' "$new" "$DEST/$name" "$state"
  done
}

check_all() {
  local name want have drift=0
  for name in "${ROOT_SCRIPTS[@]}"; do
    if ! want="$(render "$name" | sha256sum | cut -d' ' -f1)"; then
      echo "ERROR $name: the repo copy does not render"; drift=1; continue
    fi
    have="$(sha "$DEST/$name")"
    if [ "$want" = "$have" ]; then
      echo "OK      $DEST/$name ${want:0:12}"
    else
      [ -n "$have" ] || have=missing
      echo "DIFFERS $DEST/$name: repo renders ${want:0:12}, installed ${have:0:12}"
      drift=1
    fi
  done
  [ "$drift" = 0 ] || return 3
}

main() {
  case "${1:-}" in
    --check)  check_all ;;
    --render) render "${2:?usage: --render NAME}" ;;
    -h|--help) sed -n '2,/^set -/p' "$0" | sed '$d; s/^# \{0,1\}//' ;;
    "")
      if [ "$EUID" -ne 0 ]; then
        echo "refusing: install mode runs as root only (EUID is $EUID). --check and --render need no privilege." >&2
        exit 1
      fi
      install_all
      ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
}

# Sourceable, so tests can exercise install_all against a scratch DEST (with OWNER/GROUP set to
# their own ids) without running as root. Executed, it always installs as root:root.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
