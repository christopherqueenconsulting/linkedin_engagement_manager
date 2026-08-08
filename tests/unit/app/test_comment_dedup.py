"""Unit tests for the persistent at-most-once feed-comment dedup guard in comment_on_feed_inline
and the URL-based comment_on_post task.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"
# The zero-walk grading moved to its own module (#1021) so scrapper/company_page_inviter can
# share it; the logger it calls lives there now.
_ZW = "cqc_lem.utilities.linkedin.zero_walk"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


def _box(text):
    b = MagicMock()
    b.text = text
    return b


def _messages(mock, needle):
    """Log messages recorded on a patched logger call that mention `needle`. Matched on the bare
    word 'hash' rather than either message's own phrasing, so the #1064 guard still fires if the
    warning comes back worded the way it was before ('unstable content-hash key').
    """
    return [c.args[0] for c in mock.call_args_list if c.args and needle in c.args[0]]


def _card_for(box):
    """A card mock that remembers which post text it was resolved from, so a test can answer the
    URN scan per post rather than once for the whole run.
    """
    card = MagicMock()
    card.box_text = box.text
    return card


def _run_feed(boxes, *, claim_side_effect=None, has_commented=False, max_posts=10,
              author="Jane Author", is_me=False, react_returns=True, post_returns=True,
              prefs=None, matches=True, real_key=False, urn_scan=None, find_elements=None,
              urn_by_text=None):
    """Drive comment_on_feed_inline with all the SDUI/DB collaborators mocked. Returns a dict of
    the key mocks so assertions can inspect calls.

    `find_elements` overrides the driver's element lookup wholesale, so a test can answer the post-
    text selector and the zero-walk cross-check selector differently (issue #1013).
    `urn_by_text` maps a post's text to the URN its card carries, so one scan can mix cards that
    resolve a URN with cards that fall back to the content hash.
    """
    from cqc_lem.app import run_automation as ra

    driver = MagicMock()
    if find_elements is not None:
        driver.find_elements.side_effect = find_elements
    else:
        driver.find_elements.return_value = boxes
    wait = MagicMock()
    # The ancestor/attribute URN scan runs through the driver; a non-str return means "no URN".
    driver.execute_script.return_value = urn_scan

    # A stable content->key map (simulates _feed_post_key: same content => same key).
    def _key(a, content):
        return f"feedpost://{hash(content) & 0xffff}"

    claim = MagicMock(side_effect=claim_side_effect) if claim_side_effect is not None \
        else MagicMock(return_value=True)
    post_inline = MagicMock(return_value=post_returns)
    mark = MagicMock(return_value=True)
    mark_reacted = MagicMock(return_value=True)
    release = MagicMock(return_value=True)
    react = MagicMock(return_value=react_returns)
    gen = MagicMock(return_value="A thoughtful comment.")

    with ExitStack() as es:
        p = lambda name, **kw: es.enter_context(patch(f"{_RA}.{name}", **kw))
        # Reactions ship OFF by default while #816 is open; these tests exercise the reaction
        # path itself, so they opt in explicitly rather than depending on the shipped default.
        p("INLINE_REACTIONS_ENABLED", new=True)
        p("get_engagement_preferences", return_value=prefs or {"max_comments_per_day": 20})
        p("get_recent_engagers", return_value=set())
        p("get_recent_comment_texts", return_value=[])
        p("count_comments_today", return_value=0)
        p("_switch_feed_to_recent")
        p("_card_for_textbox", side_effect=lambda d, b: _card_for(b))
        p("_post_author_from_card", return_value=author)
        p("_post_permalink_from_card", return_value=None)
        if urn_by_text is not None:
            p("_feed_post_urn_from_card",
              side_effect=lambda card, driver=None: urn_by_text.get(getattr(card, "box_text", None)))
        if not real_key:
            p("_feed_post_key", side_effect=_key)
        p("_author_is_me", return_value=is_me)
        p("has_commented_post", return_value=has_commented)
        p("has_user_commented_on_post_url", return_value=False)
        p("_passes_hard_excludes", return_value=True)
        p("_post_age_minutes", return_value=10)
        p("_post_social_counts", return_value={"comments": 0, "reactions": 0})
        p("_literal_relevant", return_value=True)
        p("_score_feed_post", return_value=1.0)
        p("post_matches_preferences", return_value=matches)
        # Empty roster: the run degrades to the plain feed walk (issue #616 acceptance).
        p("get_engagement_targets", return_value=[])
        p("post_is_relevant", return_value=True)
        funnel_holder = {}
        p("set_feed_funnel", side_effect=lambda uid, f: funnel_holder.update(f))
        p("claim_post_for_comment", new=claim)
        p("generate_ai_response", new=gen)
        p("post_comment_inline", new=post_inline)
        p("react_to_post_inline", new=react)
        p("mark_post_commented", new=mark)
        p("mark_post_reacted", new=mark_reacted)
        p("release_post_claim", new=release)
        warn = p("log_warning")
        debug = p("log_debug")
        p("insert_new_log")
        p("pace_read", return_value=0.0)
        posted = ra.comment_on_feed_inline(driver, wait, MagicMock(), user_id=1, max_posts=max_posts)

    return {"posted": posted, "claim": claim, "post_inline": post_inline, "react": react,
            "mark": mark, "mark_reacted": mark_reacted, "release": release, "gen": gen,
            "funnel": funnel_holder, "warn": warn, "debug": debug}


class TestFeedDedup:
    def test_two_distinct_posts_each_commented_once(self):
        r = _run_feed([_box("First post content that is long enough."),
                       _box("Second different post content long enough.")])
        assert r["posted"] == 2
        assert r["post_inline"].call_count == 2
        # Each distinct key claimed exactly once.
        keys = {c.args[1] for c in r["claim"].call_args_list}
        assert len(keys) == 2

    def test_duplicate_post_in_view_commented_only_once(self):
        # Same content twice => same stable key => at most one comment.
        same = "Identical post content appears twice in the feed view."
        r = _run_feed([_box(same), _box(same)])
        assert r["posted"] == 1
        assert r["post_inline"].call_count == 1

    def test_rerendered_truncation_does_not_earn_a_second_comment(self):
        # #580: the collapsed card truncates a long post, the expanded render carries the whole
        # body. Hashing everything made those two renders two keys -> two comments on one post.
        full = ("Most teams do not have a data problem, they have a definitions problem: three "
                "dashboards, three revenue numbers, and nobody willing to own the discrepancy in "
                "the weekly review where the very same question gets asked all over again.")
        truncated = full[:160].rsplit(" ", 1)[0] + "…see more"
        r = _run_feed([_box(truncated), _box(full)], real_key=True)
        assert r["posted"] == 1
        assert r["post_inline"].call_count == 1

    def test_urn_from_ancestor_scan_is_the_dedup_key(self):
        # With no permalink on the card, the ancestor data-* scan must still produce a feedurn://
        # key — the whole point of #580 (a hash key is the duplicate-prone fallback).
        r = _run_feed([_box("A feed post whose activity urn lives on an ancestor node.")],
                      real_key=True, urn_scan="urn:li:activity:7486221543367397377")
        assert r["posted"] == 1
        assert r["claim"].call_args[0][1] == "feedurn://urn:li:activity:7486221543367397377"
        assert r["funnel"]["commented_key_sources"] == {"card": 1}

    def test_funnel_records_hash_key_source_when_no_urn(self):
        r = _run_feed([_box("A feed post with no activity urn anywhere on the card.")],
                      real_key=True)
        assert r["posted"] == 1
        assert r["funnel"]["commented_key_sources"] == {"hash": 1}
        assert r["funnel"]["key_sources"] == {"hash": 1}

    def test_one_urnless_card_among_readable_ones_is_debug_not_a_warning(self):
        # #1064: a single URN-less card is designed degradation — the content fingerprints hold it
        # inside the scan and reconcile_recent_comment_urns upgrades the row later. Warning on it
        # escalated working behaviour into a filed PostHog issue.
        r = _run_feed([_box("A feed post whose card carries its activity urn."),
                       _box("A feed post with no activity urn anywhere on it.")],
                      real_key=True,
                      urn_by_text={"A feed post whose card carries its activity urn.":
                                   "urn:li:activity:7486221543367397377"})
        assert r["posted"] == 2
        assert r["funnel"]["commented_key_sources"] == {"card": 1, "hash": 1}
        assert not _messages(r["warn"], "hash")
        assert _messages(r["debug"], "content-hash")

    def test_no_urn_anywhere_in_the_scan_still_warns(self):
        # The #580 signature: the resolver read nothing on any post examined, so every comment
        # keyed on the volatile hash. That is drift, and it must keep escalating.
        r = _run_feed([_box("A feed post with no activity urn anywhere on the card.")],
                      real_key=True, urn_by_text={})
        assert r["posted"] == 1
        assert r["funnel"]["key_sources"] == {"hash": 1}
        assert _messages(r["warn"], "URN resolver drift")
        assert not _messages(r["debug"], "content-hash")   # the two branches are exclusive

    def test_all_urn_keyed_scan_says_nothing_about_hashes(self):
        r = _run_feed([_box("A feed post whose card carries its activity urn.")], real_key=True,
                      urn_by_text={"A feed post whose card carries its activity urn.":
                                   "urn:li:activity:7486221543367397377"})
        assert r["funnel"]["commented_key_sources"] == {"card": 1}
        assert not _messages(r["warn"], "hash")
        assert not _messages(r["debug"], "content-hash")

    def test_lost_claim_race_is_noop(self):
        # claim returns False (another run/worker already holds it) => no LLM, no comment.
        r = _run_feed([_box("A post that another worker already claimed here.")],
                      claim_side_effect=[False])
        assert r["posted"] == 0
        r["gen"].assert_not_called()
        r["post_inline"].assert_not_called()

    def test_already_commented_post_skipped_before_claim(self):
        r = _run_feed([_box("A post already recorded in the ledger from before.")],
                      has_commented=True)
        assert r["posted"] == 0
        r["claim"].assert_not_called()
        r["post_inline"].assert_not_called()

    def test_failed_post_releases_claim(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        driver.find_elements.return_value = [_box("A post whose comment submit will fail here.")]
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_RA}.{name}", **kw))
            p("get_engagement_preferences", return_value={"max_comments_per_day": 20})
            p("get_recent_engagers", return_value=set())
            p("get_recent_comment_texts", return_value=[])
            p("count_comments_today", return_value=0)
            p("_switch_feed_to_recent")
            p("_card_for_textbox", side_effect=lambda d, b: MagicMock())
            p("_post_author_from_card", return_value="Jane")
            p("_post_permalink_from_card", return_value=None)
            p("_feed_post_key", return_value="feedpost://fail")
            p("has_commented_post", return_value=False)
            p("has_user_commented_on_post_url", return_value=False)
            p("_passes_hard_excludes", return_value=True)
            p("_post_age_minutes", return_value=10)
            p("_post_social_counts", return_value={"comments": 0, "reactions": 0})
            p("_literal_relevant", return_value=True)
            p("_score_feed_post", return_value=1.0)
            p("post_matches_preferences", return_value=True)
            p("get_engagement_targets", return_value=[])
            p("post_is_relevant", return_value=True)
            p("_author_is_me", return_value=False)
            p("claim_post_for_comment", return_value=True)
            p("generate_ai_response", return_value="A comment.")
            p("post_comment_inline", return_value=False)  # submit fails
            p("mark_post_commented")
            p("mark_post_reacted")
            p("react_to_post_inline", return_value=True)
            p("log_warning")
            release = es.enter_context(patch(f"{_RA}.release_post_claim"))
            p("insert_new_log")
            p("pace_read", return_value=0.0)
            posted = ra.comment_on_feed_inline(driver, MagicMock(), MagicMock(), user_id=1, max_posts=1)
        assert posted == 0
        release.assert_called_with(1, "feedpost://fail")


class TestFeedReactions:
    def test_reaction_left_on_non_own_post(self):
        r = _run_feed([_box("A non-authored post we will comment on and react to.")],
                      is_me=False)
        assert r["posted"] == 1
        r["react"].assert_called_once()
        r["mark_reacted"].assert_called_once()

    def test_no_reaction_on_own_post(self):
        r = _run_feed([_box("Our OWN post appearing in the feed — no self reaction here.")],
                      is_me=True)
        # We still comment, but never react on our own post.
        assert r["posted"] == 1
        r["react"].assert_not_called()
        r["mark_reacted"].assert_not_called()

    def test_reaction_failure_does_not_block_comment(self):
        r = _run_feed([_box("A post whose reaction fails but the comment must still post.")],
                      react_returns=False)
        assert r["posted"] == 1            # comment still posted
        r["post_inline"].assert_called_once()
        r["mark_reacted"].assert_not_called()  # reaction failed -> not marked


class TestAuthorIsMe:
    def test_matches_own_name_case_insensitively(self):
        from cqc_lem.app.run_automation import _author_is_me
        prof = MagicMock(); prof.full_name = "Chris Queen"
        assert _author_is_me("chris queen", prof) is True
        assert _author_is_me("Someone Else", prof) is False

    def test_blank_profile_name_is_not_me(self):
        from cqc_lem.app.run_automation import _author_is_me
        prof = MagicMock(); prof.full_name = ""
        assert _author_is_me("Anybody", prof) is False


class TestCommentOnPostTaskIdempotency:
    def test_skips_when_already_claimed(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.has_user_commented_on_post_url", return_value=False), \
             patch(f"{_RA}.has_commented_post", return_value=False), \
             patch(f"{_RA}.claim_post_for_comment", return_value=False) as claim, \
             patch(f"{_RA}.get_driver_wait_pair") as gdw:
            result = ra.comment_on_post.run(user_id=1, post_link="https://x/feed/update/1/",
                                            comment_text="hi")
        assert "already claimed" in result
        claim.assert_called_once()
        gdw.assert_not_called()  # never opens a browser when it loses the claim

    def test_skips_when_already_commented(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.has_user_commented_on_post_url", return_value=False), \
             patch(f"{_RA}.has_commented_post", return_value=True), \
             patch(f"{_RA}.claim_post_for_comment") as claim, \
             patch(f"{_RA}.get_driver_wait_pair") as gdw:
            result = ra.comment_on_post.run(user_id=1, post_link="https://x/feed/update/1/",
                                            comment_text="hi")
        assert "already commented" in result
        claim.assert_not_called()
        gdw.assert_not_called()


class TestFeedFunnelAndFallback:
    def test_funnel_recorded(self):
        r = _run_feed([_box("First post content that is long enough."),
                       _box("Second different post content long enough.")])
        f = r["funnel"]
        assert f["examined"] == 2 and f["passed_filters"] == 2
        assert f["matched_topics"] == 2 and f["commented"] == 2
        assert f["fallback_used"] is False

    def test_strict_filters_zero_matches_no_fallback_when_disabled(self):
        # Include filters set, nothing matches, fallback OFF -> comment on nothing.
        boxes = [_box(f"Post number {i} with enough text to qualify here.") for i in range(9)]
        r = _run_feed(boxes, matches=False,
                      prefs={"max_comments_per_day": 20, "include_topics": ["AI"],
                             "feed_fallback_when_empty": False})
        assert r["posted"] == 0
        assert r["funnel"]["matched_topics"] == 0
        assert r["funnel"]["fallback_used"] is False

    def test_fallback_comments_when_filters_match_nothing(self):
        # Include filters set, nothing matches, fallback ON -> after enough misses it relaxes and
        # comments on the best feed posts anyway.
        boxes = [_box(f"Post number {i} with enough text to qualify here.") for i in range(9)]
        r = _run_feed(boxes, matches=False,
                      prefs={"max_comments_per_day": 20, "include_topics": ["AI"],
                             "feed_fallback_when_empty": True})
        assert r["posted"] >= 1               # fallback engaged
        assert r["funnel"]["fallback_used"] is True
        assert r["funnel"]["matched_topics"] == 0

    def test_no_include_filters_means_no_fallback_flag(self):
        # With no include filters, everything matches; fallback is irrelevant and stays False.
        r = _run_feed([_box("Just a normal post with sufficient length to pass.")],
                      prefs={"max_comments_per_day": 20})
        assert r["posted"] == 1
        assert r["funnel"]["fallback_used"] is False

    def test_empty_hard_filters_relaxes_when_enabled(self):
        # Every post fails the min-reactions gate (0 < 5), so NOTHING clears the hard filters —
        # the include-miss fallback can't help (it only triggers on posts that first passed the
        # hard gates). With fallback ON, the empty-feed path must relax recency/min-reactions and
        # comment anyway (this is the case feed_fallback_when_empty is named for).
        boxes = [_box("Sparse feed post with enough text to qualify for scanning.")]
        r = _run_feed(boxes, matches=False,
                      prefs={"max_comments_per_day": 20, "include_topics": ["AI"],
                             "feed_fallback_when_empty": True, "min_reactions": 5})
        assert r["posted"] >= 1
        assert r["funnel"]["fallback_used"] is True

    def test_empty_hard_filters_no_relax_when_disabled(self):
        # Same sparse/low-reaction feed but fallback OFF -> stays at zero comments.
        boxes = [_box("Sparse feed post with enough text to qualify for scanning.")]
        r = _run_feed(boxes, matches=False,
                      prefs={"max_comments_per_day": 20, "include_topics": ["AI"],
                             "feed_fallback_when_empty": False, "min_reactions": 5})
        assert r["posted"] == 0
        assert r["funnel"]["fallback_used"] is False


class TestFeedZeroWalkTripwire:
    """#1013: zero post-text nodes across a whole scan is indistinguishable from an empty feed in
    every funnel number — which is how #964 and #1009 stayed invisible for weeks. The scan asks the
    page through a per-post control the text-node chain does not use.
    """

    def test_a_walk_that_saw_cards_is_ok(self):
        r = _run_feed([_box("A feed post with plenty of content to comment on.")])
        assert r["funnel"]["feed_walk"] == "ok"
        assert r["funnel"]["textboxes_seen"] == 1

    def test_zero_textboxes_on_an_empty_feed_is_empty_not_drift(self):
        r = _run_feed([])
        assert r["funnel"]["textboxes_seen"] == 0
        assert r["funnel"]["feed_walk"] == "empty"

    def test_zero_textboxes_while_the_page_renders_posts_is_drift(self):
        from cqc_lem.app.run_automation import _FEED_CARD_CROSSCHECK_SEL, _FEED_POST_TEXT_SEL

        def _find(by, selector):
            if selector == _FEED_POST_TEXT_SEL:
                return []                       # the walk is blind
            if selector == _FEED_CARD_CROSSCHECK_SEL:
                return [MagicMock()] * 8        # but the page rendered eight posts
            return []

        r = _run_feed([], find_elements=_find)
        assert r["funnel"]["feed_walk"] == "drift"

    def test_only_drift_warns(self):
        """The log LEVEL is the contract: a warning that repeats re-emits at ERROR and files a
        grouped $exception, so an empty feed or an unreadable cross-check must stay DEBUG.
        """
        from cqc_lem.app.run_automation import _report_zero_walk
        driver = MagicMock()

        driver.find_elements.return_value = [MagicMock()] * 3
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            assert _report_zero_walk(driver, "sel", "Feed post-text walk") == "drift"
        warn.assert_called_once()
        debug.assert_not_called()

        driver.find_elements.return_value = []
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            assert _report_zero_walk(driver, "sel", "Feed post-text walk") == "empty"
        warn.assert_not_called()
        debug.assert_called_once()

        # "We could not ask the page" is not "the page said zero", and is never a defect.
        from selenium.common.exceptions import WebDriverException
        driver.find_elements.side_effect = WebDriverException("session gone")
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            assert _report_zero_walk(driver, "sel", "Feed post-text walk") == "unknown"
        warn.assert_not_called()
        debug.assert_called_once()
