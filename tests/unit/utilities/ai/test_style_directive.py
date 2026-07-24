"""Unit tests for the engagement-prefs style directive injected into AI comments."""

import pytest

pytestmark = pytest.mark.unit


class TestStyleDirective:
    def test_includes_tone_length_and_no_emoji_no_hashtag(self):
        from cqc_lem.utilities.ai.ai_helper import _style_directive
        d = _style_directive({"tone": "warm", "comment_length": "short",
                              "use_emojis": False, "use_hashtags": False, "comment_style": "punchy"})
        assert "warm" in d and "short" in d
        assert "Do not use emojis" in d and "Do NOT include any hashtags" in d and "punchy" in d

    def test_emojis_and_hashtags_allowed(self):
        from cqc_lem.utilities.ai.ai_helper import _style_directive
        d = _style_directive({"comment_length": "long", "use_emojis": True, "use_hashtags": True})
        assert "tasteful emoji" in d and "at most 3 highly relevant hashtags" in d

    def test_empty_without_prefs(self):
        from cqc_lem.utilities.ai.ai_helper import _style_directive
        assert _style_directive(None) == ""
        assert _style_directive({}) == ""
