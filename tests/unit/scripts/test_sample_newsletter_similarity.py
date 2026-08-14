"""Pins scripts/sample_newsletter_similarity.py — the #1433 corpus sampler.

The script exists to stop a threshold being picked from a corpus that cannot support one, so the
failure modes that matter are the ones that would read as a calibration: a `sufficient_corpus` that
says yes on one account, a distribution that pools cosine scores with token-overlap scores, and a
single-edition account reported as perfectly unique rather than as unmeasured.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/sample_newsletter_similarity.py")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("sample_newsletter_similarity", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user(user_id, editions, body=None, title=None):
    return {"user_id": user_id, "editions": editions, "body": body or {}, "title": title or {}}


class TestDistribution:
    def test_reports_the_shape_of_a_score_set(self, tool):
        shape = tool.distribution([0.6, 0.7, 0.8, 0.9])
        assert shape["sample"] == 4 and shape["min"] == 0.6 and shape["max"] == 0.9
        assert shape["median"] == 0.7 and shape["mean"] == 0.75

    def test_an_empty_set_is_unmeasured_not_zero(self, tool):
        shape = tool.distribution([])
        assert shape["sample"] == 0
        assert all(shape[key] is None for key in ("min", "median", "mean", "p90", "max"))

    def test_percentiles_come_from_the_corpus_never_between_two_editions(self, tool):
        # Nearest-rank: every reported percentile is a score some edition actually got.
        shape = tool.distribution([0.10, 0.90])
        assert shape["median"] in (0.1, 0.9) and shape["p90"] in (0.1, 0.9)

    def test_a_percentile_landing_on_a_boundary_does_not_depend_on_the_corpus_parity(self, tool):
        # p25 of four editions is exactly rank 1. Python rounds halves to EVEN, so the previous
        # `round(f*n + 0.5)` reported the SECOND-lowest here and the lowest for five editions.
        assert tool.distribution([0.10, 0.20, 0.30, 0.40])["p25"] == 0.1
        assert tool.distribution([0.10, 0.20, 0.30, 0.40])["p75"] == 0.3
        assert tool.distribution([0.10, 0.20, 0.30, 0.40, 0.50])["p25"] == 0.2


def _fake_reports(monkeypatch, tool, reports):
    """Replace the batched embedding reader and record what it was handed."""
    calls: list = []

    def _reports(texts, history=None):
        calls.append((list(texts), list(history or [])))
        return list(reports)

    monkeypatch.setattr(tool, "similarity_reports", _reports)
    return calls


class TestMeasureTexts:
    def test_uses_the_nightly_passs_own_leave_one_out_reader(self, tool, monkeypatch):
        calls = _fake_reports(monkeypatch, tool,
                              [{"score": 0.8, "measure": "embedding", "match": "b"},
                               {"score": 0.8, "measure": "embedding", "match": "a"}])
        scores = tool.measure_texts(["a", "b"])
        # Same list as items AND history — `similarity_reports` drops an item's own text, so this is
        # the leave-one-out maximum the telemetry records.
        assert calls == [(["a", "b"], ["a", "b"])]
        assert [r["score"] for r in scores] == [0.8, 0.8]

    def test_one_edition_has_nothing_to_be_similar_to(self, tool, monkeypatch):
        calls = _fake_reports(monkeypatch, tool, [])
        assert tool.measure_texts(["only edition"]) == []
        assert calls == []

    def test_unmeasured_reports_are_dropped_rather_than_scored_zero(self, tool, monkeypatch):
        _fake_reports(monkeypatch, tool, [{"score": None, "measure": "none", "match": None},
                                          {"score": 0.7, "measure": "embedding", "match": "a"}])
        assert [r["score"] for r in tool.measure_texts(["a", "b"])] == [0.7]

    def test_blank_editions_never_reach_the_embedding_call(self, tool, monkeypatch):
        calls = _fake_reports(monkeypatch, tool, [])
        tool.measure_texts(["a body", "   ", None, "another body"])
        assert calls == [(["a body", "another body"], ["a body", "another body"])]


class TestSummarize:
    def test_one_account_can_never_be_a_sufficient_corpus(self, tool):
        summary = tool.summarize([_user(1, 40, body={"embedding": [0.8] * 40})])
        assert summary["editions"] == 40
        assert summary["accounts_with_a_comparison"] == 1
        assert summary["sufficient_corpus"] is False

    def test_two_thin_accounts_are_not_a_corpus_either(self, tool):
        summary = tool.summarize([_user(1, 3, body={"embedding": [0.8] * 3}),
                                  _user(2, 4, body={"embedding": [0.7] * 4})])
        assert summary["sufficient_corpus"] is False

    def test_both_floors_met_is_the_only_yes(self, tool):
        summary = tool.summarize([_user(1, 12, body={"embedding": [0.8] * 12}),
                                  _user(2, 12, body={"embedding": [0.7] * 12})])
        assert summary["editions"] == 24 and summary["accounts_with_a_comparison"] == 2
        assert summary["sufficient_corpus"] is True

    def test_an_account_with_no_comparison_does_not_count_as_an_account(self, tool):
        # A second account with one edition adds rows and zero information.
        summary = tool.summarize([_user(1, 30, body={"embedding": [0.8] * 30}), _user(2, 1)])
        assert summary["editions"] == 31
        assert summary["accounts_with_a_comparison"] == 1
        assert summary["sufficient_corpus"] is False

    def test_the_two_scales_are_reported_separately(self, tool):
        summary = tool.summarize([_user(1, 10, body={"embedding": [0.8, 0.8]}),
                                  _user(2, 10, body={"lexical": [0.4, 0.4]})])
        body = summary["surfaces"]["body"]
        assert body["embedding"]["mean"] == 0.8 and body["embedding"]["sample"] == 2
        assert body["lexical"]["mean"] == 0.4 and body["lexical"]["sample"] == 2

    def test_titles_are_measured_as_their_own_surface(self, tool):
        summary = tool.summarize([_user(1, 4, body={"embedding": [0.8]},
                                        title={"embedding": [0.6, 0.5]})])
        assert summary["surfaces"]["title"]["embedding"]["sample"] == 2
        assert summary["surfaces"]["title"]["embedding"]["mean"] == 0.55

    def test_the_post_ceilings_ride_along_as_a_reference(self, tool):
        from cqc_lem.utilities.ai.content_framework import post_embedding_similarity_max, post_similarity_max
        reference = tool.summarize([])["post_reference"]
        assert reference["embedding"] == post_embedding_similarity_max()
        assert reference["lexical"] == post_similarity_max()


class TestRender:
    def test_a_thin_corpus_says_so_before_it_says_a_number(self, tool):
        text = tool.render(tool.summarize([_user(1, 5, body={"embedding": [0.8, 0.9]})]))
        assert "NOT ENOUGH" in text
        assert text.index("NOT ENOUGH") < text.index("Body self-similarity")

    def test_a_sufficient_corpus_reports_without_the_refusal(self, tool):
        text = tool.render(tool.summarize([_user(1, 12, body={"embedding": [0.8] * 12}),
                                           _user(2, 12, body={"embedding": [0.7] * 12})]))
        assert "NOT ENOUGH" not in text
        assert "reference, not a verdict" in text


class TestCollect:
    def test_reads_both_readers_through_the_facade_over_one_corpus(self, tool, monkeypatch):
        from cqc_lem.utilities import db
        reads: list = []

        def _bodies(user_id, limit):
            reads.append(("body", user_id, limit))
            return ["body one", "body two"]

        def _titles(user_id, limit):
            reads.append(("title", user_id, limit))
            return ["title one", "title two"]

        monkeypatch.setattr(db, "get_recent_newsletter_bodies", _bodies)
        monkeypatch.setattr(db, "get_recent_newsletter_titles", _titles)
        _fake_reports(monkeypatch, tool, [{"score": 0.8, "measure": "embedding", "match": "x"},
                                          {"score": 0.6, "measure": "embedding", "match": "y"}])
        summary = tool.collect([1], limit=50)
        # Same account, same window: bodies and titles must describe the SAME editions.
        assert reads == [("body", 1, 50), ("title", 1, 50)]
        assert summary["per_user"][0]["editions"] == 2
        assert summary["surfaces"]["body"]["embedding"]["sample"] == 2
        assert summary["sufficient_corpus"] is False

    def test_the_window_is_the_one_the_nightly_pass_compares_against(self, tool):
        from cqc_lem.utilities.ai.content_framework import COMMENT_HISTORY_LIMIT
        # `similarity_reports` truncates the history pool to COMMENT_HISTORY_LIMIT, so a wider read
        # would score the older editions against a window nothing else uses.
        assert tool.EDITIONS_PER_USER == COMMENT_HISTORY_LIMIT

    def test_an_oversized_limit_is_clamped_rather_than_silently_truncated(self, tool, monkeypatch,
                                                                         capsys):
        asked: list = []

        def _collect(ids, limit):
            asked.append(limit)
            return tool.summarize([_user(1, 2)])

        monkeypatch.setattr(tool, "collect", _collect)
        tool.main(["--users", "1", "--limit", "500"])
        tool.main(["--users", "1", "--limit", "0"])
        capsys.readouterr()
        assert asked == [tool.EDITIONS_PER_USER, 1]

    def test_main_exits_zero_on_a_corpus_too_small_to_calibrate(self, tool, monkeypatch, capsys):
        monkeypatch.setattr(tool, "collect", lambda ids, limit: tool.summarize([_user(1, 2)]))
        assert tool.main(["--users", "1"]) == 0
        assert "NOT ENOUGH" in capsys.readouterr().out

    def test_json_output_is_the_raw_summary(self, tool, monkeypatch, capsys):
        import json
        monkeypatch.setattr(tool, "collect", lambda ids, limit: tool.summarize(
            [_user(1, 2, body={"embedding": [0.8, 0.8]})]))
        tool.main(["--users", "1", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["sufficient_corpus"] is False and payload["editions"] == 2
