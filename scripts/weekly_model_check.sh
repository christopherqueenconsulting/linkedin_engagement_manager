#!/usr/bin/env bash
# Weekly LiteLLM model-health orchestrator (host cron, runs as `lem`).
#
# REACTIVE flow: plan (scripts/model_health_check.py against the LIVE box config) -> for retirements
# with a verified replacement, back up + apply the swap on the box, restart litellm, smoke-test every
# tier, and ROLL BACK on any failure; on success open a PR so the change lands in git and alert. When
# no swap is needed but a tier is 410-ing (a stale litellm that wasn't restarted after a deploy), just
# restart + re-verify. Manual-only retirements (REMOVE / unknown / no working replacement) alert.
#
# PROACTIVE flow (issue #716): after the probe, scan Ollama Cloud's published retirement schedule and
# live catalog. A configured model with a FUTURE sunset alerts now and gets its replacement
# pre-appended to model_upgrades.yaml (so the reactive half swaps it before the 410); new catalog
# tags and configured models trailing a newer version of their own family become agent:ready issues.
# Repo-visible changes (map, snapshot) go out as a PR — never a silent edit on the box.
#
# Safe by construction: a replacement is provider-verified before use, changes are smoke-tested
# before they're kept, and deploy.sh resets .litellm before its checkout so an on-box hotfix can
# never block a release.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/lem/.local/bin"

# Self-locate the repo root so this can run from a dedicated cron clone, isolated from any
# interactive dev checkout (overridable via REPO=... for tests).
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BOX_CFG="/opt/lem/.litellm/config.yaml"
MAP="$REPO/.litellm/model_upgrades.yaml"
SNAP="$REPO/.litellm/ollama_catalog_snapshot.json"
DIR="/home/lem/model-check"
LOG="$DIR/model_check.log"
COMPOSE="sudo -n docker compose -f /opt/lem/docker-compose.yml -f /opt/lem/docker-compose.prod.yml"
TIERS="lem-simple lem-medium lem-complex lem-router"
mkdir -p "$DIR"

# Python with the `openai` client (for provider probes / config rewrite). Prefer the stable
# dev-checkout venv; fall back to `poetry run` inside REPO if that venv is absent.
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(poetry run python); fi

log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

alert(){  # log + best-effort email to the admin; never fails the run
  log "ALERT: $1"
  sudo -n docker exec -i web_app python - "$1" <<'PY' >>"$LOG" 2>&1 || true
import os, sys
msg = sys.argv[1]
try:
    from cqc_lem.utilities.email import _dispatch_email
    to = os.environ.get("ADMIN_EMAIL") or os.environ.get("LINKEDIN_EMAIL") or "christopher.queen@gmail.com"
    html = "<pre>" + msg.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    _dispatch_email(to, "LEM weekly model-health check", html, text_content=msg, high_priority=True)
    print("alert emailed to", to)
except Exception as e:
    print("alert email failed (logged only):", e)
PY
}

restart_litellm(){ log "restarting litellm"; $COMPOSE restart litellm >>"$LOG" 2>&1; sleep 8; }

tier_ok(){  # $1=tier -> 0 if a completion succeeds within 3 tries
  local t="$1" i
  for i in 1 2 3; do
    if sudo -n docker exec web_app python -c "
from cqc_lem.utilities.ai.client import client
client.chat.completions.create(model='$t', messages=[{'role':'user','content':'ok'}], max_tokens=3)
" >/dev/null 2>&1; then return 0; fi
    sleep 4
  done
  return 1
}

smoke(){  # 0 iff every tier answers
  local bad=0 t
  for t in $TIERS; do
    if tier_ok "$t"; then log "smoke $t: OK"; else log "smoke $t: FAIL"; bad=1; fi
  done
  return $bad
}

open_pr(){  # $1=branch $2=title $3=body $4=mutate-fn (runs inside the worktree, stages its files)
            # Mirrors a change into the repo via an EPHEMERAL worktree — never touches the shared
            # dev checkout's branch/working tree (a human may be working there).
  local branch="$1" title="$2" body="$3" mutate="$4" wt
  wt="$(mktemp -d /tmp/model-upgrade-wt.XXXXXX)"
  ( cd "$REPO" && git fetch -q origin main \
      && git worktree add -q -B "$branch" "$wt" origin/main ) || { log "worktree add failed ($branch)"; return 1; }
  (
    cd "$wt" || exit 1
    "$mutate" "$wt" || exit 1
    git commit -q -m "$title" \
        -m "Opened by scripts/weekly_model_check.sh." >>"$LOG" 2>&1 || { log "no repo diff to PR ($branch)"; exit 0; }
    # The branch is bot-owned and recreated from origin/main every run, so a stale remote head is
    # replaced rather than left to fail the push (which used to drop the PR silently).
    git push -q -u origin "$branch" >>"$LOG" 2>&1 || git push -q -f -u origin "$branch" >>"$LOG" 2>&1
    gh pr create --base main --head "$branch" --title "$title" --body "$body" >>"$LOG" 2>&1 \
       && log "PR opened ($branch)" || log "PR create skipped ($branch; maybe one already open)"
  )
  ( cd "$REPO" && git worktree remove --force "$wt" 2>/dev/null ) || true
}

mutate_swap(){  # re-plan against the worktree's own config so the PR diff matches the box change
  "${PY[@]}" "$REPO/scripts/model_health_check.py" --apply "$1/.litellm/config.yaml" \
      --config "$1/.litellm/config.yaml" --map "$1/.litellm/model_upgrades.yaml" >>"$LOG" 2>&1
  git add .litellm/config.yaml
}

mutate_catalog(){  # pre-approve upcoming retirements + refresh the committed catalog snapshot
                   # --tags-fixture keeps the offline fixture on the SAME fetch as the snapshot;
                   # without it the shipped-state guard fails on every PR this function opens.
  "${PY[@]}" "$REPO/scripts/model_health_check.py" --catalog-scan --catalog-apply \
      --config "$1/.litellm/config.yaml" --map "$1/.litellm/model_upgrades.yaml" \
      --snapshot "$1/.litellm/ollama_catalog_snapshot.json" \
      --tags-fixture "$1/tests/unit/fixtures/ollama/tags.json" --no-usage-levels >>"$LOG" 2>&1
  git add .litellm/model_upgrades.yaml .litellm/ollama_catalog_snapshot.json \
          tests/unit/fixtures/ollama/tags.json
}

mutate_benchmark(){  # render the run we ALREADY measured into the worktree (issue #721)
                     # --render is pure: no provider or PostHog calls, so the PR always describes
                     # the same measurement the alert above was based on.
  "${PY[@]}" "$REPO/scripts/benchmark_models.py" --render "$BENCH_RESULTS" \
      --out-dir "$1/docs/model-benchmarks" >>"$LOG" 2>&1
  git add docs/model-benchmarks
}

log "=== weekly model-health check start ==="
# Provider creds for the planner's direct probes.
set -a; source <(sudo -n grep -E "^(OLLAMA_CLOUD_URL|OLLAMA_CLOUD_API_KEY)=" /opt/lem/.env); set +a

cd "$REPO"
# The upgrade map this run READS is the cron clone's copy, and last week's pre-approved mapping only
# reaches it once its PR merged — fast-forward so an approved swap actually fires. Best-effort: a
# dirty or detached clone just keeps working with what it has.
git pull -q --ff-only >>"$LOG" 2>&1 || log "repo fast-forward skipped (clone not on a clean main?)"

PLAN="$("${PY[@]}" scripts/model_health_check.py --plan-json --config "$BOX_CFG" --map "$MAP" 2>>"$LOG")"
[ -z "$PLAN" ] && { log "planner produced no output — aborting"; exit 1; }
NSWAP=$(printf '%s' "$PLAN" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['swaps']))" 2>/dev/null || echo 0)
NALERT=$(printf '%s' "$PLAN" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['alerts']))" 2>/dev/null || echo 0)
log "plan: swaps=$NSWAP alerts=$NALERT"

if [ "$NALERT" -gt 0 ]; then
  MSG=$(printf '%s' "$PLAN" | python3 -c "import sys,json;print(chr(10).join(f\"{a['group']}: {a.get('model','')} - {a['reason']}\" for a in json.load(sys.stdin)['alerts']))")
  alert "Model retirements needing manual action:"$'\n'"$MSG"
fi

if [ "$NSWAP" -gt 0 ]; then
  BK="$BOX_CFG.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  sudo -n cp -a "$BOX_CFG" "$BK"; log "backed up box config -> $BK"
  TMP=$(mktemp)
  "${PY[@]}" scripts/model_health_check.py --apply "$TMP" --config "$BOX_CFG" --map "$MAP" >>"$LOG" 2>&1
  sudo -n cp "$TMP" "$BOX_CFG"; rm -f "$TMP"
  restart_litellm
  if smoke; then
    log "swaps verified — keeping"
    open_pr "auto/model-upgrade" "fix(litellm): auto-swap retired Ollama model(s)" \
      "Automated by the weekly model-health check. The replacement was verified against Ollama Cloud and every tier smoke-tested on the box before this PR." \
      mutate_swap
    alert "Applied + verified $NSWAP model swap(s); PR opened to make it durable."
  else
    log "smoke FAILED after swaps — rolling back to $BK"
    sudo -n cp "$BK" "$BOX_CFG"; restart_litellm
    alert "Model swap smoke-test FAILED — rolled back. Manual review needed."
    exit 1
  fi
else
  if smoke; then
    log "all tiers healthy; nothing to do"
  else
    log "no swaps but a tier failed — restarting litellm (likely stale config after a deploy)"
    restart_litellm
    if smoke; then log "recovered after restart"; else alert "Tiers still failing after litellm restart — manual review."; fi
  fi
fi

# ── proactive scan: published retirement schedule + live catalog (issue #716) ────────────────
# Read-only against the box; the only writes are a PR and GitHub issues. Any fetch failure inside
# the scanner degrades to "skip that source" — the 410 probe above stays the backstop, so this
# block must never be allowed to fail the run.
CAT="$("${PY[@]}" scripts/model_health_check.py --catalog-json --config "$BOX_CFG" --map "$MAP" \
        --snapshot "$SNAP" 2>>"$LOG")"
if [ -z "$CAT" ]; then
  log "catalog scan produced no output — skipping (410 probe remains the backstop)"
else
  read -r NOTICE NEWTAG NUPG NREPOINT NVANISH REPOCH <<<"$(printf '%s' "$CAT" | python3 -c "
import sys, json
p = json.load(sys.stdin)
print(len(p['notices']), len(p['catalog']['added']), len(p['upgrades']),
      len(p.get('repoints') or []), len(p.get('vanished') or []), int(bool(p['repo_changes'])))
" 2>/dev/null || echo "0 0 0 0 0 0")"
  log "catalog scan: retirement-notices=$NOTICE new-tags=$NEWTAG family-upgrades=$NUPG" \
      "re-pointed-tags=$NREPOINT vanished-tags=$NVANISH repo-changes=$REPOCH"

  if [ "${NOTICE:-0}" -gt 0 ]; then
    MSG=$(printf '%s' "$CAT" | python3 -c "
import sys, json
for n in json.load(sys.stdin)['notices']:
    print(f\"{n['group']}: {n['bare']} retires {n['date']} (in {n['days_until']} days) -> \"
          f\"{n['alternative'] or 'NO published alternative — needs a manual call'}\")
")
    alert "Ollama Cloud retirements scheduled for models we still run:"$'\n'"$MSG"
  fi

  # A configured tag that left the catalog is next week's 410, found early — and it is also the
  # loudest re-point there is (#1237), so the scan files an issue for it below. That PR is opened
  # and merged by the agent pipeline, and merging it re-baselines the snapshot, so this is the ONE
  # run that can report it: page a human as well.
  GONE_MSG=$(printf '%s' "$CAT" | python3 -c "
import sys, json
for name in json.load(sys.stdin)['catalog'].get('removed_configured') or []:
    print(f\"{name}: no longer listed on ollama.com/api/tags, but the box config still serves it\")
" 2>/dev/null)
  if [ -n "$GONE_MSG" ]; then
    alert "Configured Ollama models have left the live catalog (a 410 is coming):"$'\n'"$GONE_MSG"
  fi

  if [ "${REPOCH:-0}" -gt 0 ]; then
    # Title + body come from the plan we already read, so they name THIS run's diff. The two halves
    # (map pre-approval, snapshot refresh) fire independently, and a fixed string claiming both was
    # wrong on most weeks — it sent reviewers looking for a map diff that wasn't there.
    CAT_TITLE="$(printf '%s' "$CAT" | python3 -c "
import sys, json; print((json.load(sys.stdin).get('pr') or {}).get('title') or '')" 2>/dev/null)"
    CAT_BODY="$(printf '%s' "$CAT" | python3 -c "
import sys, json; print((json.load(sys.stdin).get('pr') or {}).get('body') or '')" 2>/dev/null)"
    [ -n "$CAT_TITLE" ] || CAT_TITLE="chore(litellm): refresh Ollama catalog snapshot"
    [ -n "$CAT_BODY" ] || CAT_BODY="Automated by the weekly model-health check (issue #716). See the diff — the plan's own summary could not be rendered."
    open_pr "auto/ollama-catalog" "$CAT_TITLE" "$CAT_BODY" mutate_catalog
  fi

  if [ "${NEWTAG:-0}" -gt 0 ] || [ "${NUPG:-0}" -gt 0 ] || [ "${NREPOINT:-0}" -gt 0 ] \
     || [ "${NVANISH:-0}" -gt 0 ]; then
    # File from the plan we ALREADY have, not a fresh scan. A second scan re-reads docs.ollama.com
    # and /api/tags, and a transient outage there would file nothing and say nothing — while the
    # snapshot PR opened just above makes those tags no longer "new", so the issue would be lost for
    # good rather than retried next week.
    PLANF="$(mktemp)"; printf '%s' "$CAT" >"$PLANF"
    "${PY[@]}" scripts/model_health_check.py --file-issues --plan-file "$PLANF" >>"$LOG" 2>&1
    RC=$?
    rm -f "$PLANF"
    # 0/2/3 are the planner's own nothing/actions/alerts codes — only anything else is a failure.
    case "$RC" in 0|2|3) ;; *) log "issue filing failed (non-fatal, rc=$RC)";; esac
  fi

  # ── model-tier benchmark: is the candidate any GOOD? (issue #721) ──────────────────────────
  # Everything above this line detects CHANGE. This measures QUALITY: each cron-detected candidate
  # and the tier's current champion run the same synthetic suites, and the run is committed as a
  # report PR. Strictly advisory — it never edits the config, never writes model_upgrades.yaml, and
  # any failure here must NOT touch the retirement-swap safety path that already ran above.
  # POSTHOG_BENCHMARK_API_KEY is this lane's purpose-scoped personal key (issue #1453); the shared
  # POSTHOG_PERSONAL_API_KEY stays sourced as its fallback until it is revoked. Export BOTH —
  # benchmark_models.py decides which one wins (src/cqc_lem/utilities/posthog_keys.py).
  set -a; source <(sudo -n grep -E "^(BENCHMARK_[A-Z_]+|POSTHOG_API_KEY|POSTHOG_HOST|POSTHOG_BENCHMARK_API_KEY|POSTHOG_PERSONAL_API_KEY|POSTHOG_PROJECT_ID)=" /opt/lem/.env) 2>/dev/null; set +a
  if [ "${BENCHMARK_ENABLED:-false}" != "true" ]; then
    log "benchmark: BENCHMARK_ENABLED not set — skipping"
  else
    # Candidates = the new tags plus the family-upgrade candidates from the SAME plan the issues
    # were filed from. Bounded, and the drop is logged rather than silently truncated.
    CANDS="$(printf '%s' "$CAT" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
names, seen = [], set()
for name in list(plan['catalog']['added']) + [u['candidate'] for u in plan['upgrades']]:
    if name and name not in seen:
        seen.add(name); names.append(name)
print(','.join(names[:${BENCHMARK_MAX_CANDIDATES:-3}]), len(names))
" 2>/dev/null || echo " 0")"
    NCAND="${CANDS##* }"; CANDS="${CANDS%% *}"
    if [ "${NCAND:-0}" -gt "${BENCHMARK_MAX_CANDIDATES:-3}" ]; then
      log "benchmark: $NCAND candidates found, benchmarking the first ${BENCHMARK_MAX_CANDIDATES:-3}"
    fi
    if [ -z "$CANDS" ]; then
      log "benchmark: no new candidates this week — skipping"
    else
      BENCH_RESULTS="$DIR/benchmark-$(date -u +%Y%m%dT%H%M%SZ).json"
      log "benchmark: running tier suites for $CANDS (+ champions)"
      "${PY[@]}" scripts/benchmark_models.py --run --models "$CANDS" --config "$BOX_CFG" \
          --results-out "$BENCH_RESULTS" --out-dir "$DIR/benchmark-report" >>"$LOG" 2>&1
      BRC=$?
      # 0 = ran, nothing recommended; 2 = ran, recommendations. Anything else is a real failure.
      case "$BRC" in
        0|2)
          log "benchmark: completed (rc=$BRC)"
          open_pr "auto/model-benchmark" \
            "docs(model-benchmarks): tier benchmark report for cron-detected candidates" \
            "Automated by the weekly model-health check (issue #721). Each candidate and the tier's current champion ran the synthetic suites in tests/benchmarks/model_tiers/. Recommendations in the report are advisory: adopting one is a deliberate .litellm/config.yaml change, never an entry in the retirement map." \
            mutate_benchmark
          if [ "$BRC" -eq 2 ]; then
            MSG="$(printf '%s' "$(cat "$BENCH_RESULTS")" | python3 -c "
import sys, json
run = json.load(sys.stdin)
for r in run['recommendations']:
    # A recommendation is a QUALITY verdict; #842's standing spend policy says whether it may be
    # taken. This alert is where the owner meets the swap, so the 'hold' has to arrive with it.
    policy = (r.get('quota_policy') or {}).get('decision') or 'unknown'
    quota = (r.get('usage_delta') or {}).get('direction') or 'unknown'
    print(f\"{r['tier']}: {r['champion']} -> {r['model']} (benchmark-recommended, not applied; quota {quota}, spend policy: {policy})\")
" 2>/dev/null || echo "(could not read recommendations)")"
            alert "Model benchmark recommends a swap:"$'\n'"$MSG"
          fi
          ;;
        *) alert "Model-tier benchmark FAILED (rc=$BRC) — see $LOG. Model-health swaps were unaffected.";;
      esac
    fi
  fi
fi
log "=== weekly model-health check done ==="
