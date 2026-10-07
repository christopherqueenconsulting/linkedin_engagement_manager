"""The post image rhythm (#2241, anti-monotony round).

Treatment rotation, card share, the sameness gate, opinion detection and the verbatim pull-quote.
Pure — the one LLM call is mocked.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import post_treatment as pt
from cqc_lem.utilities.ai.image_concept import POST_LAYOUTS, SHOTS, ImageConcept

pytestmark = pytest.mark.unit

T, P, D, Q = (pt.TREATMENT_TYPESET_CARD, pt.TREATMENT_PHOTO_ONLY, pt.TREATMENT_DATA_CARD,
              pt.TREATMENT_QUOTE_CARD)


def _concept(**overrides) -> ImageConcept:
    base = dict(thesis="Most founders hire a content team when nobody owns the calendar.",
                audience="founders", specific_entities=(), emotional_beat="wry resolve",
                hook_phrase="Nobody owns the calendar", treatment="editorial_concept",
                treatment_rationale="", valence="positive", hook_shape="plain_claim",
                layout="split_top", shot="")
    base.update(overrides)
    return ImageConcept(**base)


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                    finish_reason="stop")])


# --- LRU ----------------------------------------------------------------------------------------

def test_lru_puts_unused_first_then_the_one_used_longest_ago():
    assert pt.lru_order(("a", "b", "c"), ["a"]) == ["b", "c", "a"]
    assert pt.lru_order(("a", "b", "c"), ["c", "a", "b"]) == ["b", "a", "c"]
    # Values outside the options are ignored, not counted.
    assert pt.lru_order(("a", "b"), ["zzz", "b"]) == ["a", "b"]


def test_lru_with_no_history_rotates_by_seed_and_is_stable():
    first = pt.lru_order(("a", "b", "c"), [], seed="one thesis")
    assert first == pt.lru_order(("a", "b", "c"), [], seed="one thesis")
    assert sorted(first) == ["a", "b", "c"]
    starts = {pt.lru_order(("a", "b", "c"), [], seed=s)[0] for s in ("x", "xy", "xyz")}
    assert len(starts) > 1, "different pieces start in different places"
    assert pt.lru_order((), ["a"]) == []


# --- Card share ---------------------------------------------------------------------------------

def _simulate_cards(share: float, n: int = 10) -> list[str]:
    """Feed each decision back as history, the way receipts accumulate."""
    history: list[str] = []
    for _ in range(n):
        history.insert(0, T if pt.wants_card(history, share) else P)
    return list(reversed(history))


@pytest.mark.parametrize("share,expected", [(0.4, 4), (0.0, 0), (0.5, 5), (1.0, 10), (0.2, 2)])
def test_card_share_holds_over_a_ten_post_window(share, expected):
    assert _simulate_cards(share).count(T) == expected


def test_card_share_spreads_cards_rather_than_bunching_them():
    assert _simulate_cards(0.4) == [P, T, P, T, P, P, T, P, T, P]


def test_card_share_is_a_rolling_window_not_a_lifetime_count():
    old_cards = [T] * 20
    assert not pt.wants_card(old_cards, 0.4), "nine recent cards already exceed 40%"
    assert pt.wants_card([P] * 9 + [T] * 20, 0.4), "only the last nine count"


def test_card_share_out_of_range_is_clamped():
    assert pt.wants_card([], 5.0) and not pt.wants_card([T], -1)


# --- Treatment plan and its fallbacks -----------------------------------------------------------

def test_a_due_card_leads_and_the_rest_follow_least_recently_used():
    plan = pt.plan_treatments([P], 0.4, {})
    assert plan.card_wanted and plan.chain[0] == T
    assert plan.chain[1:] == (D, Q, P), "unused first, the just-used photo last"


def test_no_card_due_puts_the_least_recently_used_other_treatment_first():
    plan = pt.plan_treatments([D, T, P, T], 0.4, {})
    assert not plan.card_wanted
    assert plan.chain == (Q, P, D, T)


@pytest.mark.parametrize("missing,reason", [(D, "no verified thesis stat"),
                                            (Q, "not an opinion post")])
def test_an_unsatisfiable_treatment_falls_to_the_next_and_says_why(missing, reason):
    plan = pt.plan_treatments([T, P], 0.4, {missing: reason})
    assert missing not in plan.chain
    assert (missing, reason) in plan.skipped
    assert plan.chain[0] in (D, Q) and plan.chain[0] != missing


def test_every_fallback_ends_at_a_treatment_that_always_renders():
    plan = pt.plan_treatments([], 0.0, {D: "no stat", Q: "not opinion"})
    assert set(plan.chain) == {P, T}
    assert len(plan.skipped) == 2


def test_the_sameness_gate_rerolls_a_third_typeset_card_even_at_full_share():
    plan = pt.plan_treatments([T, T], 1.0, {D: "no stat", Q: "not opinion"})
    assert plan.card_wanted, "the share asked for a card"
    assert plan.rerolled and plan.chain[0] == P, "the gate outranks the share"
    assert plan.chain[-1] == T, "the card stays as the fallback"


def test_next_treatment_skips_one_that_would_run_a_third_time():
    assert pt.next_treatment([P, D], [P, P]) == D
    assert pt.next_treatment([P], [P, P]) == P, "a single option has nothing to re-roll to"
    assert pt.next_treatment([], [P]) is None


# --- The sameness gate --------------------------------------------------------------------------

def test_repeats_run_needs_the_last_two_to_match():
    assert pt.repeats_run(["a", "a", "b"], "a")
    assert not pt.repeats_run(["a", "b", "a"], "a")
    assert not pt.repeats_run(["a"], "a"), "one in a row is not a run"
    assert not pt.repeats_run([None, None], None), "an unrecorded dimension never repeats"


def test_gate_value_keeps_a_fine_pick_and_rerolls_a_third_repeat():
    assert pt.gate_value("split_top", POST_LAYOUTS, ["split_bottom", "split_top"]) == (
        "split_top", False)
    assert pt.gate_value("split_top", POST_LAYOUTS, ["split_top", "split_top"]) == (
        "split_bottom", True)
    shot, rerolled = pt.gate_value(SHOTS[0], SHOTS, [SHOTS[0], SHOTS[0], SHOTS[1]])
    assert rerolled and shot not in (SHOTS[0],)
    assert pt.gate_value("", SHOTS, ["", ""]) == ("", False)


def test_pick_dimension_is_lru_through_the_gate():
    assert pt.pick_dimension(("a", "b", "c"), ["a", None, "b"]) == ("c", False)
    assert pt.sameness_pick(["a", "b"], ["a", "a"]) == ("b", True)
    assert pt.sameness_pick([], ["a"]) == (None, False)


# --- Opinion and the quote ----------------------------------------------------------------------

def test_opinion_comes_from_stage_one_signals_or_stance_markers():
    assert pt.opinion_signal(_concept(hook_shape="contrast"), "x") == "contrast hook"
    negative = _concept(valence="negative", thesis="Late invoices are starving small agencies.")
    assert "valence" in pt.opinion_signal(negative, "plain report.")
    assert "stance marker" in pt.opinion_signal(_concept(), "I think pricing is the lever.")
    assert pt.opinion_signal(_concept(), "We shipped the release on Tuesday.") == ""
    assert pt.opinion_signal(None, "I think so.") == ""
    assert pt.opinion_signal(_concept(thesis=""), "I think so.") == ""


_POST = ("Most founders think they need a bigger content team. They don't. "
         "I think the real problem is that nobody owns the calendar. "
         "Give one person the calendar and the output doubles! "
         "Want proof? Read this: https://example.com/case. "
         "We cut costs by $3.5M in 2025 with one rule, and nobody missed the old way. "
         "This sentence is far too long to ever be a pull quote because it keeps going and going "
         "well past the hundred and sixty character limit that the card can set at a legible size. "
         "Hashtags are not part of any quote at all. #content #founders")


def test_quote_candidates_are_complete_short_verbatim_and_link_free():
    candidates = pt.quote_candidates(_POST, "nobody owns the calendar")
    assert 0 < len(candidates) <= pt.MAX_QUOTE_CANDIDATES
    for sentence in candidates:
        assert sentence in _POST, "verbatim: an exact substring of the post"
        assert len(sentence) <= 160 and sentence.endswith((".", "!"))
        assert "#" not in sentence and "http" not in sentence
    assert not any(s.endswith("?") for s in candidates), "a question is not a pull-quote"
    assert "They don't." not in candidates, "too short to stand alone"
    assert any("$3.5M" in s for s in candidates), "a decimal point does not end a sentence"
    assert candidates[0].startswith("I think the real problem"), "stance + thesis rank first"


def test_quote_candidates_refuse_emoji_and_cap_at_five():
    text = " ".join(f"Sentence number {w} carries a complete thought here." for w in
                    ("one", "two", "three", "four", "five", "six", "seven"))
    assert len(pt.quote_candidates(text)) == 5
    assert pt.quote_candidates("This sentence has an emoji right here 🚀 at the end.") == []
    assert pt.quote_candidates("") == []


def test_the_verbatim_check_is_an_exact_substring():
    assert pt.is_verbatim("nobody owns the calendar.", "So nobody owns the calendar. Fix it.")
    assert not pt.is_verbatim("Nobody owns the calendar.", "So nobody owns the calendar.")
    assert not pt.is_verbatim("", "anything")


def test_pick_quote_one_candidate_spends_no_call():
    with patch("cqc_lem.utilities.ai.client.client.chat.completions.create") as create:
        assert pt.pick_quote(["Only one."]) == ("Only one.", "only")
        assert pt.pick_quote([]) == ("", "none")
    create.assert_not_called()


def test_pick_quote_is_one_llm_tiebreak():
    with patch("cqc_lem.utilities.ai.client.client.chat.completions.create",
               return_value=_resp(json.dumps({"index": 2}))) as create:
        assert pt.pick_quote(["First one.", "Second one."], "thesis") == ("Second one.", "llm")
    create.assert_called_once()
    assert create.call_args.kwargs["model"] == "lem-simple"


@pytest.mark.parametrize("reply", [json.dumps({"index": 9}), json.dumps({"index": True}),
                                   "not json", RuntimeError("proxy down")])
def test_pick_quote_failures_keep_the_deterministic_best(reply):
    effect = {"side_effect": reply} if isinstance(reply, Exception) else {
        "return_value": _resp(reply)}
    with patch("cqc_lem.utilities.ai.client.client.chat.completions.create", **effect):
        assert pt.pick_quote(["First one.", "Second one."]) == ("First one.", "deterministic")


# --- History and the whole plan -----------------------------------------------------------------

def test_legacy_receipts_read_as_the_charcoal_composite_they_were():
    receipts = [
        {"rhythm": {"treatment": P, "layout": None, "panel": None, "shot": None,
                    "grade": "warm_dusk"}, "concept": {}},
        {"hook_text": "Old headline", "concept": {"layout": "split_top", "shot": SHOTS[1],
                                                  "cast": {"gender": "woman"}}},
        {"concept": None},
    ]
    history = pt.rhythm_history(receipts)
    assert history["treatment"] == [P, T, P]
    assert history["panel"] == [None, "charcoal", None]
    assert history["layout"] == [None, "split_top", None]
    assert history["grade"] == ["warm_dusk", None, None]
    assert history["shot"] == [None, SHOTS[1], None]
    assert history["cast"] == [None, {"gender": "woman"}, None]


def test_unavailable_treatments_name_their_reason():
    assert pt.unavailable_treatments(None, "Jane", [], "") == {
        D: "no Stage 1 concept", Q: "no Stage 1 concept"}
    stat = {"stat": {"display": "38%"}}
    assert D not in pt.unavailable_treatments(_concept(graphic=stat), "Jane", ["x."], "marker")
    assert "headline" in pt.unavailable_treatments(_concept(graphic=stat, hook_phrase=""),
                                                   "Jane", [], "")[D]
    assert "quotable" in pt.unavailable_treatments(_concept(), "Jane", [], "marker")[Q]
    assert "byline" in pt.unavailable_treatments(_concept(), "", ["A sentence."], "marker")[Q]


def test_plan_post_rhythm_decides_every_dimension():
    history = {"treatment": [T, T], "panel": ["charcoal", "gold"], "grade": ["daylight"],
               "layout": ["split_top", "split_top"], "shot": []}
    rhythm = pt.plan_post_rhythm(_concept(), _POST, history, 1.0,
                                 ("charcoal", "off_white", "gold"), byline="Jane Doe")
    assert rhythm.plan.chain[0] != T, "a third typeset card in a row is re-rolled"
    assert rhythm.panel == "off_white", "the unused variant"
    assert rhythm.grade in ("cool_interior", "warm_dusk")
    assert rhythm.layout == "split_bottom", "Stage 1's split_top would be a third in a row"
    assert {"treatment", "layout"} <= set(rhythm.rerolled)
    assert rhythm.opinion and rhythm.quote_candidates


def test_plan_post_rhythm_without_a_concept_still_plans():
    rhythm = pt.plan_post_rhythm(None, "Plain text.", {}, 0.4, ())
    assert set(rhythm.plan.chain) == {P, T}
    assert rhythm.panel == "charcoal" and rhythm.layout == "" and rhythm.shot == ""


def test_a_six_post_sequence_never_runs_a_dimension_three_times():
    """Six posts fed back as receipts: no dimension repeats more than twice in a row."""
    concepts = [_concept(graphic={"stat": {"display": "38%"}}, layout="split_top",
                         shot=SHOTS[0]) for _ in range(6)]
    texts = [_POST, "We shipped it.", _POST, "We shipped it.", _POST, "We shipped it."]
    history = {dim: [] for dim in pt.RHYTHM_DIMENSIONS}
    for concept, text in zip(concepts, texts):
        rhythm = pt.plan_post_rhythm(concept, text, history, 1.0,
                                     ("charcoal", "off_white", "gold"), byline="Jane")
        treatment = rhythm.plan.chain[0]
        record = {"treatment": treatment, "layout": rhythm.layout,
                  "panel": rhythm.panel if treatment in (T, D) else None,
                  "shot": rhythm.shot, "grade": rhythm.grade if treatment in (T, P) else None,
                  "setting": (pt.setting_class(rhythm.setting or concept.setting) or None)
                  if treatment in (T, P) else None,
                  "style": pt.style_of(treatment), "card_layout": None}
        for dim in pt.RHYTHM_DIMENSIONS:
            history[dim].insert(0, record[dim])
    for dim, values in history.items():
        values = list(reversed(values))
        runs = [values[i:i + 3] for i in range(len(values) - 2)]
        assert not any(r[0] and r.count(r[0]) == 3 for r in runs), (dim, values)
    assert len(set(history["treatment"])) >= 3, history["treatment"]
