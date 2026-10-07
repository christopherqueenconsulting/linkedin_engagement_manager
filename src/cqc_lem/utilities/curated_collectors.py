"""Collectors for curated outside sources: LinkedIn feed/roster, the RSS allowlist, gov data, manual.

Every collector reduces what it read to ONE candidate dict (``platform, url, canonical_id,
activity_urn, link_only, author, publisher, title, excerpt, og_image_url, licence, paywalled,
published_at``) and hands it to ``record_candidate``, which runs the block gate
(``curated_sources.screen_candidate``) and stores the row — ``new`` when it passed, ``blocked`` with
the reason when it did not. A blocked row is kept: it is the evidence the filter ran, and it stops
the next run re-screening the same URL.

Collectors NEVER raise into their caller. The LinkedIn hook runs inside the feed-commenting loop,
and a curated-sources fault must not cost that lane a comment.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from cqc_lem.utilities.curated_sources import (
    LICENCE_LINKEDIN_NATIVE,
    LICENCE_PUBLIC_DOMAIN,
    LICENCE_UNKNOWN,
    PLATFORM_GOV_DATA,
    PLATFORM_LINKEDIN,
    PLATFORM_MANUAL,
    PLATFORM_RSS,
    STATUS_BLOCKED,
    STATUS_NEW,
    is_reshareable_urn,
    normalize_licence,
    screen_candidate,
    split_post_urns,
)
from cqc_lem.utilities.db import insert_curated_source
from cqc_lem.utilities.logger import log_debug, log_warning

FETCH_TIMEOUT = (5, 20)
FEED_MAX_BYTES = 3 * 1024 * 1024
ITEMS_PER_FEED = 5
EXCERPT_MAX = 2000
_USER_AGENT = "LEM curated-sources collector (+https://christopherqueenconsulting.com)"
BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/{series}"
BLS_PUBLISHER = "U.S. Bureau of Labor Statistics"

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


def curated_enabled(user_id: Optional[int]) -> bool:
    """``CURATED_SOURCES_ENABLED`` for this user, read at the call site. Unreadable is OFF."""
    try:
        from cqc_lem.utilities.flags import CURATED_SOURCES, flag_enabled
        return bool(flag_enabled(CURATED_SOURCES, user_id=user_id))
    except Exception as e:
        log_warning("Could not read the curated-sources flag — treating it as off", exc=e,
                    user_id=user_id)
        return False


def record_candidate(user_id: int, item: dict) -> tuple[Optional[int], str]:
    """Screen ``item`` and store it. Returns ``(row_id, status)``; ``row_id`` is None for a dup.

    The block gate runs HERE, at collect time, so a blocked candidate never reaches the drafter's
    list — and the drafter runs it again, because the rules can change in between.
    """
    verdict = screen_candidate(item)
    status = STATUS_NEW if verdict.ok else STATUS_BLOCKED
    row_id = insert_curated_source(user_id, item, status=status,
                                   block_reason=None if verdict.ok else verdict.reason)
    if not verdict.ok:
        log_debug("Curated candidate blocked at collect time", user_id=user_id,
                  reason=verdict.reason, action_type="curated_source")
    return row_id, status


def plain_text(html: Optional[str], limit: int = EXCERPT_MAX) -> str:
    """HTML (an RSS description, a page body) reduced to whitespace-normalised plain text."""
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _fetch(url: str) -> Optional[str]:
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        response.raise_for_status()
    except requests.RequestException as e:
        # WARNING once per dead feed per run: an allowlist entry that stopped answering is
        # something the owner has to fix in the config file, and nothing else would tell them.
        log_warning("Curated feed fetch failed", exc=e, url=url, action_type="curated_source")
        return None
    if len(response.content) > FEED_MAX_BYTES:
        log_warning("Curated feed too large — skipped", url=url, action_type="curated_source")
        return None
    return response.text


def _date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(xml_text: str, feed: dict) -> list:
    """RSS 2.0 or Atom entries as candidate dicts, newest first as the feed lists them.

    Args:
        xml_text: The feed body.
        feed: The allowlist entry — ``name``, ``url``, ``publisher``, ``licence``, ``paywalled``.

    Returns:
        Up to ``ITEMS_PER_FEED`` candidates. [] for an unparseable feed.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log_warning("Curated feed is not parseable XML", exc=e, url=feed.get("url"),
                    action_type="curated_source")
        return []
    publisher = feed.get("publisher") or feed.get("name") or ""
    licence = normalize_licence(feed.get("licence"))
    items = []
    entries = root.findall("./channel/item") or root.findall(f"{_ATOM}entry")
    for entry in entries[:ITEMS_PER_FEED]:
        if entry.tag == "item":
            link = _text(entry, "link")
            title = _text(entry, "title")
            body = _text(entry, "description")
            author = _text(entry, _DC_CREATOR) or _text(entry, "author")
            published = _date(_text(entry, "pubDate"))
        else:
            # Explicit None checks: an Element with no children is FALSY, so `a or b` would
            # skip a perfectly good <link/> element.
            link_node = entry.find(f"{_ATOM}link[@rel='alternate']")
            if link_node is None:
                link_node = entry.find(f"{_ATOM}link")
            link = (link_node.get("href") or "").strip() if link_node is not None else ""
            title = _text(entry, f"{_ATOM}title")
            body = _text(entry, f"{_ATOM}summary") or _text(entry, f"{_ATOM}content")
            author_node = entry.find(f"{_ATOM}author")
            author = _text(author_node, f"{_ATOM}name") if author_node is not None else ""
            published = _date(_text(entry, f"{_ATOM}updated") or _text(entry, f"{_ATOM}published"))
        if not link.startswith(("http://", "https://")):
            continue
        items.append({
            "platform": PLATFORM_RSS, "url": link, "title": plain_text(title, 512),
            "excerpt": plain_text(body), "author": plain_text(author, 255) or publisher,
            "publisher": publisher, "licence": licence,
            "paywalled": bool(feed.get("paywalled")), "published_at": published,
        })
    return items


def collect_rss(user_ids: list, allowlist: dict) -> dict:
    """Fetch every allowlisted feed ONCE and record its newest items for each user.

    Returns:
        ``{"feeds": n, "items": n, "new": n, "blocked": n, "failed_feeds": n}``.
    """
    counts = {"feeds": 0, "items": 0, "new": 0, "blocked": 0, "failed_feeds": 0}
    for feed in allowlist.get("feeds") or []:
        body = _fetch(feed["url"])
        if body is None:
            counts["failed_feeds"] += 1
            continue
        counts["feeds"] += 1
        for item in parse_feed(body, feed):
            counts["items"] += 1
            for user_id in user_ids:
                row_id, status = record_candidate(user_id, item)
                if row_id:
                    counts["new" if status == STATUS_NEW else "blocked"] += 1
    return counts


# --- Government data ------------------------------------------------------------------------------

def bls_candidate(series: dict, payload: dict) -> Optional[dict]:
    """The latest reading of one BLS series as a candidate, or None when the payload has none.

    The excerpt is ONE sentence in a fixed shape — ``"<label> was <value> in <Month YYYY>."`` — and
    it is the snapshot the chart's figure is validated against, so the drawn number is the
    sentence's own substring. BLS publishes into the public domain and asks for a citation.
    """
    try:
        data = payload["Results"]["series"][0]["data"]
        latest = next(d for d in data if str(d.get("period", "")).startswith("M"))
    except (KeyError, IndexError, TypeError, StopIteration):
        return None
    value = str(latest.get("value") or "").strip()
    if not value or value == "-":
        return None
    period = f"{latest.get('periodName')} {latest.get('year')}"
    unit = series.get("unit") or ""
    shown = f"${value}" if unit == "$" else (f"{value} percent" if unit == "%" else value)
    label = series.get("label") or series["series_id"]
    sentence = f"{label} was {shown} in {period}."
    sid = series["series_id"]
    return {
        "platform": PLATFORM_GOV_DATA,
        "url": f"https://data.bls.gov/timeseries/{sid}?period={latest.get('year')}-"
               f"{latest.get('period')}",
        "title": series.get("title") or label, "excerpt": sentence,
        "author": series.get("publisher") or BLS_PUBLISHER,
        "publisher": series.get("publisher") or BLS_PUBLISHER,
        "licence": LICENCE_PUBLIC_DOMAIN, "paywalled": False,
    }


def collect_gov_data(user_ids: list, allowlist: dict) -> dict:
    """The government-data watchlist: one BLS public-API read per series per run."""
    counts = {"series": 0, "new": 0, "blocked": 0}
    for series in allowlist.get("gov_series") or []:
        try:
            response = requests.get(BLS_API_URL.format(series=series["series_id"]),
                                    timeout=FETCH_TIMEOUT, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            log_warning("BLS series fetch failed", exc=e, series=series.get("series_id"),
                        action_type="curated_source")
            continue
        item = bls_candidate(series, payload)
        if not item:
            continue
        counts["series"] += 1
        for user_id in user_ids:
            row_id, status = record_candidate(user_id, item)
            if row_id:
                counts["new" if status == STATUS_NEW else "blocked"] += 1
    return counts


# --- LinkedIn -------------------------------------------------------------------------------------

def linkedin_candidate(author: str, content: str, container_urn: Optional[str],
                       permalink: Optional[str] = None,
                       profile_url: Optional[str] = None) -> Optional[dict]:
    """A feed/roster post as a candidate, recording BOTH URNs where the card exposed them.

    Only a URN read off the card's OWN container counts: a URN scanned from inside the card can
    belong to a post it reshares, and resharing the wrong parent would credit the wrong person. With
    no share/ugcPost URN the item is ``link_only`` — linked with credit, never reshared, never
    screenshotted.
    """
    share, activity = split_post_urns(container_urn, permalink)
    urn = share or activity
    if not urn or not (content or "").strip():
        return None
    return {
        "platform": PLATFORM_LINKEDIN,
        "url": f"https://www.linkedin.com/feed/update/{urn}/",
        "canonical_id": share if is_reshareable_urn(share) else None,
        "activity_urn": activity,
        "link_only": not is_reshareable_urn(share),
        "author": (author or "").strip() or None,
        "publisher": "LinkedIn",
        "title": None,
        "excerpt": (content or "").strip()[:EXCERPT_MAX],
        "licence": LICENCE_LINKEDIN_NATIVE,
        "paywalled": False,
        "og_image_url": None,
        "profile_url": profile_url,
    }


def record_linkedin_candidate(user_id: int, card, author: str, content: str, driver=None,
                              profile_url: Optional[str] = None) -> None:
    """The feed/roster hook: record the post the lane is about to engage. Never raises.

    Runs only for a user with ``CURATED_SOURCES_ENABLED``, and only reads the card's own container
    URN (one script call) — no extra navigation.
    """
    try:
        if not curated_enabled(user_id):
            return
        from cqc_lem.utilities.linkedin.cards import (
            _feed_post_container_urn,
            _post_permalink_from_card,
        )
        container_urn = _feed_post_container_urn(card, driver=driver)
        item = linkedin_candidate(author, content, container_urn, None, profile_url)
        if item is None:
            # A card with no container URN: the permalink anchor can point at a RESHARED
            # original (#2151), so it is never taken as this post's identity.
            log_debug("Curated candidate skipped — no container URN on the card", user_id=user_id,
                      action_type="curated_source",
                      has_permalink=bool(_post_permalink_from_card(card)))
            return
        record_candidate(user_id, item)
    except Exception as e:
        log_warning("Could not record a LinkedIn post as a curated candidate", exc=e,
                    user_id=user_id, action_type="curated_source")


# --- Manual ---------------------------------------------------------------------------------------

def is_linkedin_host(url: Optional[str]) -> bool:
    """True only when the URL's parsed hostname IS linkedin.com or a subdomain of it.

    Never a substring test: ``https://evil.com/?x=linkedin.com/`` names linkedin.com and is not it.
    """
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and (
        host == "linkedin.com" or host.endswith(".linkedin.com"))


def manual_candidate(url: str, author: Optional[str] = None, title: Optional[str] = None,
                     excerpt: Optional[str] = None, publisher: Optional[str] = None,
                     licence: Optional[str] = None) -> dict:
    """A URL the owner pasted (an X post, an article). The owner supplies who said what.

    Nothing is fetched for an X URL — LEM pays for no X reads (owner decision 2026-10-07) — so the
    owner's own author/title/excerpt ARE the snapshot. A LinkedIn post URL pasted here keeps its
    URNs, so a pasted share URL can still be reshared.
    """
    url = (url or "").strip()
    is_linkedin, share, activity = False, None, None
    if is_linkedin_host(url):
        # URNs are read from the PATH only: a query string is attacker-shaped text, and a URN
        # smuggled into it would reshare a post the pasted URL does not point at.
        share, activity = split_post_urns(urlparse(url).path)
        is_linkedin = bool(share or activity)
    return {
        "platform": PLATFORM_LINKEDIN if is_linkedin else PLATFORM_MANUAL,
        "url": url,
        "canonical_id": share if is_linkedin and is_reshareable_urn(share) else None,
        "activity_urn": activity if is_linkedin else None,
        "link_only": not (is_linkedin and is_reshareable_urn(share)),
        "author": (author or "").strip() or None,
        "publisher": (publisher or "").strip() or None,
        "title": (title or "").strip() or None,
        "excerpt": (excerpt or "").strip()[:EXCERPT_MAX] or None,
        "licence": LICENCE_LINKEDIN_NATIVE if is_linkedin else normalize_licence(
            licence or LICENCE_UNKNOWN),
        "paywalled": False,
    }
