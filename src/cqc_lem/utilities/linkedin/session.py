"""Acquiring a logged-in LinkedIn browser session, and giving it back (#1154).

Lifted VERBATIM out of `app/run_automation.py`, where `get_current_profile` had 25 direct readers —
more than any other symbol in that module, and spanning every engagement cluster. It is the front
door to a Chrome session, not a task, so it lives here.

Chrome capacity is a **fixed pool** of session slots shared by the Celery Selenium lanes
(`SE_NODE_MAX_SESSIONS`), so a driver that is acquired and never quit does not merely slow things
down — it permanently removes one of about eight slots until the worker process is recycled.
`browser_session` is the structural answer to that; `get_current_profile` deliberately raises rather
than swallowing an acquisition failure, because a 429, an auth wall and an unresolvable profile are
answered differently by each caller.

The fallback chain is the other invariant: a live profile refresh can fail on its own (auth-wall on
the profile view, DOM churn) while the feed is perfectly reachable, so a failed scrape falls back to
the cached profile and only a session with NEITHER is fatal.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple, Tuple

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem.utilities.db import get_user_password_pair_by_id
from cqc_lem.utilities.linkedin.helper import get_my_profile, load_profile_for_user, login_to_linkedin
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import log_error, log_info, log_warning
from cqc_lem.utilities.selenium_util import get_driver_wait_pair, is_tab_crashed, quit_gracefully


def get_current_profile(user_id: int, session_name: str = "Get Current Profile",
                        measurement_only: bool = False, debug: bool = False,
                        force_refresh: bool = False, debug_required: bool = False,
                        needs_images: bool = False) -> Tuple[
    WebDriver, WebDriverWait, str, LinkedInProfile]:
    """Update the profile of the user.

    `measurement_only` marks a read-only stat-capture session, which keeps running under the
    suppression tripwire's own pause (and only that one) so recovery stays measurable — see
    rate_limit.is_measurement_paused.

    `debug` requests the watchable Grid debug node (if free) for live inspection; it falls
    back to the normal pool when the node is busy or absent. `debug_required` removes that
    fallback and raises `DebugNodeUnavailable` instead — the live-validation probe an autonomous
    agent runs must never take a slot the engagement lanes are sized for (#1301).

    `force_refresh` makes the scrape bypass the profile cache (issue #1076). The cached FALLBACK
    below is unaffected on purpose: a forced scrape that fails still beats acting on nothing, and
    the caller learns from the synthesis it gets back, not from a missing profile.

    `needs_images` passes straight through to `get_driver_wait_pair` (#1774) — a caller whose
    session will read a `/messaging/*` surface (e.g. the follow-up thread ladder) sets this `True`
    to exempt itself from the bandwidth saver, which otherwise stops LinkedIn's messaging fastboot
    app from ever mounting. Defaults `False`, unchanged for every other caller.
    """
    log_info("Getting Updated Profile")

    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name=session_name, user_id=user_id, debug=debug,
                                        debug_required=debug_required, needs_images=needs_images)

    # Login first — a failure here (e.g. HTTP 429 rate-limit, expired cookie) is fatal
    # for this run; abort cleanly so the caller backs off instead of hammering LinkedIn.
    try:
        login_to_linkedin(driver, wait, user_email, user_password,
                          measurement_only=measurement_only)
    except Exception as e:
        if is_tab_crashed(e):
            # The renderer behind this freshly-acquired session's tab was already dead (a Grid slot
            # reused from a previous heavy session that OOM-killed it, issue #1746) before the very
            # first navigation — never a login/rate-limit fault, so "possibly rate-limited" would be
            # actively wrong here. The caller aborts this run and quits the session the same as any
            # other login failure; only the severity changes, to a warning that escalates if it
            # starts recurring (issue #1749).
            log_warning("Browser tab crashed on the first login navigation", exc=e, user_id=user_id)
        else:
            log_error("LinkedIn login failed (possibly rate-limited)", exc=e, user_id=user_id)
        quit_gracefully(driver)
        raise e

    # A live profile refresh can fail independently (auth-wall on the profile view,
    # transient DOM change) even when the feed is reachable. Don't let that abort the
    # whole task — fall back to the user's cached profile so commenting can proceed.
    try:
        my_profile = get_my_profile(driver, wait, user_email, user_password, user_id=user_id,
                                    force_refresh=force_refresh)
    except Exception as e:
        log_warning("Live profile refresh failed; falling back to cached profile", exc=e, user_id=user_id)
        my_profile = None

    if my_profile is None and user_id is not None:
        my_profile = load_profile_for_user(user_id)

    if my_profile is None:
        log_error("No profile available (live scrape failed and no cached profile)", user_id=user_id)
        quit_gracefully(driver)
        raise RuntimeError("Profile unavailable: live scrape failed and no cached profile to fall back on")

    return driver, wait, user_email, my_profile


class LinkedInSession(NamedTuple):
    """The four values every browser-driven task carries around together.

    A NamedTuple on purpose: `driver, wait, user_email, my_profile = session` still unpacks exactly
    as the bare tuple did, so this is additive for anything that already destructures the result of
    `get_current_profile`.
    """

    driver: WebDriver
    wait: WebDriverWait
    user_email: str
    my_profile: LinkedInProfile


@contextmanager
def browser_session(user_id: int, session_name: str, **kwargs) -> "Iterator[LinkedInSession]":
    """Hold a logged-in LinkedIn session for the duration of a block, and always give it back.

    Chrome capacity is a FIXED pool of session slots shared by the Selenium lanes
    (`SE_NODE_MAX_SESSIONS`), so a driver that is acquired and not quit does not degrade
    performance — it permanently removes one of about eight slots until the worker process is
    recycled. That teardown is currently a `try/finally` written out by hand in 24 task bodies, and
    nothing stops the 25th from forgetting it.

    Deliberately does NOT catch the acquisition failure. `get_current_profile` raises on a 429, an
    auth wall, or a profile that will not resolve, and each caller answers that differently — some
    return a message string the beat records, some re-raise for a retry, one is a measurement-only
    run that must stay quiet. Guessing one of those would be worse than the four lines it saves.

    Args:
        user_id: Whose stored credentials and proxy the session runs as.
        session_name: The label the Grid session is tagged with, for VNC and logs.
        **kwargs: Forwarded to `get_current_profile` — `measurement_only`, `debug`, `force_refresh`.

    Yields:
        A `LinkedInSession`, which also unpacks as the historical
        `(driver, wait, user_email, my_profile)` 4-tuple.
    """
    driver, wait, user_email, my_profile = get_current_profile(
        user_id=user_id, session_name=session_name, **kwargs)
    try:
        yield LinkedInSession(driver=driver, wait=wait, user_email=user_email, my_profile=my_profile)
    finally:
        quit_gracefully(driver)
