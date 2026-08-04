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
class TestRecommendationReadCopy:
    """#1007's read is NEW, so the image the probe is piped into does not have it — and grounding a
    rebuilt reader only after it merges is exactly how the ladder it replaces shipped dead. Same
    posture as the feed-sort chain: image first, carried copy otherwise, and the reading says which.
    A copy that drifts grounds a read nothing ships, which is worse than not probing at all."""

    def test_carried_read_is_identical_to_the_one_run_automation_uses(self):
        from cqc_lem.app import run_automation as ra

        assert llv.FALLBACK_RECOMMENDATION_ROWS_JS == ra._RECOMMENDATION_ROWS_JS
        assert llv.FALLBACK_RECOMMENDATION_RENDER_ATTEMPTS == ra._RECOMMENDATION_RENDER_ATTEMPTS

    def test_the_running_image_wins_when_it_has_the_read(self):
        from cqc_lem.app import run_automation as ra

        read, attempts, source = llv.recommendation_read()
        assert source == "image"
        assert read is ra._recommendation_reading
        assert attempts == ra._RECOMMENDATION_RENDER_ATTEMPTS

    def test_falls_back_to_the_carried_copy_on_an_image_that_predates_1007(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_read(name, *a, **k):
            if name == "cqc_lem.app.run_automation":
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
        monkeypatch.setattr("cqc_lem.app.run_automation.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        monkeypatch.setattr(llv, "probe_feed_sort", lambda d: {"verdict": "sort control OK"})
        assert llv.main(["--feed-sort"]) == 0


@pytest.mark.unit
class TestAppreciationSourcesProbe:
    """#968: the probe exists so the scrapers can be grounded BEFORE the flag is flipped, so its
    job is to distinguish 'nothing there' from 'there but unreadable'."""

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
        monkeypatch.setattr("cqc_lem.app.run_automation.get_current_profile",
                            lambda **k: (MagicMock(), MagicMock(), "a@b.c", MagicMock()))
        monkeypatch.setattr("cqc_lem.utilities.selenium_util.quit_gracefully", lambda d: None)
        monkeypatch.setattr(llv, "probe_appreciation_sources",
                            lambda d, u, p="": {"mentions": {"verdict": "no cards resolved"}})
        assert llv.main(["--appreciation-sources"]) == 0

    def test_zero_cards_on_a_dated_page_names_the_read_as_rotated(self):
        """#1007's whole finding: the recommendations page rendered dated cards and the locator
        ladder resolved none. That must not read the same as an account with no recommendations."""
        verdict = llv.appreciation_verdict({"cards": 0, "page_dated": True, "profile_anchors": 24})
        assert "silently dead" in verdict and "24 profile link(s)" in verdict

    def test_probe_reads_both_surfaces_and_never_claims_a_ledger_row(self, monkeypatch):
        """It grounds the PRODUCTION card reads + parsers (the scrapers themselves are gated
        off until this run happens), and it must stay read-only."""
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
             patch("cqc_lem.app.run_automation.getText", side_effect=lambda el: el.text), \
             patch("cqc_lem.app.run_automation._parse_recommendation_date", return_value=3.0):
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
        and NEITHER is inside the 30-day window — a fixed reader that still sends nothing today."""
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

    def test_an_image_that_predates_the_rebuild_still_grounds_both_surfaces(self, monkeypatch):
        """The pre-merge run the owner asked for: the deployed image has no `_recommendation_reading`
        to import, so a hard import would kill the whole probe — mentions half included — instead of
        grounding the branch's read against the live DOM."""
        import builtins
        from unittest.mock import patch

        real_import = builtins.__import__

        def _no_read(name, *a, **k):
            if name == "cqc_lem.app.run_automation" and "_recommendation_reading" in (a[2] or ()):
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
             patch("cqc_lem.app.run_automation._parse_recommendation_date", return_value=3.0):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        rec = report["recommendations_received"]
        assert rec["read_source"] == "script"
        assert (rec["cards"], rec["dated"]) == (1, 1)
        assert "predates #1007" in rec["verdict"]
        assert report["mentions"]["cards"] == 0

    def test_an_early_paint_is_re_read_before_it_counts_as_an_empty_section(self):
        """Production polls the render; the probe has to poll it too or it grounds a page that had
        not finished painting and calls the section empty."""
        from unittest.mock import patch

        rows = {"rows": [{"href": "https://www.linkedin.com/in/jane", "name": "Jane Doe",
                          "text": "July 24, 2026, Jane was my client"}],
                "anchors": 24, "page_dated": True}
        driver = _fake_driver(current_url="https://www.linkedin.com/in/me")
        driver.execute_script.side_effect = [{"rows": [], "anchors": 0, "page_dated": False}, rows]
        with patch("cqc_lem.utilities.selenium_util.find_all_first", return_value=[]), \
             patch("cqc_lem.utilities.db.has_appreciation_touch", return_value=False), \
             patch("cqc_lem.app.run_automation._parse_recommendation_date", return_value=3.0):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        assert report["recommendations_received"]["cards"] == 1

    def test_mention_row_reports_the_name_production_would_use(self, monkeypatch):
        """A textless actor link is what the live run actually hit — the probe has to apply the same
        sentence fallback, or a blank name here would read as a probe artifact."""
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
             patch("cqc_lem.app.run_automation.getText", side_effect=lambda el: el.text):
            report = llv.probe_appreciation_sources(driver, 1, "https://www.linkedin.com/in/me/",
                                                    sleep=lambda s: None)

        row = report["mentions"]["rows"][0]
        assert row["name"] == "Jane Doe"
        assert row["profile_url"] == "https://www.linkedin.com/in/jane-doe-42"


@pytest.mark.unit
class TestSentInvitesProbe:
    """#969: the probe that has to run GREEN before the withdrawal lane may be switched on.

    Two readings mean "production would withdraw nothing tonight" and must be named as such rather
    than read as a clean account: no rows at all, and rows whose sent dates do not parse."""

    def test_no_rows_on_a_rendered_page_names_selector_drift_as_a_possibility(self):
        verdict = llv.sent_invites_verdict(
            {"rows_seen": 0, "page_text": "Manage invitations Received Sent"})
        assert "no pending-invite rows resolved" in verdict
        assert "moved" in verdict

    def test_the_pages_own_empty_state_separates_a_clean_account_from_drift(self):
        """The first live run (PR #983) came back with zero rows and no way to tell which of the two
        it was looking at. The page says so itself when it renders empty — so read that, and still
        refuse to call the anchors grounded on a run that never saw a row."""
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
        probe exists to catch."""
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
        must not withdraw anybody."""
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
