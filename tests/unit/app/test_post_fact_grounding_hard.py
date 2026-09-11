"""Post-surface fact grounding is HARD (issue #1971).

Three generated posts carrying invented figures published to the operator's own profile: the
first-person-only detector never fired on a third-person industry claim, and the number gate ran
only on the two fact-anchored archetypes. These tests pin the new posture — EVERY post's numbers
are graded against what the writer was actually given, an ungrounded one is repaired once and then
HELD at PENDING with the number named, a grounded one still ships, a forbidden-claim subject is
refused regardless of grounding, and a post with no numbers is untouched.

The three live bodies below are paraphrases of the posts that filed the issue, carrying the exact
figures that went out.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import ai_helper, story_bank as sb
from cqc_lem.utilities.quality_gates import GATE_FACT_GROUNDING, GATE_FORBIDDEN_CLAIM, demoting_findings

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

_LIVE_POSTS = {
    "router": ("I built a complexity router for LLM calls.\n\nRoute <500-token prompts to a 350M "
               "model and you get ≈45% lower cost-per-call and <200ms latency. The frontier model "
               "only sees the hard prompts."),
    "inference": ("AI inference costs dropped 30% in Q2 2026. Teams that adopted routing saw 20% "
                  "faster time-to-value.\n\nWhat changed for you this quarter?"),
    "throughput": ("41 PRs in a day. 369 that month. That is what an agent pipeline does when the "
                   "gates are real.\n\nWhat would you ship with that throughput?"),
}
_EXPECTED_NUMBERS = {"router": ["500", "350", "45%"], "inference": ["30%", "20%"],
                     "throughput": ["41", "369"]}

_NO_NUMBERS = ("Routing is a product decision, not a model decision.\n\nI learned that the hard way "
               "when a client's simplest prompts kept landing on the frontier model.")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("FACT_GROUNDING_SEVERITY", "FACT_GROUNDING_SEVERITY_POST", "FORBIDDEN_CLAIM_TERMS"):
        monkeypatch.delenv(name, raising=False)
    ai_helper._SUPPLIED_MATERIAL.clear()


def _gates(content, **kwargs):
    from cqc_lem.app import run_content_plan as rcp
    kwargs.setdefault("post_type", "text")
    with patch(f"{_RCP}._post_missing_required_asset", return_value=False):
        return rcp.evaluate_post_gates(42, content, kwargs.pop("post_type"), **kwargs)


class TestTheThreeLivePostsFailTheGate:
    """The replay the issue's verifier asks for: each body is refused with its numbers named."""

    @pytest.mark.parametrize("key", sorted(_LIVE_POSTS))
    def test_each_live_post_is_held_with_its_numbers_named(self, key):
        findings = _gates(_LIVE_POSTS[key], authenticity_score=95)
        held = [f for f in demoting_findings(findings) if f["gate"] == GATE_FACT_GROUNDING]
        assert len(held) == 1, findings
        details = " ".join(held[0]["details"])
        for number in _EXPECTED_NUMBERS[key]:
            assert number in details

    def test_a_third_person_industry_claim_is_graded_too(self):
        # The first-person-only story-bank check never fired on this one; the post surface now does.
        findings = _gates(_LIVE_POSTS["inference"], authenticity_score=95)
        assert [f["gate"] for f in demoting_findings(findings)] == [GATE_FACT_GROUNDING]

    def test_the_archetype_no_longer_decides_whether_numbers_are_graded(self):
        findings = _gates(_LIVE_POSTS["throughput"], archetype="personal_lesson")
        assert [f["gate"] for f in findings] == [GATE_FACT_GROUNDING]


class TestGroundedNumbersStillShip:
    def test_a_number_the_story_bank_backs_passes(self):
        anchors = ["Shipped 41 PRs in one day and 369 over the month on the agent pipeline."]
        assert _gates(_LIVE_POSTS["throughput"], fact_anchors=anchors) == []

    def test_a_number_from_the_research_actually_supplied_passes(self):
        research = ("Industry survey: AI inference costs fell 30% in Q2 2026; routing adopters "
                    "report 20% faster time-to-value.")
        assert _gates(_LIVE_POSTS["inference"], extra_fact_sources=[research]) == []

    def test_a_number_from_the_profile_brief_passes(self):
        brief = "Voice brief: 15+ years full-stack; routed 500-token prompts to a 350M model for a 45% saving."
        assert _gates(_LIVE_POSTS["router"], profile_synthesis=brief) == []

    def test_a_post_with_no_numbers_is_untouched(self):
        assert _gates(_NO_NUMBERS, authenticity_score=95) == []

    def test_ops_can_put_the_surface_back_to_warn_without_a_deploy(self, monkeypatch):
        monkeypatch.setenv("FACT_GROUNDING_SEVERITY_POST", "warn")
        # A non-fact-anchored archetype is then ungraded, exactly the pre-#1971 posture.
        assert _gates(_LIVE_POSTS["throughput"], archetype="personal_lesson") == []


class TestForbiddenClaims:
    def test_a_figure_on_a_forbidden_subject_is_held_even_when_grounded(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router; LEM router")
        anchors = [_LIVE_POSTS["router"]]  # every number grounded — still refused
        findings = _gates(_LIVE_POSTS["router"], fact_anchors=anchors)
        assert [f["gate"] for f in demoting_findings(findings)] == [GATE_FORBIDDEN_CLAIM]
        assert "complexity router" in " ".join(findings[0]["details"])

    def test_an_authors_edit_does_not_clear_a_forbidden_claim(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router")
        findings = _gates(_LIVE_POSTS["router"], fact_anchors=[_LIVE_POSTS["router"]],
                          author_edited=True)
        assert [f["gate"] for f in findings] == [GATE_FORBIDDEN_CLAIM]

    def test_naming_the_subject_without_a_figure_is_fine(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router")
        assert _gates("The complexity router is the part I would build first again.") == []

    def test_the_term_list_is_parsed_and_normalised(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", " Complexity   Router ;; lem-router ; ")
        assert sb.forbidden_claim_terms() == ["complexity router", "lem router"]
        assert sb.forbidden_claims("Our complexity router saved 12 hours.") == ["complexity router"]
        assert sb.forbidden_claims("Our complexity router saved 12 hours.", terms=[]) == []
        assert sb.forbidden_claims(None, terms=["router"]) == []

    def test_the_subject_and_the_figure_need_not_share_a_sentence(self):
        # The post that filed the issue: the router is named first, its figures come two sentences on.
        assert sb.forbidden_claims(_LIVE_POSTS["router"], ["complexity router"]) == ["complexity router"]

    def test_a_year_or_list_numbering_is_not_a_figure(self):
        draft = "The complexity router shipped in 2026.\n\n1. Route\n2. Measure\n3. Repeat"
        assert sb.forbidden_claims(draft, ["complexity router"]) == []

    def test_a_term_matches_whole_words_only_with_punctuation_folded(self):
        # "ai" must never match "said"; "cost per call" must match the hyphenated spelling.
        assert sb.forbidden_claims("I said we shipped 3 things.", ["ai"]) == []
        assert sb.forbidden_claims("The problem cost us 3 days.", ["lem"]) == []
        assert sb.forbidden_claims("≈45% lower cost-per-call on the router.", ["cost per call"]) \
            == ["cost per call"]
        assert sb.forbidden_claims("Our AI router saved 12 hours.", ["AI  Router"]) == ["ai router"]


class TestReScoreIsNotAnApproval:
    """An unedited Re-score must not clear a figure the last grade already named as unbacked."""

    def test_an_untouched_unbacked_figure_stays_held_on_re_score(self):
        findings = _gates(_LIVE_POSTS["router"], author_edited=True,
                          previously_unverified=["45%"])
        assert [f["gate"] for f in findings] == [GATE_FACT_GROUNDING]
        assert findings[0]["details"] == ["Unbacked specific: 45%"]

    def test_a_figure_the_author_changed_or_added_is_credited(self):
        # The last grade named 45%; the author rewrote that sentence with 40%, so nothing holds.
        edited = _LIVE_POSTS["router"].replace("≈45%", "40%")
        assert _gates(edited, author_edited=True, previously_unverified=["45%"]) == []
        # And with no recorded finding at all, the author is credited with every figure.
        assert _gates(_LIVE_POSTS["router"], author_edited=True) == []

    def test_the_recorded_unbacked_figures_are_read_off_the_gate_reason(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.quality_gates import fact_grounding_finding
        stored = [fact_grounding_finding(["45%", "350"], []), {"gate": "ai_slop", "details": ["x"]}]
        with patch(f"{_RCP}.get_post_gate_reason", return_value=stored):
            assert rcp._recorded_unbacked_specifics(77) == ["45%", "350"]
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.log_warning") as warn:
            assert rcp._recorded_unbacked_specifics(77) == []
        warn.assert_called_once()

    def test_the_re_score_passes_the_recorded_figures_through(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.utilities.quality_gates import fact_grounding_finding
        _DB = "cqc_lem.utilities.db"
        with patch(f"{_DB}.get_post_user_id", return_value=1), \
             patch(f"{_RCP}.get_post_content", return_value=_LIVE_POSTS["router"]), \
             patch(f"{_DB}.get_post_type", return_value="text"), \
             patch(f"{_DB}.get_post_video_url", return_value=None), \
             patch(f"{_DB}.get_post_status", return_value="pending"), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}._score_and_persist_authenticity"), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._fact_anchors", return_value=[]), \
             patch(f"{_RCP}._post_material_sources", return_value=[]), \
             patch(f"{_RCP}.get_post_gate_reason",
                   return_value=[fact_grounding_finding(["45%"], [])]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as reason, \
             patch(f"{_RCP}.update_db_post_status") as status, \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}):
            result = rcp.rescore_post(77)
        assert result["status"] == "pending"
        assert [f["gate"] for f in reason.call_args.args[1]] == [GATE_FACT_GROUNDING]
        status.assert_not_called()

    def test_an_unset_list_forbids_nothing(self):
        assert sb.forbidden_claim_terms() == []
        assert sb.forbidden_claims(_LIVE_POSTS["router"]) == []


class TestTheReviewGateRepairsBeforeTheHold:
    """At generation time an ungrounded post goes to the editor ONCE with the numbers named."""

    def _ctx(self):
        from cqc_lem.domain.models import PostDraftContext
        return PostDraftContext(user_id=1, stage="awareness", post_type="thought_leadership",
                                user_profile=MagicMock(), prefs={}, profile_synthesis="voice",
                                blueprint={"format": "personal_lesson"}, post_id=77,
                                lead_magnet_cta="", story_directive="STORY DIRECTIVE")

    def _review(self, draft, repaired, anchors=(), material=()):
        from cqc_lem.app import run_content_plan as rcp
        clear = {"score": 0.2, "threshold": 0.78, "match": "x", "measure": "embedding",
                 "too_similar": False}
        with patch(f"{_RCP}.post_similarity_report", return_value=dict(clear)), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.mark_post_gate_demoted"), \
             patch(f"{_RCP}.humanize_text", side_effect=lambda text, **_: text), \
             patch(f"{_RCP}._check_post_alignment", return_value=True), \
             patch(f"{_RCP}.has_first_person_proof", return_value=True), \
             patch(f"{_RCP}._fact_anchors", return_value=list(anchors)), \
             patch(f"{_RCP}._post_material_sources", return_value=list(material)), \
             patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=repaired) as refine, \
             patch(f"{_RCP}.log_warning") as warn, \
             patch(f"{_RCP}.log_info") as info:
            out = rcp._review_generated_post(self._ctx(), draft, ["an earlier post"], story=None)
        return out, refine, (warn, info)

    def test_an_ungrounded_number_sends_the_draft_to_the_editor_with_the_finding(self):
        out, refine, _ = self._review(_LIVE_POSTS["throughput"], _NO_NUMBERS)
        assert out == _NO_NUMBERS
        findings = refine.call_args.kwargs["repair_findings"]
        assert [f["gate"] for f in findings] == [GATE_FACT_GROUNDING]
        assert any("41" in d for d in findings[0]["details"])

    def test_a_repair_that_still_invents_is_kept_for_the_gate_to_hold(self):
        out, _, (warn, info) = self._review(_LIVE_POSTS["throughput"], _LIVE_POSTS["inference"])
        assert out == _LIVE_POSTS["inference"]
        # INFO, never WARNING: a gate holding a draft is the expected outcome, and a warning here
        # would escalate into a filed defect on the third held post of the day.
        assert any("fact-grounding gate will hold it" in c.args[0] for c in info.call_args_list)
        assert not any("fact-grounding gate" in c.args[0] for c in warn.call_args_list)

    def test_grounded_numbers_never_reach_the_editor(self):
        out, refine, _ = self._review(_LIVE_POSTS["inference"], "unused",
                                      material=["costs fell 30% in Q2; 20% faster time-to-value"])
        assert out == _LIVE_POSTS["inference"]
        refine.assert_not_called()

    def test_a_forbidden_claim_is_a_repair_finding_too(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "agent pipeline")
        out, refine, _ = self._review(_LIVE_POSTS["throughput"], _NO_NUMBERS,
                                      anchors=[_LIVE_POSTS["throughput"]])
        assert out == _NO_NUMBERS
        assert [f["gate"] for f in refine.call_args.kwargs["repair_findings"]] == [GATE_FORBIDDEN_CLAIM]


class TestTheWritersMaterialWidensTheAllowList:
    def test_the_research_handed_to_the_writer_is_recorded_per_post(self):
        ai_helper.record_supplied_material(77, "costs fell 30% in Q2")
        assert ai_helper.supplied_material_for(77) == "costs fell 30% in Q2"
        ai_helper.forget_supplied_material(77)
        assert ai_helper.supplied_material_for(77) is None

    def test_material_accumulates_per_post(self):
        ai_helper.record_supplied_material(77, "research block")
        ai_helper.record_supplied_material(77, "the author's guidance")
        assert ai_helper.supplied_material_for(77) == "research block\n\nthe author's guidance"

    def test_the_lead_magnet_message_is_a_source_at_the_gate_pass(self):
        # `ensure_lead_magnet_cta` appends the resource name AFTER the review gate, so the
        # status-setter's grade would otherwise hold "my 12-point checklist" as invented.
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_profile_synthesis", return_value=None), \
             patch(f"{_RCP}.get_lead_magnet_settings",
                   return_value={"enabled": True, "keyword": "AUDIT",
                                 "message": "my 12-point AI readiness checklist"}):
            assert rcp._post_material_sources(1, 77) == ["my 12-point AI readiness checklist"]
        with patch(f"{_RCP}.get_profile_synthesis", return_value=None), \
             patch(f"{_RCP}.get_lead_magnet_settings", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.log_warning") as warn:
            assert rcp._post_material_sources(1, 77) == []
        warn.assert_called_once()

    def test_a_blog_summary_records_the_users_own_article(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.domain.models import PostDraftContext
        ctx = PostDraftContext(user_id=1, stage="awareness", post_type="blog_summary",
                               user_profile=MagicMock(), prefs={}, post_id=78)
        with patch(f"{_RCP}.get_user_blog_url", return_value="https://x.test/blog"), \
             patch(f"{_RCP}.get_main_blog_url_content",
                   return_value=("https://x.test/blog/p", "We cut onboarding time 40%.")), \
             patch(f"{_RCP}.process_selected_post"), \
             patch(f"{_RCP}.get_blog_summary_post_from_ai", return_value="draft"):
            assert rcp._draft_from_source(ctx) == ("draft", True)
        assert ai_helper.supplied_material_for(78) == "We cut onboarding time 40%."

    def test_a_website_post_records_the_selected_page(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.fetch_sitemap_urls", return_value=["https://x.test/a"]), \
             patch(f"{_RCP}.filter_relevant_urls", return_value=["https://x.test/a"]), \
             patch(f"{_RCP}.extract_page_content", return_value=("Title", "Latency fell 40%.")), \
             patch(f"{_RCP}.get_website_content_post_from_ai", return_value="draft"):
            assert rcp.generate_website_content_post("https://x.test/sitemap.xml", MagicMock(),
                                                     "awareness", post_id=79) == "draft"
        assert ai_helper.supplied_material_for(79) == "Latency fell 40%."

    def test_a_regenerates_guidance_is_the_authors_own_material(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.apply_post_guidance", return_value="We cut latency by 40%."), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RCP}.ensure_lead_magnet_cta", side_effect=lambda c, *a, **kw: c):
            out = rcp._apply_guidance_to_text_post(1, 80, "old draft", MagicMock(),
                                                   "mention we cut latency by 40%")
        assert "40%" in out
        assert ai_helper.supplied_material_for(80) == "mention we cut latency by 40%"

    def test_nothing_is_recorded_for_an_unattributed_or_empty_draft(self):
        ai_helper.record_supplied_material(None, "x")
        ai_helper.record_supplied_material(78, "   ")
        assert ai_helper._SUPPLIED_MATERIAL == {}
        assert ai_helper.supplied_material_for(None) is None
        ai_helper.forget_supplied_material(None)

    def test_the_registry_is_bounded(self):
        for i in range(ai_helper._SUPPLIED_MATERIAL_MAX + 5):
            ai_helper.record_supplied_material(i, f"m{i}")
        assert len(ai_helper._SUPPLIED_MATERIAL) == ai_helper._SUPPLIED_MATERIAL_MAX
        assert ai_helper.supplied_material_for(0) is None

    def test_the_trend_analysis_records_what_it_handed_the_writer(self):
        industry = MagicMock()
        industry.choices = [MagicMock(message=MagicMock(content="Software"))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=industry), \
             patch("cqc_lem.utilities.ai.ai_helper.research_topic",
                   return_value={"findings": "median age rose 40%", "sources": []}), \
             patch("cqc_lem.utilities.ai.ai_helper._select_focus_topic", return_value=None):
            profile = MagicMock(industry="Software")
            profile.model_dump_json.return_value = '{"industry": "Software"}'
            ai_helper.get_industry_trend_analysis_based_on_user_profile(
                profile, prefs={}, sequence_index=91)
        assert ai_helper.supplied_material_for(91) == "median age rose 40%"

    def test_the_material_sources_carry_the_brief_and_the_research(self):
        from cqc_lem.app import run_content_plan as rcp
        ai_helper.record_supplied_material(77, "research block")
        with patch(f"{_RCP}.get_profile_synthesis", return_value=("stored brief", None)) as read:
            assert rcp._post_material_sources(1, 77, None, "cta text") == [
                "cta text", "stored brief", "research block"]
            read.assert_called_once_with(1)
        assert rcp._post_material_sources(1, 77, "given brief") == ["given brief", "research block"]

    def test_an_unreadable_brief_costs_a_source_never_the_post(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_profile_synthesis", side_effect=RuntimeError("db down")):
            assert rcp._post_material_sources(1, 77) == []

    def test_the_anchors_are_read_for_every_post_while_hard(self, monkeypatch):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}._fact_anchors", return_value=["bank"]) as bank:
            assert rcp._fact_anchors_for(1, "personal_lesson") == ["bank"]
            monkeypatch.setenv("FACT_GROUNDING_SEVERITY_POST", "warn")
            assert rcp._fact_anchors_for(1, "personal_lesson") == []
            assert rcp._fact_anchors_for(1, "build_receipt") == ["bank"]
        assert bank.call_count == 2

    def test_the_status_setter_forgets_the_material_once_the_gates_ran(self):
        from cqc_lem.app import run_content_plan as rcp
        ai_helper.record_supplied_material(42, "research block")
        post = [{"user_id": 1, "id": 42, "post_type": "text", "buyer_stage": "awareness"}]
        with patch(f"{_RCP}.get_planned_posts_within_buffer", return_value=post), \
             patch(f"{_RCP}.count_ready_posts_within_buffer", return_value=0), \
             patch(f"{_RCP}.create_content", return_value=(_NO_NUMBERS, None)), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.update_db_post_status"), \
             patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._fact_anchors", return_value=[]), \
             patch(f"{_RCP}.get_profile_synthesis", return_value=None), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=95):
            rcp.auto_create_weekly_content(user_id=1)
        assert ai_helper.supplied_material_for(42) is None


class TestCommentsRefuseForbiddenClaimsToo:
    def test_a_comment_attaching_a_figure_to_a_forbidden_subject_is_skipped(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router")
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "2")
        post = "Routing by complexity is the cheapest reliability win most teams skip."
        draft = ("Routing by complexity being the skipped win is the part most cost posts miss. "
                 "Our complexity router cut the bill 45% the month we switched. "
                 "What made you finally route?")
        with patch("cqc_lem.utilities.ai.ai_helper.log_warning") as warn:
            out = ai_helper._gated_comment(lambda fix: draft, post, recent_comments=[], user_id=7)
        assert out is None
        assert "forbidden" in warn.call_args.args[0]


# The per-user list (issue #2047, phase 2 of #1971): a user's own subjects on top of the global
# floor, resolved in ONE helper and handed to every gated surface as `terms=`.
_COMMENT_POST = "Routing by complexity is the cheapest reliability win most teams skip."
_COMMENT_DRAFT = ("Routing by complexity being the skipped win is the part most cost posts miss. "
                  "Our complexity router cut the bill 45% the month we switched. "
                  "What made you finally route?")
_USER_PREFS = {"forbidden_claim_terms": ["Complexity Router"]}


class TestTheEffectiveListIsUserPlusGlobal:
    def test_the_users_terms_are_added_to_the_global_floor_never_instead_of_it(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "lem router; agent pipeline")
        assert sb.effective_forbidden_claim_terms(["Complexity Router"]) == [
            "complexity router", "lem router", "agent pipeline"]

    def test_the_two_lists_are_folded_and_de_duplicated(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "LEM-Router")
        assert sb.effective_forbidden_claim_terms(["lem router", " Lem   Router ", ""]) == ["lem router"]

    def test_no_user_list_is_exactly_the_global_list(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "lem router")
        assert sb.effective_forbidden_claim_terms(None) == ["lem router"]
        assert sb.effective_forbidden_claim_terms("not a list") == ["lem router"]
        assert sb.effective_forbidden_claim_terms([]) == ["lem router"]

    def test_neither_list_forbids_nothing(self):
        assert sb.effective_forbidden_claim_terms(None) == []
        assert sb.effective_forbidden_claim_terms([]) == []

    def test_the_env_is_read_at_call_time(self, monkeypatch):
        assert sb.effective_forbidden_claim_terms(["x"]) == ["x"]
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "y")
        assert sb.effective_forbidden_claim_terms(["x"]) == ["x", "y"]


class TestTheStoredListIsBounded:
    def test_terms_are_tidied_and_empties_dropped(self):
        assert sb.normalize_forbidden_claim_terms([" Complexity   Router ", "", "   ", None, 7]) == [
            "Complexity Router", "7"]

    def test_duplicates_on_the_folded_form_keep_the_first_spelling(self):
        assert sb.normalize_forbidden_claim_terms(["LEM-Router", "lem router", "Lem  Router"]) == [
            "LEM-Router"]

    def test_the_list_is_capped(self):
        terms = [f"subject {i}" for i in range(sb.FORBIDDEN_CLAIM_TERMS_MAX + 5)]
        out = sb.normalize_forbidden_claim_terms(terms)
        assert len(out) == sb.FORBIDDEN_CLAIM_TERMS_MAX
        assert out[0] == "subject 0" and out[-1] == f"subject {sb.FORBIDDEN_CLAIM_TERMS_MAX - 1}"

    def test_an_over_long_term_is_dropped_not_clipped(self):
        # A clipped subject would never match the text it was meant to catch — silent non-enforcement.
        long_term = "x" * (sb.FORBIDDEN_CLAIM_TERM_MAX_LEN + 1)
        edge = "y" * sb.FORBIDDEN_CLAIM_TERM_MAX_LEN
        assert sb.normalize_forbidden_claim_terms([long_term, edge, "router"]) == [edge, "router"]

    def test_anything_but_a_list_is_an_empty_list(self):
        assert sb.normalize_forbidden_claim_terms(None) == []
        assert sb.normalize_forbidden_claim_terms("complexity router") == []
        assert sb.normalize_forbidden_claim_terms({"a": 1}) == []
        assert sb.normalize_forbidden_claim_terms(("a", "b")) == ["a", "b"]


class TestAUsersOwnListHoldsTheirPostsOnly:
    def test_a_post_on_a_user_term_is_held_even_when_grounded(self):
        anchors = [_LIVE_POSTS["router"]]
        findings = _gates(_LIVE_POSTS["router"], fact_anchors=anchors, engagement_prefs=_USER_PREFS)
        assert [f["gate"] for f in demoting_findings(findings)] == [GATE_FORBIDDEN_CLAIM]
        assert "complexity router" in " ".join(findings[0]["details"])

    def test_another_user_without_the_term_is_unaffected(self):
        anchors = [_LIVE_POSTS["router"]]
        assert _gates(_LIVE_POSTS["router"], fact_anchors=anchors, engagement_prefs={}) == []
        assert _gates(_LIVE_POSTS["router"], fact_anchors=anchors,
                      engagement_prefs={"forbidden_claim_terms": ["agent pipeline"]}) == []

    def test_the_global_floor_still_applies_to_a_user_with_their_own_list(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router")
        anchors = [_LIVE_POSTS["router"]]
        findings = _gates(_LIVE_POSTS["router"], fact_anchors=anchors,
                          engagement_prefs={"forbidden_claim_terms": ["agent pipeline"]})
        assert [f["gate"] for f in findings] == [GATE_FORBIDDEN_CLAIM]
        assert "complexity router" in " ".join(findings[0]["details"])

    def test_a_user_term_that_is_never_given_a_figure_holds_nothing(self):
        assert _gates("The complexity router is the part I would build first again.",
                      engagement_prefs=_USER_PREFS) == []

    def test_the_review_gate_reads_the_list_off_the_draft_context(self):
        from cqc_lem.app import run_content_plan as rcp
        from cqc_lem.domain.models import PostDraftContext
        ctx = PostDraftContext(user_id=1, stage="awareness", post_type="thought_leadership",
                               user_profile=MagicMock(), prefs=_USER_PREFS, profile_synthesis="voice",
                               blueprint={"format": "personal_lesson"}, post_id=77,
                               lead_magnet_cta="", story_directive="STORY DIRECTIVE")
        clear = {"score": 0.2, "threshold": 0.78, "match": "x", "measure": "embedding",
                 "too_similar": False}
        with patch(f"{_RCP}.post_similarity_report", return_value=dict(clear)), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.mark_post_gate_demoted"), \
             patch(f"{_RCP}.humanize_text", side_effect=lambda text, **_: text), \
             patch(f"{_RCP}._check_post_alignment", return_value=True), \
             patch(f"{_RCP}.has_first_person_proof", return_value=True), \
             patch(f"{_RCP}._fact_anchors", return_value=[_LIVE_POSTS["router"]]), \
             patch(f"{_RCP}._post_material_sources", return_value=[]), \
             patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=_NO_NUMBERS) as refine, \
             patch(f"{_RCP}.log_info"):
            out = rcp._review_generated_post(ctx, _LIVE_POSTS["router"], ["an earlier post"], story=None)
        assert out == _NO_NUMBERS
        assert [f["gate"] for f in refine.call_args.kwargs["repair_findings"]] == [GATE_FORBIDDEN_CLAIM]


class TestACommentReadsTheAuthorsList:
    def _comment(self, prefs, user_id=7):
        with patch("cqc_lem.utilities.db.get_engagement_preferences",
                   return_value=prefs) as read, \
             patch("cqc_lem.utilities.ai.ai_helper.log_warning"):
            out = ai_helper._gated_comment(lambda fix: _COMMENT_DRAFT, _COMMENT_POST,
                                           recent_comments=[], user_id=user_id)
        return out, read

    def test_a_comment_on_the_authors_own_forbidden_subject_is_skipped(self, monkeypatch):
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "3")
        out, read = self._comment(_USER_PREFS)
        assert out is None
        # ONE prefs read per call, not one per attempt.
        read.assert_called_once_with(7)

    def test_another_authors_comment_is_untouched(self, monkeypatch):
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "3")
        out, _ = self._comment({"forbidden_claim_terms": ["agent pipeline"]}, user_id=8)
        assert out == _COMMENT_DRAFT

    def test_an_unreadable_row_fails_open_to_the_global_list(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router")
        with patch("cqc_lem.utilities.db.get_engagement_preferences",
                   side_effect=RuntimeError("db down")), \
             patch("cqc_lem.utilities.ai.ai_helper.log_warning") as warn:
            assert ai_helper._forbidden_claim_terms_for_user(7) == ["complexity router"]
        assert any("forbidden-claim preference" in c.args[0] for c in warn.call_args_list)
        monkeypatch.delenv("FORBIDDEN_CLAIM_TERMS")
        with patch("cqc_lem.utilities.db.get_engagement_preferences",
                   side_effect=RuntimeError("db down")), \
             patch("cqc_lem.utilities.ai.ai_helper.log_warning"):
            assert ai_helper._forbidden_claim_terms_for_user(7) == []

    def test_an_unattributed_caller_reads_no_row(self, monkeypatch):
        monkeypatch.setenv("FORBIDDEN_CLAIM_TERMS", "complexity router")
        with patch("cqc_lem.utilities.db.get_engagement_preferences") as read:
            assert ai_helper._forbidden_claim_terms_for_user(None) == ["complexity router"]
        read.assert_not_called()
