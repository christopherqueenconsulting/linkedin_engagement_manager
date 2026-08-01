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


def _sort_control(text: str, aria: str = ""):
    control = MagicMock()
    control.text = text
    control.tag_name = "button"
    control.get_attribute.side_effect = lambda a: {"aria-label": aria}.get(a)
    return control


def _find_first_returning(results: list):
    """`find_first` answering each call of the probe's chain in order: trigger, Recent option,
    trigger again after the flip. Running out means the probe stopped early, which is the point of
    several of these cases."""
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
        report = llv.probe_composer(MagicMock(), sleep=lambda s: None)
        assert report == {"opened": False, "controls": [], "document_affordance": None}


@pytest.mark.unit
class TestMessageThreadProbe:
    """#731: the probe's job is to name the route that won, so the NEXT rotation is visible early."""

    def test_verdict_names_the_winning_route(self):
        assert llv.message_thread_verdict({"opened": True, "route": "anchor", "events": 12,
                                           "self_name": "Christopher Queen"}) == "opened via anchor"

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
    because `Selector miss: Feed sort control` alone cannot, and they need different fixes."""

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
        dropdown must still be open when `visible_controls` is read, or it captures nothing."""
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

    def test_a_control_label_naming_both_sorts_is_unreadable_not_recent(self):
        """Production reads a both-sorts label as unknown; a probe that called it 'recent' would
        report the control healthy on exactly the reading that leaves the run unsorted."""
        assert llv.control_sort_state(_sort_control("", "Sort by, currently Top, options Recent")) == ""
        assert llv.control_sort_state(_sort_control("Sort by: Recent")) == "recent"

    def test_menu_rows_are_captured_before_the_page_is_full_of_list_items(self):
        """One comma-joined selector returns DOCUMENT order, and a feed page's nav/rail/post <li>s
        come before an overlay dropdown — so the cap would be spent on furniture and the menu this
        probe exists to re-ground could be missing from its own capture."""
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
class TestFeedSortChainCopy:
    """The probe runs inside a Selenium worker whose `cqc_lem` is the DEPLOYED image, so a pre-merge
    grounding pass cannot import the chain it is grounding. The script therefore carries a copy —
    and a copy that drifts grounds a chain nothing ships, which is worse than not probing at all."""

    def test_carried_chain_is_identical_to_the_one_run_automation_uses(self):
        from cqc_lem.app import run_automation as ra

        assert llv.FALLBACK_SORT_LOCATORS == ra._FEED_SORT_LOCATORS
        assert llv.FALLBACK_RECENT_OPTION_LOCATORS == ra._FEED_RECENT_OPTION_LOCATORS

    def test_sort_states_match_the_ones_the_run_records(self):
        from cqc_lem.app import run_automation as ra

        assert (llv.SORT_RECENT, llv.SORT_TOP) == (ra.FEED_SORT_RECENT, ra.FEED_SORT_TOP)
        assert (llv.SORT_MISSING, llv.SORT_UNKNOWN) == (ra.FEED_SORT_MISSING, ra.FEED_SORT_UNKNOWN)

    def test_the_running_image_wins_when_it_has_a_chain(self):
        sort, option, source = llv.feed_sort_chains()
        assert source == "image"
        from cqc_lem.app import run_automation as ra
        assert sort == ra._FEED_SORT_LOCATORS and option == ra._FEED_RECENT_OPTION_LOCATORS

    def test_falls_back_to_the_carried_copy_on_an_image_that_predates_817(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_chain(name, *a, **k):
            if name == "cqc_lem.app.run_automation":
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
class TestMain:
    def test_requires_something_to_probe(self):
        with pytest.raises(SystemExit):
            llv.main([])

    def test_feed_sort_alone_is_enough_to_probe(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.app.run_automation.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        monkeypatch.setattr(llv, "probe_feed_sort", lambda d: {"verdict": "sort control OK"})
        assert llv.main(["--feed-sort"]) == 0
