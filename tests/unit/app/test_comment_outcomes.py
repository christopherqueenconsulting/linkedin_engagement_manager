"""Unit tests for comment outcome tracking — issue #628. Covers the sort-control reading, our-comment
matching, the reply/like/author-reply extraction with a mocked driver, the graceful skip paths, the
sweep orchestration, the weekly quality report + commenting hold, and the live-validation probe's
verdict. Selenium DOM targeting itself is grounded on a supervised live run (the #403/#404 pattern,
`scripts/linkedin_live_validation.py --comment-outcome-url`)."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

RA = "cqc_lem.app.run_automation"
RS = "cqc_lem.app.run_scheduler"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{RA}.time.sleep", lambda *a, **k: None):
        yield


def _fn(name):
    import importlib
    return getattr(importlib.import_module(RA), name)


def _p(es, name, **kw):
    return es.enter_context(patch(f"{RA}.{name}", **kw))


class TestCommentTextMatches:
    def test_matches_a_truncated_render(self):
        f = _fn("_comment_text_matches")
        logged = ("This is exactly the kind of drift I have watched eat a quarter of a team's "
                  "throughput before anyone noticed.")
        rendered = "This is exactly the kind of drift I have watched eat a…more"
        assert f(rendered, logged) is True

    def test_matches_regardless_of_case_and_whitespace(self):
        assert _fn("_comment_text_matches")("  Nice   POINT about latency  ",
                                            "nice point about latency") is True

    def test_different_comments_do_not_match(self):
        assert _fn("_comment_text_matches")("Totally different opening line here",
                                            "Nothing like the other one at all") is False

    def test_empty_never_matches(self):
        f = _fn("_comment_text_matches")
        assert f("", "something") is False
        assert f("something", "") is False
        assert f(None, None) is False


class TestCommentSortLabel:
    def _btn(self, aria="", text=""):
        b = MagicMock()
        b.get_attribute.return_value = aria
        b.text = text
        return b

    def test_reads_most_relevant(self):
        with patch(f"{RA}.find_first", return_value=self._btn(text="Most relevant")):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == "most relevant"

    def test_reads_most_recent_from_aria_label(self):
        btn = self._btn(aria="Sort comments by, Most recent is currently selected")
        with patch(f"{RA}.find_first", return_value=btn):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == "most recent"

    def test_missing_control_is_empty_not_a_guess(self):
        with patch(f"{RA}.find_first", return_value=None):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == ""

    def test_unrecognized_label_is_empty(self):
        with patch(f"{RA}.find_first", return_value=self._btn(text="Sort by")):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == ""


class TestSwitchCommentSort:
    def test_true_only_when_the_control_confirms_the_new_sort(self):
        with patch(f"{RA}.find_first", side_effect=[MagicMock(), MagicMock()]), \
             patch(f"{RA}._comment_sort_label", return_value="most recent"):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is True

    def test_false_when_the_sort_did_not_actually_change(self):
        with patch(f"{RA}.find_first", side_effect=[MagicMock(), MagicMock()]), \
             patch(f"{RA}._comment_sort_label", return_value="most relevant"):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is False

    def test_false_when_no_control(self):
        with patch(f"{RA}.find_first", return_value=None):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is False

    def test_false_when_the_menu_option_is_missing(self):
        with patch(f"{RA}.find_first", side_effect=[MagicMock(), None]):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is False


def _item(text, author, name="cont"):
    tb = MagicMock(); tb.text = text
    return (tb, MagicMock(name=name), author)


class TestFindOurComment:
    def test_matches_our_comment_by_text(self):
        items = [_item("Someone else entirely", "https://www.linkedin.com/in/glenda/"),
                 _item("Latency is the tell here", "https://www.linkedin.com/in/me/")]
        assert _fn("_find_our_comment")(items, "me", "Latency is the tell here") is items[1][1]

    def test_falls_back_to_our_only_comment_when_text_drifted(self):
        items = [_item("@Glenda Smith latency is the tell", "https://www.linkedin.com/in/me/")]
        assert _fn("_find_our_comment")(items, "me", "Latency is the tell here") is items[0][1]

    def test_no_fallback_when_several_of_ours_are_present(self):
        items = [_item("first of ours", "https://www.linkedin.com/in/me/"),
                 _item("second of ours", "https://www.linkedin.com/in/me/")]
        assert _fn("_find_our_comment")(items, "me", "nothing alike over here") is None

    def test_none_when_we_authored_nothing(self):
        items = [_item("theirs", "https://www.linkedin.com/in/glenda/")]
        assert _fn("_find_our_comment")(items, "me", "ours") is None

    def test_none_without_a_slug(self):
        assert _fn("_find_our_comment")([_item("x", "y")], "", "x") is None


class TestCommentLikeCount:
    def test_parses_the_reactions_control(self):
        driver = MagicMock(); driver.execute_script.return_value = "1.2K"
        assert _fn("_comment_like_count")(driver, MagicMock()) == 1200

    def test_zero_when_unreadable(self):
        driver = MagicMock(); driver.execute_script.side_effect = RuntimeError("boom")
        assert _fn("_comment_like_count")(driver, MagicMock()) == 0


class TestThreadReplies:
    def test_only_nested_containers_count(self):
        ours = MagicMock(name="ours")
        nested = MagicMock(name="nested")
        outside = MagicMock(name="outside")
        items = [(MagicMock(), ours, "https://www.linkedin.com/in/me/"),
                 (MagicMock(), nested, "https://www.linkedin.com/in/glenda/"),
                 (MagicMock(), outside, "https://www.linkedin.com/in/other/")]
        driver = MagicMock()
        driver.execute_script.side_effect = lambda _js, a, b: b is nested
        out = _fn("_thread_replies")(driver, ours, items)
        assert [a for _c, a in out] == ["https://www.linkedin.com/in/glenda/"]


def _outcome_env(es, items, sort_label="most relevant", switched=False, items_after=None,
                 author_href="https://www.linkedin.com/in/authorperson/", like_count=0):
    driver = MagicMock()
    seq = [items] + ([items_after] if items_after is not None else [])
    _p(es, "_load_comment_thread")
    _p(es, "_comment_items", side_effect=seq if len(seq) > 1 else (lambda _d: items))
    _p(es, "_comment_sort_label", return_value=sort_label)
    _p(es, "_switch_comment_sort", return_value=switched)
    _p(es, "_post_author_href", return_value=author_href)
    _p(es, "_comment_like_count", return_value=like_count)
    return driver


class TestReadCommentOutcome:
    def test_visible_under_most_relevant_with_author_reply(self):
        ours = MagicMock(name="ours")
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, ours, "https://www.linkedin.com/in/me/"),
                 (MagicMock(), MagicMock(), "https://www.linkedin.com/in/authorperson/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items, like_count=4)
            _p(es, "_thread_replies",
               return_value=[(items[1][1], "https://www.linkedin.com/in/authorperson/")])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["status"] == "checked"
        assert out["visible_most_relevant"] is True
        assert out["author_replied"] is True
        assert out["reply_count"] == 1
        assert out["like_count"] == 4
        assert out["our_reply_sent"] is False

    def test_our_own_reply_is_not_counted_as_a_reply_to_us(self):
        ours = MagicMock(name="ours")
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, ours, "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items)
            _p(es, "_thread_replies", return_value=[(MagicMock(), "https://www.linkedin.com/in/me/")])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["reply_count"] == 0 and out["our_reply_sent"] is True
        assert out["author_replied"] is False

    def test_absent_from_relevant_but_present_in_recent_is_a_demotion(self):
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        after = [(our_tb, MagicMock(name="ours"), "https://www.linkedin.com/in/me/")]
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, switched=True, items_after=after)
            _p(es, "_thread_replies", return_value=[])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["visible_most_relevant"] is False and out["status"] == "checked"

    def test_missing_in_both_sorts_skips_with_a_reason(self):
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, switched=True, items_after=theirs)
            _p(es, "log_info")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert out["status"] == "skipped" and out["skip_reason"] == "comment-not-found"
        assert out["visible_most_relevant"] is None
        assert out["reply_count"] == 0 and out["like_count"] == 0

    def test_deleted_or_private_post_renders_no_comments(self):
        with ExitStack() as es:
            driver = _outcome_env(es, [], sort_label="", switched=False)
            _p(es, "log_info")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert out["status"] == "skipped" and out["skip_reason"] == "post-unavailable"
        assert out["visible_most_relevant"] is None

    def test_unknown_sort_leaves_visibility_null_even_when_found(self):
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, MagicMock(), "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items, sort_label="")
            _p(es, "_thread_replies", return_value=[])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        # We read the outcome, but we cannot claim anything about 'Most relevant' visibility.
        assert out["status"] == "checked" and out["visible_most_relevant"] is None

    def test_failed_sort_switch_never_reads_as_a_demotion(self):
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, switched=False)
            _p(es, "log_info")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert out["visible_most_relevant"] is None and out["status"] == "skipped"


class TestSweepOrchestration:
    def _driver_patches(self, es):
        profile = MagicMock(); profile.profile_url = "https://www.linkedin.com/in/me/"
        _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile))
        _p(es, "quit_gracefully")
        _p(es, "acquire_run_lock", return_value="tok")
        _p(es, "release_run_lock")
        return profile

    def test_no_targets_short_circuits_before_a_browser(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets", return_value=[])
            gcp = _p(es, "get_current_profile")
            assert "No comments due" in _run_comment_outcomes_sweep(1)
        assert not gcp.called

    def test_records_and_tracks_each_outcome(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        targets = [{"log_id": 11, "post_url": "feedurn://urn:li:activity:1", "message": "a"},
                   {"log_id": 12, "post_url": "feedurn://urn:li:activity:2", "message": "b"}]
        with ExitStack() as es:
            self._driver_patches(es)
            _p(es, "get_comment_outcome_targets", return_value=targets)
            _p(es, "_read_comment_outcome",
               side_effect=[{"status": "checked", "skip_reason": None, "author_replied": True,
                             "reply_count": 1, "like_count": 2, "visible_most_relevant": True,
                             "our_reply_sent": False},
                            {"status": "skipped", "skip_reason": "comment-not-found",
                             "author_replied": False, "reply_count": 0, "like_count": 0,
                             "visible_most_relevant": None, "our_reply_sent": False}])
            rec = _p(es, "record_comment_outcome", return_value=True)
            trk = _p(es, "track_comment_outcome")
            result = _run_comment_outcomes_sweep(1)
        assert "checked 1" in result and "skipped 1" in result
        assert rec.call_count == 2 and trk.call_count == 2
        assert rec.call_args_list[0].kwargs["visible_most_relevant"] is True
        assert rec.call_args_list[1].kwargs["skip_reason"] == "comment-not-found"

    def test_unnavigable_key_is_skipped_without_a_row(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        with ExitStack() as es:
            self._driver_patches(es)
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 9, "post_url": "feedpost://hash", "message": "a"}])
            read = _p(es, "_read_comment_outcome")
            rec = _p(es, "record_comment_outcome")
            _run_comment_outcomes_sweep(1)
        assert not read.called and not rec.called

    def test_one_failing_post_does_not_abort_the_sweep(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        targets = [{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"},
                   {"log_id": 2, "post_url": "feedurn://urn:li:activity:2", "message": "b"}]
        with ExitStack() as es:
            self._driver_patches(es)
            _p(es, "get_comment_outcome_targets", return_value=targets)
            _p(es, "log_warning")
            _p(es, "_read_comment_outcome",
               side_effect=[RuntimeError("stale"),
                            {"status": "checked", "skip_reason": None, "author_replied": False,
                             "reply_count": 0, "like_count": 0, "visible_most_relevant": True,
                             "our_reply_sent": False}])
            rec = _p(es, "record_comment_outcome", return_value=True)
            _p(es, "track_comment_outcome")
            result = _run_comment_outcomes_sweep(1)
        assert rec.call_count == 1 and "checked 1" in result

    def test_lock_contention_skips(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"}])
            _p(es, "acquire_run_lock", return_value=None)
            gcp = _p(es, "get_current_profile")
            assert "another outcome sweep" in _run_comment_outcomes_sweep(1)
        assert not gcp.called

    def test_rate_limited_session_skips_cleanly(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"}])
            _p(es, "acquire_run_lock", return_value="tok")
            rel = _p(es, "release_run_lock")
            _p(es, "log_warning")
            _p(es, "get_current_profile", side_effect=LinkedInRateLimited("429"))
            assert "rate limited" in _run_comment_outcomes_sweep(1)
        assert rel.called

    def test_missing_profile_slug_aborts_before_reading(self):
        from cqc_lem.app.run_automation import _run_comment_outcomes_sweep
        profile = MagicMock(); profile.profile_url = "https://www.linkedin.com/"
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"}])
            _p(es, "acquire_run_lock", return_value="tok")
            _p(es, "release_run_lock")
            _p(es, "quit_gracefully")
            _p(es, "log_warning")
            _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile))
            read = _p(es, "_read_comment_outcome")
            assert "no profile slug" in _run_comment_outcomes_sweep(1)
        assert not read.called


class TestCommentingHoldGate:
    def test_held_user_never_opens_a_browser(self):
        from cqc_lem.app.run_automation import automate_commenting
        with ExitStack() as es:
            _p(es, "is_commenting_held", return_value=True)
            _p(es, "commenting_hold_reason", return_value="80% demoted")
            _p(es, "log_warning")
            lock = _p(es, "acquire_run_lock")
            gcp = _p(es, "get_current_profile")
            result = automate_commenting.run(user_id=1)
        assert "held" in result and "80% demoted" in result
        assert not lock.called and not gcp.called

    def test_unheld_user_proceeds(self):
        from cqc_lem.app.run_automation import automate_commenting
        with ExitStack() as es:
            _p(es, "is_commenting_held", return_value=False)
            _p(es, "acquire_run_lock", return_value="tok")
            _p(es, "release_run_lock")
            _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock()))
            _p(es, "navigate_to_feed")
            _p(es, "comment_on_feed_inline", return_value=2)
            _p(es, "quit_gracefully")
            result = automate_commenting.run(user_id=1)
        assert "Commented on 2 posts" in result


class TestWeeklyQualityReport:
    def _rows(self, n, visible):
        return [{"status": "checked", "author_replied": 0, "reply_count": 0, "like_count": 0,
                 "visible_most_relevant": visible, "our_reply_sent": 0} for _ in range(n)]

    def _run(self, es, rows, users=(1,)):
        from cqc_lem.app.run_scheduler import auto_weekly_comment_quality
        es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=list(users)))
        es.enter_context(patch("cqc_lem.utilities.db.get_comment_outcomes", return_value=rows))
        hold = es.enter_context(
            patch("cqc_lem.utilities.linkedin.rate_limit.hold_commenting", return_value=True))
        track = es.enter_context(
            patch("cqc_lem.utilities.observability.track_comment_quality"))
        crit = es.enter_context(patch("cqc_lem.utilities.logger.log_critical"))
        return auto_weekly_comment_quality(days=7), hold, track, crit

    def test_healthy_user_is_reported_but_not_held(self):
        with ExitStack() as es:
            result, hold, track, _crit = self._run(es, self._rows(12, 1))
        assert "1/1" in result and "0 held" in result
        assert track.called and not hold.called

    def test_demoted_user_is_held_and_escalated(self):
        with ExitStack() as es:
            result, hold, _track, crit = self._run(es, self._rows(12, 0))
        assert "1 held" in result
        assert hold.called and crit.called
        assert hold.call_args.args[0] == 1

    def test_thin_sample_is_never_held(self):
        with ExitStack() as es:
            result, hold, track, _crit = self._run(es, self._rows(3, 0))
        assert "0 held" in result and track.called and not hold.called

    def test_user_with_no_readings_is_not_reported(self):
        with ExitStack() as es:
            result, hold, track, _crit = self._run(es, [])
        assert "0/1" in result and not track.called and not hold.called

    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import auto_weekly_comment_quality
        with patch(f"{RS}.get_active_user_ids", return_value=[]):
            assert auto_weekly_comment_quality() == "No active users"


class TestOutcomeDispatcher:
    def test_throttled_dispatcher_sends_nothing(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        with patch(f"{RS}._skip_if_throttled", return_value=True):
            assert dispatch_comment_outcome_sweeps() == "Automation throttled"

    def test_dispatches_only_users_with_a_session_and_a_due_interval(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        client = MagicMock(); client.set.side_effect = [True, False]
        with ExitStack() as es:
            es.enter_context(patch(f"{RS}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1, 2, 3]))
            es.enter_context(patch(f"{RS}.has_linkedin_session", side_effect=lambda u: u != 3))
            es.enter_context(patch("cqc_lem.utilities.linkedin.rate_limit._redis_client",
                                   return_value=client))
            es.enter_context(patch(f"{RS}.dispatch_jitter_seconds", return_value=5))
            apply = es.enter_context(patch(f"{RS}.sweep_comment_outcomes.apply_async"))
            result = dispatch_comment_outcome_sweeps()
        assert apply.call_count == 1  # user 1 due, user 2 already swept, user 3 has no session
        assert "1/3" in result

    def test_no_redis_still_dispatches(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        with ExitStack() as es:
            es.enter_context(patch(f"{RS}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1]))
            es.enter_context(patch(f"{RS}.has_linkedin_session", return_value=True))
            es.enter_context(patch("cqc_lem.utilities.linkedin.rate_limit._redis_client",
                                   return_value=None))
            es.enter_context(patch(f"{RS}.dispatch_jitter_seconds", return_value=5))
            apply = es.enter_context(patch(f"{RS}.sweep_comment_outcomes.apply_async"))
            dispatch_comment_outcome_sweeps()
        assert apply.call_count == 1


class TestLiveValidationVerdict:
    def _llv(self):
        import importlib.util
        from pathlib import Path
        script = Path(__file__).resolve().parents[3] / "scripts" / "linkedin_live_validation.py"
        spec = importlib.util.spec_from_file_location("linkedin_live_validation_628", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_visible(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": True}) == "visible"

    def test_demoted(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": False,
             "switched_to_recent": True, "found_most_recent": True}) == "demoted"

    def test_no_sort_control_is_ambiguous(self):
        assert self._llv().comment_outcome_verdict({"sort_control_found": False}).startswith("ambiguous")

    def test_failed_switch_is_ambiguous_not_demoted(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": False,
             "switched_to_recent": False}) == "ambiguous: could not switch sort"

    def test_absent_from_both_is_ambiguous(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": False,
             "switched_to_recent": True, "found_most_recent": False}).startswith("ambiguous")
