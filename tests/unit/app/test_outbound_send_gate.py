"""The gate where it actually stops a send: `send_dm_now` and both comment write paths.

The unit above (`tests/unit/utilities/ai/test_outbound_qa.py`) proves the predicate. These prove the
predicate is WIRED — that a refused body costs zero Selenium slots, zero LinkedIn API calls, and
returns the same falsy value the callers already handle. Every one of these paths was open on
2026-09-04; `send_dm_now` is the one a real DM went through.
"""

from unittest.mock import MagicMock, patch

import pytest

INCIDENT_DM = ("To assist you effectively, I need the actual message history JSON to analyze the "
               "conversation context. Please provide the message history so I can proceed with "
               "evaluating the new message and generating a response accordingly.")

PLACEHOLDER_DM = ("No worries if the timing didn't work out. I've put together a quick recap here "
                  "if it helps: [link]")

GOOD_DM = "Hey Dan, saw you checked my profile. What caught your eye? No pitch."


@pytest.mark.unit
class TestSendDmNowGate:
    """The gate on the ONE shared DM send path.

    `send_dm_now` is what all five DM lanes (private, scheduled, catch-up, nurture, appreciation)
    call, which is why the gate sits here and not in each lane.
    """

    @pytest.mark.parametrize("body", [INCIDENT_DM, PLACEHOLDER_DM, "", "   "])
    def test_refuses_without_opening_a_browser(self, body):
        """A body we will not send must not cost a Chrome slot off the fixed pool."""
        with patch("cqc_lem.app.engagement.outreach.get_driver_wait_pair") as driver, \
                patch("cqc_lem.app.engagement.outreach.get_user_password_pair_by_id") as creds, \
                patch("cqc_lem.app.engagement.outreach.log_warning") as warn:
            from cqc_lem.app.engagement.outreach import send_dm_now

            assert send_dm_now(1, "https://www.linkedin.com/in/someone/", body) is False
            assert not driver.called, "opened a Selenium session for a body it refused"
            assert not creds.called
            assert warn.called

    def test_refusal_names_the_checks_that_fired(self):
        with patch("cqc_lem.app.engagement.outreach.get_driver_wait_pair"), \
                patch("cqc_lem.app.engagement.outreach.get_user_password_pair_by_id"), \
                patch("cqc_lem.app.engagement.outreach.log_warning") as warn:
            from cqc_lem.app.engagement.outreach import send_dm_now

            send_dm_now(1, "https://www.linkedin.com/in/someone/", INCIDENT_DM)

            message = warn.call_args[0][0]
            assert "input_request" in message
            assert warn.call_args[1].get("action_type") == "dm"

    def test_a_good_body_still_reaches_the_browser(self):
        """The gate must not become the reason DMs stop going out."""
        with patch("cqc_lem.app.engagement.outreach.get_driver_wait_pair") as driver, \
                patch("cqc_lem.app.engagement.outreach.get_user_password_pair_by_id",
                      return_value=("e@x.com", "pw")), \
                patch("cqc_lem.app.engagement.outreach.login_to_linkedin"), \
                patch("cqc_lem.app.engagement.outreach.open_addressed_composer") as composer, \
                patch("cqc_lem.app.engagement.outreach.quit_gracefully", create=True), \
                patch("cqc_lem.app.engagement.outreach.add_log_entry", create=True):
            driver.return_value = (MagicMock(), MagicMock())
            composer.return_value = MagicMock(addressed=False, reason="test stops here")
            from cqc_lem.app.engagement.outreach import send_dm_now

            send_dm_now(1, "https://www.linkedin.com/in/someone/", GOOD_DM)

            assert driver.called, "a sendable body was refused"


@pytest.mark.unit
class TestCommentGates:
    """The gate on both comment write paths.

    A comment is PUBLIC, so the same body is worse here than in a DM. Two paths, both gated: the
    socialActions API and the inline Selenium composer.
    """

    @pytest.mark.parametrize("body", [INCIDENT_DM, PLACEHOLDER_DM])
    def test_api_comment_refused_before_the_request(self, body):
        with patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id", return_value="sub"), \
                patch("cqc_lem.utilities.linkedin.poster.get_user_access_token", return_value="t"), \
                patch("cqc_lem.utilities.linkedin.poster._restli") as restli, \
                patch("cqc_lem.utilities.linkedin.poster.log_warning") as warn:
            from cqc_lem.utilities.linkedin.poster import comment_on_linkedin_post

            assert comment_on_linkedin_post(1, "urn:li:share:1", body) is None
            assert not restli.called, "called the LinkedIn API with a body it refused"
            assert warn.called

    @pytest.mark.parametrize("body", [INCIDENT_DM, PLACEHOLDER_DM])
    def test_inline_comment_refused_before_the_composer_opens(self, body):
        with patch("cqc_lem.app.engagement.feed.strip_non_bmp", side_effect=lambda t: t), \
                patch("cqc_lem.app.engagement.feed.click_first") as click, \
                patch("cqc_lem.app.engagement.feed.log_warning") as warn:
            from cqc_lem.app.engagement.feed import post_comment_inline

            result = post_comment_inline(MagicMock(), MagicMock(), MagicMock(), body, user_id=1)

            assert result is False
            assert not click.called, "opened a comment composer for a body it refused"
            assert warn.called
