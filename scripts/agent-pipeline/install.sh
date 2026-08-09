#!/usr/bin/env bash
# Installer + updater for the LEM agent pipeline. Run as user `lem` on the VPS.
#
#   ./install.sh          first install: copy runner to /home/lem/agent-pipeline, start PAUSED,
#                         add the 5-minute cron
#   ./install.sh --sync   update an EXISTING install: copy only files that are safe to update,
#                         refuse any file the box has edited since the last install
#   ./install.sh --sync --force   also overwrite box-edited files (after you read the diff)
#
# Two traps this replaces (both bit for real):
#   - The old installer ran `touch PAUSED` unconditionally, so re-running it to pick up a fix
#     silently PAUSED a live pipeline. Now PAUSED is only touched on FIRST install.
#   - It copied tick.sh + lib/ + status.sh but not docs/, so the box's operational docs drifted
#     from the repo's, and the one place that explained the live merge-queue budgets was a repo
#     file the box never received.
#
# How --sync knows what is safe: every install writes a manifest of the sha256 of each file it
# placed (.installed.sha256). On sync, a box file whose hash still matches the manifest has not
# been touched since we put it there — safe to replace. A mismatch means a box-local edit; those
# are listed and skipped unless --force. (The pre-manifest install can't tell — the first --sync
# treats every differing file as box-edited and asks for --force once; it writes the manifest, so
# every later sync is precise.)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${LEM_PIPELINE_DEST:-/home/lem/agent-pipeline}"   # overridable so the installer is testable
MANIFEST="$DEST/.installed.sha256"

SYNC=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --sync)  SYNC=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
  esac
done

# The file set the installer owns on the box. Relative to $SRC and $DEST alike. Every entry is
# existence-guarded so the list degrades gracefully across branches (docs/ may be empty in a
# stripped checkout, and status.sh only exists from #1238 on).
files() {
  # status.sh lives next to tick.sh so it is where someone looks when they SSH in.
  for f in tick.sh RUNBOOK.md status.sh; do [ -e "$SRC/$f" ] && echo "$f"; done
  # lib/ was NOT copied here until 2026-08-09, so tick.sh shipped while the helpers it sources did
  # not. The box's copies drifted out of the repo's sight: `run_lane.sh` there had gained an export
  # block (the #842 unattended-benchmark vars) that existed nowhere in git, and would have been
  # destroyed by the first person who thought to sync lib/ the obvious way — which is exactly why
  # --sync refuses a box-edited file instead of overwriting it.
  for f in "$SRC"/lib/*.sh;  do [ -e "$f" ] && echo "lib/$(basename "$f")"; done
  for f in "$SRC"/docs/*.md; do [ -e "$f" ] && echo "docs/$(basename "$f")"; done
}

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

manifest_sha() {  # <relpath> -> recorded sha or ""
  [ -f "$MANIFEST" ] || { echo ""; return; }
  awk -v f="$1" '$2==f{print $1}' "$MANIFEST"
}

# Atomic per-file replace: a tick may be mid-run, and bash re-reads nothing it already sourced,
# but a HALF-written file must never exist on disk for the tick that starts mid-copy.
place() {  # <relpath>
  local rel="$1" tmp
  mkdir -p "$DEST/$(dirname "$rel")"
  tmp="$DEST/$(dirname "$rel")/.$(basename "$rel").new"
  cp "$SRC/$rel" "$tmp" && mv "$tmp" "$DEST/$rel"
  case "$rel" in *.sh) chmod +x "$DEST/$rel" ;; esac
}

write_manifest() {
  : > "$MANIFEST.new"
  while IFS= read -r rel; do
    printf '%s %s\n' "$(sha "$DEST/$rel")" "$rel" >> "$MANIFEST.new"
  done < <(files)
  mv "$MANIFEST.new" "$MANIFEST"
}

FIRST_INSTALL=1
[ -f "$DEST/tick.sh" ] && FIRST_INSTALL=0

if [ "$SYNC" = 1 ] && [ "$FIRST_INSTALL" = 1 ]; then
  echo "--sync requested but $DEST holds no install — run without --sync first." >&2
  exit 2
fi

mkdir -p "$DEST/logs" "$DEST/work" "$DEST/lib" "$DEST/docs" "$DEST/state" "$DEST/locks"

if [ "$SYNC" = 1 ]; then
  updated=0; skipped=(); same=0
  while IFS= read -r rel; do
    if [ ! -f "$DEST/$rel" ]; then place "$rel"; updated=$((updated+1)); continue; fi
    if [ "$(sha "$SRC/$rel")" = "$(sha "$DEST/$rel")" ]; then same=$((same+1)); continue; fi
    # Repo and box differ. Box-edited (or pre-manifest unknown) files need --force.
    if [ "$FORCE" != 1 ] && [ "$(sha "$DEST/$rel")" != "$(manifest_sha "$rel")" ]; then
      skipped+=("$rel"); continue
    fi
    place "$rel"; updated=$((updated+1))
  done < <(files)
  write_manifest
  echo "sync: $updated updated, $same already current, ${#skipped[@]} refused."
  if [ "${#skipped[@]}" -gt 0 ]; then
    echo "REFUSED (box-local edits — diff before deciding, then --sync --force):"
    for rel in "${skipped[@]}"; do
      echo "  diff $DEST/$rel $SRC/$rel"
    done
    exit 1
  fi
  exit 0
fi

# ---- first install (or plain re-install over an existing tree) ----
while IFS= read -r rel; do place "$rel"; done < <(files)
write_manifest
if [ "$FIRST_INSTALL" = 1 ]; then
  touch "$DEST/PAUSED"   # start paused — nothing runs until you remove this file
  echo "Installed to $DEST (PAUSED)."
else
  # Re-running the installer on a live box must never pause it — that is what happened on
  # 2026-08-09's re-install, and nothing said so until the next tick logged "PAUSED file present".
  echo "Re-installed to $DEST (PAUSED untouched — use --sync for guarded updates)."
fi

# Add the tick cron for lem if not already present.
# Every 5 minutes, matching what the box actually runs — this said hourly while the installed
# crontab was `*/5`, so the documented cadence was 12x slower than the real one, and anyone
# reasoning about how long a pause takes to bite (or how much work a tick can start) got it wrong.
LINE="*/5 * * * * /home/lem/agent-pipeline/tick.sh >/dev/null 2>&1"
if crontab -l 2>/dev/null | grep -qF "/home/lem/agent-pipeline/tick.sh"; then
  echo "cron entry already present."
else
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "cron entry added (every 5 minutes)."
fi

cat <<'EOF'

Next steps:
  0. See what the pipeline is doing at any time (read-only, safe to leave running):
       /home/lem/agent-pipeline/status.sh            # one-shot report
       /home/lem/agent-pipeline/status.sh --watch    # refreshing dashboard
  1. Dry-run once to validate selection/state logic (no code changes, no Claude call):
       DRY_RUN=1 /home/lem/agent-pipeline/tick.sh ; tail -n 40 /home/lem/agent-pipeline/logs/tick-*.log
  2. Go LIVE:      rm /home/lem/agent-pipeline/PAUSED
  3. Pause again:  touch /home/lem/agent-pipeline/PAUSED
  4. Run one tick manually now (instead of waiting for cron):
       /home/lem/agent-pipeline/tick.sh
  5. Later, to pick up repo changes WITHOUT pausing the box (refuses box-edited files):
       <repo>/scripts/agent-pipeline/install.sh --sync        # add --force after reading the diff
EOF
