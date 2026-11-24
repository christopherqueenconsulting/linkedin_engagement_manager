"""Phase 4 of #2655: the ICP rubric scores the funnel, leads and roster suggestions."""

from datetime import datetime

import pytest

from cqc_lem.utilities.connection_targeting import (
    apply_icp_to_prospects,
    rank_roster_suggestions,
    rubric_icp_scorer,
)
from cqc_lem.utilities.icp_rubric import IcpDefinition
from cqc_lem.utilities.lead_scoring import ICP_UNKNOWN, LeadSignalKind, score_leads

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 24, 12, 0, 0)
_DEF = IcpDefinition.model_validate({
    "segments": [{"id": "cto", "role_titles": ["cto"]}],
    "exclusions": {"competitors": ["Rival"]}})


def _url(slug: str) -> str:
    return f"https://www.linkedin.com/in/{slug}"


def _p(slug: str, stage: str = "connect") -> dict:
    return {"profile_url": _url(slug), "name": slug, "stage": stage}


def _facts(**kw) -> dict:
    return {"job_title": "CTO", "company_name": "Acme", **kw}


class TestApplyIcpToProspects:
    def test_ranks_fit_first_and_drops_known_mismatch(self):
        facts = {_url("a"): _facts(), _url("b"): _facts(job_title="Cook")}
        kept, held = apply_icp_to_prospects([_p("b"), _p("a")], _DEF, facts_by_url=facts)
        assert [p["name"] for p in kept] == ["a"]
        assert held == []

    def test_unscraped_engager_is_held_not_scored_or_dropped(self):
        kept, held = apply_icp_to_prospects([_p("new")], _DEF, facts_by_url={})
        assert kept == []
        assert held == [_url("new")]

    def test_unscraped_roster_prospect_keeps_a_neutral_place_after_scored(self):
        facts = {_url("a"): _facts()}
        kept, held = apply_icp_to_prospects([_p("r", "comment"), _p("a")], _DEF, facts_by_url=facts)
        assert [p["name"] for p in kept] == ["a", "r"]
        assert held == []

    def test_cooldown_slug_is_excluded(self):
        facts = {_url("a"): _facts()}
        kept, held = apply_icp_to_prospects([_p("a")], _DEF, facts_by_url=facts,
                                            touched_slugs={"a"})
        assert kept == [] and held == []

    def test_empty_profile_and_bot_are_dropped(self):
        facts = {_url("e"): {"full_name": "X"}, _url("b"): _facts(job_title="CTO, DM for signals")}
        kept, held = apply_icp_to_prospects([_p("e"), _p("b")], _DEF, facts_by_url=facts)
        assert kept == [] and held == []

    def test_hard_filter_drops_competitor(self):
        facts = {_url("c"): _facts(company_name="Rival Inc")}
        kept, _ = apply_icp_to_prospects([_p("c")], _DEF, facts_by_url=facts)
        assert kept == []


class TestRankRosterSuggestions:
    def test_unscraped_suggestion_is_kept_and_stage_not_leaked(self):
        pool = [{"profile_url": _url("n"), "name": "n"}, {"profile_url": _url("a"), "name": "a"}]
        out = rank_roster_suggestions(pool, _DEF, facts_by_url={_url("a"): _facts()})
        assert [s["name"] for s in out] == ["a", "n"]
        assert all("stage" not in s for s in out)

    def test_limit(self):
        pool = [{"profile_url": _url(str(i)), "name": str(i)} for i in range(5)]
        assert len(rank_roster_suggestions(pool, _DEF, limit=2)) == 2


class TestRubricLeadScorer:
    def test_no_facts_is_neutral(self):
        assert rubric_icp_scorer(_DEF)({}, _url("x"), 40) == ICP_UNKNOWN

    def test_fit_scores_total_and_mismatch_scores_zero(self):
        scorer = rubric_icp_scorer(_DEF)
        assert scorer(_facts(), _url("a"), 100) > 50
        assert scorer(_facts(job_title="Cook"), _url("b"), 100) == 0

    def test_score_leads_uses_scorer_only_when_given(self):
        rows = [{"kind": LeadSignalKind.ENGAGED, "person_name": "A",
                 "person_profile_url": _url("a"), "occurred_at": _NOW, "detail": ""}]
        facts = {_url("a"): _facts(job_title="Cook")}
        plain = score_leads(rows, _NOW, facts_by_url=facts)[0]
        rubric = score_leads(rows, _NOW, facts_by_url=facts,
                             icp_scorer=rubric_icp_scorer(_DEF))[0]
        assert rubric.icp_score == 0
        assert plain.icp_score != 0
