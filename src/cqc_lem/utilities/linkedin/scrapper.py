"""Reading a LinkedIn member profile out of a live Selenium session into plain dicts.

What comes out of here is not display data: it is dumped whole into the voice-synthesis prompt and
therefore grounds every comment and DM written for that user. A wrong read is not inert, which is why
`parse_profile_header` RAISES `ProfileUnavailableError` on a rate-limited / auth-walled / challenged
page instead of returning a thin profile — an empty section and an unavailable page must never look
the same to a caller.

Two parser generations live side by side. Experience (#970) was rebuilt on TEXT shapes — a date range
anchors an entity, and no shape means None rather than a guess. Education, certifications, awards and
skills still branch on `start_identifier_map`: the count of leading blank lines in an `<li>`, a
positional fingerprint of a pre-SDUI DOM that any added wrapper shifts. Prefer the #970 approach for
anything new here; see the long note above `_EXPERIENCE_ENTITY_SELECTORS`.
"""

import random
import re
from typing import List, Optional

from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, PageElement, ProcessingInstruction
from selenium import webdriver
from selenium.webdriver.common.by import By

from cqc_lem.utilities.date import convert_datetime_to_start_of_day, convert_viewed_on_to_date
from cqc_lem.utilities.linkedin.zero_walk import grade_zero_walk
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.selenium_util import (
    click_element_wait_retry,
    get_driver_wait,
    get_elements_as_list_wait_stale,
    getText,
    wait_for_ajax,
    window_scroll,
)

start_identifier_map = {
    "education": 19,
    "skills": 15,
    "endorsements": 19,
    "cert_name": 20,
    "cert_by": 26,
    "cert_on": 29,
    "cert_skills": 74,
    "cert_credential": 32,
    "recent_activity_number": 11,
    "recent_activity_text": 87

}


def source_as_row(s: PageElement) -> List[str]:
    """An element's text split on newlines, blank entries included.

    The blanks are the payload, not noise: `get_start_identifier` counts them to fingerprint which
    kind of row this is. Legacy — everything the #970 rebuild touched reads `visible_lines` instead,
    which returns what a reader actually sees.
    """
    return s.getText().split('\n')


def get_start_identifier(list_text: List[str]) -> int:
    """One less than the number of leading blank lines — the legacy row fingerprint.

    A row starting with content scores -1; two leading blanks score 1. Only exactly `''` and `' '`
    count as blank. The section parsers compare this against `start_identifier_map` to decide what a
    row IS, so the number is a positional accident of the markup: any wrapper LinkedIn adds or drops
    shifts it and the row is silently skipped or misread. That is what #970 replaced for experience,
    and why the live probe still reports this value — a run that shows no expected identifier is the
    evidence the legacy path is dead rather than the profile being empty.
    """
    startIdentifier = -1
    for e in list_text:
        if e == '' or e == ' ':
            startIdentifier += 1
        else:
            break
    return startIdentifier


def print_header(text):
    """Print to the console with 5 newlines before text and dashes before and after text to mark as header"""
    dashes = "-" * 10
    break_lines = "\n" * 5
    print(break_lines + dashes + text + dashes + "\n" * 2)


class ProfileUnavailableError(Exception):
    """Raised when a LinkedIn profile page can't be parsed (rate-limited, auth-wall,
    challenge, or a DOM change) — lets callers handle it gracefully instead of
    crashing with an opaque AttributeError on a None element.
    """


# Signatures of the non-profile pages LinkedIn serves at the same URL (rate-limit
# error page, guest auth-wall, login/checkpoint). Matched against visible body text.
_ERROR_PAGE_MARKERS = (
    "http error 429",
    "this page isn’t working",
    "this page isn't working",
    "too many requests",
    "join linkedin",
    "sign up | linkedin",
    "let’s sign you in",
    "security verification",
)


def _is_linkedin_error_page(page_text: str) -> bool:
    low = (page_text or "").lower()
    return any(marker in low for marker in _ERROR_PAGE_MARKERS)


def get_page_source(driver, url, scroll_times=0):
    """Soup of the page at `url`, navigating and settling it first.

    Navigation is skipped when the driver is already on `url`, so callers can chain reads of the same
    page without re-fetching it. `scroll_times` exists because LinkedIn lazy-loads: a details page
    read without scrolling returns only the entries that were above the fold.
    """
    if url != driver.current_url:
        # Open the profile URL
        driver.get(url)
        wait_for_ajax(driver)

    window_scroll(driver, scroll_times, True)

    return BeautifulSoup(driver.page_source, "html.parser")


_TITLE_PREFIX_RE = re.compile(r"^\(\d+\+?\)\s*")               # "(7) " unread-count prefix
_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*LinkedIn\s*$", re.IGNORECASE)


def _name_from_title(source) -> str:
    """Fallback name extraction from the page <title>.

    LinkedIn dropped the profile <h1> and moved to hashed/obfuscated CSS classes, but the
    member's name is still reliably in the title ("Name | LinkedIn", sometimes prefixed
    with a "(N)" unread-notification count). Guard against generic non-profile titles.
    """
    title = source.title.get_text(strip=True) if source and source.title else ""
    if not title:
        return ""
    name = _TITLE_SUFFIX_RE.sub("", _TITLE_PREFIX_RE.sub("", title)).strip()
    if not name or name.lower() in ("linkedin", "feed", "search", "messaging", "notifications"):
        return ""
    return name


# The degree badge as the SDUI page actually writes it: its own leaf node whose entire text is the
# degree. `span.dist-value` is a CLASS anchor and was confirmed dead on 2026-08-03, which made
# `LinkedInProfile.is_1st_connection` False for everybody — so every profile viewer read as a
# non-connection and the 1st-degree branch of engage_with_profile_viewer never ran (issue #1021).
_DEGREE_LEAF_RE = re.compile(r"^(?:·\s*)?(1st|2nd|3rd)\+?(?:\s+degree(?:\s+connection)?)?$",
                             re.IGNORECASE)
_PROFILE_SHELL_SELECTOR = "main a[href*='/in/'], a[href*='/recent-activity/']"


def _degree_from_source(source) -> str:
    """The degree badge from the page's own words.

    Leaf nodes only — an ancestor's text would sweep up the whole top card and match on any headline
    containing '1st'.

    Scoped to <main> and first-match-only, because a profile page renders OTHER people's badges as
    well: the "People also viewed" rail sits outside <main>, and mutual-connection highlights sit
    below the top card inside it. A badge that names a different entity must never decide THIS
    profile's degree — #1012's rule read backwards — and the top card is the first one in document
    order.

    No <main> means no scope, so it reads NO degree rather than falling back to the whole document:
    the name still resolves off the <title> there, so a rail badge would have decided the degree of
    a profile whose own card never rendered. Empty routes to the invite branch, which is the
    documented fail-open — a wrong '1st' routes to the DM/comment branch, which is not.
    """
    root = source.find('main')
    if root is None:
        return ""
    for element in root.find_all(['span', 'div', 'li', 'p']):
        if element.find(True) is not None:
            continue
        text = element.get_text(" ", strip=True)
        if _DEGREE_LEAF_RE.match(text):
            return text
    return ""


def parse_profile_header(source, profile_url, company_name=None) -> dict:
    """Extract name/title/connection from a parsed profile page (pure, no Selenium).

    Raises ProfileUnavailableError on rate-limit / auth-wall / error pages or when the
    name can't be located, so callers handle it instead of crashing on a None element.
    """
    page_text = source.get_text(" ", strip=True)[:400] if source else ""
    if not source or _is_linkedin_error_page(page_text):
        raise ProfileUnavailableError(
            f"Profile page unavailable (rate-limited/auth-wall/challenge) for {profile_url}: {page_text[:160]!r}")

    # Header container class changes often; fall back to the whole document so a class
    # rename doesn't break extraction. Then guard every lookup.
    info = source.find('div', class_='mt2 relative') or source

    # LinkedIn removed the profile <h1> and uses hashed CSS classes now, so fall back to
    # the page <title> (which still carries the name) before giving up.
    name_el = info.find('h1') or source.find('h1')
    full_name = name_el.get_text().strip() if name_el is not None else _name_from_title(source)
    if not full_name:
        # A page with no <h1> and no usable <title> is either a shell that never rendered or a
        # profile whose name anchors rotated, and those need opposite responses. The profile links
        # the page renders are the cross-check — neither the <h1> nor the <title> read touches
        # them — so the answer is graded rather than guessed (issue #1021).
        verdict = grade_zero_walk(len(source.select(_PROFILE_SHELL_SELECTOR)),
                                  "Profile-name read", action_type="profile_scrape")
        raise ProfileUnavailableError(
            f"Could not locate profile name (DOM changed?) for {profile_url} [{verdict}]")

    title_el = info.find('div', class_='text-body-medium') or source.select_one('div.text-body-medium')
    connection_el = info.find('span', class_='dist-value') or source.find('span', class_='dist-value')
    connection = (connection_el.get_text().strip() if connection_el
                  else _degree_from_source(source))

    profile = {'full_name': full_name}
    if company_name:
        profile['company_name'] = company_name
    profile['job_title'] = title_el.get_text().lstrip().strip() if title_el else ""
    if connection:
        profile['connection'] = connection
    profile['profile_url'] = profile_url
    return profile


# returns LinkedIn profile information
def returnProfileInfo(driver: webdriver, profile_url, company_name=None, is_main_user=False):
    """Scrape one profile whole — header plus every details section — as a dict.

    The header is not optional: an unavailable page raises `ProfileUnavailableError` out of here
    before any section is visited, so a rate-limited scrape can never be mistaken for a sparse
    profile. Each SECTION, by contrast, is caught individually — one details page that fails to load
    leaves its key absent and the rest of the profile still returns.

    Sections are visited in a shuffled order on purpose: a fixed sequence of details-page hits is a
    bot fingerprint. `is_main_user` skips mutual connections, which are meaningless against oneself.

    Raises:
        ProfileUnavailableError: the page was an error/auth-wall page, or the name could not be read.
    """
    url = profile_url
    source = get_page_source(driver, url, 0)

    # Bail out clearly on rate-limit / auth-wall / error pages — parsing these used to
    # crash with "'NoneType' object has no attribute 'find'" and take down auto-commenting.
    profile = parse_profile_header(source, profile_url, company_name)

    # profile_li = source.find_all('li', class_='artdeco-list__item')

    # print_header("Profile Li(s)")
    # print(profile_li)
    # for x in profile_li:
    # alltext = source_as_row(x)
    # print(alltext)
    # si = get_start_identifier(alltext)
    # Print the start identifier and the first 20 characters of the row from the start identifier
    # print("Start Index: " + str(si), " | ", alltext[si][:20]) # For Debugging
    # print("Start Index: " + str(si), " | ", str(alltext))  # For Debugging

    functions = [
        ('education', lambda: get_profile_education(driver, profile_url)),
        ('experiences', lambda: get_profile_experiences(driver, profile_url)),
        ('certifications', lambda: get_profile_certifications(driver, profile_url)),
        ('skills', lambda: get_profile_skills(driver, profile_url)),
        ('recent_activities', lambda: get_profile_recent_activity(driver, profile_url)),
        ('awards', lambda: get_profile_awards(driver, profile_url)),
        ('interests', lambda: get_profile_interests(driver, profile_url)),
    ]

    # Add mutual_connections function if not is_main_user
    if not is_main_user:
        functions.append(('mutual_connections', lambda: get_mutual_connections(driver, profile_url)))

    # Shuffle the functions to make the execution order random
    random.shuffle(functions)

    # Call each function and add the result to the profile
    for key, func in functions:
        try:
            profile[key] = func()
        except Exception as e:
            print(f"Error getting: {key} | Exception: {e}")

    # print_header("Profile")
    # print(profile)
    # print_header("")

    return profile

    # Randomizing the function calls to appear natural and avoid detection
    random.shuffle(functions)

    for key, func in functions:
        # print("Calling function to get: ", key)
        try:
            profile[key] = func()
        except Exception as e:
            print("Error getting ", key, " | ", e)

    # print_header("Profile")
    # print(profile)
    # print_header("")

    return profile


def go_to_base_employee_link(driver, employee_link):
    """Land on the member's base profile URL, navigating only if we are not already there.

    Every `get_profile_*` below starts with this and then builds its details URL from
    `driver.current_url` rather than from `employee_link` — LinkedIn redirects vanity and legacy URLs
    to the canonical one, and appending "/details/experience/" to the pre-redirect URL yields a page
    that does not exist.
    """
    if employee_link != driver.current_url:
        # Open the profile URL
        driver.get(employee_link)
        wait_for_ajax(driver)
        # time.sleep(2)


def get_mutual_connections(driver, employee_link):
    """Names of the connections shared with this member, as shown on their facetNetwork search page.

    Both steps run with `max_retry=0`, so a missing link or an empty result RAISES rather than
    retrying — `returnProfileInfo` catches that per section and simply omits the key. Names come back
    exactly as rendered; the caller decides whether they need `clean_person_name`.
    """
    go_to_base_employee_link(driver, employee_link)

    wait = get_driver_wait(driver)

    # click the link for mutual connections
    click_element_wait_retry(driver, wait, "//a[contains(@href,'facetNetwork')]", "Finding Mutual Connections Link",
                             max_retry=0)

    # Get the text of the element that contains the connection's name
    mutual_connections = get_elements_as_list_wait_stale(wait,
                                                         "//div[contains(@class,'linked-area')]//span//a//span//span[1]",
                                                         "Getting Mutual Connection Names", max_retry=0)
    # Get the text from the elements
    mutual_connections = [getText(mc) for mc in mutual_connections]

    return mutual_connections


def get_profile_education(driver, employee_link):
    """Schools listed on the main profile page, as a list of strings.

    Deliberately narrow, and legacy on both counts: a row only qualifies if its blank-line
    fingerprint matches `start_identifier_map['education']` AND the first half of the line contains
    "university", "college" or "ba" as a word. Anything else — a bootcamp, a school named neither —
    is dropped silently, so an empty list here does NOT mean the member listed no education.
    """
    source = get_page_source(driver, employee_link)
    profile_education = []
    education = source.find_all('li')
    # print_header("Education")

    for e in education:
        row = source_as_row(e)
        si = get_start_identifier(row)
        # Print the start identifier and the first 20 characters of the row from the start identifier
        # print("Start Index: " + str(si), " | ", row[si][:40])
        # print("Start Index: " + str(si), " | ", str(row))
        if si == start_identifier_map['education']:
            text_find = ['university', 'college', 'ba']
            line = row[si][:len(row[si]) // 2]
            if any(word in line.lower().split(' ') for word in text_find):
                profile_education.append(line)
                # print_header('Education: ' + line)

    return profile_education


def get_profile_recent_activity(driver, employee_link):
    """Recent posts/comments as `{'text', 'link', 'posted'}` dicts, newest first as LinkedIn orders.

    `posted` is derived from the card's relative caption ("2d") and floored to the start of that day,
    so it is a DAY, never a moment — the recency filters downstream are day-granular for that reason.

    Text, links and dates come from three independent document-wide queries zipped by POSITION: they
    must return the same cards in the same order, and a surface that renders one of them for a card
    but not the others shifts every pairing after it. Zip also truncates to the shortest, so the
    count returned is the count of whichever query read fewest.
    """
    go_to_base_employee_link(driver, employee_link)
    url = driver.current_url.rstrip('/') + '/recent-activity/all/'
    driver.get(url)
    wait_for_ajax(driver)

    source = get_page_source(driver, url, 2)
    # activities = source.find_all('li')
    # Find all the links that have 'activity' in the url
    links = source.find_all('div', attrs={'data-urn': re.compile('activity')})
    found_links = ['https://www.linkedin.com/feed/update/' + link.get('data-urn') for link in links]
    texts = source.find_all('div', class_='update-components-text')
    found_text = [text.getText().strip() for text in texts]
    posted_dates = source.select(
        'div[class*="fie-impression-container"] div.relative span[class*="update-components-actor__sub-description"] span[aria-hidden="true"]')
    found_dates = [date.getText().strip() for date in posted_dates]

    # print_header("Recent Activity")
    # print("Found Links", found_links)
    # print("Found Test", found_text)

    # combine the profile activity and the found links into a mapped dict list
    profile_activity = [{'text': text,
                         'link': link,
                         'posted': convert_datetime_to_start_of_day(convert_viewed_on_to_date(date + " ago"))} for
                        text, link, date in zip(found_text, found_links, found_dates)]

    # print(f"Profile URL: {employee_link} | Recent Activity Links {profile_activity}")

    return profile_activity


# --- Experience parsing (issue #970) -------------------------------------------------------------
# The old parser branched on `start_identifier_map` — the count of leading blank strings in an <li>'s
# split text. That is a positional fingerprint of a pre-SDUI DOM: any wrapper LinkedIn adds or drops
# shifts every index, and the parser then emits confidently-wrong companies/titles rather than
# nothing. Profile data is dumped whole into the voice-synthesis prompt (`synthesize_profile`), so
# garbage here is not inert — it grounds every comment and DM written for that user.
#
# The rebuild keys on what SDUI still exposes: entity containers by `data-view-name`, the visible
# text half of LinkedIn's doubled a11y markup (`aria-hidden="true"` beside a `visually-hidden`
# twin — the thing the old `[:len//2]` halving hack was approximating), and TEXT shapes for the
# date range / skills lines. NO class names, and nothing positional.

# Ladder, most specific first. `role='listitem'` rungs are here because the catch-up grounding pass
# (2026-08-03) found LinkedIn's fully-SDUI screens render NO `data-view-name` and no `<li>` at all —
# so a ladder that stops at `li` would match nothing the day this page converts.
#
# Specificity ALONE picks the wrong rung. The live /details/experience/ grounding run (2026-08-03,
# PR #984) found `data-view-name` absent from the page entirely, `div[data-sdui-screen]
# div[role='listitem']` matching the FOOTER's three help links ("Questions? / Visit our Help
# Center."), and the real entries sitting in the 8 `main li` under `main div[role='list']`. So the
# rungs below are scoped to `main` first, and `experience_entity_nodes` additionally skips any rung
# whose nodes carry no date range — a page's chrome can out-specify its content, but it can never
# out-DATE it.
_EXPERIENCE_ENTITY_SELECTORS = (
    "main div[data-view-name='profile-component-entity']",
    "div[data-view-name='profile-component-entity']",
    "section[data-view-name] li",
    "main div[role='list'] li",
    "main div[role='list'] div[role='listitem']",
    "main li",
    "main div[role='listitem']",
    "div[data-sdui-screen] div[role='listitem']",
    "div[role='listitem']",
    "li",
)
# Probed INSIDE a chosen entity to tell a grouped company from a single role.
_NESTED_ENTITY_SELECTOR = ("div[data-view-name='profile-component-entity'], "
                           "div[role='listitem'], li")

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Sep|Sept|Aug|Oct|Nov|Dec)[a-z]*\.?"
_DATE_TOKEN = rf"(?:{_MONTH}\s+\d{{4}}|\d{{4}})"
# "Jan 2020 - Present · 5 yrs 2 mos" / "2019 - 2021" — the one line that reliably marks a role.
_DATE_RANGE_RE = re.compile(
    rf"^(?P<start>{_DATE_TOKEN})\s*(?:-|–|—|to)\s*(?P<end>Present|{_DATE_TOKEN})"
    r"(?:\s*·\s*.+)?$", re.IGNORECASE)
_DURATION_RE = re.compile(r"^\d+\s+yrs?(?:\s+\d+\s+mos?)?$|^\d+\s+mos?$", re.IGNORECASE)
_SKILLS_RE = re.compile(r"^skills?\s*:\s*(.+)$", re.IGNORECASE)
# "Skills:" alone on its line, with the names in the next block.
_SKILLS_LABEL_RE = re.compile(r"^skills?\s*:?$", re.IGNORECASE)
# The "+9 skills" overflow chip that trails the list — a count, not a skill.
_SKILL_OVERFLOW_RE = re.compile(r"^\+\s*\d+\s+skills?$", re.IGNORECASE)

_EMPLOYMENT_TYPES = frozenset({"full-time", "part-time", "self-employed", "freelance", "contract",
                               "internship", "apprenticeship", "seasonal", "permanent",
                               "temporary"})
_WORKPLACE_TYPES = frozenset({"on-site", "onsite", "hybrid", "remote"})
# Chrome/affordance text that renders inside the same entity and is never profile content.
_NOISE_LINES = frozenset({"follow", "connect", "message", "see more", "…see more", "...see more",
                          "see less", "…see less", "...see less", "show all", "·", "",
                          "helped me get this job", "current"})
# Company logo alt text renders as its own line ("Acme Corp logo"); a description
# sentence is never this short, so the length bound keeps real prose.
_LOGO_LINE_RE = re.compile(r"^(?:\S+ ){0,5}\S*\blogo$", re.IGNORECASE)
# Section headings render inside the same list as the entries — never a company name.
_SECTION_TITLE_LINES = frozenset({"experience", "experiences", "education", "positions",
                                  "licenses & certifications", "volunteering"})
# Tags that lay out on the SAME line as their neighbours; everything else breaks one.
_INLINE_TAGS = frozenset({"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "em",
                          "font", "i", "ins", "kbd", "label", "mark", "q", "s", "samp", "small",
                          "span", "strong", "sub", "sup", "time", "u", "var"})
_UNRENDERED_TAGS = frozenset({"head", "noscript", "script", "style", "svg", "template"})
# Markup with no tag name that a reader never sees. bs4 hands these back as NavigableString
# subclasses, so `str(child)` returns a comment's TEXT — and LinkedIn's SDUI pages are full of
# them. Reading one is the same fault as reading the visually-hidden a11y twin.
_UNRENDERED_STRINGS = (CData, Comment, Declaration, Doctype, ProcessingInstruction)
# The visible half of the doubled a11y markup is ~50% of the node's text. Decorative
# `aria-hidden` icons are a rounding error — below this share the attribute is not the
# doubling and the whole text is the better read.
_ARIA_HIDDEN_COVERAGE = 0.3
# How far up from a role node to look for the company that groups it.
_MAX_COMPANY_ANCESTORS = 6


def _clean_lines(raw: List[str]) -> List[str]:
    """Strip, drop chrome/blank lines, and collapse the a11y duplicate of each line.

    LinkedIn renders most text twice — a visible `aria-hidden="true"` node and a `visually-hidden`
    twin — so the same string arrives back-to-back. Collapsing ADJACENT duplicates only, never all
    duplicates: two roles legitimately share a title, and a repeated date range is real data.
    """
    out: List[str] = []
    for line in raw:
        line = " ".join((line or "").split())
        if line.lower() in _NOISE_LINES or _LOGO_LINE_RE.search(line):
            continue
        # A bullet/pipe/dash glyph rendered on its own is an icon, never content.
        if not any(char.isalnum() for char in line):
            continue
        if out and out[-1] == line:
            continue
        out.append(line)
    return out


def _rendered_lines(node: PageElement) -> List[str]:
    """One line per LAID-OUT line — inline runs joined, block elements broken.

    `get_text("\\n")` splits on every text node, which is not what a reader sees: LinkedIn renders
    "Mar 2019 - Present · 7 yrs 6 mos" as three inline spans, and splitting them shatters the date
    range that anchors the whole parse. Adjacent identical segments collapse here too — the a11y twin
    is a sibling span, so on the fallback path it would otherwise join as "Engineer Engineer".
    """
    lines: List[str] = []
    current: List[str] = []

    def flush() -> None:
        text = " ".join(" ".join(current).split())
        current.clear()
        if text:
            lines.append(text)

    def add(text: str) -> None:
        text = " ".join((text or "").split())
        if text and (not current or current[-1] != text):
            current.append(text)

    def walk(element: PageElement) -> None:
        for child in getattr(element, "children", []):
            name = getattr(child, "name", None)
            if name is None:
                if not isinstance(child, _UNRENDERED_STRINGS):
                    add(str(child))
                continue
            name = name.lower()
            if name in _UNRENDERED_TAGS:
                continue
            if name == "br":
                flush()
                continue
            if name in _INLINE_TAGS:
                walk(child)
                continue
            flush()
            walk(child)
            flush()

    walk(node)
    flush()
    return lines


def visible_lines(node: PageElement) -> List[str]:
    """The visible text of one entity, one line per rendered line.

    Prefers the `aria-hidden="true"` half of LinkedIn's doubled markup (outermost only, so a nested
    span is not counted twice) — but only when that half actually covers the node's text. The live
    /details/experience/ render carries no doubling at all, and reading a page like that through a
    stray decorative `aria-hidden` icon would return an entity's text as one icon's worth of it. Full
    text is the fallback, where `_clean_lines` still removes any duplication.
    """
    chosen: List[PageElement] = []
    chosen_ids = set()
    for el in node.find_all(attrs={"aria-hidden": "true"}):
        if any(id(parent) in chosen_ids for parent in el.parents):
            continue
        chosen_ids.add(id(el))
        chosen.append(el)

    raw: List[str] = []
    for el in chosen:
        raw.extend(_rendered_lines(el))
    whole = len(" ".join(node.get_text(" ").split()))
    if sum(len(line) for line in raw) < whole * _ARIA_HIDDEN_COVERAGE:
        raw = _rendered_lines(node)
    return _clean_lines(raw)


def _is_date_line(line: str) -> bool:
    return bool(_DATE_RANGE_RE.match(line))


def _has_date_range(lines: List[str]) -> bool:
    return any(_is_date_line(line) for line in lines)


def experience_entity_nodes(source) -> tuple:
    """(top-level entity nodes, the selector that found them).

    Entities nest — a multi-role company holds one entity per role — so descendants of an already
    chosen node are dropped, which is what makes the grouped-company shape parseable as one unit.

    A rung only WINS if at least one of its nodes carries a date range. Without that test the ladder
    is decided by selector specificity alone, and the live run behind this rebuild is exactly why
    that fails: `div[data-sdui-screen] div[role='listitem']` matched three footer help-links and beat
    the rung holding the actual roles. An undated rung is still returned when NO rung is dated, so
    the probe (and the warning path) can report what the page did render.
    """
    fallback: tuple = ([], "")
    for selector in _EXPERIENCE_ENTITY_SELECTORS:
        nodes = source.select(selector) if source else []
        if not nodes:
            continue
        top: List[PageElement] = []
        top_ids = set()
        for node in nodes:
            if any(id(parent) in top_ids for parent in node.parents):
                continue
            top_ids.add(id(node))
            top.append(node)
        if any(_has_date_range(visible_lines(node)) for node in top):
            return top, selector
        if not fallback[0]:
            fallback = (top, selector)
    return fallback


def _is_location_line(line: str) -> bool:
    """Only ever tested on lines BELOW the date range, where LinkedIn puts the location.

    Never above it: "Founder, CEO" is a title, and a comma-and-short heuristic applied to a title
    line would silently delete it.
    """
    tail = line.rsplit("·", 1)[-1].strip().lower()
    if tail in _WORKPLACE_TYPES:
        return True
    if len(line) > 80 or line.endswith((".", "!", "?", ":")):
        return False
    return "," in line and len(line.split()) <= 8


def _is_qualifier_line(line: str) -> bool:
    """Employment type / workplace type / bare duration — sits between a title and its dates, and
    is never the title itself.
    """
    low = line.lower()
    return (low in _EMPLOYMENT_TYPES or low in _WORKPLACE_TYPES
            or bool(_DURATION_RE.match(line)))


def _company_from_subtitle(line: str) -> str:
    """"Acme Corp · Full-time" -> "Acme Corp"; a bare employment type carries no company."""
    head = line.split("·")[0].strip()
    if not head or head.lower() in _EMPLOYMENT_TYPES:
        return ""
    return head


def _split_skills(blob: str) -> List[str]:
    """"Python · Kubernetes" and "Compliance, AI for Business, +9 skills" are both skill lists.

    The live 2026-08-03 render separates them with commas and ends on a "+9 skills" overflow chip;
    splitting on "·" alone turned the whole line into one nonsense skill.
    """
    parts = blob.split("·") if "·" in blob else blob.split(",")
    return [skill for skill in (part.strip() for part in parts)
            if skill and not _SKILL_OVERFLOW_RE.match(skill)]


def _details_and_skills(chunk: List[str]) -> tuple:
    """Description lines and the "Skills: A · B" line, from everything under a role's date line."""
    details: List[str] = []
    skills: List[str] = []
    started = False
    expecting_skills = False
    for line in chunk:
        if expecting_skills:
            expecting_skills = False
            skills.extend(_split_skills(line))
            continue
        matched = _SKILLS_RE.match(line)
        if matched:
            skills.extend(_split_skills(matched.group(1)))
            continue
        if _SKILLS_LABEL_RE.match(line):
            # The label and its names can land in separate blocks.
            expecting_skills = True
            continue
        # Location / employment-type qualifiers trail the date line; once real prose starts,
        # everything after it is description and is kept verbatim.
        if not started and (_is_qualifier_line(line) or _is_location_line(line)):
            continue
        started = True
        details.append(line)
    return details, skills


def _position(title: str, date_line: str, chunk: List[str]) -> dict:
    # Read the dates off the same match that identified the line, rather than re-splitting it —
    # the old `get_start_end_dates` only ever saw the positional parser's half-strings and turned a
    # plain "2016 - 2019" into today's date.
    matched = _DATE_RANGE_RE.match(date_line)
    start_date = matched.group("start").strip() if matched else None
    end_date = matched.group("end").strip() if matched else None
    details, skills = _details_and_skills(chunk)
    position = {"details": details, "skills": skills}
    if title:
        position["title"] = title
    if start_date:
        position["start_date"] = start_date
    if end_date:
        position["end_date"] = end_date
    return position


def _title_index(lines: List[str], date_index: int, floor: int) -> int:
    """Walk back from a date line over its qualifiers to the line that is actually the title."""
    i = date_index - 1
    while i > floor and _is_qualifier_line(lines[i]):
        i -= 1
    return i


def parse_experience_entity(lines: List[str], grouped: bool = False) -> Optional[dict]:
    """One entity's visible lines -> {'company_name', 'positions': [...]}, or None if it is not an
    experience entity at all.

    The date range is the anchor: no date line means this node is navigation, an empty section or a
    shape we do not recognise — and returning None there is the whole point, because the failure the
    old parser had was emitting a plausible-looking company/title from a row it had misread.

    `grouped` says the entity holds roles as child entities, which is the ONE thing the lines alone
    cannot tell you: a single-role entity reads title-then-company, a one-role group reads
    company-then-title, and the two are the same three lines in a different order.
    """
    date_indexes = [i for i, line in enumerate(lines) if _DATE_RANGE_RE.match(line)]
    if not lines or not date_indexes:
        return None

    if len(date_indexes) == 1 and not grouped:
        # Title first, company on a subtitle line beneath it ("Acme Corp · Full-time").
        date_index = date_indexes[0]
        title = lines[0] if date_index and not _is_qualifier_line(lines[0]) else ""
        company = ""
        for line in lines[1:date_index]:
            company = _company_from_subtitle(line)
            if company:
                break
        positions = [_position(title, lines[date_index], lines[date_index + 1:])]
    else:
        # Grouped company: company first, then one title/date block per role held there.
        # A date range is never a name — if it leads the entity there is no company header here.
        company = "" if _is_date_line(lines[0]) else _company_from_subtitle(lines[0])
        title_indexes = []
        for k, date_index in enumerate(date_indexes):
            floor = 0 if k == 0 else date_indexes[k - 1]
            title_indexes.append(_title_index(lines, date_index, floor))
        positions = []
        for k, date_index in enumerate(date_indexes):
            end = title_indexes[k + 1] if k + 1 < len(title_indexes) else len(lines)
            title_index = title_indexes[k]
            # The walk back stops at the previous role's date line when a role carries nothing but
            # qualifiers above its own dates. Emitting that line would put "Jan 2020 - Jan 2022"
            # in the title — a confidently-wrong row, which is the failure #970 exists to kill.
            title = ("" if title_index <= 0 or _is_date_line(lines[title_index])
                     else lines[title_index])
            positions.append(_position(title, lines[date_index], lines[date_index + 1:end]))

    positions = [p for p in positions if p.get("title") or p["details"] or p["skills"]]
    if not positions or (not company and not any(p.get("title") for p in positions)):
        return None
    return {"company_name": company, "positions": positions}


def _holds_child_roles(node: PageElement) -> bool:
    """True when this entity nests one child entity per role (the grouped-company render)."""
    for child in node.select(_NESTED_ENTITY_SELECTOR):
        if child is node:
            continue
        if any(_DATE_RANGE_RE.match(line) for line in visible_lines(child)):
            return True
    return False


def _company_header(lines: List[str]) -> str:
    """The company name from a grouped-company HEADER entity — a name and a total duration, no date
    range of its own ("Christopher Queen Consulting" / "9 yrs 6 mos").

    The duration is required: without it a bare heading like "Experience" would be read as a company
    and then attached to every role beneath it.
    """
    if not lines or _has_date_range(lines):
        return ""
    if not any(_DURATION_RE.match(line) for line in lines):
        return ""
    if _is_qualifier_line(lines[0]) or lines[0].lower() in _SECTION_TITLE_LINES:
        return ""
    return _company_from_subtitle(lines[0])


def _date_free_runs(lines: List[str]) -> List[List[str]]:
    """`lines` split on its date lines — one run per gap, in page order, empty runs kept.

    A date line is the one reliable role boundary, so the runs between them are exactly the places a
    company header can sit. The last run is always the text since the most recent role began.
    """
    runs: List[List[str]] = [[]]
    for line in lines:
        if _is_date_line(line):
            runs.append([])
        else:
            runs[-1].append(line)
    return runs


def _header_in_run(run: List[str]) -> str:
    """The company named by a header ANYWHERE inside one date-free run — the LAST one wins.

    `_company_header` reads a run that STARTS at the header, but a run cut out of a page starts
    wherever the previous role ended, so every suffix is tried. Scanning from the bottom returns the
    header nearest the role below it, which is the one that groups it.
    """
    for start in range(len(run) - 1, -1, -1):
        company = _company_header(run[start:])
        if company:
            return company
    return ""


def _company_for_leading(leading: List[str]) -> str:
    """The company that groups the role whose own lines begin right after `leading`.

    The rule is positional, because on the flat live shape (#1096) every role is a bare `li` sibling
    and the only thing distinguishing "still company A" from "company B starts here" is WHERE the
    header sits relative to the last date line:

    - A header in the run AFTER the last date line starts a NEW group — company B's roles must never
      inherit company A, which is the failure #970 exists to kill.
    - No header since the last role means this role is that role's sibling, so the group's own header
      (the last one above it) still applies. Requiring that run to be EMPTY — the shape #1096 was
      reproduced from — would blank every role whose predecessor carries a description, which is the
      live page itself.
    - Nothing header-shaped anywhere leaves the company blank. A blank is the safe answer.
    """
    runs = _date_free_runs(leading)
    company = _header_in_run(runs[-1])
    if company:
        return company
    for run in reversed(runs[:-1]):
        company = _header_in_run(run)
        if company:
            return company
    return ""


def _company_from_ancestors(node: PageElement, lines: List[str]) -> str:
    """The company a role belongs to when the role's OWN lines never name it.

    The grouped shape puts the company once, above its roles. When the ladder selects the ROLE nodes
    (on the live page they are the `li`s), that name is only in an ancestor's leading lines — the
    text above this role's first line. The nearest ancestor holding a header wins; one that holds
    none is climbed past, because on the live page the roles' shared `<ul>` carries no company at all
    and the header is a sibling of that list.
    """
    if not lines:
        return ""
    first = lines[0]
    for depth, ancestor in enumerate(node.parents):
        if depth >= _MAX_COMPANY_ANCESTORS or (getattr(ancestor, "name", "") or "").lower() in (
                "body", "html", "[document]"):
            break
        ancestor_lines = visible_lines(ancestor)
        if first not in ancestor_lines:
            continue
        company = _company_for_leading(ancestor_lines[:ancestor_lines.index(first)])
        if company:
            return company
    return ""


def parse_profile_experiences(source) -> List[dict]:
    """Pure parse of a rendered `/details/experience/` page — no Selenium, so it is unit-testable
    against captured DOM instead of only against a live session.
    """
    experiences = []
    nodes, _selector = experience_entity_nodes(source)
    pending_company = ""
    for node in nodes:
        lines = visible_lines(node)
        parsed = parse_experience_entity(lines, grouped=_holds_child_roles(node))
        if not parsed:
            # A company header is not an experience by itself, but it names the roles that follow it
            # when LinkedIn renders them as siblings rather than as children.
            pending_company = _company_header(lines) or pending_company
            continue
        if not parsed["company_name"]:
            parsed["company_name"] = _company_from_ancestors(node, lines) or pending_company
        experiences.append(parsed)
    return experiences


def get_profile_experiences(driver, employee_link) -> List[dict]:
    """Open `/details/experience/` and parse it — the Selenium half of the #970 rebuild.

    An empty result is ambiguous on its own, so this is where the two cases are told apart: a page
    with no date ranges anywhere is a profile with no experience (DEBUG), while a page that plainly
    renders dated entries and still parses to nothing is selector rot and WARNS. The cross-check
    reads the page through `_rendered_lines`, the same way the parser does, because a date range
    split across inline spans would otherwise make rot look like an empty profile.
    """
    go_to_base_employee_link(driver, employee_link)  # Link may need to redirect so we do this first
    url = driver.current_url.rstrip('/') + '/details/experience/'
    driver.get(url)
    wait_for_ajax(driver)

    source = get_page_source(driver, url, 2)
    profile_experiences = parse_profile_experiences(source)

    if not profile_experiences:
        # A profile with no experience section is normal — that is a DEBUG no-op. A page that
        # plainly renders dated entries and still parses to nothing is selector rot, and the point
        # of this issue is that it used to be invisible. Read the page the same way the parser does,
        # or a date range split across inline spans would make the rot look like an empty profile.
        if source is not None and _has_date_range(_rendered_lines(source)):
            log_warning("Profile experience page rendered dated entries but none parsed",
                        action_type="scrape_profile")
        else:
            log_debug("No experience entries on profile", action_type="scrape_profile")

    return profile_experiences


def get_profile_certifications(driver, employee_link):
    """Certifications from `/details/certifications/` as dicts; only `name` is ever guaranteed.

    Legacy positional parser: every field is read at a FIXED index of the row (`start_identifier_map`
    `cert_by` / `cert_on` / `cert_skills` / `cert_credential`) and halved to undo LinkedIn's doubled
    a11y text. Both assumptions are markup-shaped — a shifted index yields the wrong field rather
    than no field, which is the failure mode #970 rebuilt experience to escape. Read a live probe
    before trusting a change here.
    """
    go_to_base_employee_link(driver, employee_link)  # May need to redirect first

    url = driver.current_url.rstrip('/') + '/details/certifications/'
    driver.get(url)
    wait_for_ajax(driver)

    source = get_page_source(driver, url, 2)
    profile_certifications = []
    certs = source.find_all('li')
    # print_header("Certifications")
    for c in certs:
        row = source_as_row(c)
        # print(row)
        si = get_start_identifier(row)
        if si == start_identifier_map['cert_name']:
            # Reset vars
            company = None
            issued_on = None
            cert_skills = None
            credential_id = None

            name = row[si][:len(row[si]) // 2]

            cbi = start_identifier_map['cert_by']
            if cbi < len(row) and row[cbi]:
                company = row[cbi][:len(row[cbi]) // 2]

            ioi = start_identifier_map['cert_on']
            if ioi < len(row) and row[ioi]:
                issued_on = row[ioi][:len(row[ioi]) // 2]
                # Remove Issued from prefix
                issued_on = issued_on.replace("Issued ", "").strip()

            ski = start_identifier_map['cert_skills']
            if ski < len(row) and row[ski]:
                cert_skills = row[ski][:len(row[ski]) // 2]
                # remove Skills: from prefix
                cert_skills = cert_skills.replace("Skills: ", "").strip()
                cert_skills = cert_skills.split(' · ')

            cci = start_identifier_map['cert_credential']
            if cci < len(row) and row[cci]:
                credential_id = row[cci][:len(row[cci]) // 2]
                # Remove "Credential ID " from prefix
                credential_id = credential_id.replace("Credential ID ", "").strip()

            # Create a new certification dictionary and add it to the profile's certifications list.    '
            certification = {"name": name}
            if company:
                certification["company"] = company
            if issued_on:
                certification["issue_date"] = issued_on
            if cert_skills:
                certification["skills"] = cert_skills
            if credential_id:
                certification["credential_id"] = credential_id
            profile_certifications.append(certification)

    return profile_certifications


def get_profile_skills(driver, employee_link):
    """Skills from `/details/skills/` as `{'name'}` dicts, plus `endorsements` where a count showed.

    The name is halved because LinkedIn renders it twice inside the one anchor we match (the visible
    node and its `visually-hidden` twin). Endorsements are looked up per skill against a deliberately
    short wait: most skills have none, and the absence is expected, not a failure — the count key is
    simply left off.
    """
    go_to_base_employee_link(driver, employee_link)  # May need to redirect first

    # Skills
    url = driver.current_url.rstrip('/') + '/details/skills/'
    driver.get(url)
    wait_for_ajax(driver)
    window_scroll(driver, 5, True)

    wait = get_driver_wait(driver, 3)  # Significantly reduced wait time

    profile_skills = []

    skills = get_elements_as_list_wait_stale(wait, "//a[contains(@data-field,'skill')]", "Getting Skills", max_retry=0)

    # Get the text from all the skills
    for each_skill in skills:
        skill_name = getText(each_skill)
        # Remove all new lines from the skill name
        skill_name = skill_name.replace('\n', '')
        # Remove leading and trailing spaces
        skill_name = skill_name.strip()

        # Split the skill name in half because LI has 2 text elements in the one we find
        skill_name = skill_name[:len(skill_name) // 2]

        skill_dict = {"name": skill_name}

        try:
            # Use the parent element to find the child element looking for endorsement
            endorsement_element = wait.until(
                lambda d: each_skill.find_element(By.XPATH, ".//ancestor::li//span[contains(text(),'endorsement')][1]"),
                'Finding Endorsement Text')
        except Exception:
            # No endorsement element found
            endorsement_element = None

        if endorsement_element:
            endorse_text = getText(endorsement_element)
            # print(f"Endorsement Text: {endorse_text}")
            skill_dict["endorsements"] = int(re.search(r'\d+', endorse_text).group())

        profile_skills.append(skill_dict)

    return profile_skills


def get_profile_awards(driver, employee_link):
    """Honours from `/details/honors/` as `{'name'}` dicts — names only, nothing else is read.

    Incomplete by admission (it borrows the certifications fingerprint, `cert_name`, having none of
    its own) and it says so on an empty result. Treat `[]` as "not read", not as "no awards".
    """
    go_to_base_employee_link(driver, employee_link)  # May need to redirect first

    url = driver.current_url.rstrip('/') + '/details/honors/'
    driver.get(url)
    wait_for_ajax(driver)

    source = get_page_source(driver, url, 2)
    profile_awards = []
    items = source.find_all('li')
    for item in items:
        row = source_as_row(item)
        si = get_start_identifier(row)
        if si == start_identifier_map.get('cert_name') and row:
            name = row[si][:len(row[si]) // 2]
            if name:
                profile_awards.append({"name": name})

    if not profile_awards:
        log_info("Awards section not yet fully implemented — no honors found or section unavailable")

    return profile_awards


def get_profile_interests(driver, employee_link):
    """Interests from `/details/interests/` as `{'type', 'name'}` dicts.

    `type` is LinkedIn's own tab name (top voices, companies, groups, newsletters) off
    `data-member-interests-type`, or "unknown" on the class-name fallback path — that page carries
    several lists and the tab is the only thing separating them. Like awards, this section is
    incomplete and says so on an empty result, so `[]` is not evidence of no interests.
    """
    go_to_base_employee_link(driver, employee_link)  # May need to redirect first

    url = driver.current_url.rstrip('/') + '/details/interests/'
    driver.get(url)
    wait_for_ajax(driver)

    source = get_page_source(driver, url, 2)
    profile_interests = []

    # LinkedIn interests include top voices, companies, groups, and newsletters
    interest_sections = source.find_all('section', attrs={'data-member-interests-type': True})
    if not interest_sections:
        interest_sections = source.find_all('div', class_=lambda c: c and 'interests' in c.lower())

    for section in interest_sections:
        section_type = section.get('data-member-interests-type', 'unknown')
        items = section.find_all('span', attrs={'aria-hidden': 'true'})
        for item in items:
            text = item.get_text().strip()
            if text:
                profile_interests.append({"type": section_type, "name": text})

    if not profile_interests:
        log_info("Interests section not yet fully implemented — no interests found or section unavailable")

    return profile_interests


def record_search_word_frequency(row, si, search_words, search_word_frequency=None):
    """Tally where known words land in a row, keyed `'si:<start identifier>fi:<field index>'`.

    The instrument that produces `start_identifier_map`: run it over many rows with words you expect
    ("Issued", "Credential ID", …) and the keys that recur name the fingerprint and offset to hard
    code. Nothing in the runtime path calls it — it exists to re-derive those constants when a DOM
    change makes them wrong.

    Pass `search_word_frequency` back in to accumulate across rows; omitting it starts a fresh tally.
    Only the FIRST index matching a word is counted, so a word repeated in one row scores once.
    """
    if search_word_frequency is None:
        search_word_frequency = {}

    # if any of the search words are found in any of the row items record its index in the row to the search word frequency map
    for word in search_words:
        if any(word in item for item in row):
            # Find the index in the row where the word is found
            word_index = [i for i, item in enumerate(row) if word in item][0]
            key = 'si:' + str(si) + "fi:" + str(word_index)
            # Check if key is in search_word_frequency, if not add it
            if key not in search_word_frequency:
                search_word_frequency[key] = 0
            # Increase the word frequency by 1
            search_word_frequency[key] += 1
    return search_word_frequency

