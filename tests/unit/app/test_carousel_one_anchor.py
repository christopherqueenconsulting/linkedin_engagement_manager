"""Unit tests for the carousel one-anchor split (issue #728): the WRITER gets the single selected
story-bank entry, the CHECKERS keep every active entry."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_ENTRIES = [
    {"id": 1, "kind": "artifact", "title": "Slide render",
     "body": "Rendered 40 slides in one pass", "active": True, "used_count": 0},
    {"id": 2, "kind": "number", "title": "Release count",
     "body": "Shipped 160 releases in a year", "active": True, "used_count": 3},
    {"id": 3, "kind": "mistake", "title": "Rollback",
     "body": "A VARCHAR overflow rolled back the whole upsert", "active": True, "used_count": 5},
]


def _create(entries, post_id=None, deck=None, caption="caption", used=None):
    from cqc_lem.app.run_content_plan import create_carousel_content
    with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
         patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
         patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
         patch(f"{_RCP}.get_shape_performance", return_value=None), \
         patch(f"{_RCP}.update_db_post_shape"), \
         patch(f"{_RCP}.update_db_post_status"), \
         patch(f"{_RCP}.get_story_bank_entries", return_value=entries), \
         patch(f"{_RCP}.record_story_bank_use") as record, \
         patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
               return_value=(caption, deck if deck is not None else {"bogus": True})) as gen:
        create_carousel_content(1, "awareness", post_id)
    if used is not None:
        used.append(record)
    return gen


class TestOneAnchorPerDeck:
    def test_only_one_entry_reaches_the_writer_however_full_the_bank_is(self):
        anchors = _create(_ENTRIES).call_args[1]["fact_anchors"]
        matched = [e for e in _ENTRIES if any(e["body"] in a for a in anchors)]
        assert len(matched) == 1

    def test_the_rotation_picks_the_least_used_entry(self):
        anchors = _create(_ENTRIES).call_args[1]["fact_anchors"]
        assert any("40 slides" in a for a in anchors)

    def test_an_empty_bank_leaves_the_writer_with_no_anchors_at_all(self):
        gen = _create([])
        assert gen.call_args[1]["fact_anchors"] == []
        assert "NO STORY BANK ENTRY IS AVAILABLE" in gen.call_args[1]["story_directive"]

    def test_a_bank_read_that_fails_never_blocks_the_carousel(self):
        from cqc_lem.app.run_content_plan import create_carousel_content
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", side_effect=RuntimeError("db down")), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", {"bogus": True})) as gen:
            assert create_carousel_content(1, "awareness", None) == "caption"
        assert gen.call_args[1]["fact_anchors"] == []

    def test_the_archetype_menu_follows_the_writer_anchor_not_the_bank_size(self):
        # A bank full of entries the writer never received cannot unlock a fact-anchored archetype:
        # with no anchor the deck would bake `[[…]]` placeholders into the rendered slide images.
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.ai.content_framework import fact_anchored_formats
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None):
            picks = {rcp._select_carousel_blueprint(1, [])["format"] for _ in range(200)}
        assert picks and not picks & set(fact_anchored_formats("post"))


class TestTheAnchorIsRotatedNotJustRead:
    """One anchor per deck is only half the invariant — without the usage write, `select_story`'s
    least-used ordering hands EVERY deck the same entry, and the next text post re-uses it too."""

    def test_the_deck_counts_its_anchor_as_used(self):
        used = []
        _create(_ENTRIES, used=used)
        assert used[0].call_args[0] == (1, 1)  # user 1, the least-used entry

    def test_an_empty_bank_records_nothing(self):
        used = []
        _create([], used=used)
        assert not used[0].called

    def test_a_bookkeeping_failure_never_blocks_the_carousel(self):
        from cqc_lem.app.run_content_plan import create_carousel_content
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_shape_performance", return_value=None), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=_ENTRIES), \
             patch(f"{_RCP}.record_story_bank_use", side_effect=RuntimeError("db down")), \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", {"bogus": True})):
            assert create_carousel_content(1, "awareness", None) == "caption"


class TestDeckFactGroundingIsCheckedAgainstTheWholeBank:
    _DECK = {"cover": {"title": "The receipt", "content": "Shipped 160 releases"},
             "insights": [{"title": "Cost", "content": "It cost 4,200 dollars to run"}],
             "call_to_action": {"title": "Save this", "content": "Save it."}}

    def _report(self, blueprint, deck=None, entries=_ENTRIES):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_story_bank_entries", return_value=entries), \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._report_carousel_fact_grounding(1, 7, blueprint,
                                                self._DECK if deck is None else deck)
        return warn

    def test_a_slide_number_the_whole_bank_cannot_back_is_reported(self):
        warn = self._report({"format": "build_receipt"})
        assert warn.called
        assert "4,200" in warn.call_args[0][0]

    def test_a_number_from_any_active_entry_is_verified_not_flagged(self):
        # 160 comes from an entry this deck was NOT anchored to — the checker still knows it is the
        # user's own material, so it is never called fabricated.
        warn = self._report({"format": "build_receipt"})
        assert "160" not in warn.call_args[0][0]

    def test_unfilled_placeholders_are_reported_because_they_render_into_the_images(self):
        deck = {"cover": {"title": "The receipt"},
                "insights": [{"title": "Cost", "content": "It cost [[COST: how much]] to run"}]}
        warn = self._report({"format": "build_receipt"}, deck=deck)
        assert any("COST: how much" in str(c[0][0]) for c in warn.call_args_list)

    def test_an_archetype_that_is_not_fact_anchored_is_never_graded(self):
        assert not self._report({"format": "personal_lesson"}).called

    def test_a_grading_failure_is_swallowed(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_story_bank_entries", side_effect=RuntimeError("db down")):
            rcp._report_carousel_fact_grounding(1, 7, {"format": "build_receipt"}, self._DECK)
