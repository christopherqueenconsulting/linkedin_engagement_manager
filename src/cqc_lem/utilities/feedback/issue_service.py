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

A filed issue is only useful if its LABELS land — the agent pipeline selects on them — so every
filing beat also runs `repair_auto_filed_issues`, which re-attaches the labels/assignee/Decision
Comment on any already-filed issue whose post-create calls were refused (issue #718).

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
import re
import time
from typing import Optional

from cqc_lem.utilities.ai.content_framework import as_vector, cosine_similarity, text_similarity
from cqc_lem.utilities.db import (
    FeedbackStatus,
    count_feedback_filed_by_user,
    count_pending_admin_review,
    get_open_feedback_clusters,
    get_unprocessed_feedback,
    update_feedback_triage,
)
from cqc_lem.utilities.feedback.classifier import (
    MAX_BODY_CHARS,
    NEEDS_HUMAN_LABEL,
    RISK_LABELS,
    FeedbackCategory,
    FeedbackClassification,
    FeedbackRisk,
    FeedbackRoute,
    FeedbackSeverity,
    classify_feedback,
    labels_for,
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

# An auto-filed feedback issue NEVER carries `agent:ready`.
#
# `POST /api/feedback` is UNAUTHENTICATED — the widget is deliberately offered to logged-out
# visitors, and the per-user daily cap keys on `user_id`, which is NULL for every one of them. And
# `agent:ready` is not a priority hint: it is the signal that hands an autonomous agent the owner's
# credentials and a merge to `main`. Wiring anonymous internet input to that made the classifier's
# confidence score the only thing between a stranger's prose and production.
#
# Nothing else about the loop changes — it still files, clusters, classifies, assigns and posts the
# Decision Comment. A human promotes `needs-human` -> `agent:ready`, and `tick.sh` independently
# verifies WHO did (`label_actor_trusted`), so flipping this back on alone would not re-open the
# path. Left as a named constant rather than deleted code so the intent survives the next reader.
FEEDBACK_MAY_GRANT_AGENT_READY = False
# Distinguishes "the classifier wasn't sure" from "the classifier was sure, the SOURCE isn't trusted".
FEEDBACK_HOLD_REASON = "unvetted-source"

# Cosine/overlap at-or-above this means "same underlying problem". 0.82 is tight enough that two
# distinct bugs in the same component stay separate, loose enough to catch rewordings.
DUPLICATE_SIMILARITY_DEFAULT = 0.82
FEATURE_DEMAND_MIN_DEFAULT = 2
MAX_ISSUES_PER_USER_PER_DAY_DEFAULT = 3
RATE_LIMIT_WINDOW_HOURS = 24

MAX_CLUSTER_CANDIDATES = 100
MAX_TITLE_CHARS = 120
GITHUB_TIMEOUT_SECONDS = 20

# Issue #767: a DNS/connect blip is the NETWORK, not a defect in this loop — GitHub is reached
# best-effort and the row is left for the next pass either way. It is retried in place, then
# reported as a WARNING so `log_escalation` decides: one blip stays a warning, GitHub unreachable
# for three passes running becomes the ERROR/$exception it deserves.
GITHUB_RETRY_ATTEMPTS = 3
GITHUB_RETRY_BACKOFF_SECONDS = 0.5
# Only a READ is replayed. A write that failed AFTER GitHub accepted it (read timeout, connection
# reset mid-response) is indistinguishable from one that never landed, so retrying `POST /issues`
# would file the duplicate this whole module exists to prevent. A lost write costs nothing: the
# cluster is still open and the next beat's repair pass re-attaches it.
GITHUB_IDEMPOTENT_METHODS = ("GET", "HEAD")
# Deterministic `requests` faults that also inherit IOError. A malformed URL or a redirect loop is
# OUR bug, not the network — retrying it three times and reporting it as "unreachable" would hide
# the one thing worth alerting on.
NON_TRANSPORT_REQUEST_ERRORS = ("URLRequired", "MissingSchema", "InvalidSchema", "InvalidURL",
                                "InvalidHeader", "InvalidJSONError", "TooManyRedirects")
# Once the transport is down, every other call in the same pass fails the same way. Short-circuiting
# them keeps ONE outage to ONE warning instead of one per request — three of which is exactly the
# escalation threshold, i.e. an outage would file itself as a defect.
GITHUB_UNREACHABLE_COOLDOWN_SECONDS = 60.0

# Monotonic deadline for the cool-off above. Process-local on purpose: it exists to collapse the
# retries of ONE pass, not to coordinate workers.
_unreachable_until = 0.0

# Issue #718: the label/assignee/comment sub-resource endpoints 403'd for weeks while `POST /issues`
# kept succeeding, so every auto-filed issue landed label-less. Named in every failure log so the
# operator sees the fix, not just the status code.
TOKEN_PERMISSION_HINT = ("FEEDBACK_GITHUB_TOKEN needs `Issues: Read and write` (+ `Metadata: "
                         "Read`) on this repo — see issue #718.")

# What marks an issue as OURS. `build_issue_body` writes the first; `build_decision_comment` the
# second. The repair pass will not touch an issue that carries neither.
FILED_PROVENANCE_MARKER = "Auto-filed from in-app feedback"
DECISION_COMMENT_MARKER = "Human decision needed"

# The provenance/demand fields `build_issue_body` writes, read back so a repair pass can recompute
# the exact label set WITHOUT re-paying for a classification.
_PROVENANCE_RE = re.compile(r"Classifier:\s*(?P<category>[\w-]+)/(?P<severity>[\w-]+),\s*risk\s*"
                            r"`(?P<risk>[\w-]+)`,\s*confidence\s*(?P<confidence>[0-9]*\.?[0-9]+)")
_DEMAND_RE = re.compile(r"Reported by\s+(?P<reporters>\d+)\s+distinct user")
_TITLE_RE = re.compile(r"^\w+\((?P<component>[\w-]+)\):\s*(?P<title>.+)$")

MAX_REPAIR_ISSUES = 50
REPAIR_COMMENT_PAGE_SIZE = 100
# A refused WRITE stops the sweep on the first one; a refused READ can be a single deleted issue, so
# it takes a short run of them before the same conclusion is drawn.
REPAIR_READ_FAILURE_STOP = 3


class RepairAction:
    """What `repair_filed_issue` did to ONE already-filed issue (issue #718)."""
    OK = 'ok'              # nothing missing — the attach calls landed
    REPAIRED = 'repaired'  # labels/assignee/decision comment re-attached
    SKIPPED = 'skipped'    # not ours, closed, or unreadable provenance — never touched
    ERROR = 'error'        # GitHub refused the repair too (the token is still wrong)


class IssueAction:
    """What `file_feedback_issue` actually did — the contract callers/tests assert on."""
    FILED = 'filed'                  # a new issue was opened for a new cluster
    DEDUPED = 'deduped'              # +1 comment on the existing cluster's issue
    DROPPED = 'dropped'              # noise — dismissed, nothing filed
    FAQ = 'faq'                      # a support question — answered elsewhere (#507)
    NEEDS_HUMAN = 'needs_human'      # low confidence — human triage queue, no issue spam
    RATE_LIMITED = 'rate_limited'    # over this user's daily cap — held for a later pass
    ERROR = 'error'                  # GitHub call failed; row left for the next pass to retry


# An `ERROR` result the admin panel must word differently: nothing was refused by GitHub, the
# classifier never answered. Retrying is the right next action, so the row is left in `new`.
CLASSIFIER_UNAVAILABLE_REASON = 'classification unavailable'


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
    # `run_automation.py` was emptied by #1154 and deleted by #1206 — every engagement lane lives
    # under `app/engagement/`, so that is where an implementer has to start.
    'feed-commenting': ('src/cqc_lem/app/engagement/feed.py',
                        'src/cqc_lem/utilities/linkedin/helper.py'),
    'replies': ('src/cqc_lem/app/engagement/posting.py',),
    'dms': ('src/cqc_lem/app/engagement/outreach.py', 'src/cqc_lem/utilities/ai/dm_nurture.py'),
    'connections': ('src/cqc_lem/app/engagement/invites.py',
                    'src/cqc_lem/app/engagement/outreach.py',
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
    """`owner/name` every filing call is addressed to, defaulting to this repo.

    An empty or whitespace-only override falls back to the default rather than producing a URL with
    a hole in it, so a blank line in `.env` can never point the filer at nothing.
    """
    return (os.environ.get("FEEDBACK_GITHUB_REPO") or "").strip() or DEFAULT_REPO


def github_token() -> Optional[str]:
    """The credential used to file, comment and label, or None when neither variable is set.

    `FEEDBACK_GITHUB_TOKEN` wins over the ambient `GITHUB_TOKEN` so a narrowly-scoped feedback
    credential can be installed without disturbing whatever else reads the generic one.

    Returns:
        None — never `""` — because None is the sentinel every caller branches on to mean "file
        NOTHING this beat". Filing is best-effort: an unconfigured token skips the write and leaves
        the feedback row untouched for a later run, it never fails the beat.
    """
    token = ((os.environ.get("FEEDBACK_GITHUB_TOKEN") or "").strip()
             or (os.environ.get("GITHUB_TOKEN") or "").strip())
    return token or None


def issue_assignee() -> str:
    """Who a HELD issue is assigned to — the human who answers a Decision Comment.

    Only risky / low-confidence filings carry an assignee; an `agent:ready` issue is left unassigned
    so the pipeline picks it up instead of a person.
    """
    return (os.environ.get("FEEDBACK_ISSUE_ASSIGNEE") or "").strip() or DEFAULT_ASSIGNEE


def embedding_model() -> str:
    """Tier alias used to embed a report for dedup.

    An unusable embedding is not fatal — similarity falls back to deterministic token overlap, so
    dedup degrades in accuracy but never to "everything is new".
    """
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
    """Similarity at or above which a report joins an existing cluster instead of opening an issue.

    Clamped to 0.0-1.0 and an unparseable value falls back to the default, because both ends of this
    dial are damaging out of range: too low collapses unrelated reports onto one issue, too high
    spams the backlog with the same problem over and over.
    """
    return _env_float("FEEDBACK_DUPLICATE_SIMILARITY", DUPLICATE_SIMILARITY_DEFAULT, 0.0, 1.0)


def feature_demand_min() -> int:
    """Distinct reporters a FEATURE cluster needs before it may be built unattended.

    Bugs carry no such bar — a feature is the higher-risk build, so one person's wish is held for
    proof of demand rather than shipped. Clamped to at least 1 so the gate can never be configured
    into "build every feature request".
    """
    return _env_int("FEEDBACK_FEATURE_DEMAND_MIN", FEATURE_DEMAND_MIN_DEFAULT, 1, 100)


def max_issues_per_user_per_day() -> int:
    """Abuse guard: issues ONE reporter may cause to be filed in a day.

    `POST /api/feedback` is unauthenticated, so this is what stops a single source from filling the
    backlog. Hitting it holds the row for a later beat — the feedback is never dropped. Clamped to
    at least 1, so a misconfigured value cannot silently switch filing off altogether.
    """
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
    token-overlap measure whenever either embedding is missing or unusable.
    """
    va, vb = as_vector(vector_a), as_vector(vector_b)
    if va and vb and len(va) == len(vb):
        return cosine_similarity(va, vb)
    return text_similarity(text_a or "", text_b or "")


def embed_text(text: Optional[str]) -> Optional[list]:
    """Embed one feedback body via the LiteLLM proxy. Returns None on any failure — callers must
    treat a missing embedding as "compare lexically", never as "no duplicates exist".
    """
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
    score seen). Clusters with no filed issue are skipped — there is nothing to +1.
    """
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
    None. Its judgement is honored even below the cosine threshold — it read both texts.
    """
    if cluster_id is None:
        return None
    for cluster in clusters or []:
        if cluster and cluster.get("cluster_id") == cluster_id and cluster.get(
                "github_issue_number"):
            return cluster
    return None


def duplicate_candidates(clusters: Optional[list]) -> list:
    """Open clusters in the {'id', 'title'} shape `classify_feedback(duplicate_candidates=...)`
    expects, so the LLM can flag a duplicate the vectors miss.
    """
    return [{"id": c.get("cluster_id"), "title": (c.get("body") or "").strip()}
            for c in (clusters or []) if c and c.get("cluster_id") and c.get("github_issue_number")]


# --- Label + body construction (pure) -----------------------------------------------------------

def feature_demand_met(category: FeedbackCategory, reporter_count: int) -> bool:
    """Plan §B.3 abuse guard: bugs auto-file immediately, but a FEATURE needs demand from enough
    distinct reporters before the pipeline may build it unattended — so a first-of-its-kind feature
    request is filed and HELD for the owner rather than built on one person's word.
    """
    if category != FeedbackCategory.FEATURE:
        return True
    return int(reporter_count or 0) >= feature_demand_min()


def classifier_would_auto_work(classification: FeedbackClassification,
                               reporter_count: int = 1) -> bool:
    """The CLASSIFIER's opinion: risk-free, well-evidenced, confidently categorised work.

    Kept separate from `is_agent_ready` so the Decision Comment and telemetry can still say "this
    looked buildable" even though the source is no longer trusted to grant that.
    """
    return (classification.risk == FeedbackRisk.NONE
            and classification.route == FeedbackRoute.AUTO_WORK
            and classification.confidence >= AGENT_READY_MIN_CONFIDENCE
            and feature_demand_met(classification.category, reporter_count))


def is_agent_ready(classification: FeedbackClassification, reporter_count: int = 1) -> bool:
    """Whether the pipeline may build this with no human in the loop. Always False — see
    `FEEDBACK_MAY_GRANT_AGENT_READY`.
    """
    return (FEEDBACK_MAY_GRANT_AGENT_READY
            and classifier_would_auto_work(classification, reporter_count))


def labels_for_issue(classification: FeedbackClassification, reporter_count: int = 1) -> list:
    """The full label set for a filed issue: the classifier's taxonomy labels, the `feedback-loop`
    provenance label, and EXACTLY ONE of `agent:ready` (safe to build unattended) or `needs-human`
    (risky or unproven demand — the pipeline holds it).
    """
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
    nobody ever watches the session that produced the bug.
    """
    if not isinstance(context, dict):
        return None
    from cqc_lem.utilities.observability import session_replay_url
    return session_replay_url(context.get("posthog_session_id"))


def build_issue_body(classification: FeedbackClassification, feedback_id: Optional[int] = None,
                     reporter_count: int = 1, item_count: int = 1,
                     replay_url: Optional[str] = None) -> str:
    """The `MODE=start` body: Why / Scope / Files / Acceptance, plus the provenance the pipeline and
    a human both need. Only the classifier's factual summary is included — never the raw report; a
    replay link is a pointer into PostHog, which is access-controlled and masks the same content.
    """
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
    None when it is `agent:ready`. This is the `risk:` line of the Decision Comment.
    """
    if is_agent_ready(classification, reporter_count):
        return None
    # Say WHICH gate held it. Falling through to "low-confidence" for an item the classifier was
    # confident about would send a human hunting a scoring bug that isn't there.
    if classifier_would_auto_work(classification, reporter_count):
        return FEEDBACK_HOLD_REASON
    if classification.risk != FeedbackRisk.NONE:
        return f"risk:{classification.risk}"
    if not feature_demand_met(classification.category, reporter_count):
        return "unproven-demand"
    return "low-confidence"


def build_decision_comment(classification: FeedbackClassification,
                           reporter_count: int = 1) -> str:
    """The RUNBOOK Decision Comment for a HELD auto-filed item: ONE genuine question (should this be
    built, and under what constraint), lettered options with consequences, and a recommendation. The
    question is chosen by why it is actually held — risk, unproven demand, or low confidence.
    """
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
    reporter's session is often the one that shows what the first report couldn't explain.
    """
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

def github_unreachable() -> bool:
    """True while `github_request`'s transport cool-off is open — the call a caller just lost was
    the network being down, not GitHub refusing it. Callers use this to keep an outage from being
    reported as the permission failure it looks like (issue #767).
    """
    return time.monotonic() < _unreachable_until


def _is_transport_failure(exc: BaseException) -> bool:
    """Whether the request never reached GitHub. Every `requests` transport exception
    (ConnectionError, Timeout, SSLError, …) subclasses `RequestException` -> `IOError`, as do the
    builtin socket errors — an unreachable host is exactly the OSError family, anything else is
    ours. The deterministic `requests` faults share that base but are ours too, so they are named
    out by class rather than imported (this module keeps `requests` a lazy import).
    """
    if any(base.__name__ in NON_TRANSPORT_REQUEST_ERRORS for base in type(exc).__mro__):
        return False
    return isinstance(exc, OSError)


def _log_lost_write(message: str) -> None:
    """A refused WRITE breaks the loop and is an ERROR — the operator has to fix the token. But when
    the transport is down (issue #767) that hint is a lie: nothing was refused, the call never left
    the host, and the repair pass re-attaches on the next beat.
    """
    if github_unreachable():
        log_warning(f"{message} — GitHub is unreachable from this host; the repair pass re-attaches",
                    api_provider="github")
    else:
        log_error(f"{message}. {TOKEN_PERMISSION_HINT}", api_provider="github")


def github_request(method: str, path: str, payload: dict = None) -> Optional[object]:
    """One GitHub REST call, shared with the shipped-fix notifier (issue #502). Returns the parsed
    body (a dict, or a list for collection endpoints), or None when unconfigured/failed — filing is
    best-effort by design: a GitHub outage must never lose the feedback row. `path` may carry its
    own query string.
    """
    global _unreachable_until
    token = github_token()
    if not token:
        log_warning("Feedback issue filing skipped — no FEEDBACK_GITHUB_TOKEN/GITHUB_TOKEN set",
                    api_provider="github")
        return None
    if github_unreachable():
        log_debug(f"GitHub {method} {path} skipped — transport still in the unreachable cool-off",
                  api_provider="github")
        return None
    import requests
    url = f"{GITHUB_API_BASE}/repos/{github_repo()}/{path.lstrip('/')}"
    attempts = (max(1, GITHUB_RETRY_ATTEMPTS)
                if str(method).upper() in GITHUB_IDEMPOTENT_METHODS else 1)
    response = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(
                method, url, json=payload, timeout=GITHUB_TIMEOUT_SECONDS,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
            break
        except Exception as e:
            if not _is_transport_failure(e):
                log_error(f"GitHub {method} {path} failed", exc=e, api_provider="github")
                return None
            if attempt < attempts:
                log_debug(f"GitHub {method} {path} transport attempt {attempt} failed — retrying",
                          api_provider="github")
                time.sleep(GITHUB_RETRY_BACKOFF_SECONDS * attempt)
                continue
            _unreachable_until = time.monotonic() + GITHUB_UNREACHABLE_COOLDOWN_SECONDS
            log_warning(f"GitHub {method} {path} unreachable after {attempts} "
                        f"attempt(s) — the next pass retries", exc=e, api_provider="github")
            return None
    _unreachable_until = 0.0
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
    auto-filed issue landed label-less and the pipeline never picked it up. A failed attach never
    loses the issue — the row is already on GitHub by then — but it is an ERROR, not a warning
    (issue #718): a label-less issue is invisible to the agent pipeline until a human hand-labels
    it, so the loop is BROKEN, and only an error reaches PostHog at the default threshold.
    `repair_auto_filed_issues` re-attaches what a failed pass dropped.
    """
    data = github_request("POST", "issues", {"title": title, "body": body})
    number = data.get("number") if isinstance(data, dict) else None
    if number is None:
        return None
    number = int(number)
    label_list = [str(label) for label in (labels or [])]
    if label_list and github_request("POST", f"issues/{number}/labels",
                                     {"labels": label_list}) is None:
        _log_lost_write(f"Auto-filed issue #{number} could not be labeled {label_list} — it is "
                        f"invisible to the agent pipeline until this is repaired")
    assignee_list = [str(assignee) for assignee in (assignees or [])]
    if assignee_list and github_request("POST", f"issues/{number}/assignees",
                                        {"assignees": assignee_list}) is None:
        _log_lost_write(f"Auto-filed issue #{number} could not be assigned to {assignee_list}")
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
                        clusters: list = None, admin_approved: bool = False) -> dict:
    """Take ONE captured feedback row all the way to its outcome (see `IssueAction`). Never raises.

    `classification` / `clusters` are injectable so a batch pass classifies once and loads the open
    clusters once. When a new issue IS filed, the caller should append the returned `cluster` to its
    in-memory cluster list so two identical reports in the same batch don't both file.

    `admin_approved` (issue #1036) marks the ONE path that is not unattended: an admin clicked
    Approve on this specific row in the triage panel. Two of the guards below exist purely because
    the batch pass runs with nobody watching, and re-applying them to an explicit human decision is
    what made the button look dead — the row was left in `new`, so the panel re-rendered it
    unchanged.
    """
    row = feedback or {}
    feedback_id = row.get("id")
    body = row.get("body") or ""
    if not body.strip():
        # Terminal, so it must SETTLE. An empty body can never become an issue, and leaving it `new`
        # both re-offers it to every later pass and, on the panel, makes Approve a no-op (#1036).
        if feedback_id is not None:
            update_feedback_triage(feedback_id, status=FeedbackStatus.DISMISSED)
        return _result(IssueAction.DROPPED, feedback_id, reason="empty body")
    user_id = row.get("user_id")

    # Abuse guard FIRST: one user cannot turn the backlog into their personal firehose. Checked
    # before the LLM so a held row costs nothing, and the row's status is left alone so a later pass
    # (fresh 24h window) picks it up — held, never dropped.
    #
    # An admin approving ONE row by hand is the opposite of a firehose — they have already read it
    # and decided — so the cap does not apply. It also cannot be recovered from on that path: a
    # rate-limited approve leaves a NON-admin row in `new`, and `process_new_feedback` only ever
    # drains `admin_only=True`, so "held for a later pass" would mean held forever (#1036).
    if user_id is not None and not admin_approved:
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
    if classification.route == FeedbackRoute.NEEDS_HUMAN and not admin_approved:
        update_feedback_triage(feedback_id, status=FeedbackStatus.TRIAGED)
        return _result(IssueAction.NEEDS_HUMAN, feedback_id,
                       confidence=classification.confidence)
    # NEEDS_HUMAN is "a person should look at this before it reaches the backlog" — and on the
    # approve path a person just did. Parking it back in the queue the admin is standing in is a
    # loop with no exit (#1036), so it files instead. The confidence still shapes the LABELS below:
    # a low-confidence item lands `needs-human` + assigned + with a Decision Comment, never
    # `agent:ready`, so nothing gets built unattended off a shaky classification.
    #
    # …but a LOW-confidence verdict and NO verdict are not the same thing. `errors` marks the
    # fail-safe result: the classifier never answered (proxy down, off-contract reply), so its
    # `summary` is the RAW report and its category/severity are placeholders. Filing that would
    # publish the raw feedback text this module promises never reaches GitHub, under a title made
    # of the reporter's first line — and `ISSUE_CREATED` is terminal, so a 30-second LiteLLM blip
    # would permanently spend the report on a content-free issue nobody can re-approve. Leave the
    # row in `new` and say so: retrying an approve costs the admin one click.
    if admin_approved and classification.errors:
        log_info(f"Feedback {feedback_id} approve could not file — the classifier never answered "
                 f"({'; '.join(classification.errors)[:200]})", user_id=user_id)
        return _result(IssueAction.ERROR, feedback_id, reason=CLASSIFIER_UNAVAILABLE_REASON)

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
    if not ready and not comment_on_issue(number, build_decision_comment(classification,
                                                                         reporter_count)):
        log_error(f"Auto-filed issue #{number} is held with no Decision Comment — the owner has "
                  f"nothing to answer. {TOKEN_PERMISSION_HINT}", api_provider="github")
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
    reports in one pass produces one issue and N-1 +1 comments.

    Issue #793: only rows from admin users are filed automatically. Non-admin feedback is left in
    `new` status so the admin triage panel can approve or dismiss it — the filter is applied by the
    query (`admin_only`), not here, so parked rows can never crowd admin rows out of `limit`.
    """
    rows = get_unprocessed_feedback(limit, admin_only=True)
    clusters = get_open_feedback_clusters()
    counts: dict = {}
    pending = count_pending_admin_review()
    if pending:
        counts["pending_review"] = pending
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
    side by the changelog/notify stage (issue #502).
    """
    clusters = get_open_feedback_clusters()
    # Unlike the filing pass, this one also reconsiders rows already parked in `triaged` — a report
    # a human never got to may well be the same problem as an issue filed since.
    # Issue #793: admin_only — non-admin feedback must not be silently clustered (and therefore
    # accepted) before an admin reviews it, and filtering in SQL keeps parked rows from consuming
    # the whole `limit` window pass after pass.
    rows = get_unprocessed_feedback(limit, statuses=(FeedbackStatus.NEW, FeedbackStatus.TRIAGED),
                                    admin_only=True)
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


# --- Repair pass (issue #718) --------------------------------------------------------------------
# `POST /issues` kept succeeding while `/labels`, `/assignees` and `/comments` all 403'd, so issues
# landed on GitHub with no `agent:ready`, no `priority:*`, no owner and no Decision Comment — and
# the agent pipeline, which selects on labels, could not see them at all. Nothing in the DB records
# the intended label set (recording it would need a schema change), but `build_issue_body` writes
# the classifier verdict into the body, so the set is recomputable from the issue GitHub already
# has — deterministically, with no second LLM call.


def parse_filed_issue(title: str, body: str) -> Optional[tuple]:
    """(classification, reporter_count) read back off an issue THIS loop filed, or None when the
    provenance line is missing or unreadable. Only the fields the label set depends on are
    reconstructed — `summary` stays empty because the repair never rewrites the body.

    Both lines are read relative to OUR provenance block, never as the first match anywhere in the
    body. Everything above that block is the `## Why` summary — model-written from user-supplied
    text — so a report whose summary quotes "reported by 5 distinct users" would otherwise dictate
    the demand its own issue is repaired with, and that is exactly the signal that decides whether a
    feature is built unattended.
    """
    text = body or ""
    marker = text.find(FILED_PROVENANCE_MARKER)
    provenance = _PROVENANCE_RE.search(text, max(marker, 0))
    if not provenance:
        return None
    try:
        category = FeedbackCategory(provenance.group("category"))
        severity = FeedbackSeverity(provenance.group("severity"))
        risk = FeedbackRisk(provenance.group("risk"))
        confidence = float(provenance.group("confidence"))
    except ValueError:
        return None
    # `build_issue_body` puts the demand line in the paragraph immediately BEFORE the provenance
    # block, so the LAST match in front of it is ours and anything a summary quoted is behind it.
    demand = list(_DEMAND_RE.finditer(text[:marker] if marker > 0 else text))
    # No demand line means no reporters PROVEN — 0, never 1: floored up, a first-of-its-kind feature
    # would repair itself into `agent:ready` on demand it never had.
    reporter_count = int(demand[-1].group("reporters")) if demand else 0
    named = _TITLE_RE.match((title or "").strip())
    classification = FeedbackClassification(
        category=category, severity=severity,
        component=named.group("component") if named else 'other',
        title=named.group("title") if named else (title or "").strip(),
        summary="", risk=risk, confidence=confidence)
    return classification, reporter_count


def issue_comment_bodies(issue_number: int) -> Optional[list]:
    """Every comment body on an issue, or None when the read failed (never [] on failure — "we could
    not look" must not read as "there is no Decision Comment").
    """
    data = github_request("GET", f"issues/{int(issue_number)}/comments"
                                 f"?per_page={REPAIR_COMMENT_PAGE_SIZE}")
    if not isinstance(data, list):
        return None
    return [str(comment.get("body") or "") for comment in data if isinstance(comment, dict)]


def repair_filed_issue(issue: dict) -> str:
    """Re-attach what a failed post-create pass dropped from ONE auto-filed issue (`RepairAction`).

    Only ever ADDS: the title/body are never rewritten (a human may have edited them by now) and no
    label is ever removed. `feedback-loop` is the tripwire — this loop is the only thing that
    attaches it, and it goes on in the same call as every other label, so its presence means that
    call had permission and whatever else is on the issue is a human's curation, not our loss.
    """
    if not isinstance(issue, dict):
        return RepairAction.SKIPPED
    number = int(issue.get("number") or 0)
    body = issue.get("body") or ""
    if not number or issue.get("state") == "closed" or FILED_PROVENANCE_MARKER not in body:
        return RepairAction.SKIPPED
    present = {label.get("name") if isinstance(label, dict) else str(label)
               for label in (issue.get("labels") or [])}
    if FEEDBACK_LABEL in present:
        return RepairAction.OK
    parsed = parse_filed_issue(issue.get("title") or "", body)
    if parsed is None:
        log_warning(f"Issue #{number} reads as auto-filed but its classifier line is unreadable — "
                    f"leaving it for a human", api_provider="github")
        return RepairAction.SKIPPED
    classification, reporter_count = parsed
    wanted = labels_for_issue(classification, reporter_count)
    missing = [label for label in wanted if label not in present]
    if github_request("POST", f"issues/{number}/labels", {"labels": missing}) is None:
        _log_lost_write(f"Could not repair auto-filed issue #{number} — attaching {missing} did not "
                        f"land, so it stays invisible to the pipeline")
        return RepairAction.ERROR
    if AGENT_READY_LABEL not in wanted:
        if not issue.get("assignees"):
            github_request("POST", f"issues/{number}/assignees",
                           {"assignees": [issue_assignee()]})
        comments = issue_comment_bodies(number)
        if comments is not None and not any(DECISION_COMMENT_MARKER in c for c in comments):
            comment_on_issue(number, build_decision_comment(classification, reporter_count))
    log_info(f"Repaired auto-filed issue #{number} — attached {missing}", api_provider="github")
    return RepairAction.REPAIRED


def repair_auto_filed_issues(limit: int = MAX_REPAIR_ISSUES) -> dict:
    """Re-attach the labels/assignee/Decision Comment on already-filed issues whose post-create
    calls failed (issue #718). Runs on every filing beat, so a broken window self-heals as soon as
    the token is fixed instead of waiting on a human to hand-label each issue.

    One GET per open cluster and nothing written for a healthy issue. It stops at the FIRST refused
    WRITE: a permission failure is deterministic, so if one repair is forbidden the next 49 are too,
    and hammering GitHub would only bury the one error that says why. A refused READ gets the same
    treatment after `REPAIR_READ_FAILURE_STOP` in a row — one unreadable issue is just a deleted
    issue, but a run of them is the same broken token and must not cost 50 errors every beat. Reads
    lost to an unreachable transport (issue #767) are neither: the pass defers with one warning.
    """
    if not github_token():
        return {"scanned": 0, "repaired": 0, "failed": 0}
    seen: set = set()
    scanned = repaired = failed = unreadable = 0
    for cluster in get_open_feedback_clusters(max(1, int(limit))):
        number = (cluster or {}).get("github_issue_number")
        if not number or int(number) in seen:
            continue
        seen.add(int(number))
        scanned += 1
        issue = github_request("GET", f"issues/{int(number)}")
        if not isinstance(issue, dict):
            failed += 1
            if github_unreachable():
                # Issue #767: the reads are failing because the network is, so the token hint below
                # would be a lie and the remaining GETs would only repeat it.
                log_warning("Feedback issue repair pass deferred — GitHub is unreachable from this "
                            "host; the next beat retries", api_provider="github")
                break
            unreadable += 1
            if unreadable >= REPAIR_READ_FAILURE_STOP:
                log_error(f"Feedback issue repair pass stopped — {unreadable} issue reads in a row "
                          f"failed, so the rest cannot be checked either. {TOKEN_PERMISSION_HINT}",
                          api_provider="github")
                break
            continue
        unreadable = 0
        action = repair_filed_issue(issue)
        if action == RepairAction.REPAIRED:
            repaired += 1
        elif action == RepairAction.ERROR:
            failed += 1
            break
    if repaired or failed:
        log_info(f"Feedback issue repair pass: {scanned} checked, {repaired} repaired, "
                 f"{failed} still broken", api_provider="github")
    return {"scanned": scanned, "repaired": repaired, "failed": failed}
