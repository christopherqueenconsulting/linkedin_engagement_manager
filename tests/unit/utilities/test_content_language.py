"""Unit tests for per-user content language resolution (issue #548).

Veo has no language API parameter, so the language a premium video speaks/ambiences in is
whatever the prompt says. These cover where that language comes from.
"""

import pytest
import mysql.connector
from unittest.mock import patch

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.utilities.db.get_db_connection"


class TestLanguageName:
    def test_known_subtag_without_region(self):
        from cqc_lem.utilities.geocoding import language_name
        assert language_name("es") == "Spanish"

    def test_region_is_preserved_because_it_changes_the_audio(self):
        from cqc_lem.utilities.geocoding import language_name
        assert language_name("pt-BR") == "Portuguese (pt-BR)"
        assert language_name("en_GB") == "English (en_GB)"

    def test_unknown_tag_passes_through_rather_than_being_guessed(self):
        from cqc_lem.utilities.geocoding import language_name
        assert language_name("xx-YY") == "xx-YY"

    def test_empty_falls_back_to_the_default(self):
        from cqc_lem.utilities.geocoding import language_name
        assert language_name(None) == "English (en-US)"
        assert language_name("  ") == "English (en-US)"


class TestGetUserContentLanguage:
    def test_explicit_setting_wins_over_login_location(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = ("es-ES", "en-US")
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(1) == "es-ES"

    def test_falls_back_to_the_login_location_locale(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (None, "en-GB")
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(1) == "en-GB"

    def test_blank_values_fall_through_to_english(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = ("  ", None)
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(1) == "en-US"

    def test_missing_user_row(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(404) == "en-US"

    def test_no_user_id_never_touches_the_db(self):
        with patch(_GET_CONN) as conn:
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(None) == "en-US"
        conn.assert_not_called()

    def test_query_error_is_fail_soft(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("fail")
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(1) == "en-US"

    def test_connection_error_is_fail_soft(self):
        # Callers sit inside media-generation try/except blocks that fall back to stock footage —
        # a DB blip must cost the user their language, not their video.
        with patch(_GET_CONN, side_effect=RuntimeError("no db")):
            from cqc_lem.utilities.db import get_user_content_language
            assert get_user_content_language(1) == "en-US"


class TestUpdateUserPreferencesLanguage:
    def test_omitted_language_leaves_the_column_alone(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, 90, True) is True
        sql = mock_database_connection["cursor"].execute.call_args[0][0]
        assert "content_language" not in sql

    def test_language_is_persisted_when_sent(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            from cqc_lem.utilities.db import update_user_preferences
            update_user_preferences(1, 90, True, content_language=" pt-BR ")
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert "content_language = %s" in sql
        assert "pt-BR" in params

    def test_empty_string_clears_back_to_the_location_default(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            from cqc_lem.utilities.db import update_user_preferences
            update_user_preferences(1, 90, True, content_language="")
        sql, params = mock_database_connection["cursor"].execute.call_args[0]
        assert "content_language = %s" in sql
        assert None in params
