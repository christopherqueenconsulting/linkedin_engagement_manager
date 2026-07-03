"""Unit tests for build_dm_from_template (templated, voice-aligned DMs)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


class TestBuildDmFromTemplate:
    def test_renders_and_refines(self):
        from cqc_lem.app.run_automation import build_dm_from_template
        prof = MagicMock(); prof.job_title = "AI Engineer"
        with patch(f"{_RA}.get_dm_template",
                   return_value={"template_text": "Hi {first_name}, re {headline}", "delay_hours": 0, "step": 0}), \
             patch(f"{_RA}.get_ai_message_refinement", return_value="Hi Jane, about AI Engineer (refined)"):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert msg == "Hi Jane, about AI Engineer (refined)"

    def test_none_when_no_template(self):
        from cqc_lem.app.run_automation import build_dm_from_template
        with patch(f"{_RA}.get_dm_template", return_value=None):
            assert build_dm_from_template(1, "x", "Jane", MagicMock()) is None

    def test_bad_placeholder_degrades_gracefully(self):
        from cqc_lem.app.run_automation import build_dm_from_template
        prof = MagicMock(); prof.job_title = None
        with patch(f"{_RA}.get_dm_template",
                   return_value={"template_text": "Hi {first_name} {unknown_field}", "delay_hours": 0, "step": 0}), \
             patch(f"{_RA}.get_ai_message_refinement", side_effect=lambda m, character_limit=300: m):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert "Jane" in msg  # didn't crash on the stray placeholder

    def test_refinement_failure_falls_back_to_rendered(self):
        from cqc_lem.app.run_automation import build_dm_from_template
        prof = MagicMock(); prof.job_title = "Eng"
        with patch(f"{_RA}.get_dm_template",
                   return_value={"template_text": "Hi {first_name}", "delay_hours": 0, "step": 0}), \
             patch(f"{_RA}.get_ai_message_refinement", side_effect=Exception("llm down")):
            msg = build_dm_from_template(1, "connection_accepted", "Jane", prof)
        assert msg == "Hi Jane"
