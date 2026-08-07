"""Unit tests for the authenticity gate (issue #382 — 360Brew defense): the LLM-judge scorer in
content_alignment (threshold config, JSON coercion, fail-open behavior) and its integration into
the content pipeline (persist the score, demote a low-scoring auto-approve APPROVED -> PENDING).
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import content_alignment as ca

pytestmark = pytest.mark.unit


def _llm_reply(text: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


class TestThresholdConfig:
    def test_default_min(self, monkeypatch):
        monkeypatch.delenv("AUTHENTICITY_SCORE_MIN", raising=False)
        assert ca.authenticity_score_min() == ca.AUTHENTICITY_SCORE_MIN_DEFAULT

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "75")
        assert ca.authenticity_score_min() == 75

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "not-a-number")
        assert ca.authenticity_score_min() == ca.AUTHENTICITY_SCORE_MIN_DEFAULT

    def test_env_clamped_to_range(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "500")
        assert ca.authenticity_score_min() == 100
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "-10")
        assert ca.authenticity_score_min() == 0

    def test_gate_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("AUTHENTICITY_GATE_ENABLED", raising=False)
        assert ca.authenticity_gate_enabled() is True

    @pytest.mark.parametrize("val,expected", [
        ("0", False), ("false", False), ("no", False), ("off", False),
        ("1", True), ("true", True), ("yes", True), ("on", True),
    ])
    def test_gate_toggle(self, monkeypatch, val, expected):
        monkeypatch.setenv("AUTHENTICITY_GATE_ENABLED", val)
        assert ca.authenticity_gate_enabled() is expected


class TestCoerceResult:
    def test_plain_json(self):
        out = ca._coerce_authenticity_result('{"score": 82, "reasons": ["specific", "on-voice"]}')
        assert out == {"score": 82, "reasons": ["specific", "on-voice"]}

    def test_fenced_and_prose_wrapped(self):
        text = 'Here is my judgment:\n```json\n{"score": 40, "reasons": ["generic"]}\n```\nDone.'
        out = ca._coerce_authenticity_result(text)
        assert out["score"] == 40
        assert out["reasons"] == ["generic"]

    def test_float_score_rounded(self):
        assert ca._coerce_authenticity_result('{"score": 66.7}')["score"] == 67

    def test_score_clamped(self):
        assert ca._coerce_authenticity_result('{"score": 140}')["score"] == 100
        assert ca._coerce_authenticity_result('{"score": -5}')["score"] == 0

    def test_string_reasons_normalized_to_list(self):
        assert ca._coerce_authenticity_result('{"score": 50, "reasons": "too generic"}')["reasons"] == [
            "too generic"]

    def test_missing_reasons_ok(self):
        assert ca._coerce_authenticity_result('{"score": 90}')["reasons"] == []

    @pytest.mark.parametrize("bad", [
        "", "no json here", "{not valid json}", '{"reasons": ["x"]}',
        '{"score": "high"}',
    ])
    def test_unusable_returns_none(self, bad):
        assert ca._coerce_authenticity_result(bad) is None


class TestScoreAuthenticity:
    def test_empty_content_passes_without_llm(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as m:
            out = ca.score_authenticity("   ")
        m.assert_not_called()
        assert out == {"score": 100, "reasons": [], "flagged": False}

    def test_high_score_not_flagged(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "60")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_reply('{"score": 85, "reasons": ["specific"]}')):
            out = ca.score_authenticity("A concrete, specific post about our migration.")
        assert out["score"] == 85
        assert out["flagged"] is False

    def test_low_score_flagged(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "60")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_reply('{"score": 30, "reasons": ["generic buzzwords"]}')):
            out = ca.score_authenticity("In today's fast-paced world, synergy unlocks success.")
        assert out["score"] == 30
        assert out["flagged"] is True
        assert out["reasons"] == ["generic buzzwords"]

    def test_fails_open_on_llm_error(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("proxy down")):
            out = ca.score_authenticity("anything")
        assert out == {"score": 100, "reasons": [], "flagged": False}

    def test_fails_open_on_unparseable_reply(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_reply("the model rambled with no json")):
            out = ca.score_authenticity("anything")
        assert out == {"score": 100, "reasons": [], "flagged": False}

    def test_profile_and_topics_ride_into_prompt(self):
        prof = MagicMock()
        prof.model_dump_json.return_value = '{"full_name": "Ada"}'
        captured = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return _llm_reply('{"score": 70}')

        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=_fake):
            ca.score_authenticity("post body", profile=prof,
                                  prefs={"focus_topics": ["MLOps", "observability"]})
        user_msg = captured["messages"][-1]["content"]
        assert "MLOps" in user_msg and "observability" in user_msg
        assert "Ada" in user_msg
        assert captured["model"] == "lem-medium"


_RCP = "cqc_lem.app.run_content_plan"


class TestScoreAndPersistHelper:
    def test_persists_score_and_warns_when_flagged(self):
        from cqc_lem.app import run_content_plan as rcp
        log = MagicMock()
        with patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.score_authenticity",
                   return_value={"score": 25, "reasons": ["generic"], "flagged": True}), \
             patch(f"{_RCP}.update_db_post_authenticity_score") as upd, \
             patch(f"{_RCP}.log_warning", log):
            rcp._score_and_persist_authenticity(1, 42, "content", MagicMock())
        upd.assert_called_once_with(42, 25)
        assert log.called

    def test_persists_score_when_passing(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.score_authenticity",
                   return_value={"score": 88, "reasons": [], "flagged": False}), \
             patch(f"{_RCP}.update_db_post_authenticity_score") as upd:
            rcp._score_and_persist_authenticity(1, 42, "content", MagicMock())
        upd.assert_called_once_with(42, 88)

    def test_noop_when_gate_disabled(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.authenticity_gate_enabled", return_value=False), \
             patch(f"{_RCP}.score_authenticity") as sc, \
             patch(f"{_RCP}.update_db_post_authenticity_score") as upd:
            rcp._score_and_persist_authenticity(1, 42, "content", MagicMock())
        sc.assert_not_called()
        upd.assert_not_called()

    def test_exception_never_propagates(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.authenticity_gate_enabled", return_value=True), \
             patch(f"{_RCP}.score_authenticity", side_effect=RuntimeError("boom")):
            # Must not raise.
            rcp._score_and_persist_authenticity(1, 42, "content", MagicMock())


class TestStatusDemotion:
    """The gate at the content-plan status-setter: a low authenticity score demotes an auto-approve
    to PENDING; it never upgrades, never blocks an unscored post, and honors the disable toggle.
    """

    def _run(self, score, auto_schedule=True, gate_enabled=True, score_min=60):
        from cqc_lem.app import run_content_plan as rcp
        planned = [{"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness"}]
        captured = {}

        def _capture_status(post_id, status):
            captured["status"] = status
            return True

        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=planned), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=("some content", None)), \
             patch(f"{_RCP}.get_user_preferences",
                   return_value={"auto_schedule_posts": auto_schedule}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.authenticity_gate_enabled", return_value=gate_enabled), \
             patch(f"{_RCP}.authenticity_score_min", return_value=score_min), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=score), \
             patch(f"{_RCP}.update_db_post_status", side_effect=_capture_status):
            rcp.auto_create_weekly_content(user_id=1)
        return captured.get("status")

    def test_low_score_demotes_approved_to_pending(self):
        from cqc_lem.utilities.db import PostStatus
        assert self._run(score=30) == PostStatus.PENDING

    def test_high_score_stays_approved(self):
        from cqc_lem.utilities.db import PostStatus
        assert self._run(score=90) == PostStatus.APPROVED

    def test_unscored_post_stays_approved(self):
        from cqc_lem.utilities.db import PostStatus
        assert self._run(score=None) == PostStatus.APPROVED

    def test_gate_disabled_stays_approved(self):
        from cqc_lem.utilities.db import PostStatus
        assert self._run(score=10, gate_enabled=False) == PostStatus.APPROVED

    def test_never_upgrades_a_pending_post(self):
        # auto_schedule False => PENDING regardless; a high score must not flip it to APPROVED.
        from cqc_lem.utilities.db import PostStatus
        assert self._run(score=95, auto_schedule=False) == PostStatus.PENDING
