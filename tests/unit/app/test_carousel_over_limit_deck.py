"""A deck over a list limit renders its first N slides instead of crashing (#2241 showcase).

Three of five decision-stage decks died at `model_cls(**carousel_dict)` with
`ProductDemoCarousel additional_features — List should have at most 2 items after validation, not
3/4 [too_long]`, so posts 128, 133 and 144 rendered NO slides. The construction site now trims
every list (and string) to the model's own limits first, and logs what it trimmed at INFO.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_CC = "cqc_lem.utilities.carousel_creator"


def _feature(n: int) -> dict:
    return {"title": f"Feature {n}", "content": f"What feature {n} does for you."}


def _decision_deck(extra: int) -> dict:
    """The exact failing shape: a ProductDemoCarousel with `extra` additional features."""
    return {
        "cover": {"title": "Route every call by cost", "content": "The exact router we run."},
        "main_feature": {"title": "Tiered aliases", "content": "Simple, medium and complex."},
        "additional_features": [_feature(n) for n in range(extra)],
        "call_to_action": {"title": "Save this", "content": "Save it for your next review."},
    }


def _run(deck):
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
            patch(f"{_RCP}.log_error") as log_error, \
            patch("cqc_lem.utilities.logger.log_info") as log_info:
        create_carousel_content(1, "decision", post_id=128)
    return render, status, log_error, log_info


@pytest.mark.parametrize("extra", [3, 4])
def test_a_decision_deck_with_too_many_features_still_renders(extra):
    render, status, log_error, log_info = _run(_decision_deck(extra))
    assert render.call_count == 1
    built = render.call_args[0][0]
    assert type(built).__name__ == "ProductDemoCarousel"
    # The FIRST two features survive, in order — the beats run cover-side first.
    assert [s.title for s in built.additional_features] == ["Feature 0", "Feature 1"]
    status.assert_not_called()
    assert log_error.call_count == 0
    trimmed = [c for c in log_info.call_args_list if "trimmed" in str(c.args[0])]
    assert trimmed and f"additional_features: {extra} -> 2 items" in trimmed[0].args[0]


def test_a_deck_within_its_limits_logs_no_trim():
    render, _status, _log_error, log_info = _run(_decision_deck(2))
    assert render.call_count == 1
    assert not [c for c in log_info.call_args_list if "trimmed" in str(c.args[0])]
