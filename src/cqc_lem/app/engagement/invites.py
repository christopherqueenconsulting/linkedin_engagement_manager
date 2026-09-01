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
from urllib.parse import unquote

from selenium.common import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.utilities.ai.ai_helper import get_ai_message_refinement
from cqc_lem.utilities.db import (
    ACCOUNT_RESTRICTED_MESSAGE,
    ALREADY_CONNECTED_MESSAGE,
    CONNECT_NOTE_MAX_CHARS,
    CONNECTION_REQUEST_SENT_MESSAGE,
    EMAIL_VERIFICATION_REQUIRED_MESSAGE,
    INVITE_LIMIT_REACHED_MESSAGE,
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
from cqc_lem.utilities.linkedin.rate_limit import (
    INVITE_HOLD_DEFAULT_SECONDS,
    LinkedInRateLimited,
    clear_invite_dialog_misses,
    hold_invites,
    invite_hold_reason,
    is_automation_paused,
    is_invites_held,
    record_invite_dialog_miss,
)
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
    element_label,
    find_deep_elements,
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


# A hydrating SDUI profile swaps nodes out from under a walk, so one detached element must not
# blind a page that read perfectly well everywhere else. Two attempts: the second exists for the
# case where `find_elements` ITSELF goes stale mid-enumeration, which no per-element guard can save.
_DEGREE_READ_ATTEMPTS = 2


def _element_text(element) -> str:
    """One guarded read of an element's text; `""` when the node has detached.

    Read ONCE, deliberately: the old code read `.text` twice per element — once to test it, once to
    append it — so the guard and the value could disagree, and the second read was where the page
    had had time to re-render.
    """
    try:
        return element.text or ""
    except StaleElementReferenceException:
        return ""


def _degree_badge_texts(driver) -> "list[str] | None":
    """Every degree-badge text the locator chain can READ, in chain-then-document order, or None
    when the read itself failed. None and [] are different answers: an unreadable page grounds
    nothing, an empty chain on a readable page is the zero worth cross-checking. A matched node with
    no text counts as neither — a locator that resolves to an empty element is as blind as one that
    resolves to nothing.

    Two ways to get None, and only one of them is a defect. A page that went STALE under the walk is
    a hydrating profile behaving normally: it is retried once and, if it loses both attempts, logged
    at DEBUG — warning on it filed a fingerprinted issue twice per profile per attempt for working
    behaviour (#1038/#1039). Any OTHER exception means the read could not happen at all — a dead
    driver, a lost session — and that still warns with `exc=`.
    """
    for attempt in range(_DEGREE_READ_ATTEMPTS):
        texts: "list[str]" = []
        try:
            for by, selector in _PROFILE_DEGREE_LOCATORS:
                for element in driver.find_elements(by, selector):
                    text = _element_text(element)
                    if text.strip():
                        texts.append(text)
        except StaleElementReferenceException as e:
            if attempt + 1 < _DEGREE_READ_ATTEMPTS:
                time.sleep(1)  # let the re-render settle, then walk the chain again
                continue
            log_debug(f"Degree-badge chain went stale on every attempt: {e}",
                      action_type="invite_connect")
            return None
        except Exception as e:
            log_warning("Could not read the connection-degree badge; attempting the invite anyway",
                        exc=e, action_type="invite_connect")
            return None
        return texts
    return None


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
# it that way had no route to the dialog at all.
#
# Re-grounded live 2026-08-31 (#1790): LinkedIn now phrases the profile's OWN top-card button the
# IDENTICAL way as a "People also viewed" rail card's stranger button — both
# `aria-label="Invite <Name> to connect"` (confirmed on `nikunj-bajaj-10476824` and `johnwinner`).
# The blanket `not(starts-with(@aria-label,"Invite"))` exclusion this locator used to carry, built
# to dodge #1012, now excludes the legitimate target's own button too — route 2 fell through on
# both profiles. So the exclusion is gone from the XPath: it matches a bare "Connect" (label or
# text) OR ANY "Invite … to connect" aria-label, and `_click_own_connect_button` decides, in
# Python, which of those candidates is provably the target — never the XPath alone, the same split
# `_click_own_custom_invite_anchor` already uses for the href route below.
_PROFILE_CONNECT_BUTTON_XPATH = (
    '//main//button[normalize-space()="Connect" or @aria-label="Connect" '
    'or starts-with(@aria-label,"Invite ")]'
)

# A button carries no href to attribute by slug, unlike the custom-invite anchor below — so
# identity is checked against the page's OWN <title> instead. LinkedIn writes the loaded profile's
# exact display name there ("<Name> | LinkedIn"), the same name it puts in the button's aria-label,
# so an EXACT, whole-string match (never a prefix or substring) is the discipline
# `_anchor_invite_slug` already applies to hrefs: "Jane" must not match "Janet Doe" — #1012's rule
# read for a name instead of a slug. A bare "Connect" label carries no name at all and is trusted
# outright: the rail's own controls are never bare (#1012's original 2026-08-03 grounding).
_INVITE_LABEL_RE = re.compile(r"^invite\s+(.+?)\s+to connect$", re.IGNORECASE)
_TITLE_PREFIX_RE = re.compile(r"^\(\d+\+?\)\s*")
_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*linkedin\s*$", re.IGNORECASE)
_GENERIC_PROFILE_TITLES = {"linkedin", "feed", "search", "messaging", "notifications"}


def _target_name_from_title(driver) -> str:
    """The loaded profile's own display name, off the page's `<title>` ('<Name> | LinkedIn').

    Best-effort and read-only: `""` when the title is unreadable or reads like a non-profile shell
    page, never a guess. Mirrors `_name_from_title` in `scrapper.py`, applied to `driver.title`
    directly rather than a BeautifulSoup-parsed source.
    """
    try:
        title = driver.title or ""
    except Exception:
        return ""
    name = _TITLE_SUFFIX_RE.sub("", _TITLE_PREFIX_RE.sub("", title)).strip()
    if not name or name.lower() in _GENERIC_PROFILE_TITLES:
        return ""
    return name


def _connect_button_names_target(label: str, target_name: str) -> bool:
    """Whether a candidate Connect control's OWN label may be trusted for THIS loaded profile."""
    label = " ".join((label or "").split()).strip()
    if label.lower() == "connect":
        return True
    match = _INVITE_LABEL_RE.match(label)
    if not match or not target_name:
        return False
    candidate = " ".join(match.group(1).split()).strip().lower()
    target = " ".join(target_name.split()).strip().lower()
    return bool(candidate) and candidate == target


def _click_own_connect_button(driver, wait, user_id: int) -> bool:
    """Click the target's own top-card Connect button and report whether the dialog opened.

    The button-route counterpart to `_click_own_custom_invite_anchor` below: every candidate is
    walked in document order and refused unless `_connect_button_names_target` can attribute it to
    the loaded profile, so a "People also viewed" rail card's identically-phrased stranger button
    can never be the one clicked (#1790). THIS element is clicked, never a re-lookup — a re-lookup
    would hand a re-render whichever button the XPath happens to yield first.
    """
    try:
        candidates = driver.find_elements(By.XPATH, _PROFILE_CONNECT_BUTTON_XPATH)
    except Exception as e:
        log_debug(f"Could not enumerate profile Connect buttons: {e}", user_id=user_id,
                  action_type="invite_connect")
        return False
    if not candidates:
        return False

    # Read only when there is something to check identity against — most misses never render a
    # Connect-shaped button at all, and reading the title is wasted work on every one of them.
    target_name = _target_name_from_title(driver)
    for button in candidates:
        if not _connect_button_names_target(element_label(button), target_name):
            continue
        try:
            if not button.is_displayed():
                continue
            wait.until(EC.element_to_be_clickable(button))
            ActionChains(driver).move_to_element(button).click().perform()
        except Exception as e:
            log_debug(f"Profile Connect button was not clickable: {e}", user_id=user_id,
                      action_type="invite_connect")
            return False
        # The outcome is the gate, never the click (docs/sdui-selenium-notes.md).
        return _connect_dialog_present(driver, wait, user_id)
    return False

# Re-grounded live 2026-08-29 (#1733, three profiles, docs/sdui-selenium-notes.md). Two layouts, one
# shared truth, and one dead route:
#
#   * layout A (2 of 3 profiles): NO Connect control on the top card at all. Connect lives in the
#     More menu as `<a role="menuitem" href="/preload/custom-invite/?vanityName=<slug>">Connect</a>`.
#   * layout B (1 of 3): a top-card `<a aria-label="Invite <Owner> to connect"
#     href="/preload/custom-invite/?vanityName=<slug>">Connect</a>` and NO Connect item in the More
#     menu — so neither layout is "the" layout and both routes have to stay.
#   * DEAD: `driver.get(<that URL>)`. The preload route is an in-app route, not a page. Navigating
#     to it directly now renders a COMPLETELY blank document — empty `<main>`, empty `<body>`, not
#     one control. That is why all three shipped routes missed on every profile: the URL route
#     navigated there, and the More-menu route CLICKED a link to the same place. The link must be
#     clicked in-app, never navigated to.
#
# `_PROFILE_CONNECT_BUTTON_XPATH` above cannot see layout B either: the control is an `<a>`, and
# that chain matches `//main//button` only.
#
# The href is a HARDER #1012 guard than any label. A suggestion-rail control for a stranger carries
# THAT STRANGER's `vanityName`, so requiring the anchor's own slug to equal the target's is
# machine-checkable identity rather than name-matching — and it is checked in Python, so no locator
# literal here names a person and the rail-hazard regression test needs no edit to accommodate it.
_CUSTOM_INVITE_ANCHOR_XPATH = '//a[contains(@href,"custom-invite")]'
_VANITY_NAME_RE = re.compile(r"[?&]vanityName=([^&#]+)", re.IGNORECASE)


def _anchor_invite_slug(element) -> str:
    """The slug an anchor's own custom-invite href names, lowercased.

    `""` when unreadable, and the caller refuses that: a control we cannot attribute is precisely
    the one never to click.
    """
    try:
        href = element.get_attribute("href") or ""
    except Exception:
        return ""
    match = _VANITY_NAME_RE.search(href)
    if not match:
        return ""
    return unquote(match.group(1)).strip().strip("/").lower()


def _click_own_custom_invite_anchor(driver, wait, user_id: int, slug: str) -> bool:
    """Click the TARGET's own custom-invite link and report whether the dialog opened.

    Serves both layouts: the top-card anchor and the More-menu item are the same element shape, so
    one reader covers them and a third rotation between the two costs nothing.

    Refuses every anchor whose `vanityName` is not exactly `slug` — the rail's anchors are that
    check's whole purpose. Exact equality, never a prefix: `chris` must not match `chris-queen`.
    """
    if not slug:
        return False
    try:
        anchors = driver.find_elements(By.XPATH, _CUSTOM_INVITE_ANCHOR_XPATH)
    except Exception as e:
        log_debug(f"Could not enumerate custom-invite anchors: {e}", user_id=user_id,
                  action_type="invite_connect")
        return False

    wanted = slug.strip().strip("/").lower()
    for anchor in anchors:
        if _anchor_invite_slug(anchor) != wanted:
            continue
        try:
            if not anchor.is_displayed():
                continue
            # THIS element, never a re-lookup: re-finding by the XPath would click whichever
            # custom-invite anchor the page happens to yield first, which on a profile carrying the
            # suggestion rail is a stranger's. That re-lookup IS #1012.
            wait.until(EC.element_to_be_clickable(anchor))
            ActionChains(driver).move_to_element(anchor).click().perform()
        except Exception as e:
            log_debug(f"Custom-invite anchor was not clickable: {e}", user_id=user_id,
                      action_type="invite_connect")
            return False
        # The outcome is the gate, never the click (docs/sdui-selenium-notes.md).
        return _connect_dialog_present(driver, wait, user_id)
    return False


# The vocabulary LinkedIn uses when the ACCOUNT, not the profile, is why no dialog rendered. Mirrors
# `invite_limit_signal` in scripts/linkedin_live_validation.py — the probe imports production, never
# the other way round, so the two lists are pinned against each other by a unit test instead.
_INVITE_LIMIT_RE = re.compile(
    r"weekly invitation limit"
    r"|reached the (?:weekly )?limit"
    r"|maximum number of invitations"
    r"|you(?:'|’)?ve (?:used all|reached) your invitation"
    r"|no invitations? (?:left|remaining)"
    r"|try again (?:in|next week)",
    re.IGNORECASE)
_ACCOUNT_RESTRICTED_RE = re.compile(
    r"we(?:'|’)?ve restricted your account"
    r"|your account has been (?:temporarily )?restricted"
    r"|temporarily restricted",
    re.IGNORECASE)


def _invite_restriction_reason(driver) -> "str | None":
    """Which account-level wall the page NAMES, or None.

    Reads `main`, `body` and every open dialog, because the notice renders outside `main` as often
    as in it. Returns None on an unreadable page and that is deliberate: a restriction is a claim,
    a claim needs evidence, and an unreadable page must fall through to the ordinary miss rather
    than manufacture an account-wide hold out of a failed read.
    """
    chunks = []
    for selector in ("main", "body", "[role='dialog'], [role='alertdialog'], dialog"):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = _element_text(element)
                if text:
                    chunks.append(text)
        except Exception:
            continue
    body = " ".join(chunks)
    if not body.strip():
        return None
    if _ACCOUNT_RESTRICTED_RE.search(body):
        return ACCOUNT_RESTRICTED_MESSAGE
    if _INVITE_LIMIT_RE.search(body):
        return INVITE_LIMIT_REACHED_MESSAGE
    return None


# Grounded live 2026-08-29 (#1733): the Connect dialog MOVED INTO AN OPEN SHADOW ROOT (the overlay
# layer hosted on `div.theme--light`), which is the same rotation #1621 found under the share-box
# composer. Its controls are unchanged — `Add a note` / `Send without a note` / `Send invitation`,
# `textarea#custom-message` — but neither XPath nor `driver.find_elements` crosses a shadow
# boundary, so an OPEN dialog read EXACTLY like one that never opened. That is why every route
# reported a miss while the click behind it was working the whole time.
#
# So every lookup from here to the Send click goes through `find_deep_elements`, which means CSS
# only (XPath cannot address a shadow tree at all) and label matching in Python. The XPath locators
# above are kept as the light-DOM first pass — cheap, and still correct for an account that has not
# been moved to the shadow-mounted overlay yet.
_CONNECT_DIALOG_CONTROL_CSS = "button, a, [role='button']"
# The dialog itself. Searched first so the control scan's budget is spent INSIDE the overlay rather
# than on the page chrome that precedes it in document order (#1813).
_CONNECT_DIALOG_CONTAINER_CSS = "[role='dialog'], [role='alertdialog'], dialog"
_SEND_WITHOUT_NOTE_LABEL = "send without a note"
_ADD_NOTE_LABEL = "add a note"
_SEND_INVITATION_LABEL = "send invitation"


def _dialog_control_candidates(driver) -> list:
    """The controls to search for a dialog button, nearest surface first.

    Scoped to the OPEN DIALOG before the document, because `find_deep_elements` stops after `limit`
    matches **in document order** and the overlay is mounted last. On a profile page the first sixty
    visible controls are the global nav, the top card and the "People also viewed" rail, so a
    document-wide scan spent its whole budget before reaching the dialog and reported an open dialog
    as absent — #1813, measured in production 2026-09-01, where the SAME run read
    `Add a note / Send without a note` out of the dialog container while the control scan returned
    the nav bar.

    Scoping is also a harder #1012 guard than any label: a control found inside the invite dialog
    cannot be a rail card's button for a stranger, because the rail is not in the dialog.

    The document-wide pass is kept as the fallback for a rotation that mounts these controls without
    a dialog role — losing that would trade one blind spot for another.
    """
    for container in find_deep_elements(driver, _CONNECT_DIALOG_CONTAINER_CSS,
                                        visible_only=True, limit=3):
        scoped = find_deep_elements(driver, _CONNECT_DIALOG_CONTROL_CSS,
                                    visible_only=True, limit=60, root=container)
        if scoped:
            return scoped
    return find_deep_elements(driver, _CONNECT_DIALOG_CONTROL_CSS, visible_only=True, limit=60)


def _deep_dialog_control(driver, labels: "tuple[str, ...]"):
    """The first visible dialog control matching one of `labels`, shadow roots included.

    Ordered by `labels`, not document order: the caller's first label is its most exact intent, and
    settling for a later one when an earlier matches is how a walk presses the control next to the
    one it wanted (#1012).
    """
    controls = _dialog_control_candidates(driver)
    for wanted in labels:
        for control in controls:
            if element_label(control).startswith(wanted):
                return control
    return None


def _connect_dialog_present(driver, wait, user_id: int) -> bool:
    """Whether the Connect dialog's OWN controls are on screen — light DOM or shadow root."""
    if find_first(driver, wait, _CONNECT_DIALOG_LOCATORS, "Connect invite dialog",
                  required=False, warn_on_miss=False, max_try=1, visible_only=True,
                  user_id=user_id) is not None:
        return True
    return _deep_dialog_control(driver, (_SEND_WITHOUT_NOTE_LABEL, _ADD_NOTE_LABEL)) is not None


# Class C (#1836): a subset of targets render an EMAIL-VERIFICATION variant of the Connect dialog —
# same `Add a note to your invitation?` heading, same `Add a note` / `Send without a note` controls
# _connect_dialog_present already matches, but it refuses to send without the recipient's email. Two
# variants captured production 2026-09-01 (both fixtures below, verbatim):
#
#   verification: "...To verify this member knows you, please enter their email to connect. You can
#                  also include a personal note. Learn why Add a note Send without a note..."
#   ordinary:     "...Personalize your invitation to Kaitlyn Albertoli by adding a note. LinkedIn
#                  members are more likely to accept invitations that include a note...."
#
# The prose is the only thing that differs, so the input is the primary signal and the prose is the
# fallback for a rotation that renders the notice differently but keeps the same input.
_CONNECT_DIALOG_EMAIL_INPUT_CSS = "input[type='email']"
_CONNECT_DIALOG_WANTS_EMAIL_RE = re.compile(r"enter their email", re.IGNORECASE)


def _connect_dialog_wants_email_from_text(text: "str | None") -> "bool | None":
    """Pure text → three-valued reading.

    Split out from `_connect_dialog_wants_email` so the exact production strings above can be
    asserted against it directly, with no driver to mock.

    Three-valued, matching `ThreadState`'s posture: unreadable text is UNKNOWN, never "wants email"
    — an undetected verification variant must fall through to the ordinary send/miss path rather
    than manufacture a false skip.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    return bool(_CONNECT_DIALOG_WANTS_EMAIL_RE.search(normalized))


def _connect_dialog_wants_email(driver) -> "bool | None":
    """Whether the OPEN Connect dialog is the email-verification variant (issue #1836).

    Scoped to the dialog CONTAINER the same way `_overlay_evidence` is, so a rail card's unrelated
    text or input can never answer this. Prefers the input itself; the prose is a fallback.
    """
    containers = find_deep_elements(driver, _CONNECT_DIALOG_CONTAINER_CSS, visible_only=True, limit=3)
    if not containers:
        return None
    for container in containers:
        if find_deep_elements(driver, _CONNECT_DIALOG_EMAIL_INPUT_CSS, visible_only=True, limit=1,
                              root=container):
            return True
    text = " ".join(_element_text(container) for container in containers)
    return _connect_dialog_wants_email_from_text(text)


def _find_connect_dialog_email_input(driver):
    """The OPEN dialog's email input, shadow root included, or None."""
    for container in find_deep_elements(driver, _CONNECT_DIALOG_CONTAINER_CSS, visible_only=True,
                                        limit=3):
        found = find_deep_elements(driver, _CONNECT_DIALOG_EMAIL_INPUT_CSS, visible_only=True,
                                   limit=1, root=container)
        if found:
            return found[0]
    return None


def _fill_connect_dialog_email(driver, email: str, user_id: int) -> bool:
    """Type a known email into the OPEN Connect dialog's verification input (issue #1836).

    THE ELEMENT THAT ANSWERED is what gets typed into — a shadow-mounted control cannot be re-found
    by a fresh query that never saw it (#1733). A failure here is a genuine degraded path (the input
    was proven present moments ago), so it warns; the email itself never appears in the message.
    """
    field = _find_connect_dialog_email_input(driver)
    if field is None:
        return False
    try:
        field.click()
        field.clear()
        field.send_keys(email)
        return True
    except Exception as e:
        log_warning("Could not type the recipient's email into the Connect dialog", exc=e,
                    user_id=user_id, action_type="invite_connect")
        return False


# What the overlay evidence below reads. Same control shape `_deep_dialog_control` matches, so the
# dump describes the surface the dialog check actually looked at rather than a neighbouring one.
_OVERLAY_DIALOG_CSS = _CONNECT_DIALOG_CONTAINER_CSS
_OVERLAY_TEXT_LIMIT = 400


def _overlay_evidence(driver) -> "tuple[list[str], str]":
    """What a SHADOW-PIERCING scan can see that the light DOM cannot: controls, then notice text.

    #1733 established that the Connect dialog mounts inside an open shadow root and taught
    `_connect_dialog_present` to cross that boundary. The miss evidence beside it was left on
    `driver.find_elements`, which cannot — so every dump describes the profile page and none
    describes what happened after the click, which is the only interesting part of a miss.

    Two readings, because a miss has two shapes and they need different evidence:

    - **controls** answers "did an overlay render at all". Only labels the light-DOM `main` pass
      cannot reach are worth printing; repeating the profile's own buttons would bury the signal.
    - **text** answers "did it render a wall notice". `_invite_restriction_reason` reads the light
      DOM only, so a limit or restriction message mounted in the overlay is invisible to it AND
      returns None — indistinguishable from a selector that simply missed. This prints the words so
      a human can tell the two apart without a live session.

    Best-effort, like the rest of the evidence path: `find_deep_elements` returns `[]` rather than
    raising when the query cannot run, and evidence must never cost the run.
    """
    controls: list = []
    for control in find_deep_elements(driver, _CONNECT_DIALOG_CONTROL_CSS,
                                      visible_only=True, limit=60):
        label = element_label(control)
        if label and label not in controls:
            controls.append(label[:60])
        if len(controls) >= 25:
            break
    text = " ".join(_element_text(container)
                    for container in find_deep_elements(driver, _OVERLAY_DIALOG_CSS,
                                                        visible_only=True, limit=3))
    return controls, " ".join(text.split())[:_OVERLAY_TEXT_LIMIT]


def _miss_evidence(driver) -> str:
    """A one-line description of what the page DID offer, for the total-miss log.

    Twenty identical `NO_CONNECT_BUTTON_MESSAGE` failures over 17 days produced zero diagnosable
    artifacts and needed a live session to explain (#1733). The next rotation should be readable
    from the log line it writes. Best-effort — evidence must never cost the run.

    Reads BOTH document layers (#1813). The light-DOM half describes the profile; the overlay half
    describes the dialog surface, and a miss where those two disagree — affordance present, overlay
    empty — is a different defect from one where neither has anything.
    """
    try:
        anchors = [(a.get_attribute("href") or "")[:120]
                   for a in driver.find_elements(By.XPATH, _CUSTOM_INVITE_ANCHOR_XPATH)[:5]]
    except Exception:
        anchors = []
    try:
        labels = []
        for control in driver.find_elements(
                By.CSS_SELECTOR, "main button, main a, main [role='button']")[:60]:
            label = (control.get_attribute("aria-label") or control.text or "").strip()
            if label and label not in labels:
                labels.append(label[:60])
            if len(labels) >= 25:
                break
    except Exception:
        labels = []
    try:
        overlay_controls, overlay_text = _overlay_evidence(driver)
    except Exception:
        overlay_controls, overlay_text = [], ""
    seen = {label.lower() for label in labels}
    overlay_only = [label for label in overlay_controls if label not in seen]
    return (f"custom-invite anchors={anchors} main controls={labels} "
            f"overlay controls={overlay_only} overlay text={overlay_text!r}")


def _open_connect_invite_dialog(driver, wait, user_id: int,
                                profile_url: str) -> "tuple[bool, str | None]":
    """Open the Connect invite dialog for the profile at `profile_url`.

    Four routes, cheapest and safest first (re-grounded 2026-08-29, #1733):

    1. the target's OWN custom-invite anchor already on the profile page — layout B's top-card
       `<a>`, which the button route below cannot see because it is not a `<button>`;
    2. the direct top-card Connect BUTTON (#1734), for the accounts that render one — attributed to
       the target by its own label (`_connect_button_names_target`) rather than excluded outright,
       since LinkedIn now phrases the target's OWN button the same way as a rail stranger's (#1790);
    3. the More menu, then the target's own custom-invite anchor inside it — layout A, where the
       top card carries no Connect control at all;
    4. navigating the custom-invite URL, last and expected to fail: that route is an in-app route,
       and a direct `driver.get` of it now renders a blank document. It is kept only so an account
       for which it still works is not regressed, and it is why routes 1 and 3 CLICK the link.

    Returns `(opened, restriction_reason)`. `opened` is True only when the dialog's own controls are
    provably present — a landed click is never success. False is an ordinary outcome (invite already
    pending, Connect not offered, or the SDUI rotated again) and is why the total miss is a WARNING,
    not an error (issue #571). `restriction_reason` is set only when the page NAMES an account-level
    wall, which is a different operator action entirely (#1733).
    """
    slug = profile_slug(profile_url)

    if profile_url != driver.current_url:
        driver.get(profile_url)

    if _click_own_custom_invite_anchor(driver, wait, user_id, slug):
        log_info("Connect dialog opened via the profile's own custom-invite link")
        return True, None

    if _click_own_connect_button(driver, wait, user_id):
        log_info("Connect dialog opened via the profile page's direct Connect button")
        return True, None

    if click_first(driver, wait, _PROFILE_MORE_MENU_LOCATORS, "Profile More menu",
                   required=False, warn_on_miss=False, max_try=1, use_action_chain=True,
                   user_id=user_id) is not None:
        # The menu's Connect item is the same slug-bearing anchor as layout B's, so it goes through
        # the same attribution rather than a locator that would click whichever menu item matched.
        if _click_own_custom_invite_anchor(driver, wait, user_id, slug):
            log_info("Connect dialog opened via the profile More menu")
            return True, None
        item = click_first(driver, wait, _CONNECT_MENU_ITEM_LOCATORS, "Connect menu item",
                           required=False, warn_on_miss=False, max_try=1, use_action_chain=True,
                           user_id=user_id)
        if item is not None and _connect_dialog_present(driver, wait, user_id):
            log_info("Connect dialog opened via the profile More menu")
            return True, None

    # Taken HERE, while the PROFILE is still loaded: route 4 navigates away to a page that renders
    # nothing, and evidence gathered there would describe the blank page rather than the miss.
    evidence = _miss_evidence(driver)
    restriction = _invite_restriction_reason(driver)

    if slug and restriction is None:
        driver.get(_CONNECT_INVITE_URL.format(slug=slug))
        if _connect_dialog_present(driver, wait, user_id):
            log_info("Connect dialog opened via the custom-invite URL")
            return True, None
        # #1733 called this route's page blank, measured with light-DOM reads — which is exactly
        # what a shadow-mounted overlay looks like. If the in-app route DID render something, this
        # is the only place it can be seen, so the evidence is extended rather than discarded.
        try:
            url_controls, url_text = _overlay_evidence(driver)
        except Exception:
            url_controls, url_text = [], ""
        evidence = f"{evidence} | after url route: controls={url_controls} text={url_text!r}"

    if restriction:
        # INFO, not a warning: this is the detector working. The hold the caller sets is the record
        # that matters, and warning here would file a grouped defect for an account fact.
        log_info(f"No Connect dialog because the account is walled: {restriction}")
        return False, restriction

    log_warning("No route opened the Connect invite dialog for this profile",
                user_id=user_id, action_type="invite_connect")
    log_info(f"Connect-dialog miss evidence for {profile_url}: {evidence}")
    return False, None


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
    note_button = (find_first(driver, wait, _CONNECT_NOTE_BUTTON_LOCATORS, "Add a note button",
                              required=False, warn_on_miss=False, max_try=1, visible_only=True,
                              user_id=user_id)
                   or _deep_dialog_control(driver, (_ADD_NOTE_LABEL,)))
    if note_button is None:
        if (find_first(driver, wait, _CONNECT_BARE_SEND_LOCATORS, "Send without a note",
                       required=False, warn_on_miss=False, max_try=1, visible_only=True,
                       user_id=user_id) is not None
                or _deep_dialog_control(driver, (_SEND_WITHOUT_NOTE_LABEL,)) is not None):
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
        # The affordance answered a moment ago, so failing here is a real degraded path — it raises
        # into the warning below rather than reading as "not on offer". THE ELEMENT THAT ANSWERED is
        # what gets clicked: a shadow-mounted control cannot be re-found by the XPath that never saw
        # it (#1733).
        note_button.click()

        message_box = next(iter(find_deep_elements(driver, "textarea#custom-message",
                                                   visible_only=True, limit=2)), None)
        if message_box is None:
            message_box = click_element_wait_retry(
                driver, wait, '//textarea[@id="custom-message"]', "Finding Message Box",
                max_retry=1, use_action_chain=True)
        else:
            message_box.click()
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

    A `StaleElementReferenceException` is a DIFFERENT failure than a missing button: the dialog's
    own animation (or the note step just before this) can swap the Send button's DOM node out from
    under `click_element_wait_retry` between it locating the element and clicking it —
    `click_element_wait_retry` itself never retries that case (issue #1745). One re-locate-and-click
    attempt per label rides that race out before it is treated as "no Send button at all".
    """
    xpaths = _SEND_INVITE_XPATHS if with_note else tuple(reversed(_SEND_INVITE_XPATHS))
    last_error: Exception = None
    for xpath in xpaths:
        for attempt in range(2):
            try:
                click_element_wait_retry(driver, wait, xpath, "Finding Send Connection Button",
                                         max_retry=1, use_action_chain=True)
                log_info("Found Send Connection Button and clicked it")
                return True
            except StaleElementReferenceException as e:
                last_error = e
                if attempt == 0:
                    log_debug("Send button went stale mid-click — retrying", user_id=user_id,
                             action_type="invite_connect")
                    continue
            except Exception as e:
                last_error = e  # wrong label for this dialog state — try the other one
                break

    # The same two labels, across the shadow boundary the XPaths above cannot cross (#1733). The
    # preference order still follows `with_note`, so the two passes can never disagree about which
    # Send this dialog state wants.
    labels = ((_SEND_INVITATION_LABEL, _SEND_WITHOUT_NOTE_LABEL) if with_note
              else (_SEND_WITHOUT_NOTE_LABEL, _SEND_INVITATION_LABEL))
    for attempt in range(2):
        send = _deep_dialog_control(driver, labels)
        if send is None:
            break
        try:
            send.click()
            log_info("Found Send Connection Button in the dialog's shadow root and clicked it")
            return True
        except StaleElementReferenceException as e:
            last_error = e
            if attempt == 0:
                log_debug("Shadow-mounted Send button went stale mid-click — retrying",
                          user_id=user_id, action_type="invite_connect")
                continue
        except Exception as e:
            last_error = e
            break

    # exc= is what turns this into a fingerprinted PostHog issue (the loop that filed #573); an
    # exc-less log_error only reaches Logs, so the one failure we deliberately keep as an error
    # would never page anyone.
    log_error("Failed to send the connection request (no Send button on the open Connect dialog)",
              exc=last_error, user_id=user_id, action_type="invite_connect")
    return False


def invite_to_connect_now(user_id: int, profile_url: str, message: str = None,
                          recipient_email: str = None) -> "tuple[bool, str]":
    """Core connect-invite send: open the profile, click Connect (+ optional note), log the result.
    Returns (sent, result_message) — the message is the failure reason when `sent` is False, which
    the proactive flow stores on the request row (issue #623). Shared by invite_to_connect (reactive
    profile-viewer flow) and send_connection_request (issue #398 approval-gated proactive flow) so
    both use the same send + log path (mirrors send_dm_now). Re-raises LinkedInRateLimited when the
    kill-switch / 429 breaker is open so callers can defer rather than record a false failure.

    `recipient_email` (issue #1836) is used ONLY when the dialog that opens turns out to be the
    email-verification variant; `invite_to_connect` and `send_roster_connect_invite` never pass one,
    so this defaults to None and their callers are unchanged.
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
        opened, restriction = _open_connect_invite_dialog(driver, wait, user_id, profile_url)
        if not opened:
            # A wall LinkedIn NAMED holds the whole lane; a route that merely missed counts toward
            # the miss streak, so a dead selector cannot turn a queue backlog into a burst of
            # automated profile visits the invite cap never sees (#1732).
            reason = restriction or NO_CONNECT_BUTTON_MESSAGE
            if restriction:
                hold_invites(user_id, INVITE_HOLD_DEFAULT_SECONDS, reason=restriction)
            else:
                record_invite_dialog_miss(user_id)
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.FAILURE, post_url=profile_url,
                           message=reason)
            return False, reason

        # Class C (#1836): the dialog opened, but it may be the email-verification variant, which
        # will not accept the invite without the recipient's email. A target fact — LinkedIn is
        # deliberately gating THIS person, not a selector that missed — so it never reaches
        # record_invite_dialog_miss / hold_invites above, both of which are for a dialog that never
        # opened at all.
        wants_email = _connect_dialog_wants_email(driver)
        if wants_email and not recipient_email:
            log_debug("Connect dialog demands an email we do not have; cannot send", user_id=user_id,
                     action_type="invite_connect")
            result = EMAIL_VERIFICATION_REQUIRED_MESSAGE
        else:
            if wants_email:
                _fill_connect_dialog_email(driver, recipient_email, user_id)

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
            clear_invite_dialog_misses(user_id)  # the route works; the streak starts over
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
    if reason in (INVITE_LIMIT_REACHED_MESSAGE, ACCOUNT_RESTRICTED_MESSAGE):
        # The wall is the account, so the target's one shot was never spent — hand it back to the
        # ladder exactly as a throttle does, rather than badging a reachable person 'failed'.
        set_target_connect_status(user_id, profile_url, ConnectStatus.NEEDS_CONNECTION)
        log_debug(f"Roster connection request deferred: {reason}", user_id=user_id,
                  action_type="invite_connect", task_name="send_roster_connect_invite")
        return f"Roster connection request deferred: {reason}"
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
        EMAIL_VERIFICATION_REQUIRED_MESSAGE,
        ConnectionRequestStatus,
        count_invites_sent_today,
        get_connection_request,
        record_connection_request_attempt,
        update_connection_request_status,
    )
    req = get_connection_request(request_id)
    if not req or req["status"] not in (ConnectionRequestStatus.APPROVED, ConnectionRequestStatus.SENDING):
        return f"Connection request {request_id} not sendable (status={req['status'] if req else 'missing'})"

    user_id = req["user_id"]
    if is_invites_held(user_id):
        # Re-checked here as well as in the scanner, the same way the daily cap is: the row may have
        # been dispatched before the wall was detected. Deferred to APPROVED, never FAILED — nothing
        # was attempted, so nothing failed.
        reason = invite_hold_reason(user_id) or "connection invites are held"
        log_debug(f"send_connection_request: invites held, deferring {request_id}: {reason}",
                  user_id=user_id, action_type="invite_connect",
                  task_name="send_connection_request")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED,
                                         failure_reason=reason)
        return f"Connection request {request_id} deferred ({reason})"

    prefs = get_engagement_preferences(user_id)
    if count_invites_sent_today(user_id) >= int(prefs.get("max_invites_per_day") or 0):
        log_info(f"send_connection_request: daily invite cap reached for user {user_id}; deferring {request_id}")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (daily invite cap reached)"

    try:
        sent, reason = invite_to_connect_now(user_id, req["recipient_profile_url"], req["message"],
                                             recipient_email=req.get("recipient_email"))
    except LinkedInRateLimited as e:
        # DEBUG, matching the other two wrappers: the request stays 'approved' and the next scan
        # picks it up, so nothing was lost and nothing here is new information.
        log_debug(f"send_connection_request: throttled, deferring {request_id}: {e}",
                  user_id=user_id, action_type="invite_connect",
                  task_name="send_connection_request")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (LinkedIn throttled)"
    if sent:
        update_connection_request_status(request_id, ConnectionRequestStatus.SENT)
        return f"Connection request {request_id} -> sent"
    if reason == EMAIL_VERIFICATION_REQUIRED_MESSAGE:
        # Class C (#1836): a target fact, not selector drift or an account wall — terminal on the
        # FIRST occurrence rather than record_connection_request_attempt's ceiling. Retrying without
        # a new email would spend up to three ~90s Chrome sessions learning nothing new; `retry`
        # (PUT action=retry, refused for an agent session) is the way back in once one is supplied.
        update_connection_request_status(request_id, ConnectionRequestStatus.FAILED, failure_reason=reason)
        log_debug(f"Connection request {request_id} failed: {reason}", user_id=user_id,
                 action_type="invite_connect", task_name="send_connection_request")
        return f"Connection request {request_id} -> failed ({reason})"
    # A real attempt reached LinkedIn and did not send (issue #1814) — this counts toward the
    # ceiling, unlike the three defers above. The reason was already logged at its owning step; a
    # warning here would only fork a second grouped issue for the same invite (#1038).
    log_debug(f"Connection request {request_id} failed: {reason}", user_id=user_id,
              action_type="invite_connect", task_name="send_connection_request")
    terminal, attempts = record_connection_request_attempt(request_id, reason)
    if terminal:
        # Escalating on purpose (issue #1814): a target that survives the ceiling has genuinely
        # never been reachable, and the recurrence rule promotes a repeat of this to ERROR — which
        # is the intent, not noise, since it stops costing a browser session every cycle.
        log_warning(f"Connection request {request_id} exhausted its attempts; giving up: {reason}",
                    user_id=user_id, action_type="invite_connect", task_name="send_connection_request")
        return f"Connection request {request_id} -> failed (attempt {attempts}, giving up)"
    return f"Connection request {request_id} deferred for retry (attempt {attempts}): {reason}"


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
