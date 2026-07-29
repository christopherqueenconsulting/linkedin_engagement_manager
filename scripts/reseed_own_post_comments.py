"""One-off backfill for off-topic seed comments the system left on a user's OWN posts.

Older builds stored a STATUS STRING ("Successfully created post using /posts API endpoint.") as
the POST log message, and the seed-comment task grounded the AI on that log message — so the pinned
first comments read like they were about the /posts API instead of the post's subject.

Two modes (the split exists because LinkedIn grants comment WRITE via w_member_social but not
comment READ/enumerate, so old comments can't be discovered/deleted through the API):

    MODE=api_post    Post a fresh, on-topic seed comment via the socialActions API — no Selenium,
                     no login, immune to the feed-navigation 429. Grounds on the canonical post
                     body (posts table) + cached voice synthesis. This is what the fixed
                     auto_seed_comment_on_post now does.

    MODE=delete_old  Delete the OLD off-topic comments via Selenium (the only way — the API can't
                     enumerate them). Matches OUR OWN comments whose text starts with one of
                     OLD_PREFIXES so the freshly posted good comments are never touched. Respects
                     the 429 circuit breaker; run it during a quiet, non-rate-limited window.

Env: USER_ID (default 1), POST_IDS (default "10,11,13,78"), MODE (default report).
"""
import os
import random
import time

USER_ID = int(os.getenv("USER_ID", "1"))
POST_IDS = [int(p) for p in os.getenv("POST_IDS", "10,11,13,78").split(",") if p.strip()]
MODE = os.getenv("MODE", "report").lower()

# Distinctive leading text of the known-bad seed comments (they were all about the posting API).
OLD_PREFIXES = (
    "Creating posts using the API",
    "I added exponential backoff for 429s",
    "Feels good to finally have this automated",
    "Did anyone run into rate",
)


def _api_post():
    from cqc_lem.utilities.linkedin.poster import comment_on_linkedin_post, object_urn_from_post_url
    from cqc_lem.utilities.db import (
        get_post_url_from_log_for_user, get_post_content, get_engagement_preferences,
        insert_new_log, LogActionType, LogResultType,
    )
    from cqc_lem.utilities.ai.ai_helper import generate_seed_comment, get_or_create_profile_synthesis
    from cqc_lem.utilities.linkedin.helper import load_profile_for_user
    from cqc_lem.utilities.logger import myprint

    profile = load_profile_for_user(USER_ID)
    synth = get_or_create_profile_synthesis(USER_ID, profile)
    prefs = get_engagement_preferences(USER_ID)
    for pid in POST_IDS:
        url = get_post_url_from_log_for_user(USER_ID, pid)
        body = get_post_content(pid)
        obj = object_urn_from_post_url(url)
        if not (url and body and obj):
            myprint(f"[post {pid}] SKIP — url={bool(url)} body={bool(body)} urn={obj}")
            continue
        seed = generate_seed_comment(body, profile, prefs, profile_synthesis=synth)
        myprint(f"[post {pid}] {obj}\n  NEW SEED: {seed!r}")
        if MODE != "api_post" or not seed:
            continue
        urn = comment_on_linkedin_post(USER_ID, obj, seed)
        if urn:
            insert_new_log(user_id=USER_ID, post_id=pid, action_type=LogActionType.COMMENT,
                           result=LogResultType.SUCCESS, post_url=url, message=seed)
            myprint(f"[post {pid}] POSTED via API -> {urn}")
        else:
            myprint(f"[post {pid}] FAILED to post")


def _delete_old():
    from cqc_lem.app.run_automation import get_current_profile
    from cqc_lem.utilities.db import get_post_url_from_log_for_user
    from cqc_lem.utilities.selenium_util import wait_for_ajax, quit_gracefully
    from cqc_lem.utilities.logger import myprint

    driver, wait, _email, prof = get_current_profile(user_id=USER_ID, session_name="Delete Old Seed")
    name = (prof.full_name or "").lower()
    # Open the comment overflow menu for OUR comment whose text starts with an old prefix, delete it.
    del_js = (
        "const name=(arguments[0]||'');"
        "const pref=arguments[1];"
        "const items=[...document.querySelectorAll('article,[data-id],li')];"
        "for(const it of items){const t=(it.innerText||'');"
        " if(pref.some(p=>t.includes(p))){"
        "  const b=[...it.querySelectorAll('button[aria-label]')].find(x=>{"
        "   const a=(x.getAttribute('aria-label')||'').toLowerCase();"
        "   return a.includes('options for')&&a.includes('comment')&&a.includes(name);});"
        "  if(b){b.scrollIntoView({block:'center'});b.click();return true;}}}"
        "return false;")
    del_item = ("const el=[...document.querySelectorAll('[role=menuitem],button,div,span,li')]"
                ".find(e=>/^delete\\b/i.test((e.innerText||'').trim())&&(e.innerText||'').trim().length<20);"
                "if(el){(el.closest('[role=menuitem]')||el).click();return true;}return false;")
    confirm = ("const d=[...document.querySelectorAll('[role=dialog],.artdeco-modal')];"
               "for(const m of d){const b=[...m.querySelectorAll('button')]"
               ".find(x=>((x.innerText||'').trim().toLowerCase()==='delete'));"
               "if(b){b.click();return true;}}return false;")
    try:
        for pid in POST_IDS:
            url = get_post_url_from_log_for_user(USER_ID, pid)
            if not url:
                continue
            driver.get(url)
            time.sleep(random.uniform(4, 7))
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(2)
            deleted = 0
            for _ in range(3):
                if not driver.execute_script(del_js, name, list(OLD_PREFIXES)):
                    break
                time.sleep(random.uniform(1, 2))
                if not driver.execute_script(del_item):
                    break
                time.sleep(random.uniform(1, 2))
                driver.execute_script(confirm)
                wait_for_ajax(driver)
                time.sleep(random.uniform(2, 3))
                deleted += 1
            myprint(f"[post {pid}] deleted {deleted} old comment(s)")
    finally:
        quit_gracefully(driver)


def main():
    from cqc_lem.utilities.logger import myprint
    myprint(f"Reseed backfill | user={USER_ID} posts={POST_IDS} MODE={MODE}")
    if MODE == "delete_old":
        _delete_old()
    else:
        _api_post()  # report / api_post
    myprint("DONE")


if __name__ == "__main__":
    main()
