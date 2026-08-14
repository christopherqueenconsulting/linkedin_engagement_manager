"""Unit tests for generate_carousel_content() in ai_helper.py."""

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_llm_mock(json_content: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=json.dumps(json_content)))]
    return mock_response


def _educational_carousel_json():
    return {
        "post_text": "Here are 5 tips to grow faster on LinkedIn. #linkedin #growth",
        "carousel": {
            "cover": {"title": "5 Tips for Growth", "content": "Boost your LinkedIn presence"},
            "contents": [
                {"title": "Tip 1: Post Daily", "content": "Consistency beats perfection."},
                {"title": "Tip 2: Engage Comments", "content": "Reply to every comment you get."},
            ],
            "call_to_action": {"title": "What's Your Tip?", "content": "Share in the comments!"},
        },
    }


@pytest.mark.unit
class TestGenerateCarouselContent:

    def _patch_llm(self, payload):
        return patch(
            "cqc_lem.utilities.ai.ai_helper._call_llm",
            return_value=_make_llm_mock(payload),
        )

    def _patch_profile(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME", industry="Technology")
        # generate_carousel_content uses local imports from their source modules
        import contextlib
        from contextlib import ExitStack

        @contextlib.contextmanager
        def _multi():
            with patch("cqc_lem.utilities.db.get_user_password_pair_by_id",
                       return_value=("test@example.com", "pass")), \
                 patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair",
                       return_value=(MagicMock(), MagicMock())), \
                 patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
                 patch("cqc_lem.utilities.selenium_util.quit_gracefully"):
                yield

        return _multi()

    def test_returns_tuple_of_post_text_and_dict(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert isinstance(post_text, str)
        assert len(post_text) > 0
        assert isinstance(carousel_dict, dict)

    def test_awareness_stage_returns_educational_carousel(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            _, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert "cover" in carousel_dict
        assert "contents" in carousel_dict
        assert "call_to_action" in carousel_dict

    def test_post_text_extracted_correctly(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, _ = generate_carousel_content(user_id=1, stage="awareness")
        assert post_text == payload["post_text"]

    def test_invalid_json_from_llm_returns_defaults(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="not json at all"))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=mock_response), \
             self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert isinstance(post_text, str)
        assert isinstance(carousel_dict, dict)

    def test_guidance_reaches_the_slide_prompt(self):
        """Issue #794: the regenerate flow's free-text guidance must steer the SLIDES, not just the
        caption — and must not license an invented number to satisfy the request.
        """
        payload = _educational_carousel_json()
        with self._patch_llm(payload) as mock_llm, self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage="awareness", guidance="drop the pricing slide")
        prompt = mock_llm.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "drop the pricing slide" in prompt
        assert "every slide" in prompt
        assert "never invent a number" in prompt

    def test_no_guidance_adds_no_revision_block(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload) as mock_llm, self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage="awareness", guidance="   ")
        prompt = mock_llm.call_args.kwargs["messages"][1]["content"][0]["text"]
        assert "AUTHOR'S REVISION REQUEST" not in prompt

    def test_uses_lem_complex_model(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload) as mock_llm, self._patch_profile():
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            generate_carousel_content(user_id=1, stage="awareness")
        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs.get("model") == "lem-complex"

    def test_profile_fetch_failure_falls_back_gracefully(self):
        payload = _educational_carousel_json()
        with self._patch_llm(payload), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", side_effect=Exception("DB down")):
            from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
            post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")
        assert isinstance(post_text, str)


@pytest.mark.unit
def test_wall_of_text_caption_is_reflowed_and_capped():
    """Regression (post 72): a caption that comes back as a long single paragraph must be reflowed
    into scannable paragraphs and hard-capped under the LinkedIn limit.
    """
    from contextlib import contextmanager

    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

    wall = " ".join(f"Intro sentence number {i} is here." for i in range(150))  # long, no line breaks
    assert "\n\n" not in wall and len(wall) > 3000
    payload = _educational_carousel_json()
    payload["post_text"] = wall

    profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME", industry="Technology")

    @contextmanager
    def _env():
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_make_llm_mock(payload)), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", return_value=("t@e.com", "p")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"):
            yield

    with _env():
        from cqc_lem.utilities.ai.ai_helper import _CAROUSEL_CAPTION_MAX_CHARS, generate_carousel_content
        post_text, _ = generate_carousel_content(user_id=1, stage="awareness")

    assert len(post_text) <= _CAROUSEL_CAPTION_MAX_CHARS   # hard-capped
    assert "\n\n" in post_text                             # reflowed into paragraphs


@pytest.mark.unit
class TestLoadsJsonObject:
    """Fence- and prose-tolerant JSON parsing for carousel replies.

    Issue #1323: `response_format={"type": "json_object"}` is a request, not a guarantee — a reply
    that carries a good object inside a fence or beside a line of prose must still parse.
    """

    def test_plain_object_parses(self):
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        assert _loads_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_object_parses(self):
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        assert _loads_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare_fence_without_language_tag_parses(self):
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        assert _loads_json_object('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around_the_object_parses(self):
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        raw = 'Sure! Here is the carousel:\n{"a": 1}\nLet me know if you want changes.'
        assert _loads_json_object(raw) == {"a": 1}

    @pytest.mark.parametrize("raw", ["", "   ", "not json at all", None, "[1, 2, 3]"])
    def test_unusable_replies_return_none(self, raw):
        """None is the ONE signal the caller acts on.

        A JSON array counts as unusable too: it is not the object the prompt asked for and carries
        no post_text/carousel keys.
        """
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        assert _loads_json_object(raw) is None


@pytest.mark.unit
def test_fenced_carousel_json_is_recovered_not_dropped():
    """A fenced reply must be recovered, not dropped.

    Issue #1323: it used to raise JSONDecodeError and fall back to the generic caption with an
    EMPTY deck — which flags the post 'error' downstream.
    """
    from contextlib import contextmanager

    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

    payload = _educational_carousel_json()
    fenced = MagicMock()
    fenced.choices = [MagicMock(message=MagicMock(
        content="```json\n" + json.dumps(payload) + "\n```"))]
    profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME",
                              industry="Technology")

    @contextmanager
    def _env():
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=fenced), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", return_value=("t@e.com", "p")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"), \
             patch("cqc_lem.utilities.ai.ai_helper.log_error") as mock_log_error:
            yield mock_log_error

    with _env() as mock_log_error:
        from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
        post_text, carousel_dict = generate_carousel_content(user_id=1, stage="awareness")

    assert post_text == payload["post_text"]
    assert carousel_dict == payload["carousel"]
    assert mock_log_error.call_count == 0


@pytest.mark.unit
def test_unparseable_reply_logs_one_error_with_context():
    """The residual failure keeps ERROR, with context.

    An empty deck flags the post 'error' downstream, and user_id/task_name make the grouped issue
    name the user it cost.
    """
    from contextlib import contextmanager

    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

    junk = MagicMock()
    junk.choices = [MagicMock(message=MagicMock(content="I'm sorry, I can't help with that."))]
    profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME",
                              industry="Technology")

    @contextmanager
    def _env():
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=junk), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", return_value=("t@e.com", "p")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"), \
             patch("cqc_lem.utilities.ai.ai_helper.log_error") as mock_log_error:
            yield mock_log_error

    with _env() as mock_log_error:
        from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
        post_text, carousel_dict = generate_carousel_content(user_id=7, stage="awareness")

    assert carousel_dict == {}
    assert isinstance(post_text, str) and post_text
    assert mock_log_error.call_count == 1
    kwargs = mock_log_error.call_args.kwargs
    assert kwargs["user_id"] == 7
    assert kwargs["task_name"] == "create_carousel_content"
    assert "exc" not in kwargs   # nothing was raised — no stack to attach


@pytest.mark.unit
def test_empty_reply_content_takes_the_fallback_instead_of_raising():
    """A refusal / content-filtered reply carries `content=None`.

    `.strip()` on it raised an AttributeError straight out of the task, past the fallback that
    exists for exactly this: no deck in hand. It must degrade the same way an unparseable reply
    does — one ERROR with context, empty deck, generic caption.
    """
    from contextlib import contextmanager

    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

    empty = MagicMock()
    empty.choices = [MagicMock(message=MagicMock(content=None))]
    profile = LinkedInProfile(full_name="Test User", job_title="CTO", company_name="ACME",
                              industry="Technology")

    @contextmanager
    def _env():
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=empty), \
             patch("cqc_lem.utilities.db.get_user_password_pair_by_id", return_value=("t@e.com", "p")), \
             patch("cqc_lem.utilities.selenium_util.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch("cqc_lem.utilities.linkedin.helper.get_my_profile", return_value=profile), \
             patch("cqc_lem.utilities.selenium_util.quit_gracefully"), \
             patch("cqc_lem.utilities.ai.ai_helper.log_error") as mock_log_error:
            yield mock_log_error

    with _env() as mock_log_error:
        from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
        post_text, carousel_dict = generate_carousel_content(user_id=7, stage="awareness")

    assert carousel_dict == {}
    assert isinstance(post_text, str) and post_text
    assert mock_log_error.call_count == 1
    assert mock_log_error.call_args.kwargs["user_id"] == 7
