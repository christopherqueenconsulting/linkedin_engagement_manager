#!/usr/bin/env python3
"""Post a release annotation to PostHog at deploy time (issue #654).

LEM ships multiple releases a day (`docs/zero-downtime-deploys.md`), and no dashboard graph showed
when — a metric step-change and a deploy were two facts a human had to correlate by hand.
`build-and-push.yml`'s deploy job calls this after a successful deploy; PostHog renders the
annotation as a marker on every insight graph, so "why did this change" usually answers itself.

Split the same way as the other posthog_*.py scripts: PURE payload-building logic (unit-tested)
and a thin I/O client (mocked in tests).

CLI:
  --tag TAG    Release tag the annotation names (e.g. v1.2.3). Required.
  --content    Override the default "<TAG> deployed" text.
  --dry-run    Print the payload; no network call.
Env:
  POSTHOG_PERSONAL_API_KEY  Personal API key. Scope: annotation read+write. Absent = the call is
                            skipped and this script exits 0 — a missing key degrades the release
                            pipeline to "no annotation", never a failed deploy.
  POSTHOG_PROJECT_ID        PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST          App host for the API (default https://us.posthog.com).
Exit: 0 on success, on a skipped (keyless) call, and on --dry-run; 1 on a real API error — the
caller (build-and-push.yml's deploy job) runs this step with `continue-on-error: true`, so a
PostHog outage is visible in the logs without failing a release that already passed its health
check.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Optional

DEFAULT_APP_HOST = "https://us.posthog.com"
DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.


def annotation_payload(tag: str, now: datetime, content: Optional[str] = None) -> dict:
    """PostHog's annotation body. `scope: project` marks it on every insight in the project, not
    just one dashboard — a release affects everything, not one graph."""
    return {
        "content": content or f"{tag} deployed",
        "date_marker": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "project",
    }


class PostHogAnnotationsClient:
    """Thin PostHog REST wrapper — the one call this script needs."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.app_host = app_host.rstrip("/")
        self._url = f"{self.app_host}/api/projects/{project_id}/annotations/"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def create_annotation(self, payload: dict) -> dict:
        import requests
        response = requests.post(self._url, headers=self._headers, json=payload, timeout=30)
        response.raise_for_status()
        return response.json() if response.content else {}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Post a release annotation to PostHog.")
    parser.add_argument("--tag", required=True, help="Release tag the annotation names (e.g. v1.2.3).")
    parser.add_argument("--content", help="Override the default '<TAG> deployed' text.")
    parser.add_argument("--dry-run", action="store_true", help="Print the payload; no network call.")
    args = parser.parse_args(argv)

    payload = annotation_payload(args.tag, datetime.now(timezone.utc), args.content)

    if args.dry_run:
        print(payload)
        return 0

    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    if not api_key:
        print("POSTHOG_PERSONAL_API_KEY is not set — skipping the release annotation.",
              file=sys.stderr)
        return 0

    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
    client = PostHogAnnotationsClient(api_key, project_id, app_host)
    try:
        created = client.create_annotation(payload)
    except Exception as exc:
        # A PostHog outage is not a reason to fail a release that already shipped — this runs
        # AFTER deploy.sh's own health check has passed.
        print(f"Could not post the release annotation: {exc}", file=sys.stderr)
        return 1

    print(f"Posted release annotation {created.get('id', '?')}: {payload['content']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
