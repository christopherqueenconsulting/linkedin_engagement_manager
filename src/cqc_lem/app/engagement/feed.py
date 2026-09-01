"""Feed, groups and roster — the walk that decides which posts LEM engages, and in what order.

Step 3 of the `run_automation.py` split (#1154), and the largest cluster: nine tasks and the whole
SDUI feed engine. The three contexts move as ONE module because they are one graph, not three.

**Roster owns no task at all.** `comment_on_roster_posts`, `auto_follow_roster_target`,
`advance_roster_connect` and ~20 more are reached only from `comment_on_feed_inline` — roster is
feed's tail, engaged with the same card engine under the same caps.

**Groups is barely its own cluster.** `auto_comment_in_groups` hard-calls `comment_on_feed_inline`
(300 lines), which has exactly TWO direct module-level readers in the whole tree: that task and
`automate_commenting`. Splitting groups off would drag the feed engine across a module boundary for
one call, so the design's abort criterion for this step was a THIRD reader appearing. It has not.

**Every task here pins `name='cqc_lem.app.run_automation.<fn>'`, and that is load-bearing.** Celery
derives a task's name from `<module>.<function>`, so moving one RENAMES it silently: six of these
nine are named as plain strings in `celeryconfig.task_routes` and would stop matching, messages
already queued under the old name would be rejected `NotRegistered` and dropped, and the `QueueOnce`
lock key embeds the task name, so it would re-key mid-deploy. `scripts/restructure/celery_inventory.py`
diffed across the move is what proves none of that happened.

`automate_commenting` re-queues ITSELF with `globals()[current_function_name].apply_async`.
`current_function_name` is `frame.f_code.co_name`, so the lookup reads THIS module's globals and
stays correct — but only because the task and its module moved together. Never split or wrap it.

The module imports NOTHING from `run_automation` — that is what keeps the dependency one-way, since
`run_automation` imports the nine tasks (plus `get_feed_funnel`, which `api/routers/user.py` reads
from there) back. `_report_zero_walk` is the one name that had to be re-sourced: it stays in
`run_automation` too, because the catch-up walk still reads it, so both modules alias
`utilities/linkedin/zero_walk.py` rather than one importing the other.

Posture for every lane below — recency-dominant scoring, the sort-state contract (#817), roster
follow/connect escalation (#962/#979), the weekly group post (#932) — is
`docs/engagement-automation.md`.
"""

import hashlib
import inspect
import json
import math
import os
import random
import re
import time
from datetime import datetime
from enum import StrEnum
from typing import Callable, NamedTuple, Optional, Tuple

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from selenium.common import (
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

# `queue_roster_connect_invite` dispatches the connect rail's task (#1154); the rail moved to
# `app.engagement.invites` first and imports nothing from here, so the edge runs one way.
from cqc_lem.app.engagement.invites import send_roster_connect_invite
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.domain.models import FeedRunContext
from cqc_lem.utilities import golden_hour as _golden
from cqc_lem.utilities.ai import story_bank as _story_bank
from cqc_lem.utilities.ai.ai_helper import (
    choose_post_reaction,
    generate_ai_response,
    generate_group_post,
    generate_second_wave_comment,
    generate_seed_comment,
    get_or_create_profile_synthesis,
    post_is_relevant,
)
from cqc_lem.utilities.ai.content_alignment import (
    append_link_to_comment,
)
from cqc_lem.utilities.ai.content_framework import select_blueprint
from cqc_lem.utilities.connection_targeting import (
    SOURCE_ROSTER,
    ScoredCandidate,
    target_terms_from_prefs,
)
from cqc_lem.utilities.db import (
    ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK,
    ENGAGEMENT_TARGET_CONNECT_TERMINAL,
    ENGAGEMENT_TARGET_FOLLOW_TERMINAL,
    ROSTER_FOLLOWS_PER_DAY_DEFAULT,
    ConnectStatus,
    FollowStatus,
    GroupPostDraftStatus,
    GroupPostMediaType,
    LogActionType,
    LogResultType,
    claim_post_for_comment,
    count_comments_today,
    count_invites_sent_today,
    count_open_connection_requests,
    count_user_comments_on_post_url,
    create_group_post_draft,
    disable_user_groups,
    get_duplicate_comment_posts,
    get_enabled_group_ids,
    get_engagement_preferences,
    get_engagement_targets,
    get_group_post_draft,
    get_open_group_post_draft,
    get_post_age_minutes,
    get_post_content,
    get_post_first_comment_link,
    get_post_message_from_log_for_user,
    get_post_url_from_log_for_user,
    get_recent_comment_texts,
    get_recent_engagers,
    get_story_bank_entries,
    get_user_password_pair_by_id,
    has_commented_post,
    has_user_commented_on_post_url,
    insert_new_log,
    mark_post_commented,
    mark_post_reacted,
    record_group_comment_run,
    record_group_post,
    record_group_post_run,
    record_story_bank_use,
    record_target_comment_blocked,
    record_target_engagement,
    record_target_follow_failure,
    release_post_claim,
    resolve_weekly_cap,
    set_target_connect_status,
    set_target_follow_status,
    update_group_post_draft,
    upsert_user_group,
)
from cqc_lem.utilities.dm_templates import _draft_connect_note
from cqc_lem.utilities.engagement_window import record_pre_post_run
from cqc_lem.utilities.env_constants import INLINE_REACTIONS_ENABLED, MAX_WAIT_RETRY
from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
from cqc_lem.utilities.human_pacing import (
    ACTION_COMMENT,
    ACTION_FOLLOW,
    ACTION_INVITE,
    ACTION_REPLY,
    actions_used_today,
    engagement_caps_from_prefs,
    pace_read,
    record_action,
    remaining_actions,
)
from cqc_lem.utilities.lead_scoring import (
    _author_display_name,
    person_key,
    profile_slug,
)
from cqc_lem.utilities.linkedin import zero_walk as _zw

# The SDUI mechanics every engagement cluster shares live in `utilities/linkedin/*` (#1154). They
# are imported by their ORIGINAL names, underscore and all: the bodies moved verbatim, so one
# spelling still greps to one place. A test driving the walk below patches them HERE, because this
# is the module whose globals the walk reads; `tests/unit/app/test_engagement_core_patch_seam.py`
# fails the build on a patch left pointing at the module the code came FROM.
from cqc_lem.utilities.linkedin.cards import (
    _FEED_POST_TEXT_SEL,
    _URN_RE,
    _X_LOWER_ARIA,
    _X_LOWER_TEXT,
    _card_for_textbox,
    _feed_post_urn_from_card,
    _norm_prefix,
    _post_permalink_from_card,
    _post_social_counts,
    _x_lower,
)
from cqc_lem.utilities.linkedin.composer import (
    _COMPOSER_ABOVE_SLACK_PX,
    _SUBMIT_NEAR_COMPOSER_JS,
    _composer_submitted,
    _focus_composer,
    _visible_composers,
    _visible_rect,
)
from cqc_lem.utilities.linkedin.helper import (
    load_profile_for_user,
    login_to_linkedin,
)
from cqc_lem.utilities.linkedin.poster import (
    comment_on_linkedin_post,
    determine_media_type,
    object_urn_from_post_url,
)
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import (
    _redis_client,
    acquire_run_lock,
    automation_pause_reason,
    commenting_hold_reason,
    is_automation_paused,
    is_commenting_held,
    rate_limit_cooldown_remaining,
    release_run_lock,
)
from cqc_lem.utilities.linkedin.session import get_current_profile
from cqc_lem.utilities.linkedin.share_composer import (
    COMPOSER_EDITOR_CSS,
    POST_BUTTON_LABELS,
    SHARE_BOX_LOCATORS,
    SHARE_BOX_TEXT_SIGNALS,
    find_composer_container,
    find_composer_control,
)
from cqc_lem.utilities.linkedin.sort_evidence import (
    build_sort_control_scan_js,
    scan_sort_control_candidates,
)
from cqc_lem.utilities.linkedin_formatter import normalize_currency_symbols, strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.observability import (
    FEATURE_COMMENT,
    FEATURE_CONTENT,
    llm_attribution,
    track_feed_scan,
    track_selector_evidence,
)
from cqc_lem.utilities.post_video import post_media_abs_path
from cqc_lem.utilities.selenium_util import (
    click_first,
    find_deep_elements,
    find_first,
    get_driver_wait,
    get_driver_wait_pair,
    is_grid_relay_error,
    is_session_lost,
    is_tab_crashed,
    quit_gracefully,
    wait_for_ajax,
)

# ── zero-walk tripwires (issues #1013, #1021) ────────────────────────────────────────────────
# The grading itself lives in utilities/linkedin/zero_walk.py, because scrapper and
# company_page_inviter need it too. `run_automation` keeps its own alias of the same function for
# the catch-up walk: aliasing the upstream original in BOTH modules is what lets neither import the
# other. Re-exported under the name the moved bodies already used so one spelling still greps to one
# place.
_report_zero_walk = _zw.report_zero_walk


def navigate_to_feed(driver, wait):
    """Put the session on the home feed and ask `_switch_feed_to_recent` to sort it.

    Skips the navigation when the current URL already looks like a feed, so a walk that comes back
    here between passes does not pay for a reload. The sort is best-effort — a control that is gone
    or stale warns rather than paging anyone — but it is never SILENT: `_switch_feed_to_recent`
    records the sort state it actually achieved onto the run's funnel (#817), because an unsorted
    scan must not read as recency-sorted.
    """
    # Check to see if driver url is not already on feed
    if "feed" not in driver.current_url:
        # Navigate to LinkedIn home feed
        driver.get("https://www.linkedin.com/feed/")
        wait_for_ajax(driver)

    # SDUI removed the '//button/hr' sort control this used to click, so the legacy sort block here
    # raised (and paged) on every run. _switch_feed_to_recent owns the sort now: resilient selectors,
    # best-effort, warns instead of erroring when the control isn't there.
    _switch_feed_to_recent(driver, wait)


# Why a permalink comment stopped, as the task's own return value. Named rather than inlined so the
# Celery result reads the same for both callers (profile-viewer engagement and the outreach funnel)
# and so a test can assert the outcome without matching prose.
NO_COMMENTABLE_CARD_MESSAGE = "No commentable post card on this permalink page"
COMMENT_NOT_POSTED_MESSAGE = "Comment did not post"


@shared_task.task(name='cqc_lem.app.run_automation.comment_on_post',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'post_link']},
                  reject_on_worker_lost=True, rate_limit='4/m', queue='se_engage')
def comment_on_post(self, user_id: int, post_link: str, comment_text: str):
    """Post a comment on the post a permalink points at.

    Profile-viewer engagement and the outreach funnel's COMMENT stage both fire through here. Runs on
    the SAME SDUI engine as the feed walk — `_permalink_post_card` resolves the post's card,
    `react_to_post_inline` / `post_comment_inline` do the work — rather than the class-keyed
    `comments-comment-texteditor` / `comments-comment-box__submit-button--cr` XPaths it used to
    carry. Those anchors were removed with LinkedIn's SDUI rewrite, so the composer lookup could
    only time out and the run fell through to a bare Keys.ENTER that logged its own result as
    "might not have worked" — a live comment path failing silently for both callers (issue #966).

    The reaction now happens BEFORE the comment, for the reason the feed walk does it in that order:
    submitting re-renders the card and stales every element resolved from it.

    A comment that does not land is a FAILURE log row and a RELEASED claim, never a SUCCESS row —
    `post_comment_inline` returns True only once the comment is verifiably posted, so the task no
    longer reports a typed-but-unsubmitted comment as a comment.
    """
    # Check the database logs / claim ledger to make sure user hasn't already commented here.
    if has_user_commented_on_post_url(user_id, post_link) or has_commented_post(user_id, post_link):
        # DEBUG on both guards below: the claim ledger exists BECAUSE this task is re-dispatched
        # for a post we may already hold, so tripping either one is the ledger working.
        log_debug("User has already commented on this post. Skipping...", user_id=user_id,
                  action_type="comment", task_name="comment_on_post")
        return "User has already commented on this post. Skipping..."

    # Atomically claim before doing any work — a concurrent worker with the same post_link loses
    # here and backs off (belt-and-suspenders alongside QueueOnce's user_id+post_link key).
    if not claim_post_for_comment(user_id, post_link):
        log_debug("Another task already claimed this post. Skipping...", user_id=user_id,
                  action_type="comment", task_name="comment_on_post")
        return "Another task already claimed this post. Skipping..."

    driver, wait = get_driver_wait_pair(session_name='Post Comment', user_id=user_id)

    try:

        user_email, user_password = get_user_password_pair_by_id(user_id)

        login_to_linkedin(driver, wait, user_email, user_password)

        if post_link != driver.current_url:
            # Switch to post url
            driver.get(post_link)
        time.sleep(random.uniform(2, 4))  # let the permalink page settle before reading cards

        card = _permalink_post_card(driver, post_link, user_id=user_id)
        if card is None:
            release_post_claim(user_id, post_link)
            insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT, result=LogResultType.FAILURE,
                           post_url=post_link, message=comment_text)
            return NO_COMMENTABLE_CARD_MESSAGE

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
        post_content = _post_text_from_card(card)
        method_result = ''

        # React FIRST — the comment submit re-renders the card and stales the elements resolved from
        # it, which is why the old post-comment reaction attempt could only fail. Same env tourniquet
        # the feed walk honours (#816), so one flip stands both paths down on the next SDUI rotation.
        if not INLINE_REACTIONS_ENABLED:
            log_debug("Inline reactions disabled (INLINE_REACTIONS_ENABLED=False, issue #816)",
                      user_id=user_id, action_type="comment")
        else:
            outcome = react_to_post_inline(driver, wait, card, post_content=post_content,
                                           comment_text=comment_text, user_id=user_id)
            if outcome is None:
                # Already reacted — a no-op, not a failure (see the feed walk's identical handling).
                log_debug("Post already carried our reaction — skipping", user_id=user_id,
                          action_type="comment")
            elif outcome:
                method_result = "Added Post Reaction"
            else:
                # react_to_post_inline already warned wherever the failure actually was; warning
                # again out here files a second defect for one condition (#878).
                log_debug("No reaction landed on post — continuing to the comment", user_id=user_id,
                          action_type="comment")

        if not post_comment_inline(driver, wait, card, comment_text, user_id=user_id):
            # Nothing landed — release the claim so a later run can retry, and record the attempt as
            # a FAILURE. Only SUCCESS rows count as "we commented here", so this can't self-block.
            release_post_claim(user_id, post_link)
            insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT, result=LogResultType.FAILURE,
                           post_url=post_link, message=comment_text)
            # WARNING: post_comment_inline only logs when it RAISED — the silent-False paths (no
            # composer for this card, submit never verified) are DEBUG inside it, so this is the
            # one place the OUTCOME is known. Once is SDUI noise, repeatedly is drift, which is the
            # same call get_my_profile's scrape-returned-nothing line makes.
            log_warning("Comment did not land on this post", user_id=user_id, post_id=post_link,
                     action_type="comment")
            return " | ".join(filter(None, [method_result, COMMENT_NOT_POSTED_MESSAGE]))

        # Promote the claim to 'commented' and record the log.
        mark_post_commented(user_id, post_link)
        insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT, result=LogResultType.SUCCESS,
                       post_url=post_link, message=comment_text)
        # Spend it against the SHARED account envelope, exactly where the feed walk spends its own
        # (#626). This path posted nothing before #966, so the missing call cost nothing; now that it
        # lands comments for real, a comment invisible to the governor lets the feed walk and the
        # roster lane spend a full day's envelope on top of it.
        record_action(user_id, ACTION_COMMENT)
        log_info("Added Comment via Post Button")
        method_result = " | ".join(filter(None, ["Added Comment via Post Button", method_result]))
    except Exception as e:
        # Nothing posted (login/compose failure) — release the claim so a later run can retry.
        release_post_claim(user_id, post_link)
        log_error("Error while posting comment", exc=e, user_id=user_id, post_id=post_link, action_type="comment")
        method_result = f"Error while posting comment: {e}"
    finally:
        quit_gracefully(driver)  # Close the driver

    return method_result


# --- SDUI feed engine (LinkedIn's 2026 redesign) -----------------------------------------
# LinkedIn moved the feed to a server-driven-UI framework: the old urn:li:activity data-ids,
# feed-shared-* / comments-comment-* classes and permalink navigation are gone. Posts are now
# anchored by stable data-testid / aria-label attributes and commenting happens INLINE on the
# feed card (no per-post permalink). Verified live 2026-07-03.
#
# Reading a card — its identity, permalink and counts — moved down to `utilities/linkedin/cards.py`
# in #1154, along with the `_x_lower` XPath case fold every locator chain below goes through. What
# stays here is the WALK: which cards this run engages, in what order, under whose caps.
_X_LOWER_TESTID = _x_lower("@data-testid")

# The card `_card_for_textbox` finds is the NEAREST ancestor carrying the comment action, which is
# not always the node
# LinkedIn mounts the composer into — on the group feed the comment section renders as that node's
# SIBLING. This walks back UP, keeping the widest ancestor that still covers THIS post and no
# neighbour, so widening the composer lookup can never reach the post next to it (the #876 failure).
#
# The bound is a count of per-post MARKERS, deliberately NOT of comment actions: the composer we are
# widening to find brings its own submit button, whose text is literally "Comment"
# (`_SUBMIT_NEAR_COMPOSER_JS` clicks exactly that, and expects to skip disabled/hidden ones), so
# `isCommentAction` matches it too — the first ancestor holding both the card and the sibling comment
# section would count TWO and the walk would stop before it ever widened, in precisely the render
# this exists for. The markers are what the feed itself identifies a post by: the post-text node the
# walk enumerates cards from, and the card's own "Hide post by" control (an image-only post has no
# text node but still has that). Baselines come from the card, not a hardcoded 1, so a reshare that
# renders two text nodes inside one card still widens. Issue #916.
_POST_MARKER_SELECTORS = [_FEED_POST_TEXT_SEL, "button[aria-label^='Hide post by']"]

# ── zero-walk tripwires (issues #1013, #1021) ────────────────────────────────────────────────
# The grading itself lives in utilities/linkedin/zero_walk.py, because scrapper and
# company_page_inviter need it too and both are imported BY this module. Re-exported under the
# names this module already used so every call site (and its tests) keeps one spelling.
_FEED_CARD_MARKER_SEL = ", ".join(_POST_MARKER_SELECTORS)
_FEED_CARD_CROSSCHECK_SEL = "button[aria-label^='Hide post by']"
# The FEED WALK counts both markers (#1081), and "Hide post by" is one of them — so cross-checking
# the walk against `_FEED_CARD_CROSSCHECK_SEL` would ask ONE selector both questions and could only
# ever answer 'empty' (zero_walk.py: "cross-checking a chain against its own selector proves
# nothing"). The reaction control is the per-card anchor no marker chain reads — live-grounded at 8
# of 9 cards in the #816 run — so it is the walk's independent cross-check. The sort tripwire keeps
# `_FEED_CARD_CROSSCHECK_SEL`, which IS independent of the sort control it grades.
_FEED_WALK_CROSSCHECK_SEL = "button[aria-label^='Reaction button state'], button[aria-label^='React']"

# The page's own SHELL landmark (#1777), independent of feed CONTENT: `_FEED_WALK_CROSSCHECK_SEL`
# reads a per-post anchor, so a page that never rendered at all — a login wall, an interstitial, or
# a block on this one surface — answers zero to that too, exactly like an ordinary empty feed. A
# page LinkedIn actually rendered mounts a `<main>` whether the feed inside it is full or empty; a
# live grounding run against a group id previously grounded working (#928) found it entirely
# absent — 1.6MB of markup, zero `document.body.innerText` — while the home feed in the SAME
# session rendered normally. That is not "nothing to comment on"; it is the walk never having had
# a page to read.
_PAGE_SHELL_CROSSCHECK_SEL = "main"


_SINGLE_POST_SCOPE_JS = "const MARKERS = " + json.dumps(_POST_MARKER_SELECTORS) + r""";
const counts = (root) => MARKERS.map((sel) => root.querySelectorAll(sel).length).join(",");
let scope = arguments[0], el = scope, d = 0;
const base = counts(scope);
while (el && el.parentElement && d < 6) {
  el = el.parentElement;
  if (counts(el) !== base) break;
  scope = el;
  d++;
}
return scope;
"""


def _post_author_from_card(card) -> str:
    """Return the author name from the card's 'Hide post by <Name>' control aria-label."""
    try:
        hide_btn = card.find_element(By.CSS_SELECTOR, "button[aria-label^='Hide post by']")
        label = hide_btn.get_attribute("aria-label") or ""
        return label.replace("Hide post by ", "").strip()
    except Exception:
        return ""


def _author_is_me(author: str, my_profile: LinkedInProfile) -> bool:
    """Return True if a feed card's author is the logged-in user.

    Used to skip reacting/engaging on our OWN posts (the reply-to-own-post path handles those
    separately).
    """
    try:
        me = (getattr(my_profile, "full_name", "") or "").strip().lower()
    except Exception:
        me = ""
    return bool(me) and (author or "").strip().lower() == me


# The URN-less fallback hashes a PREFIX of the body, not all of it: LinkedIn's collapsed card
# truncates a long post (~3 lines) while the expanded render carries the whole thing, so hashing
# everything mints TWO keys for one post across a re-render — which is how #474 recurred as #580.
# Truncation happens well past 120 characters, so a 120-char prefix is byte-identical in both.
_FEED_KEY_PREFIX_CHARS = 120
# Extra shorter prefix for the per-run fingerprint set: it still matches when a render truncates
# even earlier than the canonical prefix (narrow window, aggressive "…more" collapse).
_FEED_FP_PREFIX_CHARS = (60, _FEED_KEY_PREFIX_CHARS)


def _content_digest(author: str, content: str, limit: int) -> str:
    """Short sha1 over the author + a truncation-proof normalized body prefix."""
    payload = f"{(author or '').strip().lower()}|{_norm_prefix(content, limit)}"
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()[:20]


def _feed_post_key(author: str, content: str) -> str:
    """Last-resort dedup key when no stable URN/permalink is available.

    Hashes the author plus a NORMALIZED, truncation-proof body prefix (see _norm_prefix) so the
    collapsed and the expanded render of one post produce ONE key.
    """
    return f"feedpost://{_content_digest(author, content, _FEED_KEY_PREFIX_CHARS)}"


def _feed_content_fingerprints(author: str, content: str) -> "set[str]":
    """Render-stable per-run fingerprints of a post's text at several prefix lengths.

    A second dedup guard so that even on the URN-less fallback path (or when a URN is found on one
    pass and not the next) a re-render can't re-key the post and earn it a second comment.
    """
    return {f"fp{n}:{_content_digest(author, content, n)}" for n in _FEED_FP_PREFIX_CHARS}


def _feed_post_identity(card, author: str, content: str, driver=None) -> "tuple[str, str]":
    """(dedup key, key SOURCE) for a feed post.

    Source is 'permalink' | 'card' | 'hash' — recorded on the run so we can confirm live that feed
    comments key on the stable activity URN and not on the volatile content hash (issue #580).
    """
    permalink = _post_permalink_from_card(card)
    if permalink:
        m = _URN_RE.search(permalink)
        if m:
            return f"feedurn://{m.group(0).lower()}", "permalink"
    urn = _feed_post_urn_from_card(card, driver=driver)
    if urn:
        return f"feedurn://{urn}", "card"
    return _feed_post_key(author, content), "hash"


def _stable_feed_post_key(card, author: str, content: str, driver=None) -> str:
    """Single canonical dedup key for a feed post, stable across re-renders.

    Prefers the URN (from the permalink anchor OR the card/ancestor data attributes) so
    permalink-present and permalink-absent renders of the same post map to ONE key; only falls back
    to the normalized content hash when no URN can be found.
    """
    return _feed_post_identity(card, author, content, driver=driver)[0]


# Relative-age units → minutes. The SDUI card shows a token like "3h •", "5d •", "2w •", "10mo •".
_AGE_UNIT_MIN = {"s": 0, "m": 1, "h": 60, "d": 1440, "w": 10080, "mo": 43200, "y": 525600}
_AGE_TOKEN_RE = re.compile(r"^(\d+)\s?(mo|[smhdwy])", re.I)


def _post_age_minutes(driver, card) -> "int | None":
    """Minutes since the post was published, from the card's relative timestamp span.

    Reads tokens like 'now', '3h', '5d', '2w', '10mo'. None if not found — the caller treats unknown
    age as mid-priority, not top.
    """
    try:
        token = driver.execute_script(
            "const root=arguments[0];"
            "for(const el of root.querySelectorAll('span,time')){"
            "  const t=(el.innerText||el.textContent||'').trim();"
            "  if(t.length<20 && /^(now|\\d+\\s?(mo|[smhdwy])(\\s*[•·].*)?)$/i.test(t)) return t;"
            "}return null;", card)
    except Exception:
        return None
    if not token:
        return None
    token = token.strip().lower()
    if token.startswith("now"):
        return 0
    m = _AGE_TOKEN_RE.match(token)
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNIT_MIN.get(m.group(2), 60)


# Feed-post prioritization weights — defaults below, each overridable per-deploy via the matching
# FEED_SCORE_W_* / FEED_RECENCY_HALFLIFE_MIN env var (read at call time, same live-env pattern as
# POST_SIMILARITY_MAX, so ops/tests can tune without a restart). Recency dominates: golden-hour
# posts get 4–10× the algorithmic weight and the author is online to reply — which is the whole
# point (earn a thread).
_SCORE_W_RECENCY = 0.5
_SCORE_W_RELEVANCE = 0.2
_SCORE_W_RECIPROCITY = 0.2
_SCORE_W_ACTIVITY = 0.1
_RECENCY_HALFLIFE_MIN = 180.0  # exp decay: ~1.0 under an hour, ~0.37 at 3h, small by a day


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _recency_score(age_minutes) -> float:
    if age_minutes is None:
        return 0.4  # unknown age → mid-priority, never top
    halflife = _env_float("FEED_RECENCY_HALFLIFE_MIN", _RECENCY_HALFLIFE_MIN)
    return math.exp(-max(0, age_minutes) / max(halflife, 1.0))


def _activity_score(comments: int) -> float:
    # Reward a *forming* thread (still repliable, some signal); mildly penalize 0-traction and
    # heavily penalize oversaturated posts where our comment just gets buried.
    if comments <= 0:
        return 0.4
    if comments <= 15:
        return 1.0
    if comments <= 50:
        return 0.6
    return 0.3


def _passes_hard_excludes(content: str, author: str, prefs: dict) -> bool:
    """Cheap (no-LLM) exclude gate for candidate gathering."""
    if not prefs:
        return True
    text = (content or "").lower()
    auth = (author or "").lower()
    exclude_kws = (prefs.get("exclude_keywords") or []) + (prefs.get("exclude_topics") or [])
    if any(str(k).lower() in text for k in exclude_kws if k):
        return False
    if any(str(a).lower() in auth for a in (prefs.get("exclude_authors") or []) if a):
        return False
    return True


def _literal_relevant(content: str, author: str, prefs: dict) -> bool:
    """Positive relevance signal without an LLM call.

    No include constraints means everything is on-topic by config. Otherwise a literal include
    keyword/author match qualifies. Topic-only relevance is confirmed by the LLM on the selected post,
    so this is just the scoring hint.
    """
    if not prefs:
        return True
    incl_kw = [k for k in (prefs.get("include_keywords") or []) if k]
    incl_auth = [a for a in (prefs.get("include_authors") or []) if a]
    incl_topics = [t for t in (prefs.get("include_topics") or []) if t]
    if not (incl_kw or incl_auth or incl_topics):
        return True
    text = (content or "").lower()
    auth = (author or "").lower()
    if any(str(k).lower() in text for k in incl_kw):
        return True
    return any(str(a).lower() in auth for a in incl_auth)


def _score_feed_post(meta: dict, prefs: dict, engagers: set = None) -> float:
    """Prioritize which feed post to comment on.

    Recency-dominant, then relevance, reciprocity (author engaged with us / is a target), and a
    healthy-activity bonus. Higher = comment first.
    """
    engagers = engagers or set()
    recency = _recency_score(meta.get("age_minutes"))
    relevance = 1.0 if meta.get("relevant") else 0.6
    author = (meta.get("author") or "").strip().lower()
    incl_auth = {str(a).strip().lower() for a in ((prefs or {}).get("include_authors") or []) if a}
    reciprocal = bool(author) and (author in engagers or any(a and a in author for a in incl_auth))
    reciprocity = 1.0 if reciprocal else 0.0
    activity = _activity_score(meta.get("comments", 0))
    return (_env_float("FEED_SCORE_W_RECENCY", _SCORE_W_RECENCY) * recency
            + _env_float("FEED_SCORE_W_RELEVANCE", _SCORE_W_RELEVANCE) * relevance
            + _env_float("FEED_SCORE_W_RECIPROCITY", _SCORE_W_RECIPROCITY) * reciprocity
            + _env_float("FEED_SCORE_W_ACTIVITY", _SCORE_W_ACTIVITY) * activity)


# How long a composer gets to mount after the Comment click. The old `find_first` chain spent
# WAIT_DEFAULT_TIMEOUT x (MAX_WAIT_RETRY + 1) — ~35s — on every card that never opened one, and on
# the group feed that was every card of every run. A composer that is going to mount does so in well
# under a second; re-reading the DOM a few times covers a slow render without paying for a dead one.
_COMPOSER_MOUNT_POLLS = 6
_COMPOSER_MOUNT_POLL_SECONDS = 1.0


def _is_post_comment_box(box: WebElement) -> bool:
    """Return True for the box LinkedIn labels as the POST's own comment composer.

    A reply box under an existing comment is a role=textbox too, and typing this post's comment into
    one answers a stranger instead of the author.
    """
    try:
        return "creating comment" in (box.get_attribute("aria-label") or "").lower()
    except Exception:
        return False


def _single_post_scope(driver: WebDriver, card: WebElement) -> WebElement | None:
    """Return the widest ancestor of `card` that still covers this post alone — no neighbouring post."""
    try:
        return driver.execute_script(_SINGLE_POST_SCOPE_JS, card)
    except Exception:
        return None


def _composer_in_post_scope(driver: WebDriver, card: WebElement, anchor: dict) -> WebElement | None:
    """One resolution pass: this post's comment composer, or None."""
    candidates = _visible_composers(card)
    page_wide = False
    if not candidates:
        # Not inside the card — widen to the scope that still maps to this post alone, and keep the
        # reply resolver's hard above-filter: a box starting above the card is the share box or a
        # composer left mounted on a post we already did, never this one's.
        scope = _single_post_scope(driver, card)
        candidates = [] if scope is None else [
            (box, rect) for box, rect in _visible_composers(scope)
            if rect["y"] >= anchor["y"] - _COMPOSER_ABOVE_SLACK_PX]
    if not candidates:
        # `_single_post_scope` widens by ANCESTOR only, and a live grounding run (#1777) found a
        # post whose composer mounts as a sibling of the marker-bounded scope, not a descendant of
        # it — a reshare's embedded original post carries its own marker, so the boundary that
        # "still maps to this post alone" sits BELOW where the composer actually renders, and no
        # amount of climbing reaches a sibling subtree. Fall back to the reply resolver's own
        # sibling-render answer (#883): every visible textbox on the page, never above this post
        # (a composer left mounted on an earlier post in the walk sits above it), nearest one wins.
        candidates = [(box, rect) for box, rect in _visible_composers(driver)
                      if rect["y"] >= anchor["y"] - _COMPOSER_ABOVE_SLACK_PX]
        page_wide = True
    labelled = [c for c in candidates if _is_post_comment_box(c[0])]
    if page_wide and not labelled:
        # Unlike the scope-bounded widening above (proven to contain at most one post), the
        # page-wide search has NO ownership bound at all — an unlabelled candidate here can be a
        # reply box under a stranger's comment on any post already loaded on the page, not just
        # this one. `_reply_composer_for_comment` (#883) covers that same gap with an ownership
        # check (`_comment_container`); a post has no equivalent container to check against, so the
        # only safe answer page-wide is: require the "creating comment" label, or skip.
        return None
    bottom = anchor["y"] + anchor["height"]
    best = min(labelled or candidates, key=lambda br: abs(br[1]["y"] - bottom), default=None)
    return None if best is None else best[0]


def _post_composer_for_card(driver: WebDriver, card: WebElement,
                            user_id: int = None) -> WebElement | None:
    """Return the comment composer belonging to THIS post card — never a page-wide first match.

    A composer nested in the card is unambiguous and still wins (#876). What is new (issue #916) is
    that a card WITHOUT one is not automatically a dead end: `_card_for_textbox` only walks up to the
    nearest ancestor carrying the comment action, and where LinkedIn renders the comment section
    beside that node rather than inside it, the card-scoped lookup missed on every post of every run
    — 408 in 18h, every one on a group feed. Widening is bounded by `_single_post_scope`, so the
    search area provably contains one post, which is the invariant #876 actually needs.

    A miss is an expected no-op: the box never opened, so the caller skips the post and releases its
    claim. It is logged DEBUG here, once, the way `_reply_composer_for_comment` logs its own — the
    per-card `log_warning` it replaces escalated into a filed defect for a skip we already handle.
    """
    for _ in range(_COMPOSER_MOUNT_POLLS):
        anchor = _visible_rect(card)
        if anchor is None:
            log_debug("Post card is not rendered; no comment composer to resolve",
                      action_type="comment", user_id=user_id)
            return None
        composer = _composer_in_post_scope(driver, card, anchor)
        if composer is not None:
            return composer
        time.sleep(_COMPOSER_MOUNT_POLL_SECONDS)
    log_debug("No comment composer opened on this post card", action_type="comment", user_id=user_id)
    return None


def post_comment_inline(driver, wait, card, comment_text: str, user_id: int = None,
                        composer: WebElement | None = None) -> bool:
    """Open the card's inline comment composer, type the comment, and submit via the button.

    The SDUI composer has no <form>. Returns True only if the comment actually lands (composer
    clears / appears in the list), not just because text was typed.

    A failure names the STEP it died on: one `try` over the whole sequence reported every failure
    mode as the same 'Inline comment post failed' warning, which both hid the real cause and
    collapsed unrelated faults into one escalated issue. Step names carry no quotes or digits on
    purpose — the escalation dedup key masks both, so quoting them would re-merge the very keys
    this split exists to separate.

    `composer` is the ALREADY-resolved textbox for this post. When provided (e.g. the group-feed
    probe in `_engage_card` opened it before spending an LLM call), the open/find steps are skipped
    so the composer is not clicked shut while the comment is being generated.
    """
    step = "prepare text"
    try:
        comment_text = strip_non_bmp(comment_text)  # ChromeDriver send_keys throws on non-BMP emoji
        if not comment_text.strip():
            return False
        if composer is None:
            step = "open composer"
            if click_first(driver, wait, _COMMENT_ACTION_LOCATORS,
                           "Open comment composer", parent_element=card, required=False, user_id=user_id) is None:
                return False
            time.sleep(random.uniform(1.5, 3))
            step = "find composer"
            # Scoped to THIS post. The feed walk comments on several posts without reloading the page and
            # LinkedIn leaves each composer mounted after it submits, so a document-wide lookup returns
            # the FIRST visible role=textbox in DOM order — an earlier post's composer, scrolled off the
            # top, which is how the click landed under the sticky nav at y=9 (issue #876). Centering that
            # composer (#815) would not have fixed it, it would have typed the comment into the wrong
            # post. `_post_composer_for_card` owns both the resolution and the miss log; still no
            # page-wide fallback — no composer for this post means skip the post.
            composer = _post_composer_for_card(driver, card, user_id=user_id)
            if composer is None:
                return False
        step = "focus composer"
        _focus_composer(driver, composer)
        step = "type comment"
        composer.send_keys(comment_text)
        time.sleep(random.uniform(1, 2))
        step = "submit composer"
        if not driver.execute_script(_SUBMIT_NEAR_COMPOSER_JS, composer):
            composer.send_keys(Keys.CONTROL, Keys.RETURN)  # fallback
        time.sleep(random.uniform(3, 5))
        step = "verify submit"
        return _composer_submitted(driver, composer, comment_text)
    except Exception as e:
        log_warning(f"Inline comment post failed at {step}", exc=e, action_type="comment", user_id=user_id)
        return False


def _post_text_from_card(card) -> str:
    """Return the post's own body text read off its card — what the reaction chooser is given.

    `card.text` would fold the whole comment thread in, so a permalink page (which renders every
    comment) would hand the classifier someone else's words instead of the post's.
    """
    try:
        parts = [(el.text or "").strip() for el in card.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL)]
    except Exception:
        return ""
    return "\n".join(p for p in parts if p).strip()


def _permalink_post_card(driver, post_link: str, user_id: int = None) -> "WebElement | None":
    """Return the card for the post a `/feed/update/…` permalink points at, or None.

    A permalink page is NOT a one-post page — LinkedIn stacks "More posts for you" recommendations
    under the post it was asked for, and each of those is a full card with its own comment action.
    So the card is chosen by the URN in the permalink, and the topmost card is used only when no
    card claims a URN at all. A top card that provably belongs to a DIFFERENT post returns None:
    commenting there would land our comment on a recommendation, which is worse than not commenting.

    Cards are enumerated exactly the way the feed walk enumerates them (`_FEED_POST_TEXT_SEL` →
    `_card_for_textbox`) so the composer resolution downstream is the SAME card-scoped one
    (`_post_composer_for_card`) — there is no permalink-only composer lookup to drift separately.
    """
    m = _URN_RE.search(post_link or "")
    wanted = m.group(0).lower() if m else None
    cards = []
    for box in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL):
        try:
            card = _card_for_textbox(driver, box)
        except (StaleElementReferenceException, WebDriverException):
            continue
        if card is not None:
            cards.append(card)
    if not cards:
        # No card carries a comment action: comments are off/restricted on this post, the page never
        # rendered, or the walk drifted. Nothing to do either way, so it is a DEBUG no-op — the
        # caller turns it into a FAILURE log row, which is where a real drift becomes visible.
        log_debug("No commentable post card on permalink page", action_type="comment",
                  user_id=user_id, url=post_link)
        return None
    if wanted:
        for card in cards:
            if _feed_post_urn_from_card(card, driver=driver) == wanted:
                return card
        top_urn = _feed_post_urn_from_card(cards[0], driver=driver)
        if top_urn and top_urn != wanted:
            log_debug("Top card on permalink page belongs to a different post; not commenting",
                      action_type="comment", user_id=user_id, url=post_link)
            return None
    return cards[0]


# ── Comment + reaction locator chains (issue #816 live grounding, 2026-08-02) ──────────────────
#
# Every chain here is ORDERED most-stable-first and every entry is a DIFFERENT route to the same
# control, because LinkedIn rotates which attribute is canonical and commonly keeps several alive
# at once. A single locator does not fail loudly — it returns None, and a None card or a None
# trigger is indistinguishable from "there was nothing to act on".
#
# Counts from the live run (9 posts on the home feed) are recorded per entry so the next drift is
# diagnosable against a baseline instead of a docstring.
#
# XPath entries use `.//` so they stay scoped when passed `parent_element=card`; a leading `//`
# would silently search the whole document and return a neighbouring post's control.
_COMMENT_ACTION_LOCATORS = [
    (By.CSS_SELECTOR, "button[aria-label='Comment']"),            # live count: 0 (was canonical)
    (By.CSS_SELECTOR, "button[aria-label^='Comment on']"),
    (By.CSS_SELECTOR, "[data-testid*='comment-button']"),
    (By.XPATH, f".//button[{_X_LOWER_TEXT}='comment']"),          # live count: 1 per card TODAY
    (By.XPATH, f".//*[@role='button'][{_X_LOWER_TEXT}='comment']"),
]

# The way IN to the reaction fly-out. The state button doubles as the default-Like toggle (its
# text is literally "Like"), so one chain serves both roles.
# Live: `button[aria-label^='Reaction button state']` matched 8 of 9 cards — still the best anchor.
_REACTION_TRIGGER_LOCATORS = [
    (By.CSS_SELECTOR, "button[aria-label^='Reaction button state']"),   # live count: 8
    (By.XPATH, f".//button[contains({_X_LOWER_ARIA},'reaction button state')]"),
    (By.XPATH, f".//button[contains({_X_LOWER_ARIA},'reaction')]"),
    (By.CSS_SELECTOR, "button[aria-label='React Like']"),
    (By.CSS_SELECTOR, "button[aria-label^='React']"),
    (By.CSS_SELECTOR, "[data-testid*='reaction']"),
    (By.XPATH, f".//button[{_X_LOWER_TEXT}='like']"),                   # exact: never "2 likes"
]

# The explicit fly-out opener. Live count: ZERO — hovering the trigger opens the fly-out directly
# now. Kept as a trailing fallback (other surfaces and older renders still ship it), but its
# absence is the documented normal path, so a miss here must never warn.
_REACTION_OPENER_LOCATORS = [
    (By.CSS_SELECTOR, "button[aria-label='Open reactions menu']"),      # live count: 0
    (By.XPATH, f".//button[contains({_X_LOWER_ARIA},'reactions menu')]"),
    (By.XPATH, ".//button[@aria-haspopup]"),
]


# What the probe below accepts as evidence of a reaction control. It is the trigger chain plus the
# opener's LABEL-based routes only: the opener's trailing `.//button[@aria-haspopup]` matches any
# popup control the card happens to ship — the "…" control menu, a comment sort dropdown — so it
# names a different entity than the reaction (the #1012 rail hazard) and would make the probe
# answer True on nearly every card, silently re-opening issue #874.
_REACTION_AFFORDANCE_LOCATORS = _REACTION_TRIGGER_LOCATORS + [
    (by, sel) for by, sel in _REACTION_OPENER_LOCATORS if "aria-haspopup" not in sel
]


def _card_has_reaction_affordance(card, user_id: int = None) -> bool:
    """True when the card renders any reaction control at all.

    LinkedIn surfaces post text without reaction affordances on some cards (e.g., followed
    hashtags, promoted modules, third-party embeds). The #899 live run found 9 post-text nodes but
    only 8 reaction triggers, so at least one normal card type is commentable but not reactable.
    A selector miss on those cards is working behaviour and must stay DEBUG; only cards that DO
    carry reactions should warn when the state button can't be read.
    """
    try:
        for by, sel in _REACTION_AFFORDANCE_LOCATORS:
            for el in card.find_elements(by, sel):
                try:
                    if el.is_displayed():
                        return True
                except StaleElementReferenceException:
                    # Element detached while probing; keep scanning the card.
                    continue
        for el in card.find_elements(By.CSS_SELECTOR, "button, [role='button']"):
            try:
                if not el.is_displayed():
                    continue
                label = (el.get_attribute("aria-label") or "").lower()
                text = (el.text or "").lower()
                testid = (el.get_attribute("data-testid") or "").lower()
                if any(token in label or token in text or token in testid for token in (
                    "reaction", "react", "like", "celebrate", "support", "love", "insightful", "funny"
                )):
                    return True
            except StaleElementReferenceException:
                # Element detached while probing; keep scanning the card.
                continue
    except WebDriverException as e:
        # The card itself became unreachable (e.g., removed from DOM); treat as no affordance.
        # Logging at DEBUG because this is a best-effort probe, not the main action.
        log_debug("Could not probe card for reaction affordance", user_id=user_id,
                  action_type="comment", exc=e)
    return False


def _reaction_option_locators(reaction: str) -> list:
    """Ordered routes to ONE reaction inside the open fly-out.

    Document-scoped on purpose: the fly-out renders outside the card subtree (confirmed live — the
    options were only reachable from `driver`, never from the card), so scoping these to the card
    finds nothing even when the menu is wide open.
    """
    low = (reaction or "like").strip().lower()
    return [
        (By.CSS_SELECTOR, f"button[aria-label='{reaction}']"),           # live: exact labels present
        (By.XPATH, f"//button[{_X_LOWER_ARIA}='{low}']"),
        (By.XPATH, f"//*[@role='button'][{_X_LOWER_ARIA}='{low}']"),
        (By.XPATH, f"//*[self::button or @role='menuitem' or @role='menuitemradio' or "
                   f"@role='option'][{_X_LOWER_TEXT}='{low}']"),
    ]


def react_to_post_inline(driver, wait, card, post_content: str = None, comment_text: str = None,
                         user_id: int = None) -> Optional[bool]:
    """Leave a single reaction on the card's post via the SDUI reaction fly-out.

    Three-valued on purpose: True = a reaction registered, False = we tried and failed, None = the
    post already carried our reaction (a no-op, not a failure). None is falsy, so callers that only
    care whether a reaction landed keep working unchanged.

    Every locator is an ORDERED CHAIN (`_REACTION_TRIGGER_LOCATORS`, `_REACTION_OPENER_LOCATORS`,
    `_reaction_option_locators`) rather than a single anchor: LinkedIn rotates which of
    aria-label / data-testid / visible text is canonical and keeps several alive at once, so one
    locator per control is a silent single point of failure. Grounded against a live session
    2026-08-02 — the per-entry live counts are recorded beside each chain.

    The 2026-08 shape: the 'Reaction button state: …' button IS the trigger and doubles as the
    default-Like toggle (its text is literally "Like"), hovering it opens the fly-out directly, and
    'Open reactions menu' no longer exists. The fly-out renders OUTSIDE the card subtree, so the
    option lookup is document-scoped. The reaction itself is picked by a fast AI call scoped to the
    post + our comment, which self-falls-back to random. Returns True only if a reaction registered
    (the toggle no longer reads 'no reaction').
    """
    try:
        has_reaction_affordance = _card_has_reaction_affordance(card, user_id=user_id)
        if not has_reaction_affordance:
            # The #899 live run found 9 post-text nodes and 8 reaction triggers — some commentable
            # cards carry no reaction affordance at all. Skipping them silently is working behaviour;
            # warning on every such card would escalate to ERROR and file a defect (issue #874).
            log_debug("Card has no reaction affordance — skipping inline reaction", user_id=user_id,
                      action_type="comment")
            return False
        state = find_first(driver, wait, _REACTION_TRIGGER_LOCATORS,
                           "Reaction state", parent_element=card, required=False, visible_only=True,
                           warn_on_miss=has_reaction_affordance, user_id=user_id)
        if state is not None and "no reaction" not in (state.get_attribute("aria-label") or "").lower():
            return None  # already reacted on this post — a no-op, not a failure

        reaction = choose_post_reaction(post_content, comment_text)
        # One chain serves trigger AND toggle now, so there is no second lookup to fall back to.
        trigger = state
        if trigger is not None:
            try:
                ActionChains(driver).move_to_element(trigger).perform()
                time.sleep(random.uniform(0.6, 1.2))
            except Exception:
                # Swallowed on purpose: the hover is one of THREE ways this fly-out opens, and the
                # other two run below regardless — the trailing opener click, then the direct
                # toggle click that leaves a default Like. A hover that cannot be performed (the
                # trigger scrolled out of the viewport, or went stale between the lookup and the
                # move) therefore costs nothing, and the verdict is decided further down by whether
                # the toggle's label actually flipped away from 'no reaction'. Logging here would
                # fire on cards this path still reacts to — the expected-no-op the recurrence rule
                # escalates into a filed defect.
                pass
        # The explicit opener is GONE as of 2026-08 (live count: 0) — hovering the trigger is what
        # opens the fly-out. Kept as a trailing fallback for surfaces that still ship it, but its
        # absence is now the NORMAL path, so it must never warn: warning on the documented happy
        # path is exactly the expected-no-op the recurrence rule turns into a filed defect.
        # max_try=1: this control is KNOWN absent on the current SDUI (live count 0), and the
        # supervised end-to-end run showed it still burning a full retry round — "Open reactions
        # menu | not found | .....retrying" — on every single card, for a lookup we expect to miss.
        # That retry cost on a doomed lookup is precisely what made the broken flow expensive.
        # Return value deliberately unused: the option lookup below is no longer gated on the
        # opener having been clicked, because the hover alone opens the fly-out. Binding it to a
        # name would be dead code (CodeQL py/unused-local-variable, correctly).
        click_first(driver, wait, _REACTION_OPENER_LOCATORS,
                    "Open reactions menu", parent_element=card, required=False,
                             warn_on_miss=False, max_try=1, user_id=user_id)
        time.sleep(random.uniform(0.8, 1.6))
        # Document-scoped: the fly-out renders outside the card subtree (confirmed live — the
        # options were reachable only from `driver`). Try the chosen reaction, then plain Like.
        # No longer gated on `opened`: the hover alone can open the menu, so requiring the opener
        # would skip a fly-out that is sitting right there.
        picked = click_first(driver, wait, _reaction_option_locators(reaction),
                             f"React {reaction}", required=False, warn_on_miss=False,
                             user_id=user_id)
        if picked is None and reaction.strip().lower() != "like":
            picked = click_first(driver, wait, _reaction_option_locators("Like"),
                                 "React Like", required=False, warn_on_miss=False, user_id=user_id)
        if picked is None:
            # Fly-out didn't open or the specific reaction wasn't found — fall back to clicking the
            # primary toggle directly, which leaves a default Like. Better a Like than no reaction.
            if trigger is None:
                return False
            driver.execute_script("arguments[0].click();", trigger)
        time.sleep(random.uniform(0.8, 1.5))
        wait_for_ajax(driver)
        # Best-effort confirm: label flips away from 'no reaction'. If the toggle can't be re-read,
        # trust the click rather than false-negative — so the miss only means something when the
        # card HAD a readable Reaction-state button before the click. Cards that never exposed one
        # (the fly-out/React-toggle path) take the documented trust-the-click fallback, and warning
        # about that on every card escalated to ERROR and filed a defect for working behaviour
        # (issue #875). A control this card never carried is also not worth waiting out twice.
        expected = state is not None
        after = find_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label^='Reaction button state']")],
                           "Reaction state (post-click)", parent_element=card, required=False,
                           visible_only=True, warn_on_miss=expected,
                           max_try=MAX_WAIT_RETRY if expected else 1, user_id=user_id)
        if after is not None and "no reaction" in (after.get_attribute("aria-label") or "").lower():
            # The card's controls were readable and the click STILL didn't take — the one reaction
            # failure none of the selector misses above stand for, so it gets its single warning
            # here, where it is detected. The caller's blanket warning is DEBUG (issue #878).
            log_warning("Reaction did not register after clicking", user_id=user_id,
                        action_type="comment")
            return False
        log_info(f"Reacted '{reaction}' on post")
        return True
    except Exception as e:
        log_warning("Inline post reaction failed", exc=e, action_type="comment", user_id=user_id)
        return False


def post_matches_preferences(content: str, author: str, prefs: dict) -> bool:
    """Decide whether a post should be engaged given the user's targeting preferences.

    Exclude keywords/topics/authors always kill it (case-insensitive substring). If any
    include constraint is set, the post must match at least one: a literal include keyword
    or author, OR (only if literal misses) LLM topic relevance to include_topics. With no
    include constraints, engage everything not excluded.
    """
    if not prefs:
        return True
    text = (content or "").lower()
    auth = (author or "").lower()
    exclude_keys = (prefs.get("exclude_keywords") or []) + (prefs.get("exclude_topics") or [])
    if any(str(k).lower() in text for k in exclude_keys if k):
        return False
    if any(str(a).lower() in auth for a in (prefs.get("exclude_authors") or []) if a):
        return False
    incl_kw = [k for k in (prefs.get("include_keywords") or []) if k]
    incl_auth = [a for a in (prefs.get("include_authors") or []) if a]
    incl_topics = [t for t in (prefs.get("include_topics") or []) if t]
    if not (incl_kw or incl_auth or incl_topics):
        return True
    if any(str(k).lower() in text for k in incl_kw):
        return True
    if any(str(a).lower() in auth for a in incl_auth):
        return True
    if incl_topics and post_is_relevant(content, incl_topics):
        return True
    return False


def _topic_gate_topics(prefs: dict) -> list:
    """Return the topics a candidate post is judged on-topic against.

    Uses the user's focus_topics (what they want authority in), falling back to include_topics when
    no focus is set.
    """
    focus = [t for t in ((prefs or {}).get("focus_topics") or []) if t]
    if focus:
        return focus
    return [t for t in ((prefs or {}).get("include_topics") or []) if t]


def passes_topic_gate(content: str, prefs: dict) -> bool:
    """Hard on-topic gate (issue #616).

    Under 2026 Topic Authority ranking an off-topic comment actively damages distribution, so a post
    that isn't about the user's focus topics is NEVER commented on — not by the strict path and not by
    the empty-filter fallback, which is exactly how the 2026-07-25 funnel ended up commenting on
    AI-in-HR posts for a non-HR account.

    With no topics configured there is nothing to be off-topic against, so the gate is inert. A
    literal topic mention short-circuits the classifier; the classifier itself fails OPEN (a
    lem-simple hiccup must not silence all engagement).
    """
    topics = _topic_gate_topics(prefs)
    if not topics:
        return True
    text = (content or "").lower()
    if any(_mentions_topic(text, t) for t in topics):
        return True
    return post_is_relevant(content, topics)


def _mentions_topic(text: str, topic: str) -> bool:
    """Whole-term match for the gate's literal short-circuit.

    Substring matching would let a short topic ("HR") fire on an unrelated word ("thrive") and skip
    the classifier entirely, so the term has to be bounded by non-word characters on both sides.
    """
    term = str(topic or "").strip().lower()
    if not term:
        return False
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


# Roster blend (issue #616): the mix every fast-growing native creator studied engaged on —
# ~50% peers, 30% ICP, 20% large creators. Buckets that run dry spill their slots to the others.
_ROSTER_MIX = (("peer", 0.5), ("icp", 0.3), ("creator", 0.2))
# Anti-pod: at most ONE comment per roster author per run, on top of their weekly cap. Repeatedly
# engaging the same account in a session is the pod signature LinkedIn demotes.
_ROSTER_MAX_POSTS_PER_AUTHOR = 1


def _target_staleness(target: dict) -> tuple:
    """Rotation sort key: never-engaged targets first, then least-recently-engaged."""
    last = target.get("last_engaged_at")
    if last is None:
        return (0, 0.0)
    try:
        return (1, last.timestamp())
    except AttributeError:
        return (1, 0.0)


def select_roster_targets(targets: list, limit: int) -> list:
    """Which roster authors to engage this run.

    Active targets still under their per-author weekly cap, drawn in the 50/30/20 peer/ICP/creator
    blend, least-recently-engaged first.
    """
    if limit <= 0:
        return []
    eligible = []
    for t in targets or []:
        if not t.get("active", True):
            continue
        cap = resolve_weekly_cap(t.get("max_comments_per_week"))
        if int(t.get("comments_this_week") or 0) >= cap:
            continue
        eligible.append(t)
    buckets = {category: [] for category, _ in _ROSTER_MIX}
    for t in sorted(eligible, key=_target_staleness):
        buckets[t.get("category") if t.get("category") in buckets else "peer"].append(t)
    picked = []
    for category, share in _ROSTER_MIX:
        quota = int(limit * share)
        picked.extend(buckets[category][:quota])
        buckets[category] = buckets[category][quota:]
    # Rounding + short buckets leave slots over; fill them with the stalest targets left, whatever
    # their category, so a lopsided roster still uses the whole run budget.
    leftovers = sorted([t for b in buckets.values() for t in b], key=_target_staleness)
    picked.extend(leftovers[:max(0, limit - len(picked))])
    return picked[:limit]


def _roster_activity_url(profile_url: str) -> str:
    """Return a roster author's recent-activity page — the roster's equivalent of the home feed."""
    base = str(profile_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "/recent-activity/" in base:
        return base + "/"
    return f"{base}/recent-activity/all/"


# --- opt-in roster auto-follow (issue #962) -----------------------------------------------------
# A roster target who restricts commenting to connections/followers renders no comment affordance at
# all, so `comment_on_roster_posts` skips them fail-closed and the user never learns that following
# would unlock the account. Following is opt-in, paced, and piggybacks on the activity page the
# roster pass already opened — there is no dedicated follow session and no extra navigation.
#
# The control is resolved by LABEL and by HREF only (never a class name — docs/sdui-selenium-notes),
# and every accepted label must NAME the page owner, because "Follow" affordances also render inside
# feed cards, reshare headers and recommendation modules. Clicking one of those follows the wrong
# account entirely, which is a mistake no amount of retrying undoes.
#
# Why names and not geometry: the live probe run for PR #963 showed both prior scoping ideas fail on
# the real page. The target's own /in/<slug> anchor also renders inside OTHER people's cards
# ("<target> reposted this" attribution), so an unbounded ancestor walk resolved "Follow Greg Hart"
# on Andrew Ng's activity page; and the first feed card's header (which carries its author's Follow)
# renders geometrically ABOVE the top-card Follow, so a "top card is above the first post" bound
# excludes the genuine control. What IS stable: LinkedIn writes the page <title> ("Activity |
# <Name> | LinkedIn") and every follow control's aria-label ("Follow <Name>") from the same display
# name — so a control naming the owner follows the intended account wherever it sits, and a label
# that names anyone else (or nobody) is never clickable. Route A still prefers the control nearest
# the target's own /in/<slug> anchor (exact path-segment match — a substring also hits
# /in/<slug>-2b41, a different person); Route B scans the whole page for an owner-named control. A
# miss on both returns 'unknown' and NOTHING is clicked — fail closed, exactly like the comment
# affordance above.
_FOLLOW_CONTROL_JS = r"""
const SLUG = (arguments[0] || '').toLowerCase(), NAME = (arguments[1] || '')
  .replace(/\s+/g, ' ').trim().toLowerCase();
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
// aria-label wins over text: LinkedIn labels the control "Follow <Name>" while the visible text is
// just "Follow", and the name is what proves the control belongs to the right person. A bare,
// nameless label is NEVER accepted — it could belong to anyone on the page.
const label = (b) => norm(b.getAttribute('aria-label')) || norm(b.textContent);
const named = (text, verb) => {
  if (!text.startsWith(verb + ' ')) return false;
  const rest = text.slice(verb.length + 1).trim();
  return rest === NAME || rest.startsWith(NAME + ' ');
};
const readState = (text) => {
  if (named(text, 'following') || named(text, 'unfollow')) return 'following';
  if (named(text, 'follow')) return 'not_following';
  return null;
};
const shown = (el) => { const r = el.getBoundingClientRect(); return !!(r.width && r.height); };
const controls = (root) => {
  let following = null, follow = null;
  for (const b of root.querySelectorAll("button, [role='button']")) {
    if (!shown(b)) continue;
    const state = readState(label(b));
    if (!following && state === 'following') following = b;
    else if (!follow && state === 'not_following') follow = b;
  }
  // 'Following' wins: a control showing it means the account is already followed, whatever else is
  // on the page.
  return following ? ['following', following] : (follow ? ['not_following', follow] : null);
};
// The /in/<slug> path segment, exactly — a substring match also hits a different person's slug that
// merely starts the same.
const slugOf = (href) => {
  const m = (href || '').toLowerCase().match(/\/in\/([^\/?#]+)/);
  return m ? m[1] : '';
};

if (!NAME) return ['unknown', null];  // no owner name = nothing to anchor on = no safe scan

// Route A — the owner-named control nearest the target's own profile anchor.
if (SLUG) {
  for (const a of document.querySelectorAll("a[href*='/in/']")) {
    if (slugOf(a.getAttribute('href')) !== SLUG) continue;
    let el = a, d = 0;
    while (el && d < 8) {
      const hit = controls(el);
      if (hit) return hit;
      el = el.parentElement; d++;
    }
  }
}

// Route B — any owner-named control on the page.
return controls(document) || ['unknown', null];
"""


# What ONE follow attempt did. Produced in one place and compared in two, so it is a closed
# vocabulary rather than four magic strings — a typo in the budget comparison would un-bound the
# clicking.
class FollowOutcome(StrEnum):
    """What ONE auto-follow attempt did — the closed vocabulary the follow budget is spent against.

    The distinctions are the whole point: `ALREADY_FOLLOWING` is read off the card with no click at
    all, while `FAILED` means a click was attempted and the control never verifiably flipped — the
    one state that never self-corrects on its own, so it is recorded rather than retried blindly.
    A `str` enum, so a value can be logged or compared as text without conversion.
    """

    NONE = ""                    # nothing was attempted (held, no control, no URL)
    FOLLOWED = "followed"        # clicked AND the control confirmed the flip
    ALREADY_FOLLOWING = "already_following"   # the card already said so — recorded without a click
    FAILED = "failed"            # a click was dispatched and did not verifiably take


# How long to give the top card to re-render after a Follow click before calling it a failure. The
# card is replaced, not merely relabelled, so a single immediate re-read races the render — and an
# unverified flip costs the target a failed attempt it may not have earned.
_FOLLOW_FLIP_ATTEMPTS = 4
_FOLLOW_FLIP_WAIT_SECONDS = 2.0


def _activity_page_owner_name(driver: WebDriver) -> str:
    """Return the page owner's display name from the activity page's own <title>.

    Example: "(8) Activity | Arvid Kahl | LinkedIn". LinkedIn writes the title and the follow
    controls' aria-labels from the same display name, so this is the one spelling guaranteed to match
    — a roster row's stored name is only a fallback, because users type those freehand.
    """
    try:
        parts = [p.strip() for p in str(driver.title or "").split("|")]
    except Exception:
        return ""
    if len(parts) >= 2 and parts[-1].lower() == "linkedin" and parts[-2]:
        return parts[-2]
    return ""


def _resolve_follow_control(driver: WebDriver, profile_url: str,
                            name: str = "") -> tuple[FollowStatus, WebElement | None]:
    """`(state, element)` for a roster target's follow control on the activity page already open.

    The control must carry the page owner's name in its label — see `_FOLLOW_CONTROL_JS` for why
    that, and not top-card geometry, is the scoping rule.

    `FollowStatus.UNKNOWN` means we could not read it, NOT that there is nothing to follow — the
    caller must treat it as "do nothing", never as "click the first Follow you can find". The
    element is None on every state but `NOT_FOLLOWING`, so a caller that clicks must check it.
    """
    owner = _activity_page_owner_name(driver) or str(name or "").strip()
    if not owner:
        # Selector-rot breadcrumb (not a warning — plenty of pages legitimately resolve nothing):
        # a run that keeps landing here means the <title> shape rotated AND no roster name was given.
        log_debug("Follow control unresolvable — no owner name to anchor the label match on",
                  action_type="follow", task_name="_resolve_follow_control")
        return FollowStatus.UNKNOWN, None
    try:
        result = driver.execute_script(_FOLLOW_CONTROL_JS, profile_slug(profile_url), owner)
    except Exception as e:
        log_debug(f"Follow control resolution JS failed ({type(e).__name__}: {e})",
                  action_type="follow", task_name="_resolve_follow_control")
        return FollowStatus.UNKNOWN, None
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        return FollowStatus.UNKNOWN, None
    state, element = result[0], result[1]
    if state not in (FollowStatus.FOLLOWING, FollowStatus.NOT_FOLLOWING):
        return FollowStatus.UNKNOWN, None
    return FollowStatus(state), element


def roster_follow_budget(user_id: int, prefs: dict) -> int:
    """How many roster targets this user may follow right now (issue #962).

    Returns 0 when the lane is off. Re-read before EVERY follow rather than decremented from a
    per-run local: a click is recorded the moment it is dispatched, so re-reading is what makes two
    overlapping runs for the same user share one daily allowance instead of each spending the whole
    of it.

    The cap draws its own paced daily budget (`ACTION_FOLLOW`) so a follow never eats the comment
    lane's, and `caps` still engages the shared account envelope — an account that has spent its
    outbound allowance stops following too.
    """
    if not (prefs or {}).get("roster_auto_follow"):
        return 0
    try:
        cap = max(0, int((prefs.get("max_follows_per_day")
                          if prefs.get("max_follows_per_day") is not None
                          else ROSTER_FOLLOWS_PER_DAY_DEFAULT)))
    except (TypeError, ValueError):
        cap = ROSTER_FOLLOWS_PER_DAY_DEFAULT
    if cap <= 0:
        return 0
    return max(0, remaining_actions(user_id, ACTION_FOLLOW, cap,
                                    actions_used_today(user_id, ACTION_FOLLOW),
                                    caps=engagement_caps_from_prefs(prefs)))


def _outbound_hold_reason(user_id: int) -> str:
    """Why an outbound roster action (a follow, a connect invite) must not go out right now.

    Returns '' when every hard gate is clear. Pacing only ever slows a lane down; these are the
    harder gates, re-read per action because the breaker can trip mid-run. The suppression tripwire
    (#629) rides `is_automation_paused` too, so one check covers the manual pause, the deploy pause
    and a suppression hold.
    """
    if is_automation_paused():
        return automation_pause_reason() or "automation paused"
    if rate_limit_cooldown_remaining() > 0:
        return "LinkedIn 429 breaker open"
    return ""


def _await_follow_flip(driver: WebDriver, profile_url: str, name: str,
                       sleep: Optional[Callable[[float], None]] = None) -> FollowStatus:
    """Poll the control until it reads "Following", up to `_FOLLOW_FLIP_ATTEMPTS` times.

    LinkedIn REPLACES the top card after a follow rather than relabelling the button, so a single
    immediate re-read races that render — and losing that race costs the target a failed attempt it
    did not earn, twice of which retires it.
    """
    sleep = sleep or time.sleep
    state = FollowStatus.UNKNOWN
    for attempt in range(_FOLLOW_FLIP_ATTEMPTS):
        if attempt:
            sleep(_FOLLOW_FLIP_WAIT_SECONDS)
        state, _ = _resolve_follow_control(driver, profile_url, name=name)
        if state == FollowStatus.FOLLOWING:
            return state
    return state


def reconcile_roster_follow_state(driver: WebDriver, user_id: int, target: dict) -> FollowStatus:
    """Read-only follow-state correction for a target the lane already gave up on.

    Uses the activity page that is open anyway. Clicks NOTHING and spends no budget.

    This is what keeps `follow_failed` from being a life sentence: an unverified flip is recorded as
    a failure precisely because it may have landed, so the next visit has to be allowed to notice
    that it did. Only an affirmative "Following" is written — `unknown` leaves the record alone.
    """
    profile_url = str(target.get("profile_url") or "").strip()
    if not profile_url:
        return FollowStatus.UNKNOWN
    state, _ = _resolve_follow_control(driver, profile_url, name=str(target.get("name") or ""))
    if state == FollowStatus.FOLLOWING:
        set_target_follow_status(user_id, profile_url, FollowStatus.FOLLOWING)
        log_info(f"Roster target {target.get('name') or profile_url} reads as followed after all — "
                 f"clearing the failed follow", user_id=user_id, action_type="follow",
                 task_name="reconcile_roster_follow_state")
    return state


def auto_follow_roster_target(driver: WebDriver, user_id: int, target: dict,
                              sleep: Optional[Callable[[float], None]] = None) -> FollowOutcome:
    """Follow ONE roster target from the activity page already open.

    Verification is the point: `follow_status='following'` is only written once the control itself
    reads "Following" AFTER the click, because a follow that silently did not register would be
    recorded as terminal and the target never looked at again. A card that already says "Following"
    is recorded WITHOUT a click — the zero-cost catch-up that stops the lane redoing this work every
    run.

    The paced daily budget is spent on the CLICK, not on the outcome: LinkedIn saw the action
    whether or not we could read the result, and a lane whose verification broke must not be free to
    click every target on the roster.
    """
    sleep = sleep or time.sleep
    profile_url = str(target.get("profile_url") or "").strip()
    if not profile_url:
        return FollowOutcome.NONE
    name = str(target.get("name") or "")
    hold = _outbound_hold_reason(user_id)
    if hold:
        # DEBUG, not INFO: this is re-read per target because the breaker can trip mid-run, so an
        # INFO here is one line per roster target for a condition that has nothing to do with the
        # follow lane. The caller announces the hold ONCE per run.
        log_debug(f"Roster auto-follow skipped — {hold}", user_id=user_id, action_type="follow",
                  task_name="auto_follow_roster_target")
        return FollowOutcome.NONE
    state, control = _resolve_follow_control(driver, profile_url, name=name)
    if state == FollowStatus.FOLLOWING:
        if str(target.get("follow_status") or "") != FollowStatus.FOLLOWING:
            set_target_follow_status(user_id, profile_url, FollowStatus.FOLLOWING)
        return FollowOutcome.ALREADY_FOLLOWING
    if state != FollowStatus.NOT_FOLLOWING or control is None:
        # Expected no-op, not selector rot: plenty of profiles expose no Follow control at all
        # (already connected with following off, a creator-mode-off account, a restricted page). A
        # warning here would file a defect for working behaviour on every such target.
        # Nothing is written: "we could not read it" is what `unknown` already means, so storing it
        # would spend a round-trip per visit to overwrite a state with itself — and would erase a
        # `not_following` an earlier, readable visit had established.
        log_debug("No follow control on roster target's activity page", user_id=user_id,
                  action_type="follow", task_name="auto_follow_roster_target")
        return FollowOutcome.NONE
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", control)
        sleep(random.uniform(1.5, 3.5))  # a human reads the card before clicking Follow
        driver.execute_script("arguments[0].click();", control)
    except Exception as e:
        log_warning("Roster follow click failed", exc=e, user_id=user_id, action_type="follow",
                    task_name="auto_follow_roster_target")
        record_target_follow_failure(user_id, profile_url)
        return FollowOutcome.FAILED
    # Recorded on DISPATCH, before the verdict is known: the click has already gone to LinkedIn, so
    # it costs the daily allowance whatever we read next.
    record_action(user_id, ACTION_FOLLOW)
    if _await_follow_flip(driver, profile_url, name, sleep=sleep) != FollowStatus.FOLLOWING:
        # The click landed somewhere but the button never flipped. Recording 'following' here is the
        # one failure that never self-corrects, so an unverified flip counts as a failed attempt —
        # and `reconcile_roster_follow_state` is what lets a later visit take it back.
        log_warning("Roster follow did not take — control never read 'Following'", user_id=user_id,
                    action_type="follow", task_name="auto_follow_roster_target")
        record_target_follow_failure(user_id, profile_url)
        return FollowOutcome.FAILED
    set_target_follow_status(user_id, profile_url, FollowStatus.FOLLOWING)
    log_info(f"Followed roster target {name or profile_url}", user_id=user_id,
             action_type="follow", task_name="auto_follow_roster_target")
    return FollowOutcome.FOLLOWED


# Reading the top card's CONNECT state, anchored on the page owner's name for exactly the reason
# _FOLLOW_CONTROL_JS is: LinkedIn renders "Connect" and "Message" inside recommendation modules and
# other people's cards all over an activity page. Strictly a READ — no element is returned, because
# nothing here ever clicks. The invite itself goes out through the existing rail
# (`invite_to_connect_now`), which opens the profile and uses the already-grounded Connect
# affordance; this resolver only advances what we believe about a target for free.
_CONNECT_STATE_JS = r"""
const SLUG = (arguments[0] || '').toLowerCase(), NAME = (arguments[1] || '')
  .replace(/\s+/g, ' ').trim().toLowerCase();
const FIRST = NAME.split(' ')[0] || '';
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
const label = (b) => norm(b.getAttribute('aria-label')) || norm(b.textContent);
// The owner's name must appear in the label. LinkedIn writes "Message Arvid Kahl",
// "Invite Arvid Kahl to connect", "Pending, awaiting response from Arvid Kahl" — a bare nameless
// "Message" could belong to anyone on the page.
const names = (text) => !!NAME && text.includes(NAME);
const shown = (el) => { const r = el.getBoundingClientRect(); return !!(r.width && r.height); };
const slugOf = (href) => {
  const m = (href || '').toLowerCase().match(/\/in\/([^\/?#]+)/);
  return m ? m[1] : '';
};

if (!NAME) return 'unknown';   // no owner name = nothing to anchor on = no safe read

// Is this control inside the owner's OWN card? Grow outward from the control until the subtree
// holds a profile link: the owner's alone = their card, anyone else's = someone else's module, so
// stop. This is the ONLY thing that makes a shortened label safe to read (see below), and an
// unresolvable card is false — which just means the reading stays what it was without it.
const ownerCard = (el) => {
  let cur = el.parentElement, d = 0;
  while (SLUG && cur && d < 8) {
    let own = false, other = false;
    for (const a of cur.querySelectorAll("a[href*='/in/']")) {
      const s = slugOf(a.getAttribute('href'));
      if (!s) continue;
      if (s === SLUG) own = true; else other = true;
    }
    if (other) return false;
    if (own) return true;
    cur = cur.parentElement; d++;
  }
  return false;
};
// LinkedIn shortens exactly one of these labels to the first name — the top card's "Message
// Harshal" (grounded 2026-08-03 against a 1st-degree connection, where only the 1st-degree marker
// carried the reading). A bare "Connect" with no aria-label is the same problem. Both are far too
// weak to trust page-wide — a "More profiles for you" rail can hold another Harshal — so a
// shortened label counts ONLY inside the owner's own card, and NEVER for Pending: LinkedIn writes
// the full name there, and a wrong `requested` freezes the ladder instead of merely stalling it.
const shortened = (text) =>
  (!!FIRST && (text === 'message ' + FIRST || text.startsWith('message ' + FIRST + ' '))) ||
  text === 'connect';

let pending = false, message = false, connect = false;
for (const b of document.querySelectorAll("button, [role='button'], a[role='link']")) {
  if (!shown(b)) continue;
  const text = label(b);
  const named = names(text);
  if (!named && !(shortened(text) && ownerCard(b))) continue;
  if (named && (text.startsWith('pending') || text.includes('awaiting response'))) pending = true;
  else if (text.startsWith('message')) message = true;
  else if (text.includes('to connect') || text.startsWith('connect')) connect = true;
}

// A 1st-degree badge inside the owner's own card is the strongest evidence there is; it is read
// only from the DOM around the target's exact /in/<slug> anchor, never page-wide (every 1st-degree
// person in a "More profiles for you" rail carries one).
let degreeFirst = false;
if (SLUG) {
  for (const a of document.querySelectorAll("a[href*='/in/']")) {
    if (slugOf(a.getAttribute('href')) !== SLUG) continue;
    let el = a, d = 0;
    while (el && d < 8) {
      if (/(^|[^a-z0-9])1st([^a-z0-9]|$)/.test(norm(el.textContent))) { degreeFirst = true; break; }
      el = el.parentElement; d++;
    }
    if (degreeFirst) break;
  }
}

if (pending) return 'requested';
if (degreeFirst) return 'connected';
// A named Message control alone is only evidence when LinkedIn is NOT still offering to connect —
// open profiles and creators expose Message to strangers too.
if (message && !connect) return 'connected';
return 'unknown';
"""


def _connect_status_of(target: dict) -> ConnectStatus:
    """Return a roster row's stored connect state as a member.

    Anything unrecognised (a row written before the migration, a column read back NULL) is `UNKNOWN`
    — the resting state, which does nothing.
    """
    stored = str((target or {}).get("connect_status") or "")
    try:
        return ConnectStatus(stored)
    except ValueError:
        return ConnectStatus.UNKNOWN


def _resolve_connect_state(driver: WebDriver, profile_url: str, name: str = "") -> ConnectStatus:
    """Read what the open activity page says about our connection to its owner (issue #979).

    Three readings only: `REQUESTED` (a Pending control), `CONNECTED` (a 1st-degree badge in the
    owner's own card, or a Message control with no Connect offered), and `UNKNOWN` — which means "we
    could not tell", never "not connected". Every caller treats `UNKNOWN` as "change nothing".
    """
    owner = _activity_page_owner_name(driver) or str(name or "").strip()
    if not owner:
        log_debug("Connect state unresolvable — no owner name to anchor the label match on",
                  action_type="invite_connect", task_name="_resolve_connect_state")
        return ConnectStatus.UNKNOWN
    try:
        state = driver.execute_script(_CONNECT_STATE_JS, profile_slug(profile_url), owner)
    except Exception as e:
        log_debug(f"Connect state resolution JS failed ({type(e).__name__}: {e})",
                  action_type="invite_connect", task_name="_resolve_connect_state")
        return ConnectStatus.UNKNOWN
    if state not in (ConnectStatus.REQUESTED, ConnectStatus.CONNECTED):
        return ConnectStatus.UNKNOWN
    return ConnectStatus(state)


def reconcile_roster_connect_state(driver: WebDriver, user_id: int, target: dict) -> ConnectStatus:
    """Advance a roster target's connect state from the activity page that is open anyway (issue #979).

    Clicks NOTHING and spends no budget — the `reconcile_roster_follow_state` pattern.

    This is the free half of the ladder: LinkedIn already shows whether our invite is pending or
    whether we are connected, so the state advances without a single extra action. It only ever
    moves FORWARD — an unreadable card leaves the record exactly as it was, because 'unknown' means
    we could not tell, not that the invite vanished.

    Returns the target's connect state after the reading.
    """
    stored = _connect_status_of(target)
    profile_url = str(target.get("profile_url") or "").strip()
    if not profile_url:
        return ConnectStatus.UNKNOWN
    state = _resolve_connect_state(driver, profile_url, name=str(target.get("name") or ""))
    if state == ConnectStatus.UNKNOWN or state == stored:
        return stored
    if state == ConnectStatus.REQUESTED and stored == ConnectStatus.CONNECTED:
        # Never walk the ladder backwards on one ambiguous reading.
        return ConnectStatus.CONNECTED
    set_target_connect_status(user_id, profile_url, state)
    log_info(f"Roster target {target.get('name') or profile_url} reads as {state} on LinkedIn — "
             f"connect state advanced", user_id=user_id, action_type="invite_connect",
             task_name="reconcile_roster_connect_state")
    return state


# How small a slice of the day's REMAINING invite budget the roster ladder may take. A third, so
# #398's profile-viewer and proactive lanes are never starved by a roster of restricted authors —
# most days that arithmetic means zero or one roster invite, which is the intended pace.
ROSTER_CONNECT_BUDGET_DIVISOR = 3


def roster_connect_budget(user_id: int, prefs: dict) -> int:
    """How many roster connect invites this user may send right now (issue #979).

    Returns 0 when the lane is off. There is no separate roster invite cap on purpose: an invite is
    an invite to LinkedIn, so the ladder spends the SAME `max_invites_per_day` the profile-viewer
    and proactive flows spend (`ACTION_INVITE`, the account envelope's own field). Already-queued
    requests count as spent for the same reason `_connect_target_budget` counts them — they will
    spend tomorrow's cap the moment it opens.
    """
    if not (prefs or {}).get("roster_auto_connect"):
        return 0
    try:
        cap = max(0, int(prefs.get("max_invites_per_day") or 0))
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return 0
    remaining = remaining_actions(user_id, ACTION_INVITE, cap,
                                  count_invites_sent_today(user_id)
                                  + count_open_connection_requests(user_id),
                                  caps=engagement_caps_from_prefs(prefs))
    if remaining <= 0:
        return 0
    # Ceiling division, floored at 1: a minority SHARE, but never a share so small it rounds the
    # ladder out of existence on a day that does have budget left.
    return max(1, -(-remaining // ROSTER_CONNECT_BUDGET_DIVISOR))


def _roster_connect_note(user_id: int, target: dict, prefs: dict) -> str:
    """Draft the invite note for a roster target.

    Uses the SAME voice-aligned path #486 does (`_draft_connect_note` → grounded template +
    `lem-simple` refinement), with the roster's own honest shared ground: we read and comment on
    their posts. Never a pitch.
    """
    profile_url = str(target.get("profile_url") or "").strip()
    name = str(target.get("name") or "").strip() or _author_display_name(profile_url)
    terms = target_terms_from_prefs(prefs)
    candidate = ScoredCandidate(person_key=person_key(name, profile_url), person_name=name,
                                person_profile_url=profile_url, source=SOURCE_ROSTER)
    return _draft_connect_note(user_id, candidate, topic=(terms[0] if terms else None))


def queue_roster_connect_invite(user_id: int, target: dict, prefs: dict,
                                queued_this_run: int = 0) -> bool:
    """Send one connection request for a roster target following did not unlock (issue #979).

    This is the ladder's single connect rung. Returns True when an invite was dispatched. It goes out
    through the EXISTING invite rail (`invite_to_connect_now`, via the `se_outreach` task below) — no
    second invite mechanic, and deliberately NOT through Outreach/Leads, whose unit of work is a DM
    sequence for a prospect. A curated peer or creator needs comment access, not a sales journey.

    `requested` is written BEFORE the dispatch, not after: this is the one-shot guarantee. A
    dispatch that is lost, or a worker that dies mid-send, must not leave the target eligible for a
    second invite on the next rotation — the task hands the target back to the ladder only when it
    knows nothing reached LinkedIn.

    `queued_this_run` is what the budget re-read cannot see. The send is ASYNCHRONOUS, so nothing
    durable records the invite until the task actually reaches LinkedIn — re-reading alone would
    hand every target in the walk the same "3 left" and invite the whole roster in one pass. The
    fresh read still does the rest of the job (other lanes, other runs, a cap changed mid-run).
    """
    if not (prefs or {}).get("roster_auto_connect"):
        return False
    profile_url = str(target.get("profile_url") or "").strip()
    if not profile_url:
        return False
    if _connect_status_of(target) in ENGAGEMENT_TARGET_CONNECT_TERMINAL:
        # ONE shot per target, enforced here as well as by the caller: LinkedIn's withdraw/expire
        # cycle governs a request that is already out, and a second automatic invite to someone who
        # declined the first is the pattern that gets accounts restricted.
        return False
    hold = _outbound_hold_reason(user_id)
    if hold:
        # DEBUG for the same reason the follow rung's is: re-read per target because the breaker can
        # trip mid-run, and a hold is a fact about the account, not about this person.
        log_debug(f"Roster connect invite skipped — {hold}", user_id=user_id,
                  action_type="invite_connect", task_name="queue_roster_connect_invite")
        return False
    if roster_connect_budget(user_id, prefs) - max(0, int(queued_this_run or 0)) <= 0:
        log_debug("Roster connect invite skipped — no share of today's invite budget left",
                  user_id=user_id, action_type="invite_connect",
                  task_name="queue_roster_connect_invite")
        return False
    note = _roster_connect_note(user_id, target, prefs)
    set_target_connect_status(user_id, profile_url, ConnectStatus.REQUESTED)
    send_roster_connect_invite.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url,
                                                   "message": note})
    log_info(f"Queued a connection request for roster target "
             f"{target.get('name') or profile_url} — following did not unlock commenting",
             user_id=user_id, action_type="invite_connect",
             task_name="queue_roster_connect_invite")
    return True


class RosterConnectOutcome(NamedTuple):
    """What the connect rung did for one target.

    Records the state it left behind, and whether THIS run is what sent the invite. The two are not
    the same — a target read as `requested` because the user invited them by hand is not a send the
    run may claim in its funnel.
    """
    state: ConnectStatus
    invited: bool


def advance_roster_connect(driver: WebDriver, user_id: int, target: dict, prefs: dict,
                           queued_this_run: int = 0) -> RosterConnectOutcome:
    """Advance the connect rung for one roster target, on the activity page already open (issue #979).

    Read-only advancement runs whatever the toggle says — a user who connected by hand must not keep
    a badge telling them to connect — and it runs FIRST, so a target LinkedIn already shows as
    connected or pending never draws an invite. Only `needs_connection` survives that reading, and
    only then does the opt-in invite fire.
    """
    stored = _connect_status_of(target)
    if stored not in (ConnectStatus.NEEDS_CONNECTION, ConnectStatus.REQUESTED,
                      ConnectStatus.FAILED):
        # 'unknown' has nothing to advance (the escalation has not fired) and 'connected' is the end
        # of the ladder. Reading either anyway would spend a JS round-trip per target per run.
        # 'failed' IS re-read, for the reason `reconcile_roster_follow_state` re-reads
        # 'follow_failed' (#962): terminal means no more SENDS, not no more reading — a send we
        # could not verify may well have landed, and a user who connected by hand must not keep a
        # badge saying the request failed. The reading can never invite: only 'needs_connection'
        # survives it, and `queue_roster_connect_invite` re-checks the terminal set anyway.
        return RosterConnectOutcome(stored, False)
    state = reconcile_roster_connect_state(driver, user_id, target)
    if state != ConnectStatus.NEEDS_CONNECTION:
        return RosterConnectOutcome(state, False)
    if not queue_roster_connect_invite(user_id, target, prefs, queued_this_run=queued_this_run):
        return RosterConnectOutcome(state, False)
    return RosterConnectOutcome(ConnectStatus.REQUESTED, True)


# What the run's feed sort actually was. Only FEED_SORT_RECENT means the recency-dominant scoring
# matrix (#622) ranked a recency-ordered feed; every other value means it ranked whatever LinkedIn's
# algorithm served, and the scan must not be read as if recency sorting was active (#817).
FEED_SORT_RECENT = "recent"            # confirmed on 'Recent' by the control itself
FEED_SORT_TOP = "top"                  # control found, still on the algorithmic sort
FEED_SORT_MISSING = "missing"          # no sort control on the home feed at all
FEED_SORT_UNKNOWN = "unknown"          # control there, but which sort applies could not be read
FEED_SORT_NOT_APPLICABLE = "n/a"       # a surface that never had one (group feed, roster activity)

# What can BE a sort trigger. Keyed on the interactive affordance — the tag, an ARIA role, or the
# popup/href that makes it clickable — never on a class name, which SDUI churns.
#
# Re-grounded for #1108. The drift sweep enumerated every DISPLAYED <button> on the live home feed
# in document order and the capture ran from the global nav straight into the first post's controls:
# there was no <button> between them AT ALL, share box included. So the miss was structural, not a
# label change — four of the five routes below used to require `self::button`, and whatever renders
# the sort today is not one. Widening the affordance is the fix that survives whichever element
# LinkedIn actually shipped; narrowing the label would only have re-guessed the same dead tag.
_X_SORT_AFFORDANCE = ("self::button or self::a or @role='button' or @role='combobox' "
                      "or @role='listbox' or @aria-haspopup")

# Ordered fallback chain for the home-feed sort trigger, most-stable anchor first: aria-label →
# data-testid → visible 'Sort by' text → a popup trigger whose whole label IS the current sort (the
# 'Sort by' prefix is dropped on narrow layouts) → a link carrying the sort in its own href.
_FEED_SORT_LOCATORS = [
    (By.XPATH, f"//*[{_X_SORT_AFFORDANCE}][contains({_X_LOWER_ARIA},'sort')]"),
    (By.XPATH, f"//*[{_X_SORT_AFFORDANCE}][contains({_X_LOWER_TESTID},'sort')]"),
    (By.XPATH, f"//*[{_X_SORT_AFFORDANCE}][contains({_X_LOWER_TEXT},'sort by')]"),
    # Exact text, never `contains`: a popup trigger reading exactly 'Top' or 'Recent' is the sort
    # control, while a card that merely CONTAINS the word 'recent' is someone's post (#1013 — never
    # click a control whose label names a different entity than the target).
    (By.XPATH, f"//*[@aria-haspopup or @role='combobox'][{_X_LOWER_TEXT}='{FEED_SORT_TOP}' or "
               f"{_X_LOWER_TEXT}='{FEED_SORT_RECENT}']"),
    # A link that carries the sort in its OWN href: navigating beats clicking when the page offers
    # it (#1030). Gated on the href also naming /feed — an unguarded 'sortby=' match would happily
    # resolve a link somebody SHARED in a post, and clicking it navigates the session off the feed
    # the scan is about to read (the #1012 wrong-entity hazard, by URL instead of by label).
    (By.XPATH, f"//main//a[contains({_x_lower('@href')},'/feed') and "
               f"(contains({_x_lower('@href')},'sortby=') or "
               f"contains({_x_lower('@href')},'sorttype='))]"),
]

_FEED_RECENT_OPTION_LOCATORS = [
    (By.XPATH, "//*[self::button or self::a or self::li or @role='menuitem' "
               "or @role='menuitemradio' or @role='radio' or @role='option']"
               f"[{_X_LOWER_TEXT}='{FEED_SORT_RECENT}']"),
    (By.XPATH, "//*[self::button or self::a or @role='menuitem' or @role='menuitemradio' "
               f"or @role='radio' or @role='option'][contains({_X_LOWER_TEXT},'{FEED_SORT_RECENT}')]"),
    (By.XPATH, f"//*[{_X_LOWER_TEXT}='{FEED_SORT_RECENT}']"),
]


# When the home-feed sort control does not resolve on a feed that DID render cards, describe what
# the page rendered instead (#1270). Same mechanism as the comment sweep's capture (#1117/#1255) —
# ONE two-pass scan in `utilities/linkedin/sort_evidence.py`, parameterised here with the feed's own
# shape. The prod reading it produces is what #1108's locator iteration is currently missing: in one
# 7-day window the sweep logged 30 `Selector miss: Feed sort control` and shipped nothing about the
# page's shape anywhere a human can query.
#
# The anchor and the prose container are BOTH `_FEED_POST_TEXT_SEL` — the same live-grounded node
# the card walk enumerates posts from. As the anchor it is the first feed card, so the header pass
# keeps only controls rendered above it (the sort control's home, whatever it now calls itself). As
# the prose container it stops a post BODY from matching the sort keywords: a short post reading
# 'sort of agree' would otherwise fill the cap with someone's writing and starve the header pass —
# the only pass that can see a control whose label rotated away from every keyword. Unlike the
# comment side's list, this selector is the text node itself, so the guard only holds because the
# scan reads containment BOTH ways — a card's wrapper divs inherit that same short post text and
# are ancestors, not descendants, of the container named here.
_FEED_SORT_CONTROL_SCAN_JS = build_sort_control_scan_js(
    item_selectors=[_FEED_POST_TEXT_SEL],
    prose_container=_FEED_POST_TEXT_SEL,
)


def _report_feed_sort_control_miss(driver, user_id: Optional[int] = None) -> list[dict]:
    """Ship the page's own shape when the home-feed sort control is unreadable, and return it.

    Called ONLY once the zero-walk cross-check has graded the miss as real drift, so the sample
    always describes a feed that provably rendered cards — a dead session and a login wall hand back
    the same missing control and describing either would be evidence about nothing.

    The level here is DEBUG deliberately: `_report_zero_walk` already owns the WARNING for this
    miss, and re-stating it would file a second grouped `$exception` for one fault
    (`utilities/CLAUDE.md`, "one condition gets ONE warning"). The EVENT is what survives prod's
    `LOG_LEVEL=INFO` + `POSTHOG_LOG_LEVEL=WARNING`, which is exactly what dropped #1118's capture.

    Never raises: evidence collection must not cost the sort attempt it rode in on.
    """
    try:
        candidates = scan_sort_control_candidates(driver, _FEED_SORT_CONTROL_SCAN_JS)
        log_debug("Feed sort control unreadable on a feed that rendered cards",
                  user_id=user_id, action_type="scrape", task_name="_switch_feed_to_recent",
                  candidates=candidates)
        track_selector_evidence("feed_sort_control", candidates, user_id=user_id,
                                task_name="_switch_feed_to_recent")
        return candidates
    except Exception as e:
        log_debug(f"Could not capture feed sort-control evidence: {e}", user_id=user_id,
                  action_type="scrape", task_name="_switch_feed_to_recent")
        return []


def _is_home_feed(driver) -> bool:
    """Return True only on linkedin.com/feed itself.

    Group feeds and a roster author's recent-activity page reuse the same commenting engine but never
    had a home-feed sort control, so a miss there is an expected no-op — warning on it would file a
    defect for working behaviour. An unreadable URL counts as NOT the home feed (issue #872): a dead
    session cannot say which surface it was on, and escalating on a guess costs a triage for working
    behaviour. A false silence loses one signal; a false defect costs a person.
    """
    try:
        path = str(driver.current_url or "").split("?")[0].split("#")[0].lower()
    except Exception:
        return False
    return path.rstrip("/").endswith("/feed")


def _feed_sort_state(control) -> str:
    """Return which sort a found control reports.

    Values are FEED_SORT_RECENT / FEED_SORT_TOP, or '' when the label is unreadable. '' is
    load-bearing: 'we could not tell' must never be recorded as 'recent'. A label naming BOTH sorts
    is unreadable too. Some dropdown triggers spell their options into the accessible name ('Sort by,
    currently Top, options Top and Recent'), and taking 'recent' from one would do the two worst
    things at once: skip the flip below (the label already 'says' Recent) and record the run as sorted
    — the exact lie #817 exists to stop.
    """
    if control is None:
        return ""
    try:
        label = f"{control.get_attribute('aria-label') or ''} {control.text or ''}".lower()
    except Exception:
        return ""
    has_recent, has_top = FEED_SORT_RECENT in label, FEED_SORT_TOP in label
    if has_recent and not has_top:
        return FEED_SORT_RECENT
    if has_top and not has_recent:
        return FEED_SORT_TOP
    return ""


def _switch_feed_to_recent(driver, wait, user_id: int = None) -> str:
    """Flip the home feed's sort from 'Top' to 'Recent' and REPORT what the run actually got.

    A silent no-op was fine before #622 made feed scoring recency-dominant; it is not now. With the
    sort control missing, `_score_feed_post`'s recency term ranks a candidate pool LinkedIn has
    already reordered by engagement, so the run quietly degrades to roughly what #622 replaced. The
    return value is the run's sort state and it rides into the feed funnel + `feed_scan` event, so a
    scan is never read as recency-sorted when it wasn't (#817).

    FEED_SORT_RECENT is returned ONLY when the control confirms it afterwards — an unverified flip
    recorded as sorted tells the same lie the silent no-op told. Fail-fast (`max_try=1`): this runs
    twice a run and each retry round burned MAX_WAIT_RETRY x ~5s before reporting the same miss.

    A miss is graded against the page itself before it is logged as drift (#1108): the return value
    is FEED_SORT_MISSING either way, so callers are unaffected, but only a feed that provably
    rendered posts turns a missing control into a WARNING — and only that same reading ships a DOM
    evidence sample (#1270), so the next locator iteration has the page's own shape to read.
    """
    try:
        if not _is_home_feed(driver):
            log_debug("Skipping feed sort — not the home feed", action_type="scrape", user_id=user_id)
            return FEED_SORT_NOT_APPLICABLE
        btn = find_first(driver, wait, _FEED_SORT_LOCATORS, "Feed sort control", required=False,
                         visible_only=True, max_try=1, warn_on_miss=False, user_id=user_id)
        if btn is None:
            # #1108: zero is not "nothing to do" until the page agrees. A dead session, a login
            # wall and a rotated anchor all hand back the same None, and only the last is a defect
            # — so the miss is graded against a per-post control this chain does not use (#1013).
            # That grading owns the log level (`warn_on_miss=False` above), because `find_first`
            # would otherwise warn on all three and file a defect for a feed that never rendered.
            verdict = _report_zero_walk(driver, _FEED_CARD_CROSSCHECK_SEL, "Feed sort control",
                                        user_id=user_id, action_type="scrape",
                                        task_name="_switch_feed_to_recent")
            # Only DRIFT is worth an evidence sample: 'empty' and 'unknown' describe a page that
            # rendered nothing to describe, and shipping those readings would put a dead session's
            # DOM next to real drift in the one query #1108 iterates from.
            if verdict == _zw.DRIFT:
                _report_feed_sort_control_miss(driver, user_id)
            return FEED_SORT_MISSING
        # The control reads 'Recent' once flipped — skip re-opening the menu so the second caller in
        # a run (navigate_to_feed then comment_on_feed_inline) is a cheap no-op.
        if _feed_sort_state(btn) == FEED_SORT_RECENT:
            return FEED_SORT_RECENT
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(random.uniform(1, 2))
        opt = find_first(driver, wait, _FEED_RECENT_OPTION_LOCATORS, "Recent sort option",
                         required=False, visible_only=True, max_try=1, user_id=user_id)
        if opt is None:
            return FEED_SORT_TOP
        driver.execute_script("arguments[0].click();", opt)
        time.sleep(random.uniform(2, 3.5))
        after = find_first(driver, wait, _FEED_SORT_LOCATORS, "Feed sort control", required=False,
                           visible_only=True, max_try=1, user_id=user_id)
        return _feed_sort_state(after) or FEED_SORT_UNKNOWN
    except Exception as e:
        log_warning("Feed recent-sort failed", exc=e, action_type="scrape", user_id=user_id)
        return FEED_SORT_UNKNOWN


_FEED_FUNNEL_KEY = "linkedin:feed_funnel:{user_id}"
_FEED_FUNNEL_TTL = 30 * 24 * 60 * 60  # keep the last scan's reach estimate for 30 days
# Consecutive top-candidate include-misses before we relax to the fallback (comment on the best feed
# post regardless of the include filters). Hard excludes / recency / min-reactions still apply.
_FEED_FALLBACK_AFTER_MISSES = 6


def set_feed_funnel(user_id: int, funnel: dict) -> None:
    """Persist the last feed scan's reach funnel.

    Stores posts examined -> matched -> commented so the UI can show the user how strict their
    targeting is. Best-effort; no-op without Redis.
    """
    client = _redis_client()
    if client is None:
        return
    try:
        client.set(_FEED_FUNNEL_KEY.format(user_id=user_id), json.dumps(funnel), ex=_FEED_FUNNEL_TTL)
    except Exception as e:
        log_warning("Could not store feed funnel", exc=e, user_id=user_id, action_type="comment")


def get_feed_funnel(user_id: int) -> "dict | None":
    """Last feed scan's reach funnel for a user, or None if there hasn't been one recently."""
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_FEED_FUNNEL_KEY.format(user_id=user_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, TypeError):
        return None


def _engage_card(ctx: FeedRunContext, card, key: str, content: str, author: str,
                 is_group_feed: bool = False) -> bool:
    """Claim, generate, react, and comment on ONE post card.

    True only when the comment actually landed. Shared by the roster pass and the feed walk so both
    go through the same at-most-once claim, the same per-run blueprint rotation, and the same
    react-before-submit ordering — `ctx` is what makes that literal rather than a convention (issue
    #1220): both passes read one run's preferences, voice synthesis and dedup state.

    `ctx.recent_comments` is the user's own recent comment history (newest first) that the quality
    gate dedups this draft against; a comment that lands is prepended to it, so two posts in the
    SAME run can't get near-identical comments either (issue #617).

    `is_group_feed` changes the ORDERING for the group-feed lane only: the comment composer is
    resolved BEFORE the `lem-medium` generation is spent (issue #1084). A miss releases the claim
    and returns False so the caller can count it as a skipped-no-composer post. The roster and home
    feed keep the original generate-first ordering, which is why this stays a per-CARD argument
    instead of being read off `ctx`: a roster target's activity page is not a group feed even when
    the run that reached it is one.
    """
    driver, wait, user_id = ctx.driver, ctx.wait, ctx.user_id
    prefs, my_profile = ctx.prefs, ctx.my_profile
    if not claim_post_for_comment(user_id, key):
        return False

    resolved_composer = None
    if is_group_feed:
        # Group feeds: prove the composer is reachable BEFORE spending an LLM call. Up to ~2,500
        # group posts per day were being run through `generate_ai_response` even though the composer
        # never resolved, so this ordering saves real cost (issue #1084). The click itself is best-
        # effort and leaves the composer open for the type/submit path below.
        if click_first(driver, wait, _COMMENT_ACTION_LOCATORS,
                       "Open comment composer", parent_element=card, required=False,
                       user_id=user_id) is None:
            log_debug("Group feed comment action not found — skipping without LLM generation",
                      user_id=user_id, action_type="comment")
            release_post_claim(user_id, key)
            return False
        time.sleep(random.uniform(1.5, 3))
        resolved_composer = _post_composer_for_card(driver, card, user_id=user_id)
        if resolved_composer is None:
            log_debug("Group feed composer did not resolve — skipping without LLM generation",
                      user_id=user_id, action_type="comment")
            release_post_claim(user_id, key)
            return False

    comment_blueprint = select_blueprint("comment", recent_formats=ctx.used_comment_shapes)
    with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
        comment_text = generate_ai_response(content, my_profile, None, prefs=prefs,
                                            profile_synthesis=ctx.profile_synthesis,
                                            blueprint=comment_blueprint,
                                            recent_comments=ctx.recent_comments, user_id=user_id,
                                            post_id=key)
    if comment_text and comment_blueprint.get("format"):
        ctx.used_comment_shapes.insert(0, comment_blueprint["format"])
    if not comment_text:
        release_post_claim(user_id, key)  # no comment generated (or none cleared the quality gate)
        return False
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
    # Read-time-realistic delay (issue #626): scaled to the post's length and floored well above
    # "instant", so we never comment faster than a human could have read the thing. The old
    # half-reading-time + thinking-time pair could clear a short post in ~3s, which is the loudest
    # cadence tell there is.
    pace_read(content, user_id=user_id)
    # React BEFORE submitting the comment: posting re-renders the card and staled the element, so
    # the old post-comment reaction attempt silently failed. Skip our OWN posts. Non-fatal — a
    # missed reaction never blocks the comment.
    #
    # Gated OFF by default since #816. DEBUG, not a warning: a deliberate stand-down is working
    # behaviour, and warning it every card is exactly the "expected no-op" that escalates into a
    # filed defect. The commenting path below is untouched.
    if not INLINE_REACTIONS_ENABLED:
        log_debug("Inline reactions disabled (INLINE_REACTIONS_ENABLED=False, issue #816)",
                  user_id=user_id, action_type="comment")
    elif not _author_is_me(author, my_profile):
        outcome = react_to_post_inline(driver, wait, card, post_content=content,
                                       comment_text=comment_text, user_id=user_id)
        if outcome is None:
            # Already reacted is a no-op, not a failure. Reporting it as one made a benign skip
            # indistinguishable from a broken selector, and now that repeated warnings escalate it
            # would file a defect for working behaviour.
            log_debug("Post already carried our reaction — skipping", user_id=user_id,
                      action_type="comment")
        elif outcome:
            mark_post_reacted(user_id, key)
        else:
            # Every way react_to_post_inline reports a failure has already warned where it happened
            # — the fly-out opener's miss when there was no toggle to default-Like (issue #873), the
            # reaction click's own miss, the wrapped exception, or the click that never registered.
            # Warning again from out here filed a SECOND PostHog defect for one condition (#878).
            log_debug("No reaction landed on post — continuing to the comment", user_id=user_id,
                      action_type="comment")
    if not post_comment_inline(driver, wait, card, comment_text, user_id=user_id,
                               composer=resolved_composer):
        release_post_claim(user_id, key)  # posting failed — let a later run retry
        return False
    mark_post_commented(user_id, key)
    insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT,
                   result=LogResultType.SUCCESS, post_url=key, message=comment_text)
    record_action(user_id, ACTION_COMMENT)  # account-level governor (issue #626)
    ctx.recent_comments.insert(0, comment_text)
    time.sleep(random.uniform(6, 14))  # human pacing between comments
    return True


def comment_on_roster_posts(ctx: FeedRunContext, max_posts: int) -> dict:
    """Comment on the user's curated engagement roster before the home feed gets a look (issue #616).

    Each selected target's recent-activity page is opened, and the first post that clears hard
    excludes, the dedup ledger and the on-topic gate gets a comment. Fail-closed by design — an
    author page that renders no commentable card (selector drift, an auth wall, a profile with only
    reshares) is logged and skipped, never guessed at. Returns the run counters the caller folds
    into the feed funnel.

    That skip used to be INVISIBLE (issue #962). A target whose posts render but whose cards carry
    no comment affordance at all is the restricted-comments signature — the author only accepts
    comments from connections or followers — and it is now counted per target so the roster card can
    tell the user that following or connecting would unlock the account. When they have opted in,
    the same visit also does the paced follow, on the page that is already open.

    `ctx.seen` is the RUN's dedup set, shared with the feed walk that follows this pass, so a post
    commented on here can never be re-commented from the home feed (issue #1220 moved it onto the
    context; it was already the same object passed by hand).
    """
    driver, user_id, prefs, seen = ctx.driver, ctx.user_id, ctx.prefs, ctx.seen
    stats = {"posted": 0, "targets_visited": 0, "examined": 0, "off_topic_skipped": 0,
             "comment_blocked": 0, "followed": 0, "connect_requested": 0,
             "key_sources": {}, "commented_key_sources": {}}
    if max_posts <= 0:
        return stats
    targets = get_engagement_targets(user_id, active_only=True)
    if not targets:
        return stats
    follow_enabled = bool((prefs or {}).get("roster_auto_follow"))
    # Announced ONCE per run rather than per target: a pause or an open breaker is not a fact about
    # any one roster target, and the per-follow re-read (the breaker can trip mid-run) is DEBUG.
    follow_hold = _outbound_hold_reason(user_id) if follow_enabled else ""
    if follow_hold:
        log_info(f"Roster auto-follow standing down this run — {follow_hold}", user_id=user_id,
                 action_type="follow", task_name="comment_on_roster_posts")
    # Blocked visits are collected and written AFTER the walk, so one run-level check can tell a
    # roster of genuinely restricted authors from `_card_for_textbox` having drifted — the latter
    # would badge every target at once with a confident lie about their accounts.
    blocked_visits: list = []
    for target in select_roster_targets(targets, max_posts):
        if stats["posted"] >= max_posts or ctx.out_of_time(time.time()):
            break
        profile_url = target.get("profile_url")
        url = _roster_activity_url(profile_url)
        if not url:
            continue
        try:
            driver.get(url)
            wait_for_ajax(driver)
        except Exception as e:
            if is_session_lost(e):
                # The browser is gone (issue #988 — a deploy quits the session once the drain
                # window is spent). Every remaining target is unreachable for that same reason, so
                # warning per target would file the deploy as a defect through this door instead:
                # three of these identical warnings cross the escalation threshold. Stop the walk
                # on what already shipped and let the caller end the run.
                log_info("Browser session ended mid-run (worker or Grid restart) — stopping the "
                         "roster walk", user_id=user_id, action_type="comment",
                         task_name="comment_on_roster_posts")
                break
            log_warning("Could not open roster target's activity page", exc=e, user_id=user_id,
                        action_type="comment", task_name="comment_on_roster_posts")
            continue
        time.sleep(random.uniform(2, 4))
        stats["targets_visited"] += 1
        weekly_left = max(0, resolve_weekly_cap(target.get("max_comments_per_week"))
                          - int(target.get("comments_this_week") or 0))
        allowed_here = min(_ROSTER_MAX_POSTS_PER_AUTHOR, weekly_left, max_posts - stats["posted"])
        posted_here = 0
        # The two halves of the restricted-comments signature (#962): posts that rendered, and posts
        # that offered a way to comment. Only "some of the first, none of the second" is evidence.
        posts_seen, commentable_seen, truncated = 0, 0, False
        for box in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL):
            if ctx.out_of_time(time.time()):
                # The walk stopped early, so "no card offered a comment affordance" is a statement
                # about how far we got, not about the author. Tracked so it can't badge them.
                truncated = True
                break
            if posted_here >= allowed_here:
                break
            try:
                content = (box.text or "").strip()
            except StaleElementReferenceException:
                continue
            if len(content) < 20:
                continue
            posts_seen += 1
            card = _card_for_textbox(driver, box)
            if card is None:
                continue  # no comment affordance on this item — not a commentable post
            commentable_seen += 1
            author = _post_author_from_card(card) or (target.get("name") or "")
            key, key_source = _feed_post_identity(card, author, content, driver=driver)
            fps = _feed_content_fingerprints(author, content)
            if key in seen or (fps & seen):
                continue
            stats["examined"] += 1
            stats["key_sources"][key_source] = stats["key_sources"].get(key_source, 0) + 1
            seen.add(key)
            seen.update(fps)
            if (has_commented_post(user_id, key) or has_user_commented_on_post_url(user_id, key)
                    or not _passes_hard_excludes(content, author, prefs)):
                continue
            if not passes_topic_gate(content, prefs):
                stats["off_topic_skipped"] += 1
                log_info(f"Skipped roster post by {author or profile_url}: off-topic for the "
                         f"user's focus topics", user_id=user_id, action_type="comment",
                         task_name="comment_on_roster_posts")
                continue
            if _engage_card(ctx, card, key, content, author):
                posted_here += 1
                stats["posted"] += 1
                stats["commented_key_sources"][key_source] = \
                    stats["commented_key_sources"].get(key_source, 0) + 1
                record_target_engagement(user_id, profile_url)
                # That call just stood any pending escalation down (issue #979) — commenting WORKED,
                # so "following didn't unlock commenting" is no longer true. The connect rung below
                # reads THIS row, which the run loaded before the comment landed, so the in-memory
                # copy has to be stood down too or the rung would spend the account's one invite on
                # a target we can demonstrably comment on.
                if _connect_status_of(target) == ConnectStatus.NEEDS_CONNECTION:
                    target["connect_status"] = ConnectStatus.UNKNOWN.value
                log_info(f"Commented on roster target {author or profile_url} "
                         f"({target.get('category')})", user_id=user_id, action_type="comment",
                         task_name="comment_on_roster_posts")
        if posts_seen and not commentable_seen and not truncated:
            stats["comment_blocked"] += 1
            blocked_visits.append(target)
            # DEBUG, not a warning: an author who restricts commenting is WORKING behaviour on their
            # side, and this visit repeats every rotation — warning it would escalate and file a
            # defect for a post nobody was ever allowed to comment on.
            log_debug(f"Roster target {profile_url} rendered {posts_seen} posts with no comment "
                      f"affordance — commenting looks restricted", user_id=user_id,
                      action_type="comment", task_name="comment_on_roster_posts")
        elif posted_here == 0:
            log_info(f"No commentable on-topic post found for roster target {profile_url}",
                     user_id=user_id, action_type="comment", task_name="comment_on_roster_posts")
        # Auto-follow LAST, on the page that is already open: a follow click re-renders the top card,
        # and doing it before the comment walk would stale the very cards that walk reads.
        if follow_enabled:
            follow_status = str(target.get("follow_status") or "")
            if follow_status in ENGAGEMENT_TARGET_FOLLOW_TERMINAL:
                if follow_status == FollowStatus.FOLLOW_FAILED:
                    # Terminal means no more CLICKS, not no more reading. Read-only, costs no
                    # budget: a follow we could not verify may well have landed, so the state has to
                    # stay correctable or 'follow_failed' is a permanently wrong answer.
                    reconcile_roster_follow_state(driver, user_id, target)
            elif roster_follow_budget(user_id, prefs) > 0:
                # Re-read per target, not decremented from a per-run local: the click is recorded on
                # dispatch, so this is also what stops two overlapping runs each spending the cap.
                if auto_follow_roster_target(driver, user_id, target) == FollowOutcome.FOLLOWED:
                    stats["followed"] += 1
        # The rung above follow (#979): free read-only advancement for a target already on the
        # ladder, and — only when the user opted in — the one connect invite for a target following
        # demonstrably did not unlock. Never gated on `follow_enabled`: a user who turned auto-follow
        # off (or connected by hand) must still see their badge clear.
        # The run's own count is passed in because the send is asynchronous: the budget re-read
        # cannot see an invite that has been dispatched but not yet delivered to LinkedIn.
        if advance_roster_connect(driver, user_id, target, prefs,
                                  queued_this_run=stats["connect_requested"]).invited:
            stats["connect_requested"] += 1
    _record_blocked_visits(user_id, blocked_visits, stats["targets_visited"])
    return stats


def _record_blocked_visits(user_id: int, blocked_visits: list, targets_visited: int) -> None:
    """Persist the run's restricted-comments findings, unless the run itself is the suspect.

    `blocked_visits` holds the roster ROWS as the run loaded them — the connect escalation is
    announced by comparing what came back against that pre-run state. `_card_for_textbox` returning
    None for EVERY card of EVERY target is far more likely to be that helper drifting against
    LinkedIn's SDUI than a roster where nobody accepts comments — and the badge it would raise tells
    the user something false about other people's accounts. Small rosters are exempt from the check:
    two restricted authors out of two visited is an ordinary roster.
    """
    if targets_visited >= 3 and len(blocked_visits) == targets_visited:
        log_warning(f"Every roster target visited ({targets_visited}) rendered posts with no "
                    f"comment affordance — treating this as comment-selector drift, not {targets_visited} "
                    f"restricted authors; no blocked visits recorded", user_id=user_id,
                    action_type="comment", task_name="comment_on_roster_posts")
        return
    for target in blocked_visits:
        profile_url = str(target.get("profile_url") or "").strip()
        visit = record_target_comment_blocked(user_id, profile_url)
        # Exactly at the threshold, so the surface crossing is announced ONCE rather than on every
        # visit for as long as the target stays blocked.
        if visit.streak == ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK:
            log_info(f"Roster target {profile_url} has been un-commentable for {visit.streak} visits "
                     f"— surfaced on the roster card", user_id=user_id, action_type="comment",
                     task_name="comment_on_roster_posts")
        # Announced once, on the CROSSING, for the same reason: the escalation only ever fires on
        # the transition out of 'unknown', so comparing against what the run loaded is what keeps a
        # target that has been waiting for a connection for weeks from saying so every rotation.
        if (visit.connect_status == ConnectStatus.NEEDS_CONNECTION
                and _connect_status_of(target) != ConnectStatus.NEEDS_CONNECTION):
            log_info(f"Roster target {profile_url} is still un-commentable after we followed them "
                     f"— flagged for a connection request", user_id=user_id,
                     action_type="invite_connect", task_name="comment_on_roster_posts")


def comment_on_feed_inline(driver, wait, my_profile: LinkedInProfile, user_id: int,
                           max_posts: int = 10, deadline_ts: float = None, prefs: dict = None,
                           engagers: set = None, is_group_feed: bool = False) -> int:
    """Comment on the user's curated engagement roster first, then walk the SDUI feed (issue #616).

    Uses whatever budget is left, prioritizing by a scoring matrix instead of DOM order:
    recency-dominant (golden hour), then relevance, reciprocity (people who engaged with us), and
    healthy activity. Applies targeting filters + per-day cap + a max-post-age gate, and never
    comments on a post that fails the on-topic gate. Returns the total number of comments posted.

    `is_group_feed` is True when the feed is a LinkedIn group feed (called from
    `auto_comment_in_groups`). On groups the composer is resolved before the `lem-medium` comment
    generation is spent, so posts with no reachable composer cost zero LLM calls (issue #1084).
    The home feed and roster pass keep the original ordering.
    """
    from selenium.common.exceptions import StaleElementReferenceException
    if prefs is None:
        prefs = get_engagement_preferences(user_id)
    if engagers is None:
        engagers = get_recent_engagers(user_id)
    daily_cap = prefs.get("max_comments_per_day") or 20
    # Human pacing (issue #626): today's allowance is a stable random draw from the cap (with
    # weekend asymmetry and occasional rest days), and the account-level governor also caps the
    # COMBINED comment/DM/invite traffic — so a flat "cap comments every day" volume signature
    # never shows up, and the lanes can't each spend a full cap on the same day.
    remaining_today = remaining_actions(user_id, ACTION_COMMENT, daily_cap,
                                        count_comments_today(user_id),
                                        caps=engagement_caps_from_prefs(prefs))
    if remaining_today <= 0:
        log_info(f"Daily comment budget spent (cap {daily_cap}) — skipping")
        return 0
    max_posts = min(max_posts, remaining_today)
    max_age_min = (prefs.get("max_post_age_hours") or 24) * 60
    min_reactions = prefs.get("min_reactions") or 0
    # Stable VOICE synthesis (cached weekly, lazily created on first use) — the voice source for every
    # comment this run, in place of the bloated/volatile full profile JSON.
    profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)

    # Per-run comment ANGLE rotation from the shared framework core: each comment this run gets a
    # different archetype (Expander, Storyteller, Questioner, ...) so a day's comments never all
    # read from the same template. In-memory only — comments are too high-volume to justify a DB
    # shape history, and per-run rotation is what a reader of the same feed would notice.
    used_comment_shapes: list = []

    # The user's own recent comments — what the comment-side similarity gate dedups each fresh draft
    # against (issue #617). Loaded once per run and appended to as comments land, so neither a
    # rewording of yesterday's comment nor two near-identical comments in this run can ship.
    try:
        recent_comments: list = list(get_recent_comment_texts(user_id))
    except Exception as e:
        log_warning("Could not load recent comment history; similarity gate degrades to none",
                    exc=e, user_id=user_id, action_type="comment")
        recent_comments = []

    posted, seen, scrolls = 0, set(), 0
    # ONE context for the whole run (issue #1220): the roster pass and the feed walk below both read
    # it, so they cannot disagree about the preferences, the voice synthesis, or the dedup/rotation
    # state they are accumulating into.
    ctx = FeedRunContext(driver=driver, wait=wait, my_profile=my_profile, user_id=user_id,
                         prefs=prefs, profile_synthesis=profile_synthesis, seen=seen,
                         used_comment_shapes=used_comment_shapes, recent_comments=recent_comments,
                         engagers=engagers, deadline_ts=deadline_ts, is_group_feed=is_group_feed)
    # Roster FIRST (issue #616): curated peers / ICP / large creators outrank whatever the home
    # feed happens to serve. An empty roster returns zeros here and the run degrades to the plain
    # feed walk below. `seen` is shared, so a roster post can never be re-commented from the feed.
    # Group feeds have no roster pass, so this call returns zeros immediately.
    roster_stats = comment_on_roster_posts(ctx, max_posts)
    posted = roster_stats["posted"]
    off_topic_skipped = 0
    skipped_no_composer = 0  # group-feed lane only (issue #1084)

    if roster_stats["targets_visited"]:
        navigate_to_feed(driver, wait)  # the roster pass navigated away from the feed
    # Surface golden-hour posts; scoring still ranks them. The returned state is recorded on the
    # funnel + the feed_scan event — an unsorted scan ranked an algorithmic candidate pool (#817).
    feed_sort = _switch_feed_to_recent(driver, wait, user_id=user_id)
    # Reach funnel (surfaced to the user so they can tell when their targeting is too strict) and the
    # empty-filter fallback. examined = posts we looked at; hard = passed excludes/recency/min-reactions;
    # include = also matched the user's include topics/keywords/authors.
    examined_keys, hard_keys, include_keys = set(), set(), set()
    # Which SOURCE produced each post's dedup key (permalink / card URN / content hash). Surfaced on
    # the funnel + logs so a live run proves feed comments key on the stable URN — a hash-keyed
    # comment is the duplicate-prone path that caused #474/#580.
    key_source_by_key: dict = {}
    posted_key_sources: dict = {}
    strict_misses, fallback_active, fallback_used = 0, False, False
    # Posts that cleared excludes + dedup but failed ONLY the recency/min-reactions gates. Tracked
    # apart from the permanent `seen` set so the empty-feed fallback can RECONSIDER them (relaxing
    # those two gates) when nothing clears the hard filters at all. Without this, a sparse or
    # low-reaction feed produces zero comments even with feed_fallback_when_empty on — because the
    # existing include-miss fallback only triggers on posts that first passed the hard gates.
    soft_seen: set = set()
    hard_relaxed = False
    # The most post MARKERS any scroll pass saw (text node OR the card's own "Hide post by"
    # control). An image-only post has no text node but still carries that control, so counting only
    # text nodes treated a feed of image/video posts as selector drift (#1081). Zero across a whole
    # scan is the "the walk is blind" case the zero-walk tripwire below cross-checks against the page
    # (#1013) — every other funnel number is downstream of this one, so a zero here makes them all
    # meaningless.
    cards_seen = 0
    textboxes_seen = 0
    # Did the walk ever READ the feed? The loop is skipped whole when the roster pass already spent
    # the run's budget, and breaks before its first read when the deadline has passed — both leave
    # every counter at zero while the page still renders cards, which the tripwire used to grade as
    # selector drift. "We never looked" is not evidence about the page (#1081).
    walk_ran = False
    _incl = [f for f in ((prefs.get("include_keywords") or []) + (prefs.get("include_authors") or [])
                         + (prefs.get("include_topics") or [])) if f]
    fallback_enabled = bool(prefs.get("feed_fallback_when_empty", True)) and bool(_incl)
    while posted < max_posts and scrolls < 15:
        if ctx.out_of_time(time.time()):
            break
        # Gather + score every fresh candidate currently in view (cheap, no-LLM gates).
        candidates = []
        boxes = driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL)
        textboxes_seen = max(textboxes_seen, len(boxes))
        cards_seen = max(cards_seen, len(driver.find_elements(By.CSS_SELECTOR, _FEED_CARD_MARKER_SEL)))
        walk_ran = True
        for box in boxes:
            try:
                content = (box.text or "").strip()
            except StaleElementReferenceException:
                continue
            if len(content) < 20:
                continue
            card = _card_for_textbox(driver, box)
            if card is None:
                continue
            author = _post_author_from_card(card)
            # Canonical URN-based key (stable across re-renders); normalized content hash only
            # when no URN exists. `fps` are render-stable fingerprints at several prefix lengths, so
            # even the URN-less fallback path can't slip a second comment through when a re-render
            # truncates the body differently (#474, recurred as #580).
            key, key_source = _feed_post_identity(card, author, content, driver=driver)
            fps = _feed_content_fingerprints(author, content)
            if key in seen or (fps & seen):
                continue
            key_source_by_key[key] = key_source
            examined_keys.add(key)
            # Persistent, cross-run/worker dedup: skip anything already claimed or commented
            # (commented_posts ledger), plus historical SUCCESS comment logs, plus hard excludes.
            if (has_commented_post(user_id, key) or has_user_commented_on_post_url(user_id, key)
                    or not _passes_hard_excludes(content, author, prefs)):
                seen.add(key)
                seen.update(fps)
                continue
            # Recency + min-reactions are soft gates: when the whole feed fails them the empty-feed
            # fallback relaxes them (below), so a reject here goes to soft_seen (reconsiderable),
            # not the permanent `seen`. Skip already-soft-rejected posts until we relax.
            if not hard_relaxed and key in soft_seen:
                continue
            age = _post_age_minutes(driver, card)
            counts = _post_social_counts(card)
            if not hard_relaxed:
                if age is not None and age > max_age_min:        # recency gate
                    soft_seen.add(key)
                    continue
                if min_reactions and counts["reactions"] < min_reactions:
                    soft_seen.add(key)
                    continue
            meta = {"author": author, "age_minutes": age, "comments": counts["comments"],
                    "reactions": counts["reactions"], "relevant": _literal_relevant(content, author, prefs)}
            hard_keys.add(key)
            candidates.append((_score_feed_post(meta, ctx.prefs, ctx.engagers), key, card, content,
                               author, age, fps, key_source))

        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            score, key, card, content, author, age, fps, key_source = candidates[0]
            seen.add(key)        # decided on this one either way
            seen.update(fps)     # and its render-stable fingerprints (URN-less fallback guard)
            # Include gate (may use the LLM topic classifier) on the chosen post. If the feed keeps
            # producing nothing that matches the user's include filters, RELAX to fallback for the rest
            # of the run — comment on the best feed post regardless of include (LinkedIn already curates
            # the feed to relevant content). Hard excludes / recency / min-reactions still applied.
            if not fallback_active and fallback_enabled and strict_misses >= _FEED_FALLBACK_AFTER_MISSES:
                fallback_active = True
                log_info("Feed targeting matched nothing — falling back to top feed posts for this run")
            # On-topic gate FIRST and unconditionally (issue #616): the fallback may widen WHICH
            # posts qualify, but it may never put a comment on a post that is off-topic for the
            # user's focus topics — that is what damaged distribution in the 2026-07-25 funnel.
            if not passes_topic_gate(content, prefs):
                off_topic_skipped += 1
                log_info(f"Skipped feed post by {author or 'unknown author'}: off-topic for the "
                         f"user's focus topics", user_id=user_id, action_type="comment",
                         task_name="comment_on_feed_inline")
                continue
            if fallback_active:
                fallback_used = True
            elif post_matches_preferences(content, author, prefs):
                include_keys.add(key)
            else:
                strict_misses += 1
                continue
            # _engage_card atomically claims the post BEFORE spending an LLM call or commenting. If
            # a prior/concurrent run already holds it, we lose the race there and move on — at most
            # one comment per post per user, across the pre-post run, the golden-hour run, and retries.
            # On group feeds the composer is resolved before generation (issue #1084), so a miss is
            # counted separately instead of being folded into "examined but not commented".
            engaged = _engage_card(ctx, card, key, content, author,
                                   is_group_feed=ctx.is_group_feed)
            if engaged:
                posted_key_sources[key_source] = posted_key_sources.get(key_source, 0) + 1
                log_info(f"Feed comment keyed by {key_source} ({key})", user_id=user_id,
                         action_type="comment", task_name="comment_on_feed_inline")
                posted += 1
                log_info(f"Commented on {author or 'a'}'s post "
                        f"(score {score:.2f}, age {'?' if age is None else str(age) + 'm'}) ({posted}/{max_posts})")
            elif is_group_feed:
                # The only other group-feed failure mode handled inside _engage_card before
                # generation is a composer that did not resolve. Claim failures, generation failures,
                # and post-submit failures are all possible too, but they are either not group-
                # specific or already counted elsewhere; for telemetry we count the composer miss.
                skipped_no_composer += 1
            continue  # DOM re-rendered / candidate consumed — re-gather from the top
        # Nothing cleared the hard filters this pass. If the whole feed keeps coming up empty
        # (0 posts past excludes + recency + min-reactions) but some only missed the recency/
        # min-reactions gates, relax THOSE gates once and re-scan from the top — otherwise a
        # sparse or low-reaction feed yields zero comments even with feed_fallback_when_empty on
        # (excludes/dedup still apply; we still comment on the best-scored post).
        if (fallback_enabled and not hard_relaxed and posted == 0 and not hard_keys
                and soft_seen and scrolls + 1 >= _FEED_FALLBACK_AFTER_MISSES):
            hard_relaxed = True
            fallback_active = True
            log_info("No posts cleared the recency/min-reaction gates — relaxing them "
                    "(empty-feed fallback) and re-scanning the top of the feed")
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(random.uniform(2.0, 3.5))
            continue
        # nothing actionable in view — scroll to load more
        driver.execute_script("window.scrollBy(0, 1200);")
        scrolls += 1
        time.sleep(random.uniform(2.5, 4))

    examined_key_sources: dict = {}
    for source in key_source_by_key.values():
        examined_key_sources[source] = examined_key_sources.get(source, 0) + 1
    for source, count in roster_stats["key_sources"].items():
        examined_key_sources[source] = examined_key_sources.get(source, 0) + count
    for source, count in roster_stats["commented_key_sources"].items():
        posted_key_sources[source] = posted_key_sources.get(source, 0) + count
    feed_commented = posted - roster_stats["posted"]
    off_topic_total = off_topic_skipped + roster_stats["off_topic_skipped"]
    # A zero walk is ambiguous FOUR ways and only one of them is a defect (#1013, #1081). Grading
    # them apart is the whole point — a warning on any of the other three files a grouped
    # $exception for an ordinary day, which is how #1081 got filed in the first place:
    #   not_walked — the loop never read the feed (roster spent the budget, or the deadline had
    #                already passed). We never looked, so nothing about the page is claimed.
    #   no_text    — cards rendered, none carried a post-text node. A feed of image/video-only
    #                posts looks exactly like this, and those are not commentable anyway.
    #   ok         — the walk enumerated post text.
    #   drift/empty/unknown — no marker matched AT ALL, so ask the page through the reaction
    #                control, which no marker chain reads (an anchor the walk itself counts could
    #                only ever answer 'empty'). `drift` is the real defect: cards the walk is blind
    #                to, which makes every zero below it a lie rather than a quiet day.
    if not walk_ran:
        feed_walk = "not_walked"
        log_debug("Feed card walk never read the feed — budget spent or deadline passed first",
                  user_id=user_id, action_type="comment", task_name="comment_on_feed_inline")
    elif textboxes_seen:
        feed_walk = "ok"
    elif cards_seen:
        feed_walk = "no_text"
        log_debug(f"Feed card walk saw {cards_seen} card(s) but no post-text node — image/video "
                  f"posts carry nothing to comment on", user_id=user_id, action_type="comment",
                  task_name="comment_on_feed_inline")
    else:
        feed_walk = _report_zero_walk(driver, _FEED_WALK_CROSSCHECK_SEL, "Feed card walk",
                                      user_id=user_id, action_type="comment",
                                      task_name="comment_on_feed_inline")
    funnel = {
        "examined": len(examined_keys) + roster_stats["examined"],
        "passed_filters": len(hard_keys),      # cleared excludes + recency + min-reactions
        "matched_topics": len(include_keys),   # also matched include topics/keywords/authors
        "commented": posted,
        # Roster-sourced vs feed-sourced split (issue #616): a healthy account comments mostly on
        # its curated roster, so this is the number that says whether the roster is actually working.
        "roster_commented": roster_stats["posted"],
        "feed_commented": feed_commented,
        "roster_targets_visited": roster_stats["targets_visited"],
        "roster_examined": roster_stats["examined"],
        # Targets whose posts rendered with no comment affordance at all, and targets followed on
        # this run (issue #962). Both are roster-only — the home feed has neither notion.
        "roster_comment_blocked": roster_stats.get("comment_blocked", 0),
        "roster_followed": roster_stats.get("followed", 0),
        # Invites the connect rung sent this run (issue #979) — targets following did not unlock.
        # A rising blocked count with zero of these is a roster the user has to fix by hand.
        "roster_connect_requested": roster_stats.get("connect_requested", 0),
        "off_topic_skipped": off_topic_total,  # failed the on-topic gate — never commented on
        "fallback_used": fallback_used,
        # Group-feed lane only (issue #1084): posts whose composer could not be reached BEFORE the
        # LLM generation was spent. This makes the cost saving measurable on `feed_scan`.
        "skipped_no_composer": skipped_no_composer,
        "key_sources": examined_key_sources,           # every post we looked at
        "commented_key_sources": posted_key_sources,   # only the ones we commented on
        "max_post_age_hours": prefs.get("max_post_age_hours") or 24,
        "min_reactions": min_reactions,
        # Which feed ordering the recency-dominant scoring matrix actually ranked (#817). Anything
        # other than FEED_SORT_RECENT means the candidate pool was LinkedIn's algorithmic one.
        "feed_sort": feed_sort,
        # How many post-text nodes the walk ever saw, how many card markers (text or "Hide post by")
        # were visible, and what a zero meant (issues #1013/#1081): ok / no_text / not_walked /
        # empty / drift / unknown. `feed_walk='drift'` says the feed had cards this scan could not
        # see — every zero below it is a lie, not a quiet day. `no_text` says cards rendered with
        # nothing commentable on them, and `not_walked` that the loop never ran at all: both are
        # ordinary, and neither is evidence of drift.
        "textboxes_seen": textboxes_seen,
        "cards_seen": cards_seen,
        "feed_walk": feed_walk,
        "at": datetime.now().isoformat(),
    }
    set_feed_funnel(user_id, funnel)
    track_feed_scan(user_id, funnel)
    log_info(f"Engagement scan: examined {len(examined_keys) + roster_stats['examined']}, "
             f"passed filters {len(hard_keys)}, matched topics {len(include_keys)}, "
             f"commented {posted} (roster {roster_stats['posted']} / feed {feed_commented}), "
             f"off-topic skipped {off_topic_total}, "
             f"skipped-no-composer {skipped_no_composer}, "
             f"roster comment-blocked {roster_stats.get('comment_blocked', 0)}, "
             f"roster followed {roster_stats.get('followed', 0)}, fallback={fallback_used}, "
             f"sort {feed_sort}, key sources {examined_key_sources}", user_id=user_id,
             action_type="comment", task_name="comment_on_feed_inline")
    hash_commented = posted_key_sources.get("hash", 0)
    if hash_commented:
        # One URN-less card is a designed, self-healing degradation, not a defect: the per-run
        # content fingerprints stop a re-render re-keying it inside the scan, and
        # reconcile_recent_comment_urns upgrades the ledger row to feedurn:// afterwards. What #580
        # actually was looks different — the URN resolver finding NOTHING anywhere, so not one post
        # in the whole scan yields a URN. Only that shape warns (and escalates); the occasional
        # URN-less card is DEBUG so it never files an issue for working behaviour.
        urn_examined = examined_key_sources.get("permalink", 0) + examined_key_sources.get("card", 0)
        if urn_examined:
            log_debug(f"{hash_commented} of {posted} feed comments fell back to the content-hash "
                      f"key; the URN resolver still read {urn_examined} of the posts examined",
                      user_id=user_id, action_type="comment", task_name="comment_on_feed_inline")
        else:
            log_warning("No activity URN on ANY feed post examined this scan — every comment "
                        "keyed on the unstable content hash (URN resolver drift, #580)",
                        user_id=user_id, action_type="comment", task_name="comment_on_feed_inline")
    return posted


# JS: count the overflow ("…") buttons for OUR OWN comments on the current post. LinkedIn labels each
# comment's overflow control "View more options for <name>'s comment.", so matching our full name
# restricts this to comments WE authored — we never touch anyone else's comment.
_COUNT_OWN_COMMENT_MENUS_JS = (
    "const name=(arguments[0]||'').toLowerCase();"
    "return [...document.querySelectorAll('button[aria-label]')].filter(x=>{"
    "const a=(x.getAttribute('aria-label')||'').toLowerCase();"
    "return a.includes('options for') && a.includes('comment') && name && a.includes(name);"
    "}).length;")

# JS: open the overflow menu of the LAST of our own comments (we keep the first/earliest and delete
# the rest). The control is hover-hidden, so we JS-click it directly.
_OPEN_LAST_OWN_COMMENT_MENU_JS = (
    "const name=(arguments[0]||'').toLowerCase();"
    "const b=[...document.querySelectorAll('button[aria-label]')].filter(x=>{"
    "const a=(x.getAttribute('aria-label')||'').toLowerCase();"
    "return a.includes('options for') && a.includes('comment') && name && a.includes(name);"
    "});"
    "if(!b.length) return false;"
    "const t=b[b.length-1]; t.scrollIntoView({block:'center'}); t.click(); return true;")

# JS: click the "Delete" item in the open overflow menu.
_CLICK_DELETE_MENUITEM_JS = (
    "const el=[...document.querySelectorAll('[role=menuitem],[role=menuitemradio],button,div,span,li,h5')]"
    ".find(e=>/^delete\\b/i.test((e.innerText||'').trim()) && (e.innerText||'').trim().length<20);"
    "if(el){(el.closest('[role=menuitem]')||el).click(); return true;} return false;")

# JS: confirm deletion in the modal that follows (its confirm button reads exactly "Delete").
_CONFIRM_DELETE_MODAL_JS = (
    "const d=[...document.querySelectorAll('[role=dialog],.artdeco-modal')];"
    "for(const m of d){const btn=[...m.querySelectorAll('button')]"
    ".find(b=>((b.innerText||'').trim().toLowerCase()==='delete'));"
    "if(btn){btn.click(); return true;}} return false;")


def _delete_extra_own_comments_on_post(driver, my_full_name: str, dry_run: bool = True) -> Tuple[int, int]:
    """On the currently-open post page, keep our earliest comment and delete the rest.

    The post ends with exactly ONE comment from us. Returns (own_comments_found, deleted). In
    dry_run mode it only counts what WOULD be deleted (deleted stays 0). Only comments authored by
    `my_full_name` are ever touched — replies/comments by others are never affected.
    """
    try:
        found = int(driver.execute_script(_COUNT_OWN_COMMENT_MENUS_JS, my_full_name) or 0)
    except Exception as e:
        log_warning("Could not count own comments on post", exc=e, action_type="comment")
        return 0, 0
    if found <= 1:
        return found, 0
    if dry_run:
        return found, 0

    deleted = 0
    # Delete the last-of-ours repeatedly, re-counting each pass (the DOM re-renders after a delete).
    # Cap the loop at the initial surplus so a stuck menu can't spin forever.
    for _ in range(found - 1):
        try:
            if not driver.execute_script(_OPEN_LAST_OWN_COMMENT_MENU_JS, my_full_name):
                break
            time.sleep(random.uniform(1, 2))
            if not driver.execute_script(_CLICK_DELETE_MENUITEM_JS):
                log_warning("Delete menu item not found; stopping this post", action_type="comment")
                break
            time.sleep(random.uniform(1, 2))
            if not driver.execute_script(_CONFIRM_DELETE_MODAL_JS):
                log_warning("Delete confirm button not found; stopping this post", action_type="comment")
                break
            wait_for_ajax(driver)
            time.sleep(random.uniform(2, 3))
            deleted += 1
            remaining = int(driver.execute_script(_COUNT_OWN_COMMENT_MENUS_JS, my_full_name) or 0)
            if remaining <= 1:
                break
        except Exception as e:
            log_warning("Error deleting a duplicate comment", exc=e, action_type="comment")
            break
    return found, deleted


@shared_task.task(name='cqc_lem.app.run_automation.consolidate_duplicate_comments_for_user',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def consolidate_duplicate_comments_for_user(self, user_id: int, dry_run: bool = True, hours: int = 168):
    """One-off cleanup for posts the user commented on more than once.

    For each post commented on MORE THAN ONCE in the last `hours`, delete the extra comments so exactly
    ONE remains. dry_run=True (default) only REPORTS what it would delete — pass dry_run=False to
    actually delete. Only real post URLs are actionable; feed comments logged under a synthetic key
    (no navigable URL) are reported as skipped.
    """
    dupes = get_duplicate_comment_posts(user_id, hours)
    if not dupes:
        return "No duplicate-commented posts found"

    actionable = [row for row in dupes if str(row[0]).startswith("http")]
    skipped = len(dupes) - len(actionable)
    if not actionable:
        return (f"Found {len(dupes)} duplicate-commented post(s) but none have a navigable URL "
                f"(synthetic feed keys) — cannot auto-delete; inspect manually.")

    try:
        driver, wait, user_email, my_profile = get_current_profile(
            user_id=user_id, session_name="Consolidate Comments")
    except Exception as e:
        log_error("Login failed for comment consolidation", exc=e, user_id=user_id,
                  task_name="consolidate_duplicate_comments_for_user")
        return f"Failed to start: {e}"

    posts_processed, total_found, total_deleted = 0, 0, 0
    try:
        for post_url, logged_count, first_at, last_at in actionable:
            try:
                driver.get(post_url)
                time.sleep(random.uniform(4, 7))
                found, deleted = _delete_extra_own_comments_on_post(driver, my_profile.full_name, dry_run)
                posts_processed += 1
                total_found += found
                total_deleted += deleted
                log_info(f"Consolidated comments on post (found={found}, deleted={deleted}, "
                         f"dry_run={dry_run})", user_id=user_id, post_id=post_url,
                         action_type="comment", task_name="consolidate_duplicate_comments_for_user")
            except Exception as e:
                log_warning("Failed to consolidate a post's comments", exc=e, user_id=user_id,
                            post_id=post_url, action_type="comment")
    finally:
        quit_gracefully(driver)

    verb = "would delete" if dry_run else "deleted"
    return (f"Processed {posts_processed} post(s); {verb} {total_deleted} extra comment(s) "
            f"(own comments found across posts={total_found}; skipped {skipped} non-URL post(s); "
            f"dry_run={dry_run})")


@shared_task.task(name='cqc_lem.app.run_automation.auto_seed_comment_on_post',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True,
                                                   'keys': ['user_id', 'post_id']},
                  queue='se_content')
def auto_seed_comment_on_post(self, user_id: int, post_id: int):
    """After the user's post publishes, leave a value-adding FIRST comment on it.

    This is an open question or a behind-the-scenes insight — no links — to seed the comment thread
    that drives reach, and beat LinkedIn's suppression of link-in-first-comment by adding real value
    instead. Posts via LinkedIn's socialActions API (w_member_social — the same token that publishes
    posts), NOT Selenium: commenting on the user's OWN post needs no browser and no login, so it is
    immune to the feed-navigation 429 rate limit. Everything it needs (post body, voice synthesis,
    profile, prefs) is read from the DB. Pinning is skipped here — LinkedIn exposes no pin API and the
    seed comment's thread-starting value stands without it.

    When the publish step held an external link back (issue #392 — C3), that link is appended to the
    comment: this is the delivery half of the link-in-first-comment mechanic.
    """
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        return "No post URL yet for seed comment"
    object_urn = object_urn_from_post_url(post_url)
    if not object_urn:
        return f"Could not derive object URN from {post_url}"
    # Idempotency: a retried/re-dispatched task must not leave a SECOND comment on the same post
    # (duplicate own-comments are what consolidate_duplicate_comments_for_user exists to clean up).
    if has_user_commented_on_post_url(user_id, post_url):
        return "Seed comment already exists for this post"
    # Ground the AI in the canonical post body (posts table). Fall back to the log message only if
    # the post row is gone. Historical POST logs stored a status string, so grounding on the log
    # made seed comments read like they were about the /posts API instead of the post's subject.
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)
    if not post_message:
        return "No post content to seed a comment from"
    try:
        my_profile = load_profile_for_user(user_id)  # cached DB read — no scrape/login
        with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
            seed = generate_seed_comment(post_message, my_profile, get_engagement_preferences(user_id),
                                         profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile),
                                         user_id=user_id)
        # The generated comment never contains links (the prompt forbids them); the link held back at
        # publish time is appended deterministically here. A link on its own still ships when the
        # generator came back empty — losing the link entirely would be the worse failure.
        raw_links = (get_post_first_comment_link(post_id) or "").split("\n")
        held_links = [link for link in raw_links if link.strip()]
        seed = append_link_to_comment(seed, held_links, post_id=post_id)
        if not seed:
            return "No seed comment generated"
        comment_urn = comment_on_linkedin_post(user_id, object_urn, seed)
        if comment_urn:
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.COMMENT,
                           result=LogResultType.SUCCESS, post_url=post_url, message=seed)
            log_info(f"Seed comment posted on post {post_id} via API ({comment_urn})")
            return f"Seed comment posted via API ({comment_urn})"
        return "Seed comment failed to post"
    except Exception as e:
        log_error("Seed comment error", exc=e, user_id=user_id, post_id=post_id, task_name="auto_seed_comment_on_post")
        return f"Seed comment error: {e}"


def _second_wave_story_directive(user_id: int, post_message: str, prefs: dict) -> tuple:
    """Return the story-bank injection for the second wave and the entry it came from (issue #620).

    The added insight must be the user's OWN material, so the writer gets one relevant entry and the
    hard rule that its facts are the only personal specifics it may state. An empty or irrelevant
    bank yields the explicit no-fabrication fallback rather than an invented anecdote.
    """
    try:
        entries = get_story_bank_entries(user_id, active_only=True)
    except Exception as e:
        log_warning("Story bank unavailable for the second-wave comment", exc=e, user_id=user_id)
        entries = []
    focus = (prefs or {}).get("focus_topics")
    story = _story_bank.select_story(entries, subject=str(post_message or "")[:300],
                                     focus_topics=focus if isinstance(focus, list) else None)
    return _story_bank.story_directive(story), story


@shared_task.task(name='cqc_lem.app.run_automation.auto_second_wave_comment',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True,
                                                   'keys': ['user_id', 'post_id']})
def auto_second_wave_comment(self, user_id: int, post_id: int):
    """Add ONE self-comment 6–8 hours after publishing (the second wave, issue #622 / G7).

    The comment brings a substantive insight the post itself didn't carry — the evening re-surface of a
    post that is still earning engagement is a second distribution window, and a comment with real
    value in it is what re-opens the thread. Like the #344 seed comment it publishes through the
    socialActions API (no Selenium, no login), so it is immune to the feed-navigation 429 — but
    unlike the seed it IS discretionary amplification, so it stands down while automation is paused
    (the #629 suppression tripwire and any manual pause). The self-comment cap is enforced on the
    COUNT of our own comments on this post, so the seed and the second wave can never stack into
    thread-stuffing however they are re-dispatched. A draft that can't clear the comment quality
    contract ships NOTHING.

    The 6–8h wait is served in HOPS, not one long countdown: with `task_acks_late` the broker
    redelivers any message left unacked past `visibility_timeout` (~75 min), so a single 8-hour
    countdown would be handed to another worker every 75 minutes and post the comment several times
    over. Each run re-checks the post's real age and re-arms itself until the post is due.
    """
    if not _golden.second_wave_enabled():
        return "Second-wave comment disabled"
    due_minutes = _golden.second_wave_due_minutes(user_id, post_id)
    hop = _golden.second_wave_hop_seconds(
        due_minutes, _golden.latency_minutes(get_post_age_minutes(user_id, post_id)))
    if hop is not None:
        # Not due yet — re-arm inside the broker's visibility timeout. Checked BEFORE the pause so a
        # pause that lifts before the post is due doesn't cost it its second wave.
        auto_second_wave_comment.apply_async(kwargs={'user_id': user_id, 'post_id': post_id},
                                             countdown=hop)
        return f"Second wave not due yet ({due_minutes:.0f} min after publish) — re-armed in {hop}s"
    if is_automation_paused():
        reason = automation_pause_reason() or "automation paused"
        log_info(f"Second-wave comment skipped — {reason}", user_id=user_id, post_id=post_id,
                 action_type="comment", task_name="auto_second_wave_comment")
        return f"Skipped — {reason}"
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        return "No post URL yet for the second-wave comment"
    object_urn = object_urn_from_post_url(post_url)
    if not object_urn:
        return f"Could not derive object URN from {post_url}"
    cap = _golden.self_comment_cap()
    already = count_user_comments_on_post_url(user_id, post_url)
    if already >= cap:
        return f"Self-comment cap reached ({already}/{cap}) for this post"
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)
    if not post_message:
        return "No post content to build a second-wave comment from"
    try:
        my_profile = load_profile_for_user(user_id)  # cached DB read — no scrape/login
        prefs = get_engagement_preferences(user_id)
        story_directive, story = _second_wave_story_directive(user_id, post_message, prefs)
        with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
            comment = generate_second_wave_comment(
                post_message, my_profile, prefs=prefs,
                profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile),
                story_directive=story_directive,
                recent_comments=list(get_recent_comment_texts(user_id)), user_id=user_id)
        if not comment:
            # The gate rejected every attempt. Nothing ships — a filler self-comment on our own post
            # costs more reach than the silence does.
            log_warning("Second-wave comment skipped — no draft cleared the quality contract",
                        user_id=user_id, post_id=post_id, action_type="comment",
                        task_name="auto_second_wave_comment")
            _record_golden_hour_report(user_id, post_id, 0,
                                       _reply_outcome("gate_failed", "no draft passed the gate"),
                                       phase=_golden.PHASE_SECOND_WAVE)
            return "No second-wave comment passed the quality gate"
        comment_urn = comment_on_linkedin_post(user_id, object_urn, comment)
        if not comment_urn:
            _record_golden_hour_report(user_id, post_id, 0,
                                       _reply_outcome("post_failed", "API rejected the comment"),
                                       phase=_golden.PHASE_SECOND_WAVE)
            return "Second-wave comment failed to post"
        insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.COMMENT,
                       result=LogResultType.SUCCESS, post_url=post_url, message=comment)
        # Recorded for visibility, NOT charged to the outbound envelope: like a reply on our own
        # post, this is presence on our own thread rather than discretionary outreach (#626).
        record_action(user_id, ACTION_REPLY)
        if story and story.get("id"):
            record_story_bank_use(user_id, story["id"])
        _record_golden_hour_report(user_id, post_id, 0,
                                   _reply_outcome("ok", "second wave posted", replies_sent=1),
                                   phase=_golden.PHASE_SECOND_WAVE)
        log_info(f"Second-wave comment posted on post {post_id} via API ({comment_urn})",
                 user_id=user_id, post_id=post_id, action_type="comment",
                 task_name="auto_second_wave_comment")
        return f"Second-wave comment posted via API ({comment_urn})"
    except Exception as e:
        log_error("Second-wave comment error", exc=e, user_id=user_id, post_id=post_id,
                  task_name="auto_second_wave_comment")
        return f"Second-wave comment error: {e}"


# ── the groups directory (issues #1052, #1316) ───────────────────────────────────────────────
# `/groups/` renders the user's OWN groups and a "Groups you might be interested in" rail on one
# page, and every anchor in both carries the same `/groups/<id>` href. Taking all of them as joins is
# how the sync invented memberships the user never had — the 2026-08-14 live directory offered 5 of
# them, and four were already sitting `enabled` in the DB, so group commenting was walking into
# groups the account does not belong to. So the walk reads each anchor's SECTION and keeps only what
# the page did not file under a recommendation heading.
#
# The section is the nearest PRECEDING heading in document order, not an ancestor of the anchor:
# that live page's own headings ("Groups listing", "Groups you might be interested in") are neither
# h1–h3 nor ancestors of the cards beneath them, so an ancestor walk attributes nothing. Live shape
# on 2026-08-14: 55 anchors, 50 under "Groups listing", 5 under the recommendation heading.
_GROUP_RECOMMENDATION_HEADINGS = ("might be interested", "may like", "you might like", "recommend",
                                  "suggested", "discover")

# A row a heading could not be attributed to is KEPT. An unreadable section is not evidence that a
# membership is a recommendation, and dropping on absence would empty the sync the first time
# LinkedIn re-words a heading — the same failure this walk exists to end, pointed the other way.
_GROUP_DIRECTORY_JS = r"""
const out = [];
const seen = new Set();
const headings = [];
for (const h of document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')) {
  const t = (h.innerText || '').trim();
  if (t) headings.push([h, t.slice(0, 80)]);
}
for (const a of document.querySelectorAll("a[href*='/groups/']")) {
  const m = (a.getAttribute('href') || '').match(/\/groups\/(\d+)/);
  if (!m) continue;
  const id = m[1];
  if (seen.has(id)) continue;
  const name = (a.innerText || '').trim().split('\n')[0];
  if (!name || name.length < 2) continue;
  let section = '';
  for (const [node, text] of headings) {
    if (node.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING) section = text;
    else break;
  }
  seen.add(id);
  out.push([id, name.slice(0, 255), section]);
  if (out.length >= 60) break;
}
return out;
"""

# The cap the walk above stops at, named so the reconcile can tell a walk that ran out of anchors
# from one whose heading attribution dropped joined rows. `test_the_anchor_cap_constant_matches_the_
# walk` fails if the two drift.
_GROUP_DIRECTORY_ANCHOR_CAP = 60

# The zero-walk cross-check for this walk (#1013): a per-row overflow control, one per JOINED group
# and none on a recommendation card (50 for 50 rows on 2026-08-14). It reads a control LABEL, so it
# shares nothing with the walk's chain — not the href, not the id shape, not the heading attribution
# — which is what makes it able to answer "the page still renders your groups" when the walk says
# zero. That includes the new failure mode this filter introduces: a re-worded joined-list heading
# that matches a recommendation marker would drop every row, and this is what says so.
_GROUPS_DIRECTORY_CROSSCHECK_SEL = "button[aria-label^='More options for ']"


def _is_group_recommendation_section(section: str) -> bool:
    """Whether a directory heading marks its anchors as OFFERS rather than memberships."""
    text = str(section or "").lower()
    return any(marker in text for marker in _GROUP_RECOMMENDATION_HEADINGS)


class GroupsDirectoryReading(NamedTuple):
    """What ONE walk of `/groups/` established, split by what the page's own headings said.

    `recommended` is POSITIVE evidence and the only "this is not a membership" answer the directory
    itself can give: the page filed that anchor under a recommendation heading. An id in NEITHER list
    was not answered either way — it may be a membership the walk's scroll or its 60-anchor cap never
    reached — which is why `_reconcile_stored_groups` never disables on absence alone.
    """

    joined: list
    recommended: list


def _read_groups_directory(driver, user_id: Optional[int] = None) -> GroupsDirectoryReading:
    """Walk /groups/ once and report both populations the page renders.

    Best-effort: a zero read is cross-checked against the page's own group rows
    (`_GROUPS_DIRECTORY_CROSSCHECK_SEL`) rather than being taken for "this user is in no groups".
    """
    driver.get("https://www.linkedin.com/groups/")
    time.sleep(random.uniform(5, 8))
    for y in (600, 1200, 1800):
        driver.execute_script(f"window.scrollTo(0,{y});")
        time.sleep(1.5)
    rows = [r for r in (driver.execute_script(_GROUP_DIRECTORY_JS) or []) if r and r[0]]
    joined, recommended = [], []
    for row in rows:
        if _is_group_recommendation_section(row[2] if len(row) > 2 else ""):
            recommended.append(str(row[0]))
        else:
            joined.append((str(row[0]), row[1]))
    if not joined:
        _report_zero_walk(driver, _GROUPS_DIRECTORY_CROSSCHECK_SEL, "Joined-groups directory walk",
                          user_id=user_id, task_name="auto_sync_user_groups")
    return GroupsDirectoryReading(joined=joined, recommended=recommended)


def _enumerate_joined_groups(driver, user_id: Optional[int] = None) -> list:
    """Scrape the user's joined groups from /groups/ → list of (group_id, name).

    Recommendation cards are excluded by the heading they sit under, so a group the user was merely
    offered never becomes a stored membership. Kept as its own name because it is the ONE thing the
    read-only live probe drives (`scripts/linkedin_live_validation.py --group-membership`), which
    grades the walk against the page's own anchors and needs the shipped enumeration verbatim.
    """
    return _read_groups_directory(driver, user_id=user_id).joined


# ── reconcile: the rows the OLD sync already wrote (issue #1487) ──────────────────────────────
# #1316 stopped the walk INVENTING memberships, but that fix is forward-only: the rows it used to
# write are still sitting `enabled=1`, so `auto_comment_in_groups` keeps walking into groups the
# account never joined. Reconciling is a write that switches engagement OFF, so every step of it
# fails CLOSED — the cost of being wrong is a group the user is in going quiet, and the only thing
# that reverses it is the user noticing.
#
# Two populations, two standards of proof, because they are not the same claim:
#   * the directory filed the id under a recommendation heading — the page SAID it is an offer;
#   * the id was simply not enumerated — which can equally be lazy-load or the walk's 60-anchor cap
#     (user 1 already renders 55), so absence is asked about again on the group's OWN page and only
#     a Join control there disables it.
_GROUP_JOIN_MARKERS = ("join", "request to join")
_GROUP_PENDING_MARKERS = ("requested", "pending", "withdraw")
_GROUP_LEAVE_MARKERS = ("leave",)

MEMBERSHIP_MEMBER = "member"
MEMBERSHIP_NOT_MEMBER = "not_member"
MEMBERSHIP_PENDING = "pending"
MEMBERSHIP_UNKNOWN = "unknown"

# How many stored-but-unseen groups one run will open a page for. A confirmation is a page load
# apiece inside a task that already has a Chrome session and a soft time limit; whatever the cap
# leaves over is logged rather than dropped silently, and the next weekly run reaches it.
GROUP_RECONCILE_MAX_CONFIRMATIONS = 10

# Controls scoped to the group's OWN header. The scoping is the point: the group page renders a
# "Join" per card in its groups-you-may-like rail, so keying membership off any Join on the page is
# the #1012 hazard one layer down — in the reading rather than in the click.
_GROUP_HEADER_CONTROLS_JS = """
const out = [];
const seen = new Set();
const h1 = document.querySelector('main h1') || document.querySelector('h1');
if (!h1) return out;
let node = h1;
for (let i = 0; i < 6 && node.parentElement; i++) {
  node = node.parentElement;
  if (node.querySelectorAll('button').length) break;
}
for (const b of node.querySelectorAll('button')) {
  const l = ((b.getAttribute('aria-label') || b.innerText || '').trim()).slice(0, 80);
  if (!l || seen.has(l)) continue;
  seen.add(l);
  out.push(l);
  if (out.length >= 20) break;
}
return out;
"""


def _control_leads_with(labels, markers: tuple) -> bool:
    """Whether any control label LEADS with one of these action words, rather than merely containing one.

    A LinkedIn control label often carries the group's own NAME, so a substring match makes the
    membership answer depend on what the group is called: a group named "Join the Data Guild" would
    read `not_member` with its share box sitting right there, and this is the one direction that must
    never be wrong. Every real membership control leads with its verb.
    """
    for label in labels or []:
        stripped = str(label or "").strip().lower()
        if any(stripped.startswith(marker) for marker in markers):
            return True
    return False


def _group_membership_answer(header_controls, share_box_present: bool) -> str:
    """What a group page says about our membership — three-valued, and `unknown` is never actioned.

    Read as presence-of-share-box + absence-of-Join, never a Leave button: the 2026-08-14 live header
    carried no membership control at all and `member` came from the share box alone (#1316), so a fix
    waiting for Leave would answer `unknown` for every group the user IS in.
    """
    if _control_leads_with(header_controls, _GROUP_PENDING_MARKERS):
        return MEMBERSHIP_PENDING
    if _control_leads_with(header_controls, _GROUP_LEAVE_MARKERS):
        return MEMBERSHIP_MEMBER
    if _control_leads_with(header_controls, _GROUP_JOIN_MARKERS):
        # A share box is itself the membership signal here, so the two together are a CONTRADICTION,
        # not a Join that outranks it: the reading that produces it is the header scope having
        # reached far enough to pick up a Join from the page's own groups-you-may-like rail. Nothing
        # else disables a group on one page load, and this is the only direction that costs a real
        # membership its engagement, so a contradiction answers `unknown` and is asked again.
        return MEMBERSHIP_UNKNOWN if share_box_present else MEMBERSHIP_NOT_MEMBER
    return MEMBERSHIP_MEMBER if share_box_present else MEMBERSHIP_UNKNOWN


def _confirm_group_membership(driver, wait, group_id: str, user_id: Optional[int] = None) -> str:
    """Open ONE group's page and ask it whether we belong. Read-only — nothing is clicked.

    Any failure answers `unknown`, which the caller leaves alone: a page that would not render is not
    evidence that the user left.
    """
    try:
        driver.get(f"https://www.linkedin.com/groups/{group_id}/")
        time.sleep(random.uniform(4, 6))
        controls = driver.execute_script(_GROUP_HEADER_CONTROLS_JS) or []
        share_box = find_first(driver, wait, _GROUP_SHARE_BOX_LOCATORS, "Group share box",
                               required=False, warn_on_miss=False, max_try=1, visible_only=True)
    except WebDriverException as e:
        # An unreadable page changes nothing, so it is an expected no-op rather than a defect.
        log_debug(f"Group membership check could not read the page: {e}", user_id=user_id,
                  group_id=str(group_id), task_name="auto_sync_user_groups")
        return MEMBERSHIP_UNKNOWN
    return _group_membership_answer(controls, share_box is not None)


def _heading_attribution_dropped_rows(reading: GroupsDirectoryReading, native: int) -> bool:
    """Whether the walk kept FEWER joined rows than the page renders joined-row controls.

    `_GROUPS_DIRECTORY_CROSSCHECK_SEL` is one control per JOINED group and none on a recommendation
    card, so walking fewer than it counts means the heading attribution filed joined rows as offers.
    The anchor cap is the one benign explanation, so a walk that hit it answers False.
    """
    if len(reading.joined) + len(reading.recommended) >= _GROUP_DIRECTORY_ANCHOR_CAP:
        return False
    return len(reading.joined) < native


def _confirmation_slice(absent: list) -> Tuple[list, list]:
    """Which stored-but-unseen groups this run pays a page load for, and which it leaves.

    SAMPLED, not sliced off the front: `get_enabled_group_ids` answers in a deterministic order
    (least-recently-commented-in first, issue #1719), so a fixed head would re-ask the same ids
    every week and a tail behind more than `GROUP_RECONCILE_MAX_CONFIRMATIONS` real memberships
    would never be reached at all — which is not what "the next weekly run reaches it" means.
    """
    if len(absent) <= GROUP_RECONCILE_MAX_CONFIRMATIONS:
        return list(absent), []
    checked = random.sample(list(absent), GROUP_RECONCILE_MAX_CONFIRMATIONS)
    taken = set(checked)
    return checked, [gid for gid in absent if gid not in taken]


def _reconcile_stored_groups(driver, wait, user_id: int,
                             reading: GroupsDirectoryReading) -> list:
    """Switch engagement off for stored groups THIS walk proved are not memberships (#1487).

    Returns:
        The group ids it disabled — empty whenever the walk was not good enough to ground a disable.
    """
    if not reading.joined:
        # A walk that enumerated nothing already reported itself through `_report_zero_walk`; it can
        # never be the evidence for switching a group off.
        log_debug("Groups reconcile skipped: the directory walk enumerated no membership",
                  user_id=user_id, task_name="auto_sync_user_groups")
        return []
    native = _zw.page_native_count(driver, _GROUPS_DIRECTORY_CROSSCHECK_SEL)
    if native is None:
        log_debug("Groups reconcile skipped: the directory cross-check could not be read",
                  user_id=user_id, task_name="auto_sync_user_groups")
        return []
    if native == 0:
        # The walk found rows the tripwire cannot see, so the tripwire is blind — the #1316
        # `crosscheck_blind` finding, and a defect in its own right.
        log_warning(f"Groups directory cross-check `{_GROUPS_DIRECTORY_CROSSCHECK_SEL}` matched "
                    f"nothing on a walk that enumerated {len(reading.joined)} group(s) — selector "
                    f"drift, and no group was reconciled", user_id=user_id,
                    task_name="auto_sync_user_groups")
        return []
    stored = [str(g) for g in (get_enabled_group_ids(user_id) or [])]
    if not stored:
        return []
    live = {gid for gid, _ in reading.joined}
    recommended = {str(g) for g in reading.recommended} - live
    if recommended and _heading_attribution_dropped_rows(reading, native):
        # The cross-check counts JOINED rows (one overflow control apiece, none on a recommendation
        # card), so it answers something `native > 0` alone does not: whether the heading attribution
        # moved joined rows into `recommended`. A re-worded joined heading that happens to match a
        # recommendation marker does exactly that, and those ids would then be disabled on the
        # heading alone. Demoting them to `absent` does not keep them forever — it just makes the
        # group's OWN page the evidence, which is what every unattributed row already gets.
        log_warning(f"Groups directory heading attribution kept {len(reading.joined)} joined row(s) "
                    f"against {native} the cross-check counted — no group was disabled on a "
                    f"recommendation heading this run", user_id=user_id,
                    task_name="auto_sync_user_groups")
        recommended = set()
    offered = [gid for gid in stored if gid in recommended]
    absent = [gid for gid in stored if gid not in live and gid not in recommended]

    disabled = []
    if offered and disable_user_groups(user_id, offered, reason="recommendation_rail"):
        disabled.extend(offered)
    checked, skipped = _confirmation_slice(absent)
    if skipped:
        log_debug(f"{len(skipped)} stored group(s) were left unconfirmed by this run's cap "
                  f"({','.join(skipped)})", user_id=user_id, task_name="auto_sync_user_groups")
    left = [gid for gid in checked
            if _confirm_group_membership(driver, wait, gid, user_id=user_id) == MEMBERSHIP_NOT_MEMBER]
    if left and disable_user_groups(user_id, left, reason="join_control_on_group_page"):
        disabled.extend(left)
    return disabled


@shared_task.task(name='cqc_lem.app.run_automation.auto_sync_user_groups',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def auto_sync_user_groups(self, user_id: int):
    """Refresh the user's joined-groups list, then reconcile what is already stored (#1487).

    New groups default to enabled; a stored group this walk PROVED is not a membership is switched
    off (never deleted).
    """
    try:
        # needs_images=True (issue #1778): /groups/ is fastboot the same way /messaging/* is
        # (#1774) — its `<img>` load events drive the client boot, so a bandwidth-saver session
        # with images blocked never mounts <main> at all, live-confirmed on 2026-08-31.
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Sync Groups",
                                                                    needs_images=True)
    except Exception as e:
        log_error("Error getting profile for group sync", exc=e, user_id=user_id, task_name="auto_sync_user_groups")
        return f"Failed: {e}"
    try:
        reading = _read_groups_directory(driver, user_id=user_id)
        for gid, name in reading.joined:
            upsert_user_group(user_id, gid, name)
        # Reconciling runs while the session is still ON /groups/ — the cross-check it fails closed
        # against is a read of THAT page.
        disabled = _reconcile_stored_groups(driver, wait, user_id, reading)
        synced = f"Synced {len(reading.joined)} group(s)"
        return f"{synced}, disabled {len(disabled)}" if disabled else synced
    finally:
        quit_gracefully(driver)


# A group walk is bounded by the SAME clock as every other task — celeryconfig's soft time limit —
# and it is the walk most likely to reach it: one Chrome session, N group feeds, each an LLM-priced
# scored comment pass. Reaching the limit is the worst way to stop, because it lands mid-comment and
# nothing downstream of it runs. So the walk carries its OWN deadline, derived from that limit and
# checked between groups AND passed INTO the feed engine — the between-groups check alone can only
# fire once a group RETURNS, so a group whose feed stalls would still be cut down mid-comment by
# the soft limit. What this bounds is the walk's TOTAL time, not any one group's share of it: a
# slow first group can still leave the later groups nothing, exactly as it did before. The reserve
# is what the in-flight group and `quit_gracefully` get to finish in.
GROUP_WALK_RESERVE_SECONDS = 10 * 60
# A limit configured tighter than the reserve must still leave a walk something to spend, or the
# deadline check would refuse the first group and the task would never comment at all.
GROUP_WALK_MIN_BUDGET_SECONDS = 5 * 60


def _group_walk_deadline(task: Task, started_ts: float) -> Optional[float]:
    """Wall-clock instant a group walk must stop by to beat its Celery soft time limit.

    Reads the limit off the task's own request first (a caller may override it per dispatch) and
    falls back to the app config; celery orders that tuple `(hard, soft)`.

    Returns:
        None when nothing bounds the task at all — the walk then runs unbounded, as it always did.
    """
    limits = getattr(task.request, "timelimit", None) or (None, None)
    soft = limits[1] if len(limits) == 2 else None
    if soft is None:
        soft = shared_task.conf.task_soft_time_limit
    if not soft:
        return None
    return started_ts + max(float(soft) - GROUP_WALK_RESERVE_SECONDS, GROUP_WALK_MIN_BUDGET_SECONDS)


@shared_task.task(name='cqc_lem.app.run_automation.auto_comment_in_groups',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def auto_comment_in_groups(self, user_id: int, max_per_group: int = 2):
    """Comment (value-add, scored) on posts in each of the user's enabled groups.

    Reuses the feed commenting engine pointed at each group's feed. Shares the per-day comment cap.
    Bounded by `_group_walk_deadline`: a run that is out of time stops between groups and keeps what
    it already posted, rather than being cut down mid-comment by the soft time limit. `get_enabled_
    group_ids` orders least-recently-walked first (issue #1719), so an out-of-time run does not
    starve the SAME tail groups forever — each reached group is stamped via `record_group_comment_
    run`, moving it to the back of the line for next time.
    """
    started_ts = time.time()
    enabled = get_enabled_group_ids(user_id)
    if not enabled:
        return "No enabled groups"
    deadline_ts = _group_walk_deadline(self, started_ts)
    try:
        # needs_images=True (issue #1778): same fastboot dependency as #1774's messaging fix —
        # /groups/<id>/ never mounts <main> with images blocked.
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id,
                                                                    session_name="Group Commenting",
                                                                    needs_images=True)
    except Exception as e:
        log_error("Error getting profile for group commenting", exc=e, user_id=user_id,
                  task_name="auto_comment_in_groups")
        return f"Failed: {e}"
    prefs = get_engagement_preferences(user_id)
    engagers = get_recent_engagers(user_id)
    total = 0
    walked = 0
    try:
        for gid in enabled:
            if deadline_ts is not None and time.time() >= deadline_ts:
                # WARNING, not DEBUG: the groups after this one got nothing this run. Once is the
                # walk being longer than its budget; repeatedly is a defect (too many groups, or a
                # group feed that stalls), and the escalation contract is what says so.
                log_warning(f"Group commenting ran out of time after {walked} of {len(enabled)} "
                            f"group(s) — remaining groups skipped this run", user_id=user_id,
                            action_type="comment", task_name="auto_comment_in_groups")
                return f"Commented {total} time(s) across {walked} group(s) before running out of time"
            try:
                driver.get(f"https://www.linkedin.com/groups/{gid}/")
                time.sleep(random.uniform(4, 7))
                if driver.find_elements(By.CSS_SELECTOR, _PAGE_SHELL_CROSSCHECK_SEL):
                    total += comment_on_feed_inline(driver, wait, my_profile, user_id,
                                                    max_posts=max_per_group, deadline_ts=deadline_ts,
                                                    prefs=prefs, engagers=engagers, is_group_feed=True)
                else:
                    # The page never rendered a `<main>` — a login wall, an interstitial, or a
                    # block on this one group — so there is no feed to have found zero posts in.
                    # `comment_on_feed_inline`'s own zero-walk cross-check cannot catch this: it
                    # reads a FEED anchor, which also answers zero on a page that never rendered
                    # (#1777). Skip WITHOUT calling it, so this never counts as "an empty feed" —
                    # once is a stalled load, repeatedly is the drift the escalation contract exists
                    # to surface.
                    log_warning(f"Group {gid} page did not render — skipping without treating it "
                                f"as an empty feed", user_id=user_id, action_type="comment",
                                task_name="auto_comment_in_groups")
                walked += 1
                # Stamped whether or not this group produced a comment (issue #1719) — an empty
                # feed still moves to the back of the rotation, or it would starve the walk the same
                # way an unpostable group did before #858. A group the deadline check above skips is
                # left untouched, so it is next in line rather than skipped again next run.
                record_group_comment_run(user_id, gid)
            except Exception as e:
                if is_session_lost(e):
                    # A walk across several group feeds is one of the longest browser sessions LEM
                    # holds, so it is the one a release lands on: the deploy drains for 8 minutes and
                    # then recreates the containers anyway, quitting this session out from under us
                    # (issue #988). The run is genuinely over — no later group can be reached on a
                    # dead session — but a routine release is not a defect, so end on what already
                    # shipped at INFO instead of crashing the task into a grouped $exception.
                    log_info("Browser session ended mid-run (worker or Grid restart) — stopping "
                             "group commenting", user_id=user_id, task_name="auto_comment_in_groups")
                    return f"Commented {total} time(s) before the browser session ended"
                if is_tab_crashed(e):
                    # The renderer behind the tab died (usually an OOM kill after many group
                    # navigations in one session) — the session is still valid, but no further
                    # navigation on it will succeed either, so the walk is over the same way a lost
                    # session ends it (issue #1746). Unlike a deploy this IS an anomaly worth
                    # surfacing, so it stays a warning (escalates if it starts recurring) rather than
                    # crashing the task into an unhandled $exception for a fault the walk cannot
                    # recover from mid-run.
                    log_warning(f"Browser tab crashed after {walked} of {len(enabled)} group(s) — "
                                f"stopping group commenting", exc=e, user_id=user_id,
                                action_type="comment", task_name="auto_comment_in_groups")
                    return f"Commented {total} time(s) before the browser tab crashed"
                if is_grid_relay_error(e):
                    # The Grid hub's relay to the node dropped ONE command (issue #1784) — a
                    # connectivity blip, not an application defect. The session is very likely
                    # unusable for the rest of the walk either way, so this is handled the same as
                    # a crashed tab: stop and keep what already shipped, WARNING (not INFO) because
                    # a live Grid relay fault, unlike a routine deploy, is worth surfacing if it
                    # recurs.
                    log_warning(f"Grid relay error after {walked} of {len(enabled)} group(s) — "
                                f"stopping group commenting", exc=e, user_id=user_id,
                                action_type="comment", task_name="auto_comment_in_groups")
                    return f"Commented {total} time(s) before the Grid relay failed"
                raise
        return f"Commented {total} time(s) across {len(enabled)} group(s)"
    except SoftTimeLimitExceeded:
        # The deadline above is the intended stop; this is the backstop for a run whose reserve was
        # not enough (issue #1198). It arrives here rather than in the loop's own handler because
        # `is_session_lost` is False for it, so that handler re-raises. Keeping the comments that
        # already landed and letting `finally` quit Chrome is the whole point of the SOFT limit —
        # crashing the task instead only files a grouped $exception for a run we deliberately capped.
        log_warning(f"Group commenting hit the Celery soft time limit after {walked} of "
                    f"{len(enabled)} group(s)", user_id=user_id, action_type="comment",
                    task_name="auto_comment_in_groups")
        return f"Commented {total} time(s) before the task time limit"
    finally:
        quit_gracefully(driver)


@shared_task.task(name='cqc_lem.app.run_automation.auto_draft_group_post',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']})
def auto_draft_group_post(self, user_id: int, group_id: str, group_name: str = None):
    """Write the coming week's group post ahead of the publish slot and park it for review (issue #932).

    This is the ONE place a group post's text is written — the publish run consumes the draft and
    generates nothing — so no group post can ever ship without having been previewable first. Opens no
    browser: the voice comes from the CACHED profile, so a draft costs one LLM call and no Chrome
    session slot.
    """
    if get_open_group_post_draft(user_id):
        log_debug("Group post draft already waiting for review", user_id=user_id,
                  task_name="auto_draft_group_post")
        return "A group post draft is already awaiting review"
    my_profile = load_profile_for_user(user_id)  # cached DB read — no scrape/login
    if my_profile is None:
        # Nothing to write in the user's voice yet (the weekly group sync ran before their first
        # profile scrape). Expected on a brand-new account, so DEBUG, not a warning.
        log_debug("No cached profile to draft a group post from", user_id=user_id,
                  task_name="auto_draft_group_post")
        return "No cached profile to draft from"
    with llm_attribution(user_id=user_id, feature=FEATURE_CONTENT):
        # The user owns ready/skipped on this draft, never its text, so the draft is what publishes
        # — normalize a stray non-USD symbol here, the way every other generated post does
        # (issue #1529). `strip_non_bmp` is the shared Selenium-typing helper and stays currency-
        # blind: it also runs over comments and invite notes written about someone ELSE's content.
        text = strip_non_bmp(normalize_currency_symbols(generate_group_post(
            my_profile, group_name=group_name, prefs=get_engagement_preferences(user_id),
            profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile)) or ""))
    if not text.strip():
        return "No group post generated"
    draft_id = create_group_post_draft(user_id, group_id, text, group_name=group_name)
    if not draft_id:
        return "Could not store the group post draft"
    log_info("Group post drafted for review", user_id=user_id, task_name="auto_draft_group_post")
    return f"Drafted group post {draft_id}"


# The group composer's three steps, as module constants so the live probe drives the SHIPPED chain
# instead of a copy of it (issue #1013): `--group-composer` imports these. An inlined duplicate
# grounds whatever the probe's author last pasted — that is how the reaction probe reported
# cards_found: 0 against a build whose walk was already fixed.
#
# The share-box trigger itself now lives in `utilities/linkedin/share_composer.py` (#1088): the
# group composer and the native occasion composer open the SAME control, and a second copy of that
# chain is exactly the drift this comment warns about. It is imported back under the name this
# module's body already used, so one spelling still greps to one place — and `--group-composer` /
# `--composer` keep importing it from here.
_GROUP_SHARE_BOX_LOCATORS = SHARE_BOX_LOCATORS
_GROUP_SHARE_BOX_TEXT_SIGNALS = SHARE_BOX_TEXT_SIGNALS
# The editor and the commit control are looked up INSIDE the resolved composer container now, not on
# the page (#1621): LinkedIn's redesigned share box mounts in `#interop-outlet`'s shadow root, where
# a page-level `div[role='textbox']` lookup — and any XPath at all — can never reach them. These two
# spellings are what the container-scoped lookup asks for; the container itself comes from
# `share_composer.find_composer_container`.
_GROUP_EDITOR_CSS = COMPOSER_EDITOR_CSS
_GROUP_POST_BUTTON_LABELS = POST_BUTTON_LABELS

# Media on a group post (issue #1224). The file input is tried FIRST and the trigger only when it
# is absent: LinkedIn renders the hidden `<input type=file>` up front in most variants, and clicking
# the styled affordance when we did not have to is what opens an overlay we then have to get back
# out of. The input is never visible, so every lookup here is `visible_only=False`.
#
# The COMPOSER's own input comes first (#1012's rule, applied to an upload): a LinkedIn page carries
# other `<input type=file>` controls — the messaging overlay's attachment input declares an image
# `accept` too — and writing the draft's file into one of those uploads the image somewhere the user
# never asked for, while this run still reports the media as attached. The page-wide chain stays as
# the last resort, the same shape `article_editor`'s cover ladder uses — but it EXCLUDES the
# messaging overlay by name (`msg-overlay-*` / `msg-form`, the containers `message_thread.py`
# already keys on), because that overlay rides every LinkedIn page: without the exclusion the "last
# resort" is not a long shot, it is the control we would deterministically land on once the
# composer's own input drifts.
_X_NOT_IN_MESSAGING = (" and not(ancestor::*[contains(@class,'msg-overlay')"
                       " or contains(@class,'msg-form')])")
_GROUP_MEDIA_INPUT_LOCATORS = [
    (By.XPATH, "//div[@role='dialog']//input[@type='file' and contains(@accept,'image')]"),
    (By.XPATH, "//div[@role='dialog']//input[@type='file' and contains(@accept,'video')]"),
    (By.XPATH, "//div[@role='dialog']//input[@type='file']"),
    (By.XPATH, f"//input[@type='file' and contains(@accept,'image'){_X_NOT_IN_MESSAGING}]"),
    (By.XPATH, f"//input[@type='file' and contains(@accept,'video'){_X_NOT_IN_MESSAGING}]"),
    (By.XPATH, f"//input[@type='file'{_X_NOT_IN_MESSAGING}]"),
]
# The trigger is the one control here we CLICK, so it carries the exclusion harder than the input
# does: LinkedIn's messaging overlay labels its own attachment control "Add a photo", and clicking
# THAT opens the message thread's file picker over the group page — #1012's rule exactly. Composer
# first, then page-wide with the messaging containers cut out; the or-chain is parenthesised because
# `and` binds tighter than `or` and an unbracketed tail would exclude messaging from the LAST label
# only.
_X_MEDIA_TRIGGER_LABELS = (f"contains({_X_LOWER_ARIA},'add media') "
                           f"or contains({_X_LOWER_ARIA},'add a photo') "
                           f"or contains({_X_LOWER_ARIA},'add photo') "
                           f"or contains({_X_LOWER_ARIA},'add a video') "
                           f"or contains({_X_LOWER_ARIA},'add video')")
_GROUP_MEDIA_TRIGGER_LOCATORS = [
    (By.XPATH, f"//div[@role='dialog']//*[self::button or @role='button'][{_X_MEDIA_TRIGGER_LABELS}]"),
    (By.XPATH, "//*[self::button or @role='button']"
               f"[({_X_MEDIA_TRIGGER_LABELS}){_X_NOT_IN_MESSAGING}]"),
]
# The media overlay's own commit control. It is NOT the share box's Post button — that one is
# clicked later, after the text goes in. Scoped to the open dialog first for the same reason the
# input is: a bare "Next" anywhere on the page belongs to some other surface.
_GROUP_MEDIA_CONFIRM_LOCATORS = [
    (By.XPATH, "//div[@role='dialog']//button[normalize-space()='Next']"),
    (By.XPATH, "//div[@role='dialog']//button[normalize-space()='Done']"),
    (By.XPATH, f"//button[normalize-space()='Next'{_X_NOT_IN_MESSAGING}]"),
    (By.XPATH, f"//button[normalize-space()='Done'{_X_NOT_IN_MESSAGING}]"),
]
# The same two controls, and the media affordance, as LABELS — what the composer-scoped lookup
# matches on when the composer is shadow-mounted and no XPath can reach it (#1621). Exact for the
# commit controls, because "Next" is the step and "Next post" would be somebody else's card.
_GROUP_MEDIA_CONFIRM_LABELS = ("next", "done")
_GROUP_MEDIA_TRIGGER_LABELS = ("add media", "add a photo", "add photo", "add a video", "add video")

# What the media chain did to the composer, which is a different question from whether the media
# went on (issue #1224). `LEFT_OPEN` is the one that matters downstream: the uploader's overlay is
# OURS, so an editor or Post button we cannot find after opening it says our overlay is still up —
# never that this group refuses member posts.
_MEDIA_UNTOUCHED = "untouched"
_MEDIA_ATTACHED = "attached"
_MEDIA_LEFT_OPEN = "left_open"

# How long the media overlay gets to finish before the run commits it, in polls of
# `_MEDIA_POLL_SECONDS`. An image is ready almost as fast as it uploads, but LinkedIn TRANSCODES a
# video server-side and keeps the overlay's commit control disabled until it is done, so a window
# sized on an image is what leaves an empty media frame on the published post (issue #1443). The
# video window is generous because the cost of waiting is a slower weekly beat and the cost of not
# waiting is a broken post.
#
# A poll that MISSES the control must cost a poll, not a selector timeout: the session's shared
# `wait` is `WAIT_DEFAULT_TIMEOUT` (15s), so re-finding through it would make the window nearly ten
# times the wall clock these numbers read as — and it would spend that inflation on the ABSENT case,
# the one answer that is an expected no-op, while the BUSY case (control found instantly, every
# poll) got only the sleeps. `_CONFIRM_LOOKUP_SECONDS` is the short wait the poll lookups use so the
# two cases cost the same and the numbers below mean what they say.
_MEDIA_POLL_SECONDS = (1.5, 2.5)
_CONFIRM_LOOKUP_SECONDS = 2
_IMAGE_READY_POLLS = 8      # ~16-36s
_VIDEO_READY_POLLS = 120    # ~3-5 min
# …and a control we have NEVER seen is answered on its own, shorter budget: the overlay renders its
# commit control DISABLED and only enables it when the transcode lands, so "not there at all" is a
# question about the overlay, not about the transcode. Without this the video window would spend its
# whole length re-asking the one question that was already answered.
_CONFIRM_ABSENT_POLLS = 20  # ~30-90s

# What the poll saw. ABSENT is not a failure: some composer variants have no commit step at all, and
# clicking nothing there is what #1224 already shipped — only a control that RESOLVED and stayed
# disabled says the upload never finished.
_CONFIRM_READY = "ready"
_CONFIRM_ABSENT = "absent"
_CONFIRM_BUSY = "busy"


def _media_control_ready(element: WebElement) -> bool:
    """Is the media overlay's commit control actually clickable, or still disabled by an upload?

    Both readings matter: LinkedIn disables the button outright while an image uploads and marks it
    `aria-disabled` while a video transcodes.
    """
    try:
        if not element.is_enabled():
            return False
        return str(element.get_attribute("aria-disabled") or "").lower() != "true"
    except WebDriverException:
        return False


def _group_media_input(driver, wait, container=None) -> Optional[WebElement]:
    """The composer's own hidden `<input type=file>` — inside the composer first, page-wide after.

    The composer-scoped read goes through the shadow-aware lookup because that is where the
    redesigned composer lives (#1621); the page-wide XPath chain stays as the last resort it always
    was, messaging overlay excluded (#1012). The input is never visible, so neither read filters on
    visibility.
    """
    if container is not None:
        found = find_deep_elements(driver, "input[type='file']", visible_only=False, limit=4,
                                   root=container)
        if found:
            return found[0]
    return find_first(driver, wait, _GROUP_MEDIA_INPUT_LOCATORS, "Group media input",
                      visible_only=False, required=False, warn_on_miss=False, max_try=1)


def _group_media_trigger(driver, wait, container=None) -> Optional[WebElement]:
    """The composer's styled "Add media" affordance — inside the composer first, page-wide after.

    Returned rather than clicked, so the caller owns the one press: this control OPENS an overlay,
    and a helper that clicks on the way out hides that from the state machine tracking it.
    """
    if container is not None:
        found = find_composer_control(container, _GROUP_MEDIA_TRIGGER_LABELS)
        if found is not None:
            return found
    return find_first(driver, wait, _GROUP_MEDIA_TRIGGER_LOCATORS, "Group media button",
                      required=False, warn_on_miss=False, max_try=1, visible_only=True)


def _await_media_confirm(driver, polls: int, container=None) -> Tuple[str, Optional[WebElement]]:
    """Poll the media overlay until its commit control is clickable.

    Returns `(state, element)` — `_CONFIRM_READY` with the control, `_CONFIRM_ABSENT` when this
    composer has no commit step, or `_CONFIRM_BUSY` when the control exists and never became
    clickable inside the window. Waiting on the CONTROL rather than on a fixed sleep is what makes
    the wait right for a video without slowing an image down: an image resolves on the first poll.

    The lookup uses its OWN short wait rather than the session's — a poll is a poll, and a miss that
    cost `WAIT_DEFAULT_TIMEOUT` would make the ABSENT case (an expected composer variant) the most
    expensive answer in the chain and the video's real window the shortest.

    `visible_only=True` for the same reason `click_first` (which this replaced) asks for it:
    LinkedIn ships hidden duplicates of a control, and the page-wide fallbacks in the locator chain
    will happily match one. A hidden button reads as ENABLED, so it would be clicked — and a click
    on a non-displayed element raises, which turns a working upload into `left_open` and costs the
    week's group post.
    """
    poll_wait = get_driver_wait(driver, wait_time=_CONFIRM_LOOKUP_SECONDS)
    seen = False
    for attempt in range(max(1, polls)):
        time.sleep(random.uniform(*_MEDIA_POLL_SECONDS))
        confirm = (find_composer_control(container, _GROUP_MEDIA_CONFIRM_LABELS, exact=True)
                   if container is not None else None)
        if confirm is None:
            confirm = find_first(driver, poll_wait, _GROUP_MEDIA_CONFIRM_LOCATORS,
                                 "Group media confirm", visible_only=True,
                                 required=False, warn_on_miss=False, max_try=1)
        if confirm is None:
            if not seen and attempt + 1 >= _CONFIRM_ABSENT_POLLS:
                return _CONFIRM_ABSENT, None
            continue
        seen = True
        if _media_control_ready(confirm):
            return _CONFIRM_READY, confirm
    return (_CONFIRM_BUSY if seen else _CONFIRM_ABSENT), None


def _media_is_video(media_type: Optional[str], path: str) -> bool:
    """Is the draft's attachment a video? The row's kind first, the stored file as the fallback.

    Both answers come from the same place originally — `_resolve_group_media` reads the file — so
    this only has to cover a draft written before the column carried a kind.
    """
    if media_type:
        return str(media_type).lower() == str(GroupPostMediaType.VIDEO)
    try:
        return determine_media_type(path) == "VIDEO"
    except ValueError:
        return False


def _attach_group_media(driver, wait, media_url: str, user_id: int = None,
                        media_type: Optional[str] = None, container=None) -> str:
    """Hand the draft's image/video to the open group composer.

    Returns what the attempt did to the COMPOSER, not just whether it worked: `_MEDIA_ATTACHED`,
    `_MEDIA_UNTOUCHED` (nothing opened — the composer is exactly as we found it), or
    `_MEDIA_LEFT_OPEN` (we opened the uploader and could not finish, so an overlay may still be
    covering the editor). The caller needs that third answer to tell OUR overlay apart from a group
    that will not take a member post.

    Never raises and never blocks the post: a group post that goes out as text is worth more than
    no post at all, so every failure here is a warning and the caller carries on — the same
    fail-open posture `render_image_gated` and the article cover take. The file is written into the
    hidden `<input type=file>` because clicking the styled control opens the OS file chooser, which
    Selenium cannot drive; `webdriver.Remote`'s local file detector ships the bytes to the Chrome
    node, so the worker's own path is the right thing to send.
    """
    path = post_media_abs_path(media_url)
    if not path:
        log_warning("Group post media is missing on disk — posting the text alone", user_id=user_id,
                    task_name="auto_post_to_group")
        return _MEDIA_UNTOUCHED
    opened = False
    try:
        file_input = _group_media_input(driver, wait, container)
        if file_input is None:
            trigger = _group_media_trigger(driver, wait, container)
            if trigger is not None:
                trigger.click()
                opened = True
                time.sleep(random.uniform(1, 2))
                file_input = _group_media_input(driver, wait, container)
        if file_input is None:
            log_warning("Group composer media control not found — posting the text alone",
                        user_id=user_id, task_name="auto_post_to_group")
            return _MEDIA_LEFT_OPEN if opened else _MEDIA_UNTOUCHED
        file_input.send_keys(path)
        opened = True
        # LinkedIn transcodes the upload before the overlay will commit, so the run waits on the
        # commit control becoming clickable rather than on a clock — a video needs minutes where an
        # image needs seconds, and committing early is what leaves an empty media frame on the post.
        state, confirm = _await_media_confirm(
            driver,
            _VIDEO_READY_POLLS if _media_is_video(media_type, path) else _IMAGE_READY_POLLS,
            container=container)
        if state == _CONFIRM_BUSY:
            # The control resolved and never became clickable: the upload did not finish inside the
            # window, so OUR overlay is still covering the composer and the caller must not read a
            # missing editor as this group refusing member posts.
            log_warning("Group post media never finished uploading — posting the text alone",
                        user_id=user_id, task_name="auto_post_to_group")
            return _MEDIA_LEFT_OPEN
        if confirm is not None:
            confirm.click()
            time.sleep(random.uniform(1, 2))
        return _MEDIA_ATTACHED
    except WebDriverException as e:
        log_warning("Group post media attach failed — posting the text alone", exc=e,
                    user_id=user_id, task_name="auto_post_to_group")
        return _MEDIA_LEFT_OPEN if opened else _MEDIA_UNTOUCHED


@shared_task.task(name='cqc_lem.app.run_automation.auto_post_to_group',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True,
                                                   'keys': ['user_id', 'group_id']},
                  queue='se_content')
def auto_post_to_group(self, user_id: int, group_id: str, group_name: str = None,
                       draft_id: int = None):
    """Publish the user's reviewed group-post draft into that group via its share box.

    The text is never written here (issue #932) — it comes from the draft the user has had days to
    read and edit, and a run with no usable draft publishes NOTHING rather than falling back to an
    un-previewed generation. Best-effort — the group composer selectors are validated in the live
    pass.
    """
    draft = get_group_post_draft(draft_id) if draft_id else None
    if (draft is None or draft.get("user_id") != user_id
            or str(draft.get("status")) != str(GroupPostDraftStatus.READY)):
        log_info("No reviewed group post draft to publish", user_id=user_id,
                 task_name="auto_post_to_group")
        return "No group post draft to publish"
    text = strip_non_bmp(draft.get("content") or "")
    if not text.strip():
        return "No group post draft to publish"
    try:
        # needs_images=True (issue #1778): the group share box lives on the same fastboot page,
        # dead with images blocked.
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Group Post",
                                                                    needs_images=True)
    except Exception as e:
        log_error("Error getting profile for group post", exc=e, user_id=user_id, task_name="auto_post_to_group")
        return f"Failed: {e}"
    try:
        driver.get(f"https://www.linkedin.com/groups/{group_id}/")
        time.sleep(random.uniform(4, 7))

        def _unpostable(reason: str) -> str:
            # The group loaded but its composer did not: members cannot post here (admin-only /
            # announcement group). Stamp the RUN so the rotation moves past it — ordering on
            # successful posts alone left such a group "least recently posted" forever, starving
            # every other post-enabled group (issue #858). `last_posted_at` is untouched, so it
            # stays the truthful record of what actually shipped.
            record_group_post_run(user_id, group_id)
            # The draft was written FOR this group, so it dies with the group's turn — the next
            # draft is written fresh for whichever group the rotation moves to.
            update_group_post_draft(draft["id"], status=GroupPostDraftStatus.FAILED)
            log_info(f"Group is not postable, rotating past it: {reason}", user_id=user_id,
                     task_name="auto_post_to_group")
            return reason

        # Open the group share box, type, and post (best-effort SDUI selectors).
        if click_first(driver, wait, _GROUP_SHARE_BOX_LOCATORS,
                       "Group share box", required=False) is None:
            # A share box that does not resolve is only "this group is unpostable" if the page
            # itself says there is no share box. If the page text still contains "Start a post" then
            # the control rendered but our chain cannot see it — selector drift, and we must warn
            # rather than quietly rotate past a postable group (#1107).
            page_text = ""
            for tag in ("main", "body"):
                try:
                    page_text = (driver.find_element(By.TAG_NAME, tag).text or "").lower()
                    if page_text.strip():
                        break
                except Exception:
                    continue
            has_share_signal = any(signal in page_text for signal in _GROUP_SHARE_BOX_TEXT_SIGNALS)
            if has_share_signal:
                log_warning("Group share box control drifted: page renders the signal but the locator "
                          "chain did not resolve it", user_id=user_id, task_name="auto_post_to_group",
                          group_id=group_id)
            return _unpostable("Group share box control drifted" if has_share_signal
                               else "Group share box not found")
        time.sleep(random.uniform(2, 3))
        # WHERE the composer opened, before anything is asked of it (#1621). The redesigned share box
        # mounts inside a shadow root, so a page-level lookup for the editor reads a working composer
        # as a group that refuses member posts — which is what stamped healthy drafts FAILED.
        composer = find_composer_container(driver, user_id=user_id)
        if composer is None:
            return _unpostable("Group composer did not open")
        # Media goes in BEFORE the text: LinkedIn's uploader takes over the composer while it
        # transcodes, and text typed first is what the overlay discards.
        media_state = (_attach_group_media(driver, wait, draft["media_url"], user_id=user_id,
                                           media_type=draft.get("media_type"), container=composer)
                       if draft.get("media_url") else _MEDIA_UNTOUCHED)
        media_attached = media_state == _MEDIA_ATTACHED

        def _composer_blocked(reason: str) -> str:
            # Our own uploader overlay, NOT a group that refuses member posts: the share box opened
            # a moment ago, so `_unpostable` here would stamp the draft FAILED and rotate past a
            # healthy group on the strength of a control WE covered up. The draft stays `ready` and
            # takes the next weekly slot; the repeat-escalating warning is what turns real drift
            # into one grouped issue.
            log_warning(f"Group composer unusable after the media step: {reason}", user_id=user_id,
                        task_name="auto_post_to_group", group_id=group_id)
            return reason

        _lost = _composer_blocked if media_state != _MEDIA_UNTOUCHED else _unpostable
        # Re-read after the media step: the uploader replaces the composer's container on some
        # variants, and the element captured before it would be stale.
        composer = find_composer_container(driver, user_id=user_id) or composer
        box = next(iter(find_deep_elements(driver, _GROUP_EDITOR_CSS, visible_only=True, limit=4,
                                           root=composer)), None)
        if box is None:
            return _lost("Group post editor not found")
        box.click()
        box.send_keys(text)
        time.sleep(random.uniform(1, 2))
        post_button = find_composer_control(composer, _GROUP_POST_BUTTON_LABELS, exact=True)
        if post_button is None:
            return _lost("Group Post button not found")
        post_button.click()
        time.sleep(random.uniform(3, 5))
        # Only a post that actually shipped advances the rotation — a failed run leaves this group
        # next in line rather than skipping its turn.
        record_group_post(user_id, group_id)
        update_group_post_draft(draft["id"], status=GroupPostDraftStatus.PUBLISHED)
        if media_attached:
            return f"Posted to group with {draft.get('media_type') or 'media'}"
        return "Posted to group"
    except Exception as e:
        log_error("Group post error", exc=e, user_id=user_id, task_name="auto_post_to_group")
        return f"Error: {e}"
    finally:
        quit_gracefully(driver)


@shared_task.task(name='cqc_lem.app.run_automation.automate_commenting',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def automate_commenting(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60,
                        post_id: int = None):
    """Walk the feed and comment.

    `post_id` is set only by the pre-post warm-up dispatch (`auto_check_scheduled_posts`) — it makes
    each pass record a per-post engagement-window marker so a report can confirm the warm-up before
    that post actually happened (issue #547). It rides the self-requeue kwargs, so every pass in the
    window accumulates onto the same marker.
    """
    log_info("Starting Automate Commenting Thread...")

    # Comment-quality hold (issue #628): when the weekly outcome report finds our comments are being
    # demoted out of 'Most relevant', commenting stops for this user until a human clears it.
    # Checked here (not in the dispatcher) so EVERY caller — golden hour, pre-post warm-up, the
    # self-requeue — is covered by one gate. Fails open when Redis is unavailable.
    if is_commenting_held(user_id):
        reason = commenting_hold_reason(user_id) or "comment quality"
        log_warning(f"Feed commenting held for user {user_id}: {reason}", user_id=user_id,
                    action_type="comment", task_name="automate_commenting")
        return f"Skipped: feed commenting is held ({reason})"

    # Single-flight per user: the pre-post trigger, the golden-hour beat, and this task's own
    # self-requeue can otherwise run concurrently and double-walk the feed. Only one commenting
    # run per user at a time; a loser skips this cycle (its own re-schedule will pick it up).
    lock_name = f"automate_commenting:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1800)
    if lock_token is None:
        # DEBUG: the comment above says the lock exists BECAUSE three triggers can overlap —
        # losing the race is the single-flight guard working, not a degraded run.
        log_debug(f"Another commenting run is in progress for user {user_id} — skipping this cycle.")
        return "Skipped: another commenting run already in progress for this user."

    if post_id:
        log_info("Pre-post engagement window opened", post_id=post_id, user_id=user_id,
                 task_name="automate_commenting")

    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Auto Commenting")
    except Exception as e:
        log_error("Error while getting profile for auto commenting", exc=e, user_id=user_id,
                  task_name="automate_commenting")
        release_run_lock(lock_name, lock_token)
        return f"Failed to start auto commenting: {e}"

    result = "Automate Commenting Task Started"

    try:

        navigate_to_feed(driver, wait)

        start_time = datetime.now()
        deadline_ts = (start_time.timestamp() + loop_for_duration) if loop_for_duration else None

        # Comment inline on the SDUI feed (no per-post permalink navigation anymore).
        post_commented_count = comment_on_feed_inline(driver, wait, my_profile, user_id,
                                                      max_posts=10, deadline_ts=deadline_ts)

        result = f"Automate Commenting Task Completed. Commented on {post_commented_count} posts."

        if post_id:
            record_pre_post_run(post_id, user_id, post_commented_count)

        # Re-schedule the task in the queue for the future
        if loop_for_duration:
            elapsed_time = datetime.now() - start_time
            new_loop_for_duration = round(loop_for_duration - elapsed_time.total_seconds() - future_forward)
            frame = inspect.currentframe()
            current_function_name = frame.f_code.co_name
            args, _, _, values = inspect.getargvalues(frame)
            kwargs = {arg: values[arg] for arg in args}
            # myprint(f"{current_function_name} parameters: {kwargs}")

            if new_loop_for_duration < 0:
                # DEBUG on both branches: the re-queue loop's own bookkeeping, and the duration
                # running out IS its exit condition.
                log_debug(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                log_debug(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
                # Remove 'self' from kwargs if it exists
                if 'self' in kwargs:
                    del kwargs['self']
                # Call self again in the future
                globals()[current_function_name].apply_async(kwargs=kwargs, countdown=future_forward)

    except Exception as e:
        log_error("Error while automating commenting", exc=e, user_id=user_id, task_name="automate_commenting")
        result = f"Error while automating commenting: {e}"
    finally:
        quit_gracefully(driver)  # Close the driver
        # Released here (after this run's feed walk); the self-requeue fires 60s later as a fresh
        # task and re-acquires. A crash still frees the lock via its TTL.
        release_run_lock(lock_name, lock_token)

    return result
