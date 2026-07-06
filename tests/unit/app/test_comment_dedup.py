"""Unit tests for the persistent at-most-once feed-comment dedup guard in comment_on_feed_inline
and the URL-based comment_on_post task."""

import pytest
from unittest.mock import MagicMock, patch
from contextlib import ExitStack

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


def _box(text):
    b = MagicMock()
    b.text = text
    return b


def _run_feed(boxes, *, claim_side_effect=None, has_commented=False, max_posts=10,
              author="Jane Author", is_me=False, react_returns=True, post_returns=True):
    """Drive comment_on_feed_inline with all the SDUI/DB collaborators mocked. Returns a dict of
    the key mocks so assertions can inspect calls."""
    from cqc_lem.app import run_automation as ra

    driver = MagicMock()
    driver.find_elements.return_value = boxes
    wait = MagicMock()

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
        p("get_engagement_preferences", return_value={"max_comments_per_day": 20})
        p("get_recent_engagers", return_value=set())
        p("count_comments_today", return_value=0)
        p("_switch_feed_to_recent")
        p("_card_for_textbox", side_effect=lambda d, b: MagicMock())
        p("_post_author_from_card", return_value=author)
        p("_post_permalink_from_card", return_value=None)
        p("_feed_post_key", side_effect=_key)
        p("_author_is_me", return_value=is_me)
        p("has_commented_post", return_value=has_commented)
        p("has_user_commented_on_post_url", return_value=False)
        p("_passes_hard_excludes", return_value=True)
        p("_post_age_minutes", return_value=10)
        p("_post_social_counts", return_value={"comments": 0, "reactions": 0})
        p("_literal_relevant", return_value=True)
        p("_score_feed_post", return_value=1.0)
        p("post_matches_preferences", return_value=True)
        p("claim_post_for_comment", new=claim)
        p("generate_ai_response", new=gen)
        p("post_comment_inline", new=post_inline)
        p("react_to_post_inline", new=react)
        p("mark_post_commented", new=mark)
        p("mark_post_reacted", new=mark_reacted)
        p("release_post_claim", new=release)
        p("log_warning")
        p("insert_new_log")
        p("simulate_reading_time", return_value=0)
        p("simulate_thinking_time", return_value=0)
        posted = ra.comment_on_feed_inline(driver, wait, MagicMock(), user_id=1, max_posts=max_posts)

    return {"posted": posted, "claim": claim, "post_inline": post_inline, "react": react,
            "mark": mark, "mark_reacted": mark_reacted, "release": release, "gen": gen}


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
            p("simulate_reading_time", return_value=0)
            p("simulate_thinking_time", return_value=0)
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
