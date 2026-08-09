"""The levels issue #1184 chose, pinned so a future mechanical sweep cannot silently undo them.

#1174 retired `myprint` by resolving every call site to `log_info`, which left real failures logging
at INFO. #1186 and this PR picked the level each site should have had. Those choices are load-bearing
in a way the code cannot express on its own: a repeated `log_warning` re-emits at ERROR and files ONE
grouped `$exception` that the daily cron turns into a GitHub issue, so the difference between DEBUG
and WARNING on an expected no-op is the difference between silence and a defect filed against working
behaviour (`src/cqc_lem/utilities/CLAUDE.md`).

Each test therefore asserts BOTH halves — the level that must fire and the ones that must not —
because "it logged something" is exactly what was true before and is not the property worth keeping.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_FEED = "cqc_lem.app.engagement.feed"
_POSTING = "cqc_lem.app.engagement.posting"
_OUTREACH = "cqc_lem.app.engagement.outreach"


class _Levels:
    """The four level mocks for one module, so a test can name what must NOT have fired."""

    def __init__(self, module: str):
        self._module = module

    def __enter__(self):
        self._patches = {
            name: patch(f"{self._module}.log_{name}") for name in
            ("debug", "info", "warning", "error")
        }
        self.debug, self.info, self.warning, self.error = (
            self._patches[name].start() for name in ("debug", "info", "warning", "error"))
        return self

    def __exit__(self, *exc):
        for p in self._patches.values():
            p.stop()
        return False

    def _all(self):
        return {"debug": self.debug, "info": self.info,
                "warning": self.warning, "error": self.error}

    def only(self, which):
        """Assert `which` fired and no other level did."""
        levels = self._all()
        assert levels[which].called, f"expected log_{which} to fire"
        for name, mock in levels.items():
            if name != which:
                assert not mock.called, f"log_{name} must not fire here, it did: {mock.call_args}"

    def fired_without_escalating(self, which):
        """Assert `which` fired and that nothing at WARNING or above did.

        For a path that legitimately logs task progress at INFO on the way past: the property that
        matters is that the no-op itself never reaches the escalation ladder.
        """
        assert self._all()[which].called, f"expected log_{which} to fire"
        for name in ("warning", "error"):
            mock = self._all()[name]
            assert not mock.called, f"log_{name} must not fire here, it did: {mock.call_args}"


def _boom(*_a, **_kw):
    raise RuntimeError("boom")


class TestBestEffortEnrichmentWarns:
    """An `except Exception` around an enrichment whose own module swallows its DB faults.

    The exception arriving here is therefore UNEXPECTED, and the second and third occurrence say it
    is systematic — which is what makes WARNING (and its escalation) the right level rather than the
    INFO the shim left behind. This is the level nine handlers written after the myprint era in
    `run_content_plan.py` already use.
    """

    def test_a_post_image_that_cannot_be_generated_warns(self):
        from cqc_lem.app.run_content_plan import _generate_text_post_image
        with _Levels(_RCP) as lv, \
                patch("cqc_lem.utilities.db.get_engagement_preferences", side_effect=_boom):
            assert _generate_text_post_image(7, "body", 42) is None
            lv.only("warning")
        assert lv.warning.call_args.kwargs["post_id"] == 42

    def test_a_missing_profile_synthesis_warns(self):
        from cqc_lem.app.run_content_plan import _profile_synthesis_or_none
        with _Levels(_RCP) as lv, patch(f"{_RCP}.load_profile_for_user", side_effect=_boom):
            assert _profile_synthesis_or_none(7) is None
            lv.only("warning")

    def test_an_unreadable_content_mix_class_warns(self):
        from cqc_lem.app.run_content_plan import _post_content_mix
        with _Levels(_RCP) as lv, patch(f"{_RCP}.get_post_content_mix", side_effect=_boom):
            assert _post_content_mix(42) is None
            lv.only("warning")

    def test_an_unreadable_post_status_warns(self):
        from cqc_lem.app.run_content_plan import _post_is_flagged_error
        with _Levels(_RCP) as lv, \
                patch("cqc_lem.utilities.db.get_post_status", side_effect=_boom):
            assert _post_is_flagged_error(42) is False
            lv.only("warning")

    def test_unreadable_lead_magnet_settings_warn(self):
        """Without the keyword the slop lint reads the sanctioned CTA as bait and HOLDS the post."""
        from cqc_lem.app.run_content_plan import _cta_keyword_for
        with _Levels(_RCP) as lv, patch(f"{_RCP}.get_lead_magnet_settings", side_effect=_boom):
            assert _cta_keyword_for(7, 42) is None
            lv.only("warning")

    def test_a_carousel_fact_grounding_failure_warns(self):
        from cqc_lem.app.run_content_plan import _report_carousel_fact_grounding
        with _Levels(_RCP) as lv, patch(f"{_RCP}.deck_slides", side_effect=_boom):
            _report_carousel_fact_grounding(7, 42, {"format": "build_receipt"}, {"slides": []})
            lv.only("warning")

    def test_a_rescore_without_a_profile_synthesis_warns(self):
        from cqc_lem.app.run_content_plan import rescore_post
        with _Levels(_RCP) as lv, \
                patch("cqc_lem.utilities.db.get_post_user_id", return_value=7), \
                patch(f"{_RCP}.get_post_content", return_value="Body"), \
                patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
                patch("cqc_lem.utilities.db.get_post_video_url", return_value=None), \
                patch("cqc_lem.utilities.db.get_post_status", return_value="pending"), \
                patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
                patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
                patch(f"{_RCP}.get_or_create_profile_synthesis", side_effect=_boom), \
                patch(f"{_RCP}._score_and_persist_authenticity"), \
                patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
                patch(f"{_RCP}._post_archetype_or_none", return_value=None), \
                patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
                patch(f"{_RCP}._fact_anchors_for", return_value=[]), \
                patch(f"{_RCP}._cta_keyword_for", return_value=None), \
                patch(f"{_RCP}.evaluate_post_gates", return_value=[]), \
                patch(f"{_RCP}._persist_gate_findings"), \
                patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": False}):
            rescore_post(42)
        assert lv.warning.called

    def test_unreadable_newsletter_settings_warn_when_a_meeting_ask_needs_replacing(self):
        """The meeting-ask repair has no artifact to close on, so the CTA policy silently degrades."""
        from cqc_lem.app.run_content_plan import create_text_post
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
                patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
                patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
                patch(f"{_RCP}._select_story_for_post", return_value=None), \
                patch(f"{_RCP}._select_post_blueprint", return_value={"format": "spiky_pov"}), \
                patch(f"{_RCP}.get_lead_magnet_settings", return_value={}), \
                patch(f"{_RCP}.should_include_lead_magnet_cta", return_value=False), \
                patch(f"{_RCP}.lead_magnet_cta_directive", return_value=""), \
                patch(f"{_RCP}.get_thought_leadership_post_from_ai", return_value="Book a call"), \
                patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda t, **k: t), \
                patch(f"{_RCP}.optimize_post_hook", side_effect=lambda t, **k: t), \
                patch(f"{_RCP}.humanize_text", side_effect=lambda t, **k: t), \
                patch(f"{_RCP}._score_and_persist_authenticity"), \
                patch(f"{_RCP}._review_generated_post", side_effect=lambda *a, **k: a[7]), \
                patch(f"{_RCP}.shape_for_dwell", side_effect=lambda t: t), \
                patch(f"{_RCP}.contains_meeting_ask", return_value=True), \
                patch(f"{_RCP}.get_newsletter_settings", side_effect=_boom), \
                patch(f"{_RCP}.replace_meeting_ask_cta", return_value=None), \
                patch(f"{_RCP}.update_db_post_shape"):
            create_text_post(7, "awareness", post_type="thought_leadership",
                             user_profile=MagicMock(), post_id=42)
        assert any("newsletter" in str(c.args[0]).lower() for c in lv.warning.call_args_list)

    def test_a_sitemap_that_will_not_fetch_stays_at_info(self):
        """A sitemap that will not fetch stays at INFO, but must reach the logger at all.

        The level is deliberately unchanged (it is on this PR's "needs a human" list, with the
        other external-site handlers), yet the bare `print()` it replaced reached no log sink.
        """
        import requests

        from cqc_lem.app.run_content_plan import fetch_sitemap_urls
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.fetch_content", side_effect=requests.RequestException("down")):
            assert fetch_sitemap_urls("https://example.com/sitemap.xml") == []
            lv.only("info")


class TestStoryBankAndShapeWritesWarn:
    """The two writes whose loss is invisible in the post that lost them.

    `record_story_bank_use` is what ADVANCES the bank's least-used ordering and `update_db_post_shape`
    is what the NEXT post rotates away from — so a silent failure means every subsequent post reuses
    the same anchor and the same archetype, which is precisely the class of fault that only shows up
    on repetition.
    """

    def test_a_failed_story_bank_use_write_warns(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.record_story_bank_use", side_effect=_boom), \
                patch(f"{_RCP}.update_db_post_shape"):
            self._run_text_post_tail(create_text_post)
            assert lv.warning.called
            assert any("story bank" in str(c.args[0]).lower() for c in lv.warning.call_args_list)

    def test_a_failed_shape_write_warns(self):
        from cqc_lem.app.run_content_plan import create_text_post
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.record_story_bank_use"), \
                patch(f"{_RCP}.update_db_post_shape", side_effect=_boom):
            self._run_text_post_tail(create_text_post)
            assert any("shape" in str(c.args[0]).lower() for c in lv.warning.call_args_list)

    def test_a_failed_carousel_shape_write_warns(self):
        from cqc_lem.app.run_content_plan import create_carousel_content
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
                patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
                patch(f"{_RCP}._select_story_for_post", return_value=None), \
                patch(f"{_RCP}._select_carousel_blueprint",
                      return_value={"format": "spiky_pov", "hook_style": "question"}), \
                patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                      return_value=("caption", None)), \
                patch(f"{_RCP}.update_db_post_status"), \
                patch(f"{_RCP}.update_db_post_shape", side_effect=_boom):
            create_carousel_content(7, "awareness", post_id=42)
        assert any("shape" in str(c.args[0]).lower() for c in lv.warning.call_args_list)

    def test_a_carousel_that_cannot_load_the_voice_synthesis_warns(self):
        from cqc_lem.app.run_content_plan import create_carousel_content
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
                patch(f"{_RCP}.get_or_create_profile_synthesis", side_effect=_boom), \
                patch(f"{_RCP}._select_story_for_post", return_value=None), \
                patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
                patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                      return_value=("caption", None)), \
                patch(f"{_RCP}.update_db_post_status"):
            create_carousel_content(7, "awareness", post_id=42)
        assert any("synthesis" in str(c.args[0]).lower() for c in lv.warning.call_args_list)

    @staticmethod
    def _run_text_post_tail(create_text_post):
        """Drive create_text_post far enough to reach its two persistence writes."""
        story = {"id": 3, "kind": "anecdote", "title": "A win", "body": "We shipped it"}
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
                patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
                patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
                patch(f"{_RCP}._select_story_for_post", return_value=story), \
                patch(f"{_RCP}._select_post_blueprint",
                      return_value={"format": "spiky_pov", "hook_style": "question"}), \
                patch(f"{_RCP}.get_lead_magnet_settings", return_value={}), \
                patch(f"{_RCP}.should_include_lead_magnet_cta", return_value=False), \
                patch(f"{_RCP}.lead_magnet_cta_directive", return_value=""), \
                patch(f"{_RCP}.get_thought_leadership_post_from_ai", return_value="Draft body"):
            create_text_post(7, "awareness", post_type="thought_leadership",
                             user_profile=MagicMock(), refine_final_post=False,
                             similarity_check=False, post_id=42)


class TestExpectedNoOpsAreDebug:
    """*Do not warn on an expected no-op.* Each of these fires on a schedule by design."""

    def test_a_lost_single_flight_lock_is_debug(self):
        """The lock exists BECAUSE the triggers overlap, so losing the race is it working."""
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        with _Levels(_POSTING) as lv, \
                patch(f"{_POSTING}.get_engagement_preferences", return_value={}), \
                patch(f"{_POSTING}.get_recent_posted_post_ids", return_value=[1]), \
                patch(f"{_POSTING}.acquire_run_lock", return_value=None):
            result = sweep_reply_comments.run(user_id=7)
            lv.only("debug")
        assert "another reply sweep in progress" in result

    def test_an_already_engaged_viewer_is_debug(self):
        """The analytics page lists the same viewer on consecutive runs — a documented repeat."""
        from cqc_lem.app.engagement.outreach import engage_with_profile_viewer
        with _Levels(_OUTREACH) as lv, \
                patch(f"{_OUTREACH}.has_engaged_url_with_x_days", return_value=True):
            engage_with_profile_viewer.run(user_id=7, viewer_url="https://x/in/a",
                                           viewer_name="Ada")
            # The task announces its own start at INFO; what matters is that the SKIP never
            # reaches the escalation ladder.
            lv.fired_without_escalating("debug")

    def test_an_already_commented_post_is_debug(self):
        from cqc_lem.app.engagement.outreach import generate_and_post_comment
        profile = MagicMock(email="a@b.c")
        with _Levels(_OUTREACH) as lv, \
                patch(f"{_OUTREACH}.get_user_id", return_value=7), \
                patch(f"{_OUTREACH}.check_commented", return_value=True):
            driver = MagicMock(current_url="https://x/p/1")
            assert generate_and_post_comment(driver, MagicMock(), "https://x/p/1", profile) is False
            lv.only("debug")

    def test_a_throttled_catchup_scrape_is_debug(self):
        """An open 429 breaker is working behaviour, reported where it is DETECTED."""
        from cqc_lem.app.engagement import outreach
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with _Levels(_OUTREACH) as lv, \
                patch(f"{_OUTREACH}.get_engagement_preferences",
                      return_value={"catchup_event_types": ["work_anniversary"]}), \
                patch(f"{_OUTREACH}.get_current_profile",
                      return_value=(MagicMock(), MagicMock(), "a@b.c", MagicMock())), \
                patch(f"{_OUTREACH}._scrape_catchup_moments",
                      side_effect=LinkedInRateLimited("429")), \
                patch(f"{_OUTREACH}.report_catchup_run"), \
                patch(f"{_OUTREACH}.quit_gracefully"):
            result = outreach.automate_catchup_touches.run(user_id=7)
            lv.only("debug")
        assert "throttled" in result.lower()


class TestAWrapperNeverRestatesAFailureAtItsOwnLevel:
    """A task wrapper is a caller (#1038). One lost action must file ONE grouped issue, not two."""

    def test_send_private_dm_logs_success_at_info_and_failure_at_debug(self):
        from cqc_lem.app.engagement.outreach import send_private_dm
        with _Levels(_OUTREACH) as lv, patch(f"{_OUTREACH}.send_dm_now", return_value=True):
            assert send_private_dm.run(user_id=7, profile_url="https://x/in/a", message="hi") \
                == "DM Sent Successfully"
            lv.only("info")
        with _Levels(_OUTREACH) as lv, patch(f"{_OUTREACH}.send_dm_now", return_value=False):
            assert send_private_dm.run(user_id=7, profile_url="https://x/in/a", message="hi") \
                == "DM Failed"
            # send_dm_now already logged the reason where it happened and wrote the FAILURE row.
            lv.only("debug")


class TestFailuresAHumanHasToFixAreErrors:
    def test_a_carousel_that_cannot_render_slides_errors(self):
        """No degraded fallback exists: the post is flagged 'error' and waits for a human."""
        from cqc_lem.app.run_content_plan import create_carousel_content
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
                patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
                patch(f"{_RCP}._select_story_for_post", return_value=None), \
                patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
                patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                      return_value=("caption", {"title": "T", "slides": []})), \
                patch("cqc_lem.utilities.carousel_creator.create_carousel_slide_images",
                      side_effect=_boom), \
                patch(f"{_RCP}.update_db_post_status") as status:
            create_carousel_content(7, "awareness", post_id=42)
        assert lv.error.called, "a carousel a human must fix has to reach PostHog"
        assert lv.error.call_args.kwargs.get("exc") is not None
        # The flag itself is DEBUG — the status write is the record, and one condition gets one.
        assert lv.debug.called
        assert status.called

    def test_content_generation_failure_errors_like_its_storing_half(self):
        """Symmetric with the storing-half handler, which has always been log_error."""
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"user_id": 7, "id": 42, "post_type": "text", "buyer_stage": "awareness",
                "content_mix": "value", "scheduled_time": None}
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.create_content", side_effect=_boom), \
                patch(f"{_RCP}.record_post_failed") as failed:
            assert _create_content_for_planned_post(post, {}) is False
            lv.only("error")
        assert failed.called
        assert lv.error.call_args.kwargs.get("exc") is not None

    def test_generation_returning_nothing_errors_without_an_exception(self):
        """A loud log line, not a filed $exception — there is no exception to attach."""
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"user_id": 7, "id": 42, "post_type": "text", "buyer_stage": "awareness",
                "content_mix": "value", "scheduled_time": None}
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.create_content", return_value=(None, None)), \
                patch(f"{_RCP}.record_post_failed"):
            assert _create_content_for_planned_post(post, {}) is False
            lv.only("error")
        assert "exc" not in lv.error.call_args.kwargs

    def test_c2pa_signing_that_raises_warns(self):
        """C2PA signing that RAISES is the best-effort module itself broken, so it warns.

        `c2pa_helper` promises it "never raises into the generation pipeline" and logs its own
        skips, so nothing should ever arrive at this handler.
        """
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"user_id": 7, "id": 42, "post_type": "video", "buyer_stage": "awareness",
                "content_mix": "value", "scheduled_time": None}
        with _Levels(_RCP) as lv, \
                patch(f"{_RCP}.create_content", return_value=("Body", "https://runway/v.mp4")), \
                patch(f"{_RCP}.create_folder_if_not_exists"), \
                patch(f"{_RCP}.save_video_url_to_dir", return_value="/assets/videos/v.mp4"), \
                patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials",
                      side_effect=_boom), \
                patch(f"{_RCP}.update_db_post_video_url"), \
                patch(f"{_RCP}._post_used_avatar_media", return_value=False), \
                patch(f"{_RCP}._score_and_persist_dwell"), \
                patch(f"{_RCP}.update_db_post_content"), \
                patch(f"{_RCP}._gate_findings_for_post", return_value=[]), \
                patch(f"{_RCP}._persist_gate_findings"), \
                patch(f"{_RCP}.update_db_post_status"), \
                patch(f"{_RCP}.record_post_generated"):
            assert _create_content_for_planned_post(post, {}) is True
        assert any("c2pa" in str(c.args[0]).lower() for c in lv.warning.call_args_list)


class TestOutcomesNothingElseRecords:
    def test_a_comment_that_does_not_land_warns(self):
        """A comment that never landed warns, because nothing else records the outcome.

        `post_comment_inline`'s silent-False paths are DEBUG inside it, so this is the one place
        the outcome is known. Once is SDUI noise; repeatedly is drift.
        """
        from cqc_lem.app.engagement.feed import comment_on_post
        with _Levels(_FEED) as lv, \
                patch(f"{_FEED}.has_user_commented_on_post_url", return_value=False), \
                patch(f"{_FEED}.has_commented_post", return_value=False), \
                patch(f"{_FEED}.claim_post_for_comment", return_value=True), \
                patch(f"{_FEED}.get_driver_wait_pair",
                      return_value=(MagicMock(current_url="https://x/p/1"), MagicMock())), \
                patch(f"{_FEED}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
                patch(f"{_FEED}.login_to_linkedin"), \
                patch(f"{_FEED}._permalink_post_card", return_value=MagicMock()), \
                patch(f"{_FEED}.react_to_post_inline", return_value=True), \
                patch(f"{_FEED}.post_comment_inline", return_value=False), \
                patch(f"{_FEED}.release_post_claim") as release, \
                patch(f"{_FEED}.insert_new_log") as logged, \
                patch(f"{_FEED}.quit_gracefully"):
            comment_on_post.run(user_id=7, post_link="https://x/p/1", comment_text="nice")
        assert lv.warning.called, "a comment that never landed is the drift signal"
        assert release.called and logged.called, "the claim is released and a FAILURE row written"

    def test_a_viewer_whose_profile_will_not_scrape_warns(self):
        """A viewer whose profile will not scrape warns — that engagement does nothing at all.

        Symmetric with `get_my_profile`'s "scrape returned nothing", which #1186 raised at
        WARNING for the same reason.
        """
        from cqc_lem.app.engagement.outreach import engage_with_profile_viewer
        with _Levels(_OUTREACH) as lv, \
                patch(f"{_OUTREACH}.has_engaged_url_with_x_days", return_value=False), \
                patch(f"{_OUTREACH}.get_current_profile",
                      return_value=(MagicMock(), MagicMock(), "a@b.c", MagicMock())), \
                patch(f"{_OUTREACH}.get_linkedin_profile_from_url", return_value=None), \
                patch(f"{_OUTREACH}.insert_new_log"), \
                patch(f"{_OUTREACH}.quit_gracefully"):
            engage_with_profile_viewer.run(user_id=7, viewer_url="https://x/in/a",
                                           viewer_name="Ada")
        assert lv.warning.called, "an unscrapeable viewer profile is the drift signal"
