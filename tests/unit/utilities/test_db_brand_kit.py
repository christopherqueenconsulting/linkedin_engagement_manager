"""Unit tests for the brand-kit repository functions (`get_brand_kit` / `set_brand_kit`)."""

import json
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_USERS = "cqc_lem.platform.db.repositories.users"


class TestGetBrandKit:
    def test_decodes_a_json_string_and_drops_invalid_fields(self, fake_cursor):
        stored = json.dumps({"primary_hex": "#E9D437", "accent_hex": "nope", "avoid": ["gears"]})
        conn, cursor = fake_cursor(fetch_one={"brand_kit": stored})
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_brand_kit
            assert get_brand_kit(1) == {"primary_hex": "#e9d437", "avoid": ["gears"]}
        sql, params = cursor.execute.call_args[0]
        assert "SELECT brand_kit FROM engagement_preferences" in sql and params == (1,)

    def test_accepts_an_already_decoded_dict_and_bytes(self, fake_cursor):
        for raw in ({"font_vibe": "bold"}, json.dumps({"font_vibe": "bold"}).encode()):
            conn, _ = fake_cursor(fetch_one={"brand_kit": raw})
            with patch(_CONN, return_value=conn):
                from cqc_lem.utilities.db import get_brand_kit
                assert get_brand_kit(1) == {"font_vibe": "bold"}

    @pytest.mark.parametrize("row", [None, {"brand_kit": None}])
    def test_missing_row_or_null_kit_is_none_at_debug(self, fake_cursor, row):
        conn, _ = fake_cursor(fetch_one=row)
        with patch(_CONN, return_value=conn), \
             patch(f"{_USERS}.log_debug") as debug, patch(f"{_USERS}.log_warning") as warn:
            from cqc_lem.utilities.db import get_brand_kit
            assert get_brand_kit(1) is None
        debug.assert_called_once()
        warn.assert_not_called()

    def test_corrupt_json_is_none_with_a_warning(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"brand_kit": "{not json"})
        with patch(_CONN, return_value=conn), patch(f"{_USERS}.log_warning") as warn:
            from cqc_lem.utilities.db import get_brand_kit
            assert get_brand_kit(1) is None
        warn.assert_called_once()

    def test_kit_with_nothing_valid_is_none(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"brand_kit": json.dumps({"primary_hex": "bad"})})
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_brand_kit
            assert get_brand_kit(1) is None

    def test_read_failure_is_none_with_a_warning(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn), patch(f"{_USERS}.log_warning") as warn:
            from cqc_lem.utilities.db import get_brand_kit
            assert get_brand_kit(1) is None
        warn.assert_called_once()


class TestSetBrandKit:
    """`set_brand_kit` is the upsert, like `set_default_video_quality` — no other column moves."""

    def _saved(self, fake_cursor, kit, stored=None):
        conn, cursor = fake_cursor(fetch_one=stored, rowcount=1)
        with patch(_CONN, return_value=conn), \
             patch(f"{_USERS}.max_catchup_touches_allowed", return_value=5):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, set_brand_kit
            assert set_brand_kit(3, kit) is True
        sql, params = cursor.execute.call_args[0]
        assert "ON DUPLICATE KEY UPDATE" in sql and "brand_kit=VALUES(brand_kit)" in sql
        return dict(zip(_ENGAGEMENT_COLS, params[1:]))

    def test_stores_only_the_valid_fields_as_json(self, fake_cursor):
        saved = self._saved(fake_cursor, {"primary_hex": "#E9D437", "accent_hex": "bad"})
        assert json.loads(saved["brand_kit"]) == {"primary_hex": "#e9d437"}

    @pytest.mark.parametrize("kit", [{}, None, {"primary_hex": "bad"}])
    def test_empty_or_invalid_kit_stores_null(self, fake_cursor, kit):
        assert self._saved(fake_cursor, kit)["brand_kit"] is None

    def test_keeps_every_other_saved_preference(self, fake_cursor):
        saved = self._saved(fake_cursor, {"font_vibe": "bold"},
                            stored={"tone": "warm", "max_comments_per_day": 7})
        assert saved["tone"] == "warm" and saved["max_comments_per_day"] == 7

    def test_an_unreadable_row_aborts_the_write(self, fake_cursor):
        conn, cursor = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import set_brand_kit
            assert set_brand_kit(3, {"font_vibe": "bold"}) is False
        assert cursor.execute.call_count == 1  # the SELECT only — no upsert over an unknown row


class TestBrandKitInEngagementPreferences:
    def test_saved_kit_is_decoded_on_the_preferences_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"brand_kit": json.dumps({"font_vibe": "bold"})})
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["brand_kit"] == {"font_vibe": "bold"}

    def test_no_row_defaults_to_no_kit(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["brand_kit"] is None

    def test_omitting_the_kit_keeps_the_stored_one(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one={"brand_kit": json.dumps({"font_vibe": "bold"})})
        with patch(_CONN, return_value=conn), \
             patch(f"{_USERS}.max_catchup_touches_allowed", return_value=5):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            assert update_engagement_preferences(3, {"tone": "dry"}) is True
        saved = dict(zip(_ENGAGEMENT_COLS, cursor.execute.call_args[0][1][1:]))
        assert json.loads(saved["brand_kit"]) == {"font_vibe": "bold"}
