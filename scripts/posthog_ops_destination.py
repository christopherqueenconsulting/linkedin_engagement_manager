#!/usr/bin/env python3
"""Provision ONE realtime PostHog CDP destination — an ops ping the instant the LinkedIn 429
breaker trips (issue #655, PH10 spike).

Every ops signal LEM has today is either a daily/weekly rollup (the four #650 threshold alerts,
evaluated `calculation_interval: daily`) or a cron (`scripts/posthog_error_issues.py`, run once a
day). CDP destinations are PostHog's realtime layer: a HogFunction fires within seconds of the
`rate_limit_trip` event actually being captured, instead of waiting for the next scheduled
evaluation. This script proves that layer out with the ONE event where a same-minute ping is worth
more than a same-day one — the breaker escalates its own cooldown (`utilities/linkedin/rate_limit.py`),
so the doom loop re-forming is exactly the case where 23 hours of delay costs real automation time.

Split like every other posthog_*.py script: PURE plan logic (unit-tested) and a thin I/O client
(mocked in tests).

CLI (--dry-run and --apply are mutually exclusive):
  --dry-run        Report what would change. No writes. (default)
  --apply          Create/update the destination.
  --print-payload  Print the HogFunction payload for a placeholder URL and exit. No network.
Env:
  POSTHOG_PERSONAL_API_KEY  Personal API key (required for network). Scope: hog_function read+write.
  POSTHOG_PROJECT_ID        PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST          App host for the API (default https://us.posthog.com).
  POSTHOG_OPS_WEBHOOK_URL   https:// URL the ping is delivered to (a Slack incoming-webhook URL, a
                            Discord webhook, or any generic https endpoint). Absent = there is
                            nothing to point the destination at yet: the script explains what's
                            missing and exits 0 rather than failing a run that has no channel
                            configured — the same "degrade to a no-op, never a failure" shape as
                            scripts/posthog_annotate.py's missing key.
Exit: 0 nothing to do / applied / no webhook URL configured yet, 2 a change is pending (--dry-run
with a URL set), 1 error (unreachable PostHog, missing personal API key).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

DESTINATION_NAME = "LEM ops ping — LinkedIn 429 breaker tripped"
TRIGGER_EVENT = "rate_limit_trip"
TEMPLATE_ID = "template-webhook"
# `destination` (not `internal_destination`): internal_destination is PostHog's reserved type for
# its OWN internal-only signals ($insight_alert_firing, $activity_log_entry_created, ...) — those
# don't flow through the regular event stream, and neither does an internal_destination function
# fire for anything else. `rate_limit_trip` is an ordinary captured event (it already backs a
# regular trends insight in posthog_provision.py), so it needs the standard `destination` type that
# consumes the normal event-ingestion pipeline; `internal_destination` would create successfully but
# never actually fire.
FUNCTION_TYPE = "destination"
DESCRIPTION = (
    "Realtime ping the moment the LinkedIn 429 circuit breaker trips (utilities/linkedin/"
    "rate_limit.py), rather than waiting for the daily 'LEM — LinkedIn 429 spike' threshold "
    "alert (issue #650) to evaluate. See docs/posthog-advanced-surface.md."
)
PLACEHOLDER_URL = "https://example.com/lem-ops-webhook"


# ── pure planning (unit-tested) ──────────────────────────────────────────────────────

def destination_filters() -> dict:
    return {"events": [{"id": TRIGGER_EVENT, "type": "events"}]}


def destination_payload(webhook_url: str) -> dict:
    """The HogFunction body PostHog expects for a webhook-template internal destination."""
    return {
        "type": FUNCTION_TYPE,
        "name": DESTINATION_NAME,
        "description": DESCRIPTION,
        "template_id": TEMPLATE_ID,
        "enabled": True,
        "filters": destination_filters(),
        "inputs": {"url": {"value": webhook_url}},
    }


def _webhook_url_of(existing: dict) -> Optional[str]:
    return ((existing.get("inputs") or {}).get("url") or {}).get("value")


def plan_destination(existing: Optional[dict], webhook_url: str) -> dict:
    """Diff the destination spec against what PostHog already has. `webhook_url` empty means there
    is nothing to point the destination at — that is `blocked`, not `create`, so a dry-run never
    claims a follow-up `--apply` could create it as-is."""
    if not webhook_url:
        return {"action": "blocked",
                "reason": "POSTHOG_OPS_WEBHOOK_URL is not set — nothing to point the destination at"}
    payload = destination_payload(webhook_url)
    if existing is None:
        return {"action": "create", "payload": payload}
    if (existing.get("filters") == payload["filters"] and existing.get("enabled", True)
            and _webhook_url_of(existing) == webhook_url):
        return {"action": "unchanged", "id": existing.get("id")}
    return {"action": "update", "id": existing.get("id"), "payload": payload}


def apply_plan(client: "PostHogFunctionsClient", action: dict, dry_run: bool) -> str:
    """Execute the plan. `dry_run` reports without writing. Returns the one log line emitted."""
    kind = action["action"]
    if kind == "blocked":
        return f"skipped '{DESTINATION_NAME}': {action['reason']}"
    if kind == "unchanged":
        return f"unchanged '{DESTINATION_NAME}' (id={action['id']})"
    verb = "create" if kind == "create" else "update"
    if dry_run:
        return f"[dry-run] {verb} destination '{DESTINATION_NAME}'"
    if kind == "create":
        created = client.create(action["payload"])
        return f"created destination '{DESTINATION_NAME}' -> {created.get('id')}"
    client.update(action["id"], action["payload"])
    return f"updated destination '{DESTINATION_NAME}' (id={action['id']})"


# ── I/O (mocked in tests) ────────────────────────────────────────────────────────────

class PostHogFunctionsClient:
    """Thin PostHog REST wrapper — only the `hog_functions` calls this script needs."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.app_host = app_host.rstrip("/")
        self._base = f"{self.app_host}/api/projects/{project_id}"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _paged(self, path: str) -> list:
        import requests
        results, url = [], f"{self._base}{path}"
        while url:
            response = requests.get(url, headers=self._headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            results.extend(payload.get("results", []))
            url = payload.get("next")
        return results

    def find_destination(self, name: str) -> Optional[dict]:
        for item in self._paged(f"/hog_functions/?type={FUNCTION_TYPE}&limit=100"):
            if item.get("name") == name and not item.get("deleted"):
                return item
        return None

    def create(self, payload: dict) -> dict:
        import requests
        response = requests.post(f"{self._base}/hog_functions/", headers=self._headers,
                                 json=payload, timeout=30)
        response.raise_for_status()
        return response.json() if response.content else {}

    def update(self, function_id, payload: dict) -> None:
        import requests
        response = requests.patch(f"{self._base}/hog_functions/{function_id}/",
                                  headers=self._headers, json=payload, timeout=30)
        response.raise_for_status()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision the LEM ops-ping realtime CDP destination (429 breaker trip).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to PostHog.")
    mode.add_argument("--dry-run", action="store_true", help="Report changes only (default).")
    parser.add_argument("--print-payload", action="store_true",
                        help="Print the HogFunction payload for a placeholder URL and exit.")
    args = parser.parse_args(argv)

    if args.print_payload:
        import json
        print(json.dumps(destination_payload(PLACEHOLDER_URL), indent=2))
        return 0

    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    if not api_key:
        print("POSTHOG_PERSONAL_API_KEY is not set — cannot reach PostHog.", file=sys.stderr)
        return 1

    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
    dry_run = args.dry_run or not args.apply
    webhook_url = os.getenv("POSTHOG_OPS_WEBHOOK_URL", "").strip()

    client = PostHogFunctionsClient(api_key, project_id, app_host)
    try:
        existing = client.find_destination(DESTINATION_NAME)
    except Exception as exc:
        print(f"Failed to read PostHog state: {exc}", file=sys.stderr)
        return 1

    action = plan_destination(existing, webhook_url)
    print(apply_plan(client, action, dry_run))

    if action["action"] == "blocked":
        print("Set POSTHOG_OPS_WEBHOOK_URL to a Slack incoming-webhook (or any https:// webhook) "
              "URL, then re-run with --apply.", file=sys.stderr)
        return 0
    if dry_run and action["action"] in ("create", "update"):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
