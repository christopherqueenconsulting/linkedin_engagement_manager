#!/usr/bin/env python3
"""Provision the LEM PostHog EXPERIMENTS — the multivariate flags and the experiment records that
turn `utilities/experiments.py`'s registry into a readout a human can open (issue #652).

An experiment in PostHog is two objects: a multivariate feature flag (the arms + the rollout that
decides who is in which) and an experiment record (which flag, which metrics, which stats engine).
The code can resolve a variant without either of them existing — it just always answers "control" —
so this script is what makes the experiment REAL, and it is code rather than UI clicks for the same
reason scripts/posthog_provision.py is: a flag someone edited by hand is not reviewable.

Split like the other two provisioners: PURE spec/plan logic (unit-tested) over a thin I/O layer.

The one deliberate difference from #650's dashboards: this script NEVER changes an existing flag's
rollout percentage. PostHog owns the ramp once an experiment is running, and an `--apply` that reset a
50% ramp back to the spec's starting 10% would silently re-cohort a live experiment. Use
`--rollout KEY=PCT` to move it on purpose.

CLI (--dry-run and --apply are mutually exclusive):
  --dry-run              Show what would be created against the live project. No writes. (default)
  --apply                Create missing flags/experiments (and any --rollout change).
  --print-specs          Print the registry + the payloads that would be sent. No network.
  --rollout KEY=PCT      Set one experiment flag's treatment rollout (0-100). Needs --apply to write.
Env:
  POSTHOG_OPERATOR_API_KEY  Personal API key (required for network), falling back to
                            POSTHOG_PERSONAL_API_KEY (posthog_keys.py owns the precedence). Scopes:
                            feature_flag and experiment read+write.
  POSTHOG_PROJECT_ID        PostHog project id (default 475262 — "CQC LEM").
  POSTHOG_APP_HOST          App host for the API (default https://us.posthog.com).
Exit: 0 in sync / applied, 2 changes pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cqc_lem.utilities.experiments import (  # noqa: E402
    ASSIGNMENT_SHIPPED,
    EXPERIMENTS,
    ExperimentSpec,
    variant_slug,
)
from cqc_lem.utilities.posthog_keys import missing_key_message, operator_api_key  # noqa: E402

DEFAULT_PROJECT_ID = "475262"  # "CQC LEM" — not a secret; the key that reaches it is.
DEFAULT_APP_HOST = "https://us.posthog.com"

TAGS = ["lem", "experiment"]

# PostHog's frequentist engine needs a minimum-detectable-effect to size the run. 30% is deliberately
# coarse: at LEM's current single-user volume nothing subtler than "obviously different" will ever
# reach significance, and claiming otherwise would be the small-sample lie docs/experiments.md warns
# about.
MINIMUM_DETECTABLE_EFFECT = 30


def media_combo_arms() -> list:
    """The media-variant arms, derived from the #396 harness's own combo matrix so they cannot drift
    from what the code can actually ship.

    Imported lazily: `cqc_lem.app` pulls in the Celery task modules and therefore the LLM client,
    which needs `OPENAI_API_KEY`. A failure is raised, never swallowed into a short arm list — an arm
    the code emits exposures for but the flag doesn't define reads as an EMPTY readout, which looks
    exactly like "the experiment found nothing"."""
    try:
        from cqc_lem.app.generate_variants import DEFAULT_COMBOS, combo_key
    except Exception as exc:  # pragma: no cover - environment, not logic
        raise SystemExit(
            f"Cannot read the media-variant combos: {exc}\n"
            "This script needs the app package importable. Source the stack's .env (OPENAI_API_KEY, "
            "…) or run it in a container:\n"
            "  docker exec celery_worker python scripts/posthog_experiments.py --dry-run")
    arms = []
    for combo in DEFAULT_COMBOS:
        slug = variant_slug(combo_key(combo))
        if slug not in arms:
            arms.append(slug)
    return arms


def variants_for(spec: ExperimentSpec) -> list:
    """The arms a flag is created with.

    A flag-assigned experiment's arms are its spec's. A SHIPPED-assignment one has data-defined arms
    (see `media_combo_arms`) rather than arms duplicated into the registry, where they would drift the
    first time a combo changed. The control arm always comes first: PostHog treats variant[0] as the
    baseline in the readout."""
    if spec.assignment != ASSIGNMENT_SHIPPED:
        return list(spec.variants)
    arms = [spec.control]
    arms += [arm for arm in media_combo_arms() if arm not in arms]
    return arms


def rollout_percentages(spec: ExperimentSpec, variants: list) -> list:
    """Even split across the treatment arms, with the remainder parked in control.

    Integer percentages that sum to exactly 100 is a PostHog validation rule, so the remainder is
    given to control rather than distributed — control absorbing a rounding point is harmless, a
    payload PostHog rejects is not."""
    treatments = [v for v in variants if v != spec.control]
    if not treatments:
        return [{"key": spec.control, "rollout_percentage": 100}]
    total = max(0, min(100, int(round(spec.rollout_pct * 100))))
    each = total // len(treatments)
    allocated = each * len(treatments)
    rows = [{"key": spec.control, "rollout_percentage": 100 - allocated}]
    rows += [{"key": key, "rollout_percentage": each} for key in treatments]
    return rows


def flag_payload(spec: ExperimentSpec) -> dict:
    """The multivariate flag body.

    `groups` carries a single 100% release condition with no properties on purpose: local evaluation
    (utilities/flags.py) cannot resolve a condition that needs person properties the server holds, and
    a condition it can't resolve makes every Celery worker fall back to control — silently. The
    variant split inside `multivariate` is what actually cohorts."""
    variants = variants_for(spec)
    return {
        "key": spec.key,
        "name": f"LEM experiment — {spec.key}",
        "tags": TAGS,
        "active": True,
        "filters": {
            "groups": [{"properties": [], "rollout_percentage": 100}],
            "multivariate": {"variants": rollout_percentages(spec, variants)},
        },
    }


def experiment_payload(spec: ExperimentSpec, flag_id: Optional[int] = None) -> dict:
    """The experiment record. Metrics are named by their event so the readout is built on the events
    LEM already emits (docs/experiments.md) rather than on a new bespoke one."""
    payload = {
        "name": f"LEM — {spec.key}",
        "description": spec.description,
        "feature_flag_key": spec.key,
        "parameters": {
            "feature_flag_variants": rollout_percentages(spec, variants_for(spec)),
            "minimum_detectable_effect": MINIMUM_DETECTABLE_EFFECT,
        },
        "metrics": [{"kind": "ExperimentMetric", "metric_type": "mean",
                     "source": {"kind": "EventsNode", "event": event}}
                    for event in spec.metric_events],
    }
    if flag_id is not None:
        payload["feature_flag"] = flag_id
    return payload


def build_specs() -> list:
    """Every registered experiment, in registry order."""
    return list(EXPERIMENTS.values())


# ── pure planning (unit-tested) ──────────────────────────────────────────────────────

def plan_actions(specs: list, existing_flags: dict, existing_experiments: dict,
                 rollouts: Optional[dict] = None) -> list:
    """Diff the registry against PostHog.

    `existing_flags` maps flag key -> {"id", "variants" (arm keys), "active"}; `existing_experiments`
    maps flag key -> {"id", "name"}. Returns ordered actions: `create_flag`, `update_flag_variants`
    (an arm the code knows about that the flag does not — otherwise every worker resolving it would
    fall back to control), `set_rollout`, `create_experiment`, `unchanged*`.

    An existing flag's rollout is NEVER planned as drift: PostHog owns the ramp once the experiment
    runs. Only an explicit `--rollout` moves it."""
    actions: list = []
    wanted_rollouts = {key: pct for key, pct in (rollouts or {}).items()}
    for spec in specs:
        wanted = variants_for(spec)
        found = existing_flags.get(spec.key)
        if found is None:
            actions.append({"action": "create_flag", "flag": spec.key,
                            "payload": flag_payload(spec)})
        else:
            missing = [arm for arm in wanted if arm not in (found.get("variants") or [])]
            if missing or not found.get("active", True):
                actions.append({"action": "update_flag_variants", "flag": spec.key,
                                "flag_id": found.get("id"), "missing": missing,
                                "payload": flag_payload(spec)})
            else:
                actions.append({"action": "unchanged_flag", "flag": spec.key,
                                "flag_id": found.get("id")})
        if spec.key in wanted_rollouts:
            actions.append({"action": "set_rollout", "flag": spec.key,
                            "flag_id": (found or {}).get("id"),
                            "rollout_pct": wanted_rollouts[spec.key],
                            "payload": rollout_payload(spec, wanted_rollouts[spec.key])})
        if spec.key in existing_experiments:
            actions.append({"action": "unchanged_experiment", "experiment": spec.key,
                            "experiment_id": existing_experiments[spec.key].get("id")})
        elif found is None:
            # PostHog needs the flag id, and a dry run that promised both in one pass would be lying
            # about what a follow-up --apply can do.
            actions.append({"action": "blocked_experiment", "experiment": spec.key,
                            "reason": f"flag '{spec.key}' does not exist yet"})
        else:
            actions.append({"action": "create_experiment", "experiment": spec.key,
                            "payload": experiment_payload(spec, found.get("id"))})
    return actions


def rollout_payload(spec: ExperimentSpec, pct: float) -> dict:
    """A rollout-only flag PATCH: the same variant list, re-split at `pct`."""
    resized = ExperimentSpec(
        key=spec.key, variants=spec.variants, owner=spec.owner,
        metric_events=spec.metric_events, description=spec.description,
        assignment=spec.assignment, rollout_pct=max(0.0, min(1.0, float(pct))))
    return {"filters": {"groups": [{"properties": [], "rollout_percentage": 100}],
                        "multivariate": {"variants": rollout_percentages(
                            resized, variants_for(resized))}}}


def parse_rollout(expression: str) -> dict:
    """`--rollout comment-contract-prompt=50` → `{"comment-contract-prompt": 0.5}`. Raises ValueError
    on an unregistered key or a percentage outside 0-100, so a typo can't quietly no-op."""
    key, _, raw = expression.partition("=")
    key = key.strip()
    if key not in EXPERIMENTS:
        raise ValueError(f"Unknown experiment '{key}'. Known: {', '.join(EXPERIMENTS)}")
    try:
        pct = float(raw.strip())
    except ValueError:
        raise ValueError(f"Rollout for '{key}' must be a number 0-100, got '{raw.strip()}'")
    if not 0 <= pct <= 100:
        raise ValueError(f"Rollout for '{key}' must be 0-100, got {pct}")
    return {key: pct / 100.0}


UNCHANGED_ACTIONS = ("unchanged_flag", "unchanged_experiment")


def pending(actions: list) -> list:
    return [a for a in actions if a["action"] not in UNCHANGED_ACTIONS]


def summarize(actions: list) -> str:
    counts: dict = {}
    for action in actions:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    return ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "nothing to do"


def experiment_url(app_host: str, project_id: str, experiment_id) -> str:
    return f"{app_host.rstrip('/')}/project/{project_id}/experiments/{experiment_id}"


# ── I/O (mocked in tests) ────────────────────────────────────────────────────────────

class PostHogClient:
    """Thin PostHog REST wrapper — only the calls this provisioner needs."""

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
            multivariate = ((item.get("filters") or {}).get("multivariate") or {})
            flags[item.get("key")] = {
                "id": item.get("id"),
                "active": item.get("active", True),
                "variants": [v.get("key") for v in (multivariate.get("variants") or [])],
            }
        return flags

    def list_experiments(self) -> dict:
        """Keyed by FLAG key, not by experiment name — the flag is the identity the code shares with
        PostHog, and an experiment someone renamed in the UI is still that experiment."""
        experiments = {}
        for item in self._paged("/experiments/?limit=100"):
            if item.get("deleted"):
                continue
            key = item.get("feature_flag_key") or (item.get("feature_flag") or {}).get("key")
            if key:
                experiments[key] = {"id": item.get("id"), "name": item.get("name")}
        return experiments

    def create_flag(self, payload: dict) -> int:
        return self._request("POST", "/feature_flags/", json=payload).get("id")

    def update_flag(self, flag_id, payload: dict) -> None:
        self._request("PATCH", f"/feature_flags/{flag_id}/", json=payload)

    def create_experiment(self, payload: dict) -> int:
        return self._request("POST", "/experiments/", json=payload).get("id")


def apply_actions(client: PostHogClient, actions: list, dry_run: bool = True) -> list:
    """Execute the plan. `dry_run` reports without writing. Returns the log lines emitted."""
    flag_ids: dict = {}
    log: list = []
    for action in actions:
        kind = action["action"]
        if kind == "create_flag":
            if dry_run:
                log.append(f"[dry-run] create flag '{action['flag']}' with variants "
                           f"{[v['key'] for v in action['payload']['filters']['multivariate']['variants']]}")
                continue
            flag_ids[action["flag"]] = client.create_flag(action["payload"])
            log.append(f"created flag '{action['flag']}' -> {flag_ids[action['flag']]}")
        elif kind == "update_flag_variants":
            if dry_run:
                log.append(f"[dry-run] update flag '{action['flag']}' (missing arms: "
                           f"{action['missing'] or 'none'}, re-activating if disabled)")
                continue
            client.update_flag(action["flag_id"], action["payload"])
            log.append(f"updated flag '{action['flag']}'")
        elif kind == "set_rollout":
            flag_id = action.get("flag_id") or flag_ids.get(action["flag"])
            if dry_run:
                log.append(f"[dry-run] set '{action['flag']}' treatment rollout to "
                           f"{action['rollout_pct'] * 100:.0f}%")
                continue
            if flag_id is None:
                log.append(f"skipped rollout for '{action['flag']}': flag does not exist yet")
                continue
            client.update_flag(flag_id, action["payload"])
            log.append(f"set '{action['flag']}' treatment rollout to "
                       f"{action['rollout_pct'] * 100:.0f}%")
        elif kind == "create_experiment":
            if dry_run:
                log.append(f"[dry-run] create experiment '{action['experiment']}'")
                continue
            payload = dict(action["payload"])
            if payload.get("feature_flag") is None:
                payload["feature_flag"] = flag_ids.get(action["experiment"])
            log.append(f"created experiment '{action['experiment']}' -> "
                       f"{client.create_experiment(payload)}")
        elif kind == "blocked_experiment":
            log.append(f"skipped experiment '{action['experiment']}': {action['reason']}")
    return log


def _print_specs(rollouts: Optional[dict] = None) -> None:
    for spec in build_specs():
        print(f"\n-- {spec.key} ({spec.assignment}-assigned, owner={spec.owner})")
        print(f"   arms: {variants_for(spec)}")
        print(f"   metrics: {list(spec.metric_events)}")
        print(json.dumps(flag_payload(spec), indent=2))
        print(json.dumps(experiment_payload(spec), indent=2))
    for key, pct in (rollouts or {}).items():
        print(f"\n-- rollout {key} -> {pct * 100:.0f}%")
        print(json.dumps(rollout_payload(EXPERIMENTS[key], pct), indent=2))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision the LEM PostHog experiments (multivariate flags + experiment records).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes to PostHog.")
    mode.add_argument("--dry-run", action="store_true", help="Report changes only (default).")
    parser.add_argument("--print-specs", action="store_true",
                        help="Print the registry and the payloads, then exit.")
    parser.add_argument("--rollout", metavar="KEY=PCT", action="append",
                        help="Set an experiment flag's treatment rollout percentage (0-100).")
    args = parser.parse_args(argv)

    rollouts: dict = {}
    for expression in args.rollout or []:
        try:
            rollouts.update(parse_rollout(expression))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.print_specs:
        _print_specs(rollouts)
        return 0

    api_key = operator_api_key()
    if not api_key:
        print(f"{missing_key_message('operator')} — cannot reach PostHog.", file=sys.stderr)
        return 1
    project_id = os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID)
    app_host = os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST)
    dry_run = args.dry_run or not args.apply

    client = PostHogClient(api_key, project_id, app_host)
    try:
        flags, experiments = client.list_flags(), client.list_experiments()
    except Exception as exc:
        print(f"Failed to read PostHog state: {exc}", file=sys.stderr)
        return 1

    actions = plan_actions(build_specs(), flags, experiments, rollouts)
    for line in apply_actions(client, actions, dry_run=dry_run):
        print(line)
    print(summarize(actions))

    if not dry_run:
        experiments = client.list_experiments()
    for key, found in experiments.items():
        if key in EXPERIMENTS:
            print(f"{key}: {experiment_url(app_host, project_id, found.get('id'))}")

    if dry_run and pending(actions):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
