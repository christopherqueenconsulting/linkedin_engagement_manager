"""The one spelling of a LinkedIn member profile URL, and the key two spellings share (issue #2684).

Pure string work, no Selenium and no network, so the API and the repositories can both import it.

Stored rows carry whatever form the scraper or supplier saw: `https://linkedin.com/in/jane-doe`,
`…/in/jane-doe?originalSubdomain=uk`, `…/in/Jane-Doe/`. `normalise_profile_url` is the form a
queue-only seat's URL is STORED in from now on; existing rows are never rewritten, so a duplicate
lookup compares `profile_url_key`, which every one of those spellings shares.
"""

import re
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

# `/in/<slug>` and nothing beneath it. The slug may be unicode or percent-encoded (`\w` is
# unicode-aware), never a separator.
_PROFILE_PATH = re.compile(r"/in/([\w\-%]+)/?")
_PROFILE_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})


def normalise_profile_url(url: str) -> Optional[str]:
    """The canonical `https://www.linkedin.com/in/<slug>/`, or None when `url` is not a profile.

    Collapses surrounding whitespace, `http`, a bare `linkedin.com`, an upper-case host, a query
    string or fragment, and a missing trailing slash. Refuses another host, a company page, a path
    beneath a profile, credentials and a port.

    Args:
        url: The URL as the caller supplied it.

    Returns:
        The canonical profile URL, or None.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # raises ValueError on a malformed port, like urlsplit on a bad netloc
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password:
        return None
    if (parts.hostname or "") not in _PROFILE_HOSTS or port is not None:
        return None
    match = _PROFILE_PATH.fullmatch(parts.path)
    if not match:
        return None
    return f"https://www.linkedin.com/in/{match.group(1)}/"


def profile_url_key(url: Optional[str]) -> Optional[str]:
    """The identity two spellings of one profile share: the slug, percent-decoded and lowercased.

    Args:
        url: Any stored or supplied profile URL, canonical or legacy.

    Returns:
        The key, or None when `url` is not a LinkedIn profile URL.
    """
    canonical = normalise_profile_url(url or "")
    if canonical is None:
        return None
    return unquote(canonical[len("https://www.linkedin.com/in/"):-1]).lower()


def profile_url_like_patterns(key: str) -> tuple[str, ...]:
    """Lowercased SQL LIKE patterns that find every stored spelling carrying `key` as its slug.

    A narrowing pre-filter only: the caller still compares `profile_url_key` on each candidate,
    because a LIKE also matches a longer slug that merely starts with this one. LIKE's own
    wildcards (`%`, `_` and the backslash) are escaped, and both the unicode and the percent-encoded slug are
    searched, since a stored row may carry either.

    Args:
        key: A `profile_url_key`.

    Returns:
        One or two patterns, for `LOWER(column) LIKE %s`.
    """
    def _like(slug: str) -> str:
        escaped = slug.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%/in/{escaped}%"

    forms = dict.fromkeys([key, quote(key, safe="").lower()])
    return tuple(_like(form) for form in forms)
