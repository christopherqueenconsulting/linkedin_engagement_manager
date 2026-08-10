"""The owner's way out of a park: reading a Decision-Comment reply.

Parking is the pipeline saying "I stopped". Every park in v2 is written by `actions/park.sh`, whose
own header promises un-parking happens "through the existing answer lane" — and that lane lived
only in v1's `tick.sh`. Once v1 was retired to a heartbeat-gated failsafe it stopped running at all,
so a park became permanent: an owner reply marked the item dirty, `decide` re-read the hold label,
and re-parked it. Measured on #1313 — answered `1B` at 14:58, still parked six hours later.

The rules here are ported from v1's `newest_owner_answer` / `answer_verdict`, deliberately
unchanged, because they encode judgements paid for in incidents:

* **Only comments AFTER the latest Decision Comment count.** A re-park must not instantly re-route
  on the answer to the PREVIOUS question.
* **Ambiguity leaves the work parked.** A reply that leads with a token but asks to hold, or a
  free-form directive ending in a question, is not a decision. Guessing wrong here starts a build
  the owner asked not to happen.
* **Non-owner comments are skipped, never terminal.** A bot commenting after the owner must not be
  able to bury the answer.
* **Agent comments are excluded by BODY SIGNATURE as well as by author.** Under the PAT identity
  the agent posted as the owner, and repos that roll back to it must not have every agent comment
  read as an owner answer.

The split mirrors `observe`: `parse` is pure and holds everything worth arguing about, `newest`
does the one GitHub read.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from . import github

LOG = logging.getLogger("lemd.answers")

#: Verdicts that un-park. The other two (`hold`, `question`) are recognised precisely so they can
#: be refused loudly rather than falling through to "not an answer" and looking like silence.
ACTIONABLE = frozenset({"answer", "directive"})

#: A Decision Comment, by body. Both v1 and `park.sh` lead with this phrase.
_DECISION = re.compile(r"human decision needed", re.I)
#: The Claude Code trailer every agent-authored comment carries.
_AGENT = re.compile(r"Generated with \[Claude Code\]")

#: An option answer: "1B", "2 c", "ok". The trailing `[^a-z0-9]|$` after the letter is load-bearing
#: — without it "2 things I want changed" parses as option 2T.
_ANSWER = re.compile(r"^\s*(?:ok\b|okay\b|\d+\s*[A-Za-z](?:[^A-Za-z0-9]|$))", re.I)
#: An off-menu instruction: the owner answering something the agent never offered.
_DIRECTIVE = re.compile(r"^\s*(?:@claude\b|decision:|go:)", re.I)
#: An explicit hold anywhere in the body outranks a leading token.
_HOLD = re.compile(
    r"(don'?t|do not) (merge|land|ship|start)|hold (off|on|this|it)\b|\bon hold\b|"
    r"\bnot yet\b|wait (for|on|until|till)\b|stand ?by\b",
    re.I,
)

#: Bodies longer than this are prose, not decisions. v1's bound, kept: an owner writing an essay is
#: describing a problem, and routing that to a build wastes a model session on a misread.
MAX_BODY = 8000


@dataclass(frozen=True)
class Answer:
    """One owner reply that the pipeline has read a verdict out of."""

    comment_id: str
    verdict: str
    excerpt: str

    @property
    def actionable(self) -> bool:
        """True when this reply un-parks the work."""
        return self.verdict in ACTIONABLE


def verdict_for(body: str) -> str | None:
    """Classify one comment body: `answer` | `directive` | `hold` | `question` | None.

    Shape is judged on the FIRST non-empty line so trailing context, off-menu options and side
    instructions all still reach the agent verbatim. The hold and question checks then read the
    WHOLE body, because that is where an owner puts the caveat that changes the answer.

    Returns:
        None when the comment is not a decision at all — the common case, and the one that must
        stay silent rather than being reported as a refusal.
    """
    if not body or len(body) >= MAX_BODY:
        return None
    first = next((ln for ln in body.replace("\r", "").splitlines() if ln.strip()), "")
    if not first:
        return None

    if _ANSWER.match(first):
        shape = "answer"
    elif _DIRECTIVE.match(first):
        shape = "directive"
    else:
        return None

    # Ambiguity fails toward the human, in both directions.
    if _HOLD.search(body):
        return "hold"
    if shape == "directive" and body.rstrip().endswith("?"):
        return "question"
    return shape


def parse(comments: list[dict[str, Any]], owner: str) -> Answer | None:
    """The newest owner reply to the LATEST Decision Comment in a thread, classified.

    Pure: `comments` is GitHub's list in chronological order. Returns None when the thread holds no
    Decision Comment, no owner reply after it, or a reply that is not a decision.
    """
    last_decision = -1
    for i, c in enumerate(comments):
        if _DECISION.search(c.get("body") or ""):
            last_decision = i
    if last_decision < 0:
        return None

    for c in reversed(comments[last_decision + 1:]):
        body = c.get("body") or ""
        if (c.get("author") or {}).get("login") != owner:
            continue
        if _DECISION.search(body) or _AGENT.search(body):
            continue
        v = verdict_for(body)
        if v is None:
            # The owner's newest comment is not a decision. Stop rather than reaching further back:
            # an older answer they have since talked past is not a live instruction.
            return None
        return Answer(
            comment_id=str(c.get("id") or c.get("url") or ""),
            verdict=v,
            excerpt=" ".join(body.split())[:60],
        )
    return None


def newest(slug: str, kind: str, number: int, owner: str, *,
           timeout: int = 30) -> Answer | None:
    """Read one thread and classify the owner's newest reply.

    One API call, made only for items already carrying a hold label — every other observation
    reaches its decision without it.

    Returns:
        None both when there is no answer and when the thread could not be read. That collapse is
        safe here and only here: the consequence of "no answer" is that the item stays parked,
        which is exactly the right outcome for an unreadable thread.
    """
    try:
        facts = github.gh_json(
            [kind, "view", str(number), "--repo", slug, "--json", "comments"],
            timeout=timeout,
        ) or {}
    except github.GitHubUnavailable as exc:
        LOG.warning("%s #%s comments unreadable — staying parked: %s", kind, number, exc)
        return None
    return parse(list(facts.get("comments") or []), owner)
