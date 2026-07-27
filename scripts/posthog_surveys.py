#!/usr/bin/env python3
"""Provision LEM's two PostHog Surveys — the 30-day NPS and the post-quality CSAT (issue #653).

LEM already had survey capture: a `survey_prompts` ledger, a hand-written `select_survey` policy and
an email beat. What it did not have was TARGETING it could change without a deploy, a throttle that
spans every survey at once, or a place to read responses that wasn't a MySQL table. PostHog Surveys
gives all three (1,500 responses/month on the free tier), so the two general-purpose asks move here
and the bespoke ones — the review that unlocks the extended trial (#499), the per-issue "did this
fix it?" (#502) — stay in `utilities/surveys.py` where they belong.

Both surveys are created as type **`api`**: PostHog decides WHO and WHEN, the SPA renders the form.
That is not a style choice. A popover survey's answer is a PostHog event and nothing else, and LEM's
whole reason for asking is the feedback->auto-work loop that turns a complaint into a GitHub issue.
Rendering headless in the SPA's own modal (`PostHogSurveyModal.tsx`) is what lets one answer become
both a `survey sent` event AND a `feedback` row. It also keeps LEM's chrome: the popover would land
on top of the feedback widget in the same corner.

Targeting is expressed the PostHog way, against person properties the SPA sets at `$identify`
(`GET /auth/session` supplies them):

  • **NPS** — `onboarding_completed_at` is before `-30d`. Thirty days after ACTIVATION, not signup:
    a user who signed up and stalled has no opinion worth a score, and their detractor answer would
    be about onboarding rather than the product.
  • **CSAT** — triggered by the `post_approved` event, gated on `posts_approved >= 5`. The event is
    what makes it land in the moment the user just judged a draft; the property is what stops it
    firing on someone's first ever approval.

Both carry `seenSurveyWaitPeriodInDays`, which is PostHog's cross-survey throttle: answering (or
dismissing) either one buys 30 days of silence from BOTH.

Split like scripts/posthog_provision.py: PURE spec/plan logic (unit-tested) and a thin I/O layer.

CLI (--dry-run and --apply are mutually exclusive):
  --dry-run     Show what would be created/updated against the live project. No writes. (default)
  --apply       Create missing surveys and update drifted ones.
  --print-spec  Print both survey specs as JSON and exit (no network).
  --launch      With --apply, also set start_date on a survey that has never been launched.
Env:
  POSTHOG_PERSONAL_API_KEY  Personal API key (required for network). Scopes: survey read+write,
                            plus feature_flag write (a targeting flag is created with the survey).
  POSTHOG_PROJECT_ID        PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST          App host for the API (default https://us.posthog.com).
Exit: 0 in sync / applied, 2 changes pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

# Survey NAMES are the contract with two other places: utilities/surveys.py maps a name onto a
# feedback source, and the SPA matches on it to know which form to render. A unit test holds this
# copy and `utilities.surveys` to each other — this script stays importable without the app package.
NPS_SURVEY_NAME = "LEM NPS"
CSAT_SURVEY_NAME = "LEM CSAT — post quality"

# Mirrors POSTHOG_NPS_AFTER_ACTIVATION_DAYS / POSTHOG_CSAT_MIN_APPROVALS / POSTHOG_SURVEY_WAIT_DAYS.
NPS_AFTER_ACTIVATION_DAYS = 30
CSAT_MIN_APPROVALS = 5
SURVEY_WAIT_DAYS = 30

# The person properties the rules read, set by the SPA at $identify off GET /auth/session.
PERSON_ONBOARDING_COMPLETED = "onboarding_completed_at"
PERSON_POSTS_APPROVED = "posts_approved"
# The SPA event the CSAT rides on (ui/src/utils/analytics.ts EVENTS.postApproved).
CSAT_TRIGGER_EVENT = "post_approved"

# `api` = PostHog targets, LEM renders. See the module docstring for why this is load-bearing.
SURVEY_TYPE = "api"

# Only these keys are compared against what PostHog returns, so a field the UI adds (appearance
# tweaks, folder, created_by) never shows up as permanent drift in --dry-run.
MANAGED_FIELDS = ("description", "type", "questions", "conditions", "schedule")


def _rating(question: str, scale: int, lower: str, upper: str,
            description: Optional[str] = None) -> dict:
    # scale 10 is PostHog's 0-10 NPS band; every other scale starts at 1.
    return {"type": "rating", "question": question, "description": description,
            "display": "number", "scale": scale,
            "lowerBoundLabel": lower, "upperBoundLabel": upper, "optional": False}


def _open(question: str, description: Optional[str] = None) -> dict:
    # Always optional: a required "why" turns a 5-second ask into an abandoned one.
    return {"type": "open", "question": question, "description": description, "optional": True}


def nps_survey_spec() -> dict:
    return {
        "name": NPS_SURVEY_NAME,
        "description": (
            f"NPS, asked once a user is {NPS_AFTER_ACTIVATION_DAYS} days past activation. Rendered "
            "headless by the SPA (issue #653); responses also land as `feedback` rows so detractors "
            "open a report in the feedback->auto-work loop."),
        "type": SURVEY_TYPE,
        "schedule": "once",
        "questions": [
            _rating("How likely are you to recommend LEM to a colleague?", 10,
                    "Not at all likely", "Extremely likely"),
            _open("What's the main reason for your score?"),
        ],
        "conditions": {"seenSurveyWaitPeriodInDays": SURVEY_WAIT_DAYS},
        "targeting_flag_filters": {
            "groups": [{
                "properties": [{
                    "key": PERSON_ONBOARDING_COMPLETED,
                    "type": "person",
                    "operator": "is_date_before",
                    "value": f"-{NPS_AFTER_ACTIVATION_DAYS}d",
                }],
                "rollout_percentage": 100,
            }],
        },
    }


def csat_survey_spec() -> dict:
    return {
        "name": CSAT_SURVEY_NAME,
        "description": (
            f"Post-quality CSAT, triggered by `{CSAT_TRIGGER_EVENT}` once a user has approved "
            f"{CSAT_MIN_APPROVALS}+ posts. Asked in the moment they just judged a draft, so the "
            "rating is about the writing rather than the product in general (issue #653)."),
        "type": SURVEY_TYPE,
        "schedule": "once",
        "questions": [
            _rating("How happy are you with the posts LEM writes for you?", 5,
                    "Not happy", "Very happy"),
            _open("What would make the writing better?"),
        ],
        "conditions": {
            "seenSurveyWaitPeriodInDays": SURVEY_WAIT_DAYS,
            # repeatedActivation so a user who dismisses without answering can be re-triggered by a
            # later approval; the wait period above is what stops that becoming nagging.
            "events": {"repeatedActivation": True, "values": [{"name": CSAT_TRIGGER_EVENT}]},
        },
        "targeting_flag_filters": {
            "groups": [{
                "properties": [{
                    "key": PERSON_POSTS_APPROVED,
                    "type": "person",
                    "operator": "gte",
                    "value": CSAT_MIN_APPROVALS,
                }],
                "rollout_percentage": 100,
            }],
        },
    }


def build_specs() -> list:
    return [nps_survey_spec(), csat_survey_spec()]


def create_payload(spec: dict) -> dict:
    """What POST /surveys/ receives. `start_date` is deliberately absent — a survey is created as a
    draft and launched explicitly (--launch), so an --apply can never start collecting responses
    from a definition nobody has read."""
    payload = {k: v for k, v in spec.items() if k != "targeting_flag_filters"}
    payload["enable_partial_responses"] = False
    if spec.get("targeting_flag_filters"):
        payload["targeting_flag_filters"] = spec["targeting_flag_filters"]
    return payload


def update_payload(spec: dict) -> dict:
    """What PATCH /surveys/<id>/ receives. Targeting filters are included: they are the rule most
    likely to be tuned, and PostHog updates the survey's existing targeting flag in place."""
    return create_payload(spec)


def drifted_fields(spec: dict, existing: dict) -> list:
    """Managed fields whose live value no longer matches the spec. Deliberately narrow — see
    MANAGED_FIELDS."""
    drift = []
    for field in MANAGED_FIELDS:
        want = spec.get(field)
        if want is None:
            continue
        if _normalize(existing.get(field)) != _normalize(want):
            drift.append(field)
    if _targeting_drifted(spec, existing):
        drift.append("targeting_flag_filters")
    return drift


def _targeting_drifted(spec: dict, existing: dict) -> bool:
    want = spec.get("targeting_flag_filters")
    if not want:
        return False
    live = (existing.get("targeting_flag") or {}).get("filters")
    if not live:
        return True
    return _normalize(_targeting_groups(live)) != _normalize(_targeting_groups(want))


def _targeting_groups(filters: object) -> list:
    """Just the property groups. PostHog echoes back extra flag machinery (payloads, multivariate,
    super_groups) that no spec here sets, and comparing the whole document would report drift on
    every run."""
    groups = (filters or {}).get("groups") if isinstance(filters, dict) else None
    normalized = []
    for group in groups or []:
        normalized.append({
            "rollout_percentage": group.get("rollout_percentage"),
            "properties": [{k: p.get(k) for k in ("key", "type", "operator", "value")}
                           for p in (group.get("properties") or [])],
        })
    return normalized


def _normalize(value):
    """Compare by VALUE, not by JSON text: PostHog returns numbers as strings in property filters
    and drops explicit nulls, either of which would otherwise read as permanent drift."""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items()) if v is not None}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return value


def plan_actions(specs: list, existing: dict, launch: bool = False) -> list:
    """Create/update/launch actions for each spec against the live surveys, keyed by name."""
    actions = []
    for spec in specs:
        found = existing.get(spec["name"])
        if not found:
            actions.append({"action": "create_survey", "survey": spec["name"], "spec": spec})
            continue
        drift = drifted_fields(spec, found)
        if drift:
            actions.append({"action": "update_survey", "survey": spec["name"], "spec": spec,
                            "survey_id": found.get("id"), "fields": drift})
        else:
            actions.append({"action": "noop", "survey": spec["name"]})
        if launch and not found.get("start_date"):
            actions.append({"action": "launch_survey", "survey": spec["name"],
                            "survey_id": found.get("id")})
    return actions


def pending(actions: list) -> list:
    return [a for a in actions if a["action"] != "noop"]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    if not pending(actions):
        return "PostHog surveys in sync — nothing to do."
    return "Pending: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()) if k != "noop")


def survey_url(app_host: str, project_id: str, survey_id) -> str:
    return f"{app_host.rstrip('/')}/project/{project_id}/surveys/{survey_id}"


class PostHogSurveyClient:
    """Thin PostHog REST wrapper — only the survey calls this provisioner needs."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.project_id = project_id
        self.app_host = app_host.rstrip("/")
        self._base = f"{self.app_host}/api/projects/{project_id}"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        import requests
        response = requests.request(method, f"{self._base}{path}", headers=self._headers,
                                    timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

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

    def list_surveys(self) -> dict:
        surveys = {}
        for item in self._paged("/surveys/?limit=100"):
            if item.get("archived"):
                continue
            surveys[item.get("name")] = item
        return surveys

    def create_survey(self, payload: dict):
        return self._request("POST", "/surveys/", json=payload).get("id")

    def update_survey(self, survey_id, payload: dict) -> None:
        self._request("PATCH", f"/surveys/{survey_id}/", json=payload)

    def launch_survey(self, survey_id, start_date: str) -> None:
        self._request("PATCH", f"/surveys/{survey_id}/", json={"start_date": start_date})


def apply_actions(client: PostHogSurveyClient, actions: list, dry_run: bool = True,
                  now: Optional[datetime] = None) -> list:
    """Execute the plan. `dry_run` reports without writing. Returns the log lines emitted."""
    log: list = []
    start_date = (now or datetime.now(timezone.utc)).isoformat()
    for action in actions:
        kind, name = action["action"], action.get("survey")
        if kind == "noop":
            log.append(f"survey '{name}' is in sync")
        elif kind == "create_survey":
            if dry_run:
                log.append(f"[dry-run] create survey '{name}' (draft — launch it explicitly)")
                continue
            survey_id = client.create_survey(create_payload(action["spec"]))
            log.append(f"created survey '{name}' -> {survey_id}")
        elif kind == "update_survey":
            fields = ", ".join(action.get("fields") or [])
            if dry_run:
                log.append(f"[dry-run] update survey '{name}' ({fields})")
                continue
            client.update_survey(action["survey_id"], update_payload(action["spec"]))
            log.append(f"updated survey '{name}' ({fields})")
        elif kind == "launch_survey":
            if dry_run:
                log.append(f"[dry-run] launch survey '{name}'")
                continue
            client.launch_survey(action["survey_id"], start_date)
            log.append(f"launched survey '{name}'")
    return log


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Provision LEM's PostHog NPS + CSAT surveys.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to PostHog.")
    mode.add_argument("--dry-run", action="store_true", help="Report changes only (default).")
    parser.add_argument("--print-spec", action="store_true",
                        help="Print both survey specs as JSON and exit.")
    parser.add_argument("--launch", action="store_true",
                        help="Also launch a survey that has never been started.")
    args = parser.parse_args(argv)

    if args.print_spec:
        print(json.dumps(build_specs(), indent=2, ensure_ascii=False))
        return 0

    api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    if not api_key:
        print("POSTHOG_PERSONAL_API_KEY is not set — cannot reach PostHog.", file=sys.stderr)
        return 1
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
    dry_run = args.dry_run or not args.apply

    client = PostHogSurveyClient(api_key, project_id, app_host)
    try:
        existing = client.list_surveys()
    except Exception as exc:
        print(f"Failed to read PostHog surveys: {exc}", file=sys.stderr)
        return 1

    actions = plan_actions(build_specs(), existing, launch=args.launch)
    for line in apply_actions(client, actions, dry_run=dry_run):
        print(line)
    print(summarize(actions))
    for name, survey in existing.items():
        if name in {spec["name"] for spec in build_specs()}:
            print(f"{name}: {survey_url(app_host, project_id, survey.get('id'))}")

    if dry_run and pending(actions):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
