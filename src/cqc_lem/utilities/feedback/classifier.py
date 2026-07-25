"""LLM auto-classifier for captured feedback (issue #497) — stage 2 of the feedback->auto-work loop.

Stage 1 (issue #496) captures raw free text into the `feedback` table. This module turns one such
row into a triaged, label-mapped decision with ZERO human triage, using ONE `lem-medium` call:

    body ──► single LLM call (strict JSON) ──► schema validation ──► deterministic label mapping
                                                                 └─► route (auto-work/FAQ/drop/human)

Two halves, deliberately separated so the expensive half can be mocked and the cheap half is
exhaustively testable:
  * `classify_feedback()` — the one LLM call plus strict parsing. Non-deterministic, fails SAFE.
  * `labels_for()` / `route_for()` — pure functions. The model NEVER names a GitHub label; it only
    picks taxonomy values, and this module maps those to labels that actually exist in the repo.

Fails SAFE, not open and not closed: any bad/unparseable/errored answer becomes a low-confidence
result routed to NEEDS_HUMAN. Feedback is never silently dropped because the classifier hiccuped —
only an explicit, confident `noise` verdict drops.

Env:
  FEEDBACK_CLASSIFIER_MODEL           — model alias for the call (default: lem-medium)
  FEEDBACK_CLASSIFIER_MIN_CONFIDENCE  — below this the item goes to human triage (default: 0.6)
"""

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

from cqc_lem.utilities.logger import log_debug, log_warning

DEFAULT_MODEL = "lem-medium"
DEFAULT_MIN_CONFIDENCE = 0.6

# Everything sent to / accepted from the model is bounded — feedback bodies are user-supplied and a
# runaway "summary" would land unbounded in an issue body.
MAX_BODY_CHARS = 4000
MAX_TITLE_CHARS = 80
MAX_SUMMARY_CHARS = 500
MAX_DUPLICATE_CANDIDATES = 25


class FeedbackCategory(StrEnum):
    """What KIND of work (if any) a piece of feedback implies."""
    BUG = 'bug'                  # something is broken / behaves wrong
    FEATURE = 'feature'          # something that does not exist yet
    ENHANCEMENT = 'enhancement'  # an existing thing should work better ("update")
    CLEANUP = 'cleanup'          # tech debt, confusing copy/UX, deprecation
    QUESTION = 'question'        # a support question — answerable, not buildable
    NOISE = 'noise'              # praise, spam, tests, empty — no work item


class FeedbackSeverity(StrEnum):
    """How badly it hurts. Maps 1:1 onto the repo's priority:* labels."""
    CRITICAL = 'critical'  # data loss, security, or the product is unusable
    HIGH = 'high'          # a core flow is broken/blocked with no workaround
    MEDIUM = 'medium'      # painful but has a workaround
    LOW = 'low'            # cosmetic / nice-to-have


class FeedbackRisk(StrEnum):
    """Why a human may need to look before this ships. Maps onto the repo's risk:* hold labels."""
    NONE = 'none'
    PRODUCT_DECISION = 'product-decision'  # needs a product/policy call
    LIVE_LINKEDIN = 'live-linkedin'        # needs live LinkedIn/Selenium validation
    MIGRATION = 'migration'                # implies a DB schema change
    SECURITY = 'security'                  # security/privacy sensitive


class FeedbackRoute(StrEnum):
    """Where the classified item goes next."""
    AUTO_WORK = 'auto_work'      # clustered + auto-opened as a GitHub issue
    FAQ = 'faq'                  # answered by the FAQ service, no issue
    DROP = 'drop'                # dismissed, no work
    NEEDS_HUMAN = 'needs_human'  # low confidence / unusable answer — human triage queue


# The product areas the classifier is allowed to name. A closed menu keeps `component` groupable
# (the clustering step buckets on it) instead of a free-text field with 40 spellings of "feed".
COMPONENTS = (
    'feed-commenting', 'replies', 'dms', 'connections', 'content-generation', 'scheduling',
    'newsletter', 'carousels', 'video', 'analytics', 'billing', 'auth', 'onboarding',
    'ui', 'api', 'infra', 'other',
)

# --- Taxonomy -> REAL repo labels ---------------------------------------------------------------
# Every value below must exist in christopherqueenconsulting/linkedin_engagement_manager's label
# set. NOISE has no label on purpose (it is dropped, never filed).
CATEGORY_LABELS: dict[FeedbackCategory, str] = {
    FeedbackCategory.BUG: 'bug',
    FeedbackCategory.FEATURE: 'feature',
    FeedbackCategory.ENHANCEMENT: 'enhancement',
    FeedbackCategory.CLEANUP: 'cleanup',
    FeedbackCategory.QUESTION: 'question',
}

SEVERITY_LABELS: dict[FeedbackSeverity, str] = {
    FeedbackSeverity.CRITICAL: 'priority:critical',
    FeedbackSeverity.HIGH: 'priority:high',
    FeedbackSeverity.MEDIUM: 'priority:medium',
    FeedbackSeverity.LOW: 'priority:low',
}

RISK_LABELS: dict[FeedbackRisk, str] = {
    FeedbackRisk.PRODUCT_DECISION: 'risk:product-decision',
    FeedbackRisk.LIVE_LINKEDIN: 'risk:live-linkedin',
    FeedbackRisk.MIGRATION: 'risk:migration',
    FeedbackRisk.SECURITY: 'risk:security',
}

NEEDS_HUMAN_LABEL = 'needs-human'

# Synonyms models reach for (and the widget's own type_hint values) folded onto the taxonomy.
_CATEGORY_ALIASES: dict[str, FeedbackCategory] = {
    'fix': FeedbackCategory.BUG, 'defect': FeedbackCategory.BUG, 'error': FeedbackCategory.BUG,
    'broken': FeedbackCategory.BUG, 'regression': FeedbackCategory.BUG,
    'feature_request': FeedbackCategory.FEATURE, 'feature-request': FeedbackCategory.FEATURE,
    'idea': FeedbackCategory.FEATURE, 'request': FeedbackCategory.FEATURE,
    'new': FeedbackCategory.FEATURE,
    'update': FeedbackCategory.ENHANCEMENT, 'improvement': FeedbackCategory.ENHANCEMENT,
    'improve': FeedbackCategory.ENHANCEMENT, 'ux': FeedbackCategory.ENHANCEMENT,
    'chore': FeedbackCategory.CLEANUP, 'refactor': FeedbackCategory.CLEANUP,
    'tech-debt': FeedbackCategory.CLEANUP, 'debt': FeedbackCategory.CLEANUP,
    'confusing': FeedbackCategory.CLEANUP,
    'support': FeedbackCategory.QUESTION, 'help': FeedbackCategory.QUESTION,
    'praise': FeedbackCategory.NOISE, 'spam': FeedbackCategory.NOISE,
    'test': FeedbackCategory.NOISE, 'none': FeedbackCategory.NOISE,
}

_SEVERITY_ALIASES: dict[str, FeedbackSeverity] = {
    'blocker': FeedbackSeverity.CRITICAL, 'urgent': FeedbackSeverity.CRITICAL,
    'p0': FeedbackSeverity.CRITICAL, 'sev1': FeedbackSeverity.CRITICAL,
    'major': FeedbackSeverity.HIGH, 'p1': FeedbackSeverity.HIGH,
    'moderate': FeedbackSeverity.MEDIUM, 'normal': FeedbackSeverity.MEDIUM,
    'p2': FeedbackSeverity.MEDIUM,
    'minor': FeedbackSeverity.LOW, 'trivial': FeedbackSeverity.LOW,
    'cosmetic': FeedbackSeverity.LOW, 'p3': FeedbackSeverity.LOW,
}

# Keys are hyphenated/lowercased the same way `_normalize` folds the model's answer.
_RISK_ALIASES: dict[str, FeedbackRisk] = {
    '': FeedbackRisk.NONE, 'null': FeedbackRisk.NONE, 'no': FeedbackRisk.NONE,
    'product': FeedbackRisk.PRODUCT_DECISION, 'policy': FeedbackRisk.PRODUCT_DECISION,
    'linkedin': FeedbackRisk.LIVE_LINKEDIN, 'selenium': FeedbackRisk.LIVE_LINKEDIN,
    'db': FeedbackRisk.MIGRATION, 'schema': FeedbackRisk.MIGRATION,
    'privacy': FeedbackRisk.SECURITY, 'auth': FeedbackRisk.SECURITY,
}

# --- Output contract -----------------------------------------------------------------------------
# Handed to the model verbatim AND enforced on the way back by `validate_classification()`, so the
# thing we prompt for and the thing we accept can never drift apart.
FEEDBACK_CLASSIFICATION_SCHEMA: dict = {
    "type": "object",
    "required": ["category", "severity", "component", "title", "summary", "risk",
                 "duplicate_of", "confidence"],
    "properties": {
        "category": {"type": "string", "enum": [c.value for c in FeedbackCategory]},
        "severity": {"type": "string", "enum": [s.value for s in FeedbackSeverity]},
        "component": {"type": "string", "enum": list(COMPONENTS)},
        "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
        "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS},
        "risk": {"type": "string", "enum": [r.value for r in FeedbackRisk]},
        "duplicate_of": {"type": ["integer", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def validate_classification(data: object) -> list[str]:
    """Validate `data` against FEEDBACK_CLASSIFICATION_SCHEMA, returning a list of human-readable
    errors (empty == valid). Supports the JSON Schema subset the contract above uses — object /
    required / properties / type (incl. union lists) / enum / maxLength / minimum / maximum — so
    the schema stays the single source of truth without pulling in a runtime dependency."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in FEEDBACK_CLASSIFICATION_SCHEMA["required"]:
        if key not in data:
            errors.append(f"missing required field '{key}'")
    for key, rule in FEEDBACK_CLASSIFICATION_SCHEMA["properties"].items():
        if key not in data:
            continue
        errors.extend(f"{key}: {e}" for e in _validate_value(data[key], rule))
    return errors


_JSON_TYPES: dict[str, tuple] = {
    "object": (dict,), "array": (list,), "string": (str,),
    "integer": (int,), "number": (int, float), "boolean": (bool,),
}


def _validate_value(value: object, rule: dict) -> list[str]:
    errors: list[str] = []
    types = rule.get("type")
    types = [types] if isinstance(types, str) else list(types or [])
    if types:
        # bool is an int subclass in Python; never let True satisfy an integer/number field.
        ok = any(value is None if t == "null"
                 else isinstance(value, _JSON_TYPES.get(t, ()))
                 and not (isinstance(value, bool) and t in ("integer", "number"))
                 for t in types)
        if not ok:
            errors.append(f"expected type {'|'.join(types)}, got {type(value).__name__}")
            return errors
    if "enum" in rule and value not in rule["enum"]:
        errors.append(f"'{value}' is not one of {rule['enum']}")
    if "maxLength" in rule and isinstance(value, str) and len(value) > rule["maxLength"]:
        errors.append(f"longer than {rule['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            errors.append(f"below minimum {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            errors.append(f"above maximum {rule['maximum']}")
    return errors


@dataclass(frozen=True)
class FeedbackClassification:
    """One classified feedback item. `labels` and `route` are derived deterministically from the
    taxonomy fields — the model does not choose them."""
    category: FeedbackCategory
    severity: FeedbackSeverity
    component: str
    title: str
    summary: str
    risk: FeedbackRisk = FeedbackRisk.NONE
    duplicate_of: Optional[int] = None
    confidence: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def route(self) -> FeedbackRoute:
        return route_for(self.category, self.confidence)

    @property
    def labels(self) -> list[str]:
        return labels_for(self.category, self.severity, self.risk, self.route)

    @property
    def needs_human(self) -> bool:
        return self.route == FeedbackRoute.NEEDS_HUMAN

    def to_dict(self) -> dict:
        return {
            "category": str(self.category),
            "severity": str(self.severity),
            "component": self.component,
            "title": self.title,
            "summary": self.summary,
            "risk": str(self.risk),
            "duplicate_of": self.duplicate_of,
            "confidence": self.confidence,
            "route": str(self.route),
            "labels": self.labels,
        }


def min_confidence() -> float:
    """Confidence floor for acting without a human, read at call time (the live-env pattern used by
    POST_SIMILARITY_MAX) so it can be tuned per-deploy without a restart."""
    raw = (os.environ.get("FEEDBACK_CLASSIFIER_MIN_CONFIDENCE") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_MIN_CONFIDENCE
    except ValueError:
        return DEFAULT_MIN_CONFIDENCE
    return max(0.0, min(1.0, value))


def classifier_model() -> str:
    return (os.environ.get("FEEDBACK_CLASSIFIER_MODEL") or "").strip() or DEFAULT_MODEL


def route_for(category: FeedbackCategory, confidence: float) -> FeedbackRoute:
    """Deterministic routing. An unsure verdict ALWAYS wins over the category — we would rather put
    a human on a praise message than silently drop a real bug the model misread."""
    if confidence < min_confidence():
        return FeedbackRoute.NEEDS_HUMAN
    if category == FeedbackCategory.NOISE:
        return FeedbackRoute.DROP
    if category == FeedbackCategory.QUESTION:
        return FeedbackRoute.FAQ
    return FeedbackRoute.AUTO_WORK


def labels_for(category: FeedbackCategory, severity: FeedbackSeverity,
               risk: FeedbackRisk = FeedbackRisk.NONE,
               route: Optional[FeedbackRoute] = None) -> list[str]:
    """Map the taxonomy onto labels that exist in the repo. Dropped items get none; questions get
    only `question` (they never become a prioritized work item); human-triage items carry
    `needs-human` so the escalation queue is one label filter."""
    route = route if route is not None else FeedbackRoute.AUTO_WORK
    if route == FeedbackRoute.DROP:
        return []
    labels: list[str] = []
    category_label = CATEGORY_LABELS.get(category)
    if category_label:
        labels.append(category_label)
    if route == FeedbackRoute.FAQ:
        return labels
    labels.append(SEVERITY_LABELS[severity])
    risk_label = RISK_LABELS.get(risk)
    if risk_label:
        labels.append(risk_label)
    if route == FeedbackRoute.NEEDS_HUMAN:
        labels.append(NEEDS_HUMAN_LABEL)
    return labels


_SYSTEM_PROMPT = (
    "You are the triage engineer for LinkedIn Engagement Manager (LEM), a SaaS that automates "
    "LinkedIn engagement: AI content generation and scheduling, feed commenting, replies, DMs and "
    "follow-ups, newsletters, carousels/video, analytics, and billing.\n"
    "You are given ONE piece of user feedback. Classify it so it can be worked with no human triage.\n\n"
    "Rules:\n"
    "- category: 'bug' (something behaves wrong), 'feature' (does not exist yet), 'enhancement' "
    "(exists but should work better), 'cleanup' (tech debt, confusing copy/UX), 'question' (a "
    "support question that an answer resolves — no code change), 'noise' (praise, spam, test "
    "submissions, or empty content).\n"
    "- severity: 'critical' (data loss, security, product unusable), 'high' (core flow blocked, no "
    "workaround), 'medium' (painful, has a workaround), 'low' (cosmetic or nice-to-have). Use 'low' "
    "for question/noise.\n"
    "- risk: 'security' if it touches auth/privacy/secrets; 'migration' if it needs a DB schema "
    "change; 'live-linkedin' if it can only be validated against a live LinkedIn session; "
    "'product-decision' if it needs a product or policy call; otherwise 'none'.\n"
    "- title: an imperative GitHub issue title, at most 80 characters, no trailing period.\n"
    "- summary: at most 500 characters, factual, only what the user actually said. Never invent "
    "reproduction steps, versions, or numbers that are not in the feedback.\n"
    "- duplicate_of: the id of an existing item below that reports the SAME underlying problem, "
    "else null. Only use ids that were given to you.\n"
    "- confidence: 0.0-1.0, how sure you are of category and severity. Be honest — vague, "
    "truncated, or off-topic feedback deserves a LOW confidence so a human reviews it.\n"
    "- The user's own type hint is a hint only; the text wins if they disagree.\n\n"
    "Respond with ONLY a compact JSON object, no prose and no code fences, matching this schema:\n"
)


def _as_int(value: object) -> Optional[int]:
    """int(value) or None — feedback ids arrive from callers/models as ints, digit strings, or junk."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _build_user_prompt(body: str, type_hint: Optional[str], context: Optional[dict],
                       duplicate_candidates: Optional[list[dict]]) -> str:
    parts = []
    if type_hint:
        parts.append(f"User's type hint: {str(type_hint)[:32]}")
    if context:
        # Only the fields that help triage — never the screenshot blob or anything unbounded.
        wanted = {k: context.get(k) for k in ("route", "app_version", "user_agent", "viewport")
                  if context.get(k)}
        if wanted:
            parts.append("Client context: " + json.dumps(wanted)[:500])
    if duplicate_candidates:
        listed = []
        for candidate in duplicate_candidates[:MAX_DUPLICATE_CANDIDATES]:
            candidate_id = _as_int(candidate.get("id"))
            text = str(candidate.get("title") or candidate.get("body") or "").strip()
            if candidate_id is not None and text:
                listed.append(f"- {candidate_id}: {text[:160]}")
        if listed:
            parts.append("Existing feedback items (for duplicate_of):\n" + "\n".join(listed))
    parts.append(f"Feedback:\n{body}")
    return "\n\n".join(parts)


def _normalize(data: dict, candidate_ids: set[int]) -> dict:
    """Fold aliases/casing/None into the taxonomy and clamp lengths BEFORE schema validation, so a
    model that answers 'Fix' / 'P1' / 'DB' is accepted while genuinely unusable output still fails."""
    out = dict(data)

    raw_category = str(out.get("category") or "").strip().lower()
    category = _CATEGORY_ALIASES.get(raw_category)
    out["category"] = str(category) if category else raw_category

    raw_severity = str(out.get("severity") or "").strip().lower()
    severity = _SEVERITY_ALIASES.get(raw_severity)
    out["severity"] = str(severity) if severity else raw_severity

    raw_risk = str(out.get("risk") or "").strip().lower().removeprefix("risk:").replace("_", "-")
    risk = _RISK_ALIASES.get(raw_risk)
    out["risk"] = str(risk) if risk else raw_risk

    component = str(out.get("component") or "").strip().lower().replace("_", "-")
    out["component"] = component if component in COMPONENTS else 'other'

    out["title"] = str(out.get("title") or "").strip()[:MAX_TITLE_CHARS]
    out["summary"] = str(out.get("summary") or "").strip()[:MAX_SUMMARY_CHARS]

    duplicate_of = _as_int(out.get("duplicate_of"))
    # A duplicate id the model invented is worse than none — it would merge unrelated reports.
    out["duplicate_of"] = duplicate_of if duplicate_of in candidate_ids else None

    try:
        confidence = float(out.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    out["confidence"] = max(0.0, min(1.0, confidence))
    return out


def parse_classification(raw_text: str, candidate_ids: Optional[set[int]] = None
                         ) -> tuple[Optional[dict], list[str]]:
    """Parse the model's reply into a schema-valid dict. Returns (data, errors) — data is None when
    the reply is not usable. Tolerates ```json fences and prose around the object."""
    match = re.search(r"\{.*\}", raw_text or "", re.DOTALL)
    if not match:
        return None, ["no JSON object in the response"]
    try:
        data = json.loads(match.group(0))
    except ValueError as e:
        return None, [f"invalid JSON: {e}"]
    normalized = _normalize(data, candidate_ids or set())
    errors = validate_classification(normalized)
    if errors:
        return None, errors
    return normalized, []


def _from_dict(data: dict) -> FeedbackClassification:
    return FeedbackClassification(
        category=FeedbackCategory(data["category"]),
        severity=FeedbackSeverity(data["severity"]),
        component=data["component"],
        title=data["title"],
        summary=data["summary"],
        risk=FeedbackRisk(data["risk"]),
        duplicate_of=data["duplicate_of"],
        confidence=data["confidence"],
    )


def _unclassified(body: str, type_hint: Optional[str], errors: list[str]) -> FeedbackClassification:
    """The fail-SAFE result: confidence 0.0, which always routes to NEEDS_HUMAN. The type hint is
    the only thing we trust here, purely so the triage queue is sortable."""
    hint = str(type_hint or "").strip().lower()
    category = _CATEGORY_ALIASES.get(hint)
    if category is None:
        try:
            category = FeedbackCategory(hint)
        except ValueError:
            category = FeedbackCategory.BUG
    return FeedbackClassification(
        category=category,
        severity=FeedbackSeverity.MEDIUM,
        component='other',
        title=(body or "").strip().splitlines()[0][:MAX_TITLE_CHARS] if (body or "").strip() else "",
        summary=(body or "").strip()[:MAX_SUMMARY_CHARS],
        risk=FeedbackRisk.NONE,
        duplicate_of=None,
        confidence=0.0,
        errors=errors,
    )


def classify_feedback(body: str, type_hint: Optional[str] = None, context: Optional[dict] = None,
                      duplicate_candidates: Optional[list[dict]] = None,
                      user_id: Optional[int] = None) -> FeedbackClassification:
    """Classify ONE feedback item with a single `lem-medium` call.

    `duplicate_candidates` is an optional list of {'id': int, 'title'|'body': str} the model may
    point `duplicate_of` at; anything else it names is discarded. NEVER raises — an empty body, a
    failed call, or an off-contract answer all come back as a confidence-0.0 result whose route is
    NEEDS_HUMAN."""
    text = (body or "").strip()[:MAX_BODY_CHARS]
    if not text:
        return _unclassified(text, type_hint, ["empty feedback body"])

    candidate_ids = {cid for cid in (_as_int(c.get("id")) for c in (duplicate_candidates or []))
                     if cid is not None}
    model = classifier_model()
    try:
        # Lazy import keeps this module importable (and unit-testable) without the AI stack loaded.
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        response = _call_llm(
            model=model,
            messages=[
                {"role": "system",
                 "content": _SYSTEM_PROMPT + json.dumps(FEEDBACK_CLASSIFICATION_SCHEMA)},
                {"role": "user",
                 "content": _build_user_prompt(text, type_hint, context, duplicate_candidates)},
            ],
            temperature=0,
            _track_user_id=user_id,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        log_warning("Feedback classification call failed — routing to human triage", exc=e,
                    ai_model=model, user_id=user_id)
        return _unclassified(text, type_hint, [f"llm call failed: {e}"])

    data, errors = parse_classification(raw, candidate_ids)
    if data is None:
        log_warning("Feedback classification was off-contract — routing to human triage",
                    ai_model=model, user_id=user_id)
        return _unclassified(text, type_hint, errors)

    result = _from_dict(data)
    log_debug(f"Classified feedback as {result.category}/{result.severity} "
              f"({result.confidence:.2f}) -> {result.route}", ai_model=model, user_id=user_id)
    return result
