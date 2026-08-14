"""Unit tests for the occasion/milestone endpoints (issue #1074).

`POST /user/post/occasion` seeds a draft LEM writes; `POST /user/post/mark-posted` is the author
saying they published it by hand. Neither ever publishes, and mark-posted is deliberately narrow —
for an automatic post, 'posted' is written by the task that holds the LinkedIn URN.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_SESSION = "tok"
_USER = 11
_OCCASION = "We shipped the scheduling rewrite after four weeks of nights."


class TestCreateOccasionPost:
    def test_creates_a_manual_publish_row_and_dispatches_the_draft(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.insert_occasion_post", return_value=77) as insert, \
             patch("cqc_lem.app.run_content_plan.draft_occasion_post_task") as task:
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": "project_launch", "occasion": _OCCASION,
            })

        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail == {"post_id": 77, "archetype": "project_launch", "manual_publish": True}
        # A launch is a decision-stage announcement — the stage comes from the archetype, not a guess.
        assert insert.call_args[0][0] == _USER
        assert insert.call_args[0][2] == "decision"
        kwargs = task.apply_async.call_args.kwargs["kwargs"]
        assert kwargs == {"post_id": 77, "archetype": "project_launch", "occasion": _OCCASION}

    def test_milestone_lands_at_the_awareness_stage(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.insert_occasion_post", return_value=78) as insert, \
             patch("cqc_lem.app.run_content_plan.draft_occasion_post_task"):
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": "educational_milestone",
                "occasion": "Finished the AWS Solutions Architect certification this week.",
            })

        assert resp.status_code == 200
        assert insert.call_args[0][2] == "awareness"

    @pytest.mark.parametrize("archetype", ["case_snapshot", "", "celebrate"])
    def test_rejects_an_archetype_outside_the_occasion_family(self, api_client, archetype):
        # A normal archetype must not be reachable here: it would publish natively-only copy that
        # the scheduler is permanently barred from sending.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.insert_occasion_post") as insert, \
             patch("cqc_lem.app.run_content_plan.draft_occasion_post_task") as task:
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": archetype, "occasion": _OCCASION,
            })

        assert resp.status_code == 400
        insert.assert_not_called()
        task.apply_async.assert_not_called()

    def test_rejects_an_occasion_too_thin_to_write_from(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.insert_occasion_post") as insert:
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": "project_launch", "occasion": "shipped",
            })

        assert resp.status_code == 422  # min_length on the field
        insert.assert_not_called()

    def test_whitespace_only_padding_is_still_rejected(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.insert_occasion_post") as insert:
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": "project_launch",
                "occasion": "shipped          ",
            })

        assert resp.status_code == 400
        insert.assert_not_called()

    def test_no_session_is_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.routers.user.insert_occasion_post") as insert:
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": "project_launch", "occasion": _OCCASION,
            })

        assert resp.status_code == 401
        insert.assert_not_called()

    def test_a_failed_insert_never_dispatches_a_draft(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.insert_occasion_post", return_value=None), \
             patch("cqc_lem.app.run_content_plan.draft_occasion_post_task") as task:
            resp = api_client.post("/api/user/post/occasion", json={
                "session_token": _SESSION, "archetype": "project_launch", "occasion": _OCCASION,
            })

        assert resp.status_code == 500
        task.apply_async.assert_not_called()


class TestMarkPosted:
    def test_marks_a_native_draft_as_published(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_manual_publish", return_value=True), \
             patch("cqc_lem.api.routers.user.get_post_status", return_value="approved"), \
             patch("cqc_lem.api.routers.user.bulk_update_posts", return_value=True) as update:
            resp = api_client.post("/api/user/post/mark-posted",
                               json={"session_token": _SESSION, "post_id": 77})

        from cqc_lem.utilities.db import PostStatus
        assert resp.status_code == 200
        assert update.call_args.kwargs["status"] == PostStatus.POSTED
        # The write is scoped to the caller as well as checked against them (issue #914).
        assert update.call_args.kwargs["user_id"] == _USER

    def test_an_automatic_post_cannot_be_marked_by_hand(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_manual_publish", return_value=False), \
             patch("cqc_lem.api.routers.user.bulk_update_posts") as update:
            resp = api_client.post("/api/user/post/mark-posted",
                               json={"session_token": _SESSION, "post_id": 77})

        assert resp.status_code == 409
        update.assert_not_called()

    def test_already_published_is_a_409_not_a_silent_no_op(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_manual_publish", return_value=True), \
             patch("cqc_lem.api.routers.user.get_post_status", return_value="posted"), \
             patch("cqc_lem.api.routers.user.bulk_update_posts") as update:
            resp = api_client.post("/api/user/post/mark-posted",
                               json={"session_token": _SESSION, "post_id": 77})

        assert resp.status_code == 409
        update.assert_not_called()

    def test_another_users_post_is_not_found(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_user_id", return_value=_USER + 1), \
             patch("cqc_lem.api.routers.user.get_post_manual_publish", return_value=True), \
             patch("cqc_lem.api.routers.user.bulk_update_posts") as update:
            resp = api_client.post("/api/user/post/mark-posted",
                               json={"session_token": _SESSION, "post_id": 77})

        assert resp.status_code == 404
        update.assert_not_called()

    def test_a_failed_write_is_a_500(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_post_manual_publish", return_value=True), \
             patch("cqc_lem.api.routers.user.get_post_status", return_value="approved"), \
             patch("cqc_lem.api.routers.user.bulk_update_posts", return_value=False):
            resp = api_client.post("/api/user/post/mark-posted",
                               json={"session_token": _SESSION, "post_id": 77})

        assert resp.status_code == 500
