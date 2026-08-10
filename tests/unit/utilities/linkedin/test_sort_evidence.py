"""Unit tests for the shared sort-control evidence scan.

Issues #1117 (the comment side) and #1270 (the feed side, which is why the scan is shared at all).

The JS is asserted as TEXT: a unit lane has no DOM, and the properties that matter here are
structural — two passes, a bounded cap, prose that cannot match — so the string is the artifact
under test. The behaviour itself is grounded live through the read-only probes
(`scripts/linkedin_live_validation.py`).
"""

from unittest.mock import MagicMock

import pytest

from cqc_lem.utilities.linkedin.sort_evidence import (
    SORT_CANDIDATE_SCAN_CAP,
    SORT_CONTROL_OWN_TEXT_MAX,
    build_sort_control_scan_js,
    scan_sort_control_candidates,
)

pytestmark = pytest.mark.unit


def _js(items=("[data-testid='item']",), container="[data-testid='list']") -> str:
    return build_sort_control_scan_js(item_selectors=list(items), prose_container=container)


class TestBuildSortControlScanJs:
    def test_the_root_is_the_main_column_for_every_surface(self):
        # The control renders ABOVE the content on both surfaces, so a scan scoped inside the
        # content list could never describe the element that went missing.
        assert "const root=document.querySelector('main')||document.body;" in _js()

    def test_item_selectors_are_tried_in_order(self):
        js = _js(items=["[data-testid='a'] [data-testid='b']", "[data-testid='a']"])
        assert ("const first=document.querySelector(\"[data-testid='a'] [data-testid='b']\")"
                "||document.querySelector(\"[data-testid='a']\");") in js

    def test_the_prose_container_scopes_the_keyword_pass_to_labels(self):
        # Inside user content only a LABEL may match: one post or comment reading 'sort of agree'
        # would otherwise fill the cap with somebody's prose and starve the header pass.
        js = _js(container="[data-testid='list']")
        assert "el.closest(\"[data-testid='list']\")" in js
        assert "KW.test(inList?label:label+' '+text.toLowerCase())" in js

    def test_a_second_pass_describes_the_controls_above_the_first_item(self):
        # A keyword pass alone cannot see the drift it exists to describe — a rotated label matches
        # no sort word, which is the exact shape that left #818 with no evidence for a month.
        js = _js()
        assert "'keyword'" in js and "'header'" in js
        assert "compareDocumentPosition" in js
        assert "if(!out.length){" in js

    def test_an_unanchored_header_pass_says_so(self):
        # 'the page has no findable content' and 'these controls precede the content' are different
        # readings, and a re-grounding pass must be able to tell a near-miss from a shot in the dark.
        assert "push(el,first?'header':'unanchored');" in _js()

    def test_both_passes_ignore_container_elements(self):
        js = _js()
        assert js.count("length>TEXT_MAX) continue;") == 2
        assert f"TEXT_MAX={SORT_CONTROL_OWN_TEXT_MAX};" in js

    def test_the_keywords_cannot_match_a_word_merely_containing_top(self):
        # 'desktop' / 'topic' must not read as the 'Top' sort.
        assert "|\\btop\\b|" in _js()

    def test_the_sample_is_capped_at_the_shared_constant(self):
        js = _js()
        assert f"const CAP={SORT_CANDIDATE_SCAN_CAP};" in js
        assert SORT_CANDIDATE_SCAN_CAP <= 8

    def test_every_row_carries_the_shape_fields_a_re_grounding_needs(self):
        js = _js()
        for field in ("tag:", "data_testid:", "aria_label:", "role:", "text:", "has_popup:",
                      "classes:", "reason:reason"):
            assert field in js


class TestScanSortControlCandidates:
    def _driver(self, result) -> MagicMock:
        driver = MagicMock()
        if isinstance(result, Exception):
            driver.execute_script.side_effect = result
        else:
            driver.execute_script.return_value = result
        return driver

    def test_returns_the_descriptors_the_page_handed_back(self):
        rows = [{"tag": "button", "text": "Sort by", "reason": "header"}]
        assert scan_sort_control_candidates(self._driver(rows), "return 1;") == rows

    def test_a_failed_read_is_an_empty_sample_never_a_raise(self):
        # Evidence collection must never cost the reading it rode in on.
        assert scan_sort_control_candidates(self._driver(RuntimeError("stale")), "js") == []

    def test_none_and_non_dict_rows_are_dropped(self):
        driver = self._driver([{"tag": "button"}, None, "not-a-dict"])
        assert scan_sort_control_candidates(driver, "js") == [{"tag": "button"}]

    def test_the_scan_is_read_only(self):
        driver = self._driver([])
        scan_sort_control_candidates(driver, "return [];")
        driver.get.assert_not_called()
        driver.click.assert_not_called()
