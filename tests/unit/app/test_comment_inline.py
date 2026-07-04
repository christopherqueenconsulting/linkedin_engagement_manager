"""Unit tests for the SDUI inline comment submit + verification fixes."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestStripNonBmp:
    def test_drops_emoji_keeps_text(self):
        from cqc_lem.app.run_automation import _strip_non_bmp
        assert _strip_non_bmp("Hooked and not even sorry 😄 nice") == "Hooked and not even sorry  nice"

    def test_plain_text_unchanged(self):
        from cqc_lem.app.run_automation import _strip_non_bmp
        assert _strip_non_bmp("plain ascii — even en-dash") == "plain ascii — even en-dash"


class TestComposerSubmitted:
    def test_true_when_composer_cleared(self):
        from cqc_lem.app.run_automation import _composer_submitted
        composer = MagicMock(); composer.text = ""
        assert _composer_submitted(MagicMock(), composer, "some comment") is True

    def test_true_when_composer_detached(self):
        from cqc_lem.app.run_automation import _composer_submitted
        composer = MagicMock(); type(composer).text = property(lambda self: (_ for _ in ()).throw(Exception("stale")))
        assert _composer_submitted(MagicMock(), composer, "x") is True

    def test_false_when_full_and_not_in_list(self):
        from cqc_lem.app.run_automation import _composer_submitted
        composer = MagicMock(); composer.text = "still typing this"
        driver = MagicMock(); driver.execute_script.return_value = False
        assert _composer_submitted(driver, composer, "still typing this") is False

    def test_true_when_full_but_in_list(self):
        from cqc_lem.app.run_automation import _composer_submitted
        composer = MagicMock(); composer.text = "leftover"
        driver = MagicMock(); driver.execute_script.return_value = True
        assert _composer_submitted(driver, composer, "leftover") is True


class TestPostCommentInline:
    def test_posts_and_confirms(self):
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""       # cleared after real submit
        driver = MagicMock(); driver.execute_script.return_value = True  # submit button clicked
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer):
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(), "Great post, thanks", user_id=1)
        assert ok is True

    def test_emoji_only_comment_skipped(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.click_first") as cf:
            ok = ra.post_comment_inline(MagicMock(), MagicMock(), MagicMock(), "😄😄😄", user_id=1)
        assert ok is False
        cf.assert_not_called()  # nothing left to type after stripping

    def test_false_when_no_composer(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=None):
            ok = ra.post_comment_inline(MagicMock(), MagicMock(), MagicMock(), "hello", user_id=1)
        assert ok is False
