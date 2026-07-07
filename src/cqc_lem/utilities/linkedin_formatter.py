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
    Em dashes become a spaced hyphen; emojis, accented letters and intentional bullets are preserved."""
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


# Drop-in directive for AI system prompts so models avoid the fancy punctuation in the first place
# (normalize_public_text is the safety net; this is the prevention).
PLAIN_PUNCTUATION_DIRECTIVE = (
    "PUNCTUATION: Use only plain ASCII punctuation. NEVER use em dashes or en dashes - use a comma, "
    "period, or a plain hyphen instead. Do NOT use curly/smart quotes (use straight ' and \") and do "
    "NOT use the ellipsis character - type three periods. Avoid any other non-standard Unicode "
    "punctuation; these read as AI-generated."
)


def sanitize_for_linkedin(text: str) -> str:
    """Strip markdown syntax from AI-generated text so it renders cleanly on LinkedIn.

    LinkedIn does not render standard markdown. This function removes formatting
    markers while preserving the underlying text, emojis, hashtags, and line breaks.
    Also normalizes rogue typographic characters (em dashes, smart quotes, ...) to plain ASCII.
    """
    if not text:
        return text

    text = normalize_public_text(text)

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

    return text.strip()


# Classic engagement-bait patterns LinkedIn's 2026 algorithm penalizes. Deliberately does NOT match
# generic "comment <keyword>" so it never strips a legitimate lead-magnet CTA (Phase 4).
_BAIT_PATTERNS = [
    r"tag (a friend|someone|three|3|your)",
    r"like if you",
    r"(hit|smash)\s+(the\s+)?like",
    r"double[- ]tap",
    r"(repost|share)\s+(this\s+)?if",
    r"comment\s+[\"']?(yes|agree|below|amen|me|👇)\b",
]
_BAIT_RE = re.compile("|".join(_BAIT_PATTERNS), re.IGNORECASE)


def is_bait_keyword(keyword: Optional[str]) -> bool:
    """True when 'comment <keyword>' would itself trip the bait filter (YES/AGREE/BELOW/AMEN/ME/👇).
    Such a lead-magnet trigger word can never reliably survive strip_engagement_bait, so it must be
    rejected at configuration time."""
    kw = str(keyword or "").strip()
    return bool(kw) and bool(_BAIT_RE.search(f"comment {kw}"))


def strip_engagement_bait(text: str, exempt_keyword: Optional[str] = None) -> str:
    """Drop lines containing classic engagement-bait CTAs (penalized). Conservative and line-level:
    bait is almost always its own CTA line, and we avoid touching 'comment <keyword>' lead magnets.
    `exempt_keyword` protects lines carrying the user's configured lead-magnet trigger word
    (whole-word, case-insensitive) when that keyword happens to collide with the bait regex."""
    if not text:
        return text
    kw = str(exempt_keyword or "").strip()
    kw_re = re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE) if kw else None
    kept = [line for line in text.split("\n")
            if not _BAIT_RE.search(line) or (kw_re is not None and kw_re.search(line))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
