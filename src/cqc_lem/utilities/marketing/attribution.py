"""Marketing attribution at the SOURCE — the ONE place an outbound LEM link gets its UTMs (#658).

#503 built the CAPTURE half: `observability.track_funnel_event` reads UTMs off a signup and derives
a `channel`, and `ui/src/utils/attribution.ts` carries the landing URL's UTMs to the auth call. None
of that can attribute anything if the links we publish are bare — a brand post CTA, a carried first
comment, a YouTube description and a newsletter subscribe link all landed on the site with no source
on them, so every organic signup read as `direct`.

Two rules make this safe to apply everywhere:

- **Only OUR OWN destinations get tagged.** UTMs are query params the destination's analytics reads;
  stamping them on a third-party article we cited pollutes someone else's reporting and attributes
  nothing to us. `is_owned_link` is the gate, and it is deliberately conservative — an unparseable
  or non-http URL is not ours.
- **An existing UTM is never overwritten.** A link the owner tagged by hand (or one already tagged
  further up the pipeline — a post body link keeps its `utm_medium=social` when the #392 split
  carries it into the first comment) wins. This function only ever FILLS IN what is missing, so it
  is idempotent and safe to run at more than one choke point.

Campaign names come from the `campaign_for_*` helpers rather than being spelled at each call site:
a breakdown by campaign is only readable if every post writes the campaign the same way.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from cqc_lem.utilities.env_constants import BRAND_SIGNUP_URL, MARKETING_OWNED_DOMAINS, PUBLIC_BASE_URL

# `utm_source` values. These feed observability.resolve_channel, which maps a source onto one of its
# `CHANNELS` — so a new source needs a rule there or it lands in `other`.
SOURCE_LINKEDIN = "linkedin"
SOURCE_NEWSLETTER = "newsletter"
SOURCE_YOUTUBE = "youtube"
SOURCE_REFERRAL = "referral"
SOURCE_EMAIL = "email"

# `utm_medium` values — the marketing medium the link was published through.
MEDIUM_SOCIAL = "social"
MEDIUM_VIDEO = "video"
MEDIUM_NEWSLETTER = "newsletter"
MEDIUM_PROFILE = "profile"
MEDIUM_REFERRAL = "referral"

# `utm_content` values — the PLACEMENT within a medium. This is what tells a link left in a post
# body apart from the same campaign's link the #392 split carried into the first comment. Spelled
# in their ALREADY-SLUGGED form: every value goes through `_slug` on the way onto a URL, so an
# underscore here would be a constant that never equals the value the dashboard actually groups on.
PLACEMENT_POST_BODY = "post-body"
PLACEMENT_FIRST_COMMENT = "first-comment"
PLACEMENT_VIDEO_DESCRIPTION = "video-description"

# Campaign for the always-on brand surfaces that have no per-item id (profile CTA, business goal).
CAMPAIGN_BRAND_PROFILE = "brand-profile"
CAMPAIGN_REFERRAL = "member-referral"

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
# The referral parameter (`?ref=<user id>`) rides beside the UTMs rather than inside `utm_content`:
# it identifies WHO referred, which is a person, not a creative variant.
REFERRAL_PARAM = "ref"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Trailing sentence punctuation is not part of the URL — "…see https://x.io/a." must not tag the dot.
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]")
_CAMPAIGN_MAX_LEN = 60


def _slug(value: Optional[str]) -> str:
    """A lowercase, hyphenated fragment safe to read in a PostHog breakdown."""
    return _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")


def _host(url: Optional[str]) -> str:
    """The bare, www-stripped host of `url`, or "" when there isn't one to read."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _bare_host(value: str) -> str:
    """A scheme-less MARKETING_OWNED_DOMAINS entry reduced to the same shape `_host` returns.

    It has to be the same shape or the comparison in `is_owned_link` silently never matches, and a
    domain the operator configured is simply never tagged — the failure looks exactly like the
    pre-#658 behaviour, so nothing surfaces it. `www.` and a port are the two that actually get
    typed ("www.example.com", "example.com:8443").
    """
    host = str(value or "").strip().lower().lstrip(".")
    host = host.split("//")[-1].split("/")[0].split("?")[0].split("#")[0]
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def owned_hosts() -> set:
    """Hosts whose analytics we own, so tagging them is ours to do: this deployment's public base
    URL, the configured trial signup URL, and anything else the operator lists in
    MARKETING_OWNED_DOMAINS (the marketing site / blog, which live outside the app).
    """
    hosts = {_host(PUBLIC_BASE_URL), _host(BRAND_SIGNUP_URL)}
    for entry in str(MARKETING_OWNED_DOMAINS or "").split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        # Accept either a bare host ("lem.example.com") or a full URL.
        hosts.add(_host(candidate) or _bare_host(candidate))
    return {h for h in hosts if h}


def is_owned_link(url: Optional[str]) -> bool:
    """Whether `url` points at a destination whose analytics we own. Subdomains of an owned host
    count (a docs/blog subdomain is the same property); an unparseable, relative or non-http URL
    never does.
    """
    host = _host(url)
    if not host:
        return False
    return any(host == owned or host.endswith("." + owned) for owned in owned_hosts())


def build_utm_url(url: Optional[str], source: str, medium: str, campaign: str,
                  content: Optional[str] = None, term: Optional[str] = None,
                  ref: Optional[str] = None) -> str:
    """`url` with the UTM (and optional `ref`) parameters filled in.

    Existing query parameters are preserved and existing UTMs are NEVER overwritten — a hand-tagged
    link keeps its own attribution, and running this twice on the same URL is a no-op. A blank or
    non-http URL comes back byte-identical, so a call site never has to guard before calling.
    """
    raw = str(url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if parsed.scheme not in ("http", "https"):
        return raw

    wanted = {"utm_source": _slug(source), "utm_medium": _slug(medium),
              "utm_campaign": _slug(campaign)[:_CAMPAIGN_MAX_LEN],
              "utm_content": _slug(content), "utm_term": _slug(term)}
    if ref:
        wanted[REFERRAL_PARAM] = str(ref).strip()

    params = parse_qsl(parsed.query, keep_blank_values=True)
    present = {key for key, _ in params}
    for key in (*UTM_KEYS, REFERRAL_PARAM):
        value = wanted.get(key)
        if value and key not in present:
            params.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(params)))


def tag_owned_link(url: Optional[str], source: str, medium: str, campaign: str,
                   content: Optional[str] = None, ref: Optional[str] = None) -> str:
    """`build_utm_url` for a link we own, and a pass-through for anything else. This is what call
    sites use: an artifact CTA may hold the user's own newsletter on linkedin.com, a post may cite a
    news article, and neither is ours to stamp.
    """
    if not is_owned_link(url):
        return str(url or "")
    return build_utm_url(url, source, medium, campaign, content=content, ref=ref)


def mark_placement(url: Optional[str], placement: str) -> str:
    """Set `utm_content` on an owned link to where it ACTUALLY landed, replacing whatever placement
    was guessed upstream.

    This is the one deliberate exception to "never overwrite": a promo CTA is written days before
    publish and assumes its link stays in the body, but #392's split decides at publish time whether
    the link is carried into the first comment instead. Only `utm_content` is touched, and only on a
    link we own — source, medium and campaign are still first-write-wins.
    """
    if not is_owned_link(url) or not _slug(placement):
        return str(url or "")
    parsed = urlparse(str(url).strip())
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
              if k != "utm_content"]
    params.append(("utm_content", _slug(placement)))
    return urlunparse(parsed._replace(query=urlencode(params)))


def tag_links_in_text(text: Optional[str], source: str, medium: str, campaign: str,
                      content: Optional[str] = None) -> str:
    """Rewrite every OWNED link inside a body of text with its UTMs, leaving third-party links (and
    the surrounding prose) untouched. Used where the link is embedded in copy a model wrote — a post
    body, a video description — rather than handed to us as a bare URL.
    """
    body = str(text or "")
    if not body:
        return body

    def _replace(match):
        link = match.group(0)
        return build_utm_url(link, source, medium, campaign, content=content) \
            if is_owned_link(link) else link

    # Rewritten in ONE pass over the match spans rather than by successive str.replace: a
    # replace-per-link corrupts any URL that another URL is a prefix of ("…/a" inside "…/a/b"),
    # and sorting longest-first does not fix it — the short one still matches inside the longer
    # one's already-rewritten text.
    return _URL_RE.sub(_replace, body)


# --- campaign vocabulary --------------------------------------------------------------------------
# One campaign per publishable ITEM, so "which post/edition/video actually drove signups" is a
# breakdown rather than a guess. Shape: <surface>-<id>[-<archetype>], all slugged.

def campaign_for_post(post_id: Optional[int] = None, archetype: Optional[str] = None) -> str:
    """The campaign for a LinkedIn post's links. `archetype` (the #619 blueprint format) rides along
    so the breakdown can answer "which SHAPE of post converts", not just which one.
    """
    parts = ["post"]
    if post_id is not None:
        parts.append(str(int(post_id)))
    shape = _slug(archetype)
    if shape:
        parts.append(shape)
    return "-".join(parts)[:_CAMPAIGN_MAX_LEN]


def campaign_for_edition(edition_id: Optional[int] = None) -> str:
    """The campaign for a newsletter edition's links — one per edition, per the issue's ask."""
    return ("newsletter" if edition_id is None else f"newsletter-{int(edition_id)}")[:_CAMPAIGN_MAX_LEN]


def campaign_for_tutorial(flow_key: Optional[str] = None) -> str:
    """The campaign for a tutorial video's description links — one per feature flow, since a flow is
    re-filmed in place rather than re-published under a new id.
    """
    slug = _slug(flow_key)
    return (f"tutorial-{slug}" if slug else "tutorial")[:_CAMPAIGN_MAX_LEN]


# --- LEM's own destinations -----------------------------------------------------------------------

def signup_landing() -> str:
    """Where a trial CTA points. `BRAND_SIGNUP_URL` when the deployment names one, otherwise this
    deployment's own public base URL — the landing page IS the signup surface, so the brand engine
    needs no extra configuration to have a link to publish (issue #736). Empty only when neither is
    set, which every caller reads as "there is no CTA link to publish".
    """
    return str(BRAND_SIGNUP_URL or "").strip() or str(PUBLIC_BASE_URL or "").strip()


def signup_url(source: str, medium: str, campaign: str, content: Optional[str] = None,
               ref: Optional[str] = None) -> str:
    """The trial signup URL, tagged. Empty string when there is no landing page configured at all."""
    base = signup_landing()
    if not base:
        return ""
    return build_utm_url(base, source, medium, campaign, content=content, ref=ref)


def referral_code(user_id: Optional[int]) -> str:
    """The `ref` value identifying the referrer. The user id, which is what the capture side already
    resolves a person by — the full referral program (codes, rewards) is its own launch-plan issue;
    this is only the link format so the groundwork is in place and measurable.
    """
    if user_id is None:
        return ""
    try:
        return str(int(user_id))
    except (TypeError, ValueError):
        return ""


def referral_url(user_id: Optional[int], base: Optional[str] = None) -> str:
    """A member's referral link: `utm_source=referral&utm_medium=referral&ref=<user>`. Falls back to
    the signup URL when no explicit landing page is given; empty when neither is configured.
    """
    code = referral_code(user_id)
    if not code:
        return ""
    landing = str(base or "").strip() or signup_landing()
    if not landing:
        return ""
    return build_utm_url(landing, SOURCE_REFERRAL, MEDIUM_REFERRAL, CAMPAIGN_REFERRAL, ref=code)


def parse_referral(url: Optional[str]) -> dict:
    """The attribution properties carried on an inbound URL — the UTMs plus `ref`. Used server-side
    to read a landing URL we were handed; the SPA does the same read off `window.location` in
    `utils/attribution.ts`. Keys with no value are omitted, so the result can be merged straight
    into a funnel event's attribution without inventing empty buckets.
    """
    try:
        query = urlparse(str(url or "").strip()).query
    except ValueError:
        return {}
    params = dict(parse_qsl(query, keep_blank_values=False))
    out = {}
    for key in (*UTM_KEYS, REFERRAL_PARAM):
        value = str(params.get(key) or "").strip()
        if value:
            out[key] = value
    return out
