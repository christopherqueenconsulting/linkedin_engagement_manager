"""Auto-maintaining public FAQ — issue #507, the support half of the feedback→auto-work loop.

The filer (#498) turns feedback that implies WORK into issues and parks everything else in
`triaged`. What is left there is the support queue: FAQ-routed questions (classifier
`category=question`) and public review free-text. This module answers it with no human triage:

    triaged, unclustered feedback ──► question-shaped? ──► cluster (embedding, then token overlap)
                                                              ├─ matches a published entry → reply, resolve
                                                              ├─ under the recurrence threshold → wait
                                                              └─ recurring ──► ONE `lem-medium` answer
                                                                                ├─ guardrails fail → held
                                                                                └─ upsert entry + reply askers

Three properties make this safe to run unattended:

* **Cost is gated on recurrence, not volume.** No answer is generated until the SAME question has
  been asked `FAQ_RECURRENCE_MIN` times, and a pass writes at most `FAQ_MAX_ANSWERS_PER_PASS`
  answers. Matching an existing entry costs nothing at all.
* **Nothing is fabricated.** The answer is generated against a closed grounding corpus (the
  published FAQ + the product docs named by `FAQ_GROUNDING_DOCS`); any number in the answer that is
  not in that corpus, any profanity, any personal data, and any product/policy/ToS claim holds the
  answer instead of publishing it — the hold becomes a `needs-human` + `risk:product-decision`
  GitHub issue carrying a letter-pickable Decision Comment.
* **Every write is revertible.** Each state an entry is put into is appended to
  `faq_entry_versions`, so an auto-revision of a published answer can be rolled back
  (`revert_faq_answer`).

New entries land as `draft` (issue #506's contract: an auto-generated answer is not published until
reviewed) unless `FAQ_AUTO_PUBLISH` is set; a revision of an ALREADY published entry stays published,
which is what "auto-maintains the FAQ" means in practice. Askers are always answered by email —
that reply is direct support, not public copy.

Privacy: only the PII-redacted, normalized question (never the raw feedback body and never a
reporter identity) reaches the model, GitHub, or the public page.

Env:
  FAQ_MODEL                  — model alias for the answer call (default: lem-medium)
  FAQ_RECURRENCE_MIN         — asks before an answer is written (default: 3)
  FAQ_CLUSTER_SIMILARITY     — incoming↔incoming "same question" threshold (default: 0.78)
  FAQ_ENTRY_SIMILARITY       — incoming↔existing-entry match threshold (default: 0.62)
  FAQ_MAX_ANSWERS_PER_PASS   — answer generations per pass (default: 3)
  FAQ_GROUNDING_DOCS         — comma-separated repo-relative docs the answer may draw on
  FAQ_AUTO_PUBLISH           — publish new answers immediately instead of `draft` (default: off)
  FAQ_AUTO_REPLY             — email askers their answer (default: on)
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

from cqc_lem.utilities.ai.content_framework import text_similarity
from cqc_lem.utilities.db import (
    FaqStatus, FeedbackStatus, apply_faq_entry_version, get_faq_candidate_feedback,
    get_faq_entries, get_faq_entry_versions, record_faq_entry_version, update_feedback_triage,
    upsert_faq_entry,
)
# Same-package internals reused on purpose: one env-parsing rule, one similarity measure, one
# GitHub client and one JSON extractor for the whole feedback loop.
from cqc_lem.utilities.feedback.classifier import (
    NEEDS_HUMAN_LABEL, RISK_LABELS, FeedbackRisk, _json_object_candidates,
)
from cqc_lem.utilities.feedback.issue_service import (
    FEEDBACK_LABEL, PLAN_DOC, _env_float, _env_int, comment_on_issue, create_github_issue,
    embed_text, issue_assignee, similarity,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning

DEFAULT_MODEL = "lem-medium"
RECURRENCE_MIN_DEFAULT = 3
CLUSTER_SIMILARITY_DEFAULT = 0.78
ENTRY_SIMILARITY_DEFAULT = 0.62
MAX_ANSWERS_PER_PASS_DEFAULT = 3
DEFAULT_GROUNDING_DOCS = "docs/LANDING_PAGE_UPDATE.md"

MAX_QUESTION_CHARS = 300
MAX_ANSWER_CHARS = 1200
MAX_GROUNDING_CHARS = 8000
MAX_ENTRIES = 200
# An auto-written entry sorts BELOW every hand-curated seed (10..60) on the landing page.
AUTO_SORT_ORDER = 1000
# A regenerated answer this close to the stored one is not worth a new version.
REWRITE_SIMILARITY_MAX = 0.95

QUESTION_LABEL = 'question'
PRODUCT_DECISION_LABEL = RISK_LABELS[FeedbackRisk.PRODUCT_DECISION]


class FaqAction:
    """What `answer_question_cluster` did — the contract callers/tests assert on."""
    PUBLISHED = 'published'    # a new entry was written for a recurring question
    REVISED = 'revised'        # an existing entry's answer was rewritten (old version kept)
    ANSWERED = 'answered'      # an existing entry already covers it — askers replied to, no spend
    PENDING = 'pending'        # not asked often enough yet (or out of budget) — waits for more
    HELD = 'held'              # policy/ToS/guardrail hold — a human owns it, nothing published
    ERROR = 'error'            # the entry could not be written; retried next pass


# Mirrors the tutorial publish guardrail's list (`marketing/video_tutorials.py`). Duplicated rather
# than imported: that module pulls Selenium in at import time, and this one must stay light.
_PROFANITY = frozenset({
    "shit", "shitty", "fuck", "fucking", "fucked", "bullshit", "asshole", "bastard", "bitch",
    "dick", "damn", "goddamn", "crap", "piss", "cunt", "twat", "wanker",
})

# A claim we must not invent on a public page. Pricing/refund/legal/roadmap wording all imply a
# commitment only the owner can make, so the answer is held even when it passed every other check.
_POLICY_PATTERNS: tuple = (
    r"terms of service", r"\btos\b", r"user agreement", r"\blegal\b", r"lawsuit", r"liability",
    r"privacy policy", r"\bgdpr\b", r"\bccpa\b", r"\bhipaa\b", r"\bcompliance\b", r"\bcontract\b",
    r"\bsla\b", r"guarantee", r"\brefund", r"money.back", r"\bprice\b", r"\bpricing\b",
    r"\bcosts?\b", r"\bdiscount", r"\bbilling\b", r"\binvoice", r"\bplan\b", r"\btier\b",
    r"\broadmap\b", r"\beta\b", r"release date", r"when will (?:you|it|this)", r"\bban(?:ned)?\b",
    r"suspend(?:ed)?", r"\bsue\b", r"\bcopyright", r"\btrademark",
)
_POLICY_RE = re.compile("|".join(_POLICY_PATTERNS), re.IGNORECASE)

# First words that make a sentence a question even without a '?'.
_QUESTION_STARTERS = frozenset({
    "how", "what", "why", "when", "where", "who", "which", "can", "could", "do", "does", "did",
    "is", "are", "was", "will", "would", "should", "any", "anyone", "anybody",
})

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SECRET_RE = re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b")
# 9+ digits with optional separators — phone numbers, card numbers, account ids.
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d(?:[\d\s().-]{7,17})\d(?![\w.])")
_LONG_DIGITS_RE = re.compile(r"(?<![\w.])\d{7,}(?![\w.])")
REDACTED = "[redacted]"

# A specific the answer is not allowed to invent: money, percentages, counts, durations.
_NUMERIC_CLAIM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*%?")

_SYSTEM_PROMPT = (
    "You write the public FAQ for LinkedIn Engagement Manager (LEM), a SaaS that automates LinkedIn "
    "engagement: AI content generation and scheduling, feed commenting, replies, DMs and follow-ups, "
    "newsletters, carousels/video and analytics.\n"
    "You are given ONE question real users keep asking, and the ONLY source material you may use.\n\n"
    "Rules:\n"
    "- Answer ONLY from the source material. If it does not contain the answer, set answerable to "
    "false and leave answer empty. Never guess, never invent a number, price, date, limit or "
    "policy.\n"
    "- policy_claim: true if answering it commits the company to a product, pricing, legal, "
    "privacy, refund, roadmap or LinkedIn-Terms-of-Service position. A human must approve those, "
    "so say so honestly even when the source material covers it.\n"
    "- question: the question rewritten as one clear, general FAQ question of at most "
    f"{MAX_QUESTION_CHARS} characters, ending in '?'. No names, emails or account details.\n"
    f"- answer: at most {MAX_ANSWER_CHARS} characters, plain text, second person, no marketing "
    "hype, no markdown headings.\n"
    "- confidence: 0.0-1.0 in the answer being correct and fully grounded.\n\n"
    "Respond with ONLY a compact JSON object, no prose and no code fences:\n"
    '{"question": str, "answer": str, "answerable": bool, "policy_claim": bool, '
    '"confidence": number}'
)


# --- Config -------------------------------------------------------------------------------------

def faq_model() -> str:
    return (os.environ.get("FAQ_MODEL") or "").strip() or DEFAULT_MODEL


def recurrence_min() -> int:
    return _env_int("FAQ_RECURRENCE_MIN", RECURRENCE_MIN_DEFAULT, 1, 100)


def cluster_similarity_min() -> float:
    return _env_float("FAQ_CLUSTER_SIMILARITY", CLUSTER_SIMILARITY_DEFAULT, 0.0, 1.0)


def entry_similarity_min() -> float:
    return _env_float("FAQ_ENTRY_SIMILARITY", ENTRY_SIMILARITY_DEFAULT, 0.0, 1.0)


def max_answers_per_pass() -> int:
    return _env_int("FAQ_MAX_ANSWERS_PER_PASS", MAX_ANSWERS_PER_PASS_DEFAULT, 0, 50)


def auto_publish() -> bool:
    return (os.environ.get("FAQ_AUTO_PUBLISH") or "").strip().lower() in ("1", "true", "yes", "on")


def auto_reply_enabled() -> bool:
    return (os.environ.get("FAQ_AUTO_REPLY") or "true").strip().lower() not in (
        "0", "false", "no", "off")


def grounding_docs() -> list:
    raw = (os.environ.get("FAQ_GROUNDING_DOCS") or "").strip() or DEFAULT_GROUNDING_DOCS
    return [part.strip() for part in raw.split(",") if part.strip()]


# --- Text hygiene -------------------------------------------------------------------------------

def redact_pii(text: Optional[str]) -> str:
    """Strip the personal data a support question tends to carry — the address someone signed up
    with, the phone number they left, an API key they pasted — BEFORE it reaches the model, GitHub
    or the public page. Order matters: emails and secrets first, so their digits can't be
    half-eaten by the phone pattern."""
    out = str(text or "")
    out = _EMAIL_RE.sub(REDACTED, out)
    out = _SECRET_RE.sub(REDACTED, out)
    out = _PHONE_RE.sub(REDACTED, out)
    return _LONG_DIGITS_RE.sub(REDACTED, out)


def contains_profanity(text: Optional[str]) -> list:
    words = {w.lower().strip(".,!?;:\"'()") for w in str(text or "").split()}
    return sorted(words & _PROFANITY)


def looks_like_question(text: Optional[str]) -> bool:
    """Free deterministic prefilter: does this feedback read like a question at all? Everything it
    rejects costs nothing — no embedding, no LLM call, and the row is left for the human queue."""
    body = str(text or "").strip()
    if not body:
        return False
    if "?" in body:
        return True
    first = re.sub(r"[^a-z]", "", body.split(maxsplit=1)[0].lower()) if body.split() else ""
    return first in _QUESTION_STARTERS


def normalize_question(text: Optional[str]) -> str:
    """The raw feedback body reduced to ONE publishable question: PII redacted, the question
    sentence isolated, whitespace collapsed, capitalized and '?'-terminated."""
    body = " ".join(redact_pii(text).split())
    if not body:
        return ""
    # The first sentence that actually asks something; a preamble ("Hi team. Does X work?") loses.
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", body) if s.strip()]
    question = next((s for s in sentences if "?" in s), None)
    if question is None:
        question = next((s for s in sentences if looks_like_question(s)), sentences[0])
    question = question.strip().rstrip(".!").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return ""
    if not question.endswith("?"):
        question += "?"
    return question[0].upper() + question[1:]


def implies_policy_claim(*texts: Optional[str]) -> Optional[str]:
    """The wording that makes an answer a commitment rather than a fact — returns the phrase that
    tripped it, or None. Deterministic on purpose: this hold must not depend on the model's own
    honesty about what it is claiming."""
    for text in texts:
        match = _POLICY_RE.search(str(text or ""))
        if match:
            return match.group(0).strip().lower()
    return None


def _numbers(text: Optional[str]) -> list:
    return [hit.strip().replace(" ", "").replace(",", "")
            for hit in _NUMERIC_CLAIM_RE.findall(str(text or ""))]


def ungrounded_numbers(answer: Optional[str], grounding: Optional[str]) -> list:
    """Numeric specifics in the answer that appear nowhere in the source material — the fabrication
    the FAQ can least afford ("$19/month", "up to 500 comments a day")."""
    allowed = set(_numbers(grounding))
    out: list = []
    for value in _numbers(answer):
        if value not in allowed and value not in out:
            out.append(value)
    return out


def check_answer(question: str, answer: str, grounding: str) -> Optional[str]:
    """Fail-closed publish guardrails, in order: something to say, inside the cap, clean, free of
    personal data, and free of invented specifics. Returns the hold reason, or None to publish."""
    text = str(answer or "").strip()
    if not text:
        return "the answer is empty"
    if len(text) > MAX_ANSWER_CHARS:
        return f"the answer is {len(text)} chars, over the {MAX_ANSWER_CHARS} cap"
    profane = contains_profanity(f"{question} {text}")
    if profane:
        return f"disallowed language: {', '.join(profane)}"
    if redact_pii(text) != text:
        return "the answer contains personal data"
    invented = ungrounded_numbers(text, grounding)
    if invented:
        return f"ungrounded specifics: {', '.join(invented[:5])}"
    return None


# --- Clustering ---------------------------------------------------------------------------------

def match_entry(question: str, entries: Optional[list]) -> tuple:
    """The existing FAQ entry that already asks this, and its score. Lexical only — an entry has no
    stored embedding, and FAQ questions are short and keyword-dense enough for token overlap."""
    best = None
    best_score = 0.0
    for entry in entries or []:
        score = text_similarity(question or "", str(entry.get("question") or ""))
        if score > best_score:
            best, best_score = entry, score
    if best is not None and best_score >= entry_similarity_min():
        return best, best_score
    return None, best_score


def cluster_questions(rows: Optional[list]) -> list:
    """Group question-shaped feedback rows into "the same question, asked N times".

    A cluster is keyed by its SEED row id — the same convention the filer uses for work clusters
    (`feedback.cluster_id` = the seed's id), so an FAQ cluster and an issue cluster can never be
    confused for one another. When embedding is unavailable the comparison degrades to token overlap
    rather than to "everything is new".

    Only the PII-redacted, normalized QUESTION is embedded and compared — never the raw body, which
    can still carry the email, phone number or API key the asker pasted in. That also means
    `feedback.embedding` is neither read nor written here: that column holds the filer's
    body-derived vector, and cosine between a question vector and a body vector is not a
    like-for-like score. The cost of re-embedding a still-pending question each pass is a bounded
    handful of `lem-embedding` calls."""
    threshold = cluster_similarity_min()
    clusters: list = []
    for row in rows or []:
        question = normalize_question(row.get("body"))
        if not question:
            continue
        vector = embed_text(question)
        match = None
        for cluster in clusters:
            if similarity(question, cluster["question"], vector, cluster["vector"]) >= threshold:
                match = cluster
                break
        if match:
            match["rows"].append(row)
            continue
        clusters.append({"cluster_id": row.get("id"), "question": question,
                         "vector": vector, "rows": [row]})
    return clusters


# --- Answer generation --------------------------------------------------------------------------

def _read_doc(path: str) -> str:
    """One grounding doc, read relative to the repo root. Unreadable/missing docs are simply not
    part of the corpus — a narrower corpus makes answers MORE conservative, never less."""
    try:
        root = Path(__file__).resolve().parents[4]
        candidate = (root / path).resolve()
        if not str(candidate).startswith(str(root)) or not candidate.is_file():
            return ""
        return candidate.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        log_warning(f"Could not read FAQ grounding doc {path}", exc=e)
        return ""


def build_grounding(entries: Optional[list] = None, docs: Optional[list] = None) -> str:
    """The closed corpus an answer may draw on: the FAQ we already stand behind, plus the product
    docs. Bounded — a corpus that does not fit is truncated, and anything outside it is unanswerable
    by construction."""
    parts: list = []
    for entry in entries or []:
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if question and answer:
            parts.append(f"Q: {question}\nA: {answer}")
    for path in (docs if docs is not None else grounding_docs()):
        text = _read_doc(path).strip()
        if text:
            parts.append(f"--- {path} ---\n{text}")
    return "\n\n".join(parts)[:MAX_GROUNDING_CHARS]


def _parse_answer(raw_text: Optional[str]) -> Optional[dict]:
    """The model's reply as {question, answer, answerable, policy_claim, confidence}, or None when
    it is unusable. Fails CLOSED — an off-contract answer is never published."""
    for candidate in _json_object_candidates(raw_text or ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        try:
            confidence = float(data.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "question": str(data.get("question") or "").strip()[:MAX_QUESTION_CHARS],
            "answer": " ".join(str(data.get("answer") or "").split())[:MAX_ANSWER_CHARS + 1],
            "answerable": bool(data.get("answerable")),
            "policy_claim": bool(data.get("policy_claim")),
            "confidence": max(0.0, min(1.0, confidence)),
        }
    return None


def generate_faq_answer(question: str, grounding: str, asks: int = 1,
                        current_answer: Optional[str] = None) -> Optional[dict]:
    """ONE `lem-medium` call that writes (or rewrites) the answer to a recurring question, using
    only `grounding`. Returns None when the call fails or the reply is off-contract — the caller
    holds instead of publishing."""
    if not str(question or "").strip() or not str(grounding or "").strip():
        return None
    model = faq_model()
    user_parts = [f"Question users keep asking ({max(1, int(asks or 1))} times):\n{question}"]
    if current_answer:
        user_parts.append("The FAQ currently answers it like this. Improve it ONLY where the "
                          "source material supports a better answer; otherwise repeat it "
                          f"unchanged:\n{current_answer}")
    user_parts.append(f"Source material (the ONLY thing you may use):\n{grounding}")
    try:
        from cqc_lem.utilities.ai.ai_helper import _call_llm
        response = _call_llm(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": "\n\n".join(user_parts)}],
            temperature=0,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        log_warning("FAQ answer generation failed — question held", exc=e, ai_model=model)
        return None
    parsed = _parse_answer(raw)
    if parsed is None:
        log_warning("FAQ answer was off-contract — question held", ai_model=model)
    return parsed


# --- Hold path ----------------------------------------------------------------------------------

def build_hold_body(question: str, answer: Optional[str], reason: str, asks: int) -> str:
    """The GitHub issue body for a question the auto-FAQ pass refuses to publish. Carries the
    redacted question and the proposed wording so the owner can approve/edit in one pass."""
    lines = [f"**Users have asked this {max(1, int(asks or 1))} time(s) and the auto-FAQ pass will "
             "not answer it unattended.**", "",
             f"**Question:** {question}", "",
             f"**Why it is held:** {reason}", ""]
    if answer:
        lines += ["**Proposed answer (NOT published):**", "", f"> {answer}", ""]
    else:
        no_answer = ("No answer could be grounded in the product docs — the docs may need the "
                     "fact first.")
        lines += [no_answer, ""]
    scope = ("Publish (or reject) an `faq_entries` row for this question. Editing the answer here "
             "is enough — the auto-FAQ pass versions every later revision.")
    provenance = (f"_Auto-raised by the auto-FAQ service (`{PLAN_DOC}` §C.7). Only the redacted "
                  "question reaches GitHub — never the raw feedback or who asked it._")
    lines += ["## Scope", scope, "",
              "## Acceptance",
              "- The question is either answered on the public FAQ or explicitly rejected.", "",
              provenance]
    return "\n".join(lines)


def build_hold_decision_comment(question: str, answer: Optional[str], reason: str) -> str:
    """RUNBOOK-shaped Decision Comment: ONE genuine question (what should the public FAQ say),
    lettered options with consequences, and a recommendation."""
    options = [
        ("Publish the proposed answer as written",
         "it goes live on the landing page FAQ and askers get it by email"),
        ("Edit the proposed answer, then publish",
         "you own the exact wording of the claim"),
        ("Don't answer this publicly",
         "the question stays out of the FAQ and no auto-reply is sent"),
    ]
    if not answer:
        options[0] = ("Add the missing fact to the product docs",
                      "the next pass can then ground and write the answer itself")
    recommended = 'B' if answer else 'A'
    lines = ["## 🧑‍⚖️ Human decision needed — reply with option letters",
             f"Held (`{NEEDS_HUMAN_LABEL}`, {reason}). Raised by the auto-FAQ service "
             f"(`{PLAN_DOC}` §C.7) — it will not publish a claim you have not made.",
             "Reply one letter per question — e.g. `1B` — or `ok` for all recommendations.", "",
             f"### 1. How should “{question}” be answered publicly?"]
    for letter, (option, consequence) in zip(('A', 'B', 'C'), options):
        mark = "  ✅ *recommended*" if letter == recommended else ""
        lines.append(f"- **{letter}. {option}** — {consequence}{mark}")
    why = ("The wording commits the company to something, so it should read exactly as you would "
           "say it." if answer else
           "The docs don't contain the answer yet, and everything downstream grounds on them.")
    lines += ["", f"**My recommendation: `1{recommended}`.** {why}"]
    return "\n".join(lines)


def hold_for_human(question: str, answer: Optional[str], reason: str, asks: int,
                   policy: bool = False) -> Optional[int]:
    """File the hold as a `needs-human` issue (plus `risk:product-decision` for a policy claim) with
    a letter-pickable Decision Comment. Returns the issue number, or None when GitHub is
    unconfigured/unreachable — holding is best-effort, the answer is withheld either way."""
    labels = [QUESTION_LABEL, FEEDBACK_LABEL, NEEDS_HUMAN_LABEL]
    if policy:
        labels.append(PRODUCT_DECISION_LABEL)
    number = create_github_issue(
        f"docs(faq): answer \"{question[:80]}\"",
        build_hold_body(question, answer, reason, asks), labels, assignees=[issue_assignee()])
    if number is None:
        return None
    comment_on_issue(number, build_hold_decision_comment(question, answer, reason))
    return number


# --- Orchestration ------------------------------------------------------------------------------

def _result(action: str, cluster_id: Optional[int], **extra) -> dict:
    return {"action": action, "cluster_id": cluster_id, **extra}


def reply_to_askers(rows: Optional[list], question: str, answer: str) -> int:
    """Email everyone in the cluster the answer to what they asked. Anonymous rows (no user_id) have
    nowhere to reply to and are skipped. Never raises — a mail outage must not block publishing."""
    if not auto_reply_enabled() or not str(answer or "").strip():
        return 0
    from cqc_lem.utilities.notifications import notify_faq_answer
    replied = 0
    seen: set = set()
    for row in rows or []:
        user_id = row.get("user_id")
        if user_id is None or user_id in seen:
            continue
        seen.add(user_id)
        try:
            if notify_faq_answer(user_id, question, answer):
                replied += 1
        except Exception as e:
            log_warning("Could not auto-reply with the FAQ answer", exc=e, user_id=user_id)
    return replied


def _close_cluster(cluster: dict, status: "FeedbackStatus") -> None:
    """Stamp every row in the cluster with the FAQ cluster id and its outcome, so the pass never
    reconsiders (or re-charges for) it. The seed carries `cluster_id = id`, exactly like the filer's
    work clusters — but at a status the filer's cluster query ignores, so the two never mix."""
    for row in cluster.get("rows") or []:
        update_feedback_triage(row.get("id"), status=status, cluster_id=cluster["cluster_id"])


def answer_question_cluster(cluster: dict, entries: Optional[list] = None,
                            grounding: Optional[str] = None,
                            allow_generate: bool = True) -> dict:
    """Take ONE clustered question to its outcome (see `FaqAction`). Never raises.

    `entries` / `grounding` are injectable so a batch pass loads the FAQ and reads the docs once;
    `allow_generate=False` is the per-pass answer budget saying "not this one, next pass"."""
    cluster_id = cluster.get("cluster_id")
    question = str(cluster.get("question") or "").strip()
    rows = cluster.get("rows") or []
    asks = len(rows)
    if not question:
        return _result(FaqAction.PENDING, cluster_id, reason="no question text")

    entry, score = match_entry(question, entries)
    threshold = recurrence_min()

    # Already answered and not (yet) recurring enough to be worth rewriting: reply from the stored
    # answer and close the rows. Costs nothing.
    if entry and asks < threshold:
        replied = reply_to_askers(rows, str(entry.get("question") or question),
                                  str(entry.get("answer") or ""))
        _close_cluster(cluster, FeedbackStatus.RESOLVED)
        return _result(FaqAction.ANSWERED, cluster_id, entry_id=entry.get("id"), asks=asks,
                       similarity=score, replied=replied)
    if asks < threshold:
        return _result(FaqAction.PENDING, cluster_id, asks=asks, needed=threshold)
    if not allow_generate:
        return _result(FaqAction.PENDING, cluster_id, asks=asks, reason="answer budget spent")

    grounding = build_grounding(entries) if grounding is None else grounding
    current = str(entry.get("answer") or "") if entry else None
    generated = generate_faq_answer(question, grounding, asks=asks, current_answer=current)

    if generated is None or not generated.get("answerable") or not generated.get("answer"):
        reason = "no answer could be grounded in the product docs"
        number = hold_for_human(question, None, reason, asks)
        _close_cluster(cluster, FeedbackStatus.TRIAGED)
        return _result(FaqAction.HELD, cluster_id, reason=reason, asks=asks, issue_number=number)

    # The published question is the model's cleaner phrasing when it kept one, but PII-redacted and
    # shape-normalized here — never trusted verbatim. The ANSWER is deliberately NOT redacted:
    # personal data in it means the model went off-corpus, which must hold the answer rather than
    # quietly publish "Ask [redacted] about it".
    published_question = normalize_question(generated.get("question") or question) or question
    answer = str(generated.get("answer") or "").strip()

    policy = implies_policy_claim(published_question, answer) or (
        "the model flagged it as a policy claim" if generated.get("policy_claim") else None)
    reason = check_answer(published_question, answer, grounding) or (
        f"implies a product/policy claim ({policy})" if policy else None)
    if reason:
        number = hold_for_human(published_question, redact_pii(answer), reason, asks,
                                policy=bool(policy))
        _close_cluster(cluster, FeedbackStatus.TRIAGED)
        log_info(f"Auto-FAQ held \"{published_question}\" — {reason}")
        return _result(FaqAction.HELD, cluster_id, reason=reason, asks=asks, issue_number=number)

    # A rewrite that says the same thing is not a revision — don't churn the history for it.
    if entry and text_similarity(answer, current or "") >= REWRITE_SIMILARITY_MAX:
        replied = reply_to_askers(rows, str(entry.get("question") or question), current or answer)
        _close_cluster(cluster, FeedbackStatus.RESOLVED)
        return _result(FaqAction.ANSWERED, cluster_id, entry_id=entry.get("id"), asks=asks,
                       similarity=score, replied=replied)

    if entry:
        # An already-public answer stays public when it is refreshed; a draft stays a draft.
        status = FaqStatus(str(entry.get("status") or FaqStatus.DRAFT))
        write_question = str(entry.get("question") or published_question)
    else:
        status = FaqStatus.PUBLISHED if auto_publish() else FaqStatus.DRAFT
        write_question = published_question

    entry_id = upsert_faq_entry(write_question, answer, cluster_id=cluster_id, status=status,
                                sort_order=None if entry else AUTO_SORT_ORDER)
    if entry_id is None:
        # Nothing was written and no row is stamped — the next pass retries the whole cluster.
        return _result(FaqAction.ERROR, cluster_id, reason="FAQ entry could not be written",
                       asks=asks)
    record_faq_entry_version(entry_id, write_question, answer, status=status, source='auto')

    replied = reply_to_askers(rows, write_question, answer)
    _close_cluster(cluster, FeedbackStatus.RESOLVED)
    action = FaqAction.REVISED if entry else FaqAction.PUBLISHED
    log_info(f"Auto-FAQ {action} \"{write_question}\" ({asks} ask(s), status {status}, "
             f"{replied} asker(s) replied to)")
    return _result(action, cluster_id, entry_id=entry_id, asks=asks, status=str(status),
                   replied=replied, question=write_question, answer=answer)


def process_faq_feedback(limit: int = 50) -> dict:
    """Drain the support queue into the FAQ (issue #507). The FAQ and the grounding docs are read
    ONCE per pass, and entries published mid-pass join the in-memory list so two clusters of the
    same question in one pass produce one entry."""
    rows = get_faq_candidate_feedback(limit)
    questions = [row for row in rows if looks_like_question(row.get("body"))]
    if not questions:
        log_debug(f"Auto-FAQ pass: {len(rows)} candidate(s), none question-shaped")
        return {"candidates": len(rows), "questions": 0, "clusters": 0, "counts": {}}

    entries = get_faq_entries(limit=MAX_ENTRIES)
    grounding = build_grounding(entries)
    clusters = sorted(cluster_questions(questions), key=lambda c: len(c["rows"]), reverse=True)
    budget = max_answers_per_pass()
    counts: dict = {}
    for cluster in clusters:
        try:
            result = answer_question_cluster(cluster, entries=entries, grounding=grounding,
                                             allow_generate=budget > 0)
        except Exception as e:
            log_error(f"Auto-FAQ failed for cluster {cluster.get('cluster_id')}", exc=e)
            counts[FaqAction.ERROR] = counts.get(FaqAction.ERROR, 0) + 1
            continue
        counts[result["action"]] = counts.get(result["action"], 0) + 1
        if result["action"] in (FaqAction.PUBLISHED, FaqAction.REVISED, FaqAction.HELD):
            budget -= 1
        if result["action"] == FaqAction.PUBLISHED:
            entries.append({"id": result.get("entry_id"), "question": result.get("question"),
                            "answer": result.get("answer"), "status": result.get("status"),
                            "cluster_id": cluster.get("cluster_id")})
    log_info(f"Auto-FAQ pass: {len(questions)} question(s) in {len(clusters)} cluster(s) — "
             f"{counts or 'nothing to do'}")
    return {"candidates": len(rows), "questions": len(questions), "clusters": len(clusters),
            "counts": counts}


def revert_faq_answer(entry_id: int, version_id: int) -> Optional[dict]:
    """Roll an FAQ entry back to a stored version (the revertible half of versioned answers). The
    revert is itself appended to the history, so nothing is ever lost by undoing."""
    version = apply_faq_entry_version(entry_id, version_id)
    if not version:
        log_warning(f"Could not revert FAQ entry {entry_id} to version {version_id}")
        return None
    record_faq_entry_version(entry_id, version["question"], version["answer"],
                             status=version["status"], source='revert')
    log_info(f"Reverted FAQ entry {entry_id} to version {version_id}")
    return version


def faq_answer_history(entry_id: int, limit: int = 20) -> list:
    """An entry's answer history, newest first — what `revert_faq_answer` picks a version id from."""
    return get_faq_entry_versions(entry_id, limit=limit)
