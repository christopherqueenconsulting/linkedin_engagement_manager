"""The task/generator entry points must scope their LLM calls to the right user + feature, so
PostHog cost lands on a real user instead of 'system'.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"
_PLAN = "cqc_lem.app.run_content_plan"


@pytest.fixture
def seen_attribution():
    """Captures the attribution active at the moment the LLM helper is invoked."""
    from cqc_lem.utilities.observability import current_llm_attribution
    captured = {}

    def capture(*args, **kwargs):
        captured["value"] = current_llm_attribution()
        return "refined text"

    return captured, capture


class TestDmAttribution:
    def test_build_dm_from_template_attributes_the_dm_feature(self, seen_attribution):
        from cqc_lem.app.engagement.outreach import build_dm_from_template
        captured, capture = seen_attribution
        with patch(f"{_OUT}.get_dm_template", return_value={"template_text": "Hi {first_name}"}), \
             patch(f"{_OUT}.get_ai_message_refinement", side_effect=capture), \
             patch(f"{_OUT}.humanize_text", side_effect=lambda text, **k: text):
            out = build_dm_from_template(42, "connection_accepted", "Ada", None)

        assert out == "refined text"
        assert captured["value"] == (42, "dm")


class TestContentAttribution:
    def test_create_carousel_content_attributes_the_content_feature(self, seen_attribution):
        from cqc_lem.app.run_content_plan import create_carousel_content
        captured, capture = seen_attribution

        def generate(user_id, stage, **kwargs):
            capture()
            return "post text", {}

        with patch(f"{_PLAN}.get_engagement_preferences", return_value={}), \
             patch(f"{_PLAN}.get_or_create_profile_synthesis", return_value="voice"), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content", side_effect=generate):
            out = create_carousel_content(7, "awareness")

        assert out == "post text"
        assert captured["value"] == (7, "content")


class TestNewsletterAttribution:
    def test_newsletter_topup_attributes_the_newsletter_feature(self, seen_attribution):
        from datetime import datetime

        from cqc_lem.app.run_scheduler import _topup_newsletter_drafts_for_user
        captured, capture = seen_attribution

        # Bail out right after the attribution scope opens: the settings read is the first thing the
        # top-up does, so raising there proves the scope wraps the whole generation body.
        def settings(user_id):
            capture()
            raise RuntimeError("stop here")

        with patch("cqc_lem.utilities.db.get_newsletter_settings", side_effect=settings):
            with pytest.raises(RuntimeError, match="stop here"):
                _topup_newsletter_drafts_for_user(3, datetime(2026, 7, 25))

        assert captured["value"] == (3, "newsletter")
