"""The deterministic profile-audit rubric (issue #2631, Phase 1). Pure: no I/O, no LLM.

Seven weighted checks over the user's OWN stored profile. Each returns pass / warn / fail /
unknown, with a fix hint. **Unknown is never fail**: a field the scrape could not read is left out
of the score's denominator, so a selector that drifts never reads as a bad profile. LEM never
edits the LinkedIn profile; this only reports.

Bump `PROFILE_RUBRIC_VERSION` whenever a check's logic or weight changes, so audits made under
different rubrics are not compared as if they were the same yardstick.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from cqc_lem.utilities.linkedin.profile import LinkedInProfile

PROFILE_RUBRIC_VERSION = "1"

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
STATUSES = (PASS, WARN, FAIL, UNKNOWN)

# check_id -> weight. Sums to 100 (asserted by the unit tests).
CHECK_WEIGHTS: Dict[str, int] = {
    "headline_outcome": 20,
    "about_hook_cta": 20,
    "featured_proof": 15,
    "banner_custom": 10,
    "custom_url": 10,
    "icp_keywords": 15,
    "experience_consistency": 10,
}

_OUTCOME_RE = re.compile(
    r"\b(help|helps|helping|so (that|you)|turn|turning|grow|growing|reduce|reducing|increase|"
    r"increasing|save|saving|build|building|scale|scaling)\b", re.IGNORECASE)
_WEAK_OPENER_RE = re.compile(
    r"^\s*(i am|i'm|experienced|passionate|results[- ]driven|seasoned|dedicated|motivated)\b",
    re.IGNORECASE)
_CTA_RE = re.compile(
    r"\b(dm|message me|reach out|connect|book|schedule|email|contact|visit|follow|"
    r"comment below|let'?s talk|get in touch)\b", re.IGNORECASE)
_AUTO_SLUG_RE = re.compile(r"-(?=[0-9a-z]*\d)[0-9a-z]{7,9}$", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]{2,}", re.IGNORECASE)
_STOP = frozenset("and the for with you your our that this from who help helps helping".split())
_HOOK_CHARS = 210  # LinkedIn folds About after about three lines


@dataclass(frozen=True)
class CheckResult:
    """One rubric check's outcome."""

    check_id: str
    weight: int
    status: str
    detail: str
    fix_hint: str


def _words(text: Optional[str]) -> set:
    return {w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOP}


def _result(check_id: str, status: str, detail: str, fix_hint: str) -> CheckResult:
    return CheckResult(check_id, CHECK_WEIGHTS[check_id], status, detail, fix_hint)


def check_headline(headline: Optional[str], job_title: Optional[str] = None) -> CheckResult:
    """Names who you help and the outcome, not just a job title."""
    hint = "Rewrite the headline as who you help and the result they get, not only your role."
    text = (headline or "").strip()
    if not text:
        return _result("headline_outcome", UNKNOWN, "Headline not read.", hint)
    if _OUTCOME_RE.search(text):
        return _result("headline_outcome", PASS, "Headline names an outcome.", hint)
    # `job_title` is read from the same page element as the headline, so equality with it carries no
    # signal (it would fail every long headline); the word count is the only title-only test.
    if len(text.split()) <= 5:
        return _result("headline_outcome", FAIL, "Headline reads as a job title only.", hint)
    return _result("headline_outcome", WARN, "Headline is long but names no clear outcome.", hint)


def check_about(about: Optional[str]) -> CheckResult:
    """Opens with a hook in the first lines and ends with a clear next step."""
    hint = ("Open About with a specific claim or question in the first two lines, and end with one "
            "clear next step (message, book, visit).")
    text = (about or "").strip()
    if not text:
        return _result("about_hook_cta", UNKNOWN, "About not read.", hint)
    if len(text) < 150:
        return _result("about_hook_cta", FAIL, "About is too short to hook and close.", hint)
    hook_ok = not _WEAK_OPENER_RE.match(text[:_HOOK_CHARS])
    cta_ok = bool(_CTA_RE.search(text[-300:]))
    if hook_ok and cta_ok:
        return _result("about_hook_cta", PASS, "Hook and next step present.", hint)
    if hook_ok or cta_ok:
        missing = "next step" if hook_ok else "hook"
        return _result("about_hook_cta", WARN, f"About has no clear {missing}.", hint)
    return _result("about_hook_cta", FAIL, "About has no hook and no next step.", hint)


def check_featured(featured_count: Optional[int]) -> CheckResult:
    """The Featured section holds proof items."""
    hint = "Pin two or three proof items to Featured: a case study, a result post, a lead magnet."
    if featured_count is None:
        return _result("featured_proof", UNKNOWN, "Featured not read.", hint)
    if featured_count >= 2:
        return _result("featured_proof", PASS, f"{featured_count} featured items.", hint)
    if featured_count == 1:
        return _result("featured_proof", WARN, "One featured item.", hint)
    return _result("featured_proof", FAIL, "No featured items.", hint)


def check_banner(banner_state: Optional[str]) -> CheckResult:
    """A banner is present and not the default."""
    hint = "Upload a banner that says what you do and for whom."
    if banner_state == "custom":
        return _result("banner_custom", PASS, "Custom banner set.", hint)
    if banner_state == "default":
        return _result("banner_custom", FAIL, "Default banner.", hint)
    return _result("banner_custom", UNKNOWN, "Banner not read.", hint)


def check_custom_url(custom_url: Optional[str]) -> CheckResult:
    """The public profile URL is a readable slug, not LinkedIn's generated one."""
    hint = "Set a custom public profile URL (Edit public profile & URL)."
    slug = (custom_url or "").strip()
    if not slug:
        return _result("custom_url", UNKNOWN, "Profile URL not read.", hint)
    if _AUTO_SLUG_RE.search(slug):
        return _result("custom_url", FAIL, "URL still carries a generated suffix.", hint)
    return _result("custom_url", PASS, "Custom URL set.", hint)


def check_keywords(headline: Optional[str], about: Optional[str],
                   icp_terms: Optional[Sequence[str]]) -> CheckResult:
    """Terms from the user's ICP vocabulary appear in the headline and About."""
    hint = "Work the words your buyers search for into the headline and the first lines of About."
    terms = [t.strip().lower() for t in (icp_terms or []) if t and t.strip()]
    haystack = f"{headline or ''} {about or ''}".lower().strip()
    if not terms or not haystack:
        return _result("icp_keywords", UNKNOWN, "No ICP vocabulary or no profile text to compare.", hint)
    found = [t for t in terms if t in haystack]
    share = len(found) / len(terms)
    detail = f"{len(found)} of {len(terms)} ICP terms appear."
    if share >= 0.5:
        return _result("icp_keywords", PASS, detail, hint)
    return _result("icp_keywords", WARN if found else FAIL, detail, hint)


def _experience_vocabulary(profile: LinkedInProfile) -> set:
    vocab: set = set()
    for skill in profile.skills or []:
        vocab |= _words(skill if isinstance(skill, str) else skill.name)
    for exp in profile.experiences or []:
        for pos in exp.positions or []:
            vocab |= _words(pos.title)
            for line in pos.details or []:
                vocab |= _words(line)
    return vocab


def check_consistency(headline: Optional[str], profile: LinkedInProfile) -> CheckResult:
    """Experience and skills back up what the headline claims."""
    hint = "Add the skills and role details the headline promises, or tighten the headline to match."
    vocab = _experience_vocabulary(profile)
    claimed = _words(headline)
    if not claimed or not vocab:
        return _result("experience_consistency", UNKNOWN, "Nothing to compare.", hint)
    overlap = claimed & vocab
    if len(overlap) >= 2:
        return _result("experience_consistency", PASS, "Headline matches experience and skills.", hint)
    if overlap:
        return _result("experience_consistency", WARN, "Headline barely overlaps experience.", hint)
    return _result("experience_consistency", FAIL, "Headline shares nothing with experience.", hint)


def audit_profile(profile: LinkedInProfile,
                  icp_terms: Optional[Sequence[str]] = None) -> List[CheckResult]:
    """Run all seven checks, in `CHECK_WEIGHTS` order."""
    return [
        check_headline(profile.headline, profile.job_title),
        check_about(profile.about),
        check_featured(profile.featured_count),
        check_banner(profile.banner_state),
        check_custom_url(profile.custom_url),
        check_keywords(profile.headline, profile.about, icp_terms),
        check_consistency(profile.headline, profile),
    ]


def score_results(results: Iterable[CheckResult]) -> Optional[int]:
    """0-100 over the KNOWN weight (pass full, warn half). None when nothing was readable."""
    known = earned = 0.0
    for r in results:
        if r.status == UNKNOWN:
            continue
        known += r.weight
        earned += r.weight * {PASS: 1.0, WARN: 0.5}.get(r.status, 0.0)
    return round(100 * earned / known) if known else None


def diff_audits(previous: Optional[Sequence[Dict[str, Any]]],
                current: Sequence[CheckResult]) -> Dict[str, List[str]]:
    """Check ids that closed, newly opened, or stayed open against the previous audit.

    `previous` is a list of {"check_id", "status"} rows. A finding is open when its status is warn
    or fail. With no previous audit nothing counts as closed, and every open finding is new.
    """
    prior = {row["check_id"]: row["status"] for row in (previous or [])}
    closed: List[str] = []
    opened: List[str] = []
    still_open: List[str] = []
    for r in current:
        is_open = r.status in (WARN, FAIL)
        was_open = prior.get(r.check_id) in (WARN, FAIL)
        if was_open and r.status == PASS:
            closed.append(r.check_id)
        elif is_open and was_open:
            still_open.append(r.check_id)
        elif is_open:
            opened.append(r.check_id)
    return {"closed": closed, "opened": opened, "still_open": still_open}
