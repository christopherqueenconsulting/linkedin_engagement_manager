"""Word lists shared by every deterministic text guardrail.

Stdlib-only on purpose: the FAQ publisher, the tutorial-video guardrail and the comment-reply
filter all import from here, and none of them may pull Selenium or the DB in through a word list.
"""

from typing import Optional

# The words no public copy of ours may carry, and that mark an incoming comment as hostile.
# Matched as WHOLE tokens only — see `profane_words`.
PROFANITY = frozenset({
    "shit", "shitty", "fuck", "fucking", "fucked", "bullshit", "asshole", "bastard", "bitch",
    "dick", "damn", "goddamn", "crap", "piss", "cunt", "twat", "wanker",
})

# Stripped from each token's edges before the lookup, so "shit!" and "(crap)" still match.
_TOKEN_EDGE_PUNCTUATION = ".,!?;:\"'()[]{}*…“”‘’"


def profane_words(text: Optional[str]) -> list[str]:
    """Return the profane words present in `text`, sorted; empty when it is clean.

    Whole-token only: the text is split on whitespace and each token is stripped of surrounding
    punctuation, so an innocent word that merely CONTAINS one of these ("Scunthorpe",
    "scrapbook") never matches.

    Args:
        text: Any text; None reads as empty.

    Returns:
        The sorted, de-duplicated, lower-cased matches.
    """
    words = {w.lower().strip(_TOKEN_EDGE_PUNCTUATION) for w in str(text or "").split()}
    return sorted(words & PROFANITY)
