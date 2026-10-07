"""Unit tests for the pure brand-kit parser and prompt describer."""

import pytest

from cqc_lem.utilities.brand_kit import (
    AVOID_MAX_ITEMS,
    FONT_VIBE_MAX,
    VISUAL_MOOD_MAX,
    BrandKit,
    color_name,
    describe_for_prompt,
    normalize_hex,
    parse_brand_kit,
)

pytestmark = pytest.mark.unit

_OWNER_KIT = {
    "primary_hex": "#e9d437",
    "secondary_hex": "#a89816",
    "neutral_dark_hex": "#1f1f1f",
    "neutral_light_hex": "#f7f5ef",
    "font_vibe": "clean geometric sans, bold headlines",
    "visual_mood": "practitioner field notes: real work, warm light, confident and specific, no hype",
    "avoid": ["pipes", "gears", "lightbulbs", "puzzle pieces", "robots", "glowing brains"],
}


class TestNormalizeHex:
    @pytest.mark.parametrize("raw,expected", [
        ("#E9D437", "#e9d437"),
        ("a89816", "#a89816"),
        ("  #1f1f1f ", "#1f1f1f"),
        ("#fff", None),
        ("#ggggggg", None),
        ("#12345g", None),
        (None, None),
        (123456, None),
    ])
    def test_only_six_digit_hex_survives(self, raw, expected):
        assert normalize_hex(raw) == expected


class TestParseBrandKit:
    def test_valid_kit_round_trips(self):
        kit = parse_brand_kit(_OWNER_KIT)
        assert kit is not None
        assert kit.to_dict() == _OWNER_KIT

    @pytest.mark.parametrize("raw", [None, "not a dict", ["#e9d437"], 42])
    def test_non_dict_is_none(self, raw):
        assert parse_brand_kit(raw) is None

    def test_invalid_fields_are_dropped_not_raised(self):
        kit = parse_brand_kit({
            "primary_hex": "#zzzzzz",
            "secondary_hex": "#A89816",
            "accent_hex": 7,
            "font_vibe": "x" * (FONT_VIBE_MAX + 1),
            "visual_mood": "   ",
            "avoid": "not, a, list",  # a CSV string is accepted and split
            "unknown_key": "ignored",
        })
        assert kit.to_dict() == {"secondary_hex": "#a89816", "avoid": ["not", "a", "list"]}

    def test_text_at_the_bound_is_kept_and_whitespace_collapsed(self):
        kit = parse_brand_kit({"font_vibe": "  bold \n sans ", "visual_mood": "m" * VISUAL_MOOD_MAX})
        assert kit.font_vibe == "bold sans"
        assert kit.visual_mood == "m" * VISUAL_MOOD_MAX

    def test_oversize_avoid_list_is_trimmed_and_deduplicated(self):
        items = ["Gears", "gears", "x" * 41, 5, ""] + [f"motif {i}" for i in range(20)]
        kit = parse_brand_kit({"avoid": items})
        assert len(kit.avoid) == AVOID_MAX_ITEMS
        assert kit.avoid[0] == "Gears"
        assert "gears" not in kit.avoid
        assert all(len(a) <= 40 for a in kit.avoid)

    def test_avoid_of_wrong_type_is_empty(self):
        assert parse_brand_kit({"avoid": {"a": 1}}).avoid == []

    def test_empty_dict_is_an_empty_kit(self):
        kit = parse_brand_kit({})
        assert kit is not None and kit.is_empty() and kit.to_dict() == {}


class TestColorName:
    def test_owner_palette_names(self):
        assert color_name("#e9d437") == "light gold"
        assert color_name("#a89816") == "dark gold"
        assert color_name("#1f1f1f") == "charcoal"
        assert color_name("#f7f5ef") == "off-white"

    def test_unparseable_is_blank(self):
        assert color_name("nope") == ""


class TestDescribeForPrompt:
    def test_full_kit_names_colors_and_keeps_hex(self):
        text = describe_for_prompt(parse_brand_kit(_OWNER_KIT))
        assert text.startswith(
            "Brand palette: light gold (#e9d437) and dark gold (#a89816) as accents against "
            "charcoal (#1f1f1f) and off-white (#f7f5ef); ")
        assert "typography feel: clean geometric sans, bold headlines" in text
        assert "mood: practitioner field notes" in text
        assert "avoid: pipes, gears, lightbulbs, puzzle pieces, robots, glowing brains" in text
        assert text.endswith(".")

    @pytest.mark.parametrize("kit", [None, BrandKit(), parse_brand_kit({"primary_hex": "bad"})])
    def test_empty_or_none_is_blank(self, kit):
        assert describe_for_prompt(kit) == ""

    def test_accents_only(self):
        assert describe_for_prompt(BrandKit(primary_hex="#e9d437")) == \
            "Brand palette: light gold (#e9d437)."

    def test_neutrals_only(self):
        assert describe_for_prompt(BrandKit(neutral_dark_hex="#000000")) == \
            "Brand palette: neutrals black (#000000)."

    def test_no_colors_capitalizes_first_clause(self):
        assert describe_for_prompt(BrandKit(visual_mood="calm")) == "Mood: calm."


class TestBrandClauseForUser:
    """The ONE resolver every image brief call site uses. Never raises; `""` means neutral."""

    def test_a_saved_kit_becomes_the_prompt_clause(self):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit", return_value=_OWNER_KIT) as read:
            clause = brand_clause_for_user(1)
        read.assert_called_once_with(1)
        assert clause == describe_for_prompt(parse_brand_kit(_OWNER_KIT))
        assert clause.startswith("Brand palette: light gold (#e9d437)")

    def test_no_kit_is_the_empty_clause(self):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit", return_value=None):
            assert brand_clause_for_user(1) == ""

    def test_a_read_that_raises_is_the_empty_clause(self):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit", side_effect=TypeError("int(None)")):
            assert brand_clause_for_user(1) == ""

    @pytest.mark.parametrize("user_id", [None, 0])
    def test_no_user_never_touches_the_db(self, user_id):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit") as read:
            assert brand_clause_for_user(user_id) == ""
        read.assert_not_called()


class TestCardShare:
    """The post treatment rotation's typeset-card share (#2241): validated, never a prompt."""

    @pytest.mark.parametrize("raw,expected", [(0.4, 0.4), (0, 0.0), (1, 1.0), ("0.25", 0.25),
                                              (0.333, 0.33), (" 1 ", 1.0)])
    def test_a_share_in_range_is_kept(self, raw, expected):
        assert parse_brand_kit({"card_share": raw}).card_share == expected

    @pytest.mark.parametrize("raw", [1.5, -0.1, True, "lots", None, [0.4], float("nan")])
    def test_anything_else_is_dropped_like_a_bad_hex(self, raw):
        assert parse_brand_kit({"card_share": raw}).card_share is None

    def test_a_kit_holding_only_a_share_is_kept_and_never_reaches_a_prompt(self):
        kit = parse_brand_kit({"card_share": 0.6})
        assert not kit.is_empty()
        assert kit.to_dict() == {"card_share": 0.6}
        assert describe_for_prompt(kit) == ""
        assert "card_share" not in BrandKit().to_dict()

    def test_the_resolver_reads_the_saved_share(self):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import card_share_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit", return_value={"card_share": 0.7}):
            assert card_share_for_user(1) == 0.7

    @pytest.mark.parametrize("stored", [None, {}, {"primary_hex": "#e9d437"}])
    def test_no_share_is_the_default(self, stored):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import DEFAULT_CARD_SHARE, card_share_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit", return_value=stored):
            assert card_share_for_user(1) == DEFAULT_CARD_SHARE == 0.4

    def test_a_failed_read_or_no_user_is_the_default(self):
        from unittest.mock import patch

        from cqc_lem.utilities.brand_kit import DEFAULT_CARD_SHARE, card_share_for_user
        with patch("cqc_lem.utilities.db.get_brand_kit", side_effect=TypeError("int(None)")):
            assert card_share_for_user(1) == DEFAULT_CARD_SHARE
        with patch("cqc_lem.utilities.db.get_brand_kit") as read:
            assert card_share_for_user(None) == DEFAULT_CARD_SHARE
        read.assert_not_called()
