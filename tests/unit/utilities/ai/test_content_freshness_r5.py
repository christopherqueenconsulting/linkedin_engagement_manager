"""Showcase round 5, the pure half (`content_framework` / `story_bank`).

Hook shapes, the topic-cluster cap, per-audience CTA menus and fold rule, and the recorded story
cooldown.
"""

import pytest

from cqc_lem.utilities.ai import content_framework as cf, story_bank as sb

pytestmark = pytest.mark.unit


# --- Opening shapes ---------------------------------------------------------------------------------

class TestOpeningShape:
    @pytest.mark.parametrize("text,shape", [
        ("What if the cheapest model is your most expensive cost?", cf.HOOK_SHAPE_QUESTION),
        ("How do you ensure feedback doesn't fall through?", cf.HOOK_SHAPE_QUESTION),
        ("I sent 51 cold emails, and not a single reply.", cf.HOOK_SHAPE_STORY),
        ("In September 2026, I faced a dilemma.", cf.HOOK_SHAPE_STORY),
        ("Last Tuesday the invoice run failed.", cf.HOOK_SHAPE_STORY),
        ("Three weeks ago a customer called.", cf.HOOK_SHAPE_STORY),
        ("$30K went to tokens nobody read.", cf.HOOK_SHAPE_NUMBER),
        ("Most AI bills are inflated because of one habit.", cf.HOOK_SHAPE_CONTRARIAN),
        ("The cheapest model isn't the cheapest.", cf.HOOK_SHAPE_CONTRARIAN),
        ("How to read your AI bill in five minutes.", cf.HOOK_SHAPE_HOW_TO),
        ("Three steps to a calmer month-end.", cf.HOOK_SHAPE_HOW_TO),
        ("Flexible infrastructure matters more than strict policy.", cf.HOOK_SHAPE_STATEMENT),
    ])
    def test_classifies_the_first_sentence(self, text, shape):
        assert cf.opening_shape(text + "\n\nBody that follows.") == shape

    def test_empty_post_has_no_shape(self):
        assert cf.opening_shape("") is None and cf.opening_shape(None) is None

    def test_what_if_detection(self):
        assert cf.opens_with_what_if("What if your AI budget is leaking?")
        assert cf.opens_with_what_if("  — What if you could cut it?")
        assert not cf.opens_with_what_if("So what if it breaks?")

    def test_what_if_is_banned_for_four_posts_after_one(self):
        recent = ["A statement.", "What if X?", "Another one.", "Third.", "Fourth."]
        assert cf.what_if_banned(recent)
        assert not cf.what_if_banned(["A.", "B.", "C.", "D.", "What if X?"])


class TestSelectHookShape:
    def test_never_the_previous_posts_shape(self):
        for opening in ("What if X?", "Most people are wrong.", "I sent 51 emails.", "Plain."):
            shape = cf.select_hook_shape([opening + "\n\nbody"])
            assert shape != cf.opening_shape(opening)

    def test_least_recently_used_with_audience_preference(self):
        recent = ["Plain statement.", "$4 per call.", "Last week it broke."]
        assert cf.select_hook_shape(recent, smb=True) == cf.HOOK_SHAPE_HOW_TO
        assert cf.select_hook_shape(recent, smb=False) == cf.HOOK_SHAPE_CONTRARIAN
        assert cf.select_hook_shape([]) == cf.HOOK_SHAPE_STATEMENT

    def test_a_ten_post_replay_never_repeats_and_what_if_is_rare(self):
        texts: list = []
        for _ in range(10):
            shape = cf.select_hook_shape(texts, smb=None)
            # Simulate the writer opening exactly as told; a question opens "What if" when allowed.
            opening = {cf.HOOK_SHAPE_STATEMENT: "Plain point.", cf.HOOK_SHAPE_NUMBER: "$4 a call.",
                       cf.HOOK_SHAPE_STORY: "Last week it broke.",
                       cf.HOOK_SHAPE_CONTRARIAN: "Most teams are wrong.",
                       cf.HOOK_SHAPE_QUESTION: "What if it leaks?",
                       cf.HOOK_SHAPE_HOW_TO: "How to fix it."}[shape]
            texts.insert(0, opening + "\n\nbody")
        shapes = [cf.opening_shape(t) for t in texts]
        assert all(a != b for a, b in zip(shapes, shapes[1:]))
        for start in range(len(texts) - cf.WHAT_IF_WINDOW + 1):
            assert sum(cf.opens_with_what_if(t) for t in texts[start:start + 5]) <= 1

    def test_a_number_led_archetype_only_offers_number_shapes(self):
        allowed = cf.allowed_hook_shapes("build_receipt")
        assert set(allowed) == {cf.HOOK_SHAPE_NUMBER, cf.HOOK_SHAPE_STORY, cf.HOOK_SHAPE_HOW_TO}
        assert cf.select_hook_shape([], allowed=allowed) in allowed
        assert cf.hook_style_for_shape(cf.HOOK_SHAPE_STORY, "build_receipt") == "mistake_confession"
        assert cf.hook_style_for_shape(cf.HOOK_SHAPE_STORY) == "micro_story"
        assert cf.hook_style_for_shape(cf.HOOK_SHAPE_QUESTION, "build_receipt") is None
        assert cf.allowed_hook_shapes() == cf.HOOK_SHAPES


class TestHookShapeRules:
    def test_directive_bans_what_if_except_on_an_allowed_question(self):
        assert "Do NOT open with 'What if'" in cf.hook_shape_directive(cf.HOOK_SHAPE_STATEMENT)
        assert "What if" not in cf.hook_shape_directive(cf.HOOK_SHAPE_QUESTION)
        assert "Do NOT open with 'What if'" in cf.hook_shape_directive(cf.HOOK_SHAPE_QUESTION, True)
        assert cf.hook_shape_directive("nonsense") == ""

    def test_violations(self):
        assert "What if" in cf.hook_shape_violation("What if X?\n\nbody",
                                                    ["Plain.", "What if Y?"])
        assert "same shape" in cf.hook_shape_violation("Plain point.", ["Another plain point."])
        assert cf.hook_shape_violation("Most are wrong.", ["Plain point."]) is None
        assert cf.hook_shape_violation("Plain point.", []) is None

    def test_drop_opening_question(self):
        post = "What if it leaks?\n\nThe bill doubled in March.\n\nHere is the fix."
        assert cf.drop_opening_question(post) == "The bill doubled in March.\n\nHere is the fix."
        assert cf.drop_opening_question("What if?\n\nOnly one more.") == "What if?\n\nOnly one more."
        statement = "Plain.\n\nTwo.\n\nThree."
        assert cf.drop_opening_question(statement) == statement

    def test_blueprint_directive_carries_shape_and_audience(self):
        bp = {"format": "personal_lesson", "hook_style": "micro_story", "hook_shape": "story",
              "what_if_banned": True, "audience_directive": "THIS POST'S READER: owners."}
        directive = cf.blueprint_directive("post", bp)
        assert "OPENING SHAPE: Short story" in directive and "Do NOT open with 'What if'" in directive
        assert "THIS POST'S READER: owners." in directive
        deck = cf.carousel_blueprint_directive({"format": "personal_lesson",
                                                "audience_directive": "THIS POST'S READER: x."})
        assert "THIS POST'S READER: x." in deck


# --- Topic-cluster cap ------------------------------------------------------------------------------

COST = "AI spend keeps rising. Routing cut our token bill and the cost per call."
COST_2 = "Your AI budget leaks through tokens. Cheaper routing lowers the price of each call."
RELIABILITY = "Silent failures hide in production. Monitoring and alerts caught the drift."
HIRING = "We hired two candidates after one interview."


class TestTopicCluster:
    def test_clusters_by_keyword_family(self):
        assert cf.topic_cluster(COST) == "ai_cost"
        assert cf.topic_cluster(RELIABILITY) == "ai_reliability"
        assert cf.topic_cluster("A plain post about gardens.") is None
        assert cf.topic_cluster("LLM cost", min_hits=1) == "ai_cost"

    def test_a_keyword_cta_line_does_not_file_a_post(self):
        post = HIRING + "\nComment AUDIT and I'll DM you where your AI spend leaks and costs hide."
        assert cf.topic_cluster(post) != "ai_cost"

    def test_three_in_the_window_caps_the_cluster(self):
        history = [COST, RELIABILITY, COST_2, HIRING, COST]
        assert cf.crowded_topic_clusters(history) == {"ai_cost"}
        assert cf.capped_topic_cluster(COST_2, history) == "ai_cost"
        assert cf.capped_topic_cluster(RELIABILITY, history) is None
        # Only the last nine count: a tenth-back cost post has left the window.
        assert cf.crowded_topic_clusters([HIRING] * 7 + [COST, COST_2, COST]) == set()

    def test_avoidance_directive(self):
        assert "AI spend, cost and model routing" in cf.cluster_avoidance_directive({"ai_cost"})
        assert cf.cluster_avoidance_directive(set()) == ""


# --- CTA menus and the fold, per audience -----------------------------------------------------------

class TestAudienceCtaAndFold:
    def test_promo_closes_on_the_artifact_only_when_allowed(self):
        assert cf.select_cta_type([], promo=True) == cf.CTA_TYPE_ARTIFACT
        assert cf.select_cta_type([], promo=True, artifact_allowed=False) != cf.CTA_TYPE_ARTIFACT

    def test_audience_menus(self):
        assert cf.select_cta_type([], smb=True) == cf.CTA_TYPE_QUESTION
        assert cf.select_cta_type([cf.CTA_TYPE_QUESTION], smb=False) == cf.CTA_TYPE_NONE
        assert cf.select_cta_type([cf.CTA_TYPE_QUESTION], smb=True) == cf.CTA_TYPE_SAVE
        for smb in (True, False):
            assert cf.select_cta_type([], smb=smb) != cf.CTA_TYPE_DM

    def test_question_sub_styles_follow_the_audience(self):
        bp = {"format": "personal_lesson"}
        smb = {cf.assign_cta_style(bp, cf.CTA_TYPE_QUESTION, i, smb=True)["cta_style"]
               for i in range(4)}
        pro = {cf.assign_cta_style(bp, cf.CTA_TYPE_QUESTION, i, smb=False)["cta_style"]
               for i in range(6)}
        assert smb == {"reply_question", "poll_prompt"}
        assert pro == {"challenge", "debate", "reply_question"}

    def test_the_fold_rule_is_per_post(self):
        owner_prefs = {"business_goals": "help small-business owners"}
        assert cf.smb_audience(owner_prefs)
        # A practitioner post of the same author is never held to the owner's fold, and vice versa.
        assert not cf.smb_audience(owner_prefs, audience={"smb": False})
        assert cf.smb_audience({}, audience={"smb": True})
        assert cf.smb_audience_text("Small-business owners") and not cf.smb_audience_text("CTOs")


# --- Recorded story cooldown ------------------------------------------------------------------------

FAB_ROI = {"id": 7, "kind": "mistake", "title": "The $49 fabricated ROI",
           "body": "My agent team wrote a business case claiming $49 saved per run in May 2026 "
                   "with no source.", "active": True, "used_count": 1}
OTHER = {"id": 8, "kind": "number", "title": "160 releases",
         "body": "Between June 25 and July 27 my system shipped 160 releases.", "active": True,
         "used_count": 4}
OWNER = {"id": 9, "kind": "client_win", "title": "Invoices on time",
         "body": "A bakery owner got her invoices out in two hours instead of two days, so "
                 "customers paid faster.", "active": True, "used_count": 9}
# A retelling of entry 7 that echoes none of its numbers or title words.
PARAPHRASE = "I once watched an AI-written business case invent a return nobody could source."


class TestRecordedStoryCooldown:
    def test_the_echo_alone_misses_a_paraphrase(self):
        assert sb.cooling_ids([FAB_ROI, OTHER], [PARAPHRASE]) == set()

    def test_the_recorded_id_cools_it_anyway(self):
        assert sb.cooling_ids([FAB_ROI, OTHER], [PARAPHRASE], {7}) == {7}
        assert sb.cooling_ids([FAB_ROI], [], {99}) == set()  # an id no longer in the bank

    def test_select_story_skips_a_recorded_entry(self):
        assert sb.select_story([FAB_ROI, OTHER], cooling_story_ids={7})["id"] == 8
        assert sb.select_story([FAB_ROI], cooling_story_ids={7}) is None

    def test_owner_reader_prefers_an_owner_story(self):
        assert sb.select_story([FAB_ROI, OWNER])["id"] == 7  # rotation: least used
        assert sb.select_story([FAB_ROI, OWNER], smb=True)["id"] == 9
        assert sb.audience_fit(OWNER, True) > 0 and sb.audience_fit(OWNER, False) == 0
        # No fitting entry: rotation still decides.
        assert sb.select_story([FAB_ROI, OTHER], smb=True)["id"] == 7


class TestHookPassShape:
    @staticmethod
    def _system(**kwargs):
        from unittest.mock import MagicMock, patch

        from cqc_lem.utilities.ai import ai_helper
        resp = MagicMock()
        resp.choices[0].message.content = "rewritten"
        with patch.object(ai_helper, "_call_llm", return_value=resp) as call:
            assert ai_helper.optimize_post_hook("draft", **kwargs) == "rewritten"
        return call.call_args.kwargs["messages"][0]["content"]

    def test_the_assigned_shape_replaces_the_open_menu(self):
        system = self._system(hook_shape=cf.HOOK_SHAPE_CONTRARIAN, ban_what_if=True)
        assert "OPENING SHAPE: Contrarian" in system and "Do NOT open with 'What if'" in system
        assert "a sharp question" not in system

    def test_without_a_shape_the_prompt_is_unchanged(self):
        system = self._system()
        assert "fold) —\n        a bold claim, a surprising stat, or a sharp question." in system
        assert "OPENING SHAPE" not in system
