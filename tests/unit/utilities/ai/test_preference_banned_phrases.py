"""Phrases the author bans BY NAME in `comment_style` are HARD on the comment surfaces (issue #2113).

The owner's `comment_style` reads `... No buzzwords (i.e "hits home") ...`, yet 13 of 102 shipped
comments said "hit(s) home" or "game-changer": the ban lived only in the prompt, and no check graded
the finished draft against it.
"""

import os
from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import slop_lint as sl
from cqc_lem.utilities.ai.content_alignment import OWN_POST_COMMENT

pytestmark = pytest.mark.unit

_AI = "cqc_lem.utilities.ai.ai_helper"

# The owner's real preference, as quoted by the 2026-09 system audit.
OWNER_PREFS = {"comment_style": (
    "Lead with a specific point from their post. Add one concrete insight from real LLM-ops "
    'experience. No buzzwords (i.e "hits home"). Keep it to two sentences, never pitch.')}

SHIPPED = ("Your point on per-alias health checks hits home. We moved ours behind the router and "
           "stopped paging on transient 502s.")
CLEAN = ("Per-alias health checks saved us too. We moved ours behind the router and stopped paging "
         "on transient 502s.")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in [n for n in os.environ if n.startswith("SLOP_LINT_")]:
        monkeypatch.delenv(name, raising=False)


def _fired(text, content_type="comment", prefs=OWNER_PREFS):
    report = sl.lint_report(text, content_type, prefs=prefs)
    return [v for v in report["violations"] if v["check"] == sl.CHECK_PREFERENCE_BANNED]


class TestPreferenceBannedPhrases:
    def test_the_owners_own_wording_bans_hits_home(self):
        assert sl.preference_banned_phrases(OWNER_PREFS)[0] == "hits home"

    def test_a_quote_without_a_ban_cue_is_guidance_not_a_ban(self):
        prefs = {"comment_style": 'Open with "what I would add". Avoid "game of thrones" jokes.'}
        phrases = sl.preference_banned_phrases(prefs)
        assert "what i would add" not in phrases
        assert "game of thrones" in phrases

    def test_a_ban_cue_in_an_earlier_sentence_does_not_carry_over(self):
        prefs = {"comment_style": 'Never pitch. Open with "here is what I saw".'}
        assert "here is what i saw" not in sl.preference_banned_phrases(prefs)

    @pytest.mark.parametrize("style, wanted", [
        ('Keep it short, no emojis, and open with "what I would add".', "what i would add"),
        ('Never pitch, always ask "what did you try?"', "what did you try"),
    ])
    def test_a_ban_cue_before_a_comma_does_not_carry_over(self, style, wanted):
        assert wanted not in sl.preference_banned_phrases({"comment_style": style})

    @pytest.mark.parametrize("style, banned", [
        ('No buzzwords, e.g. "hits home" or "synergy".', ["hits home", "synergy"]),
        ('Avoid "circle back", "deep dive", "move the needle".',
         ["circle back", "deep dive", "move the needle"]),
        ('Sound like a peer, not "a fan".', ["a fan"]),
        ('Skip clichés like "at the end of the day".', ["at the end of the day"]),
    ])
    def test_a_comma_before_a_quote_or_example_keeps_the_ban(self, style, banned):
        phrases = sl.preference_banned_phrases({"comment_style": style})
        assert all(b in phrases for b in banned)

    def test_smart_and_single_quotes_are_read(self):
        prefs = {"comment_style": "Don’t say “deep dive” or 'low-hanging fruit'; don't gush."}
        phrases = sl.preference_banned_phrases(prefs)
        assert "deep dive" in phrases
        assert "low-hanging fruit" in phrases

    def test_a_wordless_quote_bans_nothing(self):
        prefs = {"comment_style": 'Never use "--" or "..."'}
        assert not _fired(CLEAN, prefs=prefs)

    @pytest.mark.parametrize("prefs", [None, {}, {"comment_style": None}, {"comment_style": 7}])
    def test_no_style_bans_only_the_built_in_list(self, prefs):
        assert sl.preference_banned_phrases(prefs) == list(sl.COMMENT_BANNED_PHRASES)


class TestMatching:
    @pytest.mark.parametrize("text", [
        "That hits home for me.", "That hit home.", "This was a game-changer.",
        "Honestly a Game Changer.", "Routers are game-changers here.",
    ])
    def test_inflections_and_hyphens_match(self, text):
        assert _fired(text)

    @pytest.mark.parametrize("text", [
        "He hit homeruns all summer.", "We shipped it home before the game.", CLEAN,
    ])
    def test_near_misses_do_not(self, text):
        assert not _fired(text)


class TestSeverity:
    @pytest.mark.parametrize("surface", ["comment", OWN_POST_COMMENT])
    def test_hard_on_both_comment_surfaces(self, surface):
        report = sl.lint_report(SHIPPED, surface, prefs=OWNER_PREFS)
        assert not report["passes"]
        hard = [v for v in report["hard"] if v["check"] == sl.CHECK_PREFERENCE_BANNED]
        assert hard and hard[0]["evidence"] == ["hits home"]
        assert '"hits home"' in hard[0]["detail"]

    @pytest.mark.parametrize("surface", ["post", "dm", "newsletter"])
    def test_off_everywhere_else(self, surface):
        assert not _fired(SHIPPED, surface)

    def test_ops_can_demote_it(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_SEVERITY_PREFERENCE_BANNED_PHRASE_COMMENT", "warn")
        assert sl.lint_report(SHIPPED, "comment", prefs=OWNER_PREFS)["passes"]

    def test_a_users_own_ban_is_graded(self):
        prefs = {"comment_style": 'Never write "north star".'}
        assert _fired("Latency is our north star metric.", prefs=prefs)
        assert not _fired("Latency is our north star metric.", prefs=None)


class TestWiring:
    def _gate(self, drafts, prefs=OWNER_PREFS):
        from cqc_lem.utilities.ai import ai_helper

        fixes = []

        def _draft(fix=""):
            fixes.append(fix)
            return drafts[len(fixes) - 1]

        with patch(f"{_AI}._framework.comment_contract_report",
                   return_value={"passes": True, "failures": []}), \
             patch(f"{_AI}._framework.comment_similarity_report",
                   return_value={"too_similar": False}), \
             patch(f"{_AI}._framework.comment_gate_max_attempts", return_value=2):
            out = ai_helper._gated_comment(_draft, "post body", prefs=prefs)
        return out, fixes

    def test_the_feed_gate_skips_a_comment_that_keeps_using_a_banned_phrase(self):
        out, fixes = self._gate([SHIPPED, SHIPPED])
        assert out is None
        assert "hits home" in fixes[1]

    def test_the_feed_gate_ships_the_clean_regeneration(self):
        out, _ = self._gate([SHIPPED, CLEAN])
        assert out == CLEAN

    def test_lint_repaired_steers_the_regeneration_with_the_prefs(self):
        from cqc_lem.utilities.ai import ai_helper

        directives = []

        def _redraft(directive):
            directives.append(directive)
            return CLEAN

        with patch(f"{_AI}.track_slop_retry"):
            out = ai_helper.lint_repaired(SHIPPED, "comment", _redraft, prefs=OWNER_PREFS)
        assert out == CLEAN
        assert "hits home" in directives[0]
