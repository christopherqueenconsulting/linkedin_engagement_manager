"""Coverage tests for the avatar attribute / approval / guardrail DB helpers (issue #744)."""
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(fetch_one=None, fetch_all=None, rowcount=1, lastrowid=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetch_one
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


class TestAvatarRowSerialization:
    def test_full_row_round_trips(self):
        row = {
            "id": 4, "training_id": "t4", "model_ref": "m", "trigger_word": "TOK",
            "status": "succeeded", "is_active": 1,
            "gender_presentation": "man", "age_band": "40s",
            "attributes_confirmed_at": datetime(2026, 7, 30, 12, 0),
            "approval_status": "approved", "approved_at": datetime(2026, 7, 30, 12, 5),
            "sample_paths": json.dumps([{"label": "headshot", "path": "images/a/h.webp"}]),
            "samples_generated_at": datetime(2026, 7, 30, 11, 0), "sample_regen_count": 2,
            "created_at": None, "updated_at": None,
        }
        conn, _ = _conn(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_training
            result = get_avatar_training(3, 4)
        assert result["gender_presentation"] == "man"
        assert result["approval_status"] == "approved"
        assert result["sample_paths"] == [{"label": "headshot", "path": "images/a/h.webp"}]
        assert result["sample_regen_count"] == 2

    def test_unparseable_sample_json_degrades_to_empty(self):
        """A corrupt JSON blob must not 500 the avatars page — it reads as 'no samples yet'."""
        row = {"id": 4, "training_id": "t4", "model_ref": "m", "trigger_word": "TOK",
               "status": "succeeded", "sample_paths": "{not json", "created_at": None}
        conn, _ = _conn(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_training
            assert get_avatar_training(3, 4)["sample_paths"] == []

    def test_scoped_to_owner(self):
        conn, cur = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_training
            assert get_avatar_training(3, 99) is None
        _, params = cur.execute.call_args[0]
        assert params == (99, 3)

    def test_error_returns_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_training
            assert get_avatar_training(3, 4) is None


class TestUpdateAvatarAttributes:
    def test_normalizes_before_storing(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_attributes
            assert update_avatar_attributes(3, 4, " MAN ", "40s") is True
        sql, params = cur.execute.call_args[0]
        assert "attributes_confirmed_at = UTC_TIMESTAMP()" in sql
        assert params == ("man", "40s", 4, 3)

    def test_unrecognized_values_are_stored_as_null_not_guessed(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_attributes
            update_avatar_attributes(3, 4, "cisgender-ish", "middle aged")
        _, params = cur.execute.call_args[0]
        assert params == (None, None, 4, 3)

    def test_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_attributes
            assert update_avatar_attributes(3, 4, "man", "40s") is False


class TestSetAvatarApproval:
    def test_approve_stamps_approved_at(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_avatar_approval
            assert set_avatar_approval(3, 4, "approved") is True
        sql, params = cur.execute.call_args[0]
        assert "approved_at = UTC_TIMESTAMP()" in sql and params == ("approved", 4, 3)

    def test_reject_also_deactivates(self):
        """Leaving a rejected likeness active would keep publishing what the user rejected."""
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_avatar_approval
            assert set_avatar_approval(3, 4, "rejected") is True
        sql, _ = cur.execute.call_args[0]
        assert "is_active = 0" in sql and "approved_at = NULL" in sql

    def test_unknown_status_refused_without_touching_the_db(self):
        conn, cur = _conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_avatar_approval
            assert set_avatar_approval(3, 4, "sort-of") is False
        cur.execute.assert_not_called()

    def test_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_avatar_approval
            assert set_avatar_approval(3, 4, "approved") is False


class TestUpdateAvatarSamples:
    def test_stores_the_paths_without_touching_the_counter(self):
        """The re-roll is reserved before the render, never counted after it."""
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_samples
            assert update_avatar_samples(4, [{"label": "a", "path": "p"}]) is True
        sql, params = cur.execute.call_args[0]
        assert "sample_regen_count" not in sql
        assert json.loads(params[0]) == [{"label": "a", "path": "p"}]

    def test_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_samples
            assert update_avatar_samples(4, []) is False


class TestSampleRenderClaim:
    """The cap has to be decided IN the write: reading a counter that only moves when a render
    finishes let a double-click queue two full renders against the same reading.
    """

    def test_regeneration_claim_increments_under_the_cap(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_avatar_sample_render
            assert claim_avatar_sample_render(3, 4, regeneration=True, max_regenerations=3) is True
        sql, params = cur.execute.call_args[0]
        assert "sample_regen_count = sample_regen_count + 1" in sql
        assert "sample_regen_count < %s" in sql
        assert params == (4, 3, 3)

    def test_regeneration_claim_lost_at_the_cap(self):
        conn, _ = _conn(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_avatar_sample_render
            assert claim_avatar_sample_render(3, 4, regeneration=True, max_regenerations=3) is False

    def test_first_render_claim_is_conditional_on_no_prior_render(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_avatar_sample_render
            assert claim_avatar_sample_render(3, 4) is True
        sql, params = cur.execute.call_args[0]
        assert "samples_generated_at IS NULL" in sql and "sample_paths IS NULL" in sql
        assert params == (4, 3)

    def test_second_poll_mid_render_loses_the_claim(self):
        conn, _ = _conn(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_avatar_sample_render
            assert claim_avatar_sample_render(3, 4) is False

    def test_claim_error_fails_closed(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import claim_avatar_sample_render
            assert claim_avatar_sample_render(3, 4) is False

    def test_release_gives_a_regeneration_back(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import release_avatar_sample_render
            assert release_avatar_sample_render(3, 4, regeneration=True) is True
        sql, params = cur.execute.call_args[0]
        assert "GREATEST(sample_regen_count - 1, 0)" in sql
        assert params == (4, 3)

    def test_release_reopens_the_first_render_only_while_no_samples_exist(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import release_avatar_sample_render
            assert release_avatar_sample_render(3, 4) is True
        sql, _ = cur.execute.call_args[0]
        assert "samples_generated_at = NULL" in sql and "sample_paths IS NULL" in sql

    def test_release_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import release_avatar_sample_render
            assert release_avatar_sample_render(3, 4, regeneration=True) is False


class TestAvatarPreferences:
    def test_reads_every_flag(self):
        row = {"avatar_disabled": 0, "avatar_use_post_image": 1,
               "avatar_use_carousel": 0, "avatar_use_video": 1, "avatar_use_newsletter": 1}
        conn, _ = _conn(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_preferences
            prefs = get_avatar_preferences(3)
        assert prefs == {"avatar_disabled": False, "avatar_use_post_image": True,
                         "avatar_use_carousel": False, "avatar_use_video": True,
                         "avatar_use_newsletter": True}

    def test_missing_user_returns_all_off(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_preferences
            assert not any(get_avatar_preferences(3).values())

    def test_db_error_returns_all_off(self):
        """Degrading to 'don't use the avatar' is the safe direction."""
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_preferences
            assert not any(get_avatar_preferences(3).values())

    def test_update_only_writes_supplied_flags(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_preferences
            assert update_avatar_preferences(3, {"avatar_use_video": True}) is True
        sql, params = cur.execute.call_args[0]
        assert sql == "UPDATE users SET avatar_use_video = %s WHERE id = %s"
        assert params == (1, 3)

    def test_update_ignores_unknown_keys(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_preferences
            assert update_avatar_preferences(3, {"is_admin": True}) is False
        cur.execute.assert_not_called()

    def test_update_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_preferences
            assert update_avatar_preferences(3, {"avatar_disabled": True}) is False


class TestPostAvatarFlags:
    @pytest.mark.parametrize("stored,expected", [((1,), True), ((0,), False), ((None,), None)])
    def test_use_avatar_is_three_valued(self, stored, expected):
        conn, _ = _conn(fetch_one=stored)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_use_avatar
            assert get_post_use_avatar(5) is expected

    def test_use_avatar_without_post_id_is_none(self):
        from cqc_lem.utilities.db import get_post_use_avatar
        assert get_post_use_avatar(None) is None

    def test_use_avatar_error_is_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_use_avatar
            assert get_post_use_avatar(5) is None

    @pytest.mark.parametrize("value,stored", [(True, 1), (False, 0), (None, None)])
    def test_update_post_use_avatar(self, value, stored):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_post_use_avatar
            assert update_post_use_avatar(5, value) is True
        _, params = cur.execute.call_args[0]
        assert params == (stored, 5)

    def test_update_post_use_avatar_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_post_use_avatar
            assert update_post_use_avatar(5, True) is False

    def test_mark_and_read_avatar_media(self):
        conn, cur = _conn(rowcount=1, fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_post_avatar_media, post_used_avatar_media
            assert mark_post_avatar_media(5) is True
            assert post_used_avatar_media(5) is True
        assert "avatar_media = 1" in cur.execute.call_args_list[0][0][0]

    def test_avatar_media_helpers_no_op_without_post_id(self):
        from cqc_lem.utilities.db import mark_post_avatar_media, post_used_avatar_media
        assert mark_post_avatar_media(None) is False
        assert post_used_avatar_media(None) is False

    def test_avatar_media_errors_are_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_post_avatar_media, post_used_avatar_media
            assert mark_post_avatar_media(5) is False
            assert post_used_avatar_media(5) is False


class TestInsertPostUseAvatar:
    @pytest.mark.parametrize("value,stored", [(True, 1), (False, 0), (None, None)])
    def test_compose_choice_is_persisted(self, value, stored):
        from cqc_lem.utilities.db import PostStatus, PostType
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch(f"{_DB}.get_user_id", return_value=3):
            from cqc_lem.utilities.db import insert_post
            assert insert_post("a@b.c", "body", datetime(2026, 7, 30, 9, 0), PostType.TEXT,
                               status=PostStatus.PENDING, use_avatar=value) is True
        sql, params = cur.execute.call_args[0]
        # Read the column by NAME, not by position: the INSERT has grown a column since (#1030)
        # and a positional assertion silently starts checking a different field.
        columns = [c.strip() for c in
                   sql.split("(", 1)[1].split(")", 1)[0].split(",")]
        assert params[columns.index("use_avatar")] is stored
