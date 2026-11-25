"""Read-only enforce-readiness audit for the session window gate (issue #2793, refs #2767).

Answers one question with a verdict: "after N days on `enforce`, did any background action or
browser session land at night (19:00-07:00) or on a weekend in the user's zone, and did any weekday
exceed `SESSION_DAILY_CEILING` sessions?" It changes NO behaviour and makes NO writes. Shared by
`scripts/session_window_audit.py` (operator) and the weekly beat `auto_weekly_session_window_audit`.

Evidence, per user, over the last N COMPLETE local days (today is excluded):

* `logs` rows (`get_session_window_audit_rows`) — night rows and weekend rows.
* Browser sessions, counted from the session-open INFO line `get_docker_driver` writes to the
  persistent log, `<UTC stamp>Z Selenium session '<name>' egress=... | user_id=N ...`
  (`/app/logs/cqc_lem_YYYY_MM_DD.log`, plus size-rotation backups `.log.N`). Many sessions write no
  `logs` row, so the log is the only complete count. A line without `| user_id=` (the "created
  WITHOUT user_id" variant) belongs to no user and is ignored. A local day is READABLE only when
  every UTC day it spans has a log file; a missing or unreadable file makes the day INSUFFICIENT,
  never a clean one.
* The mode, from the once-per-process line `Session window mode: off|shadow|enforce`
  (`engagement_window.log_session_window_mode_once`): how long the retained logs show the current
  mode has held.

Verdict: `INSUFFICIENT` when the mode is not `enforce`, or when `enforce` was not yet in force at
the end of the first audited day, or when any audited day is unreadable and nothing offends;
`FAIL` when `enforce` is on and any night/weekend row or session, or an over-ceiling weekday, is
found (each offender is listed with its time and name/action_type, so a human can rule out a
user-started session, which the gate exempts by design); otherwise `PASS`.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from cqc_lem.utilities.engagement_window import (
    FALLBACK_TIMEZONE,
    SESSION_BAN_END_HOUR,
    SESSION_BAN_START_HOUR,
    SESSION_DAILY_CEILING,
    SESSION_WINDOW_ENFORCE,
    SESSION_WINDOW_MODE_LOG_PREFIX,
    resolve_session_zone,
    session_window_mode,
    valid_session_timezone,
)
from cqc_lem.utilities.logger import LOG_DIR

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT"

DEFAULT_AUDIT_DAYS = 14

_STAMP = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z"
_SESSION_OPEN_RE = re.compile(
    _STAMP + r" Selenium session '(?P<name>.+?)' .*\| (?:.*\s)?user_id=(?P<uid>\d+)\b")
_MODE_RE = re.compile(_STAMP + " " + re.escape(SESSION_WINDOW_MODE_LOG_PREFIX)
                      + r"(?P<mode>off|shadow|enforce)\b")


@dataclass(frozen=True)
class Offender:
    """One night/weekend row or session, or an over-ceiling day, with enough to judge it by hand."""

    kind: str       # "night_row" | "weekend_row" | "night_session" | "weekend_session" | "over_ceiling"
    local_time: str
    label: str      # the action_type or the session name


@dataclass
class DayAudit:
    """One local day's evidence."""

    day: date
    weekday: str
    readable: bool
    sessions: int = 0
    night_rows: int = 0
    weekend_rows: int = 0


@dataclass
class AuditSources:
    """Every read the audit performs, injectable so tests (and the beat) never need a database.

    `read_log_day` returns the day's log lines, or None when no file is readable.
    """

    user_timezone: Callable[[int], Optional[str]]
    session_prefs: Callable[[int], tuple]
    log_rows: Callable[[int, datetime, datetime], Optional[list]]
    read_log_day: Callable[[date], Optional[list]]
    mode: Callable[[], str] = session_window_mode


def production_sources() -> AuditSources:
    """Bind the audit to the db facade and the persistent log directory."""
    from cqc_lem.utilities.db import (
        get_session_window_audit_rows,
        get_session_window_prefs,
        get_user_timezone,
    )
    return AuditSources(
        user_timezone=get_user_timezone,
        session_prefs=get_session_window_prefs,
        log_rows=get_session_window_audit_rows,
        read_log_day=read_log_day,
    )


def read_log_day(day: date, log_dir: Optional[str] = None) -> Optional[list]:
    """Lines of the UTC-dated log file for `day` and its rotation backups; None if unreadable."""
    import glob
    import os
    base = os.path.join(log_dir or LOG_DIR, f"cqc_lem_{day:%Y_%m_%d}.log")
    if not os.path.isfile(base):
        return None
    lines: list = []
    try:
        for path in sorted(glob.glob(base + ".*"), reverse=True) + [base]:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines.extend(handle.read().splitlines())
    except OSError:
        return None
    return lines


def resolve_audit_zone(session_timezone: Optional[str], account_timezone: Optional[str]) -> ZoneInfo:
    """The zone enforce resolves: a valid `session_timezone`, else `users.timezone`, else New York."""
    return resolve_session_zone(valid_session_timezone(session_timezone) or account_timezone)


def classify_local(local: datetime) -> tuple:
    """`("night"?, "weekend"?)` reasons a local instant is outside any background window."""
    reasons = []
    if local.hour >= SESSION_BAN_START_HOUR or local.hour < SESSION_BAN_END_HOUR:
        reasons.append("night")
    if local.weekday() >= 5:
        reasons.append("weekend")
    return tuple(reasons)


def audit_days(zone: ZoneInfo, days: int, now: datetime) -> list:
    """The last `days` COMPLETE local dates, oldest first."""
    today = now.astimezone(zone).date()
    return [today - timedelta(days=offset) for offset in range(days, 0, -1)]


def local_day_span(day: date, zone: ZoneInfo) -> tuple:
    """`[start, end)` of a local day as UTC instants (DST-safe: 23 or 25 hours on a change day)."""
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc)
    return start, end


def _utc_dates(start: datetime, end: datetime) -> list:
    last = (end - timedelta(seconds=1)).date()
    return [start.date() + timedelta(days=i) for i in range((last - start.date()).days + 1)]


def _stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_session_opens(lines: list, user_id: int) -> list:
    """`(utc instant, session name)` for each session-open line of `user_id`; others are ignored."""
    opens = []
    for line in lines:
        match = _SESSION_OPEN_RE.search(line)
        if match and int(match.group("uid")) == int(user_id):
            opens.append((_stamp(match.group("ts")), match.group("name")))
    return opens


def parse_mode_events(lines: list) -> list:
    """`(utc instant, mode)` for every `Session window mode:` line."""
    events = []
    for line in lines:
        match = _MODE_RE.search(line)
        if match:
            events.append((_stamp(match.group("ts")), match.group("mode")))
    return events


def mode_held_since(events: list, mode: str) -> Optional[datetime]:
    """When the latest unbroken run of `mode` lines began, or None if the newest line differs."""
    since = None
    for at, seen in sorted(events):
        if seen == mode:
            since = since or at
        else:
            since = None
    return since


@dataclass
class AuditReport:
    """The whole answer for one user, as plain data."""

    user_id: int
    zone: str
    mode: str
    days: int
    readable_days: int
    mode_held_days: Optional[int]
    max_weekday_sessions: int
    ceiling: int
    verdict: str
    reason: str
    per_day: list = field(default_factory=list)
    offenders: list = field(default_factory=list)

    def as_dict(self) -> dict:
        """JSON-ready form."""
        return {
            "user_id": self.user_id, "zone": self.zone, "mode": self.mode, "days": self.days,
            "readable_days": self.readable_days, "mode_held_days": self.mode_held_days,
            "max_weekday_sessions": self.max_weekday_sessions, "ceiling": self.ceiling,
            "verdict": self.verdict, "reason": self.reason,
            "per_day": [{"date": d.day.isoformat(), "weekday": d.weekday, "readable": d.readable,
                         "sessions": d.sessions, "night_rows": d.night_rows,
                         "weekend_rows": d.weekend_rows} for d in self.per_day],
            "offenders": [{"kind": o.kind, "local_time": o.local_time, "label": o.label}
                          for o in self.offenders],
        }

    def summary_line(self) -> str:
        """The fixed-format line the weekly beat logs."""
        return (f"Session window audit: user_id={self.user_id} mode={self.mode} "
                f"days={self.days} verdict={self.verdict}")


def audit_user(user_id: int, days: int = DEFAULT_AUDIT_DAYS, now: Optional[datetime] = None,
               sources: Optional[AuditSources] = None) -> AuditReport:
    """Run the audit for one user. Reads only; see the module docstring for the verdict rules."""
    src = sources or production_sources()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_session_tz = (src.session_prefs(user_id) or (None, None))[1]
    zone = resolve_audit_zone(raw_session_tz, src.user_timezone(user_id) or FALLBACK_TIMEZONE)
    mode = src.mode()

    day_list = audit_days(zone, days, now)
    log_cache: dict = {}

    def day_lines(utc_day: date) -> Optional[list]:
        if utc_day not in log_cache:
            log_cache[utc_day] = src.read_log_day(utc_day)
        return log_cache[utc_day]

    per_day, offenders, mode_events = [], [], []
    max_weekday = 0
    for day in day_list:
        start, end = local_day_span(day, zone)
        row = DayAudit(day=day, weekday=day.strftime("%a"), readable=True)
        lines: list = []
        for utc_day in _utc_dates(start, end):
            got = day_lines(utc_day)
            if got is None:
                row.readable = False
            else:
                lines.extend(got)
        rows = src.log_rows(user_id, start, end)
        if rows is None:
            row.readable = False
            rows = []
        for created, action in rows:
            local = created.replace(tzinfo=timezone.utc).astimezone(zone)
            for reason in classify_local(local):
                offenders.append(Offender(f"{reason}_row", local.strftime("%Y-%m-%d %H:%M"), str(action)))
                if reason == "night":
                    row.night_rows += 1
                else:
                    row.weekend_rows += 1
        todays = [(at, name) for at, name in parse_session_opens(lines, user_id) if start <= at < end]
        row.sessions = len(todays)
        for at, name in todays:
            local = at.astimezone(zone)
            for reason in classify_local(local):
                offenders.append(Offender(f"{reason}_session", local.strftime("%Y-%m-%d %H:%M"), name))
        if day.weekday() < 5:
            max_weekday = max(max_weekday, row.sessions)
            if row.sessions > SESSION_DAILY_CEILING:
                offenders.append(Offender("over_ceiling", day.isoformat(),
                                          f"{row.sessions} sessions > {SESSION_DAILY_CEILING}"))
        mode_events.extend(parse_mode_events(lines))
        per_day.append(row)

    readable = sum(1 for d in per_day if d.readable)
    held_days = None
    since = mode_held_since(mode_events, mode)
    if since is not None:
        held_days = max(0, (now - since).days)
    first_day_end = local_day_span(day_list[0], zone)[1] if day_list else now

    if mode != SESSION_WINDOW_ENFORCE:
        verdict, reason = VERDICT_INSUFFICIENT, f"mode is {mode}, not enforce"
    elif since is None or since > first_day_end:
        verdict, reason = (VERDICT_INSUFFICIENT,
                           f"retained logs do not show enforce in force since the first of {days} days")
    elif offenders:
        verdict, reason = VERDICT_FAIL, f"{len(offenders)} offending item(s)"
    elif readable < len(day_list):
        verdict, reason = VERDICT_INSUFFICIENT, f"only {readable} of {len(day_list)} days readable"
    else:
        verdict, reason = VERDICT_PASS, "no night/weekend rows or sessions, weekdays within the ceiling"
    return AuditReport(user_id=user_id, zone=getattr(zone, "key", str(zone)), mode=mode, days=days,
                       readable_days=readable, mode_held_days=held_days,
                       max_weekday_sessions=max_weekday, ceiling=SESSION_DAILY_CEILING,
                       verdict=verdict, reason=reason, per_day=per_day, offenders=offenders)


def render_text(report: AuditReport) -> str:
    """Human rendering of one report."""
    lines = [
        f"Session window audit (issue #2793) user_id={report.user_id} zone={report.zone}",
        f"mode={report.mode} held_days={report.mode_held_days} days={report.days} "
        f"readable_days={report.readable_days}",
        f"max weekday sessions={report.max_weekday_sessions} (ceiling {report.ceiling})",
        "",
    ]
    for d in report.per_day:
        lines.append(f"  {d.day} {d.weekday} readable={'yes' if d.readable else 'NO'} "
                     f"sessions={d.sessions} night_rows={d.night_rows} weekend_rows={d.weekend_rows}")
    if report.offenders:
        lines.append("")
        lines.append("offenders (a user-started session is exempt by design - rule those out):")
        lines.extend(f"  {o.kind} {o.local_time} {o.label}" for o in report.offenders)
    lines.append("")
    lines.append(f"VERDICT: {report.verdict} - {report.reason}")
    return "\n".join(lines)
