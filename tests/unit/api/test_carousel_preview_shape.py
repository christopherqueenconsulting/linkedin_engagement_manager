"""`POST /api/generate-carousel` answers a shape failure with the field names (issue #1666).

The route used to hand the constructor an LLM-parsed dict and turn whatever pydantic raised into a
500 whose detail was the raw ValidationError dump. A deck the generator could not repair is an
upstream failure — 502, naming the slides that never came.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_CC = "cqc_lem.utilities.carousel_creator"
_AI = "cqc_lem.utilities.ai.ai_helper"

_GOOD_DECK = {
    "cover": {"title": "The 3 checks I run", "content": "The exact stack."},
    "contents": [{"title": "1. Pin the tag", "content": "Set IMAGE_TAG to the release tag."}],
    "call_to_action": {"title": "Save this", "content": "Save it for your next deploy."},
}
_NO_COVER = {k: v for k, v in _GOOD_DECK.items() if k != "cover"}


def _post(api_client, deck, render_return=None, generator=None):
    from tests.unit.api.conftest import SESSION_TOKEN
    generator_patch = patch(f"{_AI}.generate_carousel_content",
                            **({"side_effect": generator} if generator
                               else {"return_value": ("caption", deck)}))
    with generator_patch, patch(f"{_CC}.create_carousel_slide_images",
                                return_value=render_return or ["/tmp/slide_1.png"]) as render:
        resp = api_client.post("/api/generate-carousel",
                               json={"session_token": SESSION_TOKEN, "stage": "awareness"})
    return resp, render


@pytest.mark.parametrize("deck,expected", [
    (_NO_COVER, "cover"),
    ({}, "cover"),
    (None, "cover"),
], ids=["no-cover", "empty-deck", "no-deck"])
def test_an_unusable_deck_is_a_502_naming_the_fields(api_client, signed_in, deck, expected):
    resp, render = _post(api_client, deck)
    assert resp.status_code == 502
    assert expected in resp.json()["detail"]
    render.assert_not_called()


def test_a_complete_deck_still_previews(api_client, signed_in):
    resp, render = _post(api_client, _GOOD_DECK)
    assert resp.status_code == 200
    assert render.call_count == 1
    assert resp.json()["detail"]["caption"] == "caption"


def test_an_upstream_crash_is_still_a_500(api_client, signed_in):
    """The 502 is the SHAPE answer only — a generator that raised keeps the old status."""
    resp, _ = _post(api_client, None, generator=RuntimeError("provider down"))
    assert resp.status_code == 500
