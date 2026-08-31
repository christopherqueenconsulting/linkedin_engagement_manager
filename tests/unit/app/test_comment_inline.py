"""Unit tests for the SDUI inline comment submit + verification fixes."""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.env_constants import MAX_WAIT_RETRY

pytestmark = pytest.mark.unit

_FEED = "cqc_lem.app.engagement.feed"


def _ctx(**kwargs):
    """A `FeedRunContext` with every collaborator mocked (issue #1220)."""
    from cqc_lem.domain.models import FeedRunContext
    kwargs.setdefault("prefs", {})
    return FeedRunContext(driver=MagicMock(), wait=MagicMock(), my_profile=MagicMock(),
                          user_id=1, profile_synthesis="synth", **kwargs)



@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_FEED}.time.sleep"):
        yield


class TestStripNonBmp:
    def test_drops_emoji_keeps_text(self):
        from cqc_lem.utilities.linkedin_formatter import strip_non_bmp as _strip_non_bmp
        assert _strip_non_bmp("Hooked and not even sorry 😄 nice") == "Hooked and not even sorry  nice"

    def test_plain_ascii_unchanged(self):
        from cqc_lem.utilities.linkedin_formatter import strip_non_bmp as _strip_non_bmp
        assert _strip_non_bmp("plain ascii, nothing fancy here") == "plain ascii, nothing fancy here"

    def test_normalizes_rogue_typography(self):
        # Em dashes / smart quotes are AI tell-tale signs — normalized to plain ASCII before typing.
        from cqc_lem.utilities.linkedin_formatter import strip_non_bmp as _strip_non_bmp
        assert _strip_non_bmp("clean copy—no em dashes and “no” smart quotes") == \
            "clean copy - no em dashes and \"no\" smart quotes"


class TestComposerSubmitted:
    def test_true_when_composer_cleared(self):
        from cqc_lem.utilities.linkedin.composer import _composer_submitted
        composer = MagicMock(); composer.text = ""
        assert _composer_submitted(MagicMock(), composer, "some comment") is True

    def test_true_when_composer_detached(self):
        from cqc_lem.utilities.linkedin.composer import _composer_submitted
        composer = MagicMock(); type(composer).text = property(lambda self: (_ for _ in ()).throw(Exception("stale")))
        assert _composer_submitted(MagicMock(), composer, "x") is True

    def test_false_when_full_and_not_in_list(self):
        from cqc_lem.utilities.linkedin.composer import _composer_submitted
        composer = MagicMock(); composer.text = "still typing this"
        driver = MagicMock(); driver.execute_script.return_value = False
        assert _composer_submitted(driver, composer, "still typing this") is False

    def test_true_when_full_but_in_list(self):
        from cqc_lem.utilities.linkedin.composer import _composer_submitted
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
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock(); composer = MagicMock()
        ra._focus_composer(driver, composer)
        scripts = [c.args[0] for c in driver.execute_script.call_args_list]
        assert any("scrollIntoView({block:'center'})" in s for s in scripts)
        composer.click.assert_called_once()

    def test_recenters_and_retries_once_on_interception(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock(); composer = MagicMock()
        composer.click.side_effect = [_intercepted(), None]
        ra._focus_composer(driver, composer)
        assert composer.click.call_count == 2
        assert len([c for c in driver.execute_script.call_args_list
                    if "scrollIntoView" in c.args[0]]) == 2

    def test_second_interception_raises(self):
        # A real overlay must NOT be papered over with a JS click — let it raise so the caller can
        # name the step and the fault stays visible.
        from selenium.common import ElementClickInterceptedException

        from cqc_lem.app.engagement import feed as ra
        composer = MagicMock(); composer.click.side_effect = _intercepted()
        with pytest.raises(ElementClickInterceptedException):
            ra._focus_composer(MagicMock(), composer)

    def test_survives_unscrollable_element(self):
        from cqc_lem.app.engagement import feed as ra
        driver = MagicMock(); driver.execute_script.side_effect = Exception("no such element")
        composer = MagicMock()
        ra._focus_composer(driver, composer)  # positioning is best-effort
        composer.click.assert_called_once()


class TestPostCommentInline:
    def test_posts_and_confirms(self):
        from cqc_lem.app.engagement import feed as ra
        composer = MagicMock(); composer.text = ""       # cleared after real submit
        driver = MagicMock(); driver.execute_script.return_value = True  # submit button clicked
        with patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}._post_composer_for_card", return_value=composer):
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(), "Great post, thanks", user_id=1)
        assert ok is True

    def test_emoji_only_comment_skipped(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.click_first") as cf:
            ok = ra.post_comment_inline(MagicMock(), MagicMock(), MagicMock(), "😄😄😄", user_id=1)
        assert ok is False
        cf.assert_not_called()  # nothing left to type after stripping

    def test_false_when_no_composer(self):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}._post_composer_for_card", return_value=None):
            ok = ra.post_comment_inline(MagicMock(), MagicMock(), MagicMock(), "hello", user_id=1)
        assert ok is False

    def test_only_bmp_text_reaches_send_keys(self):
        # ChromeDriver's send_keys raises on non-BMP characters — the emoji must be gone by then.
        from cqc_lem.app.engagement import feed as ra
        composer = MagicMock(); composer.text = ""
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}._post_composer_for_card", return_value=composer):
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(),
                                        "Shipped it 🚀 and the numbers held 📈", user_id=1)
        assert ok is True
        typed = composer.send_keys.call_args_list[0].args[0]
        assert all(ord(c) <= 0xFFFF for c in typed)
        assert "Shipped it" in typed and "🚀" not in typed

    def test_click_interception_survived_by_recentering(self):
        # The live #815 failure: first click stolen by the sticky nav, the retry lands.
        from cqc_lem.app.engagement import feed as ra
        composer = MagicMock(); composer.text = ""
        composer.click.side_effect = [_intercepted(), None]
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}._post_composer_for_card", return_value=composer), \
             patch(f"{_FEED}.log_warning") as lw:
            ok = ra.post_comment_inline(driver, MagicMock(), MagicMock(), "Great post", user_id=1)
        assert ok is True
        lw.assert_not_called()
        composer.send_keys.assert_called_once()


def _textbox_source(*elements):
    """find_elements stand-in that answers the composer locators and nothing else."""
    return lambda by, value: list(elements) if "textbox" in value else []


def _box(y: int, aria: str = "", height: int = 40):
    """A rendered role=textbox at page-y `y`."""
    el = MagicMock()
    el.rect = {"x": 0, "y": y, "width": 600, "height": height}
    el.get_attribute.return_value = aria
    el.text = ""
    return el


def _holder(*boxes, y: int = 100, height: int = 300):
    """A rendered element whose role=textbox lookup answers with `boxes`."""
    el = MagicMock()
    el.rect = {"x": 0, "y": y, "width": 600, "height": height}
    el.find_elements.side_effect = _textbox_source(*boxes)
    return el


def _driver(scope=None, page_boxes=()):
    """A driver whose only scripted answer is the single-post scope widening; everything else
    (scrollIntoView, the submit button, the submitted check) succeeds. `page_boxes` stands in for
    every rendered role=textbox on the PAGE (issue #1777's page-wide fallback), independent of
    whatever `scope` holds.
    """
    d = MagicMock()
    d.execute_script.side_effect = lambda script, *a: scope if "MARKERS" in script else True
    d.find_elements.side_effect = _textbox_source(*page_boxes)
    return d


class TestComposerIsScopedToItsOwnPost:
    """Issue #876. The feed walk comments on several posts WITHOUT reloading the page and LinkedIn
    leaves each composer mounted after it submits, so a document-wide role=textbox lookup returned
    the first one in DOM order — an earlier post's composer, by then scrolled off the top. That is
    the click Chrome reported intercepted at y=9, and centering it (#815) would not have fixed the
    run: it would have typed this post's comment into the previous post.

    #916 widens the search from the card to the scope that still holds exactly ONE post, which is
    the invariant that actually matters — it must stay impossible to borrow a neighbour's box.
    """

    def test_types_into_this_cards_composer_not_an_earlier_posts(self):
        from cqc_lem.app.engagement import feed as ra
        earlier = _box(10)                          # from the post we just did, scrolled above
        mine = _box(300, aria="Text editor for creating comment")
        card = _holder(mine, y=100)
        with patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ok = ra.post_comment_inline(_driver(scope=_holder(earlier, mine)), MagicMock(), card,
                                        "Great post, thanks", user_id=1)
        assert ok is True
        mine.send_keys.assert_called_once()
        earlier.send_keys.assert_not_called()

    def test_card_without_a_composer_skips_instead_of_borrowing_one(self):
        # Nothing in the card, no wider single-post scope, and nothing on the PAGE either (issue
        # #1777's fallback is position-bounded, not blind — an empty page still means skip, and the
        # caller releases the claim so a later run retries this post).
        from cqc_lem.app.engagement import feed as ra
        earlier = _box(10)
        card = _holder(y=100)                       # nothing inside, and no wider single-post scope
        with patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ok = ra.post_comment_inline(_driver(scope=None), MagicMock(), card,
                                        "Great post, thanks", user_id=1)
        assert ok is False
        earlier.send_keys.assert_not_called()

    def test_a_composer_above_the_card_is_rejected_outright(self):
        # The widened scope can hold the share box or a composer left open on a post above this one.
        # Both start above the card, and neither is ours.
        from cqc_lem.app.engagement import feed as ra
        above = _box(20)
        card = _holder(y=400)
        assert ra._post_composer_for_card(_driver(scope=_holder(above, y=0)), card, user_id=1) is None

    def test_widening_stops_at_the_scope_that_still_holds_one_post(self):
        # The guarantee lives in the JS: the walk up keeps an ancestor ONLY while every per-post
        # marker count still equals the card's, so the scope can never span two posts.
        from cqc_lem.app.engagement import feed as ra
        assert "counts(el) !== base" in ra._SINGLE_POST_SCOPE_JS
        assert "break" in ra._SINGLE_POST_SCOPE_JS
        assert ra._FEED_POST_TEXT_SEL in ra._POST_MARKER_SELECTORS  # the feed's own post marker

    def test_the_widening_bound_is_not_a_comment_action_count(self):
        """The composer we widen to FIND brings its own submit button, and its text is literally
        "Comment" (`_SUBMIT_NEAR_COMPOSER_JS` clicks exactly that, skipping the disabled/hidden ones
        it expects to exist). Counting comment actions therefore sees TWO on the first ancestor that
        holds the card AND the sibling comment section — the walk would break before it ever widened,
        in exactly the render this was written for, and the DEBUG downgrade would hide that forever.
        """
        from cqc_lem.app.engagement import feed as ra
        assert "isCommentAction" not in ra._SINGLE_POST_SCOPE_JS
        assert "const base = counts(scope)" in ra._SINGLE_POST_SCOPE_JS  # baseline, never a hard 1


class TestPostComposerResolution:
    """Issue #916. `_card_for_textbox` returns the NEAREST ancestor carrying the comment action, and
    where LinkedIn renders the comment section beside that node instead of inside it the card-scoped
    lookup missed on every post — 408 misses in 18h, every one on a group feed, each one a
    `log_warning` that escalated to ERROR and filed a defect for a post we simply skip.
    """

    def test_a_composer_nested_in_the_card_still_wins(self):
        from cqc_lem.app.engagement import feed as ra
        mine = _box(300)
        driver = _driver(scope=_holder(_box(10), mine))
        assert ra._post_composer_for_card(driver, _holder(mine, y=100), user_id=1) is mine
        # The card had one, so we never even asked for a wider scope.
        assert not [c for c in driver.execute_script.call_args_list if "MARKERS" in c.args[0]]

    def test_finds_the_composer_mounted_beside_the_card(self):
        from cqc_lem.app.engagement import feed as ra
        outside = _box(420, aria="Text editor for creating comment")
        card = _holder(y=100, height=300)
        assert ra._post_composer_for_card(_driver(scope=_holder(outside, y=90)), card,
                                          user_id=1) is outside

    def test_prefers_the_posts_own_box_over_a_reply_box(self):
        # A reply box under an existing comment is a role=textbox too; typing here would answer a
        # stranger instead of the author, so the labelled box wins even when it is further away.
        from cqc_lem.app.engagement import feed as ra
        reply = _box(700)
        mine = _box(420, aria="Text editor for creating comment")
        card = _holder(y=100, height=300)
        assert ra._post_composer_for_card(_driver(scope=_holder(mine, reply, y=90)), card,
                                          user_id=1) is mine

    def test_finds_the_composer_when_the_widened_scope_never_reaches_it(self):
        """Issue #1777: a composer that mounts as a SIBLING of the widened scope, not inside it.

        A live grounding run found this: a reshare's embedded original post carries its own
        per-post marker, so `_single_post_scope`'s ancestor walk stops one level short of the
        composer no matter how far it climbs. The page-wide, position-bounded fallback
        `_reply_composer_for_comment` already uses for its own sibling-render case is the answer:
        never above this post, nearest to its bottom edge wins.
        """
        from cqc_lem.app.engagement import feed as ra
        card = _holder(y=100, height=300)                 # nothing inside
        scope = _holder(y=100, height=300)                 # widened, but still holds nothing
        mine = _box(430, aria="Text editor for creating comment")   # mounted elsewhere on the page
        driver = _driver(scope=scope, page_boxes=(mine,))
        assert ra._post_composer_for_card(driver, card, user_id=1) is mine

    def test_the_page_wide_fallback_still_rejects_a_composer_above_the_post(self):
        # Same #876 rule the widened-scope path already enforces: a box above this post is an
        # earlier post's leftover composer (or the share box), never borrowed.
        from cqc_lem.app.engagement import feed as ra
        card = _holder(y=400, height=300)
        stale = _box(20, aria="Text editor for creating comment")  # left mounted on an earlier post
        driver = _driver(scope=_holder(y=400, height=300), page_boxes=(stale,))
        assert ra._post_composer_for_card(driver, card, user_id=1) is None

    def test_a_miss_is_debug_not_a_warning(self):
        """The whole point of the issue: no composer means skip the post — an expected no-op the
        caller already handles by releasing the claim. Warning it per card filed a defect.
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.log_warning") as warn, patch(f"{_FEED}.log_debug") as debug:
            assert ra._post_composer_for_card(_driver(scope=None), _holder(y=100), user_id=1) is None
        warn.assert_not_called()
        assert debug.call_count == 1
        assert debug.call_args.args[0] == "No comment composer opened on this post card"

    def test_a_stale_card_gives_up_immediately(self):
        from cqc_lem.app.engagement import feed as ra
        card = MagicMock()
        type(card).rect = property(lambda self: (_ for _ in ()).throw(Exception("stale")))
        with patch(f"{_FEED}.log_warning") as warn, patch(f"{_FEED}.log_debug") as debug:
            assert ra._post_composer_for_card(_driver(), card, user_id=1) is None
        warn.assert_not_called()
        assert debug.call_count == 1  # one message, not one per poll
        assert debug.call_args.args[0].startswith("Post card is not rendered")

    def test_a_slow_render_is_polled_for_not_waited_out(self):
        """The old chain spent WAIT_DEFAULT_TIMEOUT x (MAX_WAIT_RETRY + 1) — ~35s — on every card
        that never opened one. A composer that mounts late is still caught.
        """
        from cqc_lem.app.engagement import feed as ra
        late = _box(420, aria="Text editor for creating comment")
        card = MagicMock()
        card.rect = {"x": 0, "y": 100, "width": 600, "height": 300}
        answers = [[], [], [late]]
        card.find_elements.side_effect = lambda by, value: answers.pop(0) if answers else [late]
        assert ra._post_composer_for_card(_driver(scope=None), card, user_id=1) is late
        assert ra._COMPOSER_MOUNT_POLLS * ra._COMPOSER_MOUNT_POLL_SECONDS < 15  # WAIT_DEFAULT_TIMEOUT

    def test_the_resolver_is_the_one_lookup_the_call_site_uses(self):
        from cqc_lem.app.engagement import feed as ra
        composer = MagicMock(); composer.text = ""
        card = MagicMock()
        driver = MagicMock(); driver.execute_script.return_value = True
        with patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}._post_composer_for_card", return_value=composer) as res, \
             patch(f"{_FEED}.find_first") as ff:
            ra.post_comment_inline(driver, MagicMock(), card, "Great post, thanks", user_id=1)
        assert res.call_args.args[1] is card
        ff.assert_not_called()  # no second, unscoped composer lookup anywhere in the path


class TestPostCommentInlineStepNaming:
    """One `try` over the whole sequence reported every failure mode as the same warning, so the
    escalated issue never said which step broke — and unrelated faults collapsed into one.
    """

    def _run(self, composer, driver=None):
        from cqc_lem.app.engagement import feed as ra
        driver = driver or MagicMock()
        with patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}._post_composer_for_card", return_value=composer), \
             patch(f"{_FEED}.log_warning") as lw:
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


def _button(label="", text="", testid="", displayed=True):
    el = MagicMock()
    el.is_displayed.return_value = displayed
    el.text = text
    el.get_attribute.side_effect = lambda name: {"aria-label": label, "data-testid": testid}.get(name)
    return el


_ANY_BUTTON = "button, [role='button']"


def _card_with(matches=None, buttons=None):
    """A card whose find_elements answers per selector — anything unlisted matches nothing."""
    card = MagicMock()

    def _find(by, sel):
        if sel == _ANY_BUTTON:
            return list(buttons or [])
        return list((matches or {}).get(sel, []))

    card.find_elements.side_effect = _find
    return card


class TestCardHasReactionAffordance:
    """The probe deciding whether a 'Reaction state' miss is rot or a card that simply can't react."""

    def test_a_popup_menu_button_is_not_a_reaction_affordance(self):
        """`.//button[@aria-haspopup]` matches the card's '…' control menu and comment sort control.

        Counting it would answer True on nearly every card, which silently re-opens #874 — the
        probe must key on controls that name the REACTION entity (the #1012 rail hazard).
        """
        from cqc_lem.app.engagement import feed as ra
        card = _card_with(matches={".//button[@aria-haspopup]": [_button(label="Open control menu")]},
                          buttons=[_button(label="Open control menu"), _button(label="Comment")])
        assert ra._card_has_reaction_affordance(card, user_id=1) is False

    def test_a_hidden_reaction_button_alone_is_not_an_affordance(self):
        from cqc_lem.app.engagement import feed as ra
        card = _card_with(buttons=[_button(label="React Like", displayed=False)])
        assert ra._card_has_reaction_affordance(card, user_id=1) is False

    def test_a_data_testid_reaction_control_counts(self):
        from cqc_lem.app.engagement import feed as ra
        card = _card_with(buttons=[_button(testid="social-actions-reaction-button")])
        assert ra._card_has_reaction_affordance(card, user_id=1) is True

    def test_a_stale_element_does_not_end_the_scan(self):
        """The feed re-renders under us; one detached node must not decide the whole card."""
        from selenium.common import StaleElementReferenceException

        from cqc_lem.app.engagement import feed as ra
        stale = MagicMock()
        stale.is_displayed.side_effect = StaleElementReferenceException("gone")
        stale_button = MagicMock()
        stale_button.is_displayed.side_effect = StaleElementReferenceException("gone")
        card = _card_with(
            matches={"button[aria-label^='Reaction button state']": [stale]},
            buttons=[stale_button, _button(text="Like")],
        )
        assert ra._card_has_reaction_affordance(card, user_id=1) is True

    def test_an_unreachable_card_probes_false_at_debug(self):
        """A card ripped out of the DOM can't be reacted to either — and that is not a warning."""
        from selenium.common import WebDriverException

        from cqc_lem.app.engagement import feed as ra
        card = MagicMock()
        card.find_elements.side_effect = WebDriverException("card detached")
        with patch(f"{_FEED}.log_debug") as ld, patch(f"{_FEED}.log_warning") as lw:
            assert ra._card_has_reaction_affordance(card, user_id=1) is False
        lw.assert_not_called()
        assert ld.call_args.args[0] == "Could not probe card for reaction affordance"


class TestReactToPostInline:
    @pytest.fixture
    def card(self):
        card = MagicMock()
        # Default card carries a visible reaction trigger, so the existing reaction-path tests
        # still exercise the full chain.
        card.find_elements.return_value = [_state("Reaction button state: no reaction")]
        return card

    def test_reacts_when_not_yet_reacted(self, card):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: Like reaction")]), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card,
                                         post_content="p", comment_text="c", user_id=1)
        assert ok is True

    def test_skips_when_already_reacted(self, card):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction") as cpr, \
             patch(f"{_FEED}.find_first", return_value=_state("Reaction button state: Celebrate reaction")), \
             patch(f"{_FEED}.click_first") as cf:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        # None, not False: already-reacted is a no-op, and reporting it as a failure made a benign
        # skip indistinguishable from a broken selector. Still falsy, so truthiness callers are safe.
        assert ok is None
        assert not ok
        cpr.assert_not_called()      # no AI spend when we've already reacted
        cf.assert_not_called()       # and we never open the menu

    def test_false_when_menu_wont_open(self, card):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=_state("Reaction button state: no reaction")), \
             patch(f"{_FEED}.click_first", return_value=None):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False  # fly-out never opened and the default-Like fallback didn't register

    def test_missing_menu_is_not_a_warning_when_a_fallback_toggle_exists(self, card):
        """The fly-out opener is optional — with a React toggle in hand its absence just takes the
        default-Like fallback, which is working behaviour. Warning per card escalated it to ERROR
        and filed a PostHog defect (issue #873).
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=_state("Reaction button state: no reaction")), \
             patch(f"{_FEED}.click_first", return_value=None) as cf:
            ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert cf.call_args_list[0].kwargs["warn_on_miss"] is False

    def test_unreadable_reaction_controls_warn_once_at_the_trigger(self, card):
        """A total trigger miss on a card that DOES carry reaction controls means those controls
        are genuinely unreadable, and that must still warn — silencing it would hide real SDUI rot.

        It warns WHERE IT IS DETECTED (the trigger chain), not at the opener. Pre-#816 the signal
        rode on the opener's `warn_on_miss=trigger is None`; the opener no longer exists on the
        live SDUI (count: 0), so hanging the one real signal off a control that is always absent
        would either warn on every card or never warn at all.
        """
        from cqc_lem.app.engagement import feed as ra
        card = MagicMock()
        card.find_elements.return_value = [_state("Reaction button state: no reaction")]
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=None) as ff, \
             patch(f"{_FEED}.click_first", return_value=None) as cf:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False
        trigger = [c for c in ff.call_args_list if c.args[3] == "Reaction state"]
        assert len(trigger) == 1
        # find_first warns on a miss by default; the trigger lookup must NOT opt out of it.
        assert trigger[0].kwargs.get("warn_on_miss", True) is True
        # ...and nothing downstream warns again for the same one condition (#877/#878).
        assert all(c.kwargs.get("warn_on_miss") is False for c in cf.call_args_list)

    def test_no_warning_when_card_has_no_reaction_affordance(self):
        """A card with no reaction affordance is working behaviour, not an unreadable control.

        The #899 live run found 9 post-text nodes but only 8 reaction triggers: at least one
        normal feed card type carries post text and no reaction affordance. A selector miss there
        is working behaviour, so it stays DEBUG and must not file a RecurringWarning (issue #874).
        """
        from cqc_lem.app.engagement import feed as ra
        card = MagicMock()
        card.find_elements.return_value = []
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first") as ff, \
             patch(f"{_FEED}.click_first") as cf, \
             patch(f"{_FEED}.log_debug") as ld:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False
        # We should skip before spending a reaction attempt or AI call.
        ff.assert_not_called()
        cf.assert_not_called()
        ld.assert_called_once_with("Card has no reaction affordance — skipping inline reaction",
                                   user_id=1, action_type="comment")

    def test_a_like_labelled_button_counts_as_affordance_and_keeps_the_warning(self):
        """No locator in the chains matches, but the card ships a visible 'Like' control.

        That card CAN be reacted to, so an unreadable state button there is real SDUI rot and must
        still warn — the token scan is what keeps issue #874's silencing narrow.
        """
        from cqc_lem.app.engagement import feed as ra
        card = _card_with(buttons=[_button(label="React Like")])
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=None) as ff, \
             patch(f"{_FEED}.click_first", return_value=None):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False
        trigger = [c for c in ff.call_args_list if c.args[3] == "Reaction state"]
        assert trigger[0].kwargs.get("warn_on_miss", True) is True

    def test_the_obsolete_opener_never_warns(self, card):
        """'Open reactions menu' matched ZERO elements on the live feed — hovering the trigger is
        what opens the fly-out now. Its absence is the documented happy path, and warning on the
        happy path is exactly the expected-no-op the recurrence rule turns into a filed defect.
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=None), \
             patch(f"{_FEED}.click_first", return_value=None) as cf:
            ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        opener = [c for c in cf.call_args_list if c.args[3] == "Open reactions menu"]
        assert len(opener) == 1
        assert opener[0].kwargs["warn_on_miss"] is False

    def test_there_is_no_second_toggle_lookup(self, card):
        """One chain now serves state AND toggle (the state button's text is literally 'Like'), so
        the separate 'React toggle' lookup is gone — one control, one lookup, one possible warning.
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=None) as ff, \
             patch(f"{_FEED}.click_first", return_value=None):
            ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert [c for c in ff.call_args_list if c.args[3] == "React toggle"] == []

    def test_react_toggle_is_not_looked_up_when_the_state_button_is_the_trigger(self, card):
        """A readable 'no reaction' state button IS the trigger, so the toggle chain never runs and
        can't miss — the warning this issue is about only ever fires on state-less cards.
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first",
                   side_effect=[_state("Reaction button state: no reaction"),
                                _state("Reaction button state: Like reaction")]) as ff, \
             patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert not [c for c in ff.call_args_list if c.args[3] == "React toggle"]

    def test_post_click_confirm_is_not_a_warning_when_the_card_never_had_the_toggle(self, card):
        """With no Reaction-state button before the click there is nothing to re-read after it, so
        the miss is the documented trust-the-click fallback. Warning per card escalated it to ERROR
        and filed a PostHog defect for working behaviour (issue #875).
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=None) as ff, \
             patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is True  # unreadable toggle never false-negatives a click that landed
        confirm = [c for c in ff.call_args_list if c.args[3] == "Reaction state (post-click)"]
        assert len(confirm) == 1
        assert confirm[0].kwargs["warn_on_miss"] is False
        assert confirm[0].kwargs["max_try"] == 1  # no retry sleep for a control this card lacks

    def test_post_click_confirm_still_warns_when_the_toggle_was_readable_before(self, card):
        """It was there before the click and isn't after — that IS selector rot, keep the signal."""
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first",
                   side_effect=[_state("Reaction button state: no reaction"), None]) as ff, \
             patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is True
        confirm = [c for c in ff.call_args_list if c.args[3] == "Reaction state (post-click)"]
        assert confirm[0].kwargs["warn_on_miss"] is True
        assert confirm[0].kwargs["max_try"] == MAX_WAIT_RETRY

    def test_clicks_the_ai_chosen_reaction(self, card):
        from cqc_lem.app.engagement import feed as ra
        seen = []

        def _capture(driver, wait, locators, label, **kw):
            seen.append(locators)
            return MagicMock()

        with patch(f"{_FEED}.choose_post_reaction", return_value="Support"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: Support reaction")]), \
             patch(f"{_FEED}.click_first", side_effect=_capture):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card,
                                         post_content="p", comment_text="c", user_id=1)
        assert ok is True
        # 2nd click_first is the reaction click; its primary locator targets aria-label='Support'
        assert any("aria-label='Support'" in loc[1] for loc in seen[1])

    def test_false_when_reaction_did_not_register(self, card):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: no reaction")]), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()):
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False  # toggle never flipped away from 'no reaction'

    def test_a_click_that_never_registered_warns_exactly_once(self, card):
        """Readable controls, a click that didn't take: the one reaction failure none of the
        selector misses stand for. It warns HERE, where it's detected, so the caller doesn't have
        to warn blindly for every False (issue #878).
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", side_effect=[_state("Reaction button state: no reaction"),
                                                     _state("Reaction button state: no reaction")]), \
             patch(f"{_FEED}.click_first", return_value=MagicMock()), \
             patch(f"{_FEED}.log_warning") as warn:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False
        assert len(warn.call_args_list) == 1
        assert warn.call_args_list[0].args[0] == "Reaction did not register after clicking"

    def test_an_unreadable_card_adds_no_warning_of_its_own(self, card):
        """No Reaction-state button and no React toggle: the fly-out opener's miss already warns for
        that condition (issue #873), so nothing in this function may warn a second time.
        """
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.choose_post_reaction", return_value="Like"), \
             patch(f"{_FEED}.wait_for_ajax"), \
             patch(f"{_FEED}.find_first", return_value=None), \
             patch(f"{_FEED}.click_first", return_value=None), \
             patch(f"{_FEED}.log_warning") as warn:
            ok = ra.react_to_post_inline(MagicMock(), MagicMock(), card, user_id=1)
        assert ok is False
        warn.assert_not_called()


class TestEngageCardReactionLogging:
    """`_engage_card`'s reaction outcome must never be the thing that files a defect: a reaction is
    best-effort and never blocks the comment, and every real failure already warned inside
    `react_to_post_inline` (issue #878).
    """

    @staticmethod
    def _engage(reaction_outcome, warn, debug):
        from cqc_lem.app.engagement import feed as ra
        with patch(f"{_FEED}.claim_post_for_comment", return_value=True), \
             patch(f"{_FEED}.select_blueprint", return_value={"format": "expander"}), \
             patch(f"{_FEED}.generate_ai_response", return_value="A real comment."), \
             patch(f"{_FEED}._author_is_me", return_value=False), \
             patch(f"{_FEED}.INLINE_REACTIONS_ENABLED", True), \
             patch(f"{_FEED}.react_to_post_inline", return_value=reaction_outcome), \
             patch(f"{_FEED}.mark_post_reacted"), \
             patch(f"{_FEED}.post_comment_inline", return_value=True), \
             patch(f"{_FEED}.mark_post_commented"), \
             patch(f"{_FEED}.insert_new_log"), \
             patch(f"{_FEED}.record_action"), \
             patch(f"{_FEED}.pace_read", return_value=0.0), \
             patch(f"{_FEED}.log_warning", warn), \
             patch(f"{_FEED}.log_debug", debug):
            return ra._engage_card(_ctx(), MagicMock(), "feedurn://x", "a post body", "Jane")

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
