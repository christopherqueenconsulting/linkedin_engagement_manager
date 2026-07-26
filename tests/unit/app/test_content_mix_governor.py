"""Unit tests for the 70/20/10 governor inside the content pipeline (issue #618): the plan
classifies every post and keeps promo at/below the ceiling, the class is persisted and flows back
into generation, the promo slot is forced into a case-study shape, and a meeting-ask CTA is both
repaired deterministically and failed by the review gate."""

from datetime import time
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_DISABLED_LM = {"enabled": False, "keyword": None, "message": None}
_NO_NEWSLETTER = {"enabled": False, "title": None}


def _plan(user_id=1, counts=None, mixes=None):
    """Run plan_content_for_user and return the saved daily plan. The plan's LENGTH depends on the
    days left in the real current month, so tests that need a specific class in a specific slot pass
    `mixes` (a long-enough class list) instead of assuming a 30-entry plan."""
    from cqc_lem.app.run_content_plan import plan_content_for_user
    patches = [
        patch(f"{_RCP}.save_content_plan"),
        patch(f"{_RCP}.get_best_posting_time", return_value=time(9, 0)),
        patch(f"{_RCP}.get_last_planned_post_date_for_user", return_value=None),
        patch(f"{_RCP}.get_post_type_counts",
              return_value=counts or {"carousel": 0, "text": 0, "video": 0, "document": 0}),
    ]
    if mixes is not None:
        patches.append(patch(f"{_RCP}.assign_content_mix", return_value=mixes))
    started = [p.start() for p in patches]
    try:
        plan_content_for_user.run(user_id=user_id)
    finally:
        for p in patches:
            p.stop()
    save = started[0]
    save.assert_called_once()
    return save.call_args.args[1]


class TestPlanGovernor:
    def test_every_planned_post_is_classified(self):
        from cqc_lem.utilities.ai.content_alignment import CONTENT_MIX_TARGET
        plan = _plan()
        assert plan and all(entry["content_mix"] in CONTENT_MIX_TARGET for entry in plan)

    def test_promo_stays_within_the_ceiling(self):
        from cqc_lem.utilities.ai.content_alignment import PROMO_MAX_RATIO
        plan = _plan()
        promo = [p for p in plan if p["content_mix"] == "promo"]
        assert len(promo) / len(plan) <= PROMO_MAX_RATIO

    def test_promo_slots_prefer_text_posts(self):
        """The promo body has to be case-study shaped, which is steered in the text-post prompt."""
        plan = _plan(mixes=["promo"] + ["value"] * 40)
        promo = [p for p in plan if p["content_mix"] == "promo"]
        assert len(promo) == 1
        assert promo[0]["post_type"] == "text"

    def test_post_type_balance_is_preserved(self):
        """Claiming a text post for the promo slot must not change the plan's length or its types."""
        baseline = _plan(mixes=["value"] * 40)
        governed = _plan(mixes=["promo"] + ["value"] * 40)
        assert len(governed) == len(baseline)
        assert all(p["post_type"] in {"text", "carousel", "video", "document"} for p in governed)

    def test_promo_falls_back_when_no_text_post_is_left(self):
        from cqc_lem.app.run_content_plan import _take_planned_post_type
        assert _take_planned_post_type(["carousel", "text", "video"], "promo") == "text"
        assert _take_planned_post_type(["carousel", "video"], "promo") == "video"
        assert _take_planned_post_type(["text", "carousel"], "value") == "carousel"

    def test_existing_posts_offset_the_cadence(self):
        counts = {"carousel": 2, "text": 2, "video": 1, "document": 0}
        with patch(f"{_RCP}.assign_content_mix", return_value=["value"] * 40) as assign:
            _plan(counts=counts)
        assert assign.call_args.kwargs["offset"] == 5


class TestSaveContentPlan:
    def test_persists_the_mix_class(self):
        from cqc_lem.app.run_content_plan import save_content_plan
        from datetime import datetime
        plan = [{"scheduled_datetime": datetime(2026, 8, 1, 14, 0), "post_type": "text",
                 "stage": "awareness", "content_mix": "promo"}]
        with patch(f"{_RCP}.insert_planned_post") as insert:
            save_content_plan(3, plan)
        assert insert.call_args.kwargs["content_mix"] == "promo"

    def test_missing_mix_is_none_not_an_error(self):
        from cqc_lem.app.run_content_plan import save_content_plan
        from datetime import datetime
        plan = [{"scheduled_datetime": datetime(2026, 8, 1, 14, 0), "post_type": "text",
                 "stage": "awareness"}]
        with patch(f"{_RCP}.insert_planned_post") as insert:
            save_content_plan(3, plan)
        assert insert.call_args.kwargs["content_mix"] is None


class TestMixFlowsIntoGeneration:
    def test_planned_post_row_carries_the_class_into_create_content(self):
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"user_id": 1, "id": 5, "post_type": "text", "buyer_stage": "awareness",
                "content_mix": "promo"}
        with patch(f"{_RCP}.create_content", return_value=("body", None)) as create, \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}._gate_findings_for_post", return_value=[]), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.record_post_generated"):
            assert _create_content_for_planned_post(post, {}) is True
        assert create.call_args.kwargs["content_mix"] == "promo"

    def test_create_content_forwards_to_the_text_post(self):
        from cqc_lem.app.run_content_plan import create_content
        with patch(f"{_RCP}.create_text_post", return_value="body") as text:
            create_content(1, "text", "awareness", post_id=5, content_mix="authority")
        assert text.call_args.kwargs["content_mix"] == "authority"


def _run_text_post(generated, content_mix=None, lead_magnet=None, newsletter=None, post_id=77,
                   stories=None):
    """Drive create_text_post with a stubbed generator, returning (content, generator, blueprint)."""
    from cqc_lem.app import run_content_plan as rcp
    from cqc_lem.utilities.ai.content_framework import select_blueprint as real_select
    gen = MagicMock(return_value=generated)
    selector = MagicMock(side_effect=real_select)
    patches = [
        patch(f"{_RCP}.get_engagement_preferences", return_value={}),
        patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
        patch(f"{_RCP}.get_lead_magnet_settings", return_value=lead_magnet or _DISABLED_LM),
        patch(f"{_RCP}.get_newsletter_settings", return_value=newsletter or _NO_NEWSLETTER),
        patch(f"{_RCP}.get_recent_post_texts", return_value=[]),
        patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
        patch(f"{_RCP}.get_story_bank_entries", return_value=stories or []),
        patch(f"{_RCP}.record_story_bank_use"),
        patch(f"{_RCP}.get_shape_performance", return_value=None),
        patch(f"{_RCP}.select_blueprint", selector),
        patch(f"{_RCP}.update_db_post_shape"),
        patch(f"{_RCP}.get_thought_leadership_post_from_ai", gen),
        patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.strip_engagement_bait", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}._score_and_persist_authenticity"),
    ]
    for p in patches:
        p.start()
    try:
        out = rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                   user_profile=MagicMock(), post_id=post_id,
                                   content_mix=content_mix)
    finally:
        for p in patches:
            p.stop()
    return out, gen, selector


_CLEAN = ("We cut their churn from 9% to 4% in one quarter.\n\n"
          "The fix was boring: one onboarding call in week one, run by a human.\n\n"
          "What's the least glamorous fix that worked for you?")

_WITH_MEETING_ASK = ("We cut their churn from 9% to 4% in one quarter.\n\n"
                     "The fix was boring: one onboarding call in week one.\n\n"
                     "Want the same? Book a call with me this week.")


class TestCreateTextPostMixHandling:
    def test_class_reaches_the_generator_prompt(self):
        _, gen, _ = _run_text_post(_CLEAN, content_mix="authority")
        assert gen.call_args.kwargs["content_mix"] == "authority"

    _PROMO_STORY = {"id": 5, "kind": "client_win", "title": "Churn fix",
                    "body": "We cut a client's churn from 9% to 4% in one quarter.",
                    "happened_at": None, "used_count": 0, "last_used_at": None, "active": True}

    def test_promo_slot_with_a_story_anchor_is_forced_into_a_case_study_shape(self):
        _, gen, selector = _run_text_post(_CLEAN, content_mix="promo",
                                          stories=[self._PROMO_STORY])
        assert selector.call_args.kwargs["guidance"] == "case_snapshot"
        assert gen.call_args.kwargs["blueprint"]["format"] == "case_snapshot"
        assert gen.call_args.kwargs["content_mix"] == "promo"

    def test_promo_without_a_story_anchor_is_demoted_to_value(self):
        # Integration seam (#618 x #620): with no story anchor the fabrication gate has no
        # allow-list and is skipped — a promo case study would be free to invent its outcome
        # number. The slot must degrade to audience-value content, not an invented case study.
        _, gen, selector = _run_text_post(_CLEAN, content_mix="promo")
        assert selector.call_args.kwargs["guidance"] is None
        assert gen.call_args.kwargs["content_mix"] == "value"

    def test_unclassified_post_keeps_pure_shape_rotation(self):
        _, gen, selector = _run_text_post(_CLEAN)
        assert selector.call_args.kwargs["guidance"] is None
        assert gen.call_args.kwargs["content_mix"] is None

    def test_meeting_ask_is_replaced_with_an_artifact_cta(self):
        from cqc_lem.utilities.ai.content_alignment import contains_meeting_ask
        lm = {"enabled": True, "keyword": "AUDIT", "message": "the churn audit checklist"}
        out, _, _ = _run_text_post(_WITH_MEETING_ASK, content_mix="promo", lead_magnet=lm,
                                   post_id=30)
        assert contains_meeting_ask(out) is False
        assert "AUDIT" in out
        assert "churn from 9% to 4%" in out

    def test_clean_draft_is_left_alone(self):
        out, _, _ = _run_text_post(_CLEAN, content_mix="value")
        assert out == _CLEAN


class TestReviewGateLint:
    """The acceptance criterion: meeting-ask CTA text fails review; an artifact CTA passes."""

    def _findings(self, content):
        from cqc_lem.app.run_content_plan import evaluate_post_gates
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False):
            return evaluate_post_gates(5, content, "text")

    def test_meeting_ask_is_held_for_review(self):
        from cqc_lem.utilities.quality_gates import GATE_MEETING_CTA, demoting_findings
        findings = self._findings(_WITH_MEETING_ASK)
        gates = [f["gate"] for f in findings]
        assert GATE_MEETING_CTA in gates
        held = demoting_findings(findings)
        assert [f["gate"] for f in held] == [GATE_MEETING_CTA]
        assert findings[0]["details"] == ["Book a call"]

    def test_artifact_cta_passes(self):
        from cqc_lem.utilities.quality_gates import GATE_MEETING_CTA
        artifact = _CLEAN + "\n\nComment AUDIT and I'll DM you the checklist."
        findings = self._findings(artifact)
        assert GATE_MEETING_CTA not in [f["gate"] for f in findings]

    def test_lint_runs_for_non_text_post_types_too(self):
        from cqc_lem.utilities.quality_gates import GATE_MEETING_CTA
        findings = self._findings("Slide deck caption. Let's set up a call to walk through it.")
        assert GATE_MEETING_CTA in [f["gate"] for f in findings]
