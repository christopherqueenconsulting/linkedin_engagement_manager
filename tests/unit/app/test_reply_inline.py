"""Unit tests for the SDUI inline reply helpers in run_automation."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestReplyInline:
    def test_posts_reply_and_confirms_via_cleared_composer(self):
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""          # composer cleared after a real post
        driver = MagicMock(); driver.execute_script.return_value = True  # submit button clicked
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer):
            ok = ra._reply_to_comment_inline(driver, MagicMock(), MagicMock(),
                                             "hello this is my reply text", user_id=1)
        assert ok is True
        composer.send_keys.assert_called()  # typed the reply

    def test_returns_false_when_composer_still_full(self):
        # Submit failed: composer keeps the text and it's not in the list → NOT a false positive.
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = "hello this is my reply text still here"
        driver = MagicMock(); driver.execute_script.side_effect = [False, False]  # no button; not in list
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer):
            ok = ra._reply_to_comment_inline(driver, MagicMock(), MagicMock(),
                                             "hello this is my reply text", user_id=1)
        assert ok is False

    def test_returns_false_when_no_reply_button(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}.find_first") as ff:
            ok = ra._reply_to_comment_inline(MagicMock(), MagicMock(), MagicMock(), "x", user_id=1)
        assert ok is False
        ff.assert_not_called()

    def test_returns_false_when_no_composer(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=None):
            ok = ra._reply_to_comment_inline(MagicMock(), MagicMock(), MagicMock(), "x", user_id=1)
        assert ok is False


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
