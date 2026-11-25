"""Session window enforce-readiness audit (issue #2793): classification, log parsing, verdicts."""

import ast
import re
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import engagement_window as ew, session_window_audit as swa

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)  # Wed; audited days end Tue 11-24


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _open_line(ts: str, name: str = "Post Stats", uid: int = 1) -> str:
    return (f"{ts}Z Selenium session '{name}' egress=direct tz=America/New_York locale=en-US "
            f"images=on video=on | user_id={uid} action_type=login")


def _sources(*, lines=None, rows=None, tz="America/New_York", prefs=(None, None), mode="enforce",
             missing=()):
    lines = lines or {}

    def read(day: date):
        return None if day in missing else list(lines.get(day, []))

    return swa.AuditSources(
        user_timezone=lambda uid: tz,
        session_prefs=lambda uid: prefs,
        log_rows=lambda uid, s, e: [] if rows is None else [r for r in rows if s <= r[0].replace(
            tzinfo=timezone.utc) < e],
        read_log_day=read,
        mode=lambda: mode,
    )


def _enforce_lines(days: int = 3, per_day=()):
    """Mode lines every UTC day so enforce reads as held from the start, plus any extra lines."""
    out = {}
    for offset in range(days + 2):
        d = date.fromordinal(date(2026, 11, 25).toordinal() - offset)
        out[d] = [f"{d.isoformat()} 00:00:05Z Session window mode: enforce"]
    for d, line in per_day:
        out[d].append(line)
    return out


class TestClassify:
    def test_night_boundaries(self):
        zone = ew.resolve_session_zone("America/New_York")
        assert swa.classify_local(datetime(2026, 11, 24, 19, 0, tzinfo=zone)) == ("night",)
        assert swa.classify_local(datetime(2026, 11, 24, 18, 59, tzinfo=zone)) == ()
        assert swa.classify_local(datetime(2026, 11, 24, 6, 59, tzinfo=zone)) == ("night",)
        assert swa.classify_local(datetime(2026, 11, 24, 7, 0, tzinfo=zone)) == ()

    def test_weekend_and_weekend_night(self):
        zone = ew.resolve_session_zone("America/New_York")
        assert swa.classify_local(datetime(2026, 11, 21, 10, 0, tzinfo=zone)) == ("weekend",)  # Sat
        assert swa.classify_local(datetime(2026, 11, 22, 23, 0, tzinfo=zone)) == ("night", "weekend")

    def test_dst_boundary_day_is_23_hours_and_classified_in_local_time(self):
        zone = ew.resolve_session_zone("America/New_York")
        start, end = swa.local_day_span(date(2026, 3, 8), zone)  # spring forward, Sunday
        assert (end - start).total_seconds() == 23 * 3600
        # Fri Mar 6 23:30Z is 18:30 EST (inside the day); Mon Mar 9 23:30Z is 19:30 EDT (night):
        # the same UTC clock time reads one local hour later once the offset has moved.
        assert swa.classify_local(_utc(2026, 3, 6, 23, 30).astimezone(zone)) == ()
        assert swa.classify_local(_utc(2026, 3, 9, 23, 30).astimezone(zone)) == ("night",)
        fall_start, fall_end = swa.local_day_span(date(2026, 11, 1), zone)
        assert (fall_end - fall_start).total_seconds() == 25 * 3600


class TestZone:
    def test_null_timezone_falls_back_to_new_york_not_utc(self):
        assert swa.resolve_audit_zone(None, None).key == "America/New_York"

    def test_valid_session_timezone_beats_account_timezone(self):
        assert swa.resolve_audit_zone("Europe/Paris", "America/New_York").key == "Europe/Paris"

    def test_invalid_session_timezone_uses_account_timezone(self):
        assert swa.resolve_audit_zone("Not/AZone", "Asia/Tokyo").key == "Asia/Tokyo"

    def test_audit_uses_session_timezone_over_users_timezone(self):
        report = swa.audit_user(1, days=1, now=NOW, sources=_sources(
            lines=_enforce_lines(), tz="America/New_York", prefs=(None, "Europe/Paris")))
        assert report.zone == "Europe/Paris"


class TestParsing:
    def test_only_target_user_counted(self):
        lines = [_open_line("2026-11-24 14:00:00"), _open_line("2026-11-24 15:00:00", uid=2),
                 "2026-11-24 16:00:00Z Selenium session 'X' created WITHOUT user_id — no proxy"]
        opens = swa.parse_session_opens(lines, 1)
        assert [name for _, name in opens] == ["Post Stats"]

    def test_user_id_not_prefix_matched(self):
        assert swa.parse_session_opens([_open_line("2026-11-24 14:00:00", uid=12)], 1) == []

    def test_mode_events_and_held_since(self):
        events = swa.parse_mode_events([
            "2026-11-20 01:00:00Z Session window mode: shadow",
            "2026-11-21 01:00:00Z Session window mode: enforce",
            "2026-11-22 01:00:00Z Session window mode: enforce"])
        assert swa.mode_held_since(events, "enforce") == _utc(2026, 11, 21, 1)
        assert swa.mode_held_since(events, "shadow") is None


class TestVerdicts:
    def test_pass(self):
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines(per_day=[
            (date(2026, 11, 24), _open_line("2026-11-24 13:10:00"))])))
        assert r.verdict == swa.VERDICT_PASS and r.max_weekday_sessions == 1 and r.readable_days == 3

    def test_night_session_fails_and_is_listed(self):
        # 2026-11-25 00:30Z = 19:30 ET on Tue 11-24.
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines(per_day=[
            (date(2026, 11, 25), _open_line("2026-11-25 00:30:00", name="Invite to Connect"))])))
        assert r.verdict == swa.VERDICT_FAIL
        assert [(o.kind, o.label) for o in r.offenders] == [("night_session", "Invite to Connect")]
        assert r.offenders[0].local_time == "2026-11-24 19:30"

    def test_weekend_row_fails(self):
        rows = [(datetime(2026, 11, 21, 15, 0), "COMMENT")]  # Sat 10:00 ET
        r = swa.audit_user(1, days=5, now=NOW, sources=_sources(
            lines=_enforce_lines(days=6), rows=rows))
        assert r.verdict == swa.VERDICT_FAIL
        assert ("weekend_row", "COMMENT") in [(o.kind, o.label) for o in r.offenders]

    def test_night_row_fails(self):
        rows = [(datetime(2026, 11, 24, 2, 0), "POST")]  # Mon 21:00 ET
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines(), rows=rows))
        assert r.verdict == swa.VERDICT_FAIL and r.offenders[0].kind == "night_row"

    def test_over_ceiling_weekday_fails(self):
        extra = [(date(2026, 11, 24), _open_line(f"2026-11-24 1{h}:00:00")) for h in range(3, 8)]
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines(per_day=extra)))
        assert r.max_weekday_sessions == 5
        assert r.verdict == swa.VERDICT_FAIL
        assert any(o.kind == "over_ceiling" for o in r.offenders)

    def test_exactly_ceiling_passes(self):
        extra = [(date(2026, 11, 24), _open_line(f"2026-11-24 1{h}:00:00")) for h in range(3, 7)]
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines(per_day=extra)))
        assert r.verdict == swa.VERDICT_PASS and r.max_weekday_sessions == 4

    def test_missing_log_day_is_insufficient_never_pass(self):
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(
            lines=_enforce_lines(), missing={date(2026, 11, 23)}))
        assert r.verdict == swa.VERDICT_INSUFFICIENT
        assert r.readable_days < 3

    def test_unreadable_rows_make_day_unreadable(self):
        src = _sources(lines=_enforce_lines())
        src.log_rows = lambda uid, s, e: None
        assert swa.audit_user(1, days=2, now=NOW, sources=src).verdict == swa.VERDICT_INSUFFICIENT

    @pytest.mark.parametrize("mode", ["off", "shadow"])
    def test_non_enforce_is_insufficient(self, mode):
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines(), mode=mode))
        assert r.verdict == swa.VERDICT_INSUFFICIENT and mode in r.reason

    def test_enforce_not_yet_held_is_insufficient(self):
        lines = _enforce_lines()
        lines[date(2026, 11, 22)] = ["2026-11-22 00:00:05Z Session window mode: shadow"]
        lines[date(2026, 11, 21)] = ["2026-11-21 00:00:05Z Session window mode: shadow"]
        lines[date(2026, 11, 23)] = ["2026-11-23 00:00:05Z Session window mode: enforce"]
        lines[date(2026, 11, 24)] = ["2026-11-24 00:00:05Z Session window mode: enforce"]
        r = swa.audit_user(1, days=5, now=NOW, sources=_sources(lines=lines))
        assert r.verdict == swa.VERDICT_INSUFFICIENT

    def test_summary_line_and_dict(self):
        r = swa.audit_user(1, days=3, now=NOW, sources=_sources(lines=_enforce_lines()))
        assert r.summary_line() == "Session window audit: user_id=1 mode=enforce days=3 verdict=PASS"
        assert r.as_dict()["verdict"] == "PASS"
        assert "VERDICT: PASS" in swa.render_text(r)


class TestReadLogDay:
    def test_missing_file_is_none_and_backups_are_read(self, tmp_path):
        assert swa.read_log_day(date(2026, 11, 24), str(tmp_path)) is None
        (tmp_path / "cqc_lem_2026_11_24.log").write_text("a\nb\n")
        (tmp_path / "cqc_lem_2026_11_24.log.1").write_text("c\n")
        assert sorted(swa.read_log_day(date(2026, 11, 24), str(tmp_path))) == ["a", "b", "c"]


class TestReadOnly:
    def test_audit_issues_only_reads_through_the_facade(self):
        """The audit imports nothing that writes, and the one reader it adds is a SELECT."""
        from cqc_lem.platform.db.repositories import engagement as repo
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        with patch.object(repo, "db_cursor", return_value=ctx) as db_cursor:
            repo.get_session_window_audit_rows(1, _utc(2026, 11, 24), _utc(2026, 11, 25))
        assert db_cursor.call_args.kwargs.get("commit") is not True
        sql = cursor.execute.call_args.args[0]
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", sql, re.I)

        for path in ("src/cqc_lem/utilities/session_window_audit.py", "scripts/session_window_audit.py"):
            text = (Path(__file__).resolve().parents[3] / path).read_text()
            assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b|cursor\(|execute\(", text)
            imported = {n.name for node in ast.walk(ast.parse(text)) if isinstance(node, ast.ImportFrom)
                        for n in node.names}
            assert not {i for i in imported if i.startswith(("insert_", "update_", "delete_", "set_"))}


class TestModeLogOnce:
    def test_logged_once_per_process(self, monkeypatch):
        monkeypatch.setattr(ew, "_MODE_LOGGED", set())
        monkeypatch.setenv("SESSION_WINDOW_MODE", "shadow")
        with patch(f"{ew.__name__}.log_info") as info, patch(f"{ew.__name__}.log_warning") as warn:
            assert ew.log_session_window_mode_once() == "shadow"
            ew.log_session_window_mode_once()
            ew.observe_session_window(None, "x")
        info.assert_called_once_with("Session window mode: shadow")
        warn.assert_not_called()

    def test_mode_line_matches_the_audit_parser(self):
        events = swa.parse_mode_events([f"2026-11-24 01:00:00Z {ew.SESSION_WINDOW_MODE_LOG_PREFIX}off"])
        assert events == [(_utc(2026, 11, 24, 1), "off")]


class TestBeat:
    def _run(self, mode, verdict):
        from cqc_lem.app import run_scheduler as rs
        report = swa.AuditReport(user_id=1, zone="America/New_York", mode=mode, days=14,
                                 readable_days=14, mode_held_days=14, max_weekday_sessions=3,
                                 ceiling=4, verdict=verdict, reason="r")
        with patch.object(rs, "get_active_user_ids", return_value=[1]), \
                patch("cqc_lem.utilities.session_window_audit.audit_user", return_value=report), \
                patch.object(rs, "log_warning") as warn, patch.object(rs, "log_info") as info:
            rs.auto_weekly_session_window_audit.run()
        return warn, info

    def test_warns_only_for_enforce_fail(self):
        warn, info = self._run("enforce", swa.VERDICT_FAIL)
        warn.assert_called_once()
        info.assert_called_once_with(
            "Session window audit: user_id=1 mode=enforce days=14 verdict=FAIL", user_id=1)

    @pytest.mark.parametrize("mode,verdict", [
        ("off", swa.VERDICT_INSUFFICIENT), ("shadow", swa.VERDICT_INSUFFICIENT),
        ("off", swa.VERDICT_FAIL), ("shadow", swa.VERDICT_FAIL),
        ("enforce", swa.VERDICT_INSUFFICIENT), ("enforce", swa.VERDICT_PASS)])
    def test_no_warning_otherwise(self, mode, verdict):
        warn, info = self._run(mode, verdict)
        warn.assert_not_called()
        info.assert_called_once()

    def test_beat_is_scheduled_monday_1300_on_default_queue(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["session-window-audit"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_weekly_session_window_audit"
        assert str(entry["schedule"].hour) and entry["schedule"].day_of_week == {1}
        assert entry["schedule"].hour == {13} and entry["schedule"].minute == {0}
        assert "queue" not in entry.get("options", {})
