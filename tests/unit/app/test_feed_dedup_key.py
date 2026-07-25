"""Unit tests for the stable feed dedup key + normalization — issues #474 and #580.

The bug: the dedup key was a hash of the post's rendered text, so a "…see more" toggle or our
own just-posted comment mutated the text and produced a NEW key for the SAME post, defeating
every dedup layer and posting a second comment. These lock in URN-first, render-stable keys.

#580 (recurrence): on the live feed EVERY comment still fell back to the content hash, because the
URN lives on an ancestor of the card `_card_for_textbox` returns — the outerHTML scan never saw it —
and the hash covered the whole body, which differs between the truncated and the expanded render.
"""

import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.unit


def _fns():
    # Lazy import: importing run_automation at module scope instantiates the OpenAI client at
    # collection time, which fails in CI (no OPENAI_API_KEY).
    from cqc_lem.app.run_automation import (
        _normalize_post_text, _feed_post_key, _feed_post_urn_from_card,
        _stable_feed_post_key, _feed_content_fingerprints,
    )
    return (_normalize_post_text, _feed_post_key, _feed_post_urn_from_card,
            _stable_feed_post_key, _feed_content_fingerprints)


def _card(outer_html=None, permalink_href=None, scan_result=None, scan_raises=False):
    """A feed card. `scan_result` is what the ancestor/attribute URN scan (a JS call through the
    driver) returns — MagicMock's default is a non-str sentinel, i.e. 'scan found nothing'."""
    card = MagicMock()
    card.get_attribute.return_value = outer_html or ""
    if scan_raises:
        card.parent.execute_script.side_effect = RuntimeError("stale element")
    elif scan_result is not None:
        card.parent.execute_script.return_value = scan_result
    if permalink_href is not None:
        anchor = MagicMock()
        anchor.get_attribute.return_value = permalink_href
        card.find_elements.return_value = [anchor]
    else:
        card.find_elements.return_value = []
    return card


class TestNormalizePostText:
    def test_strips_see_more_and_collapses_whitespace(self):
        norm = _fns()[0]
        a = norm("Great insight here.\n\n…see more")
        b = norm("Great   insight here.")
        assert a == b == "great insight here."

    def test_strips_trailing_more_toggle(self):
        norm = _fns()[0]
        assert norm("The full thought …more") == norm("The full thought")

    def test_handles_none(self):
        assert _fns()[0](None) == ""


class TestFeedPostKeyStability:
    def test_same_post_hashes_the_same_across_see_more_render(self):
        _, feed_post_key, *_ = _fns()
        collapsed = feed_post_key("Jane Doe", "Models can't fix bad data.\n…see more")
        expanded = feed_post_key("Jane Doe", "Models can't fix bad data.")
        assert collapsed == expanded  # the exact bug: these used to differ

    def test_author_case_and_padding_do_not_change_key(self):
        _, feed_post_key, *_ = _fns()
        assert feed_post_key("  Jane Doe ", "hello world") == feed_post_key("jane doe", "hello world")

    def test_different_posts_differ(self):
        _, feed_post_key, *_ = _fns()
        assert feed_post_key("Jane", "post one") != feed_post_key("Jane", "post two")


class TestUrnExtraction:
    def test_reads_urn_from_card_html(self):
        urn_from_card = _fns()[2]
        card = _card(outer_html='<div data-id="urn:li:activity:7486221543367397377">x</div>')
        assert urn_from_card(card) == "urn:li:activity:7486221543367397377"

    def test_returns_none_when_no_urn(self):
        assert _fns()[2](_card(outer_html="<div>no urn here</div>")) is None

    def test_returns_none_on_exception(self):
        card = MagicMock()
        card.get_attribute.side_effect = RuntimeError("stale")
        assert _fns()[2](card) is None

    def test_ancestor_attribute_scan_wins_over_card_html(self):
        # The #580 case: the card's own HTML has NO urn — the feed-update ancestor's data-id does.
        urn_from_card = _fns()[2]
        card = _card(outer_html="<div>no urn in the comment-button ancestor</div>",
                     scan_result="urn:li:activity:7486221543367397377")
        assert urn_from_card(card) == "urn:li:activity:7486221543367397377"

    def test_scan_result_is_lowercased_and_extracted(self):
        urn_from_card = _fns()[2]
        card = _card(outer_html="<div>nothing</div>",
                     scan_result="urn:li:comment:(urn:li:ugcPost:42,99)")
        assert urn_from_card(card) == "urn:li:ugcpost:42"

    def test_explicit_driver_is_used_for_the_scan(self):
        urn_from_card = _fns()[2]
        driver = MagicMock()
        driver.execute_script.return_value = "urn:li:share:7"
        card = _card(outer_html="<div>nothing</div>")
        assert urn_from_card(card, driver=driver) == "urn:li:share:7"
        driver.execute_script.assert_called_once()

    def test_scan_failure_falls_back_to_card_html(self):
        urn_from_card = _fns()[2]
        card = _card(outer_html='<div data-urn="urn:li:activity:5">x</div>', scan_raises=True)
        assert urn_from_card(card) == "urn:li:activity:5"

    def test_non_string_scan_result_falls_back_to_card_html(self):
        # execute_script can return null/undefined -> None; must not be treated as a URN.
        urn_from_card = _fns()[2]
        card = _card(outer_html='<div data-urn="urn:li:activity:6">x</div>', scan_result=None)
        card.parent.execute_script.return_value = None
        assert urn_from_card(card) == "urn:li:activity:6"


class TestFeedPostIdentity:
    def _identity(self):
        from cqc_lem.app.run_automation import _feed_post_identity
        return _feed_post_identity

    def test_permalink_source(self):
        key, source = self._identity()(
            _card(permalink_href="https://www.linkedin.com/feed/update/urn:li:activity:11/"), "A", "x")
        assert (key, source) == ("feedurn://urn:li:activity:11", "permalink")

    def test_card_source(self):
        key, source = self._identity()(_card(scan_result="urn:li:activity:12"), "A", "x")
        assert (key, source) == ("feedurn://urn:li:activity:12", "card")

    def test_hash_source(self):
        key, source = self._identity()(_card(outer_html="<div>no urn</div>"), "A", "a post body here")
        assert source == "hash" and key.startswith("feedpost://")


class TestStableFeedPostKey:
    def test_prefers_urn_and_is_render_independent(self):
        stable = _fns()[3]
        html = '<div data-urn="urn:li:activity:123">Bad data post …see more</div>'
        k1 = stable(_card(outer_html=html), "Jane", "Bad data post …see more")
        # a later render: text expanded, permalink now present — SAME urn, so SAME key
        k2 = stable(_card(outer_html=html, permalink_href="https://www.linkedin.com/feed/update/urn:li:activity:123/"),
                    "Jane", "Bad data post, the whole thing now visible")
        assert k1 == k2 == "feedurn://urn:li:activity:123"

    def test_permalink_and_html_urn_agree_on_one_key(self):
        stable = _fns()[3]
        by_html = stable(_card(outer_html='<a>urn:li:ugcPost:99</a>'), "A", "x")
        by_link = stable(_card(permalink_href="https://www.linkedin.com/feed/update/urn:li:ugcPost:99/"), "A", "x")
        assert by_html == by_link == "feedurn://urn:li:ugcpost:99"

    def test_falls_back_to_normalized_hash_without_urn(self):
        stable = _fns()[3]
        k1 = stable(_card(outer_html="<div>no urn</div>"), "Jane", "Same post …see more")
        k2 = stable(_card(outer_html="<div>no urn</div>"), "Jane", "Same post")
        assert k1 == k2  # render-stable even on the fallback path
        assert k1.startswith("feedpost://")


class TestContentFingerprint:
    def test_fingerprint_is_render_stable_and_distinct_namespace(self):
        fps = _fns()[4]
        assert fps("Jane", "Post …see more") == fps("Jane", "Post")
        assert all(f.startswith("fp") for f in fps("Jane", "Post"))
        assert len(fps("Jane", "Post")) >= 1

    def test_truncated_render_shares_a_fingerprint_with_the_expanded_one(self):
        # A long post the collapsed card truncates mid-way; the expanded render has the full body.
        fps = _fns()[4]
        full = ("Teams keep buying another dashboard when the real problem is that nobody trusts "
                "the numbers in the one they already own, and no tool on the market fixes trust "
                "for you — that part is a conversation your leadership has to have out loud.")
        truncated = full[:160].rsplit(" ", 1)[0] + "…see more"
        assert fps("Jane", truncated) & fps("Jane", full)

    def test_different_posts_share_no_fingerprint(self):
        fps = _fns()[4]
        assert not (fps("Jane", "Data quality is a people problem, not a tooling problem.")
                    & fps("Jane", "Hiring senior engineers is a distribution problem."))


class TestTruncationProofFallbackKey:
    def test_truncated_and_expanded_render_share_one_fallback_key(self):
        # The #580 fallback bug: hashing the WHOLE body gave the collapsed and expanded renders of
        # one post two different keys, so the ledger/claim guards protected nothing.
        _, feed_post_key, *_ = _fns()
        full = ("Most teams do not have a data problem, they have a definitions problem: three "
                "dashboards, three revenue numbers, and nobody willing to own the discrepancy in "
                "the weekly review where the very same question gets asked all over again.")
        truncated = full[:160].rsplit(" ", 1)[0] + "…see more"
        assert feed_post_key("Jane Doe", truncated) == feed_post_key("Jane Doe", full)

    def test_short_posts_still_key_on_their_whole_body(self):
        _, feed_post_key, *_ = _fns()
        assert feed_post_key("Jane", "Short and complete post.") != \
               feed_post_key("Jane", "Short and complete post!")

    def test_norm_prefix_cuts_on_a_word_boundary(self):
        from cqc_lem.app.run_automation import _norm_prefix
        assert _norm_prefix("alpha beta gamma delta", 12) == "alpha beta"
        assert _norm_prefix("alpha beta", 50) == "alpha beta"
        assert _norm_prefix("supercalifragilistic", 5) == "super"  # no boundary to cut on
