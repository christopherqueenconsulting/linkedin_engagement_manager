#!/usr/bin/env bash
# Daily LEM performance snapshot -> appends one JSON line to metrics.jsonl. Started 2026-07-24 to
# track the impact of the recent engagement features (feed-dedup #474, comment follow-ups #478,
# connection requests #398, outreach funnel #399) over ~2 weeks. Cron: daily.
#
# Two layers per line:
#   d1/cumulative — ENGAGEMENT KPIs, read straight from MySQL.
#   margin        — COST + UNIT ECONOMICS (issue #491, docs/cost-performance-margin-plan.md §D.2):
#                   spend_usd, cost_by_feature, est. mrr, contribution_margin, gross_margin_pct.
#                   Produced by `python -m cqc_lem.utilities.margin --daily-json` INSIDE the app
#                   container so it uses the deployed margin math and the container's DB creds.
#                   The app container is the ACTIVE `web_api_<color>` — never `web_app`, which is
#                   the nginx front door and has no python, so the block was null (#2109).
#                   `ledger_available: false` means cost_ledger isn't capturing yet, so the $0 spend
#                   is "not measured", not "nothing spent".
#
# This lives in the repo (not only on the box) so the snapshot is reviewable and versioned; point
# the host cron at this file through scripts/run_at_deployed_tag.sh (docs/host-crons.md).
# Overridable: PERF_DIR, LEM_ENV_FILE, LEM_ROOT, MARGIN_CONTAINER.
set -uo pipefail
DIR="${PERF_DIR:-/home/lem/perf-tracking}"
LOG="$DIR/metrics.jsonl"
ENVF="${LEM_ENV_FILE:-/opt/lem/.env}"
LEM_ROOT="${LEM_ROOT:-/opt/lem}"
mkdir -p "$DIR"

. "$(dirname "${BASH_SOURCE[0]}")/lib/app_container.sh"
MARGIN_CONTAINER="${MARGIN_CONTAINER:-$(active_api_container)}"
# One .env value the way compose reads it (#2431): the LAST assignment wins, and surrounding quotes
# and a trailing ` # comment` are not part of the value. A bare grep|cut kept every duplicate line and
# any quotes, so a credential that compose reads fine reached the mysql client mangled.
envv(){ sudo -n grep -E "^$1=" "$ENVF" 2>/dev/null | tail -n1 | cut -d= -f2- | \
  sed -E "s/^['\"](.*)['\"][[:space:]]*(#.*)?$/\1/; t; s/[[:space:]]+(#.*)?$//"; }
DBU=$(envv MYSQL_USER)
DBP=$(envv MYSQL_PASSWORD)
DBN=$(envv MYSQL_DATABASE)
# The DB container is named ${MYSQL_HOST} (docker-compose.yml), not a fixed `mysql_db`. The value
# becomes a `docker exec` target, so anything but a plain container name falls back to the default.
DBH=$(envv MYSQL_HOST)
[[ "$DBH" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || DBH=mysql_db
# The password reaches the mysql client as MYSQL_PWD from a 0600 env-file, never as a `-p` CLI arg:
# an argv password is readable in the container's process list (and by anything scraping it).
PWF=$(mktemp)                       # mktemp creates 0600
QERR=$(mktemp)                      # the queries' stderr: an unreadable count must name its cause
trap 'rm -f "$PWF" "$QERR"' EXIT
printf 'MYSQL_PWD=%s\n' "$DBP" > "$PWF"
q(){ sudo -n docker exec --env-file "$PWF" "$DBH" mysql -u"$DBU" -N -B -e "$1" 2>>"$QERR" \
  | grep -vi "using a password" | head -1; }
# A count that could not be read is null, never 0 ("unscored is never zero"). metrics.jsonl has
# all-zero d1 and cumulative rows from 2026-10-08 through 2026-10-15 (posts_tracked was 61 on
# 10-07): the old `:-0` defaults turned empty reads into rows that looked like real measurements.
# num() passes a bare integer through and turns anything else into null.
num(){ [[ "${1:-}" =~ ^[0-9]+$ ]] && printf '%s' "$1" || printf 'null'; }
note(){ echo "[$(date -u +%FT%TZ)] $1" >> "$DIR/snapshot.log"; }

U=1
c1d=; r1d=; d1d=; e1d=; p1d=; ps=; eng=; fu=; cp=
if [ -z "$DBU" ] || [ -z "$DBP" ] || [ -z "$DBN" ]; then
  note "engagement counts unavailable: DB credentials not readable from $ENVF; counts written as null"
else
DAY="SELECT COUNT(*) FROM $DBN.logs WHERE user_id=$U AND result='success' AND created_at>=NOW()-INTERVAL 1 DAY AND action_type="
c1d=$(q "${DAY}'comment';"); r1d=$(q "${DAY}'reply';"); d1d=$(q "${DAY}'dm';"); e1d=$(q "${DAY}'engaged';"); p1d=$(q "${DAY}'post';")
# LATEST stat row per post only. post_stats is append-only — the nightly scrape writes a fresh
# CUMULATIVE row per post every run, so summing every row multiplied each post's reactions /
# impressions by how many times it had been scraped (issue #627). MAX(id) GROUP BY post_id is the
# same latest-row-per-post pattern db.py uses for every analytics read.
ps=$(q "SELECT CONCAT_WS(',',COUNT(DISTINCT post_id),COALESCE(SUM(reactions),0),COALESCE(SUM(comments),0),COALESCE(SUM(impressions),0)) FROM $DBN.post_stats WHERE user_id=$U AND id IN (SELECT MAX(id) FROM $DBN.post_stats WHERE user_id=$U GROUP BY post_id);")
eng=$(q "SELECT COUNT(*) FROM $DBN.post_engagers WHERE user_id=$U;")
fu=$(q "SELECT COUNT(*) FROM $DBN.comment_followups WHERE user_id=$U;")
cp=$(q "SELECT COUNT(*) FROM $DBN.commented_posts WHERE user_id=$U;")
fi
IFS=',' read -r pt reac comm impr <<< "${ps:-,,,}"
c1d=$(num "$c1d"); r1d=$(num "$r1d"); d1d=$(num "$d1d"); e1d=$(num "$e1d"); p1d=$(num "$p1d")
pt=$(num "$pt"); reac=$(num "$reac"); comm=$(num "$comm"); impr=$(num "$impr")
eng=$(num "$eng"); fu=$(num "$fu"); cp=$(num "$cp")
if [ -n "$DBU" ] && [ -n "$DBP" ] && [ -n "$DBN" ]; then
  nulls=""
  for f in d1.comments:$c1d d1.replies:$r1d d1.dms:$d1d d1.reactions:$e1d d1.posts:$p1d \
           cumulative.posts_tracked:$pt cumulative.own_reactions:$reac cumulative.own_comments:$comm \
           cumulative.own_impressions:$impr cumulative.engagers:$eng cumulative.commented_posts:$cp \
           cumulative.followups:$fu; do
    [ "${f#*:}" = null ] && nulls="${nulls:+$nulls,}${f%%:*}"
  done
  if [ -n "$nulls" ]; then
    # The first error line the queries printed (docker or mysql), so the log names the cause rather
    # than only the null fields — 2026-10-20 onward logged nulls with no cause at all (#2431).
    why=$(grep -vi "using a password" "$QERR" | grep -m1 . | cut -c1-300)
    note "engagement counts unavailable: query returned no number for null fields: $nulls;\
 container $DBH; first error: ${why:-none printed}"
  fi
fi

# Cost/margin block. Never fatal: an unreachable container or a broken block leaves the engagement
# line intact (with "margin": null) rather than losing the whole day's snapshot.
# stderr is kept (last line only) so a null block says WHY — 48 nulls went unexplained under #2109.
ERRF=$(mktemp)
trap 'rm -f "$PWF" "$QERR" "$ERRF"' EXIT
margin=$(sudo -n docker exec "$MARGIN_CONTAINER" python -m cqc_lem.utilities.margin --daily-json 2>"$ERRF" | tail -1)
if ! printf '%s' "${margin:-}" | python3 -c "import json,sys; json.loads(sys.stdin.read())" 2>/dev/null; then
  echo "[$(date -u +%FT%TZ)] margin block unavailable from $MARGIN_CONTAINER: $(tail -1 "$ERRF")" >> "$DIR/snapshot.log"
  margin=null
fi

MARGIN_JSON="$margin" python3 - <<PY >> "$LOG"
import json, os, datetime
null = None
print(json.dumps({
  "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
  "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()+"Z",
  "d1": {"comments": ${c1d}, "replies": ${r1d}, "dms": ${d1d}, "reactions": ${e1d}, "posts": ${p1d}},
  "cumulative": {"posts_tracked": ${pt}, "own_reactions": ${reac}, "own_comments": ${comm},
                 "own_impressions": ${impr}, "engagers": ${eng}, "commented_posts": ${cp}, "followups": ${fu}},
  "margin": json.loads(os.environ["MARGIN_JSON"]),
}))
PY
echo "[$(date -u +%FT%TZ)] snapshot written" >> "$DIR/snapshot.log"
