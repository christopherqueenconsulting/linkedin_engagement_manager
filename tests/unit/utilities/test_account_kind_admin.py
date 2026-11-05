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


def _run(email: str = "QA@Example.com", kind: str = "sandbox", *, current: Any = "standard",
         admin: bool = False, creds: Any = _NO_CREDS, user_id: Any = 9, wrote: bool = True,
         audit: bool = True, **flags: bool) -> tuple[int, dict[str, Any]]:
    from cqc_lem.utilities import account_kind_admin as admin_mod

    with patch(f"{_M}.get_user_id", return_value=user_id) as lookup, \
         patch(f"{_M}.read_account_kind", return_value=current), \
         patch(f"{_M}.is_user_admin", return_value=admin), \
         patch(f"{_M}.get_linkedin_credential_flags", return_value=creds), \
         patch(f"{_M}.set_user_account_kind", return_value=wrote) as write, \
         patch(f"{_M}.record_auth_event", return_value=audit) as record, \
         patch(f"{_M}.log_info"):
        code = admin_mod.run(email, kind, **flags)
    return code, {"lookup": lookup, "write": write, "record": record}


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
        calls["write"].assert_called_once_with(9, "sandbox")
        code, calls = _run(admin=True, creds={**_NO_CREDS, "li_at": True}, allow_credentials=True)
        assert code == 2, "--allow-credentials must not open the admin refusal"

    @pytest.mark.parametrize("current", ["internal", "sandbox"])
    def test_a_move_back_to_standard_needs_an_explicit_flag(self, current: str) -> None:
        code, calls = _run(kind="standard", current=current)
        assert code == 2
        calls["write"].assert_not_called()
        code, calls = _run(kind="standard", current=current, allow_reset_to_standard=True)
        assert code == 0
        calls["write"].assert_called_once_with(9, "standard")

    def test_credentials_are_irrelevant_to_an_internal_flag(self) -> None:
        code, calls = _run(kind="internal", creds={"password": True, "access_token": True, "li_at": True})
        assert code == 0
        calls["write"].assert_called_once_with(9, "internal")


class TestAChangeThatIsAllowed:
    def test_a_clean_standard_account_becomes_sandbox_and_is_audited(self) -> None:
        from cqc_lem.utilities.db import AuthAuditEvent

        code, calls = _run()
        assert code == 0
        calls["lookup"].assert_called_once_with("qa@example.com")  # the address is lower-cased
        calls["write"].assert_called_once_with(9, "sandbox")
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
                                    allow_reset_to_standard=False, dry_run=False)

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
