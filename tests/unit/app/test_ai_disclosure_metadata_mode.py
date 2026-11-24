"""Metadata-first AI disclosure decision (issue #2634, Phase 1).

One decision function, three call sites: create, `_finish_regenerated_post`, and
`regenerate_video_for_post`. Mode off or C2PA off must equal the legacy rule.
"""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_STATE = "cqc_lem.utilities.db.post_avatar_media_state"


@pytest.fixture
def mode_on(monkeypatch):
    monkeypatch.setenv("AI_DISCLOSURE_METADATA_MODE", "true")
    with patch(f"{_RCP}.C2PA_ENABLED", True):
        yield


class TestNeedsAiDisclosure:
    def _need(self, **kw):
        from cqc_lem.app.run_content_plan import _needs_ai_disclosure
        return _needs_ai_disclosure(9, **kw)

    def test_mode_off_is_the_legacy_rule(self, monkeypatch):
        monkeypatch.delenv("AI_DISCLOSURE_METADATA_MODE", raising=False)
        with patch(f"{_RCP}.C2PA_ENABLED", True), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False):
            assert self._need(ai_video=True, ai_video_signed=True) is True
            assert self._need(ai_video=False) is False

    def test_c2pa_off_is_the_legacy_rule_even_with_mode_on(self, monkeypatch):
        monkeypatch.setenv("AI_DISCLOSURE_METADATA_MODE", "true")
        with patch(f"{_RCP}.C2PA_ENABLED", False), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False):
            assert self._need(ai_video=True, ai_video_signed=True) is True

    def test_signed_non_avatar_video_drops_the_line(self, mode_on):
        with patch(_STATE, return_value=False):
            assert self._need(ai_video=True, ai_video_signed=True) is False

    def test_avatar_media_keeps_the_line_even_when_signed(self, mode_on):
        with patch(_STATE, return_value=True):
            assert self._need(ai_video=True, ai_video_signed=True) is True

    def test_unknown_avatar_state_keeps_the_line(self, mode_on):
        with patch(_STATE, return_value=None):
            assert self._need(ai_video=False) is True

    def test_unreadable_avatar_state_keeps_the_line(self, mode_on):
        with patch(_STATE, side_effect=RuntimeError("db down")):
            assert self._need(ai_video=True, ai_video_signed=True) is True

    def test_signing_failure_keeps_the_line_with_one_warning(self, mode_on):
        with patch(_STATE, return_value=False), patch(f"{_RCP}.log_warning") as warn:
            assert self._need(ai_video=True, ai_video_signed=False) is True
        assert warn.call_count == 1

    def test_non_ai_non_avatar_needs_nothing(self, mode_on):
        with patch(_STATE, return_value=False), patch(f"{_RCP}.log_warning") as warn:
            assert self._need(ai_video=False) is False
        warn.assert_not_called()


class TestSignAiVideo:
    def _sign(self, **patch_kw):
        from cqc_lem.app.run_content_plan import _sign_ai_video
        with patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials", **patch_kw):
            return _sign_ai_video("/x.mp4", post_id=1, user_id=2, task_name="t")

    def test_returns_signer_result(self):
        assert self._sign(return_value=True) is True
        assert self._sign(return_value=False) is False

    def test_raise_is_false_with_warning(self):
        with patch(f"{_RCP}.log_warning") as warn:
            assert self._sign(side_effect=RuntimeError("boom")) is False
        assert warn.call_count == 1


class TestCreatePath:
    @pytest.mark.parametrize("signed,expect_line", [(True, False), (False, True)])
    def test_signing_result_decides_the_line(self, mode_on, monkeypatch, tmp_path, signed,
                                             expect_line):
        import cqc_lem.app.run_content_plan as rcp
        from tests.unit.app.test_content_plan_coverage import _MondayDatetime, _valid_mp4
        monkeypatch.setattr(f"{_RCP}.datetime", _MondayDatetime)
        video_file = _valid_mp4(tmp_path, "clip.mp4")
        post = [{"user_id": 1, "id": 42, "post_type": "video", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=("Post text", "http://runway/clip.mp4")), \
             patch(f"{_RCP}.create_folder_if_not_exists"), \
             patch(f"{_RCP}.save_video_url_to_dir", return_value=str(video_file)), \
             patch("cqc_lem.utilities.c2pa_helper.add_ai_content_credentials", return_value=signed), \
             patch(_STATE, return_value=False), \
             patch(f"{_RCP}.update_db_post_video_url"), \
             patch(f"{_RCP}.update_db_post_content") as upd_content, \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch.object(rcp, "AI_DISCLOSURE_ENABLED", True), \
             patch.object(rcp, "AI_DISCLOSURE_TEXT", "\n\nAI visuals."):
            rcp.auto_create_weekly_content(user_id=1)
        assert ("AI visuals." in upd_content.call_args[0][1]) is expect_line


class TestFinishRegeneratedPost:
    def _finish(self, *, ai_media, ai_signed):
        from cqc_lem.app.run_content_plan import _finish_regenerated_post
        with patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}._gate_findings_for_post", return_value=[]), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.forget_supplied_material"), \
             patch(f"{_RCP}._post_is_flagged_error", return_value=False), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_content"):
            return _finish_regenerated_post(7, 9, "body", "video", "u",
                                            ai_media=ai_media, ai_signed=ai_signed)

    def test_signed_non_avatar_regeneration_has_no_line(self, mode_on):
        with patch(_STATE, return_value=False):
            assert "Visuals created with AI" not in self._finish(ai_media=True, ai_signed=True)

    def test_unsigned_regeneration_keeps_the_line(self, mode_on):
        with patch(_STATE, return_value=False):
            assert "Visuals created with AI" in self._finish(ai_media=True, ai_signed=False)

    def test_avatar_regeneration_keeps_the_line(self, mode_on):
        with patch(_STATE, return_value=True):
            assert "Visuals created with AI" in self._finish(ai_media=True, ai_signed=True)


class TestRegenerateVideoForPost:
    def _regen(self, *, signing, avatar):
        from cqc_lem.app.run_content_plan import regenerate_video_for_post

        def _store(*a, signing=None, **k):
            signing.update(signing_result)
            return "http://api/v.mp4"

        signing_result = signing
        with patch(f"{_RCP}.get_post_content", return_value="caption"), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=7), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}._generate_video_src", return_value="https://runway/v.mp4"), \
             patch(f"{_RCP}._store_video_asset", side_effect=_store), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=avatar), \
             patch(f"{_RCP}._heal_errored_post"), \
             patch(f"{_RCP}.update_db_post_content") as store:
            regenerate_video_for_post(9)
        return store

    def test_legacy_mode_discloses_an_ai_video_now(self, monkeypatch):
        monkeypatch.delenv("AI_DISCLOSURE_METADATA_MODE", raising=False)
        store = self._regen(signing={"ai": True, "signed": True}, avatar=False)
        assert "Visuals created with AI" in store.call_args[0][1]

    def test_mode_on_signed_non_avatar_writes_nothing(self, mode_on):
        with patch(_STATE, return_value=False):
            store = self._regen(signing={"ai": True, "signed": True}, avatar=False)
        store.assert_not_called()

    def test_mode_on_unsigned_writes_the_line(self, mode_on):
        with patch(_STATE, return_value=False):
            store = self._regen(signing={"ai": True, "signed": False}, avatar=False)
        assert "Visuals created with AI" in store.call_args[0][1]

    def test_mode_on_avatar_writes_the_line(self, mode_on):
        with patch(_STATE, return_value=True):
            store = self._regen(signing={"ai": True, "signed": True}, avatar=False)
        assert "Visuals created with AI" in store.call_args[0][1]
