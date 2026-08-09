"""The SDUI comment/reply composer, and reading the thread it posts into (#1154).

Lifted VERBATIM out of `app/run_automation.py`. Every engagement cluster types into one of these
boxes, so this is where the mechanics live now — no Celery, no task, no policy.

Three invariants are load-bearing here, and each was paid for in production:

* **Success is the OUTCOME being present, never a click having landed** (#1013). The composer has no
  `<form>`, so `_composer_submitted` asks whether the box emptied or the text now shows in the
  neighbouring comment list — the old "text still in the body" check false-positived on a full
  composer and comments silently never posted.
* **Every composer lookup is scoped to its OWN comment.** A page-wide `role=textbox` lookup returns
  the first VISIBLE box in DOM order, which is the post's main "Add a comment" field — so the reply
  posted as a standalone comment (#478), and #478's own fix only PENALISED that box, letting it win
  when it was the only candidate (#886). `_reply_composer_for_comment` takes a box inside the
  comment's subtree, rejects anything above the comment OUTRIGHT, rejects a box owned by a different
  comment, and returns None rather than borrowing one.
* **The sticky global nav steals a click from an unfocused composer** (#815). `_focus_composer`
  centres before clicking; a JS click would dodge the nav but would equally dodge a real modal.

A miss is an expected no-op and logs DEBUG — `_reply_composer_for_comment` owns that logging for
both reply paths (#886), so its callers must not warn again.

The names keep their leading underscore: they moved verbatim, so a reader grepping either module
finds one spelling, and the test patches that follow them are a pure module-path change.
"""

import random
import time

from selenium.common import ElementClickInterceptedException
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from cqc_lem.utilities.linkedin_formatter import strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.selenium_util import find_all_first

# The SDUI comment/reply composer has NO <form> ancestor, so walk up from the textbox and click
# the enabled submit button whose text is Comment/Post/Reply — excluding the aria-label
# Comment/Reply buttons that OPEN a composer. Returns True if a button was clicked.
_SUBMIT_NEAR_COMPOSER_JS = (
    "let root=arguments[0]; for(let i=0;i<7 && root.parentElement;i++) root=root.parentElement;"
    "const b=[...root.querySelectorAll('button')].find(x=>!x.disabled && x.offsetParent!==null &&"
    "['comment','post','reply'].includes((x.innerText||'').trim().toLowerCase()) &&"
    "!['comment','reply'].includes((x.getAttribute('aria-label')||'').toLowerCase()));"
    "if(b){b.click(); return true;} return false;")


def _composer_submitted(driver, composer, text: str) -> bool:
    """True only if the text actually posted: the composer cleared (or detached), or the text now
    shows in the nearby comment list — NOT merely still sitting in a full composer (the old
    'text in body' check false-positived on that, so comments silently never posted).
    """
    try:
        if (composer.text or "").strip() == "":
            return True
    except Exception:
        return True  # composer detached/re-rendered after posting
    try:
        return bool(driver.execute_script(
            "let r=arguments[0]; for(let i=0;i<9 && r.parentElement;i++) r=r.parentElement;"
            "const cl=r.querySelector(\"[data-testid*='-commentList']\");"
            "return cl ? cl.innerText.includes(arguments[1]) : false;", composer, text[:25]))
    except Exception:
        return False


def _scroll_into_center(driver, element) -> None:
    """Best-effort: park `element` in the MIDDLE of the viewport. Positioning is never fatal on its
    own, so a failure here is swallowed and left to the click that follows.
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass  # a stale element or a rejected scroll is not a failure — the click below decides


def _focus_composer(driver, composer) -> None:
    """Click into a comment/reply composer, centered first.

    LinkedIn's global nav is STICKY, so whatever the previous action on the card left on screen can
    leave the composer pinned to the very top of the viewport — the nav's own <svg> then receives
    the click and Chrome raises ElementClickInterceptedException at y≈9 (issue #815). Centering is
    the actual fix; a JS click would also dodge the nav but would equally dodge a genuine modal or
    overlay, so the one retry re-centers and clicks for real and a second interception is allowed
    to raise (the caller names the step it died on).
    """
    _scroll_into_center(driver, composer)
    try:
        composer.click()
    except ElementClickInterceptedException:
        _scroll_into_center(driver, composer)
        composer.click()


# How far ABOVE a comment's own top edge a composer may still start and count as its reply box.
# Only absorbs sub-pixel/rounding drift — a real reply box opens below the comment, never above it.
_COMPOSER_ABOVE_SLACK_PX = 8


def _visible_rect(element: WebElement) -> dict | None:
    """Page-coordinate rect of a RENDERED element, else None. Zero-size IS the hidden case here —
    the same width>0 && height>0 test #478 applies to composer candidates.
    """
    try:
        r = element.rect or {}
    except Exception:
        return None  # stale/detached element is not a candidate
    if not r.get("width") or not r.get("height"):
        return None
    return r


def _visible_composers(root: WebDriver | WebElement) -> list[tuple[WebElement, dict]]:
    """`(element, rect)` for every rendered role=textbox under `root` — a WebElement to search one
    comment's subtree, or the driver to search the page.
    """
    found = []
    try:
        for box in root.find_elements(By.CSS_SELECTOR, "div[role='textbox']"):
            rect = _visible_rect(box)
            if rect:
                found.append((box, rect))
    except Exception:
        pass  # a stale root has no candidates; the caller skips
    return found


def _in_same_comment(driver: WebDriver, comment_el: WebElement, other: WebElement | None) -> bool:
    """True when `other` is this comment or shares its subtree (a reply wrapper inside it, or a
    wrapper holding it) — i.e. the composer that resolved to it is ours to type into.
    """
    if other is None:
        return False
    if other == comment_el:
        return True
    try:
        return bool(driver.execute_script(
            "return arguments[0].contains(arguments[1]) || arguments[1].contains(arguments[0]);",
            comment_el, other))
    except Exception:
        return False


def _reply_composer_for_comment(driver: WebDriver, comment_el: WebElement,
                                user_id: int = None) -> WebElement | None:
    """The reply composer belonging to THIS comment — never a page-wide first match.

    A document-wide role=textbox lookup returns the first VISIBLE composer in DOM order, so the reply
    was typed into the post's main 'Add a comment' box (it posts as a standalone comment) or into one
    left mounted by a comment replied to earlier in the same sweep. Same bug class as #478 on the
    other reply path and #876 on the post card; this is issue #883.

    Two rules, in order. A composer inside the comment's own subtree is unambiguous — LinkedIn nests
    a comment's replies, and the box that opens at the end of them, in the comment container. If this
    render puts it outside, fall back to #478's geometry: the visible composer NEAREST the comment's
    bottom edge, with anything above the comment rejected OUTRIGHT — that hard above-filter is what
    keeps the post's main box out, where #478 merely penalises it and still hands it back when it is
    the only candidate — and with a box that resolves to a DIFFERENT comment rejected too. No
    candidate means skip; we never borrow a composer.
    """
    anchor = _visible_rect(comment_el)
    if anchor is None:
        # The callers now rely on THIS function to log every miss (#886 dropped their own warning),
        # so a stale/unrendered comment must not return None silently.
        log_debug("Comment is not rendered; no reply composer to resolve",
                  action_type="reply", user_id=user_id)
        return None
    bottom = anchor["y"] + anchor["height"]
    nested = _visible_composers(comment_el)
    candidates = nested or [(box, rect) for box, rect in _visible_composers(driver)
                            if rect["y"] >= anchor["y"] - _COMPOSER_ABOVE_SLACK_PX]
    best = min(candidates, key=lambda br: abs(br[1]["y"] - bottom), default=None)
    if best is None:
        log_debug("No reply composer belongs to this comment", action_type="reply", user_id=user_id)
        return None
    if nested:
        return best[0]
    # Sibling render: reject a box that resolves to a DIFFERENT comment — the nearest box below can
    # belong to a LATER comment when our own reply box never opened, and borrowing it answers the
    # wrong person. An UNRESOLVED owner is not proof of that: `_comment_container` was written for a
    # comment BODY (`expandable-text-box`) and rejects any ancestor holding a GIF/Emoji composer
    # button, which is the composer's OWN toolbar here — requiring it to resolve would make this
    # branch skip every time, silently, whenever LinkedIn renders the reply box outside the comment.
    # Unresolved therefore falls through to #478's proven geometry, still under the hard above-filter
    # that is what actually keeps the post's main comment box out.
    owner = _comment_container(driver, best[0])
    if owner is not None and not _in_same_comment(driver, comment_el, owner):
        log_debug("Nearest reply composer belongs to another comment", action_type="reply", user_id=user_id)
        return None
    return best[0]


def _type_and_submit_reply(driver: WebDriver, composer: WebElement, reply_text: str,
                           user_id: int = None) -> bool:
    """Type into an ALREADY-resolved composer and submit (role=textbox + Ctrl+Enter fallback). Both
    reply paths share this so the submit/verify contract can never drift between them. True only when
    `_composer_submitted` confirms the post.
    """
    # `run_automation._strip_non_bmp` was an alias for exactly this, kept only as a patch seam
    # (#1154); reading the original directly is what makes a stale patch of the alias fail loudly.
    reply_text = strip_non_bmp(reply_text)
    if not reply_text.strip():
        return False
    _focus_composer(driver, composer)  # sticky nav steals a top-of-viewport click (#815)
    composer.send_keys(reply_text)
    time.sleep(random.uniform(1, 2))
    if not driver.execute_script(_SUBMIT_NEAR_COMPOSER_JS, composer):
        composer.send_keys(Keys.CONTROL, Keys.RETURN)  # fallback
    time.sleep(random.uniform(3, 5))
    return _composer_submitted(driver, composer, reply_text)


def _comment_items_from_thread(driver):
    """Comment items on the SDUI thread — walk up from each Reply button to the container that
    also holds the author link + text (comments are no longer <article> elements).
    """
    items = []
    reply_btns = find_all_first(driver, [
        (By.CSS_SELECTOR, "[data-testid*='-commentList'] button[aria-label='Reply']"),
        (By.CSS_SELECTOR, "button[aria-label='Reply']")])
    for rb in reply_btns:
        item = driver.execute_script(
            "let el=arguments[0],d=0;while(el&&d<8){"
            "if(el.querySelector&&el.querySelector(\"a[href*='/in/']\"))return el;"
            "el=el.parentElement;d++;}return arguments[0].parentElement;", rb)
        if item is not None:
            items.append(item)
    return items


# SDUI comment thread (validated live 2026-07-24 on a moderated group post, issue #478):
#   * comments render as [data-testid='expandable-text-box'] INSIDE [data-testid*='commentList']
#     — but ONLY once scrolled into view (a long post pushes them far below the fold);
#   * a comment's author is the header /in/ link that is NOT inside the text box (an @mention in a
#     reply body is also an /in/ link — that was the false "mine" match);
#   * replies are nested inside their parent comment's container (DOM containment);
#   * the like control is a button whose aria-label starts "React " (e.g. "React Like"); the reply
#     control is aria-label="Reply". "…more" truncates long replies until expanded.
_COMMENTLIST_TEXTBOX = "[data-testid*='commentList'] [data-testid='expandable-text-box']"


def _comment_header_author(driver, container) -> str:
    """A comment's author profile href from its HEADER link — never an @mention inside the body
    text box (that false match flagged a reply that mentioned us as 'ours').
    """
    try:
        return driver.execute_script(
            "const c=arguments[0];"
            "for(const a of c.querySelectorAll(\"a[href*='/in/']\")){"
            "  if(!a.closest(\"[data-testid='expandable-text-box']\")) return (a.href||'').split('?')[0];"
            "}return '';", container) or ""
    except Exception:
        return ""


def _comment_container(driver, textbox):
    """Smallest ancestor of a comment text box that carries a HEADER author link and is not the
    post wrapper (which uniquely has the GIF/Repost/Emoji composer buttons).
    """
    try:
        return driver.execute_script(
            "let el=arguments[0],d=0;while(el&&d<10){"
            " const hdr=[...el.querySelectorAll(\"a[href*='/in/']\")].some(a=>!a.closest(\"[data-testid='expandable-text-box']\"));"
            " const post=[...el.querySelectorAll('button')].some(b=>/GIF|Repost|Emoji Picker/.test(b.getAttribute('aria-label')||''));"
            " if(hdr&&!post) return el; el=el.parentElement;d++;}return null;", textbox)
    except Exception:
        return None


def _reply_under_comment_inline(driver, wait, comment_el, reply_text: str, user_id: int = None) -> bool:
    """Reply UNDER a specific comment — NOT as a new top-level comment. The bug: clicking a comment's
    Reply then taking the first page-wide role=textbox grabbed the post's main 'Add a comment' box, so
    the reply posted as a standalone comment (#478).

    #478's own fix only PENALISED a composer above the comment, so the main box still won when it was
    the only visible one — the exact failure this function exists to prevent (#886). Composer
    resolution is now `_reply_composer_for_comment`, shared with `_reply_to_comment_inline` (#883):
    a box inside this comment wins, a box above it is rejected outright, a box owned by a DIFFERENT
    comment is rejected, and no box of ours means skip. This function keeps only its own way of
    OPENING the box — the #478 thread path needs the scroll + hover that renders a hover-hidden Reply
    button before it can be clicked.
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comment_el)
        try:
            ActionChains(driver).move_to_element(comment_el).pause(0.5).perform()  # reveal action bar
        except Exception:
            pass  # hover is best-effort; the Reply button lookup below still runs
        rbtns = comment_el.find_elements(By.CSS_SELECTOR, "button[aria-label='Reply']")
        if not rbtns:
            log_warning("Reply-under-comment: no Reply button found", action_type="reply", user_id=user_id)
            return False
        try:
            ActionChains(driver).move_to_element(rbtns[0]).pause(0.2).click(rbtns[0]).perform()
        except Exception:
            driver.execute_script("arguments[0].click();", rbtns[0])
        time.sleep(random.uniform(1.5, 2.8))
        composer = _reply_composer_for_comment(driver, comment_el, user_id=user_id)
        if composer is None:
            return False  # expected no-op (the box never opened) — `_reply_composer_for_comment` logs it DEBUG
        return _type_and_submit_reply(driver, composer, reply_text, user_id=user_id)
    except Exception as e:
        log_warning("Reply-under-comment failed", exc=e, action_type="reply", user_id=user_id)
        return False


def _comment_items(driver) -> list:
    """[(text_box, container, author_href)] for every comment/reply currently rendered in the
    thread. Text boxes with no resolvable container are dropped — a comment we can't scope to a
    container has no author and no action bar, so it is not addressable.
    """
    items = []
    for tb in driver.find_elements(By.CSS_SELECTOR, _COMMENTLIST_TEXTBOX):
        cont = _comment_container(driver, tb)
        if cont is None:
            continue
        items.append((tb, cont, _comment_header_author(driver, cont)))
    return items
