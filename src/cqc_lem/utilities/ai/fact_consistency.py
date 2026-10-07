"""Date, timeline and provenance consistency for generated posts (showcase round 6).

The fact gates already ask "did the model invent this number?". This module asks the questions a
sharp reader asks next, all of them answerable from a calendar and the story the post is anchored
to — and each of them shipped in the round-6 showcase:

- a weekday that is not that date's ("Wednesday, June 22, 2026" is a Monday) —
  `weekday_mismatches`, repaired deterministically by `fix_weekday_mismatches` (the weekday is
  DROPPED, never "corrected": the date itself may be the wrong half);
- a deadline set before the offer that set it ("On February 14 2026 I offered … aiming to finish
  in January") — `deadline_contradictions`;
- more elapsed time than has passed since the anchoring story happened ("within the first month we
  saw a 30% drop" about a story dated six days ago) — `timeline_violations`.

`consistency_report` bundles the three for the review gate and the gate pass. Two text helpers sit
beside them because they serve the same posture — what ships must be true of the author:
`strip_signature_lines` cuts a leaked job-title sign-off ("Senior Applied AI & Full-Stack
Engineer") and `named_source_material` keeps only the research sentences that name their source,
so an unsourced statistic in the research block is never an allow-list entry for a post.

Pure: no LLM, no DB, no network. Every check fails OPEN on input it cannot read — an unparseable
date is not evidence of an impossible one.
"""

import calendar
import re
from datetime import date, datetime
from typing import Any, Optional

_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december")
_MONTH_INDEX = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_MONTH_INDEX.update({name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)})
_MONTH_INDEX["sept"] = 9
_MONTH_RE = (r"(?:january|february|march|april|may|june|july|august|september|october|november|"
             r"december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")

_WEEKDAY_INDEX = {name: i for i, name in enumerate(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
_WEEKDAY_INDEX.update({"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
                       "thurs": 3, "fri": 4, "sat": 5, "sun": 6})
_WEEKDAY_RE = (r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|"
               r"thurs|thur|thu|fri|sat|sun)")
# `\s` matches the narrow no-break space (U+202F) LLMs put inside dates, so a plain space class is
# enough; the weekday may be followed by a comma and "the".
_WEEKDAY_DATE_RES = (
    # Wednesday, June 22, 2026 / Wed. June 22nd 2026
    re.compile(r"\b(?P<wd>" + _WEEKDAY_RE + r")\.?,?\s+(?:the\s+)?(?P<month>" + _MONTH_RE
               + r")\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>(?:19|20)\d{2})\b",
               re.IGNORECASE),
    # Wednesday, 22 June 2026 / Wednesday the 22nd of June, 2026
    re.compile(r"\b(?P<wd>" + _WEEKDAY_RE + r")\.?,?\s+(?:the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)?"
               r"\s+(?:of\s+)?(?P<month>" + _MONTH_RE + r")\.?,?\s+(?P<year>(?:19|20)\d{2})\b",
               re.IGNORECASE),
)

# A full date in prose: "February 14 2026", "Feb 14, 2026", "14 February 2026".
_FULL_DATE_RES = (
    re.compile(r"\b(?P<month>" + _MONTH_RE + r")\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
               r"(?P<year>(?:19|20)\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>" + _MONTH_RE
               + r")\.?,?\s+(?P<year>(?:19|20)\d{2})\b", re.IGNORECASE),
)
# A deadline: a completion verb, then within a few words "in/by <Month>" — the month NOT followed by
# a day number (that is a full date, read by the patterns above).
_DEADLINE_RE = re.compile(
    r"\b(?:finish|finished|finishing|complete|completed|completing|deliver|delivered|delivering|delivery|"
    r"launch|launched|launching|ship|shipped|shipping|wrap(?:ped)?\s+up|go(?:ing)?\s+live|done|"
    r"ready|due|deadline|wrapping\s+up)\b[^.!?\n]{0,40}?\b(?:in|by|before|for|until)\s+"
    r"(?:early\s+|mid[-\s]?|late\s+|the\s+end\s+of\s+)?(?P<month>" + _MONTH_RE
    + r")\b(?!\.?\s*\d{1,2}\b)(?:,?\s+(?P<year>(?:19|20)\d{2}))?", re.IGNORECASE)
# Deadlines more than this many months AHEAD of the offer read as "earlier in the same year": an
# offer in December to finish "in January" is next month, one in February to finish "in January"
# is eleven months out — what a reader takes as an impossible date.
DEADLINE_MAX_FORWARD_MONTHS = 6

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_COUNT_RE = r"(?P<n>\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_UNIT_RE = r"(?P<unit>days?|weeks?|months?|quarters?|years?)"
_ELAPSED_RES = (
    # within the first month / after the first quarter / by the end of the first year
    re.compile(r"\b(?:within|in|during|over|after|by\s+the\s+end\s+of)\s+(?:the\s+|our\s+|my\s+)?"
               r"first\s+(?P<unit>day|week|month|quarter|year)\b", re.IGNORECASE),
    # after three months / within 6 weeks / over two years
    re.compile(r"\b(?:after|within|over|for|in)\s+(?:just\s+|only\s+)?" + _COUNT_RE + r"\s+"
               + _UNIT_RE + r"\b", re.IGNORECASE),
    # three months later / a year later
    re.compile(r"\b" + _COUNT_RE + r"\s+" + _UNIT_RE + r"\s+later\b", re.IGNORECASE),
)
# A sentence that reports the AUTHOR's own past — the only kind the story's date bounds.
_FIRST_PERSON_RE = re.compile(r"\b(?:i|i'm|i've|i'd|me|my|we|we're|we've|our|us)\b", re.IGNORECASE)
# A plan, not a result: "we will finish in three months" is not a claim about elapsed time.
_FUTURE_RE = re.compile(r"\b(?:will|won't|'ll|going\s+to|plan(?:s|ning)?\s+to|aim(?:s|ing)?\s+to|"
                        r"expect(?:s|ing)?\s+to|hope(?:s)?\s+to|next|could|would|should|might|"
                        r"if\s+you|you'll|your)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
# One day of slack on every elapsed-time comparison: "a week later" six days on is not a lie.
ELAPSED_SLACK_DAYS = 1


def _sentences(text: Optional[str]) -> list:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _date_or_none(year: Any, month: Any, day: Any) -> Optional[date]:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _month(value: str) -> Optional[int]:
    return _MONTH_INDEX.get(str(value or "").lower().rstrip("."))


def as_date(value: Any) -> Optional[date]:
    """A story's `happened_at` (date, datetime or ISO string) as a date; None when unreadable."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


# --- (a) weekday ≠ date ----------------------------------------------------------------------------

def weekday_mismatches(text: Optional[str]) -> list:
    """Every "Weekday, Month D, YYYY" in `text` whose weekday is not that date's.

    Args:
        text: The draft.

    Returns:
        ``{phrase, stated, actual, start, end}`` dicts in order; ``start``/``end`` span the weekday
        and its trailing comma/space, which is what `fix_weekday_mismatches` removes.
    """
    found = []
    seen = set()
    for rx in _WEEKDAY_DATE_RES:
        for m in rx.finditer(text or ""):
            if m.start() in seen:
                continue
            when = _date_or_none(m.group("year"), _month(m.group("month")), m.group("day"))
            stated = _WEEKDAY_INDEX.get(m.group("wd").lower())
            if when is None or stated is None or when.weekday() == stated:
                continue
            seen.add(m.start())
            # The weekday token, its optional '.', ',' and the whitespace after it.
            head = re.match(r"\w+\.?,?\s+(?:the\s+)?", m.group(0), re.IGNORECASE)
            found.append({"phrase": " ".join(m.group(0).split()),
                          "stated": m.group("wd"),
                          "actual": calendar.day_name[when.weekday()],
                          "start": m.start(), "end": m.start() + (head.end() if head else 0)})
    return sorted(found, key=lambda f: f["start"])


def fix_weekday_mismatches(text: Optional[str]) -> tuple:
    """`text` with every wrong weekday DROPPED from its date. Deterministic.

    Dropping, never correcting: when a weekday and a date disagree we cannot know which half the
    model got wrong, and "June 22, 2026" alone is at worst as wrong as the draft already was.

    Returns:
        ``(text, phrases)`` — the repaired text and the phrases that were changed.
    """
    if not text:
        return text, []
    hits = weekday_mismatches(text)
    out = text
    for hit in reversed(hits):
        out = out[:hit["start"]] + out[hit["end"]:]
    return out, [h["phrase"] for h in hits]


# --- (b) impossible deadlines and elapsed time ----------------------------------------------------

def _full_dates(sentence: str) -> list:
    out = []
    for rx in _FULL_DATE_RES:
        for m in rx.finditer(sentence):
            when = _date_or_none(m.group("year"), _month(m.group("month")), m.group("day"))
            if when:
                out.append((m.start(), when))
    return [d for _, d in sorted(out, key=lambda pair: pair[0])]


def deadline_contradictions(text: Optional[str]) -> list:
    """Deadlines that fall BEFORE the dated offer in the same sentence.

    "On February 14 2026 I offered … aiming to finish in January": a deadline month without a year
    reads as the same year when it is more than `DEADLINE_MAX_FORWARD_MONTHS` ahead of the offer
    (so December → "in January" passes); a deadline with a year is compared exactly.

    Returns:
        One plain-English issue per contradiction.
    """
    issues = []
    for sentence in _sentences(text):
        dates = _full_dates(sentence)
        if not dates:
            continue
        anchor = dates[0]
        for m in _DEADLINE_RE.finditer(sentence):
            month = _month(m.group("month"))
            if month is None:
                continue
            if m.group("year"):
                before = (int(m.group("year")), month) < (anchor.year, anchor.month)
            else:
                forward = (month - anchor.month) % 12
                before = month != anchor.month and forward > DEADLINE_MAX_FORWARD_MONTHS
            if before:
                issues.append(f"\"{' '.join(m.group(0).split())}\" sets a deadline before the "
                              f"{anchor.strftime('%B %d, %Y').replace(' 0', ' ')} date in the same "
                              f"sentence")
    return issues


def elapsed_claims(text: Optional[str]) -> list:
    """The first-person, past-tense elapsed-time claims in `text`, as ``{phrase, days}`` dicts.

    Only sentences about the author's own past are read (first person, no future/plan wording):
    "teams usually see results within six months" is not a claim about the story.
    """
    claims = []
    for sentence in _sentences(text):
        if not _FIRST_PERSON_RE.search(sentence) or _FUTURE_RE.search(sentence):
            continue
        for rx in _ELAPSED_RES:
            for m in rx.finditer(sentence):
                unit = m.group("unit").lower().rstrip("s")
                raw_n = (m.groupdict().get("n") or "1").lower()
                n = int(raw_n) if raw_n.isdigit() else _NUMBER_WORDS.get(raw_n, 1)
                claims.append({"phrase": " ".join(m.group(0).split()),
                               "days": n * _UNIT_DAYS[unit]})
    return claims


def timeline_violations(text: Optional[str], happened_at: Any, now: Any = None) -> list:
    """Elapsed-time claims longer than the time since the anchoring story happened.

    A "30% drop within the first month" about a story six days old is impossible: the month has not
    passed. Returns nothing when the story's date is unknown, unreadable or in the future — an
    unreadable date is never evidence.

    Args:
        text: The draft.
        happened_at: The story entry's ``happened_at``.
        now: Today (a date/datetime); the real today by default.

    Returns:
        One plain-English issue per violation.
    """
    start = as_date(happened_at)
    today = as_date(now) or date.today()
    if start is None or start > today:
        return []
    elapsed = (today - start).days
    issues = []
    for claim in elapsed_claims(text):
        if claim["days"] > elapsed + ELAPSED_SLACK_DAYS:
            issues.append(f"\"{claim['phrase']}\" claims about {claim['days']} days of results, but "
                          f"the story it tells happened {elapsed} day(s) ago "
                          f"({start.strftime('%B %d, %Y').replace(' 0', ' ')})")
    return issues


def consistency_report(text: Optional[str], happened_at: Any = None, now: Any = None) -> dict:
    """Every date/timeline consistency check on one draft. Deterministic.

    Args:
        text: The draft as it would ship.
        happened_at: The anchoring story's date, or None (the timeline check is then skipped).
        now: Today, for the timeline check.

    Returns:
        ``{passes, issues, weekday, deadline, timeline}`` — ``issues`` is the plain-English list a
        finding and a repair brief carry.
    """
    weekday = [f"\"{w['phrase']}\": that date is a {w['actual']}, not a {w['stated']}"
               for w in weekday_mismatches(text)]
    deadline = deadline_contradictions(text)
    timeline = timeline_violations(text, happened_at, now) if happened_at else []
    issues = weekday + deadline + timeline
    return {"passes": not issues, "issues": issues, "weekday": weekday, "deadline": deadline,
            "timeline": timeline}


# --- Leaked sign-offs ------------------------------------------------------------------------------

_TITLE_WORDS_RE = re.compile(
    r"\b(?:engineer|developer|architect|founder|co-founder|cofounder|ceo|cto|cfo|coo|cmo|"
    r"consultant|advisor|adviser|director|manager|officer|specialist|strategist|lead|head\s+of|"
    r"principal|partner|president|vp|scientist|analyst|designer|coach|owner|freelancer)\b",
    re.IGNORECASE)
_SIGNATURE_MAX_WORDS = 12


def _is_signature_line(line: str) -> bool:
    stripped = line.strip().strip("-–—•*_ ")
    if not stripped or stripped[-1] in ".!?:" or stripped.startswith(("#", "http")):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'’&.-]*", stripped)
    if not words or len(words) > _SIGNATURE_MAX_WORDS or not _TITLE_WORDS_RE.search(stripped):
        return False
    capitalised = [w for w in words if w[0].isupper() or w.isupper()]
    return len(capitalised) * 2 >= len(words)


def strip_signature_lines(text: Optional[str]) -> Optional[str]:
    """`text` without a trailing job-title sign-off ("Senior Applied AI & Full-Stack Engineer").

    A post is signed by the author's profile, never in its body. Only the LAST line(s) are read, a
    line qualifies only when it is short, unpunctuated, mostly Title Case and names a role, and
    at least one other paragraph must remain — so a closing sentence is never cut.

    Args:
        text: The post.

    Returns:
        The post, possibly shorter; unchanged when nothing qualifies.
    """
    if not text:
        return text
    lines = text.rstrip().split("\n")
    cut = False
    while len([ln for ln in lines if ln.strip()]) > 1 and _is_signature_line(lines[-1]):
        lines.pop()
        cut = True
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).rstrip() if cut else text


# --- Research with a named source ----------------------------------------------------------------

# Words that open a sentence or a noun phrase but name nobody: "The report", "A 2025 survey".
_NOT_A_NAME = (r"(?!(?:The|This|That|These|Those|A|An|Our|Their|Its|One|New|Recent|Latest|Some|"
               r"Many|Most|Another|Each|Every|Industry|Market|Internal)\b)")
_SOURCE_NOUN = r"(?:survey|study|report|research|poll|index|census|analysis|data|benchmark|findings)"
_NAMED_SOURCE_RE = re.compile(
    r"\[\d{1,2}\]|https?://|\b(?i:according\s+to|sources?:|cited\s+by|reported\s+by)|"
    r"\b(?i:per|via)\s+(?:the\s+)?" + _NOT_A_NAME + r"[A-Z][\w&.'-]+|"
    r"\b" + _SOURCE_NOUN + r"\s+(?:by|from|of)\s+(?:the\s+)?" + _NOT_A_NAME + r"[A-Z][\w&.'-]+|"
    r"\(\s*[A-Z][\w&.'\- ]{1,40},?\s*(?:19|20)\d{2}\s*\)|"
    r"\b" + _NOT_A_NAME + r"[A-Z][\w&.'-]+(?:\s+[A-Z][\w&.'-]+){0,3}(?:'s)?\s+"
    r"(?:(?:19|20)\d{2}\s+)?" + _SOURCE_NOUN + r"\b")


def named_source_material(text: Optional[str]) -> str:
    """Only the sentences of a research block that NAME their source.

    The post's number allow-list takes research numbers on trust; an unsourced research line
    ("79% of enterprises still bleed money") is not a source, it is the same invention one hop
    earlier. A sentence counts when it carries a citation marker ("[2]"), a URL, "according to", a
    "(Publisher, 2025)" cite, or a named publisher's survey/report/study.

    Args:
        text: The research block as handed to the writer.

    Returns:
        The sourced sentences, one per line; '' when none name a source.
    """
    return "\n".join(s for s in _sentences(text) if _NAMED_SOURCE_RE.search(s))
