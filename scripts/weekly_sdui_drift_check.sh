#!/usr/bin/env bash
# Weekly SDUI selector-drift sweep (host cron, runs as `lem`) — issue #1013.
#
# Three Selenium surfaces were found dead-or-dangerous within days in Aug 2026 (#964 catch-up cards,
# #1009 profile viewers, #1012 connect invites) and every one of them had been broken for WEEKS,
# silently: the lane kept running, matched nothing, and logged "nothing to do". This turns that into
# a Monday issue.
#
# Flow: run the READ-ONLY probe sweep inside the selenium worker (one Chrome session, off-peak) ->
# each surface grades itself ok / drift / unknown against a page-native cross-check -> file ONE
# deduped `agent:ready` issue per `drift`, with the probe JSON attached.
#
# Safe by construction: the sweep navigates and reads. It sends no invite, posts nothing, comments
# on nothing, ticks no checkbox and clicks no Send/Post/Invite control — see the per-probe
# docstrings in scripts/linkedin_live_validation.py. The only writes this job makes are GitHub
# issues, and `unknown` (the page did not render) is never one of them.
#
# Install (VPS, as `lem`) — Mondays 06:40 UTC, before the 11:00 release window and outside the
# golden-hour engagement beats that need the Chrome slots:
#   40 6 * * 1 /home/lem/<repo-clone>/scripts/weekly_sdui_drift_check.sh
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/lem/.local/bin"

# Self-locate the repo root so this can run from a dedicated cron clone, isolated from any
# interactive dev checkout (overridable via REPO=... for tests).
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DIR="${SDUI_DRIFT_DIR:-/home/lem/sdui-drift-check}"
LOG="$DIR/sdui_drift.log"
CONTAINER="${SDUI_PROBE_CONTAINER:-celery_worker_selenium}"
USER_ID="${SDUI_PROBE_USER_ID:-1}"
# A profile that carries a real degree badge grounds `_PROFILE_DEGREE_LOCATORS`; without one the
# sweep falls back to the user's OWN profile, which has no badge, and the report says the degree
# half went ungrounded rather than pretending it passed.
PROFILE_URL="${SDUI_PROBE_PROFILE_URL:-}"
# DRY_RUN=1 sweeps and prints the plan, but files nothing — for rehearsing on the box.
DRY_RUN="${DRY_RUN:-0}"
mkdir -p "$DIR"

# Python for the (import-free) issue filer. Prefer the stable dev-checkout venv; fall back to the
# system interpreter, which is enough — the filer imports only the stdlib.
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(python3); fi

log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

# The sweep must measure with the probe that is on `main`, never with whatever the checkout it was
# installed from happens to hold (issue #2085). `scripts/` is not in the image, so both scripts are
# read off disk from $REPO — and $REPO is an ordinary git checkout nobody pulls. On 2026-09-21 it
# sat one commit behind and re-ran the pre-#2069 company-invite probe, so the surface graded
# `unknown` for a fourth week and re-filed the blind-spot issue that fix had already closed.
# Resolving each script from a ref is READ-ONLY: the checkout is never reset, pulled or
# checked out, and only its remote-tracking ref moves. It fails OPEN to the working copy with a
# loud log line, because a sweep on a stale probe still measures more than no sweep at all.
PROBE_REF="${SDUI_PROBE_REF:-origin/main}"
# Made here, not inside `pin_script`: every call site captures the function's stdout, so anything
# the function assigns dies with its subshell and the trap would never reach this shell.
PINNED_DIR="$(mktemp -d 2>/dev/null)" && trap 'rm -rf "$PINNED_DIR"' EXIT

pin_script(){  # $1 = repo-relative path; echoes the path to run. Never fails the sweep.
  local rel="$1" dest="${PINNED_DIR:-}/$(basename "$1")"
  if [ -n "${PINNED_DIR:-}" ] && git -C "$REPO" show "$PROBE_REF:$rel" >"$dest" 2>>"$LOG" \
     && [ -s "$dest" ]; then
    echo "$dest"
  else
    log "WARNING: could not read $rel from $PROBE_REF — sweeping with the working copy in $REPO"
    echo "$REPO/$rel"
  fi
}

alert(){  # log + best-effort email to the admin; never fails the run
  log "ALERT: $1"
  [ "$DRY_RUN" = "1" ] && { log "DRY_RUN: skipping alert email"; return 0; }
  sudo -n docker exec -i web_app python - "$1" <<'PY' >>"$LOG" 2>&1 || true
import os, sys
msg = sys.argv[1]
try:
    from cqc_lem.utilities.email import _dispatch_email
    to = os.environ.get("ADMIN_EMAIL") or os.environ.get("LINKEDIN_EMAIL") or "christopher.queen@gmail.com"
    html = "<pre>" + msg.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    _dispatch_email(to, "LEM weekly SDUI drift sweep", html, text_content=msg, high_priority=True)
    print("alert emailed to", to)
except Exception as e:
    print("alert email failed (logged only):", e)
PY
}

log "=== weekly SDUI drift sweep start ==="
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SWEEP="$DIR/sweep-$STAMP.json"

# Move the remote-tracking ref before pinning, or `origin/main` is exactly as stale as HEAD. A
# fetch failure is not fatal: `pin_script` still resolves whatever ref is on disk, and the log
# below names the revision that actually measured this week, so a stale sweep is legible after
# the fact instead of silently reading as a healthy one.
git -C "$REPO" fetch --quiet origin main >>"$LOG" 2>&1 \
  || log "WARNING: git fetch origin main failed in $REPO — $PROBE_REF may be stale"
log "probe source: $PROBE_REF $(git -C "$REPO" rev-parse --short "$PROBE_REF" 2>/dev/null || echo unknown)" \
    "(checkout HEAD $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown))"
PROBE_SCRIPT="$(pin_script scripts/linkedin_live_validation.py)"
FILER_SCRIPT="$(pin_script scripts/sdui_drift_issues.py)"

# This file is the one thing pinning cannot cover — cron executes the checkout's copy, so a
# checkout that is never pulled keeps running the orchestration it was installed with. Say so out
# loud rather than letting it rot the way the probe did: it changes rarely, and an alert is how the
# checkout gets pulled at all.
# An unresolvable ref is NOT this warning — `pin_script` already said so, and claiming "out of
# date" on a ref nobody could read would send a human chasing the wrong thing.
if git -C "$REPO" rev-parse --verify --quiet "$PROBE_REF" >/dev/null 2>&1 \
   && ! git -C "$REPO" diff --quiet "$PROBE_REF" -- scripts/weekly_sdui_drift_check.sh; then
  alert "SDUI drift sweep is running an OUT-OF-DATE scripts/weekly_sdui_drift_check.sh — $REPO differs from $PROBE_REF. The probe and the filer are pinned to the ref; this orchestration cannot be, so pull $REPO."
fi

PROBE_ARGS=(--user-id "$USER_ID" --sweep --require-debug-node)
[ -n "$PROFILE_URL" ] && PROBE_ARGS+=(--sweep-profile-url "$PROFILE_URL")

# `scripts/` is not baked into the image, so the probe is piped in on stdin — the same way the
# weekly LinkedIn version check runs its probe. `$PROBE_SCRIPT` is the ref-pinned copy, not the
# checkout's working file (#2085).
sudo -n docker exec -i "$CONTAINER" python - "${PROBE_ARGS[@]}" \
      < "$PROBE_SCRIPT" >"$SWEEP" 2>>"$LOG"
PROBE_RC=$?
# 75 (EX_TEMPFAIL) is the probe REFUSING to start: the LinkedIn 429 breaker is open, the breaker
# could not be read, or the watchable Grid node was unavailable (#1301). None of those is a broken
# sweep and none of them warrants paging anyone — the refusal is in the fenced JSON, and next
# Monday's run measures the same surfaces. Anything else is a real failure.
if [ "$PROBE_RC" = "75" ]; then
  log "sweep REFUSED to start (rc=75) — see the refusal in $SWEEP; no surface probed this week."
  exit 0
fi
if [ "$PROBE_RC" != "0" ]; then
  alert "SDUI drift sweep could not run (docker exec into $CONTAINER failed, rc=$PROBE_RC) — no surface was probed this week."
  exit 1
fi

if [ ! -s "$SWEEP" ]; then
  alert "SDUI drift sweep produced no output — no surface was probed this week."
  exit 1
fi
log "sweep written -> $SWEEP"

# The filer prints the one-line summary a human reads (ok / drift / unknown counts, then a line per
# issue). It is the ONLY thing that parses the sweep: the capture below is the worker's whole stdout
# — the app logger writes there too — so `json.loads` on it belongs in exactly one place, behind the
# report fences, unit-tested (sdui_drift_issues.fenced_report). A second ad-hoc parse here would
# fail on the first log line and report "no drift" for a week that had some.
# --history-dir points the filer at the season of past sweeps kept below (issue #1770 Phase 2): a
# surface that has graded neither ok nor drift for 3 consecutive sweeps files its OWN blind-spot
# issue, separate from a drift finding, because "we cannot see this" is silent in the summary line
# otherwise.
FILER_ARGS=(--sweep-file "$SWEEP" --history-dir "$DIR")
[ "$DRY_RUN" = "1" ] && FILER_ARGS+=(--dry-run) || FILER_ARGS+=(--apply)
"${PY[@]}" "$FILER_SCRIPT" "${FILER_ARGS[@]}" >>"$LOG" 2>&1
RC=$?
# 0 = nothing to do / filed; 2 = drift pending in dry-run. Anything else is a real failure, and a
# filer that cannot reach GitHub must page a human — otherwise this week's drift is simply lost.
case "$RC" in
  0|2) log "issue filing completed (rc=$RC)" ;;
  *)   alert "SDUI drift sweep ran but issue filing FAILED (rc=$RC) — see $LOG and $SWEEP." ;;
esac

# Keep a season of sweeps: comparing this week's reading against the last is how a slow rotation
# (rows shrinking week over week) is spotted before it reaches zero.
find "$DIR" -name 'sweep-*.json' -mtime +90 -delete 2>/dev/null || true
log "=== weekly SDUI drift sweep done ==="
