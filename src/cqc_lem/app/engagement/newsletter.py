"""The newsletter rail: publish an edition, and track/grow its subscribers (#1154).

Step 2 of the `run_automation.py` split. The cluster is closed on both sides — the ten symbols here
are read by nothing outside this module, and after the shared Selenium core went down to
`utilities/linkedin/*` (#1193) they read nothing that lives in `run_automation` either. The three
tasks are reached from `run_scheduler` by `.apply_async`, which is a wire name rather than an import.

Two invariants travel with this code and are easy to lose in a move:

**`_approved_cover_path` is the ONLY thing that decides a cover may reach LinkedIn** (#893). A
generated cover sits at `pending_review` until the author approves it, because it is a public brand
asset; reading `cover_image_path` directly anywhere else publishes unreviewed artwork.

**Every task pins `name='cqc_lem.app.run_automation.<fn>'`.** Celery derives a task's name from
`<module>.<function>`, so moving a task RENAMES it — silently. Two `celeryconfig.task_routes` keys
name these tasks as plain strings and would simply stop matching; messages already queued under the
old name would be rejected `NotRegistered` and dropped; and the `QueueOnce` lock key embeds the task
name, so it would re-key mid-deploy and let a second publish of the same edition through. Pinning
keeps the wire name, the lock key and the routed queue byte-identical to the pre-move ones.
`tests/unit/app/test_task_name_stability.py` freezes that; do not remove a `name=` to "tidy up".

The module imports NOTHING from `run_automation` — that is what keeps the dependency one-way, since
`run_automation` imports the three tasks back so `run_scheduler`'s existing import sites still work.
"""

import random
import re
import time

from selenium.webdriver.common.by import By

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.utilities.ai.ai_helper import generate_newsletter_edition
from cqc_lem.utilities.blog_source import resolve_blog_source
from cqc_lem.utilities.db import (
    get_newsletter_edition,
    get_newsletter_settings,
    mark_edition_failed,
    mark_edition_published,
    mark_newsletter_published,
    record_newsletter_subscriber_stat,
)
from cqc_lem.utilities.linkedin.article_editor import fill_article_editor
from cqc_lem.utilities.linkedin.session import get_current_profile
from cqc_lem.utilities.linkedin_formatter import strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.selenium_util import (
    click_first,
    find_first,
    get_elements_as_list_wait_stale,
    getText,
    quit_gracefully,
)


def _fill_edition_description(driver, wait, subtitle: str) -> bool:
    """Best-effort: fill the newsletter edition-description field in the publish dialog. LinkedIn
    surfaces a 'what this edition is about' textarea/contenteditable whose placeholder/aria mentions
    'edition', 'about', or 'what this'. Non-fatal — the field wasn't present in one live run, so we
    never block publishing on it (fail fast: max_try=1, no retries).
    """
    if not subtitle:
        return False
    candidates = []
    for attr in ("@placeholder", "@aria-label"):
        _l = f"translate({attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
        for kw in ("edition", "about", "what this"):
            candidates.append(
                (By.XPATH, f"//*[(self::textarea or @role='textbox' or @contenteditable='true') "
                           f"and contains({_l},'{kw}')]"))
    try:
        desc_el = find_first(driver, wait, candidates, "Edition description",
                             visible_only=True, required=False, max_try=1)
        if desc_el is None:
            return False
        desc_el.click()
        desc_el.send_keys(strip_non_bmp(subtitle))
        time.sleep(random.uniform(1, 2))
        return True
    except Exception:
        return False


def _fill_and_publish_article(driver, wait, title: str, body: str, subtitle: str = None,
                              user_id: "int | None" = None,
                              cover_image_path: str = None) -> "tuple[str | None, str | None]":
    """Fill LinkedIn's article editor (title textarea + contenteditable body) and run the
    Next → Publish flow. On the publish dialog, best-effort fills the edition description with
    `subtitle`.

    Uses the selector ladder in `cqc_lem.utilities.linkedin.article_editor` so a rotated entry point
    is caught by a fallback route and `failed_step` names the exact missing action. Returns
    `(published_url, None)` on success, or `(None, failed_step)` on failure.
    """
    return fill_article_editor(
        driver, wait, strip_non_bmp(title), strip_non_bmp(body),
        user_id=user_id,
        subtitle=subtitle,
        cover_image_path=cover_image_path,
        fill_description_fn=_fill_edition_description,
    )


def _approved_cover_path(edition: dict) -> "str | None":
    """The absolute path of an edition's cover, but ONLY once the author approved it (issue #893).

    A cover is a public brand asset. Generated ones sit at `pending_review` until the author says
    otherwise, so this is the ONE place that decides a cover may reach LinkedIn — reading
    `cover_image_path` alone anywhere else would publish unreviewed artwork.
    """
    from cqc_lem.utilities.newsletter_cover import COVER_STATUS_APPROVED, cover_abs_path
    if (edition or {}).get("cover_image_status") != COVER_STATUS_APPROVED:
        if (edition or {}).get("cover_image_path"):
            # The refusal is unchanged — this only makes the drop visible. INFO, not WARNING:
            # publishing cover-less is the DESIGNED outcome of an unapproved cover, so escalating
            # it would file a defect against working behaviour (issue #1432). The pre-slot
            # reminder (`auto_notify_pending_covers`) is what asks the author to act.
            log_info("Publishing newsletter edition without its unapproved cover",
                     user_id=(edition or {}).get("user_id"),
                     edition_id=(edition or {}).get("id"), action_type="newsletter_cover")
        return None
    path = cover_abs_path(edition.get("cover_image_path"))
    if path is None and edition.get("cover_image_path"):
        log_warning("Approved newsletter cover file is missing — publishing without it",
                    user_id=edition.get("user_id"), action_type="newsletter_cover")
    return path


def _tagged_edition_body(body: str, edition_id: "int | None" = None) -> str:
    """The edition body with any OWNED link in it UTM-tagged under this edition's campaign (#658).

    The generator is told not to put links in an edition at all (off-platform links suppress an
    article's reach), so on the mainline path this is a no-op — but the SPA lets the author edit a
    draft before it publishes, and a hand-added link to their own site is exactly the traffic worth
    attributing to the edition that sent it. Publish time is the right choke point: it is the last
    moment the body is still ours, and both publish tasks pass through here.
    """
    from cqc_lem.utilities.marketing.attribution import (
        MEDIUM_NEWSLETTER,
        SOURCE_NEWSLETTER,
        campaign_for_edition,
        tag_links_in_text,
    )
    return tag_links_in_text(body, SOURCE_NEWSLETTER, MEDIUM_NEWSLETTER,
                             campaign_for_edition(edition_id))


@shared_task.task(name='cqc_lem.app.run_automation.auto_publish_newsletter_edition',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def auto_publish_newsletter_edition(self, user_id: int):
    """Generate and publish a newsletter edition for the user (opt-in via newsletter_settings).
    Best-effort — the article publish flow is multi-step; the first real publish should be
    supervised. Repurposes the user's blog when align_with_blog is set.
    """
    settings = get_newsletter_settings(user_id)
    if not settings.get("enabled"):
        return "Newsletter not enabled"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Newsletter")
    except Exception as e:
        log_error("Error getting profile for newsletter", exc=e, user_id=user_id, task_name="auto_publish_newsletter_edition")
        return f"Failed to start newsletter: {e}"
    try:
        edition = generate_newsletter_edition(my_profile, topic=settings.get("topic"),
                                              blog_content=resolve_blog_source(user_id, settings))
        if not edition:
            return "No newsletter edition generated"
        driver.get("https://www.linkedin.com/article/new/")
        time.sleep(random.uniform(6, 9))
        url, failed_step = _fill_and_publish_article(driver, wait, edition["title"],
                                                      _tagged_edition_body(edition["body"]),
                                                      subtitle=edition.get("subtitle"),
                                                      user_id=user_id)
        if url:
            mark_newsletter_published(user_id, url)
            log_info(f"Published newsletter edition for user {user_id}: {edition['title']}")
            return f"Published newsletter: {edition['title']}"
        log_error("Newsletter publish flow did not complete",
                  user_id=user_id, task_name="auto_publish_newsletter_edition", failed_step=failed_step)
        return "Newsletter publish flow did not complete"
    except Exception as e:
        log_error("Newsletter publish error", exc=e, user_id=user_id, task_name="auto_publish_newsletter_edition")
        return f"Newsletter error: {e}"
    finally:
        quit_gracefully(driver)


@shared_task.task(name='cqc_lem.app.run_automation.auto_publish_edition',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['edition_id']},
                  queue='se_content')
def auto_publish_edition(self, edition_id: int):
    """Publish a reviewed newsletter edition at its scheduled slot. Loads the pre-generated edition,
    fills LinkedIn's article editor, and records the outcome. Best-effort — the multi-step publish
    flow varies; first real publish should be supervised.

    An UNAPPROVED edition (still 'draft') publishes only for a user who opted into
    `auto_publish_newsletters` (issue #1135). That mirrors the due-filter in
    `get_editions_due_to_publish` at the worker boundary — the same defense-in-depth posture as
    `post_to_linkedin`'s `get_post_status` re-read — because a queued message outlives the query
    that produced it: the author can turn the setting off, or a retry can fire, after dispatch.
    An opted-out draft reaching here is expected rather than a defect, so it is DEBUG.
    """
    edition = get_newsletter_edition(edition_id)
    if not edition or edition.get("status") not in ("draft", "approved"):
        return f"Edition {edition_id} not publishable"
    user_id = edition["user_id"]
    if edition.get("status") == "draft" and not get_newsletter_settings(user_id).get("auto_publish_newsletters"):
        log_debug(f"Newsletter edition {edition_id} is unapproved and auto-publish is off — holding",
                  user_id=user_id, task_name="auto_publish_edition", edition_id=edition_id)
        return f"Edition {edition_id} awaiting approval"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Newsletter")
    except Exception as e:
        log_error("Error getting profile for newsletter edition", exc=e, user_id=user_id, task_name="auto_publish_edition")
        return f"Failed to start newsletter edition: {e}"
    try:
        driver.get("https://www.linkedin.com/article/new/")
        time.sleep(random.uniform(6, 9))
        url, failed_step = _fill_and_publish_article(driver, wait, edition["title"],
                                                      _tagged_edition_body(edition["body"], edition_id),
                                                      subtitle=edition.get("subtitle"),
                                                      user_id=user_id,
                                                      cover_image_path=_approved_cover_path(edition))
        if url:
            mark_edition_published(edition_id, url)
            log_info(f"Published newsletter edition {edition_id} for user {user_id}: {edition['title']}")
            return f"Published newsletter edition: {edition['title']}"
        log_error("Newsletter edition publish flow did not complete",
                  user_id=user_id, task_name="auto_publish_edition", edition_id=edition_id, failed_step=failed_step)
        mark_edition_failed(edition_id)
        return "Newsletter edition publish flow did not complete"
    except Exception as e:
        log_error("Newsletter edition publish error", exc=e, user_id=user_id, task_name="auto_publish_edition")
        mark_edition_failed(edition_id)
        return f"Newsletter edition error: {e}"
    finally:
        quit_gracefully(driver)


def _parse_subscriber_count(text: "str | None") -> "int | None":
    """Pull a subscriber count out of LinkedIn's label text, e.g. '1,234 subscribers' -> 1234,
    '3.2K subscribers' -> 3200. Returns None when no count is present.
    """
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*([KkMm]?)\s*subscriber", text)
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).upper()
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return None
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    return int(value)


def _read_newsletter_subscriber_count(driver, wait, newsletter_url: str) -> "int | None":
    """Best-effort: open the user's newsletter page and read its 'N subscribers' label. LinkedIn
    renders the count in a header near the newsletter title; selectors vary, so we scan a few
    candidates and fall back to a page-text regex. Returns None when the count can't be read —
    never raises (validated on a supervised first real run).
    """
    if not newsletter_url:
        return None
    driver.get(newsletter_url)
    time.sleep(random.uniform(4, 7))
    el = find_first(driver, wait,
                    [(By.XPATH, "//*[contains(translate(text(),'SUBSCRIBER','subscriber'),'subscriber')]")],
                    "Subscriber count", visible_only=True, required=False, max_try=1)
    if el is not None:
        count = _parse_subscriber_count(getText(el) or "")
        if count is not None:
            return count
    try:
        return _parse_subscriber_count(driver.find_element(By.TAG_NAME, "body").text)
    except Exception:
        return None


def _invite_connections_to_newsletter(driver, wait, cap: int) -> int:
    """Best-effort: from the open newsletter page, invite up to `cap` connections to subscribe.
    Opens the 'Invite' dialog (LinkedIn labels it 'Invite connections'/'Manage'), selects up to
    `cap` connection checkboxes, and sends. Returns the number invited (0 when the flow isn't
    available or cap<=0). Never raises — caps are enforced here, opt-in is enforced by the caller
    (validated on a supervised first real run).
    """
    if cap <= 0:
        return 0
    if click_first(driver, wait,
                   [(By.XPATH, "//button[contains(translate(@aria-label,'INVITE','invite'),'invite')]"),
                    (By.XPATH, "//button[contains(translate(normalize-space(),'INVITE','invite'),'invite')]")],
                   "Invite connections", required=False, max_try=1) is None:
        return 0
    time.sleep(random.uniform(2, 4))
    checkboxes = get_elements_as_list_wait_stale(
        wait, "//input[@type='checkbox' and contains(@id,'invitee')]",
        "Newsletter invitee checkboxes", max_retry=0) or []
    selected = 0
    for cb in checkboxes:
        if selected >= cap:
            break
        try:
            driver.execute_script("arguments[0].click();", cb)
            selected += 1
            time.sleep(random.uniform(0.2, 0.6))
        except Exception:
            continue
    if selected == 0:
        return 0
    if click_first(driver, wait,
                   [(By.XPATH, "//button[contains(translate(normalize-space(),'INVITE','invite'),'invite') "
                               "and not(@disabled)]")],
                   "Send newsletter invites", required=False, max_try=1) is None:
        return 0
    time.sleep(random.uniform(2, 4))
    return selected


@shared_task.task(name='cqc_lem.app.run_automation.track_newsletter_subscribers',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def track_newsletter_subscribers(self, user_id: int):
    """Capture the user's newsletter subscriber count over time and, when opted in, invite
    connections to subscribe within the per-run cap (issue #400). Reads the count from the
    newsletter page and records a growth snapshot; inviting only runs when
    invite_connections_enabled is set and stops at max_invites_per_run. Best-effort Selenium —
    the first real run should be supervised.
    """
    settings = get_newsletter_settings(user_id)
    if not settings.get("enabled"):
        return "Newsletter not enabled"
    newsletter_url = settings.get("newsletter_url")
    if not newsletter_url:
        return "No newsletter URL yet"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Newsletter")
    except Exception as e:
        log_error("Error getting profile for newsletter tracking", exc=e, user_id=user_id,
                  task_name="track_newsletter_subscribers")
        return f"Failed to start newsletter tracking: {e}"
    try:
        count = _read_newsletter_subscriber_count(driver, wait, newsletter_url)
        invited = 0
        if settings.get("invite_connections_enabled"):
            invited = _invite_connections_to_newsletter(driver, wait, int(settings.get("max_invites_per_run") or 0))
        record_newsletter_subscriber_stat(user_id, subscriber_count=count, invites_sent=invited)
        log_info("Newsletter subscriber snapshot", user_id=user_id,
                 task_name="track_newsletter_subscribers")
        return f"Subscribers: {count if count is not None else 'unknown'}; invited {invited}"
    except Exception as e:
        log_error("Newsletter subscriber tracking error", exc=e, user_id=user_id,
                  task_name="track_newsletter_subscribers")
        return f"Newsletter tracking error: {e}"
    finally:
        quit_gracefully(driver)
