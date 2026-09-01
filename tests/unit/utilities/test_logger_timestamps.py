"""Every level's line carries a UTC timestamp, and the level prefixes are untouched (issue #1839).

The clock is INJECTED — `LogRecord.created` is overwritten with a fixed epoch — so nothing here
compares against `datetime.now()`. The instant chosen is one where UTC and `America/New_York`
disagree about the DATE, not just the hour, and the UTC assertions run with the process timezone
forced to Eastern. A stamp rendered off `time.localtime` therefore fails these tests instead of
passing them on a UTC-clocked CI runner, which is the failure mode the fix exists to prevent.
"""

import logging
import os
import time
from typing import Iterator

import pytest

from cqc_lem.utilities.logger import _UTC_DATEFMT, _LevelFormatter

pytestmark = pytest.mark.unit

#: 2026-09-01 02:30:00 UTC == 2026-08-31 22:30:00 EDT. Both readings are asserted below, so the
#: tests can tell which clock produced the line rather than assuming.
_FIXED_EPOCH = 1788229800.0
_UTC_STAMP = "2026-09-01 02:30:00Z"
_EASTERN_STAMP = "2026-08-31 22:30:00Z"


@pytest.fixture()
def eastern_process_clock() -> Iterator[None]:
    """Run the test with the process timezone set to `America/New_York`, as the containers are.

    Yields:
        None. The previous `TZ` (usually unset, i.e. the runner's UTC) is restored on the way out.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _record(level: int, *, func: str = "do_thing") -> logging.LogRecord:
    """A record fixed at `_FIXED_EPOCH`, standing in for one emitted by a worker.

    Args:
        level: The level to emit at.
        func: Value for `%(funcName)s`.

    Returns:
        A `LogRecord` whose `created` is the injected instant rather than now.
    """
    record = logging.LogRecord(name="cqc-lem", level=level, pathname="/app/src/cqc_lem/feed.py",
                               lineno=42, msg="commented on post", args=(), exc_info=None,
                               func=func)
    record.created = _FIXED_EPOCH
    record.msecs = 0.0
    return record


# The FULL expected line per level: acceptance criterion 3 is that the prefixes survive byte for
# byte, and only a whole-line assertion can prove that. DEBUG keeps its bracketed stamp — it is the
# one level that already had one — so it gains no second field.
_EXPECTED = {
    logging.DEBUG: f"[{_UTC_STAMP} feed.py->do_thing():42] DEBUG: commented on post",
    logging.INFO: f"{_UTC_STAMP} commented on post",
    logging.WARNING: f"{_UTC_STAMP} WARNING [feed.py:42]: commented on post",
    logging.ERROR: f"{_UTC_STAMP} ERROR [feed.py->do_thing():42]: commented on post",
    logging.CRITICAL: f"{_UTC_STAMP} CRITICAL [feed.py->do_thing():42]: commented on post",
}


@pytest.mark.parametrize("level", sorted(_EXPECTED))
def test_every_level_is_stamped_in_utc_with_prefixes_intact(level, eastern_process_clock):
    assert _LevelFormatter().format(_record(level)) == _EXPECTED[level]


@pytest.mark.parametrize("level", sorted(_EXPECTED))
def test_no_level_renders_the_local_stamp(level, eastern_process_clock):
    # Guards the discrimination the test above relies on: under Eastern the two readings differ, so
    # a formatter that fell back to localtime would be visible here and not merely wrong-by-an-hour.
    assert _EASTERN_STAMP not in _LevelFormatter().format(_record(level))


def test_the_two_clocks_actually_disagree_at_the_fixed_instant(eastern_process_clock):
    # If this ever fails, the timezone database moved and the assertions above stopped discriminating
    # between UTC and local — they would pass on a localtime formatter, which is the vacuous check
    # this repo keeps finding. Fail loudly here rather than silently there.
    assert time.strftime(_UTC_DATEFMT, time.localtime(_FIXED_EPOCH)) == _EASTERN_STAMP
    assert time.strftime(_UTC_DATEFMT, time.gmtime(_FIXED_EPOCH)) == _UTC_STAMP


def test_converter_is_gmtime_not_the_stdlib_default():
    assert _LevelFormatter.converter is time.gmtime
    assert logging.Formatter.converter is time.localtime  # the default this deliberately overrides


def test_unmapped_level_is_stamped_too(eastern_process_clock):
    # logging allows arbitrary numeric levels; the fallback format must not be the one line in the
    # file without a clock.
    line = _LevelFormatter().format(_record(logging.INFO + 5))
    assert line == f"{_UTC_STAMP} Level 25: commented on post"


def test_explicit_datefmt_still_wins(eastern_process_clock):
    # The UTC default is a default, not a lock — but the CLOCK stays UTC regardless of the pattern.
    line = _LevelFormatter(datefmt="%H:%M").format(_record(logging.WARNING))
    assert line == "02:30 WARNING [feed.py:42]: commented on post"
