"""Unit tests for SOURCE rotation in create_text_post (issue #1526).

The source archetype (`blog_summary`, `personal_story`, …) used to be an unweighted random draw, so
a user on the default 3/week cadence could go a whole month without a blog- or story-sourced post.
The rotation rides the planned row's id, and the fallback after a missing source keeps rotating
rather than re-drawing.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}


class TestSourceForSlot:
    def test_consecutive_slots_cover_every_source(self):
        from cqc_lem.app.run_content_plan import _POST_TYPES, _post_source_for_slot

        picked = [_post_source_for_slot(100 + i) for i in range(len(_POST_TYPES))]
        assert sorted(picked) == sorted(_POST_TYPES)

    def test_same_slot_always_resolves_to_the_same_source(self):
        from cqc_lem.app.run_content_plan import _post_source_for_slot

        assert _post_source_for_slot(41) == _post_source_for_slot(41)

    def test_unplanned_draft_falls_back_to_a_random_draw(self):
        from cqc_lem.app.run_content_plan import _POST_TYPES, _post_source_for_slot

        assert _post_source_for_slot(None) in _POST_TYPES

    def test_unusable_post_id_still_yields_a_source(self):
        from cqc_lem.app.run_content_plan import _POST_TYPES, _post_source_for_slot

        assert _post_source_for_slot("not-an-id") in _POST_TYPES


class TestNextSourceInRotation:
    def test_takes_the_next_writable_source_and_wraps(self):
        from cqc_lem.app.run_content_plan import _POST_TYPES, _next_source_in_rotation

        menu = [t for t in _POST_TYPES if t != "blog_summary"]
        assert _next_source_in_rotation("blog_summary", menu) == "website_content"
        # The last entry wraps to the first still-writable one.
        assert _next_source_in_rotation(_POST_TYPES[-1], [_POST_TYPES[0]]) == _POST_TYPES[0]

    def test_unknown_current_type_takes_the_first_menu_entry(self):
        from cqc_lem.app.run_content_plan import _next_source_in_rotation

        assert _next_source_in_rotation("affiliate_promo", ["industry_news"]) == "industry_news"


def _run_draft(post_id, blog_url=None):
    """create_text_post with every source but the generators mocked; returns the type that shipped."""
    from cqc_lem.app import run_content_plan as rcp

    seen = []

    def gen(*_args, **kwargs):
        return "generated post"

    def _capture(post_type):
        def _inner(*args, **kwargs):
            seen.append(post_type)
            return gen()
        return _inner

    with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
         patch(f"{_RCP}.get_lead_magnet_settings", return_value=_DISABLED_LM), \
         patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
         patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
         patch(f"{_RCP}.record_story_bank_use"), \
         patch(f"{_RCP}.update_db_post_shape"), \
         patch(f"{_RCP}.get_user_blog_url", return_value=blog_url), \
         patch(f"{_RCP}.get_main_blog_url_content", return_value=(None, None)), \
         patch(f"{_RCP}.get_user_sitemap_url", return_value=None), \
         patch(f"{_RCP}.get_thought_leadership_post_from_ai",
               side_effect=_capture("thought_leadership")), \
         patch(f"{_RCP}.get_industry_news_post_from_ai", side_effect=_capture("industry_news")), \
         patch(f"{_RCP}.get_personal_story_post_from_ai", side_effect=_capture("personal_story")), \
         patch(f"{_RCP}.generate_engagement_prompt_post",
               side_effect=_capture("engagement_prompt")):
        rcp.create_text_post(1, "awareness", user_profile=MagicMock(), refine_final_post=False,
                             post_id=post_id)
    return seen


class TestCreateTextPostUsesTheRotation:
    def test_slot_decides_the_source_instead_of_a_random_draw(self):
        from cqc_lem.app.run_content_plan import _POST_TYPES, _post_source_for_slot

        # A slot whose rotation lands on a generator that always has a source.
        post_id = next(i for i in range(len(_POST_TYPES))
                       if _post_source_for_slot(i) == "personal_story")
        assert _run_draft(post_id) == ["personal_story"]

    def test_missing_source_falls_through_to_the_next_in_rotation(self):
        from cqc_lem.app.run_content_plan import _POST_TYPES, _post_source_for_slot

        # blog_summary with no blog configured reports no source; website_content is next, and it
        # has no sitemap either, so the third attempt is industry_news.
        post_id = next(i for i in range(len(_POST_TYPES))
                       if _post_source_for_slot(i) == "blog_summary")
        assert _run_draft(post_id) == ["industry_news"]
