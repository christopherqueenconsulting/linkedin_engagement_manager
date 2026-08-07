"""Stale connection-invite withdrawal — the daily beat that used to be a stub (issue #969).

`clean_stale_invites` sat on the beat schedule at 02:00 every day and returned
`{"status": "not_implemented"}`. Nothing withdrew anything, so a user's pending invites accumulated
forever: LinkedIn caps how many invitations may be outstanding at once, and a pile of months-old
unanswered ones both eats that ceiling and drags the acceptance rate every other outreach feature is
judged on.

Three things make this safe enough to point at a real account:

1. **Withdrawing is not reversible.** LinkedIn blocks re-inviting the same person for ~3 weeks after
   a withdrawal, so an invite withdrawn by mistake costs a real opportunity. Every decision therefore
   fails CLOSED: an invite whose age cannot be READ is never stale (`parse_sent_age_days` returns
   `None`, and `None` is skipped), and a row without a resolvable Withdraw control is left alone.
2. **The lane is OFF until someone turns it on** (`STALE_INVITE_WITHDRAWAL_ENABLED`, default false).
   The selectors below are written from LinkedIn's invitation manager as documented, not from a live
   grounding run — and an ungrounded Selenium lane that silently matches nothing is exactly how the
   catch-up lane spent months doing nothing. `scripts/linkedin_live_validation.py --sent-invites` is
   the read-only probe that grounds it before the switch is flipped.
3. **It is paced and capped like every other outbound lane.** The allowance is decided BEFORE a
   Chrome session opens (`plan_withdrawals`), draws its own `ACTION_WITHDRAW_INVITE` budget through
   the #626 engine, and every hard gate (manual/suppression pause, the 429 breaker) is re-read per
   withdrawal because a breaker can trip mid-run.

The daily spend is counted out of the immutable `logs` rows, not Redis, and it counts a DISPATCHED
withdrawal whatever the verdict was: the click already reached LinkedIn, so a lane whose verification
broke must not be free to click every row on the page.
"""

import os
import random
import re
import time
from typing import Callable, Optional

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

from cqc_lem.utilities.db import (
    STALE_INVITE_WITHDRAWN_MESSAGE,
    LogActionType,
    LogResultType,
    count_invite_withdrawals_today,
    get_engagement_preferences,
    insert_new_log,
)
from cqc_lem.utilities.human_pacing import (
    ACTION_WITHDRAW_INVITE,
    engagement_caps_from_prefs,
    record_action,
    remaining_actions,
)
from cqc_lem.utilities.linkedin.rate_limit import (
    automation_pause_reason,
    is_automation_paused,
    rate_limit_cooldown_remaining,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

# LinkedIn's own "Manage invitations -> Sent" surface. The only page that lists what is actually
# still PENDING — an accepted or ignored invite is simply not here, which is why the page is the
# source of truth rather than our own `logs` rows.
SENT_INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"

# Run statuses — stable strings so "why did this account withdraw nothing yesterday?" is a group-by
# on the telemetry rather than a log grep.
WITHDRAW_STATUS_WITHDREW = "withdrew"
WITHDRAW_STATUS_DISABLED = "disabled"              # lane not switched on (the default)
WITHDRAW_STATUS_BUDGET_REACHED = "budget_reached"  # paced to zero / cap already spent today
WITHDRAW_STATUS_NONE_STALE = "none_stale"          # rows read, none old enough
WITHDRAW_STATUS_NO_ROWS = "no_rows"                # nothing pending, or the list did not render
WITHDRAW_STATUS_PAUSED = "paused"                  # manual/suppression pause or the 429 breaker
WITHDRAW_STATUS_FAILED = "failed"
WITHDRAW_STATUS_SESSION_FAILED = "session_failed"  # no Chrome slot — kept apart from `failed`

STALE_INVITE_AGE_DAYS_DEFAULT = 21
STALE_INVITE_WITHDRAWALS_PER_DAY_DEFAULT = 10

# How much of the list one run is willing to walk. The page loads newest-first, so the invites we
# want are at the BOTTOM — without a bounded loader a run would only ever see the ones it must not
# touch. Both bounds are reported, so a run that ran out of road is visible rather than inferred.
SHOW_MORE_MAX_CLICKS = 5
MAX_ROWS_SCANNED = 200

# A withdrawal is one click, a confirm, and a re-render. Give the row time to go away before calling
# it a failure — the list is re-rendered, not merely relabelled.
WITHDRAW_CONFIRM_ATTEMPTS = 3
WITHDRAW_CONFIRM_WAIT_SECONDS = 2.0

# A human reads a row before withdrawing it. Small, randomized, and well inside the task's runtime —
# the daily budget is single/low-double digits.
ROW_PAUSE_MIN_SECONDS = 1.5
ROW_PAUSE_MAX_SECONDS = 4.0

# "Sent 3 weeks ago", "Sent 1 month ago", "Sent a day ago". The trailing "ago" is REQUIRED, and the
# match is run per LINE: a row's text is the whole card, headline included, and a profile headline
# reading "10 years building X" would otherwise date a two-day-old invite at a decade and withdraw
# it. Anything that does not match is UNREADABLE, never "old".
_AGE_RE = re.compile(r"(?<!\w)(\d+|a|an)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b",
                     re.IGNORECASE)
_UNIT_DAYS = {"second": 0.0, "minute": 0.0, "hour": 0.0, "day": 1.0,
              "week": 7.0, "month": 30.0, "year": 365.0}

# Resolve the pending-invite rows in ONE pass in the page, the same way the #962 follow control is
# resolved: LinkedIn ships hashed class names that churn, so the anchors are the person's /in/ link,
# the button's own accessible label, and visible TEXT — never a class.
_SENT_INVITE_ROWS_JS = r"""
const MAX = arguments[0];
const label = (el) => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
const shown = (el) => { const r = el.getBoundingClientRect(); return !!(r.width && r.height); };
const isWithdraw = (el) => /(^|\b)withdraw(\b|$)/i.test(label(el));

const rows = [];
const seen = new Set();
for (const b of document.querySelectorAll("button, [role='button']")) {
  if (rows.length >= MAX) break;
  if (!shown(b) || !isWithdraw(b)) continue;
  // Walk up until the ancestor also carries the invitee's profile link — that container IS the row,
  // and its text is where the "Sent ... ago" stamp lives. Bounded so a page-wide ancestor (which
  // would hand back every invite's text at once) is never accepted as a row.
  let el = b.parentElement, depth = 0, row = null, anchor = null;
  while (el && depth < 8) {
    const a = el.querySelector("a[href*='/in/']");
    if (a) { row = el; anchor = a; break; }
    el = el.parentElement; depth++;
  }
  if (!row || seen.has(row)) continue;
  // A container carrying MORE THAN ONE Withdraw control is the LIST, not a row. Accepting it would
  // hand back every invite's text as one card — so the newest invite's "Sent ... ago" stamp would
  // date the whole page — paired with whichever person's link happened to come first. Skip it: the
  // row count drops to zero, which reads as drift on the report, instead of one mis-aged row that a
  // one-way action then acts on.
  if (Array.from(row.querySelectorAll("button, [role='button']"))
        .filter((x) => shown(x) && isWithdraw(x)).length > 1) continue;
  seen.add(row);
  rows.push([anchor.getAttribute('href') || '', (anchor.innerText || '').trim(),
             (row.innerText || '').trim(), b]);
}
return rows;
"""

# The confirmation dialog's own Withdraw button. Scoped to the open dialog so it can never re-click a
# row button underneath it.
_CONFIRM_WITHDRAW_JS = r"""
const label = (el) => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
const shown = (el) => { const r = el.getBoundingClientRect(); return !!(r.width && r.height); };
for (const dialog of document.querySelectorAll("[role='dialog'], [role='alertdialog']")) {
  if (!shown(dialog)) continue;
  for (const b of dialog.querySelectorAll("button, [role='button']")) {
    if (shown(b) && /(^|\b)withdraw(\b|$)/i.test(label(b))) return b;
  }
}
return null;
"""


def withdrawal_lane_enabled() -> bool:
    """Opt-in switch, OFF by default. The selectors here have not been through a live grounding run,
    and a withdrawal cannot be taken back — so nothing goes out until someone turns this on.
    """
    return (os.environ.get("STALE_INVITE_WITHDRAWAL_ENABLED") or "false").strip().lower() \
        in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        log_warning(f"{name} ignored (not an integer: {raw!r})")
        return default


def stale_after_days() -> int:
    """How old a PENDING invite has to be before it is withdrawable. 0 disables the lane rather than
    withdrawing everything — "withdraw invites older than nothing" is never what someone meant.
    """
    return _env_int("STALE_INVITE_AGE_DAYS", STALE_INVITE_AGE_DAYS_DEFAULT)


def withdrawals_per_day_cap() -> int:
    """Ceiling on withdrawals dispatched in one day, before pacing narrows it further.

    A cap of 0 turns the lane off outright (`WITHDRAW_STATUS_DISABLED`) without a Chrome slot being
    spent — the second kill switch alongside `withdrawal_lane_enabled`, and the one an operator can
    reach mid-incident. Negative values clamp to 0 for the same reason: this action is one-way, so
    every unreadable configuration lands on doing nothing.
    """
    return _env_int("STALE_INVITE_WITHDRAWALS_PER_DAY",
                    STALE_INVITE_WITHDRAWALS_PER_DAY_DEFAULT)


def parse_sent_age_days(text: str) -> Optional[float]:
    """Age in days of the "Sent ... ago" stamp on one row, or `None` when it cannot be read.

    `None` is the load-bearing value: the caller must treat an unreadable stamp as NOT stale. Getting
    this wrong in the other direction withdraws a two-day-old invite that was about to be accepted,
    and LinkedIn will not let us re-send it for weeks.

    Sub-day units resolve to 0.0 — falsy, but explicitly not `None`: "sent 3 hours ago" IS readable,
    and it is readably young.

    Only the row's OWN "Sent ..." line is read, because the text handed in is the whole card — name,
    headline and buttons included — and a headline is free to contain a number and a time unit
    ("shipping since 10 years ago"). If LinkedIn ever stops writing "Sent", every row reads
    unreadable and this lane withdraws NOTHING, which is the direction a one-way action has to fail;
    the run report's `unreadable` count is what makes that visible instead of silent.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    stamped = [line for line in lines if "sent" in line.lower()]
    for line in stamped:
        match = _AGE_RE.search(line)
        if match:
            raw_count, unit = match.group(1), match.group(2).lower()
            count = 1.0 if raw_count.lower() in ("a", "an") else float(raw_count)
            return count * _UNIT_DAYS[unit]
    for line in stamped:
        # LinkedIn writes these instead of "1 day ago" / "0 days ago" on the freshest rows.
        lowered = line.lower()
        if "yesterday" in lowered:
            return 1.0
        if "today" in lowered or "just now" in lowered:
            return 0.0
    return None


def plan_withdrawals(user_id: int, prefs: Optional[dict] = None) -> dict:
    """Today's paced allowance, decided BEFORE any browser session is opened.

    Returns `{'allowance', 'status', 'cap', 'withdrawn_today', 'threshold_days'}`. A zero allowance
    is the ordinary case (lane off, rest day, cap already spent) and must not cost a Chrome slot,
    which is why this half is separate from the Selenium half. `status` only means anything when the
    allowance is 0 — it says WHY nothing may go out.
    """
    threshold = stale_after_days()
    base = {"allowance": 0, "cap": 0, "withdrawn_today": 0, "threshold_days": threshold}
    if not withdrawal_lane_enabled() or threshold <= 0:
        return {**base, "status": WITHDRAW_STATUS_DISABLED}
    cap = withdrawals_per_day_cap()
    if cap <= 0:
        return {**base, "status": WITHDRAW_STATUS_DISABLED}
    prefs = prefs if prefs is not None else get_engagement_preferences(user_id)
    withdrawn_today = count_invite_withdrawals_today(user_id)
    # Its OWN pacing key: `daily_budget` stores the draw under the action name, so borrowing
    # ACTION_INVITE would overwrite the connection lane's budget with this (different) cap for the
    # rest of the day. `caps` is still the shared envelope — an account that has spent its outbound
    # allowance stops withdrawing too — but ACTION_WITHDRAW_INVITE is deliberately not IN the
    # envelope, since a withdrawal must never enlarge a day's outbound allowance.
    allowance = max(0, remaining_actions(user_id, ACTION_WITHDRAW_INVITE, cap, withdrawn_today,
                                         caps=engagement_caps_from_prefs(prefs)))
    return {"allowance": allowance,
            "status": WITHDRAW_STATUS_WITHDREW if allowance > 0 else WITHDRAW_STATUS_BUDGET_REACHED,
            "cap": cap, "withdrawn_today": withdrawn_today, "threshold_days": threshold}


def _hold_reason(user_id: int) -> str:
    """Why a withdrawal must not go out right now — '' when every hard gate is clear.

    Pacing only ever slows the lane down; these are the harder gates, and they are re-read per
    withdrawal because the 429 breaker can trip mid-run. The #629 suppression tripwire rides
    `is_automation_paused`, so one check covers the manual pause, the deploy pause and a suppression
    hold.
    """
    if is_automation_paused():
        return automation_pause_reason() or "automation paused"
    if rate_limit_cooldown_remaining() > 0:
        return "LinkedIn 429 breaker open"
    return ""


def _load_more_rows(driver: WebDriver, sleep: Callable[[float], None] = time.sleep) -> int:
    """Click "Show more" until it stops appearing, up to `SHOW_MORE_MAX_CLICKS` times.

    The sent list renders newest-first, so the invites this lane exists for are at the BOTTOM. A run
    that never loads past the first page can only ever see rows it must not touch — which would look
    exactly like "nothing is stale". Returns how many times it expanded, so the report can say the
    walk ran out of road rather than out of stale invites.
    """
    clicks = 0
    for _ in range(SHOW_MORE_MAX_CLICKS):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            button = driver.execute_script(
                "const shown = (el) => { const r = el.getBoundingClientRect();"
                " return !!(r.width && r.height); };"
                "for (const b of document.querySelectorAll(\"button, [role='button']\")) {"
                "  const t = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || ''));"
                "  if (shown(b) && /show more|see more results|load more/i.test(t)) return b;"
                "} return null;")
        except Exception as e:
            log_debug(f"Sent-invite list expansion failed ({type(e).__name__}: {e})",
                      action_type="invite", task_name="_load_more_rows")
            return clicks
        if button is None:
            return clicks
        try:
            driver.execute_script("arguments[0].click();", button)
        except Exception as e:
            log_debug(f"Sent-invite 'Show more' click failed ({type(e).__name__}: {e})",
                      action_type="invite", task_name="_load_more_rows")
            return clicks
        clicks += 1
        sleep(2.0)
    return clicks


def read_pending_invites(driver: WebDriver) -> list[dict]:
    """Every pending sent invite currently rendered, as
    `{'profile_url', 'name', 'text', 'age_days', 'control'}`.

    `age_days` is `None` when the row's "Sent ... ago" stamp could not be read — the caller skips
    those. An empty list means the page rendered nothing we recognise, which is NOT the same as "no
    pending invites"; only the caller knows which of those it is looking at.
    """
    try:
        rows = driver.execute_script(_SENT_INVITE_ROWS_JS, MAX_ROWS_SCANNED)
    except Exception as e:
        log_debug(f"Sent-invite row resolution JS failed ({type(e).__name__}: {e})",
                  action_type="invite", task_name="read_pending_invites")
        return []
    invites: list[dict] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        href, name, text, control = row
        invites.append({"profile_url": str(href or ""), "name": str(name or "").strip(),
                        "text": str(text or ""), "age_days": parse_sent_age_days(text),
                        "control": control})
    return invites


def _confirm_withdrawal(driver: WebDriver, sleep: Callable[[float], None]) -> bool:
    """Click the confirmation dialog's Withdraw button. False when no dialog resolved.

    Returning False is NOT "the invite survived" — LinkedIn has shipped both a confirmed and an
    unconfirmed withdrawal, so the caller has to re-read the list either way.
    """
    for attempt in range(WITHDRAW_CONFIRM_ATTEMPTS):
        if attempt:
            sleep(WITHDRAW_CONFIRM_WAIT_SECONDS)
        try:
            button = driver.execute_script(_CONFIRM_WITHDRAW_JS)
        except Exception:
            button = None
        if button is None:
            continue
        try:
            driver.execute_script("arguments[0].click();", button)
            return True
        except Exception as e:
            log_debug(f"Withdraw confirmation click failed ({type(e).__name__}: {e})",
                      action_type="invite", task_name="_confirm_withdrawal")
    return False


def _invite_still_pending(driver: WebDriver, profile_url: str,
                          sleep: Callable[[float], None]) -> bool:
    """Whether this person's row is STILL in the sent list after the withdrawal.

    The list re-renders rather than relabelling the row, so a single immediate re-read races that
    render. An invite we cannot prove is gone is reported as unverified, never as withdrawn — and a
    row that carried no profile link is unverifiable by definition, so it stays 'pending' here.
    """
    if not profile_url:
        return True
    for _ in range(WITHDRAW_CONFIRM_ATTEMPTS):
        sleep(WITHDRAW_CONFIRM_WAIT_SECONDS)
        if not any(row.get("profile_url") == profile_url for row in read_pending_invites(driver)):
            return False
    return True


def _resolve_control(driver: WebDriver, invite: dict):
    """This invite's Withdraw control AS THE PAGE RENDERS IT NOW, or `None`.

    The controls handed back by the first `read_pending_invites` are element handles into a list that
    a withdrawal RE-RENDERS. Re-using one afterwards has two failure modes and only one of them is
    harmless: the node is detached and the click raises (a skip), or the framework re-used that node
    for the row that shifted up into its place — in which case the click withdraws a DIFFERENT,
    possibly days-old invite, and LinkedIn will not let us re-send it for weeks. So every click after
    the list has moved re-reads the page and matches on the profile URL, and an invite that cannot be
    re-identified is skipped rather than guessed at: a missed withdrawal is recoverable on the next
    run, a wrong one is not.
    """
    profile_url = invite.get("profile_url") or ""
    if not profile_url:
        return None
    for row in read_pending_invites(driver):
        if row.get("profile_url") == profile_url:
            return row.get("control")
    return None


def _record_withdrawal(user_id: int, invite: dict, verified: bool) -> None:
    """One immutable log row per DISPATCHED withdrawal, whatever the verdict.

    `count_invite_withdrawals_today` counts these rows regardless of result, because the budget is
    spent on the CLICK: the action already reached LinkedIn, and a lane whose verification broke must
    not be free to click every row on the page. The result flag is what an operator reads to tell an
    unverified withdrawal apart from a confirmed one.
    """
    insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                   result=LogResultType.SUCCESS if verified else LogResultType.FAILURE,
                   post_url=invite.get("profile_url") or None,
                   message=STALE_INVITE_WITHDRAWN_MESSAGE)
    record_action(user_id, ACTION_WITHDRAW_INVITE)


def _report(status: str, **fields) -> dict:
    report = {"status": status, "withdrawn": 0, "unverified": 0, "budget": 0, "cap": 0,
              "withdrawn_today": 0, "threshold_days": 0, "rows_seen": 0, "stale_seen": 0,
              "unreadable": 0, "expansions": 0}
    report.update(fields)
    return report


def withdraw_stale_invites(driver: WebDriver, wait: WebDriverWait, user_id: int,
                           plan: Optional[dict] = None,
                           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Withdraw AT MOST today's paced budget of pending invites older than the threshold.

    Returns the run report; the caller turns it into telemetry. `wait` is accepted for symmetry with
    the other Selenium lanes (and so a future selector fallback can use it) — the row resolution
    itself runs in one page-side pass, which is what keeps a 100-row list from costing 100 round
    trips.
    """
    plan = plan if plan is not None else plan_withdrawals(user_id)
    budget = max(0, int(plan.get("allowance") or 0))
    threshold = float(plan.get("threshold_days") or 0)
    base = {"budget": budget, "cap": plan.get("cap", 0),
            "withdrawn_today": plan.get("withdrawn_today", 0), "threshold_days": threshold}
    if budget <= 0 or threshold <= 0:
        return _report(plan.get("status") or WITHDRAW_STATUS_BUDGET_REACHED, **base)

    hold = _hold_reason(user_id)
    if hold:
        log_info(f"Stale-invite withdrawal stood down — {hold}", user_id=user_id,
                 action_type="invite", task_name="withdraw_stale_invites")
        return _report(WITHDRAW_STATUS_PAUSED, **base)

    driver.get(SENT_INVITATIONS_URL)
    sleep(4.0)
    expansions = _load_more_rows(driver, sleep=sleep)
    invites = read_pending_invites(driver)
    base["expansions"] = expansions
    base["rows_seen"] = len(invites)
    base["unreadable"] = sum(1 for i in invites if i["age_days"] is None)
    if not invites:
        # DEBUG, not a warning: an account with nothing outstanding renders exactly this, and it is
        # the common case for a healthy account. `rows_seen: 0` on the run report is what makes
        # selector rot visible, without filing a defect for an empty list.
        log_debug("No pending sent invitations resolved on the invitation manager", user_id=user_id,
                  action_type="invite", task_name="withdraw_stale_invites")
        return _report(WITHDRAW_STATUS_NO_ROWS, **base)

    if base["unreadable"] == len(invites):
        # Rows resolved but not ONE carried a readable "Sent ... ago" stamp. That is the stamp's
        # wording having moved, not an account whose invites are all undated — and the lane would
        # otherwise go quiet forever without ever saying so. One warning, at the run level.
        log_warning(f"Every pending invite row ({len(invites)}) has an unreadable sent date — "
                    f"nothing can be aged, so nothing is withdrawn", user_id=user_id,
                    action_type="invite", task_name="withdraw_stale_invites")
        return _report(WITHDRAW_STATUS_NONE_STALE, **base)

    stale = [i for i in invites if i["age_days"] is not None and i["age_days"] >= threshold]
    base["stale_seen"] = len(stale)
    if not stale:
        log_info(f"No pending invites older than {threshold:.0f} days "
                 f"({len(invites)} pending, {base['unreadable']} undated)",
                 user_id=user_id, action_type="invite", task_name="withdraw_stale_invites")
        return _report(WITHDRAW_STATUS_NONE_STALE, **base)

    # Oldest first: the budget is small, so it must be spent on the invites least likely to ever be
    # accepted rather than on whichever row happened to render first.
    stale.sort(key=lambda i: i["age_days"], reverse=True)

    withdrawn = 0
    unverified = 0
    attempted = 0
    held = False
    for invite in stale[:budget]:
        hold = _hold_reason(user_id)
        if hold:
            # INFO once, here, rather than per row: the caller already announced a clear run, and a
            # breaker that trips mid-walk is a real state change worth one line.
            log_info(f"Stale-invite withdrawal stopped mid-run — {hold}", user_id=user_id,
                     action_type="invite", task_name="withdraw_stale_invites")
            held = True
            break
        # The first click spends the handles read off the page we just walked. Every click after one
        # has gone out re-resolves against the re-rendered list — see `_resolve_control`.
        control = invite.get("control") if not attempted else _resolve_control(driver, invite)
        if control is None:
            if attempted:
                log_debug("Skipping a stale invite that no longer resolves on the re-rendered list",
                          user_id=user_id, action_type="invite",
                          task_name="withdraw_stale_invites")
            continue
        sleep(random.uniform(ROW_PAUSE_MIN_SECONDS, ROW_PAUSE_MAX_SECONDS))
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", control)
            driver.execute_script("arguments[0].click();", control)
        except Exception as e:
            # The row went stale under us (the list re-renders after each withdrawal) — the next run
            # sees it again, so this is a skip, not a defect.
            log_debug(f"Withdraw control click failed ({type(e).__name__}: {e})", user_id=user_id,
                      action_type="invite", task_name="withdraw_stale_invites")
            continue
        attempted += 1
        _confirm_withdrawal(driver, sleep)
        verified = not _invite_still_pending(driver, invite.get("profile_url") or "", sleep)
        _record_withdrawal(user_id, invite, verified)
        if verified:
            withdrawn += 1
            log_info(f"Withdrew stale invite to {invite.get('name') or invite.get('profile_url')} "
                     f"({invite['age_days']:.0f} days old)", user_id=user_id, action_type="invite",
                     task_name="withdraw_stale_invites")
        else:
            unverified += 1
            # The click went out and the row is still there. Worth a warning: repeated, it means the
            # confirm dialog moved, and this lane would then be clicking without ever withdrawing.
            log_warning("Withdrawal did not take — the invite is still listed as pending",
                        user_id=user_id, action_type="invite",
                        task_name="withdraw_stale_invites")

    # `withdrawn` is the only thing that earns the success status. A run that clicked and could not
    # verify, and a run whose stale rows all went stale before they could be clicked, are both
    # failures — they are the two shapes selector rot takes here, and calling either of them a
    # success is how a lane goes quiet without anyone noticing.
    if withdrawn:
        status = WITHDRAW_STATUS_WITHDREW
    elif attempted:
        status = WITHDRAW_STATUS_FAILED
    elif held:
        status = WITHDRAW_STATUS_PAUSED
    else:
        status = WITHDRAW_STATUS_FAILED
    return _report(status, withdrawn=withdrawn, unverified=unverified, **base)
