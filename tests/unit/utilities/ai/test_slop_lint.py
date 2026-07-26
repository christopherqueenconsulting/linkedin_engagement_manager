"""Deterministic AI-slop linter (issue #625 / D1).

Table-driven per check, with real-world positive/negative samples: the labeled drafts from the
authenticity rubric's golden set, the production comment samples named in the July 2026 engagement
audit ("I totally agree that...", "You raise an excellent point...", "Spot on—"), and the specific
constructions LinkedIn's May 2026 crackdown calls out. The negatives matter as much as the
positives — a linter that flags a good human draft costs a regeneration and, on the post gate, a
hold the author has to clear by hand.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.ai import slop_lint as sl
from cqc_lem.utilities.ai.authenticity_rubric import GOLDEN_SET

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"
_RCP = "cqc_lem.app.run_content_plan"
_RAU = "cqc_lem.app.run_automation"

# A clean, specific, human draft — the control every check is measured against.
_CLEAN = ("Three years ago I shipped a pricing change on a Friday afternoon and took down checkout "
          "for 40 minutes. We lost about $12k in orders. The fix wasn't technical. We added a rule "
          "that no pricing change goes out after noon on a Friday. Boring, and we haven't had a "
          "weekend outage since.")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("SLOP_LINT_ENABLED", "SLOP_LINT_POST_ENABLED", "SLOP_LINT_COMMENT_ENABLED",
                 "SLOP_LINT_DM_ENABLED", "SLOP_LINT_NEWSLETTER_ENABLED", "SLOP_LINT_LEXICON_MAX",
                 "SLOP_LINT_EM_DASH_PER_SENTENCE", "SLOP_LINT_BURSTINESS_MIN",
                 "SLOP_LINT_EMOJI_BULLET_MAX", "SLOP_LINT_MAX_ATTEMPTS", "SLOP_LINT_EXTRA_WORDS",
                 "SLOP_LINT_ALLOW_WORDS", "SLOP_LINT_EXTRA_PHRASES"):
        monkeypatch.delenv(name, raising=False)
    for check in sl.DEFAULT_SEVERITIES:
        monkeypatch.delenv(f"SLOP_LINT_SEVERITY_{check.upper()}", raising=False)


def _fired(text, check, content_type="post", **kwargs):
    """True when `check` produced a violation for `text`."""
    report = sl.lint_report(text, content_type, **kwargs)
    return check in {v["check"] for v in report["violations"]}


def _resp(text):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text))]
    return r


def _profile():
    p = MagicMock()
    p.model_dump_json.return_value = '{"full_name": "Jane", "job_title": "COO"}'
    return p


class TestBannedLexicon:
    @pytest.mark.parametrize("text", [
        "We leverage a robust ecosystem to unlock value.",
        "A transformative journey through the ever-evolving landscape.",
        "In today's fast-paced world it is important to note that we must delve deeper.",
    ])
    def test_a_pileup_of_tell_words_fires(self, text):
        assert _fired(text, sl.CHECK_LEXICON)

    @pytest.mark.parametrize("text", [
        _CLEAN,
        # One or two tells is weak evidence, not the signal — a real engineer says "optimize".
        "We optimize the query plan every quarter and it holds.",
        "We optimize the query plan and the roadmap says the same thing.",
    ])
    def test_an_isolated_tell_is_not_a_violation(self, text):
        assert not _fired(text, sl.CHECK_LEXICON)

    def test_phrases_the_wordbank_cannot_see_are_caught(self):
        hits = sl.find_banned_lexicon("Let's dive in. In conclusion, needless to say, buckle up.")
        assert {"let's dive in", "in conclusion", "needless to say", "buckle up"} <= set(hits)

    def test_the_ceiling_is_configurable(self, monkeypatch):
        text = "We leverage a robust ecosystem."
        assert _fired(text, sl.CHECK_LEXICON)
        monkeypatch.setenv("SLOP_LINT_LEXICON_MAX", "5")
        assert not _fired(text, sl.CHECK_LEXICON)
        monkeypatch.setenv("SLOP_LINT_LEXICON_MAX", "0")
        assert _fired("We optimize it.", sl.CHECK_LEXICON)

    def test_the_lexicon_extends_and_shrinks_by_env(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_EXTRA_WORDS", "flywheel,northstar")
        assert "flywheel" in sl.banned_words() and "northstar" in sl.banned_words()
        # A word that is slop in general prose can be a term of art in one author's niche.
        monkeypatch.setenv("SLOP_LINT_ALLOW_WORDS", "optimize,leverage")
        assert "optimize" not in sl.banned_words()
        assert "leverage" not in sl.banned_words()

    def test_extra_phrases_extend_the_phrase_list(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_EXTRA_PHRASES", "at the end of the day")
        assert "at the end of the day" in sl.banned_phrases()
        assert "at the end of the day" in sl.find_banned_lexicon(
            "At the end of the day the numbers held.")


class TestContrastiveFrame:
    @pytest.mark.parametrize("text", [
        "It's not just about managing, it's about empowering people.",
        "Success isn't a destination. It's a journey.",
        "This isn't only a tooling problem, it's a staffing one.",
        "Not only did we ship it, but also we cut the bill in half.",
    ])
    def test_the_named_frame_fires(self, text):
        assert _fired(text, sl.CHECK_CONTRASTIVE)

    @pytest.mark.parametrize("text", [
        _CLEAN,
        "We did not ship on Friday. The rule held.",
        "It's a staffing problem and we treated it as one.",
        "That is not the number I expected from the migration.",
    ])
    def test_ordinary_negation_is_left_alone(self, text):
        assert not _fired(text, sl.CHECK_CONTRASTIVE)


class TestTadaTransitions:
    @pytest.mark.parametrize("text", [
        "We cut build time by 40%. Here's the kicker: nobody noticed.",
        "We rewrote the importer. Plot twist, it got slower.",
        "We halved the bill. The result? Nobody asked for it back.",
        "Let that sink in for a second.",
    ])
    def test_manufactured_beats_fire(self, text):
        assert _fired(text, sl.CHECK_TADA)

    @pytest.mark.parametrize("text", [
        _CLEAN,
        "The result was a 40% drop in build time, measured over two weeks.",
        "Here is the migration plan we actually used.",
    ])
    def test_ordinary_prose_is_left_alone(self, text):
        assert not _fired(text, sl.CHECK_TADA)


class TestBaitCloser:
    @pytest.mark.parametrize("closer", ["Thoughts?", "Agree?", "Am I wrong?", "Who's with me?",
                                        "What do you think?", "Sound familiar?"])
    def test_reflex_closers_fire(self, closer):
        assert _fired(f"We moved the fast movers to the front aisle. {closer}",
                      sl.CHECK_BAIT_CLOSER)

    def test_the_same_question_mid_text_is_a_rhetorical_beat_not_bait(self):
        text = ("Agree? That was the whole argument, and it was wrong. We measured pick times for "
                "six weeks and the layout change carried all of it.")
        assert not _fired(text, sl.CHECK_BAIT_CLOSER)

    def test_a_specific_closing_question_is_not_bait(self):
        text = ("We moved the fast movers to the front aisle and pick times fell 18%. "
                "How long did your fast movers stay stable after the swap?")
        assert not _fired(text, sl.CHECK_BAIT_CLOSER)

    def test_reflex_action_asks_fire(self):
        assert _fired("Tag a friend who needs to read this.", sl.CHECK_BAIT_CLOSER)
        assert _fired("Follow for more takes like this.", sl.CHECK_BAIT_CLOSER)

    def test_a_sanctioned_lead_magnet_cta_is_exempt(self):
        # The SAME exemption strip_engagement_bait makes: a configured trigger word that happens to
        # collide with the bait regex is still the user's own lead-magnet mechanic.
        cta = ("Six months of testing taught us where acquisition cost hides.\n\n"
               "Comment YES and I'll DM you the checklist we used.")
        assert _fired(cta, sl.CHECK_BAIT_CLOSER)
        assert not _fired(cta, sl.CHECK_BAIT_CLOSER, exempt_keyword="YES")

    def test_the_exemption_only_covers_the_line_carrying_the_keyword(self):
        text = ("Comment YES for the checklist.\n"
                "Also tag a friend who needs this.")
        assert _fired(text, sl.CHECK_BAIT_CLOSER, exempt_keyword="YES")

    def test_closing_reflex_ask_reports_the_phrase(self):
        assert sl.closing_reflex_ask("We shipped it. Thoughts?") == "thoughts"
        assert sl.closing_reflex_ask(_CLEAN) is None
        assert sl.closing_reflex_ask("") is None


class TestEmojiBullets:
    def test_an_emoji_listicle_fires(self):
        text = ("5 ways AI will change your business:\n"
                "\U0001F680 Boost productivity\n\U0001F916 Automate everything\n"
                "\U0001F4C8 Drive growth\n\U0001F4A1 Innovate faster")
        assert _fired(text, sl.CHECK_EMOJI_BULLETS)

    def test_a_couple_of_emoji_lines_is_under_the_ceiling(self):
        text = "We shipped it.\n\U0001F680 Faster builds\n\U0001F4C8 Lower bill"
        assert not _fired(text, sl.CHECK_EMOJI_BULLETS)

    def test_the_ceiling_is_configurable(self, monkeypatch):
        text = "Out now.\n\U0001F680 One\n\U0001F4C8 Two"
        monkeypatch.setenv("SLOP_LINT_EMOJI_BULLET_MAX", "1")
        assert _fired(text, sl.CHECK_EMOJI_BULLETS)


class TestEmDashDensity:
    def test_dash_riddled_prose_fires(self):
        text = "We shipped it — fast — and it broke — twice."
        assert _fired(text, sl.CHECK_EM_DASH)

    def test_one_dash_in_a_long_draft_is_fine(self):
        assert not _fired(_CLEAN + " The rule — still in place — costs nothing.",
                          sl.CHECK_EM_DASH)

    def test_the_double_hyphen_variant_counts_too(self):
        assert sl.em_dash_density("A -- b -- c.") > 0
        assert sl.em_dash_density("") == 0.0

    def test_it_is_advisory_not_a_hold(self):
        report = sl.lint_report("We shipped it — fast — and it broke — twice.")
        assert report["passes"] is True
        assert sl.CHECK_EM_DASH in {v["check"] for v in report["warnings"]}


class TestRuleOfThree:
    @pytest.mark.parametrize("text", [
        "Great leaders inspire their teams to be faster, smarter, and better.",
        "It has to be scalable, maintainable, and testable.",
    ])
    def test_rhythm_triads_fire(self, text):
        assert _fired(text, sl.CHECK_RULE_OF_THREE)

    @pytest.mark.parametrize("text", [
        # A genuine list of three things is not a rule-of-three tell.
        "We run Postgres, Redis, and Docker in the same compose file.",
        "The team is Ana, Marcus, and Priya.",
        _CLEAN,
    ])
    def test_real_lists_are_left_alone(self, text):
        assert not _fired(text, sl.CHECK_RULE_OF_THREE)

    def test_find_rule_of_three_returns_the_excerpt(self):
        assert sl.find_rule_of_three("faster, smarter, better") == ["faster, smarter, better"]


class TestBurstiness:
    def test_uniform_sentence_lengths_fire(self):
        text = " ".join(["The team shipped the change on time again."] * 6)
        assert _fired(text, sl.CHECK_BURSTINESS)

    def test_varied_prose_passes(self):
        text = ("It broke. We spent the entire weekend rebuilding the pricing importer from the "
                "audit log, one row at a time, because nobody had a snapshot. Twice. The fix was a "
                "single line in the deploy script that nobody had thought to test before shipping. "
                "Boring work.")
        assert not _fired(text, sl.CHECK_BURSTINESS)

    def test_short_drafts_are_not_graded(self):
        # A two-sentence comment has no meaningful sentence-length variance to measure.
        assert sl.burstiness("One short line. Another short line.") is None
        assert not _fired("One short line. Another short line.", sl.CHECK_BURSTINESS)


class TestRhetoricalHook:
    @pytest.mark.parametrize("text", [
        "Ever wonder why most migrations slip? We measured ours for six weeks.",
        "What if onboarding was the retention lever? We tested it on 11 hires.",
        "Did you know most teams never read the post-mortem? We stopped writing them.",
    ])
    def test_stock_question_openers_fire(self, text):
        assert _fired(text, sl.CHECK_RHETORICAL_HOOK)

    @pytest.mark.parametrize("text", [
        _CLEAN,
        "How long did your fast movers stay stable? That is the number I care about.",
    ])
    def test_a_real_question_opener_is_left_alone(self, text):
        assert not _fired(text, sl.CHECK_RHETORICAL_HOOK)


class TestReportShape:
    def test_a_clean_draft_passes_with_nothing_recorded(self):
        report = sl.lint_report(_CLEAN)
        assert report["passes"] is True
        assert report["hard"] == [] and report["checked"] is True

    def test_hard_and_warn_violations_are_separated(self):
        text = ("In today's fast-paced world we leverage a robust ecosystem to unlock growth. "
                "It's not just software, it's a mindset. Thoughts?")
        report = sl.lint_report(text)
        assert report["passes"] is False
        assert {v["check"] for v in report["hard"]} >= {sl.CHECK_LEXICON, sl.CHECK_CONTRASTIVE,
                                                       sl.CHECK_BAIT_CLOSER}
        assert all(v["severity"] == sl.SEVERITY_WARN for v in report["warnings"])
        assert len(report["reasons"]) == len(report["violations"])

    def test_every_violation_carries_an_explainable_reason(self):
        report = sl.lint_report("Here's the kicker: it's not X, it's Y. Agree?")
        for v in report["violations"]:
            assert v["detail"] and isinstance(v["detail"], str)
            assert v["check"] and v["severity"] in (sl.SEVERITY_HARD, sl.SEVERITY_WARN)

    @pytest.mark.parametrize("text", [None, "", "   "])
    def test_empty_text_is_passing_and_unchecked(self, text):
        report = sl.lint_report(text)
        assert report["passes"] is True and report["checked"] is False

    def test_the_master_switch_fails_open(self, monkeypatch):
        bad = "It's not X, it's Y. Thoughts?"
        assert sl.lint_report(bad)["passes"] is False
        monkeypatch.setenv("SLOP_LINT_ENABLED", "off")
        report = sl.lint_report(bad)
        assert report["passes"] is True and report["checked"] is False
        assert sl.passes_slop_lint(bad) is True

    def test_one_surface_can_be_disabled_without_the_others(self, monkeypatch):
        bad = "It's not X, it's Y. Thoughts?"
        monkeypatch.setenv("SLOP_LINT_COMMENT_ENABLED", "off")
        assert sl.lint_report(bad, "comment")["checked"] is False
        assert sl.lint_report(bad, "post")["passes"] is False

    def test_severity_is_overridable_per_check(self, monkeypatch):
        text = "We shipped it — fast — and it broke — twice."
        assert sl.lint_report(text)["passes"] is True
        monkeypatch.setenv("SLOP_LINT_SEVERITY_EM_DASH_DENSITY", "hard")
        assert sl.lint_report(text)["passes"] is False
        monkeypatch.setenv("SLOP_LINT_SEVERITY_EM_DASH_DENSITY", "off")
        assert sl.lint_report(text)["violations"] == []

    def test_a_junk_severity_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_SEVERITY_BANNED_LEXICON", "sort-of")
        assert sl.check_severity(sl.CHECK_LEXICON) == sl.SEVERITY_HARD

    def test_max_attempts_is_bounded_and_live(self, monkeypatch):
        assert sl.slop_max_attempts() == sl.MAX_ATTEMPTS_DEFAULT
        monkeypatch.setenv("SLOP_LINT_MAX_ATTEMPTS", "99")
        assert sl.slop_max_attempts() == 5
        monkeypatch.setenv("SLOP_LINT_MAX_ATTEMPTS", "junk")
        assert sl.slop_max_attempts() == sl.MAX_ATTEMPTS_DEFAULT

    def test_junk_thresholds_fall_back_to_the_defaults(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_BURSTINESS_MIN", "junk")
        monkeypatch.setenv("SLOP_LINT_EM_DASH_PER_SENTENCE", "")
        assert sl.burstiness_min() == sl.BURSTINESS_MIN_DEFAULT
        assert sl.em_dash_per_sentence_max() == sl.EM_DASH_PER_SENTENCE_DEFAULT


class TestGoldenSet:
    """The rubric's labeled drafts (issue #405) are the calibration target: the linter must catch
    the generic ones and must NOT demote the good AI-assisted-but-personal ones."""

    @pytest.mark.parametrize("draft", [d for d in GOLDEN_SET if d["label"] == "generic"],
                             ids=lambda d: d["id"])
    def test_generic_drafts_are_caught(self, draft):
        assert sl.lint_report(draft["text"])["passes"] is False

    @pytest.mark.parametrize("draft", [d for d in GOLDEN_SET if d["label"] != "generic"],
                             ids=lambda d: d["id"])
    def test_authentic_and_ai_assisted_drafts_are_not(self, draft):
        report = sl.lint_report(draft["text"])
        assert report["passes"] is True, report["reasons"]


class TestProductionCommentSamples:
    """The comments the July 2026 audit sampled out of production (logs 89-96)."""

    @pytest.mark.parametrize("comment", [
        "I totally agree that layout beats software. It's not just tooling, it's culture. Thoughts?",
        "You raise an excellent point. Here's the thing: most teams never measure it. Agree?",
        "Spot on — this is a game changer for the ever-evolving logistics landscape. Am I wrong?",
    ])
    def test_the_audit_samples_are_caught(self, comment):
        assert sl.lint_report(comment, "comment")["passes"] is False

    def test_the_replacement_shape_passes(self):
        good = ("The front-aisle move is the part most layout posts skip. We tried it across two "
                "warehouses last year and pick times fell 18% before we touched the software. "
                "How long did the fast movers stay stable for you?")
        assert sl.lint_report(good, "comment")["passes"] is True


class TestPerformance:
    def test_a_long_draft_lints_well_under_50ms(self):
        draft = (_CLEAN + "\n\n") * 12          # ~3.5k chars, well past a real LinkedIn post
        start = time.perf_counter()
        for _ in range(20):
            sl.lint_report(draft)
        per_call_ms = (time.perf_counter() - start) * 1000 / 20
        assert per_call_ms < 50, f"{per_call_ms:.1f}ms per lint"


class TestRetryDirective:
    def test_it_names_every_violation_and_bans_invention(self):
        report = sl.lint_report("It's not X, it's Y. Here's the kicker: nobody cares. Thoughts?")
        text = sl.slop_retry_directive(report["hard"])
        assert "AI-SLOP LINT" in text
        for v in report["hard"]:
            assert v["detail"] in text
        assert "Do NOT invent facts" in text

    def test_no_violations_means_no_directive(self):
        assert sl.slop_retry_directive([]) == ""
        assert sl.slop_retry_directive(None) == ""

    def test_violation_reasons_tolerates_junk(self):
        assert sl.violation_reasons([{"check": "x"}, None, "nope"]) == []


class TestQualityGateFinding:
    def test_the_finding_carries_the_reasons_and_holds_the_post(self):
        from cqc_lem.utilities.quality_gates import slop_finding, GATE_SLOP, demoting_findings
        finding = slop_finding(["contrastive_frame: uses the frame"], ["burstiness: flat"])
        assert finding["gate"] == GATE_SLOP
        assert finding["demoted"] is True
        assert demoting_findings([finding]) == [finding]
        assert "contrastive_frame: uses the frame" in finding["details"]
        assert any(d.startswith("(advisory)") for d in finding["details"])

    def test_it_survives_the_persistence_round_trip(self):
        import json
        from cqc_lem.utilities.quality_gates import slop_finding, parse_gate_findings
        raw = json.dumps([slop_finding(["a reason"])])
        assert parse_gate_findings(raw)[0]["gate"] == "ai_slop"


class TestPostGateWiring:
    def _gates(self, content, **kwargs):
        from cqc_lem.app import run_content_plan as rcp
        with patch(f"{_RCP}._post_missing_required_asset", return_value=False):
            return rcp.evaluate_post_gates(7, content, "text", **kwargs)

    def test_a_slopped_post_is_held_with_the_patterns_named(self):
        findings = self._gates("It's not just code, it's a mindset. Thoughts?")
        slop = [f for f in findings if f["gate"] == "ai_slop"]
        assert slop and slop[0]["demoted"] is True
        assert any("contrastive_frame" in d for d in slop[0]["details"])

    def test_a_clean_post_is_not_held(self):
        assert [f for f in self._gates(_CLEAN) if f["gate"] == "ai_slop"] == []

    def test_the_lead_magnet_cta_does_not_hold_the_post(self):
        cta = ("Six months of testing taught us where acquisition cost hides.\n\n"
               "Comment YES and I'll DM you the checklist we used.")
        assert [f for f in self._gates(cta) if f["gate"] == "ai_slop"] != []
        assert [f for f in self._gates(cta, cta_keyword="YES") if f["gate"] == "ai_slop"] == []

    def test_warn_only_violations_never_hold_a_post(self):
        assert [f for f in self._gates("We shipped it — fast — and it broke — twice.")
                if f["gate"] == "ai_slop"] == []


class TestCommentGenerationWiring:
    """generate_ai_response spends the SAME retry budget on slop and then skips the post."""

    _POST = ("We cut our warehouse pick times by moving the fast movers to the front aisle. "
             "Nobody talks about how much layout beats software here.")
    _GOOD = ("The front-aisle move is the part most layout posts skip. We tried it across two "
             "warehouses last year and pick times fell 18% before we touched the software. "
             "How long did the fast movers stay stable for you?")
    _SLOPPED = ("The front-aisle move is the part most layout posts skip. We tried it and pick "
                "times fell 18%. Here's the kicker: nobody measured it. Thoughts?")

    def test_a_slopped_draft_regenerates_with_the_lint_directive(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", side_effect=[_resp(self._SLOPPED), _resp(self._GOOD)]) as m:
            out = ai_helper.generate_ai_response(self._POST, _profile())
        assert out == self._GOOD
        assert m.call_count == 2
        retry_system = m.call_args_list[1].kwargs["messages"][0]["content"]
        assert "tada_transition" in retry_system or "ta-da" in retry_system

    def test_a_comment_that_never_clears_the_lint_is_skipped(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(self._SLOPPED)), \
             patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_ai_response(self._POST, _profile())
        assert out is None                          # never ships a slopped comment
        assert "skipping this post" in warn.call_args.args[0]

    def test_the_lint_can_be_turned_off_for_comments(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_COMMENT_ENABLED", "off")
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=_resp(self._SLOPPED)) as m:
            out = ai_helper.generate_ai_response(self._POST, _profile())
        assert out == self._SLOPPED and m.call_count == 1


class TestShortFormRepair:
    """lint_repaired is the shared bounded-regeneration helper for the surfaces with no review
    queue — a still-slopped draft ships with a warning rather than breaking the sequence."""

    def test_a_passing_draft_costs_no_extra_call(self):
        from cqc_lem.utilities.ai import ai_helper
        redraft = MagicMock()
        assert ai_helper.lint_repaired(_CLEAN, "post", redraft) == _CLEAN
        redraft.assert_not_called()

    def test_a_slopped_draft_is_regenerated_once(self):
        from cqc_lem.utilities.ai import ai_helper
        redraft = MagicMock(return_value=_CLEAN)
        out = ai_helper.lint_repaired("It's not X, it's Y. Thoughts?", "post", redraft)
        assert out == _CLEAN
        assert redraft.call_count == 1
        assert "AI-SLOP LINT" in redraft.call_args.args[0]

    def test_a_still_slopped_draft_ships_with_a_warning(self):
        from cqc_lem.utilities.ai import ai_helper
        bad = "It's not X, it's Y. Thoughts?"
        with patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.lint_repaired(bad, "dm", MagicMock(return_value=bad))
        assert out == bad
        assert "AI-slop lint" in warn.call_args.args[0]

    def test_a_failed_regeneration_keeps_the_original(self):
        from cqc_lem.utilities.ai import ai_helper
        bad = "It's not X, it's Y. Thoughts?"
        with patch(f"{_AI}.log_warning"):
            assert ai_helper.lint_repaired(bad, "dm", MagicMock(return_value=None)) == bad

    def test_an_empty_draft_is_returned_untouched(self):
        from cqc_lem.utilities.ai import ai_helper
        redraft = MagicMock()
        assert ai_helper.lint_repaired(None, "dm", redraft) is None
        redraft.assert_not_called()

    def test_the_attempt_budget_is_configurable(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_MAX_ATTEMPTS", "3")
        from cqc_lem.utilities.ai import ai_helper
        bad = "It's not X, it's Y. Thoughts?"
        redraft = MagicMock(return_value=bad)
        with patch(f"{_AI}.log_warning"):
            ai_helper.lint_repaired(bad, "dm", redraft)
        assert redraft.call_count == 2      # initial draft + 2 regenerations = 3 total


class TestNewsletterWiring:
    def _edition(self, body):
        import json
        return _resp(json.dumps({"title": "A specific title", "subtitle": "why to read it",
                                 "subject": "the subject", "body": body}))

    def test_a_slopped_edition_is_regenerated_whole(self):
        from cqc_lem.utilities.ai import ai_helper
        slopped = "It's not just tooling, it's a mindset. Thoughts?"
        clean_body = _CLEAN
        with patch(f"{_AI}._call_llm",
                   side_effect=[self._edition(slopped), self._edition(clean_body)]) as m:
            out = ai_helper.generate_newsletter_edition(_profile(), topic="ops")
        assert out["body"] == clean_body
        # opening_line is derived from the body — regenerating only the body would leave it stale.
        assert out["opening_line"] == clean_body.splitlines()[0]
        assert m.call_count == 2

    def test_a_still_slopped_edition_is_kept_for_review(self):
        from cqc_lem.utilities.ai import ai_helper
        slopped = "It's not just tooling, it's a mindset. Thoughts?"
        with patch(f"{_AI}._call_llm", return_value=self._edition(slopped)), \
             patch(f"{_AI}.log_warning") as warn:
            out = ai_helper.generate_newsletter_edition(_profile(), topic="ops")
        assert out["body"] == slopped
        assert "AI-slop lint" in warn.call_args.args[0]

    def test_a_clean_edition_costs_one_call(self):
        from cqc_lem.utilities.ai import ai_helper
        with patch(f"{_AI}._call_llm", return_value=self._edition(_CLEAN)) as m:
            out = ai_helper.generate_newsletter_edition(_profile(), topic="ops")
        assert out["body"] == _CLEAN and m.call_count == 1


class TestDmWiring:
    def test_a_slopped_dm_is_re_refined_then_sent(self):
        from cqc_lem.app import run_automation as rau
        slopped = "It's not just a tool, it's a mindset. Thoughts?"
        with patch(f"{_RAU}.get_dm_template", return_value={"template_text": "Hi {first_name}"}), \
             patch(f"{_RAU}.get_ai_message_refinement", side_effect=[slopped, "Nice to meet you."]), \
             patch(f"{_RAU}.humanize_text", side_effect=lambda t, **k: t):
            out = rau.build_dm_from_template(1, "connection", "Ana", _profile())
        assert out == "Nice to meet you."

    def test_a_still_slopped_dm_is_sent_rather_than_dropped(self):
        from cqc_lem.app import run_automation as rau
        slopped = "It's not just a tool, it's a mindset. Thoughts?"
        with patch(f"{_RAU}.get_dm_template", return_value={"template_text": "Hi {first_name}"}), \
             patch(f"{_RAU}.get_ai_message_refinement", return_value=slopped), \
             patch(f"{_RAU}.humanize_text", side_effect=lambda t, **k: t), \
             patch(f"{_AI}.log_warning"):
            out = rau.build_dm_from_template(1, "connection", "Ana", _profile())
        # Dropping it would silently break the outreach sequence.
        assert out == slopped
