"""The video fallback after a refused source frame (showcase round 4).

The default is the $0 branded title card; Pexels is reachable only behind its opt-in flag, and
then only with a query built from Stage 1's concrete anchors — "model retirement" came back as an
elderly couple when the raw post words were the query.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.utilities.content_quality import (
    VIDEO_MODEL_PEXELS,
    VIDEO_MODEL_TITLE_CARD,
    video_model_tier,
)

pytestmark = pytest.mark.unit

_CARD = "cqc_lem.utilities.video_title_card.create_title_card_video"
_STOCK = "cqc_lem.utilities.pexels_helper.download_pexels_video"
_QUERY = "cqc_lem.utilities.video_title_card.stock_query_from_concept"


def _fallback(concept=None, **kw):
    with patch(f"{rcp.__name__}._persist_video_model") as persist, \
            patch(f"{rcp.__name__}.create_folder_if_not_exists"):
        src = rcp._fallback_video_src("A model retired at 2am.", user_id=1, post_id=9,
                                      concept=concept, ratio=kw.pop("ratio", "1:1"),
                                      byline="Jane Doe")
    return src, persist


class TestDefaultIsTheTitleCard:
    def test_no_stock_is_ever_searched_by_default(self, monkeypatch):
        monkeypatch.delenv("VIDEO_PEXELS_FALLBACK_ENABLED", raising=False)
        with patch(_CARD, return_value="/a/title_card_9_x.mp4") as card, \
                patch(_STOCK) as stock:
            src, persist = _fallback(SimpleNamespace(hook_phrase="Our cron caught it"),
                                     ratio="9:16")
        assert src == "/a/title_card_9_x.mp4"
        stock.assert_not_called()
        persist.assert_called_once_with(9, VIDEO_MODEL_TITLE_CARD)
        kwargs = card.call_args.kwargs
        assert kwargs["ratio"] == "9:16" and kwargs["byline"] == "Jane Doe"
        assert kwargs["concept"].hook_phrase == "Our cron caught it"

    def test_no_card_clears_the_model(self, monkeypatch):
        monkeypatch.delenv("VIDEO_PEXELS_FALLBACK_ENABLED", raising=False)
        with patch(_CARD, return_value=None):
            src, persist = _fallback()
        assert src is None
        persist.assert_called_once_with(9, None)


class TestPexelsOptIn:
    @pytest.fixture(autouse=True)
    def _opted_in(self, monkeypatch):
        monkeypatch.setenv("VIDEO_PEXELS_FALLBACK_ENABLED", "true")

    def test_a_safe_anchor_query_is_used(self):
        with patch(_QUERY, return_value="cron schedule printout"), \
                patch(_STOCK, return_value="/a/pexels_1.mp4") as stock, \
                patch(_CARD) as card:
            src, persist = _fallback(SimpleNamespace())
        assert src == "/a/pexels_1.mp4"
        assert stock.call_args[0][0] == "cron schedule printout"
        card.assert_not_called()
        persist.assert_called_once_with(9, VIDEO_MODEL_PEXELS)

    def test_no_safe_query_goes_straight_to_the_card(self):
        with patch(_QUERY, return_value=None), patch(_STOCK) as stock, \
                patch(_CARD, return_value="/a/title_card_9.mp4"):
            src, _ = _fallback(SimpleNamespace())
        stock.assert_not_called()
        assert src == "/a/title_card_9.mp4"

    @pytest.mark.parametrize("stock_effect", [{"return_value": None},
                                              {"side_effect": RuntimeError("401")}])
    def test_a_stock_miss_or_fault_still_ships_the_card(self, stock_effect):
        with patch(_QUERY, return_value="sticky notes"), patch(_STOCK, **stock_effect), \
                patch(_CARD, return_value="/a/title_card_9.mp4"):
            src, persist = _fallback(SimpleNamespace())
        assert src == "/a/title_card_9.mp4"
        persist.assert_called_once_with(9, VIDEO_MODEL_TITLE_CARD)


class TestTelemetry:
    @pytest.mark.parametrize("src,label", [("https://runway/x.mp4", "runway"),
                                           ("/a/videos/title_card/title_card_9_ab.mp4",
                                            "title_card"),
                                           ("/a/videos/pexels/pexels_1.mp4", "pexels")])
    def test_the_probe_names_the_source(self, src, label):
        assert rcp._video_source_label(src) == label

    def test_the_stored_card_reads_as_its_own_tier(self):
        url = "https://x/api/assets?file_name=videos/runwayml/title_card_9_ab.mp4"
        assert video_model_tier(None, url) == VIDEO_MODEL_TITLE_CARD
