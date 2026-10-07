"""Curated outside sources: the ONE place their guardrails, credit and ceiling are decided.

Phase 1 of the curated-sources feature (``docs/curated-sources.md``, research in
``docs/curated-sources-research.md``). LEM drafts a post that comments on SOMEONE ELSE'S content —
a LinkedIn post reshared with commentary, a figure re-charted from a source, or a link to an
article — and every one of those drafts waits for the owner's approval before it can publish.

Everything here is a PURE function of its arguments (no DB, no network, no LLM), so the rules that
decide what may be published are unit-testable on their own:

- ``screen_candidate`` is the block gate. It runs when a source is COLLECTED and again when it is
  DRAFTED, because the rules can change between the two and a draft is the last point before the
  owner sees it. The political filter is ABSOLUTE (owner decision 2026-10-07): partisan or electoral
  content, named politicians and regulators' statements are blocked on every platform, with no
  approval override. Paywalled content, a non-commercial (NC) licence, an excluded platform (Truth
  Social, Reddit, Threads) and the analyst houses whose terms forbid quoting (Gartner, Forrester)
  are blocked too.
- ``treatment_allowed`` decides which of the three treatments a source may take. A reshare needs a
  share/ugcPost URN; an ``urn:li:activity`` is NOT a valid reshare parent. A re-chart needs a
  licence that permits a derivative, so ``-nd`` and ``unknown`` sources never re-chart.
- ``credit_line`` is the attribution every curated post carries in its body.
- ``ceiling_allows`` keeps the account original-first: at most ONE curated post in any three
  consecutive posts, and only in a ``value`` or ``authority`` slot, never ``promo``.
- ``escape_little_text`` escapes commentary for LinkedIn's "little" text format, which the
  versioned ``/rest/posts`` endpoint parses.

Screenshots are not a treatment and never will be (owner decision): there is no code path that
captures or uploads an image of another person's post.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

from cqc_lem.utilities.logger import log_debug, log_warning

# --- Vocabularies --------------------------------------------------------------------------------

PLATFORM_LINKEDIN = "linkedin"
PLATFORM_RSS = "rss"
PLATFORM_GOV_DATA = "gov_data"
PLATFORM_MANUAL = "manual"
PLATFORMS = (PLATFORM_LINKEDIN, PLATFORM_RSS, PLATFORM_GOV_DATA, PLATFORM_MANUAL)

# `curated_sources.status` — a MySQL ENUM, so a new member needs a migration first.
STATUS_NEW = "new"
STATUS_DRAFTED = "drafted"
STATUS_APPROVED = "approved"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"
STATUSES = (STATUS_NEW, STATUS_DRAFTED, STATUS_APPROVED, STATUS_PUBLISHED, STATUS_REJECTED,
            STATUS_BLOCKED)

# `posts.source_treatment` — also a MySQL ENUM. No screenshot member, by design.
TREATMENT_RESHARE = "reshare"
TREATMENT_RECHART = "rechart"
TREATMENT_LINK = "link"
TREATMENTS = (TREATMENT_RESHARE, TREATMENT_RECHART, TREATMENT_LINK)

# Licence / terms classes, recorded per source (research §4(d)2).
LICENCE_PUBLIC_DOMAIN = "public_domain"      # US federal works: BLS, Census, SBA
LICENCE_CC_BY = "cc_by"
LICENCE_CC_BY_SA = "cc_by_sa"
LICENCE_CC_BY_ND = "cc_by_nd"                # share unmodified only: never re-charted
LICENCE_CC_NC = "cc_nc"                      # any NonCommercial variant: blocked outright
LICENCE_EDITORIAL = "editorial"              # press rooms / company blogs: link + short quote
LICENCE_FACTS_ONLY = "facts_only"            # articles we may re-chart FACTS from, with credit
LICENCE_LINKEDIN_NATIVE = "linkedin_native"  # a LinkedIn post: native reshare credits the author
LICENCE_UNKNOWN = "unknown"
LICENCES = (LICENCE_PUBLIC_DOMAIN, LICENCE_CC_BY, LICENCE_CC_BY_SA, LICENCE_CC_BY_ND, LICENCE_CC_NC,
            LICENCE_EDITORIAL, LICENCE_FACTS_ONLY, LICENCE_LINKEDIN_NATIVE, LICENCE_UNKNOWN)
# Licences whose terms permit redrawing the source's figures in our own design.
RECHART_LICENCES = frozenset({LICENCE_PUBLIC_DOMAIN, LICENCE_CC_BY, LICENCE_CC_BY_SA,
                              LICENCE_FACTS_ONLY, LICENCE_EDITORIAL})

# Content-mix slots a curated post may take (content_alignment's 70/20/10 classes).
CURATED_MIX_SLOTS = frozenset({"value", "authority"})
# At most ONE curated post in any run of this many consecutive posts.
CEILING_WINDOW = 3

# --- Block reasons -------------------------------------------------------------------------------
BLOCK_POLITICAL = "political"
BLOCK_PAYWALL = "paywall"
BLOCK_LICENCE_NC = "licence_nc"
BLOCK_EXCLUDED_PLATFORM = "excluded_platform"
BLOCK_ANALYST_TERMS = "analyst_terms"
BLOCK_NO_PROVENANCE = "no_provenance"

# Platforms excluded per the research tiers (Truth Social, Reddit, Threads) — any URL on them is
# blocked whatever its content.
_EXCLUDED_DOMAINS = ("truthsocial.com", "reddit.com", "redd.it", "threads.net", "threads.com")
# Analyst houses whose terms forbid quoting without written approval; the owner has no client
# access (2026-10-07), so neither their pages nor content citing their figures is used.
_ANALYST_DOMAINS = ("gartner.com", "forrester.com")
_ANALYST_CITATION = re.compile(r"\b(?:Gartner|Forrester)\b")

# The political filter. Case-INSENSITIVE terms are whole words/phrases that are political in any
# casing; case-SENSITIVE ones are acronyms whose lowercase form is an ordinary word ("sec", "gop").
_POLITICAL_TERMS_CI = (
    # electoral / partisan
    "election", "elections", "electoral", "midterm", "midterms", "ballot", "ballots", "voter",
    "voters", "polling station", "campaign trail", "primary election", "caucus", "partisan",
    "bipartisan", "democrat", "democrats", "democratic party", "republican", "republicans",
    "republican party", "left-wing", "right-wing", "liberals", "conservatives", "super pac",
    # offices and institutions whose statements are political speech
    "white house", "congress", "congressional", "congressman", "congresswoman", "senate",
    "senator", "senators", "house of representatives", "capitol hill", "lawmaker", "lawmakers",
    "legislator", "legislators", "legislature", "governor", "mayor", "parliament",
    "prime minister", "minister of", "secretary of state", "attorney general", "supreme court",
    "executive order", "impeachment", "filibuster",
    # regulators' statements (owner decision: no regulator statements, ever)
    "regulator", "regulators", "regulatory agency", "federal trade commission",
    "securities and exchange commission", "federal communications commission",
    "commissioner", "agency chair",
    # named politicians (non-exhaustive; the office terms above catch most of the rest)
    "trump", "biden", "kamala harris", "obama", "vance", "pelosi", "schumer", "mcconnell",
    "desantis", "newsom", "ocasio-cortez", "bernie sanders", "elizabeth warren", "ted cruz",
    "rishi sunak", "keir starmer", "macron", "putin", "zelensky", "netanyahu", "xi jinping",
    "modi", "trudeau", "milei",
    # culture-war topics
    "culture war", "woke", "anti-woke", "abortion", "gun control", "second amendment",
    "immigration policy", "border wall", "deportation", "maga",
)
_POLITICAL_TERMS_CS = ("GOP", "MAGA", "DNC", "RNC", "FTC", "SEC", "FCC", "POTUS", "SCOTUS", "PAC")
_POLITICAL_CI_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(t) for t in _POLITICAL_TERMS_CI) + r")(?!\w)",
    re.IGNORECASE)
_POLITICAL_CS_RE = re.compile(r"\b(?:" + "|".join(_POLITICAL_TERMS_CS) + r")\b")

# Paywall / login-wall markers in a fetched excerpt (Substack, Medium, news sites).
_PAYWALL_MARKERS = (
    "for paid subscribers", "paid subscribers only", "subscribe to read", "subscribe to continue",
    "subscriber-only", "subscribers only", "member-only story", "members-only", "members only",
    "sign in to continue", "log in to continue reading", "login to continue",
    "this post is for paying subscribers", "unlock this article", "become a paid subscriber",
    "already a subscriber",
)

# LinkedIn URNs. Only a share or ugcPost may be a reshare PARENT (Posts API, Reshare a post);
# the activity URN the feed exposes is refused by LinkedIn with INVALID_URN_TYPE.
RESHAREABLE_URN_RE = re.compile(r"^urn:li:(?:share|ugcPost):\d+$")
ACTIVITY_URN_RE = re.compile(r"^urn:li:activity:\d+$")
_ANY_POST_URN_RE = re.compile(r"urn:li:(share|ugcPost|activity):(\d+)")

# little-text reserved characters (Posts API "little Text Format"): every one must be escaped with
# a backslash, even when it is not part of an element.
LITTLE_RESERVED = "\\|{}@[]()<>#*_~"
_HASHTAG_RE = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]{0,99})")


@dataclass(frozen=True)
class Verdict:
    """The block gate's answer: ``ok`` or a ``reason`` (``kind:detail``) to store as block_reason."""

    ok: bool
    reason: str = ""


# --- Guardrails ----------------------------------------------------------------------------------

def _domain(url: Optional[str]) -> str:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _on_domain(url: Optional[str], domains: Iterable[str]) -> Optional[str]:
    host = _domain(url)
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


def political_reason(*texts: Optional[str]) -> Optional[str]:
    """The first political term found in any of ``texts``, or None. ABSOLUTE: never overridable.

    Args:
        *texts: Title, excerpt, author, URL — everything a reader would see or the post links to.

    Returns:
        ``political:<term>`` or None.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return None
    match = _POLITICAL_CS_RE.search(blob) or _POLITICAL_CI_RE.search(blob)
    return f"{BLOCK_POLITICAL}:{match.group(0).lower()}" if match else None


def paywall_reason(excerpt: Optional[str], paywalled: bool = False) -> Optional[str]:
    """``paywall`` when the collector flagged one or the excerpt carries a paywall marker."""
    if paywalled:
        return BLOCK_PAYWALL
    lowered = (excerpt or "").lower()
    for marker in _PAYWALL_MARKERS:
        if marker in lowered:
            return f"{BLOCK_PAYWALL}:{marker}"
    return None


def normalize_licence(value: Optional[str]) -> str:
    """Any licence spelling ("CC BY-NC 4.0", "cc-by-sa") reduced to one of ``LICENCES``."""
    raw = re.sub(r"[\s\-./]+", "_", (value or "").strip().lower())
    if not raw:
        return LICENCE_UNKNOWN
    if raw in LICENCES:
        return raw
    if raw.startswith("cc") and "nc" in raw.split("_"):
        return LICENCE_CC_NC
    if raw.startswith("cc") and "nd" in raw.split("_"):
        return LICENCE_CC_BY_ND
    if raw.startswith("cc_by_sa"):
        return LICENCE_CC_BY_SA
    if raw.startswith("cc_by") or raw == "cc":
        return LICENCE_CC_BY
    if raw in ("public_domain", "publicdomain", "pd", "cc0"):
        return LICENCE_PUBLIC_DOMAIN
    return LICENCE_UNKNOWN


def licence_reason(licence: Optional[str]) -> Optional[str]:
    """``licence_nc`` for any NonCommercial licence — incompatible with a business account."""
    return BLOCK_LICENCE_NC if normalize_licence(licence) == LICENCE_CC_NC else None


def screen_candidate(item: dict) -> Verdict:
    """The block gate, run at collect time AND again at draft time. Fails CLOSED.

    Args:
        item: A curated-source row or candidate: ``url``, ``title``, ``excerpt``, ``author``,
            ``publisher``, ``licence``, ``paywalled``.

    Returns:
        ``Verdict(True)`` or ``Verdict(False, reason)``. Order: excluded platform, analyst terms,
        political, paywall, licence — the first that fires is the stored reason.
    """
    item = item or {}
    url = item.get("url") or ""
    excluded = _on_domain(url, _EXCLUDED_DOMAINS)
    if excluded:
        return Verdict(False, f"{BLOCK_EXCLUDED_PLATFORM}:{excluded}")
    analyst = _on_domain(url, _ANALYST_DOMAINS)
    if analyst:
        return Verdict(False, f"{BLOCK_ANALYST_TERMS}:{analyst}")
    texts = (item.get("title"), item.get("excerpt"), item.get("author"), item.get("publisher"),
             url)
    cited = next((m.group(0) for t in texts if t for m in [_ANALYST_CITATION.search(t)] if m), None)
    if cited:
        return Verdict(False, f"{BLOCK_ANALYST_TERMS}:{cited.lower()}")
    political = political_reason(*texts)
    if political:
        return Verdict(False, political)
    paywall = paywall_reason(item.get("excerpt"), bool(item.get("paywalled")))
    if paywall:
        return Verdict(False, paywall)
    licence = licence_reason(item.get("licence"))
    if licence:
        return Verdict(False, licence)
    return Verdict(True)


def provenance_reason(item: dict) -> Optional[str]:
    """``no_provenance`` unless the item holds a URL, a named author or publisher, and a snapshot.

    Research §4(d)1: nothing is drafted without the URL, who said it, and what they said.
    """
    item = item or {}
    if not (item.get("url") or "").startswith(("http://", "https://")):
        return f"{BLOCK_NO_PROVENANCE}:url"
    if not ((item.get("author") or "").strip() or (item.get("publisher") or "").strip()):
        return f"{BLOCK_NO_PROVENANCE}:author"
    if not ((item.get("excerpt") or "").strip() or (item.get("title") or "").strip()):
        return f"{BLOCK_NO_PROVENANCE}:snapshot"
    return None


# --- LinkedIn URNs and treatments ----------------------------------------------------------------

def is_reshareable_urn(urn: Optional[str]) -> bool:
    """True for a ``urn:li:share:N`` / ``urn:li:ugcPost:N`` — the only valid reshare parents."""
    return bool(urn and RESHAREABLE_URN_RE.match(urn.strip()))


def split_post_urns(*values: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(reshareable_urn, activity_urn)`` read out of any attribute strings or permalinks.

    The feed exposes the activity URN; a share/ugcPost URN appears only on some SDUI variants. Both
    are kept when present. A value that names neither yields ``(None, None)``.
    """
    share = activity = None
    for value in values:
        for kind, num in _ANY_POST_URN_RE.findall(value or ""):
            urn = f"urn:li:{kind}:{num}"
            if kind == "activity":
                activity = activity or urn
            else:
                share = share or urn
    return share, activity


def treatment_allowed(item: dict, treatment: str) -> Verdict:
    """May ``item`` take ``treatment``? Screenshots are never a treatment.

    Args:
        item: The curated-source row.
        treatment: One of ``TREATMENTS``.

    Returns:
        ``Verdict(True)`` or the refusal reason.
    """
    item = item or {}
    if treatment not in TREATMENTS:
        return Verdict(False, f"unknown_treatment:{treatment}")
    if treatment == TREATMENT_RESHARE:
        if item.get("platform") != PLATFORM_LINKEDIN or item.get("link_only"):
            return Verdict(False, "reshare_needs_linkedin_post")
        if not is_reshareable_urn(item.get("canonical_id")):
            return Verdict(False, "reshare_needs_share_urn")
        return Verdict(True)
    if treatment == TREATMENT_RECHART:
        licence = normalize_licence(item.get("licence"))
        if licence not in RECHART_LICENCES:
            return Verdict(False, f"rechart_licence:{licence}")
        return Verdict(True)
    if not (item.get("url") or "").startswith(("http://", "https://")):
        return Verdict(False, "link_needs_url")
    return Verdict(True)


def pick_treatment(item: dict, has_chartable_figures: bool) -> Optional[str]:
    """The safest treatment ``item`` qualifies for, in the research's rank order.

    A LinkedIn post with a share URN reshares (LinkedIn credits the author itself). A source with
    figures that validate against it, under a licence that permits it, is re-charted. Anything else
    with a URL is a link post. None when nothing qualifies.
    """
    if treatment_allowed(item, TREATMENT_RESHARE).ok:
        return TREATMENT_RESHARE
    if has_chartable_figures and treatment_allowed(item, TREATMENT_RECHART).ok:
        return TREATMENT_RECHART
    if item.get("platform") == PLATFORM_LINKEDIN:
        # A LinkedIn post we cannot reshare is linked, never screenshotted.
        return TREATMENT_LINK if treatment_allowed(item, TREATMENT_LINK).ok else None
    if treatment_allowed(item, TREATMENT_LINK).ok:
        return TREATMENT_LINK
    return None


# --- Credit --------------------------------------------------------------------------------------

def credit_line(item: dict, treatment: str) -> str:
    """The ``Source:`` line every curated post carries in its body (research §4(c)).

    A reshare still carries it: LinkedIn embeds the original, but the owner decision is that the
    commentary must name and credit the source in its own text too.

    Args:
        item: The curated-source row.
        treatment: The treatment the post uses.

    Returns:
        ``Source: {Author}, "{Title}", {Publisher}.`` with the licence noted for a re-charted
        Creative Commons work. No URL: a link post's article card carries the link, and a URL in
        the body would be moved to the first comment by the link-in-first-comment split.
    """
    item = item or {}
    author = (item.get("author") or "").strip()
    publisher = (item.get("publisher") or "").strip()
    title = (item.get("title") or "").strip()
    parts = []
    who = author or publisher
    if who:
        parts.append(who)
    if title:
        parts.append(f'"{title[:140]}"')
    if publisher and publisher != who:
        parts.append(publisher)
    line = "Source: " + ", ".join(parts) if parts else "Source: " + (item.get("url") or "")
    licence = normalize_licence(item.get("licence"))
    if licence in (LICENCE_CC_BY, LICENCE_CC_BY_SA) and treatment == TREATMENT_RECHART:
        line += f" ({licence.replace('_', '-').upper()}, changes: re-charted)"
    return line.rstrip(".") + "."


def chart_source_line(item: dict) -> str:
    """The small-type line drawn ON a re-chart: ``Source: {Publisher}, {Title}``."""
    item = item or {}
    publisher = (item.get("publisher") or item.get("author") or "").strip()
    title = (item.get("title") or "").strip()
    text = ", ".join(p for p in (publisher, title) if p)
    return f"Source: {text}"[:90] if text else "Source: " + _domain(item.get("url"))


def has_credit(content: Optional[str], item: dict) -> bool:
    """True when ``content`` carries a ``Source:`` line naming the author or publisher."""
    content = content or ""
    if "Source:" not in content:
        return False
    names = [n for n in ((item or {}).get("author"), (item or {}).get("publisher")) if n]
    return any(n.strip() and n.strip() in content for n in names)


def with_credit(content: str, item: dict, treatment: str) -> str:
    """``content`` with the credit line appended (once) before any trailing hashtags."""
    content = (content or "").rstrip()
    if has_credit(content, item):
        return content
    line = credit_line(item, treatment)
    lines = content.split("\n")
    tail = []
    while lines and lines[-1].strip() and all(w.startswith("#") for w in lines[-1].split()):
        tail.insert(0, lines.pop())
    body = "\n".join(lines).rstrip()
    out = f"{body}\n\n{line}" if body else line
    return out + ("\n\n" + "\n".join(tail) if tail else "")


# --- Ceiling -------------------------------------------------------------------------------------

def ceiling_allows(before: list[bool], after: Optional[list[bool]] = None,
                   window: int = CEILING_WINDOW) -> bool:
    """At most one curated post in any ``window`` consecutive posts, around the slot being filled.

    Args:
        before: Whether each of the user's posts BEFORE the slot (oldest first) is curated.
        after: The same for posts AFTER the slot (nearest first) — a plan fills future slots, so a
            curated post already scheduled after this one counts too.
        window: The ceiling's window; one curated post per this many.

    Returns:
        True when no curated post sits within ``window - 1`` posts either side of the slot.
    """
    reach = max(window - 1, 0)
    near_before = list(before or [])[-reach:] if reach else []
    near_after = list(after or [])[:reach] if reach else []
    return not any(near_before) and not any(near_after)


def slot_allows(content_mix: Optional[str]) -> bool:
    """Curated posts take a ``value`` or ``authority`` slot — never ``promo``, never unclassified."""
    return (content_mix or "").strip().lower() in CURATED_MIX_SLOTS


# --- little text ---------------------------------------------------------------------------------

def escape_little_text(text: Optional[str], keep_hashtags: bool = True) -> str:
    r"""Escape commentary for the Posts API "little" text format.

    Every reserved character (``\ | { } @ [ ] ( ) < > # * _ ~``) is backslash-escaped so LinkedIn
    renders it as plain text instead of parsing it as an element (an unescaped ``(`` or ``@`` can
    truncate or reject the post). With ``keep_hashtags`` a ``#word`` is emitted as the documented
    ``{hashtag|\#|word}`` template so it still renders as a hashtag.

    Args:
        text: The commentary as written.
        keep_hashtags: Render ``#word`` as a hashtag template rather than literal text.

    Returns:
        The escaped commentary.
    """
    # #2241 showcase C: the commentary carried markdown "**bold**" list items, which this escaper
    # turned into literal asterisks. LinkedIn renders no markdown, so it is stripped to plain text
    # FIRST — the same `sanitize_for_linkedin` every generated text post already goes through.
    from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin

    text = sanitize_for_linkedin(text or "") or ""
    out: list[str] = []
    pos = 0
    for match in (_HASHTAG_RE.finditer(text) if keep_hashtags else ()):
        out.append(_escape_plain(text[pos:match.start()]))
        out.append("{hashtag|\\#|" + _escape_plain(match.group(1)) + "}")
        pos = match.end()
    out.append(_escape_plain(text[pos:]))
    return "".join(out)


def _escape_plain(text: str) -> str:
    return "".join("\\" + ch if ch in LITTLE_RESERVED else ch for ch in text)


# --- Allowlist config ----------------------------------------------------------------------------

_DEFAULT_FEEDS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources",
                                   "curated_feeds.json")


def feeds_config_path() -> str:
    """The allowlist file: ``CURATED_FEEDS_PATH`` when set, else the shipped proposal."""
    return os.getenv("CURATED_FEEDS_PATH") or _DEFAULT_FEEDS_PATH


def load_feed_allowlist(path: Optional[str] = None) -> dict:
    """The owner-editable allowlist: ``{"feeds": [...], "gov_series": [...]}``.

    Each feed is ``{name, url, publisher, licence}``; each gov series is ``{series_id, label,
    unit, publisher, title}``. An unreadable file is an EMPTY allowlist (collect nothing), never a
    crash in a beat task — and it is logged, because an allowlist that silently stops loading looks
    exactly like a quiet news week.
    """
    path = path or feeds_config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log_debug("No curated-feeds allowlist file", path=path)
        return {"feeds": [], "gov_series": []}
    except (OSError, ValueError) as e:
        log_warning("Curated-feeds allowlist unreadable — collecting nothing", exc=e, path=path)
        return {"feeds": [], "gov_series": []}
    feeds = [f for f in (data.get("feeds") or []) if isinstance(f, dict) and f.get("url")]
    series = [s for s in (data.get("gov_series") or []) if isinstance(s, dict)
              and s.get("series_id")]
    return {"feeds": feeds, "gov_series": series}
