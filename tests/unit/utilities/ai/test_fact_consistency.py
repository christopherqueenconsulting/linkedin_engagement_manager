"""Showcase round 6: dates, timelines, sign-offs and sourced research (`fact_consistency`)."""

from datetime import date, datetime

import pytest

from cqc_lem.utilities.ai import fact_consistency as fc

pytestmark = pytest.mark.unit


class TestWeekday:
    def test_the_showcase_date_is_a_monday(self):
        text = "Wednesday, June 22, 2026 the ticket board turned red."
        hits = fc.weekday_mismatches(text)
        assert [(h["stated"], h["actual"]) for h in hits] == [("Wednesday", "Monday")]

    @pytest.mark.parametrize("text", [
        "On Monday, June 22, 2026 we shipped.",
        "Mon. June 22nd 2026 was quiet.",
        "Monday the 22nd of June, 2026 was quiet.",
        "On June 22, 2026 we shipped.",            # no weekday at all
        "On Wednesday, June 22 we shipped.",       # no year: nothing to check against
        "Wednesday, February 30, 2026 is no date",  # not a date: fails open
    ])
    def test_a_correct_or_uncheckable_date_passes(self, text):
        assert fc.weekday_mismatches(text) == []

    def test_day_first_order_is_read_too(self):
        hits = fc.weekday_mismatches("It was Friday, 22 June 2026.")
        assert hits and hits[0]["actual"] == "Monday"

    def test_the_fix_drops_only_the_wrong_weekday(self):
        text = "Wednesday, June 22, 2026 the board went red. On Monday, June 22, 2026 it cleared."
        fixed, changed = fc.fix_weekday_mismatches(text)
        assert fixed == "June 22, 2026 the board went red. On Monday, June 22, 2026 it cleared."
        assert changed == ["Wednesday, June 22, 2026"]

    def test_the_fix_on_nothing(self):
        assert fc.fix_weekday_mismatches("") == ("", [])
        assert fc.fix_weekday_mismatches(None) == (None, [])
        assert fc.fix_weekday_mismatches("No dates.") == ("No dates.", [])


class TestDeadlines:
    def test_an_offer_in_february_to_finish_in_january(self):
        text = ("On February 14 2026 I offered to rebuild a small-business website for free, "
                "aiming to finish in January.")
        issues = fc.deadline_contradictions(text)
        assert len(issues) == 1 and "finish in January" in issues[0]
        assert "February 14, 2026" in issues[0]

    @pytest.mark.parametrize("text", [
        "On December 10, 2025 I offered to finish in January.",     # next month: fine
        "On February 14 2026 I agreed to finish by March.",
        "On February 14 2026 I agreed to finish in January 2027.",  # explicit later year
        "I offered to finish in January.",                          # no anchoring date
    ])
    def test_a_possible_deadline_passes(self, text):
        assert fc.deadline_contradictions(text) == []

    def test_an_explicit_earlier_year_fails(self):
        issues = fc.deadline_contradictions("On March 3, 2026 we promised delivery by May 2025.")
        assert len(issues) == 1


class TestTimeline:
    STORY = ("On October 1 2026 I worked with a seed-stage fintech.\n\nWithin the first month we "
             "mapped every touch-point and saw the cost-per-outcome ratio drop by 30 %.")

    def test_a_month_of_results_six_days_in_is_impossible(self):
        issues = fc.timeline_violations(self.STORY, date(2026, 10, 1), date(2026, 10, 7))
        assert len(issues) == 1
        assert "Within the first month" in issues[0] and "6 day(s) ago" in issues[0]

    def test_the_same_claim_months_later_is_fine(self):
        assert fc.timeline_violations(self.STORY, "2026-08-01", datetime(2026, 10, 7, 9)) == []

    @pytest.mark.parametrize("happened", [None, "", "not a date", date(2026, 12, 1)])
    def test_an_unknown_or_future_story_date_is_never_evidence(self, happened):
        assert fc.timeline_violations(self.STORY, happened, date(2026, 10, 7)) == []

    def test_counted_units_and_later(self):
        text = "Three months later our churn fell. After 2 weeks I had the answer."
        claims = fc.elapsed_claims(text)
        assert [c["days"] for c in claims] == [90, 14]
        # 19 days on: two weeks have passed, three months have not.
        issues = fc.timeline_violations(text, date(2026, 10, 1), date(2026, 10, 20))
        assert len(issues) == 1 and "Three months later" in issues[0]
        # The slack: "a week later" six days on is not a lie.
        assert fc.timeline_violations("A week later we shipped.", date(2026, 10, 1),
                                      date(2026, 10, 7)) == []

    def test_plans_and_third_party_claims_are_not_read(self):
        text = ("Most teams see results within six months. We will finish in three months. "
                "If you wait a year, your costs climb.")
        assert fc.elapsed_claims(text) == []

    def test_the_default_today_is_used(self):
        assert fc.timeline_violations("Within the first year we grew.", date.today()) != []

    def test_as_date(self):
        assert fc.as_date(datetime(2026, 1, 2, 3)) == date(2026, 1, 2)
        assert fc.as_date(date(2026, 1, 2)) == date(2026, 1, 2)
        assert fc.as_date("2026-01-02 10:00:00") == date(2026, 1, 2)
        assert fc.as_date(None) is None and fc.as_date("x") is None


class TestReport:
    def test_everything_together(self):
        text = ("Wednesday, June 22, 2026 I started. On February 14 2026 I offered to finish in "
                "January. Within the first quarter we doubled revenue.")
        report = fc.consistency_report(text, date(2026, 9, 30), date(2026, 10, 7))
        assert not report["passes"]
        assert len(report["weekday"]) == 1 and len(report["deadline"]) == 1
        assert len(report["timeline"]) == 1
        assert report["issues"] == report["weekday"] + report["deadline"] + report["timeline"]

    def test_no_story_date_skips_the_timeline(self):
        report = fc.consistency_report("Within the first quarter we doubled revenue.")
        assert report == {"passes": True, "issues": [], "weekday": [], "deadline": [],
                          "timeline": []}


class TestSignature:
    def test_a_trailing_job_title_is_cut(self):
        text = ("Would you pay for the premium tier?\n\nShare why.\n\n"
                "Senior Applied AI & Full‑Stack Engineer")
        assert fc.strip_signature_lines(text) == "Would you pay for the premium tier?\n\nShare why."

    def test_two_signature_lines_are_cut(self):
        text = "The post body.\n\nFounder, CQC Consulting\nAI Consultant | Speaker"
        assert fc.strip_signature_lines(text) == "The post body."

    @pytest.mark.parametrize("text", [
        "Body.\n\nComment AUDIT and I'll DM it to you.",
        "Body.\n\nAsk your engineer about it.",          # a sentence, lower case
        "Senior Applied AI Engineer",                     # nothing else would remain
        "Body.\n\n#AI #Engineering #Founder",
        "Body.\n\nThe tools every engineer on the team should have used from day one",
        "",
        None,
    ])
    def test_everything_else_is_left_alone(self, text):
        assert fc.strip_signature_lines(text) == text


class TestNamedSources:
    def test_only_sentences_that_name_a_source_survive(self):
        research = ("79% of enterprises still bleed money. According to Gartner, 30% of projects "
                    "fail. The report says 50%. A 2025 survey from McKinsey found 78% adoption. "
                    "Inference costs fell 13x (Stanford AI Index, 2025). Prices rose from March. "
                    "Spend rose 12% [3]. See https://example.com/data for 40%. "
                    "Per Deloitte, 61% agree. The Census Bureau data shows 9%.")
        kept = fc.named_source_material(research).splitlines()
        assert kept == ["According to Gartner, 30% of projects fail.",
                        "A 2025 survey from McKinsey found 78% adoption.",
                        "Inference costs fell 13x (Stanford AI Index, 2025).",
                        "Spend rose 12% [3].",
                        "See https://example.com/data for 40%.",
                        "Per Deloitte, 61% agree.",
                        "The Census Bureau data shows 9%."]

    def test_nothing_sourced_is_empty(self):
        assert fc.named_source_material("79% overspend. The data shows 40%.") == ""
        assert fc.named_source_material(None) == ""
