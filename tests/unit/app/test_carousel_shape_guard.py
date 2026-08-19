"""The plan task reads the deck's shape before building the model (issue #1666).

`model_cls(**carousel_dict)` on a deck missing `cover` raised a pydantic ValidationError whose
message named pydantic, not the generator that dropped the field — 332 grouped occurrences no one
could act on. The task now asks the model what it requires first, and flags the post `error`
without the opaque exception.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_CC = "cqc_lem.utilities.carousel_creator"

_GOOD_DECK = {
    "cover": {"title": "The 3 checks I run", "content": "The exact stack."},
    "contents": [{"title": "1. Pin the tag", "content": "Set IMAGE_TAG to the release tag."}],
    "call_to_action": {"title": "Save this", "content": "Save it for your next deploy."},
}
_NO_COVER = {k: v for k, v in _GOOD_DECK.items() if k != "cover"}


def _run_plan_task(deck):
    """Drive `create_carousel_content` to its model-construction step with `deck` in hand."""
    from cqc_lem.app.run_content_plan import create_carousel_content
    with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
            patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
            patch(f"{_RCP}._select_story_for_post", return_value=None), \
            patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
            patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                  return_value=("caption", deck)), \
            patch(f"{_CC}.create_carousel_slide_images", return_value=["/tmp/s1.png"]) as render, \
            patch(f"{_CC}.create_ppt"), \
            patch(f"{_RCP}.update_db_post_carousel_slides"), \
            patch(f"{_RCP}.update_db_post_shape"), \
            patch(f"{_RCP}.update_db_post_status") as status, \
            patch(f"{_RCP}.log_error") as log_error:
        create_carousel_content(7, "awareness", post_id=42)
    return render, status, log_error


@pytest.mark.parametrize("deck", [_NO_COVER, {}, None],
                         ids=["no-cover", "empty-deck", "no-deck"])
def test_an_unusable_deck_never_reaches_the_constructor(deck):
    from cqc_lem.utilities.db import PostStatus
    render, status, log_error = _run_plan_task(deck)
    render.assert_not_called()
    status.assert_called_once_with(42, PostStatus.ERROR)
    # `generate_carousel_content` already filed the ERROR where the fault was detected, so this
    # site adds no second grouped exception for the same condition.
    assert log_error.call_count == 0


def test_a_complete_deck_still_renders():
    render, status, log_error = _run_plan_task(_GOOD_DECK)
    assert render.call_count == 1
    status.assert_not_called()
    assert log_error.call_count == 0
