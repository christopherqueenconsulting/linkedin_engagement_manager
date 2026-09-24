"""The feed walk never comments twice on one post (issue #2130, regression of #580).

The 2026-09 audit found 22 same-post pairs, each 74-98 s apart, the second comment grounded on our
own first one: the re-rendered card's text node was the comment we had just posted, so it minted a
fresh key and cleared every post-keyed dedup. These drive `comment_on_feed_inline` through the
shared `_run_feed` harness and pin the two key-independent guards that answer it.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.app.test_comment_dedup import (
    _FEED,
    _box,
    _no_sleep,  # noqa: F401 — autouse: the walk's pacing sleeps
    _run_feed,
)

pytestmark = pytest.mark.unit

_OUR_COMMENT = ("The definitions problem is the real one here — we saw the same three revenue "
                "numbers until finance owned the glossary.")


class TestOwnCommentReRead:
    def test_a_rerendered_card_carrying_our_just_posted_comment_earns_no_second_comment(self):
        # The lead-in for the re-read hypothesis: after the comment lands the feed re-renders and
        # a text node holding OUR comment shows up as a fresh card (new key, new author read).
        boxes = [_box("Most teams do not have a data problem, they have a definitions problem.")]
        r = _run_feed(boxes, comment_text=_OUR_COMMENT,
                      on_post=lambda card, text: boxes.append(_box(text)))
        assert r["posted"] == 1
        assert r["post_inline"].call_count == 1
        r["gen"].assert_called_once()
        assert r["funnel"]["own_comment_skipped"] == 1

    def test_a_card_matching_a_logged_comment_from_the_last_day_is_never_commented(self):
        r = _run_feed([_box(_OUR_COMMENT)], own_history=[_OUR_COMMENT])
        assert r["posted"] == 0
        r["claim"].assert_not_called()
        r["gen"].assert_not_called()

    def test_the_collapsed_render_of_our_comment_still_reads_as_ours(self):
        collapsed = _OUR_COMMENT[:70] + "…see more"
        r = _run_feed([_box(collapsed)], own_history=[_OUR_COMMENT])
        assert r["posted"] == 0
        r["gen"].assert_not_called()

    def test_the_window_is_read_by_hours_not_by_count(self):
        with patch(f"{_FEED}.get_recent_comment_texts", return_value=[]) as history:
            from cqc_lem.app.engagement import feed as ra
            driver = MagicMock()
            driver.find_elements.return_value = []
            with patch(f"{_FEED}.get_engagement_preferences",
                       return_value={"max_comments_per_day": 20}), \
                 patch(f"{_FEED}.get_recent_engagers", return_value=set()), \
                 patch(f"{_FEED}.count_comments_today", return_value=0), \
                 patch(f"{_FEED}._switch_feed_to_recent"), \
                 patch(f"{_FEED}.get_engagement_targets", return_value=[]), \
                 patch(f"{_FEED}.get_or_create_profile_synthesis", return_value=None), \
                 patch(f"{_FEED}._has_sourced_facts", return_value=False), \
                 patch(f"{_FEED}._report_zero_walk", return_value="empty"), \
                 patch(f"{_FEED}.set_feed_funnel"), patch(f"{_FEED}.track_feed_scan"), \
                 patch(f"{_FEED}.time.sleep"):
                ra.comment_on_feed_inline(driver, MagicMock(), MagicMock(), user_id=1,
                                          max_posts=1, deadline_ts=0.0001)
        assert any(c.kwargs.get("hours") == 24 for c in history.call_args_list)

    def test_a_faulted_history_read_fails_open_instead_of_aborting_the_walk(self):
        # A non-MySQL fault (e.g. an unset DB_PORT's TypeError) escapes the repository's own
        # except; the walk must still run, guarded by this run's comments alone.
        r = _run_feed([_box("A founder on why the first sales hire should be a generalist.")],
                      own_history=TypeError("int() argument must be ... not 'NoneType'"))
        assert r["posted"] == 1

    def test_an_unrelated_post_is_not_mistaken_for_ours(self):
        r = _run_feed([_box("A founder on why the first sales hire should be a generalist.")],
                      own_history=[_OUR_COMMENT])
        assert r["posted"] == 1


class TestOneCommentPerAuthor:
    def test_two_cards_from_one_author_in_one_walk_get_at_most_one_comment(self):
        r = _run_feed([_box("First post by the same author, long enough to qualify."),
                       _box("Second different post by that author, also long enough.")],
                      author="Jane Author")
        assert r["posted"] == 1
        assert r["post_inline"].call_count == 1
        r["gen"].assert_called_once()

    def test_author_match_ignores_case_and_padding(self):
        from cqc_lem.app.engagement import feed as ra
        assert ra._author_norm("  Jane Author ") == ra._author_norm("jane author")

    def test_an_unnamed_author_is_not_folded_into_one(self):
        # A card that names nobody is not evidence two posts share an author — fails open.
        r = _run_feed([_box("First post whose card named no author at all."),
                       _box("Second post whose card also named no author.")], author="")
        assert r["posted"] == 2

    def test_an_author_commented_on_by_an_earlier_walk_is_skipped(self):
        client = MagicMock()
        client.exists.return_value = 1
        with patch(f"{_FEED}._redis_client", return_value=client):
            r = _run_feed([_box("A post by an author another walk just answered.")],
                          author="Jane Author")
        assert r["posted"] == 0
        r["claim"].assert_not_called()

    def test_a_landed_comment_starts_the_30_minute_cooldown(self):
        client = MagicMock()
        client.exists.return_value = 0
        with patch(f"{_FEED}._redis_client", return_value=client):
            r = _run_feed([_box("A post by an author we have not answered yet.")],
                          author="Jane Author")
        assert r["posted"] == 1
        (key, value), kwargs = client.set.call_args
        assert key.startswith("linkedin:feed_author_cooldown:1:")
        assert kwargs == {"ex": 30 * 60}


class TestAuthorCooldownHelpers:
    def test_fails_open_without_redis(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}._redis_client", return_value=None):
            assert ra._author_on_cooldown(1, "Jane") is False
            ra._start_author_cooldown(1, "Jane")   # no-op, no raise

    def test_fails_open_on_a_redis_error(self):
        from cqc_lem.app.engagement import feed as ra
        client = MagicMock()
        client.exists.side_effect = RuntimeError("down")
        client.set.side_effect = RuntimeError("down")
        with patch(f"{_FEED}._redis_client", return_value=client), \
             patch(f"{_FEED}.log_warning") as warn:
            assert ra._author_on_cooldown(1, "Jane") is False
            ra._start_author_cooldown(1, "Jane")
        assert warn.call_count == 2

    def test_an_unnamed_author_never_touches_redis(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}._redis_client") as rc:
            assert ra._author_on_cooldown(1, "  ") is False
            ra._start_author_cooldown(1, "")
        rc.assert_not_called()

    def test_short_text_is_never_matched(self):
        from cqc_lem.app.engagement import feed as ra
        assert ra._is_own_comment_text("Great post!", ["Great post! Thanks for sharing it."]) is False
        assert ra._is_own_comment_text(_OUR_COMMENT, ["", None, "ok"]) is False


class TestRecentCommentWindowQuery:
    def test_hours_bounds_the_read_to_a_trailing_window(self, fake_cursor):
        from cqc_lem.utilities.db import LogActionType, LogResultType, get_recent_comment_texts
        conn, cursor = fake_cursor(fetch_all=[("newest comment",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            out = get_recent_comment_texts(7, limit=500, hours=24)
        assert out == ["newest comment"]
        sql, params = cursor.execute.call_args.args
        assert "INTERVAL %s HOUR" in sql
        assert params == (7, LogActionType.COMMENT.value, LogActionType.GROUP_COMMENT.value,
                          LogResultType.SUCCESS.value, 24, 500)
