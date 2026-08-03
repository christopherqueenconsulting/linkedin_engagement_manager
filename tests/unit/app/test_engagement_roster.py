"""Unit tests for the target-creator engagement roster (issue #616): 50/30/20 selection, the
per-author weekly cap, the on-topic gate, and the roster-vs-feed funnel diagnostics."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from contextlib import ExitStack

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


def _target(url, category="peer", *, last=None, cap=2, used=0, active=True, name=None):
    return {"id": abs(hash(url)) % 10000, "profile_url": url, "name": name or url,
            "category": category, "max_comments_per_week": cap, "active": active,
            "last_engaged_at": last, "comments_this_week": used, "source": "user"}


class TestSelectRosterTargets:
    def test_blends_fifty_thirty_twenty(self):
        from cqc_lem.app.run_automation import select_roster_targets
        targets = ([_target(f"peer{i}", "peer") for i in range(10)]
                   + [_target(f"icp{i}", "icp") for i in range(10)]
                   + [_target(f"cre{i}", "creator") for i in range(10)])
        picked = select_roster_targets(targets, 10)
        assert len(picked) == 10
        by_cat = {c: sum(1 for t in picked if t["category"] == c) for c in ("peer", "icp", "creator")}
        assert by_cat == {"peer": 5, "icp": 3, "creator": 2}

    def test_never_engaged_targets_come_first(self):
        from cqc_lem.app.run_automation import select_roster_targets
        recent = _target("recent", "peer", last=datetime.now())
        stale = _target("stale", "peer", last=datetime.now() - timedelta(days=9))
        fresh = _target("never", "peer", last=None)
        picked = select_roster_targets([recent, stale, fresh], 2)
        assert [t["profile_url"] for t in picked] == ["never", "stale"]

    def test_weekly_cap_excludes_a_spent_author(self):
        from cqc_lem.app.run_automation import select_roster_targets
        spent = _target("spent", "peer", cap=2, used=2)
        left = _target("left", "peer", cap=2, used=1)
        picked = select_roster_targets([spent, left], 5)
        assert [t["profile_url"] for t in picked] == ["left"]

    def test_a_zero_cap_pauses_the_author(self):
        # The SPA tells the operator 0 pauses an account; `cap or DEFAULT` used to read that 0 as
        # unset and give the author two comments a week instead.
        from cqc_lem.app.run_automation import select_roster_targets
        paused = _target("paused", "peer", cap=0, used=0)
        live = _target("live", "peer", cap=2, used=0)
        picked = select_roster_targets([paused, live], 5)
        assert [t["profile_url"] for t in picked] == ["live"]

    def test_inactive_targets_are_skipped(self):
        from cqc_lem.app.run_automation import select_roster_targets
        picked = select_roster_targets([_target("off", "peer", active=False)], 5)
        assert picked == []

    def test_short_buckets_spill_their_slots(self):
        # Roster is peers-only: the ICP/creator quotas must not go to waste.
        from cqc_lem.app.run_automation import select_roster_targets
        targets = [_target(f"peer{i}", "peer") for i in range(6)]
        assert len(select_roster_targets(targets, 5)) == 5

    def test_zero_limit_picks_nothing(self):
        from cqc_lem.app.run_automation import select_roster_targets
        assert select_roster_targets([_target("a")], 0) == []


class TestActivityUrl:
    def test_builds_recent_activity_url(self):
        from cqc_lem.app.run_automation import _roster_activity_url
        assert _roster_activity_url("https://www.linkedin.com/in/jane/") == \
            "https://www.linkedin.com/in/jane/recent-activity/all/"

    def test_keeps_an_explicit_activity_url(self):
        from cqc_lem.app.run_automation import _roster_activity_url
        assert _roster_activity_url("https://www.linkedin.com/in/jane/recent-activity/all") == \
            "https://www.linkedin.com/in/jane/recent-activity/all/"

    def test_blank_url_is_empty(self):
        from cqc_lem.app.run_automation import _roster_activity_url
        assert _roster_activity_url("  ") == ""


class TestTopicGate:
    def test_inert_without_configured_topics(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant") as classifier:
            assert passes_topic_gate("anything at all", {}) is True
        classifier.assert_not_called()  # nothing to be off-topic against — and no LLM spend

    def test_literal_focus_topic_short_circuits_the_classifier(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant") as classifier:
            assert passes_topic_gate("Our RevOps rollout went sideways",
                                     {"focus_topics": ["RevOps"]}) is True
        classifier.assert_not_called()

    def test_short_topic_inside_another_word_does_not_short_circuit(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant", return_value=False) as classifier:
            assert passes_topic_gate("How we thrive on shorter sprints",
                                     {"focus_topics": ["HR"]}) is False
        classifier.assert_called_once()  # "thrive" is not an HR mention — the classifier decides

    def test_multi_word_topic_matches_on_a_word_boundary(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant") as classifier:
            assert passes_topic_gate("Notes on our RevOps tooling stack.",
                                     {"focus_topics": ["revops tooling"]}) is True
        classifier.assert_not_called()

    def test_off_topic_post_is_rejected(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant", return_value=False):
            assert passes_topic_gate("AI in HR hiring screens", {"focus_topics": ["RevOps"]}) is False

    def test_focus_topics_win_over_include_topics(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant", return_value=True) as classifier:
            passes_topic_gate("some post", {"focus_topics": ["RevOps"], "include_topics": ["HR"]})
        assert classifier.call_args[0][1] == ["RevOps"]

    def test_falls_back_to_include_topics(self):
        from cqc_lem.app.run_automation import passes_topic_gate
        with patch(f"{_RA}.post_is_relevant", return_value=True) as classifier:
            passes_topic_gate("some post", {"include_topics": ["HR tech"]})
        assert classifier.call_args[0][1] == ["HR tech"]


def _box(text):
    b = MagicMock()
    b.text = text
    return b


def _run_roster(boxes, targets, *, prefs=None, relevant=True, engage=True, max_posts=5,
                card=True, follow_budget=0, follow_outcome=""):
    """Drive comment_on_roster_posts with every Selenium/DB collaborator mocked.

    `card=False` makes every item render WITHOUT a comment affordance — the restricted-comments
    signature #962 detects. `follow_budget`/`follow_outcome` drive the opt-in auto-follow lane."""
    from cqc_lem.app import run_automation as ra

    driver = MagicMock()
    driver.find_elements.return_value = boxes
    driver.execute_script.return_value = None  # no URN on the card -> hash key

    engage_mock = MagicMock(return_value=engage)
    record = MagicMock(return_value=True)
    blocked = MagicMock(return_value=1)
    follow = MagicMock(return_value=follow_outcome)
    seen = set()

    with ExitStack() as es:
        p = lambda name, **kw: es.enter_context(patch(f"{_RA}.{name}", **kw))
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
        p("auto_follow_roster_target", new=follow)
        stats = ra.comment_on_roster_posts(driver, MagicMock(), MagicMock(), 1, max_posts,
                                           prefs or {}, "synthesis", [], seen)
    return {"stats": stats, "engage": engage_mock, "record": record, "driver": driver, "seen": seen,
            "blocked": blocked, "follow": follow}


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
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.get_engagement_targets",
                   return_value=[_target("https://www.linkedin.com/in/jane")]), \
             patch(f"{_RA}._engage_card") as engage:
            driver = MagicMock()
            stats = ra.comment_on_roster_posts(driver, MagicMock(), MagicMock(), 1, 5, {},
                                               "synthesis", [], set(), deadline_ts=1.0)
        assert stats["posted"] == 0
        driver.get.assert_not_called()
        engage.assert_not_called()

    def test_navigation_failure_moves_on(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        driver.get.side_effect = Exception("auth wall")
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_RA}.{name}", **kw))
            p("get_engagement_targets", return_value=[_target("https://www.linkedin.com/in/jane")])
            p("wait_for_ajax")
            p("log_warning")
            engage = es.enter_context(patch(f"{_RA}._engage_card"))
            stats = ra.comment_on_roster_posts(driver, MagicMock(), MagicMock(), 1, 5, {},
                                               "synthesis", [], set())
        assert stats["posted"] == 0 and stats["targets_visited"] == 0
        engage.assert_not_called()


def _run_feed(boxes, *, prefs=None, relevant=True, roster_stats=None, matches=True):
    """Drive comment_on_feed_inline end-to-end with the roster pass stubbed, so the assertions are
    about the feed walk's on-topic gate and the merged funnel."""
    from cqc_lem.app import run_automation as ra

    driver = MagicMock()
    driver.find_elements.return_value = boxes
    driver.execute_script.return_value = None
    engage = MagicMock(return_value=True)
    funnel = {}
    empty_roster = {"posted": 0, "targets_visited": 0, "examined": 0, "off_topic_skipped": 0,
                    "key_sources": {}, "commented_key_sources": {}}

    with ExitStack() as es:
        p = lambda name, **kw: es.enter_context(patch(f"{_RA}.{name}", **kw))
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

    def test_a_landed_comment_clears_the_streak_in_the_same_statement(self):
        # record_target_engagement is the ONE place the streak resets — a comment landing IS the
        # proof the target is commentable.
        from cqc_lem.utilities import db
        conn, cursor = MagicMock(), MagicMock()
        cursor.rowcount = 1
        conn.cursor.return_value = cursor
        with patch.object(db, "get_db_connection", return_value=conn):
            assert db.record_target_engagement(1, "https://www.linkedin.com/in/jane") is True
        sql = cursor.execute.call_args[0][0]
        assert "comment_blocked_streak = 0" in sql


class TestRosterAutoFollow:
    def test_lane_is_off_unless_the_budget_allows_it(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")], follow_budget=0)
        r["follow"].assert_not_called()

    def test_follows_within_budget_and_counts_it(self):
        r = _run_roster([_box("A roster author's post, long enough to scan.")],
                        [_target("https://www.linkedin.com/in/jane")],
                        follow_budget=1, follow_outcome="followed")
        r["follow"].assert_called_once()
        assert r["stats"]["followed"] == 1

    def test_budget_is_spent_across_targets(self):
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        r = _run_roster([_box("A roster author's post, long enough to scan.")], targets,
                        follow_budget=2, follow_outcome="followed", max_posts=3)
        assert r["follow"].call_count == 2
        assert r["stats"]["followed"] == 2

    def test_a_terminal_follow_status_is_never_re_examined(self):
        target = _target("https://www.linkedin.com/in/jane")
        target["follow_status"] = "following"
        r = _run_roster([_box("A roster author's post, long enough to scan.")], [target],
                        follow_budget=3)
        r["follow"].assert_not_called()

    def test_a_failed_click_still_spends_the_budget(self):
        # Otherwise a run where the control stopped flipping keeps clicking Follow on every target
        # it visits — the cap exists to bound CLICKS, not just successes.
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        r = _run_roster([_box("A roster author's post, long enough to scan.")], targets,
                        follow_budget=1, follow_outcome="failed", max_posts=3)
        assert r["follow"].call_count == 1
        assert r["stats"]["followed"] == 0

    def test_a_target_with_no_follow_control_does_not_spend_the_budget(self):
        # Nothing was clicked, so nothing was spent — plenty of profiles simply expose no control.
        targets = [_target(f"https://www.linkedin.com/in/p{i}") for i in range(3)]
        r = _run_roster([_box("A roster author's post, long enough to scan.")], targets,
                        follow_budget=1, follow_outcome="", max_posts=3)
        assert r["follow"].call_count == 3
        assert r["stats"]["followed"] == 0


class TestRosterFollowBudget:
    def test_zero_when_the_toggle_is_off(self):
        from cqc_lem.app.run_automation import roster_follow_budget
        assert roster_follow_budget(1, {"max_follows_per_day": 5}) == 0

    def test_zero_when_the_cap_is_zero(self):
        from cqc_lem.app.run_automation import roster_follow_budget
        assert roster_follow_budget(1, {"roster_auto_follow": True,
                                        "max_follows_per_day": 0}) == 0

    def test_draws_its_own_paced_budget_not_the_comment_lane_s(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.human_pacing import ACTION_FOLLOW
        with patch(f"{_RA}.remaining_actions", return_value=2) as remaining, \
             patch(f"{_RA}.actions_used_today", return_value=1):
            assert ra.roster_follow_budget(7, {"roster_auto_follow": True,
                                               "max_follows_per_day": 4}) == 2
        assert remaining.call_args[0][1] == ACTION_FOLLOW
        assert remaining.call_args[0][2] == 4        # the follow cap, not max_comments_per_day
        assert remaining.call_args[1]["caps"] is not None  # still bounded by the account envelope

    def test_a_missing_cap_falls_back_to_the_conservative_default(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.db import ROSTER_FOLLOWS_PER_DAY_DEFAULT
        with patch(f"{_RA}.remaining_actions", return_value=9) as remaining, \
             patch(f"{_RA}.actions_used_today", return_value=0):
            ra.roster_follow_budget(7, {"roster_auto_follow": True})
        assert remaining.call_args[0][2] == ROSTER_FOLLOWS_PER_DAY_DEFAULT


def _follow_env(state_before, state_after=None, hold=""):
    """Patch stack for auto_follow_roster_target: the resolver returns `state_before`, then
    `state_after` on the post-click re-read."""
    es = ExitStack()
    p = lambda name, **kw: es.enter_context(patch(f"{_RA}.{name}", **kw))
    control = MagicMock()
    states = [(state_before, control if state_before == "not_following" else None)]
    if state_after is not None:
        states.append((state_after, None))
    p("_resolve_follow_control", side_effect=states)
    p("_follow_hold_reason", return_value=hold)
    return es, {
        "set": p("set_target_follow_status", return_value=True),
        "fail": p("record_target_follow_failure", return_value=1),
        "record": p("record_action"),
    }


class TestAutoFollowRosterTarget:
    def _target_dict(self):
        return {"profile_url": "https://www.linkedin.com/in/jane", "name": "Jane"}

    def test_an_already_following_card_is_recorded_without_a_click(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        es, m = _follow_env("following")
        with es:
            assert ra.auto_follow_roster_target(driver, 1, self._target_dict()) == "already_following"
        m["set"].assert_called_once_with(1, "https://www.linkedin.com/in/jane", "following")
        driver.execute_script.assert_not_called()
        m["record"].assert_not_called()   # a catch-up spends no daily budget

    def test_a_verified_flip_records_following_and_spends_the_budget(self):
        from cqc_lem.app import run_automation as ra
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
        from cqc_lem.app import run_automation as ra
        es, m = _follow_env("not_following", "not_following")
        with es:
            assert ra.auto_follow_roster_target(MagicMock(), 1, self._target_dict()) == "failed"
        m["fail"].assert_called_once_with(1, "https://www.linkedin.com/in/jane")
        assert all(c.args[2] != "following" for c in m["set"].call_args_list)
        m["record"].assert_not_called()

    def test_an_unreadable_control_clicks_nothing(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        es, m = _follow_env("unknown")
        with es:
            assert ra.auto_follow_roster_target(driver, 1, self._target_dict()) == ""
        driver.execute_script.assert_not_called()
        m["fail"].assert_not_called()

    def test_a_hard_gate_stands_the_lane_down(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        es, m = _follow_env("not_following", "following", hold="LinkedIn 429 breaker open")
        with es:
            assert ra.auto_follow_roster_target(driver, 1, self._target_dict()) == ""
        driver.execute_script.assert_not_called()
        m["set"].assert_not_called()


class TestFollowHoldReason:
    def test_a_paused_account_holds_follows(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.is_automation_paused", return_value=True), \
             patch(f"{_RA}.automation_pause_reason", return_value="suppression"):
            assert ra._follow_hold_reason(1) == "suppression"

    def test_an_open_breaker_holds_follows(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.is_automation_paused", return_value=False), \
             patch(f"{_RA}.rate_limit_cooldown_remaining", return_value=900):
            assert ra._follow_hold_reason(1) == "LinkedIn 429 breaker open"

    def test_clear_gates_return_no_reason(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.is_automation_paused", return_value=False), \
             patch(f"{_RA}.rate_limit_cooldown_remaining", return_value=0):
            assert ra._follow_hold_reason(1) == ""


class TestResolveFollowControl:
    """The live probe for PR #963 showed the resolver MUST anchor on the page owner's name — the
    only stable discriminator between the top-card control and a feed card author's Follow."""

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
        from cqc_lem.app import run_automation as ra
        control = MagicMock()
        driver = self._driver(result=["not_following", control])
        assert ra._resolve_follow_control(driver, self._URL) == ("not_following", control)
        # slug and the title-derived name are what the JS anchors on
        assert driver.execute_script.call_args[0][1] == "arvidkahl"
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"

    def test_title_beats_the_freehand_roster_name(self):
        # The title and the aria-labels are written from the same display name; a roster row's
        # stored name is user-typed and may not match, so it is only the fallback.
        from cqc_lem.app import run_automation as ra
        driver = self._driver(result=["following", MagicMock()])
        ra._resolve_follow_control(driver, self._URL, name="Arvid Kahl Verified Profile 1st")
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"

    def test_roster_name_is_the_fallback_when_the_title_is_unreadable(self):
        from cqc_lem.app import run_automation as ra
        driver = self._driver(title="LinkedIn", result=["following", MagicMock()])
        ra._resolve_follow_control(driver, self._URL, name="Arvid Kahl")
        assert driver.execute_script.call_args[0][2] == "Arvid Kahl"

    def test_no_owner_name_is_unknown_and_never_scans(self):
        # No name = nothing to anchor the label match on = fail closed BEFORE touching the page.
        from cqc_lem.app import run_automation as ra
        driver = self._driver(title="LinkedIn")
        assert ra._resolve_follow_control(driver, self._URL) == ("unknown", None)
        driver.execute_script.assert_not_called()

    def test_a_js_error_is_unknown(self):
        from cqc_lem.app import run_automation as ra
        assert ra._resolve_follow_control(self._driver(raises=True), self._URL) == ("unknown", None)

    def test_a_malformed_result_is_unknown(self):
        from cqc_lem.app import run_automation as ra
        assert ra._resolve_follow_control(self._driver(result="following"), self._URL) == ("unknown", None)

    def test_an_unrecognized_state_is_unknown(self):
        from cqc_lem.app import run_automation as ra
        driver = self._driver(result=["followed?", MagicMock()])
        assert ra._resolve_follow_control(driver, self._URL) == ("unknown", None)


class TestActivityPageOwnerName:
    def test_reads_the_middle_segment_of_the_title(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        driver.title = "(8) Activity | Arvid Kahl | LinkedIn"
        assert ra._activity_page_owner_name(driver) == "Arvid Kahl"

    def test_a_rotated_title_shape_reads_as_no_name(self):
        from cqc_lem.app import run_automation as ra
        for title in ("LinkedIn", "", "Arvid Kahl", "Activity |  | LinkedIn"):
            driver = MagicMock()
            driver.title = title
            assert ra._activity_page_owner_name(driver) == ""

    def test_an_unreadable_title_reads_as_no_name(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        type(driver).title = property(lambda self: (_ for _ in ()).throw(RuntimeError("stale")))
        assert ra._activity_page_owner_name(driver) == ""
