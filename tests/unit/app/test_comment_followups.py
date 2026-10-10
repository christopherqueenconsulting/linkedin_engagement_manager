"""Unit tests for the auto-follow-up feature — issue #478. Covers URL/URN derivation, question
detection, stable reply keys, the DOM helpers + worker (mocked driver), the sweep/single/reconcile
orchestration, and the guest-voice reply generator. Selenium DOM targeting itself is validated on a
supervised live run.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The follow-up sweep moved to `app.engagement.posting` (#1154) — that is the module whose globals
# the worker, the reconcile walk and the activity scrape read, so it is where they are patched.
POST = "cqc_lem.app.engagement.posting"
# The composer/comment DOM helpers moved down to `utilities/linkedin/composer.py` (#1154)
# and took their imports with them, so a collaborator THEY read has to be patched there —
# patching it on either app module rebinds a name they never look at.
COMPOSER = "cqc_lem.utilities.linkedin.composer"


@pytest.fixture(autouse=True)
def _no_sleep():
    # The worker's scroll/pacing loops call time.sleep; skip the real waits in unit tests.
    with patch(f"{POST}.time.sleep", lambda *a, **k: None):
        yield


def _fn(name):
    import importlib
    return getattr(importlib.import_module(POST), name)


class TestPostUrlFromKey:
    def test_feedurn_becomes_navigable_url(self):
        f = _fn("_post_url_from_key")
        assert f("feedurn://urn:li:activity:7486451907129958400") == \
            "https://www.linkedin.com/feed/update/urn:li:activity:7486451907129958400/"

    def test_legacy_hash_key_is_not_navigable(self):
        assert _fn("_post_url_from_key")("feedpost://abc123") is None

    def test_passthrough_http(self):
        assert _fn("_post_url_from_key")("https://x/y").startswith("https://")

    def test_none_and_garbage(self):
        f = _fn("_post_url_from_key")
        assert f(None) is None
        assert f("feedurn://not-a-urn") is None


class TestReplyIsQuestion:
    def test_question_mark_is_a_question(self):
        assert _fn("_reply_is_question")("Interesting — how do you handle scale?") is True

    def test_statement_is_not(self):
        assert _fn("_reply_is_question")("Great point, totally agree.") is False

    def test_url_query_string_does_not_count(self):
        assert _fn("_reply_is_question")("see https://x.com/a?b=1 for more") is False

    def test_empty(self):
        assert _fn("_reply_is_question")("") is False


class TestFollowupReplyKey:
    def test_stable_across_whitespace_and_case(self):
        f = _fn("_followup_reply_key")
        a = f("feedurn://urn:li:activity:1", "https://www.linkedin.com/in/jane-doe/", "Nice!  …see more")
        b = f("feedurn://urn:li:activity:1", "https://www.linkedin.com/in/jane-doe/?x=1", "nice!")
        assert a == b  # same replier + same normalized text -> one key

    def test_namespaced_to_post_and_replier(self):
        f = _fn("_followup_reply_key")
        k = f("feedurn://urn:li:activity:1", "https://www.linkedin.com/in/jane-doe/", "hi")
        assert k.startswith("feedurn://urn:li:activity:1#reply:jane-doe:")

    def test_different_repliers_differ(self):
        f = _fn("_followup_reply_key")
        assert f("p", "https://www.linkedin.com/in/a/", "hi") != \
               f("p", "https://www.linkedin.com/in/b/", "hi")


from contextlib import ExitStack


def _p(es, name, **kw):
    return es.enter_context(patch(f"{POST}.{name}", **kw))


class TestReactToCommentInline:
    def test_likes_when_button_present(self):
        from cqc_lem.app.engagement.posting import _react_to_comment_inline
        btn = MagicMock(); btn.get_attribute.return_value = None; btn.size = {"width": 20, "height": 20}
        comment = MagicMock(); comment.find_elements.return_value = [btn]
        driver = MagicMock()
        with patch(f"{POST}.ActionChains") as AC:
            AC.return_value.move_to_element.return_value.pause.return_value.click.return_value.perform.return_value = None
            AC.return_value.move_to_element.return_value.pause.return_value.perform.return_value = None
            assert _react_to_comment_inline(driver, MagicMock(), comment, user_id=1) is True

    def test_skips_when_already_reacted(self):
        from cqc_lem.app.engagement.posting import _react_to_comment_inline
        btn = MagicMock(); btn.get_attribute.return_value = "true"  # aria-pressed
        comment = MagicMock(); comment.find_elements.return_value = [btn]
        with patch(f"{POST}.ActionChains"):
            assert _react_to_comment_inline(MagicMock(), MagicMock(), comment, user_id=1) is False

    def test_returns_false_and_logs_when_no_button(self):
        from cqc_lem.app.engagement.posting import _react_to_comment_inline
        comment = MagicMock(); comment.find_elements.return_value = []
        with patch(f"{POST}.ActionChains"), patch(f"{POST}.log_warning") as lw:
            assert _react_to_comment_inline(MagicMock(), MagicMock(), comment, user_id=1) is False
        assert lw.called


class TestReplyUnderComment:
    def test_types_into_this_comments_composer_and_submits(self):
        from cqc_lem.utilities.linkedin.composer import _reply_under_comment_inline
        rbtn = MagicMock()
        comment = MagicMock(); comment.find_elements.return_value = [rbtn]
        composer = MagicMock()
        driver = MagicMock()
        driver.execute_script.return_value = True  # scrollIntoView, then the submit-button JS
        with patch(f"{COMPOSER}.ActionChains"), \
             patch(f"{COMPOSER}.strip_non_bmp", side_effect=lambda s: s), \
             patch(f"{COMPOSER}._reply_composer_for_comment", return_value=composer) as rc, \
             patch(f"{COMPOSER}._composer_submitted", return_value=True):
            assert _reply_under_comment_inline(driver, MagicMock(), comment, "Great point!", user_id=1) is True
        composer.send_keys.assert_called()
        assert rc.call_args.args[1] is comment  # resolution is anchored to THIS comment

    def test_returns_false_when_no_reply_button(self):
        from cqc_lem.utilities.linkedin.composer import _reply_under_comment_inline
        comment = MagicMock(); comment.find_elements.return_value = []
        driver = MagicMock()
        with patch(f"{COMPOSER}.ActionChains"), patch(f"{COMPOSER}.log_warning"):
            assert _reply_under_comment_inline(driver, MagicMock(), comment, "hi", user_id=1) is False

    def test_no_composer_of_ours_is_a_clean_skip_not_an_exception(self):
        # Issue #886: the sweep and the lead-signal delivery both treat the return value as a
        # boolean skip — a miss must never raise, and must never warn (an unopened reply box is an
        # expected no-op; a repeated log_warning re-escalates as a defect).
        from cqc_lem.utilities.linkedin.composer import _reply_under_comment_inline
        comment = MagicMock(); comment.find_elements.return_value = [MagicMock()]
        driver = MagicMock()
        with patch(f"{COMPOSER}.ActionChains"), patch(f"{COMPOSER}.log_warning") as lw, \
             patch(f"{COMPOSER}._reply_composer_for_comment", return_value=None):
            assert _reply_under_comment_inline(driver, MagicMock(), comment, "hi", user_id=1) is False
        lw.assert_not_called()


class TestCommentContainerHelpers:
    def test_header_author_passthrough(self):
        from cqc_lem.utilities.linkedin.composer import _comment_header_author
        driver = MagicMock(); driver.execute_script.return_value = "https://www.linkedin.com/in/jane/"
        assert _comment_header_author(driver, MagicMock()) == "https://www.linkedin.com/in/jane/"

    def test_header_author_empty_on_error(self):
        from cqc_lem.utilities.linkedin.composer import _comment_header_author
        driver = MagicMock(); driver.execute_script.side_effect = RuntimeError("x")
        assert _comment_header_author(driver, MagicMock()) == ""

    def test_container_passthrough(self):
        from cqc_lem.utilities.linkedin.composer import _comment_container
        node = MagicMock()
        driver = MagicMock(); driver.execute_script.return_value = node
        assert _comment_container(driver, MagicMock()) is node


def _worker_env(es, reply_text="Thanks! How do you test drift?", followup_state=None,
                react=True, reply=True, replies_remaining=5):
    """Patch the follow-up worker's collaborators; returns (driver, my_profile, records)."""
    our_tb, reply_tb = MagicMock(), MagicMock()
    our_tb.text = "our original comment"
    reply_tb.text = reply_text
    # reply's text box @mentions us -> mentions_us True
    reply_tb.find_elements.return_value = [MagicMock()]
    our_tb.find_elements.return_value = [MagicMock()]
    our_cont, reply_cont = MagicMock(name="our_cont"), MagicMock(name="reply_cont")

    driver = MagicMock()
    driver.current_url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
    driver.find_elements.side_effect = lambda _by, _sel: []  # scroll sentinel + expand buttons
    driver.execute_script.return_value = None  # scrollBy + contains() -> falsy (use mentions path)

    def body(_drv, cont):
        return our_tb if cont is our_cont else reply_tb
    def author(_drv, cont):
        return "https://www.linkedin.com/in/me/" if cont is our_cont else "https://www.linkedin.com/in/glenda/"

    # Since #2020 `_comment_items` enters through the shared anchor ladder rather than one
    # selector, so the seam this fixture stubs is the ladder's RESULT — which is what this test is
    # actually about (two comments, one ours). How they were found is the ladder's own suite.
    es.enter_context(patch(f"{COMPOSER}._comment_containers",
                           return_value=([our_cont, reply_cont], "reply_prefix")))
    es.enter_context(patch(f"{COMPOSER}._comment_body", side_effect=body))
    es.enter_context(patch(f"{COMPOSER}._comment_header_author", side_effect=author))
    _p(es, "get_comment_followup", return_value=followup_state)
    rec = _p(es, "record_comment_followup", return_value=True)
    _p(es, "insert_new_log")
    _p(es, "_react_to_comment_inline", return_value=react)
    _p(es, "_reply_under_comment_inline", return_value=reply)
    _p(es, "generate_comment_reply_followup", return_value="a thoughtful answer")
    _p(es, "_flag_lead_signal", return_value=None)   # inbound-intent detection rides this path (#483)
    my_profile = MagicMock(); my_profile.profile_url = "https://www.linkedin.com/in/me/"
    return driver, my_profile, rec


class TestFollowupWorker:
    def test_reacts_and_replies_to_a_question_reply(self):
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        with ExitStack() as es:
            driver, prof, rec = _worker_env(es)
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1,
                    "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "feedurn://urn:li:activity:1", prof, "voice", {}, replies_remaining=5)
        assert r == {"reacted": 1, "replied": 1, "leads": 0, "filtered": 0, "filtered_reasons": {}}

    def test_reacts_only_when_reply_is_not_a_question(self):
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        with ExitStack() as es:
            driver, prof, rec = _worker_env(es, reply_text="Nice, totally agree.")
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1, "u", "feedurn://urn:li:activity:1",
                                                  prof, "voice", {}, replies_remaining=5)
        assert r["replied"] == 0 and r["reacted"] == 1

    def test_dedup_skips_already_handled(self):
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        with ExitStack() as es:
            driver, prof, rec = _worker_env(es, followup_state={"reacted": 1, "replied": 1})
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1, "u", "feedurn://urn:li:activity:1",
                                                  prof, "voice", {}, replies_remaining=5)
        assert r == {"reacted": 0, "replied": 0, "leads": 0, "filtered": 0, "filtered_reasons": {}}

    def test_reply_cap_blocks_reply_but_not_react(self):
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        with ExitStack() as es:
            driver, prof, rec = _worker_env(es)
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1, "u", "feedurn://urn:li:activity:1",
                                                  prof, "voice", {}, replies_remaining=0)
        assert r["reacted"] == 1 and r["replied"] == 0

    def test_no_slug_returns_early(self):
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        prof = MagicMock(); prof.profile_url = "https://www.linkedin.com/"
        with patch(f"{POST}.log_warning"):
            r = _followup_on_post_comment_replies(MagicMock(), MagicMock(), 1, "u", "k", prof, "v", {}, 5)
        assert r == {"reacted": 0, "replied": 0, "leads": 0, "filtered": 0, "filtered_reasons": {}}


class TestOrchestration:
    def _profile_patches(self, es):
        _p(es, "get_engagement_preferences", return_value={})
        _p(es, "count_followup_replies_today", return_value=0)
        _p(es, "get_or_create_profile_synthesis", return_value="voice")
        _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock()))
        _p(es, "quit_gracefully")

    def test_single_post_no_urn(self):
        from cqc_lem.app.engagement.posting import _run_single_post_followup
        assert "No activity URN" in _run_single_post_followup(1, "https://linkedin.com/feed/x")

    def test_single_post_lock_held(self):
        from cqc_lem.app.engagement.posting import _run_single_post_followup
        with ExitStack() as es:
            _p(es, "acquire_run_lock", return_value=None)
            out = _run_single_post_followup(1, "https://www.linkedin.com/feed/update/urn:li:activity:9/")
        assert "another follow-up run" in out.lower()

    def test_single_post_runs_worker(self):
        from cqc_lem.app.engagement.posting import _run_single_post_followup
        with ExitStack() as es:
            _p(es, "acquire_run_lock", return_value="tok")
            rel = _p(es, "release_run_lock")
            self._profile_patches(es)
            _p(es, "_followup_on_post_comment_replies", return_value={"reacted": 1, "replied": 1})
            out = _run_single_post_followup(1, "https://www.linkedin.com/feed/update/urn:li:activity:9/")
        assert "reacted 1, replied 1" in out
        rel.assert_called()

    def test_sweep_no_posts(self):
        from cqc_lem.app.engagement.posting import _run_comment_followups_sweep
        with ExitStack() as es:
            _p(es, "get_recent_navigable_commented_posts", return_value=[])
            assert "No recent navigable" in _run_comment_followups_sweep(1)

    def test_sweep_runs_worker_over_posts(self):
        from cqc_lem.app.engagement.posting import _run_comment_followups_sweep
        with ExitStack() as es:
            _p(es, "get_recent_navigable_commented_posts",
               return_value=[{"post_key": "feedurn://urn:li:activity:1"},
                             {"post_key": "feedpost://hash"}])  # 2nd not navigable -> skipped
            _p(es, "acquire_run_lock", return_value="tok")
            rel = _p(es, "release_run_lock")
            self._profile_patches(es)
            w = _p(es, "_followup_on_post_comment_replies", return_value={"reacted": 2, "replied": 1})
            out = _run_comment_followups_sweep(1)
        assert "reacted 2, replied 1" in out
        assert w.call_count == 1  # only the feedurn post
        rel.assert_called()

    def test_sweep_lock_held(self):
        from cqc_lem.app.engagement.posting import _run_comment_followups_sweep
        with ExitStack() as es:
            _p(es, "get_recent_navigable_commented_posts", return_value=[{"post_key": "feedurn://urn:li:activity:1"}])
            _p(es, "acquire_run_lock", return_value=None)
            assert "in progress" in _run_comment_followups_sweep(1).lower()

    def test_reconcile_no_stale(self):
        from cqc_lem.app.engagement.posting import _run_reconcile_comment_urns
        with ExitStack() as es:
            _p(es, "get_recent_commented_rows_with_text",
               return_value=[{"post_key": "feedurn://urn:li:activity:1", "comment_text": "x"}])
            assert "No stale" in _run_reconcile_comment_urns(1)

    def test_reconcile_upgrades_stale_key(self):
        from cqc_lem.app.engagement.posting import _run_reconcile_comment_urns
        with ExitStack() as es:
            _p(es, "get_recent_commented_rows_with_text",
               return_value=[{"post_key": "feedpost://h", "comment_text": "my comment"}])
            _p(es, "acquire_run_lock", return_value="tok")
            _p(es, "release_run_lock")
            _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock()))
            _p(es, "quit_gracefully")
            _p(es, "_scrape_activity_comment_urns",
               return_value={"my comment": "https://www.linkedin.com/feed/update/urn:li:activity:5/"})
            upd = _p(es, "update_commented_post_key", return_value=True)
            out = _run_reconcile_comment_urns(1)
        assert "Reconciled 1/1" in out
        assert "feedurn://urn:li:activity:5" in upd.call_args[0][2]


class TestGuestReplyGenerator:
    def test_frames_as_guest_and_humanizes(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock(); resp.choices = [MagicMock()]
        resp.choices[0].message.content = "  raw reply  "
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call, \
             patch.object(ai_helper, "_humanize_text", side_effect=lambda t, **k: "clean reply"), \
             patch.object(ai_helper, "_voice_reference", return_value="voice"):
            out = ai_helper.generate_comment_reply_followup("How do you test?", MagicMock(),
                                                            prefs={}, profile_synthesis="v")
        assert out == "clean reply"
        system = call.call_args.kwargs["messages"][0]["content"].lower()
        assert "someone else" in system and "do not speak as if you own" in system

    def test_returns_none_when_llm_empty(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock(); resp.choices = [MagicMock()]; resp.choices[0].message.content = None
        with patch.object(ai_helper, "_call_llm", return_value=resp), \
             patch.object(ai_helper, "_voice_reference", return_value="voice"):
            assert ai_helper.generate_comment_reply_followup("hi?", MagicMock()) is None


class TestScrapeActivityUrns:
    def test_maps_normalized_text_to_post_url(self):
        from cqc_lem.app.engagement.posting import _scrape_activity_comment_urns
        box = MagicMock(); box.text = "My earlier comment here"
        driver = MagicMock(); driver.find_elements.return_value = [box]
        prof = MagicMock(); prof.profile_url = "https://www.linkedin.com/in/me/"
        with patch(f"{POST}._card_for_textbox", return_value=MagicMock()), \
             patch(f"{POST}._post_permalink_from_card",
                   return_value="https://www.linkedin.com/feed/update/urn:li:activity:7/"):
            m = _scrape_activity_comment_urns(driver, MagicMock(), prof)
        assert "https://www.linkedin.com/feed/update/urn:li:activity:7/" in m.values()

    def test_empty_when_no_profile_path(self):
        from cqc_lem.app.engagement.posting import _scrape_activity_comment_urns
        prof = MagicMock(); prof.profile_url = "https://www.linkedin.com/"
        assert _scrape_activity_comment_urns(MagicMock(), MagicMock(), prof) == {}


class TestDispatchCommentFollowups:
    SCH = "cqc_lem.app.run_scheduler"

    def test_dispatches_for_users_with_session(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_followups
        with ExitStack() as es:
            es.enter_context(patch(f"{self.SCH}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{self.SCH}.get_active_user_ids", return_value=[1, 2]))
            es.enter_context(patch(f"{self.SCH}.has_linkedin_session", side_effect=lambda u: u == 1))
            es.enter_context(patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=None))
            sweep = es.enter_context(patch(f"{self.SCH}.sweep_comment_followups"))
            out = dispatch_comment_followups()
        assert sweep.apply_async.call_count == 1  # only the user with a session
        assert "1/2" in out

    def test_throttled_returns_early(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_followups
        with patch(f"{self.SCH}._skip_if_throttled", return_value=True):
            assert "throttled" in dispatch_comment_followups().lower()


class TestGuestReplyContext:
    def test_includes_post_and_prior_comment_context(self):
        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock(); resp.choices = [MagicMock()]; resp.choices[0].message.content = "x"
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call, \
             patch.object(ai_helper, "_humanize_text", side_effect=lambda t, **k: t), \
             patch.object(ai_helper, "_voice_reference", return_value="v"):
            ai_helper.generate_comment_reply_followup("their reply?", MagicMock(),
                                                      our_comment="my comment", post_content="the post")
        user_msg = call.call_args.kwargs["messages"][1]["content"]
        assert "the post" in user_msg and "my comment" in user_msg


class TestDispatchBranches:
    SCH = "cqc_lem.app.run_scheduler"

    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_followups
        with patch(f"{self.SCH}._skip_if_throttled", return_value=False), \
             patch(f"{self.SCH}.get_active_user_ids", return_value=[]):
            assert "No active users" in dispatch_comment_followups()

    def test_redis_interval_gate(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_followups
        client = MagicMock(); client.set.return_value = True  # due
        with patch(f"{self.SCH}._skip_if_throttled", return_value=False), \
             patch(f"{self.SCH}.get_active_user_ids", return_value=[1]), \
             patch(f"{self.SCH}.has_linkedin_session", return_value=True), \
             patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=client), \
             patch(f"{self.SCH}.sweep_comment_followups") as sweep:
            out = dispatch_comment_followups()
        assert sweep.apply_async.called and "1/1" in out
        assert client.set.call_args.kwargs.get("nx") is True


class TestFollowupSafetyFilter:
    """Owner ruling: only positive, safe replies go out.

    The follow-up sweep applies the same scam/hostility filter as the reply sweep, before the lead
    flag, the reaction and the reply, and refuses a drafted answer that fails the outbound gates.
    """

    _URL = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
    _KEY = "feedurn://urn:li:activity:1"

    def _run(self, es, **env):
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        driver, prof, _rec = _worker_env(es, **env)
        return _followup_on_post_comment_replies(driver, MagicMock(), 1, self._URL, self._KEY, prof,
                                                 "voice", {}, replies_remaining=5)

    def test_a_scam_reply_gets_no_lead_flag_reaction_draft_or_log(self):
        import cqc_lem.app.engagement.posting as posting
        with ExitStack() as es:
            r = self._run(es, reply_text="Can you help? Message me on WhatsApp +1 555 123 4567")
            # Read back the mocks `_worker_env` installed, while they are still in place.
            for name in ("_flag_lead_signal", "_react_to_comment_inline",
                         "generate_comment_reply_followup", "_reply_under_comment_inline",
                         "insert_new_log", "record_comment_followup"):
                getattr(posting, name).assert_not_called()
        assert r == {"reacted": 0, "replied": 0, "leads": 0, "filtered": 1,
                     "filtered_reasons": {"off_platform_contact": 1}}

    def test_a_hostile_reply_is_filtered(self):
        import cqc_lem.app.engagement.posting as posting
        with ExitStack() as es:
            r = self._run(es, reply_text="Why would anyone listen? You're a clown.")
            posting._react_to_comment_inline.assert_not_called()
        assert r["filtered"] == 1 and r["reacted"] == 0

    def test_a_safe_reply_still_proceeds(self):
        with ExitStack() as es:
            r = self._run(es, reply_text="Great point on investment strategy. How do you test drift?")
        assert r == {"reacted": 1, "replied": 1, "leads": 0, "filtered": 0, "filtered_reasons": {}}

    def test_a_refused_draft_is_not_posted(self):
        """Outbound gate: an outbound_qa refusal (a leaked placeholder) never reaches the thread."""
        with ExitStack() as es:
            warn = _p(es, "log_warning")
            driver, prof, _rec = _worker_env(es)
            _p(es, "generate_comment_reply_followup", return_value="Great question, [Name]!")
            reply = _p(es, "_reply_under_comment_inline", return_value=True)
            from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1, self._URL, self._KEY, prof,
                                                  "voice", {}, replies_remaining=5)
        reply.assert_not_called()
        assert r["replied"] == 0 and r["reacted"] == 1 and r["filtered"] == 0
        assert "Refusing to post an unsendable reply" in warn.call_args.args[0]

    def test_a_refused_draft_is_latched_so_the_next_sweep_does_not_redraft_it(self):
        """An outsider's reply must not make every sweep re-draft and re-warn (escalating)."""
        store = {}
        redis = MagicMock()
        redis.get.side_effect = store.get
        redis.set.side_effect = lambda key, value, ex=None: store.__setitem__(key, value)
        from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
        drafts, warnings, caps = [], [], []
        for _sweep in range(2):
            with ExitStack() as es:
                _p(es, "_redis_client", return_value=redis)
                warn = _p(es, "log_warning")
                driver, prof, rec = _worker_env(es)
                gen = _p(es, "generate_comment_reply_followup", return_value="Ping @fxqueen")
                _followup_on_post_comment_replies(driver, MagicMock(), 1, self._URL, self._KEY, prof,
                                                  "voice", {}, replies_remaining=5)
                drafts.append(gen.call_count)
                warnings.append(warn.call_count)
                caps.append([c for c in rec.call_args_list if c.kwargs.get("replied")])
        assert drafts == [1, 0] and warnings == [1, 0]
        assert len(store) == 1 and next(iter(store)).startswith("linkedin:followup_draft_refused:1:")
        # The daily-cap record (`comment_followups.replied`) is never latched for a refusal.
        assert caps == [[], []]

    def test_a_draft_that_reads_hostile_is_not_posted(self):
        with ExitStack() as es:
            warn = _p(es, "log_warning")
            driver, prof, _rec = _worker_env(es)
            _p(es, "generate_comment_reply_followup", return_value="Honestly, nobody cares about that.")
            reply = _p(es, "_reply_under_comment_inline", return_value=True)
            from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1, self._URL, self._KEY, prof,
                                                  "voice", {}, replies_remaining=5)
        reply.assert_not_called()
        assert r["replied"] == 0
        assert "reads hostile (insult)" in warn.call_args.args[0]

    def test_the_sweep_summary_reports_filtered(self):
        from cqc_lem.app.engagement.posting import _run_comment_followups_sweep
        with ExitStack() as es:
            _p(es, "get_recent_navigable_commented_posts", return_value=[{"post_key": self._KEY}])
            _p(es, "acquire_run_lock", return_value="tok")
            _p(es, "release_run_lock")
            _p(es, "get_engagement_preferences", return_value={})
            _p(es, "count_followup_replies_today", return_value=0)
            _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock()))
            _p(es, "get_or_create_profile_synthesis", return_value="s")
            _p(es, "quit_gracefully")
            _p(es, "_followup_on_post_comment_replies",
               return_value={"reacted": 0, "replied": 0, "leads": 0, "filtered": 2,
                             "filtered_reasons": {"link_push": 1, "insult": 1}})
            info = _p(es, "log_info")
            warn = _p(es, "log_warning")
            out = _run_comment_followups_sweep(1)
        assert "filtered 2" in out
        # ONE INFO roll-up per run: the count and the check names, never any text, and no warning.
        roll_up = [c for c in info.call_args_list
                   if c.args[0] == "Comment safety filter skipped comments"]
        assert len(roll_up) == 1
        assert roll_up[0].kwargs["filtered"] == 2
        assert roll_up[0].kwargs["reasons"] == "insult:1,link_push:1"
        warn.assert_not_called()

    @pytest.mark.parametrize("draft", [
        "Great point, see cryptofx-pro.io",
        "Ping @fxqueen on Telegram",
        "Thanks! Call +1 555 010 2000",
    ])
    def test_a_draft_naming_a_contact_or_link_is_not_posted(self, draft):
        with ExitStack() as es:
            warn = _p(es, "log_warning")
            debug = _p(es, "log_debug")
            driver, prof, _rec = _worker_env(es)
            _p(es, "generate_comment_reply_followup", return_value=draft)
            reply = _p(es, "_reply_under_comment_inline", return_value=True)
            from cqc_lem.app.engagement.posting import _followup_on_post_comment_replies
            r = _followup_on_post_comment_replies(driver, MagicMock(), 1, self._URL, self._KEY, prof,
                                                  "voice", {}, replies_remaining=5)
        reply.assert_not_called()
        assert r["replied"] == 0
        message = warn.call_args.args[0]
        assert message.startswith("Refusing to post an unsendable reply: ")
        # The warning carries check names only — the attacker-steerable draft goes to DEBUG.
        assert draft not in str(warn.call_args)
        assert any(c.kwargs.get("response") == draft for c in debug.call_args_list)
