"""Unit tests for avatar credit and training DB functions."""

from unittest.mock import MagicMock, patch

import pytest


def _make_db_mocks(fetchone_return=None, fetchall_return=None, rowcount=1):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = fetchall_return or []
    cursor.rowcount = rowcount
    cursor.lastrowid = 42
    connection = MagicMock()
    # wire connection.cursor(...) → cursor regardless of kwargs
    connection.cursor.return_value = cursor
    return connection, cursor


@pytest.mark.unit
class TestGetAvatarCreditBalance:
    def test_returns_sum_from_ledger(self):
        conn, cur = _make_db_mocks(fetchone_return={"balance": 5})
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_credit_balance

            balance = get_avatar_credit_balance(1)

        assert balance == 5

    def test_returns_zero_when_no_rows(self):
        conn, cur = _make_db_mocks(fetchone_return={"balance": 0})
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_credit_balance

            balance = get_avatar_credit_balance(1)

        assert balance == 0

    def test_returns_zero_on_db_error(self):
        conn, cur = _make_db_mocks()
        cur.execute.side_effect = __import__("mysql.connector", fromlist=["connector"]).Error("DB down")
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_credit_balance

            balance = get_avatar_credit_balance(99)

        assert balance == 0


@pytest.mark.unit
class TestAddAvatarCredits:
    def test_inserts_positive_delta(self):
        conn, cur = _make_db_mocks()
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_avatar_credits

            result = add_avatar_credits(1, 3, "purchase_value", "sess_abc")

        assert result is True
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO avatar_credit_ledger" in sql

    def test_returns_false_on_db_error(self):
        conn, cur = _make_db_mocks()
        cur.execute.side_effect = __import__("mysql.connector", fromlist=["connector"]).Error("fail")
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_avatar_credits

            result = add_avatar_credits(1, 1, "purchase_starter")

        assert result is False


@pytest.mark.unit
class TestDeductAvatarCredit:
    def test_inserts_negative_delta(self):
        conn, cur = _make_db_mocks()
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import deduct_avatar_credit

            result = deduct_avatar_credit(1, "train-xyz")

        assert result is True
        sql, params = cur.execute.call_args[0]
        assert "-1" in sql or -1 in params

    def test_returns_false_on_error(self):
        conn, cur = _make_db_mocks()
        cur.execute.side_effect = __import__("mysql.connector", fromlist=["connector"]).Error("fail")
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import deduct_avatar_credit

            assert deduct_avatar_credit(1, "train-xyz") is False


@pytest.mark.unit
class TestInsertAvatarTraining:
    def test_returns_lastrowid(self):
        conn, cur = _make_db_mocks()
        cur.lastrowid = 7
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_avatar_training

            result = insert_avatar_training(1, "train-abc", "LEMAVTR1")

        assert result == 7

    def test_returns_none_on_error(self):
        conn, cur = _make_db_mocks()
        cur.execute.side_effect = __import__("mysql.connector", fromlist=["connector"]).Error("fail")
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_avatar_training

            assert insert_avatar_training(1, "train-abc", "TOK") is None


@pytest.mark.unit
class TestUpdateAvatarTrainingStatus:
    def test_updates_status_and_model_ref(self):
        conn, cur = _make_db_mocks(rowcount=1)
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_training_status

            result = update_avatar_training_status("train-1", "succeeded", "user/model:v1")

        assert result is True

    def test_issues_refund_on_failure(self):
        conn, cur = _make_db_mocks(rowcount=1, fetchone_return={"user_id": 5})
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn), \
             patch("cqc_lem.utilities.db.refund_avatar_credit") as mock_refund:
            from cqc_lem.utilities.db import update_avatar_training_status

            update_avatar_training_status("train-fail", "failed")

            mock_refund.assert_called_once_with(5, "train-fail")


@pytest.mark.unit
class TestGetActiveAvatar:
    def test_returns_none_when_no_active(self):
        conn, cur = _make_db_mocks(fetchone_return=None)
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_active_avatar

            result = get_active_avatar(1)

        assert result is None

    def test_returns_dict_when_active_exists(self):
        conn, cur = _make_db_mocks(fetchone_return={
            "id": 3,
            "training_id": "train-1",
            "model_ref": "user/model:v1",
            "trigger_word": "LEMAVTR1",
            "status": "succeeded",
        })
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_active_avatar

            result = get_active_avatar(1)

        assert result is not None
        assert result["trigger_word"] == "LEMAVTR1"
        assert result["status"] == "succeeded"
