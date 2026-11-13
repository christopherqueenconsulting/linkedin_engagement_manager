"""Recent-activity post-card chain (issue #2551).

The fixtures below are SHAPED on the SDUI render the roster lane already reads on the same page
(`[data-testid='expandable-text-box']` inside a card carrying a `/feed/update/` anchor) and on the
pre-SDUI `data-urn` render. They are NOT yet cut from a live probe reading: the `--roster-follow`
probe's `activity_locators.sample_card_html` is the reading that replaces them.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from cqc_lem.utilities.linkedin import scrapper

POST_A = "Shipping agents in production means owning the boring parts: retries, budgets and logs."
POST_B = "Three things I learned running evals on every pull request for a year, in order of cost."

SDUI_PAGE = f"""
<main>
  <div role="list">
    <div role="listitem">
      <a href="https://www.linkedin.com/feed/update/urn:li:activity:111/">Ann Author</a>
      <span><span>2d •</span></span>
      <div><span data-testid="expandable-text-box">{POST_A}</span></div>
      <button>Comment</button>
    </div>
    <div role="listitem">
      <a href="https://www.linkedin.com/feed/update/urn:li:ugcPost:222/">Ann Author</a>
      <span>1w • Edited</span>
      <div><span data-testid="expandable-text-box">{POST_B}</span></div>
      <button>Comment</button>
    </div>
  </div>
</main>
"""

LEGACY_PAGE = f"""
<div data-urn="urn:li:activity:333">
  <span class="update-components-actor__sub-description"><span aria-hidden="true">3d •</span></span>
  <div class="update-components-text">{POST_A}</div>
</div>
<div data-urn="urn:li:activity:444">
  <div class="update-components-text">{POST_B}</div>
</div>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


@pytest.mark.unit
class TestParseRecentActivity:
    def test_sdui_cards_read_through_the_first_rung(self):
        posts, rung = scrapper.parse_recent_activity(_soup(SDUI_PAGE))
        assert rung == "sdui_expandable_text"
        assert [p["link"] for p in posts] == [
            "https://www.linkedin.com/feed/update/urn:li:activity:111",
            "https://www.linkedin.com/feed/update/urn:li:ugcPost:222",
        ]
        assert [p["text"] for p in posts] == [POST_A, POST_B]

    def test_each_card_is_dated_off_its_own_caption(self):
        posts, _ = scrapper.parse_recent_activity(_soup(SDUI_PAGE))
        today = datetime.combine(datetime.now(), datetime.min.time())
        assert posts[0]["posted"] == today - timedelta(days=2)
        assert posts[1]["posted"] == today - timedelta(days=7)

    def test_legacy_render_falls_through_to_the_second_rung(self):
        posts, rung = scrapper.parse_recent_activity(_soup(LEGACY_PAGE))
        assert rung == "legacy_update_text"
        assert [p["link"] for p in posts] == [
            "https://www.linkedin.com/feed/update/urn:li:activity:333",
            "https://www.linkedin.com/feed/update/urn:li:activity:444",
        ]

    def test_an_undated_card_keeps_its_post_and_reads_none_not_today(self):
        # The old zip truncated to the shortest query, so ONE undated card dropped a post.
        posts, _ = scrapper.parse_recent_activity(_soup(LEGACY_PAGE))
        assert posts[0]["posted"] is not None
        assert posts[1]["posted"] is None

    def test_the_card_never_borrows_a_neighbours_urn(self):
        html = f"""<div>
          <div><a href="/feed/update/urn:li:activity:1/">x</a>
               <span data-testid="expandable-text-box">{POST_A}</span></div>
          <div><span data-testid="expandable-text-box">{POST_B}</span></div>
        </div>"""
        posts, _ = scrapper.parse_recent_activity(_soup(html))
        assert [p["text"] for p in posts] == [POST_A]

    def test_a_container_urn_outranks_a_reshared_anchor(self):
        html = f"""<div data-urn="urn:li:activity:9">
          <a href="/feed/update/urn:li:activity:8/">original</a>
          <span data-testid="expandable-text-box">{POST_A}</span></div>"""
        posts, _ = scrapper.parse_recent_activity(_soup(html))
        assert posts[0]["link"].endswith("urn:li:activity:9")

    def test_a_reshare_with_two_boxes_is_one_post(self):
        # Two text boxes, ONE Comment button: the reshare's own commentary and the original's text.
        html = f"""<div><div data-urn="urn:li:activity:5">
          <span data-testid="expandable-text-box">{POST_A}</span>
          <div><span data-testid="expandable-text-box">{POST_B}</span></div>
          <button aria-label="Comment">Comment</button></div>
          <div data-urn="urn:li:activity:6">
          <span data-testid="expandable-text-box">{POST_B} again</span>
          <button>Comment</button></div></div>"""
        posts, _ = scrapper.parse_recent_activity(_soup(html))
        assert [p["link"][-1] for p in posts] == ["5", "6"]
        assert posts[0]["text"] == POST_A

    def test_short_text_boxes_are_not_posts(self):
        html = """<div data-urn="urn:li:activity:5">
          <span data-testid="expandable-text-box">see more</span></div>"""
        assert scrapper.parse_recent_activity(_soup(html)) == ([], None)

    def test_now_caption_is_today(self):
        html = f"""<div data-urn="urn:li:activity:5"><span>now</span>
          <span data-testid="expandable-text-box">{POST_A}</span></div>"""
        posts, _ = scrapper.parse_recent_activity(_soup(html))
        assert posts[0]["posted"] == datetime.combine(datetime.now(), datetime.min.time())

    def test_degree_badge_is_not_a_date(self):
        html = f"""<div data-urn="urn:li:activity:5"><span>• 1st</span><span>1st</span>
          <span data-testid="expandable-text-box">{POST_A}</span></div>"""
        posts, _ = scrapper.parse_recent_activity(_soup(html))
        assert posts[0]["posted"] is None

    def test_page_post_count_spans_every_rung(self):
        assert scrapper.recent_activity_page_posts(_soup(SDUI_PAGE)) == 2
        assert scrapper.recent_activity_page_posts(_soup(LEGACY_PAGE)) == 2
        assert scrapper.recent_activity_page_posts(_soup("<main></main>")) == 0


def _driver() -> MagicMock:
    driver = MagicMock()
    driver.current_url = "https://www.linkedin.com/in/ann/"
    return driver


@pytest.mark.unit
class TestGetProfileRecentActivity:
    def _run(self, html: str):
        with patch.object(scrapper, "go_to_base_employee_link"), \
                patch.object(scrapper, "wait_for_ajax"), \
                patch.object(scrapper, "get_page_source", return_value=_soup(html)), \
                patch("cqc_lem.utilities.linkedin.zero_walk.log_warning") as warn, \
                patch("cqc_lem.utilities.linkedin.zero_walk.log_debug") as debug:
            driver = _driver()
            result = scrapper.get_profile_recent_activity(driver, "https://www.linkedin.com/in/ann/")
        return driver, result, warn, debug

    def test_opens_the_all_activity_page_and_returns_the_cards(self):
        driver, result, warn, _debug = self._run(SDUI_PAGE)
        driver.get.assert_called_once_with("https://www.linkedin.com/in/ann/recent-activity/all/")
        assert len(result) == 2
        warn.assert_not_called()

    def test_page_has_posts_but_chain_empty_warns(self):
        # Text boxes render, but no card carries a URN or a /feed/update/ anchor.
        html = f"<main><div><span data-testid='expandable-text-box'>{POST_A}</span></div></main>"
        _driver, result, warn, _debug = self._run(html)
        assert result == []
        warn.assert_called_once()
        assert "Recent-activity post-card chain" in warn.call_args.args[0]

    def test_a_page_with_no_posts_is_a_quiet_author_not_a_warning(self):
        _driver, result, warn, debug = self._run("<main><h2>No recent posts</h2></main>")
        assert result == []
        warn.assert_not_called()
        debug.assert_called_once()
