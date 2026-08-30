#!/usr/bin/env python3
"""Provision LEM's boolean feature flags in PostHog from the in-code registry.

Every other PostHog surface has a provisioner (dashboards, alerts, surveys, experiments, endpoints);
plain boolean flags were the one gap — `docs/feature-flags.md` just said "create the flag in
PostHog", and nobody had, so all five resolved by failing open to their env vars and the kill
switches did not actually exist.

`utilities/flags.py` is the source of truth, not a copy here: the registry supplies the key, the env
var and the default, so a new flag is one registry entry rather than two edits that can drift.

THE SAFETY PROPERTY: a flag is created at the rollout that REPRODUCES WHAT IT RESOLVES TO TODAY.
`flag_enabled()` currently falls back to `env_default()` everywhere, so creating a flag that resolves
differently would change production behaviour the moment it appears — silently, with no deploy and no
log line. `feed-fallback-when-empty-default` is the live example: it defaults to TRUE, so it must be
created at 100%. Creating it at the "obvious" 0% would flip the fleet engagement default for every
user without a saved preferences row.

Release conditions are rollout-percentage only, never person properties: `flags.py` evaluates
locally (`only_evaluate_locally=True`), and a condition needing server-held person properties
silently falls back to env in every Celery worker — which looks identical to the flag working.

Usage:
    python scripts/posthog_flags.py --dry-run     # default; report only
    python scripts/posthog_flags.py --apply
Exit: 0 in sync / applied, 2 pending changes in dry-run, 1 error.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# Reached by path, not by installation: this is run by hand from a checkout, the same way
# posthog_key_check.py reaches the resolver. posthog_keys.py is stdlib-only, so this costs nothing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from cqc_lem.utilities.posthog_keys import missing_key_message, operator_api_key  # noqa: E402

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

# Only these are compared against what PostHog returns, so a field the UI adds (folder, created_by,
# tags, experiment links) never reads as permanent drift in --dry-run.
MANAGED_FIELDS = ("name", "active", "rollout")


def registry_specs() -> list:
    """The registry as provisioning specs. Imports the app package deliberately —
    `utilities/flags.py` IS the contract, and duplicating it here is how the two drift."""
    from cqc_lem.utilities import flags

    specs = []
    for row in flags.registry_rows():
        resolved = flags.env_default(row["key"])
        specs.append({
            "key": row["key"],
            # A flag's rollout must reproduce today's resolved value, not its registry default —
            # the env var may already override it in prod.
            "rollout": 100 if resolved else 0,
            "resolved": resolved,
            "name": f"{row['description'][:120]}",
            "env_var": row["env_var"],
            "owner": row["owner"],
        })
    return specs


def flag_payload(spec: dict) -> dict:
    return {
        "key": spec["key"],
        "name": spec["name"],
        "active": True,
        "filters": {
            # Property-free by design — see the module docstring.
            "groups": [{"properties": [], "rollout_percentage": spec["rollout"]}],
        },
    }


def _current_rollout(flag: dict) -> Optional[int]:
    groups = ((flag.get("filters") or {}).get("groups") or [])
    if not groups:
        return None
    return groups[0].get("rollout_percentage")


def plan_flags(specs: list, existing: dict) -> list:
    """Pure planning: create / update / noop per flag. No network."""
    actions = []
    for spec in specs:
        found = existing.get(spec["key"])
        if not found:
            actions.append({"action": "create_flag", "key": spec["key"], "spec": spec})
            continue
        drift = []
        if _current_rollout(found) != spec["rollout"]:
            drift.append("rollout")
        if not found.get("active"):
            drift.append("active")
        if drift:
            actions.append({"action": "update_flag", "key": spec["key"], "spec": spec,
                            "flag_id": found.get("id"), "fields": drift})
        else:
            actions.append({"action": "noop", "key": spec["key"]})
    return actions


def pending(actions: list) -> list:
    return [a for a in actions if a["action"] != "noop"]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    if not pending(actions):
        return "PostHog feature flags in sync — nothing to do."
    return "Pending: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()) if k != "noop")


class PostHogFlagClient:
    """Thin PostHog REST wrapper — only the feature-flag calls this provisioner needs."""

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

    def list_flags(self) -> dict:
        flags = {}
        for item in self._paged("/feature_flags/?limit=100"):
            if item.get("deleted"):
                continue
            flags[item.get("key")] = item
        return flags

    def create_flag(self, payload: dict):
        return self._request("POST", "/feature_flags/", json=payload).get("id")

    def update_flag(self, flag_id, payload: dict) -> None:
        self._request("PATCH", f"/feature_flags/{flag_id}/", json=payload)


def apply_actions(client: PostHogFlagClient, actions: list, dry_run: bool = True) -> list:
    log: list = []
    for action in actions:
        kind, key = action["action"], action.get("key")
        spec = action.get("spec") or {}
        if kind == "noop":
            log.append(f"flag '{key}' is in sync")
        elif kind == "create_flag":
            resolved = "enabled" if spec.get("resolved") else "disabled"
            detail = f"'{key}' at {spec.get('rollout')}% (matches today's {resolved} via {spec.get('env_var')})"
            if dry_run:
                log.append(f"[dry-run] create flag {detail}")
                continue
            flag_id = client.create_flag(flag_payload(spec))
            log.append(f"created flag {detail} -> {flag_id}")
        elif kind == "update_flag":
            fields = ", ".join(action.get("fields") or [])
            if dry_run:
                log.append(f"[dry-run] update flag '{key}' ({fields}) -> {spec.get('rollout')}%")
                continue
            client.update_flag(action["flag_id"], flag_payload(spec))
            log.append(f"updated flag '{key}' ({fields}) -> {spec.get('rollout')}%")
    return log


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision LEM's boolean feature flags in PostHog from utilities/flags.py.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to PostHog.")
    mode.add_argument("--dry-run", action="store_true", help="Report changes only (default).")
    parser.add_argument("--print-spec", action="store_true",
                        help="Print the planned flags and exit (no network, no key needed).")
    args = parser.parse_args(argv)
    dry_run = not args.apply

    try:
        specs = registry_specs()
    except Exception as exc:
        print(f"error: could not read the flag registry ({exc}). Run with the app venv.",
              file=sys.stderr)
        return 1

    if args.print_spec:
        for spec in specs:
            state = "ON" if spec["resolved"] else "OFF"
            print(f"{spec['key']:<36} {spec['rollout']:>3}%  ({state} today via {spec['env_var']})")
        return 0

    api_key = operator_api_key()
    if not api_key:
        print(f"error: {missing_key_message('operator')} (scope: feature_flag:read+write).",
              file=sys.stderr)
        return 1
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)

    client = PostHogFlagClient(api_key, project_id, app_host)
    try:
        existing = client.list_flags()
    except Exception as exc:
        print(f"error: could not list PostHog feature flags ({exc})", file=sys.stderr)
        return 1

    actions = plan_flags(specs, existing)
    try:
        for line in apply_actions(client, actions, dry_run=dry_run):
            print(line)
    except Exception as exc:
        print(f"error: applying flag changes failed ({exc})", file=sys.stderr)
        return 1

    print(summarize(actions))
    if dry_run and pending(actions):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
