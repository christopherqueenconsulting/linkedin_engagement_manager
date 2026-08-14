"""Integration tests for the group-post preview/edit API (issue #932).

The weekly group post used to be written and published inside one Selenium run, so a user only ever
saw the per-group toggle — never the text. These endpoints are the preview: the draft is readable
before it ships, editable in place, and skippable. Everything is scoped to the caller's OWN open
draft — the request never names a draft id, so one session can't reach another user's post.
"""

from unittest.mock import patch

import pytest
from freezegun import freeze_time

pytestmark = pytest.mark.integration

_API = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"

_DRAFT = {"id": 11, "user_id": 1, "group_id": "g1", "group_name": "AI Leaders",
          "content": "A useful insight.", "status": "ready", "media_url": None, "media_type": None,
          "created_at": "2026-08-09T15:00:00",
          "updated_at": "2026-08-09T15:00:00", "published_at": None}

# The draft above was written by the Sunday beat for the Tuesday 2026-08-11 15:00 UTC slot, so the
# undo window on a skip is open before that instant and closed from it on (issue #1415).
_BEFORE_SLOT = "2026-08-10T12:00:00"
_AFTER_SLOT = "2026-08-12T09:00:00"

_MEDIA_URL = "http://api/api/assets?file_name=images/post_previews/1/img_abc.png"


class TestGetGroupPostDraft:
    def test_returns_the_queued_post_text(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"]["content"] == "A useful insight."
        assert r.json()["detail"]["group_name"] == "AI Leaders"

    def test_nothing_queued_is_null_not_an_error(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=None):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"] is None

    def test_401_without_a_session(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=None), \
             patch(f"{_USER}.get_current_group_post_draft") as read:
            r = api_client.get("/api/user/group-post-draft?session_token=bad")
        assert r.status_code == 401
        read.assert_not_called()


class TestPutGroupPostDraft:
    def test_saves_the_users_rewrite(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "My own words."})
        assert r.status_code == 200
        saved.assert_called_once_with(11, content="My own words.", status=None)

    def test_skipping_cancels_this_weeks_post(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "skipped"})
        assert r.status_code == 200
        assert str(saved.call_args.kwargs["status"]) == "skipped"

    def test_the_draft_is_resolved_from_the_session_not_the_request(self, api_client):
        """A caller-supplied id would let one session edit another user's post — it is ignored."""
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)) as read, \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "Mine.", "id": 99})
        assert r.status_code == 200
        read.assert_called_once_with(1)
        assert saved.call_args[0][0] == 11

    @pytest.mark.parametrize("body", [
        {"content": "   "},                    # emptying it is not how you cancel
        {"content": "x" * 3001},               # past LinkedIn's own post cap
        {"status": "published"},               # only the publish run may claim a ship
        {},                                    # nothing asked for
    ])
    def test_rejects_a_write_that_would_corrupt_the_queued_post(self, body, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft", json={"session_token": "tok", **body})
        assert r.status_code == 422
        saved.assert_not_called()

    def test_404_when_nothing_is_queued(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=None), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "Mine."})
        assert r.status_code == 404
        saved.assert_not_called()

    def test_a_failed_write_is_a_500(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.update_group_post_draft", return_value=False):
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "Mine."})
        assert r.status_code == 500

    def test_401_without_a_session(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=None), \
             patch(f"{_USER}.get_current_group_post_draft") as read, \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "bad", "content": "Mine."})
        assert r.status_code == 401
        read.assert_not_called()
        saved.assert_not_called()


@freeze_time(_BEFORE_SLOT)
class TestGroupPostDraftStatuses:
    """The studio could only edit or cancel (issue #1224).

    A skipped draft is now visible and restorable, because skipping by accident used to be permanent
    for that week. The clock is frozen inside this week's undo window, which is what makes a restore
    legal at all (issue #1415).
    """

    def test_a_skipped_draft_is_still_shown_so_it_can_be_restored(self, api_client):
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"]["status"] == "skipped"

    def test_the_draft_carries_the_best_practices_the_prompt_follows(self, api_client):
        from cqc_lem.utilities.ai.content_framework import GROUP_POST_BEST_PRACTICES
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.json()["detail"]["best_practices"] == list(GROUP_POST_BEST_PRACTICES)

    def test_restoring_puts_the_post_back_in_the_queue(self, api_client):
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=None), \
             patch(f"{_USER}.get_post_enabled_group_ids", return_value=["g1"]), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 200
        assert r.json()["detail"] == "Group post restored"
        assert str(saved.call_args.kwargs["status"]) == "ready"

    def test_restoring_a_draft_the_publish_beat_dropped_is_refused_with_the_reason(self,
                                                                                  api_client):
        """Not every skipped draft is one the USER skipped.

        `auto_group_posts` skips a draft whose group has since been switched off for posting, and
        that row is now visible in the studio. Restoring it would report success and then be dropped
        again at the next weekly slot, every week, with nothing on screen saying why.
        """
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=None), \
             patch(f"{_USER}.get_post_enabled_group_ids", return_value=["other-group"]), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 409
        assert "no longer takes posts" in r.json()["detail"]
        saved.assert_not_called()

    def test_unreadable_post_switches_do_not_block_a_restore(self, api_client):
        """None is "we could not tell", not "opted out".

        The publish beat holds the draft on that read too, so refusing here would turn a transient
        DB fault into a lost week.
        """
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=None), \
             patch(f"{_USER}.get_post_enabled_group_ids", return_value=None), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 200
        assert str(saved.call_args.kwargs["status"]) == "ready"

    def test_restoring_is_refused_when_a_newer_draft_is_already_queued(self, api_client):
        """ONE open draft per user is what stops the weekly beat replacing a post being edited.

        A restore that would make a second one is refused rather than leaving two publishable rows.
        """
        skipped = {**_DRAFT, "id": 9, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 409
        saved.assert_not_called()

    def test_the_draft_says_the_undo_is_still_live_and_when_it_closes(self, api_client):
        """The SPA offers "Undo skip" off this flag, so it has to travel with the draft."""
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"]["can_undo_skip"] is True
        assert r.json()["detail"]["undo_deadline"].startswith("2026-08-11T15:00:00")

    def test_a_queued_draft_is_not_undoable_because_it_was_never_skipped(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.json()["detail"]["can_undo_skip"] is False

    def test_undoing_restores_the_same_row_rather_than_drafting_a_second(self, api_client):
        """ONE open draft per user, carried forward (issue #932).

        The recovery the reporter needed is the draft they set aside, not a regenerated one — a
        second row is exactly what the lane's invariant forbids.
        """
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=None), \
             patch(f"{_USER}.get_post_enabled_group_ids", return_value=["g1"]), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 200
        assert saved.call_args[0][0] == _DRAFT["id"]
        assert saved.call_args.kwargs["content"] is None

    def test_undoing_a_week_that_was_never_skipped_is_an_accepted_no_op(self, api_client):
        """Expected, so it is a DEBUG line and none of the restore refusals apply to it."""
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.get_open_group_post_draft") as open_read, \
             patch(f"{_USER}.get_post_enabled_group_ids") as switches, \
             patch(f"{_USER}.update_group_post_draft", return_value=True):
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 200
        open_read.assert_not_called()
        switches.assert_not_called()

    @pytest.mark.parametrize("status", ["published", "failed", "draft"])
    def test_only_the_users_own_statuses_are_accepted(self, api_client, status):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": status})
        assert r.status_code == 422
        saved.assert_not_called()


@freeze_time(_AFTER_SLOT)
class TestGroupPostSkipUndoWindow:
    """The undo closes when the publish beat for that week has run (issue #1415).

    Until then "Skip this week" is reversible; after it the week is spent, and putting the draft
    back would ship a post written for a week that has passed.
    """

    def test_undo_is_refused_once_the_publish_beat_has_run(self, api_client):
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=None), \
             patch(f"{_USER}.get_post_enabled_group_ids", return_value=["g1"]), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 409
        assert "the skip is final" in r.json()["detail"]
        saved.assert_not_called()

    def test_the_draft_says_the_undo_has_closed_so_the_spa_can_say_so(self, api_client):
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped):
            r = api_client.get("/api/user/group-post-draft?session_token=tok")
        assert r.json()["detail"]["can_undo_skip"] is False

    def test_editing_a_closed_draft_is_still_allowed(self, api_client):
        """The window bounds the UNDO, not the row.

        The next slot's draft is a separate row, and a text edit on this one publishes nothing by
        itself.
        """
        skipped = {**_DRAFT, "status": "skipped"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "My own words."})
        assert r.status_code == 200
        saved.assert_called_once_with(11, content="My own words.", status=None)

    def test_an_unreadable_creation_time_leaves_the_undo_open(self, api_client):
        """Fails OPEN.

        The reported bug is a user stuck with an accidental skip, and a restore is an explicit
        action that publishes at the next slot rather than silently.
        """
        skipped = {**_DRAFT, "status": "skipped", "created_at": None}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=skipped), \
             patch(f"{_USER}.get_open_group_post_draft", return_value=None), \
             patch(f"{_USER}.get_post_enabled_group_ids", return_value=["g1"]), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "status": "ready"})
        assert r.status_code == 200
        assert str(saved.call_args.kwargs["status"]) == "ready"


class TestGroupPostDraftMedia:
    """A group post can ship with a native image or video (issue #1224).

    The URL is caller input on a field the publish run later hands to LinkedIn, so it passes the same
    ownership gate a compose-time post image does.
    """

    def test_attaching_media_records_the_kind_from_the_stored_file(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.owns_post_image_url", return_value=True), \
             patch(f"{_USER}.post_image_abs_path", return_value="/assets/img_abc.png"), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "media_url": _MEDIA_URL})
        assert r.status_code == 200
        assert saved.call_args.kwargs["media_url"] == _MEDIA_URL
        assert str(saved.call_args.kwargs["media_type"]) == "image"

    def test_a_url_we_did_not_issue_this_user_is_refused(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.owns_post_image_url", return_value=False), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok",
                                    "media_url": "https://evil.example/payload.png"})
        assert r.status_code == 400
        saved.assert_not_called()

    def test_media_that_is_no_longer_on_disk_is_refused(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.owns_post_image_url", return_value=True), \
             patch(f"{_USER}.post_image_abs_path", return_value=None), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "media_url": _MEDIA_URL})
        assert r.status_code == 400
        saved.assert_not_called()

    def test_a_file_that_is_neither_image_nor_video_is_refused(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=dict(_DRAFT)), \
             patch(f"{_USER}.owns_post_image_url", return_value=True), \
             patch(f"{_USER}.post_image_abs_path", return_value="/assets/notes.txt"), \
             patch(f"{_USER}.update_group_post_draft") as saved:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "media_url": _MEDIA_URL})
        assert r.status_code == 400
        saved.assert_not_called()

    def test_removing_media_drops_both_the_row_pointer_and_the_file(self, api_client):
        attached = {**_DRAFT, "media_url": _MEDIA_URL, "media_type": "image"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=attached), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved, \
             patch(f"{_USER}.remove_post_image_file") as removed:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "remove_media": True})
        assert r.status_code == 200
        assert saved.call_args.kwargs == {"content": None, "status": None,
                                          "media_url": None, "media_type": None}
        removed.assert_called_once_with(_MEDIA_URL)

    def test_replacing_media_deletes_only_the_file_it_replaced(self, api_client):
        attached = {**_DRAFT, "media_url": "http://api/api/assets?file_name=images/post_previews/1/old.png",
                    "media_type": "image"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=attached), \
             patch(f"{_USER}.owns_post_image_url", return_value=True), \
             patch(f"{_USER}.post_image_abs_path", return_value="/assets/img_abc.png"), \
             patch(f"{_USER}.update_group_post_draft", return_value=True), \
             patch(f"{_USER}.remove_post_image_file") as removed:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "media_url": _MEDIA_URL})
        assert r.status_code == 200
        removed.assert_called_once_with(attached["media_url"])

    def test_a_text_edit_never_touches_the_attached_media(self, api_client):
        """The three-valued media argument, from the API side.

        Saving text must not detach the image the author attached a minute earlier.
        """
        attached = {**_DRAFT, "media_url": _MEDIA_URL, "media_type": "image"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_current_group_post_draft", return_value=attached), \
             patch(f"{_USER}.update_group_post_draft", return_value=True) as saved, \
             patch(f"{_USER}.remove_post_image_file") as removed:
            r = api_client.put("/api/user/group-post-draft",
                              json={"session_token": "tok", "content": "My own words."})
        assert r.status_code == 200
        assert "media_url" not in saved.call_args.kwargs
        removed.assert_not_called()
