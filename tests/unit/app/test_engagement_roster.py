"""Unit tests for the target-creator engagement roster (issue #616): 50/30/20 selection, the
per-author weekly cap, the on-topic gate, and the roster-vs-feed funnel diagnostics.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_FEED = "cqc_lem.app.engagement.feed"
# The connect rail moved to its own module (#1154); patches for it must bind THERE, because that
# is the module whose globals the invite code reads.
_INV = "cqc_lem.app.engagement.invites"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_FEED}.time.sleep"):
        yield


def _ctx(driver, *, prefs=None, seen=None, deadline_ts=None, **kwargs):
    """The run context the roster pass reads (issue #1220), with the parts it never touches mocked."""
    from cqc_lem.domain.models import FeedRunContext
    return FeedRunContext(driver=driver, wait=MagicMock(), my_profile=MagicMock(), user_id=1,
                          prefs=dict(prefs or {}), profile_synthesis="synthesis",
                          seen=seen if seen is not None else set(), deadline_ts=deadline_ts,
                          **kwargs)


def _target(url, category="peer", *, last=None, cap=2, used=0, active=True, name=None):
    return {"id": abs(hash(url)) % 10000, "profile_url": url, "name": name or url,
            "category": category, "max_comments_per_week": cap, "active": active,
            "last_engaged_at": last, "comments_this_week": used, "source": "user"}


class TestSelectRosterTargets:
    def test_blends_fifty_thirty_twenty(self):
        from cqc_lem.app.engagement.feed import select_roster_targets
        targets = ([_target(f"peer{i}", "peer") for i in range(10)]
                   + [_target(f"icp{i}", "icp") for i in range(10)]
                   + [_target(f"cre{i}", "creator") for i in range(10)])
        picked = select_roster_targets(targets, 10)
        assert len(picked) == 10
        by_cat = {c: sum(1 for t in picked if t["category"] == c) for c in ("peer", "icp", "creator")}
        assert by_cat == {"peer": 5, "icp": 3, "creator": 2}

    def test_never_engaged_targets_come_first(self):
        from cqc_lem.app.engagement.feed import select_roster_targets
        recent = _target("recent", "peer", last=datetime.now())
        stale = _target("stale", "peer", last=datetime.now() - timedelta(days=9))
        fresh = _target("never", "peer", last=None)
        picked = select_roster_targets([recent, stale, fresh], 2)
        assert [t["profile_url"] for t in picked] == ["never", "stale"]

    def test_weekly_cap_excludes_a_spent_author(self):
        from cqc_lem.app.engagement.feed import select_roster_targets
        spent = _target("spent", "peer", cap=2, used=2)
        left = _target("left", "peer", cap=2, used=1)
        picked = select_roster_targets([spent, left], 5)
        assert [t["profile_url"] for t in picked] == ["left"]

    def test_a_zero_cap_pauses_the_author(self):
        # The SPA tells the operator 0 pauses an account; `cap or DEFAULT` used to read that 0 as
        # unset and give the author two comments a week instead.
        from cqc_lem.app.engagement.feed import select_roster_targets
        paused = _target("paused", "peer", cap=0, used=0)
        live = _target("live", "peer", cap=2, used=0)
        picked = select_roster_targets([paused, live], 5)
        assert [t["profile_url"] for t in picked] == ["live"]

    def test_inactive_targets_are_skipped(self):
        from cqc_lem.app.engagement.feed import select_roster_targets
        picked = select_roster_targets([_target("off", "peer", active=False)], 5)
        assert picked == []

    def test_short_buckets_spill_their_slots(self):
        # Roster is peers-only: the ICP/creator quotas must not go to waste.
        from cqc_lem.app.engagement.feed import select_roster_targets
        targets = [_target(f"peer{i}", "peer") for i in range(6)]
        assert len(select_roster_targets(targets, 5)) == 5

    def test_zero_limit_picks_nothing(self):
        from cqc_lem.app.engagement.feed import select_roster_targets
        assert select_roster_targets([_target("a")], 0) == []


class TestActivityUrl:
    def test_builds_recent_activity_url(self):
        from cqc_lem.app.engagement.feed import _roster_activity_url
        assert _roster_activity_url("https://www.linkedin.com/in/jane/") == \
            "https://www.linkedin.com/in/jane/recent-activity/all/"

    def test_keeps_an_explicit_activity_url(self):
        from cqc_lem.app.engagement.feed import _roster_activity_url
        assert _roster_activity_url("https://www.linkedin.com/in/jane/recent-activity/all") == \
            "https://www.linkedin.com/in/jane/recent-activity/all/"

    def test_blank_url_is_empty(self):
        from cqc_lem.app.engagement.feed import _roster_activity_url
        assert _roster_activity_url("  ") == ""


class TestTopicGate:
    def test_inert_without_configured_topics(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant") as classifier:
            assert passes_topic_gate("anything at all", {}) is True
        classifier.assert_not_called()  # nothing to be off-topic against — and no LLM spend

    def test_literal_focus_topic_short_circuits_the_classifier(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant") as classifier:
            assert passes_topic_gate("Our RevOps rollout went sideways",
                                     {"focus_topics": ["RevOps"]}) is True
        classifier.assert_not_called()

    def test_short_topic_inside_another_word_does_not_short_circuit(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant", return_value=False) as classifier:
            assert passes_topic_gate("How we thrive on shorter sprints",
                                     {"focus_topics": ["HR"]}) is False
        classifier.assert_called_once()  # "thrive" is not an HR mention — the classifier decides

    def test_multi_word_topic_matches_on_a_word_boundary(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant") as classifier:
            assert passes_topic_gate("Notes on our RevOps tooling stack.",
                                     {"focus_topics": ["revops tooling"]}) is True
        classifier.assert_not_called()

    def test_off_topic_post_is_rejected(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant", return_value=False):
            assert passes_topic_gate("AI in HR hiring screens", {"focus_topics": ["RevOps"]}) is False

    def test_focus_topics_win_over_include_topics(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant", return_value=True) as classifier:
            passes_topic_gate("some post", {"focus_topics": ["RevOps"], "include_topics": ["HR"]})
        assert classifier.call_args[0][1] == ["RevOps"]

    def test_falls_back_to_include_topics(self):
        from cqc_lem.app.engagement.feed import passes_topic_gate
        with patch(f"{_FEED}.post_is_relevant", return_value=True) as classifier:
            passes_topic_gate("some post", {"include_topics": ["HR tech"]})
        assert classifier.call_args[0][1] == ["HR tech"]


def _box(text):
    b = MagicMock()
    b.text = text
    return b


def _run_roster(boxes, targets, *, prefs=None, relevant=True, engage=True, max_posts=5,
                card=True, follow_budget=0, follow_outcome="", follow_on=None, follow_hold="",
                blocked_streak=1, blocked_connect="unknown", connect_outcome=None,
                deadline_ts=None):
    """Drive comment_on_roster_posts with every Selenium/DB collaborator mocked.

    `card=False` makes every item render WITHOUT a comment affordance — the restricted-comments
    signature #962 detects. `follow_budget`/`follow_outcome` drive the opt-in auto-follow lane;
    `follow_on` overrides the toggle so the budget gate and the toggle can be tested apart.
    `blocked_connect` is the connect state the blocked-visit write reports back (#979).
    """
    from cqc_lem.app.engagement import feed as ra
    from cqc_lem.utilities.db import BlockedVisit, ConnectStatus

    driver = MagicMock()
    driver.find_elements.return_value = boxes
    driver.execute_script.return_value = None  # no URN on the card -> hash key

    engage_mock = MagicMock(return_value=engage)
    record = MagicMock(return_value=True)
    blocked = MagicMock(return_value=BlockedVisit(blocked_streak, blocked_connect))
    follow = MagicMock(return_value=follow_outcome)
    reconcile = MagicMock(return_value="unknown")
    connect = MagicMock(return_value=connect_outcome
                        or ra.RosterConnectOutcome(ConnectStatus.UNKNOWN, False))
    prefs = dict(prefs or {})
    prefs.setdefault("roster_auto_follow",
                     bool(follow_budget) if follow_on is None else bool(follow_on))
    seen = set()

    with ExitStack() as es:
        p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
        p("get_engagement_targets", return_value=targets)
        p("wait_for_ajax")
        p("_card_for_textbox", side_effect=lambda d, b: MagicMock() if card else None)
        p("_post_author_from_card", return_value="Jane Author")
        p("_post_permalink_from_card", return_value=None)
        p("has_commented_post", return_value=False)
        p("has_user_commented_on_post_url", return_value=False)
        p("_passes_hard_excludes", return_value=True)
        p("post_is_relevant", return_value=relevant)
        p("_engage_card", new=engage_mock)
        p("record_target_engagement", new=record)
        p("record_target_comment_blocked", new=blocked)
        p("roster_follow_budget", return_value=follow_budget)
        p("_outbound_hold_reason", return_value=follow_hold)
        p("auto_follow_roster_target", new=follow)
        p("reconcile_roster_follow_state", new=reconcile)
        p("advance_roster_connect", new=connect)
        stats = ra.comment_on_roster_posts(
            _ctx(driver, prefs=prefs, seen=seen, deadline_ts=deadline_ts), max_posts)
    return {"stats": stats, "engage": engage_mock, "record": record, "driver": driver, "seen": seen,
            "blocked": blocked, "follow": follow, "reconcile": reconcile, "connect": connect}


class TestCommentOnRosterPosts:
    def test_empty_roster_is_a_noop(self):
        r = _run_roster([_box("A post long enough to be scanned here.")], [])
        assert r["stats"]["posted"] == 0
        assert r["stats"]["targets_visited"] == 0
        r["driver"].get.assert_not_called()  # never opens a browser page for an empty roster

    def test_visits_activity_page_and_comments(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane", "peer")])
        assert r["stats"]["posted"] == 1
        assert r["stats"]["targets_visited"] == 1
        r["driver"].get.assert_called_once_with(
            "https://www.linkedin.com/in/jane/recent-activity/all/")
        r["record"].assert_called_once_with(1, "https://www.linkedin.com/in/jane")

    def test_one_comment_per_author_per_run(self):
        # Three posts on one author's page — anti-pod means we take exactly one.
        boxes = [_box(f"Roster author post number {i}, long enough to scan.") for i in range(3)]
        r = _run_roster(boxes, [_target("https://www.linkedin.com/in/jane", "peer")])
        assert r["stats"]["posted"] == 1
        assert r["engage"].call_count == 1

    def test_off_topic_roster_post_is_never_commented_on(self):
        r = _run_roster([_box("Wholly unrelated content of sufficient length.")],
                        [_target("https://www.linkedin.com/in/jane", "peer")],
                        prefs={"focus_topics": ["RevOps"]}, relevant=False)
        assert r["stats"]["posted"] == 0
        assert r["stats"]["off_topic_skipped"] == 1
        r["engage"].assert_not_called()
        r["record"].assert_not_called()

    def test_author_at_weekly_cap_is_not_visited(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane", "peer", cap=2, used=2)])
        assert r["stats"]["targets_visited"] == 0
        r["driver"].get.assert_not_called()

    def test_seen_keys_block_the_later_feed_walk(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane", "peer")])
        assert r["seen"], "roster keys must be shared so the feed walk can't re-comment them"

    def test_page_without_commentable_cards_is_skipped(self):
        r = _run_roster([_box("An item with no comment affordance at all.")],
                        [_target("https://www.linkedin.com/in/jane")], card=False)
        assert r["stats"]["posted"] == 0
        r["engage"].assert_not_called()

    def test_deadline_stops_the_roster_pass(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.get_engagement_targets",
                   return_value=[_target("https://www.linkedin.com/in/jane")]), \
             patch(f"{_FEED}._engage_card") as engage:
            driver = MagicMock()
            stats = ra.comment_on_roster_posts(_ctx(driver, deadline_ts=1.0), 5)
        assert stats["posted"] == 0
        driver.get.assert_not_called()
        engage.assert_not_called()

    def test_navigation_failure_moves_on(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        driver.get.side_effect = Exception("auth wall")
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
            p("get_engagement_targets", return_value=[_target("https://www.linkedin.com/in/jane")])
            p("wait_for_ajax")
            p("log_warning")
            engage = es.enter_context(patch(f"{_FEED}._engage_card"))
            stats = ra.comment_on_roster_posts(_ctx(driver), 5)
        assert stats["posted"] == 0 and stats["targets_visited"] == 0
        engage.assert_not_called()

    def test_a_session_quit_out_from_under_the_walk_stops_it_without_warning(self):
        """Issue #988: a deploy quits the browser once the drain window is spent. Every remaining
        target is unreachable for that same reason, so warning per target would escalate into a
        filed defect for a routine release — the walk stops at INFO on what already shipped.
        """
        from selenium.common import InvalidSessionIdException

        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        driver.get.side_effect = InvalidSessionIdException("Unable to find session with ID: abc")
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
            p("get_engagement_targets", return_value=[_target("https://www.linkedin.com/in/jane"),
                                                      _target("https://www.linkedin.com/in/john")])
            p("wait_for_ajax")
            warned = p("log_warning")
            info = p("log_info")
            engage = p("_engage_card")
            stats = ra.comment_on_roster_posts(_ctx(driver), 5)
        assert stats["posted"] == 0 and stats["targets_visited"] == 0
        assert driver.get.call_count == 1  # the 2nd target is unreachable on a dead session
        warned.assert_not_called()
        assert info.called
        engage.assert_not_called()


def _run_feed(boxes, *, prefs=None, relevant=True, roster_stats=None, matches=True):
    """Drive comment_on_feed_inline end-to-end with the roster pass stubbed, so the assertions are
    about the feed walk's on-topic gate and the merged funnel.
    """
    from cqc_lem.app.engagement import feed as ra

    driver = MagicMock()
    driver.find_elements.return_value = boxes
    driver.execute_script.return_value = None
    engage = MagicMock(return_value=True)
    funnel = {}
    empty_roster = {"posted": 0, "targets_visited": 0, "examined": 0, "off_topic_skipped": 0,
                    "key_sources": {}, "commented_key_sources": {}}

    with ExitStack() as es:
        p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
        p("get_engagement_preferences", return_value=prefs or {"max_comments_per_day": 20})
        p("get_recent_engagers", return_value=set())
        p("get_recent_comment_texts", return_value=[])
        p("count_comments_today", return_value=0)
        p("get_or_create_profile_synthesis", return_value="synthesis")
        p("comment_on_roster_posts", return_value=roster_stats or empty_roster)
        p("navigate_to_feed")
        p("_switch_feed_to_recent")
        p("_card_for_textbox", side_effect=lambda d, b: MagicMock())
        p("_post_author_from_card", return_value="Jane Author")
        p("_post_permalink_from_card", return_value=None)
        p("has_commented_post", return_value=False)
        p("has_user_commented_on_post_url", return_value=False)
        p("_passes_hard_excludes", return_value=True)
        p("_post_age_minutes", return_value=10)
        p("_post_social_counts", return_value={"comments": 0, "reactions": 0})
        p("_literal_relevant", return_value=True)
        p("_score_feed_post", return_value=1.0)
        p("post_matches_preferences", return_value=matches)
        p("post_is_relevant", return_value=relevant)
        p("_engage_card", new=engage)
        p("set_feed_funnel", side_effect=lambda uid, f: funnel.update(f))
        posted = ra.comment_on_feed_inline(driver, MagicMock(), MagicMock(), user_id=1, max_posts=5)
    return {"posted": posted, "engage": engage, "funnel": funnel}


class TestFeedOnTopicGate:
    def test_off_topic_feed_post_is_never_commented_on(self):
        r = _run_feed([_box("AI in HR hiring screens, a post of adequate length.")],
                      prefs={"max_comments_per_day": 20, "focus_topics": ["RevOps"]},
                      relevant=False)
        assert r["posted"] == 0
        r["engage"].assert_not_called()
        assert r["funnel"]["off_topic_skipped"] == 1

    def test_fallback_cannot_comment_on_an_off_topic_post(self):
        # The empty-filter fallback widens WHICH posts qualify — it must never widen past the
        # on-topic gate, which is exactly what produced the off-ICP comments in the 2026-07 funnel.
        boxes = [_box(f"Off-topic post number {i} of adequate scanning length.") for i in range(9)]
        r = _run_feed(boxes, matches=False,
                      prefs={"max_comments_per_day": 20, "include_topics": ["RevOps"],
                             "feed_fallback_when_empty": True},
                      relevant=False)
        assert r["posted"] == 0
        r["engage"].assert_not_called()
        assert r["funnel"]["fallback_used"] is False

    def test_on_topic_post_still_gets_a_comment(self):
        r = _run_feed([_box("A RevOps pipeline post of adequate scanning length.")],
                      prefs={"max_comments_per_day": 20, "focus_topics": ["RevOps"]})
        assert r["posted"] == 1
        assert r["funnel"]["off_topic_skipped"] == 0


class TestFunnelSourceSplit:
    def test_roster_and_feed_counts_are_reported_separately(self):
        roster = {"posted": 2, "targets_visited": 3, "examined": 4, "off_topic_skipped": 1,
                  "key_sources": {"card": 4}, "commented_key_sources": {"card": 2}}
        r = _run_feed([_box("A feed post of entirely adequate scanning length.")],
                      roster_stats=roster)
        f = r["funnel"]
        assert f["commented"] == 3            # 2 roster + 1 feed
        assert f["roster_commented"] == 2 and f["feed_commented"] == 1
        assert f["roster_targets_visited"] == 3
        assert f["examined"] == 5             # 4 roster + 1 feed
        assert f["off_topic_skipped"] == 1
        assert f["key_sources"] == {"card": 4, "hash": 1}
        assert f["commented_key_sources"] == {"hash": 1, "card": 2}

    def test_roster_comments_count_against_the_run_budget(self):
        # The roster used the whole budget: the feed walk must not comment again on top of it.
        roster = {"posted": 5, "targets_visited": 5, "examined": 5, "off_topic_skipped": 0,
                  "key_sources": {}, "commented_key_sources": {}}
        r = _run_feed([_box("A feed post of entirely adequate scanning length.")],
                      roster_stats=roster)
        assert r["posted"] == 5
        r["engage"].assert_not_called()


# --- restricted-comment detection + opt-in auto-follow (issue #962) ------------------------------

class TestCommentBlockedDetection:
    def test_posts_with_no_comment_affordance_record_a_blocked_visit(self):
        r = _run_roster([_box("A post from an author who restricts commenting.")],
                        [_target("https://www.linkedin.com/in/jane")], card=False)
        assert r["stats"]["comment_blocked"] == 1
        r["blocked"].assert_called_once_with(1, "https://www.linkedin.com/in/jane")

    def test_a_page_with_no_posts_at_all_is_not_blocked(self):
        # "No posts / only reshares" says nothing about whether the author accepts comments —
        # recording it would badge people who simply have not posted.
        r = _run_roster([], [_target("https://www.linkedin.com/in/jane")], card=False)
        assert r["stats"]["comment_blocked"] == 0
        r["blocked"].assert_not_called()

    def test_short_text_nodes_are_not_counted_as_posts(self):
        r = _run_roster([_box("too short")], [_target("https://www.linkedin.com/in/jane")],
                        card=False)
        assert r["stats"]["comment_blocked"] == 0
        r["blocked"].assert_not_called()

    def test_a_commentable_page_is_never_recorded_as_blocked(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")])
        assert r["stats"]["comment_blocked"] == 0
        r["blocked"].assert_not_called()

    def test_a_walk_cut_short_by_the_deadline_never_badges_the_author(self):
        # "No card offered a comment affordance" is only evidence when the walk finished. A run that
        # ran out of time says how far WE got, not what the author allows.
        from cqc_lem.app.engagement import feed as ra
        ticks = iter([0.0])   # the target-loop check passes, the inner one is past the deadline

        def _clock():
            return next(ticks, 100.0)

        with patch(f"{_FEED}.time.time", side_effect=_clock):
            r = _run_roster([_box("A post from an author who restricts commenting.")],
                            [_target("https://www.linkedin.com/in/jane")], card=False,
                            deadline_ts=50.0)
        assert r["stats"]["comment_blocked"] == 0
        r["blocked"].assert_not_called()

    def test_every_target_reading_blocked_is_treated_as_selector_drift(self):
        # If `_card_for_textbox` breaks, EVERY target renders as restricted. Badging them all would
        # tell the user something false about other people's accounts, so nothing is written.
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        r = _run_roster([_box("A post that renders with no comment affordance.")], targets,
                        card=False, max_posts=3)
        assert r["stats"]["comment_blocked"] == 3   # still reported — the run saw it
        r["blocked"].assert_not_called()            # but nothing is persisted, and no badge appears

    def test_a_small_roster_of_restricted_authors_is_still_recorded(self):
        # Two out of two is an ordinary roster, not a broken selector.
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(2)]
        r = _run_roster([_box("A post that renders with no comment affordance.")], targets,
                        card=False, max_posts=3)
        assert r["blocked"].call_count == 2

    def test_the_badge_crossing_is_announced_exactly_once(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK as THRESHOLD
        for streak, announced in ((THRESHOLD - 1, 0), (THRESHOLD, 1), (THRESHOLD + 1, 0)):
            with patch(f"{_FEED}.log_info") as info:
                _run_roster([_box("A post from an author who restricts commenting.")],
                            [_target("https://www.linkedin.com/in/jane")], card=False,
                            blocked_streak=streak)
            assert len([c for c in info.call_args_list
                        if "un-commentable" in str(c.args[0])]) == announced

    def test_a_landed_comment_clears_the_streak_in_the_same_statement(self):
        # record_target_engagement is the ONE place the streak resets — a comment landing IS the
        # proof the target is commentable.
        from cqc_lem.utilities import db
        conn, cursor = MagicMock(), MagicMock()
        cursor.rowcount = 1
        conn.cursor.return_value = cursor
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert db.record_target_engagement(1, "https://www.linkedin.com/in/jane") is True
        sql = cursor.execute.call_args[0][0]
        assert "comment_blocked_streak = 0" in sql


class TestRosterAutoFollow:
    def test_lane_is_off_unless_the_budget_allows_it(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")], follow_on=True,
                        follow_budget=0)
        r["follow"].assert_not_called()

    def test_lane_is_off_unless_the_user_opted_in(self):
        # The budget is never even read: the toggle is what makes this an outbound lane at all.
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")], follow_on=False,
                        follow_budget=5)
        r["follow"].assert_not_called()
        r["reconcile"].assert_not_called()

    def test_follows_within_budget_and_counts_it(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")],
                        follow_budget=1, follow_outcome="followed")
        r["follow"].assert_called_once()
        assert r["stats"]["followed"] == 1

    def test_an_already_followed_target_is_never_re_examined(self):
        target = _target("https://www.linkedin.com/in/jane")
        target["follow_status"] = "following"
        r = _run_roster([_box("A roster author's post, long enough to scan.")], [target],
                        follow_budget=3)
        r["follow"].assert_not_called()
        r["reconcile"].assert_not_called()

    def test_a_failed_target_is_re_read_but_never_re_clicked(self):
        # 'follow_failed' is terminal for CLICKS only: an unverified flip may well have landed, so
        # a later visit has to be allowed to notice — read-only, and it spends no budget.
        target = _target("https://www.linkedin.com/in/jane")
        target["follow_status"] = "follow_failed"
        r = _run_roster([_box("A roster author's post, long enough to scan.")], [target],
                        follow_budget=3)
        r["follow"].assert_not_called()
        r["reconcile"].assert_called_once()

    def test_the_budget_is_re_read_per_target_not_decremented_locally(self):
        # The click is recorded on dispatch, so re-reading is what makes two overlapping runs for
        # one user share a single daily allowance instead of each spending the whole of it.
        from cqc_lem.app.engagement import feed as ra
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        with patch(f"{_FEED}.roster_follow_budget", side_effect=[2, 1, 0]) as budget, \
             patch(f"{_FEED}._outbound_hold_reason", return_value=""), \
             patch(f"{_FEED}.auto_follow_roster_target", return_value="followed") as follow, \
             patch(f"{_FEED}.get_engagement_targets", return_value=targets), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.record_target_comment_blocked", return_value=1), \
             patch(f"{_FEED}._card_for_textbox", return_value=None):
            driver = MagicMock()
            driver.find_elements.return_value = [_box("A roster author's post, long enough.")]
            stats = ra.comment_on_roster_posts(
                _ctx(driver, prefs={"roster_auto_follow": True}), 3)
        assert budget.call_count == 3      # asked again for every target, never cached
        assert follow.call_count == 2      # the third read says the allowance is spent
        assert stats["followed"] == 2


    def test_a_hold_is_announced_once_per_run_not_once_per_target(self):
        # A pause or an open breaker is a fact about the account, not about each roster target —
        # one INFO per target is exactly the repeated-expected-condition noise #962 argues against.
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        with patch(f"{_FEED}.log_info") as info:
            _run_roster([_box("A roster author's post, long enough to scan.")], targets,
                        follow_budget=3, max_posts=3, follow_hold="automation paused")
        assert len([c for c in info.call_args_list
                    if "standing down" in str(c.args[0])]) == 1


class TestReconcileRosterFollowState:
    _TARGET = {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane"}

    def test_a_card_that_now_reads_following_clears_the_failure(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}._resolve_follow_control", return_value=("following", None)), \
             patch(f"{_FEED}.set_target_follow_status", return_value=True) as st:
            assert ra.reconcile_roster_follow_state(MagicMock(), 1, self._TARGET) == "following"
        st.assert_called_once_with(1, "https://www.linkedin.com/in/jane", "following")

    def test_anything_else_leaves_the_record_alone_and_clicks_nothing(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        for state in ("not_following", "unknown"):
            with patch(f"{_FEED}._resolve_follow_control", return_value=(state, MagicMock())), \
                 patch(f"{_FEED}.set_target_follow_status") as st, \
                 patch(f"{_FEED}.record_action") as rec:
                assert ra.reconcile_roster_follow_state(driver, 1, self._TARGET) == state
            st.assert_not_called()
            rec.assert_not_called()
        driver.execute_script.assert_not_called()


class TestRosterFollowBudget:
    def test_zero_when_the_toggle_is_off(self):
        from cqc_lem.app.engagement.feed import roster_follow_budget
        assert roster_follow_budget(1, {"max_follows_per_day": 5}) == 0

    def test_zero_when_the_cap_is_zero(self):
        from cqc_lem.app.engagement.feed import roster_follow_budget
        assert roster_follow_budget(1, {"roster_auto_follow": True,
                                        "max_follows_per_day": 0}) == 0

    def test_draws_its_own_paced_budget_not_the_comment_lane_s(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.human_pacing import ACTION_FOLLOW
        with patch(f"{_FEED}.remaining_actions", return_value=2) as remaining, \
             patch(f"{_FEED}.actions_used_today", return_value=1):
            assert ra.roster_follow_budget(7, {"roster_auto_follow": True,
                                               "max_follows_per_day": 4}) == 2
        assert remaining.call_args[0][1] == ACTION_FOLLOW
        assert remaining.call_args[0][2] == 4        # the follow cap, not max_comments_per_day
        assert remaining.call_args[1]["caps"] is not None  # still bounded by the account envelope

    def test_a_missing_cap_falls_back_to_the_conservative_default(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ROSTER_FOLLOWS_PER_DAY_DEFAULT
        with patch(f"{_FEED}.remaining_actions", return_value=9) as remaining, \
             patch(f"{_FEED}.actions_used_today", return_value=0):
            ra.roster_follow_budget(7, {"roster_auto_follow": True})
        assert remaining.call_args[0][2] == ROSTER_FOLLOWS_PER_DAY_DEFAULT


def _follow_env(state_before, state_after=None, hold="", flip_on_attempt=None):
    """Patch stack for auto_follow_roster_target: the resolver returns `state_before`, then
    `state_after` on every post-click re-read — the verification POLLS, so the after-state has to
    answer more than once. `flip_on_attempt` makes it read 'following' only from that poll onward,
    which is the render race the polling exists for.
    """
    es = ExitStack()
    p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
    control = MagicMock()
    reads = iter([(state_before, control if state_before == "not_following" else None)])
    after_calls = {"n": 0}

    def _resolve(*_a, **_kw):
        try:
            return next(reads)
        except StopIteration:
            pass
        after_calls["n"] += 1
        if flip_on_attempt is not None:
            return ("following" if after_calls["n"] >= flip_on_attempt else "not_following"), None
        return (state_after, None)

    p("_resolve_follow_control", side_effect=_resolve)
    p("_outbound_hold_reason", return_value=hold)
    return es, {
        "set": p("set_target_follow_status", return_value=True),
        "fail": p("record_target_follow_failure", return_value=1),
        "record": p("record_action"),
    }


class TestAutoFollowRosterTarget:
    def _target_dict(self):
        return {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane"}

    def test_an_already_following_card_is_recorded_without_a_click(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        es, m = _follow_env("following")
        with es:
            assert ra.auto_follow_roster_target(driver, 1, self._target_dict()) == "already_following"
        m["set"].assert_called_once_with(1, "https://www.linkedin.com/in/jane", "following")
        driver.execute_script.assert_not_called()
        m["record"].assert_not_called()   # a catch-up spends no daily budget

    def test_a_verified_flip_records_following_and_spends_the_budget(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.human_pacing import ACTION_FOLLOW
        es, m = _follow_env("not_following", "following")
        with es:
            assert ra.auto_follow_roster_target(MagicMock(), 1, self._target_dict()) == "followed"
        m["set"].assert_called_once_with(1, "https://www.linkedin.com/in/jane", "following")
        m["fail"].assert_not_called()
        m["record"].assert_called_once_with(1, ACTION_FOLLOW)

    def test_an_unverified_flip_counts_as_a_failed_attempt(self):
        # Writing 'following' on an unverified click is the one failure that never self-corrects:
        # the status is terminal, so the target would never be looked at again.
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.human_pacing import ACTION_FOLLOW
        es, m = _follow_env("not_following", "not_following")
        with es:
            assert ra.auto_follow_roster_target(MagicMock(), 1, self._target_dict()) == "failed"
        m["fail"].assert_called_once_with(1, "https://www.linkedin.com/in/jane")
        assert all(c.args[2] != "following" for c in m["set"].call_args_list)
        # The click still went to LinkedIn, so it still costs the day's allowance — otherwise a lane
        # whose verification broke would be free to click every target on the roster.
        m["record"].assert_called_once_with(1, ACTION_FOLLOW)

    def test_a_slow_re_render_is_polled_for_rather_than_called_a_failure(self):
        # LinkedIn REPLACES the top card after a follow; losing that race once used to cost the
        # target a failed attempt it never earned, and two of those retire it for good.
        from cqc_lem.app.engagement import feed as ra
        es, m = _follow_env("not_following", flip_on_attempt=3)
        with es:
            assert ra.auto_follow_roster_target(MagicMock(), 1, self._target_dict()) == "followed"
        m["set"].assert_called_once_with(1, "https://www.linkedin.com/in/jane", "following")
        m["fail"].assert_not_called()

    def test_an_unreadable_control_clicks_nothing(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        es, m = _follow_env("unknown")
        with es:
            assert ra.auto_follow_roster_target(driver, 1, self._target_dict()) == ""
        driver.execute_script.assert_not_called()
        m["fail"].assert_not_called()

    def test_a_hard_gate_stands_the_lane_down(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        es, m = _follow_env("not_following", "following", hold="LinkedIn 429 breaker open")
        with es:
            assert ra.auto_follow_roster_target(driver, 1, self._target_dict()) == ""
        driver.execute_script.assert_not_called()
        m["set"].assert_not_called()


class TestOutboundHoldReason:
    def test_a_paused_account_holds_follows(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.is_automation_paused", return_value=True), \
             patch(f"{_FEED}.automation_pause_reason", return_value="suppression"):
            assert ra._outbound_hold_reason(1) == "suppression"

    def test_an_open_breaker_holds_follows(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.is_automation_paused", return_value=False), \
             patch(f"{_FEED}.rate_limit_cooldown_remaining", return_value=900):
            assert ra._outbound_hold_reason(1) == "LinkedIn 429 breaker open"

    def test_clear_gates_return_no_reason(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.is_automation_paused", return_value=False), \
             patch(f"{_FEED}.rate_limit_cooldown_remaining", return_value=0):
            assert ra._outbound_hold_reason(1) == ""


class TestResolveFollowControl:
    """The live probe for PR #963 showed the resolver MUST anchor on the page owner's name — the
    only stable discriminator between the top-card control and a feed card author's Follow.
    """

    _URL = "https://www.linkedin.com/in/arvidkahl/"

    def _driver(self, title="(8) Activity | Arvid Kahl | LinkedIn", result=None, raises=False):
        driver = MagicMock()
        driver.title = title
        if raises:
            driver.execute_script.side_effect = RuntimeError("boom")
        else:
            driver.execute_script.return_value = result
        return driver

    def test_owner_name_comes_from_the_page_title(self):
        from cqc_lem.app.engagement import feed as ra
        control = MagicMock()
        driver = self._driver(result=["not_following", control])
        assert ra._resolve_follow_control(driver, self._URL) == ("not_following", control)
        # slug and the title-derived name are what the JS anchors on
        assert driver.execute_script.call_args[0][1] == "arvidkahl"
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"

    def test_title_beats_the_freehand_roster_name(self):
        # The title and the aria-labels are written from the same display name; a roster row's
        # stored name is user-typed and may not match, so it is only the fallback.
        from cqc_lem.app.engagement import feed as ra
        driver = self._driver(result=["following", MagicMock()])
        ra._resolve_follow_control(driver, self._URL, name="Arvid Kahl Verified Profile 1st")
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"

    def test_roster_name_is_the_fallback_when_the_title_is_unreadable(self):
        from cqc_lem.app.engagement import feed as ra
        driver = self._driver(title="LinkedIn", result=["following", MagicMock()])
        ra._resolve_follow_control(driver, self._URL, name="Arvid Kahl")
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"

    def test_no_owner_name_is_unknown_and_never_scans(self):
        # No name = nothing to anchor the label match on = fail closed BEFORE touching the page.
        from cqc_lem.app.engagement import feed as ra
        driver = self._driver(title="LinkedIn")
        assert ra._resolve_follow_control(driver, self._URL) == ("unknown", None)
        driver.execute_script.assert_not_called()

    def test_a_js_error_is_unknown(self):
        from cqc_lem.app.engagement import feed as ra
        assert ra._resolve_follow_control(self._driver(raises=True), self._URL) == ("unknown", None)

    def test_a_malformed_result_is_unknown(self):
        from cqc_lem.app.engagement import feed as ra
        assert ra._resolve_follow_control(self._driver(result="following"), self._URL) == ("unknown", None)

    def test_an_unrecognized_state_is_unknown(self):
        from cqc_lem.app.engagement import feed as ra
        driver = self._driver(result=["followed?", MagicMock()])
        assert ra._resolve_follow_control(driver, self._URL) == ("unknown", None)


class TestActivityPageOwnerName:
    def test_reads_the_middle_segment_of_the_title(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        driver.title = "(8) Activity | Arvid Kahl | LinkedIn"
        assert ra._activity_page_owner_name(driver) == "Arvid Kahl"

    def test_a_rotated_title_shape_reads_as_no_name(self):
        from cqc_lem.app.engagement import feed as ra
        for title in ("LinkedIn", "", "Arvid Kahl", "Activity |  | LinkedIn"):
            driver = MagicMock()
            driver.title = title
            assert ra._activity_page_owner_name(driver) == ""

    def test_an_unreadable_title_reads_as_no_name(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock()
        type(driver).title = property(lambda self: (_ for _ in ()).throw(RuntimeError("stale")))
        assert ra._activity_page_owner_name(driver) == ""


# --- connect escalation when following didn't unblock commenting (issue #979) --------------------

class TestConnectEscalationAnnouncement:
    def _target(self, connect_status="unknown"):
        t = _target("https://www.linkedin.com/in/jane")
        t["connect_status"] = connect_status
        return t

    def test_the_crossing_is_announced_when_the_escalation_fires(self):
        with patch(f"{_FEED}.log_info") as info:
            _run_roster([_box("A post from an author who restricts commenting.")],
                        [self._target()], card=False, blocked_connect="needs_connection")
        assert len([c for c in info.call_args_list
                    if "flagged for a connection request" in str(c.args[0])]) == 1

    def test_a_target_already_waiting_says_so_only_once(self):
        # The escalation only ever fires on the transition out of 'unknown'; a target that has been
        # waiting for weeks must not re-announce it on every rotation.
        with patch(f"{_FEED}.log_info") as info:
            _run_roster([_box("A post from an author who restricts commenting.")],
                        [self._target("needs_connection")], card=False,
                        blocked_connect="needs_connection")
        assert not [c for c in info.call_args_list
                    if "flagged for a connection request" in str(c.args[0])]

    def test_an_un_escalated_blocked_visit_announces_nothing(self):
        with patch(f"{_FEED}.log_info") as info:
            _run_roster([_box("A post from an author who restricts commenting.")],
                        [self._target()], card=False, blocked_connect="unknown")
        assert not [c for c in info.call_args_list
                    if "flagged for a connection request" in str(c.args[0])]

    def test_the_rung_runs_for_every_visited_target_regardless_of_the_follow_toggle(self):
        # Read-only advancement is free and must not be gated on auto-follow: a user who connected
        # by hand has to see the badge clear.
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")], follow_on=False)
        r["connect"].assert_called_once()

    def test_a_queued_invite_is_counted_on_the_funnel(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")],
                        connect_outcome=ra.RosterConnectOutcome(ConnectStatus.REQUESTED, True))
        assert r["stats"]["connect_requested"] == 1

    def test_an_invite_someone_else_sent_is_never_claimed_by_the_run(self):
        # 'requested' read off the card (the user invited them by hand) is not a send this run made.
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")],
                        connect_outcome=ra.RosterConnectOutcome(ConnectStatus.REQUESTED, False))
        assert r["stats"]["connect_requested"] == 0


class TestAdvanceRosterConnect:
    def _target(self, status):
        return {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane",
                "connect_status": status}

    def _run(self, status, read="unknown", prefs=None, queued=True):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.reconcile_roster_connect_state", return_value=read) as reconcile, \
             patch(f"{_FEED}.queue_roster_connect_invite", return_value=queued) as queue:
            outcome = ra.advance_roster_connect(MagicMock(), 1, self._target(status),
                                                prefs or {"roster_auto_connect": True})
        return outcome, reconcile, queue

    def test_an_un_escalated_target_is_never_even_read(self):
        # 'unknown' means the escalation has not fired — reading the card would spend a JS
        # round-trip per target per run for a state with nothing to advance.
        from cqc_lem.utilities.db import ConnectStatus
        outcome, reconcile, queue = self._run("unknown")
        assert outcome == (ConnectStatus.UNKNOWN, False)
        reconcile.assert_not_called()
        queue.assert_not_called()

    def test_a_connected_target_is_never_read_or_re_invited(self):
        outcome, reconcile, queue = self._run("connected")
        assert outcome.invited is False
        reconcile.assert_not_called()
        queue.assert_not_called()

    def test_a_failed_target_is_still_re_read_but_never_re_invited(self):
        # Terminal means no more SENDS, not no more reading — the same rule
        # `reconcile_roster_follow_state` follows for 'follow_failed' (#962). A user who connected
        # by hand must not keep a badge saying the request failed.
        outcome, reconcile, queue = self._run("failed", read="connected")
        reconcile.assert_called_once()
        queue.assert_not_called()
        assert outcome == ("connected", False)

    def test_a_needs_connection_target_invites_once(self):
        from cqc_lem.utilities.db import ConnectStatus
        outcome, reconcile, queue = self._run("needs_connection", read="needs_connection")
        assert outcome == (ConnectStatus.REQUESTED, True)
        queue.assert_called_once()

    def test_a_card_that_already_reads_connected_never_draws_an_invite(self):
        from cqc_lem.utilities.db import ConnectStatus
        outcome, _, queue = self._run("needs_connection", read=ConnectStatus.CONNECTED)
        assert outcome == (ConnectStatus.CONNECTED, False)
        queue.assert_not_called()

    def test_a_requested_target_is_advanced_but_never_re_invited(self):
        # One shot per target: the request is already out, and LinkedIn's withdraw/expire cycle
        # governs it from here.
        outcome, reconcile, queue = self._run("requested", read="requested")
        reconcile.assert_called_once()
        queue.assert_not_called()
        assert outcome.invited is False

    def test_a_refused_queue_is_not_reported_as_an_invite(self):
        from cqc_lem.utilities.db import ConnectStatus
        outcome, _, _ = self._run("needs_connection", read="needs_connection", queued=False)
        assert outcome == (ConnectStatus.NEEDS_CONNECTION, False)


class TestReconcileRosterConnectState:
    _TARGET = {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane",
               "connect_status": "needs_connection"}

    def test_a_pending_control_advances_the_state_for_free(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        driver = MagicMock()
        with patch(f"{_FEED}._resolve_connect_state", return_value=ConnectStatus.REQUESTED), \
             patch(f"{_FEED}.set_target_connect_status", return_value=True) as st:
            assert ra.reconcile_roster_connect_state(driver, 1, self._TARGET) == "requested"
        st.assert_called_once_with(1, "https://www.linkedin.com/in/jane", ConnectStatus.REQUESTED)

    def test_a_first_degree_card_finishes_the_ladder(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        with patch(f"{_FEED}._resolve_connect_state", return_value=ConnectStatus.CONNECTED), \
             patch(f"{_FEED}.set_target_connect_status", return_value=True) as st:
            assert ra.reconcile_roster_connect_state(MagicMock(), 1, self._TARGET) == "connected"
        st.assert_called_once_with(1, "https://www.linkedin.com/in/jane", ConnectStatus.CONNECTED)

    def test_an_unreadable_card_leaves_the_record_alone(self):
        # 'unknown' means we could not tell, never that the invite vanished.
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        with patch(f"{_FEED}._resolve_connect_state", return_value=ConnectStatus.UNKNOWN), \
             patch(f"{_FEED}.set_target_connect_status") as st:
            assert ra.reconcile_roster_connect_state(MagicMock(), 1, self._TARGET) == \
                "needs_connection"
        st.assert_not_called()

    def test_the_ladder_never_walks_backwards(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        target = dict(self._TARGET, connect_status="connected")
        with patch(f"{_FEED}._resolve_connect_state", return_value=ConnectStatus.REQUESTED), \
             patch(f"{_FEED}.set_target_connect_status") as st:
            assert ra.reconcile_roster_connect_state(MagicMock(), 1, target) == "connected"
        st.assert_not_called()


class TestRosterConnectBudget:
    def test_zero_when_the_toggle_is_off(self):
        from cqc_lem.app.engagement.feed import roster_connect_budget
        assert roster_connect_budget(1, {"max_invites_per_day": 10}) == 0

    def test_zero_when_the_account_has_no_invite_cap(self):
        from cqc_lem.app.engagement.feed import roster_connect_budget
        assert roster_connect_budget(1, {"roster_auto_connect": True,
                                         "max_invites_per_day": 0}) == 0

    def test_takes_at_most_a_minority_share_of_what_is_left(self):
        # #398's profile-viewer and proactive lanes must never be starved by a roster of restricted
        # authors, so the ladder gets a third of the remaining budget, rounded up.
        from cqc_lem.app.engagement import feed as ra
        for remaining, share in ((9, 3), (7, 3), (3, 1), (2, 1), (1, 1), (0, 0)):
            with patch(f"{_FEED}.remaining_actions", return_value=remaining), \
                 patch(f"{_FEED}.count_invites_sent_today", return_value=0), \
                 patch(f"{_FEED}.count_open_connection_requests", return_value=0):
                assert ra.roster_connect_budget(1, {"roster_auto_connect": True,
                                                    "max_invites_per_day": 10}) == share

    def test_it_spends_the_shared_invite_budget_not_a_lane_of_its_own(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.human_pacing import ACTION_INVITE
        with patch(f"{_FEED}.remaining_actions", return_value=6) as remaining, \
             patch(f"{_FEED}.count_invites_sent_today", return_value=2), \
             patch(f"{_FEED}.count_open_connection_requests", return_value=1):
            ra.roster_connect_budget(1, {"roster_auto_connect": True, "max_invites_per_day": 10})
        assert remaining.call_args[0][1] == ACTION_INVITE
        assert remaining.call_args[0][2] == 10
        # Queued-but-unsent requests count as spent: they take tomorrow's cap the moment it opens.
        assert remaining.call_args[0][3] == 3
        assert remaining.call_args[1]["caps"] is not None


def _queue_env(*, budget=2, hold="", terminal=False, auto_connect=True):
    es = ExitStack()
    p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
    target = {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane",
              "connect_status": "requested" if terminal else "needs_connection"}
    mocks = {
        "set": p("set_target_connect_status", return_value=True),
        "task": p("send_roster_connect_invite"),
        "note": p("_roster_connect_note", return_value="Hi Jane, I read your posts."),
    }
    p("roster_connect_budget", return_value=budget)
    p("_outbound_hold_reason", return_value=hold)
    return es, target, mocks, {"roster_auto_connect": auto_connect}


class TestQueueRosterConnectInvite:
    def test_the_one_shot_is_recorded_before_the_send_is_dispatched(self):
        # A dispatch that is lost, or a worker that dies mid-send, must not leave the target
        # eligible for a second invite on the next rotation.
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        order = []
        es, target, m, prefs = _queue_env()
        with es:
            m["set"].side_effect = lambda *a, **k: order.append("set") or True
            m["task"].apply_async.side_effect = lambda *a, **k: order.append("dispatch")
            assert ra.queue_roster_connect_invite(1, target, prefs) is True
        assert order == ["set", "dispatch"]
        m["set"].assert_called_once_with(1, "https://www.linkedin.com/in/jane",
                                         ConnectStatus.REQUESTED)

    def test_it_goes_out_through_the_existing_invite_rail(self):
        from cqc_lem.app.engagement import feed as ra
        es, target, m, prefs = _queue_env()
        with es:
            ra.queue_roster_connect_invite(1, target, prefs)
        kwargs = m["task"].apply_async.call_args[1]["kwargs"]
        assert kwargs["profile_url"] == "https://www.linkedin.com/in/jane"
        assert kwargs["message"] == "Hi Jane, I read your posts."

    def test_the_toggle_is_what_makes_this_an_outbound_lane(self):
        from cqc_lem.app.engagement import feed as ra
        es, target, m, prefs = _queue_env(auto_connect=False)
        with es:
            assert ra.queue_roster_connect_invite(1, target, prefs) is False
        m["task"].apply_async.assert_not_called()
        m["set"].assert_not_called()

    def test_a_target_already_invited_is_never_invited_again(self):
        from cqc_lem.app.engagement import feed as ra
        es, target, m, prefs = _queue_env(terminal=True)
        with es:
            assert ra.queue_roster_connect_invite(1, target, prefs) is False
        m["task"].apply_async.assert_not_called()

    def test_a_hard_gate_stands_the_lane_down(self):
        from cqc_lem.app.engagement import feed as ra
        es, target, m, prefs = _queue_env(hold="LinkedIn 429 breaker open")
        with es:
            assert ra.queue_roster_connect_invite(1, target, prefs) is False
        m["task"].apply_async.assert_not_called()
        m["set"].assert_not_called()

    def test_no_budget_share_means_no_invite(self):
        from cqc_lem.app.engagement import feed as ra
        es, target, m, prefs = _queue_env(budget=0)
        with es:
            assert ra.queue_roster_connect_invite(1, target, prefs) is False
        m["task"].apply_async.assert_not_called()

    def test_invites_already_dispatched_this_run_count_against_the_share(self):
        # The send is asynchronous, so nothing durable records the invite until the task reaches
        # LinkedIn — re-reading alone would hand every target in the walk the same "2 left".
        from cqc_lem.app.engagement import feed as ra
        es, target, m, prefs = _queue_env(budget=2)
        with es:
            assert ra.queue_roster_connect_invite(1, target, prefs, queued_this_run=1) is True
            assert ra.queue_roster_connect_invite(1, target, prefs, queued_this_run=2) is False
        assert m["task"].apply_async.call_count == 1


class TestConnectRungBudgetAcrossOneWalk:
    def test_one_walk_can_never_invite_more_than_the_day_s_share(self):
        # The regression this exists for: with the budget re-read per target and nothing durable
        # recording an in-flight dispatch, a roster of restricted authors would all be invited in a
        # single pass.
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import BlockedVisit
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(5)]
        for t in targets:
            t["connect_status"] = "needs_connection"
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
            p("get_engagement_targets", return_value=targets)
            p("wait_for_ajax")
            p("_card_for_textbox", return_value=None)
            p("record_target_comment_blocked", return_value=BlockedVisit(1, "needs_connection"))
            p("reconcile_roster_connect_state", return_value="needs_connection")
            p("roster_connect_budget", return_value=2)   # a third of six left, re-read every time
            p("_outbound_hold_reason", return_value="")
            p("set_target_connect_status", return_value=True)
            p("_roster_connect_note", return_value="note")
            task = p("send_roster_connect_invite")
            driver = MagicMock()
            driver.find_elements.return_value = [_box("A post with no comment affordance at all.")]
            stats = ra.comment_on_roster_posts(
                _ctx(driver, prefs={"roster_auto_connect": True}), 5)
        assert task.apply_async.call_count == 2
        assert stats["connect_requested"] == 2


class TestSendRosterConnectInvite:
    _URL = "https://www.linkedin.com/in/jane"

    def _send(self, result=None, raises=None):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_INV}.invite_to_connect_now",
                   side_effect=raises, return_value=result) as rail, \
             patch(f"{_INV}.set_target_connect_status") as st, \
             patch(f"{_INV}.log_warning"):
            out = ra.send_roster_connect_invite(1, self._URL, "note")
        return out, st, rail

    def test_a_sent_invite_leaves_the_one_shot_state_alone(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        out, st, _ = self._send(result=(True, CONNECTION_REQUEST_SENT_MESSAGE))
        assert out == CONNECTION_REQUEST_SENT_MESSAGE
        st.assert_not_called()   # already 'requested' before the dispatch

    def test_a_real_failure_is_terminal_and_never_auto_retried(self):
        from cqc_lem.utilities.db import ConnectStatus
        _, st, _ = self._send(result=(False, "no Connect button"))
        st.assert_called_once_with(1, self._URL, ConnectStatus.FAILED)

    def test_an_already_connected_profile_records_the_truth(self):
        from cqc_lem.utilities.db import ALREADY_CONNECTED_MESSAGE, ConnectStatus
        _, st, _ = self._send(result=(False, ALREADY_CONNECTED_MESSAGE))
        st.assert_called_once_with(1, self._URL, ConnectStatus.CONNECTED)

    def test_a_throttled_send_hands_the_target_back_to_the_ladder(self):
        # Nothing reached LinkedIn, so the one shot was not spent.
        from cqc_lem.utilities.db import ConnectStatus
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        _, st, _ = self._send(raises=LinkedInRateLimited("cooldown"))
        st.assert_called_once_with(1, self._URL, ConnectStatus.NEEDS_CONNECTION)


class TestResolveConnectState:
    _URL = "https://www.linkedin.com/in/arvidkahl/"

    def _driver(self, title="(8) Activity | Arvid Kahl | LinkedIn", result=None, raises=False):
        driver = MagicMock()
        driver.title = title
        if raises:
            driver.execute_script.side_effect = RuntimeError("boom")
        else:
            driver.execute_script.return_value = result
        return driver

    def test_the_two_readable_states_come_back_as_members(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        for raw, expected in (("requested", ConnectStatus.REQUESTED),
                              ("connected", ConnectStatus.CONNECTED)):
            assert ra._resolve_connect_state(self._driver(result=raw), self._URL) == expected

    def test_anything_else_is_unknown(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        for raw in ("unknown", "needs_connection", "", None, ["connected"]):
            assert ra._resolve_connect_state(self._driver(result=raw), self._URL) == \
                ConnectStatus.UNKNOWN

    def test_no_owner_name_never_even_scans(self):
        # Same rule as the follow control: no name = nothing to anchor the label match on.
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        driver = self._driver(title="LinkedIn")
        assert ra._resolve_connect_state(driver, self._URL) == ConnectStatus.UNKNOWN
        driver.execute_script.assert_not_called()

    def test_a_js_error_is_unknown(self):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        assert ra._resolve_connect_state(self._driver(raises=True), self._URL) == \
            ConnectStatus.UNKNOWN

    def test_it_anchors_on_the_slug_and_the_page_owner(self):
        from cqc_lem.app.engagement import feed as ra
        driver = self._driver(result="connected")
        ra._resolve_connect_state(driver, self._URL, name="Freehand Name")
        assert driver.execute_script.call_args[0][1] == "arvidkahl"
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"


class TestConnectStateShortenedLabels:
    """The reading itself only runs in a real browser, so what a unit test can hold is the two
    invariants that make the shortened-label path safe. Both exist because the live run grounded
    "Message Harshal" — a first name, not the display name the strict matcher wanted.
    """

    def test_a_shortened_label_is_read_only_inside_the_owner_card(self):
        from cqc_lem.app.engagement.feed import _CONNECT_STATE_JS
        assert "'message ' + FIRST" in _CONNECT_STATE_JS
        used = [line for line in _CONNECT_STATE_JS.splitlines()
                if "shortened(" in line and "const shortened" not in line]
        assert used, "the shortened-label path is gone"
        for line in used:
            assert "ownerCard(" in line, f"shortened label read page-wide: {line.strip()}"

    def test_pending_is_never_read_off_a_shortened_label(self):
        # A wrong `requested` freezes the ladder; a wrong `unknown` only stalls it.
        from cqc_lem.app.engagement.feed import _CONNECT_STATE_JS
        line = next(l for l in _CONNECT_STATE_JS.splitlines() if "pending = true" in l)
        assert "named &&" in line


class TestALandedCommentStandsTheRungDown:
    """The seam between the comment walk and the connect rung. `record_target_engagement` stands a
    pending escalation down in the DB, but the rung reads the row the run loaded BEFORE the comment
    landed — so without the in-memory stand-down the same pass would invite an account it had just
    successfully commented on, and burn that target's one shot forever.
    """

    def _walk(self, engaged: bool):
        from cqc_lem.app.engagement import feed as ra
        from cqc_lem.utilities.db import ConnectStatus
        target = _target("https://www.linkedin.com/in/jane")
        target["connect_status"] = "needs_connection"
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
            p("get_engagement_targets", return_value=[target])
            p("wait_for_ajax")
            p("_card_for_textbox", return_value=MagicMock())
            p("_post_author_from_card", return_value="Jane Author")
            p("_post_permalink_from_card", return_value=None)
            p("has_commented_post", return_value=False)
            p("has_user_commented_on_post_url", return_value=False)
            p("_passes_hard_excludes", return_value=True)
            p("post_is_relevant", return_value=True)
            p("_engage_card", return_value=engaged)
            p("record_target_engagement", return_value=True)
            p("roster_follow_budget", return_value=0)
            p("_outbound_hold_reason", return_value="")
            p("reconcile_roster_connect_state",
              return_value=ConnectStatus.NEEDS_CONNECTION)
            queue = p("queue_roster_connect_invite", return_value=True)
            driver = MagicMock()
            driver.find_elements.return_value = [_box("A roster author's post, long enough to scan.")]
            driver.execute_script.return_value = None
            stats = ra.comment_on_roster_posts(
                _ctx(driver, prefs={"roster_auto_connect": True}), 5)
        return stats, queue, target

    def test_a_target_we_just_commented_on_is_never_invited(self):
        stats, queue, target = self._walk(engaged=True)
        queue.assert_not_called()
        assert stats["connect_requested"] == 0
        assert target["connect_status"] == "unknown"

    def test_a_target_we_could_not_comment_on_still_climbs_the_ladder(self):
        # The stand-down must not disarm the rung itself — nothing landed here.
        stats, queue, _ = self._walk(engaged=False)
        queue.assert_called_once()
        assert stats["connect_requested"] == 1
