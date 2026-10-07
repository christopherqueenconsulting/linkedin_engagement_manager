"""Curated sources orchestration: collector beat, slot drafter, publish gate (docs/curated-sources.md).

Acceptance (#2260):
- a political / paywalled / NC candidate never reaches PENDING — the drafter re-screens and blocks;
- at most one curated post in three, never a promo slot;
- no curated post publishes without a HUMAN approval (`user:<id>`), automatic approvers refused;
- every published curated post carries a `Source:` line, whatever the owner edited.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app import run_curated_sources as rcs
from cqc_lem.utilities.db import PostStatus

pytestmark = pytest.mark.unit

_M = "cqc_lem.app.run_curated_sources"

SOURCE = {"id": 4, "platform": "rss", "url": "https://example.com/a", "author": "Jane Doe",
          "publisher": "Example Co", "title": "Agents in finance",
          "excerpt": "Teams cut handling time by 30%.", "licence": "editorial",
          "canonical_id": None, "link_only": False, "og_image_url": "https://example.com/og.png"}
LI_SOURCE = {**SOURCE, "id": 5, "platform": "linkedin", "url":
             "https://www.linkedin.com/feed/update/urn:li:share:9/", "canonical_id":
             "urn:li:share:9", "licence": "linkedin_native", "publisher": "LinkedIn"}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")


class TestCollect:
    def test_no_enabled_users_is_a_quiet_no_op(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "false")
        with patch(f"{_M}.get_active_user_ids", return_value=[1]), \
             patch(f"{_M}.collect_rss") as rss:
            assert "No users" in rcs.collect_curated_sources.run()
        rss.assert_not_called()

    def test_collects_and_reports(self, flag_on):
        with patch(f"{_M}.get_active_user_ids", return_value=[1, 2]), \
             patch(f"{_M}.load_feed_allowlist", return_value={"feeds": [], "gov_series": []}), \
             patch(f"{_M}.collect_rss", return_value={"feeds": 2, "items": 4, "new": 3,
                                                      "blocked": 1, "failed_feeds": 1}), \
             patch(f"{_M}.collect_gov_data", return_value={"series": 1, "new": 1, "blocked": 0}), \
             patch(f"{_M}.track_curated_source") as track:
            out = rcs.collect_curated_sources.run()
        assert out == "Collected 4 new / 1 blocked for 2 user(s)"
        assert track.call_args.args == ("collect", "ok")
        assert track.call_args.kwargs == {"new": 4, "blocked": 1}

    def test_every_feed_failing_reports_failed(self, flag_on):
        with patch(f"{_M}.get_active_user_ids", return_value=[1]), \
             patch(f"{_M}.load_feed_allowlist", return_value={}), \
             patch(f"{_M}.collect_rss", return_value={"feeds": 0, "items": 0, "new": 0,
                                                      "blocked": 0, "failed_feeds": 3}), \
             patch(f"{_M}.collect_gov_data", return_value={"series": 0, "new": 0, "blocked": 0}), \
             patch(f"{_M}.track_curated_source") as track:
            rcs.collect_curated_sources.run()
        assert track.call_args.args == ("collect", "failed")

    def test_the_beat_names_a_registered_task(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["collect-curated-sources"]
        assert entry["task"] in app.tasks


class TestDraft:
    @pytest.fixture
    def writes(self):
        with patch(f"{_M}.update_curated_source_status") as status, \
             patch(f"{_M}.attach_curated_source_to_post", return_value=True) as attach, \
             patch(f"{_M}.update_db_post_image_url") as image, \
             patch(f"{_M}.track_curated_source"):
            yield status, attach, image

    @pytest.mark.parametrize("over,reason", [
        ({"title": "Senator says AI is great"}, "political"),
        ({"paywalled": True}, "paywall"),
        ({"licence": "cc-by-nc"}, "licence_nc"),
        ({"author": "", "publisher": ""}, "no_provenance"),
    ])
    def test_blocked_sources_never_reach_a_draft(self, writes, over, reason):
        status, attach, _ = writes
        with patch(f"{_M}.generate_curated_commentary") as gen:
            assert rcs.draft_curated_post(1, 10, {**SOURCE, **over}, "value") is None
        gen.assert_not_called()
        attach.assert_not_called()
        assert status.call_args.args[1] == "blocked"
        assert status.call_args.kwargs["block_reason"].startswith(reason)

    def test_a_linkedin_share_is_drafted_as_a_reshare(self, writes):
        status, attach, image = writes
        with patch(f"{_M}.extract_chart_facts") as chart, \
             patch(f"{_M}.generate_curated_commentary", return_value="Take. Source: Jane Doe.") \
                as gen:
            assert rcs.draft_curated_post(1, 10, LI_SOURCE, "authority") == \
                "Take. Source: Jane Doe."
        chart.assert_not_called()
        assert gen.call_args.args[2] == "reshare"
        attach.assert_called_once_with(10, 5, "reshare")
        assert status.call_args.args == (5, "drafted")
        image.assert_not_called()

    def test_chartable_figures_are_recharted(self, writes):
        _, attach, image = writes
        with patch(f"{_M}.extract_chart_facts", return_value={"thesis_stat": {}}), \
             patch(f"{_M}.validated_chart", return_value={"stat": {}}), \
             patch(f"{_M}._render_rechart", return_value="https://x/api/assets?f=1"), \
             patch(f"{_M}.generate_curated_commentary", return_value="Take."):
            rcs.draft_curated_post(1, 10, SOURCE, "value")
        attach.assert_called_once_with(10, 4, "rechart")
        image.assert_called_once_with(10, "https://x/api/assets?f=1")

    def test_a_refused_render_falls_back_to_a_link(self, writes):
        _, attach, image = writes
        with patch(f"{_M}.extract_chart_facts", return_value={"thesis_stat": {}}), \
             patch(f"{_M}.validated_chart", return_value={"stat": {}}), \
             patch(f"{_M}._render_rechart", return_value=None), \
             patch(f"{_M}.generate_curated_commentary", return_value="Take."):
            rcs.draft_curated_post(1, 10, SOURCE, "value")
        attach.assert_called_once_with(10, 4, "link")
        image.assert_not_called()

    def test_no_figures_is_a_link_post(self, writes):
        _, attach, _ = writes
        with patch(f"{_M}.extract_chart_facts", return_value={}), \
             patch(f"{_M}.generate_curated_commentary", return_value="Take."):
            rcs.draft_curated_post(1, 10, SOURCE, "value")
        attach.assert_called_once_with(10, 4, "link")

    def test_refused_commentary_and_political_commentary_block(self, writes):
        status, attach, _ = writes
        with patch(f"{_M}.extract_chart_facts", return_value={}), \
             patch(f"{_M}.generate_curated_commentary", return_value=None):
            assert rcs.draft_curated_post(1, 10, SOURCE, "value") is None
        assert status.call_args.kwargs["block_reason"] == "commentary_refused"
        with patch(f"{_M}.extract_chart_facts", return_value={}), \
             patch(f"{_M}.generate_curated_commentary",
                   return_value="This is like an election for tools."):
            assert rcs.draft_curated_post(1, 10, SOURCE, "value") is None
        assert status.call_args.kwargs["block_reason"].startswith("commentary_political")
        attach.assert_not_called()

    def test_no_treatment_blocks(self, writes):
        status, _, _ = writes
        with patch(f"{_M}.extract_chart_facts", return_value={}), \
             patch(f"{_M}.pick_treatment", return_value=None):
            assert rcs.draft_curated_post(1, 10, SOURCE, "value") is None
        assert status.call_args.args[1] == "blocked"
        assert status.call_args.kwargs["block_reason"].endswith("no_treatment")

    def test_a_refused_render_with_no_link_fallback_blocks(self, writes):
        status, attach, _ = writes
        with patch(f"{_M}.extract_chart_facts", return_value={"thesis_stat": {}}), \
             patch(f"{_M}.validated_chart", return_value={"stat": {}}), \
             patch(f"{_M}._render_rechart", return_value=None), \
             patch(f"{_M}.treatment_allowed",
                   side_effect=lambda s, t: MagicMock(ok=t != "link" and t != "reshare")):
            assert rcs.draft_curated_post(1, 10, SOURCE, "value") is None
        assert status.call_args.kwargs["block_reason"] == "rechart_failed"
        attach.assert_not_called()

    def test_attach_failure_drafts_nothing(self, writes):
        status, attach, _ = writes
        attach.return_value = False
        with patch(f"{_M}.extract_chart_facts", return_value={}), \
             patch(f"{_M}.generate_curated_commentary", return_value="Take."):
            assert rcs.draft_curated_post(1, 10, SOURCE, "value") is None
        status.assert_not_called()


class TestRenderRechart:
    def test_stores_the_drawn_card(self, tmp_path):
        drawn = tmp_path / "card.png"
        drawn.write_bytes(b"png")
        with patch("cqc_lem.utilities.ai.image_graphics.render_graphic",
                   return_value=MagicMock(path=str(drawn))) as render, \
             patch("cqc_lem.utilities.post_image.store_rendered_post_image",
                   return_value="https://x/img") as store:
            assert rcs._render_rechart(1, 10, SOURCE, {"stat": {"display": "30%"}}) == \
                "https://x/img"
        assert render.call_args.args[0] == "stat_card"
        assert render.call_args.kwargs["surface"] == "post_image"
        store.assert_called_once_with(1, str(drawn), 10)
        assert not drawn.exists()

    def test_an_untraceable_figure_is_refused_before_render(self):
        from cqc_lem.utilities.ai.image_graphics import UngroundedFactError
        with patch("cqc_lem.utilities.ai.image_graphics.render_graphic",
                   side_effect=UngroundedFactError("45% does not trace")), \
             patch("cqc_lem.utilities.post_image.store_rendered_post_image") as store:
            assert rcs._render_rechart(1, 10, SOURCE, {"stat": {}}) is None
        store.assert_not_called()

    def test_hook(self):
        assert rcs._chart_hook({"title": "one two three four five six seven eight nine"}) == \
            "one two three four five six seven eight"
        assert rcs._chart_hook({}) == "The numbers"


class TestSlot:
    def test_off_without_the_flag(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "false")
        with patch(f"{_M}.get_curated_neighbors") as neighbors:
            assert rcs.curated_content_for_slot(1, 10, "value") is None
        neighbors.assert_not_called()

    @pytest.mark.parametrize("mix", ["promo", None, ""])
    def test_never_a_promo_or_unclassified_slot(self, flag_on, mix):
        with patch(f"{_M}.get_curated_neighbors") as neighbors:
            assert rcs.curated_content_for_slot(1, 10, mix) is None
        neighbors.assert_not_called()

    @pytest.mark.parametrize("neighbors", [None, ([False, True], []), ([], [True])])
    def test_the_ceiling_holds(self, flag_on, neighbors):
        with patch(f"{_M}.get_curated_neighbors", return_value=neighbors), \
             patch(f"{_M}.get_draftable_curated_sources") as sources:
            assert rcs.curated_content_for_slot(1, 10, "value") is None
        sources.assert_not_called()

    def test_claims_the_slot_with_the_first_draftable_source(self, flag_on):
        voice = MagicMock(return_value=({"tone": "x"}, "synth"))
        with patch(f"{_M}.get_curated_neighbors", return_value=([False, False], [False])), \
             patch(f"{_M}.get_draftable_curated_sources", return_value=[SOURCE, LI_SOURCE]), \
             patch(f"{_M}.draft_curated_post", side_effect=[None, "Take."]) as draft:
            assert rcs.curated_content_for_slot(1, 10, "authority", load_voice=voice) == "Take."
        assert draft.call_count == 2
        assert draft.call_args.kwargs["profile_synthesis"] == "synth"
        voice.assert_called_once()

    def test_no_candidates_costs_no_voice_read(self, flag_on):
        voice = MagicMock()
        with patch(f"{_M}.get_curated_neighbors", return_value=([], [])), \
             patch(f"{_M}.get_draftable_curated_sources", return_value=[]):
            assert rcs.curated_content_for_slot(1, 10, "value", load_voice=voice) is None
        voice.assert_not_called()

    def test_no_post_id_and_faults(self, flag_on):
        assert rcs.curated_content_for_slot(1, None, "value") is None
        with patch(f"{_M}.get_curated_neighbors", side_effect=RuntimeError("boom")):
            assert rcs.curated_content_for_slot(1, 10, "value") is None

    def test_every_candidate_failing_is_none(self, flag_on):
        with patch(f"{_M}.get_curated_neighbors", return_value=([], [])), \
             patch(f"{_M}.get_draftable_curated_sources", return_value=[SOURCE]), \
             patch(f"{_M}.draft_curated_post", return_value=None):
            assert rcs.curated_content_for_slot(1, 10, "value") is None


def _ctx(**over):
    base = {"post_id": 10, "post_user_id": 1, "post_status": "scheduled", "approved_by": "user:1",
            "source_treatment": "reshare", "image_url": None, "curated_source_id": 5,
            "source": LI_SOURCE}
    base.update(over)
    return base


class TestPublishGate:
    def test_not_curated(self):
        assert rcs.curated_publish_refusal(None) is None

    def test_a_human_approval_publishes(self):
        assert rcs.curated_publish_refusal(_ctx()) is None
        assert rcs.curated_publish_refusal(_ctx(post_status="approved")) is None

    @pytest.mark.parametrize("approver", ["system:auto_schedule", "system:rescore", None, "",
                                          "agent:7"])
    def test_an_automatic_approval_is_refused_back_to_pending(self, approver):
        assert rcs.curated_publish_refusal(_ctx(approved_by=approver)) == (
            "not_owner_approved", PostStatus.PENDING)

    @pytest.mark.parametrize("status", ["pending", "planning", "posted", "rejected"])
    def test_an_unapproved_status_is_refused(self, status):
        assert rcs.curated_publish_refusal(_ctx(post_status=status)) == (
            "not_approved", PostStatus.PENDING)

    def test_source_problems_hold_at_error(self):
        assert rcs.curated_publish_refusal(_ctx(source=None))[0] == "source_missing"
        assert rcs.curated_publish_refusal(_ctx(source_treatment="screenshot"))[0] == \
            "no_treatment"
        blocked = rcs.curated_publish_refusal(_ctx(source={**LI_SOURCE, "excerpt":
                                                           "The senator agrees."}))
        assert blocked[0].startswith("blocked:political") and blocked[1] == PostStatus.ERROR
        no_urn = rcs.curated_publish_refusal(_ctx(source={**LI_SOURCE, "canonical_id": None}))
        assert no_urn == ("treatment_not_allowed", PostStatus.ERROR)


class TestPublish:
    _RS = "cqc_lem.utilities.linkedin.reshare"

    @pytest.mark.parametrize("treatment,source", [("reshare", LI_SOURCE), ("rechart", SOURCE),
                                                  ("link", SOURCE)])
    def test_every_treatment_publishes_with_a_source_line(self, treatment, source):
        context = _ctx(source_treatment=treatment, source=source, image_url="https://x/img")
        with patch(f"{self._RS}.share_reshare_on_linkedin", return_value="urn:li:share:1") as rs, \
             patch(f"{self._RS}.share_curated_image_on_linkedin",
                   return_value="urn:li:share:2") as img, \
             patch(f"{self._RS}.share_article_on_linkedin", return_value="urn:li:share:3") as art, \
             patch("cqc_lem.utilities.post_image.post_image_abs_path", return_value="/a/x.png"), \
             patch(f"{_M}.update_curated_source_status") as status, \
             patch(f"{_M}.track_curated_source"):
            # The owner's edit dropped the credit line; publish puts it back.
            body, urn = rcs.publish_curated_post(1, 10, "My edited take.", context)
        assert "Source: Jane Doe" in body
        assert urn
        assert status.call_args.args == (source["id"], "published")
        called = {"reshare": rs, "rechart": img, "link": art}[treatment]
        assert "Source: Jane Doe" in called.call_args.args[1]
        if treatment == "reshare":
            assert called.call_args.args[2] == "urn:li:share:9"
        if treatment == "link":
            assert called.call_args.args[2] == SOURCE["url"]
            assert called.call_args.args[5] == SOURCE["og_image_url"]

    def test_a_refused_reshare_blocks_the_source(self):
        from cqc_lem.utilities.linkedin.reshare import ReshareRefused
        with patch(f"{self._RS}.share_reshare_on_linkedin", side_effect=ReshareRefused("403")), \
             patch(f"{_M}.update_curated_source_status") as status, \
             patch(f"{_M}.track_curated_source"):
            _, urn = rcs.publish_curated_post(1, 10, "Take", _ctx())
        assert urn is None
        assert status.call_args.kwargs["block_reason"] == "reshare_refused"

    def test_a_rechart_without_its_image_publishes_nothing(self):
        context = _ctx(source_treatment="rechart", source=SOURCE)
        with patch("cqc_lem.utilities.post_image.post_image_abs_path", return_value=None), \
             patch(f"{self._RS}.share_curated_image_on_linkedin") as img, \
             patch(f"{_M}.update_curated_source_status") as status:
            assert rcs.publish_curated_post(1, 10, "Take", context)[1] is None
        img.assert_not_called()
        status.assert_not_called()
