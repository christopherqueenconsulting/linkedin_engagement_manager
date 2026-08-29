"""The connect rail: profile-degree reads, the invite dialog, and the four invite tasks (#1154).

First slice out of `run_automation.py` under the layered-architecture program. It is a closed
sub-graph — every caller of `invite_to_connect_now`, `_profile_is_first_degree`,
`_open_connect_invite_dialog`, `_add_connect_note` and `_submit_connect_invite` is in this file, and
the two entry points the rest of the app uses (`invite_to_connect`, `send_roster_connect_invite`)
are reached through `.apply_async`, which is a wire name rather than an import.

**Every task here pins `name='cqc_lem.app.run_automation.<fn>'`, and that is load-bearing.** Celery
derives a task's name from `<module>.<function>`, so moving a task RENAMES it — silently. The three
`celeryconfig.task_routes` keys naming these tasks are plain strings that would simply stop matching,
messages already queued under the old name would be rejected `NotRegistered` and dropped, and the
`QueueOnce` Redis lock key (which embeds the task name, and which none of these tasks release before
running) would re-key mid-flight and let a duplicate invite through. Pinning makes the wire name, the
lock key and the routed queue byte-identical to the pre-move ones.
`tests/unit/app/test_task_name_stability.py` freezes that; do not remove a `name=` to "tidy up".

The module imports NOTHING from `run_automation` — that is what keeps the dependency one-way, since
`run_automation` imports two of these tasks back. Three names had to be re-sourced to achieve it:
`grade_zero_walk` from `utilities.linkedin.zero_walk`, `profile_slug` from `utilities.lead_scoring`,
and `strip_non_bmp`, which moved to `utilities.linkedin_formatter` alongside the
`normalize_public_text` it wraps.
"""

import re
import time

from selenium.webdriver.common.by import By

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement
from cqc_lem.utilities.db import (
    ALREADY_CONNECTED_MESSAGE,
    CONNECT_NOTE_MAX_CHARS,
    CONNECTION_REQUEST_SENT_MESSAGE,
    INVITE_NOT_SENT_MESSAGE,
    NO_CONNECT_BUTTON_MESSAGE,
    ConnectStatus,
    LogActionType,
    LogResultType,
    get_engagement_preferences,
    get_user_password_pair_by_id,
    insert_new_log,
    set_target_connect_status,
)
from cqc_lem.utilities.human_pacing import ACTION_INVITE, record_action
from cqc_lem.utilities.lead_scoring import profile_slug
from cqc_lem.utilities.linkedin.company_page_inviter import (
    INVITE_STATUS_DISABLED,
    INVITE_STATUS_FAILED,
    INVITE_STATUS_PAUSED,
    INVITE_STATUS_SESSION_FAILED,
    automate_invitations,
    plan_daily_invites,
)
from cqc_lem.utilities.linkedin.helper import is_first_degree, login_to_linkedin
from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited, is_automation_paused
from cqc_lem.utilities.linkedin.stale_invites import (
    WITHDRAW_STATUS_DISABLED,
    WITHDRAW_STATUS_FAILED,
    WITHDRAW_STATUS_PAUSED,
    WITHDRAW_STATUS_SESSION_FAILED,
    plan_withdrawals,
    withdraw_stale_invites,
)
from cqc_lem.utilities.linkedin.zero_walk import grade_zero_walk
from cqc_lem.utilities.linkedin_formatter import strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.observability import track_company_page_invite_run, track_stale_invite_run
from cqc_lem.utilities.selenium_util import (
    click_element_wait_retry,
    click_first,
    find_first,
    get_driver_wait_pair,
    quit_gracefully,
)


@shared_task.task(name='cqc_lem.app.run_automation.clean_stale_invites',
                  bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='2/m', queue='se_outreach')
def clean_stale_invites(self, user_id: int):
    """Withdraw this user's pending connection invites older than the threshold (issue #969).

    The budget is decided BEFORE a browser session is opened: most days are paced to zero even with
    the lane on (`STALE_INVITE_WITHDRAWAL_ENABLED`, default true since #1006) — a Chrome slot spent
    discovering that is a slot an engagement lane needed. Returns the run report so a Flower run and
    the `stale_invite_run` event tell the same story.
    """
    task_name = "clean_stale_invites"

    plan = plan_withdrawals(user_id)
    if plan["allowance"] <= 0:
        report = {"status": plan["status"], "cap": plan["cap"],
                  "withdrawn_today": plan["withdrawn_today"],
                  "threshold_days": plan["threshold_days"]}
        # DEBUG for the switched-off case: someone deliberately set the switch (or a zero cap /
        # threshold), it repeats for every active user every night, and it is working behaviour —
        # an INFO line here is one row per user per day saying nothing happened on purpose.
        emit = log_debug if plan["status"] == WITHDRAW_STATUS_DISABLED else log_info
        emit(f"Stale-invite withdrawal skipped — {plan['status']} "
             f"(cap {plan['cap']}, withdrawn today {plan['withdrawn_today']})",
             user_id=user_id, task_name=task_name, action_type="invite")
        track_stale_invite_run(user_id, report)
        return report

    # Session acquisition is INSIDE the reporting path: "the browser never came up" and "LinkedIn's
    # markup moved" need different fixes, and an escaping exception would emit nothing at all —
    # indistinguishable from a day paced down to zero.
    try:
        driver, wait = get_driver_wait_pair(session_name='Withdraw Stale Invites', user_id=user_id)
    except Exception as e:
        log_error("Could not start a browser session to withdraw stale invites", exc=e,
                  user_id=user_id, task_name=task_name, action_type="invite")
        report = {"status": WITHDRAW_STATUS_SESSION_FAILED, "cap": plan["cap"],
                  "withdrawn_today": plan["withdrawn_today"],
                  "threshold_days": plan["threshold_days"]}
        track_stale_invite_run(user_id, report)
        return report

    try:
        user_email, user_password = get_user_password_pair_by_id(user_id)
        login_to_linkedin(driver, wait, user_email, user_password)
        report = withdraw_stale_invites(driver, wait, user_id, plan=plan)
    except LinkedInRateLimited as e:
        # The breaker opened between the plan and the page. Not a failure of this lane — defer.
        # DEBUG for the same reason send_roster_connect_invite's copy of this is: an open breaker
        # is working behaviour, it is reported where it is DETECTED (rate_limit.mark_rate_limited),
        # and this lane retries on the next rotation by design.
        log_debug(f"clean_stale_invites deferred (throttled): {e}", user_id=user_id,
                  task_name=task_name, action_type="invite")
        report = {"status": WITHDRAW_STATUS_PAUSED, "cap": plan["cap"],
                  "withdrawn_today": plan["withdrawn_today"],
                  "threshold_days": plan["threshold_days"]}
    except Exception as e:
        log_error("Error while withdrawing stale invites", exc=e, user_id=user_id,
                  task_name=task_name, action_type="invite")
        report = {"status": WITHDRAW_STATUS_FAILED, "cap": plan["cap"],
                  "withdrawn_today": plan["withdrawn_today"],
                  "threshold_days": plan["threshold_days"]}
    finally:
        quit_gracefully(driver)

    track_stale_invite_run(user_id, report)
    log_info(f"Withdrew {report.get('withdrawn') or 0} stale invite(s) ({report.get('status')})",
             user_id=user_id, task_name=task_name, action_type="invite")
    return report


# The connection-degree badge on a profile page. `span.dist-value` / `span.distance-badge` are
# CLASS anchors, and class anchors are gone from the SDUI profile — both were confirmed dead on
# 2026-08-03, which left this read (and, through `profile.is_1st_connection`, the whole
# profile-viewer 1st-vs-other branch) blind. The chain now leads with what the page still WRITES:
# the badge is its own leaf node whose entire text is the degree. Class anchors stay last, as a
# legacy tail that costs nothing if a pre-SDUI layout is ever served (issues #623, #1021).
#
# The two text shapes are ONE union expression, not two locators, because a union comes back in
# DOCUMENT order — and document order is the only thing that attributes a badge to THIS profile.
# `<main>` carries other people's badges too (mutual-connection highlights, "More profiles for
# you"), so the top card's badge is the FIRST one and every later one names a different entity —
# the #1012 rule read backwards. Two separate locators would let a highlight's bare "1st" outrank
# the top card's "2nd degree connection" purely because its locator came first in the list.
_DEGREE_TOKENS = ("1st", "2nd", "3rd", "3rd+")
_DEGREE_LEAF_XPATH = (
    "//main//*[self::span or self::div or self::li or self::p][not(*)]["
    + " or ".join(f"normalize-space()='{t}' or normalize-space()='· {t}'" for t in _DEGREE_TOKENS)
    + "]"
    " | //main//*[not(*)][contains(normalize-space(),'degree connection')]")
_PROFILE_DEGREE_LOCATORS = [
    (By.XPATH, _DEGREE_LEAF_XPATH),
    (By.CSS_SELECTOR, "main span.dist-value"),
    (By.CSS_SELECTOR, "main span.distance-badge"),
    (By.XPATH, "//main//span[contains(@class,'distance-badge')]"),
]

# The cross-check for a chain that matched NOTHING: the page's own degree line, read out of the
# rendered text rather than through a locator. Whole-line on purpose — a loose `\b1st\b` would
# fire on a headline ("1st place, 2026 awards") and warn on a healthy profile forever.
_DEGREE_LINE_RE = re.compile(r"^(?:·\s*)?(1st|2nd|3rd)\+?(?:\s+degree(?:\s+connection)?)?$",
                             re.IGNORECASE)


def _degree_lines_on_page(driver) -> "int | None":
    """How many degree-badge LINES the page renders, or None when the read itself failed."""
    try:
        text = driver.find_element(By.TAG_NAME, "main").text or ""
    except Exception:
        return None
    return sum(1 for line in text.splitlines() if _DEGREE_LINE_RE.match(line.strip()))


def _degree_badge_texts(driver) -> "list[str] | None":
    """Every degree-badge text the locator chain can READ, in chain-then-document order, or None
    when the read itself failed. None and [] are different answers: an unreadable page grounds
    nothing, an empty chain on a readable page is the zero worth cross-checking. A matched node with
    no text counts as neither — a locator that resolves to an empty element is as blind as one that
    resolves to nothing.
    """
    texts: "list[str]" = []
    try:
        for by, selector in _PROFILE_DEGREE_LOCATORS:
            for element in driver.find_elements(by, selector):
                if (element.text or "").strip():
                    texts.append(element.text)
    except Exception as e:
        log_warning("Could not read the connection-degree badge; attempting the invite anyway",
                    exc=e, action_type="invite_connect")
        return None
    return texts


def _profile_is_first_degree(driver) -> bool:
    """True only when THIS profile's own badge says 1st degree. Fails open (False) on any read
    problem — a missed badge just means we try the invite, which is the old behaviour. A chain that
    matched NO badge at all is cross-checked against the page's own degree line first, so the blind
    read that #1012 paid for cannot recur silently (issue #1021).

    Only the FIRST badge is judged, never "any of them": the top card is the first thing under
    <main>, and every badge below it — a mutual-connection highlight, a "More profiles for you"
    card — belongs to somebody else. Reading those would abort the invite to a 2nd-degree target
    just because one of their mutuals is a 1st, which is #1012's mistake in a read instead of a
    click.
    """
    texts = _degree_badge_texts(driver)
    if texts is None:
        return False
    if texts:
        return is_first_degree(texts[0])
    grade_zero_walk(_degree_lines_on_page(driver), "Profile degree-badge chain",
                     action_type="invite_connect")
    return False


# Grounded live 2026-08-03 (docs/sdui-selenium-notes.md): the profile top card carries NO
# "Invite <name> to connect" button on the current layout — the only buttons matching
# //main//button[contains(@aria-label,"Invite ")] are the "More profiles for you" rail, so an
# unscoped click INVITED A RANDOM SUGGESTED PERSON and then failed on the missing Send dialog.
# The top-card More menu's Connect item is an <a role=menuitem> linking to /preload/custom-invite,
# which means the invite dialog is addressable by URL — that hazard-free route leads, and no
# locator here may ever click an Invite button that names someone other than the target.
_CONNECT_INVITE_URL = "https://www.linkedin.com/preload/custom-invite/?vanityName={slug}"

# The dialog's own controls, unchanged across the rotation: its presence — not a click having
# landed — is what proves the invite flow is actually open for the TARGET.
_CONNECT_DIALOG_LOCATORS = [
    (By.XPATH, '//button[@aria-label="Send without a note"]'),
    (By.XPATH, '//button[@aria-label="Add a note"]'),
]

_PROFILE_MORE_MENU_LOCATORS = [
    (By.XPATH, '//main//button[@aria-label="More" or normalize-space()="More"]'),
    (By.XPATH, '//main//button[contains(@aria-label,"More actions")]'),  # pre-2026 label
]

_CONNECT_MENU_ITEM_LOCATORS = [
    (By.XPATH, '//a[@role="menuitem"][contains(@href,"custom-invite")]'),
    (By.XPATH, '//*[@role="menuitem"][normalize-space()="Connect"]'),
]

# Issue #1734: user reports (and the 2026-08-03 grounding above was ONE profile's rotation, not
# every one) show LinkedIn placing Connect two different ways depending on the target — a bare
# button directly on the top card for some profiles, buried in the More menu for others. The URL
# and More-menu routes below miss the direct-button case entirely, so a target whose page renders
# it that way had no route to the dialog at all. The direct button's own aria-label, when it has
# one, is the BARE word "Connect" — never "Invite <name> to connect".
#
# Matching on visible text alone ("Connect") is NOT enough to close the #1012 wrong-person hazard
# here: the 2026-08-03 grounding only proved the "More profiles for you" rail's aria-label names
# the suggested person (`Invite <name> to connect`) — it never grounded that rail button's VISIBLE
# text also carries the name, and a short "Connect" label with a longer accessible name is a common
# pattern. So the aria-label exclusion below is load-bearing, not decoration: a button whose own
# aria-label names someone ("Invite ...") is excluded even when its bare visible text reads
# "Connect".
_PROFILE_CONNECT_BUTTON_LOCATORS = [
    (By.XPATH, '//main//button[(normalize-space()="Connect" or @aria-label="Connect") '
               'and not(starts-with(@aria-label,"Invite"))]'),
]


def _connect_dialog_present(driver, wait, user_id: int) -> bool:
    return find_first(driver, wait, _CONNECT_DIALOG_LOCATORS, "Connect invite dialog",
                      required=False, warn_on_miss=False, max_try=1, visible_only=True,
                      user_id=user_id) is not None


def _open_connect_invite_dialog(driver, wait, user_id: int, profile_url: str) -> bool:
    """Open the Connect invite dialog for the profile at `profile_url` — the custom-invite URL
    first, then the profile page's own top-card Connect button when it is directly visible, else
    the top-card More menu (issue #1734: LinkedIn renders one or the other depending on the
    target). True only when the dialog's own controls are provably present. False is an ordinary
    outcome (invite already pending, Connect not offered, or the SDUI rotated again) and is why
    the total miss is a WARNING, not an error (issue #571).
    """
    slug = profile_slug(profile_url)
    if slug:
        driver.get(_CONNECT_INVITE_URL.format(slug=slug))
        if _connect_dialog_present(driver, wait, user_id):
            log_info("Connect dialog opened via the custom-invite URL")
            return True
        # An already-pending invite renders no dialog here — an expected outcome for this
        # route, so the miss stays quiet and the profile-page route gets its turn.
        log_debug("custom-invite page did not render the Connect dialog — trying the "
                  "profile page directly", user_id=user_id, action_type="invite_connect")

    if profile_url != driver.current_url:
        driver.get(profile_url)

    if click_first(driver, wait, _PROFILE_CONNECT_BUTTON_LOCATORS, "Profile Connect button",
                   required=False, warn_on_miss=False, max_try=1, use_action_chain=True,
                   user_id=user_id) is not None:
        if _connect_dialog_present(driver, wait, user_id):
            log_info("Connect dialog opened via the profile page's direct Connect button")
            return True

    if click_first(driver, wait, _PROFILE_MORE_MENU_LOCATORS, "Profile More menu",
                   required=False, warn_on_miss=False, max_try=1, use_action_chain=True,
                   user_id=user_id) is not None:
        item = click_first(driver, wait, _CONNECT_MENU_ITEM_LOCATORS, "Connect menu item",
                           required=False, warn_on_miss=False, max_try=1, use_action_chain=True,
                           user_id=user_id)
        if item is not None and _connect_dialog_present(driver, wait, user_id):
            log_info("Connect dialog opened via the profile More menu")
            return True

    log_warning("No route opened the Connect invite dialog for this profile",
                user_id=user_id, action_type="invite_connect")
    return False


# The bare-send control is the dialog's own word for "no note is on offer here": a quota-spent
# dialog renders it and NO Add-a-note (both labels live-grounded 2026-08-03,
# docs/sdui-selenium-notes.md). Same `contains` form _submit_connect_invite clicks, so the
# cross-check below can never disagree with the send that immediately follows it.
_SEND_WITHOUT_NOTE_XPATH = '//button[contains(@aria-label,"Send without a note")]'
_CONNECT_BARE_SEND_LOCATORS = [(By.XPATH, _SEND_WITHOUT_NOTE_XPATH)]

_CONNECT_NOTE_BUTTON_LOCATORS = [
    (By.XPATH, '//button[contains(@aria-label,"Add a note")]'),
    (By.XPATH, '//button[normalize-space()="Add a note"]'),
]


def _add_connect_note(driver, wait, message: str, user_id: int) -> bool:
    """Type a personalized note into an OPEN Connect dialog. Best-effort by design (issue #573):
    LinkedIn hides the note affordance once a free account's personalized-invite quota is spent, and
    a note that can't be attached must not cost us the invite — the caller sends it bare instead.
    The note is stripped of non-BMP characters first because ChromeDriver's send_keys raises on the
    emoji an AI-written note routinely carries.

    A MISSING affordance is not a failure at all (issue #1039). It is the quota-spent no-op this
    docstring already describes, the fallback is in hand, and warning on it filed a fingerprinted
    defect for working behaviour once per lost note — so it is DEBUG, and the bare-send control the
    dialog still shows is what says which no-op it was. A step that fails AFTER the affordance
    answered is a genuine degraded path and still warns with `exc=`.
    """
    if find_first(driver, wait, _CONNECT_NOTE_BUTTON_LOCATORS, "Add a note button",
                  required=False, warn_on_miss=False, max_try=1, visible_only=True,
                  user_id=user_id) is None:
        if find_first(driver, wait, _CONNECT_BARE_SEND_LOCATORS, "Send without a note",
                      required=False, warn_on_miss=False, max_try=1, visible_only=True,
                      user_id=user_id) is not None:
            log_debug("Connect dialog offers no Add-a-note affordance (personalized-invite quota "
                      "spent) — sending the invite bare", user_id=user_id,
                      action_type="invite_connect")
        else:
            # Not the quota case: the dialog proven open moments ago no longer shows either of its
            # controls. This is DEBUG too, because the invite it costs us is the ONE thing
            # _submit_connect_invite is about to log as an error — warning here would file a second
            # grouped issue for that same lost invite, which is exactly what #1038 fixed.
            log_debug("Connect dialog is no longer showing its own controls; attaching no note",
                      user_id=user_id, action_type="invite_connect")
        return False

    try:
        # required=True: the affordance answered a moment ago, so failing to click it now is a real
        # degraded path — it raises into the warning below rather than reading as "not on offer".
        click_first(driver, wait, _CONNECT_NOTE_BUTTON_LOCATORS, "Add a note button",
                    max_try=1, use_action_chain=True, user_id=user_id)

        message_box = click_element_wait_retry(driver, wait, '//textarea[@id="custom-message"]',
                                               "Finding Message Box", max_retry=1, use_action_chain=True)
        message_box.clear()

        for _ in range(3):
            if len(message) > CONNECT_NOTE_MAX_CHARS:
                message = get_ai_message_refinement(message, CONNECT_NOTE_MAX_CHARS)
            else:
                break

        message_box.send_keys(strip_non_bmp(message)[:CONNECT_NOTE_MAX_CHARS])

        # The Send button only enables once the textarea has registered input.
        time.sleep(2)
        log_info("Added note to the connection request")
        return True
    except Exception as e:
        log_warning("Could not attach a note to the connection request; sending it without one",
                    exc=e, user_id=user_id, action_type="invite_connect")
        return False


# The Send button's aria-label differs between the bare dialog and the one carrying a note, and a
# note attempt can leave the dialog in either state — so both are tried, preferred label first.
_SEND_INVITE_XPATHS = ('//button[contains(@aria-label,"Send invitation")]',
                       _SEND_WITHOUT_NOTE_XPATH)


def _submit_connect_invite(driver, wait, user_id: int, with_note: bool) -> bool:
    """Click Send on the open Connect dialog. False only when NEITHER Send affordance is clickable,
    which loses the invite outright — that one stays an error (issue #573).
    """
    xpaths = _SEND_INVITE_XPATHS if with_note else tuple(reversed(_SEND_INVITE_XPATHS))
    last_error: Exception = None
    for xpath in xpaths:
        try:
            click_element_wait_retry(driver, wait, xpath, "Finding Send Connection Button",
                                     max_retry=1, use_action_chain=True)
            log_info("Found Send Connection Button and clicked it")
            return True
        except Exception as e:
            last_error = e  # wrong label for this dialog state — try the other one

    # exc= is what turns this into a fingerprinted PostHog issue (the loop that filed #573); an
    # exc-less log_error only reaches Logs, so the one failure we deliberately keep as an error
    # would never page anyone.
    log_error("Failed to send the connection request (no Send button on the open Connect dialog)",
              exc=last_error, user_id=user_id, action_type="invite_connect")
    return False


def invite_to_connect_now(user_id: int, profile_url: str, message: str = None) -> "tuple[bool, str]":
    """Core connect-invite send: open the profile, click Connect (+ optional note), log the result.
    Returns (sent, result_message) — the message is the failure reason when `sent` is False, which
    the proactive flow stores on the request row (issue #623). Shared by invite_to_connect (reactive
    profile-viewer flow) and send_connection_request (issue #398 approval-gated proactive flow) so
    both use the same send + log path (mirrors send_dm_now). Re-raises LinkedInRateLimited when the
    kill-switch / 429 breaker is open so callers can defer rather than record a false failure.
    """
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Invite to Connect', user_id=user_id)

    invite_sent = False

    try:

        login_to_linkedin(driver, wait, user_email, user_password)

        if profile_url != driver.current_url:
            # Open the profile URL
            driver.get(profile_url)

        log_info(f"Inviting to connect: {profile_url}")

        # Already connected? There is no Connect button to find — bail with a reason instead of
        # burning a session hunting for one and recording an opaque failure.
        if _profile_is_first_degree(driver):
            log_info("Skipping invite: already a 1st-degree connection", user_id=user_id,
                     action_type="invite_connect")
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.FAILURE, post_url=profile_url,
                           message=ALREADY_CONNECTED_MESSAGE)
            return False, ALREADY_CONNECTED_MESSAGE

        # Open the Connect dialog. With none open the note/send steps below can only fail, and
        # their errors would bury the real reason — so stop here with a named one instead.
        if not _open_connect_invite_dialog(driver, wait, user_id, profile_url):
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.FAILURE, post_url=profile_url,
                           message=NO_CONNECT_BUTTON_MESSAGE)
            return False, NO_CONNECT_BUTTON_MESSAGE

        # The note is an optional extra on an invite we already decided to send — a missing note
        # affordance must not abandon an open Connect dialog, so the invite goes out bare (#573).
        noted = bool(message) and _add_connect_note(driver, wait, message, user_id)

        result = (CONNECTION_REQUEST_SENT_MESSAGE
                  if _submit_connect_invite(driver, wait, user_id, with_note=noted)
                  else INVITE_NOT_SENT_MESSAGE)
    except LinkedInRateLimited:
        # Kill-switch / 429 breaker is open — let the caller defer instead of logging a false failure.
        raise
    except Exception as e:
        log_error("Error while inviting to connect", exc=e, user_id=user_id, action_type="invite_connect")
        result = f"Error while inviting to connect: {e}"
        insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                       result=LogResultType.FAILURE, post_url=profile_url, message=str(e))
    else:
        invite_sent = result == CONNECTION_REQUEST_SENT_MESSAGE
        insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                       result=LogResultType.SUCCESS if invite_sent else LogResultType.FAILURE,
                       post_url=profile_url, message=result)
        if invite_sent:
            record_action(user_id, ACTION_INVITE)  # account-level governor (issue #626)
    finally:
        quit_gracefully(driver)  # Close the driver

    return invite_sent, result


@shared_task.task(name='cqc_lem.app.run_automation.invite_to_connect',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'profile_url']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def invite_to_connect(self, user_id: int, profile_url: str, message: str = None):
    """Send a LinkedIn connection request (reactive profile-viewer flow). Thin wrapper over
    invite_to_connect_now; a throttle / kill-switch defers silently.
    """
    try:
        sent, reason = invite_to_connect_now(user_id, profile_url, message)
    except LinkedInRateLimited as e:
        # DEBUG, matching send_roster_connect_invite: an open breaker is working behaviour and is
        # already reported where it is detected.
        log_debug(f"invite_to_connect deferred (throttled): {e}", user_id=user_id,
                  action_type="invite_connect", task_name="invite_to_connect")
        return "Invitation deferred (LinkedIn throttled)"
    if sent:
        return CONNECTION_REQUEST_SENT_MESSAGE
    # DEBUG, not a warning: every reason invite_to_connect_now returns has ALREADY been logged by
    # the step that owns it — at ERROR with exc= for a dialog with no Send button, WARNING for no
    # route to the dialog, INFO for an existing connection. Restating it here forked a SECOND
    # grouped $exception (and a second auto-filed issue) for one lost invite (#1038 / #1042).
    log_debug(f"Connection request failed: {reason}", user_id=user_id, action_type="invite_connect",
              task_name="invite_to_connect")
    return f"Connection Request Failed: {reason}"


@shared_task.task(name='cqc_lem.app.run_automation.send_roster_connect_invite',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'profile_url']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def send_roster_connect_invite(self, user_id: int, profile_url: str, message: str = None):
    """Send the roster ladder's ONE connection request for a target (issue #979).

    A thin wrapper over the SAME `invite_to_connect_now` rail the reactive and proactive flows use —
    it exists only to write the outcome back onto the roster row, so the badge tells the truth about
    what happened. It runs on `se_outreach` rather than inline in the roster pass because the rail
    opens its OWN Chrome session, and a second session inside the roster pass's would take a slot
    out of the pool the Selenium lanes share.

    The status is already 'requested' before this runs (the one-shot guarantee). Only two things
    move it: a send that could not happen at all — throttled, nothing reached LinkedIn — hands the
    target back to the ladder, and a real failure is terminal ('failed'), never auto-retried.
    """
    try:
        sent, reason = invite_to_connect_now(user_id, profile_url, message)
    except LinkedInRateLimited as e:
        # Nothing went out, so the one shot was not spent. DEBUG, not a warning: an open breaker is
        # working behaviour and this lane retries on the next rotation by design.
        log_debug(f"Roster connect invite deferred (throttled): {e}", user_id=user_id,
                  action_type="invite_connect", task_name="send_roster_connect_invite")
        set_target_connect_status(user_id, profile_url, ConnectStatus.NEEDS_CONNECTION)
        return "Roster connection request deferred (LinkedIn throttled)"
    if sent:
        return CONNECTION_REQUEST_SENT_MESSAGE
    if reason == ALREADY_CONNECTED_MESSAGE:
        # Not a failure — the ladder's goal was already met, so record the truth rather than badging
        # a connected account as a failed invite.
        set_target_connect_status(user_id, profile_url, ConnectStatus.CONNECTED)
        return ALREADY_CONNECTED_MESSAGE
    set_target_connect_status(user_id, profile_url, ConnectStatus.FAILED)
    # The 'failed' badge is the record that matters here; the reason itself was already logged by
    # the step that owns it, so re-warning would double-count it into a second issue (#1038).
    log_debug(f"Roster connection request failed: {reason}", user_id=user_id,
              action_type="invite_connect", task_name="send_roster_connect_invite")
    return f"Roster connection request failed: {reason}"


@shared_task.task(name='cqc_lem.app.run_automation.send_connection_request',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['request_id']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def send_connection_request(self, request_id: int):
    """Send an approved proactive connection request (issue #398). Enforces the per-day invite cap at
    send time (defers back to 'approved' for the next scan when the cap is hit or LinkedIn is
    throttled) and updates the connection_requests status. Reuses invite_to_connect_now, so it
    honors the rate-limit / kill-switch.
    """
    from cqc_lem.utilities.db import (
        ConnectionRequestStatus,
        count_invites_sent_today,
        get_connection_request,
        update_connection_request_status,
    )
    req = get_connection_request(request_id)
    if not req or req["status"] not in (ConnectionRequestStatus.APPROVED, ConnectionRequestStatus.SENDING):
        return f"Connection request {request_id} not sendable (status={req['status'] if req else 'missing'})"

    user_id = req["user_id"]
    prefs = get_engagement_preferences(user_id)
    if count_invites_sent_today(user_id) >= int(prefs.get("max_invites_per_day") or 0):
        log_info(f"send_connection_request: daily invite cap reached for user {user_id}; deferring {request_id}")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (daily invite cap reached)"

    try:
        sent, reason = invite_to_connect_now(user_id, req["recipient_profile_url"], req["message"])
    except LinkedInRateLimited as e:
        # DEBUG, matching the other two wrappers: the request stays 'approved' and the next scan
        # picks it up, so nothing was lost and nothing here is new information.
        log_debug(f"send_connection_request: throttled, deferring {request_id}: {e}",
                  user_id=user_id, action_type="invite_connect",
                  task_name="send_connection_request")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (LinkedIn throttled)"
    if not sent:
        # The reason is stored on the request row below and was already logged at its owning step;
        # a warning here would only fork a second grouped issue for the same invite (#1038).
        log_debug(f"Connection request {request_id} failed: {reason}", user_id=user_id,
                  action_type="invite_connect", task_name="send_connection_request")
    update_connection_request_status(
        request_id, ConnectionRequestStatus.SENT if sent else ConnectionRequestStatus.FAILED,
        failure_reason=(None if sent else reason))
    return f"Connection request {request_id} -> {'sent' if sent else 'failed'}"


@shared_task.task(name='cqc_lem.app.run_automation.automate_invites_to_company_page_for_user',
                  bind=True, base=QueueOnce, once={'graceful': False}, reject_on_worker_lost=True,
                  rate_limit='4/m', queue='se_outreach')
def automate_invites_to_company_page_for_user(self, user_id: int):
    """Send this user's paced daily drip of company-page invites (issue #732).

    The budget is decided BEFORE a browser session is opened: on most days the allowance is zero
    (rest day, budget already spent, single-digit cap already reached) and a Chrome slot spent to
    discover that is a slot an engagement lane needed. A paused account stands down here too — page
    invites are discretionary amplification, never a response owed to someone.
    """
    task_name = "automate_invites_to_company_page_for_user"

    if is_automation_paused():
        report = {"status": INVITE_STATUS_PAUSED}
        log_info("Company page invites skipped — automation paused", user_id=user_id,
                 task_name=task_name, action_type="company_invite")
        track_company_page_invite_run(user_id, report)
        return "Company page invites skipped — automation paused"

    plan = plan_daily_invites(user_id)
    if plan["allowance"] <= 0:
        report = {"status": plan["status"], "cap": plan["cap"], "sent_today": plan["sent_today"]}
        # Same split clean_stale_invites makes: DEBUG for the switched-off case, which is the
        # DEFAULT for any user with no company page and repeats for every active user every day.
        emit = log_debug if plan["status"] == INVITE_STATUS_DISABLED else log_info
        emit(f"Company page invites skipped — {plan['status']} "
             f"(cap {plan['cap']}, sent today {plan['sent_today']})",
             user_id=user_id, task_name=task_name, action_type="company_invite")
        track_company_page_invite_run(user_id, report)
        return f"No company page invites to send ({plan['status']})"

    # Session acquisition is INSIDE the reporting path: a run that could not start a browser is the
    # one failure mode `company_page_invite_run` exists to make visible, and letting the exception
    # escape here would emit nothing at all — indistinguishable from a day paced down to zero.
    try:
        driver, wait = get_driver_wait_pair(session_name='Company Page Invites', user_id=user_id)
    except Exception as e:
        log_error("Could not start a browser session for company page invites", exc=e,
                  user_id=user_id, task_name=task_name, action_type="company_invite")
        track_company_page_invite_run(user_id, {"status": INVITE_STATUS_SESSION_FAILED,
                                                "cap": plan["cap"], "sent_today": plan["sent_today"]})
        return "Company page invites skipped — browser session unavailable"

    try:
        report = automate_invitations(driver, wait, user_id, plan=plan)
    except Exception as e:
        log_error("Error while inviting to company page", exc=e, user_id=user_id,
                  task_name=task_name, action_type="company_invite")
        report = {"status": INVITE_STATUS_FAILED, "cap": plan["cap"], "sent_today": plan["sent_today"]}
    finally:
        quit_gracefully(driver)

    track_company_page_invite_run(user_id, report)
    result = (f"Invited {report.get('invites_sent') or 0} people to the company page "
              f"({report.get('status')}).")
    log_info(result, user_id=user_id, task_name=task_name, action_type="company_invite")
    return result
