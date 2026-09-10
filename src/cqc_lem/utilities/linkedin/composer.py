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
from typing import NamedTuple, Optional

from selenium.common import ElementClickInterceptedException
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from cqc_lem.utilities.linkedin.helper import clean_person_name, connection_degree
from cqc_lem.utilities.linkedin_formatter import strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_warning

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


# The reply control is the ONE thing the page renders exactly once per comment, in BOTH DOM
# generations, so it is what both readers below anchor on (issue #2020). Prefix-matched, never
# exact: LinkedIn now scopes the label to the comment's author ("Reply to Matthew B.'s comment"),
# and the exact match that used to work returns zero. Live 2026-09-10 on a post whose own page said
# "4 comments": exact 0, prefix 4. `[role='button']` is carried alongside `button` because the
# reply control is not guaranteed to be a real button element on either generation.
# The SDUI comment list, as grounded 2026-07-24. Defined here rather than beside the rest of the
# SDUI notes below because `_COMMENT_ANCHOR_LADDER` names it as a rung and Python reads a module
# top to bottom. Returned zero on 2026-09-10; kept as a rung, never as the only way in (#2020).
_COMMENTLIST_TEXTBOX = "[data-testid*='commentList'] [data-testid='expandable-text-box']"

_COMMENT_REPLY_CONTROLS = ("main button[aria-label^='Reply'], main [role='button'][aria-label^='Reply'], "
                           "button[aria-label^='Reply'], [role='button'][aria-label^='Reply']")
# The same control, looked up INSIDE one comment. Kept separate because the page-wide form above is
# `main`-scoped first and a `find_elements` on an element ignores that prefix — and because the
# clicks that use this one are already scoped to the comment they mean, which is what keeps #1012's
# rule ("never click a control whose label names a different entity than the target") satisfied even
# though the label now names an entity.
_COMMENT_REPLY_CONTROL_SCOPED = "button[aria-label^='Reply'], [role='button'][aria-label^='Reply']"

# Walk up from a reply control to the comment it belongs to: the nearest ancestor that carries a
# profile link and is not the POST wrapper (which uniquely holds the GIF/Repost/Emoji composer
# controls). Structural on purpose — it resolved the same container on the testid-era DOM and on the
# class-era DOM LinkedIn served on 2026-09-10, where `data-testid` had vanished from the thread
# entirely (`main [data-testid]` matched nothing) and the container was back to being an <article>.
# Never keys on a class name: `docs/sdui-selenium-notes.md`.
_COMMENT_FROM_REPLY_JS = (
    "let el=arguments[0].parentElement,d=0;"
    "while(el&&d<10){"
    " if(el.querySelector&&el.querySelector(\"a[href*='/in/']\")){"
    "   const post=[...el.querySelectorAll('button')].some("
    "     b=>/GIF|Repost|Emoji Picker/.test(b.getAttribute('aria-label')||''));"
    "   if(!post) return el;"
    " }"
    " el=el.parentElement;d++;}"
    "return null;")


# The ladder (issue #2020). LinkedIn does not serve ONE DOM: what a page renders varies by day, by
# viewer, by the connection degree between the viewer and the author, and by the route taken to the
# page. A single selector — however carefully live-grounded — is therefore a snapshot of one
# rendering, and every silent outage in this repo's history (#964, #1009, #1774, #2020) is the same
# story: the one anchor stopped matching and the walk reported "nothing here" instead of "I no
# longer know how to look".
#
# So comments are found by an ORDERED CHAIN of independent strategies. Each rung is a way of
# recognising a comment that does not depend on the rungs above it; the first rung that yields
# anything wins, and the reading names which one answered. Only when EVERY rung has been tried and
# come back empty is the answer "no comments" — and that is the answer the zero-walk tripwire then
# grades against the page's own count.
#
# Naming the winning rung is half the value: a rung that stops answering is visible in the funnel
# long before it becomes an outage, which is exactly the warning none of the four issues above got.
#
# Ordered cheapest-and-most-current first. ADD to this list rather than replacing it — a rung that
# looks dead today is a rendering LinkedIn may serve again tomorrow, and keeping it costs one
# `find_elements` on a page that does not use it.
_COMMENT_ANCHOR_LADDER = (
    # 2026-09-10: the entity-scoped reply control ("Reply to Matthew B.'s comment"). Live-measured
    # 4 hits on a post whose own page said "4 comments", where the exact-label rung returned 0.
    ("reply_prefix", _COMMENT_REPLY_CONTROLS),
    # 2026-07-24: the bare reply control, before the label carried the author's name.
    ("reply_exact", "button[aria-label='Reply'], [role='button'][aria-label='Reply']"),
    # The SDUI generation's comment list. Returned 0 on 2026-09-10 — `main [data-testid]` matched
    # nothing at all on that rendering — and is kept because it is what some renderings still serve.
    ("commentlist_testid", _COMMENTLIST_TEXTBOX),
    # Purely structural, and the rung that survives a vocabulary change: a comment is an <article>
    # carrying somebody's profile link. This is what the page reverted TO on 2026-09-10.
    ("article", "main article"),
)


def _comment_containers(driver, ladder=_COMMENT_ANCHOR_LADDER) -> tuple:
    """`(containers, rung)` — the rendered comments, and WHICH strategy found them.

    The ONE walk both readers share (issue #2020). They used to disagree about how to find a
    comment — one walked up from the reply control, the other keyed on the comment list's testid —
    so they could go blind independently, and on 2026-09-10 both had.

    `rung` is `""` when every strategy came back empty. That is the only reading that means "no
    comments", and it is deliberately indistinguishable at this level from "the page changed again":
    telling those apart is the caller's job, by asking the PAGE (`zero_walk`), never this function's.
    """
    for name, selector in ladder:
        seen, items = [], []
        try:
            anchors = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for anchor in anchors:
            try:
                cont = driver.execute_script(_COMMENT_FROM_REPLY_JS, anchor)
            except Exception:
                continue
            if cont is None or any(cont == s for s in seen):
                continue
            seen.append(cont)
            items.append(cont)
        if items:
            return items, name
    return [], ""


def _comment_items_from_thread(driver):
    """Comment containers on the thread — see `_comment_containers`.

    Kept as a name because the reply sweep reads it; it is now the same walk `_comment_items` uses.
    Drops the rung, because this caller counts comments rather than diagnosing the page;
    `comment_containers_with_rung` is for callers that want to record which strategy answered.
    """
    return _comment_containers(driver)[0]


def comment_containers_with_rung(driver) -> tuple:
    """`(containers, rung)` for callers that record WHICH anchor strategy answered.

    Public because the value of a ladder is mostly in noticing a rung go quiet: a funnel that says
    `reply_prefix` every day and then says `article` has told you LinkedIn moved, weeks before the
    day no rung answers at all.
    """
    return _comment_containers(driver)


# SDUI comment thread (validated live 2026-07-24 on a moderated group post, issue #478):
#   * comments render as [data-testid='expandable-text-box'] INSIDE [data-testid*='commentList']
#     — but ONLY once scrolled into view (a long post pushes them far below the fold);
#   * a comment's author is the header /in/ link that is NOT inside the text box (an @mention in a
#     reply body is also an /in/ link — that was the false "mine" match);
#   * replies are nested inside their parent comment's container (DOM containment);
#   * the like control is a button whose aria-label starts "React "; so does the reply control.
#     Both are ENTITY-SCOPED as of 2026-09-10 ("React Like to Matthew B.'s comment", "Reply to
#     Matthew B.'s comment"), so both must be matched by PREFIX. The reply control's exact
#     `aria-label="Reply"` was the shape on 2026-07-24 and returned zero on 2026-09-10 (#2020).
#     "…more" truncates long replies until expanded.
#   * RE-GROUNDED 2026-09-10: the testid vocabulary this block describes is GONE on the post
#     permalink — `main [data-testid]` matched nothing at all, and the comment container was back to
#     `<article>`. Everything below that names a `data-testid` is kept only as a fallback for the
#     generation that still serves it; nothing may depend on it alone.


# The header-author read, as a named constant so the probe's carried copy can be held
# identical to it by a build guard (`TestCarriedLadderCopy`). See
# `_comment_header_author` for what each of the three rules is defending against.
_COMMENT_HEADER_AUTHOR_JS = (
    "const c=arguments[0];"
    "const own=(el)=>{let s='';for(const n of el.childNodes) if(n.nodeType===3) s+=n.textContent;"
    "                 return s.trim();};"
    "let body=null,bestLen=0;"
    "for(const el of c.querySelectorAll('*')){const len=own(el).length;"
    " if(len>bestLen){body=el;bestLen=len;}}"
    "let fallback='';"
    "for(const a of c.querySelectorAll(\"a[href*='/in/']\")){"
    "  if(a.closest(\"[data-testid='expandable-text-box']\")) continue;"
    "  if(body&&body.contains(a)&&body!==a) continue;"
    "  const href=(a.href||'').split('?')[0];"
    "  if(((a.innerText||a.textContent||'')+'').trim()) return href;"
    "  if(!fallback) fallback=href;"
    "}return fallback;")


def _comment_header_author(driver, container) -> str:
    """A comment's author profile href from its HEADER link — never an @mention inside the body.

    #1091 is the bug this exists to prevent: the avatar anchor (no text) and an @mention in a reply
    body are both `/in/` links, so a naive "first profile link" read named nobody, `upsert_engager`
    was skipped in silence, and `post_engagers` recorded nothing for a month.

    That fix excluded links inside `[data-testid='expandable-text-box']` — and on 2026-09-10 that
    testid was gone from the page entirely, which quietly turned the guard into a no-op and would
    have let #1091 back in behind a fixed reader. The rule is now expressed three ways, so no single
    vocabulary change disarms it (issue #2020): skip a link inside the testid'd box when the page
    still serves one, skip a link inside the element this comment's text lives in, and prefer a link
    that carries visible TEXT — an avatar anchor has none, and a header link always does.
    """
    try:
        return driver.execute_script(_COMMENT_HEADER_AUTHOR_JS, container) or ""
    except Exception:
        return ""


class CommentAuthor(NamedTuple):
    """Who wrote one comment: display name, profile URL, connection-degree badge.

    `name` is `''` when no anchor on the card carried name-like text — the reading is unusable for
    anything name-keyed (`post_engagers`, lead flagging), and the caller must treat it as a miss
    rather than as "nobody engaged". `profile_url` can still be set in that case, because the href
    survives on an anchor that renders no text at all.
    """

    name: str
    profile_url: str
    connection_degree: Optional[str]


_COMMENT_AUTHOR_ANCHORS_JS = (
    "const c=arguments[0],out=[];"
    "for(const a of c.querySelectorAll(\"a[href*='/in/']\")){"
    "  if(a.closest(\"[data-testid='expandable-text-box']\")) continue;"
    "  out.push({href:(a.href||'').split('?')[0],"
    "            text:((a.innerText||a.textContent||'')+'').trim(),"
    "            aria:(a.getAttribute('aria-label')||'').trim()});"
    "}return out;")


def comment_author_identity(driver, container) -> CommentAuthor:
    """Who wrote this comment — the ONE reader for a commenter's name+URL+degree (#1091).

    Taking `find_element("a[href*='/in/']")` off a comment card reads whichever /in/ anchor comes
    first in the DOM, and on the current SDUI card that is routinely NOT the name link: the avatar
    anchor renders no text, and an @mention inside the body is an /in/ link too. Both yield an empty
    `clean_person_name`, which silently skipped every `upsert_engager` — `post_engagers` recorded
    nothing for a month while replies on the same cards kept landing, because replying only needs the
    href.

    So this walks the card's HEADER anchors (the #478 grounding: an /in/ link NOT inside
    `[data-testid='expandable-text-box']`) and returns the first one carrying a name, falling back to
    the first header href so the slug half never regresses when nothing is named.
    """
    try:
        anchors = driver.execute_script(_COMMENT_AUTHOR_ANCHORS_JS, container) or []
    except Exception:
        anchors = []
    fallback_href = ""
    for anchor in anchors:
        if not isinstance(anchor, dict):
            continue
        href = str(anchor.get("href") or "")
        raw = str(anchor.get("text") or "") or str(anchor.get("aria") or "")
        fallback_href = fallback_href or href
        name = clean_person_name(raw)
        if name:
            return CommentAuthor(name, href or fallback_href, connection_degree(raw))
    return CommentAuthor("", fallback_href, None)


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
    OPENING the box — the scroll + hover that renders a hover-hidden Reply button before it can be
    clicked (the #478 thread path was fixed first; `_reply_to_comment_inline` needed the identical
    fix for the same reason, issue #1899).
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comment_el)
        try:
            ActionChains(driver).move_to_element(comment_el).pause(0.5).perform()  # reveal action bar
        except Exception:
            pass  # hover is best-effort; the Reply button lookup below still runs
        rbtns = comment_el.find_elements(By.CSS_SELECTOR, _COMMENT_REPLY_CONTROL_SCOPED)
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


# The comment BODY inside a resolved container: the element carrying the longest run of its OWN
# text. Structural because the body's vocabulary is exactly what keeps changing —
# `[data-testid='expandable-text-box']` on the 2026-07-24 rendering, a bare
# `<span>` under `update-components-text` on 2026-09-10 — while "the comment's text is the longest
# thing written in it" has been true of every rendering. Own text only: an ancestor inherits every
# descendant's text, so measuring `innerText` would always pick the container itself.
_COMMENT_BODY_JS = (
    "const c=arguments[0];let best=null,bestLen=0;"
    "const own=(el)=>{let s='';for(const n of el.childNodes) if(n.nodeType===3) s+=n.textContent;"
    "                 return s.trim();};"
    "for(const el of c.querySelectorAll('*')){"
    " const len=own(el).length;"
    " if(len>bestLen){best=el;bestLen=len;}}"
    "return best;")


def _comment_body(driver, container):
    """The element holding a comment's text, or the container itself when nothing stands out."""
    try:
        return driver.execute_script(_COMMENT_BODY_JS, container) or container
    except Exception:
        return container


def _comment_items(driver) -> list:
    """[(body, container, author_href)] for every comment/reply currently rendered in the thread.

    Rebuilt on the shared anchor ladder (issue #2020). It used to enter from
    `_COMMENTLIST_TEXTBOX` alone, which is one rendering's vocabulary — when LinkedIn stopped
    serving that rendering the list came back empty on a thread the page said held four comments,
    and nothing distinguished that from a post nobody had commented on.

    The first tuple slot is the comment BODY rather than a testid'd text box. Callers use it for its
    text and to scope a search, both of which the body element still satisfies; `_comment_container`
    remains for the one caller that starts from a text box it already has.
    """
    return [(_comment_body(driver, cont), cont, _comment_header_author(driver, cont))
            for cont in _comment_containers(driver)[0]]
