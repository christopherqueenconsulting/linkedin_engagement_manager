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
  --repo OWNER/NAME  Repository to triage (default: this repo).
  --date DATE        Run date (default: today UTC, YYYY-MM-DD).
  --report-dir PATH  Where dated reports are written (default: docs/triage).
  --max-issues N     Cap issues sent to the LLM in one run (default: 50).
  --email-to ADDR    Optional: email a short summary on apply (uses the same ADMIN_EMAIL fallback
                     as other host crons).

Env:
  GITHUB_TOKEN is not required: the host cron is already `gh` authenticated.
  LITELLM_MASTER_KEY / LITELLM_BASE_URL for the lem-medium call; fallback to OPENAI_API_KEY at
  base_url http://litellm:4000. If no key is available the LLM step is skipped and the script still
  emits the deterministic report.
  ADMIN_EMAIL, LINKEDIN_EMAIL override the default alert recipient.

Exit: 0 in sync / applied, 2 changes pending (--dry-run), 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

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

EMAIL_FALLBACK = "christopher.queen@gmail.com"

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
    """Pick the priority label to add, respecting existing owner-set priorities."""
    existing = [l for l in current_labels if l.startswith("priority:")]
    if existing:
        return None
    return decision.priority


def select_flow_label(decision: TriageDecision, current_labels: list[str]) -> Optional[str]:
    """Only add a flow label if none exists."""
    existing = [l for l in current_labels if l in FLOW_LABELS]
    if existing:
        return None
    return decision.flow if decision.flow in ("agent:ready", "needs-human") else None


def select_topical_labels(decision: TriageDecision, current_labels: list[str],
                          allowed: set[str]) -> list[str]:
    """Add topical labels that are not already present and are in the repo's label vocabulary."""
    return [l for l in decision.topical_labels
            if l not in current_labels and l in allowed and l not in (*PRIORITY_LABELS, *FLOW_LABELS)]


def select_milestone(decision: TriageDecision, milestones: list[dict]) -> Optional[int]:
    """Resolve a milestone title to its number, or None if it is a proposal."""
    if decision.milestone_number:
        return decision.milestone_number
    title = (decision.milestone_title or "").strip()
    for m in milestones or []:
        if str(m.get("title") or "").strip() == title:
            return int(m.get("number"))
    return None


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
        result = self._run(
            ["api", f"repos/{self.repo}/issues", "--method", "GET", "--field", "state=open",
             "--field", "per_page=100", "--paginate",
             "--jq", ".[] | {number,title,body,state,labels,milestone,createdAt,updatedAt,author,isPullRequest}"],
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
        flow_label = select_flow_label(d, issue.labels)
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


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Daily issue triage for LEM.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show the plan without applying it (default).")
    mode.add_argument("--apply", action="store_true", help="Apply label/milestone changes and write the report.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo to triage.")
    parser.add_argument("--date", default=str(date.today()), help="Run date (YYYY-MM-DD).")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, help="Directory for dated markdown reports.")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES,
                        help="Max issues sent to the LLM in one run.")
    parser.add_argument("--email-to", help="Override the alert email recipient.")
    args = parser.parse_args(argv)

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

    gh = GitHubClient(args.repo)
    applied_count = apply_changes(gh, changes, dry_run=dry_run)

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
