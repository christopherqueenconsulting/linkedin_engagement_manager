"""Unit tests for the SDUI inline comment submit + verification fixes."""

import pytest
from unittest.mock import MagicMock, patch

from cqc_lem.utilities.env_constants import MAX_WAIT_RETRY

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


def _wait():
    """Minimal WebDriverWait stand-in so `find_first` can run for real: poll once, TimeoutException
    on a falsy result (which is what a total selector miss looks like to it)."""
    from selenium.common import TimeoutException
    w = MagicMock()

    def until(fn, label=None):
        found = fn(None)
        if not found:
            raise TimeoutException(label)
        return found

    w.until.side_effect = until
    return w


def _textbox_source(*elements):
    """find_elements stand-in that answers the composer locators and nothing else."""
    return lambda by, value: list(elements) if "textbox" in value else []


class TestComposerIsScopedToTheCard:
    """Issue #876. The feed walk comments on several posts WITHOUT reloading the page and LinkedIn
    leaves each composer mounted after it submits, so a document-wide role=textbox lookup returned
    the first one in DOM order — an earlier post's composer, by then scrolled off the top. That is
    the click Chrome reported intercepted at y=9, and centering it (#815) would not have fixed the
    run: it would have typed this post's comment into the previous post."""

    def test_types_into_this_cards_composer_not_an_earlier_posts(self):
        from cqc_lem.app import run_automation as ra
        earlier = MagicMock()                 # first in document order, from the post we just did
        mine = MagicMock(); mine.text = ""    # this card's own composer
        driver = MagicMock()
        driver.find_elements.side_effect = _textbox_source(earlier, mine)
        driver.execute_script.return_value = True
        card = MagicMock()
        card.find_elements.side_effect = _textbox_source(mine)
        with patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra.post_comment_inline(driver, _wait(), card, "Great post, thanks", user_id=1)
        assert ok is True
        mine.send_keys.assert_called_once()
        earlier.send_keys.assert_not_called()
        earlier.click.assert_not_called()

    def test_card_without_a_composer_skips_instead_of_borrowing_one(self):
        # No document-wide fallback on purpose — commenting on the wrong post is worse than not
        # commenting, and the caller releases the claim so a later run retries this post.
        from cqc_lem.app import run_automation as ra
        earlier = MagicMock()
        driver = MagicMock()
        driver.find_elements.side_effect = _textbox_source(earlier)
        card = MagicMock(); card.find_elements.return_value = []
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch("cqc_lem.utilities.selenium_util.time.sleep"), \
             patch("cqc_lem.utilities.selenium_util.log_warning"):
            ok = ra.post_comment_inline(driver, _wait(), card, "Great post, thanks", user_id=1)
        assert ok is False
        earlier.send_keys.assert_not_called()

    def test_lookup_is_scoped_to_the_card(self):
        from cqc_lem.app import run_automation as ra
        composer = MagicMock(); composer.text = ""
        card = MagicMock()
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.find_first", return_value=composer) as ff:
            ra.post_comment_inline(driver, MagicMock(), card, "Great post, thanks", user_id=1)
        assert ff.call_args.kwargs["parent_element"] is card


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

    def test_unreadable_reaction_controls_warn_once_at_the_trigger(self):
        """A total trigger miss means the card's reaction controls are genuinely unreadable, and
        that must still warn — silencing it would hide real SDUI rot.

        It warns WHERE IT IS DETECTED (the trigger chain), not at the opener. Pre-#816 the signal
        rode on the opener's `warn_on_miss=trigger is None`; the opener no longer exists on the
        live SDUI (count: 0), so hanging the one real signal off a control that is always absent
        would either warn on every card or never warn at all."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=None) as ff, \
             patch(f"{_RA}.click_first", return_value=None) as cf:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False
        trigger = [c for c in ff.call_args_list if c.args[3] == "Reaction state"]
        assert len(trigger) == 1
        # find_first warns on a miss by default; the trigger lookup must NOT opt out of it.
        assert trigger[0].kwargs.get("warn_on_miss", True) is True
        # ...and nothing downstream warns again for the same one condition (#877/#878).
        assert all(c.kwargs.get("warn_on_miss") is False for c in cf.call_args_list)

    def test_the_obsolete_opener_never_warns(self):
        """'Open reactions menu' matched ZERO elements on the live feed — hovering the trigger is
        what opens the fly-out now. Its absence is the documented happy path, and warning on the
        happy path is exactly the expected-no-op the recurrence rule turns into a filed defect."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=None), \
             patch(f"{_RA}.click_first", return_value=None) as cf:
            ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        opener = [c for c in cf.call_args_list if c.args[3] == "Open reactions menu"]
        assert len(opener) == 1
        assert opener[0].kwargs["warn_on_miss"] is False

    def test_there_is_no_second_toggle_lookup(self):
        """One chain now serves state AND toggle (the state button's text is literally 'Like'), so
        the separate 'React toggle' lookup is gone — one control, one lookup, one possible warning."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=None) as ff, \
             patch(f"{_RA}.click_first", return_value=None):
            ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert [c for c in ff.call_args_list if c.args[3] == "React toggle"] == []

    def test_react_toggle_is_not_looked_up_when_the_state_button_is_the_trigger(self):
        """A readable 'no reaction' state button IS the trigger, so the toggle chain never runs and
        can't miss — the warning this issue is about only ever fires on state-less cards."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first",
                   side_effect=[_state("Reaction button state: no reaction"),
                                _state("Reaction button state: Like reaction")]) as ff, \
             patch(f"{_RA}.click_first", return_value=MagicMock()):
            ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert not [c for c in ff.call_args_list if c.args[3] == "React toggle"]

    def test_post_click_confirm_is_not_a_warning_when_the_card_never_had_the_toggle(self):
        """With no Reaction-state button before the click there is nothing to re-read after it, so
        the miss is the documented trust-the-click fallback. Warning per card escalated it to ERROR
        and filed a PostHog defect for working behaviour (issue #875)."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=None) as ff, \
             patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is True  # unreadable toggle never false-negatives a click that landed
        confirm = [c for c in ff.call_args_list if c.args[3] == "Reaction state (post-click)"]
        assert len(confirm) == 1
        assert confirm[0].kwargs["warn_on_miss"] is False
        assert confirm[0].kwargs["max_try"] == 1  # no retry sleep for a control this card lacks

    def test_post_click_confirm_still_warns_when_the_toggle_was_readable_before(self):
        """It was there before the click and isn't after — that IS selector rot, keep the signal."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first",
                   side_effect=[_state("Reaction button state: no reaction"), None]) as ff, \
             patch(f"{_RA}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is True
        confirm = [c for c in ff.call_args_list if c.args[3] == "Reaction state (post-click)"]
        assert confirm[0].kwargs["warn_on_miss"] is True
        assert confirm[0].kwargs["max_try"] == MAX_WAIT_RETRY

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

    def test_a_click_that_never_registered_warns_exactly_once(self):
        """Readable controls, a click that didn't take: the one reaction failure none of the
        selector misses stand for. It warns HERE, where it's detected, so the caller doesn't have
        to warn blindly for every False (issue #878)."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: no reaction")]), \
             patch(f"{_RA}.click_first", return_value=MagicMock()), \
             patch(f"{_RA}.log_warning") as warn:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False
        assert len(warn.call_args_list) == 1
        assert warn.call_args_list[0].args[0] == "Reaction did not register after clicking"

    def test_an_unreadable_card_adds_no_warning_of_its_own(self):
        """No Reaction-state button and no React toggle: the fly-out opener's miss already warns for
        that condition (issue #873), so nothing in this function may warn a second time."""
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.choose_post_reaction", return_value="Like"), \
             patch(f"{_RA}.wait_for_ajax"), \
             patch(f"{_RA}.find_first", return_value=None), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}.log_warning") as warn:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), MagicMock(), user_id=1)
        assert ok is False
        warn.assert_not_called()


class TestEngageCardReactionLogging:
    """`_engage_card`'s reaction outcome must never be the thing that files a defect: a reaction is
    best-effort and never blocks the comment, and every real failure already warned inside
    `react_to_post_inline` (issue #878)."""

    @staticmethod
    def _engage(reaction_outcome, warn, debug):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.claim_post_for_comment", return_value=True), \
             patch(f"{_RA}.select_blueprint", return_value={"format": "expander"}), \
             patch(f"{_RA}.generate_ai_response", return_value="A real comment."), \
             patch(f"{_RA}._author_is_me", return_value=False), \
             patch(f"{_RA}.INLINE_REACTIONS_ENABLED", True), \
             patch(f"{_RA}.react_to_post_inline", return_value=reaction_outcome), \
             patch(f"{_RA}.mark_post_reacted"), \
             patch(f"{_RA}.post_comment_inline", return_value=True), \
             patch(f"{_RA}.mark_post_commented"), \
             patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.record_action"), \
             patch(f"{_RA}.pace_read", return_value=0.0), \
             patch(f"{_RA}.log_warning", warn), \
             patch(f"{_RA}.log_debug", debug):
            return ra._engage_card(MagicMock(), MagicMock(), MagicMock(), 1, MagicMock(),
                                   "feedurn://x", "a post body", "Jane", {}, "synth", [], [])

    def test_a_failed_reaction_is_debug_not_a_warning(self):
        warn, debug = MagicMock(), MagicMock()
        assert self._engage(False, warn, debug) is True  # the comment still lands
        warn.assert_not_called()
        assert debug.call_args_list[0].args[0].startswith("No reaction landed on post")

    def test_an_already_reacted_post_is_still_debug(self):
        # None = the post already carried our reaction: a no-op, and mark_post_reacted is not owed.
        warn, debug = MagicMock(), MagicMock()
        assert self._engage(None, warn, debug) is True
        warn.assert_not_called()
        assert debug.call_args_list[0].args[0].startswith("Post already carried our reaction")

    def test_a_landed_reaction_logs_nothing(self):
        warn, debug = MagicMock(), MagicMock()
        assert self._engage(True, warn, debug) is True
        warn.assert_not_called()
        debug.assert_not_called()
