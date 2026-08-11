"""Unit tests for the live LinkedIn validation probe (scripts/linkedin_live_validation.py)."""

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "linkedin_live_validation.py"
_spec = importlib.util.spec_from_file_location("linkedin_live_validation", SCRIPT)
llv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llv)

# The two layouts the 2026-07-23 owner grab found on /analytics/post-summary/: the Discovery hero
# stacks value-first, the Engagement breakdown label-first.
ANALYTICS_TEXT = "Discovery\n72\nImpressions\nEngagement\nReactions\n4\nComments\n1\nReposts\n0\nSaves\n3"


def clear_the_breaker(monkeypatch):
    """Let `main()` past the #1301 breaker gate.

    Every `main()` case has to say this out loud: the gate fails CLOSED, so in a unit environment
    (no Redis) the probe REFUSES by default. A test that walks through main is asserting what
    happens after the gate opens, never that the gate is absent.
    """
    monkeypatch.setattr(llv, "breaker_reading",
                        lambda: {"readable": True, "open": False, "wait_seconds": 0,
                                 "reason": "clear"})


def _fake_driver(text: str = "", current_url: str = ""):
    driver = MagicMock()
    element = MagicMock()
    element.text = text
    driver.find_element.return_value = element
    driver.current_url = current_url
    return driver


def _sort_control(text: str, aria: str = ""):
    control = MagicMock()
    control.text = text
    control.tag_name = "button"
    control.get_attribute.side_effect = lambda a: {"aria-label": aria}.get(a)
    return control


def _find_first_returning(results: list):
    """`find_first` answering each call of the probe's chain in order: trigger, Recent option,
    trigger again after the flip. Running out means the probe stopped early, which is the point of
    several of these cases.
    """
    calls = iter(results)

    def _find_first(*_a, **_k):
        return next(calls, None)

    return _find_first


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
        driver = MagicMock()
        driver.find_elements.return_value = []
        report = llv.probe_composer(driver, sleep=lambda s: None)
        assert report["opened"] is False
        assert report["controls"] == []
        assert report["document_affordance"] is None
        assert report["chain_source"] == "image"
        # No share box AND no page text: the feed never rendered, so this run grounds nothing.
        assert report["state"] == llv.STATE_UNKNOWN

    def test_a_rendered_feed_with_no_share_box_is_drift_not_unknown(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first", lambda *a, **k: None)
        driver = _fake_driver()
        main = MagicMock()
        main.text = "Sort by Recent  Start a conversation"
        driver.find_elements.side_effect = lambda *a, **k: [main]
        report = llv.probe_composer(driver, sleep=lambda s: None)
        assert report["state"] == llv.STATE_DRIFT

    def test_a_missing_share_box_does_not_warn(self, monkeypatch):
        """A missing share box is graded, not warned.

        The probe already grades it drift/unknown. Logging that miss as a warning escalates to a
        RecurringWarning in error tracking for expected best-effort drift.
        """
        calls = []
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first",
                            lambda *a, **k: calls.append(k) or None)
        driver = MagicMock()
        driver.find_elements.return_value = []
        llv.probe_composer(driver, sleep=lambda s: None)
        assert any(k.get("warn_on_miss") is False for k in calls), calls


@pytest.mark.unit
class TestGroupShareBoxChainCopy:
    """The probe runs inside a Selenium worker whose `cqc_lem` is the DEPLOYED image.

    A pre-merge grounding pass cannot import the chain it is grounding. The script therefore carries
    a copy — and a copy that drifts grounds a chain nothing ships, which is worse than not probing
    at all.
    """

    def test_carried_chain_is_identical_to_the_one_the_run_uses(self):
        from cqc_lem.app.engagement import feed

        assert llv._CARRIED_GROUP_SHARE_BOX_LOCATORS == feed._GROUP_SHARE_BOX_LOCATORS

    def test_the_running_image_wins_when_it_has_a_chain(self):
        locators, source = llv._share_box_chains()
        assert source == "image"
        from cqc_lem.app.engagement import feed

        assert locators == feed._GROUP_SHARE_BOX_LOCATORS

    def test_falls_back_to_the_carried_copy_on_an_image_that_predates_1107(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_chain(name, *a, **k):
            if name == "cqc_lem.app.engagement.feed":
                raise ImportError("cannot import name '_GROUP_SHARE_BOX_LOCATORS'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_chain)
        locators, source = llv._share_box_chains()
        assert source == "script"
        assert locators == llv._CARRIED_GROUP_SHARE_BOX_LOCATORS


@pytest.mark.unit
class TestMessageThreadProbe:
    """#731: the probe's job is to name the route that won, so the NEXT rotation is visible early."""

    def test_verdict_names_the_winning_route(self):
        assert llv.message_thread_verdict({"opened": True, "route": "anchor", "events": 12,
                                           "self_name": "Christopher Queen"}) == "opened via anchor"

    def test_the_search_route_is_not_probe_able_and_says_so_instead_of_crashing(self, monkeypatch):
        """The production ladder's last resort types a name and presses Enter.

        The #1301 guard refuses that — a box that takes text and commits on Enter is, from here,
        indistinguishable from a composer — so the probe must report an UNKNOWN naming the guard,
        not die mid-run and not read as a broken route.
        """
        thread = types.ModuleType("cqc_lem.utilities.linkedin.message_thread")
        thread.open_message_thread = MagicMock(
            side_effect=llv.ReadOnlyViolation("refused to type text"))
        thread.read_last_sender = lambda d: ""
        thread.profile_urn_from_page = lambda d, u: ""
        monkeypatch.setitem(sys.modules, "cqc_lem.utilities.linkedin.message_thread", thread)

        reading = llv.probe_message_thread(MagicMock(), "https://www.linkedin.com/in/jane/")
        assert reading["state"] == llv.STATE_UNKNOWN
        assert "messaging-search route" in reading["verdict"]
        assert reading["read_only_blocked"]

    def test_a_readable_thread_with_no_saved_name_is_still_unknown(self):
        # The other half of a live reply check: the ladder can win and the verdict still be UNKNOWN
        # because Settings has no display name to compare the sender against.
        verdict = llv.message_thread_verdict({"opened": True, "route": "anchor", "events": 12,
                                              "self_name": ""})
        assert "no LinkedIn display name" in verdict and "unknown" in verdict

    def test_an_unreadable_thread_is_not_a_clean_pass(self):
        verdict = llv.message_thread_verdict({"opened": True, "route": "direct_url", "events": 0})
        assert "no message events" in verdict and "unknown" in verdict

    def test_no_route_opened_is_its_own_verdict(self):
        assert llv.message_thread_verdict({"opened": False}) == "no route opened a thread"
        assert llv.message_thread_verdict(None) == "no route opened a thread"

    def test_probe_reports_the_route_surface_and_reader_output(self, monkeypatch):
        from cqc_lem.utilities.linkedin.message_thread import ThreadOpen
        opened = ThreadOpen(opened=True, route="anchor", events=18, composer=True,
                            surface="overlay", tried=["anchor"])
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.open_message_thread",
                            lambda *a, **k: opened)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.read_last_sender",
                            lambda d: "Jane Doe")
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.profile_urn_from_page",
                            lambda *_a: "urn:li:fsd_profile:ABC")
        report = llv.probe_message_thread(MagicMock(), "https://x/in/jane", "Jane Doe",
                                          self_name="Christopher Queen", sleep=lambda s: None)
        assert report["route"] == "anchor" and report["surface"] == "overlay"
        assert report["events"] == 18 and report["last_sender"] == "Jane Doe"
        assert report["profile_urn"] == "urn:li:fsd_profile:ABC"
        assert report["verdict"] == "opened via anchor"
        # The sender is someone else, so a live run of this thread would stop the sequence.
        assert report["reply_state"] == "replied"

    def test_probe_reports_the_reply_state_the_sequencer_would_reach(self, monkeypatch):
        from cqc_lem.utilities.linkedin.message_thread import ThreadOpen
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.open_message_thread",
                            lambda *a, **k: ThreadOpen(opened=True, route="anchor", events=4,
                                                       surface="page", tried=["anchor"]))
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.profile_urn_from_page",
                            lambda *_a: None)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.read_last_sender",
                            lambda d: "Christopher Queen")
        ours = llv.probe_message_thread(MagicMock(), "https://x/in/jane",
                                        self_name="Christopher Queen", sleep=lambda s: None)
        assert ours["reply_state"] == "not_replied"
        # A saved name that does NOT match what LinkedIn renders is the silent failure this probe
        # exists to surface: the thread is readable, and the sequencer still skips the person.
        mismatch = llv.probe_message_thread(MagicMock(), "https://x/in/jane",
                                            self_name="", sleep=lambda s: None)
        assert mismatch["reply_state"] == "unknown"

    def test_a_ladder_that_opened_nothing_reads_no_sender(self, monkeypatch):
        from cqc_lem.utilities.linkedin.message_thread import ThreadOpen
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.open_message_thread",
                            lambda *a, **k: ThreadOpen(tried=["anchor", "button"]))
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.read_last_sender",
                            lambda d: (_ for _ in ()).throw(AssertionError("must not read")))
        monkeypatch.setattr("cqc_lem.utilities.linkedin.message_thread.profile_urn_from_page",
                            lambda *_a: None)
        report = llv.probe_message_thread(MagicMock(), "https://x/in/jane", sleep=lambda s: None)
        assert report["last_sender"] == ""
        assert report["routes_tried"] == ["anchor", "button"]


@pytest.mark.unit
class TestFeedSortProbe:
    """#817: the probe has to say WHICH half of the sort control broke — the trigger or the menu —
    because `Selector miss: Feed sort control` alone cannot, and they need different fixes.
    """

    def test_recent_is_the_only_healthy_verdict(self):
        assert llv.feed_sort_verdict({"control_found": True, "sort_before": "top",
                                      "sort_after": "recent"}) == "sort control OK — flipped to Recent"
        assert llv.feed_sort_verdict({"control_found": True, "sort_before": "recent",
                                      "sort_after": "recent"}).endswith("already on Recent")

    def test_a_missing_trigger_points_at_the_control_locators(self):
        verdict = llv.feed_sort_verdict({"control_found": False, "sort_after": "missing"})
        assert "NO sort control" in verdict and "_FEED_SORT_LOCATORS" in verdict

    def test_a_trigger_that_would_not_flip_points_at_the_option_locators(self):
        verdict = llv.feed_sort_verdict({"control_found": True, "sort_after": "top"})
        assert "_FEED_RECENT_OPTION_LOCATORS" in verdict

    def test_an_unreadable_flip_is_never_reported_as_ok(self):
        assert "unreadable" in llv.feed_sort_verdict({"control_found": True, "sort_after": "unknown"})
        assert llv.feed_sort_verdict(None).startswith("NO sort control")

    def test_probe_reports_the_control_and_the_live_labels(self, monkeypatch):
        control = _sort_control("Sort by: Top")
        recent = _sort_control("Sort by: Recent")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([control, recent, recent]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: ["Sort by: Recent"])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: ["Recent"])

        report = llv.probe_feed_sort(_fake_driver(current_url=llv.FEED_URL), sleep=lambda s: None)
        assert report["control_found"] is True
        assert report["sort_before"] == "top"
        assert report["option_found"] is True
        assert report["sort_after"] == "recent"
        assert report["visible_controls"] == ["Sort by: Recent", "Recent"]
        assert report["control"]["text"] == "Sort by: Top"
        assert report["verdict"].startswith("sort control OK")

    def test_a_flip_the_control_will_not_confirm_is_never_reported_as_recent(self, monkeypatch):
        """The lie #817 exists to stop: a clicked flip whose result is unreadable is 'unknown'."""
        control = _sort_control("Sort by: Top")
        blank = _sort_control("")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([control, blank, blank]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: [])

        report = llv.probe_feed_sort(_fake_driver(current_url=llv.FEED_URL), sleep=lambda s: None)
        assert report["sort_after"] == "unknown"
        assert "unreadable" in report["verdict"]

    def test_an_unresolved_recent_option_leaves_the_menu_open_for_the_capture(self, monkeypatch):
        """`option_found is False` is the finding: the trigger is fine, the MENU rotated — and the
        dropdown must still be open when `visible_controls` is read, or it captures nothing.
        """
        control = _sort_control("Sort by: Top")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([control, None]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: ["Top", "Recent posts"])

        report = llv.probe_feed_sort(_fake_driver(current_url=llv.FEED_URL), sleep=lambda s: None)
        assert report["option_found"] is False
        assert report["sort_after"] == "top"
        assert report["visible_controls"] == ["Top", "Recent posts"]
        assert "_FEED_RECENT_OPTION_LOCATORS" in report["verdict"]

    def test_a_session_failure_mid_flip_is_reported_as_the_cause_not_as_selectors(self, monkeypatch):
        control = _sort_control("Sort by: Top")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([control]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: [])
        driver = _fake_driver(current_url=llv.FEED_URL)
        driver.execute_script.side_effect = RuntimeError("session deleted")

        report = llv.probe_feed_sort(driver, sleep=lambda s: None)
        assert report["sort_after"] == "unknown"
        assert "session deleted" in report["flip_error"]

    def test_probe_reports_selector_evidence_from_the_new_scan(self, monkeypatch):
        """#1270: one live ``--feed-sort`` run should validate both the locator chain AND the
        two-pass DOM sample production now emits as an event."""
        control = _sort_control("Sort by: Top")
        recent = _sort_control("Sort by: Recent")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([control, recent, recent]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "_feed_sort_evidence_scan",
                            lambda d: [{"tag": "button", "text": "Sort by", "reason": "header"}])

        report = llv.probe_feed_sort(_fake_driver(current_url=llv.FEED_URL), sleep=lambda s: None)
        assert report["selector_evidence"] == [{"tag": "button", "text": "Sort by", "reason": "header"}]

    def test_selector_evidence_scan_is_read_only_and_never_raises(self, monkeypatch):
        """A dead scan must not stop the probe from grading the real locator chain."""
        control = _sort_control("Sort by: Top")
        recent = _sort_control("Sort by: Recent")
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([control, recent, recent]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "_feed_sort_evidence_scan", lambda d: [])

        report = llv.probe_feed_sort(_fake_driver(current_url=llv.FEED_URL), sleep=lambda s: None)
        assert report["state"] == "ok"
        assert report["selector_evidence"] == []

    def test_a_control_label_naming_both_sorts_is_unreadable_not_recent(self):
        """Production reads a both-sorts label as unknown; a probe that called it 'recent' would
        report the control healthy on exactly the reading that leaves the run unsorted.
        """
        assert llv.control_sort_state(_sort_control("", "Sort by, currently Top, options Recent")) == ""
        assert llv.control_sort_state(_sort_control("Sort by: Recent")) == "recent"

    def test_menu_rows_are_captured_before_the_page_is_full_of_list_items(self):
        """One comma-joined selector returns DOCUMENT order, and a feed page's nav/rail/post <li>s
        come before an overlay dropdown — so the cap would be spent on furniture and the menu this
        probe exists to re-ground could be missing from its own capture.
        """
        def _item(text):
            el = MagicMock()
            el.text = text
            el.is_displayed.return_value = True
            el.get_attribute.return_value = None
            return el

        furniture = [_item(f"Nav {i}") for i in range(60)]
        menu = [_item("Top"), _item("Recent")]
        driver = MagicMock()
        driver.find_elements.side_effect = lambda by, sel: menu if "role=" in sel else furniture

        labels = llv.menu_item_labels(driver)
        assert labels[:2] == ["Top", "Recent"]
        assert len(labels) == 40

    def test_menu_item_labels_skips_hidden_and_duplicate_rows(self):
        def _item(text, displayed=True):
            el = MagicMock()
            el.text = text
            el.is_displayed.return_value = displayed
            el.get_attribute.return_value = None
            return el

        driver = MagicMock()
        driver.find_elements.return_value = [_item("Recent"), _item("Recent"),
                                             _item("Top", displayed=False), _item("")]
        assert llv.menu_item_labels(driver) == ["Recent"]


@pytest.mark.unit
class TestFeedSortCandidates:
    """#1108: `visible_controls` graded the drift but could not re-ground it.

    It lists `<button>` labels, and the finding WAS that the live feed has no button between the
    nav and the first post. `sort_candidates` is the capture a trigger chain can be written from.
    """

    @staticmethod
    def _candidate(tag="div", displayed=True, **attrs):
        el = MagicMock()
        el.tag_name = tag
        el.text = attrs.pop("text", "")
        el.is_displayed.return_value = displayed
        el.get_attribute.side_effect = lambda a: attrs.get(a)
        return el

    def test_captures_the_anchors_a_locator_can_be_written_from(self):
        driver = MagicMock()
        driver.find_elements.return_value = [self._candidate(
            tag="div", text="Recent", **{"role": "button", "aria-label": "Sort feed",
                                         "data-testid": "feed-sort-toggle", "aria-haspopup": "true",
                                         "href": None})]
        [row] = llv.feed_sort_candidates(driver)
        assert row["tag"] == "div" and row["role"] == "button"
        assert row["aria_label"] == "Sort feed" and row["data_testid"] == "feed-sort-toggle"
        assert row["aria_haspopup"] == "true" and row["text"] == "Recent"

    def test_skips_hidden_elements(self):
        driver = MagicMock()
        driver.find_elements.return_value = [self._candidate(text="Top", displayed=False),
                                             self._candidate(text="Recent")]
        assert [r["text"] for r in llv.feed_sort_candidates(driver)] == ["Recent"]

    def test_a_hidden_element_that_cannot_answer_is_skipped_not_fatal(self):
        stale = self._candidate(text="Top")
        stale.is_displayed.side_effect = RuntimeError("stale element")
        driver = MagicMock()
        driver.find_elements.return_value = [stale, self._candidate(text="Recent")]
        assert [r["text"] for r in llv.feed_sort_candidates(driver)] == ["Recent"]

    def test_the_cap_is_spent_on_the_header_because_the_order_is_the_dom_order(self):
        """The header comes first in `main`, so a cap hit deep in the feed still carries it.

        The #1108 reading spent all 40 of its label slots on post furniture.
        """
        driver = MagicMock()
        driver.find_elements.return_value = [self._candidate(text=f"c{i}") for i in range(50)]
        rows = llv.feed_sort_candidates(driver)
        assert len(rows) == 20
        assert [r["text"] for r in rows[:2]] == ["c0", "c1"]

    def test_the_selector_is_scoped_to_main_and_covers_non_button_affordances(self):
        selector = llv.SORT_CANDIDATE_SELECTOR
        assert "[role='button']" in selector and "[aria-haspopup]" in selector
        assert "a[href]" in selector
        assert all(part.strip().startswith("main ") for part in selector.split(","))

    def test_an_unreadable_page_reports_the_cause_instead_of_an_empty_list(self):
        """An empty list means 'the feed rendered nothing', which grades UNKNOWN.

        A failed query must not be able to say that.
        """
        driver = MagicMock()
        driver.find_elements.side_effect = RuntimeError("invalid session id")
        [row] = llv.feed_sort_candidates(driver)
        assert "RuntimeError" in row["error"]

    def test_the_probe_emits_the_candidates_before_the_labels(self, monkeypatch):
        """The issue body truncates the evidence blob.

        The half a re-grounding pass reads has to be the half that survives the cut.
        """
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                            _find_first_returning([None]))
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: ["Comment"])
        monkeypatch.setattr(llv, "menu_item_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "feed_sort_candidates",
                            lambda d, **k: [{"tag": "a", "text": "Recent"}])

        report = llv.probe_feed_sort(_fake_driver(current_url=llv.FEED_URL), sleep=lambda s: None)
        keys = list(report)
        assert keys.index("sort_candidates") < keys.index("visible_controls")
        assert report["sort_candidates"] == [{"tag": "a", "text": "Recent"}]
        assert "`sort_candidates`" in report["verdict"]


@pytest.mark.unit
class TestFeedSortChainCopy:
    """The probe runs inside a Selenium worker whose `cqc_lem` is the DEPLOYED image, so a pre-merge
    grounding pass cannot import the chain it is grounding. The script therefore carries a copy —
    and a copy that drifts grounds a chain nothing ships, which is worse than not probing at all.
    """

    def test_carried_chain_is_identical_to_the_one_the_run_uses(self):
        from cqc_lem.app.engagement import feed

        assert llv.FALLBACK_SORT_LOCATORS == feed._FEED_SORT_LOCATORS
        assert llv.FALLBACK_RECENT_OPTION_LOCATORS == feed._FEED_RECENT_OPTION_LOCATORS

    def test_sort_states_match_the_ones_the_run_records(self):
        from cqc_lem.app.engagement import feed

        assert (llv.SORT_RECENT, llv.SORT_TOP) == (feed.FEED_SORT_RECENT, feed.FEED_SORT_TOP)
        assert (llv.SORT_MISSING, llv.SORT_UNKNOWN) == (feed.FEED_SORT_MISSING,
                                                        feed.FEED_SORT_UNKNOWN)

    def test_the_running_image_wins_when_it_has_a_chain(self):
        sort, option, source = llv.feed_sort_chains()
        assert source == "image"
        from cqc_lem.app.engagement import feed
        assert sort == feed._FEED_SORT_LOCATORS and option == feed._FEED_RECENT_OPTION_LOCATORS

    def test_falls_back_to_the_carried_copy_on_an_image_that_predates_817(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_chain(name, *a, **k):
            if name == "cqc_lem.app.engagement.feed":
                raise ImportError("cannot import name '_FEED_SORT_LOCATORS'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_chain)
        sort, option, source = llv.feed_sort_chains()
        assert source == "script"
        assert sort == llv.FALLBACK_SORT_LOCATORS and option == llv.FALLBACK_RECENT_OPTION_LOCATORS

    def test_a_reading_taken_from_the_carried_copy_says_so(self):
        verdict = llv.feed_sort_verdict({"control_found": True, "sort_before": "top",
                                         "sort_after": "recent", "chain_source": "script"})
        assert verdict.startswith("sort control OK")
        assert "predates #817" in verdict
        assert "predates" not in llv.feed_sort_verdict({"control_found": True, "sort_after": "recent",
                                                        "chain_source": "image"})


@pytest.mark.unit
class TestRecommendationReadCopy:
    """#1007's read is NEW, so the image the probe is piped into does not have it — and grounding a
    rebuilt reader only after it merges is exactly how the ladder it replaces shipped dead. Same
    posture as the feed-sort chain: image first, carried copy otherwise, and the reading says which.
    A copy that drifts grounds a read nothing ships, which is worse than not probing at all.
    """

    def test_carried_read_is_identical_to_the_one_outreach_uses(self):
        from cqc_lem.app.engagement import outreach as ra

        assert llv.FALLBACK_RECOMMENDATION_ROWS_JS == ra._RECOMMENDATION_ROWS_JS
        assert llv.FALLBACK_RECOMMENDATION_RENDER_ATTEMPTS == ra._RECOMMENDATION_RENDER_ATTEMPTS

    def test_the_running_image_wins_when_it_has_the_read(self):
        from cqc_lem.app.engagement import outreach as ra

        read, attempts, source = llv.recommendation_read()
        assert source == "image"
        assert read is ra._recommendation_reading
        assert attempts == ra._RECOMMENDATION_RENDER_ATTEMPTS

    def test_falls_back_to_the_carried_copy_on_an_image_that_predates_1007(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_read(name, *a, **k):
            if name == "cqc_lem.app.engagement.outreach":
                raise ImportError("cannot import name '_recommendation_reading'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_read)
        read, attempts, source = llv.recommendation_read()
        assert source == "script"
        assert read is llv._carried_recommendation_reading
        assert attempts == llv.FALLBACK_RECOMMENDATION_RENDER_ATTEMPTS

    def test_the_carried_copy_normalizes_what_the_page_hands_back(self):
        driver = MagicMock()
        driver.execute_script.return_value = {"rows": [{"href": "u"}, "junk"], "anchors": "24",
                                              "page_dated": 1}
        assert llv._carried_recommendation_reading(driver) == {
            "rows": [{"href": "u"}], "anchors": 24, "page_dated": True}

    def test_a_read_that_blows_up_is_an_empty_read_not_a_dead_probe(self):
        empty = {"rows": [], "anchors": 0, "page_dated": False}
        driver = MagicMock()
        driver.execute_script.side_effect = Exception("session died mid-read")
        assert llv._carried_recommendation_reading(driver) == empty
        driver.execute_script.side_effect = None
        driver.execute_script.return_value = "not a dict"
        assert llv._carried_recommendation_reading(driver) == empty

    def test_a_reading_taken_from_the_carried_copy_says_so(self):
        verdict = llv.appreciation_verdict({"cards": 2, "dated": 2, "lookback_days": 30,
                                            "people": [], "read_source": "script"})
        assert "2 card(s), 2 dated" in verdict and "predates #1007" in verdict
        assert "predates" not in llv.appreciation_verdict(
            {"cards": 2, "dated": 2, "lookback_days": 30, "people": [], "read_source": "image"})


@pytest.mark.unit
class TestMain:
    def test_requires_something_to_probe(self):
        with pytest.raises(SystemExit):
            llv.main([])

    def test_feed_sort_alone_is_enough_to_probe(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        clear_the_breaker(monkeypatch)
        monkeypatch.setattr(llv, "probe_feed_sort", lambda d: {"verdict": "sort control OK"})
        assert llv.main(["--feed-sort"]) == 0


@pytest.mark.unit
class TestAppreciationSourcesProbe:
    """#968: the probe exists so the scrapers can be grounded BEFORE the flag is flipped, so its
    job is to distinguish 'nothing there' from 'there but unreadable'.
    """

    def test_no_cards_says_production_sends_nothing(self):
        verdict = llv.appreciation_verdict({"cards": 0})
        assert "no cards resolved" in verdict

    def test_cards_but_nothing_dated_is_the_finding_that_matters(self):
        verdict = llv.appreciation_verdict({"cards": 4, "dated": 0})
        assert "silently dead" in verdict

    def test_dated_cards_report_who_would_be_thanked(self):
        verdict = llv.appreciation_verdict({"cards": 4, "dated": 4, "lookback_days": 30,
                                            "people": [{"name": "Jane"}]})
        assert "1 inside the 30-day window" in verdict

    def test_appreciation_sources_alone_is_enough_to_probe(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        clear_the_breaker(monkeypatch)
        monkeypatch.setattr(llv, "probe_appreciation_sources",
                            lambda d, u, p="": {"mentions": {"verdict": "no cards resolved"}})
        assert llv.main(["--appreciation-sources"]) == 0

    def test_zero_cards_on_a_dated_page_names_the_read_as_rotated(self):
        """#1007's whole finding: the recommendations page rendered dated cards and the locator
        ladder resolved none. That must not read the same as an account with no recommendations.
        """
        verdict = llv.appreciation_verdict({"cards": 0, "page_dated": True, "profile_anchors": 24})
        assert "silently dead" in verdict and "24 profile link(s)" in verdict

    def test_probe_reads_both_surfaces_and_never_claims_a_ledger_row(self, monkeypatch):
        """It grounds the PRODUCTION card reads + parsers (the scrapers themselves are gated
        off until this run happens), and it must stay read-only.
        """
        from unittest.mock import patch

        link = MagicMock()
        link.get_attribute.return_value = "https://www.linkedin.com/in/jane?trk=x"
        link.text = "Jane Doe"
        card = MagicMock()
        card.text = "July 24, 2026, Jane was my client"
        card.find_elements.return_value = [link]

        driver = _fake_driver(current_url="https://www.linkedin.com/in/me")
        # Since #1007 the recommendations half is one JS read, not a locator chain.
        driver.execute_script.return_value = {
            "rows": [{"href": "https://www.linkedin.com/in/jane?trk=x", "name": "Jane Doe",
                      "text": "Jane Doe\n· 1st\nJuly 24, 2026, Jane was my client"}],
            "anchors": 24, "page_dated": True}
        with patch("cqc_lem.utilities.selenium_util.find_all_first", return_value=[card]), \
             patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False), \
             patch("cqc_lem.app.engagement.outreach.getText", side_effect=lambda el: el.text), \
             patch("cqc_lem.app.engagement.outreach._parse_recommendation_date", return_value=3.0):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        rec = report["recommendations_received"]
        assert rec["url"].endswith("/details/recommendations/")
        assert rec["cards"] == 1 and rec["dated"] == 1
        assert rec["profile_anchors"] == 24 and rec["page_dated"] is True
        assert rec["people"][0]["profile_url"] == "https://www.linkedin.com/in/jane"
        assert rec["people"][0]["name"] == "Jane Doe"
        # The mentions surface is read too, and its cards must SAY they were a mention.
        assert report["mentions"]["cards"] == 0
        assert "no cards resolved" in report["mentions"]["verdict"]

    def test_the_probe_reports_the_grounded_shape_of_this_profile(self):
        """The acceptance reading from the issue: two 2010-2012 recommendations resolve, both date,
        and NEITHER is inside the 30-day window — a fixed reader that still sends nothing today.
        """
        from unittest.mock import patch

        driver = _fake_driver(current_url="https://www.linkedin.com/in/christopherqueen")
        driver.execute_script.return_value = {
            "rows": [{"href": "https://www.linkedin.com/in/uday", "name": "Uday Shankar",
                      "text": "Uday Shankar\n· 1st\nGroup Supervisor at JHU/APL\n"
                              "April 25, 2012, Uday was Christopher's client\nWe hired Chris..."},
                     {"href": "https://www.linkedin.com/in/jeremiah", "name": "Jeremiah A. Myers",
                      "text": "Jeremiah A. Myers\n· 1st\nSr. Technical Product Manager @ AWS\n"
                              "September 14, 2010, Jeremiah A. and Christopher studied together"}],
            "anchors": 24, "page_dated": True}
        with patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False):
            report = llv.probe_appreciation_sources(
                driver, 1, "https://www.linkedin.com/in/christopherqueen/", sleep=lambda s: None)

        rec = report["recommendations_received"]
        assert (rec["cards"], rec["dated"], len(rec["people"])) == (2, 2, 0)
        assert [r["name"] for r in rec["rows"]] == ["Uday Shankar", "Jeremiah A. Myers"]
        assert rec["read_source"] == "image"

    def test_a_block_resolved_around_the_users_own_link_is_never_a_thankable_person(self):
        """The page is full of the account's OWN /in/ links (nav, the Received/Given/Pending tabs).
        Production drops a row whose href is the user themselves; a probe that reports it as someone
        production would thank is grounding a person who can never be messaged.
        """
        from unittest.mock import patch

        driver = _fake_driver(current_url="https://www.linkedin.com/in/me")
        driver.execute_script.return_value = {
            "rows": [{"href": "https://www.linkedin.com/in/me/?trk=nav", "name": "Me",
                      "text": "Me\nJuly 24, 2026, someone was my client"},
                     {"href": "https://www.linkedin.com/in/jane", "name": "Jane Doe",
                      "text": "Jane Doe\nJuly 24, 2026, Jane was my client"}],
            "anchors": 24, "page_dated": True}
        with patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False), \
             patch("cqc_lem.app.engagement.outreach._parse_recommendation_date", return_value=3.0):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        rec = report["recommendations_received"]
        assert [r["is_self"] for r in rec["rows"]] == [True, False]
        assert [p["profile_url"] for p in rec["people"]] == ["https://www.linkedin.com/in/jane"]

    def test_an_image_that_predates_the_rebuild_still_grounds_both_surfaces(self, monkeypatch):
        """The pre-merge run the owner asked for: the deployed image has no `_recommendation_reading`
        to import, so a hard import would kill the whole probe — mentions half included — instead of
        grounding the branch's read against the live DOM.
        """
        import builtins
        from unittest.mock import patch

        real_import = builtins.__import__

        def _no_read(name, *a, **k):
            if name == "cqc_lem.app.engagement.outreach" and "_recommendation_reading" in (a[2] or ()):
                raise ImportError("cannot import name '_recommendation_reading'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_read)
        driver = _fake_driver(current_url="https://www.linkedin.com/in/me")
        driver.execute_script.return_value = {
            "rows": [{"href": "https://www.linkedin.com/in/jane", "name": "Jane Doe",
                      "text": "Jane Doe\n· 1st\nJuly 24, 2026, Jane was my client"}],
            "anchors": 24, "page_dated": True}
        with patch("cqc_lem.utilities.selenium_util.find_all_first", return_value=[]), \
             patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False), \
             patch("cqc_lem.app.engagement.outreach._parse_recommendation_date", return_value=3.0):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        rec = report["recommendations_received"]
        assert rec["read_source"] == "script"
        assert (rec["cards"], rec["dated"]) == (1, 1)
        assert "predates #1007" in rec["verdict"]
        assert report["mentions"]["cards"] == 0

    def test_an_early_paint_is_re_read_before_it_counts_as_an_empty_section(self):
        """Production polls the render; the probe has to poll it too or it grounds a page that had
        not finished painting and calls the section empty.
        """
        from unittest.mock import patch

        rows = {"rows": [{"href": "https://www.linkedin.com/in/jane", "name": "Jane Doe",
                          "text": "July 24, 2026, Jane was my client"}],
                "anchors": 24, "page_dated": True}
        driver = _fake_driver(current_url="https://www.linkedin.com/in/me")
        driver.execute_script.side_effect = [{"rows": [], "anchors": 0, "page_dated": False}, rows]
        with patch("cqc_lem.utilities.selenium_util.find_all_first", return_value=[]), \
             patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False), \
             patch("cqc_lem.app.engagement.outreach._parse_recommendation_date", return_value=3.0):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        assert report["recommendations_received"]["cards"] == 1

    def test_mention_row_reports_the_name_production_would_use(self, monkeypatch):
        """A textless actor link is what the live run actually hit — the probe has to apply the same
        sentence fallback, or a blank name here would read as a probe artifact.
        """
        from unittest.mock import patch

        link = MagicMock()
        link.get_attribute.return_value = "https://www.linkedin.com/in/jane%2Ddoe%2D42"
        link.text = ""
        card = MagicMock()
        card.text = "Unread notification.\nJane Doe mentioned you in a comment 2h"
        card.find_elements.return_value = [link]

        driver = _fake_driver(current_url="https://www.linkedin.com/notifications/")
        with patch("cqc_lem.utilities.selenium_util.find_all_first", return_value=[card]), \
             patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False), \
             patch("cqc_lem.app.engagement.outreach.getText", side_effect=lambda el: el.text):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        row = report["mentions"]["rows"][0]
        assert row["name"] == "Jane Doe"
        assert row["profile_url"] == "https://www.linkedin.com/in/jane-doe-42"


@pytest.mark.unit
class TestSentInvitesProbe:
    """#969: the probe that has to run GREEN before the withdrawal lane may be switched on.

    Two readings mean "production would withdraw nothing tonight" and must be named as such rather
    than read as a clean account: no rows at all, and rows whose sent dates do not parse.
    """

    def test_no_rows_on_a_rendered_page_names_selector_drift_as_a_possibility(self):
        verdict = llv.sent_invites_verdict(
            {"rows_seen": 0, "page_text": "Manage invitations Received Sent"})
        assert "no pending-invite rows resolved" in verdict
        assert "moved" in verdict

    def test_the_pages_own_empty_state_separates_a_clean_account_from_drift(self):
        """The first live run (PR #983) came back with zero rows and no way to tell which of the two
        it was looking at. The page says so itself when it renders empty — so read that, and still
        refuse to call the anchors grounded on a run that never saw a row.
        """
        verdict = llv.sent_invites_verdict(
            {"rows_seen": 0, "page_text": "Manage invitations You have no pending invitations",
             "empty_state": "no pending invitations"})
        assert "nothing outstanding" in verdict
        assert "UNTESTED" in verdict
        assert "moved" not in verdict

    def test_no_rows_and_no_page_text_grounds_nothing(self):
        verdict = llv.sent_invites_verdict({"rows_seen": 0, "page_text": "   "})
        assert "did not render" in verdict
        assert "grounds nothing" in verdict

    def test_an_empty_state_phrase_needs_its_own_negation(self):
        """A looser pattern would match "You have 3 pending invitations" and report an empty state on
        a page FULL of rows the anchors missed — a false clean bill of health on the exact drift this
        probe exists to catch.
        """
        assert llv.sent_invite_empty_state("You have no pending invitations") == \
            "no pending invitations"
        assert llv.sent_invite_empty_state("No invitations") == "No invitations"
        assert llv.sent_invite_empty_state("You have 3 pending invitations") is None
        assert llv.sent_invite_empty_state("Manage invitations") is None
        assert llv.sent_invite_empty_state(None) is None

    def test_page_text_is_sampled_best_effort(self):
        driver = MagicMock()
        main = MagicMock()
        main.text = "You have no  pending\ninvitations"
        driver.find_elements.return_value = [main]
        assert llv.page_text_sample(driver) == "You have no pending invitations"
        broken = MagicMock()
        broken.find_elements.side_effect = RuntimeError("stale")
        assert llv.page_text_sample(broken) == ""

    def test_rows_without_dates_are_not_a_clean_account(self):
        verdict = llv.sent_invites_verdict({"rows_seen": 6, "dated": 0})
        assert "NOT ONE" in verdict
        assert "withdraws nothing" in verdict

    def test_dated_rows_report_what_production_would_attempt(self):
        verdict = llv.sent_invites_verdict({"rows_seen": 6, "dated": 6, "stale_at_threshold": 2,
                                            "threshold_days": 21})
        assert "2 at/over the 21-day threshold" in verdict

    def test_the_probe_clicks_nothing(self, monkeypatch):
        """Read-only is the whole safety story: withdrawing is one-way, so grounding the selectors
        must not withdraw anybody.
        """
        driver = MagicMock()
        driver.current_url = llv_sent_url()
        monkeypatch.setattr("cqc_lem.utilities.linkedin.stale_invites._load_more_rows",
                            lambda d, sleep=None: 0)
        monkeypatch.setattr(
            "cqc_lem.utilities.linkedin.stale_invites.read_pending_invites",
            lambda d: [{"profile_url": "/in/ann/", "name": "Ann", "text": "Sent 9 weeks ago",
                        "age_days": 63.0, "control": MagicMock()}])
        reading = llv.probe_sent_invites(driver, threshold_days=21, sleep=lambda *_: None)
        assert reading["rows_seen"] == 1 and reading["stale_at_threshold"] == 1
        assert reading["oldest_days"] == 63.0
        # driver.get navigates; nothing else on the driver is exercised by the probe.
        driver.execute_script.assert_not_called()

    def test_an_empty_account_reads_as_empty_rather_than_as_drift(self, monkeypatch):
        driver = MagicMock()
        driver.current_url = llv_sent_url()
        main = MagicMock()
        main.text = "Manage invitations\nSent\nYou have no pending invitations"
        driver.find_elements.return_value = [main]
        monkeypatch.setattr("cqc_lem.utilities.linkedin.stale_invites._load_more_rows",
                            lambda d, sleep=None: 0)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.stale_invites.read_pending_invites",
                            lambda d: [])
        reading = llv.probe_sent_invites(driver, threshold_days=21, sleep=lambda *_: None)
        assert reading["rows_seen"] == 0
        assert reading["empty_state"] == "no pending invitations"
        assert "nothing outstanding" in reading["verdict"]


def llv_sent_url():
    from cqc_lem.utilities.linkedin.stale_invites import SENT_INVITATIONS_URL
    return SENT_INVITATIONS_URL


@pytest.mark.unit
class TestThreeStateVerdicts:
    """#1013: every probe grades ok / drift / unknown, and the three are NOT interchangeable.

    `drift` is the only state that files an issue, so it may only be claimed against a page-native
    cross-check; `unknown` (the page did not render) must never be filed, or the weekly sweep puts
    the same non-finding in the backlog until it buries the real drift underneath.
    """

    def test_worst_state_is_drift_then_unknown_then_ok(self):
        assert llv.worst_state([llv.STATE_OK, llv.STATE_UNKNOWN]) == llv.STATE_UNKNOWN
        assert llv.worst_state([llv.STATE_DRIFT, llv.STATE_OK]) == llv.STATE_DRIFT
        assert llv.worst_state([llv.STATE_OK, llv.STATE_OK]) == llv.STATE_OK

    def test_a_report_that_measured_nothing_is_unknown_not_ok(self):
        """An empty grade list must never read as a clean bill of health."""
        assert llv.worst_state([]) == llv.STATE_UNKNOWN
        assert llv.worst_state([None, "nonsense"]) == llv.STATE_UNKNOWN

    def test_post_stats_labels_on_the_page_turn_zero_counts_into_drift(self):
        zeroed = {"signals": {"reactions": {"value": 0}}, "detail_lines": ["72 | Impressions | x"]}
        assert llv.post_stats_state(zeroed) == llv.STATE_DRIFT
        assert llv.post_stats_state({"signals": {"reactions": {"value": 4}}}) == llv.STATE_OK
        # No counts AND no labels: the page never rendered, which grounds nothing.
        assert llv.post_stats_state({"signals": {}, "detail_lines": []}) == llv.STATE_UNKNOWN

    def test_feed_sort_needs_visible_controls_before_it_may_claim_drift(self):
        blank = {"control_found": False, "visible_controls": []}
        assert llv.feed_sort_state(blank) == llv.STATE_UNKNOWN
        rendered = {"control_found": False, "visible_controls": ["Start a post"]}
        assert llv.feed_sort_state(rendered) == llv.STATE_DRIFT
        assert llv.feed_sort_state({"sort_after": llv.SORT_RECENT}) == llv.STATE_OK

    def test_feed_sort_candidates_alone_are_enough_to_prove_the_feed_rendered(self):
        """#1108's exact shape: a header with no `<button>` reads blank to `visible_button_labels`.

        Grading that `unknown` would excuse the very drift the probe exists to catch.
        """
        rendered = {"control_found": False, "visible_controls": [],
                    "sort_candidates": [{"tag": "div", "role": "button", "text": "Top"}]}
        assert llv.feed_sort_state(rendered) == llv.STATE_DRIFT
        assert llv.feed_sort_state({"control_found": False, "visible_controls": [],
                                    "sort_candidates": []}) == llv.STATE_UNKNOWN

    def test_sent_invites_zero_rows_reads_three_different_ways(self):
        assert llv.sent_invites_state({"rows_seen": 0, "empty_state": "no pending invitations",
                                       "page_text": "x"}) == llv.STATE_OK
        assert llv.sent_invites_state({"rows_seen": 0, "page_text": "  "}) == llv.STATE_UNKNOWN
        assert llv.sent_invites_state({"rows_seen": 0, "page_text": "Manage invitations Sent"}) == \
            llv.STATE_DRIFT
        assert llv.sent_invites_state({"rows_seen": 4, "dated": 0}) == llv.STATE_DRIFT

    def test_appreciation_no_cards_is_unknown_because_empty_and_dead_look_alike(self):
        assert llv.appreciation_state({"cards": 0}) == llv.STATE_UNKNOWN
        assert llv.appreciation_state({"cards": 3, "dated": 0}) == llv.STATE_DRIFT
        assert llv.appreciation_state({"cards": 3, "dated": 3}) == llv.STATE_OK

    def test_profile_viewers_only_calls_a_flat_scroll_drift_when_the_headline_says_theres_more(self):
        """An account with eight viewers really does have one page of them — grading that drift
        would file the same issue every week for a healthy surface.
        """
        driver = MagicMock()
        rows = [{"href": "/in/a/", "viewed": "Viewed 1h ago"}] * 8
        driver.execute_script.side_effect = [rows, 8, None, rows]
        assert llv.probe_profile_viewers(driver, sleep=lambda *_: None)["state"] == llv.STATE_OK

        driver = MagicMock()
        driver.execute_script.side_effect = [rows, 136, None, rows]
        reading = llv.probe_profile_viewers(driver, sleep=lambda *_: None)
        assert reading["state"] == llv.STATE_DRIFT
        assert "lazy-load drift" in reading["verdict"]

    def test_a_headline_stat_with_no_rows_is_drift(self):
        driver = MagicMock()
        driver.execute_script.side_effect = [[], 136, None, []]
        assert llv.probe_profile_viewers(driver, sleep=lambda *_: None)["state"] == llv.STATE_DRIFT

    def test_an_empty_viewers_page_grounds_nothing(self):
        driver = MagicMock()
        driver.execute_script.side_effect = [[], None, None, []]
        assert llv.probe_profile_viewers(driver, sleep=lambda *_: None)["state"] == \
            llv.STATE_UNKNOWN


@pytest.mark.unit
class TestSurfaceCoverageMatrix:
    """Deliverable 1: a touchpoint with no probe row is a surface that can rot silently."""

    def test_every_surface_names_a_real_cli_flag(self):
        flags = set(llv.build_parser()._option_string_actions)
        missing = [s["flag"] for s in llv.SURFACES if s["flag"] not in flags]
        assert not missing, f"surfaces name flags the CLI does not have: {missing}"

    def test_every_sweepable_surface_actually_runs_in_the_sweep(self):
        ran = []
        runners = {key: (lambda k=key: ran.append(k) or {"state": llv.STATE_OK})
                   for key in llv.SWEEP_ORDER}
        llv.run_sweep(MagicMock(), 1, runners=runners, session_state="signed_in")
        assert sorted(ran) == sorted(llv.SWEEP_ORDER)

    def test_every_sweepable_surface_has_a_default_runner(self):
        """`run_sweep` filters its keys by the runner map, so a sweepable surface with no runner is
        dropped in silence — the sweep would report full coverage while never probing it.
        """
        report = llv.run_sweep(MagicMock(), 1, session_state="signed_out")
        assert sorted(report["probes"]) == sorted(llv.SWEEP_ORDER)

    def test_the_doc_names_every_surface(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "sdui-probe-coverage.md").read_text()
        missing = [s["flag"] for s in llv.SURFACES if s["flag"] not in doc]
        assert not missing, f"docs/sdui-probe-coverage.md is missing: {missing}"


@pytest.mark.unit
class TestSweep:
    def test_a_probe_that_raises_costs_only_its_own_reading(self):
        """One rotated surface must not cost the reading of the nine that did not rotate."""
        runners = {"feed_sort": lambda: {"state": llv.STATE_OK},
                   "catchup_cards": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                   "sent_invites": lambda: {"state": llv.STATE_DRIFT}}
        report = llv.run_sweep(MagicMock(), 1, runners=runners, keys=list(runners),
                               session_state="signed_in")
        assert report["probes"]["catchup_cards"]["state"] == llv.STATE_UNKNOWN
        assert "RuntimeError" in report["probes"]["catchup_cards"]["probe_error"]
        assert report["summary"]["drift"] == ["sent_invites"]
        assert report["summary"]["ok"] == ["feed_sort"]

    def test_the_sweep_names_what_it_could_not_cover(self):
        report = llv.run_sweep(MagicMock(), 1, runners={"feed_sort": lambda: {"state": "ok"}},
                               keys=["feed_sort"], session_state="signed_in")
        assert "connect_dialog" in report["skipped"]
        assert report["surfaces"]["feed_sort"]["flag"] == "--feed-sort"

    def test_summary_state_is_worst_wins(self):
        probes = {"a": {"state": llv.STATE_OK}, "b": {"state": llv.STATE_DRIFT}}
        assert llv.sweep_summary(probes)["state"] == llv.STATE_DRIFT


@pytest.mark.unit
class TestConnectDialogProbe:
    """#1012: the surface whose last drift sent ~20 connection requests to strangers."""

    def test_a_control_naming_someone_else_is_a_hazard(self):
        labels = ["Message", "Invite Bob Smith to connect", "Invite Jane Doe to connect"]
        assert llv.rail_invite_hazards(labels, "Jane Doe") == ["Bob Smith"]

    def test_an_unattributable_control_is_a_hazard_too(self):
        """With no target name every invite control is a hazard: a control we cannot attribute is
        precisely the one production must never click.
        """
        assert llv.rail_invite_hazards(["Invite Bob Smith to connect"], "") == ["Bob Smith"]

    def test_invite_control_names_are_extracted_in_order(self):
        assert llv.invite_control_names(["Invite A B to connect", "Follow", None]) == ["A B"]

    def test_a_pending_invite_grounds_nothing_rather_than_reading_as_drift(self):
        reading = {"dialog_present": False, "page_text": "Pending", "invite_pending": True}
        assert llv.connect_dialog_state(reading) == llv.STATE_UNKNOWN
        assert "already pending" in llv.connect_dialog_verdict(reading)

    def test_a_rendered_profile_with_no_dialog_is_drift(self):
        reading = {"dialog_present": False, "page_text": "About Experience", "invite_pending": False}
        assert llv.connect_dialog_state(reading) == llv.STATE_DRIFT

    def test_the_verdict_carries_the_hazard_even_when_the_dialog_opened(self):
        reading = {"dialog_present": True, "page_text": "x", "rail_hazards": ["Bob Smith"]}
        assert llv.connect_dialog_state(reading) == llv.STATE_OK
        assert "never click one" in llv.connect_dialog_verdict(reading)

    def test_a_dialog_without_the_note_affordance_is_reported_but_never_drift(self):
        """#1039: production logs that absence at DEBUG because a spent personalized-invite quota
        hides the control. This probe is the only place the quota reading and selector rot can be
        told apart, so it says so — without claiming drift it cannot ground.
        """
        reading = {"dialog_present": True, "page_text": "x", "bare_send_present": True,
                   "note_affordance_present": False}
        assert llv.connect_dialog_state(reading) == llv.STATE_OK
        assert "no Add-a-note control" in llv.connect_dialog_verdict(reading)

    def test_a_dialog_with_the_note_affordance_says_nothing_extra(self):
        reading = {"dialog_present": True, "page_text": "x", "note_affordance_present": True,
                   "bare_send_present": True}
        assert "Add-a-note" not in llv.connect_dialog_verdict(reading)

    def _probe(self, monkeypatch, present: set[str], page_text: str = "About Experience"):
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/preload/custom-invite/?vanityName=jane"
        looked_up: list[str] = []

        def find_first(_d, _w, _locators, label, **_kwargs):
            looked_up.append(label)
            return MagicMock() if label in present else None

        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first", find_first)
        monkeypatch.setattr(llv, "element_evidence", lambda el: "aria-label=Send without a note")
        monkeypatch.setattr(llv, "page_text_sample", lambda d, **k: page_text)
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])
        monkeypatch.setattr(llv, "_page_owner_name", lambda d: "Jane Doe")
        reading = llv.probe_connect_dialog(driver, "https://www.linkedin.com/in/jane/",
                                           sleep=lambda *_: None)
        return reading, looked_up

    def test_the_probe_records_which_of_the_dialogs_controls_rendered(self, monkeypatch):
        """The verdict tests above build readings by hand, so only this one fails if the probe
        stops writing the fields the #1039 reading is made of.
        """
        reading, _looked_up = self._probe(monkeypatch, {"Connect invite dialog",
                                                        "Send without a note"})
        assert reading["dialog_present"] is True
        assert reading["note_affordance_present"] is False
        assert reading["bare_send_present"] is True
        assert reading["state"] == llv.STATE_OK
        assert "no Add-a-note control" in reading["verdict"]

    def test_a_page_with_no_dialog_reports_no_affordance_reading_at_all(self, monkeypatch):
        """None, not False: with no dialog on the page there is nothing to read the note affordance
        against, and 'no Add-a-note control' would be a drift signal nobody observed.
        """
        reading, looked_up = self._probe(monkeypatch, set())
        assert reading["dialog_present"] is False
        assert reading["note_affordance_present"] is None
        assert reading["bare_send_present"] is None
        assert "Add a note button" not in looked_up
        assert "Add-a-note" not in reading["verdict"]


@pytest.mark.unit
class TestProfileScrapeProbe:
    """The owner's #1013 comment: `span.dist-value` / `span.distance-badge` are dead, and the same
    read drives `profile.is_1st_connection` for every profile viewer.
    """

    def test_a_badge_the_page_writes_and_the_locators_miss_is_drift(self):
        reading = {"page_text": "Jane Doe · 2nd  Head of Ops", "full_name": "Jane Doe",
                   "page_degree_token": "2nd", "degree_locator_matches": []}
        assert llv.profile_scrape_state(reading) == llv.STATE_DRIFT
        assert "every profile viewer reads as a non-connection" in \
            llv.profile_scrape_verdict(reading)

    def test_a_locator_that_still_matches_is_ok(self):
        reading = {"page_text": "Jane Doe · 2nd", "full_name": "Jane Doe", "connection": "2nd",
                   "page_degree_token": "2nd", "degree_grounded": True,
                   "degree_locator_matches": [{"locator": "css=main span.dist-value",
                                               "texts": ["2nd"]}]}
        assert llv.profile_scrape_state(reading) == llv.STATE_OK

    def test_a_profile_with_no_badge_at_all_says_the_degree_half_is_ungrounded(self):
        """Your OWN profile carries no degree badge — a green verdict there would claim coverage
        the run does not have.
        """
        reading = {"page_text": "Jane Doe  Head of Ops", "full_name": "Jane Doe",
                   "page_degree_token": None, "degree_grounded": False,
                   "degree_locator_matches": []}
        assert llv.profile_scrape_state(reading) == llv.STATE_OK
        assert "UNGROUNDED" in llv.profile_scrape_verdict(reading)

    def test_a_page_that_never_rendered_grounds_nothing(self):
        assert llv.profile_scrape_state({"page_text": "", "full_name": ""}) == llv.STATE_UNKNOWN

    def test_a_rendered_page_with_no_name_is_drift(self):
        reading = {"page_text": "Something rendered", "full_name": "",
                   "parse_error": "Could not locate profile name"}
        assert llv.profile_scrape_state(reading) == llv.STATE_DRIFT
        assert "blind" in llv.profile_scrape_verdict(reading)

    def test_the_anchor_dump_reports_every_shape_the_shipped_chain_leads_with(self):
        """`degree_anchors` is what a re-grounding PR is written from (#1021 → #1031).

        A shape the shipped chain matches but the dump cannot report makes the probe come back
        empty on exactly the profile it was pointed at — a 3rd-degree badge renders as the bare
        token "3rd+", and the issue asks for a run against a 2nd/3rd-degree profile.
        """
        import re

        from cqc_lem.app.engagement.invites import _DEGREE_TOKENS
        pattern = re.search(r"/(\^.*\$)/i\.test", llv._DEGREE_ANCHOR_JS).group(1)
        badge = re.compile(pattern, re.IGNORECASE)
        for token in _DEGREE_TOKENS:
            assert badge.match(token), token
            assert badge.match(f"· {token}"), token
            assert badge.match(f"{token} degree connection"), token
        # Still a WHOLE-badge dump: a headline that merely contains a degree token is not an anchor.
        assert not badge.match("1st place, 2026 Acme awards")


@pytest.mark.unit
class TestCatchupProbe:
    """#964: the chain matched zero cards on a feed showing ten, and `no_moments` was logged daily."""

    def test_profile_anchors_with_no_matched_cards_is_the_964_shape(self):
        reading = {"cards_matched": 0, "profile_anchors": 12, "page_text": "Catch up"}
        assert llv.catchup_state(reading) == llv.STATE_DRIFT
        assert "#964 shape" in llv.catchup_verdict(reading)

    def test_a_genuinely_empty_feed_is_not_drift(self):
        reading = {"cards_matched": 0, "profile_anchors": 0, "page_text": "No new moments"}
        assert llv.catchup_state(reading) == llv.STATE_UNKNOWN

    def test_matched_cards_are_ok(self):
        assert llv.catchup_state({"cards_matched": 10, "profile_anchors": 10}) == llv.STATE_OK


@pytest.mark.unit
class TestGroupComposerProbe:
    def test_an_admin_only_group_is_unknown_not_drift(self):
        """Production already records such a group unpostable and rotates past it — that is correct
        behaviour, and filing an issue for it every week would be noise.
        """
        reading = {"page_text": "Announcements group", "share_box_present": False}
        assert llv.group_composer_state(reading) == llv.STATE_UNKNOWN

    def test_a_share_box_that_opens_nothing_is_drift(self):
        reading = {"page_text": "x", "share_box_present": True, "editor_present": False,
                   "post_button_present": True}
        assert llv.group_composer_state(reading) == llv.STATE_DRIFT
        assert "the editor" in llv.group_composer_verdict(reading)

    def test_all_three_steps_resolving_is_ok(self):
        reading = {"page_text": "x", "share_box_present": True, "editor_present": True,
                   "post_button_present": True}
        assert llv.group_composer_state(reading) == llv.STATE_OK


def _group_driver(page_text: str, scripts: list):
    """A driver whose page says `page_text` and whose execute_script answers the probe's JS reads.

    The answers come in order: the directory read, then the group-header read.
    """
    driver = MagicMock()
    element = MagicMock()
    element.text = page_text
    driver.find_elements.return_value = [element]
    driver.execute_script.side_effect = list(scripts)
    return driver


def _stubbed_group_modules(share_box):
    """The two cqc_lem imports the group-page half makes, stubbed so the unit lane never drags in Celery.

    `find_first` answers with the share box (or None).
    """
    package = types.ModuleType("cqc_lem")
    app = types.ModuleType("cqc_lem.app")
    utilities = types.ModuleType("cqc_lem.utilities")
    # The group composer moved to `app.engagement.feed` in #1154, and this stub kept naming
    # `run_automation` — so it had been inert ever since: the probe imported the REAL module and
    # the docstring's promise above was false. Named after the module the probe actually imports.
    engagement = types.ModuleType("cqc_lem.app.engagement")
    feed = types.ModuleType("cqc_lem.app.engagement.feed")
    feed._GROUP_SHARE_BOX_LOCATORS = [("xpath", "//button")]
    selenium_util = types.ModuleType("cqc_lem.utilities.selenium_util")
    selenium_util.find_first = lambda *a, **k: share_box
    return {"cqc_lem": package, "cqc_lem.app": app, "cqc_lem.utilities": utilities,
            "cqc_lem.app.engagement": engagement, "cqc_lem.app.engagement.feed": feed,
            "cqc_lem.utilities.selenium_util": selenium_util}


def _stubbed_group_modules_with_db(share_box, enabled_ids):
    """Same stubs plus `cqc_lem.utilities.db`, whose `get_enabled_group_ids` returns or raises.

    Pass an exception instance for `enabled_ids` to make the DB read fail.
    """
    modules = _stubbed_group_modules(share_box)
    db = types.ModuleType("cqc_lem.utilities.db")

    def get_enabled_group_ids(user_id):
        if isinstance(enabled_ids, Exception):
            raise enabled_ids
        return enabled_ids

    db.get_enabled_group_ids = get_enabled_group_ids
    modules["cqc_lem.utilities.db"] = db
    return modules


@pytest.mark.unit
class TestGroupMembershipProbe:
    """#1052: production commented in a group whose page was offering to let it JOIN."""

    def test_a_join_control_in_the_header_reads_not_member(self):
        assert llv.group_membership_answer(["Join", "Share"]) == "not_member"

    def test_a_leave_control_reads_member(self):
        assert llv.group_membership_answer(["Leave", "Invite"]) == "member"

    def test_a_requested_control_reads_pending_not_not_member(self):
        """A sent request is not a membership AND not an invitation to send another one."""
        assert llv.group_membership_answer(["Requested"]) == "pending"
        assert llv.group_membership_answer(["Withdraw request"]) == "pending"

    def test_a_header_that_says_neither_is_unknown_even_with_a_join_elsewhere(self):
        """The #1012 hazard: the groups-you-may-like rail renders a Join per card.

        None of them name THIS group, so only the header decides.
        """
        assert llv.group_membership_answer([]) == "unknown"
        assert llv.group_membership_answer(["Sort by", "Show more"]) == "unknown"

    def test_a_share_box_is_a_positive_membership_signal_only(self):
        assert llv.group_membership_answer([], share_box_present=True) == "member"
        assert llv.group_membership_answer(["Join"], share_box_present=True) == "not_member"

    def test_a_rendered_group_page_that_says_nothing_either_way_is_drift(self):
        reading = {"directory": {"page_text": "Groups", "enumerated": [["1", "A"]],
                                 "anchors": [{"id": "1"}]},
                   "group_page": {"group_id": "1", "page_text": "Some group", "membership": "unknown"}}
        assert llv.group_membership_state(reading) == llv.STATE_DRIFT
        assert "nothing to key on" in llv.group_membership_verdict(reading)

    def test_not_member_is_the_probe_working_not_drift(self):
        reading = {"directory": {"page_text": "Groups", "enumerated": [["1", "A"]],
                                 "anchors": [{"id": "1", "section": "Your groups"}]},
                   "group_page": {"group_id": "1", "page_text": "Some group",
                                  "membership": "not_member", "header_controls": ["Join"]}}
        assert llv.group_membership_state(reading) == llv.STATE_OK

    def test_a_control_that_only_NAMES_the_group_never_decides_membership(self):
        """The #1012 hazard inside the reading: the live header renders `More options for <group>`.

        A substring match would let the group's own NAME answer the membership question, and it would
        answer it `not_member` — the one direction that must never be wrong.
        """
        controls = ["Dismiss", "More options for Join the Data Guild", "Share"]
        assert llv.group_membership_signal(controls, share_box_present=True) == "share_box"
        assert llv.group_membership_answer(controls, share_box_present=True) == "member"
        assert llv.group_membership_answer(["More options for Leave No Trace Marketing"]) == "unknown"
        assert llv.group_membership_answer(["About Pending Innovations group"]) == "unknown"
        # The real controls still read, verb-first, however they are suffixed.
        assert llv.group_membership_answer(["Join group Data Guild"]) == "not_member"
        assert llv.group_membership_answer(["Leave this group"]) == "member"
        assert llv.group_membership_answer(["Request to join Data Guild"]) == "not_member"

    def test_a_directory_the_sync_matched_nothing_on_is_drift(self):
        reading = {"directory": {"page_text": "Groups", "enumerated": [],
                                 "anchors": [{"id": "1"}, {"id": "2"}]},
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert llv.group_membership_state(reading) == llv.STATE_DRIFT
        assert "blind" in llv.group_membership_verdict(reading)

    def test_a_directory_that_never_rendered_grounds_nothing(self):
        reading = {"directory": {"page_text": "", "enumerated": [], "anchors": []},
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert llv.group_membership_state(reading) == llv.STATE_UNKNOWN

    def test_the_probe_reports_groups_the_db_still_comments_in(self):
        """The reporter's symptom as a set difference.

        Enabled in the DB, absent from the live directory.
        """
        driver = _group_driver("Groups you manage", [
            {"headings": ["Your groups"], "anchors": [{"id": "111", "text": "A", "section": "Your groups"}]},
            {"h1": "A", "levels": 2, "header_controls": ["Leave"], "all_controls": ["Leave"]},
        ])
        with patch.dict(sys.modules, _stubbed_group_modules(None)):
            reading = llv.probe_group_membership(driver, user_id=1, enabled_ids=["111", "222"],
                                                 enumerate_groups=lambda d: [("111", "A")],
                                                 sleep=lambda *_: None)
        assert reading["stored_not_live"] == ["222"]
        assert reading["target_source"] == "db_enabled"
        assert "#1052 symptom" in reading["verdict"]

    def test_the_group_page_half_reads_the_header_and_clicks_nothing(self):
        driver = _group_driver("Some Group 1,200 members", [
            {"headings": ["Your groups"], "anchors": []},
            {"h1": "Some Group", "levels": 3, "header_controls": ["Join"],
             "all_controls": ["Join", "Join", "Sort by"]},
        ])
        with patch.dict(sys.modules, _stubbed_group_modules(None)):
            reading = llv.probe_group_membership(driver, user_id=1, group_id="777",
                                                 enabled_ids=[], enumerate_groups=lambda d: [],
                                                 sleep=lambda *_: None)
        assert reading["group_page"]["membership"] == "not_member"
        assert reading["group_page"]["join_controls_in_header"] == ["Join"]
        assert reading["target_source"] == "argument"
        driver.get.assert_called_once_with("https://www.linkedin.com/groups/777/")
        # Read-only: the probe resolves controls, it never clicks one.
        assert not any(c[0] == "click" for c in driver.method_calls)

    def test_no_group_anywhere_leaves_the_group_half_ungraded(self):
        driver = _group_driver("Groups", [{"headings": [], "anchors": []}])
        reading = llv.probe_group_membership(driver, user_id=1, enabled_ids=[],
                                             enumerate_groups=lambda d: [], sleep=lambda *_: None)
        assert reading["target_source"] == "none"
        assert llv.group_membership_state(reading) == llv.STATE_UNKNOWN
        assert "no group to probe" in reading["verdict"]


@pytest.mark.unit
class TestGroupMembershipLiveGrounding:
    """What the 2026-08-07 live run (`--group-membership`, user 1) said the probe was getting wrong.

    Each case is a reading that run actually produced.
    """

    def test_a_member_page_with_no_membership_control_names_the_share_box_as_its_source(self):
        """Live: header controls were ['Dismiss', 'Public group', …, 'More options for …'] — no Leave anywhere.

        The answer `member` came entirely from the share box, and reporting only the word sends the
        fix hunting for a control this surface does not render.
        """
        controls = ["Dismiss", "Public group", "Open about group", "Share", "Manage notifications"]
        assert llv.group_membership_signal(controls, share_box_present=True) == "share_box"
        assert llv.group_membership_answer(controls, share_box_present=True) == "member"
        reading = {"directory": {"page_text": "Groups", "enumerated": [["1", "A"]],
                                 "anchors": [{"id": "1", "section": "Your groups"}]},
                   "group_page": {"group_id": "1", "page_text": "Some group", "membership": "member",
                                  "membership_signal": "share_box", "header_controls": controls}}
        verdict = llv.group_membership_verdict(reading)
        assert "carried NO membership control" in verdict
        assert "not a Leave button" in verdict

    def test_a_leave_control_is_still_reported_as_the_source_when_it_is_there(self):
        assert llv.group_membership_signal(["Leave"]) == "leave_control"
        assert llv.group_membership_signal(["Join"]) == "join_control"
        assert llv.group_membership_signal(["Requested"]) == "pending_control"
        assert llv.group_membership_signal([]) == "none"
        reading = {"directory": {"page_text": "Groups", "enumerated": [["1", "A"]],
                                 "anchors": [{"id": "1", "section": "Your groups"}]},
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member",
                                  "membership_signal": "leave_control", "header_controls": ["Leave"]}}
        assert "`leave_control`" in llv.group_membership_verdict(reading)

    def test_an_enumerated_id_under_a_recommendation_heading_is_drift(self):
        """The sync takes every `/groups/<id>` anchor as a join.

        The live directory renders 'Groups you might be interested in' on the same page, so an id
        from that section becomes a membership the user never had, and LEM comments in it.
        """
        directory = {"page_text": "Groups listing",
                     "enumerated": [["1", "Joined"], ["2", "Offered"]],
                     "anchors": [{"id": "1", "section": "Your groups"},
                                 {"id": "2", "section": "Groups you might be interested in"}]}
        findings = llv.group_enumeration_findings(directory)
        assert findings["enumerated_under_recommendation"] == ["2"]
        assert findings["sections_readable"] is True
        reading = {"directory": directory,
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert llv.group_membership_state(reading) == llv.STATE_DRIFT
        assert "never joined" in llv.group_membership_verdict(reading)

    def test_sections_no_anchor_could_be_attributed_to_are_reported_as_unanswered(self):
        """Live: all 40 anchors came back with `section: ''`.

        Silence there is not 'no recommendations', it is the discriminating reading having failed.
        It also has to grade `drift`: this surface is swept weekly and the sweep only looks at
        `drift`, so an unanswerable reading graded `ok` leaves the question open under a green report.
        """
        directory = {"page_text": "Groups listing", "enumerated": [["1", "A"], ["2", "B"]],
                     "anchors": [{"id": "1", "section": ""}, {"id": "2", "section": ""}]}
        findings = llv.group_enumeration_findings(directory)
        assert findings["sections_readable"] is False
        assert findings["enumerated_under_recommendation"] == []
        reading = {"directory": directory,
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert "UNANSWERED" in llv.group_membership_verdict(reading)
        assert llv.group_membership_state(reading) == llv.STATE_DRIFT

    def test_a_stale_set_differenced_against_a_blind_enumeration_is_unverifiable(self):
        """`stored_not_live` inherits the enumeration's blind spots.

        If the sync matched none of the page's anchors, every enabled group falls into the difference
        — which is the probe failing to see the directory, not six memberships ending.
        """
        reading = {"directory": {"page_text": "Groups", "enumerated": [],
                                 "anchors": [{"id": "1", "section": "Your groups"}]},
                   "stored_not_live": ["77427", "60878"],
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert llv.stale_set_grounded(reading) is False
        verdict = llv.group_membership_verdict(reading)
        assert "UNVERIFIABLE, not stale" in verdict
        assert "#1052 symptom" not in verdict

    def test_a_db_list_that_could_not_be_read_never_reads_as_nothing_stale(self):
        driver = _group_driver("Groups listing", [
            {"headings": ["Your groups"], "anchor_total": 1,
             "anchors": [{"id": "111", "text": "A", "section": "Your groups"}]},
            {"h1": "A", "levels": 2, "header_controls": ["Leave"], "all_controls": ["Leave"]},
        ])
        modules = _stubbed_group_modules_with_db(None, RuntimeError("no db"))
        with patch.dict(sys.modules, modules):
            reading = llv.probe_group_membership(driver, user_id=1,
                                                 enumerate_groups=lambda d: [("111", "A")],
                                                 sleep=lambda *_: None)
        assert reading["enabled_ids_readable"] is False
        assert reading["stored_not_live"] == []
        assert reading["stored_not_live_grounded"] is False
        assert "could not be read from the DB" in reading["verdict"]
        # The group half still has something to probe — the live directory named one.
        assert reading["target_source"] == "directory"
        assert "not the #1052 symptom" in reading["verdict"]

    def test_a_readable_db_list_grounds_the_stale_set(self):
        driver = _group_driver("Groups listing", [
            {"headings": ["Your groups"], "anchor_total": 1,
             "anchors": [{"id": "111", "text": "A", "section": "Your groups"}]},
            {"h1": "A", "levels": 2, "header_controls": ["Leave"], "all_controls": ["Leave"]},
        ])
        with patch.dict(sys.modules, _stubbed_group_modules_with_db(None, ["111", "222"])):
            reading = llv.probe_group_membership(driver, user_id=1,
                                                 enumerate_groups=lambda d: [("111", "A")],
                                                 sleep=lambda *_: None)
        assert reading["enabled_ids_readable"] is True
        assert reading["stored_not_live"] == ["222"]
        assert reading["stored_not_live_grounded"] is True
        assert "#1052 symptom" in reading["verdict"]

    def test_a_truncated_anchor_list_is_never_read_as_ids_the_sync_invented(self):
        """Live: 55 enumerated against an anchor list the probe itself capped at 40, which read as 15 invented ids.

        Truncation is the probe's own ceiling, not a finding.
        """
        directory = {"page_text": "Groups listing",
                     "enumerated": [["1", "A"], ["2", "B"], ["3", "C"]],
                     "anchors": [{"id": "1", "section": "Your groups"}], "anchor_total": 3}
        findings = llv.group_enumeration_findings(directory)
        assert findings["anchors_truncated"] is True
        assert findings["enumerated_not_anchored"] == []
        assert llv.group_membership_state({"directory": directory,
                                           "group_page": {"group_id": "1", "page_text": "x",
                                                          "membership": "member"}}) == llv.STATE_OK
        assert "from 3 anchor(s)" in llv.group_membership_verdict({"directory": directory})

    def test_an_id_on_no_anchor_of_a_complete_list_is_drift(self):
        directory = {"page_text": "Groups listing", "enumerated": [["1", "A"], ["9", "Ghost"]],
                     "anchors": [{"id": "1", "section": "Your groups"}], "anchor_total": 1}
        findings = llv.group_enumeration_findings(directory)
        assert findings["enumerated_not_anchored"] == ["9"]
        reading = {"directory": directory,
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert llv.group_membership_state(reading) == llv.STATE_DRIFT
        assert "on no anchor" in llv.group_membership_verdict(reading)

    def test_the_verdict_says_when_it_cut_the_evidence_short(self):
        """Live: 6 stale ids, 5 named, and the verdict read like the whole set."""
        reading = {"directory": {"page_text": "Groups", "enumerated": [["1", "A"]],
                                 "anchors": [{"id": "1", "section": "Your groups"}]},
                   "stored_not_live": ["1", "2", "3", "4", "5", "6"],
                   "group_page": {"group_id": "1", "page_text": "x", "membership": "member"}}
        assert "+1 more" in llv.group_membership_verdict(reading)

    def test_the_probe_reports_its_own_enumeration_findings_in_the_reading(self):
        driver = _group_driver("Groups listing Your groups", [
            {"headings": ["Your groups", "Groups you might be interested in"],
             "anchor_total": 2,
             "anchors": [{"id": "111", "text": "A", "section": "Your groups"},
                         {"id": "222", "text": "B",
                          "section": "Groups you might be interested in"}]},
            {"h1": "A", "levels": 2, "header_controls": [], "all_controls": ["Share"]},
        ])
        with patch.dict(sys.modules, _stubbed_group_modules(MagicMock())):
            reading = llv.probe_group_membership(
                driver, user_id=1, enabled_ids=["111"],
                enumerate_groups=lambda d: [("111", "A"), ("222", "B")], sleep=lambda *_: None)
        assert reading["directory"]["enumerated_under_recommendation"] == ["222"]
        assert reading["directory"]["anchor_total"] == 2
        assert reading["group_page"]["membership_signal"] == "share_box"
        assert reading["state"] == llv.STATE_DRIFT


def _composer_card(hits_on=("normalize-space",)):
    """A feed card whose locator-chain rungs hit only for the selectors named in `hits_on`.

    Defaults to the shape the #816 home-feed run found: the aria-label/data-testid rungs match
    nothing and the visible-TEXT XPath rungs carry the surface.
    """
    card = MagicMock()
    card.find_elements.side_effect = lambda by, selector: (
        [MagicMock()] if any(token in selector for token in hits_on) else [])
    return card


def _composer_feed_driver(text_nodes: int = 1, markers: int = 1, page_text: str = "A group feed"):
    """A driver whose feed renders `text_nodes` post-text nodes and `markers` `Hide post by` controls."""
    driver = MagicMock()
    body = MagicMock()
    body.text = page_text
    boxes = [MagicMock() for _ in range(text_nodes)]
    marker_els = [MagicMock() for _ in range(markers)]

    def find_elements(by, selector):
        if selector in ("main", "body"):
            return [body]
        if "expandable-text-box" in selector:
            return boxes
        if "Hide post by" in selector:
            return marker_els
        return []

    driver.find_elements.side_effect = find_elements
    driver.current_url = "https://www.linkedin.com/groups/42/"
    return driver


def _patch_composer_chain(monkeypatch, *, card=None, composer=None, in_card=(), scope=None,
                          clicks=None):
    """Point the shipped chain at fakes: `_card_for_textbox`, the action locators, the resolver.

    `in_card` is what `_visible_composers(card)` returns, so a composer NOT in it is one the widened
    `_single_post_scope` found — the #916 question the probe exists to answer.
    """
    card = card if card is not None else _composer_card()
    # Each name is patched where the probe IMPORTS it from (#1154): the composer walk moved to
    # `app.engagement.feed`, `_card_for_textbox` lives in `linkedin.cards` and `_visible_composers`
    # in `linkedin.composer`. Patching the old spelling would bind a name nothing reads.
    monkeypatch.setattr("cqc_lem.utilities.linkedin.cards._card_for_textbox", lambda d, b: card)
    monkeypatch.setattr("cqc_lem.app.engagement.feed._post_composer_for_card",
                        lambda d, c, user_id=None: composer)
    monkeypatch.setattr("cqc_lem.app.engagement.feed._single_post_scope",
                        lambda d, c: scope if scope is not None else c)
    monkeypatch.setattr("cqc_lem.utilities.linkedin.composer._visible_composers",
                        lambda root: [(box, {"y": 0}) for box in
                                      (in_card if root is card else [])])
    monkeypatch.setattr("cqc_lem.app.engagement.feed._is_post_comment_box",
                        lambda box: True)
    monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first",
                        lambda *a, **k: MagicMock())

    def click_first(driver, wait, locators, label, **kwargs):
        if clicks is not None:
            clicks.append(label)
        return MagicMock()

    monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first", click_first)
    monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: ["Comment", "Like"])
    return card


@pytest.mark.unit
class TestGroupFeedComposerProbe:
    """#928: #916 widened the composer lookup FOR the group feed and made every outcome silent.

    The lane posting nothing and the lane working look identical from the desk either way.
    """

    def test_locator_hits_are_summed_across_cards_in_chain_order(self):
        merged = llv.merge_locator_hits([{"a": 0, "b": 1}, {"a": 0, "b": 2}])
        assert merged == {"a": 0, "b": 3}
        assert list(merged) == ["a", "b"]

    def test_a_rung_that_could_not_be_read_says_so_instead_of_reading_as_zero(self):
        """A clean zero is evidence to re-ground from; an unreadable rung is not.

        The two must never print the same.
        """
        merged = llv.merge_locator_hits([{"a": 1}, {"a": "<StaleElementReferenceException>"}])
        assert merged == {"a (unreadable on 1 card(s))": 1}

    def test_a_composer_on_any_card_is_ok(self):
        feed = {"feed": "group", "page_text": "posts", "cards_found": 2, "composers_resolved": 1,
                "composers_from_widening": 1}
        assert llv.feed_composer_state(feed) == llv.STATE_OK
        assert "#916 widening is what carries this surface" in llv.feed_composer_verdict(feed)

    def test_a_composer_that_came_from_inside_the_card_says_so(self):
        feed = {"feed": "home", "page_text": "posts", "cards_found": 2, "composers_resolved": 2,
                "composers_from_widening": 0}
        assert "from inside the card itself" in llv.feed_composer_verdict(feed)

    def test_posts_on_the_page_the_card_walk_reached_none_of_is_drift(self):
        feed = {"feed": "group", "page_text": "posts", "post_text_nodes": 6, "page_post_markers": 6,
                "cards_found": 0}
        assert llv.feed_composer_state(feed) == llv.STATE_DRIFT
        assert "reached NONE of them" in llv.feed_composer_verdict(feed)

    def test_a_feed_with_no_posts_on_it_grounds_nothing(self):
        """A quiet group is not a defect.

        Filing it weekly would bury the run where the composer really is gone.
        """
        feed = {"feed": "group", "page_text": "No new posts", "post_text_nodes": 0,
                "page_post_markers": 0, "cards_found": 0}
        assert llv.feed_composer_state(feed) == llv.STATE_UNKNOWN
        assert "no posts at all" in llv.feed_composer_verdict(feed)

    def test_a_feed_that_never_rendered_grounds_nothing(self):
        assert llv.feed_composer_state({"feed": "group", "page_text": ""}) == llv.STATE_UNKNOWN
        assert llv.feed_composer_state({"feed": "group", "error": "TimeoutException: x"}) \
            == llv.STATE_UNKNOWN
        assert "could not be read" in llv.feed_composer_verdict(
            {"feed": "group", "error": "TimeoutException: x"})

    def test_the_three_zero_shapes_are_graded_apart(self):
        """They need opposite fixes and a bare `composers_resolved: 0` reads the same for all three."""
        no_action = {"feed": "group", "page_text": "posts", "cards_found": 3,
                     "composers_resolved": 0, "cards": [{"comment_action": None}]}
        assert llv.feed_composer_state(no_action) == llv.STATE_DRIFT
        assert "_COMMENT_ACTION_LOCATORS" in llv.feed_composer_verdict(no_action)

        unclaimed = {"feed": "group", "page_text": "posts", "cards_found": 3,
                     "composers_resolved": 0, "page_textboxes_after_click": 2,
                     "cards": [{"comment_action": {"text": "Comment"}}]}
        assert "claimed none of them for their card" in llv.feed_composer_verdict(unclaimed)

        none_mounted = {"feed": "group", "page_text": "posts", "cards_found": 3,
                        "composers_resolved": 0, "page_textboxes_after_click": 0,
                        "cards": [{"comment_action": {"text": "Comment"}}]}
        verdict = llv.feed_composer_verdict(none_mounted)
        assert "NO composer mounted anywhere" in verdict and "#1084" in verdict

    def test_the_home_control_is_what_makes_a_group_finding_about_groups(self):
        ok = {"feed": "home", "page_text": "posts", "cards_found": 2, "composers_resolved": 2}
        drift = {"feed": "group", "page_text": "posts", "cards_found": 2, "composers_resolved": 0,
                 "page_textboxes_after_click": 1, "cards": [{"comment_action": {"text": "C"}}]}
        reading = {"group_id": "42", "group": drift, "home": ok}
        assert llv.group_feed_composer_state(reading) == llv.STATE_DRIFT
        assert "group surface is what differs" in llv.group_feed_composer_verdict(reading)

    def test_a_control_that_failed_the_same_way_is_not_a_group_finding(self):
        drift = {"feed": "x", "page_text": "posts", "cards_found": 2, "composers_resolved": 0,
                 "page_textboxes_after_click": 1, "cards": [{"comment_action": {"text": "C"}}]}
        reading = {"group_id": "42", "group": dict(drift, feed="group"),
                   "home": dict(drift, feed="home")}
        assert "nothing to do with groups" in llv.group_feed_composer_verdict(reading)

    def test_no_group_to_probe_grades_unknown_and_opens_no_feed(self):
        driver = MagicMock()
        reading = llv.probe_group_feed_composer(driver, user_id=1, enabled_ids=[],
                                                sleep=lambda *_: None)
        assert reading["target_source"] == "none"
        assert reading["state"] == llv.STATE_UNKNOWN
        assert "no group to probe" in reading["verdict"]
        driver.get.assert_not_called()

    def test_an_unreadable_enabled_list_is_not_an_empty_one(self):
        db = types.ModuleType("cqc_lem.utilities.db")

        def get_enabled_group_ids(user_id):
            raise RuntimeError("no db")

        db.get_enabled_group_ids = get_enabled_group_ids
        with patch.dict(sys.modules, {"cqc_lem.utilities.db": db}):
            reading = llv.probe_group_feed_composer(MagicMock(), user_id=1, sleep=lambda *_: None)
        assert reading["enabled_ids_readable"] is False
        assert "RuntimeError" in reading["enabled_ids_error"]
        assert reading["state"] == llv.STATE_UNKNOWN

    def test_a_box_only_the_widened_scope_holds_is_reported_as_the_widenings_work(self, monkeypatch):
        """The #916 answer: the card carries no textbox, `_single_post_scope` is what found one."""
        driver = _composer_feed_driver()
        composer = MagicMock()
        clicks: list = []
        _patch_composer_chain(monkeypatch, composer=composer, in_card=(), clicks=clicks)

        reading = llv._walk_feed_composers(driver, "group", "https://www.linkedin.com/groups/42/",
                                           max_cards=1, sleep=lambda *_: None)

        assert reading["cards_found"] == 1
        assert reading["cards"][0]["composer_source"] == "widened_scope"
        assert reading["composers_from_widening"] == 1
        assert reading["state"] == llv.STATE_OK
        # Read-only: the ONE click is the card's own Comment button, and nothing is ever typed.
        assert clicks == ["Open comment composer"]
        composer.send_keys.assert_not_called()

    def test_a_composer_the_escape_closes_still_counts_as_having_mounted(self, monkeypatch):
        """The page-wide count is taken while the box is OPEN, never after the walk Escaped it.

        Read afterwards, a working Escape zeroes it and the two zero-shapes that need OPPOSITE
        fixes collapse: "a composer mounted that the card-scoped resolver claimed for no card"
        (re-ground the resolver) reads as "nothing mounts here at all" (#1084 — stop the lane).
        """
        driver = _composer_feed_driver()
        card = _patch_composer_chain(monkeypatch, composer=None)
        mounted = [MagicMock()]
        monkeypatch.setattr("cqc_lem.utilities.linkedin.composer._visible_composers",
                            lambda root: [] if root is card else
                            [(box, {"y": 0}) for box in mounted])

        class _EscapeClosesTheComposer:
            def __init__(self, _driver):
                pass

            def send_keys(self, *_):
                return self

            def perform(self):
                mounted.clear()

        monkeypatch.setattr("selenium.webdriver.ActionChains", _EscapeClosesTheComposer)

        reading = llv._walk_feed_composers(driver, "group", "https://www.linkedin.com/groups/42/",
                                           max_cards=1, sleep=lambda *_: None)

        assert reading["composers_resolved"] == 0
        assert reading["page_textboxes_after_click"] == 1
        assert reading["state"] == llv.STATE_DRIFT
        assert "claimed none of them for their card" in reading["verdict"]
        assert "#1084" not in reading["verdict"]

    def test_a_box_inside_the_card_is_not_credited_to_the_widening(self, monkeypatch):
        driver = _composer_feed_driver()
        composer = MagicMock()
        _patch_composer_chain(monkeypatch, composer=composer, in_card=(composer,))

        reading = llv._walk_feed_composers(driver, "home", llv.FEED_URL, max_cards=1,
                                           sleep=lambda *_: None)

        assert reading["cards"][0]["composer_source"] == "in_card"
        assert reading["composers_from_widening"] == 0

    def test_per_locator_hit_counts_ride_along_so_a_zero_is_re_groundable(self, monkeypatch):
        driver = _composer_feed_driver()
        _patch_composer_chain(monkeypatch, composer=MagicMock())

        reading = llv._walk_feed_composers(driver, "group", "https://www.linkedin.com/groups/42/",
                                           max_cards=1, sleep=lambda *_: None)

        hits = reading["locator_hits"]
        assert hits, "the chain's own hit counts are the reading a re-grounding pass is written from"
        assert any(count for count in hits.values()), "the TEXT rungs should have matched"
        assert any(count == 0 for count in hits.values()), "the aria-label rungs should not have"

    def test_a_card_the_page_renders_that_the_walk_misses_is_drift(self, monkeypatch):
        driver = _composer_feed_driver(text_nodes=4, markers=4)
        _patch_composer_chain(monkeypatch)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.cards._card_for_textbox", lambda d, b: None)

        reading = llv._walk_feed_composers(driver, "group", "https://www.linkedin.com/groups/42/",
                                           sleep=lambda *_: None)

        assert reading["cards_found"] == 0 and reading["post_text_nodes"] == 4
        assert reading["page_post_markers"] == 4
        assert reading["state"] == llv.STATE_DRIFT

    def test_the_probe_walks_the_group_then_the_home_feed_as_a_control(self, monkeypatch):
        driver = _composer_feed_driver()
        _patch_composer_chain(monkeypatch, composer=MagicMock())

        reading = llv.probe_group_feed_composer(driver, user_id=1, enabled_ids=["42", "43"],
                                                max_cards=1, sleep=lambda *_: None)

        assert reading["group_id"] == "42" and reading["target_source"] == "db_enabled"
        assert [c.args[0] for c in driver.get.call_args_list] == [
            "https://www.linkedin.com/groups/42/", llv.FEED_URL]
        assert reading["group"]["feed"] == "group" and reading["home"]["feed"] == "home"
        assert reading["state"] == llv.STATE_OK

    def test_group_feed_composer_alone_is_enough_to_probe(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        clear_the_breaker(monkeypatch)
        monkeypatch.setattr(llv, "probe_group_feed_composer",
                            lambda d, uid, group_id=None, max_cards=3: {"verdict": "ok"})
        assert llv.main(["--group-feed-composer"]) == 0
        assert llv.main(["--group-feed-composer", "42", "--group-feed-cards", "5"]) == 0


@pytest.mark.unit
class TestCompanyInviteProbe:
    def test_credit_copy_the_parser_cannot_read_is_drift(self):
        reading = {"page_text": "50 credits available", "credits_text_on_page": True,
                   "total_credits": 0}
        assert llv.company_invite_state(reading) == llv.STATE_DRIFT
        assert "parsed nothing" in llv.company_invite_verdict(reading)

    def test_credits_with_no_invitee_rows_is_drift(self):
        reading = {"page_text": "x credits available", "credits_text_on_page": True,
                   "total_credits": 100, "credits_remaining": 50, "invitee_rows": 0}
        assert llv.company_invite_state(reading) == llv.STATE_DRIFT

    def test_a_page_with_no_invite_panel_grounds_nothing(self):
        reading = {"page_text": "Company home", "credits_text_on_page": False}
        assert llv.company_invite_state(reading) == llv.STATE_UNKNOWN

    def test_a_healthy_panel_is_ok(self):
        reading = {"page_text": "x credits available", "credits_text_on_page": True,
                   "total_credits": 100, "credits_remaining": 50, "invitee_rows": 20,
                   "checkboxes": 20}
        assert llv.company_invite_state(reading) == llv.STATE_OK


@pytest.mark.unit
class TestSweepSessionGate:
    """A signed-out session renders prose and controls at EVERY surface, so a per-probe grade reads
    `drift` on nearly all of them at once — a whole Monday of `priority:high` issues about one
    expired cookie. The sweep grades that `unknown`, which files nothing.
    """

    def _driver(self, url: str = "https://www.linkedin.com/feed/", text: str = "Start a post"):
        driver = MagicMock()
        driver.current_url = url
        element = MagicMock()
        element.text = text
        driver.find_elements.return_value = [element]
        return driver

    def test_an_auth_wall_url_is_signed_out(self):
        driver = self._driver(url="https://www.linkedin.com/authwall?trk=x")
        assert llv.sweep_session_state(driver, sleep=lambda *_: None) == "signed_out"

    def test_linkedins_own_guest_copy_is_signed_out(self):
        driver = self._driver(text="Join LinkedIn to see more")
        assert llv.sweep_session_state(driver, sleep=lambda *_: None) == "signed_out"

    def test_an_unreadable_page_fails_open_rather_than_standing_the_sweep_down(self):
        """`unknown` is what an unreadable page is for; refusing to sweep on one would be the same
        silence this issue exists to end, one level up.
        """
        driver = MagicMock()
        driver.get.side_effect = RuntimeError("session gone")
        assert llv.sweep_session_state(driver, sleep=lambda *_: None) == "signed_in"

    def test_a_signed_out_sweep_runs_no_probe_and_grades_everything_unknown(self):
        ran = []
        runners = {"feed_sort": lambda: ran.append("feed_sort") or {"state": llv.STATE_DRIFT},
                   "catchup_cards": lambda: ran.append("catchup") or {"state": llv.STATE_DRIFT}}
        report = llv.run_sweep(MagicMock(), 1, runners=runners, keys=list(runners),
                               session_state="signed_out")
        assert ran == []
        assert report["session"] == "signed_out"
        assert report["summary"]["drift"] == []
        assert report["summary"]["unknown"] == ["catchup_cards", "feed_sort"]

    def test_a_signed_in_sweep_still_grades_normally(self):
        report = llv.run_sweep(MagicMock(), 1, runners={"feed_sort": lambda: {"state": "drift"}},
                               keys=["feed_sort"], session_state="signed_in")
        assert report["summary"]["drift"] == ["feed_sort"]

    def test_the_matrix_carries_each_flags_argument_into_the_sweep(self):
        """The filer builds its reproduce command from this — a `--profile-scrape` with nothing
        after it is an argparse error, not a repro.
        """
        report = llv.run_sweep(MagicMock(), 1,
                               runners={"profile_scrape": lambda: {"state": "drift"},
                                        "catchup_cards": lambda: {"state": "ok"}},
                               keys=["profile_scrape", "catchup_cards"], session_state="signed_in")
        assert report["surfaces"]["profile_scrape"]["arg"] == "<profile-url>"
        assert report["surfaces"]["catchup_cards"]["arg"] == ""


@pytest.mark.unit
class TestReportFences:
    """The probe shares stdout with `cqc_lem.utilities.logger` (`get_current_profile` alone logs
    before the first probe), and the weekly cron pipes that stdout into `json.loads`.
    """

    def test_the_fences_are_distinct_and_non_empty(self):
        assert llv.REPORT_JSON_BEGIN and llv.REPORT_JSON_END
        assert llv.REPORT_JSON_BEGIN != llv.REPORT_JSON_END


@pytest.mark.unit
@pytest.mark.unit
class TestCommentOutcomeProbe:
    """#818: the comment-outcome reader needs to expose the actual DOM evidence when a rendered
    thread yields no readable sort control, so the next locator iteration is written against live
    selectors rather than guesses.
    """

    def _item(self, text, author):
        tb = MagicMock(); tb.text = text
        return (tb, MagicMock(), author)

    def test_probe_returns_candidates_when_thread_rendered_but_sort_control_missing(self, monkeypatch):
        items = [self._item("Latency is the tell here", "https://www.linkedin.com/in/me/")]

        def _load_comment_thread(_d):
            pass

        monkeypatch.setattr("cqc_lem.app.engagement.posting._load_comment_thread", _load_comment_thread)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.composer._comment_items", lambda d: items)
        monkeypatch.setattr("cqc_lem.app.engagement.posting._comment_sort_label", lambda d, w: "")
        monkeypatch.setattr("cqc_lem.app.engagement.posting._find_our_comment", lambda it, s, t: it[0][1])
        monkeypatch.setattr("cqc_lem.app.engagement.posting._thread_replies", lambda d, o, it: [])
        monkeypatch.setattr("cqc_lem.app.engagement.posting._comment_like_count", lambda d, c: 0)
        monkeypatch.setattr("cqc_lem.app.engagement.posting._post_author_href", lambda d: "")
        monkeypatch.setattr("cqc_lem.app.engagement.posting._diagnose_sort_control_miss",
                            lambda d: [{"tag": "button", "text": "Most relevant"}])

        report = llv.probe_comment_outcome(MagicMock(), "https://post", "me", "x",
                                           sleep=lambda s: None)
        assert report["sort_control_found"] is False
        assert report["rendered_comments"] == 1
        assert report["sort_control_candidates"] == [{"tag": "button", "text": "Most relevant"}]
        assert report["state"] == llv.STATE_DRIFT

    def test_probe_omits_candidates_when_sort_control_resolves(self, monkeypatch):
        items = [self._item("Latency is the tell here", "https://www.linkedin.com/in/me/")]

        monkeypatch.setattr("cqc_lem.app.engagement.posting._load_comment_thread", lambda d: None)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.composer._comment_items", lambda d: items)
        monkeypatch.setattr("cqc_lem.app.engagement.posting._comment_sort_label",
                            lambda d, w: "most relevant")
        monkeypatch.setattr("cqc_lem.app.engagement.posting._find_our_comment", lambda it, s, t: it[0][1])
        monkeypatch.setattr("cqc_lem.app.engagement.posting._thread_replies", lambda d, o, it: [])
        monkeypatch.setattr("cqc_lem.app.engagement.posting._comment_like_count", lambda d, c: 0)
        monkeypatch.setattr("cqc_lem.app.engagement.posting._post_author_href", lambda d: "")
        monkeypatch.setattr("cqc_lem.app.engagement.posting._diagnose_sort_control_miss",
                            lambda d: [{"tag": "button"}])  # would not be called

        report = llv.probe_comment_outcome(MagicMock(), "https://post", "me", "x",
                                           sleep=lambda s: None)
        assert report["sort_control_found"] is True
        assert "sort_control_candidates" not in report
        assert report["state"] == llv.STATE_OK

    def test_probe_omits_candidates_when_thread_never_rendered(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.app.engagement.posting._load_comment_thread", lambda d: None)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.composer._comment_items", lambda d: [])
        monkeypatch.setattr("cqc_lem.app.engagement.posting._comment_sort_label", lambda d, w: "")
        monkeypatch.setattr("cqc_lem.app.engagement.posting._diagnose_sort_control_miss",
                            lambda d: [{"tag": "button"}])  # would not be called

        report = llv.probe_comment_outcome(MagicMock(), "https://post", "me", "x",
                                           sleep=lambda s: None)
        assert report["sort_control_found"] is False
        assert report["rendered_comments"] == 0
        assert "sort_control_candidates" not in report
        assert report["state"] == llv.STATE_UNKNOWN


class TestPermalinkCommentProbe:
    """#966: the permalink comment path failed SILENTLY, so the probe's job is to say plainly
    whether production would have a composer to type into.
    """

    _URL = "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000001/"
    _URN = "urn:li:activity:7000000000000000001"

    def test_verdict_names_a_resolved_composer(self):
        verdict = llv.permalink_comment_verdict(
            {"cards_found": 2, "permalink_urn": self._URN, "card_matched_urn": True,
             "comment_action": {"text": "Comment"}, "composer": {"role": "textbox"}})
        assert "composer resolved" in verdict

    def test_no_card_is_the_drift_verdict(self):
        assert "posts nothing" in llv.permalink_comment_verdict({"cards_found": 0})

    def test_a_composer_that_never_mounts_names_the_regression(self):
        verdict = llv.permalink_comment_verdict(
            {"cards_found": 1, "permalink_urn": self._URN, "card_matched_urn": True,
             "comment_action": {"text": "Comment"}, "composer": None})
        assert "#966" in verdict

    def test_a_top_card_from_another_post_is_called_out(self):
        verdict = llv.permalink_comment_verdict(
            {"cards_found": 3, "permalink_urn": self._URN, "card_matched_urn": False,
             "top_card_urn": "urn:li:activity:7000000000000000002"})
        assert "DIFFERENT post" in verdict

    def test_probe_reports_the_card_the_action_and_the_composer(self, monkeypatch):
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        driver.current_url = self._URL
        card, composer = MagicMock(), MagicMock()
        composer.get_attribute.return_value = "textbox"
        monkeypatch.setattr("cqc_lem.utilities.linkedin.cards._card_for_textbox", lambda d, b: card)
        monkeypatch.setattr("cqc_lem.utilities.linkedin.cards._feed_post_urn_from_card",
                            lambda c, driver=None: self._URN)
        monkeypatch.setattr("cqc_lem.app.engagement.feed._permalink_post_card",
                            lambda d, url, user_id=None: card)
        monkeypatch.setattr("cqc_lem.app.engagement.feed._post_composer_for_card",
                            lambda d, c, user_id=None: composer)
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.find_first", lambda *a, **k: MagicMock())
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.click_first", lambda *a, **k: MagicMock())
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: ["Comment"])

        reading = llv.probe_permalink_comment(driver, self._URL, sleep=lambda s: None)
        assert reading["card_resolved"] is True and reading["card_matched_urn"] is True
        assert reading["composer"] is not None
        assert "composer resolved" in reading["verdict"]

    def test_probe_says_so_when_no_card_carries_a_comment_action(self, monkeypatch):
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        monkeypatch.setattr("cqc_lem.utilities.linkedin.cards._card_for_textbox", lambda d, b: None)
        monkeypatch.setattr("cqc_lem.app.engagement.feed._permalink_post_card",
                            lambda d, url, user_id=None: None)
        monkeypatch.setattr(llv, "visible_button_labels", lambda d, **k: [])

        reading = llv.probe_permalink_comment(driver, self._URL, sleep=lambda s: None)
        assert reading["cards_found"] == 0 and reading["card_resolved"] is False
        assert reading["composer"] is None
        assert "posts nothing" in reading["verdict"]

    def test_permalink_comment_alone_is_enough_to_probe(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        clear_the_breaker(monkeypatch)
        monkeypatch.setattr(llv, "probe_permalink_comment",
                            lambda d, url: {"verdict": "composer resolved"})
        assert llv.main(["--permalink-comment", self._URL]) == 0


@pytest.mark.unit
class TestProfileExperiencesProbe:
    """#970 — the probe has to separate 'page never rendered' from 'grammar moved', because those
    two need opposite fixes and both look like an empty experience list from the desk.
    """

    def _page(self, *lines_per_entity):
        entities = ""
        for lines in lines_per_entity:
            inner = "".join(f'<div><span aria-hidden="true">{ln}</span>'
                            f'<span class="visually-hidden">{ln}</span></div>' for ln in lines)
            entities += f'<div data-view-name="profile-component-entity">{inner}</div>'
        driver = MagicMock()
        driver.page_source = f"<html><body><main>{entities}</main></body></html>"
        driver.current_url = "https://www.linkedin.com/in/someone/details/experience/"
        return driver

    def test_reads_the_rebuilt_parse_and_the_raw_lines_behind_it(self):
        driver = self._page(["Senior Engineer", "Acme Corp · Full-time",
                             "Jan 2020 - Present · 5 yrs 2 mos"])

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        assert reading["entity_count"] == 1
        assert reading["entities_with_dates"] == 1
        assert reading["entity_selector"] == "main div[data-view-name='profile-component-entity']"
        assert reading["experiences"][0]["company_name"] == "Acme Corp"
        assert reading["entities"][0]["lines"][0] == "Senior Engineer"
        assert reading["entities"][0]["parsed"] is True
        assert "1 experiences" in reading["verdict"]

    def test_reports_the_hit_count_of_every_ladder_rung(self):
        """The 2026-08-03 run needed a hand-written second pass to explain which rung won and why."""
        driver = self._page(["Senior Engineer", "Acme", "Jan 2020 - Present"])

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        counts = reading["selector_counts"]
        assert counts["main div[data-view-name='profile-component-entity']"] == 1
        assert counts["[data-view-name]"] == 1
        assert counts["div[role='listitem']"] == 0
        assert counts["main"] == 1

    def test_navigates_to_the_details_page_and_clicks_nothing(self):
        driver = self._page(["Senior Engineer", "Acme", "Jan 2020 - Present"])

        llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone/",
                                      sleep=lambda s: None)

        driver.get.assert_called_once_with(
            "https://www.linkedin.com/in/someone/details/experience/")
        driver.find_element.assert_not_called()

    def test_entity_sample_is_capped_but_the_counts_are_not(self):
        driver = self._page(*[["Engineer", "Acme", "Jan 2020 - Present"] for _ in range(5)])

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/x",
                                                max_entities=2, sleep=lambda s: None)

        assert len(reading["entities"]) == 2
        assert reading["entity_count"] == 5 and reading["entities_with_dates"] == 5

    def test_nothing_rendered_reads_as_page_not_loaded(self):
        assert "did not load" in llv.profile_experiences_verdict({"entity_count": 0})

    def test_nothing_matched_on_a_page_full_of_dates_names_the_ladder(self):
        verdict = llv.profile_experiences_verdict({"entity_count": 0, "page_dated_lines": 8})
        assert "every selector in the ladder is gone" in verdict

    def test_entities_without_dates_are_not_reported_as_selector_rot(self):
        verdict = llv.profile_experiences_verdict({"entity_count": 12, "entities_with_dates": 0})
        assert "none carrying a date range" in verdict
        assert "lists no experience" in verdict

    def test_an_undated_rung_beside_a_dated_page_names_the_wrong_rung(self):
        verdict = llv.profile_experiences_verdict({"entity_count": 3, "entities_with_dates": 0,
                                                   "page_dated_lines": 8})
        assert "the rung that won is not the one holding the experience" in verdict

    def test_dated_entities_that_parse_to_nothing_are_selector_rot(self):
        verdict = llv.profile_experiences_verdict({"entity_count": 4, "entities_with_dates": 4,
                                                   "experiences": []})
        assert "selector rot" in verdict

    def test_a_parsed_page_grades_ok(self):
        driver = self._page(["Senior Engineer", "Acme Corp", "Jan 2020 - Present · 5 yrs 2 mos"])

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        assert reading["state"] == llv.STATE_OK

    def test_dated_lines_the_parser_cannot_read_are_drift(self):
        """The 2026-08-03 failure: the page plainly rendered dated roles and the winning rung saw
        the footer. Graded off the PAGE's own dated lines, not off the ladder that missed them.
        """
        driver = MagicMock()
        driver.page_source = (
            "<html><body><main><div role='list'><div>Mar 2019 - Present · 7 yrs 6 mos</div>"
            "</div></main><div data-sdui-screen='x'><div role='listitem'>Questions?</div>"
            "</div></body></html>")
        driver.current_url = "https://www.linkedin.com/in/someone/details/experience/"

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        assert reading["page_dated_lines"] >= 1
        assert reading["experiences"] == []
        assert reading["state"] == llv.STATE_DRIFT

    def test_a_profile_with_no_dated_line_anywhere_grounds_nothing(self):
        driver = MagicMock()
        driver.page_source = "<html><body><main><p>Nothing here</p></main></body></html>"
        driver.current_url = "https://www.linkedin.com/in/someone/details/experience/"

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        assert reading["state"] == llv.STATE_UNKNOWN

    def test_no_target_navigates_nothing_and_grades_unknown(self):
        """The sweep resolves its own target; when it cannot, probing a relative URL would grade
        this surface off whatever page that lands on.
        """
        driver = MagicMock()

        reading = llv.probe_profile_experiences(driver, "", sleep=lambda s: None)

        driver.get.assert_not_called()
        assert reading["state"] == llv.STATE_UNKNOWN

    def test_company_attribution_is_counted_and_named(self):
        """#1096 shipped as a clean run: 8 entities parsed, 7 companyless, and the JSON was silent.

        The count and the titles behind it are what make the next drift diagnosable.
        """
        driver = MagicMock()
        driver.page_source = (
            "<html><body><main><ul>"
            "<li><div>Consultant</div><div>Jan 2015 - Dec 2016 · 2 yrs</div></li>"
            "<li><div>Advisor</div><div>Jan 2017 - Dec 2018 · 2 yrs</div></li>"
            "</ul></main></body></html>")
        driver.current_url = "https://www.linkedin.com/in/someone/details/experience/"

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        assert reading["experiences_without_company"] == 2
        assert reading["companyless_titles"] == ["Consultant", "Advisor"]
        assert "empty company_name" in reading["verdict"]

    def test_a_fully_attributed_page_says_so(self):
        driver = self._page(["Senior Engineer", "Acme Corp · Full-time",
                             "Jan 2020 - Present · 5 yrs 2 mos"])

        reading = llv.probe_profile_experiences(driver, "https://www.linkedin.com/in/someone",
                                                sleep=lambda s: None)

        assert reading["experiences_without_company"] == 0
        assert "all 1 carry a company_name" in reading["verdict"]

    def test_roles_parsed_with_no_company_at_all_grade_drift(self):
        """The roles are there and the grouping above them is not being read.

        That is drift, not a profile that lists no experience.
        """
        state = llv.profile_experiences_state(
            {"experiences": [{"company_name": "", "positions": [{"title": "Advisor"}]}],
             "page_dated_lines": 2})

        assert state == llv.STATE_DRIFT

    def test_one_unresolvable_company_beside_attributed_ones_is_not_drift(self):
        """A blank is the honest answer for an entry nothing groups; filing it weekly is noise."""
        state = llv.profile_experiences_state(
            {"experiences": [{"company_name": "Acme Corp"}, {"company_name": ""}],
             "page_dated_lines": 2})

        assert state == llv.STATE_OK

    def test_profile_experiences_alone_is_enough_to_probe(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        clear_the_breaker(monkeypatch)
        monkeypatch.setattr(llv, "probe_profile_experiences",
                            lambda d, url: {"verdict": "ok"})
        assert llv.main(["--profile-experiences", "https://www.linkedin.com/in/someone/"]) == 0


# ─────────────────────── read-only enforcement + the breaker gate (#1301) ───────────────────────
# These are the tests that let an autonomous agent run this probe at all. Before #1301 "read-only"
# was a property of the prose; here it is a property of the process.

@pytest.fixture(autouse=True)
def selenium_unpatched():
    """Put Selenium back exactly as it was after EVERY test in this module.

    The guard patches Selenium's own classes for the life of the PROCESS, which is right for a
    probe run and wrong for a test session: any test that walks through `main()` installs it as a
    side effect, and without this the next install would wrap the wrapper and double-count.
    Restoring here — rather than exposing an uninstall hook on the script — also leaves the probe
    with no way to turn its own guard off.
    """
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.remote.webdriver import WebDriver
    from selenium.webdriver.remote.webelement import WebElement

    targets = [(WebElement, "click"), (WebElement, "send_keys"), (WebElement, "submit"),
               (WebElement, "clear"), (WebDriver, "execute_script"), (ActionChains, "click"),
               (ActionChains, "send_keys"), (ActionChains, "send_keys_to_element")]
    originals = [(owner, name, getattr(owner, name)) for owner, name in targets]
    llv._GUARD_LEDGER.update(installed=False, refusals=[], unlabelled_clicks=0)
    yield
    for owner, name, original in originals:
        setattr(owner, name, original)
    llv._GUARD_LEDGER.update(installed=False, refusals=[], unlabelled_clicks=0)


@pytest.fixture
def selenium_restored(selenium_unpatched):
    """The guard installed for one test; `selenium_unpatched` takes it back off."""
    llv.install_read_only_guard()


def _element(aria: str = "", text: str = ""):
    """A real WebElement (so the patched methods are the ones under test) with a stubbed page."""
    from selenium.webdriver.remote.webelement import WebElement

    class _Stubbed(WebElement):
        def get_attribute(self, name):
            return aria if name == "aria-label" else None

        @property
        def text(self):
            return text

    parent = MagicMock()
    # Selenium's real send_keys asks the driver whether each key is a local file path to upload
    # (the detector answers with a path, or None); a bare MagicMock answers with a Mock and it
    # tries to zip it.
    parent.file_detector.is_local_file.return_value = None
    return _Stubbed(parent, "element-id")


def _remote_driver():
    from selenium.webdriver.remote.webdriver import WebDriver

    driver = WebDriver.__new__(WebDriver)
    driver.execute = MagicMock(return_value={"value": None})
    return driver


@pytest.mark.unit
class TestSubmitVocabulary:
    """The click half. It must catch the controls that commit and leave the ones that only OPEN
    something alone — a probe that cannot press 'Comment' cannot measure the comment composer.
    """

    @pytest.mark.parametrize("label", ["Post", "Send", "Send now", "Publish", "Submit",
                                       "Connect", "Invite Jane Doe to connect", "Withdraw",
                                       "Follow", "Unfollow", "Following", "Following Jane Doe",
                                       "Like", "Celebrate", "Repost", "Subscribe", "Join",
                                       "Accept", "Add a note", "Reply"])
    def test_a_commit_control_is_named(self, label):
        assert llv.submit_control_reason(label)

    @pytest.mark.parametrize("label", ["Start a post", "Create a post", "Comment",
                                       "Comment on Jane Doe's post", "Sort by: Most relevant",
                                       "Most recent", "Recent", "Load more comments",
                                       "Show all 24 comments", "Open reactions menu", ""])
    def test_an_opening_or_reading_control_is_not(self, label):
        # These are exactly the controls the probes DO click. Refusing them would make the guard
        # break the measurement it exists to protect.
        assert llv.submit_control_reason(label) is None

    def test_matching_ignores_case_and_whitespace(self):
        assert llv.submit_control_reason("  SEND \n now ")


@pytest.mark.unit
class TestTypedCharacters:
    def test_control_keys_are_not_typing(self):
        from selenium.webdriver.common.keys import Keys

        assert llv.typed_characters([Keys.ESCAPE, Keys.TAB, Keys.ARROW_DOWN]) == ""

    def test_printable_characters_are_typing(self):
        assert llv.typed_characters(["Great post!"]) == "Great post!"

    def test_text_hidden_among_control_keys_is_still_typing(self):
        from selenium.webdriver.common.keys import Keys

        assert llv.typed_characters([Keys.ESCAPE, ["ok"], Keys.ENTER]) == "ok"


@pytest.mark.unit
class TestReadOnlyGuard:
    def test_typing_is_refused_outright(self, selenium_restored):
        with pytest.raises(llv.ReadOnlyViolation):
            _element().send_keys("Great post, Jane!")

    def test_escape_still_closes_a_composer(self, selenium_restored):
        from selenium.webdriver.common.keys import Keys

        _element().send_keys(Keys.ESCAPE)  # no raise: this is how every probe cleans up

    def test_a_commit_control_cannot_be_clicked(self, selenium_restored):
        with pytest.raises(llv.ReadOnlyViolation):
            _element(aria="Post").click()

    def test_the_controls_the_probes_use_still_click(self, selenium_restored):
        _element(aria="Comment on Jane Doe's post").click()
        _element(text="Start a post").click()

    def test_the_element_text_is_read_too_not_just_aria_label(self, selenium_restored):
        # The live reaction toggle's text is literally "Like".
        with pytest.raises(llv.ReadOnlyViolation):
            _element(text="Like").click()

    def test_submit_and_clear_are_never_allowed(self, selenium_restored):
        with pytest.raises(llv.ReadOnlyViolation):
            _element(aria="Comment").submit()
        with pytest.raises(llv.ReadOnlyViolation):
            _element(aria="Comment").clear()

    def test_a_javascript_click_goes_through_the_same_gate(self, selenium_restored):
        # `arguments[0].click()` is how the feed-sort probe presses things, so it is also the
        # obvious way around a guard that only watched WebElement.click.
        with pytest.raises(llv.ReadOnlyViolation):
            _remote_driver().execute_script("arguments[0].click();", _element(aria="Send"))

    def test_a_javascript_click_on_a_reading_control_is_allowed(self, selenium_restored):
        _remote_driver().execute_script("arguments[0].click();",
                                        _element(aria="Sort by: Most relevant"))

    def test_an_interactive_script_naming_no_element_is_refused(self, selenium_restored):
        with pytest.raises(llv.ReadOnlyViolation):
            _remote_driver().execute_script("document.querySelector('.share-actions').click();")

    def test_reading_and_scrolling_scripts_are_untouched(self, selenium_restored):
        driver = _remote_driver()
        driver.execute_script("window.scrollBy(0, 900);")
        driver.execute_script("return document.querySelectorAll('li').length;")

    def test_action_chains_cannot_type_either(self, selenium_restored):
        from selenium.webdriver.common.action_chains import ActionChains

        chain = ActionChains.__new__(ActionChains)
        with pytest.raises(llv.ReadOnlyViolation):
            chain.send_keys("Great post!")
        with pytest.raises(llv.ReadOnlyViolation):
            chain.send_keys_to_element(_element(), "Great post!")

    def test_action_chains_cannot_press_a_commit_control(self, selenium_restored):
        from selenium.webdriver.common.action_chains import ActionChains

        chain = ActionChains.__new__(ActionChains)
        with pytest.raises(llv.ReadOnlyViolation):
            chain.click(_element(aria="Send"))

    def test_the_ledger_records_what_was_refused(self, selenium_restored):
        with pytest.raises(llv.ReadOnlyViolation):
            _element(aria="Follow").click()
        ledger = llv.guard_ledger()
        assert ledger["installed"] is True
        assert ledger["refusals"][0]["action"] == "click a control"
        assert "follow" in ledger["refusals"][0]["detail"]

    def test_an_unlabelled_click_is_allowed_but_counted(self, selenium_restored):
        # The one gap in the click half: a control with no readable label cannot be classified,
        # and refusing it would break probes on stale elements. It must not be invisible.
        _element().click()
        assert llv.guard_ledger()["unlabelled_clicks"] == 1

    def test_installing_twice_does_not_stack_wrappers(self, selenium_restored):
        llv.install_read_only_guard()
        with pytest.raises(llv.ReadOnlyViolation):
            _element(aria="Post").click()
        assert len(llv.guard_ledger()["refusals"]) == 1


@pytest.mark.unit
class TestBreakerReading:
    """The gate fails CLOSED, which is the opposite of the app's own breaker read: production must
    keep running when Redis is down, a probe must not run when it cannot prove it is safe to.
    """

    @staticmethod
    def _rate_limit(monkeypatch, client):
        from cqc_lem.utilities.linkedin import rate_limit

        monkeypatch.setattr(rate_limit, "shared_redis_client", lambda: client)
        return rate_limit

    def test_a_clear_breaker_reads_readable_and_closed(self, monkeypatch):
        self._rate_limit(monkeypatch, MagicMock(ttl=MagicMock(return_value=-2)))
        reading = llv.breaker_reading()
        assert reading["readable"] is True and reading["open"] is False
        assert llv.breaker_refusal(reading) is None

    def test_an_open_cooldown_is_a_wait_with_the_seconds_attached(self, monkeypatch):
        self._rate_limit(monkeypatch, MagicMock(ttl=MagicMock(return_value=1800)))
        refusal = llv.breaker_refusal(llv.breaker_reading())
        assert refusal["reason"] == "rate_limit_breaker_open"
        assert refusal["wait_seconds"] == 1800
        assert "do not escalate" in refusal["action"]

    def test_the_manual_automation_pause_blocks_it_too(self, monkeypatch):
        # The kill-switch is a harder gate than the breaker, and a probe is Selenium traffic like
        # any other — "automation is paused" has to include it.
        ttls = {"linkedin:429_cooldown": -2, "linkedin:automation_paused": 600}
        self._rate_limit(monkeypatch, MagicMock(ttl=MagicMock(side_effect=lambda k: ttls[k])))
        refusal = llv.breaker_refusal(llv.breaker_reading())
        assert refusal["wait_seconds"] == 600
        assert "manual automation pause" in refusal["detail"]

    def test_no_redis_refuses_rather_than_reading_as_clear(self, monkeypatch):
        self._rate_limit(monkeypatch, None)
        refusal = llv.breaker_refusal(llv.breaker_reading())
        assert refusal["reason"] == "rate_limit_breaker_unreadable"

    def test_a_failing_read_refuses(self, monkeypatch):
        self._rate_limit(monkeypatch, MagicMock(ttl=MagicMock(side_effect=Exception("boom"))))
        reading = llv.breaker_reading()
        assert reading["readable"] is False
        assert llv.breaker_refusal(reading)["reason"] == "rate_limit_breaker_unreadable"


@pytest.mark.unit
class TestMainRefusals:
    """What an agent actually keys on: a non-zero exit code and a machine-readable reason."""

    @staticmethod
    def _no_session(monkeypatch):
        opened = MagicMock(side_effect=AssertionError("the browser must never open on a refusal"))
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile", opened)
        return opened

    @staticmethod
    def _fenced(capsys) -> dict:
        out = capsys.readouterr().out
        return json.loads(out.split(llv.REPORT_JSON_BEGIN)[1].split(llv.REPORT_JSON_END)[0])

    def test_an_open_breaker_refuses_before_the_browser_opens(self, monkeypatch, capsys):
        self._no_session(monkeypatch)
        monkeypatch.setattr(llv, "breaker_reading",
                            lambda: {"readable": True, "open": True, "wait_seconds": 900,
                                     "reason": "the 429 circuit breaker is OPEN for another 900s"})
        assert llv.main(["--feed-sort"]) == llv.BREAKER_REFUSAL_EXIT_CODE
        report = self._fenced(capsys)
        assert report["refusal"]["reason"] == "rate_limit_breaker_open"
        assert report["refusal"]["wait_seconds"] == 900

    def test_an_unreadable_breaker_refuses(self, monkeypatch):
        self._no_session(monkeypatch)
        monkeypatch.setattr(llv, "breaker_reading",
                            lambda: {"readable": False, "open": False, "wait_seconds": 0,
                                     "reason": "Redis is unavailable"})
        assert llv.main(["--feed-sort"]) == llv.BREAKER_REFUSAL_EXIT_CODE

    def test_no_flag_can_bypass_an_open_breaker(self, monkeypatch, capsys):
        """There is no override, and that is the point.

        The probe drives the owner's real LinkedIn session on an account with a 429-lockout
        history. A hatch that only an instruction keeps agents away from is not a control, because
        headless lanes launch with `--dangerously-skip-permissions`.
        """
        monkeypatch.setattr(llv, "breaker_reading",
                            lambda: {"readable": True, "open": True, "wait_seconds": 900,
                                     "reason": "open"})
        with pytest.raises(SystemExit):          # argparse rejects it: the flag does not exist
            llv.main(["--feed-sort", "--ignore-breaker"])
        assert llv.main(["--feed-sort"]) == 75    # and the breaker still refuses, as a WAIT

    def test_the_guard_is_armed_after_login_and_before_the_first_probe(self, monkeypatch, capsys):
        """The one seam: signing in types the credentials and the emailed PIN.

        So the guard cannot already be on while the session opens — and it must be on for
        everything after, or "read-only" is back to being a promise.
        """
        seen = {}

        def _login(**kwargs):
            seen["armed_during_login"] = llv.guard_ledger()["installed"]
            return MagicMock(), MagicMock(), "a@b.c", MagicMock()

        def _probe(_driver):
            seen["armed_during_probe"] = llv.guard_ledger()["installed"]
            return {"verdict": "ok"}

        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile", _login)
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        monkeypatch.setattr(llv, "probe_feed_sort", _probe)
        clear_the_breaker(monkeypatch)
        assert llv.main(["--feed-sort"]) == 0
        assert seen == {"armed_during_login": False, "armed_during_probe": True}

    def test_a_clean_run_reports_the_guard_and_the_breaker(self, monkeypatch, capsys):
        monkeypatch.setattr("cqc_lem.utilities.linkedin.session.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        monkeypatch.setattr(llv, "probe_feed_sort", lambda d: {"verdict": "ok"})
        clear_the_breaker(monkeypatch)
        assert llv.main(["--feed-sort"]) == 0
        report = self._fenced(capsys)
        assert report["read_only_guard"]["installed"] is True
        assert report["breaker"]["readable"] is True


@pytest.mark.unit
class TestProbeSessionPin:
    """A probe that borrows a lane slot competes with the engagement it exists to measure."""

    def test_the_watchable_node_is_the_default(self):
        captured = {}

        def _profile(**kwargs):
            captured.update(kwargs)
            return MagicMock(), MagicMock(), "a@b.c", MagicMock()

        llv.open_probe_session(_profile, 1, require_debug_node=False)
        assert captured["debug"] is True
        assert "debug_required" not in captured

    def test_requiring_the_pin_passes_it_through(self):
        captured = {}

        def _profile(user_id=None, session_name="", debug=False, debug_required=False):
            captured.update(user_id=user_id, debug=debug, debug_required=debug_required)
            return MagicMock(), MagicMock(), "a@b.c", MagicMock()

        _d, _p, reading = llv.open_probe_session(_profile, 7, require_debug_node=True)
        assert captured == {"user_id": 7, "debug": True, "debug_required": True}
        assert reading["debug_node_pin"] == "required"

    def test_an_image_that_cannot_pin_refuses_rather_than_downgrading(self):
        # The probe is piped into whatever image is deployed, so "predates the pin" is an ordinary
        # state for a few hours after a merge — and a silent fallback there is exactly what this
        # issue removes.
        def _old_image(user_id=None, session_name="", debug=False):
            return MagicMock(), MagicMock(), "a@b.c", MagicMock()

        with pytest.raises(llv.ProbeRefused) as refused:
            llv.open_probe_session(_old_image, 1, require_debug_node=True)
        assert refused.value.refusal["reason"] == "debug_node_pin_unsupported"

    def test_a_busy_debug_node_becomes_a_wait_not_a_crash(self):
        class DebugNodeUnavailable(RuntimeError):
            pass

        def _profile(user_id=None, session_name="", debug=False, debug_required=False):
            raise DebugNodeUnavailable("debug node selenium-node-debug unavailable")

        with pytest.raises(llv.ProbeRefused) as refused:
            llv.open_probe_session(_profile, 1, require_debug_node=True)
        assert refused.value.refusal["reason"] == "debug_node_unavailable"

    def test_any_other_login_failure_still_surfaces(self):
        def _profile(**kwargs):
            raise RuntimeError("LinkedIn login failed")

        with pytest.raises(RuntimeError, match="login failed"):
            llv.open_probe_session(_profile, 1, require_debug_node=False)

    def test_main_turns_an_unavailable_node_into_the_wait_exit_code(self, monkeypatch):
        clear_the_breaker(monkeypatch)
        monkeypatch.setattr(llv, "open_probe_session",
                            MagicMock(side_effect=llv.ProbeRefused(
                                {"refused": True, "reason": "debug_node_unavailable",
                                 "detail": "busy", "action": "re-run later"})))
        assert llv.main(["--feed-sort", "--require-debug-node"]) == llv.BREAKER_REFUSAL_EXIT_CODE
