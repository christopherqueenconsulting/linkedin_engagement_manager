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

    def test_plain_ascii_unchanged(self):
        from cqc_lem.app.run_automation import _strip_non_bmp
        assert _strip_non_bmp("plain ascii, nothing fancy here") == "plain ascii, nothing fancy here"

    def test_normalizes_rogue_typography(self):
        # Em dashes / smart quotes are AI tell-tale signs — normalized to plain ASCII before typing.
        from cqc_lem.app.run_automation import _strip_non_bmp
        assert _strip_non_bmp("clean copy—no em dashes and “no” smart quotes") == \
            "clean copy - no em dashes and \"no\" smart quotes"


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


def _intercepted():
    from selenium.common import ElementClickInterceptedException
    return ElementClickInterceptedException(
        "element click intercepted: Element <div class=\"ql-editor\"> is not clickable at "
        "point (890, 9). Other element would receive the click: <svg ...>")


class TestFocusComposer:
    def test_centers_before_clicking(self):
        # y=9 interception (#815) is the sticky global nav: the composer must be centered, never
        # left wherever the previous action on the card scrolled it to.
        from cqc_lem.app import run_automation as ra
        driver = MagicMock(); composer = MagicMock()
        ra._focus_composer(driver, composer)
        scripts = [c.args[0] for c in driver.execute_script.call_args_list]
        assert any("scrollIntoView({block:'center'})" in s for s in scripts)
        composer.click.assert_called_once()

    def test_recenters_and_retries_once_on_interception(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock(); composer = MagicMock()
        composer.click.side_effect = [_intercepted(), None]
        ra._focus_composer(driver, composer)
        assert composer.click.call_count == 2
        assert len([c for c in driver.execute_script.call_args_list
                    if "scrollIntoView" in c.args[0]]) == 2

    def test_second_interception_raises(self):
        # A real overlay must NOT be papered over with a JS click — let it raise so the caller can
        # name the step and the fault stays visible.
        from cqc_lem.app import run_automation as ra
        from selenium.common import ElementClickInterceptedException
        composer = MagicMock(); composer.click.side_effect = _intercepted()
        with pytest.raises(ElementClickInterceptedException):
            ra._focus_composer(MagicMock(), composer)

    def test_survives_unscrollable_element(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock(); driver.execute_script.side_effect = Exception("no such element")
        composer = MagicMock()
        ra._focus_composer(driver, composer)  # positioning is best-effort
        composer.click.assert_called_once()


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

    def test_only_bmp_text_reaches_send_keys(self):
        # ChromeDriver's send_keys raises on non-BMP characters — the emoji must be gone by then.
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer):
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(),
                                        "Shipped it 🚀 and the numbers held 📈", user_id=1)
        assert ok is True
        typed = composer.send_keys.call_args_list[0].args[0]
        assert all(ord(c) <= 0xFFFF for c in typed)
        assert "Shipped it" in typed and "🚀" not in typed

    def test_click_interception_survived_by_recentering(self):
        # The live #815 failure: first click stolen by the sticky nav, the retry lands.
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""
        composer.click.side_effect = [_intercepted(), None]
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer), \
             patch(f"{_RA}.log_warning") as lw:
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(), "Great post", user_id=1)
        assert ok is True
        lw.assert_not_called()
        composer.send_keys.assert_called_once()


class TestPostCommentInlineStepNaming:
    """One `try` over the whole sequence reported every failure mode as the same warning, so the
    escalated issue never said which step broke — and unrelated faults collapsed into one."""

    def _run(self, composer, driver=None):
        from cqc_lem.app import run_automation as ra
        driver = driver or MagicMock()
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer), \
             patch(f"{_RA}.log_warning") as lw:
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(), "Great post", user_id=1)
        return ok, lw

    def test_names_focus_step(self):
        composer = MagicMock(); composer.click.side_effect = _intercepted()
        ok, lw = self._run(composer)
        assert ok is False
        assert lw.call_args.args[0] == "Inline comment post failed at focus composer"
        assert isinstance(lw.call_args.kwargs["exc"], Exception)

    def test_names_type_step(self):
        from selenium.common import WebDriverException
        composer = MagicMock(); composer.send_keys.side_effect = WebDriverException("bad char")
        ok, lw = self._run(composer)
        assert ok is False
        assert lw.call_args.args[0] == "Inline comment post failed at type comment"

    def test_names_submit_step(self):
        from selenium.common import JavascriptException
        composer = MagicMock(); composer.text = ""
        driver = MagicMock(); driver.execute_script.side_effect = [None, JavascriptException("boom")]
        ok, lw = self._run(composer, driver=driver)
        assert ok is False
        assert lw.call_args.args[0] == "Inline comment post failed at submit composer"

    def test_step_messages_are_distinct_dedup_keys(self):
        # The escalation dedup key masks quoted strings and numbers, so a quoted/numbered step name
        # would re-merge these into one issue and hide every mode but the loudest.
        from cqc_lem.utilities.log_escalation import normalize_message
        keys = {normalize_message(f"Inline comment post failed at {s}")[1]
                for s in ("prepare text", "open composer", "find composer", "focus composer",
                          "type comment", "submit composer", "verify submit")}
        assert len(keys) == 7


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
        # None, not False: already-reacted is a no-op, and reporting it as a failure made a benign
        # skip indistinguishable from a broken selector. Still falsy, so truthiness callers are safe.
        assert ok is None
        assert not ok
        cpr.assert_not_called()      # no AI spend when we've already reacted
        cf.assert_not_called()       # and we never open the menu

    def test_false_when_menu_wont_open(self):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=_state("Reaction button state: no reaction")), \
             patch(f"{_RA}.click_first", return_value=None):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False  # fly-out never opened and the default-Like fallback didn't register

    def test_missing_menu_is_not_a_warning_when_a_fallback_toggle_exists(self):
        """The fly-out opener is optional — with a React toggle in hand its absence just takes the
        default-Like fallback, which is working behaviour. Warning per card escalated it to ERROR
        and filed a PostHog defect (issue #873)."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=_state("Reaction button state: no reaction")), \
             patch(f"{_RA}.click_first", return_value=None) as cf:
            ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert cf.call_args_list[0].kwargs["warn_on_miss"] is False

    def test_missing_menu_still_warns_when_there_is_no_fallback(self):
        """No Reaction-state button and no React toggle means the card's reaction controls are
        genuinely unreadable — silencing that would hide real SDUI rot."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=None), \
             patch(f"{_RA}.click_first", return_value=None) as cf:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False
        assert cf.call_args_list[0].kwargs["warn_on_miss"] is True

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
