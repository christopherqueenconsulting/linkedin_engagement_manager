"""The per-user brand kit image generation reads: palette, typography feel, mood, and what to avoid.

Pure — no I/O. Storage is `engagement_preferences.brand_kit` (JSON), read and written through
`cqc_lem.utilities.db.get_brand_kit` / `set_brand_kit`. Parsing is TOLERANT: a malformed field is
dropped, never raised, because a kit is steering, not a gate — an image renders with or without it.
An empty kit describes as `""`, which leaves the image engine on its neutral grading.

Image models follow colour NAMES better than hex codes, so `describe_for_prompt` names every colour
off a small nearest-named-colour table and keeps the hex beside it. `docs/image-stack.md`.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

COLOR_FIELDS = ("primary_hex", "secondary_hex", "accent_hex", "neutral_dark_hex", "neutral_light_hex")
FONT_VIBE_MAX = 80
VISUAL_MOOD_MAX = 160
AVOID_MAX_ITEMS = 12
AVOID_ITEM_MAX = 40
FOUNDER_PHOTO_MAX = 300
# The share of POST images that get a typeset card (``post_treatment``) — a float in [0, 1]. Unset
# means the default; a value outside the range, a bool or a non-number is dropped like a bad hex.
DEFAULT_CARD_SHARE = 0.4
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
        card_share: The share of post images that get a typeset card, 0-1; None means
            ``DEFAULT_CARD_SHARE``. Steers the post treatment rotation, never a prompt.
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
    card_share: Optional[float] = None

    def is_empty(self) -> bool:
        """Whether the kit carries nothing at all.

        Returns:
            True when every colour, text field, the avoid list and the card share are unset.
        """
        return not any(getattr(self, f) for f in COLOR_FIELDS) and not (
            self.font_vibe or self.visual_mood or self.avoid or self.founder_photo
            or self.card_share is not None)

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
        if self.card_share is not None:
            out["card_share"] = self.card_share
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


def _clean_share(value: Any) -> Optional[float]:
    """A card share in [0, 1] rounded to 2 places — from a number or a numeric string; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or math.isnan(value) or not 0 <= value <= 1:
        return None
    return round(float(value), 2)


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
    kit.card_share = _clean_share(raw.get("card_share"))
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


# ── The deck theme: every carousel template is skinned from this ─────────────────────────────
# Decks used to ship a navy/blue/green template palette in a system font, whatever the brand
# (#2241 showcase). A template now keeps only its LAYOUT; every colour comes from here.
# The neutral default is a kit-less author's deck: the same charcoal/off-white grounds with slate
# accents, so no unbranded deck borrows another author's gold.
NEUTRAL_DARK = "#1f1f1f"
NEUTRAL_LIGHT = "#f7f5ef"
NEUTRAL_ACCENT = "#94a3b8"
NEUTRAL_ACCENT_DEEP = "#475569"
# WCAG 2.x AA for body-size text.
MIN_TEXT_CONTRAST = 4.5


def hex_to_rgb(hex_value: str) -> tuple[int, int, int]:
    """``#rrggbb`` → ``(r, g, b)``.

    Args:
        hex_value: A colour accepted by ``normalize_hex``.

    Returns:
        The channels.

    Raises:
        ValueError: The value is not a six-digit hex colour.
    """
    norm = normalize_hex(hex_value)
    if not norm:
        raise ValueError(f"not a hex colour: {hex_value!r}")
    return int(norm[1:3], 16), int(norm[3:5], 16), int(norm[5:7], 16)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """The WCAG contrast ratio of two colours, 1.0 (none) to 21.0 (black on white).

    Args:
        fg: Foreground RGB.
        bg: Background RGB.

    Returns:
        The ratio.
    """
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """``a`` moved a fraction ``t`` of the way to ``b``.

    Args:
        a: Start RGB.
        b: End RGB.
        t: 0 keeps ``a``, 1 is ``b``.

    Returns:
        The blended RGB.
    """
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def readable_on(fg: tuple[int, int, int], bg: tuple[int, int, int],
                inks: tuple[tuple[int, int, int], ...],
                minimum: float = MIN_TEXT_CONTRAST) -> tuple[int, int, int]:
    """``fg`` when it already reads on ``bg``; else ``fg`` pushed toward the better ink until it does.

    Deterministic: ten steps of 10% toward whichever of ``inks`` contrasts more with ``bg``, and
    that ink itself when nothing short of it passes. A brand colour is kept wherever it is legible.

    Args:
        fg: The wanted text colour.
        bg: What it is drawn on.
        inks: The theme's dark and light inks.
        minimum: The contrast floor.

    Returns:
        A colour with at least ``minimum`` contrast on ``bg`` (or the best ink, if none reaches it).
    """
    if contrast_ratio(fg, bg) >= minimum:
        return fg
    ink = max(inks, key=lambda i: contrast_ratio(i, bg))
    for step in range(1, 11):
        candidate = mix(fg, ink, step / 10)
        if contrast_ratio(candidate, bg) >= minimum:
            return candidate
    return ink


@dataclass(frozen=True)
class DeckTheme:
    """The colours and type every carousel template is skinned with.

    Attributes:
        dark: The dark ground (cover/CTA backgrounds, bars), RGB.
        light: The light ground (content slides), RGB.
        accent: The bright accent — light gold for the reference brand — RGB.
        accent_deep: The deep accent — dark gold — RGB.
        ink_dark: Text on a light ground, RGB.
        ink_light: Text on a dark ground, RGB.
        branded: Whether the colours came from a saved kit (False = the neutral default).
    """

    dark: tuple
    light: tuple
    accent: tuple
    accent_deep: tuple
    ink_dark: tuple
    ink_light: tuple
    branded: bool = False

    @property
    def inks(self) -> tuple:
        """The two inks text may fall back to."""
        return self.ink_dark, self.ink_light

    def ink_on(self, bg: tuple) -> tuple:
        """Whichever ink reads better on ``bg``.

        Args:
            bg: The fill text sits on.

        Returns:
            ``ink_dark`` or ``ink_light``.
        """
        return max(self.inks, key=lambda ink: contrast_ratio(ink, bg))


def deck_theme(kit: Optional[BrandKit]) -> DeckTheme:
    """The deck theme for a kit — the neutral default for None or an empty kit.

    The kit's primary is the bright accent and its secondary (else its accent) the deep one; its
    neutrals are the grounds. A kit missing a field takes the neutral default's for that field.
    Inks are the grounds themselves, so body text is charcoal on off-white and off-white on
    charcoal for the reference brand.

    Args:
        kit: The parsed kit, or None.

    Returns:
        The theme.
    """
    has_kit = kit is not None and any(getattr(kit, f) for f in COLOR_FIELDS)

    def pick(value: Optional[str], default: str) -> tuple[int, int, int]:
        return hex_to_rgb(value or default)

    dark = pick(kit.neutral_dark_hex if has_kit else None, NEUTRAL_DARK)
    light = pick(kit.neutral_light_hex if has_kit else None, NEUTRAL_LIGHT)
    if has_kit and (kit.primary_hex or kit.accent_hex):
        accent = hex_to_rgb(kit.primary_hex or kit.accent_hex)
        deep_hex = kit.secondary_hex or (kit.accent_hex if kit.primary_hex else None)
        accent_deep = hex_to_rgb(deep_hex) if deep_hex else mix(accent, dark, 0.35)
    else:
        accent, accent_deep = hex_to_rgb(NEUTRAL_ACCENT), hex_to_rgb(NEUTRAL_ACCENT_DEEP)
    if contrast_ratio(dark, light) < MIN_TEXT_CONTRAST:
        # A kit whose two neutrals do not contrast cannot carry text either way round.
        dark, light = hex_to_rgb(NEUTRAL_DARK), hex_to_rgb(NEUTRAL_LIGHT)
    return DeckTheme(dark=dark, light=light, accent=accent, accent_deep=accent_deep,
                     ink_dark=dark, ink_light=light, branded=has_kit)


def brand_kit_for_user(user_id: Optional[int]) -> Optional[BrandKit]:
    """The user's parsed kit, or None. Never raises — a missing kit is the common case (DEBUG).

    Args:
        user_id: The author; None for a surface with no user.

    Returns:
        The kit, or None when there is no user, no kit, or it cannot be read.
    """
    if not user_id:
        return None
    try:
        from cqc_lem.utilities.db import get_brand_kit
        return parse_brand_kit(get_brand_kit(user_id))
    except Exception as e:
        from cqc_lem.utilities.logger import log_debug
        log_debug("Brand kit unreadable — theming the deck on the neutral default",
                  error=str(e), user_id=user_id, action_type="brand_kit")
        return None


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


def card_share_for_user(user_id: Optional[int]) -> float:
    """The user's post card share, else ``DEFAULT_CARD_SHARE``. Never raises.

    Args:
        user_id: The author; None reads as the default.

    Returns:
        A float in [0, 1].
    """
    if not user_id:
        return DEFAULT_CARD_SHARE
    try:
        from cqc_lem.utilities.db import get_brand_kit
        kit = parse_brand_kit(get_brand_kit(user_id))
    except Exception as e:
        from cqc_lem.utilities.logger import log_debug
        log_debug("Brand kit unreadable — default card share", error=str(e), user_id=user_id,
                  action_type="brand_kit")
        return DEFAULT_CARD_SHARE
    if kit is None or kit.card_share is None:
        return DEFAULT_CARD_SHARE
    return kit.card_share
