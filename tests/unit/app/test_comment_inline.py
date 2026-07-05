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


def _state(label):
    el = MagicMock()
    el.get_attribute.return_value = label
    return el


class TestReactToPostInline:
    def test_reacts_when_not_yet_reacted(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: Like reaction")]), \
             patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(),
                                         post_content="p", comment_text="c", user_id=1)
        assert ok is True

    def test_skips_when_already_reacted(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction") as cpr, \
             patch(f"{_RA}.find_first", return_value=_state("Reaction button state: Celebrate reaction")), \
             patch(f"{_RA}.click_first") as cf:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False
        cpr.assert_not_called()      # no AI spend when we've already reacted
        cf.assert_not_called()       # and we never open the menu

    def test_false_when_menu_wont_open(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.find_first", return_value=_state("Reaction button state: no reaction")), \
             patch(f"{_RA}.click_first", return_value=None):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False

    def test_clicks_the_ai_chosen_reaction(self):
        from cqc_lem.app import run_automation as ra
        seen = []

        def _capture(driver, wait, locators, label, **kw):
            seen.append(locators)
            return MagicMock()

        with patch(f"{_RA}.choose_post_reaction", return_value="Support"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: Support reaction")]), \
             patch(f"{_RA}.click_first", side_effect=_capture):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(),
                                         post_content="p", comment_text="c", user_id=1)
        assert ok is True
        # 2nd click_first is the reaction click; its primary locator targets aria-label='Support'
        assert any("aria-label='Support'" in loc[1] for loc in seen[1])

    def test_false_when_reaction_did_not_register(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: no reaction")]), \
             patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False  # toggle never flipped away from 'no reaction'
