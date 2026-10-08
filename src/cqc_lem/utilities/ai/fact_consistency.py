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

Showcase round 8 adds two more: a dated first-person event reported with results it had no time
to produce (`dated_outcome_violations`), and two totals of one thing that do not agree
(`count_conflicts`).

`consistency_report` bundles them for the review gate and the gate pass. Two text helpers sit
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


# --- (c) a dated event with no time for its results (showcase round 8) ---------------------------

# slot_125: "On Oct 1 2026 I helped three startups…" and, a week later, "those teams stayed stable,
# delivered on schedule". A dated event reported with OUTCOMES must be at least this old.
OUTCOME_MIN_DAYS = 14
_OUTCOME_RE = re.compile(
    r"\b(?:stayed|delivered|saved|cut|reduced|grew|increased|dropped|improved|avoided|achieved|"
    r"doubled|tripled|halved|paid\s+off|went\s+from|turned\s+into|the\s+results?\s+(?:was|were)|"
    r"resulted\s+in|ended\s+up)\b", re.IGNORECASE)


def dated_outcome_violations(text: Optional[str], now: Any = None) -> list:
    """A first-person event dated too recently for the results the post reports after it.

    The date is the post's own ("On Oct 1 2026 I…"), so this needs no story-bank date. Only the
    sentences AFTER the dated one are read for outcomes, and a plan or a conditional ("could",
    "will", "your") is never one. Fails open on a date it cannot read or one in the future.

    Args:
        text: The draft.
        now: Today (the generation date); the real today by default.

    Returns:
        One plain-English issue per dated event.
    """
    today = as_date(now) or date.today()
    sentences = _sentences(text)
    issues = []
    for i, sentence in enumerate(sentences):
        if not _FIRST_PERSON_RE.search(sentence):
            continue
        for when in _full_dates(sentence):
            age = (today - when).days
            if age < 0 or age >= OUTCOME_MIN_DAYS:
                continue
            outcome = next((m.group(0) for s in sentences[i + 1:]
                            if not _FUTURE_RE.search(s) for m in [_OUTCOME_RE.search(s)] if m), "")
            if outcome:
                issues.append(f"the post dates its story {when.strftime('%B %d, %Y').replace(' 0', ' ')} "
                              f"({age} day(s) ago) yet reports its results (\"{outcome}\"): "
                              f"date it as a span that leaves time for them, or drop the result")
                break
    return issues


# --- (d) counts that do not reconcile inside one post (showcase round 8) ---------------------------

# slot_143: "I sent 51 cold emails" then "Out of 63 recipients". Two TOTALS of the same kind of thing
# in one post must agree, or one sentence must state both (which explains the difference).
_COUNT_CLASSES = {
    "outreach": ("emails", "email", "e-mails", "messages", "message", "dms", "recipients",
                 "recipient", "prospects", "prospect", "contacts", "contact", "leads", "invites",
                 "invitations", "pitches"),
}
_COUNT_NOUN_TO_CLASS = {noun: cls for cls, nouns in _COUNT_CLASSES.items() for noun in nouns}
_COUNT_NOUN_RE = "|".join(sorted((re.escape(n) for n in _COUNT_NOUN_TO_CLASS), key=len,
                                 reverse=True))
_COUNT_MODIFIERS = r"(?:[a-z][a-z'-]*\s+){0,2}?"
_TOTAL_COUNT_RES = (
    # Round 9: slot_135 wrote "after sending 51 cold emails" — the round-8 verbs were past tense
    # only, so the total was never read and 51 vs 63 shipped unreconciled.
    re.compile(r"\b(?:sent|send|sends|sending|emailed|emailing|messaged|messaging|contacted|"
               r"contacting|pitched|pitching|reached\s+out\s+to|reaching\s+out\s+to|wrote|"
               r"writing)\s+"
               r"(?:out\s+)?(?P<n>\d[\d,]*)\s+" + _COUNT_MODIFIERS + r"(?P<noun>" + _COUNT_NOUN_RE
               + r")\b", re.IGNORECASE),
    re.compile(r"\bout\s+of\s+(?:the\s+|those\s+|all\s+|my\s+)?(?P<n>\d[\d,]*)\s+"
               + _COUNT_MODIFIERS + r"(?P<noun>" + _COUNT_NOUN_RE + r")\b", re.IGNORECASE),
    re.compile(r"\ball\s+(?P<n>\d[\d,]*)\s+" + _COUNT_MODIFIERS + r"(?P<noun>" + _COUNT_NOUN_RE
               + r")\b", re.IGNORECASE),
)


def count_conflicts(text: Optional[str]) -> list:
    """Two different totals of the same kind of thing, stated in different sentences.

    Only TOTALS are read ("sent 51 emails", "out of 63 recipients", "all 40 leads"), so a subset
    ("3 emails bounced") never conflicts with its whole. A sentence that states both numbers
    relates them itself ("51 emails to 63 recipients") and is never flagged.

    Args:
        text: The draft (a deck's caption plus its slides reads as one).

    Returns:
        One plain-English issue per conflicting pair.
    """
    totals: dict = {}
    for index, sentence in enumerate(_sentences(text)):
        for rx in _TOTAL_COUNT_RES:
            for m in rx.finditer(sentence):
                cls = _COUNT_NOUN_TO_CLASS.get(m.group("noun").lower())
                value = m.group("n").replace(",", "")
                totals.setdefault(cls, []).append((value, " ".join(m.group(0).split()), index,
                                                   sentence))
    issues = []
    for cls, found in totals.items():
        seen: dict = {}
        for value, phrase, index, sentence in found:
            other = next((f for v, f in seen.items() if v != value
                          and f[1] != index and value not in _numbers_in(f[2])
                          and v not in _numbers_in(sentence)), None)
            if other:
                issues.append(f"\"{other[0]}\" and \"{phrase}\" count the same {cls} differently: "
                              f"reconcile the two totals, or say in one sentence how they relate")
                break
            seen.setdefault(value, (phrase, index, sentence))
    return issues


def _numbers_in(sentence: str) -> set:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", sentence or "")}


# --- (e) one story per post (showcase round 9) ------------------------------------------------------

# slot_130 opened on the story-bank anchor (an AI agent negotiating a Lincoln Nautilus) and then
# told a second, unrelated case ("A client needed trustworthy AI for a critical platform…") with
# its own invented result. A paragraph that OPENS a different protagonist's case, on a post whose
# anchor never mentions that protagonist, is a second story.
_CASE_OPENER_RE = re.compile(
    r"^\W*(?:(?:A|One|Another)\s+(?:of\s+my\s+)?|Last\s+\w+,?\s+(?:a|one)\s+|"
    r"(?:I|We)\s+(?:recently\s+)?(?:worked\s+with|helped|advised|consulted\s+for)\s+(?:a|an|one|"
    r"another)\s+)(?:[a-z-]+\s+){0,2}?(?P<who>clients?|customers?|compan(?:y|ies)|startups?|"
    r"retailers?|agenc(?:y|ies)|founders?|teams?|prospects?|brands?|firms?)\b")


def second_story_issues(text: Optional[str], story_text: Optional[str]) -> list:
    """A paragraph telling a second case beside the post's ONE story-bank anchor. Deterministic.

    Args:
        text: The draft.
        story_text: The anchoring story-bank entry's title and body; None or '' skips the check
            (a post with no anchor has no story to stay inside).

    Returns:
        One plain-English issue for the first second-story opener found.
    """
    if not text or not (story_text or "").strip():
        return []
    anchor = (story_text or "").lower()
    for paragraph in re.split(r"\n\s*\n", text):
        line = " ".join(paragraph.split())
        match = _CASE_OPENER_RE.match(line)
        if not match:
            continue
        who = match.group("who").lower()
        stem = who[:4]
        if stem in anchor:
            continue
        opener = " ".join(line.split()[:8])
        return [f"the post tells a second story (\"{opener}…\") beside its story-bank anchor: "
                f"tell only the anchor's story, or drop that paragraph"]
    return []


def consistency_report(text: Optional[str], happened_at: Any = None, now: Any = None,
                       hook_facts: Optional[list] = None,
                       hook_flagged: Optional[list] = None,
                       story_text: Optional[str] = None) -> dict:
    """Every date/timeline/count consistency check on one draft. Deterministic.

    Args:
        text: The draft as it would ship.
        happened_at: The anchoring story's date, or None (the timeline check is then skipped).
        now: Today, for the timeline checks.
        hook_facts: The story-bank facts a figure in the hook may come from (showcase round 7,
            ``hook_provenance_issues``). None skips the hook check — only a caller holding the
            allow-list can run it.
        hook_flagged: Figures the fact-grounding gate already holds the post for.
        story_text: The anchoring story-bank entry's text; a second story beside it is a finding
            (``second_story_issues``, round 9). None skips that check.

    Returns:
        ``{passes, issues, weekday, deadline, timeline, provenance, counts}`` — ``issues`` is the
        plain-English list a finding and a repair brief carry.
    """
    weekday = [f"\"{w['phrase']}\": that date is a {w['actual']}, not a {w['stated']}"
               for w in weekday_mismatches(text)]
    deadline = deadline_contradictions(text)
    timeline = timeline_violations(text, happened_at, now) if happened_at else []
    timeline += dated_outcome_violations(text, now)
    provenance = (hook_provenance_issues(text, hook_facts, hook_flagged)
                  if hook_facts is not None else [])
    counts = count_conflicts(text)
    stories = second_story_issues(text, story_text)
    # A deck's caption and slides read as one text, so one claim can surface twice: one issue.
    issues = list(dict.fromkeys(weekday + deadline + timeline + provenance + counts + stories))
    return {"passes": not issues, "issues": issues, "weekday": weekday, "deadline": deadline,
            "timeline": timeline, "provenance": provenance, "counts": counts,
            "stories": stories}


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
_SOURCE_NOUN = (r"(?:survey|study|report|research|poll|index|census|analysis|data|benchmark|findings|"
                r"results?)")
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


# --- Number provenance on images and hooks (showcase round 7) -------------------------------------

# How the post vouches for a figure: a supplied fact (story bank / curated source text), a sentence
# that names its source, or — when the caller had no fact list — a first-person sentence.
PROVENANCE_FACT = "fact"
PROVENANCE_SOURCE = "source"
PROVENANCE_FIRST_HAND = "first_hand"

# The longest source name a cover's source line carries; longer reads as a sentence, not a credit.
SOURCE_NAME_MAX = 60
_NAME_WORDS = r"[A-Z][\w&.'’-]*(?:\s+(?:of\s+|and\s+|&\s+)?[A-Z0-9][\w&.'’-]*){0,6}"
_SOURCE_NAME_RES = (
    re.compile(r"\b(?i:according\s+to)\s+(?:the\s+|a\s+|an\s+)?(?P<name>" + _NAME_WORDS + r")"),
    re.compile(r"\b(?i:per|via)\s+(?:the\s+)?(?P<name>" + _NAME_WORDS + r")"),
    re.compile(r"\b" + _SOURCE_NOUN + r"\s+(?:by|from|of)\s+(?:the\s+)?(?P<name>" + _NAME_WORDS
               + r")"),
    re.compile(r"\(\s*(?P<name>[A-Z][\w&.'’\- ]{1,40}?),?\s*(?:19|20)\d{2}\s*\)"),
    re.compile(r"\b" + _NOT_A_NAME + r"(?P<name>[A-Z][\w&.'’-]+(?:\s+[A-Z][\w&.'’-]+){0,3}?)"
               r"(?:'s|’s)?\s+(?:(?:19|20)\d{2}\s+)?" + _SOURCE_NOUN + r"\b"),
)


# A named source that opens its own sentence with a research verb: "Originality.ai looked at 3,000
# posts", "Gartner found…". The figure is often in the NEXT sentence, which is why a figure's
# provenance window is its own sentence and the one before it.
_RESEARCH_VERB_SOURCE_RE = re.compile(
    r"^\W*" + _NOT_A_NAME + r"(?!(?:I|We|You|They|He|She|It|Teams?|People|Companies|Most|"
    r"Everyone|Nobody)\b)(?P<name>[A-Z][\w&.'’-]+(?:\s+[A-Z][\w&.'’-]+){0,3})\s+"
    r"(?:found|finds|reported|reports|surveyed|surveys|analy[sz]ed|studied|looked\s+at|measured|"
    r"tracked|estimated|estimates|published|polled)\b")


def _names_source(sentence: str) -> bool:
    return bool(_NAMED_SOURCE_RE.search(sentence) or _RESEARCH_VERB_SOURCE_RE.search(sentence))


def _figure_windows(value: str, body: Optional[str]) -> list:
    """``(sentence, window)`` for every body sentence stating ``value``.

    The window is that sentence plus the one before it.
    """
    sentences = _sentences(body)
    return [(s, sentences[max(0, i - 1):i + 1]) for i, s in enumerate(sentences)
            if any(c["value"] == value for c in _claims(s))]


def _claims(text: Optional[str]) -> list:
    from cqc_lem.utilities.ai.content_framework import numeric_claims

    return numeric_claims(text)


def _fact_values(facts: Optional[list]) -> set:
    # Every number a fact contains — the same generous read the fact-grounding gate's anchors
    # get, so "Shipped 41 PRs" is never mistaken for a product version and dropped.
    from cqc_lem.utilities.ai.content_framework import _anchor_numbers

    return _anchor_numbers([str(f) for f in (facts or []) if f])


# Showcase round 9: cover_18's body opens "53.7% of long-form LinkedIn posts in 2025 were probably
# AI-generated" and names its source two paragraphs on: "a striking result from Originality.ai:
# they scanned more than 3,000 posts … found that over half were likely AI-generated". The figure
# IS sourced — by a sentence that restates the same finding. A source-naming sentence that shares
# at least RESTATED_MIN_SHARED distinctive words with the figure's own sentence vouches for it.
RESTATED_MIN_SHARED = 3
_FINDING_WORD = re.compile(r"[a-z][a-z'-]{3,}|(?:19|20)\d{2}")
_FINDING_STOP = frozenset((
    "that", "this", "with", "from", "were", "they", "them", "their", "have", "been", "more", "than",
    "over", "about", "into", "what", "when", "which", "while", "also", "only", "just", "some",
    "most", "many", "much", "very", "same", "such", "those", "these", "there", "here", "will",
    "would", "could", "should", "found", "showed", "shows", "said", "says", "likely",
    "probably", "half", "nearly", "almost", "around", "least",
))


def _finding_words(sentence: str) -> set:
    return {w for w in _FINDING_WORD.findall((sentence or "").lower()) if w not in _FINDING_STOP}


def restated_by_source(figure_sentences: list, body: Optional[str]) -> bool:
    """Does a body sentence that NAMES a source restate the finding a figure states?

    Args:
        figure_sentences: The body sentences that state the figure.
        body: The post body.

    Returns:
        True when one source-naming sentence shares ``RESTATED_MIN_SHARED`` distinctive words
        (four letters or more, or a year) with one of them.
    """
    sourced = [s for s in _sentences(body) if _names_source(s)]
    for sentence in figure_sentences:
        words = _finding_words(sentence)
        if any(len(words & _finding_words(s)) >= RESTATED_MIN_SHARED for s in sourced):
            return True
    return False


def figure_provenance(value: str, body: Optional[str], facts: Optional[list] = None) -> str:
    """How the post vouches for ONE figure, or '' when it does not. Deterministic.

    A figure printed on an image, or used in a hook, is only as true as the post body makes it.
    It must appear in a body sentence AND be backed there by one of (a source named in the
    sentence just before counts too — "Originality.ai looked at 3,000 posts. 53.7% of …"):

    - a supplied fact (``facts``: the author's story-bank facts, curated source text) — first-hand
      and verified;
    - the sentence naming its source ("according to Gartner", "(Edelman, 2025)", "Stanford's
      survey");
    - with ``facts`` None (the caller had no allow-list), a first-person sentence — the body's own
      fabrication gate is what checks a first-person number against the story bank.

    Args:
        value: The figure, normalised the way ``content_framework.numeric_claims`` writes it.
        body: The post body the figure must appear in.
        facts: The allow-list, or None when the caller has none.

    Returns:
        ``PROVENANCE_FACT``, ``PROVENANCE_SOURCE``, ``PROVENANCE_FIRST_HAND``, or ''.
    """
    windows = _figure_windows(value, body)
    if not windows:
        return ""
    if facts is not None and value in _fact_values(facts):
        return PROVENANCE_FACT
    if any(_names_source(w) for _, window in windows for w in window):
        return PROVENANCE_SOURCE
    if restated_by_source([s for s, _ in windows], body):
        return PROVENANCE_SOURCE
    if facts is None and any(_FIRST_PERSON_RE.search(s) for s, _ in windows):
        return PROVENANCE_FIRST_HAND
    return ""


def unprovenanced_figures(surface_text: Optional[str], body: Optional[str],
                          facts: Optional[list] = None) -> list:
    """The figures ``surface_text`` prints that the post body does not vouch for, as written.

    Args:
        surface_text: What the image or hook prints (a headline, a stat, a slide, a title).
        body: The post body (``figure_provenance``).
        facts: The allow-list, or None.

    Returns:
        The offending figures as the surface writes them, in order, de-duplicated.
    """
    out: list = []
    seen: set = set()
    for claim in _claims(surface_text):
        if claim["value"] not in seen and (
                not figure_provenance(claim["value"], body, facts)
                or upgrades_hedge(claim["value"], claim["context"], body)):
            out.append(claim["raw"])
        seen.add(claim["value"])
    return out


# Showcase round 8: rhythm_3's body said "You could cut your AI spend by 60%" and its image said
# "AI spend cut by 60%" — a hypothetical printed as a result. A figure the body only ever states
# hedged may only be printed hedged. "may" is matched lower-case only, so the month never hedges.
_HEDGE_RE = re.compile(
    r"\b(?:could|can|might|(?-i:may)|would|potentially|possibly|up\s+to|as\s+much\s+as|aim(?:s|ing)?\s+"
    r"(?:for|to)|target(?:s|ing)?|hope(?:s|d)?\s+to|expect(?:s|ed)?\s+to|estimated?|projected|"
    r"if)\b", re.IGNORECASE)


def is_hedged(sentence: Optional[str]) -> bool:
    """Does ``sentence`` state its claim as a possibility ("could cut", "up to 60%")?"""
    return bool(_HEDGE_RE.search(sentence or ""))


def upgrades_hedge(value: str, surface_sentence: Optional[str], body: Optional[str]) -> bool:
    """Does the surface ASSERT a figure the body only ever states hedged?

    Args:
        value: The figure, normalised.
        surface_sentence: The sentence the surface prints it in.
        body: The post body.

    Returns:
        True when every body sentence stating ``value`` is hedged and the surface's is not.
    """
    windows = _figure_windows(value, body)
    return (bool(windows) and all(is_hedged(sentence) for sentence, _ in windows)
            and not is_hedged(surface_sentence))


def prints_any(text: Optional[str], figures) -> bool:
    """Does ``text`` print any of ``figures`` ("53.7 %" and "53.7%" are one figure)?"""
    wanted = {c["value"] for f in (figures or ()) for c in _claims(str(f))}
    return bool(wanted) and any(c["value"] in wanted for c in _claims(text))


def source_name_for(value: str, body: Optional[str]) -> str:
    """The source a body sentence names for ``value`` ("Gartner"), or '' when none is readable.

    A citation marker or a bare URL vouches for a figure but names nobody a cover could print, so
    it returns ''.

    Args:
        value: The figure, normalised (``numeric_claims``).
        body: The post body.

    Returns:
        The source's name as the body writes it, at most ``SOURCE_NAME_MAX`` characters.
    """
    for _sentence, window in _figure_windows(value, body):
        for rx, sentence in ((rx, w) for w in reversed(window)
                             for rx in (*_SOURCE_NAME_RES, _RESEARCH_VERB_SOURCE_RE)):
            match = rx.search(sentence)
            if match:
                name = re.sub(r"['’]s$", "", " ".join(match.group("name").split()))
                name = name.strip(" .,;:'’")
                if name and len(name) <= SOURCE_NAME_MAX:
                    return name
    return ""


def hook_line(text: Optional[str]) -> str:
    """A post's hook: the opening sentence of its first non-empty line — what stops the scroll."""
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return (_sentences(first) or [""])[0]


def hook_provenance_issues(text: Optional[str], facts: Optional[list],
                           already_flagged: Optional[list] = None) -> list:
    """The hook's figures the post does not vouch for, as plain-English issues.

    The hook is the line a reader sees before deciding to read on, so a figure there needs the
    same backing as one printed on an image: a story-bank fact, or a sentence naming its source.
    A first-person sentence alone is not enough here — ``facts`` is the allow-list.

    Args:
        text: The post.
        facts: The story-bank facts (plus any curated source text); None reads as no facts.
        already_flagged: Figures another finding already names (the fact-grounding gate's
            unverified values), so one invented number is never reported twice.

    Returns:
        One issue per figure.
    """
    skip = {c["value"] for raw in (already_flagged or []) for c in _claims(str(raw))}
    return [f"the hook states \"{raw}\" but the post never backs it with a story-bank fact or a "
            f"named source: cut it from the hook, or name where it comes from"
            for raw in unprovenanced_figures(hook_line(text), text, list(facts or []))
            if not ({c["value"] for c in _claims(raw)} & skip)]


# A result claimed with no figure: "Error rates dropped sharply", "Satisfaction rose noticeably".
_RESULT_VERBS = (r"(?:dropped|fell|rose|grew|doubled|tripled|halved|increased|decreased|improved|"
                 r"declined|surged|plummeted|soared|jumped|climbed|shrank|spiked|skyrocketed|"
                 r"tanked|dipped|slid)")
_VAGUE_INTENSIFIERS = (r"(?:sharply|significantly|dramatically|noticeably|drastically|massively|"
                       r"substantially|considerably|greatly|hugely|markedly|tremendously|"
                       r"steeply|rapidly)")
_VAGUE_RESULT_RE = re.compile(
    r"\b" + _RESULT_VERBS + r"\s+(?:\w+\s+){0,2}?" + _VAGUE_INTENSIFIERS + r"\b|\b"
    + _VAGUE_INTENSIFIERS + r"\s+" + _RESULT_VERBS + r"\b", re.IGNORECASE)


def vague_result_claim(text: Optional[str]) -> str:
    """A result claimed with no figure ("dropped sharply"), or '' when there is none or a number.

    A headline that says something dropped must say what and by how much, from the facts, or not
    claim a result at all.

    Args:
        text: A headline or hook.

    Returns:
        The vague phrase, or ''.
    """
    if not text or any(ch.isdigit() for ch in text):
        return ""
    match = _VAGUE_RESULT_RE.search(text)
    return " ".join(match.group(0).split()) if match else ""
