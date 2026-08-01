"""Unit tests for the SDUI inline reply helpers in run_automation."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


def _wait():
    """Minimal WebDriverWait stand-in so a real `find_first` can run: poll once, TimeoutException on
    a falsy result (what a total selector miss looks like to it)."""
    from selenium.common import TimeoutException
    w = MagicMock()

    def until(fn, label=None):
        found = fn(None)
        if not found:
            raise TimeoutException(label)
        return found

    w.until.side_effect = until
    return w


def _box(y, height=40, text=""):
    """A rendered role=textbox whose top edge sits at page coordinate `y`."""
    el = MagicMock()
    el.rect = {"x": 0, "y": y, "width": 600, "height": height}
    el.text = text
    return el


def _comment(y, height=120, composers=()):
    """A comment container at page coordinate `y`, holding `composers` in its own subtree."""
    el = MagicMock()
    el.rect = {"x": 0, "y": y, "width": 600, "height": height}
    el.find_elements.side_effect = lambda by, value: list(composers) if "textbox" in value else []
    return el


def _driver(*boxes):
    """A driver whose document holds `boxes`, in that order — the order a page-wide lookup sees."""
    d = MagicMock()
    d.find_elements.side_effect = lambda by, value: list(boxes) if "textbox" in value else []
    return d


class TestReplyComposerForComment:
    """Issue #883. The composer lookup was page-wide, so the first VISIBLE role=textbox in document
    order won — the post's main 'Add a comment' box (the reply then posts as a standalone comment),
    or one left mounted by a comment already answered earlier in the same sweep."""

    def test_prefers_the_composer_nested_in_this_comment(self):
        from cqc_lem.app import run_automation as ra
        main, mine = _box(100), _box(1030)          # main box is first in document order
        comment = _comment(900, composers=[mine])
        assert ra._reply_composer_for_comment(_driver(main, mine), comment, user_id=1) is mine

    def test_nested_pick_is_the_box_at_the_end_of_the_reply_list(self):
        # A comment's replies nest inside it and the reply box opens after them, so the box nearest
        # the container's BOTTOM is ours — not the first one in document order.
        from cqc_lem.app import run_automation as ra
        a_replys_box, mine = _box(950), _box(1180)
        comment = _comment(900, height=300, composers=[a_replys_box, mine])   # bottom = 1200
        assert ra._reply_composer_for_comment(_driver(a_replys_box, mine), comment, user_id=1) is mine

    def test_takes_the_sibling_box_below_and_never_the_main_box_above(self):
        from cqc_lem.app import run_automation as ra
        main, sibling = _box(100), _box(1030)
        comment = _comment(900)                     # bottom = 1020
        with patch(f"{_RA}._comment_container", return_value=comment):
            got = ra._reply_composer_for_comment(_driver(main, sibling), comment, user_id=1)
        assert got is sibling

    def test_skips_when_the_only_composer_is_above_the_comment(self):
        # Acceptance: borrow nothing. A composer above the comment cannot be its reply box, and
        # answering into the main comment box posts our reply as a new top-level comment.
        from cqc_lem.app import run_automation as ra
        main = _box(100)
        assert ra._reply_composer_for_comment(_driver(main), _comment(900), user_id=1) is None

    def test_skips_when_the_nearest_composer_belongs_to_another_comment(self):
        # Our reply box never opened and a LATER comment has one — replying there answers the
        # wrong person, so skip and let the sweep log the miss.
        from cqc_lem.app import run_automation as ra
        theirs = _box(1300)
        driver = _driver(theirs)
        driver.execute_script.return_value = False          # neither element contains the other
        with patch(f"{_RA}._comment_container", return_value=_comment(1200)):
            got = ra._reply_composer_for_comment(driver, _comment(900), user_id=1)
        assert got is None

    def test_accepts_a_sibling_box_whose_owner_wraps_this_comment(self):
        # The walk-up can stop on a wrapper that HOLDS the comment (reply lists sit inside one);
        # that is still this comment's thread, not another's.
        from cqc_lem.app import run_automation as ra
        sibling = _box(1030)
        driver = _driver(sibling)
        driver.execute_script.return_value = True           # wrapper contains the comment
        with patch(f"{_RA}._comment_container", return_value=_comment(880, height=400)):
            got = ra._reply_composer_for_comment(driver, _comment(900), user_id=1)
        assert got is sibling

    def test_unresolvable_owner_still_takes_the_box_below_this_comment(self):
        # `_comment_container` was written to walk up from a comment BODY and rejects any ancestor
        # holding a GIF/Emoji composer button — which here is the reply composer's OWN toolbar, so
        # it can return null for a perfectly valid reply box. Demanding it resolve would make this
        # branch skip EVERY sibling render, silently. Unresolved is not proof the box is another
        # comment's; the hard above-filter is what keeps the post's main box out.
        from cqc_lem.app import run_automation as ra
        main, sibling = _box(100), _box(1030)
        with patch(f"{_RA}._comment_container", return_value=None):
            got = ra._reply_composer_for_comment(_driver(main, sibling), _comment(900), user_id=1)
        assert got is sibling

    def test_hidden_composers_are_not_candidates(self):
        from cqc_lem.app import run_automation as ra
        collapsed = _box(1030, height=0)                    # display:none renders 0x0
        assert ra._reply_composer_for_comment(_driver(collapsed), _comment(900), user_id=1) is None

    def test_none_when_the_comment_itself_is_not_rendered(self):
        from cqc_lem.app import run_automation as ra
        comment = MagicMock()
        type(comment).rect = property(lambda self: (_ for _ in ()).throw(Exception("stale")))
        assert ra._reply_composer_for_comment(_driver(_box(1030)), comment, user_id=1) is None


class TestInSameComment:
    def test_identity_needs_no_dom_call(self):
        from cqc_lem.app import run_automation as ra
        comment = MagicMock(); driver = MagicMock()
        assert ra._in_same_comment(driver, comment, comment) is True
        driver.execute_script.assert_not_called()

    def test_unresolved_owner_is_not_ours(self):
        from cqc_lem.app import run_automation as ra
        assert ra._in_same_comment(MagicMock(), MagicMock(), None) is False

    def test_dom_failure_is_not_ours(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock(); driver.execute_script.side_effect = Exception("stale")
        assert ra._in_same_comment(driver, MagicMock(), MagicMock()) is False


class TestReplyInline:
    def test_types_into_this_comments_composer_not_the_main_comment_box(self):
        from cqc_lem.app import run_automation as ra
        main = _box(100)                                    # first in document order
        mine = _box(1030, text="")                          # this comment's own reply box
        comment = _comment(900, composers=[mine])
        driver = _driver(main, mine); driver.execute_script.return_value = True
        with patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra._reply_to_comment_inline(driver, _wait(), comment,
                                             "Thanks - what made that work for you?", user_id=1)
        assert ok is True
        mine.send_keys.assert_called_once()
        main.send_keys.assert_not_called()
        main.click.assert_not_called()

    def test_comment_without_a_reachable_composer_skips_instead_of_borrowing_one(self):
        from cqc_lem.app import run_automation as ra
        main = _box(100)
        driver = _driver(main)
        with patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra._reply_to_comment_inline(driver, _wait(), _comment(900), "some reply", user_id=1)
        assert ok is False
        main.send_keys.assert_not_called()

    def test_posts_reply_and_confirms_via_cleared_composer(self):
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""          # composer cleared after a real post
        driver = MagicMock(); driver.execute_script.return_value = True  # submit button clicked
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}._reply_composer_for_comment", return_value=composer):
            ok = ra._reply_to_comment_inline(driver, MagicMock(), MagicMock(),
                                             "hello this is my reply text", user_id=1)
        assert ok is True
        composer.send_keys.assert_called()  # typed the reply

    def test_returns_false_when_composer_still_full(self):
        # Submit failed: composer keeps the text and it's not in the list → NOT a false positive.
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = "hello this is my reply text still here"
        # composer-centering scrollIntoView (#815); then: no submit button; text not in list
        driver = MagicMock(); driver.execute_script.side_effect = [None, False, False]
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}._reply_composer_for_comment", return_value=composer):
            ok = ra._reply_to_comment_inline(driver, MagicMock(), MagicMock(),
                                             "hello this is my reply text", user_id=1)
        assert ok is False
        # Pin the sequence: a shift would exhaust the side_effect list and make _composer_submitted
        # return False off a swallowed StopIteration instead of off the comment-list check.
        assert driver.execute_script.call_count == 3

    def test_returns_false_when_no_reply_button(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}._reply_composer_for_comment") as rc:
            ok = ra._reply_to_comment_inline(MagicMock(), MagicMock(), MagicMock(), "x", user_id=1)
        assert ok is False
        rc.assert_not_called()

    def test_returns_false_when_no_composer(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}._reply_composer_for_comment", return_value=None):
            ok = ra._reply_to_comment_inline(MagicMock(), MagicMock(), MagicMock(), "x", user_id=1)
        assert ok is False

    def test_composer_lookup_is_anchored_to_the_comment(self):
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""
        comment = MagicMock()
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}._reply_composer_for_comment", return_value=composer) as rc:
            ra._reply_to_comment_inline(driver, MagicMock(), comment, "a reply worth posting", user_id=1)
        assert rc.call_args.args[1] is comment


def _thread_comment(y, height=120, composers=()):
    """`_reply_under_comment_inline`'s comment: a rendered container that also answers the Reply
    button lookup (`button[aria-label='Reply']`) the composer lookup does not."""
    el = _comment(y, height=height, composers=composers)
    el.find_elements.side_effect = lambda by, value: (
        list(composers) if "textbox" in value else [MagicMock(name="reply_btn")])
    return el


class TestReplyUnderCommentComposerPick:
    """Issue #886. #478 only PENALISED a composer above the comment (`+1e6`), so when the post's main
    'Add a comment' box was the ONLY visible role=textbox — our reply box never opened, the thread
    re-rendered, the box collapsed — it still won as `best` and the reply posted as a standalone
    top-level comment. Resolution is now shared with `_reply_to_comment_inline` (#883): hard filter."""

    def test_main_comment_box_as_the_only_composer_is_never_borrowed(self):
        from cqc_lem.app import run_automation as ra
        main = _box(100)                                    # the post's own 'Add a comment' box
        comment = _thread_comment(900)
        driver = _driver(main)
        with patch(f"{_RA}.ActionChains"):
            ok = ra._reply_under_comment_inline(driver, MagicMock(), comment, "a real reply", user_id=1)
        assert ok is False
        main.send_keys.assert_not_called()
        main.click.assert_not_called()

    def test_another_comments_composer_is_never_typed_into(self):
        from cqc_lem.app import run_automation as ra
        theirs = _box(1300)                                 # a LATER comment's open reply box
        comment = _thread_comment(900)
        driver = _driver(theirs)
        driver.execute_script.return_value = False          # neither element contains the other
        with patch(f"{_RA}.ActionChains"), \
             patch(f"{_RA}._comment_container", return_value=_comment(1200)):
            ok = ra._reply_under_comment_inline(driver, MagicMock(), comment, "a real reply", user_id=1)
        assert ok is False
        theirs.send_keys.assert_not_called()

    def test_types_into_our_own_box_below_the_comment(self):
        from cqc_lem.app import run_automation as ra
        main, mine = _box(100), _box(1030, text="")
        comment = _thread_comment(900, composers=[mine])
        driver = _driver(main, mine); driver.execute_script.return_value = True
        with patch(f"{_RA}.ActionChains"):
            ok = ra._reply_under_comment_inline(driver, MagicMock(), comment, "a real reply", user_id=1)
        assert ok is True
        mine.send_keys.assert_called_once()
        main.send_keys.assert_not_called()

    def test_both_reply_paths_use_the_same_composer_resolver(self):
        # Acceptance: ONE composer-resolution helper. Kept as a test so a future edit can't quietly
        # reintroduce a second, softer pick on one of the two paths.
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""
        under_comment, inline_comment = MagicMock(), MagicMock()
        under_comment.find_elements.return_value = [MagicMock(name="reply_btn")]
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_RA}.ActionChains"), patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}._reply_composer_for_comment", return_value=composer) as rc:
            assert ra._reply_under_comment_inline(driver, _wait(), under_comment, "reply one", user_id=1) is True
            assert ra._reply_to_comment_inline(driver, _wait(), inline_comment, "reply two", user_id=1) is True
        assert [c.args[1] for c in rc.call_args_list] == [under_comment, inline_comment]

    def test_empty_reply_text_is_never_typed(self):
        from cqc_lem.app import run_automation as ra
        composer = MagicMock()
        comment = MagicMock(); comment.find_elements.return_value = [MagicMock(name="reply_btn")]
        with patch(f"{_RA}.ActionChains"), \
             patch(f"{_RA}._reply_composer_for_comment", return_value=composer):
            ok = ra._reply_under_comment_inline(MagicMock(), _wait(), comment, "   ", user_id=1)
        assert ok is False
        composer.send_keys.assert_not_called()


class TestCommentItemsFromThread:
    def test_walks_up_from_reply_buttons(self):
        from cqc_lem.app import run_automation as ra
        rb1, rb2 = MagicMock(), MagicMock()
        item1, item2 = MagicMock(), MagicMock()
        driver = MagicMock()
        driver.execute_script.side_effect = [item1, item2]  # JS walk-up returns a container each
        with patch(f"{_RA}.find_all_first", return_value=[rb1, rb2]):
            items = ra._comment_items_from_thread(driver)
        assert items == [item1, item2]

    def test_empty_when_no_reply_buttons(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.find_all_first", return_value=[]):
            assert ra._comment_items_from_thread(MagicMock()) == []
