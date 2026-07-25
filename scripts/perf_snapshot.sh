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
#                   `ledger_available: false` means cost_ledger isn't capturing yet, so the $0 spend
#                   is "not measured", not "nothing spent".
#
# This lives in the repo (not only on the box) so the snapshot is reviewable and versioned; point
# the host cron at this file. Overridable: PERF_DIR, LEM_ENV_FILE, MARGIN_CONTAINER.
set -uo pipefail
DIR="${PERF_DIR:-/home/lem/perf-tracking}"
LOG="$DIR/metrics.jsonl"
ENVF="${LEM_ENV_FILE:-/opt/lem/.env}"
MARGIN_CONTAINER="${MARGIN_CONTAINER:-web_app}"
mkdir -p "$DIR"
DBU=$(sudo -n grep -E "^MYSQL_USER=" "$ENVF"|cut -d= -f2)
DBP=$(sudo -n grep -E "^MYSQL_PASSWORD=" "$ENVF"|cut -d= -f2)
DBN=$(sudo -n grep -E "^MYSQL_DATABASE=" "$ENVF"|cut -d= -f2)
q(){ sudo -n docker exec mysql_db mysql -u"$DBU" -p"$DBP" -N -B -e "$1" 2>/dev/null | grep -vi "using a password" | head -1; }

U=1
DAY="SELECT COUNT(*) FROM $DBN.logs WHERE user_id=$U AND result='success' AND created_at>=NOW()-INTERVAL 1 DAY AND action_type="
c1d=$(q "${DAY}'comment';"); r1d=$(q "${DAY}'reply';"); d1d=$(q "${DAY}'dm';"); e1d=$(q "${DAY}'engaged';"); p1d=$(q "${DAY}'post';")
ps=$(q "SELECT CONCAT_WS(',',COUNT(DISTINCT post_id),COALESCE(SUM(reactions),0),COALESCE(SUM(comments),0),COALESCE(SUM(impressions),0)) FROM $DBN.post_stats WHERE user_id=$U;")
eng=$(q "SELECT COUNT(*) FROM $DBN.post_engagers WHERE user_id=$U;")
fu=$(q "SELECT COUNT(*) FROM $DBN.comment_followups WHERE user_id=$U;")
cp=$(q "SELECT COUNT(*) FROM $DBN.commented_posts WHERE user_id=$U;")
IFS=',' read -r pt reac comm impr <<< "${ps:-0,0,0,0}"

# Cost/margin block. Never fatal: an unreachable container or a broken block leaves the engagement
# line intact (with "margin": null) rather than losing the whole day's snapshot.
margin=$(sudo -n docker exec "$MARGIN_CONTAINER" python -m cqc_lem.utilities.margin --daily-json 2>/dev/null | tail -1)
if ! printf '%s' "${margin:-}" | python3 -c "import json,sys; json.loads(sys.stdin.read())" 2>/dev/null; then
  echo "[$(date -u +%FT%TZ)] margin block unavailable" >> "$DIR/snapshot.log"
  margin=null
fi

MARGIN_JSON="$margin" python3 - <<PY >> "$LOG"
import json, os, datetime
print(json.dumps({
  "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
  "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()+"Z",
  "d1": {"comments": ${c1d:-0}, "replies": ${r1d:-0}, "dms": ${d1d:-0}, "reactions": ${e1d:-0}, "posts": ${p1d:-0}},
  "cumulative": {"posts_tracked": ${pt:-0}, "own_reactions": ${reac:-0}, "own_comments": ${comm:-0},
                 "own_impressions": ${impr:-0}, "engagers": ${eng:-0}, "commented_posts": ${cp:-0}, "followups": ${fu:-0}},
  "margin": json.loads(os.environ["MARGIN_JSON"]),
}))
PY
echo "[$(date -u +%FT%TZ)] snapshot written" >> "$DIR/snapshot.log"
