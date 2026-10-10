#!/usr/bin/env bash
# Nightly backup of the MySQL database + Chrome profile volume.
# Run from cron on the VPS, e.g.:
#   0 3 * * * cd /opt/lem && ./scripts/backup.sh >> logs/backup.log 2>&1
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${LEM_ENV_FILE:-${ROOT_DIR}/.env}"

# Read a single KEY=value from an env file WITHOUT sourcing it. Handles bare,
# single-quoted and double-quoted values, ignores inline comments, and tolerates
# values containing spaces or shell-special characters. This is the fix for the
# 2026-07-08 outage: sourcing the whole .env with `set -a` tried to execute
# unquoted spaced values as commands under `set -e`.
env_value() {  # $1 = key, $2 = default
  [[ -r "$ENV_FILE" ]] || { echo "${2:-}"; return 0; }
  local val
  val=$(sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -n1 | \
    sed -E "s/^['\"](.*)['\"][[:space:]]*(#.*)?$/\1/; t; s/[[:space:]]+$//; s/[[:space:]](#.*)$//")
  echo "${val:-${2:-}}"
}

load_config() {
  # Load only the variables this script actually needs.
  MYSQL_HOST="${MYSQL_HOST:-$(env_value MYSQL_HOST mysql_db)}"
  MYSQL_DATABASE="${MYSQL_DATABASE:-$(env_value MYSQL_DATABASE)}"
  MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-$(env_value MYSQL_ROOT_PASSWORD)}"
  BACKUP_DIR="${BACKUP_DIR:-$(env_value BACKUP_DIR "${ROOT_DIR}/backups")}"
  RETAIN_DAYS="${RETAIN_DAYS:-$(env_value RETAIN_DAYS 7)}"
  BACKUP_REMOTE="${BACKUP_REMOTE:-$(env_value BACKUP_REMOTE)}"
}

backup() {
  load_config

  [[ -n "${MYSQL_DATABASE:-}" ]] || { echo "[backup] ERROR: MYSQL_DATABASE not set" >&2; exit 1; }
  [[ -n "${MYSQL_ROOT_PASSWORD:-}" ]] || { echo "[backup] ERROR: MYSQL_ROOT_PASSWORD not set" >&2; exit 1; }

  # DB_FILE is deliberately NOT local: the EXIT trap below runs after the function frame is gone
  # when `set -e` aborts the dump pipeline, and a local would already be out of scope by then.
  local STAMP CHROME_VOL CHROME_FILE chrome_size uncompressed_size
  STAMP="$(date -u +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"

  log() { echo "[backup ${STAMP}] $*"; }
  error() { echo "[backup ${STAMP}] ERROR: $*" >&2; }

  log "dumping MySQL ${MYSQL_DATABASE}"
  DB_FILE="${BACKUP_DIR}/db-${STAMP}.sql.gz"
  # A dump that never ran, or died mid-stream, still leaves a syntactically valid .gz behind —
  # gzip of an empty stream is 20 bytes. That file is fresh, so the watchdog's age check reads it
  # as a healthy backup and a human restoring from it gets an empty database. Nothing incomplete
  # is allowed to survive: the trap clears on the line after the last validation.
  trap 'if [[ -n "${DB_FILE:-}" ]]; then rm -f "$DB_FILE"; fi' EXIT
  docker exec "${MYSQL_HOST}" \
    mysqldump --single-transaction --quick --routines --triggers \
    -u root -p"${MYSQL_ROOT_PASSWORD}" "${MYSQL_DATABASE}" \
    | gzip > "$DB_FILE"

  # Freshness guard: a failed/empty/corrupt dump must exit non-zero and be visible.
  if [[ ! -s "$DB_FILE" ]]; then
    error "MySQL dump is missing or empty: ${DB_FILE}"
    exit 1
  fi
  if ! gzip -t "$DB_FILE" >/dev/null 2>&1; then
    error "MySQL dump is not a valid gzip archive: ${DB_FILE}"
    exit 1
  fi
  uncompressed_size=$(gzip -l "$DB_FILE" | tail -n1 | awk '{print $2}')
  if [[ "$uncompressed_size" =~ ^[0-9]+$ ]] && (( uncompressed_size == 0 )); then
    error "MySQL dump produced empty uncompressed output: ${DB_FILE}"
    exit 1
  fi
  trap - EXIT
  log "MySQL dump OK: $(stat -c %s "$DB_FILE" 2>/dev/null || echo "?") bytes"

  log "archiving chrome-profile volume"
  # The volume is Compose-project-prefixed (e.g. lem_chrome-profile), so detect it
  # rather than hardcoding a name — a wrong name makes docker create an empty
  # volume and silently back up nothing.
  # Use awk instead of grep so an empty list (no chrome-profile volume) does not
  # trip `set -o pipefail` and abort the whole script.
  CHROME_VOL="$(docker volume ls --format '{{.Name}}' | awk '/_chrome-profile$/ {print}' | head -1)"
  CHROME_FILE="${BACKUP_DIR}/chrome-profile-${STAMP}.tar.gz"
  if [[ -n "$CHROME_VOL" ]]; then
    docker run --rm \
      -v "${CHROME_VOL}:/data:ro" \
      -v "${BACKUP_DIR}:/backup" \
      alpine tar czf "/backup/chrome-profile-${STAMP}.tar.gz" -C /data . 2>/dev/null
    if [[ -f "$CHROME_FILE" ]]; then
      chrome_size=$(stat -c %s "$CHROME_FILE" 2>/dev/null || echo 0)
      if (( chrome_size < 200 )); then
        log "WARN: chrome-profile archive is only ${chrome_size} bytes — volume may be empty; cookies now live encrypted in the database"
      else
        log "chrome-profile archive OK: ${chrome_size} bytes"
      fi
    else
      error "chrome-profile archive was not created"
      exit 1
    fi
  else
    log "no *_chrome-profile volume found — skipping"
  fi

  log "pruning backups older than ${RETAIN_DAYS} days"
  find "$BACKUP_DIR" -name '*.gz' -mtime "+${RETAIN_DAYS}" -delete

  offsite_copy "$DB_FILE" || exit 1
  log "done"
}

# Off-host copy of tonight's DB dump (docs/offsite-backups.md). Until it succeeds the dump lives
# only on this disk, so every outcome is logged: an unset remote says so on every run, and a set
# remote that rclone cannot use fails the run. Only the new file is sent, with --immutable, so the
# box never overwrites or deletes anything off-host; remote retention is the bucket's own rule.
offsite_copy() {  # $1 = the dump this run wrote
  local name dest remote_name remote_type
  if [[ -z "${BACKUP_REMOTE:-}" ]]; then
    log "off-host copy: not configured (BACKUP_REMOTE unset); this dump exists only on this disk"
    return 0
  fi
  if ! command -v rclone >/dev/null 2>&1; then
    error "off-host copy: BACKUP_REMOTE is set but rclone is not installed"
    return 1
  fi
  # The dump is plain SQL holding member personal data, so it only ever leaves through a configured
  # rclone `crypt` remote: the provider stores ciphertext. Fail closed on anything else — a plain
  # bucket remote, an inline connection string (":s3,…", which would also put keys in this log),
  # or a value rclone would parse as a flag. Only the remote's NAME is ever logged.
  remote_name="${BACKUP_REMOTE%%:*}"
  if [[ "$BACKUP_REMOTE" != *:* || -z "$remote_name" || "$remote_name" == -* ]]; then
    error "off-host copy: BACKUP_REMOTE must name a configured rclone crypt remote" \
      "(name:path); refusing to upload"
    return 1
  fi
  remote_type="$(rclone listremotes --long 2>/dev/null | awk -v r="${remote_name}:" '$1 == r {print $2; exit}')"
  if [[ "$remote_type" != "crypt" ]]; then
    error "off-host copy: BACKUP_REMOTE ${remote_name} is not a crypt remote" \
      "(type: ${remote_type:-not found}); refusing to upload plaintext member data"
    return 1
  fi
  name="$(basename "$1")"
  # "remote:" names the remote's root; "remote:path" gets a separator. Never "remote:/name".
  dest="${BACKUP_REMOTE%/}"
  if [[ "$dest" == *: ]]; then dest="${dest}${name}"; else dest="${dest}/${name}"; fi
  log "off-host copy: ${name} -> ${remote_name}:"
  # rclone's own timeouts bound a stalled connection; this bounds a slow provider, so the nightly
  # run always ends and the watchdog's marker age is the signal.
  if ! timeout "${BACKUP_REMOTE_TIMEOUT:-3600}" rclone copyto --immutable "$1" "$dest"; then
    error "off-host copy failed: ${name} -> ${remote_name}:"
    return 1
  fi
  # The watchdog reads this marker's age (stack_watchdog.sh check_backup_freshness).
  printf '%s\n' "$name" > "$(dirname "$1")/.offsite-last-ok" \
    || { error "off-host copy landed but the watchdog marker could not be written"; return 1; }
  log "off-host copy OK: ${name}"
}

# Only execute the backup when the script is run directly; sourcing it loads the
# functions for unit testing.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  backup
fi
