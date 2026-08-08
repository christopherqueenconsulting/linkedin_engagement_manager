"""Zero-walk tripwires — the ONE place a zero-item read is graded (issues #1013, #1021).

A walk that returns zero items is ambiguous, and every silent SDUI outage of August 2026 lived in
that ambiguity: #964's catch-up scan logged `no_moments` daily while the feed showed ten moments,
and #1009's viewer walk engaged nobody for weeks. The fix is the same everywhere — before a zero is
treated as "nothing to do", ask the PAGE, through an anchor the walk itself does not depend on. The
independence is the whole point: cross-checking a chain against its own selector proves nothing,
since a rotated anchor answers zero to both questions.

Lives here rather than in `run_automation` because three trees need it and two of them
(`scrapper`, `company_page_inviter`) are imported BY the task module — a helper in the task module
would be a cycle. Only the logger is imported, so this module can be used from any of them.
"""

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from cqc_lem.utilities.logger import log_debug, log_warning

DRIFT = "drift"
EMPTY = "empty"
UNKNOWN = "unknown"


def page_native_count(driver, selector: str) -> "int | None":
    """How many of `selector` the page renders, or None when the read itself failed.

    None is load-bearing: "we could not ask the page" must never be recorded as "the page said zero".
    """
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, selector))
    except WebDriverException:
        return None


def zero_walk_verdict(page_native: "int | None") -> str:
    """What a zero-item walk means, given the page's own count of an INDEPENDENT anchor.

    'drift'   — the page renders items the walk could not see. A real defect.
    'empty'   — the page renders none either. An ordinary quiet day, and a no-op.
    'unknown' — the cross-check itself could not be read. Grounds nothing, so never a defect.
    """
    if page_native is None:
        return UNKNOWN
    return DRIFT if page_native > 0 else EMPTY


def grade_zero_walk(page_native: "int | None", what: str, **context) -> str:
    """Grade a zero-item walk against an already-taken page-native count, and log at the level the answer deserves.

    Drift is a WARNING on purpose — once is a warning, repeatedly is a defect, and repeated selector
    rot is exactly the defect that should file itself. An empty page and an unreadable cross-check
    are DEBUG: warning on either would file an issue for a quiet day (see utilities/CLAUDE.md).
    """
    verdict = zero_walk_verdict(page_native)
    if verdict == DRIFT:
        log_warning(f"{what} matched nothing while the page still renders cards — selector drift",
                    **context)
    else:
        log_debug(f"{what} matched nothing and the page shows none either ({verdict})", **context)
    return verdict


def report_zero_walk(driver, selector: str, what: str, **context) -> str:
    """Cross-check a zero-item walk against a CSS anchor the walk does not use, and log it."""
    return grade_zero_walk(page_native_count(driver, selector), what, **context)
