"""Unit tests for the permalink comment path (issue #966).

`comment_on_post` is the live comment task behind BOTH profile-viewer engagement and the outreach
funnel's COMMENT stage. It used to locate the composer through pre-SDUI class-keyed XPaths
(`comments-comment-texteditor`, `comments-comment-box__submit-button--cr`) that LinkedIn removed,
so it could only time out and fall through to a bare Keys.ENTER that logged its own result as
"might not have worked". These tests pin the rebuilt path: the shared SDUI engine, the react-then-
comment order, and the fact that a comment which does not land is never recorded as one.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# `check_commented` / `_thread_carries_our_comment` went with the DM cluster to
# `app.engagement.outreach` (#1154); `comment_on_post` and the permalink card walk went to
# `app.engagement.feed`. Both aliases are live in this file, and each test patches the module
# whose globals the code under test reads.
_OUT = "cqc_lem.app.engagement.outreach"
_FEED = "cqc_lem.app.engagement.feed"

_PERMALINK = "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000001/"
_WANTED_URN = "urn:li:activity:7000000000000000001"
_OTHER_URN = "urn:li:activity:7000000000000000002"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_OUT}.time.sleep"), patch(f"{_FEED}.time.sleep"):
        yield


def _driver_with_boxes(n):
    driver = MagicMock()
    driver.find_elements.return_value = [MagicMock(name=f"box{i}") for i in range(n)]
    return driver


class TestPermalinkPostCard:
    """A permalink page is not a one-post page — LinkedIn stacks recommendations beneath the post."""

    def test_picks_the_card_carrying_the_permalinks_urn(self):
        from cqc_lem.app.engagement import feed as ra
        driver = _driver_with_boxes(3)
        cards = [MagicMock(name="rec"), MagicMock(name="target"), MagicMock(name="rec2")]
        urns = {id(cards[0]): _OTHER_URN, id(cards[1]): _WANTED_URN, id(cards[2]): _OTHER_URN}
        with patch(f"{_FEED}._card_for_textbox", side_effect=lambda d, b: cards.pop(0) if cards else None), \
             patch(f"{_FEED}._feed_post_urn_from_card", side_effect=lambda c, driver=None: urns[id(c)]):
            chosen = ra._permalink_post_card(driver, _PERMALINK, user_id=1)
        assert urns[id(chosen)] == _WANTED_URN

    def test_no_commentable_card_returns_none(self):
        from cqc_lem.app.engagement import feed as ra
        driver = _driver_with_boxes(2)
        with patch(f"{_FEED}._card_for_textbox", return_value=None):
            assert ra._permalink_post_card(driver, _PERMALINK, user_id=1) is None

    def test_top_card_belonging_to_another_post_is_refused(self):
        # Commenting on a "More posts for you" recommendation is worse than not commenting.
        from cqc_lem.app.engagement import feed as ra
        driver = _driver_with_boxes(1)
        with patch(f"{_FEED}._card_for_textbox", side_effect=lambda d, b: MagicMock()), \
             patch(f"{_FEED}._feed_post_urn_from_card", return_value=_OTHER_URN):
            assert ra._permalink_post_card(driver, _PERMALINK, user_id=1) is None

    def test_falls_back_to_top_card_when_no_urn_is_readable(self):
        from cqc_lem.app.engagement import feed as ra
        driver = _driver_with_boxes(2)
        top = MagicMock(name="top")
        cards = [top, MagicMock(name="second")]
        with patch(f"{_FEED}._card_for_textbox", side_effect=lambda d, b: cards.pop(0)), \
             patch(f"{_FEED}._feed_post_urn_from_card", return_value=None):
            assert ra._permalink_post_card(driver, _PERMALINK, user_id=1) is top

    def test_url_without_a_urn_uses_the_top_card(self):
        from cqc_lem.app.engagement import feed as ra
        driver = _driver_with_boxes(1)
        top = MagicMock(name="top")
        urn_scan = MagicMock()
        with patch(f"{_FEED}._card_for_textbox", return_value=top), \
             patch(f"{_FEED}._feed_post_urn_from_card", urn_scan):
            assert ra._permalink_post_card(driver, "https://www.linkedin.com/posts/foo", user_id=1) is top
        urn_scan.assert_not_called()  # nothing to match against, so no URN scan is paid for

    def test_stale_box_is_skipped_not_fatal(self):
        from selenium.common import StaleElementReferenceException

        from cqc_lem.app.engagement import feed as ra
        driver = _driver_with_boxes(2)
        good = MagicMock(name="good")
        outcomes = [StaleElementReferenceException("gone"), good]

        def _card(_d, _b):
            nxt = outcomes.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        with patch(f"{_FEED}._card_for_textbox", side_effect=_card), \
             patch(f"{_FEED}._feed_post_urn_from_card", return_value=None):
            assert ra._permalink_post_card(driver, _PERMALINK, user_id=1) is good


class TestPostTextFromCard:
    def test_reads_only_the_post_body_nodes(self):
        from cqc_lem.app.engagement import feed as ra
        card = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.text = "The post body."
        second.text = "  "
        card.find_elements.return_value = [first, second]
        assert ra._post_text_from_card(card) == "The post body."

    def test_unreadable_card_is_empty_not_fatal(self):
        from cqc_lem.app.engagement import feed as ra
        card = MagicMock()
        card.find_elements.side_effect = Exception("boom")
        assert ra._post_text_from_card(card) == ""


def _run_comment_on_post(*, card=MagicMock, post_returns=True, react_returns=True,
                         reactions_enabled=True, already_commented=False, claim=True):
    """Drive comment_on_post with every DB/Selenium collaborator mocked. Returns (result, mocks)."""
    from cqc_lem.app.engagement import feed as ra

    resolved_card = MagicMock(name="card") if card is MagicMock else card
    calls = []
    driver = MagicMock()
    mocks = {}

    with ExitStack() as es:
        p = lambda name, **kw: es.enter_context(patch(f"{_FEED}.{name}", **kw))
        p("INLINE_REACTIONS_ENABLED", new=reactions_enabled)
        p("has_user_commented_on_post_url", return_value=False)
        p("has_commented_post", return_value=already_commented)
        mocks["claim"] = p("claim_post_for_comment", return_value=claim)
        p("get_driver_wait_pair", return_value=(driver, MagicMock()))
        p("get_user_password_pair_by_id", return_value=("a@b.c", "pw"))
        p("login_to_linkedin")
        p("quit_gracefully")
        p("_permalink_post_card", return_value=resolved_card)
        p("_post_text_from_card", return_value="The post body.")
        mocks["react"] = p("react_to_post_inline",
                           side_effect=lambda *a, **k: calls.append("react") or react_returns)
        mocks["post_inline"] = p("post_comment_inline",
                                 side_effect=lambda *a, **k: calls.append("comment") or post_returns)
        mocks["mark"] = p("mark_post_commented")
        mocks["release"] = p("release_post_claim")
        mocks["log"] = p("insert_new_log")
        mocks["record"] = p("record_action")
        result = ra.comment_on_post.run(user_id=1, post_link=_PERMALINK, comment_text="Nice one.")
    mocks["calls"] = calls
    return result, mocks


def _log_results(insert_new_log):
    return [c.kwargs.get("result") for c in insert_new_log.call_args_list]


class TestCommentOnPost:
    def test_reacts_before_commenting_then_records_success(self):
        from cqc_lem.utilities.db import LogResultType
        result, m = _run_comment_on_post()
        # Submitting re-renders the card and stales everything resolved from it, so the reaction
        # has to happen first — the old order could only ever fail.
        assert m["calls"] == ["react", "comment"]
        m["mark"].assert_called_once()
        m["release"].assert_not_called()
        assert _log_results(m["log"]) == [LogResultType.SUCCESS]
        assert "Added Comment via Post Button" in result
        assert "Added Post Reaction" in result

    def test_failed_comment_releases_the_claim_and_logs_failure(self):
        from cqc_lem.utilities.db import LogResultType
        result, m = _run_comment_on_post(post_returns=False)
        m["mark"].assert_not_called()          # never recorded as a comment we left
        m["release"].assert_called_once()      # a later run may retry
        assert _log_results(m["log"]) == [LogResultType.FAILURE]
        from cqc_lem.app.engagement.feed import COMMENT_NOT_POSTED_MESSAGE
        assert COMMENT_NOT_POSTED_MESSAGE in result

    def test_no_commentable_card_never_opens_a_composer(self):
        from cqc_lem.app.engagement.feed import NO_COMMENTABLE_CARD_MESSAGE
        from cqc_lem.utilities.db import LogResultType
        result, m = _run_comment_on_post(card=None)
        assert result == NO_COMMENTABLE_CARD_MESSAGE
        m["post_inline"].assert_not_called()
        m["react"].assert_not_called()
        m["mark"].assert_not_called()
        m["release"].assert_called_once()
        assert _log_results(m["log"]) == [LogResultType.FAILURE]

    def test_reaction_failure_never_blocks_the_comment(self):
        from cqc_lem.utilities.db import LogResultType
        result, m = _run_comment_on_post(react_returns=False)
        m["mark"].assert_called_once()
        assert _log_results(m["log"]) == [LogResultType.SUCCESS]
        assert "Added Post Reaction" not in result

    def test_already_reacted_is_a_no_op_not_a_failure(self):
        result, m = _run_comment_on_post(react_returns=None)
        m["mark"].assert_called_once()
        assert "Added Post Reaction" not in result

    def test_reaction_tourniquet_stands_the_permalink_path_down_too(self):
        # One env flip has to stand BOTH comment paths down on the next SDUI rotation (#816).
        result, m = _run_comment_on_post(reactions_enabled=False)
        m["react"].assert_not_called()
        m["post_inline"].assert_called_once()
        assert "Added Comment via Post Button" in result

    def test_lost_claim_never_opens_a_browser(self):
        result, m = _run_comment_on_post(claim=False)
        assert "already claimed" in result
        m["post_inline"].assert_not_called()

    def test_a_landed_comment_spends_the_account_envelope(self):
        # This path posted nothing while #966 was live, so its missing record_action cost nothing.
        # Now that it lands comments, one the governor can't see lets the feed walk and the roster
        # lane spend a whole day's envelope on top of it (#626).
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT
        _result, m = _run_comment_on_post()
        m["record"].assert_called_once_with(1, ACTION_COMMENT)

    def test_a_comment_that_never_landed_spends_nothing(self):
        _result, m = _run_comment_on_post(post_returns=False)
        m["record"].assert_not_called()

    def test_no_commentable_card_spends_nothing(self):
        _result, m = _run_comment_on_post(card=None)
        m["record"].assert_not_called()


class TestNoPreSduiAnchorsRemain:
    def test_removed_linkedin_classes_are_never_keyed_on_again(self):
        # These anchors were deleted with LinkedIn's SDUI rewrite; keying on them is the silent
        # failure #966 exists to end, so a LOCATOR using one again is a regression. Prose that
        # merely names them (this file's own docstrings) is fine — the check is for a class-keyed
        # selector, which is what `@class` marks.
        #
        # EVERY engagement module is scanned, by glob rather than by name. The feed engine that
        # owns most of these locators moved to `app.engagement.feed` in #1154 and the comment-thread
        # walk to `app.engagement.posting`, so the old `run_automation`-only scan would have kept
        # passing while going blind to the file the regression would actually land in — and a scan
        # that names its modules one by one goes blind again on the next slice.
        from pathlib import Path

        sources = sorted(Path("src/cqc_lem/app/engagement").glob("*.py"))
        assert len(sources) > 2, f"the scan lost its input: {sources}"
        dead = ("comments-comment-texteditor", "comments-comment-box__submit-button",
                "comments-comment-list__container")
        offenders = [f"{src.name}: {line.strip()}"
                     for src in sources
                     for line in src.read_text(encoding="utf-8").splitlines()
                     if ("@class" in line or "class=" in line) and any(d in line for d in dead)]
        assert offenders == [], f"pre-SDUI class-keyed locator(s) are back: {offenders}"


class TestThreadCarriesOurComment:
    def _profile(self, url="https://www.linkedin.com/in/chris-queen-9b1/"):
        prof = MagicMock()
        prof.profile_url = url
        return prof

    def test_true_when_our_slug_authored_a_rendered_comment(self):
        from cqc_lem.app.engagement import outreach as ra
        items = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/someone-else/"),
                 (MagicMock(), MagicMock(), "https://www.linkedin.com/in/chris-queen-9b1/")]
        with patch(f"{_OUT}._comment_items", return_value=items):
            assert ra._thread_carries_our_comment(MagicMock(), self._profile()) is True

    def test_a_slug_we_are_a_prefix_of_is_not_us(self):
        # Substring matching would read a stranger's comment as ours and silence the post.
        from cqc_lem.app.engagement import outreach as ra
        items = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/chris-queen-9b1-extra/")]
        with patch(f"{_OUT}._comment_items", return_value=items):
            assert ra._thread_carries_our_comment(
                MagicMock(), self._profile("https://www.linkedin.com/in/chris/")) is False

    def test_no_slug_is_false_without_reading_the_thread(self):
        from cqc_lem.app.engagement import outreach as ra
        reader = MagicMock()
        with patch(f"{_OUT}._comment_items", reader):
            assert ra._thread_carries_our_comment(MagicMock(), self._profile("")) is False
        reader.assert_not_called()

    def test_driver_fault_is_false_not_fatal(self):
        from selenium.common import WebDriverException

        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}._comment_items", side_effect=WebDriverException("gone")):
            assert ra._thread_carries_our_comment(MagicMock(), self._profile()) is False

    def test_waits_for_the_thread_to_mount_before_deciding(self):
        # The comment list hydrates AFTER driver.get() returns. Reading it on the first paint sees
        # zero comments on a post that plainly has them, which would make this rebuilt guard a
        # second silently-never-firing check — the #966 defect itself.
        from cqc_lem.app.engagement import outreach as ra
        ours = (MagicMock(), MagicMock(), "https://www.linkedin.com/in/chris-queen-9b1/")
        renders = [[], [], [ours]]
        with patch(f"{_OUT}._comment_items", side_effect=lambda _d: renders.pop(0)):
            assert ra._thread_carries_our_comment(MagicMock(), self._profile()) is True

    def test_a_rendered_thread_without_us_stops_polling(self):
        from cqc_lem.app.engagement import outreach as ra
        stranger = [(MagicMock(), MagicMock(), "https://www.linkedin.com/in/someone-else/")]
        reader = MagicMock(return_value=stranger)
        with patch(f"{_OUT}._comment_items", reader):
            assert ra._thread_carries_our_comment(MagicMock(), self._profile()) is False
        # One poll to render, one to see it stopped growing — never the whole budget.
        assert reader.call_count == 2

    def test_a_thread_that_never_renders_is_false_not_a_hang(self):
        from cqc_lem.app.engagement import outreach as ra
        reader = MagicMock(return_value=[])
        with patch(f"{_OUT}._comment_items", reader):
            assert ra._thread_carries_our_comment(MagicMock(), self._profile()) is False
        assert reader.call_count == ra._COMMENT_THREAD_MOUNT_POLLS


class TestCheckCommented:
    def test_ledger_hit_short_circuits_the_thread_read(self):
        from cqc_lem.app.engagement import outreach as ra
        reader = MagicMock()
        driver = MagicMock()
        driver.current_url = _PERMALINK
        with patch(f"{_OUT}.has_user_commented_on_post_url", return_value=True), \
             patch(f"{_OUT}._thread_carries_our_comment", reader):
            assert ra.check_commented(driver, MagicMock(), 1, _PERMALINK,
                                      my_profile=MagicMock()) is True
        reader.assert_not_called()

    def test_without_a_profile_only_the_ledger_decides(self):
        from cqc_lem.app.engagement import outreach as ra
        reader = MagicMock(return_value=True)
        driver = MagicMock()
        driver.current_url = _PERMALINK
        with patch(f"{_OUT}.has_user_commented_on_post_url", return_value=False), \
             patch(f"{_OUT}._thread_carries_our_comment", reader):
            assert ra.check_commented(driver, MagicMock(), 1, _PERMALINK) is False
        reader.assert_not_called()

    def test_thread_read_catches_a_comment_the_ledger_missed(self):
        from cqc_lem.app.engagement import outreach as ra
        driver = MagicMock()
        driver.current_url = _PERMALINK
        with patch(f"{_OUT}.has_user_commented_on_post_url", return_value=False), \
             patch(f"{_OUT}._thread_carries_our_comment", return_value=True):
            assert ra.check_commented(driver, MagicMock(), 1, _PERMALINK,
                                      my_profile=MagicMock()) is True
