"""ICP rubric evaluation (issue #2655, Phase 5): report, exploration slice, back-test, admin route."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from cqc_lem.utilities import icp_evaluation as ev
from cqc_lem.utilities.connection_targeting import CandidateSignal, apply_icp_rubric, rank_candidates
from cqc_lem.utilities.icp_rubric import IcpDefinition, IcpSegment

DAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 12, 0, 0)
DEFINITION = IcpDefinition(segments=[IcpSegment(id="ops", role_titles=["coo"], industries=["software"])])


def _row(score=80, segment="ops", accepted=0, reason="r", version="v1", status="sent"):
    return {"status": status, "icp_score": score, "segment_id": segment, "accepted": accepted,
            "icp_reason": reason, "icp_rubric_version": version}


def test_score_band_edges():
    assert [ev.score_band(s) for s in (100, 70, 69, 40, 39, 0, None)] == \
        ["top", "top", "mid", "mid", "low", "low", "low"]


def test_report_insufficient_under_floor_and_rate_over_it():
    rows = [_row(80, "ops", accepted=int(i < 5)) for i in range(ev.MIN_SAMPLE)]
    rows += [_row(50, "ops2") for _ in range(3)]
    rep = ev.outcome_report(rows)
    top = rep["by_band"]["top"]
    assert top["sent"] == ev.MIN_SAMPLE and top["accepted"] == 5 and top["acceptance_rate"] == 0.25
    assert rep["by_band"]["mid"] == {"sent": 3, "accepted": 0, "acceptance_rate": None,
                                     "note": ev.INSUFFICIENT}
    assert rep["by_segment"]["ops2"]["note"] == ev.INSUFFICIENT
    assert rep["replies"] == ev.NOT_MEASURED and "lower bound" in rep["caveat"]


def test_report_excludes_unscored_and_unsent_and_tracks_exploration():
    rows = [_row(version=None), _row(status="failed"),
            _row(55, reason=f"x {ev.EXPLORATION_MARK}", accepted=1), _row(90)]
    rep = ev.outcome_report(rows)
    assert rep["total"]["sent"] == 2
    assert rep["exploration"]["sent"] == 1 and rep["exploration"]["accepted"] == 1


def test_report_empty():
    assert ev.outcome_report(None)["total"]["sent"] == 0


def test_backtest_counts_bands_unscored_and_ineligible():
    good = {"job_title": "COO", "company_name": "Initech", "industry": "Software"}
    facts = {"https://www.linkedin.com/in/a": good,
             "https://www.linkedin.com/in/b": {"job_title": "Plumber", "company_name": "X",
                                               "industry": "Trades"}}
    contacts = [{"recipient_profile_url": f"https://www.linkedin.com/in/{n}"} for n in "abc"]
    out = ev.backtest(contacts, DEFINITION, facts)
    assert out["accepted"] == 3 and out["unscored"] == 1 and out["ineligible"] == 1
    assert out["top_band"] + out["mid_band"] + out["low_band"] == 1
    assert out["top_band_share"] in (0.0, 1.0) and out["window_days"] == ev.BACKTEST_DAYS


def test_backtest_nothing_scored_has_no_share():
    assert ev.backtest([], DEFINITION, {})["top_band_share"] is None


def test_exploration_count_is_seeded_and_bounded():
    assert ev.exploration_count(0, 1, DAY) == 0
    assert ev.exploration_count(20, 1, DAY) == 2
    assert ev.exploration_count(5, 1, DAY) == ev.exploration_count(5, 1, DAY)
    assert {ev.exploration_count(5, u, DAY) for u in range(60)} == {0, 1}


def _cand(key, score):
    return SimpleNamespace(person_key=key, score=score)


def test_split_draws_from_next_lower_band_and_respects_limit():
    ranked = [_cand(f"t{i}", 90 - i) for i in range(10)] + [_cand(f"m{i}", 60 - i) for i in range(5)]
    picked, explored = ev.split_exploration(ranked, 10, 1, DAY)
    assert len(picked) == 10 and len(explored) == 1
    assert all(k.startswith("m") for k in explored)
    assert [c.person_key for c in picked[:9]] == [f"t{i}" for i in range(9)]


def test_split_no_lower_band_keeps_ranked_top():
    ranked = [_cand(f"t{i}", 90 - i) for i in range(12)]
    picked, explored = ev.split_exploration(ranked, 10, 1, DAY)
    assert explored == set() and [c.person_key for c in picked] == [f"t{i}" for i in range(10)]


def test_split_zero_slots_and_empty():
    assert ev.split_exploration([], 5, 1, DAY) == ([], set())
    ranked = [_cand("a", 90), _cand("b", 50)]
    assert ev.split_exploration(ranked, 2, 1, DAY, fraction=0)[1] == set()


def test_apply_rubric_marks_exploration_and_keeps_floor():
    names = [f"p{i}" for i in range(20)]
    signals = [CandidateSignal(person_name=n, person_profile_url=f"https://www.linkedin.com/in/{n}",
                               occurred_at=NOW) for n in names]
    ranked = rank_candidates(signals, NOW, min_icp=0)
    facts = {}
    for i, n in enumerate(names):
        title = "COO" if i < 10 else "Chief of Staff"
        facts[f"https://www.linkedin.com/in/{n}"] = {"job_title": title, "company_name": "Co",
                                                      "industry": "Software"}
    with patch("cqc_lem.utilities.connection_targeting.split_exploration",
               wraps=ev.split_exploration) as spy:
        kept, scores, _ = apply_icp_rubric(ranked, DEFINITION, facts, limit=10, user_id=1, today=DAY)
    assert spy.called and len(kept) <= 10
    marked = [c for c in kept if ev.EXPLORATION_MARK in c.reasons]
    assert all(ev.EXPLORATION_MARK in scores[c.person_key].reason for c in marked)


def test_apply_rubric_without_user_id_never_explores():
    signals = [CandidateSignal(person_name="a", person_profile_url="https://www.linkedin.com/in/a",
                               occurred_at=NOW)]
    ranked = rank_candidates(signals, NOW, min_icp=0)
    facts = {"https://www.linkedin.com/in/a": {"job_title": "COO", "company_name": "C",
                                               "industry": "Software"}}
    kept, _, _ = apply_icp_rubric(ranked, DEFINITION, facts, limit=5)
    assert len(kept) == 1 and ev.EXPLORATION_MARK not in kept[0].reasons


class TestAdminRoute:
    URL = "/api/admin/icp-evaluation"
    PARAMS = {"user_id": 1}
    HEAD = {"x-admin-secret": "s3cret"}

    def test_forbidden_without_secret(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            assert api_client.get(self.URL, params=self.PARAMS).status_code == 403

    def test_unreadable_is_503(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.utilities.db.get_icp_outcome_rows", return_value=None):
            assert api_client.get(self.URL, params=self.PARAMS, headers=self.HEAD).status_code == 503

    def test_no_definition_backtest_not_measured(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.utilities.db.get_icp_outcome_rows", return_value=[_row()]), \
             patch("cqc_lem.utilities.db.get_engagement_preferences", return_value={}):
            r = api_client.get(self.URL, params=self.PARAMS, headers=self.HEAD)
        assert r.status_code == 200
        assert r.json()["detail"]["backtest"] == ev.NOT_MEASURED

    def test_with_definition_runs_backtest(self, api_client):
        rows = [{**_row(accepted=1), "recipient_profile_url": "https://www.linkedin.com/in/a"}]
        facts = {"https://www.linkedin.com/in/a": {"job_title": "COO", "company_name": "C",
                                                   "industry": "Software"}}
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.utilities.db.get_icp_outcome_rows", return_value=rows), \
             patch("cqc_lem.utilities.db.get_engagement_preferences",
                   return_value={"icp_definition": DEFINITION.model_dump()}), \
             patch("cqc_lem.utilities.db.get_profile_facts", return_value=facts):
            r = api_client.get(self.URL, params=self.PARAMS, headers=self.HEAD)
        assert r.json()["detail"]["backtest"]["accepted"] == 1

    def test_hidden_from_published_schema(self, api_client):
        assert self.URL not in api_client.app.openapi()["paths"]
