"""Unit tests for the live LinkedIn validation probe (scripts/linkedin_live_validation.py)."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "linkedin_live_validation.py"
_spec = importlib.util.spec_from_file_location("linkedin_live_validation", SCRIPT)
llv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llv)

# The two layouts the 2026-07-23 owner grab found on /analytics/post-summary/: the Discovery hero
# stacks value-first, the Engagement breakdown label-first.
ANALYTICS_TEXT = "Discovery\n72\nImpressions\nEngagement\nReactions\n4\nComments\n1\nReposts\n0\nSaves\n3"


def _fake_driver(text: str = "", current_url: str = ""):
    driver = MagicMock()
    element = MagicMock()
    element.text = text
    driver.find_element.return_value = element
    driver.current_url = current_url
    return driver


@pytest.mark.unit
class TestLabelLines:
    def test_captures_each_label_with_its_neighbours(self):
        lines = llv.label_lines(ANALYTICS_TEXT)
        assert "72 | Impressions | Engagement" in lines
        assert "Engagement | Reactions | 4" in lines
        assert "1 | Reposts | 0" in lines

    def test_ignores_prose_that_merely_contains_a_label_word(self):
        assert llv.label_lines("Save this checklist for later\nSomething else") == []

    def test_blank_text_is_empty(self):
        assert llv.label_lines("") == []
        assert llv.label_lines(None) == []


@pytest.mark.unit
class TestSignalSources:
    def test_attributes_each_signal_to_the_page_that_yielded_it(self):
        out = llv.signal_sources({"reactions": 4, "comments": 1},
                                 {"reactions": 4, "impressions": 72, "saves": 3})
        assert out["reactions"]["source"] == "both"
        assert out["comments"]["source"] == "detail"
        assert out["impressions"]["source"] == "analytics"
        assert out["saves"]["source"] == "analytics"
        assert out["reposts"]["source"] == "none"

    def test_value_is_the_max_so_a_blank_view_cannot_zero_a_signal(self):
        out = llv.signal_sources({"impressions": 0}, {"impressions": 72})
        assert out["impressions"]["value"] == 72

    def test_missing_and_none_values_are_treated_as_zero(self):
        out = llv.signal_sources({}, {"saves": None})
        assert out["saves"] == {"detail": 0, "analytics": 0, "source": "none", "value": 0}


@pytest.mark.unit
class TestMediaClassification:
    def test_document_token_wins_over_image_nodes(self):
        anchors = [{"testid": "", "cls": "update-components-image", "aria": ""},
                   {"testid": "document-container", "cls": "", "aria": ""}]
        assert llv.media_verdict(anchors) == "document"

    def test_image_share_has_no_document_token(self):
        anchors = [{"testid": "", "cls": "update-components-image", "aria": "carousel"}]
        assert llv.media_verdict(anchors) == "image"

    def test_no_anchors_is_unknown_not_a_verdict(self):
        assert llv.media_verdict([]) == "unknown"
        assert llv.classify_media_anchor({"cls": "feed-shared-text"}) == "unknown"

    def test_aria_label_alone_identifies_a_document(self):
        assert llv.classify_media_anchor({"aria": "Document: 2026 playbook"}) == "document"


@pytest.mark.unit
class TestFindDocumentAffordance:
    def test_matches_the_add_a_document_control(self):
        assert llv.find_document_affordance(["Add a photo", "Add a document"]) == "Add a document"

    def test_returns_none_when_the_composer_offers_no_document_control(self):
        assert llv.find_document_affordance(["Add a photo", "Celebrate an occasion"]) is None
        assert llv.find_document_affordance(None) is None


@pytest.mark.unit
class TestProbePostStats:
    def test_visits_the_analytics_page_for_the_redirected_activity_urn(self, monkeypatch):
        driver = _fake_driver(ANALYTICS_TEXT,
                              current_url="https://www.linkedin.com/feed/update/urn:li:activity:99/")
        monkeypatch.setattr(llv, "_activity_urn", lambda d, u: "urn:li:activity:99")
        report = llv.probe_post_stats(driver, "https://www.linkedin.com/feed/update/urn:li:share:1/",
                                      counts_fn=lambda c: {"impressions": 72, "saves": 3},
                                      sleep=lambda s: None)

        assert report["activity_urn"] == "urn:li:activity:99"
        assert driver.get.call_args_list[-1][0][0] == \
            "https://www.linkedin.com/analytics/post-summary/urn:li:activity:99/"
        assert report["signals"]["saves"]["value"] == 3
        assert "72 | Impressions | Engagement" in report["analytics_lines"]

    def test_skips_the_analytics_hop_when_no_urn_resolves(self, monkeypatch):
        driver = _fake_driver("4 reactions")
        monkeypatch.setattr(llv, "_activity_urn", lambda d, u: None)
        report = llv.probe_post_stats(driver, "https://example.com/not-a-post",
                                      counts_fn=lambda c: {"reactions": 4}, sleep=lambda s: None)

        assert driver.get.call_count == 1
        assert report["signals"]["reactions"]["source"] == "detail"
        assert report["analytics_lines"] == []

    def test_a_missing_main_container_yields_empty_counts_not_a_crash(self, monkeypatch):
        driver = MagicMock()
        driver.find_element.side_effect = Exception("no main")
        monkeypatch.setattr(llv, "_activity_urn", lambda d, u: None)
        report = llv.probe_post_stats(driver, "https://www.linkedin.com/feed/update/urn:li:share:1/",
                                      counts_fn=lambda c: {"reactions": 4}, sleep=lambda s: None)
        assert report["signals"]["reactions"]["source"] == "none"


@pytest.mark.unit
class TestProbeDocumentRender:
    def test_reports_the_verdict_and_tags_each_anchor(self):
        driver = MagicMock()
        driver.execute_script.return_value = [{"tag": "div", "testid": "document-container",
                                               "cls": "", "aria": ""}]
        report = llv.probe_document_render(driver, "https://www.linkedin.com/feed/update/x/",
                                           sleep=lambda s: None)
        assert report["verdict"] == "document"
        assert report["anchors"][0]["kind"] == "document"

    def test_a_script_failure_is_reported_not_raised(self):
        driver = MagicMock()
        driver.execute_script.side_effect = Exception("boom")
        report = llv.probe_document_render(driver, "https://www.linkedin.com/feed/update/x/",
                                           sleep=lambda s: None)
        assert report["verdict"] == "unknown"
        assert "boom" in report["error"]


@pytest.mark.unit
class TestProbeComposer:
    def test_captures_control_labels_and_the_document_affordance(self, monkeypatch):
        driver = MagicMock()
        button = MagicMock()
        button.get_attribute.return_value = "Add a document"
        dialog = MagicMock()
        dialog.find_elements.return_value = [button]
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first",
                            lambda *a, **k: MagicMock())
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            lambda *a, **k: dialog)

        report = llv.probe_composer(driver, sleep=lambda s: None)
        assert report["opened"] is True
        assert report["document_affordance"] == "Add a document"

    def test_a_stale_control_mid_enumeration_still_reports_what_was_captured(self, monkeypatch):
        driver = MagicMock()
        dialog = MagicMock()
        dialog.find_elements.side_effect = Exception("stale element")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first",
                            lambda *a, **k: MagicMock())
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            lambda *a, **k: dialog)

        report = llv.probe_composer(driver, sleep=lambda s: None)
        assert report["opened"] is True
        assert report["controls"] == ["<enumeration stopped: Exception>"]
        assert report["document_affordance"] is None

    def test_reports_a_composer_that_never_opened(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first", lambda *a, **k: None)
        report = llv.probe_composer(MagicMock(), sleep=lambda s: None)
        assert report == {"opened": False, "controls": [], "document_affordance": None}


@pytest.mark.unit
class TestMain:
    def test_requires_something_to_probe(self):
        with pytest.raises(SystemExit):
            llv.main([])
