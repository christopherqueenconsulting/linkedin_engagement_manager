"""The owner-only way to flag an account `internal` or `sandbox` (`utilities/account_kind_admin.py`).

The module is the ONLY writer of `users.account_kind`. Its refusals are the safety rules: no admin
account may be flagged, no sandbox account that holds real LinkedIn credentials, and no silent move
back to `standard`. Every collaborator is replaced, so no database is touched.
"""

from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.account_kind_admin"
_NO_CREDS = {"password": False, "access_token": False, "li_at": False}
_NOTHING_QUEUED = {"scheduled_dms": 0, "connection_requests": 0, "catchup_touches": 0,
                   "lead_signals": 0, "outreach_funnel_targets": 0, "posts": 0}


def _run(email: str = "QA@Example.com", kind: str = "sandbox", *, current: Any = "standard",
         admin: bool = False, creds: Any = _NO_CREDS, user_id: Any = 9, wrote: bool = True,
         audit: bool = True, queued: Any = _NOTHING_QUEUED, **flags: bool) -> tuple[int, dict[str, Any]]:
    from cqc_lem.utilities import account_kind_admin as admin_mod

    with patch(f"{_M}.get_user_id", return_value=user_id) as lookup, \
         patch(f"{_M}.read_account_kind", return_value=current), \
         patch(f"{_M}.is_user_admin", return_value=admin), \
         patch(f"{_M}.get_linkedin_credential_flags", return_value=creds), \
         patch(f"{_M}.set_user_account_kind", return_value=wrote) as write, \
         patch(f"{_M}.count_queued_sends", return_value=queued) as count, \
         patch(f"{_M}.record_auth_event", return_value=audit) as record, \
         patch(f"{_M}.log_info"):
        code = admin_mod.run(email, kind, **flags)
    return code, {"lookup": lookup, "write": write, "record": record, "count": count}


class TestRefusals:
    def test_an_unknown_kind_is_refused(self) -> None:
        code, calls = _run(kind="Sandbox")
        assert code == 2
        calls["write"].assert_not_called()

    def test_an_unknown_account_is_refused(self) -> None:
        code, calls = _run(user_id=None)
        assert code == 2
        calls["write"].assert_not_called()

    def test_an_unreadable_current_kind_is_refused(self) -> None:
        code, calls = _run(current=None)
        assert code == 2
        calls["write"].assert_not_called()

    @pytest.mark.parametrize("kind", ["internal", "sandbox"])
    def test_an_admin_account_cannot_be_flagged_internal_or_sandbox(self, kind: str) -> None:
        code, calls = _run(kind=kind, admin=True)
        assert code == 2
        calls["write"].assert_not_called()
        calls["record"].assert_not_called()

    @pytest.mark.parametrize("held", ["password", "access_token", "li_at"])
    def test_a_sandbox_with_real_credentials_is_refused(self, held: str) -> None:
        code, calls = _run(creds={**_NO_CREDS, held: True})
        assert code == 2
        calls["write"].assert_not_called()

    def test_credentials_that_cannot_be_read_count_as_present(self) -> None:
        code, calls = _run(creds=None)
        assert code == 2
        calls["write"].assert_not_called()

    def test_allow_credentials_overrides_only_that_rule(self) -> None:
        code, calls = _run(creds={**_NO_CREDS, "li_at": True}, allow_credentials=True)
        assert code == 0
        calls["write"].assert_called_once_with(9, "sandbox", cancel_queued=False)
        code, calls = _run(admin=True, creds={**_NO_CREDS, "li_at": True}, allow_credentials=True)
        assert code == 2, "--allow-credentials must not open the admin refusal"

    def test_an_internal_account_moving_to_standard_needs_an_explicit_flag(self) -> None:
        code, calls = _run(kind="standard", current="internal")
        assert code == 2
        calls["write"].assert_not_called()
        code, calls = _run(kind="standard", current="internal", allow_reset_to_standard=True)
        assert code == 0
        calls["write"].assert_called_once_with(9, "standard", cancel_queued=False)

    def test_credentials_are_irrelevant_to_an_internal_flag(self) -> None:
        code, calls = _run(kind="internal", creds={"password": True, "access_token": True, "li_at": True})
        assert code == 0
        calls["write"].assert_called_once_with(9, "internal", cancel_queued=False)


class TestLeavingSandbox:
    """Leaving sandbox needs a flag, and then needs the queue empty or cancelled with the change.

    The kind is read when a task RUNS, so everything queued on a sandbox account goes live the moment
    the flag flips.
    """

    @pytest.mark.parametrize("target", ["internal", "standard"])
    def test_any_move_away_from_sandbox_is_refused_without_the_flag(self, target: str) -> None:
        code, calls = _run(kind=target, current="sandbox", allow_reset_to_standard=True)
        assert code == 2
        calls["write"].assert_not_called()
        calls["count"].assert_not_called()

    def test_standard_needs_both_the_reset_flag_and_the_leave_flag(self) -> None:
        code, calls = _run(kind="standard", current="sandbox", allow_leave_sandbox=True)
        assert code == 2, "--allow-leave-sandbox alone must not open the reset-to-standard refusal"
        calls["write"].assert_not_called()
        code, calls = _run(kind="standard", current="sandbox", allow_leave_sandbox=True,
                           allow_reset_to_standard=True)
        assert code == 0
        calls["write"].assert_called_once_with(9, "standard", cancel_queued=False)

    @pytest.mark.parametrize("table", ["scheduled_dms", "connection_requests", "catchup_touches",
                                       "lead_signals", "outreach_funnel_targets", "posts"])
    def test_with_the_flag_but_a_queued_row_it_is_still_refused(self, table: str) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True,
                           queued={**_NOTHING_QUEUED, table: 1})
        assert code == 2
        calls["write"].assert_not_called()
        calls["record"].assert_not_called()

    def test_an_approved_scheduled_dm_blocks_the_change(self, capsys: Any) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True,
                           queued={**_NOTHING_QUEUED, "scheduled_dms": 2})
        assert code == 2
        assert "scheduled_dms=2" in capsys.readouterr().err

    def test_a_queue_that_cannot_be_counted_blocks_the_change(self) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True, queued=None)
        assert code == 2
        calls["write"].assert_not_called()

    def test_cancel_queued_cancels_in_the_same_write_and_the_change_applies(self) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True,
                           cancel_queued=True, queued={**_NOTHING_QUEUED, "scheduled_dms": 3})
        assert code == 0
        calls["write"].assert_called_once_with(9, "internal", cancel_queued=True)
        details = calls["record"].call_args.kwargs["details"]
        assert details["cancelled_queued"]["scheduled_dms"] == 3
        assert details["allow_leave_sandbox"] is True

    def test_cancel_queued_still_cancels_when_the_count_could_not_be_read(self) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True,
                           cancel_queued=True, queued=None)
        assert code == 0
        calls["write"].assert_called_once_with(9, "internal", cancel_queued=True)

    def test_an_empty_queue_needs_no_cancel_flag(self) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True)
        assert code == 0
        calls["write"].assert_called_once_with(9, "internal", cancel_queued=False)

    def test_the_queue_is_only_counted_when_leaving_sandbox(self) -> None:
        for current, kind in (("standard", "sandbox"), ("standard", "internal"), ("internal", "sandbox")):
            _code, calls = _run(kind=kind, current=current)
            calls["count"].assert_not_called()

    def test_cancel_queued_does_nothing_when_not_leaving_sandbox(self) -> None:
        code, calls = _run(kind="sandbox", current="standard", cancel_queued=True)
        assert code == 0
        calls["write"].assert_called_once_with(9, "sandbox", cancel_queued=False)

    def test_dry_run_reports_the_cancellation_and_writes_nothing(self, capsys: Any) -> None:
        code, calls = _run(kind="internal", current="sandbox", allow_leave_sandbox=True,
                           cancel_queued=True, queued={**_NOTHING_QUEUED, "posts": 4}, dry_run=True)
        assert code == 0
        calls["write"].assert_not_called()
        assert "posts" in capsys.readouterr().out

    def test_standard_to_sandbox_still_works_and_still_refuses_credentials(self) -> None:
        code, calls = _run(kind="sandbox", current="standard")
        assert code == 0
        calls["write"].assert_called_once_with(9, "sandbox", cancel_queued=False)
        code, calls = _run(kind="sandbox", current="standard", creds={**_NO_CREDS, "password": True})
        assert code == 2
        calls["write"].assert_not_called()


class TestAChangeThatIsAllowed:
    def test_a_clean_standard_account_becomes_sandbox_and_is_audited(self) -> None:
        from cqc_lem.utilities.db import AuthAuditEvent

        code, calls = _run()
        assert code == 0
        calls["lookup"].assert_called_once_with("qa@example.com")  # the address is lower-cased
        calls["write"].assert_called_once_with(9, "sandbox", cancel_queued=False)
        calls["record"].assert_called_once()
        event = calls["record"].call_args.args[0]
        assert event == AuthAuditEvent.ACCOUNT_KIND_CHANGED == "account_kind_changed"
        assert calls["record"].call_args.kwargs["user_id"] == 9
        assert calls["record"].call_args.kwargs["details"]["from"] == "standard"
        assert calls["record"].call_args.kwargs["details"]["to"] == "sandbox"

    def test_a_no_op_change_writes_nothing(self) -> None:
        code, calls = _run(current="sandbox")
        assert code == 0
        calls["write"].assert_not_called()
        calls["record"].assert_not_called()

    def test_dry_run_writes_nothing(self, capsys: Any) -> None:
        code, calls = _run(dry_run=True)
        assert code == 0
        calls["write"].assert_not_called()
        calls["record"].assert_not_called()
        assert "dry run" in capsys.readouterr().out

    def test_a_failed_write_is_exit_3_and_unaudited(self) -> None:
        code, calls = _run(wrote=False)
        assert code == 3
        calls["record"].assert_not_called()

    def test_a_failed_audit_write_warns_but_the_change_stands(self, capsys: Any) -> None:
        code, calls = _run(audit=False)
        assert code == 0
        calls["write"].assert_called_once()
        assert "audit row could not be written" in capsys.readouterr().err


class TestCommandLine:
    def test_main_parses_and_runs(self) -> None:
        from cqc_lem.utilities import account_kind_admin as admin_mod

        with patch.object(admin_mod, "run", return_value=0) as run:
            assert admin_mod.main(["--email", "a@b.co", "--kind", "sandbox", "--allow-credentials"]) == 0
        run.assert_called_once_with("a@b.co", "sandbox", allow_credentials=True,
                                    allow_reset_to_standard=False, dry_run=False,
                                    allow_leave_sandbox=False, cancel_queued=False)

    def test_the_new_flags_are_parsed(self) -> None:
        from cqc_lem.utilities import account_kind_admin as admin_mod

        with patch.object(admin_mod, "run", return_value=0) as run:
            admin_mod.main(["--email", "a@b.co", "--kind", "internal", "--allow-leave-sandbox",
                            "--cancel-queued"])
        assert run.call_args.kwargs["allow_leave_sandbox"] is True
        assert run.call_args.kwargs["cancel_queued"] is True

    def test_an_unknown_kind_never_reaches_run(self) -> None:
        from cqc_lem.utilities import account_kind_admin as admin_mod

        with pytest.raises(SystemExit):
            admin_mod.main(["--email", "a@b.co", "--kind", "root"])


class TestTheRepositoryWriter:
    def test_it_validates_the_value_before_touching_the_database(self) -> None:
        from cqc_lem.utilities.db import set_user_account_kind

        with patch("cqc_lem.platform.db.connection.get_db_connection") as conn:
            for bad in ("Sandbox", "", "admin", None):
                with pytest.raises(ValueError):
                    set_user_account_kind(1, bad)  # type: ignore[arg-type]
        conn.assert_not_called()

    def test_it_reports_true_only_when_exactly_one_row_changed(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import set_user_account_kind

        for rowcount, expected in ((1, True), (0, False), (2, False)):
            conn, cur = fake_cursor(rowcount=rowcount)
            with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
                assert set_user_account_kind(5, "sandbox") is expected
            sql, params = cur.execute.call_args.args
            assert "account_kind" in sql and params == ("sandbox", 5)

    def test_credential_flags_are_presence_only(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import get_linkedin_credential_flags

        conn, cur = fake_cursor(fetch_one=(1, 0, 1))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_linkedin_credential_flags(5) == {"password": True, "access_token": False,
                                                        "li_at": True}
        sql = cur.execute.call_args.args[0]
        assert "<> ''" in sql and "EXISTS" in sql
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_linkedin_credential_flags(5) is None


class TestTheQueuedSendQueues:
    """The cancellation list is real tables and real status values, pinned to the status enums."""

    _ENUMS = {
        "scheduled_dms": "ScheduledDmStatus", "connection_requests": "ConnectionRequestStatus",
        "catchup_touches": "CatchupTouchStatus", "lead_signals": "LeadSignalStatus",
        "outreach_funnel_targets": "OutreachStatus", "posts": "PostStatus",
    }

    def test_every_queue_is_listed_once_with_real_statuses(self) -> None:
        from cqc_lem.platform.db import enums
        from cqc_lem.platform.db.repositories.users import QUEUED_SEND_QUEUES

        assert {q[0] for q in QUEUED_SEND_QUEUES} == set(self._ENUMS)
        assert len(QUEUED_SEND_QUEUES) == len(self._ENUMS)
        for table, live, cancelled, _reason in QUEUED_SEND_QUEUES:
            values = {m.value for m in getattr(enums, self._ENUMS[table])}
            assert set(live) <= values, (table, set(live) - values)
            assert cancelled in values, (table, cancelled)
            assert cancelled not in live, table
            # Never a state that has already gone out.
            assert not {"sent", "posted", "acted"} & set(live), table

    def test_count_runs_one_query_per_queue_with_the_live_statuses(self, fake_cursor: Any) -> None:
        from cqc_lem.platform.db.repositories.users import QUEUED_SEND_QUEUES
        from cqc_lem.utilities.db import count_queued_sends

        conn, cur = fake_cursor(fetch_one=(2,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            counts = count_queued_sends(9)
        assert counts == {q[0]: 2 for q in QUEUED_SEND_QUEUES}
        assert cur.execute.call_count == len(QUEUED_SEND_QUEUES)
        for call, (table, live, _c, _r) in zip(cur.execute.call_args_list, QUEUED_SEND_QUEUES):
            sql, params = call.args
            assert f"FROM {table} WHERE user_id = %s" in sql
            assert params == (9, *live)

    def test_a_database_fault_counts_as_unknown(self, fake_cursor: Any) -> None:
        import mysql.connector

        from cqc_lem.utilities.db import count_queued_sends

        conn, _ = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert count_queued_sends(9) is None

    def test_cancel_runs_after_the_kind_write_in_one_commit(self, fake_cursor: Any) -> None:
        from cqc_lem.platform.db.repositories.users import QUEUED_SEND_QUEUES
        from cqc_lem.utilities.db import set_user_account_kind

        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert set_user_account_kind(9, "internal", cancel_queued=True) is True
        statements = [c.args for c in cur.execute.call_args_list]
        assert "UPDATE users SET account_kind" in statements[0][0]
        assert len(statements) == 1 + len(QUEUED_SEND_QUEUES)
        for (sql, params), (table, live, cancelled, reason) in zip(statements[1:], QUEUED_SEND_QUEUES):
            assert f"UPDATE {table} SET status = %s" in sql
            assert params[0] == cancelled
            assert params[-len(live):] == live and params[-len(live) - 1] == 9
            assert ("rejection_reason" in sql) is bool(reason)
        conn.commit.assert_called_once()

    def test_nothing_is_cancelled_when_the_kind_row_did_not_change(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import set_user_account_kind

        conn, cur = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert set_user_account_kind(9, "internal", cancel_queued=True) is False
        assert cur.execute.call_count == 1

    def test_without_the_flag_no_queue_is_touched(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import set_user_account_kind

        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert set_user_account_kind(9, "sandbox") is True
        assert cur.execute.call_count == 1
