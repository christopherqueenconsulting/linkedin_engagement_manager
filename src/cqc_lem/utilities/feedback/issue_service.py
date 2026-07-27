"""Auto-file GitHub issues from classified feedback — issue #498, stage 3 of the feedback->auto-work
loop (docs/launch-and-marketing-plan.md §B.2/B.3).

Stage 1 (#496) captures raw feedback; stage 2 (#497, `classifier.py`) turns one row into a
category/severity/risk verdict. This stage decides what the world sees:

    feedback row ──► classify (#497) ──► route
                                         ├─ drop / FAQ / human-triage  → status only, NO issue
                                         └─ auto_work
                                              ├─ over the per-user file cap → held, retried later
                                              ├─ similar to an OPEN cluster → +1 ITS issue
                                              └─ new problem → open ONE issue, stamp it back

Dedup is the whole point: one recurring problem must be one issue, so the Nth report bumps demand on
the existing issue instead of spamming the backlog. Similarity is embedding cosine when an embedding
is available and falls back to `content_framework.text_similarity` (the deterministic token-overlap
measure the post-uniqueness gate already uses) when it is not — so dedup NEVER silently degrades to
"everything is new" just because the embedding endpoint is down.

Every filed body is shaped for the pipeline RUNBOOK's `MODE=start` (Why / Scope / Files /
Acceptance) and carries `agent:ready` only when it is safe to build unattended: risk `none`,
confidence ≥ 0.7, and — for *features*, which are higher-risk than bugs — enough distinct reporters
to prove demand. Anything risky gets the matching `risk:*` + `needs-human` label, the owner assigned, and a
RUNBOOK-shaped Decision Comment so the hold is letter-pickable instead of prose.

Privacy: only the classifier's factual `summary` reaches GitHub — never the raw feedback text or any
reporter identity. Issues reference the internal `feedback.id` for correlation, and (issue #649) a
LINK to the PostHog session replay — a pointer into an access-controlled tool that masks the same
content the SPA does, not the content itself.

Env:
  FEEDBACK_GITHUB_TOKEN / GITHUB_TOKEN   — token used to file/comment; NO token == nothing is filed
  FEEDBACK_GITHUB_REPO                   — owner/name (default is this repo)
  FEEDBACK_ISSUE_ASSIGNEE                — who risky issues are assigned to (default gitchrisqueen)
  FEEDBACK_EMBEDDING_MODEL               — embedding alias (default lem-embedding)
  FEEDBACK_DUPLICATE_SIMILARITY          — dedup threshold 0-1 (default 0.82)
  FEEDBACK_FEATURE_DEMAND_MIN            — distinct reporters a FEATURE cluster needs (default 2)
  FEEDBACK_MAX_ISSUES_PER_USER_PER_DAY   — abuse guard (default 3)
"""

import json
import os
from typing import Optional

from cqc_lem.utilities.ai.content_framework import as_vector, cosine_similarity, text_similarity
from cqc_lem.utilities.db import (
    FeedbackStatus, count_feedback_filed_by_user, get_open_feedback_clusters,
    get_unprocessed_feedback, update_feedback_triage,
)
from cqc_lem.utilities.feedback.classifier import (
    MAX_BODY_CHARS, NEEDS_HUMAN_LABEL, RISK_LABELS, FeedbackCategory, FeedbackClassification,
    FeedbackRisk, FeedbackRoute, classify_feedback, labels_for,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning

PLAN_DOC = "docs/launch-and-marketing-plan.md"
GITHUB_API_BASE = "https://api.github.com"
DEFAULT_REPO = "christopherqueenconsulting/linkedin_engagement_manager"
DEFAULT_ASSIGNEE = "gitchrisqueen"
DEFAULT_EMBEDDING_MODEL = "lem-embedding"

FEEDBACK_LABEL = "feedback-loop"
AGENT_READY_LABEL = "agent:ready"

# Plan §B.2: `agent:ready` (build it unattended) requires risk none AND confidence >= 0.7. This is
# deliberately ABOVE the classifier's own min_confidence floor (0.6) — 0.6-0.7 is filed but held.
AGENT_READY_MIN_CONFIDENCE = 0.7

# Cosine/overlap at-or-above this means "same underlying problem". 0.82 is tight enough that two
# distinct bugs in the same component stay separate, loose enough to catch rewordings.
DUPLICATE_SIMILARITY_DEFAULT = 0.82
FEATURE_DEMAND_MIN_DEFAULT = 2
MAX_ISSUES_PER_USER_PER_DAY_DEFAULT = 3
RATE_LIMIT_WINDOW_HOURS = 24

MAX_CLUSTER_CANDIDATES = 100
MAX_TITLE_CHARS = 120
GITHUB_TIMEOUT_SECONDS = 20


class IssueAction:
    """What `file_feedback_issue` actually did — the contract callers/tests assert on."""
    FILED = 'filed'                  # a new issue was opened for a new cluster
    DEDUPED = 'deduped'              # +1 comment on the existing cluster's issue
    DROPPED = 'dropped'              # noise — dismissed, nothing filed
    FAQ = 'faq'                      # a support question — answered elsewhere (#507)
    NEEDS_HUMAN = 'needs_human'      # low confidence — human triage queue, no issue spam
    RATE_LIMITED = 'rate_limited'    # over this user's daily cap — held for a later pass
    ERROR = 'error'                  # GitHub call failed; row left for the next pass to retry


# Conventional-commit type per category, so a filed title reads like the commit that will close it.
_COMMIT_TYPES: dict[FeedbackCategory, str] = {
    FeedbackCategory.BUG: 'fix',
    FeedbackCategory.FEATURE: 'feat',
    FeedbackCategory.ENHANCEMENT: 'feat',
    FeedbackCategory.CLEANUP: 'chore',
    FeedbackCategory.QUESTION: 'docs',
    FeedbackCategory.NOISE: 'chore',
}

# Component -> the files an implementer starts in. This is the `## Files` hint MODE=start reads; it
# is a STARTING POINT, not a contract, which is why every entry ends with the generic test dirs.
_COMPONENT_FILES: dict[str, tuple] = {
    'feed-commenting': ('src/cqc_lem/app/run_automation.py',
                        'src/cqc_lem/utilities/linkedin/helper.py'),
    'replies': ('src/cqc_lem/app/run_automation.py',),
    'dms': ('src/cqc_lem/app/run_automation.py', 'src/cqc_lem/utilities/ai/dm_nurture.py'),
    'connections': ('src/cqc_lem/app/run_automation.py',
                    'src/cqc_lem/utilities/linkedin/company_page_inviter.py'),
    'content-generation': ('src/cqc_lem/app/run_content_plan.py',
                           'src/cqc_lem/utilities/ai/ai_helper.py',
                           'src/cqc_lem/utilities/ai/content_framework.py'),
    'scheduling': ('src/cqc_lem/app/run_scheduler.py',),
    'newsletter': ('src/cqc_lem/app/run_scheduler.py',),
    'carousels': ('src/cqc_lem/utilities/carousel_creator.py',),
    'video': ('src/cqc_lem/app/generate_variants.py',),
    'analytics': ('src/cqc_lem/utilities/observability.py',),
    'billing': ('src/cqc_lem/api/main.py', 'src/cqc_lem/utilities/db.py'),
    'auth': ('src/cqc_lem/api/main.py', 'src/cqc_lem/utilities/linkedin/token_refresh.py'),
    'onboarding': ('src/cqc_lem/ui/src/', 'src/cqc_lem/api/main.py'),
    'ui': ('src/cqc_lem/ui/src/',),
    'api': ('src/cqc_lem/api/main.py',),
    'infra': ('docker-compose.yml', 'compose/local/'),
    'other': (),
}

# The single genuine decision a held auto-filed item poses, per hold reason (RUNBOOK: never invent
# decisions — one question when there is one call to make). Each value is
# (question, *(option, consequence)) with option A always the recommendation.
_RISK_QUESTIONS: dict[FeedbackRisk, tuple] = {
    FeedbackRisk.PRODUCT_DECISION: (
        "This needs a product call before anything is built — how should the agent proceed?",
        ("Build it as scoped above", "agent implements the scope as written"),
        ("Build a narrower version", "reply with the reduced scope; agent implements that"),
        ("Don't build it", "issue is closed as wontfix"),
    ),
    FeedbackRisk.LIVE_LINKEDIN: (
        "This can only be verified against a live LinkedIn session — how should it be validated?",
        ("Agent ships it behind unit tests; you validate live after deploy",
         "fastest, live behavior unverified at merge"),
        ("Hold until you run a live grounding pass", "no code lands until you validate selectors"),
        ("Don't build it", "issue is closed as wontfix"),
    ),
    FeedbackRisk.MIGRATION: (
        "This implies a schema change — how should the migration be handled?",
        ("Additive-only migration (new nullable column/table), agent writes it",
         "no data loss, backward compatible"),
        ("You author the migration; agent does the code only",
         "you own the schema, agent wires it up"),
        ("Don't build it", "issue is closed as wontfix"),
    ),
    FeedbackRisk.SECURITY: (
        "This touches auth/privacy/secrets — how should it be handled?",
        ("Agent implements it, you review the security diff before merge",
         "held at merge for your review"),
        ("You handle it yourself", "agent stays out of it entirely"),
        ("Don't build it", "issue is closed as wontfix"),
    ),
}

# Hold reasons that are NOT about risk: unproven feature demand, and a scope the classifier itself
# was only ~two-thirds sure of.
_DEMAND_QUESTION: tuple = (
    "Only a few people have asked for this feature — should the pipeline build it now?",
    ("Build it now", "agent implements it this cycle"),
    ("Wait for more demand", "issue stays open, unbuilt, until more reports land"),
    ("Don't build it", "issue is closed as wontfix"),
)

_CONFIDENCE_QUESTION: tuple = (
    "The classifier was only moderately sure it read this report correctly — is the scope right?",
    ("Scope is right, build it", "agent implements the scope as written"),
    ("Scope is wrong", "reply with the correct scope; agent implements that"),
    ("Don't build it", "issue is closed as wontfix"),
)

_LETTERS = ('A', 'B', 'C', 'D')

# One sentence of WHY per hold reason — the RUNBOOK requires the recommendation to be justified.
_RECOMMENDATION_WHY: dict[str, str] = {
    'unproven-demand': "The report is concrete enough to build; A only costs one cycle if demand "
                       "never grows, while waiting costs the reporter.",
    'low-confidence': "The scope reads as filed; A is cheapest to correct if the classifier "
                      "misread it, since the change is small and scoped.",
    **{f"risk:{risk}": "The report is concrete enough to build as scoped; the hold exists for the "
                       "risk, not for the scope." for risk in RISK_LABELS},
}


# --- Env readers (read at call time, the live-env pattern used across the codebase) --------------

def github_repo() -> str:
    return (os.environ.get("FEEDBACK_GITHUB_REPO") or "").strip() or DEFAULT_REPO


def github_token() -> Optional[str]:
    token = ((os.environ.get("FEEDBACK_GITHUB_TOKEN") or "").strip()
             or (os.environ.get("GITHUB_TOKEN") or "").strip())
    return token or None


def issue_assignee() -> str:
    return (os.environ.get("FEEDBACK_ISSUE_ASSIGNEE") or "").strip() or DEFAULT_ASSIGNEE


def embedding_model() -> str:
    return (os.environ.get("FEEDBACK_EMBEDDING_MODEL") or "").strip() or DEFAULT_EMBEDDING_MODEL


def _env_float(key: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return max(low, min(high, value))


def _env_int(key: str, default: int, low: int, high: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    try:
        value = int(float(raw)) if raw else default
    except ValueError:
        return default
    return max(low, min(high, value))


def duplicate_similarity_min() -> float:
    return _env_float("FEEDBACK_DUPLICATE_SIMILARITY", DUPLICATE_SIMILARITY_DEFAULT, 0.0, 1.0)


def feature_demand_min() -> int:
    return _env_int("FEEDBACK_FEATURE_DEMAND_MIN", FEATURE_DEMAND_MIN_DEFAULT, 1, 100)


def max_issues_per_user_per_day() -> int:
    return _env_int("FEEDBACK_MAX_ISSUES_PER_USER_PER_DAY",
                    MAX_ISSUES_PER_USER_PER_DAY_DEFAULT, 1, 100)


# --- Similarity ---------------------------------------------------------------------------------
# `cosine_similarity` / `as_vector` live in content_framework alongside `text_similarity` — the ONE
# similarity toolbox every dedup gate shares (the comment gate uses the same pair, issue #617). They
# are re-exported here because this module's public API has always carried them.


def similarity(text_a: str, text_b: str, vector_a: object = None,
               vector_b: object = None) -> float:
    """How alike two reports are. Prefers embedding cosine (catches "comments never post" vs "my
    replies don't go through", which share almost no vocabulary); falls back to the deterministic
    token-overlap measure whenever either embedding is missing or unusable."""
    va, vb = as_vector(vector_a), as_vector(vector_b)
    if va and vb and len(va) == len(vb):
        return cosine_similarity(va, vb)
    return text_similarity(text_a or "", text_b or "")


def embed_text(text: Optional[str]) -> Optional[list]:
    """Embed one feedback body via the LiteLLM proxy. Returns None on any failure — callers must
    treat a missing embedding as "compare lexically", never as "no duplicates exist"."""
    body = (text or "").strip()[:MAX_BODY_CHARS]
    if not body:
        return None
    model = embedding_model()
    try:
        # Lazy import keeps this module importable without the AI stack configured.
        from cqc_lem.utilities.ai.client import client
        response = client.embeddings.create(model=model, input=body)
        vector = as_vector(list(response.data[0].embedding))
    except Exception as e:
        log_warning("Feedback embedding failed — dedup falls back to token overlap", exc=e,
                    ai_model=model)
        return None
    if not vector:
        log_warning("Feedback embedding was empty — dedup falls back to token overlap",
                    ai_model=model)
    return vector


def find_duplicate_cluster(body: str, vector: Optional[list], clusters: Optional[list],
                           threshold: float = None) -> tuple:
    """(cluster, score) of the most similar OPEN cluster at-or-above the threshold, else (None, best
    score seen). Clusters with no filed issue are skipped — there is nothing to +1."""
    limit = duplicate_similarity_min() if threshold is None else threshold
    best_cluster, best_score = None, 0.0
    for cluster in (clusters or [])[:MAX_CLUSTER_CANDIDATES]:
        if not cluster or not cluster.get("github_issue_number"):
            continue
        score = similarity(body, cluster.get("body") or "", vector, cluster.get("embedding"))
        if score > best_score:
            best_score = score
            if score >= limit:
                best_cluster = cluster
    return (best_cluster if best_score >= limit else None), best_score


def cluster_by_id(clusters: Optional[list], cluster_id: Optional[int]) -> Optional[dict]:
    """The cluster whose id the classifier named in `duplicate_of` (it sees the same candidates), or
    None. Its judgement is honored even below the cosine threshold — it read both texts."""
    if cluster_id is None:
        return None
    for cluster in clusters or []:
        if cluster and cluster.get("cluster_id") == cluster_id and cluster.get(
                "github_issue_number"):
            return cluster
    return None


def duplicate_candidates(clusters: Optional[list]) -> list:
    """Open clusters in the {'id', 'title'} shape `classify_feedback(duplicate_candidates=...)`
    expects, so the LLM can flag a duplicate the vectors miss."""
    return [{"id": c.get("cluster_id"), "title": (c.get("body") or "").strip()}
            for c in (clusters or []) if c and c.get("cluster_id") and c.get("github_issue_number")]


# --- Label + body construction (pure) -----------------------------------------------------------

def feature_demand_met(category: FeedbackCategory, reporter_count: int) -> bool:
    """Plan §B.3 abuse guard: bugs auto-file immediately, but a FEATURE needs demand from enough
    distinct reporters before the pipeline may build it unattended — so a first-of-its-kind feature
    request is filed and HELD for the owner rather than built on one person's word."""
    if category != FeedbackCategory.FEATURE:
        return True
    return int(reporter_count or 0) >= feature_demand_min()


def is_agent_ready(classification: FeedbackClassification, reporter_count: int = 1) -> bool:
    """Whether the pipeline may build this with no human in the loop."""
    return (classification.risk == FeedbackRisk.NONE
            and classification.route == FeedbackRoute.AUTO_WORK
            and classification.confidence >= AGENT_READY_MIN_CONFIDENCE
            and feature_demand_met(classification.category, reporter_count))


def labels_for_issue(classification: FeedbackClassification, reporter_count: int = 1) -> list:
    """The full label set for a filed issue: the classifier's taxonomy labels, the `feedback-loop`
    provenance label, and EXACTLY ONE of `agent:ready` (safe to build unattended) or `needs-human`
    (risky or unproven demand — the pipeline holds it)."""
    labels = list(labels_for(classification.category, classification.severity,
                             classification.risk, classification.route))
    labels.append(FEEDBACK_LABEL)
    if is_agent_ready(classification, reporter_count):
        labels.append(AGENT_READY_LABEL)
    elif NEEDS_HUMAN_LABEL not in labels:
        labels.append(NEEDS_HUMAN_LABEL)
    # dict.fromkeys: de-dupe (risk/needs-human can arrive from both sources) while keeping order.
    return list(dict.fromkeys(labels))


def build_issue_title(classification: FeedbackClassification) -> str:
    """Conventional-commit-shaped title: `<type>(<component>): <imperative summary>`."""
    commit_type = _COMMIT_TYPES.get(classification.category, 'chore')
    title = (classification.title or classification.summary or "Investigate user feedback").strip()
    return f"{commit_type}({classification.component}): {title}"[:MAX_TITLE_CHARS]


def _files_hint(component: str) -> list:
    return list(_COMPONENT_FILES.get(component, ())) + ["tests/unit/"]


def replay_url_from_context(context: object) -> Optional[str]:
    """The PostHog replay link for the session a report was filed from (issue #649), or None. The
    widget stamps `posthog_session_id` onto every report; without this the id sits in the DB and
    nobody ever watches the session that produced the bug."""
    if not isinstance(context, dict):
        return None
    from cqc_lem.utilities.observability import session_replay_url
    return session_replay_url(context.get("posthog_session_id"))


def build_issue_body(classification: FeedbackClassification, feedback_id: Optional[int] = None,
                     reporter_count: int = 1, item_count: int = 1,
                     replay_url: Optional[str] = None) -> str:
    """The `MODE=start` body: Why / Scope / Files / Acceptance, plus the provenance the pipeline and
    a human both need. Only the classifier's factual summary is included — never the raw report; a
    replay link is a pointer into PostHog, which is access-controlled and masks the same content."""
    ready = is_agent_ready(classification, reporter_count)
    demand = (f"Reported by {max(0, int(reporter_count or 0))} distinct user(s) across "
              f"{max(1, int(item_count or 0))} feedback item(s).")
    provenance = (f"Auto-filed from in-app feedback"
                  f"{f' #{feedback_id}' if feedback_id else ''} by the feedback→auto-work loop "
                  f"(`{PLAN_DOC}` §B.2/B.3). Classifier: {classification.category}/"
                  f"{classification.severity}, risk `{classification.risk}`, confidence "
                  f"{classification.confidence:.2f}.")

    scope = [f"Address the reported {classification.category} in the `{classification.component}` "
             f"area of the product.",
             "Keep the change minimal and scoped to this report — no unrelated refactors."]
    if classification.risk != FeedbackRisk.NONE:
        scope.append(f"Risk `{classification.risk}` — see the Decision Comment below before "
                     f"implementing.")

    acceptance = ["The behavior the reporter described is fixed/implemented.",
                  "Unit tests cover the new/changed logic (≥80% patch coverage).",
                  "All required CI gates pass."]
    if classification.risk == FeedbackRisk.MIGRATION:
        acceptance.append("Any migration is additive and named "
                          "`V<YYYYMMDDHHMMSS>__short_name.sql`.")
    if not ready:
        acceptance.append("A human has answered the Decision Comment before this is merged.")

    lines = ["## Why", classification.summary or "(no summary — see the classifier verdict below)",
             "", demand, "", provenance, ""]
    if replay_url:
        lines += [f"[Watch the session replay]({replay_url}) — the browser session this report was "
                  f"filed from.", ""]
    lines += ["## Scope"]
    lines += [f"- {item}" for item in scope]
    lines += ["", "## Files",
              "Likely starting points (verify before editing; add/extend the unit tests):"]
    lines += [f"- `{path}`" for path in _files_hint(classification.component)]
    lines += ["", "## Acceptance"]
    lines += [f"- {item}" for item in acceptance]
    return "\n".join(lines)


def hold_reason(classification: FeedbackClassification, reporter_count: int = 1) -> Optional[str]:
    """Why this item can't ship unattended — `risk:<kind>`, `unproven-demand`, `low-confidence` — or
    None when it is `agent:ready`. This is the `risk:` line of the Decision Comment."""
    if is_agent_ready(classification, reporter_count):
        return None
    if classification.risk != FeedbackRisk.NONE:
        return f"risk:{classification.risk}"
    if not feature_demand_met(classification.category, reporter_count):
        return "unproven-demand"
    return "low-confidence"


def build_decision_comment(classification: FeedbackClassification,
                           reporter_count: int = 1) -> str:
    """The RUNBOOK Decision Comment for a HELD auto-filed item: ONE genuine question (should this be
    built, and under what constraint), lettered options with consequences, and a recommendation. The
    question is chosen by why it is actually held — risk, unproven demand, or low confidence."""
    reason = hold_reason(classification, reporter_count) or "low-confidence"
    if classification.risk != FeedbackRisk.NONE:
        question, *options = _RISK_QUESTIONS[classification.risk]
    elif reason == "unproven-demand":
        question, *options = _DEMAND_QUESTION
    else:
        question, *options = _CONFIDENCE_QUESTION

    lines = ["## 🧑‍⚖️ Human decision needed — reply with option letters",
             f"Held (`{NEEDS_HUMAN_LABEL}`, {reason}). Auto-filed from user feedback by the "
             f"feedback→auto-work loop (`{PLAN_DOC}` §B.2), but it cannot ship unattended.",
             "Reply one letter per question — e.g. `1A` — or `ok` for all recommendations.", "",
             f"### 1. {question}"]
    for letter, (option, consequence) in zip(_LETTERS, options):
        recommended = "  ✅ *recommended*" if letter == 'A' else ""
        lines.append(f"- **{letter}. {option}** — {consequence}{recommended}")
    why = _RECOMMENDATION_WHY.get(reason, _RECOMMENDATION_WHY['low-confidence'])
    lines += ["", f"**My recommendation: `1A`.** {why}"]
    return "\n".join(lines)


def build_duplicate_comment(classification: FeedbackClassification,
                            feedback_id: Optional[int] = None, reporter_count: int = 1,
                            item_count: int = 1, replay_url: Optional[str] = None) -> str:
    """The +1 that a repeat report leaves on the EXISTING issue instead of opening a new one — the
    demand signal the pipeline prioritizes by. Each repeat carries its OWN replay: the second
    reporter's session is often the one that shows what the first report couldn't explain."""
    lines = [
        "**+1 — reported again via in-app feedback.**",
        "",
        f"New detail: {classification.summary or '(none)'}",
        "",
        f"Demand now: {max(0, int(reporter_count or 0))} distinct reporter(s), "
        f"{max(1, int(item_count or 0))} report(s). "
        f"Latest: feedback{f' #{feedback_id}' if feedback_id else ''} "
        f"({classification.category}/{classification.severity}, confidence "
        f"{classification.confidence:.2f}).",
        "",
    ]
    if replay_url:
        lines += [f"[Watch this report's session replay]({replay_url})", ""]
    lines.append(f"_Auto-deduplicated by the feedback→auto-work loop (`{PLAN_DOC}` §B.3)._")
    return "\n".join(lines)


# --- GitHub I/O ---------------------------------------------------------------------------------

def github_request(method: str, path: str, payload: dict = None) -> Optional[object]:
    """One GitHub REST call, shared with the shipped-fix notifier (issue #502). Returns the parsed
    body (a dict, or a list for collection endpoints), or None when unconfigured/failed — filing is
    best-effort by design: a GitHub outage must never lose the feedback row. `path` may carry its
    own query string."""
    token = github_token()
    if not token:
        log_warning("Feedback issue filing skipped — no FEEDBACK_GITHUB_TOKEN/GITHUB_TOKEN set",
                    api_provider="github")
        return None
    import requests
    url = f"{GITHUB_API_BASE}/repos/{github_repo()}/{path.lstrip('/')}"
    try:
        response = requests.request(
            method, url, json=payload, timeout=GITHUB_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
    except Exception as e:
        log_error(f"GitHub {method} {path} failed", exc=e, api_provider="github")
        return None
    if response.status_code >= 300:
        log_error(f"GitHub {method} {path} returned {response.status_code}: "
                  f"{response.text[:300]}", api_provider="github",
                  http_status=response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        return {}


def create_github_issue(title: str, body: str, labels: list,
                        assignees: list = None) -> Optional[int]:
    """Open an issue; returns its number or None.

    Labels and assignees are attached in SEPARATE calls after creation (issue #598): GitHub
    silently drops both when a fine-grained PAT supplies them in the create payload, so every
    auto-filed issue landed label-less and the pipeline never picked it up. A failed attach is a
    warning, never a lost issue — the row is already on GitHub by then."""
    data = github_request("POST", "issues", {"title": title, "body": body})
    number = data.get("number") if isinstance(data, dict) else None
    if number is None:
        return None
    number = int(number)
    label_list = [str(label) for label in (labels or [])]
    if label_list and github_request("POST", f"issues/{number}/labels",
                                     {"labels": label_list}) is None:
        log_warning(f"Auto-filed issue #{number} could not be labeled {label_list}",
                    api_provider="github")
    assignee_list = [str(assignee) for assignee in (assignees or [])]
    if assignee_list and github_request("POST", f"issues/{number}/assignees",
                                        {"assignees": assignee_list}) is None:
        log_warning(f"Auto-filed issue #{number} could not be assigned to {assignee_list}",
                    api_provider="github")
    log_info(f"Auto-filed feedback issue #{number}: {title}", api_provider="github")
    return number


def comment_on_issue(issue_number: int, body: str) -> bool:
    """Comment on an existing issue; True when GitHub accepted it."""
    if not issue_number:
        return False
    return github_request("POST", f"issues/{int(issue_number)}/comments",
                           {"body": body}) is not None


# --- Orchestration ------------------------------------------------------------------------------

def _result(action: str, feedback_id: Optional[int], **extra) -> dict:
    return {"action": action, "feedback_id": feedback_id, **extra}


def file_feedback_issue(feedback: dict, classification: FeedbackClassification = None,
                        clusters: list = None) -> dict:
    """Take ONE captured feedback row all the way to its outcome (see `IssueAction`). Never raises.

    `classification` / `clusters` are injectable so a batch pass classifies once and loads the open
    clusters once. When a new issue IS filed, the caller should append the returned `cluster` to its
    in-memory cluster list so two identical reports in the same batch don't both file."""
    row = feedback or {}
    feedback_id = row.get("id")
    body = row.get("body") or ""
    if not body.strip():
        return _result(IssueAction.DROPPED, feedback_id, reason="empty body")
    user_id = row.get("user_id")

    # Abuse guard FIRST: one user cannot turn the backlog into their personal firehose. Checked
    # before the LLM so a held row costs nothing, and the row's status is left alone so a later pass
    # (fresh 24h window) picks it up — held, never dropped.
    if user_id is not None:
        recent = count_feedback_filed_by_user(user_id, RATE_LIMIT_WINDOW_HOURS)
        if recent >= max_issues_per_user_per_day():
            log_info(f"Feedback {feedback_id} held — {recent} of this user's report(s) already "
                     f"reached GitHub in {RATE_LIMIT_WINDOW_HOURS}h", user_id=user_id)
            return _result(IssueAction.RATE_LIMITED, feedback_id, recent=recent)

    open_clusters = get_open_feedback_clusters() if clusters is None else clusters

    context = row.get("context_json")
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except ValueError:
            context = None
    replay_url = replay_url_from_context(context)

    if classification is None:
        classification = classify_feedback(
            body, type_hint=row.get("type_hint"), context=context,
            duplicate_candidates=duplicate_candidates(open_clusters), user_id=user_id)

    # Routes that must never produce an issue. Statuses still move so the row leaves the queue.
    if classification.route == FeedbackRoute.DROP:
        update_feedback_triage(feedback_id, status=FeedbackStatus.DISMISSED)
        return _result(IssueAction.DROPPED, feedback_id, reason="classified as noise")
    if classification.route == FeedbackRoute.FAQ:
        update_feedback_triage(feedback_id, status=FeedbackStatus.TRIAGED)
        return _result(IssueAction.FAQ, feedback_id)
    if classification.route == FeedbackRoute.NEEDS_HUMAN:
        update_feedback_triage(feedback_id, status=FeedbackStatus.TRIAGED)
        return _result(IssueAction.NEEDS_HUMAN, feedback_id,
                       confidence=classification.confidence)

    vector = as_vector(row.get("embedding")) or embed_text(body)

    match, score = find_duplicate_cluster(body, vector, open_clusters)
    match = match or cluster_by_id(open_clusters, classification.duplicate_of)
    if match:
        # reporter_count counts DISTINCT identified users; an anonymous report adds a report but no
        # reporter, so it must never be floored up to 1 — that would fake the feature-demand signal.
        reporter_count = int(match.get("reporter_count") or 0) + (1 if user_id is not None else 0)
        item_count = int(match.get("item_count") or 0) + 1
        commented = comment_on_issue(match["github_issue_number"], build_duplicate_comment(
            classification, feedback_id, reporter_count, item_count, replay_url))
        if not commented:
            return _result(IssueAction.ERROR, feedback_id, reason="duplicate comment failed",
                           cluster_id=match.get("cluster_id"))
        update_feedback_triage(feedback_id, status=FeedbackStatus.CLUSTERED,
                               cluster_id=match["cluster_id"],
                               github_issue_number=match["github_issue_number"],
                               embedding=vector)
        log_debug(f"Feedback {feedback_id} deduped into cluster {match['cluster_id']} "
                  f"(similarity {score:.2f})", user_id=user_id)
        return _result(IssueAction.DEDUPED, feedback_id,
                       cluster_id=match["cluster_id"],
                       issue_number=match["github_issue_number"], similarity=score)

    reporter_count = 1 if user_id is not None else 0
    labels = labels_for_issue(classification, reporter_count)
    ready = AGENT_READY_LABEL in labels
    number = create_github_issue(
        build_issue_title(classification),
        build_issue_body(classification, feedback_id, reporter_count, item_count=1,
                         replay_url=replay_url),
        labels, assignees=None if ready else [issue_assignee()])
    if number is None:
        # Leave the row unclustered/`new` — the next pass retries rather than losing the report.
        return _result(IssueAction.ERROR, feedback_id, reason="issue creation failed",
                       labels=labels)
    if not ready:
        comment_on_issue(number, build_decision_comment(classification, reporter_count))
    update_feedback_triage(feedback_id, status=FeedbackStatus.ISSUE_CREATED,
                           cluster_id=feedback_id, github_issue_number=number, embedding=vector)
    return _result(IssueAction.FILED, feedback_id, issue_number=number, labels=labels,
                   agent_ready=ready, similarity=score,
                   cluster={"cluster_id": feedback_id, "body": body, "embedding": vector,
                            "github_issue_number": number, "item_count": 1,
                            "reporter_count": reporter_count})


def process_new_feedback(limit: int = 25) -> dict:
    """Drain the capture queue: classify, dedup, and file each unprocessed feedback row. The open
    clusters are loaded ONCE and grown in memory as issues are filed, so a burst of identical
    reports in one pass produces one issue and N-1 +1 comments."""
    rows = get_unprocessed_feedback(limit)
    clusters = get_open_feedback_clusters()
    counts: dict = {}
    for row in rows:
        try:
            result = file_feedback_issue(row, clusters=clusters)
        except Exception as e:
            log_error(f"Auto-filing feedback {row.get('id')} failed", exc=e)
            counts[IssueAction.ERROR] = counts.get(IssueAction.ERROR, 0) + 1
            continue
        counts[result["action"]] = counts.get(result["action"], 0) + 1
        if result.get("cluster"):
            clusters.append(result["cluster"])
    log_info(f"Feedback auto-filing pass: {len(rows)} row(s) — {counts or 'nothing to do'}")
    return {"processed": len(rows), "counts": counts}


def recluster_feedback(limit: int = 200) -> dict:
    """Nightly regrouping pass (`auto_recluster_feedback`). Deliberately does NOT file anything: it
    backfills missing embeddings and attaches leftover rows — including ones the classifier left in
    human triage — to an open cluster they now match, +1-ing that cluster's issue. Filing stays with
    `process_new_feedback` so a nightly re-run can never open a second issue for a known problem.

    Auto-closing issues whose cluster is resolved is NOT here — resolution is tracked on the GitHub
    side by the changelog/notify stage (issue #502)."""
    clusters = get_open_feedback_clusters()
    # Unlike the filing pass, this one also reconsiders rows already parked in `triaged` — a report
    # a human never got to may well be the same problem as an issue filed since.
    rows = get_unprocessed_feedback(limit, statuses=(FeedbackStatus.NEW, FeedbackStatus.TRIAGED))
    attached = 0
    embedded = 0
    for row in rows:
        body = row.get("body") or ""
        if not body.strip():
            continue
        vector = as_vector(row.get("embedding"))
        if vector is None:
            vector = embed_text(body)
            if vector:
                update_feedback_triage(row.get("id"), embedding=vector)
                embedded += 1
        match, score = find_duplicate_cluster(body, vector, clusters)
        if not match:
            continue
        commented = comment_on_issue(match["github_issue_number"], "\n".join([
            "**+1 — a previously untriaged report matches this issue.**",
            "",
            f"Nightly reclustering matched feedback #{row.get('id')} to this cluster "
            f"(similarity {score:.2f}).",
            "",
            f"_Auto-reclustered by the feedback→auto-work loop (`{PLAN_DOC}` §B.3)._",
        ]))
        if not commented:
            continue
        update_feedback_triage(row.get("id"), status=FeedbackStatus.CLUSTERED,
                               cluster_id=match["cluster_id"],
                               github_issue_number=match["github_issue_number"])
        attached += 1
    log_info(f"Feedback recluster pass: {len(rows)} candidate(s), {attached} attached, "
             f"{embedded} embedding(s) backfilled")
    return {"scanned": len(rows), "attached": attached, "embedded": embedded,
            "clusters": len(clusters)}
