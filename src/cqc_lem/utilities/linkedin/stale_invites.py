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
2. **The switch is still a switch** (`STALE_INVITE_WITHDRAWAL_ENABLED`), but it now defaults TRUE.
   It shipped false because the selectors below were written from LinkedIn's invitation manager as
   documented, not from a live grounding run — and an ungrounded Selenium lane that silently matches
   nothing is exactly how the catch-up lane spent months doing nothing. That reason is spent: #1006
   grounded the whole path against a real account on 2026-08-07 (row read, the "Sent … ago" stamps,
   scroll-loading past the newest 20, the confirm dialog, and the entity check — one real withdrawal
   proved the confirm step end to end), and the owner authorised the flip on the issue. Setting the
   variable to `false` still turns the lane off. `scripts/linkedin_live_validation.py --sent-invites`
   remains the read-only probe to re-ground it whenever the page moves.
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
WITHDRAW_STATUS_DISABLED = "disabled"              # switch set to off, or a zero cap/threshold
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
#
# The first grounding run (#1006, 2026-08-07) hit the old cap of 5 EXACTLY, which is the one reading
# that cannot be told apart from running out of road. Raising it answered that: the control stops
# appearing on its own at 6, and 20 rows render against the page's own "People (44)" — so the
# remaining 24 are NOT behind this bound, and what holds them is still unexplained. Expanding is
# read-only, so the bound stays generous and tunable, and `expansions` keeps saying which of the two
# a given run hit.
SHOW_MORE_MAX_CLICKS_DEFAULT = 20
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

# "Ann Lee · 1st" — the connection badge SDUI appends to a name line. Stripped before the name is
# compared, so a badge never turns a matching control into a refusal.
_DEGREE_SUFFIX_RE = re.compile(r"\s*[·•|]\s*\d*(st|nd|rd|th)?\+?\s*$", re.IGNORECASE)

# Resolve the pending-invite rows in ONE pass in the page, the same way the #962 follow control is
# resolved: LinkedIn ships hashed class names that churn, so the anchors are the person's /in/ link,
# the button's own accessible label, and visible TEXT — never a class.
_SENT_INVITE_ROWS_JS = r"""
const MAX = arguments[0];
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
const aria = (el) => norm(el.getAttribute('aria-label') || '');
const shown = (el) => { const r = el.getBoundingClientRect(); return !!(r.width && r.height); };

// Live shape (grounded 2026-08-07, issue #1006): the control is an ANCHOR whose accessible label
// NAMES the invitee — "Withdraw invitation sent to <Name>" — and carries no button role at all.
// Matching `button, [role='button']` found zero of 80 Withdraw nodes on a page of 44 pending
// invites. Matching the bare word instead is worse than useless: it renders three nested
// presentational nodes per row, so every row would multiply by four. Anchor on the LABEL, which is
// also the only reading that says WHO a click would act on.
const LABELLED = /^withdraw invitation\b/i;
const BARE = /(^|\b)withdraw(\b|$)/i;
const bareLabel = (el) => norm(aria(el) + ' ' + (el.innerText || ''));
const invitee = (el) => {
  const m = aria(el).match(/^withdraw invitation(?:\s+sent)?\s+to\s+(.+)$/i);
  return m ? norm(m[1]) : '';
};

// Prefer the labelled control; keep the bare button as a fallback for when the shape changes back.
let controls = Array.from(document.querySelectorAll('[aria-label]'))
    .filter((el) => shown(el) && LABELLED.test(aria(el)));
const labelled = controls.length > 0;
if (!labelled) {
  controls = Array.from(document.querySelectorAll("button, [role='button']"))
      .filter((el) => shown(el) && BARE.test(bareLabel(el)));
}
const controlsInside = (root) => Array.from(labelled
      ? root.querySelectorAll('[aria-label]')
      : root.querySelectorAll("button, [role='button']"))
    .filter((x) => shown(x) && (labelled ? LABELLED.test(aria(x)) : BARE.test(bareLabel(x)))).length;

const rows = [];
const seen = new Set();
for (const b of controls) {
  if (rows.length >= MAX) break;
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
  if (controlsInside(row) > 1) continue;
  seen.add(row);
  // Row text keeps its LINE BREAKS — `parse_sent_age_days` reads the row's own "Sent ..." line, and
  // flattening the card would let a headline's "... 10 years ago" date the invite.
  rows.push([anchor.getAttribute('href') || '', (anchor.innerText || '').trim(),
             (row.innerText || '').trim(), b, invitee(b)]);
}
return rows;
"""

# The confirmation dialog's own Withdraw button. Scoped to the open dialog so it can never re-click a
# row button underneath it.
_CONFIRM_WITHDRAW_JS = r"""
const label = (el) => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
const shown = (el) => { const r = el.getBoundingClientRect(); return !!(r.width && r.height); };
// The confirmation is a NATIVE <dialog data-testid="dialog"> carrying no role attribute (grounded
// 2026-08-07, #1006) — `[role='dialog']` alone matched nothing, so a real withdrawal clicked the
// row control and then found no confirm button, leaving the invite pending and the run "unverified".
// The row's own control is likewise an ANCHOR with no button role, so controls are matched on
// clickable TAG rather than on role alone — never a bare span or div, three of which carry the same
// word inside one control and none of which own the handler.
for (const dialog of document.querySelectorAll(
    "dialog, [role='dialog'], [role='alertdialog'], [data-testid='dialog']")) {
  if (!shown(dialog)) continue;
  const controls = Array.from(dialog.querySelectorAll("a, button, [role='button']"))
      .filter((b) => shown(b) && /(^|\b)withdraw(\b|$)/i.test(label(b)));
  if (!controls.length) continue;
  // Prefer one that says so in its own accessible label — the row's control does, and a labelled
  // control is the only one that states what it acts on.
  return controls.find((b) => /withdraw/i.test(b.getAttribute('aria-label') || '')) || controls[0];
}
return null;
"""


def withdrawal_lane_enabled() -> bool:
    """The lane's kill switch, ON by default since #1006 grounded every step of it live.

    It was opt-in OFF while the selectors were unproven; they are proven now, and leaving the lane
    off means pending invites keep eating LinkedIn's outstanding-invite ceiling — the very thing
    #969 exists to stop. A withdrawal still cannot be taken back, so the switch stays: an operator
    who wants the beat silent sets `STALE_INVITE_WITHDRAWAL_ENABLED=false` (or `0`/`no`), and every
    other guard — the 21-day threshold, the daily cap, pacing, the fail-closed age read, the
    entity check, the 429 breaker — is unchanged and independent of this. A value that parses as
    nothing recognisable also reads as off: on the one action that cannot be undone, a typo must
    fail in the direction that withdraws less, not more. An EMPTY value reads as unset (the
    default); a whitespace-only one does not — it takes the same off reading as a typo, which is
    the safe direction here even though `_env_int` would hand back its default.
    """
    return (os.environ.get("STALE_INVITE_WITHDRAWAL_ENABLED") or "true").strip().lower() \
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
    withdrawing everything — "withdraw invites older than nothing" is never what someone meant."""
    return _env_int("STALE_INVITE_AGE_DAYS", STALE_INVITE_AGE_DAYS_DEFAULT)


def withdrawals_per_day_cap() -> int:
    """The lane's own per-day ceiling, before pacing.

    A ceiling, not the allowance: `plan_withdrawals` takes this through the #626 engine (variable
    draw, weekends, rest days) and bounds it by the shared account envelope, so a run's budget is
    normally lower than this and never higher.
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

    The stamp line must START with "Sent" — the live rows write exactly "Sent 4 days ago". Merely
    CONTAINING "sent" also matches the headlines "Sales Representative", "Sentiment analysis" and
    "Presenting at ...", and each of those pairs with its own "12 years ago" to date a three-hour-old
    invite at 4380 days. Since `stale` sorts oldest-first, such a row would be withdrawn FIRST, and
    the withdrawal cannot be undone.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    stamped = [line for line in lines if line.lower().startswith("sent")]
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
    hold."""
    if is_automation_paused():
        return automation_pause_reason() or "automation paused"
    if rate_limit_cooldown_remaining() > 0:
        return "LinkedIn 429 breaker open"
    return ""


def show_more_max_clicks() -> int:
    """How many times one run may expand the sent list. Reading is read-only, so this bounds page
    load rather than risk."""
    return _env_int("STALE_INVITE_SHOW_MORE_MAX_CLICKS", SHOW_MORE_MAX_CLICKS_DEFAULT)


# Scroll the last row into view and let the list load the next page under it, then hand back how
# many rows are now rendered. Returning the COUNT is what makes the loop's stop condition an
# observation ("the list stopped growing") rather than a guess ("the control disappeared").
_SCROLL_LIST_JS = r"""
const LABELLED = /^withdraw invitation\b/i;
const rows = Array.from(document.querySelectorAll('[aria-label]'))
    .filter((el) => LABELLED.test(el.getAttribute('aria-label') || ''));
if (rows.length) {
  // The list scrolls inside its own container, not the document — `document.body.scrollHeight`
  // stays put while rows load. Asking the LAST row to bring itself into view moves whichever
  // element actually scrolls, without having to identify it.
  rows[rows.length - 1].scrollIntoView({block: 'end'});
}
window.scrollTo(0, document.body.scrollHeight);
return rows.length;
"""


def _load_more_rows(driver: WebDriver, sleep: Callable[[float], None] = time.sleep) -> int:
    """Scroll the sent list until it stops growing, up to `show_more_max_clicks()` rounds.

    The sent list renders newest-first, so the invites this lane exists for are at the BOTTOM. A run
    that never loads past the first page can only ever see rows it must not touch — which would look
    exactly like "nothing is stale". Returns how many rounds it scrolled, so the report can say the
    walk ran out of road rather than out of stale invites.

    It does NOT click anything. The page has no pager: the only controls matching "show more" are
    the "… show more" headline expanders INSIDE the invite rows (grounded 2026-08-07, #1006), so the
    previous implementation spent its whole budget expanding profile headlines and reported that as
    depth. Rows arrive on SCROLL, and the row count is what says whether they did."""
    rounds = 0
    last_count = -1
    stalled = 0
    for _ in range(show_more_max_clicks()):
        try:
            count = int(driver.execute_script(_SCROLL_LIST_JS) or 0)
        except Exception as e:
            log_debug(f"Sent-invite list expansion failed ({type(e).__name__}: {e})",
                      action_type="invite", task_name="_load_more_rows")
            return rounds
        rounds += 1
        # Two quiet rounds, not one: an infinite-scroll list often needs a beat to fetch, and
        # stopping on the first unchanged count would leave the oldest invites unloaded.
        stalled = stalled + 1 if count <= last_count else 0
        last_count = count
        if stalled >= 2:
            return rounds
        sleep(2.0)
    return rounds


def control_names_row(label_name: str, row_text: str) -> bool:
    """Does the Withdraw control's own label name the person this row is about?

    The live control's label is "Withdraw invitation sent to <Name>" — so unlike almost every other
    surface, this one states WHO the click acts on, and that reading is checked rather than assumed.
    A withdrawal is one-way (LinkedIn blocks re-inviting for weeks), and #1012 cost ~20 invites by
    clicking a control whose label named someone other than the target.

    An empty `label_name` is the unlabelled fallback shape, which carries no name to contradict —
    that reads True, exactly as before this check existed. A name that is NOT in the row's text
    reads False, and the caller refuses the row: a missed withdrawal is recoverable, a wrong one is
    not."""
    name = " ".join(str(label_name or "").split()).casefold()
    if not name:
        return True
    # The name must BE one of the card's lines — the row writes the person's name on its own line,
    # above the headline and the "Sent ..." stamp. Matching it anywhere in the card, or merely as a
    # line PREFIX, would let a mutual-connection blurb ("Ann Lee and 3 others are connections")
    # vouch for a control that names Ann Lee while sitting beside somebody else's row — which is
    # exactly the drift this check exists to catch. A trailing degree badge is the one decoration
    # the name line is allowed to carry.
    for line in str(row_text or "").splitlines():
        candidate = " ".join(line.split()).casefold()
        if candidate == name or _DEGREE_SUFFIX_RE.sub("", candidate).strip() == name:
            return True
    return False


def read_pending_invites(driver: WebDriver) -> list[dict]:
    """Every pending sent invite currently rendered, as
    `{'profile_url', 'name', 'text', 'age_days', 'control', 'label_name', 'entity_ok'}`.

    `age_days` is `None` when the row's "Sent ... ago" stamp could not be read — the caller skips
    those. `entity_ok` is False when the control's label names someone the row does not — also a
    skip. An empty list means the page rendered nothing we recognise, which is NOT the same as "no
    pending invites"; only the caller knows which of those it is looking at."""
    try:
        rows = driver.execute_script(_SENT_INVITE_ROWS_JS, MAX_ROWS_SCANNED)
    except Exception as e:
        log_debug(f"Sent-invite row resolution JS failed ({type(e).__name__}: {e})",
                  action_type="invite", task_name="read_pending_invites")
        return []
    invites: list[dict] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) not in (4, 5):
            continue
        href, name, text, control = row[0], row[1], row[2], row[3]
        label_name = str(row[4] or "").strip() if len(row) == 5 else ""
        text = str(text or "")
        # The live profile links carry no visible text, so the control's own label is what names the
        # person in the withdrawal ledger. Without this the audit trail records blanks.
        name = str(name or "").strip() or label_name
        invites.append({"profile_url": str(href or ""), "name": name,
                        "text": text, "age_days": parse_sent_age_days(text),
                        "control": control, "label_name": label_name,
                        "entity_ok": control_names_row(label_name, text)})
    return invites


def _confirm_withdrawal(driver: WebDriver, sleep: Callable[[float], None]) -> bool:
    """Click the confirmation dialog's Withdraw button. False when no dialog resolved.

    Returning False is NOT "the invite survived" — LinkedIn has shipped both a confirmed and an
    unconfirmed withdrawal, so the caller has to re-read the list either way."""
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

    A read that comes back EMPTY proves nothing and must not be read as absence. `read_pending_invites`
    answers `[]` for a JS failure, a navigation away, an auth wall and a page still loading, so
    "the row is not in this list" would otherwise be satisfied by never having read a list at all —
    and the withdrawal would be recorded as a verified success on the strength of a page we could not
    see. Absence only counts against a list that rendered SOMETHING."""
    if not profile_url:
        return True
    for _ in range(WITHDRAW_CONFIRM_ATTEMPTS):
        sleep(WITHDRAW_CONFIRM_WAIT_SECONDS)
        rows = read_pending_invites(driver)
        if rows and not any(row.get("profile_url") == profile_url for row in rows):
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
            # Re-resolved rows go through the same naming check as the first pass — the re-render is
            # exactly when a control could end up beside the wrong person's link.
            return row.get("control") if row.get("entity_ok", True) else None
    return None


def _record_withdrawal(user_id: int, invite: dict, verified: bool) -> None:
    """One immutable log row per DISPATCHED withdrawal, whatever the verdict.

    `count_invite_withdrawals_today` counts these rows regardless of result, because the budget is
    spent on the CLICK: the action already reached LinkedIn, and a lane whose verification broke must
    not be free to click every row on the page. The result flag is what an operator reads to tell an
    unverified withdrawal apart from a confirmed one."""
    insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                   result=LogResultType.SUCCESS if verified else LogResultType.FAILURE,
                   post_url=invite.get("profile_url") or None,
                   message=STALE_INVITE_WITHDRAWN_MESSAGE)
    record_action(user_id, ACTION_WITHDRAW_INVITE)


def _report(status: str, **fields) -> dict:
    report = {"status": status, "withdrawn": 0, "unverified": 0, "budget": 0, "cap": 0,
              "withdrawn_today": 0, "threshold_days": 0, "rows_seen": 0, "stale_seen": 0,
              "unreadable": 0, "entity_mismatch": 0, "expansions": 0}
    report.update(fields)
    return report


def withdraw_stale_invites(driver: WebDriver, wait: WebDriverWait, user_id: int,
                           plan: Optional[dict] = None,
                           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Withdraw AT MOST today's paced budget of pending invites older than the threshold.

    Returns the run report; the caller turns it into telemetry. `wait` is accepted for symmetry with
    the other Selenium lanes (and so a future selector fallback can use it) — the row resolution
    itself runs in one page-side pass, which is what keeps a 100-row list from costing 100 round
    trips."""
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
    base["entity_mismatch"] = sum(1 for i in invites if not i.get("entity_ok", True))
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

    if base["entity_mismatch"] == len(invites):
        # Every control on the page names someone its own row does not. That is the label wording
        # having moved, not 44 mislabelled rows — and since nothing can be attributed, nothing is
        # withdrawn. One warning at the run level, mirroring the unreadable-date case above.
        log_warning(f"Every pending invite row ({len(invites)}) carries a Withdraw control whose "
                    f"label names a different person — nothing can be attributed, so nothing is "
                    f"withdrawn", user_id=user_id, action_type="invite",
                    task_name="withdraw_stale_invites")
        return _report(WITHDRAW_STATUS_NONE_STALE, **base)

    old_enough = [i for i in invites if i["age_days"] is not None and i["age_days"] >= threshold]
    stale = [i for i in old_enough if i.get("entity_ok", True)]
    refused = len(old_enough) - len(stale)
    if refused:
        # These rows ARE old enough and were refused for a different reason. Without saying so the
        # run reports "no invites older than N days", which is false and reads exactly like a
        # healthy account — the #1012 drift shape hiding as good news.
        log_warning(f"{refused} invite(s) past the {threshold:.0f}-day threshold were refused "
                    f"because the Withdraw control named a different person", user_id=user_id,
                    action_type="invite", task_name="withdraw_stale_invites")
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
