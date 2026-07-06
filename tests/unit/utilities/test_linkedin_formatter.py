"""Unit tests for LinkedIn text formatter utility."""

import pytest
from cqc_lem.utilities.linkedin_formatter import (
    sanitize_for_linkedin, normalize_public_text, PLAIN_PUNCTUATION_DIRECTIVE,
    strip_engagement_bait)


@pytest.mark.unit
class TestStripEngagementBait:
    @pytest.mark.parametrize("bait", [
        "Tag a friend who needs this.",
        "Like if you agree!",
        "Comment YES below if you want more.",
        "Repost this if it helped.",
    ])
    def test_strips_classic_bait_lines(self, bait):
        text = f"Here is a real insight.\n\n{bait}"
        out = strip_engagement_bait(text)
        assert "real insight" in out
        assert bait not in out

    def test_preserves_lead_magnet_cta(self):
        # A sanctioned lead-magnet CTA ("comment KEYWORD") must survive the bait filter.
        text = ("Cutting acquisition cost took us six months of testing.\n\n"
                "Comment AUDIT and I'll DM you the 12-point checklist we used.")
        out = strip_engagement_bait(text)
        assert "Comment AUDIT" in out
        assert "12-point checklist" in out

    def test_preserves_lead_magnet_cta_lowercase(self):
        text = "Real value here.\n\ncomment SCALE and I'll send over the playbook."
        out = strip_engagement_bait(text)
        assert "comment SCALE" in out


@pytest.mark.unit
class TestNormalizePublicText:
    def test_empty_and_none(self):
        assert normalize_public_text("") == ""
        assert normalize_public_text(None) is None

    def test_em_dash_becomes_spaced_hyphen(self):
        assert normalize_public_text("The result—surprising—was clear") == "The result - surprising - was clear"
        assert normalize_public_text("A — B") == "A - B"

    def test_en_dash_ranges_become_hyphen(self):
        assert normalize_public_text("pages 5–10") == "pages 5-10"

    def test_smart_quotes_become_straight(self):
        assert normalize_public_text("“Hello” it’s ‘here’") == "\"Hello\" it's 'here'"

    def test_ellipsis_becomes_three_dots(self):
        assert normalize_public_text("Wait… really") == "Wait... really"

    def test_zero_width_chars_stripped(self):
        assert normalize_public_text("in​vis﻿ible") == "invisible"

    def test_replacement_char_removed(self):
        assert normalize_public_text("bad�char") == "badchar"

    def test_preserves_emoji_accents_and_bullets(self):
        # Intentional characters must survive: emoji, accented letters, and the LinkedIn bullet.
        assert normalize_public_text("Café \U0001F680 • bullet") == "Café \U0001F680 • bullet"

    def test_preserves_newlines(self):
        assert normalize_public_text("line1\n\nline2") == "line1\n\nline2"

    def test_directive_is_ascii_and_nonempty(self):
        assert PLAIN_PUNCTUATION_DIRECTIVE
        assert PLAIN_PUNCTUATION_DIRECTIVE.isascii()


@pytest.mark.unit
class TestSanitizeNormalizesTypography:
    def test_sanitize_strips_markdown_and_typography(self):
        out = sanitize_for_linkedin("**Bold** text—with em dash and “quotes”")
        assert out == "Bold text - with em dash and \"quotes\""
        assert "—" not in out and "“" not in out


@pytest.mark.unit
class TestSanitizeForLinkedIn:

    def test_passes_clean_text_unchanged(self):
        text = "This is a clean LinkedIn post.\n\nWith two paragraphs.\n\n#hashtag #linkedin"
        assert sanitize_for_linkedin(text) == text

    def test_strips_bold_double_asterisks(self):
        assert sanitize_for_linkedin("This is **bold** text.") == "This is bold text."

    def test_strips_bold_double_underscores(self):
        assert sanitize_for_linkedin("This is __bold__ text.") == "This is bold text."

    def test_strips_italic_single_asterisk(self):
        assert sanitize_for_linkedin("This is *italic* text.") == "This is italic text."

    def test_strips_italic_single_underscore(self):
        assert sanitize_for_linkedin("Hello _world_.") == "Hello world."

    def test_converts_markdown_h1_header(self):
        result = sanitize_for_linkedin("# My Heading\n\nSome body text.")
        assert "# My Heading" not in result
        assert "My Heading" in result

    def test_converts_markdown_h2_header(self):
        result = sanitize_for_linkedin("## Section Title\n\nContent here.")
        assert "## Section Title" not in result
        assert "Section Title" in result

    def test_converts_markdown_h3_through_h6(self):
        for n in range(3, 7):
            prefix = "#" * n
            result = sanitize_for_linkedin(f"{prefix} Header {n}\n\nBody.")
            assert f"{prefix} Header" not in result
            assert f"Header {n}" in result

    def test_converts_markdown_link_to_text_and_url(self):
        result = sanitize_for_linkedin("Visit [Google](https://www.google.com) for more.")
        assert "[Google]" not in result
        assert "Google (https://www.google.com)" in result

    def test_strips_inline_code_backticks(self):
        result = sanitize_for_linkedin("Use `pip install` to install.")
        assert "`" not in result
        assert "pip install" in result

    def test_removes_horizontal_rule_dashes(self):
        result = sanitize_for_linkedin("Above\n\n---\n\nBelow")
        assert "---" not in result

    def test_removes_horizontal_rule_asterisks(self):
        result = sanitize_for_linkedin("Above\n\n***\n\nBelow")
        assert "***" not in result

    def test_converts_dash_bullets_to_unicode(self):
        result = sanitize_for_linkedin("- First item\n- Second item")
        assert "- First" not in result
        assert "• First item" in result
        assert "• Second item" in result

    def test_converts_asterisk_bullets_to_unicode(self):
        result = sanitize_for_linkedin("* First item\n* Second item")
        assert "* First" not in result
        assert "• First item" in result

    def test_normalizes_excessive_blank_lines(self):
        result = sanitize_for_linkedin("Para 1\n\n\n\n\nPara 2")
        assert "\n\n\n" not in result

    def test_strips_trailing_whitespace_per_line(self):
        result = sanitize_for_linkedin("Line one   \nLine two  ")
        lines = result.split("\n")
        for line in lines:
            assert line == line.rstrip()

    def test_preserves_emojis(self):
        text = "Great insight! 🔑 Keep going 👉 #ai"
        result = sanitize_for_linkedin(text)
        assert "🔑" in result
        assert "👉" in result

    def test_preserves_hashtags(self):
        result = sanitize_for_linkedin("Post text\n\n#linkedin #ai #growth")
        assert "#linkedin" in result
        assert "#ai" in result

    def test_handles_empty_string(self):
        assert sanitize_for_linkedin("") == ""

    def test_handles_none_gracefully(self):
        assert sanitize_for_linkedin(None) is None

    def test_combined_markdown_cleanup(self):
        messy = (
            "## My Big Title\n\n"
            "Here is **bold** and *italic* content.\n\n"
            "---\n\n"
            "- Bullet one\n"
            "- Bullet two\n\n"
            "Check out [this link](https://example.com).\n\n"
            "#ai #linkedin"
        )
        result = sanitize_for_linkedin(messy)
        assert "##" not in result
        assert "**" not in result
        assert "*italic*" not in result
        assert "---" not in result
        assert "• Bullet one" in result
        assert "this link (https://example.com)" in result
        assert "#ai" in result
