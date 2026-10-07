"""Showcase round 4 engine rules, as pure functions.

Story cooldown, plain fold, CTA rotation, topic diversity, deck counts, document wording and
bolted-on industry claims. The gauntlet ran the real ``create_content`` path on user 1's next ten
slots and an independent critic failed both monotony and engagement readiness: seven story-bank
entries told ~16 times,
engineer copy for a small-business audience, "Comment AUDIT" on most posts, "4 Steps" on a
three-step deck. Each rule here is deterministic, so each is pinned exactly.
"""

import pytest

from cqc_lem.utilities.ai import content_framework as cf, slop_lint as sl, story_bank as sb

pytestmark = pytest.mark.unit


# --- Story cooldown -------------------------------------------------------------------------------

# Seven entries, each with two checkable particulars of its own so a post that tells it is
# recognisable (`entry_echoed_in`), and none sharing particulars with another.
STORIES = [
    {"id": i, "kind": "anecdote", "title": f"Story {i}",
     "body": f"In month {i}1 we cut {i}7 hours from the weekly close.", "used_count": 12 + i,
     "active": True}
    for i in range(1, 8)
]


def _told(entry: dict) -> str:
    return f"A post retelling: {sb.entry_text(entry)}"


class TestStoryCooldown:
    def test_seven_stories_over_ten_slots_never_repeat_inside_the_window(self):
        posts: list = []  # newest first, as the window reads them
        anchors = []
        for _slot in range(10):
            window = posts[:sb.STORY_COOLDOWN_POSTS]
            story = sb.select_story(STORIES, recent_texts=window[:sb.STORY_RECENT_POSTS],
                                    cooldown_texts=window)
            anchors.append(story["id"] if story else None)
            posts.insert(0, _told(story) if story else "A research post about BLS wage data.")
        told = [a for a in anchors if a is not None]
        assert len(told) == 7 and len(set(told)) == 7
        assert anchors[7:] == [None, None, None]

    def test_a_story_comes_back_once_it_leaves_the_window(self):
        window = [_told(STORIES[0])] + ["filler"] * (sb.STORY_COOLDOWN_POSTS - 1)
        later = ["filler"] * sb.STORY_COOLDOWN_POSTS
        assert sb.select_story([STORIES[0]], cooldown_texts=window) is None
        assert sb.select_story([STORIES[0]], cooldown_texts=later)["id"] == 1

    def test_without_a_cooldown_window_the_soft_rotation_still_repeats(self):
        # Comments pass no window, so the least-used entry still anchors them.
        recent = [_told(e) for e in STORIES]
        assert sb.select_story(STORIES, recent_texts=recent)["id"] == 1

    def test_cooling_ids_reads_the_window(self):
        assert sb.cooling_ids(STORIES, [_told(STORIES[2]), _told(STORIES[4])]) == {3, 5}
        assert sb.cooling_ids(STORIES, []) == set()

    def test_the_cooldown_directive_forbids_the_authors_stories_and_credits_items(self):
        items = [{"publisher": "U.S. Bureau of Labor Statistics", "title": "Wages rose 0.3%",
                  "excerpt": "Average hourly earnings rose 0.3% in September."},
                 {"publisher": "One Useful Thing", "title": "", "excerpt": "skipped: no title"}]
        directive = sb.cooldown_story_directive(items)
        assert "NO STORY THIS TIME" in directive
        assert "no first-person claims" in directive
        assert "OUTSIDE ITEM (U.S. Bureau of Labor Statistics): Wages rose 0.3%" in directive
        assert "skipped" not in directive

    def test_the_cooldown_directive_without_items_points_at_research_alone(self):
        directive = sb.cooldown_story_directive([])
        assert "research findings above:" in directive and "OUTSIDE ITEM" not in directive

    def test_at_most_three_items_ride_into_the_prompt(self):
        items = [{"publisher": f"P{i}", "title": f"T{i}"} for i in range(6)]
        assert sb.cooldown_story_directive(items).count("OUTSIDE ITEM") == sb.COOLDOWN_CURATED_ITEMS


class TestIndustryClaims:
    BANK = ["I built a LinkedIn automation system that shipped 160 releases in 32 days."]

    def test_a_bolted_on_industry_in_the_authors_story_is_flagged(self):
        draft = ("One e-commerce story taught me that routing saves thousands. "
                 "Our e-commerce platform now runs on cheap models.")
        assert sb.unsourced_industry_claims(draft, self.BANK) == ["e-commerce"]

    def test_an_industry_the_bank_or_profile_states_passes(self):
        draft = "My retail clients cut returns by a third."
        assert sb.unsourced_industry_claims(draft, ["Consultant to retail owners"]) == []

    def test_a_third_person_research_sentence_is_not_a_claim(self):
        assert sb.unsourced_industry_claims("Healthcare spending rose again this year.",
                                            self.BANK) == []

    def test_a_client_sentence_counts_even_without_first_person(self):
        assert sb.unsourced_industry_claims("A SaaS client saw churn fall.", self.BANK) == ["saas"]


# --- The plain fold ------------------------------------------------------------------------------

class TestFoldJargon:
    def test_jargon_above_the_fold_is_found(self):
        post = ("Our MySQL VARCHAR(64) column silently rolled back a deploy.\n\n"
                "It took a cron job to notice.\n\nBelow the fold the API detail is fine.")
        assert cf.fold_jargon_hits(post) == ["MySQL", "VARCHAR", "deploy", "cron job"]

    def test_jargon_below_the_fold_is_allowed(self):
        post = "One setting quietly cost a client 4 hours a week.\n\nIt saved them $300.\n\nA cron job."
        assert cf.fold_jargon_hits(post) == []

    @pytest.mark.parametrize("text", ["A rag rug and a pr agency.", "Our apiary sold honey."])
    def test_acronyms_only_match_as_written_and_whole(self, text):
        assert cf.fold_jargon_hits(text) == []

    def test_an_uppercase_acronym_is_jargon(self):
        assert cf.fold_jargon_hits("Your next PR fixes the API.") == ["PR", "API"]

    @pytest.mark.parametrize("prefs,synthesis,expected", [
        ({"business_goals": "Help small-business owners save hours"}, None, True),
        ({"focus_topics": ["AI for SMBs"]}, None, True),
        ({}, "A consultant to local businesses and solopreneurs", True),
        ({}, {"audience": "Main Street retailers"}, True),
        ({"business_goals": "Hire senior platform engineers"}, "A staff engineer", False),
        (None, None, False),
    ])
    def test_the_smb_audience_is_read_from_goals_topics_and_voice(self, prefs, synthesis, expected):
        assert cf.smb_audience(prefs, synthesis) is expected

    def test_the_blueprint_carries_the_fold_rule_only_when_switched_on(self):
        bp = {"format": "personal_lesson", "hook_style": "micro_story",
              "cta_style": "reply_question"}
        assert "FOLD RULE" not in cf.blueprint_directive("post", bp)
        assert "FOLD RULE" in cf.blueprint_directive("post", {**bp, "plain_fold": True})

    def test_a_deck_gets_the_fold_rule_and_its_rotated_close(self):
        bp = {"format": "personal_lesson"}
        plain = cf.carousel_blueprint_directive(bp)
        assert "FOLD RULE" not in plain and "CAPTION CTA STYLE" not in plain
        steered = cf.carousel_blueprint_directive(
            {**bp, "plain_fold": True, "cta_type": cf.CTA_TYPE_SHARE, "cta_style": "share_one"})
        assert "FOLD RULE" in steered and "COVER slide" in steered
        assert "CAPTION CTA STYLE: Pass It To One Person" in steered

    def test_the_slop_check_fires_only_for_a_plain_fold_author(self):
        post = "The cron job behind our MySQL backup failed.\n\nHere is what it cost."
        assert not any(v["check"] == sl.CHECK_FOLD_JARGON
                       for v in sl.lint_report(post, "post")["violations"])
        report = sl.lint_report(post, "post", plain_fold=True)
        jargon = [v for v in report["hard"] if v["check"] == sl.CHECK_FOLD_JARGON]
        assert jargon and jargon[0]["evidence"] == ["cron job", "MySQL"]
        assert "rewrite ONLY the first two lines" in jargon[0]["detail"]

    def test_a_plain_fold_passes_the_check(self):
        post = "A late invoice cost a bakery $900 last month.\n\nHere is the fix."
        assert not [v for v in sl.lint_report(post, "post", plain_fold=True)["violations"]
                    if v["check"] == sl.CHECK_FOLD_JARGON]


# --- CTA rotation ---------------------------------------------------------------------------------

class TestCtaType:
    @pytest.mark.parametrize("close,expected", [
        ("Comment AUDIT and I'll DM you the checklist.", cf.CTA_TYPE_ARTIFACT),
        ("Send me a message and I'll share the template.", cf.CTA_TYPE_DM),
        ("Save this for the next time a vendor quotes you.", cf.CTA_TYPE_SAVE),
        ("Know someone who runs payroll by hand? Send this to them.", cf.CTA_TYPE_SHARE),
        ("Which would you cut first, the tool or the hours?", cf.CTA_TYPE_QUESTION),
        ("That is the whole trick.", cf.CTA_TYPE_NONE),
    ])
    def test_the_close_is_classified(self, close, expected):
        assert cf.cta_type_of(f"Opening line.\n\nThe middle.\n\n{close}") == expected

    def test_a_hashtag_line_is_not_the_close(self):
        assert cf.cta_type_of("Body.\n\nWhat would you change?\n\n#smallbusiness #ai") == \
            cf.CTA_TYPE_QUESTION

    def test_an_empty_post_has_no_cta(self):
        assert cf.cta_type_of("") == cf.CTA_TYPE_NONE

    def test_the_promo_slot_always_closes_on_its_artifact(self):
        assert cf.select_cta_type([cf.CTA_TYPE_ARTIFACT], promo=True) == cf.CTA_TYPE_ARTIFACT

    def test_a_non_promo_post_never_gets_the_artifact_or_a_dm_by_default(self):
        picks = {cf.select_cta_type([], sequence_index=i) for i in range(12)}
        assert cf.CTA_TYPE_ARTIFACT not in picks and cf.CTA_TYPE_DM not in picks

    def test_the_dm_ask_is_offered_only_where_policy_allows(self):
        recent = [cf.CTA_TYPE_QUESTION, cf.CTA_TYPE_SAVE, cf.CTA_TYPE_SHARE, cf.CTA_TYPE_NONE]
        assert cf.select_cta_type(recent, allow_dm=True) == cf.CTA_TYPE_DM

    def test_never_the_same_type_twice_in_three_posts(self):
        history: list = []
        for post_id in range(30):
            pick = cf.select_cta_type(history, sequence_index=post_id)
            assert pick not in history[:cf.CTA_TYPE_WINDOW - 1]
            history.insert(0, pick)

    def test_the_question_type_rotates_its_sub_style(self):
        styles = {cf.assign_cta_style({"format": "x"}, cf.CTA_TYPE_QUESTION, i)["cta_style"]
                  for i in range(4)}
        assert styles == {"reply_question", "challenge", "debate", "poll_prompt"}

    def test_assigning_a_type_sets_its_style_and_keeps_the_input(self):
        bp = {"format": "x", "cta_style": "reply_question"}
        out = cf.assign_cta_style(bp, cf.CTA_TYPE_NONE)
        assert out["cta_style"] == "no_ask" and out["cta_type"] == cf.CTA_TYPE_NONE
        assert bp == {"format": "x", "cta_style": "reply_question"}
        assert cf.assign_cta_style(bp, cf.CTA_TYPE_ARTIFACT)["cta_style"] == "reply_question"
        assert cf.assign_cta_style(None, cf.CTA_TYPE_SAVE) is None

    def test_a_no_ask_close_drops_the_reply_driving_rule(self):
        bp = {"format": "personal_lesson", "hook_style": "micro_story"}
        no_ask = cf.blueprint_directive("post", {**bp, "cta_style": "no_ask"})
        question = cf.blueprint_directive("post", {**bp, "cta_style": "reply_question"})
        assert "must invite a genuine, specific response" not in no_ask
        assert "NEVER ask for a meeting" in no_ask
        assert "must invite a genuine, specific response" in question

    def test_strip_closing_ask_cuts_only_an_ask_paragraph(self):
        post = "Hook.\n\nThe insight.\n\nWhich would you pick?"
        assert cf.strip_closing_ask(post) == "Hook.\n\nThe insight."
        assert cf.strip_closing_ask("Hook.\n\nThe insight.\n\nThat is all.") == \
            "Hook.\n\nThe insight.\n\nThat is all."
        assert cf.strip_closing_ask("Hook.\n\nWhich would you pick?") == \
            "Hook.\n\nWhich would you pick?"


# --- Topic diversity ------------------------------------------------------------------------------

class TestTopicDiversity:
    def test_a_post_topic_is_its_subject_or_its_fingerprint(self):
        assert cf.post_topic("anything", "Payroll for bakeries") == "Payroll for bakeries"
        assert cf.post_topic("Routing routing models models cost") == "routing, models, cost"

    def test_a_repeated_topic_is_found_and_a_fresh_one_is_not(self):
        recent = ["model, retirement, retired, litellm, cron", "invoices, late, bakery"]
        assert cf.repeated_topic("retirement, model, cron, alias, upgrade", recent) == recent[0]
        assert cf.repeated_topic("hiring, first, ops, person", recent) is None
        assert cf.repeated_topic("", recent) is None

    def test_the_bar_is_tunable_and_rejects_nonsense(self, monkeypatch):
        monkeypatch.setenv("TOPIC_REPEAT_MIN", "0.2")
        assert cf.topic_repeat_min() == 0.2
        monkeypatch.setenv("TOPIC_REPEAT_MIN", "7")
        assert cf.topic_repeat_min() == cf.TOPIC_REPEAT_MIN_DEFAULT
        monkeypatch.setenv("TOPIC_REPEAT_MIN", "lots")
        assert cf.topic_repeat_min() == cf.TOPIC_REPEAT_MIN_DEFAULT

    def test_the_avoidance_directive_names_the_topics(self):
        directive = cf.topic_avoidance_directive(["a, b", "", "c, d"])
        assert "- a, b" in directive and "- c, d" in directive
        assert cf.topic_avoidance_directive([]) == ""


# --- Deck counts and document wording -------------------------------------------------------------

def _deck(cover_title: str, body_slides: int) -> dict:
    return {"cover": {"title": cover_title, "content": "Swipe through"},
            "tips": [{"title": f"Step {i}", "content": f"Do thing {i}."}
                     for i in range(1, body_slides + 1)],
            "call_to_action": {"title": "Follow", "content": "More soon."}}


class TestDeckCounts:
    def test_the_cover_promise_is_set_to_the_slides_it_carries(self):
        deck, caption, changes = cf.reconcile_deck_counts(_deck("4 Steps to Faster Invoicing", 3),
                                                          "A short caption.")
        assert deck["cover"]["title"] == "3 Steps to Faster Invoicing"
        assert changes == ["4 Steps"] and caption == "A short caption."

    def test_a_spelled_out_count_keeps_its_form(self):
        deck, _, _ = cf.reconcile_deck_counts(_deck("Three Key Steps to Calm Payroll", 5), "")
        assert deck["cover"]["title"] == "Five Key Steps to Calm Payroll"

    def test_a_single_caption_claim_is_fixed_and_a_matching_one_left(self):
        _, caption, _ = cf.reconcile_deck_counts(_deck("Faster invoicing", 3),
                                                 "Here are the 5 steps we use.")
        assert caption == "Here are the 3 steps we use."
        _, same, changes = cf.reconcile_deck_counts(_deck("Faster invoicing", 3),
                                                    "Here are the 3 steps we use.")
        assert same == "Here are the 3 steps we use." and changes == []

    def test_a_caption_with_two_claims_is_never_touched(self):
        caption = "I made 2 mistakes before these 5 steps."
        _, out, _ = cf.reconcile_deck_counts(_deck("Faster invoicing", 3), caption)
        assert out == caption

    def test_no_deck_no_change(self):
        assert cf.reconcile_deck_counts(None, "x") == (None, "x", [])

    def test_the_input_deck_is_not_mutated(self):
        deck = _deck("4 Steps", 3)
        cf.reconcile_deck_counts(deck, "")
        assert deck["cover"]["title"] == "4 Steps"


class TestDocumentWording:
    @pytest.mark.parametrize("text,expected", [
        ("Swipe through this carousel.", "Swipe through this document."),
        ("Carousels get saved.", "Documents get saved."),
        ("THE CAROUSEL", "THE DOCUMENT"),
        ("No deck word here.", "No deck word here."),
    ])
    def test_a_document_is_never_called_a_carousel(self, text, expected):
        assert cf.document_wording(text) == expected

    def test_every_string_in_a_deck_is_reworded(self):
        deck = {"cover": {"title": "This carousel"}, "tips": [{"content": "carousel tip"}],
                "count": 3}
        assert cf.document_deck_wording(deck) == {"cover": {"title": "This document"},
                                                  "tips": [{"content": "document tip"}],
                                                  "count": 3}
        assert cf.document_wording(None) is None
