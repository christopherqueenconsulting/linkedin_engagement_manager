"""Company-page invitations — a paced daily drip, not a monthly credit blast (issue #732).

A LinkedIn Page spends a MONTHLY invitation-credit pool that renews on the 1st and is refunded when
an invite is ACCEPTED (up to 72h later). This lane used to fire once a month and deliberately drain
that pool in one sitting — it selected as many invitees as there were credits and then recursed into
itself until there were none left. Dozens-to-hundreds of identical actions inside one narrow window
is the loudest velocity signal in the product, and it was the one outbound path that consulted
neither `max_invites_per_day` nor the #626 pacing engine.

Three ceilings now bound a run, and the smallest wins:

1. **The user's cap** — `min(max_company_page_invites_per_day, max_invites_per_day)`, so the
   brand-account phase policy (which already clamps `max_invites_per_day`) governs the brand user
   with no second ceiling to keep in step. That cap goes through `human_pacing` like every other
   outbound lane: a stable 40-100% daily draw, weekend asymmetry, rest days, and the shared account
   envelope, minus what today's log rows say was already spent. The draw uses its OWN lane key
   (`ACTION_COMPANY_INVITE`) — a budget drawn against `ACTION_INVITE` would be the stored budget the
   connection-request lane reads back, clamping it to this smaller cap for the rest of the day.
2. **The credit spread** — `credits_remaining / days_left_in_month`, so the pool lasts the month
   instead of being front-loaded. Acceptances refund credits, so a drip can reach MORE people per
   month than a blast can.
3. **The credits themselves** — a hard stop at 0, read live off the page.

`automate_invitations` sends at most that budget, in ONE pass. The recursion is gone.
"""

import calendar
import random
import re
import time
from datetime import date, datetime, timezone
from typing import Optional

from selenium.common import TimeoutException, WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from cqc_lem.utilities.db import (
    COMPANY_PAGE_INVITE_SENT_MESSAGE,
    COMPANY_PAGE_INVITES_PER_DAY_DEFAULT,
    LogActionType,
    LogResultType,
    count_company_page_invites_sent_today,
    get_company_linked_in_url_for_user,
    get_engagement_preferences,
    get_user_password_pair_by_id,
    insert_new_log,
)
from cqc_lem.utilities.human_pacing import (
    ACTION_COMPANY_INVITE,
    ACTION_INVITE,
    engagement_caps_from_prefs,
    pacing_enabled,
    record_action,
    remaining_actions,
)
from cqc_lem.utilities.linkedin.helper import login_to_linkedin
from cqc_lem.utilities.linkedin.zero_walk import report_zero_walk
from cqc_lem.utilities.logger import log_info
from cqc_lem.utilities.selenium_util import (
    click_element_wait_retry,
    get_element_wait_retry,
    get_elements_as_list_wait_stale,
    getText,
    wait_for_ajax,
)

# Run statuses — stable strings, so "why did this account send nothing yesterday?" is a group-by on
# the telemetry rather than a log grep.
INVITE_STATUS_SENT = "sent"
INVITE_STATUS_BUDGET_REACHED = "budget_reached"
INVITE_STATUS_DISABLED = "disabled"           # the user's cap (or max_invites_per_day) is 0
INVITE_STATUS_CREDITS_EXHAUSTED = "credits_exhausted"
INVITE_STATUS_NO_CANDIDATES = "no_candidates"
# Zero invitees while the modal still renders invitee rows — the row/checkbox XPaths rotated. Kept
# apart from `no_candidates` for the reason `session_failed` is kept apart from `failed`: "everyone
# is already invited" is a quiet day and "we cannot see the list" is a defect, and a run that
# reported both as `no_candidates` made the second one invisible (issue #1021).
INVITE_STATUS_DRIFT = "drift"
INVITE_STATUS_NO_PAGE = "no_page"
INVITE_STATUS_FAILED = "failed"
INVITE_STATUS_PAUSED = "paused"
# A click reached LinkedIn but its outcome could not be confirmed. Kept apart from `sent` and `failed`
# because the budget must not be spent on an unconfirmed click, yet the run is not a missing-button
# failure either. Issue #1102.
INVITE_STATUS_UNCONFIRMED = "unconfirmed"
# No Chrome session could be started (grid full, container restart). Kept apart from `failed` for the
# same reason the golden-hour sweep does it: "the browser never came up" and "LinkedIn's UI moved"
# need different fixes, and a run that emitted nothing at all would read as paced-to-zero.
INVITE_STATUS_SESSION_FAILED = "session_failed"

# Hand-selecting invitees is not instant. A short randomized pause between checkbox clicks keeps a
# batch from being N identical machine-timed clicks; the budget is single digits, so the total added
# time stays well inside the task's own runtime (and inside MAX_INLINE_SLEEP_SECONDS).
SELECTION_PAUSE_MIN_SECONDS = 0.4
SELECTION_PAUSE_MAX_SECONDS = 2.5


def days_left_in_month(day: Optional[date] = None) -> int:
    """Days remaining in `day`'s month, counting today. The credit pool renews on the 1st, so this
    is the horizon the remaining credits have to cover.
    """
    day = day or datetime.now(timezone.utc).date()
    return calendar.monthrange(day.year, day.month)[1] - day.day + 1


def credit_spread_budget(credits_remaining: int, day: Optional[date] = None) -> int:
    """How many of the remaining monthly credits today may spend, so the pool lasts the month.

    Floor division of credits over the days left, with a floor of 1 while any credit remains — a
    thin pool late in the month should still drip rather than stop entirely (the daily cap and the
    credit count are the real ceilings above this). 0 credits is 0, never 1.
    """
    credits = max(0, int(credits_remaining or 0))
    if credits <= 0:
        return 0
    return max(1, credits // max(1, days_left_in_month(day)))


def invite_cap_for_user(prefs: Optional[dict]) -> int:
    """The lane's own per-day ceiling: its cap, bounded by the account-wide invite cap.

    `max_invites_per_day` is the harder bound on purpose — it is what `brand_account`'s launch-phase
    policy clamps, so the brand account can never run page invites hotter than its phase allows.
    """
    prefs = prefs or {}

    def _cap(key: str, default: int) -> int:
        try:
            value = prefs.get(key)
            return max(0, int(default if value is None else value))
        except (TypeError, ValueError):
            return default

    return min(_cap("max_company_page_invites_per_day", COMPANY_PAGE_INVITES_PER_DAY_DEFAULT),
               _cap("max_invites_per_day", 0))


def plan_daily_invites(user_id: int, prefs: Optional[dict] = None) -> dict:
    """Today's paced allowance for this user, decided BEFORE any browser session is opened.

    Returns `{'allowance': int, 'status': str, 'cap': int, 'sent_today': int}`. A zero allowance is
    the common case on most days (rest day, budget already spent, cap of 0) and must not cost a
    Chrome slot, which is why this is separate from the Selenium half. `status` is only meaningful
    when the allowance is 0 — it says WHY nothing may go out.
    """
    prefs = prefs if prefs is not None else get_engagement_preferences(user_id)
    cap = invite_cap_for_user(prefs)
    if cap <= 0:  # lane switched off — don't spend a DB round-trip proving it
        return {"allowance": 0, "status": INVITE_STATUS_DISABLED, "cap": 0, "sent_today": 0}
    sent_today = count_company_page_invites_sent_today(user_id)
    # ACTION_COMPANY_INVITE, not ACTION_INVITE: `daily_budget` stores its draw under the action name,
    # so drawing this small cap against the connection lane's key would clamp whichever lane ran
    # second to the other's cap. The `caps` envelope is still the shared one — a page invite spends
    # the account's outbound allowance, it just no longer redefines the connection lane's budget.
    allowance = max(0, remaining_actions(user_id, ACTION_COMPANY_INVITE, cap, sent_today,
                                         caps=engagement_caps_from_prefs(prefs)))
    status = INVITE_STATUS_SENT if allowance > 0 else INVITE_STATUS_BUDGET_REACHED
    return {"allowance": allowance, "status": status, "cap": cap, "sent_today": sent_today}


def get_available_credits(driver, wait):
    """Read the page's live "<current>/<total> credits available" counter — ceiling number 3.

    Returns `(0, 0)` when that element never resolves, which is the fail-CLOSED reading the caller
    depends on: it treats 0 as `credits_exhausted` and sends nothing. An unreadable counter and a
    genuinely empty pool therefore both stop the run, because spending an invite we cannot account
    for is the one outcome worse than skipping a day.
    """
    # myprint("Entering get_available_credits function.")
    current_credits = 0
    total_credits = 0

    try:
        credit_text_element = get_element_wait_retry(driver, wait, '//span[text()[contains(.,"credits available")]]/span',
                                                 "Finding Credits Text Element", max_try=0)
        credit_text = getText(credit_text_element)
        current_credits, total_credits = map(int, credit_text.split('/'))
    except TimeoutException:
        log_info("No remaining invite credits")

    log_info(f"Credits available: {current_credits}/{total_credits}")
    return current_credits, total_credits


def get_initial_selected_count(driver, wait):
    """The invitee picker's own "N selected" counter, read BEFORE this run ticks anything.

    The dialog can open with boxes already ticked, so counting only our own clicks would let a run
    send more invites than the day's budget. Starting the count from what the page says is what keeps
    the budget the number of invites actually dispatched.

    Raises rather than defaulting to 0: an unreadable counter means the ceiling is unknown, and
    guessing low is how a paced drip turns back into a blast.
    """
    # myprint("Entering get_initial_selected_count function.")
    selected_text_element = get_element_wait_retry(driver, wait, '//span[text()[contains(.,"selected")]]',
                                                   "Finding Selected Text Element")
    selected_text = selected_text_element.text.strip()
    initial_selected_count = int(re.search(r'\d+', selected_text).group())
    log_info(f"Initial selected count: {initial_selected_count}")
    return initial_selected_count


def scroll_invitee_list(driver, wait):
    """Scroll the invitee picker once to make its next lazy-loaded page of connections render.

    Goes through mouse-wheel ActionChains against a sentinel div below the list rather than setting
    `scrollTop` — the picker's infinite scroller does not fire on a scripted scroll position, so the
    JS route silently loads nothing. Waits for the AJAX round-trip before returning, but does NOT
    promise the list grew: the caller decides whether it did and stops when it stops growing.
    """
    invitee_list_element = get_element_wait_retry(driver, wait,
                                                  "//div[contains(@class,'scaffold-finite-scroll__content')]",
                                                  "Finding Invitee List Element", max_try=0)

    # myprint("Entering scroll_invitee_list function.")
    current_height = driver.execute_script("return arguments[0].scrollHeight", invitee_list_element)
    #myprint(f"Current height: {current_height}")

    # There is a div at the bottom of the ul. Sroll it into view
    hidden_div = get_element_wait_retry(driver, wait, '//*[@id="invitee-picker-results-container"]/div/div[2]',
                                        "Finding Hidden Div")

    # driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight; arguments[1].scrollIntoView(false);",
    #                      invitee_list_element, hidden_div)  # Almost but no cigar

    # Use Selenium mouse wheel actions to simulate scrolling
    actions = ActionChains(driver)
    actions.move_to_element(invitee_list_element).move_to_element(hidden_div).scroll_by_amount(0,
                                                                                               1000).perform()  # Simulate scrolling down 1000 pixels
    wait_for_ajax(driver)  # Wait for the AJAX request to load more connections
    time.sleep(2)  # Sleep for a bit to let the AJAX request load more connections

    log_info("Scrolled down to load more connections.")


def _pause_between_selections(rng: Optional[random.Random] = None) -> float:
    """A short human pause between ticking two invitees. 0 (and no sleep) when pacing is off, so
    HUMAN_PACING_ENABLED=false restores the pre-#626 behaviour here too.
    """
    if not pacing_enabled():
        return 0.0
    delay = (rng or random).uniform(SELECTION_PAUSE_MIN_SECONDS, SELECTION_PAUSE_MAX_SECONDS)
    time.sleep(delay)
    return delay


# The zero-walk cross-check for select_connection_checkboxes (#1021). Independent of BOTH XPaths
# that walk drives (`scaffold-finite-scroll__content//li` and `input[id*=invitee]`): an invitee row
# renders the member's avatar, and an avatar is still there when the row markup rotates. A
# cross-check that itself stops matching counts 0 → `empty` → DEBUG, which is the fail-safe
# direction: this tripwire may never turn "everyone is already invited" into a filed defect.
_INVITEE_ROW_CROSSCHECK_SEL = ("#invitee-picker-results-container img, "
                               "div[role='dialog'] [role='listitem']")


def select_connection_checkboxes(driver, wait, limit):
    """Tick invitees until the picker's own selected count reaches `limit`, and return that count.

    `limit` is the run's whole budget — the smallest of the paced allowance, the credit spread and
    the live credit count — so this is where that number becomes clicks. The count starts from
    `get_initial_selected_count`, not from zero, so pre-ticked boxes spend the budget too.

    Scrolling continues only while a pass loads new invitees AND new checkboxes AND the checkbox
    count is still under the limit — so it stops as soon as EITHER counter stalls, and a page with
    fewer candidates than budget returns short instead of looping. Selections are spaced by
    `_pause_between_selections`; N machine-timed clicks in a row is the velocity signal #732 exists
    to remove.
    """
    # myprint("Entering select_connection_checkboxes function.")

    # Get the list of connections and scroll until there are as many available as the limit we need or end if there are now new connections
    checkbox_count = 0
    connections_list_count = 0
    checkboxes = []
    while checkbox_count < limit:
        try:
            connections_list = get_elements_as_list_wait_stale(wait,
                                                               "//div[contains(@class,'scaffold-finite-scroll__content')]//li",
                                                               "Finding Connections List", max_retry=0)
        except TimeoutException:
            log_info("No invitee list rendered.")
            connections_list = []
        new_connections_list_count = len(connections_list)

        try:
            checkboxes = get_elements_as_list_wait_stale(wait, "//input[@type='checkbox' and contains(@id, 'invitee')]",
                                                         "Finding Checkboxes", max_retry=0)
        except TimeoutException:
            log_info("No checkboxes found.")
            checkboxes = []

        new_checkbox_count = len(checkboxes)

        log_info(f"New connections list count: {new_connections_list_count}, New checkbox count: {new_checkbox_count}")

        if (new_checkbox_count != checkbox_count and new_checkbox_count < limit) and new_connections_list_count != connections_list_count:
            checkbox_count = new_checkbox_count
            connections_list_count = new_connections_list_count
            # Scroll
            scroll_invitee_list(driver, wait)
        else:
            log_info("No new checkboxes nor invitees after scrolling.")
            break  # Break the while loop

    if not checkboxes:
        # Nothing to tick, so the picker's counter cannot change the outcome — and reading it on a
        # page with no invitee rows is exactly where it raises, which is the crash #1102 names. 0
        # hands the decision to the zero-walk cross-check, which is what tells `no_candidates`
        # (nobody left to invite) apart from `drift` (rows on screen we can no longer read).
        log_info("No invitee checkboxes to select.")
        return 0

    selected_count = get_initial_selected_count(driver, wait)
    log_info(f"Starting with selected_count = {selected_count}")

    for checkbox in checkboxes:
        if selected_count >= limit:
            break
        if not checkbox.is_selected():
            driver.execute_script("arguments[0].click();", checkbox)
            selected_count += 1
            _pause_between_selections()

    if selected_count < limit:
        log_info(f"Selected {selected_count} connections so far. Could not reach limit of {limit}.")

    log_info(f"Completed selecting checkboxes with selected_count = {selected_count}")
    return selected_count


_INVITE_BUTTON_XPATH = "//div[contains(@class,'modal')]//button[contains(@class,'artdeco-button--primary')]"
# `//*`, not `//div`: every other reference to this id in this module leaves the tag open, because
# the tag is not what was live-grounded — and a locator that matches nothing is "invisible", which
# would make the disappearance check below pass on every run.
_INVITEE_LIST_XPATH = "//*[@id='invitee-picker-results-container']"
_INVITE_CONFIRMATION_XPATH = ("//div[contains(@class,'artdeco-toast') "
                              "or contains(@class,'artdeco-modal__confirmation')] "
                              "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                              "'abcdefghijklmnopqrstuvwxyz'),'invited') or "
                              "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                              "'abcdefghijklmnopqrstuvwxyz'),'invitation')]")

# The three outcomes of one invite click. Only CONFIRMED may be logged as a sent batch (#1102).
INVITE_CLICK_NOT_CLICKED = "not_clicked"
INVITE_CLICK_UNCONFIRMED = "unconfirmed"
INVITE_CLICK_CONFIRMED = "confirmed"


def _is_present(driver, xpath: str) -> bool:
    """Whether `xpath` matches anything RIGHT NOW — no waiting, no raise on a miss."""
    try:
        return bool(driver.find_elements(By.XPATH, xpath))
    except WebDriverException:
        return False


def _went_away(wait, xpath: str) -> bool:
    """Whether `xpath` stops being visible within the wait. False when it is still there."""
    try:
        return bool(wait.until(EC.invisibility_of_element_located((By.XPATH, xpath))))
    except TimeoutException:
        return False


def _confirm_invite_sent(driver, wait, present_before: Optional[dict] = None) -> bool:
    """Whether LinkedIn accepted the invite batch after the primary button was clicked.

    The dialog closing, the invitee picker going away, or a rendered confirmation each count as
    proof the click had an outcome. A read that proves nothing returns False — "unconfirmed" is a
    separate status from "not clicked" and "confirmed sent".

    `present_before` is what was on the page BEFORE the click, and a disappearance check counts only
    for something it says was there. Selenium reports a locator that matches nothing as invisible,
    so without that gate a rotated selector would confirm every click — the fail-OPEN shape that put
    a landed click in the ledger as a sent batch in the first place (#1102, the #1013 invariant).
    """
    present_before = present_before or {}

    # 1) Dialog gone: the modal we clicked into is no longer showing.
    modal_gone = bool(present_before.get("modal")) and _went_away(wait, _INVITE_BUTTON_XPATH)

    # 2) Picker gone: the container holding the rows we selected from no longer renders.
    list_gone = (not modal_gone and bool(present_before.get("list"))
                 and _went_away(wait, _INVITEE_LIST_XPATH))

    # 3) Rendered confirmation: a success/toast label containing "invited" or "invitation". A
    #    presence read, so it is the one check that still answers when the dialog stays open.
    confirmation = None
    if not (modal_gone or list_gone):
        confirmation = get_element_wait_retry(driver, wait, _INVITE_CONFIRMATION_XPATH,
                                              "Finding invite confirmation", max_try=0,
                                              element_always_expected=False)

    confirmed = bool(modal_gone or list_gone or confirmation)
    log_info(f"Invite outcome confirmation: modal_gone={modal_gone}, list_gone={list_gone}, "
            f"confirmation={bool(confirmation)} -> {confirmed}")
    return confirmed


def invite_selected_connections(driver, wait):
    """Click the invite dialog's primary button and report the outcome.

    Returns one of three strings so the caller can distinguish a missing button (`not_clicked`), a
    button that was clicked but whose effect could not be verified (`unconfirmed`), and a batch that
    LinkedIn visibly accepted (`confirmed`). Only `confirmed` may be logged as a sent batch and spent
    against tomorrow's budget.
    """
    # myprint("Entering invite_selected_connections function.")
    # Read the page BEFORE the click: a thing can only be proven gone if it was there to go.
    present_before = {"modal": _is_present(driver, _INVITE_BUTTON_XPATH),
                      "list": _is_present(driver, _INVITEE_LIST_XPATH)}

    invite_button = click_element_wait_retry(driver, wait, _INVITE_BUTTON_XPATH, "Finding Invite Button",
                                             element_always_expected=False)
    if not invite_button:
        log_info("Invite button not found.")
        return INVITE_CLICK_NOT_CLICKED

    # invite_button.click()
    log_info("Invite button clicked.")
    return (INVITE_CLICK_CONFIRMED if _confirm_invite_sent(driver, wait, present_before)
            else INVITE_CLICK_UNCONFIRMED)


def dismiss_prompt(driver, wait):
    """Clear the "boost this post?" nudge LinkedIn may raise after a batch of invites goes out.

    Cosmetic housekeeping, and False is the ordinary case — the nudge usually is not shown at all.
    It runs AFTER the invites are logged and recorded precisely so that a missing (or moved) dismiss
    control can never cost the run its record of what it sent.
    """
    # myprint("Entering dismiss_prompt function.")
    dismiss_button = click_element_wait_retry(driver, wait,
                                              "//button[@data-test-org-post-nudge-dismiss-cta]",
                                              "Finding Dismiss Button", element_always_expected=False,
                                              max_retry=0)
    if dismiss_button:
        log_info('"No thanks" button clicked.')
        return True
    log_info("No 'No thanks' button found.")
    return False


def _report(status: str, **fields) -> dict:
    report = {"status": status, "invites_sent": 0, "budget": 0, "cap": 0, "sent_today": 0,
              "credits_remaining": None, "credit_spread": None}
    report.update(fields)
    return report


def automate_invitations(driver, wait, user_id: int, plan: Optional[dict] = None) -> dict:
    """Send AT MOST today's paced budget of company-page invites, in one pass.

    Returns the run report — the caller turns it into telemetry. `plan` lets the task reuse the
    allowance it already computed to decide whether opening a browser was worth it at all.
    """
    log_info("Automate invitations to Company Page.")

    plan = plan if plan is not None else plan_daily_invites(user_id)
    allowance = max(0, int(plan.get("allowance") or 0))
    base = {"cap": plan.get("cap", 0), "sent_today": plan.get("sent_today", 0)}
    if allowance <= 0:
        log_info(f"Company page invites: nothing to send ({plan.get('status')})", user_id=user_id,
                 action_type="company_invite")
        return _report(plan.get("status") or INVITE_STATUS_BUDGET_REACHED, **base)

    user_email, user_password = get_user_password_pair_by_id(user_id)

    # Get Company page from DB
    li_company_page_url = get_company_linked_in_url_for_user(user_id)
    if not li_company_page_url:
        return _report(INVITE_STATUS_NO_PAGE, **base)

    login_to_linkedin(driver, wait, user_email, user_password)

    # Add ?invite=true query parameter to the URL to navigate to the invite page
    invite_page_url = li_company_page_url + "?invite=true"

    # Navigate to Company Page
    if driver.current_url != invite_page_url:
        driver.get(invite_page_url)

    current_credits, total_credits = get_available_credits(driver, wait)
    if current_credits <= 0:
        log_info("Company page invites: no credits left this month", user_id=user_id,
                 action_type="company_invite")
        return _report(INVITE_STATUS_CREDITS_EXHAUSTED, credits_remaining=current_credits, **base)

    # Credits are a CEILING, not a target: spend at most this day's share of what's left so the
    # monthly pool (and the credits acceptances refund into it) lasts past the first week.
    spread = credit_spread_budget(current_credits)
    budget = min(allowance, spread, current_credits)
    log_info(f"Company page invite budget {budget} "
             f"(allowance {allowance}, spread {spread}, credits {current_credits}/{total_credits})",
             user_id=user_id, action_type="company_invite")
    base.update({"budget": budget, "credits_remaining": current_credits, "credit_spread": spread})

    selected_count = select_connection_checkboxes(driver, wait, budget)
    if selected_count <= 0:
        # Zero ticked boxes is ambiguous, so ask the modal before calling it a quiet day (#1021).
        verdict = report_zero_walk(driver, _INVITEE_ROW_CROSSCHECK_SEL, "Company invitee-row walk",
                                   user_id=user_id, action_type="company_invite")
        log_info("No more connections to invite or already selected. Exiting automate_invitations.")
        return _report(INVITE_STATUS_DRIFT if verdict == "drift" else INVITE_STATUS_NO_CANDIDATES,
                       **base)

    invite_outcome = invite_selected_connections(driver, wait)
    if invite_outcome == INVITE_CLICK_NOT_CLICKED:
        insert_new_log(user_id, LogActionType.ENGAGED, LogResultType.FAILURE,
                       post_url=li_company_page_url,
                       message="Failed to invite to company page: invite button not found")
        log_info("No invite button found, stopping automate_invitations.")
        return _report(INVITE_STATUS_FAILED, **base)

    if invite_outcome == INVITE_CLICK_UNCONFIRMED:
        # The click reached LinkedIn but we cannot prove the batch landed. Do NOT spend tomorrow's
        # budget on it; keep the outcome visible as its own status (#1102).
        insert_new_log(user_id, LogActionType.ENGAGED, LogResultType.FAILURE,
                       post_url=li_company_page_url,
                       message=(f"Company page invite click unconfirmed: "
                                f"{selected_count} invitees selected"))
        log_info("Invite click could not be confirmed, stopping automate_invitations.")
        return _report(INVITE_STATUS_UNCONFIRMED, **base)

    # The "<message>: <n>" shape is load-bearing — count_company_page_invites_sent_today SUMS that
    # number, and it is what makes a second run today idempotent.
    insert_new_log(user_id, LogActionType.ENGAGED, LogResultType.SUCCESS,
                   post_url=li_company_page_url,
                   message=f"{COMPANY_PAGE_INVITE_SENT_MESSAGE}: {selected_count}")
    # Recorded under ACTION_INVITE on purpose: the lane draws its own budget (ACTION_COMPANY_INVITE)
    # but SPENDS the shared outbound envelope, and ACTION_INVITE is the envelope field.
    record_action(user_id, ACTION_INVITE, selected_count)  # account-level governor (issue #626)
    time.sleep(2)  # Delay to ensure the prompt appears before checking for it
    if dismiss_prompt(driver, wait):
        log_info("Prompt handled")
    else:
        log_info("No prompt to handle.")

    # NO recursion: whatever is left of the monthly pool is tomorrow's drip, not this run's.
    return _report(INVITE_STATUS_SENT, invites_sent=selected_count, **base)