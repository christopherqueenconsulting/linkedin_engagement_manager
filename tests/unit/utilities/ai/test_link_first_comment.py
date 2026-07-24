"""Unit tests for the link-in-first-comment split/deliver core (issue #392 — C3)."""

import pytest

from cqc_lem.utilities.ai.content_alignment import (
    FIRST_COMMENT_LINK_MENU, append_link_to_comment, extract_external_links,
    first_comment_link_text, is_external_link, split_link_for_first_comment,
)

pytestmark = pytest.mark.unit


class TestExternalLinkDetection:
    @pytest.mark.parametrize("url", [
        "https://example.com/post", "http://blog.example.io/a?b=1", "www.example.com/x",
    ])
    def test_external(self, url):
        assert is_external_link(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/feed/update/urn:li:ugcPost:1/",
        "https://linkedin.com/in/someone", "https://lnkd.in/abc123", "",
    ])
    def test_not_external(self, url):
        assert is_external_link(url) is False

    def test_lookalike_host_is_still_external(self):
        # notlinkedin.com must not be treated as LinkedIn's own domain.
        assert is_external_link("https://notlinkedin.com/x") is True

    def test_extracts_in_order_without_duplicates(self):
        content = ("Read https://example.com/a and then https://example.com/b. "
                   "Again: https://example.com/a")
        assert extract_external_links(content) == ["https://example.com/a", "https://example.com/b"]

    def test_trailing_sentence_punctuation_is_not_part_of_the_link(self):
        assert extract_external_links("See https://example.com/a.") == ["https://example.com/a"]
        assert extract_external_links("(https://example.com/a)") == ["https://example.com/a"]


class TestSplitLinkForFirstComment:
    def test_link_moves_out_of_the_body(self):
        body, links = split_link_for_first_comment(
            "Three lessons from the rebuild.\n\nFull breakdown: https://example.com/rebuild\n\n#growth")
        assert links == ["https://example.com/rebuild"]
        assert "https://" not in body
        assert "Full breakdown" in body and "#growth" in body

    def test_dangling_connector_is_tidied(self):
        body, _ = split_link_for_first_comment("I wrote it up here: https://example.com/a")
        assert body == "I wrote it up here"

    def test_mid_sentence_link_leaves_clean_punctuation(self):
        body, _ = split_link_for_first_comment(
            "I wrote about it (https://example.com/a). It changed how we hire.")
        assert body == "I wrote about it. It changed how we hire."

    def test_line_that_only_held_the_link_disappears(self):
        body, links = split_link_for_first_comment("Body line.\n\nhttps://example.com/a\n\nClosing line.")
        assert links == ["https://example.com/a"]
        assert body == "Body line.\n\nClosing line."

    def test_disabled_returns_content_untouched(self):
        content = "Read https://example.com/a"
        assert split_link_for_first_comment(content, enabled=False) == (content, [])

    def test_linkedin_links_stay_in_the_body(self):
        content = "My last post: https://www.linkedin.com/feed/update/urn:li:ugcPost:1/"
        assert split_link_for_first_comment(content) == (content, [])

    def test_no_links_returns_content_unchanged(self):
        content = "No links here at all."
        assert split_link_for_first_comment(content) == (content, [])

    def test_empty_content_is_safe(self):
        assert split_link_for_first_comment(None) == (None, [])

    def test_over_budget_links_stay_in_the_body(self):
        """A link that will not be carried must NOT be stripped — losing it entirely is worse
        than paying the body-link penalty for it."""
        content = "a https://example.com/1 b https://example.com/2 c https://example.com/3 d https://example.com/4"
        body, links = split_link_for_first_comment(content, max_links=2)
        assert links == ["https://example.com/1", "https://example.com/2"]
        assert "https://example.com/3" in body and "https://example.com/4" in body

    def test_char_budget_caps_what_is_carried(self):
        content = "x https://example.com/aaaaaaaaaa y https://example.com/bbbbbbbbbb"
        body, links = split_link_for_first_comment(content, max_chars=30)
        assert links == ["https://example.com/aaaaaaaaaa"]
        assert "https://example.com/bbbbbbbbbb" in body

    def test_prefix_link_is_not_corrupted_by_a_longer_sibling(self):
        content = "one https://example.com/a two https://example.com/a/deeper"
        body, links = split_link_for_first_comment(content)
        assert set(links) == {"https://example.com/a", "https://example.com/a/deeper"}
        assert "example.com" not in body


class TestFirstCommentDelivery:
    def test_link_text_rotates_by_post_id(self):
        variants = {first_comment_link_text(["https://example.com/a"], post_id=i)
                    for i in range(len(FIRST_COMMENT_LINK_MENU))}
        assert len(variants) == len(FIRST_COMMENT_LINK_MENU)

    def test_extra_links_are_listed_after_the_lead_line(self):
        text = first_comment_link_text(["https://example.com/a", "https://example.com/b"], post_id=0)
        assert text.endswith("https://example.com/b")
        assert "https://example.com/a" in text.split("\n")[0]

    def test_appends_to_the_generated_seed_comment(self):
        out = append_link_to_comment("What surprised you most?", ["https://example.com/a"], post_id=1)
        assert out.startswith("What surprised you most?")
        assert "https://example.com/a" in out

    def test_link_only_comment_when_generation_returned_nothing(self):
        out = append_link_to_comment(None, ["https://example.com/a"], post_id=1)
        assert "https://example.com/a" in out

    def test_no_links_leaves_the_comment_alone(self):
        assert append_link_to_comment("Just a question?", []) == "Just a question?"
        assert append_link_to_comment(None, []) == ""
