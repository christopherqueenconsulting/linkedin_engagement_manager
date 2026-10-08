"""Issue #2306: `add_linkedin_profile` against a live MySQL server.

`profiles` carries three UNIQUE keys (profile_url, email, user_id), and the 1062 this fixes only
exists when two different rows hold two of them — a unit test with a mocked cursor cannot reproduce
it. The shape is the one production hit: the user's profile URL changed, the anonymous by-URL scrape
cached the NEW url as its own unowned row, and the owned upsert then landed on that row by
`profile_url` and collided with the user's real row on `email`.
"""

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "profile-upsert-2306@example.test"
_OTHER_EMAIL = "profile-upsert-2306-other@example.test"
_OLD_URL = "https://www.linkedin.com/in/profile-upsert-2306-old/"
_NEW_URL = "https://www.linkedin.com/in/profile-upsert-2306-new/"


def _schema_available() -> bool:
    """A reachable server is not enough — this needs the migrated `profiles` table."""
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM profiles LIKE 'synthesis'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=(), fetch: bool = False):
    connection = db.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows
    finally:
        cursor.close()
        connection.close()


def _profile(url: str, email=None):
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile
    return LinkedInProfile(full_name="Jane Doe", email=email, profile_url=url)


def _cleanup() -> None:
    _exec("DELETE FROM profiles WHERE profile_url IN (%s, %s) OR email IN (%s, %s)",
          (_OLD_URL, _NEW_URL, _EMAIL, _OTHER_EMAIL))
    _exec("DELETE FROM users WHERE email IN (%s, %s)", (_EMAIL, _OTHER_EMAIL))


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the profile-upsert integration test")
    _cleanup()
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _cleanup()


class TestOwnedUpsertAfterAUrlChange:
    def test_the_stray_scrape_row_no_longer_raises_a_duplicate_email(self, user_id):
        assert db.add_linkedin_profile(_profile(_OLD_URL, _EMAIL), user_id=user_id) is True
        db.set_profile_synthesis(user_id, "- brief distilled from the profile")
        # The anonymous by-URL scrape of the user's NEW url: no email, no owner.
        assert db.add_linkedin_profile(_profile(_NEW_URL)) is True

        assert db.add_linkedin_profile(_profile(_NEW_URL, _EMAIL), user_id=user_id) is True

        rows = _exec("SELECT profile_url, email, user_id FROM profiles "
                     "WHERE profile_url IN (%s, %s) OR email = %s", (_OLD_URL, _NEW_URL, _EMAIL),
                     fetch=True)
        assert rows == [{"profile_url": _NEW_URL, "email": _EMAIL, "user_id": user_id}]
        # The OWNED row was the one kept, so what lives on it survives the merge.
        assert db.get_profile_synthesis(user_id)[0] == "- brief distilled from the profile"

    def test_an_anonymous_rescrape_never_blanks_the_owned_email(self, user_id):
        db.add_linkedin_profile(_profile(_OLD_URL, _EMAIL), user_id=user_id)
        db.add_linkedin_profile(_profile(_OLD_URL))
        rows = _exec("SELECT email, user_id FROM profiles WHERE profile_url = %s", (_OLD_URL,),
                     fetch=True)
        assert rows == [{"email": _EMAIL, "user_id": user_id}]

    def test_a_row_owned_by_another_user_is_never_deleted(self, user_id):
        db.add_user(_OTHER_EMAIL, "x")
        other_id = db.get_user_id(_OTHER_EMAIL)
        db.add_linkedin_profile(_profile(_NEW_URL, _OTHER_EMAIL), user_id=other_id)
        db.add_linkedin_profile(_profile(_OLD_URL, _EMAIL), user_id=user_id)

        # A genuine conflict: the url belongs to someone else's account. It still fails, loudly.
        assert db.add_linkedin_profile(_profile(_NEW_URL, _EMAIL), user_id=user_id) is False
        rows = _exec("SELECT user_id FROM profiles WHERE profile_url = %s", (_NEW_URL,), fetch=True)
        assert rows == [{"user_id": other_id}]
