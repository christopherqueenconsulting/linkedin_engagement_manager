"""Unit tests for the lead scorer (issue #484) — identity keys, recency/frequency weighting, ICP
fit, stage derivation, recommended actions, and the end-to-end ranking.
"""

from datetime import datetime, timedelta, timezone

import pytest

from cqc_lem.utilities.db import LeadSignalKind, LeadStage
from cqc_lem.utilities.lead_scoring import (
    ICP_UNKNOWN,
    LeadActivity,
    derive_stage,
    engagement_strength,
    group_activity,
    icp_fit,
    person_key,
    profile_slug,
    recency_weight,
    recommend_action,
    score_lead,
    score_leads,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 24, 12, 0, 0)


def _act(kind, days_ago=0, detail=""):
    return LeadActivity(kind=kind, occurred_at=_NOW - timedelta(days=days_ago), detail=detail)


class TestPersonKey:
    def test_prefers_the_profile_slug(self):
        assert person_key("Jane Doe", "https://www.linkedin.com/in/Jane-Doe/") == "in:jane-doe"

    def test_query_string_and_trailing_slash_collapse_to_one_person(self):
        a = person_key("Jane", "https://linkedin.com/in/jane-doe/")
        b = person_key(None, "https://www.linkedin.com/in/jane-doe?trk=feed")
        assert a == b == "in:jane-doe"

    def test_falls_back_to_the_normalized_name(self):
        assert person_key("  Jane   Doe ", None) == "name:jane doe"

    def test_no_identity_at_all_is_empty(self):
        assert person_key(None, None) == ""
        assert person_key("  ", "https://linkedin.com/company/acme") == ""

    def test_profile_slug_of_a_non_profile_url(self):
        assert profile_slug("https://linkedin.com/feed/update/123") == ""


class TestRecencyWeight:
    def test_today_is_full_weight(self):
        assert recency_weight(_NOW, _NOW) == 1.0

    def test_half_life_halves_it(self):
        assert recency_weight(_NOW - timedelta(days=14), _NOW) == pytest.approx(0.5, abs=0.01)

    def test_ancient_signal_hits_the_floor_not_zero(self):
        assert recency_weight(_NOW - timedelta(days=3650), _NOW) == pytest.approx(0.1)

    def test_missing_or_future_timestamps_are_treated_as_now(self):
        assert recency_weight(None, _NOW) == 1.0
        assert recency_weight(_NOW + timedelta(days=2), _NOW) == 1.0

    def test_naive_db_timestamp_against_an_aware_now_does_not_raise(self):
        aware = _NOW.replace(tzinfo=timezone.utc)
        assert recency_weight(_NOW - timedelta(days=14), aware) == pytest.approx(0.5, abs=0.01)


class TestIcpFit:
    def test_no_facts_is_neutral_never_a_penalty(self):
        assert icp_fit({}, ["ai"]) == ICP_UNKNOWN
        assert icp_fit({"job_title": "  "}, ["ai"]) == ICP_UNKNOWN

    def test_topic_matches_and_seniority_compound(self):
        senior = icp_fit({"job_title": "Chief Technology Officer", "industry": "AI automation"},
                         ["ai", "automation", "saas"])
        junior = icp_fit({"job_title": "Intern", "industry": "AI automation"},
                         ["ai", "automation", "saas"])
        assert senior > junior and senior >= 75

    def test_matching_every_term_of_a_short_list_saturates_the_topic_half(self):
        assert icp_fit({"job_title": "Founder", "industry": "AI automation"}, ["ai"]) == 100

    def test_three_matched_terms_saturate_the_topic_half(self):
        facts = {"job_title": "VP Sales", "industry": "ai automation saas fintech"}
        assert icp_fit(facts, ["ai", "automation", "saas"]) == \
            icp_fit(facts, ["ai", "automation", "saas", "fintech"])

    def test_mid_level_title_beats_an_unmatched_one(self):
        assert icp_fit({"job_title": "Director of Ops"}, []) > icp_fit({"job_title": "Analyst"}, [])

    def test_titles_and_topics_match_whole_words_only(self):
        # 'cto' hides inside 'director' and 'ai' inside 'chain' — substring matching would call
        # a supply-chain director a C-level AI buyer.
        assert icp_fit({"job_title": "Director of Ops"}, []) < icp_fit({"job_title": "CTO"}, [])
        assert icp_fit({"job_title": "Analyst", "industry": "supply chain"}, ["ai"]) == \
            icp_fit({"job_title": "Analyst", "industry": "supply chain"}, ["blockchain"])

    def test_no_stated_targeting_stays_mid_range(self):
        assert 30 <= icp_fit({"job_title": "Analyst", "company_name": "Acme"}, []) <= 60


class TestEngagementStrength:
    def test_a_buying_question_outranks_a_pile_of_comments(self):
        intent = engagement_strength([_act(LeadSignalKind.INTENT)], _NOW)
        chatty = engagement_strength([_act(LeadSignalKind.ENGAGED, i) for i in range(6)], _NOW)
        assert intent > chatty

    def test_repeats_add_with_diminishing_returns(self):
        one = engagement_strength([_act(LeadSignalKind.ENGAGED)], _NOW)
        three = engagement_strength([_act(LeadSignalKind.ENGAGED) for _ in range(3)], _NOW)
        assert one < three < 3 * one

    def test_recent_beats_stale_for_the_same_signal(self):
        fresh = engagement_strength([_act(LeadSignalKind.ENGAGED, 0)], _NOW)
        stale = engagement_strength([_act(LeadSignalKind.ENGAGED, 60)], _NOW)
        assert fresh > stale

    def test_no_activity_scores_zero_and_is_capped_at_100(self):
        assert engagement_strength([], _NOW) == 0
        assert engagement_strength([_act(LeadSignalKind.INTENT, i) for i in range(20)], _NOW) <= 100

    def test_unknown_kind_still_counts_a_little(self):
        assert engagement_strength([LeadActivity(kind="mystery", occurred_at=_NOW)], _NOW) > 0

    def test_missing_timestamps_do_not_break_ordering(self):
        acts = [LeadActivity(kind=LeadSignalKind.ENGAGED), _act(LeadSignalKind.ENGAGED, 3)]
        assert engagement_strength(acts, _NOW) > 0


class TestDeriveStage:
    def test_answered_buying_question_is_an_opportunity(self):
        acts = [_act(LeadSignalKind.INTENT, detail="sent")]
        assert derive_stage(acts, 40) == LeadStage.OPPORTUNITY

    def test_dm_plus_inbound_engagement_is_a_conversation(self):
        acts = [_act(LeadSignalKind.DM), _act(LeadSignalKind.ENGAGED)]
        assert derive_stage(acts, 30) == LeadStage.IN_CONVERSATION

    def test_an_unanswered_outbound_dm_is_not_a_conversation(self):
        assert derive_stage([_act(LeadSignalKind.DM)], 10) == LeadStage.COLD

    def test_an_unanswered_buying_question_is_hot(self):
        assert derive_stage([_act(LeadSignalKind.INTENT, detail="new")], 5) == LeadStage.HOT

    def test_high_score_alone_is_hot(self):
        assert derive_stage([_act(LeadSignalKind.ENGAGED)], 85) == LeadStage.HOT

    def test_warm_and_cold_split_on_the_threshold(self):
        assert derive_stage([_act(LeadSignalKind.ENGAGED)], 40) == LeadStage.WARM
        assert derive_stage([_act(LeadSignalKind.ENGAGED)], 5) == LeadStage.COLD
        assert derive_stage([], 0) == LeadStage.COLD


class TestRecommendAction:
    def test_hot_with_intent_points_at_the_leads_inbox(self):
        action = recommend_action(LeadStage.HOT, [_act(LeadSignalKind.INTENT)])
        assert "Leads inbox" in action

    def test_hot_and_already_connected_suggests_a_dm(self):
        action = recommend_action(LeadStage.HOT, [_act(LeadSignalKind.CONNECT),
                                                  _act(LeadSignalKind.ENGAGED)])
        assert "DM" in action

    def test_does_not_suggest_connecting_with_someone_already_invited(self):
        action = recommend_action(LeadStage.WARM, [_act(LeadSignalKind.CONNECT)])
        assert "connection note" not in action

    def test_warm_without_an_invite_suggests_one(self):
        assert "connection note" in recommend_action(LeadStage.WARM, [_act(LeadSignalKind.ENGAGED)])

    def test_every_stage_has_an_action(self):
        for stage in LeadStage:
            assert recommend_action(stage, []).strip()


class TestScoreLead:
    def test_icp_fit_scales_the_same_engagement_up_and_down(self):
        acts = [_act(LeadSignalKind.ENGAGED, 1), _act(LeadSignalKind.PROFILE_VIEW, 2)]
        good = score_lead(acts, _NOW, person_name="Jane",
                          person_profile_url="https://linkedin.com/in/jane",
                          facts={"job_title": "Founder", "industry": "AI automation"},
                          target_terms=["ai", "automation"])
        poor = score_lead(acts, _NOW, person_name="Bob",
                          person_profile_url="https://linkedin.com/in/bob",
                          facts={"job_title": "Student", "industry": "hospitality"},
                          target_terms=["ai", "automation"])
        assert good.score > poor.score
        assert good.engagement_score == poor.engagement_score  # only the ICP half differs

    def test_reports_signals_counts_and_window(self):
        acts = [_act(LeadSignalKind.ENGAGED, 5), _act(LeadSignalKind.ENGAGED, 1),
                _act(LeadSignalKind.INTENT, 0, detail="new")]
        lead = score_lead(acts, _NOW, person_name="Jane",
                          person_profile_url="https://linkedin.com/in/jane")
        assert lead.person_key == "in:jane"
        assert lead.signal_count == 3
        assert set(lead.signals) == {"engaged", "intent"}
        assert lead.first_signal_at == _NOW - timedelta(days=5)
        assert lead.last_signal_at == _NOW
        assert lead.stage == LeadStage.HOT

    def test_reasons_explain_the_score_in_plain_language(self):
        acts = [_act(LeadSignalKind.INTENT, 0), _act(LeadSignalKind.ENGAGED, 2),
                _act(LeadSignalKind.ENGAGED, 4)]
        lead = score_lead(acts, _NOW, person_name="Jane",
                          facts={"job_title": "Founder", "industry": "ai"}, target_terms=["ai"])
        assert "asked a buying question" in lead.reasons
        assert "engaged with your posts (2×)" in lead.reasons
        assert "last signal today" in lead.reasons

    def test_reasons_flag_a_weak_fit_and_an_older_signal(self):
        lead = score_lead([_act(LeadSignalKind.ENGAGED, 9)], _NOW, person_name="Bob",
                          facts={"job_title": "Student", "company_name": "School"},
                          target_terms=["ai", "automation", "saas"])
        assert "weak ICP fit" in lead.reasons and "last signal 9d ago" in lead.reasons

    def test_no_activity_scores_zero_and_stays_cold(self):
        lead = score_lead([], _NOW, person_name="Nobody")
        assert lead.score == 0 and lead.stage == LeadStage.COLD
        assert lead.first_signal_at is None and lead.last_signal_at is None


class TestGroupActivity:
    def test_name_only_and_url_rows_stay_separate_leads(self):
        rows = [
            {"kind": "engaged", "person_name": "Jane Doe", "person_profile_url": None,
             "occurred_at": _NOW},
            {"kind": "dm", "person_name": "Jane Doe",
             "person_profile_url": "https://linkedin.com/in/jane-doe", "occurred_at": _NOW},
        ]
        people = group_activity(rows)
        # Name-only and URL rows key differently by design; the URL row keeps the profile link.
        assert people["in:jane-doe"]["person_profile_url"].endswith("jane-doe")
        assert people["name:jane doe"]["activities"][0].kind == "engaged"

    def test_two_signals_for_one_url_collapse_into_one_person(self):
        rows = [{"kind": "engaged", "person_name": "Jane",
                 "person_profile_url": "https://linkedin.com/in/jane/", "occurred_at": _NOW},
                {"kind": "intent", "person_name": None,
                 "person_profile_url": "https://linkedin.com/in/jane?x=1", "occurred_at": _NOW,
                 "detail": "new"}]
        people = group_activity(rows)
        assert list(people) == ["in:jane"]
        assert len(people["in:jane"]["activities"]) == 2
        assert people["in:jane"]["person_name"] == "Jane"

    def test_rows_with_no_identity_are_dropped(self):
        assert group_activity([{"kind": "engaged", "person_name": "  ",
                                "person_profile_url": None, "occurred_at": _NOW}]) == {}

    def test_empty_input(self):
        assert group_activity([]) == {} and group_activity(None) == {}


class TestScoreLeads:
    def _rows(self):
        return [
            {"kind": "intent", "person_name": "Ada", "detail": "new",
             "person_profile_url": "https://linkedin.com/in/ada", "occurred_at": _NOW},
            {"kind": "engaged", "person_name": "Bob",
             "person_profile_url": "https://linkedin.com/in/bob",
             "occurred_at": _NOW - timedelta(days=40)},
        ]

    def test_ranks_the_hottest_lead_first(self):
        leads = score_leads(self._rows(), _NOW, target_terms=["ai"])
        assert [l.person_name for l in leads] == ["Ada", "Bob"]
        assert leads[0].stage == LeadStage.HOT and leads[1].stage == LeadStage.COLD

    def test_profile_facts_are_matched_regardless_of_url_form(self):
        facts = {"https://www.linkedin.com/in/Ada/": {"job_title": "Founder", "industry": "ai"}}
        leads = score_leads(self._rows(), _NOW, facts_by_url=facts, target_terms=["ai"])
        ada = next(l for l in leads if l.person_name == "Ada")
        assert ada.icp_score >= 90

    def test_facts_for_unknown_people_are_ignored(self):
        leads = score_leads(self._rows(), _NOW, facts_by_url={"not-a-profile": {"job_title": "CEO"}})
        assert all(l.icp_score == ICP_UNKNOWN for l in leads)

    def test_no_rows_yields_no_leads(self):
        assert score_leads([], _NOW) == []
