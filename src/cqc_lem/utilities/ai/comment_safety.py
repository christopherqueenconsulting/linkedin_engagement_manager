"""Scam and hostility filter for comments on our posts, and for the replies we draft to them.

The owner's ruling: keep comment auto-replies, but only positive, safe replies go out. Engaging a
scam comment (a like, a reply, a lead flag) vouches for it in front of our audience and lifts it in
the thread; engaging a hostile one feeds the fight in public. This module decides which is which.

Pure and deterministic on purpose — no LLM, no DB, no Selenium:

* the same text always gets the same verdict, so a skip is reproducible from the log line;
* a classifier that cannot fail cannot fail OPEN — there is no "model was down, so it went out";
* it is free, so it runs before every other step of the reply flow, including the LLM draft.

The checks are shaped by HOW scam and hostile text is written, never by topic words. A finance or
marketing audience talks about "investment", "crypto", "spam filters" and "Telegram" all day, so a
bare topic word never fires; an ask to move the conversation off-platform, a money-for-nothing
promise or an insult aimed at the reader does.

v1 is SKIP-only: a filtered comment gets no reaction, no reply, no lead flag and no log row. There
is no hold-for-review queue yet (`docs/engagement-automation.md`).
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from cqc_lem.utilities.text_safety import profane_words

LABEL_SAFE = "safe"
LABEL_SCAM = "scam"
LABEL_HOSTILE = "hostile"

# Reason names. Stable, digit- and quote-free: they are what a log line and a test key on, never
# the comment text itself.
CHECK_OFF_PLATFORM_CONTACT = "off_platform_contact"
CHECK_MONEY_FOR_NOTHING = "money_for_nothing"
CHECK_RECOVERY_PITCH = "recovery_pitch"
CHECK_LINK_PUSH = "link_push"
CHECK_PROFILE_REDIRECT = "profile_redirect"
CHECK_ACCOUNT_RECOVERY = "account_recovery"
CHECK_PRIZE_CLAIM = "prize_claim"
CHECK_PROFANITY = "profanity"
CHECK_INSULT = "insult"
CHECK_THREAT = "threat"
CHECK_DISINTEREST = "disinterest"
# Outbound only: OUR drafted reply names a contact channel or a link (`outbound_contact_or_link`).
CHECK_OUTBOUND_CONTACT_OR_LINK = "outbound_contact_or_link"

# Text past this is not read. A real comment is far shorter; the cap bounds the regex work an
# attacker-sized comment can cost the sweep.
MAX_CLASSIFY_CHARS = 5000

_MESSENGERS = r"(?:whats\s?app|telegram|signal|wechat|kik|viber)"

# A phone number: international (leading +) or the separated 3-3-4 shape. An unseparated digit run
# is an order or ticket id far more often than a number to call.
_PHONE_PATTERNS = (
    r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}\b",
    r"(?<!\w)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b",
)

# Real top-level domains a scam link uses. Never a code suffix (`js`, `ts`, `py`), so "Node.js" and
# "e.g." are not domains, and never a common English word beyond these few (`in`, `to`, `us`), so a
# missing space after a full stop ("Agreed.to be fair") does not read as one either.
_TLDS = (r"(?:com|net|org|io|co|xyz|info|biz|me|app|site|online|top|vip|live|cc|ru|cn|ly|gg|"
         r"tk|ml|ga|cf|ws|su|icu|buzz)")

# A bare domain, inbound, in ANY case. `_has_bare_domain` lets exactly two shapes through: a bare
# `linkedin.com`, and a capitalised path-less brand on .com/.net/.org ("Booking.com", "ASP.NET").
# A path, or any other TLD ("Cryptofxpro.io", "CRYPTOFX-PRO.IO"), fires whatever the case.
_BARE_DOMAIN_RE = re.compile(
    rf"(?<![\w@./-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+{_TLDS})(?![\w-])(/\S*)?",
    re.IGNORECASE)
# Bare mentions of these, with no path, are the platform naming itself, not a link push.
_SAFE_BARE_DOMAINS = frozenset({"linkedin.com"})
# The only TLDs on which a CAPITALISED, path-less token can be brand prose ("Booking.com",
# "ASP.NET"). Everywhere else — any path, any other TLD — case changes nothing.
_BRAND_PROSE_TLDS = frozenset({"com", "net", "org"})

# Outbound is stricter: ANY case, any domain-shaped token, an @handle, or an obfuscated dot.
_OUTBOUND_CONTACT_OR_LINK_RE = re.compile(
    "|".join(f"(?:{p})" for p in (
        r"(?<![\w.])@[a-z0-9_][\w.]*",
        rf"(?<![\w@./-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+{_TLDS}(?![\w-])",
        rf"\b\s*(?:\[\s*(?:\.|dot)\s*\]|\(\s*(?:\.|dot)\s*\)|\{{\s*(?:\.|dot)\s*\}})\s*{_TLDS}\b",
        rf"\s+dot\s+{_TLDS}\b",
        r"\bhttps?://|\bwww\.",
        # Any domain-shaped token WITH a path, on any TLD ("scam.finance/x", "evil.ai/x"). A false
        # positive here only costs one skipped reply of ours.
        r"\b[a-z0-9-]+\.[a-z]{2,24}/\S",
        *_PHONE_PATTERNS,
    )),
    re.IGNORECASE)


def _compile(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_SCAM_CHECKS: tuple[tuple[str, re.Pattern], ...] = (
    (CHECK_OFF_PLATFORM_CONTACT, _compile(
        # An ASK to move off-platform. "Telegram" alone is news, "reach me on Telegram" is a pitch.
        rf"\b(?:reach|contact|message|text|dm|ping|call|find|add|hit)\s+(?:me|us)\s+(?:up\s+)?"
        rf"(?:on|via|through|at|over)\s+{_MESSENGERS}\b",
        rf"\b{_MESSENGERS}\s+(?:me|us)\b",
        rf"\b{_MESSENGERS}\s+(?:number|handle|id|contact)\b",
        # "Signal:" is how an analyst opens a sentence, so the handle shape names only the
        # messengers that are never an ordinary English word.
        r"\b(?:whats\s?app|telegram|wechat|viber|kik)\s*[:\-]?\s*[+@]",
        r"\btext\s+me\b",
        r"\bdm\s+me\s+(?:on|via|at|through)\b",
        *_PHONE_PATTERNS,
    )),
    (CHECK_MONEY_FOR_NOTHING, _compile(
        # Day/week/hour only: "earn $10k a month" is how consultants describe client results.
        r"\bearn(?:ed|ing|s)?\s+(?:up\s+to\s+|over\s+|about\s+)?[$£€]\s?\d[\d,.]*\s*k?"
        r"(?:\s*(?:usd|dollars))?\s*(?:per|a|an|every|each|/)\s*(?:day|week|hour|daily|weekly)\b",
        r"\bearn(?:ed|ing|s)?\b.{0,40}\bfrom\s+(?:the\s+comfort\s+of\s+)?(?:your|my)\s+home\b",
        r"\bguaranteed\s+(?:\d+\s*%\s+)?(?:returns?|profits?|income|roi|payouts?|earnings)\b",
        r"\bdouble\s+your\s+(?:money|investment|income|capital|bitcoin|btc|crypto|funds|savings)\b",
    )),
    (CHECK_RECOVERY_PITCH, _compile(
        r"\btrading\s+signals?\b",
        # Never "investment manager" — that is a job title half a finance audience holds.
        r"\b(?:forex|crypto|bitcoin|binary\s+options?)\s+(?:trading\s+)?(?:account\s+manager|mentor)\b",
        r"\baccount\s+manager\b.{0,40}\b(?:profits?|returns?|invest(?:ed|ing|ment)?|trades?|"
        r"crypto|forex|bitcoin)\b",
        r"\b(?:funds?|crypto|asset)\s+recovery\b",
        r"\b(?:recover(?:ed|s)?|retrieve[ds]?|got\s+back)\b.{0,40}\b(?:lost|stolen|scammed)\s+"
        r"(?:funds|money|crypto|bitcoin|btc|assets|investments?)\b",
    )),
    (CHECK_LINK_PUSH, _compile(
        r"\bhttps?://",
        r"\bwww\.[a-z0-9-]+\.",
        r"\b(?:bit\.ly|tinyurl\.com|t\.me|wa\.me|goo\.gl|ow\.ly|is\.gd|cutt\.ly|rb\.gy|"
        r"shorturl\.at|tiny\.cc|t\.co|lnkd\.in)/",
        # The imperative only — "I clicked the link in your post" is a reader, not a pitch.
        r"(?:^|[.!?]\s*|\bjust\s+|\bplease\s+)click\s+(?:on\s+)?(?:the|this|my|that)?\s*link\b",
    )),
    (CHECK_PROFILE_REDIRECT, _compile(
        r"\b(?:check|visit|see|view)\s+(?:out\s+)?my\s+(?:profile|bio|page|about)\b",
        r"\b(?:link|details|info|offer)\s+(?:is\s+)?in\s+my\s+(?:bio|profile)\b",
    )),
    (CHECK_ACCOUNT_RECOVERY, _compile(
        r"\b(?:recover|restore|unlock|retrieve|regain\s+access\s+to|get\s+back)\s+"
        r"(?:your|my|their|any)\s+(?:hacked\s+|lost\s+|locked\s+|disabled\s+|suspended\s+)?"
        r"(?:account|page|profile|password)s?\b",
        r"\bhacked\s+(?:account|page|profile)s?\b",
    )),
    (CHECK_PRIZE_CLAIM, _compile(
        r"\byou(?:'ve|\s+have)?\s+(?:been\s+)?(?:selected|chosen)\s+(?:as|for|to)\b",
        r"\byou(?:'ve|\s+have)?\s+won\s+(?:a|an|the|our|[$£€])",
        r"\bclaim\s+your\s+(?:prize|reward|gift|bonus|winnings)\b",
        r"\bgiveaway\s+winner\b",
    )),
)

_HOSTILE_CHECKS: tuple[tuple[str, re.Pattern], ...] = (
    (CHECK_INSULT, _compile(
        r"\byou(?:'re|\s+are|r)\s+(?:such\s+)?(?:an?\s+|the\s+|a\s+total\s+|a\s+complete\s+)?"
        r"(?:idiot|moron|stupid|clown|fraud|scammer|joke|loser|fool|liar|grifter|dumb|pathetic|"
        r"imbecile|charlatan)\b",
        r"\b(?:what|such)\s+an?\s+(?:idiot|moron|clown|fraud|loser|joke)\b",
        r"\bshut\s+up\b",
        r"\bgo\s+to\s+hell\b",
        r"\b(?:nobody|no\s+one)\s+cares\b",
        r"\bthis\s+is\s+(?:pure\s+|total\s+|complete\s+|absolute\s+|just\s+)?"
        r"(?:garbage|trash|bs|b\.s|nonsense|rubbish|drivel)(?!\w)",
    )),
    (CHECK_THREAT, _compile(
        r"\bi(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to|'m\s+gonna|\s+am\s+gonna|\s+gonna)\s+"
        r"(?:find|hurt|kill|destroy|ruin|report|expose|sue|end)\s+you\b",
        r"\bwatch\s+your\s+back\b",
        r"\byou(?:'ll|\s+will)\s+regret\b",
        r"\bkill\s+yourself\b",
        r"\bkys\b",
    )),
    (CHECK_DISINTEREST, _compile(
        r"\bstop\s+(?:spamming|tagging\s+me|messaging\s+me|posting\s+this)\b",
        r"\bleave\s+me\s+alone\b",
        r"\bunfollow(?:ed|ing)?\s+(?:you|this|now)\b",
        r"\bi(?:'m|\s+am|'ll|\s+will|\s+just|\s+have)\s+(?:just\s+)?unfollow(?:ed|ing)?\b",
        # "Spam" as an ACCUSATION. A marketing audience says "spam filter" and "spam folder" daily.
        r"\b(?:this\s+is|that's|thats|it's|such|pure|total|absolute)\s+(?:just\s+|pure\s+|total\s+)?"
        r"spam\b(?!\s*(?:filters?|folders?|traps?|scores?|rates?|complaints?|box))",
        r"\bspammy\b",
        r"\bspammers?\b",
        r"^\W*spam\W*$",
    )),
)

# Curly quotes are what LinkedIn renders, straight ones are what the patterns spell.
_QUOTE_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


@dataclass(frozen=True)
class CommentVerdict:
    """The filter's answer for one piece of text.

    Attributes:
        label: `safe`, `scam` or `hostile`. Scam wins when both families fire, because a scam
            comment is the one an engagement actively amplifies.
        reasons: The check names that fired, in a stable order. Never the text itself.
    """

    label: str
    reasons: tuple[str, ...] = ()

    @property
    def is_safe(self) -> bool:
        """True when nothing fired."""
        return self.label == LABEL_SAFE


def classify_comment(text: Optional[str]) -> CommentVerdict:
    """Classify a comment (or a reply we drafted) as safe, scam or hostile.

    Empty or whitespace-only text is `safe` here: callers already skip an empty comment, and this
    function's job is only to say whether NON-empty text may be engaged with.

    Args:
        text: The comment body; None reads as empty.

    Returns:
        A `CommentVerdict` naming every check that fired.
    """
    body = normalize_for_matching(text)
    if not body:
        return CommentVerdict(LABEL_SAFE)
    scam = tuple(name for name, pattern in _SCAM_CHECKS
                 if pattern.search(body) or (name == CHECK_LINK_PUSH and _has_bare_domain(body)))
    hostile = []
    if profane_words(body):
        hostile.append(CHECK_PROFANITY)
    hostile.extend(name for name, pattern in _HOSTILE_CHECKS if pattern.search(body))
    if scam:
        return CommentVerdict(LABEL_SCAM, scam + tuple(hostile))
    if hostile:
        return CommentVerdict(LABEL_HOSTILE, tuple(hostile))
    return CommentVerdict(LABEL_SAFE)


def normalize_for_matching(text: Optional[str]) -> str:
    """Fold text into the plain form the patterns are written against.

    Order matters: NFKC first turns math-bold and fullwidth letters into ASCII, then every format
    character (Unicode category Cf: zero-width spaces and joiners, direction marks) is dropped so a
    "WhatsApp" or "t.me" split by an invisible character reads as written, then curly quotes fold
    to straight and whitespace collapses. Confusable homoglyphs (a Cyrillic "а"
    for a Latin "a") are NOT folded — a documented v1 gap.

    Args:
        text: Any text; None reads as empty. Only the first `MAX_CLASSIFY_CHARS` are read.

    Returns:
        The folded, whitespace-collapsed, stripped text.
    """
    # Cut before AND after NFKC: one input character can expand to several, so only the second cut
    # makes the cap literal.
    body = unicodedata.normalize("NFKC", str(text or "")[:MAX_CLASSIFY_CHARS])[:MAX_CLASSIFY_CHARS]
    body = "".join(ch for ch in body if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", " ", body.translate(_QUOTE_FOLD)).strip()


def _has_bare_domain(body: str) -> bool:
    for match in _BARE_DOMAIN_RE.finditer(body):
        domain, path = match.group(1), match.group(2)
        if path:
            return True
        folded = domain.lower()
        if folded in _SAFE_BARE_DOMAINS:
            continue
        if domain != folded and folded.rsplit(".", 1)[-1] in _BRAND_PROSE_TLDS:
            continue  # "Booking.com" named in prose — capitalised, no path, .com/.net/.org
        return True
    return False


def outbound_contact_or_link(text: Optional[str]) -> bool:
    """Return True when a reply WE drafted names a contact channel or a link.

    Stricter than the inbound link check on purpose: our replies on our own post have no reason to
    carry any domain-shaped token (any case, including "linkedin.com"), an @handle, an obfuscated
    dot ("x dot com", "x[.]io") or a phone number, and a model steered by a scam comment is exactly
    what would put one there.

    Args:
        text: The drafted reply.

    Returns:
        Whether any of those shapes is present.
    """
    body = normalize_for_matching(text)
    return bool(body) and bool(_OUTBOUND_CONTACT_OR_LINK_RE.search(body))


def is_safe_to_engage(text: Optional[str]) -> bool:
    """Return True when `text` may be reacted to, replied to or flagged as a lead.

    Args:
        text: The comment body; None reads as empty (and therefore safe).

    Returns:
        Whether `classify_comment` found nothing.
    """
    return classify_comment(text).is_safe
