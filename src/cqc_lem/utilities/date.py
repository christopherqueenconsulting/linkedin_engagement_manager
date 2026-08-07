"""Date reading and comparison for scraped LinkedIn text and scheduling windows.

LinkedIn almost never renders an absolute date — profiles carry tenures ("2 yrs 3 mos") and feed
cards carry relative captions ("20h", "1w", "2mo") — so this module owns turning that text into
datetimes, plus the range/ordering helpers built on top of it.

The one invariant: unreadable text is never silently a date. `get_datetime` raises ValueError where
`dateparser` would hand back None, and every list helper here is built on that — they DROP what they
cannot parse rather than propagating a guess.
"""

import datetime as DT
import math
import re

import dateparser
import tzlocal


def format_year(year: str) -> str:
    """Formats a 4 digit year to a 2 digit year.

    Args:
    year: A 4 digit year.

    Returns:
    A 2 digit year.
    """
    year = int(year)

    if year < 100:
        y = year
    else:
        y = year % 100

    return str(y)

def convert_datetime_to_local_tz(dt: DT.datetime, assumed_utc=True) -> DT.datetime:
    """Return `dt` in the host's local zone, always timezone-aware.

    `assumed_utc` only decides what a NAIVE `dt` MEANS — UTC by default (what the DB and Celery hand
    back), local wall-clock when False. An already-aware `dt` is converted, never reinterpreted, so
    the flag has no effect on it.
    """
    # Add TZ Info if missing
    if dt.tzinfo is None:
        if assumed_utc:
            dt = dt.replace(tzinfo=DT.timezone.utc)
        else:
            dt = dt.replace(tzinfo=tzlocal.get_localzone())

    # Convert to Local Timezone
    dt = dt.astimezone(tzlocal.get_localzone())

    return dt



def get_datetime(text: str) -> DT.datetime:
    """Parse arbitrary date text, raising rather than returning `dateparser`'s None.

    Turning the None into a ValueError is the whole point of the wrapper: every caller in this module
    treats "unparseable" as a control-flow branch (drop the string, fall through to the relative
    reader), and a None escaping into a comparison would raise somewhere far from the bad input.

    Raises:
        ValueError: `text` is not a date `dateparser` recognises.
    """
    dt = dateparser.parse(text)
    if dt is None:
        raise ValueError("invalid datetime as string: " + text)
    return dt

def get_linkedin_datetime_from_text(text: str) -> str:
    """Turn a LinkedIn tenure caption ("2 yrs 3 mos", "6 mos") into the month it started.

    Returns a "%b %Y" LABEL, not a date — years and months are counted back as 365 and 30 days, which
    is close enough to name a month and nothing more. Text carrying no recognisable "N yr" / "N mo"
    counts back zero and yields the CURRENT month, so an unreadable caption reads as "started now"
    rather than failing.
    """
    # Remove any leading/trailing whitespace and convert to lowercase
    text = text.strip().lower()

    # Define regex patterns to extract years and months
    years_pattern = re.compile(r'(\d+)\s*yr?s?')
    months_pattern = re.compile(r'(\d+)\s*mo?s?')

    # Extract years and months from the text
    years_match = years_pattern.search(text)
    months_match = months_pattern.search(text)

    years = int(years_match.group(1)) if years_match else 0
    months = int(months_match.group(1)) if months_match else 0

    # Calculate the date by subtracting years and months from the current date
    current_date = DT.datetime.now()
    past_date = current_date - DT.timedelta(days=(years * 365 + months * 30))

    # Format the date into a datetime string
    return past_date.strftime("%b %Y")


def is_checkdate_before_date(check_date: DT.datetime | DT.date, before_date: DT.datetime | DT.date):
    """Strictly before — equal dates are False.

    The comparison is DAY-granular whatever you pass: `datetime` subclasses `date`, so both operands
    go through `combine(..., min.time())` and any time-of-day is dropped. Two moments on the same day
    are never before one another here.
    """
    if isinstance(before_date, DT.date):
        before_date = DT.datetime.combine(before_date, DT.datetime.min.time())
    if isinstance(check_date, DT.date):
        check_date = DT.datetime.combine(check_date, DT.datetime.min.time())

    return check_date < before_date


def is_checkdate_after_date(check_date: DT.datetime | DT.date, after_date: DT.datetime | DT.date):
    """Strictly after — equal dates are False.

    Day-granular exactly as `is_checkdate_before_date`: time-of-day is dropped from both operands.
    """
    if isinstance(after_date, DT.date):
        after_date = DT.datetime.combine(after_date, DT.datetime.min.time())
    if isinstance(check_date, DT.date):
        check_date = DT.datetime.combine(check_date, DT.datetime.min.time())

    return after_date < check_date


def is_date_in_range(start_date: DT.datetime | DT.date, check_date: DT.datetime | DT.date,
                     end_date: DT.datetime | DT.date):
    """Inclusive on both ends, comparing calendar days rather than moments.

    `start_date` and `check_date` are floored to midnight and `end_date` raised to the end of its
    day, so the last day of a range counts whole. `datetime` subclasses `date`, so this normalisation
    applies to datetimes too and their time-of-day never decides the answer.
    """
    if isinstance(start_date, DT.date):
        start_date = DT.datetime.combine(start_date, DT.datetime.min.time())
    if isinstance(check_date, DT.date):
        check_date = DT.datetime.combine(check_date, DT.datetime.min.time())
    if isinstance(end_date, DT.date):
        end_date = DT.datetime.combine(end_date, DT.datetime.max.time())

    time_format = "%m-%d-%Y %H:%M:%S %Z"
    # print("Checking Date Range | Start: %s | Check: %s | End: %s" % (start_date.strftime(time_format),
    #                                                                 check_date.strftime(time_format),
    #                                                                 end_date.strftime(time_format)))
    # pprint(due_dates)

    return start_date <= check_date <= end_date


def filter_dates_in_range(date_strings: list[str], start_date: DT.datetime | DT.date, end_date: DT.datetime | DT.date):
    """The subset of `date_strings` falling inside the range, still as the original strings.

    Unparseable and blank entries are dropped rather than raising, so a scrape that picked up one bad
    line still yields the dates it did read.
    """
    date_strings = purge_empty_and_invalid_dates(date_strings)

    filtered_dates = [s for s in date_strings if is_date_in_range(start_date, get_datetime(s), end_date)]
    return filtered_dates


def purge_empty_and_invalid_dates(date_strings: list[str]) -> list[str]:
    """Drop blanks and anything `get_datetime` rejects, keeping the surviving strings verbatim.

    This is the guard that lets the ordering helpers below assume every entry parses; they call
    `get_datetime` inside a sort key, where a ValueError would abort the whole sort.
    """
    # Purge the list of any empty strings
    date_strings = [x for x in date_strings if x.strip()]

    # Remove any dates that throw ValueError from get_datetime function
    valid_dates = []
    for date_str in date_strings:
        try:
            get_datetime(date_str)
            valid_dates.append(date_str)
        except ValueError:
            continue

    return valid_dates


def order_dates(date_strings: list[str]) -> list[str]:
    """Ascending order of the input strings, with blanks and unparseable entries removed first.

    The sort key is each parsed date rendered "%m-%d-%Y %H:%M:%S" and compared as TEXT, so ordering
    is month-major: correct within one year, but a December 2019 string sorts after a January 2020
    one. Do not rely on this across a year boundary — measured, not theoretical.
    """
    time_format = "%m-%d-%Y %H:%M:%S"

    # Remove empty and invalid dates
    date_strings = purge_empty_and_invalid_dates(date_strings)

    return sorted(date_strings, key=lambda x: get_datetime(x).strftime(time_format)) if date_strings else []


def get_latest_date(date_strings: list[str]) -> str:
    """Last entry of `order_dates`, or "" when nothing in the list parsed.

    Empty string is the sentinel for "no readable date" — there is no exception path. It inherits
    `order_dates`' month-major ordering caveat.
    """
    # Return the latest date from the order_dates function or empty string if no dates or empty list
    ordered_dates = order_dates(date_strings)
    return ordered_dates[-1] if ordered_dates else ""


def get_earliest_date(date_strings: list[str]) -> str:
    """First entry of `order_dates`, or "" when nothing in the list parsed.

    Same sentinel and same month-major ordering caveat as `get_latest_date`.
    """
    # Return the earliest date from the order_dates function or empty string if not dates or empty list
    ordered_dates = order_dates(date_strings)
    return ordered_dates[0] if ordered_dates else ""


def weeks_between_dates(date1: DT.date, date2: DT.date, round_up: bool = False) -> int:
    """Whole weeks between two dates, order-independent (the day gap is taken as an absolute value).

    Truncates by default — 9 days is 1 week — and `round_up` ceilings instead, for the caller that
    needs "how many weeks does this span touch" rather than "how many complete weeks fit".
    """
    # Calculate the difference in days between the two dates
    delta_days = abs((date2 - date1).days)

    if round_up:
        # Round up to the nearest week
        weeks = math.ceil(delta_days / 7)
    else:
        # Calculate the number of weeks without rounding up
        weeks = delta_days // 7

    return weeks


def convert_datetime_to_end_of_day(dt: DT.datetime) -> DT.datetime:
    """Same calendar day at 23:59:59.999999 — the inclusive upper bound of a day-wide window.

    Any tzinfo on `dt` is dropped: `combine` takes only the date half, so the result is naive.
    """
    return DT.datetime.combine(dt, DT.datetime.max.time())


def convert_datetime_to_start_of_day(dt: DT.datetime) -> DT.datetime:
    """Same calendar day at midnight, dropping any tzinfo along with the time.

    Used to make a scraped relative timestamp ("20h ago") comparable by DAY, which is the only
    resolution LinkedIn's captions actually carry.
    """
    return DT.datetime.combine(dt, DT.datetime.min.time())


def convert_date_to_datetime(date: DT.date) -> DT.datetime:
    """Widen a `date` to the naive `datetime` at its midnight, for comparison against datetimes."""
    return DT.datetime.combine(date, DT.datetime.min.time())


# LinkedIn's viewed-on captions are relative ("Viewed 1h ago", "20h", "1d", "1w", "1mo", "2mo") —
# grounded live 2026-08-03. Months/years only need to sort out of any realistic lookback window,
# so calendar-exact arithmetic is not required for them.
_VIEWED_ON_UNIT_DAYS = {'mo': 30, 'mos': 30, 'month': 30, 'months': 30,
                        'y': 365, 'yr': 365, 'yrs': 365, 'year': 365, 'years': 365}
_VIEWED_ON_UNITS = {'s': 'seconds', 'sec': 'seconds', 'secs': 'seconds',
                    'second': 'seconds', 'seconds': 'seconds',
                    'm': 'minutes', 'min': 'minutes', 'mins': 'minutes',
                    'minute': 'minutes', 'minutes': 'minutes',
                    'h': 'hours', 'hr': 'hours', 'hrs': 'hours',
                    'hour': 'hours', 'hours': 'hours',
                    'd': 'days', 'day': 'days', 'days': 'days',
                    'w': 'weeks', 'wk': 'weeks', 'wks': 'weeks',
                    'week': 'weeks', 'weeks': 'weeks'}


def convert_viewed_on_to_date(viewed_on: str) -> DT.datetime:
    """A LinkedIn relative caption ("Viewed 1h ago", "20h", "1d", "1w", "2mo") as an absolute moment.

    The relative forms are handled here rather than by `dateparser` because they are the ones
    LinkedIn actually renders, and the "Viewed"/"Edited"/"•" chrome around them has to come off
    first. Months and years are approximated (see `_VIEWED_ON_UNIT_DAYS`) — they only have to sort.

    Anything that is not a relative caption falls through to `get_datetime`, so an unreadable string
    RAISES ValueError rather than resolving to now; callers depend on that to tell "old" from
    "unreadable".
    """
    text = re.sub(r'(?i)viewed|edited|•', '', viewed_on or '').strip()
    match = re.fullmatch(r'(?i)(\d+)\s*([a-z]+)\.?(?:\s+ago)?', text)
    if match:
        count, unit = int(match.group(1)), match.group(2).lower()
        if unit in _VIEWED_ON_UNIT_DAYS:
            return DT.datetime.now() - DT.timedelta(days=_VIEWED_ON_UNIT_DAYS[unit] * count)
        if unit in _VIEWED_ON_UNITS:
            return DT.datetime.now() - DT.timedelta(**{_VIEWED_ON_UNITS[unit]: count})
    # Anything non-relative (absolute dates, locale forms) still goes through dateparser,
    # which raises ValueError via get_datetime when it can't parse — callers rely on that.
    return get_datetime(text)
