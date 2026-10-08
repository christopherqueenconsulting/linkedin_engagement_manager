"""Showcase round 6: curated audience fit and the link preview image.

A curated candidate must fit the author's readers before any commentary is written, and a
link post needs the publisher's fetchable preview image.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from cqc_lem.app import run_curated_sources as rcs
from cqc_lem.utilities import curated_collectors as cc, curated_sources as cs
from cqc_lem.utilities.ai import curated_commentary as ccm

pytestmark = pytest.mark.unit

_M = "cqc_lem.app.run_curated_sources"
SMB = {"id": 4, "platform": "rss", "url": "https://example.com/a", "author": "Jane Doe",
       "publisher": "Example Co", "title": "Payroll tools for small business owners",
       "excerpt": "Owners cut payroll time with AI.", "licence": "editorial",
       "canonical_id": None, "link_only": False, "og_image_url": None}
TEEN = {**SMB, "id": 6, "title": "A college planner for teens", "publisher": "Teen News",
        "excerpt": "Students plan their applications."}
PREFS = {"focus_topics": ["AI for payroll"],
         "audience_mix": {"primary_audience": "Small-business owners", "secondary_share": 0.4,
                          "secondary_audience": "Ops leaders", "secondary_focus_topics":
                              ["AI operations"]}}


class TestTheBrief:
    def test_it_reads_both_audiences_and_every_topic(self):
        brief = cs.audience_brief(PREFS)
        assert brief == {"audiences": ["Small-business owners", "Ops leaders"],
                         "topics": ["AI for payroll", "AI operations"]}

    def test_nothing_described_is_none(self):
        assert cs.audience_brief({}) is None
        assert cs.audience_brief(None) is None

    def test_the_floor_is_tunable(self, monkeypatch):
        assert cs.audience_fit_min() == cs.AUDIENCE_FIT_MIN_DEFAULT
        monkeypatch.setenv("CURATED_AUDIENCE_FIT_MIN", "7.5")
        assert cs.audience_fit_min() == 7.5
        monkeypatch.setenv("CURATED_AUDIENCE_FIT_MIN", "nope")
        assert cs.audience_fit_min() == cs.AUDIENCE_FIT_MIN_DEFAULT

    def test_the_token_fallback(self):
        brief = cs.audience_brief(PREFS)
        assert cs.audience_token_fit(SMB, brief) >= cs.AUDIENCE_FIT_MIN_DEFAULT
        assert cs.audience_token_fit(TEEN, brief) == 0.0
        assert cs.audience_token_fit(TEEN, None) == 10.0
        assert cs.audience_token_fit({}, brief) == 0.0


class TestTheScoringCall:
    def test_it_parses_and_clamps(self):
        with patch.object(ccm, "_complete", return_value='{"score": 14, "why": "x"}') as call:
            assert ccm.score_audience_fit(SMB, cs.audience_brief(PREFS)) == 10.0
        prompt = call.call_args.args[0][1]["content"]
        assert "Small-business owners" in prompt and "Payroll tools" in prompt
        assert call.call_args.kwargs == {"model": "lem-simple", "json_mode": True}

    def test_a_failure_is_none(self):
        with patch.object(ccm, "_complete", side_effect=RuntimeError("down")), \
             patch.object(ccm, "log_warning") as warn:
            assert ccm.score_audience_fit(SMB, cs.audience_brief(PREFS)) is None
        warn.assert_called_once()
        with patch.object(ccm, "_complete", return_value="{}"), patch.object(ccm, "log_warning"):
            assert ccm.score_audience_fit(SMB, cs.audience_brief(PREFS)) is None

    def test_no_brief_costs_no_call(self):
        with patch.object(ccm, "_complete") as call:
            assert ccm.score_audience_fit(SMB, None) is None
        call.assert_not_called()


class TestTheSlotDropsAnOffAudienceItem:
    def test_the_teen_planner_is_dropped_and_the_next_item_drafted(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")
        scores = {6: 1.0, 4: 8.0}
        with patch(f"{_M}.get_curated_neighbors", return_value=([], [])), \
             patch(f"{_M}.get_draftable_curated_sources", return_value=[TEEN, SMB]), \
             patch(f"{_M}.get_recent_curated_publishers", return_value=["Example Co"]), \
             patch(f"{_M}.score_audience_fit", side_effect=lambda s, b: scores[s["id"]]), \
             patch(f"{_M}.update_curated_source_status") as status, \
             patch(f"{_M}.track_curated_source"), \
             patch(f"{_M}.draft_curated_post", return_value="Take.") as draft:
            out = rcs.curated_content_for_slot(1, 10, "value",
                                               load_voice=lambda: (PREFS, "voice"))
        assert out == "Take."
        assert draft.call_args.args[2]["id"] == 4
        assert status.call_args.args == (6, "blocked")
        assert status.call_args.kwargs["block_reason"] == "audience_fit:1.0"

    def test_the_fallback_runs_when_the_call_cannot(self):
        with patch(f"{_M}.score_audience_fit", return_value=None):
            assert rcs.audience_fit(1, TEEN, cs.audience_brief(PREFS)) == (False, 0.0)
            fits, score = rcs.audience_fit(1, SMB, cs.audience_brief(PREFS))
        assert fits and score >= 5

    def test_no_brief_means_everything_fits(self):
        assert rcs.audience_fit(1, TEEN, None) == (True, 10.0)

    def test_draft_attempts_stay_bounded(self, monkeypatch):
        monkeypatch.setenv("CURATED_SOURCES_ENABLED", "true")
        pool = [{**SMB, "id": i} for i in range(10)]
        with patch(f"{_M}.get_curated_neighbors", return_value=([], [])), \
             patch(f"{_M}.get_draftable_curated_sources", return_value=pool), \
             patch(f"{_M}.get_recent_curated_publishers", return_value=[]), \
             patch(f"{_M}.score_audience_fit", return_value=9.0) as score, \
             patch(f"{_M}.draft_curated_post", return_value=None) as draft:
            assert rcs.curated_content_for_slot(1, 10, "value",
                                                load_voice=lambda: (PREFS, "voice")) is None
        assert draft.call_count == rcs.DRAFT_CANDIDATES
        assert score.call_count == rcs.DRAFT_CANDIDATES


class TestTheLinkPreview:
    def test_a_known_fetchable_image_is_kept(self):
        source = {**SMB, "og_image_url": "https://example.com/og.png"}
        with patch(f"{_M}.image_fetchable", return_value=True), \
             patch(f"{_M}.discover_og_image") as discover:
            assert rcs._link_preview(source, 1) == "https://example.com/og.png"
        discover.assert_not_called()

    def test_a_missing_image_is_discovered_and_recorded(self):
        source = dict(SMB)
        with patch(f"{_M}.discover_og_image", return_value="https://example.com/new.png"), \
             patch(f"{_M}.image_fetchable", return_value=True), \
             patch(f"{_M}.update_curated_source_og_image") as record:
            assert rcs._link_preview(source, 1) == "https://example.com/new.png"
        record.assert_called_once_with(4, "https://example.com/new.png")
        assert source["og_image_url"] == "https://example.com/new.png"

    def test_a_discovered_image_that_will_not_load_is_no_preview(self):
        # Showcase round 8: a discovered og:image is verified like a collected one.
        source = dict(SMB)
        with patch(f"{_M}.discover_og_image", return_value="https://example.com/dead.png"), \
             patch(f"{_M}.image_fetchable", return_value=False), \
             patch(f"{_M}.update_curated_source_og_image") as record:
            assert rcs._link_preview(source, 1) is None
        record.assert_not_called()

    def test_none_found_is_none(self):
        with patch(f"{_M}.discover_og_image", return_value=None), \
             patch(f"{_M}.update_curated_source_og_image") as record:
            assert rcs._link_preview(dict(SMB), 1) is None
        record.assert_not_called()

    def test_a_link_with_no_preview_is_skipped_before_any_commentary(self):
        with patch(f"{_M}.extract_chart_facts", return_value={}), \
             patch(f"{_M}._link_preview", return_value=None), \
             patch(f"{_M}.update_curated_source_status") as status, \
             patch(f"{_M}.track_curated_source"), \
             patch(f"{_M}.generate_curated_commentary") as gen:
            assert rcs.draft_curated_post(1, 10, dict(SMB), "value") is None
        gen.assert_not_called()
        assert status.call_args.kwargs["block_reason"] == "link_no_preview_image"


def _response(status=200, ctype="text/html", text=""):
    response = MagicMock(status_code=status, text=text)
    response.headers = {"content-type": ctype}
    return response


class TestPreviewDiscovery:
    PAGE = ('<html><head><meta property="og:image" content="/img/card.png">'
            '<meta name="twitter:image" content="https://cdn.example.com/t.png"></head></html>')

    def test_the_og_image_is_resolved_and_verified(self):
        def get(url, **_kw):
            if url == "https://example.com/a":
                return _response(text=self.PAGE)
            return _response(ctype="image/png")

        with patch.object(cc.requests, "get", side_effect=get):
            assert cc.discover_og_image("https://example.com/a") == "https://example.com/img/card.png"

    def test_a_non_image_answer_falls_to_the_next_tag(self):
        def get(url, **_kw):
            if url == "https://example.com/a":
                return _response(text=self.PAGE)
            if url.endswith("card.png"):
                return _response(ctype="text/html")
            return _response(ctype="image/png; charset=binary")

        with patch.object(cc.requests, "get", side_effect=get):
            assert cc.discover_og_image("https://example.com/a") == "https://cdn.example.com/t.png"

    @pytest.mark.parametrize("url", [None, "", "ftp://x"])
    def test_no_url_is_none(self, url):
        assert cc.discover_og_image(url) is None

    def test_an_unreadable_page_is_none(self):
        with patch.object(cc.requests, "get", side_effect=requests.ConnectionError("x")):
            assert cc.discover_og_image("https://example.com/a") is None
        with patch.object(cc.requests, "get", return_value=_response(status=404)):
            assert cc.discover_og_image("https://example.com/a") is None
        with patch.object(cc.requests, "get", return_value=_response(text="<html></html>")):
            assert cc.discover_og_image("https://example.com/a") is None

    def test_image_fetchable(self):
        assert cc.image_fetchable(None) is False
        assert cc.image_fetchable("http://insecure.example.com/x.png") is False
        with patch.object(cc.requests, "get", return_value=_response(ctype="image/jpeg")):
            assert cc.image_fetchable("https://example.com/x.jpg") is True
        with patch.object(cc.requests, "get", return_value=_response(status=403,
                                                                    ctype="image/jpeg")):
            assert cc.image_fetchable("https://example.com/x.jpg") is False
        with patch.object(cc.requests, "get", side_effect=requests.Timeout("slow")):
            assert cc.image_fetchable("https://example.com/x.jpg") is False
