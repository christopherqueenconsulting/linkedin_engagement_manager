"""The last deterministic pass over text that is about to be PUBLIC on LinkedIn.

Everything here is post-processing of model output, and each piece is paired with a prompt directive
that tries to prevent the problem upstream — `PLAIN_PUNCTUATION_DIRECTIVE` before
`normalize_public_text`, `linkedin_post_format_directive` before `enforce_post_readability`. The
directive is the prevention, the function is the safety net; a model that ignores the directive is
the normal case, not the exception, which is why neither half is optional.

Two things the safety net is deliberately careful NOT to touch: URLs are masked out of every
markdown transform in `sanitize_for_linkedin` and come back byte-identical, and intentional glyphs
(bullets, emoji, accented letters) survive normalization — LinkedIn renders markdown literally, but
mangling a link or an emoji is a worse failure than a stray asterisk.

`_BAIT_RE` is the ONE engagement-bait vocabulary: detection (`contains_engagement_bait`), removal
(`strip_engagement_bait`) and lead-magnet keyword rejection (`is_bait_keyword`) all read that same
list, so a pattern added there takes effect on every surface at once and cannot disagree with itself
about what counts as bait.
"""

import re
import unicodedata
from typing import Optional

# Fancy typographic glyphs that models love to emit — em/en dashes, curly quotes, ellipsis, exotic
# spaces — are tell-tale AI signs and sometimes render as mojibake on downstream surfaces. Map each
# to a plain ASCII equivalent. Uses \u escapes (never literal glyphs) so the table stays unambiguous.
# Deliberately does NOT touch the bullet char (U+2022, used on purpose for LinkedIn bullets), the
# em dash (handled separately -> spaced hyphen), or emojis/accented letters, which may be intentional.
_TYPOGRAPHY_MAP = {
    "–": "-", "―": "-", "‒": "-", "‑": "-", "−": "-",  # en/horiz-bar/figure/nb-hyphen/minus
    "‘": "'", "’": "'", "‚": "'", "‛": "'",  # single curly quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',  # double curly quotes
    "′": "'", "″": '"', "‵": "'", "‶": '"',  # primes
    "«": '"', "»": '"', "‹": "'", "›": "'",  # guillemets
    "…": "...",  # horizontal ellipsis
    # Exotic / non-breaking spaces -> plain space
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "　": " ",
    "�": "",  # replacement char (mojibake artifact)
}
# Zero-width / invisible chars models sometimes inject — strip entirely.
_ZERO_WIDTH = {ord(c): None for c in "​‌‍⁠﻿"}

# Em dash (U+2014) -> spaced hyphen, absorbing any padding so "a — b" and "a—b" both become "a - b".
_EM_DASH_RE = re.compile(r"[ \t]*—[ \t]*")


def normalize_public_text(text: str) -> str:
    """Normalize AI-generated, public-facing text to plain ASCII punctuation so no rogue non-standard
    characters (em dashes, smart quotes, ellipsis, exotic spaces, zero-width/control chars) leak out.
    Em dashes become a spaced hyphen; emojis, accented letters and intentional bullets are preserved.
    """
    if not text:
        return text
    text = _EM_DASH_RE.sub(" - ", text)
    for bad, good in _TYPOGRAPHY_MAP.items():
        text = text.replace(bad, good)
    text = text.translate(_ZERO_WIDTH)
    # Drop control chars except tab/newline/carriage-return.
    text = "".join(c for c in text if c in "\n\r\t" or unicodedata.category(c)[0] != "C")
    # Collapse runs of spaces/tabs left by the substitutions (never touch newlines).
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def strip_non_bmp(text: str) -> str:
    """`normalize_public_text` plus the one thing ChromeDriver cannot type: non-BMP characters.

    Two separate problems that always travel together on a Selenium path. The normalize pass keeps
    AI typography (em dashes, smart quotes) out of public text; the second drops anything above
    U+FFFF, because `send_keys` raises `WebDriverException` on it — which is most emoji — and takes
    the whole composer down with it.

    Lives here rather than in a task module because both Selenium composers and the invite path need
    it, and `linkedin_formatter` is the leaf both can import without a cycle.
    """
    text = normalize_public_text(text or "")
    return "".join(c for c in text if ord(c) <= 0xFFFF)


# Drop-in directive for AI system prompts so models avoid the fancy punctuation in the first place
# (normalize_public_text is the safety net; this is the prevention).
PLAIN_PUNCTUATION_DIRECTIVE = (
    "PUNCTUATION: Use only plain ASCII punctuation. NEVER use em dashes or en dashes - use a comma, "
    "period, or a plain hyphen instead. Do NOT use curly/smart quotes (use straight ' and \") and do "
    "NOT use the ellipsis character - type three periods. Avoid any other non-standard Unicode "
    "punctuation; these read as AI-generated."
)


def linkedin_post_format_directive(max_chars: int = 2200) -> str:
    """Shared LinkedIn post-formatting best practices for AI system prompts (the QA rules we
    researched): hook first line, short scannable paragraphs separated by BLANK LINES, a hard length
    cap, and no markdown. This is the PREVENTION side; enforce_post_readability() is the safety net
    for when the model ignores it (e.g. JSON-mode carousel captions that come back as one long block).
    """
    return (
        "FORMAT FOR LINKEDIN: Open with a scroll-stopping first line (the hook) - it is the only line "
        "shown before the '...more' fold. Then write SHORT, scannable paragraphs of 1-2 sentences each, "
        "each separated by a BLANK LINE (real line breaks - never one long block of text). Keep the "
        f"entire post under {max_chars} characters. Do NOT use markdown (no **bold**, no # headers, no "
        "bullet characters unless intentional) - LinkedIn renders it literally."
    )


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# A "meta" line that should stay on its own, not be reflowed into prose (hashtag line, bare URL,
# or a near-empty line).
_META_LINE_RE = re.compile(r"^(#\S|https?://|\W{0,3}$)")


def _reflow_into_paragraphs(prose: str, target_paragraph_chars: int) -> list:
    """Group a single long prose string into short paragraphs of ~target_paragraph_chars, breaking
    only on sentence boundaries.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(prose) if s.strip()]
    if len(sentences) <= 1:
        return [prose]
    paras, cur = [], ""
    for s in sentences:
        if cur and len(cur) + 1 + len(s) > target_paragraph_chars:
            paras.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        paras.append(cur)
    return paras


def enforce_post_readability(text: str, max_chars: int = 2200, target_paragraph_chars: int = 220,
                             long_paragraph_chars: int = 350) -> str:
    """Deterministic QA guard for AI-generated post captions - the safety net when the model ignores
    the format directive (e.g. a carousel caption that came back as a wall of text):

      - break any OVER-LONG paragraph (a single-line prose block longer than long_paragraph_chars)
        into short, scannable paragraphs. This catches both a total wall (one giant paragraph) AND an
        under-formatted post (a few very long paragraphs), while leaving already-short paragraphs and
        multi-line blocks (lists) untouched. Trailing hashtag/link lines stay on their own.
      - hard-cap length at a sentence boundary (LinkedIn's limit is 3000; the default stays well under).

    Well-formatted (short paragraphs) or short posts are returned unchanged.
    """
    if not text:
        return text
    text = text.strip()
    out_paras = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        # Peel trailing meta lines (hashtags / links) so they aren't reflowed into the prose.
        lines = block.split("\n")
        trailer = []
        while len(lines) > 1 and _META_LINE_RE.match(lines[-1].strip()):
            trailer.insert(0, lines.pop().strip())
        prose = "\n".join(lines).strip()
        # Only reflow a SINGLE-LINE prose block that is too long; leave short blocks and multi-line
        # blocks (lists, deliberately line-broken content) as they are.
        if "\n" not in prose and len(prose) > long_paragraph_chars:
            out_paras.extend(_reflow_into_paragraphs(prose, target_paragraph_chars))
        elif prose:
            out_paras.append(prose)
        if trailer:
            out_paras.append("\n".join(trailer))
    text = "\n\n".join(out_paras)
    if len(text) > max_chars:
        cut = text[:max_chars]
        ends = list(re.finditer(r"[.!?](?:\s|$)", cut))
        text = (cut[:ends[-1].end()] if ends else cut).rstrip()
    return text


# A URL is markdown-shaped by accident: `?utm_source=a&utm_medium=b` is exactly the `_word_` the
# italic pass eats, and `**`/backtick/`---` can fall inside one just as easily. Mask every http(s)
# span before the markdown transforms and restore it verbatim after, so a link publishes
# byte-identical to the one the user configured (issue #823).
#
# Two character classes, because a markdown marker means different things inside a URL and at its
# edge. `*` and `_` are legal URL characters, so the span must be allowed to CONTAIN them (stopping
# at the first `*` exposes the rest of the URL to the italic pass: `?q=a*b*c` -> `?q=abc`). But a
# span may not END on one, or emphasis wrapped around a URL gets swallowed: `_url_` would absorb the
# closing `_`, leaving the opener unpaired and publishing a link with a trailing underscore. Trailing
# sentence punctuation is excluded for the same reason — it belongs to the prose, not the link.
# `)`, `]`, quotes and backtick are excluded outright: those are what markdown wraps a URL IN, so
# `[text](url)` still converts (it matches against the masked token) and `` `url` `` still unwraps.
_URL_SPAN_RE = re.compile(r"https?://[^\s<>\"'`)\]]*[^\s<>\"'`)\]*_.,;:!?]")
# Starts with a control char, so no line-start rule (header, bullet, numbered list) can match it.
# normalize_public_text has already dropped any control char the input itself carried.
_URL_TOKEN = "\x00lemurl{}\x00"


def _mask_urls(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def _capture(match: re.Match) -> str:
        urls.append(match.group(0))
        return _URL_TOKEN.format(len(urls) - 1)

    return _URL_SPAN_RE.sub(_capture, text), urls


def _unmask_urls(text: str, urls: list[str]) -> str:
    for index, url in enumerate(urls):
        text = text.replace(_URL_TOKEN.format(index), url)
    return text


def sanitize_for_linkedin(text: str) -> str:
    """Strip markdown syntax from AI-generated text so it renders cleanly on LinkedIn.

    LinkedIn does not render standard markdown. This function removes formatting
    markers while preserving the underlying text, emojis, hashtags, and line breaks.
    Also normalizes rogue typographic characters (em dashes, smart quotes, ...) to plain ASCII.
    URLs are exempt from every markdown transform and survive byte-identical.
    """
    if not text:
        return text

    text = normalize_public_text(text)
    text, urls = _mask_urls(text)

    # Remove markdown headers (# through ######) at line start, keep the text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Convert markdown unordered bullets (- item or * item at line start) → • item
    # Must run BEFORE italic stripping so "* item" isn't consumed by the italic regex
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Remove horizontal rules (--- or *** or ___ on their own line)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Remove bold markers (**text** or __text__)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)

    # Remove italic markers (*text* or _text_) — only single * not preceded/followed by *
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text, flags=re.DOTALL)

    # Convert markdown links [text](url) → text (url)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 (\2)", text)

    # Remove inline code backticks `code` → code
    text = re.sub(r"`(.+?)`", r"\1", text)

    # Convert numbered lists (1. item) → keep numbering but clean up spacing
    text = re.sub(r"^(\d+)\.\s+", r"\1. ", text, flags=re.MULTILINE)

    # Strip trailing whitespace on each line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)

    # Normalize excessive blank lines (3 or more newlines → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return _unmask_urls(text, urls).strip()


# Classic engagement-bait patterns LinkedIn's 2026 algorithm penalizes. Deliberately does NOT match
# generic "comment <keyword>" so it never strips a legitimate lead-magnet CTA (Phase 4).
_BAIT_PATTERNS = [
    r"tag (a friend|someone|three|3|your)",
    r"like if you",
    r"(hit|smash)\s+(the\s+)?like",
    r"double[- ]tap",
    r"(repost|share)\s+(this\s+)?if",
    r"comment\s+[\"']?(yes|agree|below|amen|me|👇)\b",
    # 2026 additions (issue #393): reflex-action asks the algorithm now demotes outright.
    r"follow\s+(me\s+|us\s+)?for\s+more",
    r"(hit|smash|tap)\s+(that\s+|the\s+)?(save|follow|subscribe)",
    r"type\s+[\"'“”]?\w+[\"'“”]?\s+(below|in the comments)",
    r"drop\s+(a|an)\s+(like|emoji|\d+)\b",
    r"drop\s+(a|an)\s+[\U0001F300-\U0001FAFF]",
]
_BAIT_RE = re.compile("|".join(_BAIT_PATTERNS), re.IGNORECASE)


def contains_engagement_bait(text: Optional[str]) -> bool:
    """True when `text` asks for a reflex engagement action for its own sake (a one-word reply, a
    like, a tag, a follow) — the closes LinkedIn demotes in 2026. This is the ONE bait detector:
    `strip_engagement_bait` removes such lines from drafts, `is_bait_keyword` rejects colliding
    lead-magnet trigger words, and the content-framework CTA menus are guarded against it.
    """
    return bool(text) and bool(_BAIT_RE.search(str(text)))


def is_bait_keyword(keyword: Optional[str]) -> bool:
    """True when 'comment <keyword>' would itself trip the bait filter (YES/AGREE/BELOW/AMEN/ME/👇).
    Such a lead-magnet trigger word can never reliably survive strip_engagement_bait, so it must be
    rejected at configuration time.
    """
    kw = str(keyword or "").strip()
    return bool(kw) and bool(_BAIT_RE.search(f"comment {kw}"))


def strip_engagement_bait(text: str, exempt_keyword: Optional[str] = None) -> str:
    """Drop lines containing classic engagement-bait CTAs (penalized). Conservative and line-level:
    bait is almost always its own CTA line, and we avoid touching 'comment <keyword>' lead magnets.
    `exempt_keyword` protects lines carrying the user's configured lead-magnet trigger word
    (whole-word, case-insensitive) when that keyword happens to collide with the bait regex.
    """
    if not text:
        return text
    kw = str(exempt_keyword or "").strip()
    kw_re = re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE) if kw else None
    kept = [line for line in text.split("\n")
            if not _BAIT_RE.search(line) or (kw_re is not None and kw_re.search(line))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
