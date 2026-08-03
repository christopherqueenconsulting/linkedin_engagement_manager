"""Blog/site source material for newsletter editions — the ONE place `align_with_blog` becomes text.

`align_with_blog` is a user-facing toggle that defaults ON and promises the edition repurposes the
author's own writing. It resolves through the SAME fetchers the `blog_summary` / `website_content`
post types already use (`run_content_plan`), so the app keeps one scraper, not two.

Best-effort by design: an unset, unreachable, or empty blog yields None and the edition is written
from topic + profile exactly as before — source material must never block an edition from existing.
The blog URL is the primary source; the sitemap is the fallback for users who set one but no blog.
"""

import random

from bs4 import BeautifulSoup

from cqc_lem.utilities.logger import log_debug, log_warning

# What we carry around per edition. The generator applies its own (smaller) prompt budget on top;
# this only bounds how much page text a single resolve holds in memory.
BLOG_SOURCE_MAX_CHARS = 8000

# Sitemap pages that yield nothing readable are cheap to skip and expensive to keep trying.
_SITEMAP_ATTEMPTS = 3

_ACTION = "newsletter_blog_align"


def _plain_text(raw: object) -> str:
    """HTML / bytes / a list of paragraphs -> readable prose.

    WordPress hands back rendered HTML and the scrape fallback hands back whole pages, so without
    this the writer's source budget is spent on markup instead of the author's words.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, (list, tuple)):
        raw = "\n\n".join(str(part) for part in raw if part)
    text = BeautifulSoup(str(raw), "html.parser").get_text(separator="\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _from_blog(blog_url: str, user_id: int) -> "tuple[str, bool]":
    """(one recent article as plain text, whether the failure was already reported).

    The flag is what keeps ONE unreadable source to ONE warning: a fetch that failed has already
    warned where it was detected, so the caller must not restate it.
    """
    from cqc_lem.app.run_content_plan import get_main_blog_url_content
    try:
        _post_url, post_content = get_main_blog_url_content(blog_url)
    except Exception as e:
        log_warning("Blog fetch failed for newsletter alignment", exc=e, user_id=user_id,
                    action_type=_ACTION)
        return "", True
    return _plain_text(post_content)[:BLOG_SOURCE_MAX_CHARS], False


def _from_sitemap(sitemap_url: str, user_id: int) -> "tuple[str, bool]":
    """(a readable content page as plain text, whether the failure was already reported).

    The page is drawn at RANDOM from the relevant URLs rather than fixed at the first one. This
    resolves per edition, so a deterministic pick would hand every edition queued in one run the
    same page — exactly what the blog path already avoids by choosing its article at random.
    """
    from cqc_lem.app.run_content_plan import (extract_page_content, fetch_sitemap_urls,
                                              filter_relevant_urls)
    try:
        urls = list(filter_relevant_urls(fetch_sitemap_urls(sitemap_url) or []))
    except Exception as e:
        log_warning("Sitemap fetch failed for newsletter alignment", exc=e, user_id=user_id,
                    action_type=_ACTION)
        return "", True
    for url in random.sample(urls, min(_SITEMAP_ATTEMPTS, len(urls))):
        try:
            title, paragraphs = extract_page_content(url)
        except Exception:
            # One dead page out of a sitemap is routine — we just try the next one.
            log_debug("Sitemap page unreadable for newsletter alignment", user_id=user_id,
                      action_type=_ACTION)
            continue
        text = _plain_text(paragraphs)
        if text:
            carried = _plain_text(f"{title}\n\n{text}" if title else text)
            return carried[:BLOG_SOURCE_MAX_CHARS], False
    return "", False


def resolve_blog_source(user_id: int, settings: dict = None) -> "str | None":
    """Source material for ONE newsletter edition, or None when there is nothing to repurpose.

    Returns None (never raises) whenever the toggle is off, the user configured no blog/sitemap, or
    nothing readable came back — the caller then generates from topic + profile. Each call picks a
    fresh recent article, so two editions queued in the same run don't repurpose the same one.
    """
    if not (settings or {}).get("align_with_blog"):
        return None
    try:
        return _resolve(user_id)
    except Exception as e:
        # Best-effort means best-effort: two of the three call sites resolve OUTSIDE their own try,
        # so a DB blip reading the configured URLs would otherwise take the whole edition down.
        log_warning("Blog alignment could not be resolved", exc=e, user_id=user_id,
                    action_type=_ACTION)
        return None


def _resolve(user_id: int) -> "str | None":
    from cqc_lem.utilities.db import get_user_blog_url, get_user_sitemap_url

    reported = False
    blog_url = get_user_blog_url(user_id)
    if blog_url:
        text, reported = _from_blog(blog_url, user_id)
        if text:
            log_debug("Resolved blog source material for newsletter edition", user_id=user_id,
                      action_type=_ACTION)
            return text

    sitemap_url = get_user_sitemap_url(user_id)
    if sitemap_url:
        text, sitemap_reported = _from_sitemap(sitemap_url, user_id)
        reported = reported or sitemap_reported
        if text:
            log_debug("Resolved sitemap source material for newsletter edition", user_id=user_id,
                      action_type=_ACTION)
            return text

    if not blog_url and not sitemap_url:
        # Expected no-op: the toggle defaults ON, so most users have it set with no blog configured.
        log_debug("Blog alignment on but no blog or sitemap URL is set", user_id=user_id,
                  action_type=_ACTION)
    elif not reported:
        # The one failure nothing has warned about yet: a source that fetched fine and read empty.
        log_warning("Blog alignment on but the configured source returned no readable text",
                    user_id=user_id, action_type=_ACTION)
    return None
