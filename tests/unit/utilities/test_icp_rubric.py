"""ICP rubric (issue #2630, Phase 1): filters first, weights sum to 100, deterministic components."""

from datetime import datetime

import pytest

from cqc_lem.utilities import icp_rubric as r
from cqc_lem.utilities.connection_targeting import (
    CandidateSignal,
    apply_icp_rubric,
    rank_candidates,
)

NOW = datetime(2026, 11, 23, 12, 0, 0)

DEFINITION = r.IcpDefinition(
    segments=[r.IcpSegment(id="ops", name="Ops leaders", role_titles=["coo", "head of operations"],
                           seniority=["head", "chief"], industries=["software"],
                           company_size_min=10, company_size_max=200, geographies=["united states"])],
    exclusions=r.IcpExclusions(clients=["Acme"], competitors=["Rivalco"],
                               do_not_contact=["jane-doe"], seniority=["intern"]))
GOOD = {"job_title": "Head of Operations", "company_name": "Initech", "industry": "Software"}


def test_weights_sum_to_100():
    assert sum(r.WEIGHTS.values()) == 100


def test_scoring_is_deterministic_and_stores_components_and_reason():
    a = r.score_person(DEFINITION, GOOD, "https://www.linkedin.com/in/bob", warmth=80,
                       connection_degree="2nd")
    b = r.score_person(DEFINITION, GOOD, "https://www.linkedin.com/in/bob", warmth=80,
                       connection_degree="2nd")
    assert a == b and a.eligible
    assert a.segment_id == "ops" and a.rubric_version == r.ICP_RUBRIC_VERSION
    assert set(a.components) == set(r.WEIGHTS)
    assert a.components["engagement"] == 20.0 and a.components["reachability"] == 10.0
    assert "Ops leaders" in a.reason and a.total == round(sum(a.components.values()))


def test_missing_size_geo_timing_score_neutral_never_filter():
    s = r.score_person(DEFINITION, GOOD, warmth=0)
    assert s.eligible
    assert s.components["timing"] == 5.0


@pytest.mark.parametrize("facts,url,degree,touched,expected", [
    (GOOD, "https://www.linkedin.com/in/Jane-Doe/", None, False, r.FILTER_DO_NOT_CONTACT),
    ({**GOOD, "company_name": "Acme Corp"}, "", None, False, r.FILTER_CLIENT),
    ({**GOOD, "company_name": "Rivalco"}, "", None, False, r.FILTER_COMPETITOR),
    (GOOD, "", "1st", False, r.FILTER_CONNECTED),
    (GOOD, "", None, True, r.FILTER_RECENT_TOUCH),
    ({**GOOD, "job_title": "Head of Operations Intern"}, "", None, False, r.FILTER_SENIORITY),
    ({**GOOD, "job_title": "Barista"}, "", None, False, r.FILTER_NO_SEGMENT),
    ({**GOOD, "company_size": 5}, "", None, False, r.FILTER_NO_SEGMENT),
    ({**GOOD, "location": "Germany"}, "", None, False, r.FILTER_NO_SEGMENT),
    ({**GOOD, "industry": "Mining"}, "", None, False, r.FILTER_NO_SEGMENT),
])
def test_hard_filter_blocks_and_scores_zero(facts, url, degree, touched, expected):
    s = r.score_person(DEFINITION, facts, url, warmth=100, connection_degree=degree,
                       recently_touched=touched)
    assert not s.eligible and s.total == 0 and s.filtered_by == expected


def test_best_segment_wins_and_timing_signal():
    two = r.IcpDefinition(segments=[
        r.IcpSegment(id="weak", role_titles=["operations"]),
        r.IcpSegment(id="strong", role_titles=["head of operations"], seniority=["head"],
                     industries=["software"])])
    s = r.score_person(two, {**GOOD, "recent_job_change": True})
    assert s.segment_id == "strong" and s.components["timing"] == 10.0


def test_parse_definition_rejects_bad_or_empty():
    assert r.parse_icp_definition(None) is None
    assert r.parse_icp_definition({"segments": []}) is None
    assert r.parse_icp_definition({"segments": [{"id": ""}]}) is None
    assert r.parse_icp_definition({"segments": [{"id": "a"}]}).segments[0].id == "a"
    assert r.has_facts({"industry": "x"}) and not r.has_facts({"job_title": " "})


def _signals(*urls):
    return [CandidateSignal(person_name=u, person_profile_url=f"https://www.linkedin.com/in/{u}",
                            occurred_at=NOW) for u in urls]


def test_apply_rubric_holds_factless_drops_filtered_and_ranks():
    ranked = rank_candidates(_signals("bob", "nofacts", "intern"), NOW, min_icp=0)
    facts = {"https://www.linkedin.com/in/bob": GOOD,
             "https://www.linkedin.com/in/intern": {**GOOD, "job_title": "Intern"}}
    kept, scores, held = apply_icp_rubric(ranked, DEFINITION, facts_by_url=facts, min_score=0)
    assert held == 1
    assert [c.person_name for c in kept] == ["bob"]
    assert kept[0].icp_score == scores[kept[0].person_key].total and kept[0].known_fit


def test_apply_rubric_floor_and_limit():
    ranked = rank_candidates(_signals("a", "b"), NOW, min_icp=0)
    facts = {f"https://www.linkedin.com/in/{n}": GOOD for n in "ab"}
    assert apply_icp_rubric(ranked, DEFINITION, facts, min_score=101)[0] == []
    assert len(apply_icp_rubric(ranked, DEFINITION, facts, limit=1)[0]) == 1
