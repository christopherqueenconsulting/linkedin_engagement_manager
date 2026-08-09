"""Ordered fallback chains for the comment action and the reaction fly-out (issue #816).

Grounded against a live session 2026-08-02. The live counts on a 9-post home feed:

    [data-testid='expandable-text-box']            9   post text nodes
    button[aria-label^='Reaction button state']    8   reaction trigger
    button[aria-label='Comment']                   0   <- had been THE anchor
    .feed-shared-update-v2 *                       0   legacy classes, confirmed dead

The Comment button now carries no aria-label at all — only the visible text "Comment". That single
anchor was load-bearing in three places (card walk, URN-scan boundary, composer opener), which is
how one label rotation took out commenting AND reactions at once. These tests pin the properties
that make the replacement survive the next rotation.
"""

import pytest

pytestmark = pytest.mark.unit


class TestCommentActionChain:
    def test_is_an_ordered_chain_not_a_single_anchor(self):
        from cqc_lem.app.run_automation import _COMMENT_ACTION_LOCATORS
        assert len(_COMMENT_ACTION_LOCATORS) >= 3, "one locator is a silent single point of failure"

    def test_keeps_the_aria_label_route_first(self):
        """It matched zero live, but LinkedIn rotates anchors back. Ordered most-stable-first, and
        an entry that costs nothing when absent is worth keeping ahead of the text route.
        """
        from cqc_lem.app.run_automation import _COMMENT_ACTION_LOCATORS
        assert "aria-label='Comment'" in _COMMENT_ACTION_LOCATORS[0][1]

    def test_carries_a_text_route(self):
        """The only route that resolves on the current SDUI."""
        from cqc_lem.app.run_automation import _COMMENT_ACTION_LOCATORS
        assert any("'comment'" in sel for _by, sel in _COMMENT_ACTION_LOCATORS)

    def test_xpath_entries_stay_card_scoped(self):
        """`//` searches the whole document even when passed parent_element=card, so a bare `//`
        entry would silently return a NEIGHBOURING post's Comment button and comment on the wrong
        post. Every XPath in a card-scoped chain must be relative.
        """
        from selenium.webdriver.common.by import By

        from cqc_lem.app.run_automation import (
            _COMMENT_ACTION_LOCATORS,
            _REACTION_OPENER_LOCATORS,
            _REACTION_TRIGGER_LOCATORS,
        )
        for chain in (_COMMENT_ACTION_LOCATORS, _REACTION_TRIGGER_LOCATORS,
                      _REACTION_OPENER_LOCATORS):
            for by, sel in chain:
                if by == By.XPATH:
                    assert sel.startswith(".//"), f"card-scoped XPath must be relative: {sel}"


class TestCommentActionPredicate:
    """The JS predicate shared by the card walk and the URN-scan boundary."""

    def test_matches_the_action_button_but_not_the_count(self):
        """The feed renders comment-COUNT affordances alongside the action:

            {"tag": "button", "type": "button", "text": "Comment"}          <- action
            {"tag": "div", "role": "button", "text": "7 comments"}          <- count

        A `contains('comment')` match returns the count happily, and clicking a count opens the
        thread instead of the composer — a silent wrong-action, not a visible failure.
        """
        from cqc_lem.utilities.linkedin.cards import _COMMENT_ACTION_JS
        assert "=== 'comment'" in _COMMENT_ACTION_JS or "text === 'comment'" in _COMMENT_ACTION_JS
        assert "includes('comment')" not in _COMMENT_ACTION_JS.replace(
            "testid.includes('comment') && text === 'comment'", "")

    def test_both_js_consumers_share_one_predicate(self):
        """The card walk and the URN-scan boundary must agree on what a comment action is. When
        they disagreed, the scan climbed past the card and returned a neighbour's URN.
        """
        from cqc_lem.utilities.linkedin.cards import (
            _CARD_FOR_TEXTBOX_JS,
            _COMMENT_ACTION_JS,
            _URN_SCAN_JS,
        )
        assert _CARD_FOR_TEXTBOX_JS.startswith(_COMMENT_ACTION_JS)
        assert _URN_SCAN_JS.startswith(_COMMENT_ACTION_JS)


class TestReactionChains:
    def test_trigger_chain_leads_with_the_anchor_that_actually_resolves(self):
        from cqc_lem.app.run_automation import _REACTION_TRIGGER_LOCATORS
        assert "Reaction button state" in _REACTION_TRIGGER_LOCATORS[0][1]
        assert len(_REACTION_TRIGGER_LOCATORS) >= 4

    def test_trigger_chain_matches_like_exactly_never_a_count(self):
        """'2 likes' must not be mistaken for the Like toggle."""
        from cqc_lem.app.run_automation import _REACTION_TRIGGER_LOCATORS
        like = [s for _b, s in _REACTION_TRIGGER_LOCATORS if "'like'" in s]
        assert like, "expected a text route to the Like toggle"
        assert all("contains(" not in s for s in like)

    def test_option_locators_are_document_scoped(self):
        """The fly-out renders OUTSIDE the card subtree — confirmed live, the options were
        reachable only from `driver`. A card-relative `.//` would find nothing with the menu open.
        """
        from selenium.webdriver.common.by import By

        from cqc_lem.app.run_automation import _reaction_option_locators
        for by, sel in _reaction_option_locators("Celebrate"):
            if by == By.XPATH:
                assert sel.startswith("//"), f"fly-out lookup must be document-scoped: {sel}"

    def test_option_locators_are_case_insensitive_beyond_the_exact_match(self):
        from cqc_lem.app.run_automation import _reaction_option_locators
        chain = _reaction_option_locators("Celebrate")
        assert "aria-label='Celebrate'" in chain[0][1]          # exact, confirmed live
        assert any("translate(" in sel for _by, sel in chain)   # case-folded fallbacks

    @pytest.mark.parametrize("reaction", ["Like", "Celebrate", "Support", "Love", "Insightful",
                                          "Funny"])
    def test_every_live_reaction_builds_a_chain(self, reaction):
        """These six are the exact aria-labels captured from the open fly-out."""
        from cqc_lem.app.run_automation import _reaction_option_locators
        chain = _reaction_option_locators(reaction)
        assert len(chain) >= 3
        assert f"aria-label='{reaction}'" in chain[0][1]
