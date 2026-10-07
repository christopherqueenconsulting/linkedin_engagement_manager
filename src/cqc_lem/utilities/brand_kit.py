"""The per-user brand kit image generation reads: palette, typography feel, mood, and what to avoid.

Pure — no I/O. Storage is `engagement_preferences.brand_kit` (JSON), read and written through
`cqc_lem.utilities.db.get_brand_kit` / `set_brand_kit`. Parsing is TOLERANT: a malformed field is
dropped, never raised, because a kit is steering, not a gate — an image renders with or without it.
An empty kit describes as `""`, which leaves the image engine on its neutral grading.

Image models follow colour NAMES better than hex codes, so `describe_for_prompt` names every colour
off a small nearest-named-colour table and keeps the hex beside it. `docs/image-stack.md`.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Optional

COLOR_FIELDS = ("primary_hex", "secondary_hex", "accent_hex", "neutral_dark_hex", "neutral_light_hex")
FONT_VIBE_MAX = 80
VISUAL_MOOD_MAX = 160
AVOID_MAX_ITEMS = 12
AVOID_ITEM_MAX = 40
FOUNDER_PHOTO_MAX = 300
# An owner-approved REAL photo of the founder, for a future quote-card archetype
# (docs/visual-archetypes-research.md §6.2 G). Parsed only — nothing renders it yet, and it never
# reaches a prompt.
_PHOTO_PATH = re.compile(r"^[\w\-./:?=&%]+\.(?:png|jpe?g|webp)$", re.IGNORECASE)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

# (name, rgb). Small on purpose: the point is a word an image model has seen thousands of times,
# not a precise swatch — the hex rides along for that.
_NAMED_COLORS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("black", (0, 0, 0)),
    ("charcoal", (40, 40, 40)),
    ("slate gray", (100, 110, 120)),
    ("silver gray", (190, 190, 190)),
    ("off-white", (246, 244, 238)),
    ("white", (255, 255, 255)),
    ("navy", (20, 35, 90)),
    ("royal blue", (50, 90, 200)),
    ("sky blue", (130, 190, 235)),
    ("teal", (0, 128, 128)),
    ("forest green", (35, 100, 50)),
    ("sage green", (150, 175, 140)),
    ("lime green", (140, 210, 60)),
    ("light gold", (233, 212, 70)),
    ("dark gold", (168, 150, 30)),
    ("mustard yellow", (215, 175, 40)),
    ("orange", (240, 140, 30)),
    ("terracotta", (200, 95, 65)),
    ("crimson", (190, 25, 45)),
    ("burgundy", (110, 20, 40)),
    ("pink", (240, 150, 180)),
    ("purple", (110, 50, 150)),
    ("brown", (110, 75, 45)),
    ("beige", (220, 200, 165)),
)


@dataclass
class BrandKit:
    """A validated brand kit. Every field is optional; an all-empty kit is `is_empty()`.

    Attributes:
        primary_hex: Main brand colour, `#rrggbb` lowercase.
        secondary_hex: Second brand colour, `#rrggbb`.
        accent_hex: Accent colour, `#rrggbb`.
        neutral_dark_hex: The dark neutral (backgrounds, type), `#rrggbb`.
        neutral_light_hex: The light neutral, `#rrggbb`.
        font_vibe: Typography feel in words, at most 80 characters.
        visual_mood: The imagery mood in words, at most 160 characters.
        avoid: Motifs to keep out of a render — at most 12, each at most 40 characters.
        founder_photo: The owner-approved real founder photo — an assets path or URL to a PNG,
            JPEG or WebP, at most 300 characters. Parsed only; no archetype draws it yet.
    """

    primary_hex: Optional[str] = None
    secondary_hex: Optional[str] = None
    accent_hex: Optional[str] = None
    neutral_dark_hex: Optional[str] = None
    neutral_light_hex: Optional[str] = None
    font_vibe: Optional[str] = None
    visual_mood: Optional[str] = None
    avoid: list[str] = field(default_factory=list)
    founder_photo: Optional[str] = None

    def is_empty(self) -> bool:
        """Whether the kit carries nothing at all.

        Returns:
            True when every colour, text field and the avoid list are unset.
        """
        return not any(getattr(self, f) for f in COLOR_FIELDS) and not (
            self.font_vibe or self.visual_mood or self.avoid or self.founder_photo)

    def to_dict(self) -> dict[str, Any]:
        """The kit as a JSON-ready dict, set fields only.

        Returns:
            Only the populated fields, so a stored kit never grows null keys.
        """
        out: dict[str, Any] = {f: getattr(self, f) for f in COLOR_FIELDS if getattr(self, f)}
        if self.font_vibe:
            out["font_vibe"] = self.font_vibe
        if self.visual_mood:
            out["visual_mood"] = self.visual_mood
        if self.avoid:
            out["avoid"] = list(self.avoid)
        if self.founder_photo:
            out["founder_photo"] = self.founder_photo
        return out


def normalize_hex(value: Any) -> Optional[str]:
    """A `#rrggbb` colour, lowercased — or None for anything else.

    Args:
        value: Candidate colour; a leading `#` is optional.

    Returns:
        The normalized hex, or None when it is not six hex digits.
    """
    if not isinstance(value, str):
        return None
    m = _HEX_RE.match(value.strip())
    return f"#{m.group(1).lower()}" if m else None


def _clean_text(value: Any, max_len: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if 0 < len(text) <= max_len else None


def _clean_avoid(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_text(item, AVOID_ITEM_MAX)
        if text and text.lower() not in (o.lower() for o in out):
            out.append(text)
        if len(out) == AVOID_MAX_ITEMS:
            break
    return out


def _clean_photo(value: Any) -> Optional[str]:
    """A founder photo reference: one image path or URL, no traversal; else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > FOUNDER_PHOTO_MAX or ".." in text or not _PHOTO_PATH.match(text):
        return None
    return text


def parse_brand_kit(raw: Any) -> Optional[BrandKit]:
    """Build a `BrandKit` from untrusted input, dropping whatever is invalid. Never raises.

    Args:
        raw: A dict as stored or as sent by the SPA. Anything that is not a dict is None.

    Returns:
        The kit (possibly empty) for a dict, else None. An over-long text field or a bad hex is
        dropped; an avoid list is trimmed to its first 12 valid, de-duplicated items.
    """
    if not isinstance(raw, dict):
        return None
    kit = BrandKit()
    for f in COLOR_FIELDS:
        setattr(kit, f, normalize_hex(raw.get(f)))
    kit.font_vibe = _clean_text(raw.get("font_vibe"), FONT_VIBE_MAX)
    kit.visual_mood = _clean_text(raw.get("visual_mood"), VISUAL_MOOD_MAX)
    kit.avoid = _clean_avoid(raw.get("avoid"))
    kit.founder_photo = _clean_photo(raw.get("founder_photo"))
    return kit


def color_name(hex_value: str) -> str:
    """The nearest named colour to a hex, by squared RGB distance.

    Args:
        hex_value: A colour accepted by `normalize_hex`.

    Returns:
        The table name, or `""` when the hex does not parse.
    """
    norm = normalize_hex(hex_value)
    if not norm:
        return ""
    r, g, b = (int(norm[i:i + 2], 16) for i in (1, 3, 5))
    return min(_NAMED_COLORS,
               key=lambda nc: (nc[1][0] - r) ** 2 + (nc[1][1] - g) ** 2 + (nc[1][2] - b) ** 2)[0]


def _named(hex_value: str) -> str:
    return f"{color_name(hex_value)} ({hex_value})"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def describe_for_prompt(kit: Optional[BrandKit]) -> str:
    """One natural-language clause an image prompt can carry.

    Args:
        kit: The parsed kit, or None.

    Returns:
        e.g. "Brand palette: light gold (#e9d437) and dark gold (#a89816) as accents against
        charcoal (#1f1f1f) and off-white (#f7f5ef); typography feel: …; mood: …; avoid: …."
        `""` for a None or empty kit, so the caller falls back to neutral grading.
    """
    if kit is None or kit.is_empty():
        return ""
    parts: list[str] = []
    accents = [_named(h) for h in (kit.primary_hex, kit.secondary_hex, kit.accent_hex) if h]
    neutrals = [_named(h) for h in (kit.neutral_dark_hex, kit.neutral_light_hex) if h]
    if accents and neutrals:
        parts.append(f"Brand palette: {_join(accents)} as accents against {_join(neutrals)}")
    elif accents:
        parts.append(f"Brand palette: {_join(accents)}")
    elif neutrals:
        parts.append(f"Brand palette: neutrals {_join(neutrals)}")
    if kit.font_vibe:
        parts.append(f"typography feel: {kit.font_vibe}")
    if kit.visual_mood:
        parts.append(f"mood: {kit.visual_mood}")
    if kit.avoid:
        parts.append(f"avoid: {', '.join(kit.avoid)}")
    if not parts:
        return ""  # a kit holding only a founder photo has nothing a prompt may carry
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def brand_clause_for_user(user_id: Optional[int]) -> str:
    """The ONE resolver from a user to the brand clause every image brief carries. Never raises.

    Lazy-imports the db facade so this module stays pure for its other callers. Every unhappy path
    — no user, no kit, an unreadable kit, a DB fault — is the same answer as an empty kit: `""`,
    which leaves the brief on its neutral grading. That is the expected state for most users, so it
    logs at DEBUG.

    Args:
        user_id: The author whose kit to read; None for a surface with no user.

    Returns:
        `describe_for_prompt` of the saved kit, or `""`.
    """
    if not user_id:
        return ""
    try:
        from cqc_lem.utilities.db import get_brand_kit
        clause = describe_for_prompt(parse_brand_kit(get_brand_kit(user_id)))
    except Exception as e:
        from cqc_lem.utilities.logger import log_debug
        log_debug("Brand kit unreadable — briefing without it", error=str(e), user_id=user_id,
                  action_type="brand_kit")
        return ""
    if not clause:
        from cqc_lem.utilities.logger import log_debug
        log_debug("No brand kit — briefing on neutral grading", user_id=user_id,
                  action_type="brand_kit")
    return clause
