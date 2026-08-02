#!/usr/bin/env bash
# One-shot installer for the LEM stack watchdog (docs/stack-watchdog.md).
#
# Run as root ON THE VPS, after a release containing scripts/stack_watchdog.sh has deployed:
#   sudo /opt/lem/scripts/install_watchdog.sh you@example.com
#
# Idempotent: safe to re-run after every release. Re-running refreshes the units and restarts the
# timer, which is what you want if the unit files changed.
set -euo pipefail

LEM_DIR="${LEM_DIR:-/opt/lem}"
ENVF="${LEM_ENV_FILE:-${LEM_DIR}/.env}"
UNIT_DIR="/etc/systemd/system"
ALERT_EMAIL="${1:-}"

say()  { echo "[install-watchdog] $*"; }
die()  { echo "[install-watchdog] ERROR: $*" >&2; exit 1; }

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
WATCHDOG="${LEM_DIR}/scripts/stack_watchdog.sh"
[[ -f "$WATCHDOG" ]] || die "not found: ${WATCHDOG}
  /opt/lem is checked out at a release tag. Confirm the release containing the watchdog is deployed:
    grep '^IMAGE_TAG=' ${ENVF} && git -C ${LEM_DIR} describe --tags"
chmod +x "$WATCHDOG"

for unit in lem-watchdog.service lem-watchdog.timer; do
  [[ -f "${LEM_DIR}/scripts/systemd/${unit}" ]] || die "not found: ${LEM_DIR}/scripts/systemd/${unit}"
done

# 3. Alert recipient. Falls back to COST_ALERT_EMAIL inside the watchdog itself, so an empty value
#    is a warning rather than a failure — but say so loudly, because a watchdog nobody hears from
#    is indistinguishable from a healthy stack.
if [[ -n "$ALERT_EMAIL" ]]; then
  if grep -qE '^WATCHDOG_ALERT_EMAIL=' "$ENVF" 2>/dev/null; then
    sed -i -E "s|^WATCHDOG_ALERT_EMAIL=.*|WATCHDOG_ALERT_EMAIL=${ALERT_EMAIL}|" "$ENVF"
  else
    printf '\n# Stack watchdog alert recipient (docs/stack-watchdog.md)\nWATCHDOG_ALERT_EMAIL=%s\n' \
      "$ALERT_EMAIL" >> "$ENVF"
  fi
  say "WATCHDOG_ALERT_EMAIL=${ALERT_EMAIL} written to ${ENVF}"
else
  current="$(sed -nE 's/^[[:space:]]*WATCHDOG_ALERT_EMAIL=([^#[:space:]]*).*/\1/p' "$ENVF" 2>/dev/null | tail -n1)"
  fallback="$(sed -nE 's/^[[:space:]]*COST_ALERT_EMAIL=([^#[:space:]]*).*/\1/p' "$ENVF" 2>/dev/null | tail -n1)"
  if [[ -n "$current" ]]; then
    say "WATCHDOG_ALERT_EMAIL already set to ${current} — leaving it"
  elif [[ -n "$fallback" ]]; then
    say "WARN: no WATCHDOG_ALERT_EMAIL; will fall back to COST_ALERT_EMAIL (${fallback})"
  else
    say "WARN: no WATCHDOG_ALERT_EMAIL and no COST_ALERT_EMAIL — EMAIL ALERTS WILL NOT SEND."
    say "      Re-run as: sudo $0 you@example.com"
  fi
fi

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
#    failure — so don't let `set -e` treat it as one.
say "running once now..."
set +e
systemctl start lem-watchdog.service
rc=$?
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
