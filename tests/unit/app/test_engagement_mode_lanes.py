"""Suggest-only engagement never opens a browser, on ANY lane (issue #2367).

A trial account's engagement is suggestions only: no Selenium session may ever run on its LinkedIn
account. Every lane task is driven here with `get_docker_driver` (and the Selenium Remote
constructor behind it) replaced by a mock that FAILS if touched, for a `suggest` account AND for an
account whose mode cannot be read — the guard fails closed, so the two must behave identically.

Two assertions per lane, because either alone could pass vacuously: the task returned the
guard's own skip/saved message (so the guard ran, rather than the lane bailing out early for some
unrelated reason before it would have reached a driver), and the driver mock was never called.

`TestEveryBrowserLaneIsCovered` keeps the list honest: it reads the lane modules' source and fails
when a function that opens a session is neither a parametrized task here nor a known helper only
those tasks reach.
"""

import ast
import pathlib
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.engagement_mode import SUGGEST_ONLY_SKIP_MESSAGE, SUGGESTION_SAVED_MESSAGE

pytestmark = pytest.mark.unit

_FEED = "cqc_lem.app.engagement.feed"
_INV = "cqc_lem.app.engagement.invites"
_NEWS = "cqc_lem.app.engagement.newsletter"
_OUT = "cqc_lem.app.engagement.outreach"
_POST = "cqc_lem.app.engagement.posting"
_MODE = "cqc_lem.utilities.engagement_mode.get_user_engagement_mode"

# (module, task, kwargs, row patches) — every Selenium lane task, with the smallest call that
# reaches its guard. Row-id tasks resolve their user from a row, so the row reader is stubbed.
_SKIP_LANES = [
    (_FEED, "consolidate_duplicate_comments_for_user", {"user_id": 1}, {}),
    (_FEED, "auto_sync_user_groups", {"user_id": 1}, {}),
    (_FEED, "auto_comment_in_groups", {"user_id": 1}, {}),
    (_FEED, "auto_post_to_group", {"user_id": 1, "group_id": "g1"}, {}),
    (_FEED, "automate_commenting", {"user_id": 1}, {}),
    (_INV, "clean_stale_invites", {"user_id": 1}, {}),
    (_INV, "invite_to_connect", {"user_id": 1, "profile_url": "https://www.linkedin.com/in/x/"}, {}),
    (_INV, "send_roster_connect_invite",
     {"user_id": 1, "profile_url": "https://www.linkedin.com/in/x/"}, {}),
    (_INV, "automate_invites_to_company_page_for_user", {"user_id": 1}, {}),
    (_INV, "send_connection_request", {"request_id": 5},
     {"cqc_lem.utilities.db.get_connection_request":
      {"status": "approved", "user_id": 1, "recipient_profile_url": "u", "message": "m"}}),
    (_NEWS, "auto_publish_newsletter_edition", {"user_id": 1}, {}),
    (_NEWS, "auto_publish_edition", {"edition_id": 5},
     {f"{_NEWS}.get_newsletter_edition": {"status": "approved", "user_id": 1}}),
    (_NEWS, "track_newsletter_subscribers", {"user_id": 1}, {}),
    (_OUT, "process_user_followups", {"user_id": 1}, {}),
    (_OUT, "automate_appreciation_dms_for_user", {"user_id": 1}, {}),
    (_OUT, "automate_profile_viewer_engagement", {"user_id": 1}, {}),
    (_OUT, "engage_with_profile_viewer",
     {"user_id": 1, "viewer_url": "https://www.linkedin.com/in/v/", "viewer_name": "V"}, {}),
    (_OUT, "send_scheduled_dm", {"dm_id": 5},
     {"cqc_lem.utilities.db.get_scheduled_dm":
      {"status": "approved", "user_id": 1, "scheduled_time": None,
       "recipient_profile_url": "u", "message": "m"}}),
    (_OUT, "send_lead_response", {"signal_id": 5},
     {f"{_OUT}.get_lead_signal":
      {"status": "approved", "user_id": 1, "draft_response": "hi", "channel": "dm",
       "person_profile_url": "u"}}),
    (_OUT, "scan_connection_candidates", {"user_id": 1}, {}),
    (_OUT, "process_outreach_funnel", {"user_id": 1}, {}),
    (_OUT, "scan_outreach_funnel_targets", {"user_id": 1}, {}),
    (_OUT, "automate_catchup_touches", {"user_id": 1}, {}),
    (_OUT, "send_catchup_touch", {"touch_id": 5},
     {f"{_OUT}.get_catchup_touch": {"status": "approved", "user_id": 1, "message": "Congrats!"}}),
    (_POST, "auto_scrape_post_stats", {"user_id": 1}, {}),
    (_POST, "capture_follower_stats", {"user_id": 1}, {}),
    (_POST, "sweep_reply_comments", {"user_id": 1}, {}),
    (_POST, "sweep_comment_followups", {"user_id": 1}, {}),
    (_POST, "process_comment_followups_for_url",
     {"user_id": 1, "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/"}, {}),
    (_POST, "reconcile_recent_comment_urns", {"user_id": 1}, {}),
    (_POST, "sweep_comment_outcomes", {"user_id": 1}, {}),
    (_POST, "automate_reply_commenting", {"user_id": 1, "post_id": 3}, {}),
    (_POST, "update_stale_profile", {"user_id": 1}, {}),
    (_POST, "auto_publish_occasion_post", {"user_id": 1, "post_id": 3}, {}),
]

# The two send tasks that carry a finished draft: a suggest account gets it STORED, not skipped.
_SAVE_LANES = [
    (_FEED, "comment_on_post",
     {"user_id": 1, "post_link": "https://www.linkedin.com/feed/update/urn:li:activity:9/",
      "comment_text": "Sharp point on pricing."},
     "comment", "Sharp point on pricing.", "https://www.linkedin.com/feed/update/urn:li:activity:9/"),
    (_OUT, "send_private_dm",
     {"user_id": 1, "profile_url": "https://www.linkedin.com/in/sam/", "message": "Thanks, Sam."},
     "dm", "Thanks, Sam.", "https://www.linkedin.com/in/sam/"),
]

# Readings of `users.engagement_mode` that must all behave as `suggest` — the stored value, and
# the three ways a read can fail to say `automate`.
_NOT_AUTOMATE = [
    pytest.param({"return_value": "suggest"}, id="suggest"),
    pytest.param({"return_value": None}, id="unreadable-none"),
    pytest.param({"side_effect": TypeError("int() argument must be ... not 'NoneType'")},
                 id="unreadable-raises"),
    pytest.param({"return_value": "AUTOMATE "}, id="unrecognised-value"),
]


def _task(module: str, name: str):
    import importlib

    return getattr(importlib.import_module(module), name)


def _browser_tripwires(stack: ExitStack) -> tuple[MagicMock, MagicMock]:
    """Replace every way to a Chrome session with a mock that fails the test if it is reached."""
    driver = stack.enter_context(patch(
        "cqc_lem.utilities.selenium_util.get_docker_driver",
        side_effect=AssertionError("get_docker_driver called for a suggest-only account")))
    remote = stack.enter_context(patch(
        "selenium.webdriver.Remote",
        side_effect=AssertionError("webdriver.Remote called for a suggest-only account")))
    return driver, remote


class TestNoBrowserForASuggestOnlyAccount:
    @pytest.mark.parametrize("mode", _NOT_AUTOMATE)
    @pytest.mark.parametrize("module,name,kwargs,rows", _SKIP_LANES,
                             ids=[lane[1] for lane in _SKIP_LANES])
    def test_the_lane_skips_before_any_driver(self, module, name, kwargs, rows, mode):
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, **mode))
            driver, remote = _browser_tripwires(stack)
            for target, row in rows.items():
                stack.enter_context(patch(target, return_value=row))
            result = _task(module, name).run(**kwargs)
        assert result == SUGGEST_ONLY_SKIP_MESSAGE
        driver.assert_not_called()
        remote.assert_not_called()

    @pytest.mark.parametrize("mode", _NOT_AUTOMATE)
    @pytest.mark.parametrize("module,name,kwargs,kind,body,target", _SAVE_LANES,
                             ids=[lane[1] for lane in _SAVE_LANES])
    def test_a_finished_draft_is_stored_as_a_suggestion(self, module, name, kwargs, kind, body,
                                                        target, mode):
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, **mode))
            driver, remote = _browser_tripwires(stack)
            store = stack.enter_context(patch(
                "cqc_lem.utilities.engagement_mode.insert_engagement_suggestion", return_value=True))
            result = _task(module, name).run(**kwargs)
        assert result == SUGGESTION_SAVED_MESSAGE
        driver.assert_not_called()
        remote.assert_not_called()
        store.assert_called_once()
        args, call_kwargs = store.call_args
        assert args[0] == 1
        assert str(args[1]) == kind
        assert args[3] == body
        assert call_kwargs["target_url"] == target


class TestAnAutomateAccountIsUnchanged:
    """Anti-overreach: the guard lets an `automate` account through to the browser as before.

    Each lane's driver acquisition is stubbed to raise a sentinel, so reaching it — and only
    reaching it — proves the guard stood aside.
    """

    class _DriverReached(Exception):
        pass

    @pytest.mark.parametrize("module,name,kwargs,acquire,setup", [
        (_FEED, "comment_on_post",
         {"user_id": 1, "post_link": "https://www.linkedin.com/feed/update/urn:li:activity:9/",
          "comment_text": "x"},
         f"{_FEED}.get_driver_wait_pair",
         {f"{_FEED}.has_user_commented_on_post_url": False, f"{_FEED}.has_commented_post": False,
          f"{_FEED}.claim_post_for_comment": True}),
        (_POST, "update_stale_profile", {"user_id": 1}, f"{_POST}.get_current_profile", {}),
        (_OUT, "send_private_dm",
         {"user_id": 1, "profile_url": "https://www.linkedin.com/in/sam/", "message": "hi"},
         f"{_OUT}.send_dm_now", {}),
    ], ids=["comment_on_post", "update_stale_profile", "send_private_dm"])
    def test_the_lane_reaches_its_browser(self, module, name, kwargs, acquire, setup):
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value="automate"))
            reached = stack.enter_context(patch(acquire, side_effect=self._DriverReached()))
            for target, value in setup.items():
                stack.enter_context(patch(target, return_value=value))
            store = stack.enter_context(patch(
                "cqc_lem.utilities.engagement_mode.insert_engagement_suggestion"))
            try:
                result = _task(module, name).run(**kwargs)
            except self._DriverReached:
                result = None
        reached.assert_called_once()
        store.assert_not_called()
        assert result not in (SUGGEST_ONLY_SKIP_MESSAGE, SUGGESTION_SAVED_MESSAGE)


class TestApiPublishingIsNotGated:
    """`post_to_linkedin` is the OAuth `w_member_social` path, not a browser.

    A trial account's approved posts must still publish.
    """

    def test_a_suggest_account_still_publishes_through_the_api(self):
        from cqc_lem.app.engagement.posting import post_to_linkedin
        from cqc_lem.utilities.db import PostType

        slides = ["/assets/images/carousel/40/slide_01.png"]
        stubs = {
            "get_post_status": "approved",
            "get_post_manual_publish": False,
            "get_user_password_pair_by_id": ("u@example.com", "pw"),
            "get_post_content": "Post text",
            "insert_new_log": None,
            "update_db_post_status": None,
            "get_engagement_preferences": {"reply_check_mode": "event"},
            "sweep_reply_comments": None,
            "auto_seed_comment_on_post": None,
            "auto_second_wave_comment": None,
            "get_post_type": PostType.DOCUMENT,
            "get_carousel_slides": slides,
        }
        with ExitStack() as stack:
            stack.enter_context(patch(_MODE, return_value="suggest"))
            driver, remote = _browser_tripwires(stack)
            for name, value in stubs.items():
                stack.enter_context(patch(f"{_POST}.{name}", return_value=value))
            share = stack.enter_context(patch(f"{_POST}.share_document_on_linkedin",
                                              return_value="urn:li:share:40"))
            post_to_linkedin.run(1, 40)
        share.assert_called_once()
        driver.assert_not_called()
        remote.assert_not_called()


# Functions in the lane modules that open a session but are NOT Celery tasks: each is reached only
# through a task in the lists above. Adding one here is a claim that must stay true.
_KNOWN_SESSION_HELPERS = {
    "invite_to_connect_now",          # invite_to_connect / send_roster_connect_invite / send_connection_request
    "accept_connection_request",      # automate_appreciation_dms_for_user
    "send_dm_now",                    # send_private_dm / send_scheduled_dm / send_lead_response / ...
    "_send_lead_response",            # send_lead_response
    "_swap_post_stats_session",       # auto_scrape_post_stats
    "_run_comment_followups_sweep",   # sweep_comment_followups
    "_run_single_post_followup",      # process_comment_followups_for_url
    "_run_reconcile_comment_urns",    # reconcile_recent_comment_urns
    "_run_comment_outcomes_sweep",    # sweep_comment_outcomes
}
_SESSION_CALLS = {"get_current_profile", "get_driver_wait_pair", "browser_session", "get_docker_driver"}


def _session_openers(path: pathlib.Path) -> set:
    """Top-level functions in `path` whose body calls one of `_SESSION_CALLS`."""
    tree = ast.parse(path.read_text())
    found = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id in _SESSION_CALLS):
                found.add(node.name)
                break
    return found


class TestEveryBrowserLaneIsCovered:
    def test_every_function_that_opens_a_session_is_a_tested_lane_or_a_known_helper(self):
        root = pathlib.Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "app" / "engagement"
        openers = set()
        for name in ("feed", "invites", "newsletter", "outreach", "posting"):
            openers |= _session_openers(root / f"{name}.py")
        assert len(openers) > 20, "the walk found almost nothing — it is not reading the lanes"
        tested = {lane[1] for lane in _SKIP_LANES} | {lane[1] for lane in _SAVE_LANES}
        uncovered = openers - tested - _KNOWN_SESSION_HELPERS
        assert not uncovered, (
            f"{sorted(uncovered)} open a browser session but have no suggest-mode test — add the "
            "task to _SKIP_LANES (and a skip_browser_lane guard), or name the helper and its tasks")

    def test_the_known_helpers_still_exist(self):
        """A stale entry would silently widen what the check above lets through."""
        root = pathlib.Path(__file__).resolve().parents[3] / "src" / "cqc_lem" / "app" / "engagement"
        openers = set()
        for name in ("feed", "invites", "newsletter", "outreach", "posting"):
            openers |= _session_openers(root / f"{name}.py")
        assert _KNOWN_SESSION_HELPERS <= openers
