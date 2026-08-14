"""Integration tests for the avatar preview / approval / guardrail endpoints (issue #744)."""
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

SESSION = "test-session-token"
USER_ID = 42
_M = "cqc_lem.api.main"
# The avatar handlers moved to their own router (#1154). `get_session_user_id` still patches
# on main -- the handlers reach it as a host-module attribute at request time -- but every db
# function they call is read from the ROUTER's globals now.
_AV = "cqc_lem.api.routers.avatar"


_APPROVABLE = {
    "id": 7, "training_id": "t7", "model_ref": "owner/lora:v1", "trigger_word": "TOK",
    "status": "succeeded", "is_active": False,
    "gender_presentation": "man", "age_band": "40s",
    "attributes_confirmed_at": "2026-07-30T12:00:00",
    "approval_status": "pending", "approved_at": None,
    "sample_paths": [{"label": "headshot", "path": "images/avatar_samples/7/headshot.webp"}],
    "samples_generated_at": "2026-07-30T11:00:00", "sample_regen_count": 1,
    "created_at": None, "updated_at": None,
}


_SESSION_TOKEN = SESSION


@contextmanager
def _signed_in():
    """Compose and edit resolve their caller from the session since issue #914."""
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_M}.get_session_user_id", return_value=USER_ID))
        stack.enter_context(patch(f"{_M}.get_user_email", return_value="a@b.c"))
        stack.enter_context(patch(f"{_M}.user_owns_posts", return_value=True))
        yield


@pytest.mark.integration
class TestSamplesEndpoint:
    def test_returns_gallery_and_regeneration_budget(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch("cqc_lem.utilities.env_constants.AVATAR_SAMPLE_REGEN_MAX", 3):
            r = api_client.get("/api/avatar/training/7/samples",
                              params={"session_token": SESSION})
        assert r.status_code == 200
        detail = r.json()["detail"]
        assert detail["approval_status"] == "pending"
        assert detail["samples"][0]["label"] == "headshot"
        assert "images/avatar_samples/7/headshot.webp" in detail["samples"][0]["url"]
        assert detail["sample_regen_remaining"] == 2
        assert detail["gender_presentation"] == "man"

    def test_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            r = api_client.get("/api/avatar/training/7/samples", params={"session_token": "bad"})
        assert r.status_code == 401

    def test_404_for_another_users_avatar(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=None):
            r = api_client.get("/api/avatar/training/7/samples",
                              params={"session_token": SESSION})
        assert r.status_code == 404


@pytest.mark.integration
class TestRegenerateSamplesEndpoint:
    def test_queues_a_counted_regeneration(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch("cqc_lem.utilities.env_constants.AVATAR_SAMPLE_REGEN_MAX", 3), \
             patch(f"{_AV}.claim_avatar_sample_render", return_value=True) as claim, \
             patch("cqc_lem.app.run_avatar.render_avatar_samples_task.apply_async") as queued:
            r = api_client.post("/api/avatar/training/7/samples",
                               json={"session_token": SESSION})
        assert r.status_code == 200
        assert queued.call_args.kwargs["kwargs"] == {
            "avatar_id": 7, "user_id": USER_ID, "count_regeneration": True}
        # Reserved BEFORE the render is queued — a second click has to lose the claim, not read
        # a counter that only moves minutes later.
        claim.assert_called_once_with(USER_ID, 7, regeneration=True, max_regenerations=3)

    def test_cap_returns_429(self, api_client):
        """The cap sits on top of the credit ledger — samples cost inference money but no credit."""
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch("cqc_lem.utilities.env_constants.AVATAR_SAMPLE_REGEN_MAX", 3), \
             patch(f"{_AV}.claim_avatar_sample_render", return_value=False), \
             patch("cqc_lem.app.run_avatar.render_avatar_samples_task.apply_async") as queued:
            r = api_client.post("/api/avatar/training/7/samples",
                               json={"session_token": SESSION})
        assert r.status_code == 429
        queued.assert_not_called()

    def test_a_broker_failure_hands_the_reservation_back(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch("cqc_lem.utilities.env_constants.AVATAR_SAMPLE_REGEN_MAX", 3), \
             patch(f"{_AV}.claim_avatar_sample_render", return_value=True), \
             patch(f"{_AV}.release_avatar_sample_render") as released, \
             patch("cqc_lem.app.run_avatar.render_avatar_samples_task.apply_async",
                   side_effect=RuntimeError("broker down")):
            r = api_client.post("/api/avatar/training/7/samples",
                               json={"session_token": SESSION})
        assert r.status_code == 500
        released.assert_called_once_with(USER_ID, 7, regeneration=True)

    def test_unfinished_training_returns_400(self, api_client):
        avatar = {**_APPROVABLE, "status": "processing", "model_ref": None}
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=avatar), \
             patch(f"{_AV}.claim_avatar_sample_render") as claim:
            r = api_client.post("/api/avatar/training/7/samples",
                               json={"session_token": SESSION})
        assert r.status_code == 400
        claim.assert_not_called()


@pytest.mark.integration
class TestAttributesEndpoint:
    def test_saves_declared_attributes(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch(f"{_AV}.update_avatar_attributes", return_value=True) as upd:
            r = api_client.put("/api/avatar/training/7/attributes", json={
                "session_token": SESSION, "gender_presentation": "man", "age_band": "40s"})
        assert r.status_code == 200
        upd.assert_called_once_with(USER_ID, 7, "man", "40s")

    def test_nulls_clear_the_declaration(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch(f"{_AV}.update_avatar_attributes", return_value=True) as upd:
            r = api_client.put("/api/avatar/training/7/attributes",
                              json={"session_token": SESSION})
        assert r.status_code == 200
        upd.assert_called_once_with(USER_ID, 7, None, None)

    def test_write_failure_returns_500(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch(f"{_AV}.update_avatar_attributes", return_value=False):
            r = api_client.put("/api/avatar/training/7/attributes",
                              json={"session_token": SESSION, "gender_presentation": "man"})
        assert r.status_code == 500


@pytest.mark.integration
class TestApproveRejectEndpoints:
    def test_approve_records_the_verdict(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch(f"{_AV}.set_avatar_approval", return_value=True) as approve:
            r = api_client.post("/api/avatar/training/7/approve",
                               json={"session_token": SESSION})
        assert r.status_code == 200
        approve.assert_called_once_with(USER_ID, 7, "approved")

    def test_cannot_approve_an_avatar_with_no_rendered_samples(self, api_client):
        """Approving what nobody has seen is the blind activation this gate removes."""
        avatar = {**_APPROVABLE, "sample_paths": []}
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=avatar), \
             patch(f"{_AV}.set_avatar_approval") as approve:
            r = api_client.post("/api/avatar/training/7/approve",
                               json={"session_token": SESSION})
        assert r.status_code == 400
        approve.assert_not_called()

    def test_cannot_approve_an_unfinished_training(self, api_client):
        avatar = {**_APPROVABLE, "status": "processing"}
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=avatar):
            r = api_client.post("/api/avatar/training/7/approve",
                               json={"session_token": SESSION})
        assert r.status_code == 400

    def test_approve_write_failure_returns_500(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch(f"{_AV}.set_avatar_approval", return_value=False):
            r = api_client.post("/api/avatar/training/7/approve",
                               json={"session_token": SESSION})
        assert r.status_code == 500

    def test_reject_records_the_verdict(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_training", return_value=_APPROVABLE), \
             patch(f"{_AV}.set_avatar_approval", return_value=True) as reject:
            r = api_client.post("/api/avatar/training/7/reject",
                               json={"session_token": SESSION})
        assert r.status_code == 200
        reject.assert_called_once_with(USER_ID, 7, "rejected")

    def test_reject_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            r = api_client.post("/api/avatar/training/7/reject", json={"session_token": "bad"})
        assert r.status_code == 401


@pytest.mark.integration
class TestGuardrailPreferencesEndpoints:
    _PREFS = {"avatar_disabled": False, "avatar_use_post_image": False,
              "avatar_use_carousel": False, "avatar_use_video": True}

    def test_get_returns_every_flag(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.get_avatar_preferences", return_value=self._PREFS):
            r = api_client.get("/api/avatar/preferences", params={"session_token": SESSION})
        assert r.status_code == 200 and r.json()["detail"] == self._PREFS

    def test_put_patches_one_toggle(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.update_avatar_preferences", return_value=True) as upd, \
             patch(f"{_AV}.get_avatar_preferences", return_value=self._PREFS):
            r = api_client.put("/api/avatar/preferences",
                              json={"session_token": SESSION, "avatar_use_video": True})
        assert r.status_code == 200
        upd.assert_called_once_with(USER_ID, {"avatar_use_video": True})

    def test_put_with_nothing_to_change_is_400(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.update_avatar_preferences") as upd:
            r = api_client.put("/api/avatar/preferences", json={"session_token": SESSION})
        assert r.status_code == 400
        upd.assert_not_called()

    def test_put_write_failure_returns_500(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=USER_ID), \
             patch(f"{_AV}.update_avatar_preferences", return_value=False):
            r = api_client.put("/api/avatar/preferences",
                              json={"session_token": SESSION, "avatar_disabled": True})
        assert r.status_code == 500

    def test_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            r = api_client.get("/api/avatar/preferences", params={"session_token": "bad"})
        assert r.status_code == 401


@pytest.mark.integration
class TestComposeHonoursUseAvatar:
    def test_use_avatar_reaches_the_post_row(self, api_client):
        """PostRequest.use_avatar was accepted by the API and dropped (issue #744 §2.4)."""
        with _signed_in(), patch(f"{_M}.insert_post", return_value=True) as insert:
            r = api_client.post("/api/schedule_post/", json={
                "session_token": _SESSION_TOKEN,
                "content": "body", "post_type": "text",
                "scheduled_datetime": "2026-08-01T09:00:00", "use_avatar": False})
        assert r.status_code == 200
        assert insert.call_args.kwargs["use_avatar"] is False

    def test_omitting_it_leaves_the_choice_to_the_user_preferences(self, api_client):
        with _signed_in(), patch(f"{_M}.insert_post", return_value=True) as insert:
            api_client.post("/api/schedule_post/", json={
                "session_token": _SESSION_TOKEN,
                "content": "body", "post_type": "text",
                "scheduled_datetime": "2026-08-01T09:00:00"})
        assert insert.call_args.kwargs["use_avatar"] is None

    def test_editing_a_post_persists_an_explicit_choice(self, api_client):
        with _signed_in(), patch(f"{_M}.update_db_post", return_value=True), \
             patch(f"{_M}.update_post_use_avatar") as upd:
            r = api_client.post("/api/update_post/?post_id=5", json={
                "session_token": _SESSION_TOKEN,
                "content": "body", "post_type": "text",
                "scheduled_datetime": "2026-08-01T09:00:00", "use_avatar": True})
        assert r.status_code == 200
        upd.assert_called_once_with(5, True)

    def test_editing_without_the_field_leaves_the_stored_choice_alone(self, api_client):
        with _signed_in(), patch(f"{_M}.update_db_post", return_value=True), \
             patch(f"{_M}.update_post_use_avatar") as upd:
            api_client.post("/api/update_post/?post_id=5", json={
                "session_token": _SESSION_TOKEN,
                "content": "body", "post_type": "text",
                "scheduled_datetime": "2026-08-01T09:00:00"})
        upd.assert_not_called()
