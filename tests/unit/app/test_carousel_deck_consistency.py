"""A deck that contradicts its own caption is held for review (issue #2106).

Two production decks from the 2026-09 system audit are the fixtures:

* deck 110 — the caption promises "the exact 5 checks" and the deck carries 4 (slides 2-5);
* deck 105 — "AI in E-commerce: 5 Key Trends" rendered under a post about AI governance.

Both are measured where the slide text exists (`create_carousel_content`), recorded on
`posts.gate_reason`, and re-read at every gate pass — where, unlike the advisory slide slop note,
they DEMOTE, so the post is held at PENDING instead of auto-approved.
"""

from unittest.mock import patch

import pytest

import cqc_lem.app.run_content_plan as rcp
from cqc_lem.utilities.ai import content_framework as fw
from cqc_lem.utilities.ai.content_alignment import TOPIC_AUTHORITY_MIN_DEFAULT
from cqc_lem.utilities.quality_gates import (
    GATE_DECK_COUNT,
    GATE_DECK_TOPIC,
    GATE_SLIDE_SLOP,
    authenticity_finding,
    deck_count_finding,
    deck_topic_finding,
    demoting_findings,
    slide_slop_finding,
)

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"

# --- Deck 110: "the exact 5 checks", four item slides ----------------------------------------------
_CAPTION_110 = (
    "Every deploy that went wrong for us last year had one thing in common: nobody ran the checks.\n\n"
    "So we wrote them down. These are the exact 5 checks I run before any production release, "
    "in the order I run them.\n\n"
    "Save this for your next release review.")
_DECK_110 = {
    "cover": {"title": "The pre-release checklist", "content": "What I verify before every deploy."},
    "contents": [
        {"title": "Migrations are reversible", "content": "Every migration has a tested down step."},
        {"title": "Feature flags default off", "content": "New code ships dark until verified."},
        {"title": "Rollback tag is recorded", "content": "The last good tag is written before deploy."},
        {"title": "Health check is green", "content": "The deep health endpoint returns 200."},
    ],
    "call_to_action": {"title": "Save this", "content": "Keep it for your next release."},
}

# --- Deck 105: an e-commerce trends deck under a governance post -----------------------------------
_CAPTION_105 = (
    "Most AI governance programs fail before the first model ships.\n\n"
    "Not because the policy is wrong. Because nobody owns the decision when a model misbehaves in "
    "production.\n\n"
    "Last quarter I sat with a team whose risk committee met monthly while their models retrained "
    "weekly. Every approval was stale before it was signed.\n\n"
    "Governance that works is operational: a named owner per model, an audit trail on every prompt "
    "change, and a kill switch someone is allowed to pull.\n\n"
    "What does your approval path look like today?")
_DECK_105 = {
    "cover": {"title": "AI in E-commerce: 5 Key Trends",
              "content": "How retailers are using AI to sell more in 2026."},
    "insights": [
        {"title": "Personalized product recommendations",
         "content": "Shoppers see products matched to their browsing and purchase history."},
        {"title": "Dynamic pricing",
         "content": "Retail prices adjust in real time to demand, inventory and competitor offers."},
        {"title": "Visual search",
         "content": "Customers snap a photo and find similar items in the online store."},
        {"title": "Chatbots for customer service",
         "content": "Conversational assistants answer shipping and return questions all day."},
    ],
    "call_to_action": {"title": "Ready to grow your store?", "content": "Follow for more retail."},
}
# The same caption with a deck that is actually about it.
_DECK_ON_TOPIC = {
    "cover": {"title": "Operational AI governance",
              "content": "The controls that keep a production model accountable."},
    "insights": [
        {"title": "Name an owner per model",
         "content": "One person owns the decision when a model misbehaves in production."},
        {"title": "Review on the retrain cadence",
         "content": "If the model retrains weekly, the risk review runs weekly."},
        {"title": "Audit every prompt change",
         "content": "Log who changed the prompt, when, and which approval covered it."},
    ],
    "call_to_action": {"title": "Your approval path?", "content": "Tell me in the comments."},
}


class TestCountClaims:
    @pytest.mark.parametrize("text, count", [
        ("These are the exact 5 checks I run.", 5),
        ("Five key trends shaping retail.", 5),
        ("the 3 mistakes I made", 3),
        ("12 red flags in a contract", 12),
    ])
    def test_a_caption_counting_deck_items_is_read(self, text, count):
        assert [c["count"] for c in fw.deck_count_claims(text)] == [count]

    @pytest.mark.parametrize("text", [
        "I spent 10 years of lessons learning this.",  # a preposition is never a modifier
        "We cut deploy time for 3 clients.",            # not a deck-item noun
        "40% of teams skip this.",
        "One tip: write it down.",                      # below the list floor
        "",
        None,
    ])
    def test_a_claim_about_the_world_is_not_a_deck_count(self, text):
        assert fw.deck_count_claims(text) == []


class TestDeckCountReport:
    def test_deck_110_five_promised_four_delivered_fails(self):
        report = fw.deck_count_report(_DECK_110, _CAPTION_110)
        assert report["checked"] is True and report["passes"] is False
        assert report["items"] == 4 and report["readings"] == [4]
        assert report["claimed"] == ["5 checks"]
        assert "5 checks" in report["reasons"][0]

    def test_a_caption_that_matches_the_deck_passes(self):
        report = fw.deck_count_report(_DECK_110, _CAPTION_110.replace("5 checks", "4 checks"))
        assert report["checked"] is True and report["passes"] is True

    def test_any_matching_claim_passes_the_deck(self):
        # "3 mistakes" is about the author's past; "4 checks" is the deck — the deck is honest.
        caption = "After 3 mistakes in production, these are the 4 checks I run."
        assert fw.deck_count_report(_DECK_110, caption)["passes"] is True

    def test_a_deck_listing_its_items_on_one_slide_passes(self):
        deck = {"cover": {"title": "Checks", "content": "Before every release."},
                "contents": [{"title": "The list",
                              "content": "1. Reversible\n2. Flags off\n3. Tag\n4. Health\n5. Logs"}]}
        assert fw.deck_count_report(deck, _CAPTION_110)["passes"] is True

    def test_a_caption_that_counts_nothing_is_not_checked(self):
        report = fw.deck_count_report(_DECK_110, "Save this for your next release.")
        assert report["checked"] is False and report["passes"] is True

    def test_a_deck_with_no_body_slide_fails_open(self):
        report = fw.deck_count_report({"cover": {"title": "T", "content": "C"}}, _CAPTION_110)
        assert report["checked"] is False and report["passes"] is True


class TestDeckTopicReport:
    def test_deck_105_scores_below_the_threshold(self):
        report = fw.deck_topic_report(_DECK_105, _CAPTION_105)
        assert report["checked"] is True
        assert report["threshold"] == TOPIC_AUTHORITY_MIN_DEFAULT
        assert report["score"] < report["threshold"]
        assert report["passes"] is False

    def test_a_deck_about_its_caption_passes(self):
        report = fw.deck_topic_report(_DECK_ON_TOPIC, _CAPTION_105)
        assert report["passes"] is True and report["score"] >= report["threshold"]

    def test_the_threshold_is_the_shared_topic_authority_one(self, monkeypatch):
        monkeypatch.setenv("TOPIC_AUTHORITY_MIN", "0.0")
        assert fw.deck_topic_report(_DECK_105, _CAPTION_105)["passes"] is True

    @pytest.mark.parametrize("deck, caption", [({}, _CAPTION_105), (_DECK_105, ""),
                                               (_DECK_105, None)])
    def test_an_empty_side_fails_open(self, deck, caption):
        report = fw.deck_topic_report(deck, caption)
        assert report["checked"] is False and report["passes"] is True


def _record(deck, caption, existing=None):
    """Run the reporter with the DB seams mocked; returns the `update_db_post_gate_reason` mock."""
    with patch(f"{_RCP}.get_post_gate_reason", return_value=list(existing or [])), \
         patch(f"{_RCP}.update_db_post_gate_reason") as store:
        rcp._report_carousel_deck_consistency(1, 7, deck, caption)
    return store


class TestRecordingAtGeneration:
    def test_deck_110_records_a_holding_count_finding(self):
        findings = _record(_DECK_110, _CAPTION_110).call_args[0][1]
        assert [f["gate"] for f in findings] == [GATE_DECK_COUNT]
        assert findings[0]["demoted"] is True
        assert findings[0]["deck_items"] == 4 and findings[0]["deck_counts"] == [4]

    def test_deck_105_records_a_holding_topic_finding(self):
        findings = _record(_DECK_105, _CAPTION_105).call_args[0][1]
        assert [f["gate"] for f in findings] == [GATE_DECK_TOPIC]
        assert findings[0]["demoted"] is True

    def test_other_findings_survive_and_a_stale_deck_hold_is_replaced(self):
        prior = [authenticity_finding(41, 60), deck_topic_finding(0.01, 0.15)]
        findings = _record(_DECK_110, _CAPTION_110, prior).call_args[0][1]
        assert [f["gate"] for f in findings] == ["authenticity", GATE_DECK_COUNT]

    def test_a_regenerated_consistent_deck_clears_the_hold(self):
        prior = [authenticity_finding(41, 60), deck_count_finding(4, ["5 checks"])]
        store = _record(_DECK_ON_TOPIC, _CAPTION_105, prior)
        assert [f["gate"] for f in store.call_args[0][1]] == ["authenticity"]

    def test_a_consistent_deck_with_nothing_to_clear_never_writes(self):
        assert not _record(_DECK_ON_TOPIC, _CAPTION_105).called

    def test_no_post_row_or_no_deck_records_nothing(self):
        with patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._report_carousel_deck_consistency(1, None, _DECK_110, _CAPTION_110)
            rcp._report_carousel_deck_consistency(1, 7, {}, _CAPTION_110)
        store.assert_not_called()

    def test_an_unwritable_reason_only_logs(self):
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store, \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._report_carousel_deck_consistency(1, 7, _DECK_110, _CAPTION_110)
        store.assert_not_called()
        warn.assert_called_once()

    def test_create_carousel_content_checks_the_generated_deck_against_its_caption(self):
        deck = {"cover": {"title": "T", "content": "C"}}
        with patch(f"{_RCP}.get_engagement_preferences", return_value={}), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="brief"), \
             patch(f"{_RCP}._select_story_for_post", return_value=None), \
             patch(f"{_RCP}._select_carousel_blueprint", return_value=None), \
             patch(f"{_RCP}._report_carousel_fact_grounding"), \
             patch(f"{_RCP}._report_carousel_slide_slop"), \
             patch(f"{_RCP}._report_carousel_deck_consistency") as check, \
             patch("cqc_lem.utilities.ai.ai_helper.generate_carousel_content",
                   return_value=("caption", deck)):
            rcp.create_carousel_content(1, "awareness", None)
        check.assert_called_once_with(1, None, deck, "caption")


class TestTheGatePassHoldsAtPending:
    # Only the deck's own findings: the caption's other gates (fact grounding reads "5" as an
    # unverified number) are not what these tests are about.
    _DECK_GATES = (GATE_DECK_COUNT, GATE_DECK_TOPIC, GATE_SLIDE_SLOP)

    def _gates(self, content, recorded, post_type="carousel"):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=recorded):
            findings = rcp.evaluate_post_gates(7, content, post_type)
        return [f for f in findings if f["gate"] in self._DECK_GATES]

    def _held(self, findings):
        """The status-setter's approve decision — False means the post stays PENDING."""
        with patch(f"{_RCP}.get_post_ever_gate_demoted", return_value=False):
            return not rcp._may_auto_approve(1, 7, True, findings)

    def test_the_110_fixture_holds_at_pending(self):
        recorded = _record(_DECK_110, _CAPTION_110).call_args[0][1]
        findings = self._gates(_CAPTION_110, recorded)
        assert [f["gate"] for f in demoting_findings(findings)] == [GATE_DECK_COUNT]
        assert self._held(findings)
        assert not self._held([f for f in findings if f["gate"] != GATE_DECK_COUNT])

    def test_the_105_fixture_holds_at_pending(self):
        recorded = _record(_DECK_105, _CAPTION_105).call_args[0][1]
        findings = self._gates(_CAPTION_105, recorded)
        assert [f["gate"] for f in demoting_findings(findings)] == [GATE_DECK_TOPIC]
        assert self._held(findings)

    def test_editing_the_caption_to_match_the_deck_releases_the_count_hold(self):
        recorded = [deck_count_finding(4, ["5 checks"], [4])]
        edited = _CAPTION_110.replace("5 checks", "4 checks")
        assert self._gates(edited, recorded) == []

    def test_an_edit_that_still_mismatches_keeps_the_hold_naming_the_new_claim(self):
        recorded = [deck_count_finding(4, ["5 checks"], [4])]
        findings = self._gates("Here are the six checks.", recorded)
        assert [f["gate"] for f in findings] == [GATE_DECK_COUNT]
        assert findings[0]["details"] == ["six checks"] and findings[0]["deck_items"] == 4

    def test_a_count_hold_without_recorded_counts_is_carried_as_is(self):
        legacy = deck_count_finding(4, ["5 checks"])
        legacy.pop("deck_counts")
        assert self._gates(_CAPTION_110, [legacy]) == [legacy]

    def test_the_topic_hold_survives_a_caption_edit(self):
        # The slides are images: no caption edit can change what they are about.
        recorded = [deck_topic_finding(0.02, 0.15)]
        assert [f["gate"] for f in self._gates("A new caption.", recorded)] == [GATE_DECK_TOPIC]

    def test_the_advisory_slide_note_rides_along_unchanged(self):
        recorded = [slide_slop_finding([], ["x"]), deck_topic_finding(0.02, 0.15)]
        assert [f["gate"] for f in self._gates("", recorded)] == [GATE_SLIDE_SLOP, GATE_DECK_TOPIC]

    def test_a_text_post_never_reads_deck_holds(self):
        assert self._gates(_CAPTION_110, [deck_count_finding(4, ["5 checks"], [4])], "text") == []

    def test_a_failed_gate_pass_keeps_the_deck_holds(self):
        recorded = [deck_count_finding(4, ["5 checks"], [4]), deck_topic_finding(0.02, 0.15)]
        with patch(f"{_RCP}.get_post_authenticity_score", return_value=None), \
             patch(f"{_RCP}._post_archetype_or_none", return_value=None), \
             patch(f"{_RCP}._recorded_similarity_finding", return_value=[]), \
             patch(f"{_RCP}._engagement_prefs_or_empty", return_value={}), \
             patch(f"{_RCP}._fact_anchors_for", return_value=[]), \
             patch(f"{_RCP}._cta_keyword_for", return_value=None), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=recorded), \
             patch(f"{_RCP}.evaluate_post_gates", side_effect=RuntimeError("gates down")), \
             patch(f"{_RCP}.log_warning"):
            findings = rcp._gate_findings_for_post(1, 7, _CAPTION_110, "carousel")
        assert [f["gate"] for f in findings] == [GATE_DECK_COUNT, GATE_DECK_TOPIC]


class TestFindingCopy:
    def test_the_count_finding_names_both_numbers_and_the_caption_fix(self):
        finding = deck_count_finding(4, ["5 checks"])
        assert "“5 checks”" in finding["explanation"] and "4 item slide(s)" in finding["explanation"]
        assert "Edit the post text" in finding["remediation"]
        assert finding["score"] is None and finding["threshold"] is None
        assert finding["deck_counts"] == [4]

    def test_the_topic_finding_carries_its_score_pair(self):
        finding = deck_topic_finding(0.0175, 0.15)
        assert finding["score"] == 0.0175 and finding["threshold"] == 0.15
        assert "regenerate" in finding["remediation"].lower()
