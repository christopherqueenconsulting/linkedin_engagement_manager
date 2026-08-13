#!/usr/bin/env python3
"""Daily issue triage: organize uncategorized GitHub issues into milestones with an impact-first
rubric (issue #748).

The cron finds open issues that are missing structure (milestone, priority:* label, flow label,
topical label), applies deterministic staleness/phase-drop detection, and uses a single lem-medium
LLM call to propose priority and milestone grouping. Writes are applied only in --apply mode; the
default is a dry-run plan.

Deliberately imports nothing from `cqc_lem`: this runs from a host cron clone with no app env, DB
or broker. All GitHub I/O is a thin wrapper around the `gh` CLI and is mocked in unit tests.

CLI:
  --dry-run          Show what would be changed (default).
  --apply            Apply label/milestone changes and write the report.
  --hourly           Run the lightweight hourly pass instead of the daily sweep: only issues with
                     no flow label yet, a second adversarial-review LLM call before any
                     `agent:ready` is trusted, and a bounded fan-out (see docs/AGENT_WORKFLOW_PLAYBOOK.md).
                     Combine with --apply for the systemd timer's real run, or without it for a
                     dry-run inspection pass. `--hourly` never touches milestone/topical labels —
                     that reorg stays daily-only.
  --repo OWNER/NAME  Repository to triage (default: this repo).
  --date DATE        Run date (default: today UTC, YYYY-MM-DD).
  --report-dir PATH  Where dated reports are written (default: docs/triage).
  --max-issues N     Cap issues sent to the LLM in one run (default: 50).
  --email-to ADDR    Optional: email a short summary on apply (uses the same ADMIN_EMAIL fallback
                     as other host crons).
  --lock-dir PATH    Directory holding the shared `triage.lock` (default: locks). Held for the
                     duration of any --apply run, daily OR hourly, so the two can never race the
                     same GitHub snapshot.
  --state-file PATH  Per-issue memoization state for --hourly (default: state/triage_hourly_state.json).
  --queue-db PATH    Override for the daemon's `v2/state/queue.db` (default: $BASE/v2/state/queue.db,
                     BASE defaults to /home/lem/agent-pipeline) — read-only, used only by --hourly.

Env:
  GITHUB_TOKEN is not required: the host cron is already `gh` authenticated.
  LITELLM_MASTER_KEY / LITELLM_BASE_URL for the lem-medium call; fallback to OPENAI_API_KEY at
  base_url http://litellm:4000. If no key is available the LLM step is skipped and the script still
  emits the deterministic report.
  ADMIN_EMAIL, LINKEDIN_EMAIL override the default alert recipient.
  TRUSTED_ASSOCIATIONS       Space-separated author associations eligible for `agent:ready`
                             (default "OWNER MEMBER COLLABORATOR") — the SAME var/default
                             `scripts/agent-pipeline/lib/guards.sh` reads, so this cron can never
                             silently diverge from what the daemon itself trusts.
  TRIAGE_HOURLY_MAX_NEW_READY    Hard per-hour ceiling on new `agent:ready` grants (default 2).
  TRIAGE_HOURLY_TARGET_INFLIGHT  Target queue depth for --hourly's admission cap; falls back to
                                 LEMD_MAX_AGENTS (read live, default 3) when unset — never a frozen
                                 copy of it.
  BASE                       Deployed agent-pipeline root (default /home/lem/agent-pipeline); used
                             only to locate v2/state/queue.db for --hourly's in-flight read.
  POSTHOG_API_KEY / POSTHOG_HOST   Optional: --hourly emits one lifecycle event per run, mirroring
                                   `lib/posthog.sh`'s posthog_capture shape. Best-effort; a missing
                                   key just skips the event.

Exit: 0 in sync / applied, 2 changes pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Iterator, Optional

DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"
DEFAULT_LLM_MODEL = "lem-medium"
DEFAULT_LLM_BASE_URL = "http://litellm:4000"
DEFAULT_REPORT_DIR = "docs/triage"
DEFAULT_MAX_ISSUES = 50
DAYS_STALE = 30

PRIORITY_LABELS = ("priority:critical", "priority:high", "priority:medium", "priority:low")
FLOW_LABELS = ("agent:ready", "agent:working", "needs-human", "agent:blocked")
# Labels the triage script is allowed to add/remove.
MUTABLE_LABELS = (*PRIORITY_LABELS, "agent:ready", "needs-human")
# Labels only the owner should change.
OWNER_LABELS = ("agent:model:sonnet", "agent:model:haiku", "agent:model:opus", "risk:*")
# Author standing that may receive `agent:ready` from this cron. `tick.sh` re-checks both the
# author AND who applied the label, so this is the first of two independent gates, not the only
# one. Read from the SAME env var/default `scripts/agent-pipeline/lib/guards.sh` reads
# (space-separated, exactly as bash word-splits it) instead of a separately hardcoded Python
# tuple — that duplicated-knob shape is what already caused the documented
# MAX_AGENTS/LEMD_MAX_AGENTS silent-drift incident, and this is the same hazard with a trust
# boundary attached instead of a concurrency limit.
TRUSTED_ASSOCIATIONS = tuple(
    os.environ.get("TRUSTED_ASSOCIATIONS", "OWNER MEMBER COLLABORATOR").split()
)

EMAIL_FALLBACK = "christopher.queen@gmail.com"

# ─────────────────────────────── hourly mode ─────────────────────────────────
# Members of the `lem-medium` LiteLLM routing group, mirrored from `.litellm/config.yaml`. Kept in
# sync manually: this script deliberately has no admin API call into LiteLLM to read live group
# membership, and letting this list go stale only weakens the independence guarantee below (the
# reviewer call is still a real, separate LLM call either way) — it never breaks anything.
LEM_MEDIUM_MEMBERS = (
    "openai/gpt-oss:120b",
    "openai/deepseek-v4-flash:preview",
    "openai/gemma4:31b",
    "openai/gpt-4o-mini",
)

DEFAULT_LOCK_DIR = "locks"
DEFAULT_STATE_FILE = "state/triage_hourly_state.json"
DEFAULT_TRIAGE_HOURLY_MAX_NEW_READY = 2
# Mirrors `v2/lemd/config.py`'s own `_int(env, "LEMD_MAX_AGENTS", 3)` fallback.
DEFAULT_LEMD_MAX_AGENTS = 3
DEFAULT_PIPELINE_BASE = "/home/lem/agent-pipeline"

# Impact rank for the fan-out sort — lower sorts first. Anything not in this map (missing/unknown
# priority) sorts after every known priority, never ahead of one.
PRIORITY_RANK = {"priority:critical": 0, "priority:high": 1, "priority:medium": 2, "priority:low": 3}

POSTHOG_EVENT_HOURLY = "triage_hourly_run"

ADVERSARIAL_REVIEW_PROMPT = """You are LEM's adversarial triage reviewer — an INDEPENDENT second \
opinion on a batch of issues a first pass already proposed for "agent:ready" (meaning: an \
autonomous coding agent, holding this repo owner's own credentials, will read the issue body as \
its prompt and act on it unsupervised).

Your ONLY job is to try to REFUTE each proposed "agent:ready". You may downgrade an item to \
"needs-human" when you find a real problem; you may NEVER grant "agent:ready" to anything — the \
planner already decided that, you can only make its decision MORE conservative, never less.

Refute an item when you find:
- miscategorized priority (the impact doesn't match the label),
- ambiguous or underspecified scope an autonomous agent could reasonably misread,
- signs the issue reads like it needs a product/security/migration decision a human should make,
- signs the author trust check will fail anyway (issue text hints at an outside/unverified author),
- anything else that smells like it should not run unsupervised.

If you find nothing wrong with an item, confirm it.

INPUT ISSUES (title + up to 1200 chars of body — the SAME issue text the planner saw, not just its
summary — plus the planner's own proposed priority and reason):
{issues}

Respond with a single JSON object:
{{
  "reviews": [
    {{ "number": 123, "verdict": "confirm" or "veto", "reason": "one-sentence rationale" }}
  ]
}}
Include a review for every issue listed above. Return ONLY the JSON object, no markdown fences.
"""

TRIAGE_PROMPT = """You are LEM's daily issue triage assistant. Your job is to read a batch of
GitHub issues and return a JSON plan that assigns each issue a priority label and a milestone,
following the impact-first rubric below.

IMPACT-FIRST RUBRIC:
- priority:critical — a user hits it today: core loop broken, data loss, security exposure, runaway
  spend, or the product silently doing the WRONG thing (worse than obvious failure).
- priority:high — core loop degraded, growth/revenue blocked, or it unblocks several other issues
  (dependency leverage counts as impact).
- priority:medium — real improvement, contained blast radius.
- priority:low — polish, docs, nits.
Bumps:
- An issue reported by a REAL user via feedback widget outranks an equivalent internally-speculated
  one.
- An issue that unblocks others outranks a leaf issue of the same size.
- NEVER let a pile of small easy issues outrank one high-impact hard one. Order by impact, then
  unblocking power, then effort — never by count or ease.

MILESTONE GROUPING:
- Prefer assigning to an existing milestone whose theme matches the issue.
- Propose a NEW milestone only when at least 3 issues share a theme none of the existing open
  milestones cover.
- A milestone title should state intent (e.g. "Stability & Trust — Fix What's Broken In Front of
  Users"). A milestone is a releasable increment with a one-line exit criterion, not a bucket.
- Target 4-12 issues per milestone.

DECISIONS about flow labels:
- Add "agent:ready" for clear, well-scoped engineering work that the pipeline can pick up.
- Route to "needs-human" when the issue looks like it needs an owner decision (product, security,
  migration, live LinkedIn, ambiguous scope).
- Never change "agent:model:*" or "risk:*".

OPEN MILESTONES (with number and current open issue count):
{milestones}

EXISTING TOPICAL LABELS in the repo (for reference only):
{labels}

INPUT ISSUES (JSON):
{issues}

Respond with a single JSON object:
{{
  "issues": [
    {{
      "number": 123,
      "priority": "priority:high",
      "milestone_title": "exact title of an existing milestone OR a proposed new one",
      "milestone_number": 17,
      "flow": "agent:ready" or "needs-human",
      "topical_labels": ["bug", "observability"],
      "reason": "one-sentence impact rationale"
    }}
  ],
  "proposed_milestones": [
    {{ "title": "New milestone title", "description": "one-line exit criterion" }}
  ]
}}
Rules for the JSON:
- Use the exact existing milestone_title if you assign to an existing milestone; otherwise use a
  new title and put it in proposed_milestones.
- If an issue already has a priority:* label in its current labels, you may keep it but still decide
  its milestone and flow label.
- If an issue already has a flow label (agent:ready, agent:working, needs-human, agent:blocked),
  preserve it unless it is missing entirely; this script only adds "agent:ready" or "needs-human" to
  issues that have no flow label.
- Only include proposed_milestones when you are actually proposing new ones.
- "topical_labels" should only include labels that already exist in the repo or that are clearly
  warranted (bug, feature, observability, ui, etc.). Do not invent arbitrary labels.
- Return ONLY the JSON object, no markdown fences.
"""


# ─────────────────────────────── data model ─────────────────────────────────

@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    milestone: Optional[dict] = None
    created_at: str = ""
    updated_at: str = ""
    author: str = ""
    # OWNER / MEMBER / COLLABORATOR / CONTRIBUTOR / NONE — how GitHub rates the author's standing
    # in this repo. Empty means "unreadable", which is treated as untrusted.
    author_association: str = ""
    is_pull_request: bool = False

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "body": (self.body or "")[:1200],
            "labels": self.labels,
            "milestone": self.milestone.get("title") if self.milestone else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "author": self.author,
        }


@dataclass
class TriageDecision:
    number: int
    priority: Optional[str] = None
    milestone_title: Optional[str] = None
    milestone_number: Optional[int] = None
    flow: Optional[str] = None
    topical_labels: list[str] = field(default_factory=list)
    reason: str = ""
    proposed_milestone: Optional[dict] = None


@dataclass
class IssueGap:
    missing_milestone: bool
    missing_priority: bool
    missing_flow: bool
    missing_topical: bool


@dataclass
class HourlyStats:
    """Counters for one --hourly run's report + PostHog event.

    `planner_proposed_count` and `adversarial_vetoed_count` cover only issues FRESHLY planned/
    reviewed this run (memoized issues reuse a prior verdict without spending another LLM call, by
    design) — these are a token-spend signal, not a backlog census. `candidates_seen` and
    `admitted_count` are cumulative across fresh + memoized, because those are what actually
    happened to the backlog this hour.
    """

    candidates_seen: int = 0
    memoized_skipped: int = 0
    planner_proposed_count: int = 0
    adversarial_vetoed_count: int = 0
    trust_downgraded_count: int = 0
    admitted_count: int = 0
    cap: int = 0
    cap_hit: bool = False
    planner_model: Optional[str] = None
    reviewer_model: Optional[str] = None


# ─────────────────────────── pure logic ─────────────────────────────────────

def parse_issue(raw: dict) -> Issue:
    """Normalize a GitHub issue JSON into the internal Issue shape."""
    return Issue(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state") or "open").lower(),
        labels=[str(l.get("name")) for l in raw.get("labels", []) if isinstance(l, dict)],
        milestone=raw.get("milestone") if isinstance(raw.get("milestone"), dict) else None,
        created_at=str(raw.get("createdAt") or raw.get("created_at") or ""),
        updated_at=str(raw.get("updatedAt") or raw.get("updated_at") or ""),
        author=str((raw.get("author") or {}).get("login") or raw.get("user", {}).get("login") or ""),
        author_association=str(raw.get("authorAssociation")
                               or raw.get("author_association") or "").upper(),
        is_pull_request=bool(raw.get("isPullRequest") or raw.get("pull_request")),
    )


def issue_gap(issue: Issue) -> IssueGap:
    """Which structural pieces an issue is missing."""
    has_priority = any(l.startswith("priority:") for l in issue.labels)
    has_flow = any(l in FLOW_LABELS for l in issue.labels)
    has_topical = any(
        l not in (*PRIORITY_LABELS, *FLOW_LABELS, *OWNER_LABELS) and not l.startswith("risk:")
        for l in issue.labels
    )
    return IssueGap(
        missing_milestone=issue.milestone is None,
        missing_priority=not has_priority,
        missing_flow=not has_flow,
        missing_topical=not has_topical,
    )


def needs_triage(issue: Issue) -> bool:
    """True for open issues missing any structural label or milestone."""
    if issue.state != "open" or issue.is_pull_request:
        return False
    gap = issue_gap(issue)
    return gap.missing_milestone or gap.missing_priority or gap.missing_flow or gap.missing_topical


def is_stale(issue: Issue, now: datetime, days: int = DAYS_STALE) -> bool:
    """An issue open for more than `days` with no recent update."""
    updated = _parse_iso(issue.updated_at) or _parse_iso(issue.created_at)
    if updated is None:
        return False
    return (now - updated).days > days


def is_phase_drop(issue: Issue, milestones: list[dict]) -> bool:
    """Issue belongs to a closed milestone but is still open."""
    if not issue.milestone:
        return False
    m_number = issue.milestone.get("number")
    for m in milestones or []:
        if m.get("number") == m_number:
            return str(m.get("state") or "").lower() == "closed"
    return False


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def select_priority_label(decision: TriageDecision, current_labels: list[str]) -> Optional[str]:
    """Pick the priority label to add, respecting existing owner-set priorities.

    Validated against the fixed `PRIORITY_LABELS` vocabulary before being trusted: `decision.priority`
    is LLM output exactly like `topical_labels` is, so passing it through unvalidated is the same
    label-injection shape `select_topical_labels()` closes — just via a different JSON field. A
    priority has only four legal values, so this is a plain membership check, not an allow-list
    lookup.
    """
    existing = [l for l in current_labels if l.startswith("priority:")]
    if existing:
        return None
    return decision.priority if decision.priority in PRIORITY_LABELS else None


def select_flow_label(decision: TriageDecision, current_labels: list[str],
                      author_association: str = "") -> Optional[str]:
    """Only add a flow label if none exists — and never grant `agent:ready` to an outsider's issue.

    This function is a privilege boundary, not a categoriser. `agent:ready` is what makes an issue
    body the prompt for an autonomous run holding the owner's credentials, and this repo is public,
    so anyone can author the text an LLM is reading here. An untrusted author's issue can still be
    triaged, prioritised and milestoned — it just lands `needs-human`, and a person promotes it.

    Unreadable association is untrusted: the whole point is to fail toward the label that waits.
    """
    existing = [l for l in current_labels if l in FLOW_LABELS]
    if existing:
        return None
    if decision.flow not in ("agent:ready", "needs-human"):
        return None
    if decision.flow == "agent:ready" and author_association.upper() not in TRUSTED_ASSOCIATIONS:
        return "needs-human"
    return decision.flow


def _is_owner_or_risk_label(label: str) -> bool:
    """True for any label this cron may never write, regardless of what the LLM proposed.

    `OWNER_LABELS` lists the exact `agent:model:*` names it knows about today, but that vocabulary
    (and `risk:*`) can grow, and a literal-list check needs updating every time it does — forgetting
    once reopens the exact gap this function exists to close. Prefix-matching both families closes
    it for every present AND future value in one place.
    """
    return label in OWNER_LABELS or label.startswith("agent:model:") or label.startswith("risk:")


def select_topical_labels(decision: TriageDecision, current_labels: list[str],
                          allowed: set[str]) -> list[str]:
    """Add topical labels that are not already present and are in the repo's label vocabulary.

    This is a privilege boundary, not just a vocabulary filter. `decision.topical_labels` is free
    text the LLM produced, partly from up to 1200 untrusted chars of the issue body — so a
    hallucination, or a prompt injection planted in that body, can put `"agent:model:opus"` or
    `"risk:security"` in the list. The prompt instructs the model never to touch those, but a
    prompt instruction is advisory, not enforcement; this filter is the enforcement, and it runs
    even when the owner label is (correctly) present in the repo's real label vocabulary — `allowed`
    membership alone was the gap. `PRIORITY_LABELS`/`FLOW_LABELS` are excluded too: those have
    their own dedicated selectors and must never be double-written through the generic topical path.
    """
    return [l for l in decision.topical_labels
            if l not in current_labels and l in allowed
            and l not in (*PRIORITY_LABELS, *FLOW_LABELS)
            and not _is_owner_or_risk_label(l)]


def select_milestone(decision: TriageDecision, milestones: list[dict]) -> Optional[int]:
    """Resolve a milestone title to its number, or None if it is a proposal."""
    if decision.milestone_number:
        return decision.milestone_number
    title = (decision.milestone_title or "").strip()
    for m in milestones or []:
        if str(m.get("title") or "").strip() == title:
            return int(m.get("number"))
    return None


def needs_hourly_triage(issue: Issue) -> bool:
    """Hourly scope: open, non-PR issues with NO flow label yet.

    Narrower than the daily sweep's `needs_triage` (which also catches missing milestone/priority/
    topical): hourly never re-litigates an issue the daily sweep or a human already gave a flow
    label to, and it never touches milestone/topical assignment — that reorg stays daily-only.
    """
    if issue.state != "open" or issue.is_pull_request:
        return False
    return issue_gap(issue).missing_flow


def issue_fingerprint(issue: Issue) -> str:
    """A cheap change-detector for hourly memoization.

    Either `updated_at` or the label set changing invalidates a memoized verdict: a relabel can
    change the trust/flow inputs even on a GitHub API shape where a label-only edit doesn't bump
    `updated_at`, and a body edit always bumps `updated_at`. Hashed rather than stored raw only to
    keep the state file compact — it is never compared to anything but another hash of the same
    shape.
    """
    label_key = ",".join(sorted(issue.labels))
    raw = f"{issue.updated_at}|{label_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_hourly_state(path: Path) -> dict:
    """Load the per-issue hourly memoization state, tolerating a missing or corrupt file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_hourly_state(path: Path, state: dict) -> None:
    """Persist hourly memoization state, atomically (write-then-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def pick_reviewer_model(planner_served_model: Optional[str]) -> str:
    """Pick a `lem-medium` member for the adversarial reviewer, distinct from the planner's.

    `lem-medium` is a LiteLLM routing GROUP (least-busy load balancing across
    `LEM_MEDIUM_MEMBERS`), so independence between two calls both addressed as `lem-medium` is an
    accident of load balancing, not guaranteed. This makes it explicit: given the model the
    planner call actually got served, return a DIFFERENT member for the reviewer to pin.
    """
    for candidate in LEM_MEDIUM_MEMBERS:
        if not planner_served_model or candidate not in planner_served_model:
            return candidate
    return LEM_MEDIUM_MEMBERS[-1]


def build_adversarial_review_prompt(candidates: list[tuple[Issue, TriageDecision]]) -> str:
    """Render the adversarial-review prompt for the issues the planner proposed `agent:ready` for.

    Feeds the reviewer the SAME issue text the planner saw (title + up to 1200 chars of body), not
    just the planner's summary/reason — reviewing only the summary could only ever catch what that
    summary chose to expose.
    """
    items = [
        {
            "number": issue.number,
            "title": issue.title,
            "body": (issue.body or "")[:1200],
            "planner_priority": decision.priority,
            "planner_reason": decision.reason,
        }
        for issue, decision in candidates
    ]
    return ADVERSARIAL_REVIEW_PROMPT.format(issues=json.dumps(items, indent=2))


def parse_adversarial_review(raw: Optional[str],
                             candidates: list[tuple[Issue, TriageDecision]]) -> dict[int, str]:
    """Parse the reviewer's verdicts, failing CLOSED to "veto" on anything unreadable.

    An issue absent from the response, an unparseable response, or no response at all (no LLM key)
    all become "veto" — the same "unreadable is untrusted" rule this script already applies to
    author standing. A second opinion that could not be obtained is not a second opinion that was
    satisfied.
    """
    verdicts = {issue.number: "veto" for issue, _ in candidates}
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    if not text:
        return verdicts
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return verdicts
    valid_numbers = {issue.number for issue, _ in candidates}
    for item in data.get("reviews", []) if isinstance(data, dict) else []:
        try:
            number = int(item.get("number") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if number not in valid_numbers:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        verdicts[number] = "confirm" if verdict == "confirm" else "veto"
    return verdicts


def compute_admission_cap(max_new_ready: int, target_inflight: int,
                          current_inflight: Optional[int]) -> int:
    """N = max(0, min(max_new_ready, target_inflight - current_inflight)).

    Never admit more than the configured per-hour ceiling, AND never admit more than needed to top
    the in-flight queue up to its target size — so a quiet daemon doesn't get flooded the moment a
    backlog exists. An unreadable `current_inflight` (queue.db missing/corrupt) fails CLOSED to
    zero admissions rather than guessing zero-in-flight, the same "unreadable is untrusted" rule
    this script applies everywhere else.
    """
    if current_inflight is None:
        return 0
    return max(0, min(max_new_ready, target_inflight - current_inflight))


def rank_eligible_for_admission(
    eligible: list[tuple[Issue, TriageDecision]], cap: int
) -> tuple[list[tuple[Issue, TriageDecision]], list[tuple[Issue, TriageDecision]], bool]:
    """Stable-sort `agent:ready`-eligible candidates (priority, then age) and take the first `cap`.

    Sorted only AFTER the trust-downgrade-before-cap ordering has already removed anything that
    would not actually ship as `agent:ready` — otherwise an untrusted author's issue could consume
    one of the N admission slots and then get silently downgraded at apply time, wasting that
    hour's budget on an issue that was never going to ship as `agent:ready`.

    Returns:
        (admitted, pending, cap_hit). `pending` is left unlabeled by the caller — eligible but not
        admitted this hour, reconsidered next hourly pass (or the daily sweep), never silently
        dropped and never downgraded to `needs-human`. `cap_hit` is True when more issues were
        eligible than fit under `cap`, so the caller can tell "quiet backlog, everyone got in" from
        "flood control actually bit."
    """
    def _key(pair: tuple[Issue, TriageDecision]) -> tuple[int, datetime]:
        issue, decision = pair
        rank = PRIORITY_RANK.get(decision.priority or "", len(PRIORITY_RANK))
        age = (_parse_iso(issue.created_at) or _parse_iso(issue.updated_at)
               or datetime.max.replace(tzinfo=timezone.utc))
        return (rank, age)

    ordered = sorted(eligible, key=_key)
    admitted = ordered[:cap] if cap > 0 else []
    pending = ordered[len(admitted):]
    return admitted, pending, len(pending) > 0


def parse_llm_plan(raw: str, issues: list[Issue],
                     milestones: list[dict]) -> tuple[list[TriageDecision], list[dict]]:
    """Validate and coerce the LLM's JSON plan into decisions. Drop decisions for unknown issues."""
    text = (raw or "").strip()
    # Strip markdown fences if the model wrapped JSON in ```json ... ```.
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    if not text:
        return [], []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid JSON: {exc}") from exc

    decisions = []
    valid_numbers = {i.number for i in issues}
    for item in data.get("issues", []):
        number = int(item.get("number") or 0)
        if number not in valid_numbers:
            continue
        title = str(item.get("milestone_title") or "").strip()
        m_number = None
        for m in milestones:
            if str(m.get("title") or "").strip() == title:
                m_number = int(m.get("number"))
                break
        decisions.append(TriageDecision(
            number=number,
            priority=str(item.get("priority") or "").strip() or None,
            milestone_title=title or None,
            milestone_number=m_number,
            flow=str(item.get("flow") or "").strip() or None,
            topical_labels=[str(l) for l in item.get("topical_labels", []) if l],
            reason=str(item.get("reason") or "").strip(),
        ))
    return decisions, data.get("proposed_milestones", []) or []


def build_prompt(issues: list[Issue], milestones: list[dict], all_labels: list[str]) -> str:
    """Render the triage prompt with the current issue batch and repo metadata."""
    milestone_lines = []
    for m in milestones:
        title = m.get("title") or "Untitled"
        number = m.get("number") or "?"
        count = m.get("open_issues", "?")
        milestone_lines.append(f"- #{number}: {title} ({count} open)")
    labels = sorted({l for l in all_labels
                     if l not in (*PRIORITY_LABELS, *FLOW_LABELS) and not l.startswith("risk:")
                     and not l.startswith("agent:model:")})
    return TRIAGE_PROMPT.format(
        milestones="\n".join(milestone_lines) or "(none)",
        labels=", ".join(labels) or "(none)",
        issues=json.dumps([i.to_dict() for i in issues], indent=2),
    )


def build_report(run_date: str, triaged: list[Issue], decisions: list[TriageDecision],
                 proposed: list[dict], stale: list[Issue], phase_drops: list[Issue],
                 applied: bool) -> str:
    """Render the dated markdown triage report."""
    lines = [
        f"# Triage report — {run_date}",
        "",
        f"Mode: **{'applied' if applied else 'dry-run'}** · Issues reviewed: {len(triaged)} · "
        f"Stale (>30d): {len(stale)} · Phase-drop (closed milestone, open issue): {len(phase_drops)}",
        "",
        "## Proposed changes",
        "",
    ]
    if not decisions:
        lines.append("_No triage decisions produced._")
    else:
        for d in decisions:
            issue = next((i for i in triaged if i.number == d.number), None)
            title = issue.title if issue else f"#{d.number}"
            lines.append(f"### #{d.number}: {title}")
            if d.priority:
                lines.append(f"- **Priority:** {d.priority}")
            if d.milestone_title:
                lines.append(f"- **Milestone:** {d.milestone_title} "
                             f"({f'#{d.milestone_number}' if d.milestone_number else 'proposed'})")
            if d.flow:
                lines.append(f"- **Flow:** {d.flow}")
            if d.topical_labels:
                lines.append(f"- **Topical labels:** {', '.join(d.topical_labels)}")
            if d.reason:
                lines.append(f"- **Reason:** {d.reason}")
            lines.append("")
    if proposed:
        lines += ["## Proposed new milestones", ""]
        for p in proposed:
            lines.append(f"- **{p.get('title')}** — {p.get('description')}")
        lines.append("")
    if stale:
        lines += ["## Stale issues (>30 days with no activity)", ""]
        for s in stale:
            lines.append(f"- #{s.number}: {s.title}")
        lines.append("")
    if phase_drops:
        lines += ["## Phase-drop issues (open in a closed milestone)", ""]
        for p in phase_drops:
            lines.append(f"- #{p.number}: {p.title} (milestone: {p.milestone.get('title')})")
        lines.append("")
    lines += [
        "## Notes",
        "- Deterministic checks (missing labels, staleness, phase-drop) run without an LLM.",
        "- Impact/priority and milestone grouping are LLM-assisted via a single `lem-medium` call.",
        "- Re-running the same day is idempotent: the report file is overwritten with the same date.",
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ]
    return "\n".join(lines)


def build_email_summary(run_date: str, n_triaged: int, n_applied: int, n_stale: int,
                        n_phase_drop: int, n_proposed: int, report_path: str) -> tuple[str, str]:
    """(subject, text body) for the alert email."""
    subject = f"LEM daily triage — {run_date}: {n_applied} changes, {n_stale} stale, {n_phase_drop} phase-drops"
    body = (
        f"Daily triage ({run_date})\n"
        f"- Issues reviewed: {n_triaged}\n"
        f"- Changes applied: {n_applied}\n"
        f"- Stale issues (>30d): {n_stale}\n"
        f"- Phase-drop issues: {n_phase_drop}\n"
        f"- Proposed new milestones: {n_proposed}\n"
        f"Report: {report_path}\n"
    )
    return subject, body


def build_hourly_report(run_date: str, run_hour: str, candidates: list[Issue],
                        admitted: list[tuple[Issue, TriageDecision]],
                        immediate: list[tuple[Issue, TriageDecision]],
                        pending: list[tuple[Issue, TriageDecision]],
                        stats: HourlyStats, applied: bool) -> str:
    """Render the dated markdown report for one --hourly run."""
    lines = [
        f"# Hourly triage report — {run_date} {run_hour}",
        "",
        f"Mode: **{'applied' if applied else 'dry-run'}** · Candidates seen: {stats.candidates_seen} "
        f"· Memoized (skipped LLM): {stats.memoized_skipped} · Planner-proposed: "
        f"{stats.planner_proposed_count} · Adversarial-vetoed: {stats.adversarial_vetoed_count} "
        f"· Trust-downgraded: {stats.trust_downgraded_count} · Admitted: {stats.admitted_count} "
        f"· Cap: {stats.cap} · Cap hit: {stats.cap_hit}",
        f"Planner model: `{stats.planner_model or 'unknown'}` · "
        f"Reviewer model: `{stats.reviewer_model or 'unknown'}`",
        "",
        "## Admitted this hour (agent:ready)",
        "",
    ]
    if not admitted:
        lines.append("_None admitted this hour._")
    else:
        for issue, decision in admitted:
            lines.append(f"- #{issue.number}: {issue.title} "
                         f"({decision.priority or 'no priority'}) — {decision.reason}")
    lines += ["", "## Downgraded to needs-human this hour", ""]
    if not immediate:
        lines.append("_None._")
    else:
        for issue, decision in immediate:
            lines.append(f"- #{issue.number}: {issue.title} — {decision.reason}")
    lines += ["", "## Eligible but held for a later hour (cap reached)", ""]
    if not pending:
        lines.append("_None held back._")
    else:
        for issue, decision in pending:
            lines.append(f"- #{issue.number}: {issue.title} ({decision.priority or 'no priority'})")
    lines += [
        "",
        "## Notes",
        "- Scope: open issues with no flow label yet — milestone/priority reorg stays daily-only.",
        "- The planner call proposes priority + flow; a second, independently-pinned `lem-medium` "
        "call may only downgrade a proposed `agent:ready` to `needs-human`, never upgrade.",
        "- Untrusted-author downgrade is applied BEFORE ranking/capping, so it never wastes an "
        "admission slot.",
        "- Held-back issues are left unlabeled, not `needs-human` — reconsidered next hourly pass "
        "(or the daily sweep) without repaying the LLM cost, via per-issue memoization.",
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ]
    return "\n".join(lines)


# ─────────────────────────────── I/O layer ──────────────────────────────────

class GitHubClient:
    """Thin `gh` CLI wrapper. All methods are side-effect free except explicit mutators."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        # `gh api` does not accept --repo; rely on GH_REPO env or the already-active repo context.
        env = dict(os.environ)
        env["GH_REPO"] = self.repo
        cmd = ["gh"] + args
        return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=120, env=env)

    def list_open_issues(self, limit: int = 200) -> list[Issue]:
        """Fetch open issues (not PRs) with labels and milestone. Use `gh api` directly so we get
        every open issue, not the first page of the default view."""
        # This is the REST endpoint, so the REST spellings are the ones that actually arrive —
        # `user` / `author_association` / `pull_request`. The camelCase names are kept because
        # `parse_issue` also accepts GraphQL-shaped fixtures.
        fields = ("number,title,body,state,labels,milestone,createdAt,updatedAt,author,"
                  "isPullRequest,user,author_association,pull_request")
        result = self._run(
            ["api", f"repos/{self.repo}/issues", "--method", "GET", "--field", "state=open",
             "--field", "per_page=100", "--paginate", "--jq", f".[] | {{{fields}}}"],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
        # --paginate with --jq returns newline-delimited JSON objects.
        data = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line:
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        issues = [parse_issue(d) for d in data if not d.get("isPullRequest")]
        return [i for i in issues if i.state == "open"][:limit]

    def list_milestones(self) -> list[dict]:
        result = self._run(
            ["api", f"repos/{self.repo}/milestones", "--method", "GET", "--field", "state=all",
             "--field", "per_page=100"],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"milestones fetch failed: {(result.stderr or '').strip()[:200]}")
        return json.loads(result.stdout or "[]")

    def list_labels(self) -> list[str]:
        result = self._run(["label", "list", "--limit", "500"], check=False)
        if result.returncode != 0:
            raise RuntimeError(f"labels fetch failed: {(result.stderr or '').strip()[:200]}")
        rows = (result.stdout or "").strip().splitlines()
        # `gh label list` table header is the first two lines; names are the first column.
        names = []
        for row in rows[2:]:
            name = row.split("\t")[0].strip()
            if name:
                names.append(name)
        return names

    def add_labels(self, issue_number: int, labels: list[str]) -> bool:
        if not labels:
            return True
        result = self._run(
            ["issue", "edit", str(issue_number), "--add-label", ",".join(labels)],
            check=False,
        )
        return result.returncode == 0

    def set_milestone(self, issue_number: int, milestone_number: int) -> bool:
        result = self._run(
            ["issue", "edit", str(issue_number), "--milestone", str(milestone_number)],
            check=False,
        )
        return result.returncode == 0


class LLMClient:
    """Minimal LiteLLM/OpenAI-compatible chat client."""

    def __init__(self, api_key: Optional[str], base_url: str, model: str = DEFAULT_LLM_MODEL) -> None:
        self.api_key = api_key or ""
        self.base_url = base_url
        self.model = model
        # The model LiteLLM actually served for the last `classify()` call — a `lem-*` alias is a
        # routing GROUP, so this is the only way to know which member answered. None until a call
        # succeeds; `--hourly` uses it to pin the adversarial reviewer to a DIFFERENT member.
        self.last_model: Optional[str] = None

    def classify(self, prompt: str) -> Optional[str]:
        if not self.api_key:
            return None
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=4000,
            )
            self.last_model = getattr(response, "model", None)
            return response.choices[0].message.content
        except Exception as exc:
            print(f"LLM call failed: {exc}", file=sys.stderr)
            return None


# ─────────────────────────────── orchestration ────────────────────────────────

def gather(repo: str) -> tuple[list[Issue], list[dict], list[str]]:
    """Collect open issues, all milestones, and all labels."""
    gh = GitHubClient(repo)
    issues = gh.list_open_issues()
    milestones = gh.list_milestones()
    labels = gh.list_labels()
    return issues, milestones, labels


def deterministic_flags(issues: list[Issue], milestones: list[dict],
                        now: datetime) -> tuple[list[Issue], list[Issue], list[Issue]]:
    """Return (triaged_issues, stale_issues, phase_drop_issues)."""
    triaged = [i for i in issues if needs_triage(i)]
    stale = [i for i in issues if is_stale(i, now)]
    phase_drops = [i for i in issues if is_phase_drop(i, milestones)]
    return triaged, stale, phase_drops


def run_triage(triaged: list[Issue], milestones: list[dict], all_labels: list[str],
               llm: LLMClient) -> tuple[list[TriageDecision], list[dict], Optional[str]]:
    """Run the LLM prompt and parse the plan."""
    if not triaged:
        return [], [], None
    prompt = build_prompt(triaged, milestones, all_labels)
    raw = llm.classify(prompt)
    if raw is None:
        return [], [], None
    decisions, proposed = parse_llm_plan(raw, triaged, milestones)
    return decisions, proposed, raw


class TriageLockBusy(RuntimeError):
    """Raised when another triage run (daily OR hourly) already holds `locks/triage.lock`."""


@contextmanager
def acquire_triage_lock(lock_dir: Path) -> Iterator[None]:
    """Hold the ONE shared `triage.lock`, non-blocking — same `flock` pattern `claim_branch` uses.

    Shared by BOTH `--apply` (daily) and `--apply --hourly`: they hit the identical
    missing-flow-label issue set through the identical `select_flow_label`/`apply_changes` path, so
    a lock scoped to only one mode would still let a manually-triggered daily run race an
    in-progress hourly tick — both computing admission math off GitHub snapshots taken before the
    other's write lands.

    Raises:
        TriageLockBusy: another run already holds the lock.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "triage.lock"
    fh = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise TriageLockBusy(f"another triage run already holds {lock_path}") from exc
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _load_queue_read_module() -> Optional[ModuleType]:
    """Import `scripts/agent-pipeline/v2/lemd/queue_read.py` if this checkout has it.

    Added to `sys.path` at CALL time, not import time, so a stripped checkout without the pipeline
    tree degrades to "queue.db unreadable" (the caller then fails closed to zero admissions)
    instead of an ImportError the first time this module loads.
    """
    v2_dir = Path(__file__).resolve().parent / "agent-pipeline" / "v2"
    if not (v2_dir / "lemd" / "queue_read.py").exists():
        return None
    if str(v2_dir) not in sys.path:
        sys.path.insert(0, str(v2_dir))
    try:
        from lemd import queue_read
        return queue_read
    except ImportError:
        return None


def current_inflight_count(queue_db_path: Path) -> Optional[int]:
    """The daemon's own in-flight PR count, read read-only from `v2/state/queue.db`.

    Delegates to the daemon's own `db.wip_count()` via `queue_read.py` so this script — which
    deliberately has no package context of its own (it runs as a bare `python3` invocation) — reads
    the SAME definition of "in flight" the daemon's concurrency gate uses. Never reimplemented
    against issue-label counting: `GitHubClient` has no PR-listing method, and even a naive issue
    label count would measure the wrong signal (an `agent:ready` ISSUE is a queue entry, not work
    in flight — `wip_count()`'s own docstring).
    """
    module = _load_queue_read_module()
    if module is None:
        return None
    try:
        return module.read_inflight_count(queue_db_path)
    except Exception:
        return None


def emit_hourly_posthog_event(stats: HourlyStats, repo: str) -> None:
    """Best-effort PostHog capture for one --hourly run.

    Mirrors `lib/posthog.sh`'s `posthog_capture` shape (same env vars, same `/capture` endpoint,
    same "never break the caller" contract) so this event lands in the same PostHog project as
    every other agent-pipeline event. `cap_hit` is sent as a string, not a bare bool — PostHog
    matches an alert filter on the ingested type, so a boolean property silently stops a
    string-typed filter from firing (the same trap `utilities/observability.py` documents app-side).
    """
    api_key = os.getenv("POSTHOG_API_KEY", "")
    if not api_key:
        return
    host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
    properties = {
        "lem_component": "agent-pipeline",
        "environment": os.getenv("ENVIRONMENT", "production"),
        "repo": repo,
        "candidates_seen": stats.candidates_seen,
        "memoized_skipped": stats.memoized_skipped,
        "planner_proposed_count": stats.planner_proposed_count,
        "adversarial_vetoed_count": stats.adversarial_vetoed_count,
        "trust_downgraded_count": stats.trust_downgraded_count,
        "admitted_count": stats.admitted_count,
        "cap": stats.cap,
        "cap_hit": "true" if stats.cap_hit else "false",
        "planner_model": stats.planner_model or "unknown",
        "reviewer_model": stats.reviewer_model or "unknown",
    }
    body = json.dumps({
        "api_key": api_key,
        "event": POSTHOG_EVENT_HOURLY,
        "distinct_id": "agent-pipeline",
        "properties": properties,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{host}/capture", data=body, headers={"content-type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=3).close()  # noqa: S310 (fixed https host, best-effort)
    except (urllib.error.URLError, OSError, ValueError):
        pass


def hourly_max_new_ready() -> int:
    """`TRIAGE_HOURLY_MAX_NEW_READY` — the hard per-hour ceiling on new `agent:ready` grants."""
    try:
        return int(os.getenv("TRIAGE_HOURLY_MAX_NEW_READY", str(DEFAULT_TRIAGE_HOURLY_MAX_NEW_READY)))
    except ValueError:
        return DEFAULT_TRIAGE_HOURLY_MAX_NEW_READY


def hourly_target_inflight() -> int:
    """`TRIAGE_HOURLY_TARGET_INFLIGHT`, falling back to `LEMD_MAX_AGENTS` read LIVE.

    Never a frozen copy of `LEMD_MAX_AGENTS` — the same duplicated-knob shape already caused the
    documented MAX_AGENTS/LEMD_MAX_AGENTS silent-drift incident, and a stale copy here would
    silently strand the hourly cap below what the daemon can actually run the moment someone bumps
    `LEMD_MAX_AGENTS` without also touching this script.
    """
    raw = os.getenv("TRIAGE_HOURLY_TARGET_INFLIGHT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        return int(os.getenv("LEMD_MAX_AGENTS", str(DEFAULT_LEMD_MAX_AGENTS)))
    except ValueError:
        return DEFAULT_LEMD_MAX_AGENTS


def default_queue_db_path() -> Path:
    """`$BASE/v2/state/queue.db` — `BASE` defaults to the deployed pipeline root."""
    base = os.getenv("BASE", DEFAULT_PIPELINE_BASE)
    return Path(base) / "v2" / "state" / "queue.db"


def plan_changes(issues: list[Issue], decisions: list[TriageDecision],
                 milestones: list[dict], all_labels: list[str]) -> list[dict]:
    """Build the concrete GitHub edits each decision implies."""
    allowed_labels = set(all_labels)
    changes = []
    for d in decisions:
        issue = next((i for i in issues if i.number == d.number), None)
        if issue is None:
            continue
        change: dict = {"number": d.number, "title": issue.title, "add_labels": [],
                         "milestone_number": None, "milestone_title": None, "reason": d.reason}
        priority_label = select_priority_label(d, issue.labels)
        flow_label = select_flow_label(d, issue.labels, issue.author_association)
        topical = select_topical_labels(d, issue.labels, allowed_labels)
        labels_to_add = [l for l in [priority_label, flow_label] if l] + topical
        change["add_labels"] = labels_to_add
        change["milestone_number"] = select_milestone(d, milestones)
        change["milestone_title"] = d.milestone_title
        changes.append(change)
    return changes


def apply_changes(gh: GitHubClient, changes: list[dict], dry_run: bool) -> int:
    """Apply label/milestone edits. Returns count of successful edits."""
    applied = 0
    for c in changes:
        if dry_run:
            print(f"  #{c['number']}: would add {c['add_labels']}; "
                  f"milestone={c['milestone_title'] or 'none'}")
            applied += 1
            continue
        ok = True
        if c["add_labels"]:
            if not gh.add_labels(c["number"], c["add_labels"]):
                ok = False
                print(f"  ! #{c['number']}: label edit failed", file=sys.stderr)
        if c["milestone_number"]:
            if not gh.set_milestone(c["number"], c["milestone_number"]):
                ok = False
                print(f"  ! #{c['number']}: milestone edit failed", file=sys.stderr)
        if ok:
            applied += 1
            print(f"  #{c['number']}: updated")
    return applied


def write_report(report_dir: Path, run_date: str, content: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"
    path.write_text(content, encoding="utf-8")
    return path


def send_email(subject: str, body: str) -> bool:
    """Best-effort email using the app container's dispatch, if available."""
    to = os.getenv("ADMIN_EMAIL") or os.getenv("LINKEDIN_EMAIL") or EMAIL_FALLBACK
    try:
        # If this is running inside or beside the app container with the module on PYTHONPATH:
        from cqc_lem.utilities.email import _dispatch_email
        html = "<pre>" + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
        _dispatch_email(to, subject, html, text_content=body, high_priority=False)
        return True
    except Exception:
        pass
    return False


def main_hourly(args: argparse.Namespace) -> int:
    """Run `--hourly`: planner pass → adversarial review → trust-downgrade → bounded fan-out.

    Scope is narrower than the daily sweep (open issues with no flow label yet only) and the
    change set is narrower too (priority + flow only — milestone/topical reorg stays daily-only).
    """
    dry_run = not args.apply
    now = datetime.now(timezone.utc)
    run_date = now.strftime("%Y-%m-%d")
    run_hour = now.strftime("%H%M")

    try:
        issues, milestones, all_labels = gather(args.repo)
    except Exception as exc:
        print(f"GitHub fetch failed: {exc}", file=sys.stderr)
        return 1

    candidates = [i for i in issues if needs_hourly_triage(i)]
    state_path = Path(args.state_file)
    state = load_hourly_state(state_path)

    to_plan = [i for i in candidates
              if state.get(str(i.number), {}).get("fingerprint") != issue_fingerprint(i)]
    memoized = [i for i in candidates if i not in to_plan]
    # Cap the LLM batch, the same knob the daily sweep uses.
    to_plan = to_plan[: args.max_issues]

    api_key = os.getenv("LITELLM_MASTER_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("LITELLM_BASE_URL", DEFAULT_LLM_BASE_URL)
    planner_llm = LLMClient(api_key=api_key, base_url=base_url, model=DEFAULT_LLM_MODEL)

    try:
        decisions, _proposed, _raw = run_triage(to_plan, milestones, all_labels, planner_llm)
    except ValueError as exc:
        print(f"Triage plan invalid: {exc}", file=sys.stderr)
        return 1

    stats = HourlyStats(candidates_seen=len(candidates), memoized_skipped=len(memoized))
    stats.planner_model = planner_llm.last_model
    stats.planner_proposed_count = sum(1 for d in decisions if d.flow == "agent:ready")

    issue_by_number = {i.number: i for i in to_plan}
    review_candidates = [(issue_by_number[d.number], d) for d in decisions
                         if d.flow == "agent:ready" and d.number in issue_by_number]

    reviewer_model = pick_reviewer_model(planner_llm.last_model)
    reviewer_llm = LLMClient(api_key=api_key, base_url=base_url, model=reviewer_model)
    if review_candidates:
        raw_review = reviewer_llm.classify(build_adversarial_review_prompt(review_candidates))
        verdicts = parse_adversarial_review(raw_review, review_candidates)
        for issue, d in review_candidates:
            if verdicts.get(issue.number) != "confirm":
                d.flow = "needs-human"
                stats.adversarial_vetoed_count += 1
    stats.reviewer_model = reviewer_llm.last_model

    # Persist this hour's fresh verdicts (post-review, pre-trust-check) so an unchanged issue is
    # not re-planned/re-reviewed next hour. Trust is re-checked live below, every hour, because it
    # depends on facts (author standing) this memoization deliberately does not cache.
    for i in to_plan:
        d = next((d for d in decisions if d.number == i.number), None)
        if d is None:
            continue  # dropped by parse_llm_plan or no LLM key — retried next hour, unmemoized.
        state[str(i.number)] = {
            "fingerprint": issue_fingerprint(i),
            "flow": d.flow,
            "priority": d.priority,
            "reason": d.reason,
        }
    try:
        save_hourly_state(state_path, state)
    except OSError as exc:
        print(f"Could not save hourly state: {exc}", file=sys.stderr)

    # Combine this hour's fresh decisions with memoized ones for trust-downgrade + cap.
    combined: list[tuple[Issue, TriageDecision]] = []
    for i in to_plan:
        d = next((d for d in decisions if d.number == i.number), None)
        if d is not None:
            combined.append((i, d))
    for i in memoized:
        rec = state.get(str(i.number))
        if rec:
            combined.append((i, TriageDecision(number=i.number, priority=rec.get("priority"),
                                                flow=rec.get("flow"), reason=rec.get("reason") or "")))

    # Trust-downgrade BEFORE ranking/capping: an untrusted author's issue must never consume one
    # of the N admission slots and then get silently downgraded at apply time.
    immediate: list[tuple[Issue, TriageDecision]] = []
    eligible: list[tuple[Issue, TriageDecision]] = []
    for issue, d in combined:
        flow = select_flow_label(d, issue.labels, issue.author_association)
        if flow is None:
            continue  # already has a flow label — nothing to do (race with another writer).
        if flow == "agent:ready":
            eligible.append((issue, d))
        else:
            if d.flow == "agent:ready":
                stats.trust_downgraded_count += 1
            immediate.append((issue, TriageDecision(number=issue.number, priority=d.priority,
                                                     flow=flow, reason=d.reason)))

    max_new_ready = hourly_max_new_ready()
    target_inflight = hourly_target_inflight()
    queue_db = Path(args.queue_db) if args.queue_db else default_queue_db_path()
    inflight = current_inflight_count(queue_db)
    cap = compute_admission_cap(max_new_ready, target_inflight, inflight)
    admitted, pending, cap_hit = rank_eligible_for_admission(eligible, cap)

    stats.admitted_count = len(admitted)
    stats.cap = cap
    stats.cap_hit = cap_hit

    def _hourly_change(issue: Issue, decision: TriageDecision, flow_label: str) -> dict:
        priority_label = select_priority_label(decision, issue.labels)
        return {"number": issue.number, "title": issue.title,
                "add_labels": [l for l in [priority_label, flow_label] if l],
                "milestone_number": None, "milestone_title": None, "reason": decision.reason}

    changes = [_hourly_change(issue, d, "needs-human") for issue, d in immediate]
    changes += [_hourly_change(issue, d, "agent:ready") for issue, d in admitted]

    if dry_run:
        print(f"Hourly triage plan for {args.repo}: {len(candidates)} candidates "
              f"({len(memoized)} memoized), cap={cap}, admitted={len(admitted)}, "
              f"vetoed={stats.adversarial_vetoed_count}, cap_hit={cap_hit}.")
        gh = GitHubClient(args.repo)
        apply_changes(gh, changes, dry_run=True)
    else:
        try:
            with acquire_triage_lock(Path(args.lock_dir)):
                gh = GitHubClient(args.repo)
                apply_changes(gh, changes, dry_run=False)
        except TriageLockBusy as exc:
            print(f"Triage lock busy: {exc}", file=sys.stderr)
            return 1

    report = build_hourly_report(run_date, run_hour, candidates, admitted, immediate, pending,
                                 stats, applied=not dry_run)
    try:
        write_report(Path(args.report_dir), f"{run_date}-hourly-{run_hour}", report)
    except OSError as exc:
        print(f"Could not write hourly report: {exc}", file=sys.stderr)

    emit_hourly_posthog_event(stats, args.repo)

    pending_changes = len(changes)
    return 2 if (dry_run and pending_changes > 0) else 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Daily issue triage for LEM.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show the plan without applying it (default).")
    mode.add_argument("--apply", action="store_true", help="Apply label/milestone changes and write the report.")
    parser.add_argument("--hourly", action="store_true",
                        help="Run the lightweight hourly pass instead of the daily sweep.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo to triage.")
    parser.add_argument("--date", default=str(date.today()), help="Run date (YYYY-MM-DD).")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, help="Directory for dated markdown reports.")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES,
                        help="Max issues sent to the LLM in one run.")
    parser.add_argument("--email-to", help="Override the alert email recipient.")
    parser.add_argument("--lock-dir", default=DEFAULT_LOCK_DIR,
                        help="Directory holding the shared triage.lock (daily and hourly both use it).")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help="Per-issue memoization state file for --hourly.")
    parser.add_argument("--queue-db", default=None,
                        help="Override for the daemon's v2/state/queue.db (--hourly only).")
    args = parser.parse_args(argv)

    if args.hourly:
        return main_hourly(args)

    dry_run = not args.apply
    run_date = str(args.date)
    report_dir = Path(args.report_dir)

    # Optional email recipient override.
    if args.email_to:
        os.environ["ADMIN_EMAIL"] = args.email_to

    try:
        issues, milestones, all_labels = gather(args.repo)
    except Exception as exc:
        print(f"GitHub fetch failed: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    triaged, stale, phase_drops = deterministic_flags(issues, milestones, now)

    # Cap the LLM batch; deterministic checks still cover everything.
    llm_batch = triaged[:args.max_issues]

    api_key = os.getenv("LITELLM_MASTER_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("LITELLM_BASE_URL", DEFAULT_LLM_BASE_URL)
    llm = LLMClient(api_key=api_key, base_url=base_url)

    try:
        decisions, proposed, _raw = run_triage(llm_batch, milestones, all_labels, llm)
    except ValueError as exc:
        print(f"Triage plan invalid: {exc}", file=sys.stderr)
        return 1

    changes = plan_changes(llm_batch, decisions, milestones, all_labels)

    if dry_run:
        print(f"Triage plan for {args.repo}: {len(triaged)} issues need structure "
              f"({len(llm_batch)} reviewed by LLM), {len(stale)} stale, {len(phase_drops)} phase-drops.")
        if proposed:
            print(f"Proposed milestones: {', '.join(p.get('title') for p in proposed)}")

    if dry_run:
        gh = GitHubClient(args.repo)
        applied_count = apply_changes(gh, changes, dry_run=True)
    else:
        try:
            with acquire_triage_lock(Path(args.lock_dir)):
                gh = GitHubClient(args.repo)
                applied_count = apply_changes(gh, changes, dry_run=False)
        except TriageLockBusy as exc:
            print(f"Triage lock busy: {exc}", file=sys.stderr)
            return 1

    report = build_report(
        run_date=run_date,
        triaged=llm_batch,
        decisions=decisions,
        proposed=proposed,
        stale=stale,
        phase_drops=phase_drops,
        applied=not dry_run,
    )

    try:
        report_path = write_report(report_dir, run_date, report)
    except OSError as exc:
        print(f"Could not write report: {exc}", file=sys.stderr)
        return 1

    if not dry_run:
        subject, body = build_email_summary(
            run_date=run_date,
            n_triaged=len(triaged),
            n_applied=applied_count,
            n_stale=len(stale),
            n_phase_drop=len(phase_drops),
            n_proposed=len(proposed),
            report_path=str(report_path),
        )
        if not send_email(subject, body):
            print("Email summary skipped (no dispatcher available).")

    pending_changes = sum(1 for c in changes if c["add_labels"] or c["milestone_number"])
    return 2 if (dry_run and pending_changes > 0) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
