"""Unit tests for comment outcome tracking — issue #628. Covers the sort-control reading, our-comment
matching, the reply/like/author-reply extraction with a mocked driver, the graceful skip paths, the
sweep orchestration, the weekly quality report + commenting hold, and the live-validation probe's
verdict. Selenium DOM targeting itself is grounded on a supervised live run (the #403/#404 pattern,
`scripts/linkedin_live_validation.py --comment-outcome-url`).
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from selenium.webdriver.common.by import By

pytestmark = pytest.mark.unit

# The comment-outcome sweep, the sort control and the outcome read moved to
# `app.engagement.posting` (#1154) — that is the module whose globals they read, so it is where
# they are patched. `automate_commenting` (the commenting hold gate) moved to `app.engagement.feed`.
POST = "cqc_lem.app.engagement.posting"
FEED = "cqc_lem.app.engagement.feed"
RS = "cqc_lem.app.run_scheduler"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{POST}.time.sleep", lambda *a, **k: None):
        yield


def _fn(name):
    import importlib
    return getattr(importlib.import_module(POST), name)


def _p(es, name, **kw):
    return es.enter_context(patch(f"{POST}.{name}", **kw))


def _pf(es, name, **kw):
    """`_p` for the feed module — the commenting hold gate lives in `automate_commenting`."""
    return es.enter_context(patch(f"{FEED}.{name}", **kw))


def _sort_chain() -> list:
    return list(_fn("_COMMENT_SORT_LOCATORS"))


class TestCommentTextMatches:
    def test_matches_a_truncated_render(self):
        f = _fn("_comment_text_matches")
        logged = ("This is exactly the kind of drift I have watched eat a quarter of a team's "
                  "throughput before anyone noticed.")
        rendered = "This is exactly the kind of drift I have watched eat a…more"
        assert f(rendered, logged) is True

    def test_matches_regardless_of_case_and_whitespace(self):
        assert _fn("_comment_text_matches")("  Nice   POINT about latency  ",
                                            "nice point about latency") is True

    def test_different_comments_do_not_match(self):
        assert _fn("_comment_text_matches")("Totally different opening line here",
                                            "Nothing like the other one at all") is False

    def test_empty_never_matches(self):
        f = _fn("_comment_text_matches")
        assert f("", "something") is False
        assert f("something", "") is False
        assert f(None, None) is False


class TestCommentSortLabel:
    def _btn(self, aria="", text=""):
        b = MagicMock()
        b.get_attribute.return_value = aria
        b.text = text
        return b

    def test_reads_most_relevant(self):
        with patch(f"{POST}.find_first", return_value=self._btn(text="Most relevant")):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == "most relevant"

    def test_reads_most_recent_from_aria_label(self):
        btn = self._btn(aria="Sort comments by, Most recent is currently selected")
        with patch(f"{POST}.find_first", return_value=btn):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == "most recent"

    def test_missing_control_is_empty_not_a_guess(self):
        with patch(f"{POST}.find_first", return_value=None):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == ""

    def test_unrecognized_label_is_empty(self):
        with patch(f"{POST}.find_first", return_value=self._btn(text="Sort by")):
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock()) == ""


class TestFindCommentSortControl:
    def _el(self, aria="", text=""):
        e = MagicMock()
        e.get_attribute.return_value = aria
        e.text = text
        return e

    def _driver(self, per_locator):
        """A driver whose find_elements answers each locator in chain order from `per_locator`."""
        driver = MagicMock()
        answers = list(per_locator) + [[]] * len(_sort_chain())
        driver.find_elements.side_effect = lambda *a, **k: answers.pop(0) if answers else []
        return driver

    def test_prefers_a_candidate_that_actually_names_a_sort(self):
        # An unrelated 'sort' button matched by an earlier locator must not short-circuit the chain:
        # find_first would hand it back and the reading would be unreadable forever, with no
        # 'Selector miss' warning to say so.
        wrong = self._el(aria="Sort your saved items")
        right = self._el(text="Most relevant")
        driver = self._driver([[wrong], [], [right]])
        assert _fn("_find_comment_sort_control")(driver, MagicMock()) is right

    def test_falls_back_to_the_first_match_when_none_name_a_sort(self):
        # Some renders label the control only inside its popup — the click path still needs it.
        first = self._el(aria="Sort")
        later = self._el(aria="Sort by")
        driver = self._driver([[first], [later]])
        assert _fn("_find_comment_sort_control")(driver, MagicMock()) is first

    def test_total_miss_defers_to_find_first_for_the_selector_miss_warning(self):
        driver = self._driver([])
        with patch(f"{POST}.find_first", return_value=None) as ff:
            assert _fn("_find_comment_sort_control")(driver, MagicMock()) is None
        assert ff.call_count == 1
        assert ff.call_args.kwargs["warn_on_miss"] is True

    def test_the_caller_can_stand_the_miss_warning_down(self):
        # A page that rendered no comment thread renders no sort control either — the miss is that
        # page, not selector rot (#1063).
        driver = self._driver([])
        with patch(f"{POST}.find_first", return_value=None) as ff:
            assert _fn("_find_comment_sort_control")(driver, MagicMock(),
                                                     warn_on_miss=False) is None
        assert ff.call_args.kwargs["warn_on_miss"] is False

    def test_the_label_reader_passes_the_cross_check_through(self):
        with patch(f"{POST}._find_comment_sort_control", return_value=None) as fc:
            assert _fn("_comment_sort_label")(MagicMock(), MagicMock(), warn_on_miss=False) == ""
        assert fc.call_args.kwargs["warn_on_miss"] is False

    def test_a_locator_that_raises_does_not_abort_the_chain(self):
        right = self._el(text="Most recent")
        driver = MagicMock()
        answers = [RuntimeError("stale"), [right]]

        def _find(*_a, **_k):
            nxt = answers.pop(0) if answers else []
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        driver.find_elements.side_effect = _find
        assert _fn("_find_comment_sort_control")(driver, MagicMock()) is right

    def test_scan_is_bounded_per_locator(self):
        # Reading a label is two Selenium round-trips; the broad tail of the chain can match many
        # nodes on a busy thread.
        noise = [self._el(aria="nope") for _ in range(50)]
        driver = self._driver([noise])
        _fn("_find_comment_sort_control")(driver, MagicMock())
        read = [e for e in noise if e.get_attribute.called]
        assert len(read) == _fn("_SORT_CANDIDATE_SCAN_CAP")


class TestDiagnoseSortControlMiss:
    def _driver(self, candidates):
        driver = MagicMock()
        driver.execute_script.return_value = candidates
        return driver

    def test_returns_descriptors_from_js(self):
        candidates = [{"tag": "button", "data_testid": "comment-sort-dropdown",
                       "aria_label": "Sort comments", "role": "button", "text": "Most relevant",
                       "has_popup": "true", "classes": "artdeco-dropdown"}]
        out = _fn("_diagnose_sort_control_miss")(self._driver(candidates))
        assert out == candidates

    def test_returns_empty_when_js_raises(self):
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError("stale")
        assert _fn("_diagnose_sort_control_miss")(driver) == []

    def test_returns_empty_when_js_returns_none(self):
        driver = MagicMock()
        driver.execute_script.return_value = None
        assert _fn("_diagnose_sort_control_miss")(driver) == []

    def test_filters_non_dict_entries(self):
        driver = MagicMock()
        driver.execute_script.return_value = [{"tag": "button"}, None, "not-a-dict"]
        assert _fn("_diagnose_sort_control_miss")(driver) == [{"tag": "button"}]

    def test_scan_falls_back_to_the_header_strip_when_no_label_names_a_sort(self):
        # The keyword pass cannot see a control whose label rotated away from every sort word — the
        # exact drift the capture exists to describe (#1117), so the JS carries a second pass.
        js = _fn("_SORT_CONTROL_DIAGNOSTIC_JS")
        assert "'keyword'" in js and "'header'" in js
        assert "compareDocumentPosition" in js

    def test_the_scan_root_is_the_main_column_not_the_comment_list(self):
        # The locator chain searches the whole document and the control renders ABOVE the list, so
        # a scan scoped INSIDE the list could never describe the element that went missing.
        js = _fn("_SORT_CONTROL_DIAGNOSTIC_JS")
        assert "const root=document.querySelector('main')||document.body;" in js

    def test_the_first_comment_anchor_is_the_live_grounded_one(self):
        # Comments are NOT <article> elements on SDUI (composer.py, validated #478) — an invented
        # anchor leaves `first` null on every real page and the header pass unanchored.
        js = _fn("_SORT_CONTROL_DIAGNOSTIC_JS")
        assert "[data-testid='expandable-text-box']" in js
        assert "comment-item" not in js and "article" not in js
        assert "'unanchored'" in js

    def test_both_passes_ignore_container_elements(self):
        # A container div inherits every descendant's text: matched on it, one 'topic' anywhere in
        # the thread fills the cap with ancestors, the header pass never runs, and other people's
        # comment text ships to analytics.
        js = _fn("_SORT_CONTROL_DIAGNOSTIC_JS")
        assert js.count("length>TEXT_MAX) continue;") == 2
        # The bound lives in `utilities/linkedin/sort_evidence` since #1270 — the ONE scan both the
        # comment sweep and the feed walk are built from. `posting` carries no alias for it: an
        # unused re-export is what CodeQL flagged, and reading it here from anywhere but its home
        # would let the two drift.
        from cqc_lem.utilities.linkedin.sort_evidence import SORT_CONTROL_OWN_TEXT_MAX

        assert f"TEXT_MAX={SORT_CONTROL_OWN_TEXT_MAX};" in js
        # 'desktop'/'topic' must not read as the 'top' sort keyword.
        assert "|\\btop\\b|" in js

    def test_comment_bodies_cannot_match_the_keyword_pass(self):
        # Verified against a fake DOM: with the thread's own prose eligible, two comments reading
        # "assorted sorting" filled the cap and the header pass — the only one that can see a
        # rotated label — never ran. Inside the list only a LABEL may match.
        js = _fn("_SORT_CONTROL_DIAGNOSTIC_JS")
        assert "el.closest(\"[data-testid*='commentList']\")" in js
        assert "KW.test(inList?label:label+' '+text.toLowerCase())" in js

    def test_the_cap_is_the_scan_cap_constant(self):
        js = _fn("_SORT_CONTROL_DIAGNOSTIC_JS")
        assert f"const CAP={_fn('_SORT_CANDIDATE_SCAN_CAP')};" in js
        assert "out.length>=8" not in js


class TestReportSortControlMiss:
    def _driver(self, candidates):
        driver = MagicMock()
        driver.execute_script.return_value = candidates
        return driver

    def test_evidence_is_emitted_as_an_event_not_only_a_log(self):
        # DEBUG never leaves the worker in prod (LOG_LEVEL=INFO, POSTHOG_LOG_LEVEL=WARNING), which
        # is why #1118's capture produced nothing to iterate from.
        cands = [{"tag": "button", "text": "Sort by", "reason": "header"}]
        with ExitStack() as es:
            track = _p(es, "track_selector_evidence")
            _p(es, "log_debug")
            out = _fn("_report_sort_control_miss")(self._driver(cands), 7, "https://post")
        assert out == cands
        assert track.call_args.args[0] == "comment_sort_control"
        assert track.call_args.args[1] == cands
        assert track.call_args.kwargs["post_url"] == "https://post"
        assert track.call_args.kwargs["user_id"] == 7

    def test_an_empty_scan_is_still_reported(self):
        # "The scan found nothing describable" is the reading that says the capture itself is blind.
        with ExitStack() as es:
            track = _p(es, "track_selector_evidence")
            _p(es, "log_debug")
            _fn("_report_sort_control_miss")(self._driver([]), 7, "https://post")
        assert track.call_args.args[1] == []

    def test_a_telemetry_failure_never_costs_the_outcome_read(self):
        with ExitStack() as es:
            _p(es, "track_selector_evidence", side_effect=RuntimeError("posthog down"))
            _p(es, "log_debug")
            assert _fn("_report_sort_control_miss")(self._driver([{"tag": "button"}]), 7, "u") == []

    # ── the level the miss picks (#1117) ────────────────────────────────────────────────────
    # Grounded on four `--comment-outcome-url` probe runs (2026-08-14) against posts that had warned
    # in production: threads of 1-2 comments, and the ONLY candidate above the list was the post's
    # own overflow menu (`Open control menu for post by <author>`, reason='header'). LinkedIn renders
    # no comment sort control on a short thread; the chain that "missed" it read 'most relevant' on
    # 21 of 22 checked readings over the same period.
    _LIVE_HEADER_CANDIDATE = {"tag": "button", "data_testid": "", "role": "",
                              "aria_label": "Open control menu for post by Davey Green",
                              "text": "", "has_popup": "", "reason": "header"}

    def test_an_absent_affordance_is_debug_not_a_defect(self):
        with ExitStack() as es:
            _p(es, "track_selector_evidence")
            debug, warn = _p(es, "log_debug"), _p(es, "log_warning")
            _fn("_report_sort_control_miss")(self._driver([self._LIVE_HEADER_CANDIDATE]), 7, "u")
        assert not warn.called
        assert debug.call_args.args[0] == "Selector miss: Comment sort control"

    def test_an_empty_scan_is_debug_too(self):
        # Nothing describable on the page is not evidence that a control was there.
        with ExitStack() as es:
            _p(es, "track_selector_evidence")
            debug, warn = _p(es, "log_debug"), _p(es, "log_warning")
            _fn("_report_sort_control_miss")(self._driver([]), 7, "u")
        assert not warn.called and debug.called

    def test_a_page_that_still_names_a_sort_is_drift_and_warns(self):
        # The keyword pass matched, so the affordance IS rendered and the chain cannot reach it.
        rotated = {"tag": "button", "text": "Top comments", "reason": "keyword"}
        with ExitStack() as es:
            _p(es, "track_selector_evidence")
            debug, warn = _p(es, "log_debug"), _p(es, "log_warning")
            _fn("_report_sort_control_miss")(self._driver([rotated]), 7, "u")
        assert not debug.called
        assert warn.call_args.args[0] == "Selector miss: Comment sort control"
        assert warn.call_args.kwargs["candidate_count"] == 1

    def test_the_message_is_the_one_find_first_would_have_emitted(self):
        # The escalation dedup key is (masked message + call site): a different spelling would split
        # this drift signal from its own history.
        assert _fn("_SORT_CONTROL_LABEL") == "Comment sort control"

    def test_candidates_ride_the_event_not_the_log_line(self):
        # An aria_label names whoever wrote the post, and a WARNING is forwarded to PostHog Logs.
        with ExitStack() as es:
            track = _p(es, "track_selector_evidence")
            _p(es, "log_debug"); warn = _p(es, "log_warning")
            cands = [{"tag": "button", "text": "Most relevant", "reason": "keyword"}]
            _fn("_report_sort_control_miss")(self._driver(cands), 7, "u")
        assert track.call_args.args[1] == cands
        assert "candidates" not in warn.call_args.kwargs

    def test_only_the_keyword_pass_counts_as_a_named_sort(self):
        named = _fn("_page_still_names_a_sort")
        assert named([{"reason": "keyword"}]) is True
        assert named([{"reason": "header"}, {"reason": "unanchored"}]) is False
        assert named([]) is False
        assert named(None) is False


class TestSwitchCommentSort:
    def test_true_only_when_the_control_confirms_the_new_sort(self):
        with patch(f"{POST}.find_first", side_effect=[MagicMock(), MagicMock()]), \
             patch(f"{POST}._comment_sort_label", return_value="most recent"):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is True

    def test_false_when_the_sort_did_not_actually_change(self):
        with patch(f"{POST}.find_first", side_effect=[MagicMock(), MagicMock()]), \
             patch(f"{POST}._comment_sort_label", return_value="most relevant"):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is False

    def test_false_when_no_control(self):
        with patch(f"{POST}.find_first", return_value=None):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is False

    def test_false_when_the_menu_option_is_missing(self):
        with patch(f"{POST}.find_first", side_effect=[MagicMock(), None]):
            assert _fn("_switch_comment_sort")(MagicMock(), MagicMock()) is False


def _item(text, author, name="cont"):
    tb = MagicMock(); tb.text = text
    return (tb, MagicMock(name=name), author)


class TestFindOurComment:
    def test_matches_our_comment_by_text(self):
        items = [_item("Someone else entirely", "https://www.linkedin.com/in/glenda/"),
                 _item("Latency is the tell here", "https://www.linkedin.com/in/me/")]
        assert _fn("_find_our_comment")(items, "me", "Latency is the tell here") is items[1][1]

    def test_falls_back_to_our_only_comment_when_text_drifted(self):
        items = [_item("@Glenda Smith latency is the tell", "https://www.linkedin.com/in/me/")]
        assert _fn("_find_our_comment")(items, "me", "Latency is the tell here") is items[0][1]

    def test_no_fallback_when_several_of_ours_are_present(self):
        items = [_item("first of ours", "https://www.linkedin.com/in/me/"),
                 _item("second of ours", "https://www.linkedin.com/in/me/")]
        assert _fn("_find_our_comment")(items, "me", "nothing alike over here") is None

    def test_none_when_we_authored_nothing(self):
        items = [_item("theirs", "https://www.linkedin.com/in/glenda/")]
        assert _fn("_find_our_comment")(items, "me", "ours") is None

    def test_none_without_a_slug(self):
        assert _fn("_find_our_comment")([_item("x", "y")], "", "x") is None

    def test_a_slug_we_are_merely_a_prefix_of_is_not_us(self):
        # '/in/chris' is a substring of '/in/chris-queen-9b1' — matching on containment would read
        # a stranger's comment as ours and record their thread as our outcome.
        items = [_item("their comment", "https://www.linkedin.com/in/chris-queen-9b1/")]
        assert _fn("_find_our_comment")(items, "chris", "their comment") is None

    def test_matching_is_case_insensitive_on_the_href(self):
        items = [_item("ours", "https://www.linkedin.com/in/Chris-Queen-9b1/")]
        assert _fn("_find_our_comment")(items, "chris-queen-9b1", "ours") is items[0][1]


class TestHrefIsProfile:
    def test_exact_slug_only(self):
        f = _fn("_href_is_profile")
        assert f("https://www.linkedin.com/in/chris-queen-9b1/", "chris-queen-9b1") is True
        assert f("https://www.linkedin.com/in/chris-queen-9b1/", "chris") is False
        assert f("https://www.linkedin.com/in/chris/", "chris-queen-9b1") is False

    def test_empty_inputs_never_match(self):
        f = _fn("_href_is_profile")
        assert f("", "chris") is False
        assert f("https://www.linkedin.com/in/chris/", "") is False
        assert f(None, None) is False


class TestSortOptionLocators:
    def test_compares_case_insensitively_against_the_lowercase_label(self):
        # LinkedIn renders 'Most recent'. A title-cased literal ('Most Recent') never matches in
        # XPath, which would leave the flip permanently failing and every demotion unseen.
        for _by, xpath in _fn("_sort_option_locators")("most recent"):
            assert "'most recent'" in xpath
            assert "Most Recent" not in xpath and "Most recent" not in xpath
            assert "translate(normalize-space()" in xpath

    def test_sort_control_locators_are_case_folded_too(self):
        # CSS selectors have no case folding to do; every XPath that names a sort label must
        # compare lowercase literals through translate(), or it silently never fires.
        for by, expr in _sort_chain():
            if by != By.XPATH:
                continue
            assert "Most relevant" not in expr and "Most recent" not in expr
            if "relevant" in expr or "recent" in expr:
                assert "translate(" in expr
                assert "'most relevant'" in expr and "'most recent'" in expr

    def test_sort_control_chain_includes_data_testid_fallback(self):
        chain = _sort_chain()
        exprs = [expr for (_by, expr) in chain]
        assert any("data-testid" in e for e in exprs)
        assert any("@role='button'" in e for e in exprs if "[" in e)
        assert len(chain) > 3  # original three plus the new fallbacks

    def test_testid_wildcard_cannot_claim_an_unrelated_sort_control(self):
        # A bare [data-testid*='sort'] sits FIRST in the chain, so any other 'sort' button on the
        # page would be handed back and read as unreadable forever — with no 'Selector miss'
        # warning, because find_first did find something.
        for by, expr in _sort_chain():
            if by == By.CSS_SELECTOR and "*='sort'" in expr:
                assert "comment" in expr

    def test_the_known_comment_testid_leads_the_wildcard(self):
        # Ordered most-specific first: the exact testid can only be the comment sort control, the
        # wildcard behind it merely probably is.
        exprs = [e for _by, e in _sort_chain()]
        assert exprs.index("[data-testid='comment-sort-dropdown']") < \
            min(i for i, e in enumerate(exprs) if "*='sort'" in e)

    def test_subtree_text_locators_cannot_match_a_wrapper(self):
        # normalize-space() is the WHOLE SUBTREE's text, so an unbounded contains() on a generic
        # element matches every ancestor up to <body>, and find_first returns the outermost match.
        # The sort would then be decided by any comment saying 'most recent', and a click on that
        # wrapper would never open the real control.
        for by, expr in _sort_chain():
            if by != By.XPATH or "normalize-space()," not in expr:
                continue
            if expr.startswith("//button["):
                continue  # a <button> cannot wrap the page
            assert "string-length(normalize-space()) <" in expr, expr
        divs = [e for by, e in _sort_chain() if by == By.XPATH and e.startswith("//div[")]
        assert divs and all("not(.//div)" in e for e in divs)


class TestCommentLikeCount:
    def test_parses_the_reactions_control(self):
        driver = MagicMock(); driver.execute_script.return_value = "1.2K"
        assert _fn("_comment_like_count")(driver, MagicMock()) == 1200

    def test_zero_when_unreadable(self):
        driver = MagicMock(); driver.execute_script.side_effect = RuntimeError("boom")
        assert _fn("_comment_like_count")(driver, MagicMock()) == 0


class TestThreadReplies:
    def test_only_nested_containers_count(self):
        ours = MagicMock(name="ours")
        nested = MagicMock(name="nested")
        outside = MagicMock(name="outside")
        items = [(MagicMock(), ours, "https://www.linkedin.com/in/me/"),
                 (MagicMock(), nested, "https://www.linkedin.com/in/glenda/"),
                 (MagicMock(), outside, "https://www.linkedin.com/in/other/")]
        driver = MagicMock()
        driver.execute_script.side_effect = lambda _js, a, b: b is nested
        out = _fn("_thread_replies")(driver, ours, items)
        assert [a for _c, a in out] == ["https://www.linkedin.com/in/glenda/"]


def _outcome_env(es, items, sort_label="most relevant", switched=False, items_after=None,
                 author_href="https://www.linkedin.com/in/authorperson/", like_count=0):
    driver = MagicMock()
    seq = [items] + ([items_after] if items_after is not None else [])
    _p(es, "_load_comment_thread")
    _p(es, "_comment_items", side_effect=seq if len(seq) > 1 else (lambda _d: items))
    _p(es, "_comment_sort_label", return_value=sort_label)
    _p(es, "_switch_comment_sort", return_value=switched)
    _p(es, "_post_author_href", return_value=author_href)
    _p(es, "_comment_like_count", return_value=like_count)
    return driver


class TestReadCommentOutcome:
    def test_visible_under_most_relevant_with_author_reply(self):
        ours = MagicMock(name="ours")
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, ours, "https://www.linkedin.com/in/me/"),
                 (MagicMock(), MagicMock(), "https://www.linkedin.com/in/authorperson/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items, like_count=4)
            _p(es, "_thread_replies",
               return_value=[(items[1][1], "https://www.linkedin.com/in/authorperson/")])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["status"] == "checked"
        assert out["visible_most_relevant"] is True
        assert out["author_replied"] is True
        assert out["reply_count"] == 1
        assert out["like_count"] == 4
        assert out["our_reply_sent"] is False

    def test_our_own_reply_is_not_counted_as_a_reply_to_us(self):
        ours = MagicMock(name="ours")
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, ours, "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items)
            _p(es, "_thread_replies", return_value=[(MagicMock(), "https://www.linkedin.com/in/me/")])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["reply_count"] == 0 and out["our_reply_sent"] is True
        assert out["author_replied"] is False

    def test_a_replier_whose_slug_starts_with_ours_is_still_a_reply(self):
        # '/in/me' is a prefix of '/in/me-too-9b1' and '/in/authorperson' of
        # '/in/authorperson-2': containment matching would swallow a real reply as our own and
        # credit a stranger as the post author replying.
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, MagicMock(name="ours"), "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items)
            _p(es, "_thread_replies",
               return_value=[(MagicMock(), "https://www.linkedin.com/in/me-too-9b1/"),
                             (MagicMock(), "https://www.linkedin.com/in/authorperson-2/")])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["reply_count"] == 2
        assert out["our_reply_sent"] is False
        assert out["author_replied"] is False

    def test_absent_from_relevant_but_present_in_recent_is_a_demotion(self):
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        after = [(our_tb, MagicMock(name="ours"), "https://www.linkedin.com/in/me/")]
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, switched=True, items_after=after)
            _p(es, "_thread_replies", return_value=[])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        assert out["visible_most_relevant"] is False and out["status"] == "checked"

    def test_missing_in_both_sorts_skips_with_a_reason(self):
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, switched=True, items_after=theirs)
            _p(es, "log_info")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert out["status"] == "skipped" and out["skip_reason"] == "comment-not-found"
        assert out["visible_most_relevant"] is None
        assert out["reply_count"] == 0 and out["like_count"] == 0

    def test_deleted_or_private_post_renders_no_comments(self):
        with ExitStack() as es:
            driver = _outcome_env(es, [], sort_label="", switched=False)
            _p(es, "log_info")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
            label = _fn("_comment_sort_label")
        assert out["status"] == "skipped" and out["skip_reason"] == "post-unavailable"
        assert out["visible_most_relevant"] is None
        # A gone post is not selector rot: the miss must not file a RecurringWarning defect (#1063).
        assert label.call_args.kwargs["warn_on_miss"] is False

    def test_the_label_read_never_warns_on_its_own(self):
        # #1117: the level is decided from the evidence scan, which has not run yet at this point.
        # A rendered thread used to warn here (#1063) — and LinkedIn renders no sort control on a
        # short thread, so a normal 1-comment revisit filed a defect for working behaviour.
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, sort_label="", switched=False)
            _p(es, "log_info")
            _p(es, "_report_sort_control_miss")
            _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
            label = _fn("_comment_sort_label")
        assert label.call_args.kwargs["warn_on_miss"] is False

    def test_rendered_thread_with_unreadable_sort_reports_candidates(self):
        # A rendered thread where the sort control exists but is not readable is #818's starvation
        # signal: capture candidate descriptors for the next iteration.
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, MagicMock(), "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items, sort_label="", switched=False)
            _p(es, "_thread_replies", return_value=[])
            report = _p(es, "_report_sort_control_miss")
            _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                          "Latency is the tell here")
        assert report.call_args.args[1:] == (1, "https://post")

    def test_evidence_is_captured_even_when_our_comment_was_not_found(self):
        # Whether OUR comment is on the page says nothing about whether the page rendered a sort
        # control, and the skip returns early — gating the capture on it threw away most of the
        # readings that had evidence (#1117).
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, sort_label="", switched=False)
            _p(es, "log_info")
            report = _p(es, "_report_sort_control_miss")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert out["skip_reason"] == "comment-not-found"
        assert report.called

    def test_a_post_that_rendered_nothing_captures_no_evidence(self):
        # No thread means no control was expected — the same reason that miss does not warn (#1063).
        with ExitStack() as es:
            driver = _outcome_env(es, [], sort_label="", switched=False)
            _p(es, "log_info")
            report = _p(es, "_report_sort_control_miss")
            _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert not report.called

    def test_a_readable_sort_captures_no_evidence(self):
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, MagicMock(), "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items)
            _p(es, "_thread_replies", return_value=[])
            report = _p(es, "_report_sort_control_miss")
            _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                          "Latency is the tell here")
        assert not report.called

    def test_unknown_sort_leaves_visibility_null_even_when_found(self):
        our_tb = MagicMock(); our_tb.text = "Latency is the tell here"
        items = [(our_tb, MagicMock(), "https://www.linkedin.com/in/me/")]
        with ExitStack() as es:
            driver = _outcome_env(es, items, sort_label="")
            _p(es, "_thread_replies", return_value=[])
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me",
                                              "Latency is the tell here")
        # We read the outcome, but we cannot claim anything about 'Most relevant' visibility.
        assert out["status"] == "checked" and out["visible_most_relevant"] is None

    def test_failed_sort_switch_never_reads_as_a_demotion(self):
        theirs = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/glenda/")]
        theirs[0][0].text = "someone else"
        with ExitStack() as es:
            driver = _outcome_env(es, theirs, switched=False)
            _p(es, "log_info")
            out = _fn("_read_comment_outcome")(driver, MagicMock(), 1, "https://post", "me", "ours")
        assert out["visible_most_relevant"] is None and out["status"] == "skipped"


class TestSweepOrchestration:
    def _driver_patches(self, es):
        profile = MagicMock(); profile.profile_url = "https://www.linkedin.com/in/me/"
        _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile))
        _p(es, "quit_gracefully")
        _p(es, "acquire_run_lock", return_value="tok")
        _p(es, "release_run_lock")
        return profile

    def test_no_targets_short_circuits_before_a_browser(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets", return_value=[])
            gcp = _p(es, "get_current_profile")
            assert "No comments due" in _run_comment_outcomes_sweep(1)
        assert not gcp.called

    def test_records_and_tracks_each_outcome(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        targets = [{"log_id": 11, "post_url": "feedurn://urn:li:activity:1", "message": "a"},
                   {"log_id": 12, "post_url": "feedurn://urn:li:activity:2", "message": "b"}]
        with ExitStack() as es:
            self._driver_patches(es)
            _p(es, "get_comment_outcome_targets", return_value=targets)
            _p(es, "_read_comment_outcome",
               side_effect=[{"status": "checked", "skip_reason": None, "author_replied": True,
                             "reply_count": 1, "like_count": 2, "visible_most_relevant": True,
                             "our_reply_sent": False},
                            {"status": "skipped", "skip_reason": "comment-not-found",
                             "author_replied": False, "reply_count": 0, "like_count": 0,
                             "visible_most_relevant": None, "our_reply_sent": False}])
            rec = _p(es, "record_comment_outcome", return_value=True)
            trk = _p(es, "track_comment_outcome")
            result = _run_comment_outcomes_sweep(1)
        assert "checked 1" in result and "skipped 1" in result
        assert rec.call_count == 2 and trk.call_count == 2
        assert rec.call_args_list[0].kwargs["visible_most_relevant"] is True
        assert rec.call_args_list[1].kwargs["skip_reason"] == "comment-not-found"

    def test_unnavigable_key_is_skipped_without_a_row(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        with ExitStack() as es:
            self._driver_patches(es)
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 9, "post_url": "feedpost://hash", "message": "a"}])
            read = _p(es, "_read_comment_outcome")
            rec = _p(es, "record_comment_outcome")
            _run_comment_outcomes_sweep(1)
        assert not read.called and not rec.called

    def test_one_failing_post_does_not_abort_the_sweep(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        targets = [{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"},
                   {"log_id": 2, "post_url": "feedurn://urn:li:activity:2", "message": "b"}]
        with ExitStack() as es:
            self._driver_patches(es)
            _p(es, "get_comment_outcome_targets", return_value=targets)
            _p(es, "log_warning")
            _p(es, "_read_comment_outcome",
               side_effect=[RuntimeError("stale"),
                            {"status": "checked", "skip_reason": None, "author_replied": False,
                             "reply_count": 0, "like_count": 0, "visible_most_relevant": True,
                             "our_reply_sent": False}])
            rec = _p(es, "record_comment_outcome", return_value=True)
            _p(es, "track_comment_outcome")
            result = _run_comment_outcomes_sweep(1)
        assert rec.call_count == 1 and "checked 1" in result

    def test_lock_contention_skips(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"}])
            _p(es, "acquire_run_lock", return_value=None)
            gcp = _p(es, "get_current_profile")
            assert "another outcome sweep" in _run_comment_outcomes_sweep(1)
        assert not gcp.called

    def test_rate_limited_session_skips_cleanly(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"}])
            _p(es, "acquire_run_lock", return_value="tok")
            rel = _p(es, "release_run_lock")
            _p(es, "log_warning")
            _p(es, "get_current_profile", side_effect=LinkedInRateLimited("429"))
            assert "rate limited" in _run_comment_outcomes_sweep(1)
        assert rel.called

    def test_missing_profile_slug_aborts_before_reading(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        profile = MagicMock(); profile.profile_url = "https://www.linkedin.com/"
        with ExitStack() as es:
            _p(es, "get_comment_outcome_targets",
               return_value=[{"log_id": 1, "post_url": "feedurn://urn:li:activity:1", "message": "a"}])
            _p(es, "acquire_run_lock", return_value="tok")
            _p(es, "release_run_lock")
            _p(es, "quit_gracefully")
            _p(es, "log_warning")
            _p(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", profile))
            read = _p(es, "_read_comment_outcome")
            assert "no profile slug" in _run_comment_outcomes_sweep(1)
        assert not read.called


class TestCommentingHoldGate:
    def test_held_user_never_opens_a_browser(self):
        from cqc_lem.app.engagement.feed import automate_commenting
        with ExitStack() as es:
            _pf(es, "is_commenting_held", return_value=True)
            _pf(es, "commenting_hold_reason", return_value="80% demoted")
            _pf(es, "log_warning")
            lock = _pf(es, "acquire_run_lock")
            gcp = _pf(es, "get_current_profile")
            result = automate_commenting.run(user_id=1)
        assert "held" in result and "80% demoted" in result
        assert not lock.called and not gcp.called

    def test_unheld_user_proceeds(self):
        from cqc_lem.app.engagement.feed import automate_commenting
        with ExitStack() as es:
            _pf(es, "is_commenting_held", return_value=False)
            _pf(es, "acquire_run_lock", return_value="tok")
            _pf(es, "release_run_lock")
            _pf(es, "get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock()))
            _pf(es, "navigate_to_feed")
            _pf(es, "comment_on_feed_inline", return_value=2)
            _pf(es, "quit_gracefully")
            result = automate_commenting.run(user_id=1)
        assert "Commented on 2 posts" in result


class TestWeeklyQualityReport:
    def _rows(self, n, visible):
        return [{"status": "checked", "author_replied": 0, "reply_count": 0, "like_count": 0,
                 "visible_most_relevant": visible, "our_reply_sent": 0} for _ in range(n)]

    def _run(self, es, rows, users=(1,)):
        from cqc_lem.app.run_scheduler import auto_weekly_comment_quality
        es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=list(users)))
        es.enter_context(patch("cqc_lem.utilities.db.get_comment_outcomes", return_value=rows))
        hold = es.enter_context(
            patch("cqc_lem.utilities.linkedin.rate_limit.hold_commenting", return_value=True))
        track = es.enter_context(
            patch("cqc_lem.utilities.observability.track_comment_quality"))
        crit = es.enter_context(patch("cqc_lem.utilities.logger.log_critical"))
        return auto_weekly_comment_quality(days=7), hold, track, crit

    def test_healthy_user_is_reported_but_not_held(self):
        with ExitStack() as es:
            result, hold, track, _crit = self._run(es, self._rows(12, 1))
        assert "1/1" in result and "0 held" in result
        assert track.called and not hold.called
        assert track.call_args.args[1]["unreadable_readings"] == 0

    def test_demoted_user_is_held_and_escalated(self):
        with ExitStack() as es:
            result, hold, _track, crit = self._run(es, self._rows(12, 0))
        assert "1 held" in result
        assert hold.called and crit.called
        assert hold.call_args.args[0] == 1

    def test_thin_sample_is_never_held(self):
        with ExitStack() as es:
            result, hold, track, _crit = self._run(es, self._rows(3, 0))
        assert "0 held" in result and track.called and not hold.called

    def test_user_with_no_readings_is_not_reported(self):
        with ExitStack() as es:
            result, hold, track, _crit = self._run(es, [])
        assert "0/1" in result and not track.called and not hold.called

    def test_unreadable_readings_are_tracked(self):
        from cqc_lem.app.run_scheduler import auto_weekly_comment_quality
        rows = [{"status": "checked", "author_replied": 0, "reply_count": 0, "like_count": 0,
                 "visible_most_relevant": None, "our_reply_sent": 0}]
        with patch(f"{RS}.get_active_user_ids", return_value=[1]), \
             patch("cqc_lem.utilities.db.get_comment_outcomes", return_value=rows), \
             patch("cqc_lem.utilities.linkedin.rate_limit.hold_commenting") as hold, \
             patch("cqc_lem.utilities.observability.track_comment_quality") as track:
            auto_weekly_comment_quality(days=7)
        assert track.called
        assert track.call_args.args[1]["unreadable_readings"] == 1
        assert not hold.called

    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import auto_weekly_comment_quality
        with patch(f"{RS}.get_active_user_ids", return_value=[]):
            assert auto_weekly_comment_quality() == "No active users"


class TestOutcomeDispatcher:
    def test_throttled_dispatcher_sends_nothing(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        with patch(f"{RS}._skip_if_throttled", return_value=True):
            assert dispatch_comment_outcome_sweeps() == "Automation throttled"

    def test_dispatches_only_users_with_a_session_and_a_due_interval(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        client = MagicMock(); client.set.side_effect = [True, False]
        with ExitStack() as es:
            es.enter_context(patch(f"{RS}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1, 2, 3]))
            es.enter_context(patch(f"{RS}.has_linkedin_session", side_effect=lambda u: u != 3))
            es.enter_context(patch("cqc_lem.utilities.linkedin.rate_limit._redis_client",
                                   return_value=client))
            es.enter_context(patch(f"{RS}.dispatch_jitter_seconds", return_value=5))
            apply = es.enter_context(patch(f"{RS}.sweep_comment_outcomes.apply_async"))
            result = dispatch_comment_outcome_sweeps()
        assert apply.call_count == 1  # user 1 due, user 2 already swept, user 3 has no session
        assert "1/3" in result

    def test_no_active_users(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        with ExitStack() as es:
            es.enter_context(patch(f"{RS}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[]))
            apply = es.enter_context(patch(f"{RS}.sweep_comment_outcomes.apply_async"))
            assert dispatch_comment_outcome_sweeps() == "No active users"
        assert not apply.called

    def test_a_redis_error_on_the_interval_gate_still_dispatches(self):
        # The gate is a nicety; losing it must not cost the sweep — the work list is already
        # at-most-once per comment, so an extra dispatch just finds nothing to do.
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        client = MagicMock(); client.set.side_effect = RuntimeError("boom")
        with ExitStack() as es:
            es.enter_context(patch(f"{RS}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1]))
            es.enter_context(patch(f"{RS}.has_linkedin_session", return_value=True))
            es.enter_context(patch("cqc_lem.utilities.linkedin.rate_limit._redis_client",
                                   return_value=client))
            es.enter_context(patch(f"{RS}.dispatch_jitter_seconds", return_value=5))
            apply = es.enter_context(patch(f"{RS}.sweep_comment_outcomes.apply_async"))
            dispatch_comment_outcome_sweeps()
        assert apply.call_count == 1

    def test_no_redis_still_dispatches(self):
        from cqc_lem.app.run_scheduler import dispatch_comment_outcome_sweeps
        with ExitStack() as es:
            es.enter_context(patch(f"{RS}._skip_if_throttled", return_value=False))
            es.enter_context(patch(f"{RS}.get_active_user_ids", return_value=[1]))
            es.enter_context(patch(f"{RS}.has_linkedin_session", return_value=True))
            es.enter_context(patch("cqc_lem.utilities.linkedin.rate_limit._redis_client",
                                   return_value=None))
            es.enter_context(patch(f"{RS}.dispatch_jitter_seconds", return_value=5))
            apply = es.enter_context(patch(f"{RS}.sweep_comment_outcomes.apply_async"))
            dispatch_comment_outcome_sweeps()
        assert apply.call_count == 1


class TestLiveValidationVerdict:
    def _llv(self):
        import importlib.util
        from pathlib import Path
        script = Path(__file__).resolve().parents[3] / "scripts" / "linkedin_live_validation.py"
        spec = importlib.util.spec_from_file_location("linkedin_live_validation_628", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_visible(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": True}) == "visible"

    def test_demoted(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": False,
             "switched_to_recent": True, "found_most_recent": True}) == "demoted"

    def test_no_sort_control_is_ambiguous(self):
        assert self._llv().comment_outcome_verdict({"sort_control_found": False}).startswith("ambiguous")

    def test_failed_switch_is_ambiguous_not_demoted(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": False,
             "switched_to_recent": False}) == "ambiguous: could not switch sort"

    def test_absent_from_both_is_ambiguous(self):
        assert self._llv().comment_outcome_verdict(
            {"sort_control_found": True, "found_most_relevant": False,
             "switched_to_recent": True, "found_most_recent": False}).startswith("ambiguous")
