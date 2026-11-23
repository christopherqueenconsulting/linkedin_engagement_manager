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
* **Untrusted comments are skipped, never terminal.** A bot commenting after the owner must not be
  able to bury the answer.
* **Only a TRUSTED author answers.** The owner ruled that comment instructions come only from
  trusted authors: the configured owner login, or an account holding `admin`/`maintain`/`write` on
  the repository (`github.comment_author_trusted`). Never `authorAssociation` — on an org-owned repo
  `MEMBER` is any org member and `COLLABORATOR` includes read-only collaborators. An unreadable
  permission lookup is NOT trusted. The pipeline App never answers its own question.
* **Agent comments are excluded by BODY SIGNATURE as well as by author.** Under the PAT identity
  the agent posted as the owner, and repos that roll back to it must not have every agent comment
  read as an owner answer.

An owner's GitHub APPROVED review on a held PR is the answer `ok` too (`approval`), so one touch
suffices: approving no longer has to be followed by typing `ok` under the menu. It keeps the comment
answer's rules and adds four — the owner or a listed answer login (never a Bot, never the App), on
the PR's CURRENT head, newer than the latest Decision Comment and every trusted (or unattributable)
reply after it, and never in place of an explicit answer: an actionable trusted reply anywhere after
the menu is what routes, and the approval is then only a sign-off. It is spent once under its own
id (`review:<id>`), through the same `unpark.sh`, which re-checks it at execution time
(`v2_owner_approved`), and the runner names it to MODE=revise (`ANSWER_KIND_PR=approval`).

The split mirrors `observe`: `parse` and `approval` are pure and hold everything worth arguing
about, `read_thread` does the GitHub reads.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import github

LOG = logging.getLogger("lemd.answers")

#: Verdicts that un-park. The other two (`hold`, `question`) are recognised precisely so they can
#: be refused loudly rather than falling through to "not an answer" and looking like silence.
ACTIONABLE = frozenset({"answer", "directive", "approval"})

#: The answer id an approval carries: its REST review id in a namespace of its own, so it can never
#: collide with a comment node id and `unpark.sh` knows which execution-time re-check to run.
APPROVAL_PREFIX = "review:"

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


@dataclass(frozen=True)
class Thread:
    """What ONE read of a held item's comments says, both halves from the same call (#1736).

    `answer` is the owner's newest reply to the latest Decision Comment, exactly as `newest`
    returns it. `menu_posted` is whether a Decision Comment exists on the thread AT ALL — the fact
    that tells "held and asked" from "held and never asked". Three-valued on purpose: `None` is an
    unreadable thread, and an unreadable thread is never evidence that no menu exists.
    """

    answer: Answer | None = None
    menu_posted: bool | None = None


#: What an unreadable thread reads as: no answer, and NO claim about the menu either way.
UNREADABLE = Thread(answer=None, menu_posted=None)


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


def menu_posted(comments: list[dict[str, Any]], owner: str = "",
                trusted: Callable[[str], bool] | None = None) -> bool:
    """Does this thread carry a Decision Comment — has a question ever been put to the owner?

    Recognised by BODY, the same marker `parse` and `common.sh`'s `v2_owner_answered` key on, from
    an author who may ask (`_may_ask`): the App's slug, the owner, or a trusted author. Both logins
    the pipeline has posted under qualify — the owner's own PAT identity and the App bot — so a
    PAT-era menu is still the question the answer lane reads, and #1736's double menu cannot come
    back. What no longer qualifies is anybody else quoting the phrase: an outsider's "Human
    decision needed" would otherwise read as "already asked" and suppress the real menu for good.
    Lookups are lazy, newest first, and only for decision-text comments from neither the owner
    nor the App's slug.
    """
    return any(_DECISION.search(c.get("body") or "")
               and _may_ask(c.get("author") or {}, owner, trusted)
               for c in reversed(comments))


def _may_ask(author: dict[str, Any], owner: str,
             trusted: Callable[[str], bool | None] | None) -> bool:
    """May this author's Decision Comment open the answer window?

    The pipeline (by the App's slug — this payload is untyped — or under the PAT identity, the
    owner) or a trusted author. Anybody else's "Human decision needed" is an ordinary comment:
    counted as a window boundary it would push a trusted answer out of the window. Matching the
    App by its bare slug is safe HERE, unlike for review evidence: a boundary can only NARROW the
    window, so a spoofed one can keep an item parked, never release it.
    """
    login = str(author.get("login") or "")
    if not login:
        return False
    if login == owner or github.is_pipeline_app(author) or github.names_pipeline_app(login):
        return True
    return trusted is not None and bool(trusted(login))


def parse(comments: list[dict[str, Any]], owner: str,
          trusted: Callable[[str], bool] | None = None) -> Answer | None:
    """The newest TRUSTED reply to the LATEST Decision Comment in a thread, classified.

    `comments` is GitHub's list in chronological order. `trusted(login)` says whether a non-owner
    author may answer (and may ask: see `_may_ask`); it is injected so this stays testable without
    GitHub, and `read_thread` passes `github.comment_author_trusted`. Left out, only the owner
    answers. One walk, newest first: the first trusted reply is the candidate, and the first
    Decision Comment from someone who may ask closes the window. Lookups are lazy — a thread costs
    one per distinct untrusted commenter at most, and none when the owner replied last.

    `v2_owner_answered` in `actions/common.sh` walks the same way and must name the same comment.

    Returns:
        None when the thread holds no Decision Comment from someone who may ask, no trusted reply
        after it, or a reply that is not a decision.
    """
    answer: Answer | None = None
    for c in reversed(comments):
        body = c.get("body") or ""
        author = c.get("author") or {}
        if _DECISION.search(body):
            if _may_ask(author, owner, trusted):
                return answer
            continue
        if answer is not None or _AGENT.search(body):
            continue
        login = str(author.get("login") or "")
        if not login or github.is_pipeline_app(author) or github.names_pipeline_app(login):
            continue
        if login != owner and not (trusted is not None and trusted(login)):
            continue
        v = verdict_for(body)
        if v is None:
            # The newest trusted comment is not a decision. Stop rather than reaching further back:
            # an older answer that has since been talked past is not a live instruction.
            return None
        answer = Answer(
            comment_id=str(c.get("id") or c.get("url") or ""),
            verdict=v,
            excerpt=" ".join(body.split())[:60],
        )
    # No Decision Comment from anyone who may ask: nothing was asked, so nothing is answered.
    return None


def _at(value: Any) -> float:
    """ISO-8601 to epoch seconds; 0.0 for absent or unparseable, which no comparison below trusts."""
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _window(comments: list[dict[str, Any]], owner: str,
            trusted: Callable[[str], bool | None] | None, submitted: float) -> tuple[float, bool]:
    """When the latest Decision Comment was posted, and whether a reply in its window BLOCKS an approval.

    `parse`'s walk: the same window boundary (`_may_ask`) and the same reply filter. Two kinds of
    reply block an approval submitted at `submitted`, and nothing else does:

    * an ACTIONABLE trusted reply (`verdict_for` says `answer`/`directive`) ANYWHERE in the window.
      An explicit instruction is never overridden by a sign-off, in either order: before the
      approval `parse` routes it; after the approval it is the newer act anyway;
    * ANY reply at or after the approval whose author is trusted OR unreadable (`trusted` returned
      None), and any trusted reply whose time is unreadable. A remark is not an answer, but it is the
      owner speaking after the approval ("hold off" typed after approving still holds), and a reply
      we cannot place or attribute must never be jumped.

    `trusted(login)` is three-valued here: True, False, or None for a lookup that failed. Anywhere
    else (the window boundary, a reply before the approval) None reads as untrusted, as in `parse`.

    Returns:
        `(asked, blocked)`. `asked` is epoch seconds, 0.0 when no menu from anyone who may ask is on
        the thread or its time is unreadable.
    """
    blocked = False
    for c in reversed(comments):
        body = c.get("body") or ""
        author = c.get("author") or {}
        if _DECISION.search(body):
            if _may_ask(author, owner, trusted):
                return _at(c.get("createdAt")), blocked
            continue
        if blocked or _AGENT.search(body):
            continue
        login = str(author.get("login") or "")
        if not login or github.is_pipeline_app(author) or github.names_pipeline_app(login):
            continue
        standing = True if login == owner else (trusted(login) if trusted is not None else False)
        if standing is not None and not standing:
            continue
        # An unreadable time reads as "now": it can never let an approval jump the reply.
        after = (_at(c.get("createdAt")) or float("inf")) >= submitted
        if after or (standing is not None and verdict_for(body) in ACTIONABLE):
            blocked = True
    return 0.0, blocked


def _approver(user: dict[str, Any], owner: str, answer_logins: frozenset[str]) -> bool:
    """May this REVIEW author's approval answer? The owner, or a listed answer login — typed.

    Narrower than a comment answer on purpose: an approval stands for "take every recommended
    option", the owner's sign-off, so a write collaborator's approval is not one. REST types the
    author, so a Bot is refused outright (a listed App included), and the App never answers.
    """
    login = str(user.get("login") or "")
    if not login or user.get("type") != "User" or github.names_pipeline_app(login):
        return False
    return (bool(owner) and login.lower() == owner.lower()) or login.lower() in answer_logins


def approval(comments: list[dict[str, Any]], reviews: list[dict[str, Any]], head_sha: str,
             owner: str, *, answer_logins: frozenset[str] = frozenset(),
             trusted: Callable[[str], bool | None] | None = None) -> Answer | None:
    """The owner's GitHub APPROVED review as the answer `ok` to a held PR's latest Decision Comment.

    `reviews` is REST `pulls/{n}/reviews` (typed `user`, `state`, `commit_id`, `submitted_at`). The
    approver's NEWEST submitted review decides: a later `CHANGES_REQUESTED` or `COMMENTED` review
    takes the approval back, and a dismissed one reads `DISMISSED`. It counts only when it is
    `APPROVED`, on `head_sha` (an approval of an older head approved code that is no longer the PR),
    submitted after the latest Decision Comment, and only when `_window` finds nothing blocking it:
    no actionable trusted reply anywhere after that menu (an explicit answer always wins over a
    sign-off, whichever came first), and no trusted or unattributable reply at or after the
    approval, so "hold off" typed after approving still holds. A hold phrase in the review's own
    body holds too (`verdict="hold"`), exactly as it does in a comment.

    Returns:
        An `Answer` whose id is `review:<id>`, or None, including for anything unreadable.
    """
    if not head_sha or not owner:
        return None
    mine = [r for r in reviews if _approver(r.get("user") or {}, owner, answer_logins)
            and _at(r.get("submitted_at"))]
    if not mine:
        return None
    newest = max(mine, key=lambda r: _at(r.get("submitted_at")))
    if (str(newest.get("state") or "").upper() != "APPROVED"
            or str(newest.get("commit_id") or "") != head_sha or not newest.get("id")):
        return None
    submitted = _at(newest.get("submitted_at"))
    asked, blocked = _window(comments, owner, trusted, submitted)
    if not asked or submitted <= asked or blocked:
        return None
    return Answer(comment_id=f"{APPROVAL_PREFIX}{newest['id']}",
                  verdict="hold" if _HOLD.search(newest.get("body") or "") else "approval",
                  excerpt=f"APPROVED review at {head_sha[:7]}")


def _reviews(slug: str, number: int, *, timeout: int) -> list[dict[str, Any]] | None:
    """Every review on a PR over REST, whose authors are TYPED; None when unreadable."""
    try:
        pages = github.gh_json(
            ["api", f"repos/{slug}/pulls/{number}/reviews", "--paginate", "--slurp"],
            timeout=timeout,
        )
    except github.GitHubUnavailable as exc:
        LOG.warning("PR #%s reviews unreadable — an approval is not an answer: %s", number, exc)
        return None
    if not isinstance(pages, list):
        return None
    return [r for page in pages if isinstance(page, list) for r in page if isinstance(r, dict)]


def read_thread(slug: str, kind: str, number: int, owner: str, *,
                answer_logins: frozenset[str] = frozenset(), head_sha: str | None = None,
                timeout: int = 30) -> Thread:
    """Read one thread ONCE: the newest trusted reply, and whether a menu was ever posted.

    One API call, made only for items already carrying a hold label — every other observation
    reaches its decision without it. Both facts come off the same read so #1736's `menu_posted`
    costs nothing the answer lane was not already paying. A held PR with a known `head_sha` and a
    menu on its thread costs ONE more read, its reviews, because an owner approval is the answer
    `ok` there (`approval`). An approval qualifies only when no actionable trusted reply sits after
    the menu, so it never outranks an explicit comment answer; an unreadable review read is simply
    no approval.

    Returns:
        `UNREADABLE` when the thread could not be read: no answer, and `menu_posted=None`. The
        answer half of that collapse is safe — "no answer" keeps the item parked, which is right
        for an unreadable thread. The menu half is why this is three-valued: `None` is what stops
        an unreadable thread being read as "no menu yet" and posting one.
    """
    try:
        facts = github.gh_json(
            [kind, "view", str(number), "--repo", slug, "--json", "comments"],
            timeout=timeout,
        ) or {}
    except github.GitHubUnavailable as exc:
        LOG.warning("%s #%s comments unreadable — staying parked: %s", kind, number, exc)
        return UNREADABLE
    comments = list(facts.get("comments") or [])
    # A listed App (`<slug>[bot]`) shows up here as its bare slug, indistinguishable from a human
    # who registered that login. Re-type those comments over REST; only a proven Bot gets the
    # `[bot]` login back, so an impostor's bare login never matches a bot entry.
    bot_slugs = {e[:-5] for e in answer_logins if e.endswith("[bot]")}
    for comment in comments:
        author = comment.get("author") or {}
        login = str(author.get("login") or "").lower()
        if login in bot_slugs:
            verified = github.verified_bot_login(slug, str(comment.get("url") or comment.get("html_url") or ""),
                                                 timeout=timeout)
            if verified == f"{login}[bot]":
                comment["author"] = {**author, "login": verified}

    def trusted(login: str) -> bool:
        # `answer_logins` is LEMD_TRUSTED_ANSWER_LOGINS, already stripped of the App at parse time;
        # the App check is repeated here so no caller can hand it back in.
        if login.lower() in answer_logins and not github.names_pipeline_app(login):
            return True
        return github.comment_author_trusted(slug, login, owner, timeout=timeout)

    def standing(login: str) -> bool | None:
        # `trusted`, but a failed permission lookup is None, so a reply after an approval that we
        # cannot attribute blocks it instead of being skipped as untrusted (`_window`).
        if login.lower() in answer_logins and not github.names_pipeline_app(login):
            return True
        return github.comment_author_standing(slug, login, owner, timeout=timeout)

    asked = menu_posted(comments, owner, trusted)
    answer = None
    if kind == "pr" and head_sha and asked:
        reviews = _reviews(slug, number, timeout=timeout)
        if reviews is not None:
            answer = approval(comments, reviews, head_sha, owner,
                              answer_logins=answer_logins, trusted=standing)
    return Thread(answer=answer or parse(comments, owner, trusted), menu_posted=asked)


def newest(slug: str, kind: str, number: int, owner: str, *,
           timeout: int = 30) -> Answer | None:
    """Read one thread and classify the owner's newest reply.

    The answer half of `read_thread`, kept for callers that only want the reply.

    Returns:
        None both when there is no answer and when the thread could not be read. That collapse is
        safe here and only here: the consequence of "no answer" is that the item stays parked,
        which is exactly the right outcome for an unreadable thread.
    """
    return read_thread(slug, kind, number, owner, timeout=timeout).answer
