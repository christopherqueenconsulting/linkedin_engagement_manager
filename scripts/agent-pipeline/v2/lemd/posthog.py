"""Which webhook deliveries may PROMPT a PostHog PR takeover — a trigger, never an authority.

PostHog's self-driving App opens PRs here and then keeps iterating on them, billed per iteration.
`v2/posthog_takeover.py` hands each one to this pipeline (docs/posthog-pr-takeover.md). The daemon
learns about a new PostHog PR from the `pull_request` delivery the App's webhook already sends, and
this module is the cheap filter that decides whether a delivery is worth spawning the action for.

It is deliberately a PRE-filter only. A webhook payload is untrusted and can be stale, so nothing
here authorises anything: the action re-reads the PR from the REST API and applies the full identity
check (login, numeric id, account type, head repository, branch prefix, base) before it touches
GitHub. A false positive here costs one short-lived process that reads a PR and exits; a false
negative costs latency until the reconcile safety net lists open PostHog PRs.

Pure and stdlib-only: no network, no clock, no database.
"""

from __future__ import annotations

import json
from typing import Any

#: The PostHog App's bot account and its branch namespace. Also imported by the action module, so
#: the trigger and the authority can never disagree about what "PostHog" is called.
POSTHOG_BOT_LOGIN = "posthog[bot]"
POSTHOG_BOT_ID = 206114724
POSTHOG_BRANCH_PREFIX = "posthog-self-driving/"
#: `gh pr list --author` spelling of the same App, for the reconcile safety net.
POSTHOG_GH_AUTHOR = "app/posthog"

#: `pull_request` actions that mean "a PostHog PR exists, or exists again, or moved". `synchronize`
#: is included so a PR whose `opened` delivery was lost is still caught at its next push.
TRIGGER_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})

#: The daemon's mode name for the takeover child, and the placeholder `kind` it carries — never an
#: item kind, so `collect()` finds no queue row for it and leaves the queue untouched.
TAKEOVER_MODE = "posthog_takeover"
TAKEOVER_KIND = "posthog"


def _payload(raw: Any) -> dict[str, Any]:
    """Decode a stored (trimmed) payload; anything unreadable is an empty dict."""
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_takeover_trigger(event: str | None, action: str | None, number: Any, payload: Any) -> bool:
    """True when a stored delivery may be a PostHog self-driving PR worth re-reading.

    Args:
        event: The `X-GitHub-Event` name.
        action: The payload's `action`.
        number: The PR number the receiver extracted.
        payload: The receiver's trimmed payload (JSON text or dict).

    Returns:
        True for a `pull_request` delivery of a trigger action whose PR author, sender or head
        branch names PostHog. The receiver stores `author`/`head_ref` from this change on; `sender`
        is what older rows carry, and for `opened`/`synchronize` it is the App itself.
    """
    if event != "pull_request" or action not in TRIGGER_ACTIONS:
        return False
    if not isinstance(number, int) or number <= 0:
        return False
    data = _payload(payload)
    return (
        data.get("author") == POSTHOG_BOT_LOGIN
        or data.get("sender") == POSTHOG_BOT_LOGIN
        or str(data.get("head_ref") or "").startswith(POSTHOG_BRANCH_PREFIX)
    )
