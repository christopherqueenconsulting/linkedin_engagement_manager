"""Pins scripts/sample_newsletter_scaffolds.py — the #1285 corpus sampler.

The script produces the number a severity decision is made on, so the failure mode that matters is a
confidently wrong measurement: a hit rate computed over a corpus too small to mean anything, a
candidate phrase list that recycles phrases already banned, or a summary that silently counts posts
as newsletters.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/sample_newsletter_scaffolds.py")

SCAFFOLDED = "In today's edition, without further ado, three ideas about pricing."
CLEAN = ("Last March a client's edition went out at 6am and nobody opened it. We moved the send to "
         "Tuesday noon; opens went from 11% to 28%.")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("sample_newsletter_scaffolds", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _edition(ref_id, text, user_id=1):
    return {"surface": "newsletter", "ref_id": ref_id, "text": text, "shipped_on": "2026-08-01",
            "user_id": user_id}


class TestEditionReport:
    def test_reports_the_hits_and_the_severity_the_linter_would_apply(self, tool):
        report = tool.edition_report(_edition("7", SCAFFOLDED))
        assert "in today's edition" in report["hits"]
        assert "without further ado" in report["newsletter_hits"]
        assert report["severity"] == "hard"      # issue #1285: newsletter scaffolds hold the draft
        assert report["lint_passes"] is False

    def test_a_clean_edition_reports_nothing(self, tool):
        report = tool.edition_report(_edition("8", CLEAN))
        assert report["hits"] == []
        assert report["severity"] is None

    def test_an_empty_body_is_not_a_hit(self, tool):
        assert tool.edition_report({"ref_id": "9", "text": None})["hits"] == []


class TestSummarize:
    def test_hit_rate_counts_editions_not_phrases(self, tool):
        summary = tool.summarize([_edition("1", SCAFFOLDED), _edition("2", CLEAN)])
        # The first edition carries TWO phrases; the rate is still 1 of 2 editions.
        assert summary["editions"] == 2
        assert summary["editions_with_scaffold"] == 1
        assert summary["hit_rate"] == 0.5
        assert summary["would_hold"] == 1

    def test_a_small_corpus_is_flagged_as_insufficient(self, tool):
        summary = tool.summarize([_edition("1", SCAFFOLDED)])
        assert summary["sufficient_corpus"] is False
        assert summary["min_corpus"] >= 20
        big = tool.summarize([_edition(str(i), CLEAN) for i in range(tool.MIN_CORPUS)])
        assert big["sufficient_corpus"] is True

    def test_an_empty_corpus_reports_no_rate_rather_than_zero(self, tool):
        summary = tool.summarize([])
        assert summary["editions"] == 0
        assert summary["hit_rate"] is None

    def test_phrase_counts_and_unused_phrases_name_the_dead_entries(self, tool):
        summary = tool.summarize([_edition("1", SCAFFOLDED), _edition("2", SCAFFOLDED)])
        assert summary["phrase_counts"]["in today's edition"] == 2
        assert "long story short" in summary["unused_phrases"]
        assert "in today's edition" not in summary["unused_phrases"]

    def test_telemetry_rows_from_other_surfaces_are_excluded(self, tool):
        scores = [{"surface": "post", "slop_hard": 3, "slop_warn": 1},
                  {"surface": "newsletter", "slop_hard": 1, "slop_warn": 2},
                  {"surface": "newsletter", "slop_hard": 0, "slop_warn": 0}]
        summary = tool.summarize([_edition("1", CLEAN)], scores)
        assert summary["telemetry_rows"] == 2
        assert summary["telemetry_with_hard"] == 1
        assert summary["telemetry_with_warn"] == 1

    def test_a_null_slop_column_is_not_counted_as_a_hit(self, tool):
        # #630 writes NULL for "not measured"; counting it as 0 would be fine, counting it as a
        # hit would not.
        summary = tool.summarize([], [{"surface": "newsletter", "slop_hard": None,
                                       "slop_warn": None}])
        assert summary["telemetry_with_hard"] == 0
        assert summary["telemetry_with_warn"] == 0


class TestCandidatePhrases:
    def test_a_phrase_repeated_across_editions_is_a_candidate(self, tool):
        line = "the one number I always check first is churn"
        candidates = [c["phrase"] for c in tool.candidate_phrases([line + " today", line + " again"])]
        assert any("the one number i always check" in c for c in candidates)

    def test_a_phrase_in_one_edition_only_is_not_a_candidate(self, tool):
        candidates = tool.candidate_phrases(["a run of words that appears exactly once here",
                                             "an entirely different sentence about pricing"])
        assert candidates == []

    def test_already_banned_phrases_are_never_re_proposed(self, tool):
        phrases = [c["phrase"] for c in tool.candidate_phrases([SCAFFOLDED, SCAFFOLDED])]
        assert not any("in today's edition" in p for p in phrases)
        assert not any("without further ado" in p for p in phrases)

    def test_a_shorter_run_inside_a_longer_one_is_reported_once(self, tool):
        body = "we ship one edition every single tuesday morning without exception"
        phrases = [c["phrase"] for c in tool.candidate_phrases([body, body])]
        # The 6-word run subsumes its 4- and 5-word prefixes seen in the same editions.
        assert len(phrases) == len(set(phrases))
        assert not any(a != b and a in b for a in phrases for b in phrases)

    def test_the_shortlist_is_bounded(self, tool):
        body = " ".join(f"word{i}" for i in range(400))
        assert len(tool.candidate_phrases([body, body])) <= tool.CANDIDATE_LIMIT


class TestCollect:
    def test_only_newsletter_rows_are_sampled(self, tool, monkeypatch):
        import cqc_lem.utilities.db as db

        monkeypatch.setattr(db, "get_shipped_content_for_quality", lambda user_id, days: [
            {"surface": "post", "ref_id": "p1", "text": SCAFFOLDED},
            {"surface": "newsletter", "ref_id": "n1", "text": SCAFFOLDED},
        ])
        monkeypatch.setattr(db, "get_content_quality_scores", lambda user_id, days: [])
        summary = tool.collect([1], 30)
        assert summary["editions"] == 1
        assert summary["per_edition"][0]["ref_id"] == "n1"

    def test_every_requested_user_is_read(self, tool, monkeypatch):
        import cqc_lem.utilities.db as db

        seen = []

        def _shipped(user_id, days):
            seen.append(user_id)
            return [{"surface": "newsletter", "ref_id": f"n{user_id}", "text": CLEAN}]

        monkeypatch.setattr(db, "get_shipped_content_for_quality", _shipped)
        monkeypatch.setattr(db, "get_content_quality_scores", lambda user_id, days: [])
        summary = tool.collect([1, 2], 30)
        assert seen == [1, 2]
        assert summary["editions"] == 2


class TestCli:
    def test_json_mode_emits_a_parseable_summary(self, tool, monkeypatch, capsys):
        import json

        import cqc_lem.utilities.db as db

        monkeypatch.setattr(db, "get_shipped_content_for_quality", lambda user_id, days: [
            {"surface": "newsletter", "ref_id": "n1", "text": SCAFFOLDED}])
        monkeypatch.setattr(db, "get_content_quality_scores", lambda user_id, days: [])
        assert tool.main(["--users", "1", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["editions"] == 1
        assert payload["current_severity"] == "hard"

    def test_the_text_report_names_the_corpus_shortfall(self, tool, monkeypatch, capsys):
        import cqc_lem.utilities.db as db

        monkeypatch.setattr(db, "get_shipped_content_for_quality", lambda user_id, days: [
            {"surface": "newsletter", "ref_id": "n1", "text": SCAFFOLDED}])
        monkeypatch.setattr(db, "get_content_quality_scores", lambda user_id, days: [])
        assert tool.main(["--users", "1"]) == 0
        out = capsys.readouterr().out
        assert "NOT ENOUGH" in out
        assert "in today's edition" in out

    def test_an_empty_corpus_still_exits_zero(self, tool, monkeypatch, capsys):
        import cqc_lem.utilities.db as db

        monkeypatch.setattr(db, "get_shipped_content_for_quality", lambda user_id, days: [])
        monkeypatch.setattr(db, "get_content_quality_scores", lambda user_id, days: [])
        assert tool.main(["--users", "1"]) == 0
        assert "Editions sampled          : 0" in capsys.readouterr().out
