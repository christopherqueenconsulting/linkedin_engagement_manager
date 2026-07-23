#!/usr/bin/env bash
# One-time: label the roadmap issues so the pipeline knows what to work, what to auto-merge,
# and what to hand to you. Idempotent. Run as `lem`.
set -uo pipefail
SLUG="christopherqueenconsulting/linkedin_engagement_manager"
add(){ gh issue edit "$1" --repo "$SLUG" --add-label "$2" >/dev/null 2>&1 && echo "  #$1 += $2"; }

echo "agent:ready (workable by the pipeline)"
for n in 382 383 384 385 405 386 387 388 389 390 391 392 393 394 406 395 396 397 398 399 400 401 402; do add "$n" "agent:ready"; done

echo "needs-human + assign (live-LinkedIn spikes that can't run headless)"
for n in 403 404; do
  gh issue edit "$n" --repo "$SLUG" --add-label needs-human --add-label risk:live-linkedin --add-assignee gitchrisqueen >/dev/null 2>&1 && echo "  #$n needs-human+assigned"
done

echo "risk:migration (built to green, held for your merge)"
for n in 382 385 386 400; do add "$n" "risk:migration"; done
echo "risk:product-decision"
for n in 385 398 399 400; do add "$n" "risk:product-decision"; done
echo "risk:live-linkedin (agent-workable, held for your merge)"
for n in 387 390; do add "$n" "risk:live-linkedin"; done
echo "done"
