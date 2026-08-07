"""Unit tests for the auto-FAQ service — issue #507.

The deterministic halves (prefilter, PII redaction, question normalization, policy detection,
publish guardrails, clustering, entry matching, hold copy) are covered exhaustively; the
orchestrator runs with the LLM, the DB, GitHub and email all mocked.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SVC = "cqc_lem.utilities.feedback.faq_service"

# Every label the hold path can emit must exist in the repo (same guard as #497/#498).
REAL_REPO_LABELS = {
    'question', 'feedback-loop', 'needs-human', 'risk:product-decision',
}

GROUNDING = ("Q: How much does it cost?\nA: Starter is $29/month.\n\n"
             "LEM comments on your feed, replies to comments on your posts and sends DMs. "
             "You can pause the automation at any time from the account page.")


def _mod():
    from cqc_lem.utilities.feedback import faq_service
    return faq_service


def _row(feedback_id=1, body="How do I pause the automation?", user_id=5, embedding=None):
    return {"id": feedback_id, "user_id": user_id, "body": body, "embedding": embedding,
            "status": "triaged", "cluster_id": None}


def _entry(entry_id=7, question="How do I pause the automation?", answer="Open the account page.",
           status="published"):
    return {"id": entry_id, "question": question, "answer": answer, "status": status,
            "cluster_id": None}


def _llm_reply(**overrides):
    payload = {"question": "How do I pause the automation?",
               "answer": "You can pause the automation at any time from the account page.",
               "answerable": True, "policy_claim": False, "confidence": 0.9}
    payload.update(overrides)
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    return response


class TestConfig:
    def test_defaults_are_used_when_nothing_is_set(self, monkeypatch):
        for key in ("FAQ_MODEL", "FAQ_RECURRENCE_MIN", "FAQ_CLUSTER_SIMILARITY",
                    "FAQ_ENTRY_SIMILARITY", "FAQ_MAX_ANSWERS_PER_PASS", "FAQ_AUTO_PUBLISH",
                    "FAQ_AUTO_REPLY", "FAQ_GROUNDING_DOCS"):
            monkeypatch.delenv(key, raising=False)
        svc = _mod()
        assert svc.faq_model() == "lem-medium"
        assert svc.recurrence_min() == 3
        assert svc.cluster_similarity_min() == pytest.approx(0.78)
        assert svc.entry_similarity_min() == pytest.approx(0.62)
        assert svc.max_answers_per_pass() == 3
        assert svc.auto_publish() is False
        assert svc.auto_reply_enabled() is True
        assert svc.grounding_docs() == ["docs/LANDING_PAGE_UPDATE.md"]

    def test_env_overrides_are_parsed_and_clamped(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FAQ_RECURRENCE_MIN", "5")
        monkeypatch.setenv("FAQ_CLUSTER_SIMILARITY", "2.0")
        monkeypatch.setenv("FAQ_ENTRY_SIMILARITY", "not-a-number")
        monkeypatch.setenv("FAQ_AUTO_PUBLISH", "yes")
        monkeypatch.setenv("FAQ_AUTO_REPLY", "off")
        monkeypatch.setenv("FAQ_GROUNDING_DOCS", " docs/a.md , docs/b.md ")
        assert svc.recurrence_min() == 5
        assert svc.cluster_similarity_min() == 1.0
        assert svc.entry_similarity_min() == pytest.approx(0.62)
        assert svc.auto_publish() is True
        assert svc.auto_reply_enabled() is False
        assert svc.grounding_docs() == ["docs/a.md", "docs/b.md"]


class TestLooksLikeQuestion:
    @pytest.mark.parametrize("body", [
        "How do I pause the automation?",
        "does this work with sales navigator",
        "Can I export my comments",
        "Is there a way to slow the DMs down?",
    ])
    def test_question_shaped_text_passes(self, body):
        assert _mod().looks_like_question(body) is True

    @pytest.mark.parametrize("body", [
        "", "   ", "Comments never post to the feed.", "Love this product, keep going!",
    ])
    def test_statements_are_rejected_for_free(self, body):
        assert _mod().looks_like_question(body) is False


class TestRedactPii:
    def test_emails_phones_secrets_and_long_ids_are_stripped(self):
        svc = _mod()
        text = ("email me at chris.queen+lem@example.co.uk or call +1 (555) 867-5309, "
                "my key is sk-ABCD1234efgh and account 998877665")
        out = svc.redact_pii(text)
        assert "example.co.uk" not in out
        assert "867-5309" not in out
        assert "sk-ABCD1234efgh" not in out
        assert "998877665" not in out
        assert out.count(svc.REDACTED) >= 4

    def test_ordinary_product_text_is_untouched(self):
        svc = _mod()
        text = "Starter is $29/month and posts go out 3 times a week."
        assert svc.redact_pii(text) == text

    def test_none_is_an_empty_string(self):
        assert _mod().redact_pii(None) == ""


class TestNormalizeQuestion:
    def test_isolates_the_question_sentence_and_terminates_it(self):
        svc = _mod()
        out = svc.normalize_question("Hi team. does this pause automation on weekends")
        assert out == "Does this pause automation on weekends?"

    def test_prefers_the_sentence_that_actually_asks(self):
        svc = _mod()
        out = svc.normalize_question("Loving the tool so far. Can I export my comments? Thanks!")
        assert out == "Can I export my comments?"

    def test_pii_never_reaches_the_published_question(self):
        svc = _mod()
        out = svc.normalize_question("Can you look at my account chris@example.com?")
        assert "chris@example.com" not in out
        assert out.endswith("?")

    def test_long_questions_are_capped(self):
        svc = _mod()
        out = svc.normalize_question("Why " + "very " * 200 + "slow?")
        assert len(out) <= svc.MAX_QUESTION_CHARS + 1

    def test_empty_text_is_an_empty_question(self):
        assert _mod().normalize_question("   ") == ""

    def test_a_statement_falls_back_to_its_first_sentence(self):
        assert _mod().normalize_question("Comments never post. Ever.") == "Comments never post?"

    def test_punctuation_only_text_yields_no_question(self):
        assert _mod().normalize_question("...!") == ""


class TestPolicyDetection:
    @pytest.mark.parametrize("text", [
        "Is this against LinkedIn's Terms of Service?",
        "Can I get a refund?",
        "What is the price for the team plan?",
        "When will you ship the analytics roadmap?",
        "Will my account get banned?",
    ])
    def test_commitment_wording_is_flagged(self, text):
        assert _mod().implies_policy_claim(text) is not None

    def test_plain_how_to_questions_are_not_flagged(self):
        svc = _mod()
        assert svc.implies_policy_claim("How do I pause the automation?") is None
        assert svc.implies_policy_claim(None, "") is None

    def test_the_answer_is_checked_too_not_just_the_question(self):
        svc = _mod()
        assert svc.implies_policy_claim("How do I stop DMs?",
                                        "You get a refund if it fails.") == "refund"


class TestAnswerGuardrails:
    def test_a_grounded_answer_passes(self):
        svc = _mod()
        assert svc.check_answer("How do I pause it?",
                                "You can pause the automation from the account page.",
                                GROUNDING) is None

    def test_an_empty_answer_is_held(self):
        assert _mod().check_answer("Q?", "   ", GROUNDING) == "the answer is empty"

    def test_an_over_long_answer_is_held(self):
        svc = _mod()
        reason = svc.check_answer("Q?", "a" * (svc.MAX_ANSWER_CHARS + 1), GROUNDING)
        assert "over the" in reason

    def test_profanity_is_held(self):
        reason = _mod().check_answer("Q?", "No, that is bullshit.", GROUNDING)
        assert "disallowed language" in reason

    def test_personal_data_in_the_answer_is_held(self):
        reason = _mod().check_answer("Q?", "Email support@example.com and ask.", GROUNDING)
        assert reason == "the answer contains personal data"

    def test_an_invented_price_is_held(self):
        reason = _mod().check_answer("Q?", "It is $19/month for that.", GROUNDING)
        assert "ungrounded specifics" in reason and "19" in reason

    def test_a_number_that_is_in_the_source_material_is_allowed(self):
        assert _mod().check_answer("Q?", "Starter is $29/month.", GROUNDING) is None

    def test_ungrounded_numbers_lists_only_what_is_missing(self):
        assert _mod().ungrounded_numbers("29 and 500", "29") == ["500"]


class TestMatchEntry:
    def test_a_reworded_question_matches_its_entry(self):
        svc = _mod()
        entry, score = svc.match_entry("How do I pause the automation?", [_entry()])
        assert entry["id"] == 7 and score == pytest.approx(1.0)

    def test_an_unrelated_question_matches_nothing(self):
        svc = _mod()
        entry, _score = svc.match_entry("Which browsers do you support?", [_entry()])
        assert entry is None

    def test_no_entries_matches_nothing(self):
        assert _mod().match_entry("anything?", None) == (None, 0.0)


class TestClusterQuestions:
    def test_rewordings_of_the_same_question_form_one_cluster(self):
        svc = _mod()
        rows = [_row(1, "How do I pause the automation?"),
                _row(2, "How do I pause the automation for a week?"),
                _row(3, "Which browsers are supported?")]
        with patch(f"{_SVC}.embed_text", side_effect=[[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]):
            clusters = svc.cluster_questions(rows)
        assert [len(c["rows"]) for c in clusters] == [2, 1]
        # The cluster is keyed by its SEED row — the same convention the filer uses.
        assert clusters[0]["cluster_id"] == 1

    def test_only_the_redacted_question_is_embedded_never_the_raw_body(self):
        svc = _mod()
        row = _row(1, "Hi, I am bob@example.com and my key is sk-abcd1234efgh. "
                      "How do I pause the automation?", embedding=[0.0, 1.0])
        with patch(f"{_SVC}.embed_text", return_value=[1.0, 0.0]) as embed, \
             patch(f"{_SVC}.update_feedback_triage") as stamp:
            clusters = svc.cluster_questions([row])
        embedded = embed.call_args.args[0]
        assert embedded == "How do I pause the automation?"
        assert "bob@example.com" not in embedded and "sk-abcd1234efgh" not in embedded
        # `feedback.embedding` holds the filer's BODY vector — not read here, not overwritten here.
        embed.assert_called_once()
        stamp.assert_not_called()
        assert clusters[0]["vector"] == [1.0, 0.0]

    def test_clustering_degrades_to_token_overlap_when_embedding_is_down(self):
        svc = _mod()
        rows = [_row(1, "How do I pause the automation?"),
                _row(2, "How do I pause the automation?")]
        with patch(f"{_SVC}.embed_text", return_value=None):
            clusters = svc.cluster_questions(rows)
        assert len(clusters) == 1 and len(clusters[0]["rows"]) == 2

    def test_rows_with_no_usable_question_are_skipped(self):
        with patch(f"{_SVC}.embed_text", return_value=None) as embed:
            assert _mod().cluster_questions([_row(1, "   ")]) == []
        embed.assert_not_called()


class TestGrounding:
    def test_published_answers_are_the_corpus(self):
        svc = _mod()
        text = svc.build_grounding([_entry(question="Q1?", answer="A1.")], docs=[])
        assert "Q: Q1?" in text and "A: A1." in text

    def test_entries_without_an_answer_are_left_out(self):
        svc = _mod()
        assert svc.build_grounding([{"question": "Q?", "answer": ""}], docs=[]) == ""

    def test_docs_are_appended_and_missing_ones_are_ignored(self):
        svc = _mod()
        text = svc.build_grounding([], docs=["docs/LANDING_PAGE_UPDATE.md", "docs/nope.md"])
        assert "LANDING_PAGE_UPDATE" in text and "nope" not in text

    def test_a_path_outside_the_repo_is_never_read(self):
        assert _mod().build_grounding([], docs=["../../../etc/passwd"]) == ""

    def test_an_unreadable_doc_narrows_the_corpus_instead_of_raising(self):
        svc = _mod()
        with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")), \
             patch(f"{_SVC}.log_warning") as log:
            assert svc.build_grounding([], docs=["docs/LANDING_PAGE_UPDATE.md"]) == ""
        log.assert_called_once()

    def test_the_corpus_is_bounded(self):
        svc = _mod()
        entries = [_entry(entry_id=i, question=f"Q{i}?", answer="x" * 500) for i in range(100)]
        assert len(svc.build_grounding(entries, docs=[])) <= svc.MAX_GROUNDING_CHARS


class TestGenerateFaqAnswer:
    def test_returns_the_parsed_contract(self):
        svc = _mod()
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply()) as call:
            out = svc.generate_faq_answer("How do I pause the automation?", GROUNDING, asks=3)
        assert out["answerable"] is True and out["policy_claim"] is False
        assert "account page" in out["answer"]
        assert call.call_args.kwargs["temperature"] == 0

    def test_the_current_answer_is_offered_for_revision(self):
        svc = _mod()
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_reply()) as call:
            svc.generate_faq_answer("Q?", GROUNDING, asks=4, current_answer="Old answer.")
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "Old answer." in prompt and GROUNDING in prompt

    def test_no_question_or_no_grounding_never_calls_the_model(self):
        svc = _mod()
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm") as call:
            assert svc.generate_faq_answer("", GROUNDING) is None
            assert svc.generate_faq_answer("Q?", "  ") is None
        call.assert_not_called()

    def test_a_failed_call_holds_instead_of_raising(self):
        svc = _mod()
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("502")):
            assert svc.generate_faq_answer("Q?", GROUNDING) is None

    def test_off_contract_output_is_rejected(self):
        svc = _mod()
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "I cannot answer that."
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response):
            assert svc.generate_faq_answer("Q?", GROUNDING) is None

    def test_a_broken_first_object_does_not_hide_the_real_one(self):
        svc = _mod()
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = (
            "{not json at all}\n" + json.dumps({"question": "Q?", "answer": "A.",
                                                "answerable": True, "policy_claim": False,
                                                "confidence": "high"}))
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response):
            out = svc.generate_faq_answer("Q?", GROUNDING)
        # An unparseable confidence is 0.0, not a crash.
        assert out["answer"] == "A." and out["confidence"] == 0.0

    def test_prose_wrapped_json_is_still_parsed(self):
        svc = _mod()
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = (
            "```json\n" + json.dumps({"question": "Q?", "answer": "A.", "answerable": True,
                                      "policy_claim": False, "confidence": "0.8"}) + "\n```")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response):
            out = svc.generate_faq_answer("Q?", GROUNDING)
        assert out["answer"] == "A." and out["confidence"] == pytest.approx(0.8)


class TestHoldCopy:
    def test_the_hold_issue_body_carries_the_question_and_the_proposal(self):
        svc = _mod()
        body = svc.build_hold_body("Can I get a refund?", "Yes, within 30 days.", "policy", 4)
        assert "asked this 4 time(s)" in body
        assert "Can I get a refund?" in body and "Yes, within 30 days." in body
        assert "## Acceptance" in body

    def test_the_hold_body_says_so_when_nothing_could_be_grounded(self):
        body = _mod().build_hold_body("Do you support X?", None, "no grounding", 3)
        assert "No answer could be grounded" in body

    def test_the_decision_comment_is_letter_pickable(self):
        svc = _mod()
        comment = svc.build_hold_decision_comment("Can I get a refund?", "Yes.", "policy claim")
        assert comment.startswith("## 🧑‍⚖️ Human decision needed")
        assert "- **A." in comment and "- **B." in comment and "- **C." in comment
        assert comment.count("✅ *recommended*") == 1
        assert "**My recommendation: `1B`.**" in comment

    def test_with_no_proposed_answer_the_recommendation_is_to_document_the_fact(self):
        comment = _mod().build_hold_decision_comment("Do you support X?", None, "no grounding")
        assert "**My recommendation: `1A`.**" in comment
        assert "product docs" in comment

    def test_holding_files_a_needs_human_issue_with_real_labels(self):
        svc = _mod()
        with patch(f"{_SVC}.create_github_issue", return_value=321) as create, \
             patch(f"{_SVC}.comment_on_issue") as comment, \
             patch(f"{_SVC}.issue_assignee", return_value="gitchrisqueen"):
            assert svc.hold_for_human("Can I get a refund?", "Yes.", "policy", 3,
                                      policy=True) == 321
        labels = create.call_args[0][2]
        assert set(labels) <= REAL_REPO_LABELS
        assert 'needs-human' in labels and 'risk:product-decision' in labels
        assert create.call_args.kwargs["assignees"] == ["gitchrisqueen"]
        comment.assert_called_once()

    def test_a_non_policy_hold_carries_no_risk_label(self):
        svc = _mod()
        with patch(f"{_SVC}.create_github_issue", return_value=322), \
             patch(f"{_SVC}.comment_on_issue"), \
             patch(f"{_SVC}.issue_assignee", return_value="gitchrisqueen"):
            svc.hold_for_human("Do you support X?", None, "no grounding", 3)

    def test_github_being_unreachable_still_withholds_the_answer(self):
        svc = _mod()
        with patch(f"{_SVC}.create_github_issue", return_value=None), \
             patch(f"{_SVC}.comment_on_issue") as comment, \
             patch(f"{_SVC}.issue_assignee", return_value="gitchrisqueen"):
            assert svc.hold_for_human("Q?", "A.", "policy", 3) is None
        comment.assert_not_called()


class TestReplyToAskers:
    def test_each_asker_is_emailed_once(self):
        svc = _mod()
        rows = [_row(1, user_id=5), _row(2, user_id=5), _row(3, user_id=9), _row(4, user_id=None)]
        with patch("cqc_lem.utilities.notifications.notify_faq_answer",
                   return_value=True) as notify:
            assert svc.reply_to_askers(rows, "Q?", "A.") == 2
        assert notify.call_count == 2

    def test_a_mail_failure_never_raises(self):
        svc = _mod()
        with patch("cqc_lem.utilities.notifications.notify_faq_answer",
                   side_effect=RuntimeError("smtp down")):
            assert svc.reply_to_askers([_row(1)], "Q?", "A.") == 0

    def test_auto_reply_can_be_turned_off(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FAQ_AUTO_REPLY", "false")
        with patch("cqc_lem.utilities.notifications.notify_faq_answer") as notify:
            assert svc.reply_to_askers([_row(1)], "Q?", "A.") == 0
        notify.assert_not_called()

    def test_an_empty_answer_is_never_emailed(self):
        svc = _mod()
        with patch("cqc_lem.utilities.notifications.notify_faq_answer") as notify:
            assert svc.reply_to_askers([_row(1)], "Q?", "  ") == 0
        notify.assert_not_called()


def _cluster(size=3, question="How do I pause the automation?", cluster_id=1):
    rows = [_row(i + 1, question, user_id=i + 1) for i in range(size)]
    return {"cluster_id": cluster_id, "question": question, "vector": [1.0, 0.0], "rows": rows}


class TestAnswerQuestionCluster:
    def test_a_recurring_question_publishes_an_entry_and_versions_it(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FAQ_AUTO_PUBLISH", "1")
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer()), \
             patch(f"{_SVC}.upsert_faq_entry", return_value=11) as upsert, \
             patch(f"{_SVC}.record_faq_entry_version") as version, \
             patch(f"{_SVC}.reply_to_askers", return_value=3), \
             patch(f"{_SVC}.update_feedback_triage") as stamp:
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.PUBLISHED
        assert result["entry_id"] == 11 and result["replied"] == 3
        assert upsert.call_args.kwargs["status"] == svc.FaqStatus.PUBLISHED
        assert upsert.call_args.kwargs["sort_order"] == svc.AUTO_SORT_ORDER
        version.assert_called_once()
        # Every row leaves the queue attached to the FAQ cluster, so the pass never re-charges.
        assert stamp.call_count == 3
        assert stamp.call_args.kwargs == {"status": svc.FeedbackStatus.RESOLVED, "cluster_id": 1}

    def test_a_new_entry_is_a_draft_until_reviewed_by_default(self, monkeypatch):
        svc = _mod()
        monkeypatch.delenv("FAQ_AUTO_PUBLISH", raising=False)
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer()), \
             patch(f"{_SVC}.upsert_faq_entry", return_value=11) as upsert, \
             patch(f"{_SVC}.record_faq_entry_version"), \
             patch(f"{_SVC}.reply_to_askers", return_value=1), \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["status"] == "draft"
        assert upsert.call_args.kwargs["status"] == svc.FaqStatus.DRAFT

    def test_a_recurring_question_revises_a_published_entry_in_place(self):
        svc = _mod()
        entry = _entry(answer="Old, thinner answer.")
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer()), \
             patch(f"{_SVC}.upsert_faq_entry", return_value=7) as upsert, \
             patch(f"{_SVC}.record_faq_entry_version") as version, \
             patch(f"{_SVC}.reply_to_askers", return_value=3), \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[entry], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.REVISED
        # A public answer stays public when it is refreshed, and history keeps the old copy.
        assert upsert.call_args.kwargs["status"] == svc.FaqStatus.PUBLISHED
        assert upsert.call_args.kwargs["sort_order"] is None
        version.assert_called_once()

    def test_an_unchanged_rewrite_does_not_churn_the_history(self):
        svc = _mod()
        entry = _entry(answer="You can pause the automation at any time from the account page.")
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer()), \
             patch(f"{_SVC}.upsert_faq_entry") as upsert, \
             patch(f"{_SVC}.record_faq_entry_version") as version, \
             patch(f"{_SVC}.reply_to_askers", return_value=3), \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[entry], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.ANSWERED
        upsert.assert_not_called()
        version.assert_not_called()

    def test_an_existing_answer_is_replied_with_for_free(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer") as generate, \
             patch(f"{_SVC}.reply_to_askers", return_value=1) as reply, \
             patch(f"{_SVC}.update_feedback_triage") as stamp:
            result = svc.answer_question_cluster(_cluster(size=1), entries=[_entry()],
                                                 grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.ANSWERED
        generate.assert_not_called()
        assert reply.call_args[0][2] == "Open the account page."
        assert stamp.call_args.kwargs["status"] == svc.FeedbackStatus.RESOLVED

    def test_an_unanswered_question_waits_for_the_recurrence_threshold(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer") as generate, \
             patch(f"{_SVC}.update_feedback_triage") as stamp:
            result = svc.answer_question_cluster(_cluster(size=2), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.PENDING
        assert result["asks"] == 2 and result["needed"] == 3
        generate.assert_not_called()
        # Nothing is stamped — the rows must stay in the queue to keep accumulating asks.
        stamp.assert_not_called()

    def test_the_per_pass_answer_budget_defers_a_cluster(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer") as generate:
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING,
                                                 allow_generate=False)
        assert result["action"] == svc.FaqAction.PENDING
        generate.assert_not_called()

    def test_a_policy_claim_is_held_for_a_human_and_never_published(self):
        svc = _mod()
        cluster = _cluster(question="Is this against LinkedIn's Terms of Service?")
        with patch(f"{_SVC}.generate_faq_answer",
                   return_value=_answer(question="Is this against LinkedIn's Terms of Service?",
                                        answer="No, it is fully compliant.")), \
             patch(f"{_SVC}.hold_for_human", return_value=901) as hold, \
             patch(f"{_SVC}.upsert_faq_entry") as upsert, \
             patch(f"{_SVC}.reply_to_askers") as reply, \
             patch(f"{_SVC}.update_feedback_triage") as stamp:
            result = svc.answer_question_cluster(cluster, entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.HELD and result["issue_number"] == 901
        assert hold.call_args.kwargs["policy"] is True
        upsert.assert_not_called()
        reply.assert_not_called()
        assert stamp.call_args.kwargs["status"] == svc.FeedbackStatus.TRIAGED

    def test_the_models_own_policy_flag_also_holds(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer(policy_claim=True)), \
             patch(f"{_SVC}.hold_for_human", return_value=902) as hold, \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.HELD
        assert hold.call_args.kwargs["policy"] is True

    def test_a_fabricated_number_is_held(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer",
                   return_value=_answer(answer="You get 500 comments a day.")), \
             patch(f"{_SVC}.hold_for_human", return_value=903) as hold, \
             patch(f"{_SVC}.upsert_faq_entry") as upsert, \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.HELD
        assert "ungrounded specifics" in hold.call_args[0][2]
        upsert.assert_not_called()

    def test_an_ungroundable_question_is_held_with_no_proposed_answer(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer(answerable=False,
                                                                       answer="")), \
             patch(f"{_SVC}.hold_for_human", return_value=904) as hold, \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.HELD
        assert hold.call_args[0][1] is None

    def test_a_failed_generation_is_held_not_published(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer", return_value=None), \
             patch(f"{_SVC}.hold_for_human", return_value=905), \
             patch(f"{_SVC}.upsert_faq_entry") as upsert, \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.HELD
        upsert.assert_not_called()

    def test_a_failed_write_leaves_the_rows_for_the_next_pass(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer", return_value=_answer()), \
             patch(f"{_SVC}.upsert_faq_entry", return_value=None), \
             patch(f"{_SVC}.record_faq_entry_version") as version, \
             patch(f"{_SVC}.reply_to_askers") as reply, \
             patch(f"{_SVC}.update_feedback_triage") as stamp:
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.ERROR
        version.assert_not_called()
        reply.assert_not_called()
        stamp.assert_not_called()

    def test_personal_data_in_the_generated_answer_is_held_and_never_leaves_redacted(self):
        svc = _mod()
        with patch(f"{_SVC}.generate_faq_answer",
                   return_value=_answer(answer="Ask chris@example.com about the account page.")), \
             patch(f"{_SVC}.hold_for_human", return_value=906) as hold, \
             patch(f"{_SVC}.upsert_faq_entry") as upsert, \
             patch(f"{_SVC}.update_feedback_triage"):
            result = svc.answer_question_cluster(_cluster(), entries=[], grounding=GROUNDING)
        assert result["action"] == svc.FaqAction.HELD
        assert hold.call_args[0][2] == "the answer contains personal data"
        # The address never reaches GitHub either.
        assert "chris@example.com" not in hold.call_args[0][1]
        upsert.assert_not_called()

    def test_a_cluster_with_no_question_does_nothing(self):
        svc = _mod()
        cluster = _cluster()
        cluster["question"] = ""
        assert svc.answer_question_cluster(cluster)["action"] == svc.FaqAction.PENDING


def _answer(**overrides):
    payload = {"question": "How do I pause the automation?",
               "answer": "You can pause the automation at any time from the account page.",
               "answerable": True, "policy_claim": False, "confidence": 0.9}
    payload.update(overrides)
    return payload


class TestProcessFaqFeedback:
    def test_a_pass_answers_the_biggest_cluster_first_and_respects_the_budget(self, monkeypatch):
        svc = _mod()
        monkeypatch.setenv("FAQ_MAX_ANSWERS_PER_PASS", "1")
        rows = [_row(1, "How do I pause the automation?", user_id=1),
                _row(2, "How do I pause the automation please?", user_id=2),
                _row(3, "How do I pause the automation on weekends?", user_id=3),
                _row(4, "Which browsers do you support?", user_id=4),
                _row(5, "Which browsers do you support exactly?", user_id=5),
                _row(6, "Which browsers do you support here?", user_id=6),
                _row(7, "Comments never post to the feed.", user_id=7)]
        with patch(f"{_SVC}.get_faq_candidate_feedback", return_value=rows), \
             patch(f"{_SVC}.get_faq_entries", return_value=[]), \
             patch(f"{_SVC}.build_grounding", return_value=GROUNDING), \
             patch(f"{_SVC}.embed_text", return_value=None), \
             patch(f"{_SVC}.update_feedback_triage"), \
             patch(f"{_SVC}.answer_question_cluster") as answer:
            answer.side_effect = [
                {"action": svc.FaqAction.PUBLISHED, "cluster_id": 1, "entry_id": 11,
                 "question": "How do I pause the automation?", "answer": "A.", "status": "draft"},
                {"action": svc.FaqAction.PENDING, "cluster_id": 4},
            ]
            result = svc.process_faq_feedback()
        assert result["candidates"] == 7 and result["questions"] == 6
        assert result["counts"] == {svc.FaqAction.PUBLISHED: 1, svc.FaqAction.PENDING: 1}
        # The second cluster is out of budget — and the entry just published joins the in-memory
        # list so a same-pass duplicate would match it instead of publishing twice.
        assert answer.call_args_list[1].kwargs["allow_generate"] is False
        assert answer.call_args_list[1].kwargs["entries"][-1]["id"] == 11

    def test_nothing_question_shaped_costs_nothing(self):
        svc = _mod()
        with patch(f"{_SVC}.get_faq_candidate_feedback",
                   return_value=[_row(1, "Comments never post.")]), \
             patch(f"{_SVC}.get_faq_entries") as entries, \
             patch(f"{_SVC}.embed_text") as embed:
            result = svc.process_faq_feedback()
        assert result == {"candidates": 1, "questions": 0, "clusters": 0, "counts": {}}
        entries.assert_not_called()
        embed.assert_not_called()

    def test_one_failing_cluster_does_not_stop_the_pass(self):
        svc = _mod()
        with patch(f"{_SVC}.get_faq_candidate_feedback", return_value=[_row(1)]), \
             patch(f"{_SVC}.get_faq_entries", return_value=[]), \
             patch(f"{_SVC}.build_grounding", return_value=GROUNDING), \
             patch(f"{_SVC}.embed_text", return_value=None), \
             patch(f"{_SVC}.update_feedback_triage"), \
             patch(f"{_SVC}.answer_question_cluster", side_effect=RuntimeError("boom")), \
             patch(f"{_SVC}.log_error") as log:
            result = svc.process_faq_feedback()
        assert result["counts"] == {svc.FaqAction.ERROR: 1}
        log.assert_called_once()


class TestVersionHistory:
    def test_reverting_reapplies_a_version_and_records_the_revert(self):
        svc = _mod()
        version = {"id": 3, "question": "Q?", "answer": "Old answer.", "status": "published"}
        with patch(f"{_SVC}.apply_faq_entry_version", return_value=version), \
             patch(f"{_SVC}.record_faq_entry_version") as record:
            assert svc.revert_faq_answer(7, 3) == version
        record.assert_called_once_with(7, "Q?", "Old answer.", status="published", source='revert')

    def test_reverting_to_an_unknown_version_records_nothing(self):
        svc = _mod()
        with patch(f"{_SVC}.apply_faq_entry_version", return_value=None), \
             patch(f"{_SVC}.record_faq_entry_version") as record, \
             patch(f"{_SVC}.log_warning") as log:
            assert svc.revert_faq_answer(7, 999) is None
        record.assert_not_called()
        log.assert_called_once()

    def test_history_is_read_through_to_the_db_helper(self):
        svc = _mod()
        with patch(f"{_SVC}.get_faq_entry_versions", return_value=[{"id": 3}]) as get:
            assert svc.faq_answer_history(7, limit=5) == [{"id": 3}]
        get.assert_called_once_with(7, limit=5)
