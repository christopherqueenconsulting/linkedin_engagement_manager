"""Unit tests for issue #1134 — a REPAIRED post is held for its author by default.

Two halves, and the second one only exists because of the first.

The review gate's second attempt is now an EDIT of the failing draft
(`get_ai_linked_post_refinement` with the findings) instead of another draft from the same writer.
A draft that only passed because it was edited is precisely the post nobody has read, and no later
pass can tell: by the time `evaluate_post_gates` runs, the draft that failed is gone. So the repair
path — and ONLY the repair path — raises `posts.ever_gate_demoted`, and `_may_auto_approve` reads it
against the user's own `hold_repaired_posts_for_review` toggle.

The regression the round-3 critic named is pinned here too: the three PRE-EXISTING callers of
`_persist_gate_findings` must never raise that flag, or the default-ON toggle would hold posts that
were never repaired.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

# Carries a concrete first-person lived detail, so the A2-proof check never fires on its own.
_DRAFT = "I cut a client's onboarding from 12 days to 3."
_REPAIRED = "I moved a team off nightly batch jobs in 9 days."

_OVER = {"score": 0.84, "threshold": 0.78, "match": "an earlier post", "measure": "embedding",
         "too_similar": True}
_CLEAR = {"score": 0.20, "threshold": 0.78, "match": "an earlier post", "measure": "embedding",
          "too_similar": False}


def _ctx(post_id=77):
    from cqc_lem.domain.models import PostDraftContext
    return PostDraftContext(user_id=1, stage="awareness", post_type="thought_leadership",
                            user_profile=MagicMock(), prefs={}, profile_synthesis="voice",
                            blueprint={}, post_id=post_id, lead_magnet_cta="",
                            story_directive="STORY DIRECTIVE")


def _review(verdicts, repaired=_REPAIRED):
    """Run the review gate over a draft, returning (content, refinement mock, mark mock)."""
    from cqc_lem.app import run_content_plan as rcp
    repair = (patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=repaired)
              if isinstance(repaired, Exception) else
              patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=repaired))
    with patch(f"{_RCP}.post_similarity_report", side_effect=list(verdicts)), \
         patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
         patch(f"{_RCP}.update_db_post_gate_reason"), \
         patch(f"{_RCP}.mark_post_gate_demoted") as marked, \
         patch(f"{_RCP}.humanize_text", side_effect=lambda text, **_: text), \
         patch(f"{_RCP}._check_post_alignment", return_value=True), \
         repair as refine:
        out = rcp._review_generated_post(_ctx(), _DRAFT, ["an earlier post"], story=None)
    return out, refine, marked


class TestTheRepairIsTheEditorNotTheWriter:
    def test_the_failing_draft_goes_to_the_refinement_editor(self):
        out, refine, _ = _review([_OVER, _CLEAR])
        assert out == _REPAIRED
        refine.assert_called_once()
        # THIS draft, handed to the editor with the findings — not a fresh brief for the writer.
        assert refine.call_args.args[0] == _DRAFT
        assert [f["gate"] for f in refine.call_args.kwargs["repair_findings"]] == ["similarity"]

    def test_the_writer_is_never_re_invoked(self):
        """The point of the change: `_compose_draft` is the writer, and the repair is not it."""
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.post_similarity_report", side_effect=[dict(_OVER), dict(_CLEAR)]), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.mark_post_gate_demoted"), \
             patch(f"{_RCP}.humanize_text", side_effect=lambda text, **_: text), \
             patch(f"{_RCP}._check_post_alignment", return_value=True), \
             patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=_REPAIRED), \
             patch(f"{_RCP}._compose_draft") as compose:
            rcp._review_generated_post(_ctx(), _DRAFT, ["an earlier post"], story=None)
        compose.assert_not_called()

    def test_the_editor_gets_the_writers_own_story_material(self):
        """The proof/fabrication findings are unanswerable without it.

        Both ask the editor to add or substitute a real first-person specific while forbidding
        invention — and the editor can see only the draft. `ctx.story_directive` is the exact
        string the writer was given, so the edit may draw on what the writer could and no more.
        """
        _, refine, _ = _review([_OVER, _CLEAR])
        assert refine.call_args.kwargs["repair_source_material"] == "STORY DIRECTIVE"

    def test_with_no_anchored_entry_the_editor_still_gets_the_no_invention_rule(self):
        """An unanchored draft must not silently drop the bank's absolute rule."""
        from dataclasses import replace

        from cqc_lem.app import run_content_plan as rcp
        ctx = replace(_ctx(), story_directive=None)
        with patch(f"{_RCP}.post_similarity_report", side_effect=[dict(_OVER), dict(_CLEAR)]), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.mark_post_gate_demoted"), \
             patch(f"{_RCP}.humanize_text", side_effect=lambda text, **_: text), \
             patch(f"{_RCP}._check_post_alignment", return_value=True), \
             patch(f"{_RCP}.get_ai_linked_post_refinement", return_value=_REPAIRED) as refine:
            rcp._review_generated_post(ctx, _DRAFT, ["an earlier post"], story=None)
        assert "do NOT invent" in refine.call_args.kwargs["repair_source_material"]

    def test_the_repair_brief_names_the_fix_the_review_queue_would_show(self):
        _, refine, _ = _review([_OVER, _CLEAR])
        finding = refine.call_args.kwargs["repair_findings"][0]
        assert finding["remediation"] and finding["explanation"]

    def test_a_clean_draft_is_never_repaired_or_flagged(self):
        out, refine, marked = _review([_CLEAR])
        assert out == _DRAFT
        refine.assert_not_called()
        marked.assert_not_called()

    def test_an_editor_that_fails_leaves_the_first_draft_standing(self):
        out, _, marked = _review([_OVER], repaired=RuntimeError("llm down"))
        assert out == _DRAFT
        # It still WENT through the repair path — the flag is about the attempt's cause, and the
        # findings that triggered it were already persisted.
        marked.assert_called_once_with(77)

    @pytest.mark.parametrize("edit", ["", "   "], ids=["nothing_back", "whitespace_only"])
    def test_an_empty_edit_leaves_the_first_draft_standing(self, edit):
        out, _, _ = _review([_OVER], repaired=edit)
        assert out == _DRAFT


class TestOnlyTheRepairPathRaisesTheFlag:
    def test_the_repair_path_marks_the_post(self):
        _, _, marked = _review([_OVER, _CLEAR])
        marked.assert_called_with(77)

    def test_the_generation_time_gate_pass_never_marks(self):
        """Pre-existing caller #1: `_gate_findings_for_post`'s persist at generation time."""
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.mark_post_gate_demoted") as marked:
            rcp._persist_gate_findings(1, 77, [{"gate": "ai_slop", "demoted": True}])
        marked.assert_not_called()

    def test_a_persist_that_fails_never_costs_the_post(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.update_db_post_gate_reason", side_effect=RuntimeError("no db")), \
             patch(f"{_RCP}.mark_post_gate_demoted") as marked:
            rcp._persist_gate_findings(1, 77, [], mark_repaired=True)
        # The findings write failed; the flag is a separate write and still has to land, or the
        # post auto-schedules as if it had never been repaired.
        marked.assert_called_once_with(77)

    def test_an_unwritable_flag_costs_the_hold_not_the_post(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.update_db_post_gate_reason"), \
             patch(f"{_RCP}.mark_post_gate_demoted", side_effect=RuntimeError("no db")):
            rcp._persist_gate_findings(1, 77, [], mark_repaired=True)  # must not raise

    def test_a_preview_with_no_row_writes_nothing(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.update_db_post_gate_reason") as write, \
             patch(f"{_RCP}.mark_post_gate_demoted") as marked:
            rcp._persist_gate_findings(1, None, [], mark_repaired=True)
        write.assert_not_called()
        marked.assert_not_called()


class TestMayAutoApprove:
    """`_may_auto_approve` is the ONE approve decision both call sites read."""

    def _decide(self, *, auto_schedule=True, findings=None, repaired=False, hold=True,
                prefs_raise=False):
        """`prefs_raise` is the unreadable-prefs answer `_engagement_prefs_or_empty` hands back."""
        from cqc_lem.app import run_content_plan as rcp
        prefs = (patch(f"{_RCP}._engagement_prefs_or_empty", return_value={})
                 if prefs_raise else
                 patch(f"{_RCP}._engagement_prefs_or_empty",
                       return_value={"hold_repaired_posts_for_review": hold}))
        with prefs, patch(f"{_RCP}.get_post_ever_gate_demoted", return_value=repaired):
            return rcp._may_auto_approve(1, 77, auto_schedule, findings or [])

    def test_a_clean_never_repaired_post_auto_approves(self):
        assert self._decide() is True

    def test_a_repaired_post_is_held_by_default(self):
        assert self._decide(repaired=True) is False

    def test_the_toggle_off_restores_the_old_behaviour(self):
        assert self._decide(repaired=True, hold=False) is True

    def test_auto_scheduling_off_still_wins(self):
        assert self._decide(auto_schedule=False) is False
        assert self._decide(auto_schedule=False, repaired=True, hold=False) is False

    def test_a_demoting_finding_still_holds(self):
        assert self._decide(findings=[{"gate": "ai_slop", "demoted": True}]) is False

    def test_an_advisory_finding_does_not_hold(self):
        assert self._decide(findings=[{"gate": "focus_alignment", "demoted": False}]) is True

    def test_an_unreadable_prefs_row_fails_open(self):
        # A DB hiccup must cost the extra review, never the publish — the gates' own posture.
        assert self._decide(repaired=True, prefs_raise=True) is True

    def test_a_preview_with_no_row_is_never_held(self):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_post_ever_gate_demoted") as read:
            assert rcp._may_auto_approve(1, None, True, []) is True
        read.assert_not_called()


class TestRescorePromoteOnPass:
    """A re-score may promote a PENDING post — but not one whose text was repaired."""

    def _rescore(self, *, repaired, hold=True):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}.get_post_content", return_value="An edit with nothing in common."), \
             patch("cqc_lem.utilities.db.get_post_user_id", return_value=1), \
             patch("cqc_lem.utilities.db.get_post_type", return_value="text"), \
             patch("cqc_lem.utilities.db.get_post_video_url", return_value=None), \
             patch("cqc_lem.utilities.db.get_post_status", return_value="pending"), \
             patch("cqc_lem.utilities.db.get_post_archetype", return_value="personal_lesson"), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}._engagement_prefs_or_empty",
                   return_value={"hold_repaired_posts_for_review": hold}), \
             patch(f"{_RCP}.load_profile_for_user", return_value=None), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}._score_and_persist_authenticity"), \
             patch(f"{_RCP}.get_post_authenticity_score", return_value=90), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RCP}._persist_gate_findings") as persist, \
             patch(f"{_RCP}.get_post_ever_gate_demoted", return_value=repaired), \
             patch(f"{_RCP}.get_user_preferences", return_value={"auto_schedule_posts": True}), \
             patch(f"{_RCP}.update_db_post_status") as status:
            result = rcp.rescore_post(7)
        return result, status, persist

    def test_a_never_repaired_post_is_promoted_exactly_as_before(self):
        result, status, _ = self._rescore(repaired=False)
        assert result["status"] == "approved"
        status.assert_called_once()

    def test_a_repaired_post_passes_but_waits_for_its_author(self):
        result, status, _ = self._rescore(repaired=True)
        assert result["passed"] is True
        assert result["status"] == "pending"
        assert "repaired" in result["detail"]
        status.assert_not_called()

    def test_the_toggle_off_promotes_the_repaired_post(self):
        result, status, _ = self._rescore(repaired=True, hold=False)
        assert result["status"] == "approved"
        status.assert_called_once()

    def test_the_rescore_persist_is_still_a_pre_existing_caller(self):
        """Pre-existing caller #2: re-scoring grades the text, it never repairs it."""
        _, _, persist = self._rescore(repaired=False)
        persist.assert_called_once()
        assert "mark_repaired" not in persist.call_args.kwargs
        assert len(persist.call_args.args) == 3


class TestGenerationTimeStatus:
    """The status a freshly-generated post lands on — the second call site of `_may_auto_approve`."""

    def _generate(self, *, repaired, hold=True, auto_schedule=True, findings=None):
        from cqc_lem.app.run_content_plan import _create_content_for_planned_post
        post = {"id": 9, "user_id": 7, "post_type": "text", "buyer_stage": "awareness",
                "content_mix": "value", "scheduled_time": None}
        with patch(f"{_RCP}.create_content", return_value=("a finished post", None)), \
             patch(f"{_RCP}._post_used_avatar_media", return_value=False), \
             patch(f"{_RCP}._score_and_persist_dwell"), \
             patch(f"{_RCP}._gate_findings_for_post", return_value=list(findings or [])), \
             patch(f"{_RCP}._persist_gate_findings"), \
             patch(f"{_RCP}.get_post_ever_gate_demoted", return_value=repaired), \
             patch(f"{_RCP}._engagement_prefs_or_empty",
                   return_value={"hold_repaired_posts_for_review": hold}), \
             patch(f"{_RCP}.update_db_post_content"), \
             patch(f"{_RCP}.record_post_generated"), \
             patch(f"{_RCP}.update_db_post_status") as status:
            assert _create_content_for_planned_post(
                post, {"auto_schedule_posts": auto_schedule}) is True
        return status.call_args.args[1].value

    def test_a_clean_never_repaired_post_is_approved(self):
        assert self._generate(repaired=False) == "approved"

    def test_a_repaired_post_that_now_passes_every_gate_is_still_held(self):
        assert self._generate(repaired=True) == "pending"

    def test_the_toggle_off_ships_the_repaired_post_exactly_as_before(self):
        assert self._generate(repaired=True, hold=False) == "approved"

    def test_a_demoting_gate_finding_still_holds_an_unrepaired_post(self):
        assert self._generate(
            repaired=False, findings=[{"gate": "ai_slop", "demoted": True,
                                       "explanation": "slop"}]) == "pending"

    def test_auto_scheduling_off_is_unchanged(self):
        assert self._generate(repaired=False, auto_schedule=False) == "pending"


class TestTheDbFlag:
    def test_the_mark_is_write_once_and_one_way(self, fake_cursor):
        from cqc_lem.platform.db.repositories.posts import mark_post_gate_demoted
        conn, cursor = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert mark_post_gate_demoted(9) is True
        sql = cursor.execute.call_args[0][0]
        assert "ever_gate_demoted = 1" in sql and "UPDATE posts" in sql

    def test_marking_a_post_that_is_already_marked_is_not_a_failure(self, fake_cursor):
        # MySQL reports rows CHANGED, so the second call on one post updates zero rows.
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.platform.db.repositories.posts import mark_post_gate_demoted
            assert mark_post_gate_demoted(9) is True

    def test_the_reader_answers_false_for_an_unflagged_post(self, fake_cursor):
        from cqc_lem.platform.db.repositories.posts import get_post_ever_gate_demoted
        conn, _ = fake_cursor(fetch_one=(0,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_post_ever_gate_demoted(9) is False

    def test_the_reader_answers_true_for_a_flagged_post(self, fake_cursor):
        from cqc_lem.platform.db.repositories.posts import get_post_ever_gate_demoted
        conn, _ = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_post_ever_gate_demoted(9) is True

    def test_an_unreadable_flag_reads_as_never_repaired(self, fake_cursor):
        import mysql.connector

        from cqc_lem.platform.db.repositories.posts import get_post_ever_gate_demoted
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("no db"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_post_ever_gate_demoted(9) is False

    def test_an_unwritable_flag_reports_failure_rather_than_raising(self, fake_cursor):
        import mysql.connector

        from cqc_lem.platform.db.repositories.posts import mark_post_gate_demoted
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("no db"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert mark_post_gate_demoted(9) is False


class TestTheRepairBriefReachesTheEditorsPrompt:
    """The findings are the brief — so they have to survive into the prompt that is sent."""

    def _resp(self, text="edited"):
        r = MagicMock()
        r.choices = [MagicMock()]
        r.choices[0].message.content = text
        return r

    def _sent(self, **kwargs):
        from cqc_lem.utilities.ai import ai_helper
        with patch.object(ai_helper, "_call_llm", return_value=self._resp()) as call:
            ai_helper.get_ai_linked_post_refinement("draft", **kwargs)
        return str(call.call_args.kwargs["messages"])

    def test_the_findings_are_spelled_out_for_the_editor(self):
        from cqc_lem.utilities.quality_gates import slop_finding
        sent = self._sent(repair_findings=[slop_finding(["contrastive 'not X, it's Y'"])])
        assert "REQUIRED REPAIRS" in sent
        assert "contrastive" in sent
        assert "NEVER invent a fact" in sent

    def test_the_ordinary_refinement_prompt_is_untouched(self):
        assert "REQUIRED REPAIRS" not in self._sent()
        assert "REQUIRED REPAIRS" not in self._sent(repair_findings=[])
        assert "STORY BANK" not in self._sent(repair_source_material=None)

    def test_the_authors_material_is_read_before_the_fixes(self):
        """Otherwise "never invent a specific" is an instruction with nowhere to go.

        The material has to be ABOVE the repairs so the ban reads as a pointer at facts the editor
        can see, which is the same order the writer's own prompt puts them in.
        """
        from cqc_lem.utilities.ai.story_bank import story_directive
        from cqc_lem.utilities.quality_gates import proof_finding
        material = story_directive({"kind": "anecdote", "body": "Cut onboarding 12 days to 3."})
        sent = self._sent(repair_source_material=material, repair_findings=[proof_finding()])
        assert "Cut onboarding 12 days to 3." in sent
        assert sent.index("STORY BANK") < sent.index("REQUIRED REPAIRS")

    def test_the_cta_keyword_rule_is_read_before_the_fixes(self):
        from cqc_lem.utilities.quality_gates import proof_finding
        sent = self._sent(preserve_cta_keyword="AUDIT", repair_findings=[proof_finding()])
        assert sent.index("PRESERVE") < sent.index("REQUIRED REPAIRS")


class TestTheNewFindingShapes:
    def test_the_proof_finding_points_at_the_authors_own_background(self):
        from cqc_lem.utilities.quality_gates import GATE_PERSONAL_PROOF, proof_finding
        finding = proof_finding("Ran a 40-person consultancy for 12 years.")
        assert finding["gate"] == GATE_PERSONAL_PROOF
        assert finding["label"] == "Missing personal proof"
        assert "40-person consultancy" in " ".join(finding["details"])
        assert "invent" in finding["remediation"].lower()

    def test_the_proof_finding_survives_an_empty_synthesis(self):
        from cqc_lem.utilities.quality_gates import proof_finding
        assert proof_finding()["details"] == []

    def test_the_fabrication_finding_names_every_invented_specific(self):
        from cqc_lem.utilities.quality_gates import GATE_FABRICATION, fabrication_finding
        finding = fabrication_finding(["47%", "$1.2M", "  "])
        assert finding["gate"] == GATE_FABRICATION
        assert finding["score"] == 2.0
        assert "47%" in " ".join(finding["details"])
        assert "$1.2M" in " ".join(finding["details"])

    def test_both_new_findings_carry_the_full_contract(self):
        from cqc_lem.utilities.quality_gates import GATE_LABELS, fabrication_finding, proof_finding
        for finding in (proof_finding("voice"), fabrication_finding(["47%"])):
            assert finding["label"] == GATE_LABELS[finding["gate"]]
            assert finding["explanation"] and finding["remediation"]


class TestThePreference:
    def test_it_defaults_on(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_COLS, _ENGAGEMENT_DEFAULTS
        assert _ENGAGEMENT_DEFAULTS["hold_repaired_posts_for_review"] is True
        assert "hold_repaired_posts_for_review" in _ENGAGEMENT_COLS

    def test_it_is_stored_as_a_boolean_column(self, fake_cursor):
        from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
        conn, cursor = fake_cursor(fetch_one=None, rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.utilities.db.max_catchup_touches_allowed", return_value=10):
            update_engagement_preferences(1, {"hold_repaired_posts_for_review": False})
        saved = dict(zip(_ENGAGEMENT_COLS, cursor.execute.call_args[0][1][1:]))
        assert saved["hold_repaired_posts_for_review"] == 0

    def test_the_api_model_carries_it_defaulted_on(self):
        from cqc_lem.api.routers.user import EngagementPreferencesRequest
        field = EngagementPreferencesRequest.model_fields["hold_repaired_posts_for_review"]
        assert field.default is True
