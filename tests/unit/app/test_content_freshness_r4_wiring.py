"""Showcase round 4 wiring in the content plan.

The story cooldown, the CTA rotation, the plain fold, topic diversity, deck counts / document
wording and the author-fact industry check — each proven at the seam where `run_content_plan`
applies it.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.domain.models import PostDraftContext
from cqc_lem.utilities.ai import content_framework as cf, story_bank as sb

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

ENTRIES = [
    {"id": 1, "kind": "number", "title": "160 releases in 32 days",
     "body": "Between June 25 and July 27 my system shipped 160 releases.", "active": True,
     "used_count": 17},
    {"id": 2, "kind": "mistake", "title": "The 429 doom loop",
     "body": "LinkedIn returned 429s on July 20 and my breaker stayed tripped for 6 days.",
     "active": True, "used_count": 12},
]
TOLD_1 = "Between June 25 and July 27 we shipped 160 releases with no downtime."
TOLD_2 = "On July 20 the 429s started and the breaker stayed tripped for 6 days."


def _ctx(**overrides) -> PostDraftContext:
    fields = dict(user_id=1, stage="awareness", post_type="thought_leadership",
                  user_profile=MagicMock(), prefs={"focus_topics": ["AI costs", "Hiring"]},
                  profile_synthesis="voice", blueprint={"format": "personal_lesson",
                                                        "cta_type": cf.CTA_TYPE_QUESTION},
                  post_id=40, history_directive="", story_directive="")
    fields.update(overrides)
    return PostDraftContext(**fields)


# --- Story cooldown --------------------------------------------------------------------------------

class TestStoryCooldownWiring:
    def test_every_story_cooling_returns_none_and_says_so(self):
        outcome: dict = {}
        with patch(f"{_RCP}.get_story_bank_entries", return_value=ENTRIES), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[TOLD_1, TOLD_2]) as recent:
            assert rcp._select_story_for_post(1, {}, outcome=outcome) is None
        recent.assert_called_once_with(1, limit=sb.STORY_COOLDOWN_POSTS,
                                       within_days=sb.STORY_COOLDOWN_DAYS)
        assert outcome == {"cooled": True}

    def test_a_fresh_story_still_anchors(self):
        outcome: dict = {}
        with patch(f"{_RCP}.get_story_bank_entries", return_value=ENTRIES), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[TOLD_1]):
            assert rcp._select_story_for_post(1, {}, outcome=outcome)["id"] == 2
        assert outcome == {}

    def test_an_empty_bank_is_not_a_cooldown(self):
        outcome: dict = {}
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[TOLD_1]):
            assert rcp._select_story_for_post(1, {}, outcome=outcome) is None
        assert outcome == {"cooled": False}

    def test_the_cooled_directive_carries_the_curated_pool(self):
        items = [{"publisher": "Census", "title": "Retail sales rose", "excerpt": "Up 0.4%."}]
        with patch(f"{_RCP}.get_draftable_curated_sources", return_value=items) as pool:
            directive = rcp._no_anchor_directive(1, {"cooled": True})
        pool.assert_called_once_with(1, limit=sb.COOLDOWN_CURATED_ITEMS)
        assert "NO STORY THIS TIME" in directive and "OUTSIDE ITEM (Census)" in directive

    def test_an_unreadable_pool_leaves_research_alone(self):
        with patch(f"{_RCP}.get_draftable_curated_sources", side_effect=RuntimeError("db")):
            directive = rcp._no_anchor_directive(1, {"cooled": True})
        assert "NO STORY THIS TIME" in directive and "OUTSIDE ITEM" not in directive

    def test_no_cooldown_is_the_empty_bank_directive(self):
        assert rcp._no_anchor_directive(1, {}) == sb.no_story_directive()
        assert rcp._no_anchor_directive(1, None) == sb.no_story_directive()

    def test_resolve_story_anchor_reports_the_cooldown(self):
        outcome: dict = {}
        with patch(f"{_RCP}._select_story_for_post",
                   side_effect=lambda *a, **kw: kw["outcome"].update(cooled=True)), \
             patch(f"{_RCP}.get_draftable_curated_sources", return_value=[]):
            story, directive, mix = rcp._resolve_story_anchor(1, 9, {}, None, None, "value",
                                                              outcome=outcome)
        assert story is None and mix == "value" and outcome == {"cooled": True}
        assert "NO STORY THIS TIME" in directive

    def test_a_cooled_personal_story_slot_is_written_from_research(self):
        captured = {}

        def tl(*_a, **kw):
            captured["story_directive"] = kw.get("story_directive")
            return "A research post about wages."

        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=ENTRIES), \
             patch(f"{_RCP}._select_story_for_post",
                   side_effect=lambda *a, **kw: kw["outcome"].update(cooled=True)), \
             patch(f"{_RCP}.get_draftable_curated_sources", return_value=[]), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.get_personal_story_post_from_ai") as personal, \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=tl), \
             patch(f"{_RCP}.get_industry_news_post_from_ai", side_effect=tl):
            out = rcp.create_text_post(1, "awareness", post_type="personal_story",
                                       user_profile=MagicMock(), refine_final_post=False,
                                       post_id=4)
        assert out == "A research post about wages."
        personal.assert_not_called()
        assert "NO STORY THIS TIME" in captured["story_directive"]


# --- CTA rotation ---------------------------------------------------------------------------------

class TestCtaRotationWiring:
    def test_the_blueprint_gets_a_type_its_last_two_posts_did_not_use(self):
        recent = ["Body.\n\nWhich would you pick?", "Body.\n\nSave this for later."]
        out = rcp._steer_post_blueprint(1, {"format": "x"}, 7, "value", {}, None, recent)
        assert out["cta_type"] not in (cf.CTA_TYPE_QUESTION, cf.CTA_TYPE_SAVE,
                                       cf.CTA_TYPE_ARTIFACT)

    def test_the_promo_slot_gets_the_artifact(self):
        out = rcp._steer_post_blueprint(1, {"format": "x"}, 7, "promo", {}, None, [])
        assert out["cta_type"] == cf.CTA_TYPE_ARTIFACT

    def test_a_small_business_author_gets_the_plain_fold(self):
        prefs = {"business_goals": "AI for small-business owners"}
        assert rcp._steer_post_blueprint(1, {"format": "x"}, 7, "value", prefs, None,
                                         [])["plain_fold"] is True
        assert "plain_fold" not in rcp._steer_post_blueprint(1, {"format": "x"}, 7, "value",
                                                             {}, None, [])

    def test_history_is_read_when_the_caller_has_none(self):
        with patch(f"{_RCP}.get_recent_post_texts", return_value=["a\n\nWhat now?"]) as recent:
            assert rcp._recent_cta_types(1) == [cf.CTA_TYPE_QUESTION]
        recent.assert_called_once_with(1, limit=cf.CTA_TYPE_WINDOW)
        with patch(f"{_RCP}.get_recent_post_texts", side_effect=RuntimeError("db")):
            assert rcp._recent_cta_types(1) == []

    def test_a_non_dict_blueprint_passes_through(self):
        assert rcp._steer_post_blueprint(1, None, 7, "value") is None

    @pytest.mark.parametrize("allow,expected", [(True, True), (False, False)])
    def test_the_rotation_decides_the_lead_magnet(self, allow, expected):
        lm = {"enabled": True, "keyword": "AUDIT", "message": "A checklist."}
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value=lm):
            # post_id 30 is ON the legacy 1-in-3 cadence: the rotation overrides it both ways.
            _, include, directive = rcp._resolve_lead_magnet(1, 31 if allow else 30, None,
                                                             allow_artifact=allow)
        assert include is expected and bool(directive) is expected

    def test_a_promo_posts_keyword_is_exempt_from_the_bait_lint(self):
        lm = {"enabled": True, "keyword": "AUDIT", "message": "A checklist."}
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value=lm), \
             patch(f"{_RCP}.get_post_content_mix", return_value="promo"):
            assert rcp._cta_keyword_for(1, 31) == "AUDIT"
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value=lm), \
             patch(f"{_RCP}.get_post_content_mix", return_value="value"):
            assert rcp._cta_keyword_for(1, 31) is None

    def test_a_repeated_close_is_cut(self):
        recent = ["x\n\nWhich would you pick?", "x\n\nSave this."]
        draft = "Hook.\n\nThe insight.\n\nWhat would you do?"
        assert rcp._enforce_cta_rotation(_ctx(), draft, recent) == "Hook.\n\nThe insight."

    def test_a_fresh_close_and_the_artifact_are_left_alone(self):
        draft = "Hook.\n\nThe insight.\n\nWhat would you do?"
        assert rcp._enforce_cta_rotation(_ctx(), draft, ["x\n\nSave this."]) == draft
        artifact = _ctx(blueprint={"cta_type": cf.CTA_TYPE_ARTIFACT})
        assert rcp._enforce_cta_rotation(artifact, draft, ["x\n\nWhat?"]) == draft
        assert rcp._enforce_cta_rotation(_ctx(blueprint={}), draft, ["x\n\nWhat?"]) == draft

    def test_no_cut_when_a_no_ask_ending_is_itself_recent(self):
        draft = "Hook.\n\nThe insight.\n\nWhat would you do?"
        recent = ["x\n\nWhat?", "x\n\nThat is all."]
        assert rcp._enforce_cta_rotation(_ctx(), draft, recent) == draft

    def test_the_hook_pass_keeps_the_assigned_close(self):
        from cqc_lem.utilities.ai import ai_helper

        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="rewritten"))]
        with patch.object(ai_helper, "_call_llm", return_value=response) as call:
            ai_helper.optimize_post_hook("draft", cta_type=cf.CTA_TYPE_NONE)
            kept = call.call_args.kwargs["messages"][0]["content"]
            ai_helper.optimize_post_hook("draft", cta_type=cf.CTA_TYPE_SAVE)
            save = call.call_args.kwargs["messages"][0]["content"]
        assert "CLOSE — PRESERVE" in kept and "CLOSE — PRESERVE" not in save

    def test_the_refine_step_threads_the_cta_type(self):
        with patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c), \
             patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c) as hook:
            rcp._refine_draft("Hook.\n\nBody.", _ctx(), None, None)
        assert hook.call_args.kwargs["cta_type"] == cf.CTA_TYPE_QUESTION


# --- Topic diversity ------------------------------------------------------------------------------

REPEAT = "Model retirement retirement model cron cron alias."
FRESH = "Hiring hiring first ops person bakery bakery."


class TestTopicDiversityWiring:
    def test_recent_topics_prefer_the_recorded_topic(self):
        rows = [{"topic": "payroll, bakery", "content": "x"},
                {"topic": None, "content": REPEAT}, {"topic": None, "content": ""}]
        with patch(f"{_RCP}.get_recent_post_topics", return_value=rows) as read:
            topics = rcp._recent_topics(1, 9)
        read.assert_called_once_with(1, limit=cf.TOPIC_DIVERSITY_WINDOW, exclude_post_id=9)
        assert topics == ["payroll, bakery", cf.post_topic(REPEAT)]
        with patch(f"{_RCP}.get_recent_post_topics", side_effect=RuntimeError("db")):
            assert rcp._recent_topics(1, 9) == []

    def test_a_fresh_topic_ships_unchanged(self):
        ctx = _ctx()
        with patch(f"{_RCP}._recent_topics", return_value=[cf.post_topic(REPEAT)]), \
             patch(f"{_RCP}._compose_draft") as compose:
            assert rcp._diversify_topic(ctx, FRESH, None, None) == (FRESH, ctx)
        compose.assert_not_called()

    def test_a_repeat_is_regenerated_once_with_the_topics_named(self):
        ctx = _ctx()
        with patch(f"{_RCP}._recent_topics", return_value=[cf.post_topic(REPEAT)]), \
             patch(f"{_RCP}._compose_draft", side_effect=lambda c, **kw: (FRESH, c)) as compose:
            out, used = rcp._diversify_topic(ctx, REPEAT, None, None)
        assert out == FRESH and compose.call_count == 1
        assert "TOPIC RULE" in used.history_directive

    def test_a_second_repeat_falls_back_to_a_research_topic(self):
        ctx = _ctx(prefs={"focus_topics": ["model retirement", "Hiring"]})
        drafts = iter([(REPEAT, None), ("A research post.", None)])

        def compose(c, **kw):
            text, _ = next(drafts)
            return text, c

        with patch(f"{_RCP}._recent_topics", return_value=[cf.post_topic(REPEAT)]), \
             patch(f"{_RCP}._compose_draft", side_effect=compose) as composer:
            out, used = rcp._diversify_topic(ctx, REPEAT, None, None)
        assert out == "A research post." and composer.call_count == 2
        assert used.post_type == rcp._RESEARCH_TOPIC_TYPE
        assert used.prefs["focus_topics"] == ["Hiring"]

    def test_a_pinned_subject_or_a_raw_draft_is_never_second_guessed(self):
        pinned = _ctx(blueprint={"subject": "A launch"})
        raw = _ctx(similarity_check=False)
        with patch(f"{_RCP}._recent_topics") as read:
            assert rcp._diversify_topic(pinned, REPEAT, None, None)[0] == REPEAT
            assert rcp._diversify_topic(raw, REPEAT, None, None)[0] == REPEAT
            assert rcp._diversify_topic(_ctx(), None, None, None)[0] is None
        read.assert_not_called()


# --- Review gate: the plain fold and bolted-on industries -----------------------------------------

class TestReviewGateWiring:
    def _review(self, content, repaired, blueprint, profile="voice"):
        ctx = _ctx(blueprint=blueprint, profile_synthesis=profile)
        with patch(f"{_RCP}._fact_anchors", return_value=[sb.entry_text(ENTRIES[0])]), \
             patch(f"{_RCP}._post_material_sources", return_value=[]), \
             patch(f"{_RCP}._repair_draft", return_value=repaired) as repair, \
             patch(f"{_RCP}._persist_gate_findings") as persist, \
             patch(f"{_RCP}._record_post_similarity_finding"), \
             patch(f"{_RCP}._check_post_alignment"):
            out = rcp._review_generated_post(ctx, content, [], story=None)
        return out, repair, persist

    def test_a_jargon_fold_spends_one_rewrite_on_the_opening(self):
        jargon = ("I shipped 160 releases and the cron job never missed.\n\nThe MySQL side held.\n\n"
                  "Between June 25 and July 27 my system shipped 160 releases.")
        plain = ("I shipped 160 releases and no customer noticed a single one.\n\nThat saved hours."
                 "\n\nBetween June 25 and July 27 my system shipped 160 releases.")
        out, repair, persist = self._review(jargon, plain,
                                            {"format": "personal_lesson", "plain_fold": True})
        assert out == plain and repair.call_count == 1
        recorded = persist.call_args_list[0].args[2]
        assert any("fold_jargon" in d for f in recorded for d in f.get("details", []))

    def test_the_same_fold_for_a_developer_audience_is_not_rewritten(self):
        jargon = ("I shipped 160 releases and the cron job never missed.\n\nThe MySQL side held.\n\n"
                  "Between June 25 and July 27 my system shipped 160 releases.")
        out, repair, _ = self._review(jargon, "unused", {"format": "personal_lesson"})
        assert out == jargon
        repair.assert_not_called()

    def test_a_bolted_on_industry_is_repaired_against_the_authors_facts(self):
        draft = ("I shipped 160 releases between June 25 and July 27.\n\n"
                 "Our e-commerce platform never noticed.")
        clean = "I shipped 160 releases between June 25 and July 27.\n\nNobody noticed."
        out, repair, _ = self._review(draft, clean, {"format": "personal_lesson"})
        assert out == clean and repair.call_count == 1
        findings = repair.call_args.args[2]
        assert any("e-commerce" in " ".join(f.get("details", [])) for f in findings)

    def test_the_authors_facts_are_the_bank_and_profile_only(self):
        profile = MagicMock()
        profile.model_dump_json.return_value = '{"industry": "Retail"}'
        with patch(f"{_RCP}._fact_anchors", return_value=["bank"]):
            facts = rcp._author_facts(1, {"voice": "plain"}, profile)
        assert facts == ["bank", '{"voice": "plain"}', '{"industry": "Retail"}']
        broken = MagicMock()
        broken.model_dump_json.side_effect = ValueError("no")
        with patch(f"{_RCP}._fact_anchors", return_value=[]):
            assert rcp._author_facts(1, "brief", broken) == ["brief"]


# --- Decks ----------------------------------------------------------------------------------------

class TestDeckWiring:
    def _create(self, post_type, deck, caption):
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}._select_story_for_post", return_value=None), \
             patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
             patch(f"{_RCP}._report_carousel_fact_grounding"), \
             patch(f"{_RCP}._report_carousel_slide_slop"), \
             patch(f"{_RCP}._report_carousel_deck_consistency") as check, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=(caption, deck)):
            out = rcp.create_carousel_content(1, "awareness", None, post_type=post_type)
        return out, check.call_args.args[2]

    _DECK = {"cover": {"title": "4 Steps to calmer payroll", "content": "Swipe the carousel"},
             "tips": [{"title": "One", "content": "Do one."}, {"title": "Two", "content": "Do two."},
                      {"title": "Three", "content": "Do three."}],
             "call_to_action": {"title": "Follow", "content": "More soon."}}

    def test_a_document_never_calls_itself_a_carousel_and_its_cover_count_is_true(self):
        caption, deck = self._create("document", dict(self._DECK), "This carousel has 4 steps.")
        assert caption == "This document has 3 steps."
        assert deck["cover"] == {"title": "3 Steps to calmer payroll",
                                 "content": "Swipe the document"}

    def test_a_carousel_keeps_its_word(self):
        caption, deck = self._create("carousel", dict(self._DECK), "This carousel has 4 steps.")
        assert caption == "This carousel has 3 steps."
        assert deck["cover"]["content"] == "Swipe the carousel"
