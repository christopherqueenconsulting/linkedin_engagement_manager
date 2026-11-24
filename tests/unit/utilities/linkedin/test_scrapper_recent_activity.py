"""Recent-activity post-card chain (issue #2551).

`SDUI_PAGE` and `LEGACY_PAGE` are SHAPED fixtures that pin the chain's mechanics. The two files under
`fixtures/` are different: whole cards cut from a live read-only probe of a roster author's
`/recent-activity/all/` (deployed v0.215.0, 2026-11-24), anonymised for this public repo. Tag
structure, attributes, class tokens, `componentkey` and `data-sdui-anchor-id` are as read; names,
slugs, post text, image sources and the member id are placeholders. Neither card carries a
`/feed/update/` anchor or an activity URN attribute (#2574), so the chain cannot resolve a permalink.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
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


FIXTURES = Path(__file__).parent / "fixtures"
LIVE_CARD_FILES = ("recent_activity_card_connect.html", "recent_activity_card_follow.html")
ALLOWED_PROFILE_SLUGS = {
    "source-author", "original-author-a", "original-author-b",
    "mentioned-person-one", "mentioned-person-two",
}
ALLOWED_COMPANY_SLUGS = {"company-one", "company-two", "company-three"}


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _live_card(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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


@pytest.mark.unit
class TestLiveCards:
    """Whole cards from the live reading: complete, but with no permalink the chain can use."""

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_card_is_complete_and_carries_the_live_anchors(self, name):
        html = _live_card(name)
        soup = _soup(html)
        assert html.count("<div") == html.count("</div>")
        assert len(soup.select("[data-testid='expandable-text-box']")) == 1
        assert soup.select("[role='listitem'][componentkey^='update-card-focus']")
        assert soup.select("[data-sdui-anchor-id^='feed-header-']")
        assert soup.find("button", attrs={"aria-label": "Comment"}) is not None

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_card_has_no_feed_update_anchor_and_no_urn_attribute(self, name):
        shape = scrapper.recent_activity_shape(_soup(_live_card(name)))
        assert shape["text_boxes"] == 1
        assert shape["feed_update_anchors"] == 0
        assert shape["urn_attrs"] == 0

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_chain_resolves_no_post_and_the_page_still_counts_one(self, name):
        soup = _soup(_live_card(name))
        assert scrapper.parse_recent_activity(soup) == ([], None)
        assert scrapper.recent_activity_page_posts(soup) == 1

    def test_two_cards_on_one_page_count_two_posts(self):
        page = "<main><div role='list'>" + "".join(_live_card(n) for n in LIVE_CARD_FILES) + "</div></main>"
        soup = _soup(page)
        assert scrapper.recent_activity_page_posts(soup) == 2
        assert scrapper.parse_recent_activity(soup) == ([], None)

    def test_a_live_card_with_a_permalink_added_reads_through_the_first_rung(self):
        # The only change is the anchor the live card lacks: the chain's other steps work on it.
        soup = _soup(_live_card(LIVE_CARD_FILES[0]))
        soup.select_one("[data-testid='expandable-text-box']").insert_before(
            _soup('<a href="https://www.linkedin.com/feed/update/urn:li:activity:7/">x</a>').a)
        posts, rung = scrapper.parse_recent_activity(soup)
        assert rung == "sdui_expandable_text"
        assert posts[0]["link"].endswith("urn:li:activity:7")
        assert posts[0]["posted"] == datetime.combine(datetime.now(), datetime.min.time()) - timedelta(days=1)


@pytest.mark.unit
class TestLiveCardsAreAnonymised:
    """The repo is public: a fixture must never carry a real person, post or image."""

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_only_placeholder_profile_and_company_slugs(self, name):
        html = _live_card(name)
        assert set(re.findall(r"linkedin\.com/in/([^/\"?]+)", html)) <= ALLOWED_PROFILE_SLUGS
        assert set(re.findall(r"linkedin\.com/company/([^/\"?]+)", html)) <= ALLOWED_COMPANY_SLUGS

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_no_real_image_source_or_member_id(self, name):
        html = _live_card(name)
        assert "licdn.com" not in html
        assert set(re.findall(r"urn:li:member:(\d+)", html)) <= {"0000000"}

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_names_in_labels_are_placeholders(self, name):
        allowed = ("Source Author", "Original Author A", "Original Author B",
                   "Mentioned Person One", "Mentioned Person Two",
                   "Company One", "Company Two", "Company Three")
        named = re.compile(r"^(View .+’s profile|View company: .+|Open control menu for post by .+|"
                           r"Invite .+ to connect|Follow .+)$")
        labels = [el["aria-label"] for el in _soup(_live_card(name)).find_all(attrs={"aria-label": named})]
        assert labels
        for label in labels:
            assert any(a in label for a in allowed), label


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

    @pytest.mark.parametrize("name", LIVE_CARD_FILES)
    def test_live_card_without_a_permalink_warns_with_its_shape(self, name):
        _driver, result, warn, _debug = self._run("<main>" + _live_card(name) + "</main>")
        assert result == []
        warn.assert_called_once()
        assert "Recent-activity post-card chain" in warn.call_args.args[0]
        assert warn.call_args.kwargs["text_boxes"] == 1
        assert warn.call_args.kwargs["feed_update_anchors"] == 0
        assert warn.call_args.kwargs["urn_attrs"] == 0

    def test_drift_warning_carries_author_shape_and_lane(self):
        html = (f"<main><div data-x='urn:li:comment:(post,2)'>"
                f"<span data-testid='expandable-text-box'>{POST_A}</span></div></main>")
        with patch.object(scrapper, "go_to_base_employee_link"), \
                patch.object(scrapper, "wait_for_ajax"), \
                patch.object(scrapper, "get_page_source", return_value=_soup(html)), \
                patch("cqc_lem.utilities.linkedin.zero_walk.log_warning") as warn:
            scrapper.get_profile_recent_activity(
                _driver(), "https://www.linkedin.com/in/ann/", lane="outreach_roster")
        ctx = warn.call_args.kwargs
        assert ctx["profile_url"] == "https://www.linkedin.com/in/ann/"
        assert ctx["activity_url"].endswith("/recent-activity/all/")
        assert ctx["lane"] == "outreach_roster"
        assert ctx["text_boxes"] == 1
        assert ctx["feed_update_anchors"] == 0
        assert ctx["comment_urn_attrs"] == 1

    def test_lane_defaults_to_unspecified(self):
        html = f"<main><span data-testid='expandable-text-box'>{POST_A}</span></main>"
        _d, _r, warn, _dbg = self._run(html)
        assert warn.call_args.kwargs["lane"] == "unspecified"
        assert warn.call_args.kwargs["urn_attrs"] == 0

    def test_a_page_with_no_posts_is_a_quiet_author_not_a_warning(self):
        _driver, result, warn, debug = self._run("<main><h2>No recent posts</h2></main>")
        assert result == []
        warn.assert_not_called()
        debug.assert_called_once()
