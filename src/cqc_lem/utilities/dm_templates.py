"""Turning a stored outreach TEMPLATE into the message that actually gets sent (#1154).

Lifted VERBATIM out of `app/run_automation.py`. Three paths reach the same substitution — the DM
template ladder, the Comment→DM lead magnet, and the catch-up touch — and a fourth (the connect
note) runs the same template-then-refine shape, so they share one implementation rather than three
that drift.

Two rules:

* **An unknown or malformed token never drops the message.** `_SafePlaceholders` leaves `{frst_name}`
  literal instead of raising, and a stray brace falls back to a plain replace of the tokens we know.
  A user's typo costs them one visibly-wrong word, not a silently-skipped outreach step.
* **Every fallback is grammatical.** `{first_name}` empty becomes "there", `{headline}` becomes
  "my professional field", `{event_detail}` becomes "the news" — the same "there" that
  `connection_targeting.first_name` uses, so a connect note and a DM read the same way when the name
  did not scrape.

What is NOT here: the LLM voice-refinement of a DM (`build_dm_from_template`) stays with the task
that owns the DM ladder. `_draft_connect_note` moved because the connect rail and the feed rail both
call it; `default_connect_note` — the pure template it starts from — stays in
`connection_targeting.py`, which is deliberately LLM-free.
"""

from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement
from cqc_lem.utilities.connection_targeting import CONNECT_NOTE_LIMIT, ScoredCandidate, default_connect_note
from cqc_lem.utilities.logger import log_warning


class _SafePlaceholders(dict):
    """format_map backing dict that leaves unknown {tokens} literal instead of raising —
    so a user typo like {frst_name} never drops the whole message.
    """
    def __missing__(self, key):
        return "{" + key + "}"


def render_dm_placeholders(text: str, *, first_name: str = "", headline: str = "",
                           blog_url: str = "", event_detail: str = "") -> str:
    """Single source of truth for filling DM / lead-magnet {placeholders}: {first_name},
    {headline}, {blog_url}, {event_detail}. Used by BOTH the DM-template path and the Comment->DM
    lead magnet so their substitution can never drift. Tolerates unknown/malformed tokens gracefully.
    """
    if not text:
        return text or ""
    ctx = _SafePlaceholders(first_name=first_name or "there",
                            headline=headline or "my professional field",
                            blog_url=blog_url or "",
                            # Catch-up templates (issue #482) reference the specific milestone; the
                            # fallback keeps the sentence grammatical if the detail didn't scrape.
                            event_detail=event_detail or "the news")
    try:
        return text.format_map(ctx)
    except (IndexError, ValueError):
        # malformed/positional braces (e.g. a stray "{") — replace known tokens only
        out = text
        for k in ("first_name", "headline", "blog_url", "event_detail"):
            out = out.replace("{" + k + "}", str(ctx[k]))
        return out


def _draft_connect_note(user_id: int, candidate: ScoredCandidate, topic: str = None) -> str:
    """Personalized connect note for one candidate: a grounded template (it names the actual shared
    context) refined into the user's voice. Falls back to the template if the LLM is unavailable —
    a missing note must never block the target.
    """
    base = default_connect_note(candidate, topic=topic)
    try:
        refined = (get_ai_message_refinement(base, character_limit=CONNECT_NOTE_LIMIT) or "").strip()
    except Exception as e:
        log_warning("Connect-note refinement failed", exc=e, user_id=user_id,
                    action_type="connection_targeting")
        return base
    return (refined or base)[:CONNECT_NOTE_LIMIT]
