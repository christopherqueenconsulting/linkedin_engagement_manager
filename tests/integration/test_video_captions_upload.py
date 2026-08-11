"""Issue #1278: the captioned video actually reaches LinkedIn, and the columns behind it are real.

Two halves that only an integration lane can answer. The first is the SQL: `posts.caption_text` /
`posts.caption_srt_url` and `users.avatar_caption_overlay` are a migration this branch adds, and a
unit test with a mocked cursor would pass against a schema that never got them. The second is the
upload path: captions are burned INTO the stored file, so what proves a viewer sees them is that
`post_to_linkedin` uploads the bytes at `posts.video_url` — the ones the burn rewrote — rather than
re-deriving a source anywhere.
"""
import os
from contextlib import ExitStack
from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "video-captions-1278@example.test"
_POSTING = "cqc_lem.app.engagement.posting"


def _schema_available() -> bool:
    """A reachable server is not enough — the caption columns come from this branch's migration."""
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM posts LIKE 'caption_srt_url'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=()):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the video-caption integration test")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


@pytest.fixture
def post_id(user_id):
    return _exec(
        "INSERT INTO posts (user_id, content, scheduled_time, post_type, status) "
        "VALUES (%s, %s, NOW(), 'video', 'approved')",
        (user_id, "Most teams ship AI nobody asked for.\n\nHere is the fix."))


class TestCaptionColumns:
    def test_captions_round_trip(self, post_id):
        assert db.update_db_post_captions(post_id, "Most teams ship AI\nnobody asked for.",
                                          "https://lem.test/api/assets?file_name=videos/captions/a.srt")
        row = db.get_post_captions(post_id)
        assert row["caption_text"] == "Most teams ship AI\nnobody asked for."
        assert row["caption_srt_url"].endswith("videos/captions/a.srt")

    def test_an_uncaptioned_post_reads_as_none_not_empty_string(self, post_id):
        assert db.get_post_captions(post_id) == {"caption_text": None, "caption_srt_url": None}

    def test_a_sidecar_without_a_burn_is_still_recorded(self, post_id):
        """The avatar opt-out path writes the sidecar and no burned text."""
        assert db.update_db_post_captions(post_id, None, "https://lem.test/x.srt")
        assert db.get_post_captions(post_id)["caption_text"] is None
        assert db.get_post_captions(post_id)["caption_srt_url"] == "https://lem.test/x.srt"

    def test_an_unknown_post_reads_empty_rather_than_raising(self, user_id):
        assert db.get_post_captions(10_000_000) == {"caption_text": None, "caption_srt_url": None}


class TestAvatarCaptionOverlayPreference:
    def test_defaults_off_for_a_new_account(self, user_id):
        assert db.get_avatar_preferences(user_id)["avatar_caption_overlay"] is False

    def test_opting_in_persists_without_touching_the_render_surfaces(self, user_id):
        assert db.update_avatar_preferences(user_id, {"avatar_caption_overlay": True})
        prefs = db.get_avatar_preferences(user_id)
        assert prefs["avatar_caption_overlay"] is True
        # Permission to caption is not permission to render — the surfaces stay off.
        assert prefs["avatar_use_video"] is False


class TestUploadPath:
    def test_the_stored_captioned_file_is_what_gets_uploaded(self, tmp_path):
        """The burn rewrites the file in place, so uploading `posts.video_url` uploads the captions."""
        from cqc_lem.app.engagement.posting import post_to_linkedin

        captioned = tmp_path / "clip.mp4"
        captioned.write_bytes(b"CAPTIONED BYTES")
        stored_url = str(captioned)

        with ExitStack() as stack:
            patched = {
                "get_post_status": {"return_value": "approved"},
                "get_post_manual_publish": {"return_value": False},
                "get_user_password_pair_by_id": {"return_value": ("u@example.com", "pw")},
                "get_post_content": {"return_value": "Most teams ship AI nobody asked for."},
                "get_engagement_preferences": {"return_value": {}},
                "get_post_type": {"return_value": db.PostType.VIDEO},
                "get_post_video_url": {"return_value": stored_url},
                "share_on_linkedin": {"return_value": "urn:li:ugcPost:1"},
                "insert_new_log": {},
                "update_db_post_status": {},
                "update_db_post_content": {},
                "update_db_post_first_comment_link": {},
                "sweep_reply_comments": {},
                "auto_seed_comment_on_post": {},
                "auto_second_wave_comment": {},
            }
            mocks = {name: stack.enter_context(patch(f"{_POSTING}.{name}", **kw))
                     for name, kw in patched.items()}
            post_to_linkedin.run(1, 99)
        share = mocks["share_on_linkedin"]

        assert share.call_args.args[2] == stored_url
        assert open(share.call_args.args[2], "rb").read() == b"CAPTIONED BYTES"
        assert os.path.basename(stored_url) == "clip.mp4"
