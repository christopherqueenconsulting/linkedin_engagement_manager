"""The (B) writer: LEM-promotional content published from the user's own account (issue #770).

`affiliate.py` decided *whether* promotion is allowed and *what* a compliant piece looks like; #750
built all of that and deliberately stopped there — a consent record and a publish gate with nothing
upstream of them. This module is that upstream writer, and it is the ONE place promotional copy
about LEM is produced.

Four things it refuses to be, each of which is the whole point:

1. **Not a second consent path.** `promo_content_allowed()` is still the only authorisation, read
   off the stored enrollment row. `AFFILIATE_PROMO_CONTENT_ENABLED` can only ever turn the writer
   OFF; there is no env var, cohort or default that turns promotion ON for a user who did not.
2. **Not an extra post.** An affiliate post CLAIMS one of the 70/20/10 governor's promo slots
   (issue #618) instead of adding a post beside it, so the 10% promo ceiling holds by construction
   rather than by a second counter that could drift from the first. On a promo slot it does not
   claim, the author's own case-study post is written exactly as before.
3. **Not an author of facts.** The writer's allow-list is `PRODUCT_FACTS` and nothing else — the
   same posture the story bank (#620) takes for personal specifics. It may not state a result, a
   metric, a time saved or a testimonial, because LEM cannot verify any claim about what the tool
   did for this user, and an invented one is a false endorsement rather than a bad post.
4. **Not silently published.** The copy leaves here already disclosed (`apply_disclosure`, tagged),
   and the post is held for the author's explicit approval by the `affiliate_promo` quality gate.
   The publish-time `disclosure_report()` gate in `post_to_linkedin` stays the backstop it was
   designed to be — belt AND braces, because the two failure modes are different: this module
   guarantees the disclosure is written, the gate guarantees an undisclosed one never publishes.
"""

from __future__ import annotations

import re
from typing import Optional

from cqc_lem.utilities.env_constants import (AFFILIATE_PROMO_CONTENT_ENABLED,
                                             AFFILIATE_PROMO_EVERY_N_PROMO_SLOTS,
                                             AFFILIATE_PROMO_MAX_PER_DAY,
                                             AFFILIATE_PROMO_PRODUCT_NAME)
from cqc_lem.utilities.marketing.affiliate import (apply_disclosure, disclosure_report,
                                                   disclosure_text, is_affiliate_content,
                                                   link_for_user, program_enabled,
                                                   promo_content_allowed)

# Why the writer declined or refused. Only the last three (`_blocked`) are events: they mean copy
# that was supposed to be compliant could not be made so. Declining to write — no consent, not this
# slot, paced out — is the common, healthy case, and a refusal series that fires nine times out of
# ten tells nobody anything.
REASON_DISABLED = "generator_disabled"
REASON_NO_CONSENT = "no_promo_consent"
REASON_NO_LINK = "no_referral_link"
REASON_NO_DISCLOSURE_CONFIGURED = "no_disclosure_configured"
REASON_NOT_PROMO_SLOT = "not_promo_slot"
REASON_NOT_SELECTED = "not_selected"
REASON_PACED_OUT = "paced_out"
REASON_EMPTY_DRAFT = "empty_draft"
REASON_SLOP = "slop_lint"
REASON_UNDISCLOSED = "undisclosed_draft"

# The ONLY claims the writer may make about the product. Everything here is a description of what
# LEM does — checkable against the product itself — and nothing here is an outcome, because an
# outcome would be a claim about this author's results that LEM has no way to verify.
PRODUCT_FACTS = (
    "It plans a month of LinkedIn posts, writes the drafts, and schedules them around the hours the "
    "author's own audience is active.",
    "It leaves comments and replies on the author's behalf inside limits the author sets — how many "
    "a day, which topics, which people.",
    "Everything it writes can be reviewed and edited before it publishes.",
    "It is a paid tool with a free trial.",
)

_SYSTEM_PROMPT = (
    "You write ONE short LinkedIn post in the author's own voice. The post recommends a tool the "
    "author uses and links to it. You are not writing an advertisement: you are writing the way "
    "somebody mentions a tool that solved a chore for them, and then gets out of the way.\n\n"
    "Absolute rules — breaking any of them makes the post unusable:\n"
    "- State NOTHING about the product beyond the facts you are given. No results, no metrics, no "
    "percentages, no hours saved, no follower or engagement numbers, no 'since I started using it'. "
    "You do not know what it did for this author and you must not imply that you do.\n"
    "- No urgency, no scarcity, no discount, no 'limited spots', no hard sell.\n"
    "- Never ask the reader for a call, demo, meeting or DM.\n"
    "- Do not thank the reader, do not open with a rhetorical question, do not close with "
    "'thoughts?'.\n"
    "- Reproduce the referral link and the disclosure line EXACTLY as given, each on its own line, "
    "once. Do not shorten, re-word or decorate either one.\n"
    "Return the post body only — no title, no preamble, no commentary."
)


def generator_enabled() -> bool:
    """Whether this deployment runs the (B) writer at all. Off restores the pre-#770 product: the
    consent toggle still exists and still records consent, nothing writes against it."""
    return bool(AFFILIATE_PROMO_CONTENT_ENABLED)


def promo_slot_cadence() -> int:
    """How many promo slots pass per affiliate one. Clamped at 1 — a zero or negative setting must
    never read as "no cadence, take every slot" via a modulo that divides by zero."""
    try:
        return max(1, int(AFFILIATE_PROMO_EVERY_N_PROMO_SLOTS))
    except (TypeError, ValueError):
        return 3


def max_promo_per_day() -> int:
    return max(0, int(AFFILIATE_PROMO_MAX_PER_DAY or 0))


def product_name() -> str:
    return str(AFFILIATE_PROMO_PRODUCT_NAME or "").strip() or "LinkedIn Engagement Manager"


def promo_allowed_for_user(user_id: int) -> bool:
    """Whether THIS user has authorised promotional content — the stored consent, read through the
    one gate that answers it. An unreadable enrollment row is a no: the failure mode of guessing is
    publishing an ad on somebody's professional identity."""
    from cqc_lem.utilities.db import get_affiliate_enrollment
    from cqc_lem.utilities.logger import log_warning
    try:
        return promo_content_allowed(get_affiliate_enrollment(user_id))
    except Exception as e:
        log_warning("Could not read affiliate consent — writing no promotional content", exc=e,
                    user_id=user_id)
        return False


def _selected_by_cadence(post_id: Optional[int]) -> bool:
    """Deterministic 1-in-N over promo slots, keyed on a stable per-post integer exactly like
    `should_include_lead_magnet_cta` — so the same post always makes the same choice and a retry
    cannot turn an ordinary promo slot into an affiliate one."""
    if post_id is None:
        return False
    n = promo_slot_cadence()
    return n == 1 or int(post_id) % n == 0


def _within_daily_pace(user_id: int) -> bool:
    """Whether today's paced allowance has room for another promotional piece.

    Fails OPEN on a pacing error, like every other reader of the engine: the hard limits on this
    path are consent and the promo slot itself, and neither of them lives in Redis."""
    from cqc_lem.utilities.human_pacing import (ACTION_AFFILIATE_PROMO, actions_used_today,
                                                daily_budget)
    from cqc_lem.utilities.logger import log_warning
    cap = max_promo_per_day()
    if cap <= 0:
        return False
    try:
        budget = daily_budget(user_id, ACTION_AFFILIATE_PROMO, cap)
        return actions_used_today(user_id, ACTION_AFFILIATE_PROMO) < budget
    except Exception as e:
        log_warning("Could not read the affiliate promo pacing budget — allowing this one", exc=e,
                    user_id=user_id)
        return True


def promo_slot_verdict(user_id: int, post_id: Optional[int], content_mix: Optional[str]) -> str:
    """Whether an affiliate post may claim THIS planned slot, and why not when it may not.

    Ordered cheapest-first so the overwhelmingly common answer (this is not a promo slot) costs no
    DB read and no Redis round trip."""
    from cqc_lem.utilities.ai.content_alignment import ContentMix, normalize_content_mix
    if not program_enabled() or not generator_enabled():
        return REASON_DISABLED
    if normalize_content_mix(content_mix) != ContentMix.PROMO.value:
        return REASON_NOT_PROMO_SLOT
    if not _selected_by_cadence(post_id):
        return REASON_NOT_SELECTED
    if not disclosure_text():
        return REASON_NO_DISCLOSURE_CONFIGURED
    if not promo_allowed_for_user(user_id):
        return REASON_NO_CONSENT
    if not link_for_user(user_id):
        return REASON_NO_LINK
    if not _within_daily_pace(user_id):
        return REASON_PACED_OUT
    return ""


def claims_promo_slot(user_id: int, post_id: Optional[int], content_mix: Optional[str]) -> bool:
    """True when an affiliate post takes this promo slot instead of the author's own case study.

    Reports the refusal HERE rather than leaving it to `generate_promo_post`: this is the call the
    content plan actually makes, and it short-circuits the writer entirely — so a deployment whose
    consented users can never publish (no disclosure sentence configured) would otherwise say so
    nowhere at all."""
    verdict = promo_slot_verdict(user_id, post_id, content_mix)
    if verdict:
        _declined(user_id, post_id, verdict)
        return False
    return True


def _track(event: str, user_id: int, **extra) -> None:
    from cqc_lem.utilities.observability import track_affiliate_event
    track_affiliate_event(event, user_id=user_id, **extra)


def _blocked(user_id: int, post_id: Optional[int], reason: str) -> None:
    """A draft that was supposed to be compliant and is not — WARNING + an event."""
    from cqc_lem.utilities.logger import log_warning
    from cqc_lem.utilities.observability import AFFILIATE_PROMO_BLOCKED
    log_warning(f"Affiliate promo content blocked at generation ({reason})", user_id=user_id,
                post_id=post_id, task_name="generate_promo_post")
    _track(AFFILIATE_PROMO_BLOCKED, user_id, post_id=post_id, reason=reason)


def _declined(user_id: int, post_id: Optional[int], reason: str) -> None:
    """Not writing is the normal answer, so it is DEBUG, not a warning — a user who never consented
    would otherwise emit one warning per planned promo slot forever. The exception is a deployment
    that consented users cannot publish from at all: that one is a misconfiguration."""
    from cqc_lem.utilities.logger import log_debug, log_warning
    if reason == REASON_NO_DISCLOSURE_CONFIGURED:
        log_warning("Affiliate promo content skipped — AFFILIATE_DISCLOSURE_TEXT is blank, so this "
                    "deployment cannot publish affiliate content at all",
                    user_id=user_id, post_id=post_id, task_name="generate_promo_post")
        return
    log_debug(f"No affiliate promo content for this slot ({reason})", user_id=user_id,
              post_id=post_id, task_name="generate_promo_post")


def _prompt(prefs: Optional[dict], profile_synthesis: Optional[str], referral_link: str) -> str:
    from cqc_lem.utilities.ai.content_alignment import ARTIFACT_CTA_POLICY, style_directive
    facts = "\n".join(f"- {fact}" for fact in PRODUCT_FACTS)
    voice = (profile_synthesis or "").strip()
    parts = [
        f"Write the post as me. The tool is {product_name()}.",
        "\nThe ONLY things you may say about the tool (say fewer of them if the post reads better "
        f"for it, but never anything beyond them):\n{facts}",
        "\nSay in your own words why a professional who is trying to stay visible on LinkedIn "
        "without spending an hour a day on it might want to look at it. Keep it to a few short "
        "paragraphs. Be plain. If the reader would not care, do not write it.",
        f"\nInclude this link on its own line, exactly once, exactly as written:\n{referral_link}",
        f"\nInclude this disclosure on its own line, exactly as written:\n{disclosure_text()}",
    ]
    if voice:
        parts.append("\nMy voice — match how this reads, do not copy phrases out of it and do not "
                     f"state anything it does not say about me:\n{voice[:800]}")
    # The promo-slot mix directive (`mix_directive`) is deliberately NOT injected: it demands a case
    # study built on "one specific real outcome number", which is the one thing this writer must
    # never state. The half of the promo policy that DOES apply is the CTA ban, taken directly.
    parts.append("\n- " + ARTIFACT_CTA_POLICY)
    parts.append("\n- Frame it explicitly no-pressure: it may not be a fit for everyone, and the "
                 "reader owes you nothing for reading.")
    parts.append(style_directive(prefs, "post"))
    return "\n".join(p for p in parts if p)


def _draft(user_id: int, prompt: str, retry_directive: str = "") -> Optional[str]:
    from cqc_lem.utilities.ai.ai_helper import _call_llm
    from cqc_lem.utilities.logger import log_warning
    from cqc_lem.utilities.observability import FEATURE_CONTENT
    try:
        response = _call_llm(
            model="lem-medium",
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": prompt + (retry_directive or "")}],
            temperature=0.6,
            _track_user_id=user_id,
            _track_feature=FEATURE_CONTENT,
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception as e:
        log_warning("Affiliate promo generation call failed", exc=e, user_id=user_id)
        return None


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _restore_referral_link(body: str, referral_link: str) -> str:
    """Put the exact referral URL back over any copy the markdown pass mangled.

    `sanitize_for_linkedin` strips markdown italics, and `_word_` is precisely the shape of a UTM'd
    referral link: `utm_source=…&utm_medium=…` comes back as `utmsource=…&utmmedium=…`. That is a
    dead link on the one URL in the post whose whole job is attribution — and because the mangled
    copy no longer matches, the caller would append a second, clean one beside it and publish both.
    Matched on the underscore-free form so the repair works wherever in the body the link landed."""
    if not referral_link:
        return body
    wanted = referral_link.replace("_", "")

    def _repair(match) -> str:
        url = match.group(0)
        core = url.rstrip(".,;:!?)]")
        return referral_link + url[len(core):] if core.replace("_", "") == wanted else url

    return _URL_RE.sub(_repair, body)


def _finalize(user_id: int, draft: str, referral_link: str) -> str:
    """Everything that must be true of the copy regardless of what the model returned: LinkedIn-safe
    punctuation, the referral link present AND intact (it is what earns the trial time and what the
    publish gate grades on), and the disclosure stamped."""
    from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin
    body = _restore_referral_link(sanitize_for_linkedin(draft).strip(), referral_link)
    if referral_link and referral_link not in body:
        body = f"{body}\n\n{referral_link}"
    return apply_disclosure(body, user_id=user_id, tagged=True).strip()


def generate_promo_post(user_id: int, post_id: Optional[int] = None,
                        content_mix: Optional[str] = None,
                        prefs: Optional[dict] = None,
                        profile_synthesis: Optional[str] = None) -> Optional[str]:
    """The promotional post for one claimed promo slot, already disclosed — or None.

    None is the normal answer and always means "write the ordinary post instead": the user has not
    consented, this slot is not an affiliate one, the day's pace is spent, or no draft could be made
    compliant. The model call and every gate read fail CLOSED to None rather than propagating, and
    the content-plan caller contains the rest — a user who opted into (B) must never lose the post
    they would otherwise have had because the promotional one failed."""
    from cqc_lem.utilities.ai.slop_lint import (lint_report, slop_max_attempts, slop_retry_directive,
                                                violation_reasons)
    from cqc_lem.utilities.logger import log_info, log_warning
    from cqc_lem.utilities.observability import AFFILIATE_PROMO_GENERATED

    verdict = promo_slot_verdict(user_id, post_id, content_mix)
    if verdict:
        _declined(user_id, post_id, verdict)
        return None

    referral_link = link_for_user(user_id)
    prompt = _prompt(prefs, profile_synthesis, referral_link)

    content, retry_directive, attempts = None, "", 0
    for _ in range(max(1, slop_max_attempts())):
        attempts += 1
        draft = _draft(user_id, prompt, retry_directive)
        if not draft:
            _blocked(user_id, post_id, REASON_EMPTY_DRAFT)
            return None
        candidate = _finalize(user_id, draft, referral_link)
        report = lint_report(candidate, "post")
        if report["passes"]:
            content = candidate
            break
        log_warning("Affiliate promo draft tripped the AI-slop lint — regenerating: "
                    + "; ".join(violation_reasons(report["hard"])),
                    user_id=user_id, post_id=post_id, task_name="generate_promo_post")
        retry_directive = slop_retry_directive(report["hard"])

    if not content:
        _blocked(user_id, post_id, REASON_SLOP)
        return None

    # The one check that must not be left to the publish gate: if the copy somehow arrived without a
    # disclosure it is refused HERE, so the gate never has to decide about content we wrote.
    if not disclosure_report(content, user_id=user_id, tagged=True).get("ok"):
        _blocked(user_id, post_id, REASON_UNDISCLOSED)
        return None

    _record_generated(user_id)
    log_info("Generated affiliate promotional post (held for the author's approval)",
             user_id=user_id, post_id=post_id, task_name="generate_promo_post")
    _track(AFFILIATE_PROMO_GENERATED, user_id, post_id=post_id, surface="post",
           attempts=attempts, chars=len(content))
    return content


def _record_generated(user_id: int) -> None:
    from cqc_lem.utilities.human_pacing import ACTION_AFFILIATE_PROMO, record_action
    from cqc_lem.utilities.logger import log_warning
    try:
        record_action(user_id, ACTION_AFFILIATE_PROMO)
    except Exception as e:
        log_warning("Could not record the affiliate promo action for pacing", exc=e, user_id=user_id)


def is_affiliate_post(content: Optional[str], user_id: Optional[int] = None,
                      first_comment_links: Optional[list] = None) -> bool:
    """Whether a finished post is affiliate promotion — graded on the body AND any link the #392
    split carried into the first comment, the same text the publish gate grades."""
    graded = "\n".join([content or "", *(first_comment_links or [])])
    return is_affiliate_content(graded, user_id=user_id)


def record_promo_published(user_id: int, post_id: int, content: Optional[str],
                           first_comment_links: Optional[list] = None,
                           post_url: Optional[str] = None) -> bool:
    """Emit `affiliate_promo_published` for a post that just went live carrying the user's referral
    link. Best-effort and never raises — the post is already published; analytics cannot un-publish
    it and must not turn a successful share into a task failure."""
    from cqc_lem.utilities.logger import log_warning
    from cqc_lem.utilities.observability import AFFILIATE_PROMO_PUBLISHED
    try:
        if not is_affiliate_post(content, user_id=user_id, first_comment_links=first_comment_links):
            return False
        _track(AFFILIATE_PROMO_PUBLISHED, user_id, post_id=post_id, surface="post",
               post_url=post_url,
               link_in_first_comment=bool(first_comment_links))
        return True
    except Exception as e:
        log_warning("Could not track affiliate promo publish", exc=e, user_id=user_id,
                    post_id=post_id)
        return False
