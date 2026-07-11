"""Unit tests for LinkedIn text formatter utility."""

import pytest
from cqc_lem.utilities.ai.content_alignment import LEAD_MAGNET_CTA_REPAIR_MENU
from cqc_lem.utilities.linkedin_formatter import (
    sanitize_for_linkedin, normalize_public_text, PLAIN_PUNCTUATION_DIRECTIVE,
    strip_engagement_bait, is_bait_keyword)


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

    def test_exempt_keyword_protects_colliding_cta_line(self):
        # A configured trigger word that collides with the bait regex (e.g. 'YES') must survive
        # when threaded through as exempt_keyword.
        text = "Real value here.\n\nComment YES and I'll DM you the checklist."
        assert "Comment YES" not in strip_engagement_bait(text)  # collides by default
        out = strip_engagement_bait(text, exempt_keyword="YES")
        assert "Comment YES and I'll DM you the checklist." in out

    def test_exempt_keyword_is_whole_word(self):
        # 'ME' as the exempt keyword must not shield unrelated bait lines containing 'me' inside
        # other words.
        text = "Insight.\n\nTag a friend who needs this moment."
        out = strip_engagement_bait(text, exempt_keyword="ME")
        assert "Tag a friend" not in out

    def test_exempt_keyword_none_keeps_default_behavior(self):
        text = "Insight.\n\nComment YES below if you want more."
        assert strip_engagement_bait(text, exempt_keyword=None) == "Insight."

    @pytest.mark.parametrize("kw,expected", [
        ("YES", True), ("agree", True), ("BELOW", True), ("AMEN", True), ("ME", True),
        ("AUDIT", False), ("GUIDE", False), ("SCALE", False), ("", False), (None, False),
    ])
    def test_is_bait_keyword(self, kw, expected):
        assert is_bait_keyword(kw) is expected

    @pytest.mark.parametrize("resource", ["a free 12-point LinkedIn profile audit PDF", "the resource"])
    @pytest.mark.parametrize("template", LEAD_MAGNET_CTA_REPAIR_MENU)
    def test_preserves_every_repair_menu_variant(self, template, resource):
        # The verify-and-repair menu (content_alignment.LEAD_MAGNET_CTA_REPAIR_MENU) must never be
        # eaten by the bait filter or mangled by markdown sanitization — otherwise the repair would
        # be undone the next time the content flows through the formatters.
        variant = template.format(keyword="AUDIT", resource=resource)
        text = f"Here is a real insight.\n\n{variant}"
        assert variant in strip_engagement_bait(text)
        assert variant in sanitize_for_linkedin(text)


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


@pytest.mark.unit
class TestEnforcePostReadability:
    def test_reflows_wall_of_text_into_paragraphs(self):
        from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
        wall = " ".join(f"Sentence number {i} is here." for i in range(60))  # long, no blank lines
        assert "\n\n" not in wall and len(wall) > 600
        out = enforce_post_readability(wall, max_chars=5000)
        assert out.count("\n\n") >= 3  # broke into multiple paragraphs

    def test_hard_caps_length_at_sentence_boundary(self):
        from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
        text = " ".join(f"Sentence {i}." for i in range(300))
        out = enforce_post_readability(text, max_chars=200)
        assert len(out) <= 200
        assert out.rstrip().endswith(".")  # trimmed at a sentence end, not mid-word

    def test_short_post_unchanged(self):
        from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
        s = "A short and sweet post."
        assert enforce_post_readability(s) == s

    def test_already_paragraphed_post_unchanged(self):
        from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
        s = "Hook line here.\n\n" + ("Body sentence. " * 40).strip()
        assert enforce_post_readability(s, max_chars=5000) == s  # has blank lines → not reflowed

    def test_keeps_trailing_hashtag_line_separate(self):
        from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
        body = " ".join(f"Point {i} made here." for i in range(50))
        text = body + "\n#ai #linkedin #ml"
        out = enforce_post_readability(text, max_chars=5000)
        assert out.rstrip().endswith("#ai #linkedin #ml")

    def test_empty_and_none(self):
        from cqc_lem.utilities.linkedin_formatter import enforce_post_readability
        assert enforce_post_readability("") == ""
        assert enforce_post_readability(None) is None


@pytest.mark.unit
class TestLinkedinPostFormatDirective:
    def test_states_hook_paragraphs_and_char_cap(self):
        from cqc_lem.utilities.linkedin_formatter import linkedin_post_format_directive
        d = linkedin_post_format_directive(2200)
        assert "hook" in d.lower()
        assert "2200" in d
        assert "blank line" in d.lower()
        assert "markdown" in d.lower()
