"""One-off backfill: replace off-topic seed comments the system left on a user's OWN posts.

Older builds stored a STATUS STRING ("Successfully created post using /posts API endpoint.") as
the POST log message, and the seed-comment task grounded the AI on that log message — so the pinned
first comments read like they were about the /posts API instead of the post's subject. This script
navigates to each affected post, deletes OUR OWN comment(s) on it, and posts a fresh seed comment
grounded on the canonical post body (posts table) — exactly what the fixed code now does.

Only comments authored by the user (matched by full name) are ever touched.

Usage (inside a selenium-capable celery container):
    MODE=report python scripts/reseed_own_post_comments.py        # dry run: show old body + new seed
    MODE=apply  python scripts/reseed_own_post_comments.py        # delete old + post new + pin
Env: USER_ID (default 1), POST_IDS (comma list, default "10,11,13,78").
"""
import os
import random
import time

from selenium.webdriver.common.by import By

from cqc_lem.app.run_automation import (
    get_current_profile, post_comment_inline, _pin_own_comment,
    _OPEN_LAST_OWN_COMMENT_MENU_JS, _CLICK_DELETE_MENUITEM_JS,
    _CONFIRM_DELETE_MODAL_JS, _COUNT_OWN_COMMENT_MENUS_JS,
)
from cqc_lem.utilities.ai.ai_helper import generate_seed_comment, get_or_create_profile_synthesis
from cqc_lem.utilities.db import (
    get_post_content, get_post_url_from_log_for_user, get_engagement_preferences,
    insert_new_log, LogActionType, LogResultType,
)
from cqc_lem.utilities.selenium_util import wait_for_ajax, find_first, quit_gracefully
from cqc_lem.utilities.logger import myprint

USER_ID = int(os.getenv("USER_ID", "1"))
POST_IDS = [int(p) for p in os.getenv("POST_IDS", "10,11,13,78").split(",") if p.strip()]
MODE = os.getenv("MODE", "report").lower()


def _delete_all_own_comments(driver, full_name: str, cap: int = 10) -> int:
    """Delete every comment authored by full_name on the currently-open post. Re-counts each pass
    because the DOM re-renders after a delete. Only our own comments are matched."""
    deleted = 0
    for _ in range(cap):
        if int(driver.execute_script(_COUNT_OWN_COMMENT_MENUS_JS, full_name) or 0) <= 0:
            break
        if not driver.execute_script(_OPEN_LAST_OWN_COMMENT_MENU_JS, full_name):
            break
        time.sleep(random.uniform(1, 2))
        if not driver.execute_script(_CLICK_DELETE_MENUITEM_JS):
            myprint("  delete menu item not found; stopping this post")
            break
        time.sleep(random.uniform(1, 2))
        if not driver.execute_script(_CONFIRM_DELETE_MODAL_JS):
            myprint("  delete confirm button not found; stopping this post")
            break
        wait_for_ajax(driver)
        time.sleep(random.uniform(2, 3))
        deleted += 1
    return deleted


def main() -> None:
    myprint(f"Reseed backfill | user={USER_ID} posts={POST_IDS} MODE={MODE}")
    driver, wait, _email, prof = get_current_profile(user_id=USER_ID, session_name="Reseed Backfill")
    full_name = prof.full_name
    synth = get_or_create_profile_synthesis(USER_ID, prof)
    prefs = get_engagement_preferences(USER_ID)
    try:
        for pid in POST_IDS:
            content = get_post_content(pid)
            url = get_post_url_from_log_for_user(USER_ID, pid)
            if not content or not url:
                myprint(f"[post {pid}] SKIP — missing content/url (content={bool(content)}, url={bool(url)})")
                continue
            driver.get(url)
            time.sleep(random.uniform(4, 7))
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(2)
            own = int(driver.execute_script(_COUNT_OWN_COMMENT_MENUS_JS, full_name) or 0)
            new_seed = generate_seed_comment(content, prof, prefs, profile_synthesis=synth)
            myprint(f"[post {pid}] own_comments={own}\n  BODY: {content[:90]!r}\n  NEW SEED: {new_seed!r}")
            if MODE != "apply":
                continue
            if not new_seed:
                myprint(f"[post {pid}] no seed generated — leaving as-is")
                continue
            deleted = _delete_all_own_comments(driver, full_name)
            myprint(f"[post {pid}] deleted {deleted} old comment(s)")
            card = find_first(driver, wait,
                              [(By.CSS_SELECTOR, "div.feed-shared-update-v2"), (By.TAG_NAME, "main")],
                              "Post container", required=False) or driver.find_element(By.TAG_NAME, "body")
            if post_comment_inline(driver, wait, card, new_seed, user_id=USER_ID):
                insert_new_log(user_id=USER_ID, post_id=pid, action_type=LogActionType.COMMENT,
                               result=LogResultType.SUCCESS, post_url=url, message=new_seed)
                time.sleep(random.uniform(2, 4))
                pinned = _pin_own_comment(driver)
                myprint(f"[post {pid}] reseeded (pinned={pinned})")
            else:
                myprint(f"[post {pid}] FAILED to post new seed comment")
            time.sleep(random.uniform(8, 15))
    finally:
        quit_gracefully(driver)


if __name__ == "__main__":
    main()
