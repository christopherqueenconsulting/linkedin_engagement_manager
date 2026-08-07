"""Unit tests for store_linkedin_li_at (cookie-session ingest helper)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


class TestStoreLinkedInLiAt:
    def test_stores_li_at_cookie(self):
        with patch(f"{_DB}.get_user_email", return_value="u@e.com"), \
             patch(f"{_DB}.store_cookies") as store:
            from cqc_lem.utilities.db import store_linkedin_li_at
            assert store_linkedin_li_at(7, "TOKENvalue1234567890abc") is True
        email, cookies = store.call_args.args
        assert email == "u@e.com"
        li = cookies[0]
        assert li["name"] == "li_at" and li["value"] == "TOKENvalue1234567890abc"
        assert li["domain"] == ".linkedin.com" and li["secure"] and li["httpOnly"]
        assert len(cookies) == 1

    def test_includes_jsessionid_when_provided(self):
        with patch(f"{_DB}.get_user_email", return_value="u@e.com"), \
             patch(f"{_DB}.store_cookies") as store:
            from cqc_lem.utilities.db import store_linkedin_li_at
            store_linkedin_li_at(7, "TOKENvalue1234567890abc", jsessionid="ajax:9")
        cookies = store.call_args.args[1]
        names = {c["name"] for c in cookies}
        assert names == {"li_at", "JSESSIONID"}
        js = next(c for c in cookies if c["name"] == "JSESSIONID")
        assert js["value"] == "ajax:9" and js["httpOnly"] is False

    def test_returns_false_when_no_email(self):
        with patch(f"{_DB}.get_user_email", return_value=None), \
             patch(f"{_DB}.store_cookies") as store:
            from cqc_lem.utilities.db import store_linkedin_li_at
            assert store_linkedin_li_at(99, "TOKENvalue1234567890abc") is False
        store.assert_not_called()

    def test_returns_false_when_store_raises(self):
        with patch(f"{_DB}.get_user_email", return_value="u@e.com"), \
             patch(f"{_DB}.store_cookies", side_effect=Exception("db down")):
            from cqc_lem.utilities.db import store_linkedin_li_at
            assert store_linkedin_li_at(7, "TOKENvalue1234567890abc") is False
