#!/usr/bin/env bash
# One-shot installer for the LEM stack watchdog (docs/stack-watchdog.md).
#
# Run as root ON THE VPS, after a release containing scripts/stack_watchdog.sh has deployed:
#   sudo /opt/lem/scripts/install_watchdog.sh <alert-recipient-address>
#
# Idempotent: safe to re-run after every release. Re-running refreshes the units and restarts the
# timer, which is what you want if the unit files changed.
#
# Only runs the install when executed directly; sourcing loads the functions so the
# placeholder-rejection logic can be exercised in isolation (tests/unit/scripts/test_stack_watchdog.py).
set -euo pipefail

LEM_DIR="${LEM_DIR:-/opt/lem}"
ENVF="${LEM_ENV_FILE:-${LEM_DIR}/.env}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

say()  { echo "[install-watchdog] $*"; }
die()  { echo "[install-watchdog] ERROR: $*" >&2; exit 1; }

# A copy-pasted placeholder is the single most likely misconfiguration for an alert recipient.
# Keep this in sync with `is_placeholder_email` in stack_watchdog.sh — the two scripts don't share a
# process, so the check is duplicated rather than sourced.
is_placeholder_email() {  # $1 = candidate address
  local v="${1,,}"
  [[ -z "$v" ]] && return 0
  case "$v" in
    *@example.com|*@example.org|*@example.net|*changeme*) return 0 ;;
  esac
  return 1
}

# 3. Alert recipient. Falls back to COST_ALERT_EMAIL inside the watchdog itself, so an empty value
#    is a warning rather than a failure — but say so loudly, because a watchdog nobody hears from
#    is indistinguishable from a healthy stack. A placeholder is refused outright: writing it, or
#    reporting it back as "already set", would reassure the operator that the broken state is correct.
resolve_and_write_alert_email() {  # $1 = CLI arg (may be empty), $2 = env file path
  local alert_email="$1" envf="$2" current fallback
  if [[ -n "$alert_email" ]]; then
    is_placeholder_email "$alert_email" && \
      die "refusing to write placeholder address '${alert_email}' to WATCHDOG_ALERT_EMAIL — pass the real recipient."
    if grep -qE '^WATCHDOG_ALERT_EMAIL=' "$envf" 2>/dev/null; then
      sed -i -E "s|^WATCHDOG_ALERT_EMAIL=.*|WATCHDOG_ALERT_EMAIL=${alert_email}|" "$envf"
    else
      printf '\n# Stack watchdog alert recipient (docs/stack-watchdog.md)\nWATCHDOG_ALERT_EMAIL=%s\n' \
        "$alert_email" >> "$envf"
    fi
    say "WATCHDOG_ALERT_EMAIL=${alert_email} written to ${envf}"
    return 0
  fi

  current="$(sed -nE 's/^[[:space:]]*WATCHDOG_ALERT_EMAIL=([^#[:space:]]*).*/\1/p' "$envf" 2>/dev/null | tail -n1)"
  fallback="$(sed -nE 's/^[[:space:]]*COST_ALERT_EMAIL=([^#[:space:]]*).*/\1/p' "$envf" 2>/dev/null | tail -n1)"
  if [[ -n "$current" ]] && is_placeholder_email "$current"; then
    say "WARN: WATCHDOG_ALERT_EMAIL in ${envf} is a placeholder ('${current}') — treating it as unset."
    current=""
  fi
  if [[ -n "$current" ]]; then
    say "WATCHDOG_ALERT_EMAIL already set to ${current} — leaving it"
  elif [[ -n "$fallback" ]]; then
    say "WARN: no WATCHDOG_ALERT_EMAIL; will fall back to COST_ALERT_EMAIL (${fallback})"
  else
    say "WARN: no WATCHDOG_ALERT_EMAIL and no COST_ALERT_EMAIL — EMAIL ALERTS WILL NOT SEND."
    say "      Re-run as: sudo $0 <alert-recipient-address>"
  fi
}

main() {
  local alert_email="${1:-}"

  [[ $EUID -eq 0 ]] || die "must run as root (systemd units + /opt/lem/.env). Try: sudo $0 $*"

  # 1. Prerequisites. The watchdog builds its email payload with jq and posts with curl; without
  #    them it would install cleanly and then silently fail to alert, which is the one outcome worse
  #    than not installing it.
  for bin in jq curl docker systemctl; do
    command -v "$bin" >/dev/null 2>&1 || die "missing required binary: ${bin}"
  done
  say "prerequisites OK (jq, curl, docker, systemctl)"

  # 2. The payload must actually be on the box. /opt/lem tracks release TAGS, so this file only
  #    appears once a release containing it has been deployed — a missing file here almost always
  #    means "the PR merged but that release hasn't shipped yet", not a broken install.
  local watchdog="${LEM_DIR}/scripts/stack_watchdog.sh"
  [[ -f "$watchdog" ]] || die "not found: ${watchdog}
  /opt/lem is checked out at a release tag. Confirm the release containing the watchdog is deployed:
    grep '^IMAGE_TAG=' ${ENVF} && git -C ${LEM_DIR} describe --tags"
  chmod +x "$watchdog"

  for unit in lem-watchdog.service lem-watchdog.timer; do
    [[ -f "${LEM_DIR}/scripts/systemd/${unit}" ]] || die "not found: ${LEM_DIR}/scripts/systemd/${unit}"
  done

  resolve_and_write_alert_email "$alert_email" "$ENVF"

  # Warn on the other two credentials rather than failing: PostHog-only alerting is still useful.
  for key in SENDGRID_API_KEY SENDGRID_FROM_EMAIL POSTHOG_API_KEY; do
    grep -qE "^${key}=." "$ENVF" 2>/dev/null || say "WARN: ${key} unset in ${ENVF} — that alert channel is disabled"
  done

  # 4. Install and arm.
  install -m 0644 "${LEM_DIR}/scripts/systemd/lem-watchdog.service" "${UNIT_DIR}/lem-watchdog.service"
  install -m 0644 "${LEM_DIR}/scripts/systemd/lem-watchdog.timer"   "${UNIT_DIR}/lem-watchdog.timer"
  systemctl daemon-reload
  systemctl enable --now lem-watchdog.timer
  systemctl restart lem-watchdog.timer   # pick up unit changes on a re-run
  say "timer installed and enabled"

  # 5. Smoke test. Exit 1 from the watchdog means "something is down" — a true report, not an install
  #    failure — so don't let that abort the script.
  say "running once now..."
  set +e
  systemctl start lem-watchdog.service
  local rc=$?
  set -e

  echo
  say "─── result ───"
  systemctl list-timers lem-watchdog.timer --no-pager | sed 's/^/  /'
  echo
  journalctl -u lem-watchdog.service -n 20 --no-pager | sed 's/^/  /'
  echo
  if [[ $rc -eq 0 ]]; then
    say "OK — installed and armed. Silence from here on means the stack is healthy."
  else
    say "installed and armed. The run above reported a problem — read the log lines."
  fi
  say "Next: point an external monitor at https://<host>/health/deep and assert on the JSON"
  say "      'status' field, NOT just HTTP 200 — 'degraded' returns 200 by design."
}

# Only run the install when executed directly; sourcing loads the functions above so
# resolve_and_write_alert_email can be exercised in isolation without root/systemd.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
