"""Production zero-walk tripwires for the last three SDUI surfaces (issue #1021, follow-up of #1013).

#1013 gave profile-viewers and the catch-up/feed walks a cross-check; three lanes were left where a
zero result is indistinguishable from a healthy quiet day:

* the profile **degree badge** — read through `span.dist-value` / `span.distance-badge`, both class
  anchors and both confirmed dead on 2026-08-03, which fails OPEN into inviting people we are
  already connected to and (through `LinkedInProfile.is_1st_connection`) reads EVERY profile viewer
  as a non-connection;
* the **company-page invite modal** — zero ticked boxes reported as `no_candidates`;
* **own post stats** — every signal scored 0, which is also what a quiet post looks like.

The contract these tests hold is the escalation contract: drift warns, an empty surface and an
unreadable cross-check stay DEBUG (a repeated warning re-emits at ERROR and files a defect, so a
quiet day must never warn).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By

# The profile-viewer branch below runs in `app.engagement.outreach` (#1154); the post-stats sweep
# went to `app.engagement.posting`, so both spellings are live in this file.
_OUT = "cqc_lem.app.engagement.outreach"
_POST = "cqc_lem.app.engagement.posting"
# The connect rail moved to its own module (#1154); patches for it must bind THERE, because that
# is the module whose globals the invite code reads.
_INV = "cqc_lem.app.engagement.invites"
_CPI = "cqc_lem.utilities.linkedin.company_page_inviter"
_ZW = "cqc_lem.utilities.linkedin.zero_walk"

# The SDUI profile top card as it renders today: hashed classes, no `dist-value`, and the degree
# written as its own leaf node. Both halves of the old chain match NOTHING in here on purpose —
# that is exactly the page the shipped locators went blind on.
TOP_CARD_HTML = """
<html><head><title>Jane Doe | LinkedIn</title></head><body>
<main>
  <div class="ph5 pb5">
    <h1 class="AbCdEf">Jane Doe</h1>
    <div class="text-body-medium">Fractional CTO at Acme</div>
    <span class="XyZ123">Los Angeles, California</span>
    <span class="GhIjKl">{degree}</span>
    <a href="/in/jane-doe/recent-activity/all/">Show all activity</a>
  </div>
</main></body></html>
"""


class _Element:
    """The two things the degree read asks of a Selenium element."""

    def __init__(self, node):
        self._node = node

    @property
    def text(self):
        return " ".join(self._node.itertext()).strip()


class _DomDriver:
    """A driver backed by a real parsed document, so the shipped XPath is what gets exercised.

    CSS selectors resolve to nothing (`cssselect` is not a test dependency) — which is the point:
    the class-anchor tail of the chain must not be what makes the read work.
    """

    def __init__(self, html: str):
        import lxml.html
        self.tree = lxml.html.fromstring(html)

    def find_elements(self, by, selector):
        if by != By.XPATH:
            return []
        return [_Element(node) for node in self.tree.xpath(selector)]

    def find_element(self, by, selector):
        if by == By.TAG_NAME:
            nodes = self.tree.xpath(f"//{selector}")
            if not nodes:
                raise RuntimeError("no such element")
            return _Element(nodes[0])
        raise RuntimeError("no such element")


class TestDegreeBadgeIsReadFromWhatThePageWrites:
    def test_the_chain_leads_with_text_anchors_not_class_anchors(self):
        from cqc_lem.app.engagement import invites as ra
        leading = ra._PROFILE_DEGREE_LOCATORS[0][1]
        assert "class" not in leading
        assert "1st" in leading and "2nd" in leading

    def test_a_first_degree_top_card_reads_as_first_degree(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(TOP_CARD_HTML.format(degree="1st"))
        assert ra._profile_is_first_degree(driver) is True

    @pytest.mark.parametrize("degree", ["2nd", "3rd+", "· 2nd", "3rd degree connection"])
    def test_a_non_first_degree_top_card_does_not_block_the_invite(self, degree):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(TOP_CARD_HTML.format(degree=degree))
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()  # the chain SAW a badge — nothing to cross-check

    def test_first_degree_written_out_in_full_still_reads(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(TOP_CARD_HTML.format(degree="1st degree connection"))
        assert ra._profile_is_first_degree(driver) is True

    def test_a_headline_containing_a_degree_token_is_not_a_badge(self):
        r"""`\b1st\b` over the page text would fire here forever — the badge is a WHOLE line."""
        from cqc_lem.app.engagement import invites as ra
        html = TOP_CARD_HTML.format(degree="Winner, 1st place in the 2026 Acme awards")
        driver = _DomDriver(html)
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()
        debug.assert_called_once()


# The live 2026-08-14 grab (#1031: `--profile-scrape` against a 3rd-degree profile, run against the
# deployed build carrying #1025). The badge is a `<p>` leaf — NOT the `<span>` the earlier fixture
# assumed — its classes are hashed, and the top card writes `· 3rd` while the "key signals" block
# below it writes `· 3rd+` and other people's `· 2nd`. Pinned because the tag was the one part of the
# shape the chain was never grounded on.
LIVE_TOP_CARD_HTML = """
<html><head><title>Bill Gates | LinkedIn</title></head><body>
<main>
  <div class="_648bd2fe"><h1 class="_46e0469d">Bill Gates</h1>
    <p class="d3e5c957 _797b549d d820e14d _648bd2fe _46e0469d">{degree}</p></div>
  <section><p class="d3e5c957 b52885fd _81da398e f12aed22">· 3rd+</p>
    <p class="d3e5c957 b52885fd _81da398e f12aed22">· 2nd</p></section>
</main></body></html>
"""


class TestTheLiveBadgeShapeStaysGrounded:
    """#1031's report, turned into a test that fails if the chain drifts off the live shape."""

    def test_the_third_degree_top_card_the_live_probe_read_does_not_block_the_invite(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(LIVE_TOP_CARD_HTML.format(degree="· 3rd"))
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()

    def test_the_top_cards_badge_is_the_one_read_not_the_signals_blocks(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(LIVE_TOP_CARD_HTML.format(degree="· 3rd"))
        assert (ra._degree_badge_texts(driver) or [""])[0] == "· 3rd"

    def test_a_first_degree_badge_in_the_live_shape_still_blocks_the_invite(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(LIVE_TOP_CARD_HTML.format(degree="· 1st"))
        assert ra._profile_is_first_degree(driver) is True


# The 2026-08-31 drift (#1021 tripwire fired): LinkedIn wrapped the badge text beside an icon child,
# so the badge element is no longer childless. The old `not(*)` leaf predicate matched nothing here
# while `<main>` still rendered the degree line — the exact split that fired the zero-walk tripwire
# and fired invites at people we already connect to.
NESTED_BADGE_HTML = """
<html><head><title>Jane Doe | LinkedIn</title></head><body>
<main>
  <div class="_648bd2fe"><h1 class="_46e0469d">Jane Doe</h1>
    <p class="d3e5c957 _797b549d">{degree}<svg aria-hidden="true"><use></use></svg></p></div>
</main></body></html>
"""


class TestABadgeWrappedBesideAnIconStillReads:
    """The 2026-08-31 regression: the badge text no longer sits in a childless node."""

    def test_the_leaf_locator_no_longer_requires_a_childless_node(self):
        from cqc_lem.app.engagement import invites as ra
        assert "not(*)" not in ra._PROFILE_DEGREE_LOCATORS[0][1]

    @pytest.mark.parametrize("degree", ["1st", "· 1st", "1st degree connection"])
    def test_a_first_degree_nested_badge_blocks_the_invite(self, degree):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(NESTED_BADGE_HTML.format(degree=degree))
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is True
        warn.assert_not_called()  # the chain SAW the badge — nothing to cross-check

    @pytest.mark.parametrize("degree", ["· 2nd", "3rd+", "3rd degree connection"])
    def test_a_non_first_degree_nested_badge_does_not_block_the_invite(self, degree):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(NESTED_BADGE_HTML.format(degree=degree))
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()


# A 2nd-degree profile as `<main>` actually renders it: the top card's badge first, then the
# mutual-connection highlight — which carries SOMEBODY ELSE's `1st`. Reading "any badge under
# main" makes this profile look already-connected and cancels the invite (#1012's mistake in a
# read instead of a click).
MUTUALS_HTML = """
<html><head><title>Jane Doe | LinkedIn</title></head><body>
<main>
  <div class="ph5 pb5"><h1>Jane Doe</h1><span class="GhIjKl">{degree}</span></div>
  <section>
    <div><a href="/in/bob-smith/">Bob Smith</a><span>1st</span></div>
    <p>You both know Bob Smith</p>
  </section>
</main></body></html>
"""


class TestTheBadgeMustBelongToThisProfile:
    """Document order is what attributes a badge to the profile — the top card is first."""

    def test_a_mutual_connections_badge_does_not_cancel_the_invite(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(MUTUALS_HTML.format(degree="· 2nd"))
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()

    def test_the_spelled_out_top_card_badge_still_outranks_a_later_bare_one(self):
        """Both text shapes are ONE union locator, so document order decides — not list order."""
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(MUTUALS_HTML.format(degree="2nd degree connection"))
        assert ra._profile_is_first_degree(driver) is False

    def test_a_genuine_first_degree_top_card_is_unaffected(self):
        from cqc_lem.app.engagement import invites as ra
        driver = _DomDriver(MUTUALS_HTML.format(degree="1st"))
        assert ra._profile_is_first_degree(driver) is True

    def test_the_rail_outside_main_never_reaches_the_profile_header_parser(self):
        """`parse_profile_header` reads the WHOLE page source.

        So the "People also viewed" rail is in scope unless the badge read is scoped to <main>.
        """
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        html = ('<html><head><title>Jane Doe | LinkedIn</title></head><body>'
                '<main><h1>Jane Doe</h1></main>'
                '<aside><ul><li><a href="/in/bob/">Bob</a><span>1st</span></li></ul></aside>'
                '</body></html>')
        parsed = parse_profile_header(BeautifulSoup(html, "html.parser"),
                                      "https://www.linkedin.com/in/jane-doe")
        assert "connection" not in parsed

    def test_a_page_that_rendered_no_main_reads_no_degree_rather_than_the_rail(self):
        """The name still resolves off the <title> when the top card never rendered.

        So a whole-document fallback would hand this profile the RAIL's badge — the misroute the
        <main> scope exists to stop, on the one page shape where it is most likely to happen.
        """
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        html = ('<html><head><title>Jane Doe | LinkedIn</title></head><body>'
                '<aside><ul><li><a href="/in/bob/">Bob</a><span>1st</span></li></ul></aside>'
                '</body></html>')
        parsed = parse_profile_header(BeautifulSoup(html, "html.parser"),
                                      "https://www.linkedin.com/in/jane-doe")
        assert parsed["full_name"] == "Jane Doe"
        assert "connection" not in parsed

    def test_a_mutuals_badge_below_the_top_card_never_wins_in_the_parser(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        parsed = parse_profile_header(
            BeautifulSoup(MUTUALS_HTML.format(degree="· 2nd"), "html.parser"),
            "https://www.linkedin.com/in/jane-doe")
        assert parsed["connection"] == "· 2nd"
        assert LinkedInProfile(**parsed).is_1st_connection is False


class TestDegreeBadgeZeroWalk:
    def _blind(self, page_text):
        """A driver whose locator chain matches nothing, rendering `page_text` under <main>."""
        driver = MagicMock()
        driver.find_elements.return_value = []
        main = MagicMock()
        main.text = page_text
        driver.find_element.return_value = main
        return driver

    def test_a_badge_the_chain_cannot_see_warns_as_drift(self):
        from cqc_lem.app.engagement import invites as ra
        driver = self._blind("Jane Doe\n2nd\nFractional CTO at Acme")
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_called_once()
        assert "selector drift" in warn.call_args[0][0]

    def test_a_drifted_1st_degree_badge_is_used_as_the_value_not_just_a_cross_check(self):
        """A chain graded DRIFT still warns, but the page's own unambiguous word is the READ (#1843).

        Falling open to False here would attempt an invite on a target the page plainly shows is
        already 1st-degree, burning the session and an attempt-ceiling slot for nothing.
        """
        from cqc_lem.app.engagement import invites as ra
        driver = self._blind("Jane Doe\n1st\nFractional CTO at Acme")
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is True
        warn.assert_called_once()  # the warning still fires — this is not a silence-the-guard fix
        assert "selector drift" in warn.call_args[0][0]

    def test_a_drifted_non_1st_degree_badge_still_reads_false(self):
        """The complementary case to the 1st-degree fallback above — a 2nd/3rd read stays False."""
        from cqc_lem.app.engagement import invites as ra
        driver = self._blind("Jane Doe\n3rd+\nFractional CTO at Acme")
        with patch(f"{_ZW}.log_warning") as warn:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_called_once()

    def test_a_profile_with_no_badge_at_all_stays_a_debug_no_op(self):
        """Your own profile carries no degree badge, and every invite run opens a profile."""
        from cqc_lem.app.engagement import invites as ra
        driver = self._blind("Jane Doe\nFractional CTO at Acme")
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()
        debug.assert_called_once()

    def test_an_unreadable_page_grounds_nothing(self):
        from cqc_lem.app.engagement import invites as ra
        driver = MagicMock()
        driver.find_elements.return_value = []
        driver.find_element.side_effect = Exception("no main")
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_not_called()
        assert "unknown" in debug.call_args[0][0]

    def test_an_unreadable_chain_never_reaches_the_cross_check(self):
        from cqc_lem.app.engagement import invites as ra
        driver = MagicMock()
        driver.find_elements.side_effect = Exception("stale element")
        with patch(f"{_INV}.log_warning") as warn, patch(f"{_ZW}.log_warning") as drift:
            assert ra._profile_is_first_degree(driver) is False
        warn.assert_called_once()
        drift.assert_not_called()


class TestProfileTopCardSettle:
    """`driver.get()` returns before the top card hydrates, so a read right after drifts (#1843).

    That drifted 100% of invite attempts on 2026-09-01 even though a settled read of the SAME
    profiles grounds cleanly. `_wait_for_profile_top_card` closes that race; it must not itself
    become a new hang or a new source of noisy warnings.
    """

    def test_it_stops_as_soon_as_the_name_or_the_badge_appears(self):
        from cqc_lem.app.engagement import invites as ra

        calls = []

        def until(condition):
            calls.append(1)
            assert condition(driver) is True  # the settled state satisfies the wait's own predicate
            return True

        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]  # main h1 present
        wait = MagicMock()
        wait.until.side_effect = until

        ra._wait_for_profile_top_card(driver, wait)
        assert calls == [1]

    def test_a_page_that_never_settles_falls_through_silently(self):
        """No exception escapes — the degree read's own None/[]/DRIFT handling covers the rest."""
        from selenium.common.exceptions import TimeoutException

        from cqc_lem.app.engagement import invites as ra

        driver = MagicMock()
        wait = MagicMock()
        wait.until.side_effect = TimeoutException("never settled")

        ra._wait_for_profile_top_card(driver, wait)  # must not raise

    def test_it_does_not_call_the_logging_degree_read_while_polling(self):
        """Polling `_degree_badge_texts` here would multiply its one warning by the poll count (#1843).

        The settle check must stay a silent DOM probe, leaving the logging to the single read that
        follows it.
        """
        from cqc_lem.app.engagement import invites as ra

        driver = MagicMock()
        wait = MagicMock()
        wait.until.side_effect = lambda condition: condition(driver)

        with patch(f"{_INV}._degree_badge_texts") as texts, \
             patch(f"{_INV}._matching_degree_lines") as lines:
            ra._wait_for_profile_top_card(driver, wait)

        texts.assert_not_called()
        lines.assert_not_called()


class TestProfileHeaderDegree:
    """`parse_profile_header` feeds `LinkedInProfile.connection`.

    That is what routes every profile viewer down the 1st-vs-other branch — the same dead class
    anchor, one layer up.
    """

    def _parse(self, html):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        return parse_profile_header(BeautifulSoup(html, "html.parser"),
                                    "https://www.linkedin.com/in/jane-doe")

    def test_a_first_degree_card_yields_a_first_degree_profile(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        parsed = self._parse(TOP_CARD_HTML.format(degree="1st"))
        assert parsed["connection"] == "1st"
        assert LinkedInProfile(**parsed).is_1st_connection is True

    def test_a_second_degree_card_is_not_a_connection(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        parsed = self._parse(TOP_CARD_HTML.format(degree="2nd"))
        assert parsed["connection"] == "2nd"
        assert LinkedInProfile(**parsed).is_1st_connection is False

    def test_the_legacy_class_anchor_still_wins_where_it_exists(self):
        html = ('<html><head><title>Jane Doe | LinkedIn</title></head><body><main>'
                '<h1>Jane Doe</h1><span class="dist-value">1st</span>'
                '<span>2nd</span></main></body></html>')
        assert self._parse(html)["connection"] == "1st"

    def test_a_card_with_no_badge_reports_no_connection(self):
        parsed = self._parse('<html><head><title>Jane Doe | LinkedIn</title></head>'
                             '<body><main><h1>Jane Doe</h1></main></body></html>')
        assert "connection" not in parsed

    def test_a_badge_wrapped_beside_an_icon_still_reads(self):
        """The 2026-08-31 drift, on the parser half: the badge is no longer a childless node."""
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        parsed = self._parse(NESTED_BADGE_HTML.format(degree="1st"))
        assert parsed["connection"] == "1st"
        assert LinkedInProfile(**parsed).is_1st_connection is True

    def test_a_non_first_nested_badge_is_not_a_connection(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        parsed = self._parse(NESTED_BADGE_HTML.format(degree="· 2nd"))
        assert parsed["connection"] == "· 2nd"
        assert LinkedInProfile(**parsed).is_1st_connection is False


class TestProfileNameZeroWalk:
    def _parse(self, html):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_header
        return parse_profile_header(BeautifulSoup(html, "html.parser"),
                                    "https://www.linkedin.com/in/jane-doe")

    def test_a_rendered_profile_with_no_name_is_drift(self):
        from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError
        html = ('<html><head><title>LinkedIn</title></head><body><main>'
                '<a href="/in/jane-doe/">Jane</a><a href="/in/bob/">Bob</a>'
                '</main></body></html>')
        with patch(f"{_ZW}.log_warning") as warn, pytest.raises(ProfileUnavailableError) as err:
            self._parse(html)
        warn.assert_called_once()
        assert "drift" in str(err.value)

    def test_a_shell_that_never_rendered_is_not_a_defect(self):
        from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError
        html = '<html><head><title>LinkedIn</title></head><body><main></main></body></html>'
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug, \
             pytest.raises(ProfileUnavailableError) as err:
            self._parse(html)
        warn.assert_not_called()
        debug.assert_called_once()
        assert "empty" in str(err.value)


class TestProfileViewerBranch:
    """Both sides of the 1st-vs-other branch the dead badge silently collapsed into one."""

    def _engage(self, connection, activities=()):
        """Run one engagement with `profile_viewer_dm_auto_send` ON.

        Which BRANCH a viewer takes is what this class is about, and the #1137 approval gate is a
        question about what each branch then does — so the toggle is held ON here to keep the
        dispatch mocks below the branch's observable outcome.
        """
        from cqc_lem.app.engagement import outreach as ra
        profile_data = {"full_name": "Jane Doe", "connection": connection,
                        "profile_url": "https://www.linkedin.com/in/jane-doe",
                        "recent_activities": list(activities)}
        my_profile = MagicMock()
        my_profile.full_name = "Chris Queen"
        my_profile.email = "chris@example.com"
        with patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=False), \
             patch(f"{_OUT}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "chris@example.com", my_profile)), \
             patch(f"{_OUT}.get_linkedin_profile_from_url", return_value=profile_data), \
             patch(f"{_OUT}.get_engagement_preferences",
                   return_value={"profile_viewer_dm_auto_send": True}), \
             patch(f"{_OUT}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_OUT}.generate_and_post_comment", return_value=True) as commented, \
             patch(f"{_OUT}.summarize_recent_activity", return_value="they shipped a thing"), \
             patch(f"{_OUT}.get_ai_message_refinement", return_value="Hi Jane"), \
             patch(f"{_OUT}.get_user_id", return_value=1), \
             patch(f"{_OUT}.invite_to_connect") as invite, \
             patch(f"{_OUT}.insert_new_log"), patch(f"{_OUT}.quit_gracefully"):
            ra.engage_with_profile_viewer.run(user_id=1,
                                              viewer_url="https://www.linkedin.com/in/jane-doe",
                                              viewer_name="Jane Doe")
        return commented, invite

    def test_a_first_degree_viewer_gets_a_comment_never_an_invite(self):
        activity = {"text": "shipped", "link": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "posted": (datetime.now() - timedelta(days=2)).isoformat()}
        commented, invite = self._engage("1st", [activity])
        commented.assert_called_once()
        invite.apply_async.assert_not_called()

    def test_a_second_degree_viewer_gets_an_invite_never_a_comment(self):
        commented, invite = self._engage("2nd")
        invite.apply_async.assert_called_once()
        commented.assert_not_called()

    def test_a_missing_badge_still_routes_to_the_invite_branch(self):
        """Fail-open is the OLD behaviour and stays — the tripwire is what makes it visible."""
        commented, invite = self._engage(None)
        invite.apply_async.assert_called_once()
        commented.assert_not_called()


class TestCompanyInviteZeroWalk:
    def _run(self, cross_check_rows):
        from cqc_lem.utilities.linkedin import company_page_inviter as cpi
        driver, wait = MagicMock(), MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        driver.find_elements.return_value = [MagicMock()] * cross_check_rows
        plan = {"allowance": 4, "cap": 5, "sent_today": 0, "status": "sent"}
        with patch(f"{_CPI}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
             patch(f"{_CPI}.get_company_linked_in_url_for_user",
                   return_value="https://www.linkedin.com/company/acme"), \
             patch(f"{_CPI}.login_to_linkedin"), \
             patch(f"{_CPI}.get_available_credits", return_value=(200, 250)), \
             patch(f"{_CPI}.select_connection_checkboxes", return_value=0), \
             patch(f"{_CPI}.insert_new_log") as log, \
             patch(f"{_CPI}.record_action") as rec, \
             patch(f"{_CPI}.time.sleep"):
            report = cpi.automate_invitations(driver, wait, 1, plan=plan)
        return report, log, rec

    def test_zero_invitees_on_a_modal_that_renders_rows_is_drift(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_DRIFT
        with patch(f"{_ZW}.log_warning") as warn:
            report, log, rec = self._run(cross_check_rows=12)
        assert report["status"] == INVITE_STATUS_DRIFT
        assert report["invites_sent"] == 0
        warn.assert_called_once()
        # A drifted read still sends nothing and logs no send.
        log.assert_not_called()
        rec.assert_not_called()

    def test_zero_invitees_on_an_empty_modal_stays_no_candidates(self):
        from cqc_lem.utilities.linkedin.company_page_inviter import INVITE_STATUS_NO_CANDIDATES
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            report, _, _ = self._run(cross_check_rows=0)
        assert report["status"] == INVITE_STATUS_NO_CANDIDATES
        warn.assert_not_called()
        debug.assert_called_once()

    def test_the_cross_check_uses_neither_xpath_the_walk_drives(self):
        from cqc_lem.utilities.linkedin import company_page_inviter as cpi
        selector = cpi._INVITEE_ROW_CROSSCHECK_SEL
        assert "scaffold-finite-scroll__content" not in selector
        assert "checkbox" not in selector


class TestPostStatsZeroWalk:
    def _run(self, counts, page_text):
        from cqc_lem.app.engagement import posting as ra
        driver = MagicMock()
        main = MagicMock()
        main.text = page_text
        driver.find_element.return_value = main
        with patch(f"{_POST}.time.sleep"), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[9]), \
             patch(f"{_POST}.get_uncaptured_posted_post_ids", return_value=[]), \
             patch(f"{_POST}.get_current_profile",
                   return_value=(driver, MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://x/urn"), \
             patch(f"{_POST}._post_social_counts", return_value=dict(counts)), \
             patch(f"{_POST}._post_analytics_counts", return_value={}), \
             patch(f"{_POST}.get_shipped_variant_keys", return_value={}), \
             patch(f"{_POST}.track_post_outcome"), \
             patch(f"{_POST}.record_post_stats") as rec, patch(f"{_POST}.quit_gracefully"):
            result = ra.auto_scrape_post_stats.run(user_id=1)
        return result, rec

    ZERO = {"reactions": 0, "comments": 0, "reposts": 0, "impressions": 0, "saves": 0}

    def test_a_page_showing_numbers_the_parser_missed_is_left_uncaptured(self):
        with patch(f"{_ZW}.log_warning") as warn:
            result, rec = self._run(self.ZERO, "Impressions\n412\nReactions\n11\nComments\n4")
        warn.assert_called_once()
        rec.assert_not_called()  # a written zero is permanent for a backfilled post (#809)
        assert "0 post" in result

    def test_a_genuinely_quiet_post_is_still_recorded(self):
        with patch(f"{_ZW}.log_warning") as warn:
            _, rec = self._run(self.ZERO, "Impressions\n0\nReactions\n0\nBe the first to comment")
        warn.assert_not_called()
        rec.assert_called_once()

    def test_a_post_with_any_signal_never_reaches_the_tripwire(self):
        counts = dict(self.ZERO, reactions=3)
        with patch(f"{_ZW}.log_warning") as warn, patch(f"{_ZW}.log_debug") as debug:
            _, rec = self._run(counts, "Reactions\n3")
        warn.assert_not_called()
        debug.assert_not_called()
        rec.assert_called_once()

    def test_a_label_with_no_number_beside_it_is_not_a_finding(self):
        """Button rows ("Like / Comment / Repost") and a label whose value moved away from it.

        Both read as `empty` — the fail-safe direction for a tripwire that files defects.
        """
        from cqc_lem.app.engagement import posting as ra
        assert ra._rendered_count_signals("Like\nComment\nRepost\nSend") == 0
        assert ra._rendered_count_signals("Impressions\n\nsome prose\n412") == 0

    def test_the_cross_check_reads_labels_the_parser_does_not_map(self):
        """A cross-check limited to the parser's own vocabulary could never see a label rename."""
        from cqc_lem.app.engagement import posting as ra
        assert ra._rendered_count_signals("Members reached\n1,204") == 1
        assert ra._rendered_count_signals("Views\n88") == 1
