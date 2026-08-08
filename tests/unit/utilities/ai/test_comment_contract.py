"""Comment quality contract v2 + comment-side similarity gate (issue #617 / G2).

Production comments (logs 89-96) opened with "I totally agree that...", ran one
[validate]+[generic insight]+[question] template, and near-duplicated each other across different
posts the same day. These tests pin the deterministic contract that now rejects exactly that, the
embedding dedup that catches the reworded repeat, and the generate-side behaviour the acceptance
criteria demand: bounded regeneration, then SKIP the post — never ship a failing comment.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import content_framework as fw

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_CLIENT = "cqc_lem.utilities.ai.client.client"

# A draft that satisfies every rule against _POST: grounded in its specifics, 3 sentences, a lived
# first-hand value-add plus a real number, and no validation-filler opener.
_POST = ("We cut our warehouse pick times by moving the fast movers to the front aisle. "
         "Nobody talks about how much layout beats software here.")
_GOOD = ("The front-aisle move is the part most layout posts skip. We tried it across two "
         "warehouses last year and pick times fell 18% before we touched the software. "
         "How long did the fast movers stay stable for you?")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("COMMENT_FILLER_OPENERS", "COMMENT_SIMILARITY_MAX",
                 "COMMENT_LEXICAL_SIMILARITY_MAX", "COMMENT_GATE_MAX_ATTEMPTS"):
        monkeypatch.delenv(name, raising=False)


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


def _embedding_response(vectors):
    response = MagicMock()
    response.data = [MagicMock(index=i, embedding=v) for i, v in enumerate(vectors)]
    return response


class TestFillerOpeners:
    @pytest.mark.parametrize("opener", [
        "I totally agree that layout beats software.",
        "You raise an excellent point about the front aisle.",
        "Great post on warehouse layout!",
        "Spot on — the front aisle is where it starts.",
        "So insightful, especially the layout part.",
        "Love this take on pick times.",
        "Well said about the fast movers.",
    ])
    def test_every_banned_opener_is_caught(self, opener):
        draft = opener + " We saw the same thing after re-mapping our aisles last year."
        assert fw.filler_openers_found(draft)
        report = fw.comment_contract_report(draft, _POST)
        assert report["passes"] is False
        assert any("validation filler" in f for f in report["failures"])

    def test_clean_comment_has_no_filler(self):
        assert fw.filler_openers_found(_GOOD) == []

    def test_filler_deep_in_the_body_is_not_an_opener(self):
        draft = ("The front-aisle move is the part most layout posts skip. We ran it across two "
                 "warehouses and pick times fell 18%. Great post, by the way — what did month "
                 "three look like?")
        assert fw.filler_openers_found(draft) == []

    def test_word_boundary_guard_keeps_post_mortem_out(self):
        draft = ("Your great post-mortem on the aisle change is the part I keep re-reading. We "
                 "ran the same layout swap last year.")
        assert fw.filler_openers_found(draft) == []

    def test_smart_apostrophes_still_match(self):
        draft = "Couldn’t agree more about the aisles. We saw the same thing last year."
        assert "couldn't agree more" in fw.filler_openers_found(draft)

    def test_env_extends_the_ban_list_without_shrinking_it(self, monkeypatch):
        monkeypatch.setenv("COMMENT_FILLER_OPENERS", "this hits home, so much this")
        openers = fw.comment_filler_openers()
        assert "this hits home" in openers and "so much this" in openers
        assert "great post" in openers          # built-ins can never be dropped
        draft = "This hits home. We ran the same layout swap across two warehouses last year."
        assert fw.filler_openers_found(draft) == ["this hits home"]


class TestContractChecks:
    def test_good_comment_passes_every_check(self):
        report = fw.comment_contract_report(_GOOD, _POST)
        assert report["passes"] is True
        assert report["failures"] == []
        assert all(report["checks"].values())

    def test_one_liner_is_rejected(self):
        report = fw.comment_contract_report(
            "We tried the front-aisle layout swap and pick times fell 18%.", _POST)
        assert report["checks"]["min_sentences"] is False
        assert any("sentences" in f for f in report["failures"])

    def test_comment_that_could_sit_under_any_post_is_rejected(self):
        drifting = ("Culture eats strategy every single time. We learned that the hard way when "
                    "our team doubled.")
        report = fw.comment_contract_report(drifting, _POST)
        assert report["checks"]["references_post"] is False
        assert any("specific claim" in f for f in report["failures"])

    def test_ungradeable_post_does_not_burn_regenerations(self):
        # No extractable post content -> grounding cannot be checked, so it must not fail closed.
        assert fw.references_post("Two sentences here. And a second one.", "") is True

    def test_value_adds_are_detected_individually(self):
        assert "experience" in fw.comment_value_adds(
            "We ran that layout swap last year. It held.")
        assert "data_point" in fw.comment_value_adds(
            "Pick times fell 18% for us. That held for a quarter.")
        assert "disagreement" in fw.comment_value_adds(
            "The aisle framing is where I'd push back. Layout only helps once volume is stable.")
        assert "question" in fw.comment_value_adds(
            "The aisle framing lands. How long did the fast movers stay stable for you?")

    def test_reflex_closer_is_not_a_genuine_question(self):
        assert fw.has_genuine_question("Layout beats software here. Thoughts?") is False
        assert fw.has_genuine_question("Layout beats software. Agree?") is False

    def test_validate_plus_platitude_plus_reflex_question_fails(self):
        """The exact production template this issue exists to kill."""
        template = ("I totally agree that layout matters. Warehouse efficiency really is about "
                    "the fundamentals. Thoughts?")
        report = fw.comment_contract_report(template, _POST)
        assert report["passes"] is False
        assert report["checks"]["no_filler_opener"] is False
        assert report["checks"]["value_add"] is False

    def test_empty_comment_fails(self):
        report = fw.comment_contract_report("", _POST)
        assert report["passes"] is False and "empty comment" in report["failures"]

    def test_meets_comment_contract_matches_the_report(self):
        assert fw.meets_comment_contract(_GOOD, _POST) is True
        assert fw.meets_comment_contract("Great post!", _POST) is False


class TestCommentSimilarityGate:
    def test_no_history_means_no_embedding_call(self):
        with patch(_CLIENT) as client:
            report = fw.comment_similarity_report(_GOOD, [])
        client.embeddings.create.assert_not_called()
        assert report["too_similar"] is False and report["measure"] == "none"

    def test_near_duplicate_rejected_by_embedding_cosine(self):
        with patch(_CLIENT) as client:
            client.embeddings.create.return_value = _embedding_response(
                [[1.0, 0.0, 0.0], [0.99, 0.1, 0.0], [0.0, 1.0, 0.0]])
            report = fw.comment_similarity_report(_GOOD, ["an earlier comment", "another one"])
        assert report["measure"] == "embedding"
        assert report["too_similar"] is True
        assert report["match"] == "an earlier comment"
        client.embeddings.create.assert_called_once()
        assert client.embeddings.create.call_args.kwargs["model"] == fw.COMMENT_EMBEDDING_MODEL

    def test_distinct_comment_passes(self):
        with patch(_CLIENT) as client:
            client.embeddings.create.return_value = _embedding_response(
                [[1.0, 0.0], [0.0, 1.0]])
            report = fw.comment_similarity_report(_GOOD, ["a totally different comment"])
        assert report["too_similar"] is False and report["score"] == 0.0

    def test_falls_back_to_token_overlap_when_embeddings_fail(self):
        with patch(_CLIENT) as client:
            client.embeddings.create.side_effect = RuntimeError("embedding endpoint down")
            report = fw.comment_similarity_report(_GOOD, [_GOOD])
        # A dead embedding endpoint must never mean "nothing is similar".
        assert report["measure"] == "lexical"
        assert report["too_similar"] is True
        assert report["threshold"] == fw.COMMENT_LEXICAL_SIMILARITY_MAX_DEFAULT

    def test_history_is_capped_at_the_window(self):
        with patch(_CLIENT) as client:
            client.embeddings.create.return_value = _embedding_response(
                [[1.0, 0.0]] * (fw.COMMENT_HISTORY_LIMIT + 1))
            fw.comment_similarity_report(_GOOD, ["c%d" % i for i in range(200)])
        sent = client.embeddings.create.call_args.kwargs["input"]
        assert len(sent) == fw.COMMENT_HISTORY_LIMIT + 1   # draft + capped history

    def test_thresholds_are_read_live(self, monkeypatch):
        assert fw.comment_similarity_max() == fw.COMMENT_SIMILARITY_MAX_DEFAULT == 0.82
        monkeypatch.setenv("COMMENT_SIMILARITY_MAX", "0.7")
        assert fw.comment_similarity_max() == 0.7
        monkeypatch.setenv("COMMENT_SIMILARITY_MAX", "not-a-number")
        assert fw.comment_similarity_max() == fw.COMMENT_SIMILARITY_MAX_DEFAULT
        monkeypatch.setenv("COMMENT_LEXICAL_SIMILARITY_MAX", "0.4")
        assert fw.comment_lexical_similarity_max() == 0.4
        assert fw.COMMENT_LEXICAL_SIMILARITY_MAX_DEFAULT == fw.POST_SIMILARITY_MAX_DEFAULT

    def test_bad_lexical_threshold_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("COMMENT_LEXICAL_SIMILARITY_MAX", "not-a-number")
        assert fw.comment_lexical_similarity_max() == fw.COMMENT_LEXICAL_SIMILARITY_MAX_DEFAULT

    def test_embed_comments_rejects_an_empty_or_blank_batch(self):
        with patch(_CLIENT) as client:
            assert fw.embed_comments([]) is None
            assert fw.embed_comments(["a comment", ""]) is None
        client.embeddings.create.assert_not_called()

    def test_malformed_embedding_response_degrades_to_lexical(self):
        with patch(_CLIENT) as client:
            # Short row set / unusable vector — must not be trusted as "nothing is similar".
            client.embeddings.create.return_value = _embedding_response([[1.0, 0.0]])
            assert fw.embed_comments(["draft", "history"]) is None
            client.embeddings.create.return_value = _embedding_response([["x"], [1.0]])
            assert fw.embed_comments(["draft", "history"]) is None

    def test_max_attempts_is_bounded_and_live(self, monkeypatch):
        assert fw.comment_gate_max_attempts() == fw.COMMENT_GATE_MAX_ATTEMPTS_DEFAULT
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "1")
        assert fw.comment_gate_max_attempts() == 1
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "99")
        assert fw.comment_gate_max_attempts() == 5
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "junk")
        assert fw.comment_gate_max_attempts() == fw.COMMENT_GATE_MAX_ATTEMPTS_DEFAULT


class TestDirectives:
    def test_contract_directive_names_the_rules_and_the_ban_list(self):
        text = fw.comment_contract_directive()
        assert "REFERENCE A SPECIFIC CLAIM" in text
        assert "'great post'" in text
        assert "WRITE FOR THE POST'S READERS" in text     # the Tip #8 framing
        assert str(fw.COMMENT_MIN_SENTENCES) in text

    def test_retry_directive_carries_the_failures_and_the_offender(self):
        text = fw.comment_retry_directive(["opens with validation filler (great post)"],
                                          offending_comment="an earlier comment")
        assert "REJECTED" in text
        assert "opens with validation filler" in text
        assert "an earlier comment" in text

    def test_comment_menu_covers_every_value_add(self):
        # Each contract value-add has a rotating archetype behind it, so the gate is reachable by
        # generation rather than a trap.
        assert {"storyteller", "questioner", "respectful_contrarian", "evidence_add"} \
            <= set(fw.COMMENT_FORMATS)
        assert fw.blueprint_directive("comment", {"format": "evidence_add"})


class TestGenerationGate:
    """generate_ai_response is where 'never ship a failing comment' is actually enforced."""

    def test_passing_draft_ships_on_the_first_attempt(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as m:
            out = ai_helper.generate_ai_response(_POST, _profile())
        assert out == _GOOD
        assert m.call_count == 1

    def test_contract_is_injected_into_the_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as m:
            ai_helper.generate_ai_response(_POST, _profile())
        system = m.call_args.kwargs["messages"][0]["content"]
        assert "COMMENT QUALITY CONTRACT" in system
        assert "WRITE FOR THE POST'S READERS" in system

    def test_failing_draft_regenerates_with_the_fix_list_then_passes(self):
        from cqc_lem.utilities.ai import ai_helper
        drafts = [_resp("I totally agree that layout beats software. Thoughts?"), _resp(_GOOD)]
        with patch(f"{_AI}._call_llm", side_effect=drafts) as m:
            out = ai_helper.generate_ai_response(_POST, _profile())
        assert out == _GOOD
        assert m.call_count == 2
        retry_system = m.call_args_list[1].kwargs["messages"][0]["content"]
        assert "YOUR PREVIOUS DRAFT WAS REJECTED" in retry_system
        assert "validation filler" in retry_system

    def test_post_is_skipped_when_no_draft_ever_clears_the_gate(self):
        from cqc_lem.utilities.ai import ai_helper
        bad = _resp("Great post! Thoughts?")
        with patch(f"{_AI}._call_llm", return_value=bad) as m, \
             patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_ai_response(_POST, _profile())
        assert out is None                                   # never ships a failing comment
        assert m.call_count == fw.COMMENT_GATE_MAX_ATTEMPTS_DEFAULT
        assert "skipping this post" in warn.call_args.args[0]

    def test_attempt_budget_is_configurable(self, monkeypatch):
        monkeypatch.setenv("COMMENT_GATE_MAX_ATTEMPTS", "1")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Great post!")) as m, \
             patch(f"{_AI}.log_warning"):
            assert ai_helper.generate_ai_response(_POST, _profile()) is None
        assert m.call_count == 1

    def test_near_duplicate_of_a_recent_comment_is_rejected(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as m, \
             patch(f"{_AI}.log_warning"), \
             patch(_CLIENT) as client:
            client.embeddings.create.side_effect = RuntimeError("down")  # lexical fallback path
            out = ai_helper.generate_ai_response(_POST, _profile(), recent_comments=[_GOOD])
        assert out is None
        assert m.call_count == fw.COMMENT_GATE_MAX_ATTEMPTS_DEFAULT

    def test_reply_to_a_specific_comment_is_not_gated(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Thanks — glad it helped.")) as m:
            out = ai_helper.generate_ai_response(_POST, _profile(), post_comment="their comment")
        # Replies have their own acknowledge-and-answer contract; one call, no gate, no rewrite.
        assert out == "Thanks — glad it helped." and m.call_count == 1
        assert "COMMENT QUALITY CONTRACT" not in m.call_args.kwargs["messages"][0]["content"]


class TestEngageCardWiring:
    """The run-level half: history in, landed comments back out, claim released on a skip."""

    def _engage(self, comment_text, recent):
        from cqc_lem.app import run_automation as ra
        with patch("cqc_lem.app.run_automation.claim_post_for_comment", return_value=True), \
             patch("cqc_lem.app.run_automation.generate_ai_response",
                   return_value=comment_text) as gen, \
             patch("cqc_lem.app.run_automation.release_post_claim") as release, \
             patch("cqc_lem.app.run_automation.post_comment_inline", return_value=True), \
             patch("cqc_lem.app.run_automation.react_to_post_inline", return_value=True), \
             patch("cqc_lem.app.run_automation.mark_post_commented"), \
             patch("cqc_lem.app.run_automation.mark_post_reacted"), \
             patch("cqc_lem.app.run_automation.insert_new_log"), \
             patch("cqc_lem.app.run_automation._author_is_me", return_value=False), \
             patch("cqc_lem.app.run_automation.pace_read", return_value=0.0), \
             patch("cqc_lem.app.run_automation.time.sleep"):
            ok = ra._engage_card(MagicMock(), MagicMock(), MagicMock(), 1, MagicMock(),
                                 "feedurn://x", _POST, "Jane", {}, "synthesis", [], recent)
        return ok, gen, release

    def test_history_is_passed_in_and_the_landed_comment_is_appended(self):
        recent = ["an older comment"]
        ok, gen, _ = self._engage(_GOOD, recent)
        assert ok is True
        assert gen.call_args.kwargs["recent_comments"] is recent
        assert recent[0] == _GOOD          # in-run dedup: the next post sees this one

    def test_gate_skip_releases_the_claim_and_leaves_history_alone(self):
        recent = ["an older comment"]
        ok, _, release = self._engage(None, recent)
        assert ok is False
        release.assert_called_once_with(1, "feedurn://x")
        assert recent == ["an older comment"]


class TestPermalinkCommentPathSkips:
    """The single-post path (generate_and_post_comment) must skip, not post an empty comment."""

    def test_gate_skip_never_queues_a_comment(self):
        from cqc_lem.app import run_automation as ra
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
        with patch("cqc_lem.app.run_automation.get_user_id", return_value=1), \
             patch("cqc_lem.app.run_automation.check_commented", return_value=False), \
             patch("cqc_lem.app.run_automation.get_element_wait_retry", return_value=MagicMock()), \
             patch("cqc_lem.app.run_automation.getText", return_value=_POST), \
             patch("cqc_lem.app.run_automation.get_engagement_preferences", return_value={}), \
             patch("cqc_lem.app.run_automation.get_recent_comment_texts",
                   return_value=["an older comment"]) as history, \
             patch("cqc_lem.app.run_automation.pace_read", return_value=0.0), \
             patch("cqc_lem.app.run_automation.time.sleep"), \
             patch("cqc_lem.app.run_automation.generate_ai_response", return_value=None) as gen, \
             patch.object(ra.comment_on_post, "apply_async") as queued:
            ok = ra.generate_and_post_comment(driver, MagicMock(), driver.current_url, MagicMock())
        assert ok is False
        queued.assert_not_called()
        history.assert_called_once_with(1)
        assert gen.call_args.kwargs["recent_comments"] == ["an older comment"]


class TestRecentCommentHistoryQuery:
    def test_reads_successful_comment_bodies_newest_first(self):
        from cqc_lem.utilities import db
        from cqc_lem.utilities.db import LogActionType, LogResultType, get_recent_comment_texts
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.fetchall.return_value = [("newest comment",), ("older comment",)]
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            out = get_recent_comment_texts(7, limit=25)
        assert out == ["newest comment", "older comment"]
        sql, params = cursor.execute.call_args.args
        assert "FROM logs" in sql and "ORDER BY id DESC" in sql
        assert params == (7, LogActionType.COMMENT.value, LogResultType.SUCCESS.value, 25)


class TestPromptExperiment:
    """The pilot LLM prompt experiment (issue #652): the contract's closing ask is the variable, and
    the six graded rules are deliberately NOT — an arm that loosened one would change what "passes
    the gate" means and the two arms would stop being comparable.
    """

    _RULE = "END ON A QUESTION ONLY THIS AUTHOR COULD ANSWER"

    def test_treatment_adds_the_author_question_rule(self):
        text = fw.comment_contract_directive(fw.COMMENT_CONTRACT_AUTHOR_QUESTION_VARIANT)
        assert self._RULE in text
        # The graded rules are untouched, so both arms face the same deterministic gate.
        for rule in ("REFERENCE A SPECIFIC CLAIM", "WRITE FOR THE POST'S READERS",
                     "NEVER open with validation filler"):
            assert rule in text and rule in fw.comment_contract_directive()

    @pytest.mark.parametrize("variant", [None, "", "control", "some-unknown-arm"])
    def test_every_other_arm_is_the_control_contract(self, variant):
        assert fw.comment_contract_directive(variant) == fw.comment_contract_directive()

    def test_the_users_arm_reaches_the_feed_comment_prompt(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as m, \
             patch("cqc_lem.utilities.experiments.resolve_variant",
                   return_value=fw.COMMENT_CONTRACT_AUTHOR_QUESTION_VARIANT) as resolve:
            ai_helper.generate_ai_response(_POST, _profile(), user_id=7)
        assert self._RULE in m.call_args.kwargs["messages"][0]["content"]
        assert resolve.call_args.args[1] == 7

    def test_a_broken_experiment_plane_still_writes_the_control_contract(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(_GOOD)) as m, \
             patch("cqc_lem.utilities.experiments.resolve_variant",
                   side_effect=RuntimeError("posthog down")), \
             patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_ai_response(_POST, _profile(), user_id=7)
        assert out == _GOOD
        assert "COMMENT QUALITY CONTRACT" in m.call_args.kwargs["messages"][0]["content"]
        assert self._RULE not in m.call_args.kwargs["messages"][0]["content"]
        assert warn.called

    def test_replying_to_a_comment_is_not_part_of_the_experiment(self):
        """A reply has its own acknowledge-and-answer contract, and the #628 sweep never measures it,
        so enrolling it would only add exposures the metric can't cover.
        """
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp("Sure — here is the number.")), \
             patch("cqc_lem.utilities.experiments.resolve_variant") as resolve:
            ai_helper.generate_ai_response(_POST, _profile(), post_comment="what number?",
                                           user_id=7)
        resolve.assert_not_called()
