"""Unit tests for the content-quality beat tasks — issue #630 / D6.

Covers the nightly scoring pass (what it scores, what it persists, what it emits, the per-surface
similarity history, the item cap, the detector budget) and the weekly rollup (the PostHog event, the
regression alerts routed through log_error, and the paths that report nothing). PostHog and the DB are
mocked throughout; the scoring arithmetic itself lives in tests/unit/utilities/test_content_quality.py.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

RS = "cqc_lem.app.run_scheduler"
DB = "cqc_lem.utilities.db"
OBS = "cqc_lem.utilities.observability"
CQ = "cqc_lem.utilities.content_quality"


def _post(ref_id="1", text="A plain first line.\nAnd a second sentence for the body.", **kw):
    row = {"surface": "post", "ref_id": ref_id, "text": text, "shipped_on": "2026-07-26",
           "format_key": None, "post_type": "text", "video_url": None,
           "authenticity_score": 88, "reactions": 10, "comments": 4,
           "reposts": 1, "impressions": 1000}
    row.update(kw)
    return row


def _video_post(ref_id="1", text="A plain first line.\nAnd a second sentence for the body.", **kw):
    row = {"surface": "post", "ref_id": ref_id, "text": text, "shipped_on": "2026-07-26",
           "format_key": None, "post_type": "video",
           "video_url": f"/api/assets?file_name=videos/runwayml/{ref_id}.mp4",
           "authenticity_score": 88, "reactions": 10, "comments": 4,
           "reposts": 1, "impressions": 1000}
    row.update(kw)
    return row


def _carousel_post(ref_id="7", text="A caption for the deck.", **kw):
    row = {"surface": "post", "ref_id": ref_id, "text": text, "shipped_on": "2026-07-26",
           "format_key": None, "post_type": "carousel", "video_url": None,
           "carousel_slides": [f"/api/assets?file_name=images/carousel/{ref_id}/slide_01.png"],
           "authenticity_score": None, "reactions": 5, "comments": 2,
           "reposts": 0, "impressions": 400}
    row.update(kw)
    return row


def _newsletter(ref_id="12", text="An opening line.\n\nA developed section with a real example.",
                **kw):
    row = {"surface": "newsletter", "ref_id": ref_id, "text": text, "shipped_on": "2026-07-26",
           "format_key": "deep_dive", "authenticity_score": None, "reactions": None,
           "comments": None, "reposts": None, "impressions": None}
    row.update(kw)
    return row


def _comment(ref_id="50", text="A specific point about the index change, and what we saw.", **kw):
    row = {"surface": "comment", "ref_id": ref_id, "text": text, "shipped_on": "2026-07-26",
           "format_key": None, "authenticity_score": None, "reactions": None, "comments": None,
           "reposts": None, "impressions": None}
    row.update(kw)
    return row


class TestNightlyContentQuality:
    def _run(self, es, items, users=(1,), post_history=None, comment_history=None,
             newsletter_history=None, days=None):
        from cqc_lem.app.run_scheduler import auto_nightly_content_quality
        es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=list(users)))
        es.enter_context(patch(f"{DB}.get_shipped_content_for_quality", return_value=items))
        es.enter_context(patch(f"{DB}.get_recent_post_texts", return_value=post_history or []))
        es.enter_context(patch(f"{DB}.get_recent_comment_texts", return_value=comment_history or []))
        es.enter_context(patch(f"{DB}.get_recent_newsletter_bodies",
                               return_value=newsletter_history or []))
        es.enter_context(patch(f"{DB}.get_lead_magnet_settings", return_value={"keyword": None}))
        record = es.enter_context(patch(f"{DB}.record_content_quality_score", return_value=True))
        track = es.enter_context(patch(f"{OBS}.track_content_quality"))
        # No network: the batch embedding call is the only I/O in the scoring path.
        es.enter_context(patch(f"{CQ}.embed_comments", return_value=None))
        return auto_nightly_content_quality(days=days), record, track

    def test_scores_persists_and_emits_every_shipped_piece(self):
        with ExitStack() as es:
            result, record, track = self._run(es, [_post(), _comment()])
        assert "scored 2 piece(s)" in result
        assert record.call_count == 2 and track.call_count == 2
        surfaces = {call.args[1]["surface"] for call in record.call_args_list}
        assert surfaces == {"post", "comment"}

    def test_engagement_rate_is_derived_from_the_captured_stats(self):
        with ExitStack() as es:
            _result, record, _track = self._run(es, [_post()])
        score = record.call_args.args[1]
        # engagement_score = 10 + 2*4 + 2*1 = 20 over 1000 impressions.
        assert score["engagement_rate"] == pytest.approx(0.02)
        assert score["impressions"] == 1000
        assert score["authenticity_score"] == 88

    def test_a_post_with_no_stats_yet_reports_an_unmeasured_rate(self):
        with ExitStack() as es:
            _result, record, _track = self._run(
                es, [_post(reactions=None, comments=None, reposts=None, impressions=None)])
        score = record.call_args.args[1]
        assert score["engagement_rate"] is None and score["impressions"] is None

    def test_similarity_history_is_scoped_to_the_matching_surface(self):
        with ExitStack() as es:
            sim = es.enter_context(patch(f"{CQ}.similarity_reports",
                                         return_value=[{"score": 0.3, "measure": "lexical",
                                                        "match": "m"}]))
            self._run(es, [_post(), _comment()], post_history=["older post"],
                      comment_history=["older comment"])
        # One batched call per surface, each against ITS OWN history — a post graded against the
        # user's comments would look unique no matter how templated it is.
        assert sim.call_count == 2
        histories = [call.args[1] for call in sim.call_args_list]
        assert ["older post"] in histories and ["older comment"] in histories

    def test_video_posts_score_the_rendered_asset(self):
        with ExitStack() as es:
            video = es.enter_context(patch(f"{CQ}.score_video_asset",
                                           return_value={"video_render_ok": True,
                                                         "video_model_tier": "gen4_turbo",
                                                         "video_duration_seconds": 5,
                                                         "video_aspect_ratio": "9:16",
                                                         "video_asset_probe": "ok"}))
            _result, record, _track = self._run(es, [_video_post(), _post()])
        assert video.call_count == 1
        score = record.call_args_list[0].args[1]
        assert score["video_render_ok"] is True
        assert score["video_model_tier"] == "gen4_turbo"
        # Non-video posts carry no video keys.
        text_score = record.call_args_list[1].args[1]
        assert text_score.get("video_render_ok") is None

    def test_video_posts_pass_the_video_url_to_the_scorer(self):
        with ExitStack() as es:
            video = es.enter_context(patch(f"{CQ}.score_video_asset",
                                           return_value={"video_render_ok": False,
                                                         "video_model_tier": None,
                                                         "video_duration_seconds": None,
                                                         "video_aspect_ratio": None,
                                                         "video_asset_probe": "missing"}))
            self._run(es, [_video_post(video_url="/api/assets?file_name=videos/runwayml/1.mp4")])
        assert video.call_args.kwargs["video_url"] == "/api/assets?file_name=videos/runwayml/1.mp4"

    def test_a_carousel_post_scores_its_deck_on_its_own_surface(self):
        with ExitStack() as es:
            deck = es.enter_context(patch(f"{CQ}.score_carousel_deck", return_value={
                "deck_probe": "ok", "deck_slides": 5, "deck_template": "bold_listicle",
                "deck_body_chars_avg": 148.0, "deck_body_chars_max": 196,
                "deck_chars_dropped": 57, "deck_slides_clipped": 1, "deck_slides_with_band": 3}))
            result, record, track = self._run(es, [_carousel_post()])
        # TWO readings for one carousel: the caption as a post, the render as a deck.
        assert "scored 2 piece(s)" in result
        assert deck.call_args.kwargs["slide_urls"] == [
            "/api/assets?file_name=images/carousel/7/slide_01.png"]
        caption, deck_row = (call.args[1] for call in record.call_args_list)
        assert caption["surface"] == "post" and caption.get("deck_slides") is None
        assert deck_row["surface"] == "carousel" and deck_row["ref_id"] == "7"
        assert deck_row["deck_chars_dropped"] == 57 and deck_row["deck_template"] == "bold_listicle"
        assert [call.args[1]["surface"] for call in track.call_args_list] == ["post", "carousel"]

    def test_a_document_post_is_the_same_deck_surface(self):
        with ExitStack() as es:
            es.enter_context(patch(f"{CQ}.score_carousel_deck", return_value={
                "deck_probe": "ok", "deck_slides": 4, "deck_template": "step_framework",
                "deck_body_chars_avg": 90.0, "deck_body_chars_max": 120, "deck_chars_dropped": 0,
                "deck_slides_clipped": 0, "deck_slides_with_band": 2}))
            _result, record, _track = self._run(es, [_carousel_post(post_type="document")])
        assert [call.args[1]["surface"] for call in record.call_args_list] == ["post", "carousel"]

    def test_a_deck_with_no_readable_render_records_that_not_a_zero(self):
        with ExitStack() as es:
            es.enter_context(patch(f"{CQ}.score_carousel_deck", return_value={
                "deck_probe": "missing", "deck_slides": None, "deck_template": None,
                "deck_body_chars_avg": None, "deck_body_chars_max": None,
                "deck_chars_dropped": None, "deck_slides_clipped": None,
                "deck_slides_with_band": None}))
            _result, record, _track = self._run(es, [_carousel_post()])
        deck_row = record.call_args_list[1].args[1]
        assert deck_row["deck_probe"] == "missing"
        assert deck_row["deck_chars_dropped"] is None and deck_row["chars"] is None

    def test_text_and_video_posts_produce_no_deck_reading(self):
        with ExitStack() as es:
            deck = es.enter_context(patch(f"{CQ}.score_carousel_deck"))
            es.enter_context(patch(f"{CQ}.score_video_asset", return_value={}))
            _result, record, _track = self._run(es, [_post(), _video_post(ref_id="2")])
        assert deck.call_count == 0
        assert [call.args[1]["surface"] for call in record.call_args_list] == ["post", "post"]

    def test_the_embedding_spend_is_billed_to_the_user_it_scored(self):
        # similarity_reports is the only LLM spend here, and this task loops over users instead of
        # taking a user_id kwarg — with no explicit scope, current_llm_attribution() has nobody to
        # bill and every embedding lands on the "system" sentinel.
        from cqc_lem.utilities.observability import current_llm_attribution
        seen = []

        def _capture(texts, history=None):
            seen.append(current_llm_attribution())
            return [{"score": 0.2, "measure": "lexical", "match": "m"} for _ in texts]

        with ExitStack() as es:
            es.enter_context(patch(f"{CQ}.similarity_reports", side_effect=_capture))
            self._run(es, [_post(), _comment()], users=(7,), post_history=["older post"],
                      comment_history=["older comment"])
        assert seen and all(scope == (7, "content") for scope in seen)
        # The scope must not leak past the user it was opened for.
        assert current_llm_attribution() == (None, None)

    def test_a_surface_with_nothing_shipped_is_not_scored_for_similarity(self):
        with ExitStack() as es:
            sim = es.enter_context(patch(f"{CQ}.similarity_reports",
                                         return_value=[{"score": 0.1, "measure": "lexical",
                                                        "match": "m"}]))
            self._run(es, [_comment()], post_history=["older post"],
                      comment_history=["older comment"])
        assert sim.call_count == 1
        assert sim.call_args.args[1] == ["older comment"]

    def test_the_item_cap_is_reported_rather_than_silently_truncating(self):
        items = [_comment(ref_id=str(i)) for i in range(8)]
        with ExitStack() as es:
            warn = es.enter_context(patch(f"{RS}.log_warning"))
            es.enter_context(patch(f"{CQ}.max_items_per_run", return_value=3))
            result, record, _track = self._run(es, items)
        assert record.call_count == 3
        assert "5 over cap" in result
        assert warn.called

    def test_users_with_nothing_shipped_are_skipped(self):
        with ExitStack() as es:
            result, record, track = self._run(es, [], users=(1, 2))
        assert "scored 0 piece(s)" in result
        assert not record.called and not track.called

    def test_disabled_flag_short_circuits(self):
        from cqc_lem.app.run_scheduler import auto_nightly_content_quality
        with patch.dict("os.environ", {"CONTENT_QUALITY_TELEMETRY_ENABLED": "false"}):
            assert auto_nightly_content_quality() == "Content quality telemetry disabled"

    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import auto_nightly_content_quality
        with patch(f"{RS}.get_active_user_ids", return_value=[]):
            assert auto_nightly_content_quality() == "No active users"

    def test_the_detector_is_not_called_when_disabled(self):
        with ExitStack() as es:
            score = es.enter_context(patch(f"{CQ}.detector_score", return_value=0.9))
            _result, record, _track = self._run(es, [_post()])
        assert not score.called
        assert record.call_args.args[1]["detector_score"] is None

    def test_the_detector_budget_caps_calls_per_user(self):
        items = [_comment(ref_id=str(i)) for i in range(5)]
        with ExitStack() as es:
            es.enter_context(patch(f"{CQ}.detector_sampled", return_value=True))
            es.enter_context(patch(f"{CQ}.detector_daily_max", return_value=2))
            detector = es.enter_context(patch(f"{CQ}.detector_score", return_value=0.7))
            _result, record, _track = self._run(es, items)
        assert detector.call_count == 2
        scored = [call.args[1]["detector_score"] for call in record.call_args_list]
        assert scored.count(0.7) == 2 and scored.count(None) == 3

    def test_an_explicit_window_overrides_the_env_default(self):
        with ExitStack() as es:
            reader = es.enter_context(patch(f"{DB}.get_shipped_content_for_quality",
                                            return_value=[]))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1]))
            from cqc_lem.app.run_scheduler import auto_nightly_content_quality
            auto_nightly_content_quality(days=5)
        assert reader.call_args.kwargs["days"] == 5


class TestWeeklyContentQualityRollup:
    def _rows(self, count, day, **kw):
        row = {"surface": "post", "ref_id": "1", "shipped_on": day, "slop_hard": 0, "slop_warn": 0,
               "slop_score": 1.0, "similarity": 0.3, "similarity_measure": "embedding",
               "authenticity_score": 90, "hook_chars": 90, "hook_within_budget": True,
               "engagement_rate": 0.05, "impressions": 1000, "detector_score": None}
        row.update(kw)
        return [dict(row, ref_id=str(i)) for i in range(count)]

    def _run(self, es, rows, users=(1,), days=7):
        from cqc_lem.app.run_scheduler import auto_weekly_content_quality
        es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=list(users)))
        es.enter_context(patch(f"{DB}.get_content_quality_scores", return_value=rows))
        track = es.enter_context(patch(f"{OBS}.track_content_quality_rollup"))
        error = es.enter_context(patch("cqc_lem.utilities.logger.log_error"))
        return auto_weekly_content_quality(days=days), track, error

    def _today(self):
        from datetime import date
        return date.today()

    def _ago(self, days):
        from datetime import timedelta
        return (self._today() - timedelta(days=days)).isoformat()

    def test_a_steady_week_is_reported_without_alerts(self):
        rows = self._rows(6, self._ago(1)) + self._rows(6, self._ago(9))
        with ExitStack() as es:
            result, track, error = self._run(es, rows)
        assert "1/1" in result and "0 regression" in result
        assert track.called and not error.called
        rollup = track.call_args.args[1]
        assert rollup["current"]["items"] == 6 and rollup["prior"]["items"] == 6

    def test_a_slop_regression_is_emitted_and_logged_as_an_error(self):
        rows = (self._rows(6, self._ago(1), slop_score=5.0)
                + self._rows(6, self._ago(9), slop_score=1.0))
        with ExitStack() as es:
            result, track, error = self._run(es, rows)
        assert "1 regression alert(s)" in result
        assert error.call_count == 1
        assert "slop_regression" in error.call_args.args[0]
        assert error.call_args.kwargs["task_name"] == "auto_weekly_content_quality"
        assert [a["name"] for a in track.call_args.args[1]["alerts"]] == ["slop_regression"]

    def test_an_engagement_collapse_alerts_without_a_prior_period(self):
        rows = self._rows(6, self._ago(1), engagement_rate=0.001)
        with ExitStack() as es:
            result, _track, error = self._run(es, rows)
        assert "1 regression alert(s)" in result
        assert "engagement_floor" in error.call_args.args[0]

    def test_every_alert_gets_its_own_log_line(self):
        rows = (self._rows(6, self._ago(1), slop_score=5.0, similarity=0.6, engagement_rate=0.001)
                + self._rows(6, self._ago(9)))
        with ExitStack() as es:
            result, _track, error = self._run(es, rows)
        assert "3 regression alert(s)" in result
        assert error.call_count == 3

    def test_a_user_who_shipped_nothing_this_period_is_not_reported(self):
        # Only the PRIOR period has rows — there is no current quality to report and, critically,
        # nothing to call a regression.
        with ExitStack() as es:
            result, track, error = self._run(es, self._rows(6, self._ago(9)))
        assert "0/1" in result
        assert not track.called and not error.called

    def test_no_scored_rows_at_all_reports_nothing(self):
        with ExitStack() as es:
            result, track, error = self._run(es, [])
        assert "0/1" in result and not track.called and not error.called

    def test_reads_two_periods_of_history(self):
        with ExitStack() as es:
            reader = es.enter_context(patch(f"{DB}.get_content_quality_scores", return_value=[]))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1]))
            from cqc_lem.app.run_scheduler import auto_weekly_content_quality
            auto_weekly_content_quality(days=7)
        assert reader.call_args.kwargs["days"] == 14

    def test_disabled_flag_short_circuits(self):
        from cqc_lem.app.run_scheduler import auto_weekly_content_quality
        with patch.dict("os.environ", {"CONTENT_QUALITY_TELEMETRY_ENABLED": "false"}):
            assert auto_weekly_content_quality() == "Content quality telemetry disabled"

    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import auto_weekly_content_quality
        with patch(f"{RS}.get_active_user_ids", return_value=[]):
            assert auto_weekly_content_quality() == "No active users"


class TestPostHogEvents:
    def test_content_quality_event_never_carries_the_body(self):
        from cqc_lem.utilities.observability import track_content_quality
        score = {"surface": "post", "ref_id": "1", "shipped_on": "2026-07-26", "chars": 400,
                 "slop_checked": True, "slop_hard": 1, "slop_warn": 0, "slop_score": 3.0,
                 "slop_checks": ["tada_transition"],
                 "slop_reasons": ["tada_transition: uses ... the user's own sentence"],
                 "similarity": 0.4, "similarity_measure": "embedding", "authenticity_score": 80,
                 "hook_chars": 90, "hook_within_budget": True, "engagement_rate": 0.03,
                 "impressions": 900, "detector_score": None, "detector_provider": None,
                 "video_render_ok": True, "video_model_tier": "gen4_turbo",
                 "video_duration_seconds": 5, "video_aspect_ratio": "9:16",
                 "video_asset_probe": "ok"}
        with patch(f"{OBS}.posthog.capture") as capture:
            track_content_quality(7, score)
        props = capture.call_args.kwargs["properties"]
        assert capture.call_args.kwargs["event"] == "content_quality"
        assert capture.call_args.kwargs["distinct_id"] == "7"
        assert props["slop_checks"] == ["tada_transition"]
        assert props["engagement_rate"] == 0.03
        assert props["video_render_ok"] is True
        assert props["video_model_tier"] == "gen4_turbo"
        # The lint's reason strings quote the draft, so they must not ride along.
        assert "slop_reasons" not in props
        assert not any("sentence" in str(v) for v in props.values())

    def test_the_deck_reading_rides_the_same_event_with_string_filters(self):
        """The deck reading is an existing EventSpec's fields, never a new capture (issue #1513).

        The two properties a breakdown filters on ingest as strings.
        """
        from cqc_lem.utilities.observability import track_content_quality
        deck = {"surface": "carousel", "ref_id": "87", "shipped_on": "2026-08-14", "chars": 740,
                "deck_probe": "ok", "deck_slides": 5, "deck_template": "bold_listicle",
                "deck_body_chars_avg": 148.0, "deck_body_chars_max": 196,
                "deck_chars_dropped": 57, "deck_slides_clipped": 1, "deck_slides_with_band": 3}
        with patch(f"{OBS}.posthog.capture") as capture:
            track_content_quality(7, deck)
        capture.assert_called_once()
        props = capture.call_args.kwargs["properties"]
        assert capture.call_args.kwargs["event"] == "content_quality"
        assert props["surface"] == "carousel" and isinstance(props["surface"], str)
        assert props["deck_probe"] == "ok" and isinstance(props["deck_probe"], str)
        assert props["deck_template"] == "bold_listicle"
        assert props["deck_chars_dropped"] == 57 and props["deck_slides_clipped"] == 1
        assert props["deck_slides"] == 5 and props["deck_slides_with_band"] == 3

    def test_an_unmeasured_deck_never_ingests_as_zero(self):
        from cqc_lem.utilities.observability import track_content_quality
        with patch(f"{OBS}.posthog.capture") as capture:
            track_content_quality(7, {"surface": "carousel", "ref_id": "9",
                                      "deck_probe": "missing", "deck_slides": None,
                                      "deck_chars_dropped": None})
        props = capture.call_args.kwargs["properties"]
        assert props["deck_chars_dropped"] is None and props["deck_slides"] is None
        assert props["deck_probe"] == "missing"

    def test_a_text_post_carries_no_deck_dimensions(self):
        from cqc_lem.utilities.observability import track_content_quality
        with patch(f"{OBS}.posthog.capture") as capture:
            track_content_quality(7, {"surface": "post", "ref_id": "1", "chars": 400})
        props = capture.call_args.kwargs["properties"]
        assert props["deck_probe"] is None and props["deck_template"] is None
        assert props["deck_slides"] is None

    def test_rollup_event_flattens_both_periods_and_the_deltas(self):
        from cqc_lem.utilities.observability import track_content_quality_rollup
        rollup = {"days": 7,
                  "current": {"items": 6, "slop_score_avg": 4.0, "by_surface": {"post": 6}},
                  "prior": {"items": 5, "slop_score_avg": 1.0, "by_surface": {"post": 5}},
                  "deltas": {"slop_score_avg": 3.0},
                  "alerts": [{"name": "slop_regression", "reason": "rose"}],
                  "config": {"min_sample": 5}}
        with patch(f"{OBS}.posthog.capture") as capture:
            track_content_quality_rollup(3, rollup)
        props = capture.call_args.kwargs["properties"]
        assert capture.call_args.kwargs["event"] == "content_quality_rollup"
        assert props["current_slop_score_avg"] == 4.0
        assert props["prior_slop_score_avg"] == 1.0
        assert props["delta_slop_score_avg"] == 3.0
        assert props["alerts"] == ["slop_regression"] and props["alert_count"] == 1
        assert props["by_surface"] == {"post": 6}
        # by_surface is a dict; flattening it per period would create unqueryable property names.
        assert "current_by_surface" not in props

    def test_a_systemless_user_id_falls_back_to_the_sentinel(self):
        from cqc_lem.utilities.observability import track_content_quality
        with patch(f"{OBS}.posthog.capture") as capture:
            track_content_quality(None, {"surface": "post"})
        assert capture.call_args.kwargs["distinct_id"] == "system"


class TestBeatSchedule:
    def _beat(self):
        from cqc_lem.app.my_celery import app
        return app.conf.beat_schedule

    def test_nightly_entry_runs_after_the_stats_scrape(self):
        entry = self._beat()["nightly-content-quality"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_nightly_content_quality"
        # 02:40 UTC is after the 23:00 post-stats scrape, so yesterday's posts have impressions.
        assert entry["schedule"].hour == {2} and entry["schedule"].minute == {40}

    def test_weekly_entry_is_the_last_monday_report(self):
        entry = self._beat()["weekly-content-quality"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_weekly_content_quality"
        assert entry["schedule"].hour == {9} and entry["schedule"].minute == {45}


class TestNewsletterSelfSimilarity:
    """#1284. Newsletter editions had no body-history reader, so their self-similarity was recorded
    as unmeasured on every run — which reads as "nothing to see" in the rollup. The real corpus sat
    at 0.68-0.83 embedding cosine against itself while that field was NULL.
    """

    _run = TestNightlyContentQuality._run

    def test_editions_are_graded_against_the_users_own_editions(self):
        with ExitStack() as es:
            sim = es.enter_context(patch(f"{CQ}.similarity_reports",
                                         return_value=[{"score": 0.81, "measure": "embedding",
                                                        "match": "older edition"}]))
            _result, record, _track = self._run(es, [_newsletter()],
                                                newsletter_history=["older edition"])
        assert sim.call_count == 1
        assert sim.call_args.args[1] == ["older edition"]
        assert record.call_args.args[1]["similarity"] == 0.81

    def test_each_surface_still_reads_only_its_own_history(self):
        with ExitStack() as es:
            sim = es.enter_context(patch(f"{CQ}.similarity_reports",
                                         return_value=[{"score": 0.3, "measure": "embedding",
                                                        "match": "m"}]))
            self._run(es, [_post(), _comment(), _newsletter()], post_history=["older post"],
                      comment_history=["older comment"], newsletter_history=["older edition"])
        histories = [call.args[1] for call in sim.call_args_list]
        assert histories.count(["older edition"]) == 1
        assert ["older post"] in histories and ["older comment"] in histories

    def test_no_edition_history_reports_unmeasured_rather_than_unique(self):
        with ExitStack() as es:
            _result, record, _track = self._run(es, [_newsletter()], newsletter_history=[])
        score = record.call_args.args[1]
        assert score["similarity"] is None and score["similarity_measure"] == "none"
