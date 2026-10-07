"""Showcase round 5 wiring in the content plan.

The keyword-CTA fix replayed over a ten-slot plan, the recorded story cooldown, dual-audience
alternation, the opening-shape rotation and the topic-cluster cap — each proven at the seam where
`run_content_plan` applies it.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app import run_content_plan as rcp
from cqc_lem.domain.models import PostDraftContext
from cqc_lem.utilities.ai import content_framework as cf
from cqc_lem.utilities.ai.content_alignment import has_lead_magnet_cta_mechanic

pytestmark = pytest.mark.unit

_RCP = "cqc_lem.app.run_content_plan"
_LM = {"enabled": True, "keyword": "AUDIT", "message": "the AI Ops Readiness Checklist"}
_STORY = {"id": 7, "kind": "mistake", "title": "Agent business case",
          "body": "My agent team wrote a business case with no source behind its numbers.",
          "active": True}


def _ctx(**overrides) -> PostDraftContext:
    fields = dict(user_id=1, stage="awareness", post_type="thought_leadership",
                  user_profile=MagicMock(), prefs={"focus_topics": ["AI costs"]},
                  profile_synthesis="voice",
                  blueprint={"format": "personal_lesson", "hook_shape": cf.HOOK_SHAPE_STATEMENT,
                             "cta_type": cf.CTA_TYPE_QUESTION},
                  post_id=40, history_directive="", story_directive="")
    fields.update(overrides)
    return PostDraftContext(**fields)


# --- The keyword CTA, replayed over a ten-slot plan -----------------------------------------------

class _PlanStore:
    """The posts table for one user, as the plan's history reads see it."""

    def __init__(self, slots):
        self.rows = {pid: {"id": pid, "scheduled_time": when, "content": None,
                           "content_mix": mix, "audience": None, "story_id": None}
                     for pid, when, mix, _ in slots}
        self.reclassed: dict = {}

    def near_slot(self, user_id, post_id, days):
        me = self.rows[post_id]["scheduled_time"]
        return [r["content"] for r in self.rows.values()
                if r["id"] != post_id and r["content"]
                and abs(r["scheduled_time"] - me) <= timedelta(days=days)]

    def records(self, user_id, limit=10, exclude_post_id=None, within_days=None):
        rows = [r for r in sorted(self.rows.values(), key=lambda r: -r["id"])
                if r["id"] != exclude_post_id and (r["content"] or r["story_id"])]
        return [dict(r) for r in rows[:limit]]

    def record(self, post_id, audience, story_id):
        self.rows[post_id].update(audience=audience, story_id=story_id)


# Ten slots, three a week. Video slots 123/135/141 are the round-4 showcase's "Comment AUDIT" posts:
# multiples of three, which the legacy 1-in-3 cadence selected because the video caption never
# received the slot's class. Two promo slots sit five days apart.
_START = datetime(2026, 10, 13, 12, 0)
_SLOTS = [
    (123, _START, "authority", "video"),
    (125, _START + timedelta(days=2), "value", "text"),
    (126, _START + timedelta(days=4), "promo", "text"),
    (128, _START + timedelta(days=7), "authority", "text"),
    (129, _START + timedelta(days=9), "promo", "text"),
    (130, _START + timedelta(days=11), "value", "text"),
    (135, _START + timedelta(days=14), "value", "video"),
    (138, _START + timedelta(days=16), "value", "text"),
    (141, _START + timedelta(days=18), "authority", "video"),
    (144, _START + timedelta(days=21), "promo", "text"),
]


def _replay_plan():
    """Generate every slot through `create_content`, storing each post before the next is written.

    The writer stub honours the sanctioned keyword directive when it is given one, and on two
    non-promo posts copies the "Comment AUDIT" close anyway, the way a model echoes recent posts.
    """
    store = _PlanStore(_SLOTS)
    copy_anyway = {128, 141}

    def writer(user_profile, stage, prefs=None, profile_synthesis=None, blueprint=None,
               lead_magnet_cta=None, post_id=None, **kwargs):
        body = (f"Post {post_id} is about a different decision.\n\n"
                "The middle paragraph carries the detail.\n\nThe last line lands the point.")
        if (lead_magnet_cta and "SANCTIONED" in lead_magnet_cta) or post_id in copy_anyway:
            body += "\n\nComment AUDIT and I'll DM you the checklist."
        return body

    def reclass(post_id, mix):
        store.reclassed[post_id] = mix

    patches = [
        patch(f"{_RCP}.get_engagement_preferences", return_value={}),
        patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"),
        patch(f"{_RCP}._resolve_user_profile", return_value=MagicMock()),
        patch(f"{_RCP}.load_profile_for_user", return_value=MagicMock()),
        patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM),
        patch(f"{_RCP}.get_newsletter_settings", return_value={"enabled": False}),
        patch(f"{_RCP}.get_recent_post_texts", return_value=[]),
        patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]),
        patch(f"{_RCP}.get_shape_performance", return_value=None),
        patch(f"{_RCP}.get_recent_post_topics", return_value=[]),
        patch(f"{_RCP}.get_recent_post_records", side_effect=store.records),
        patch(f"{_RCP}.get_post_texts_near_slot", side_effect=store.near_slot),
        patch(f"{_RCP}.update_post_generation_record", side_effect=store.record),
        patch(f"{_RCP}.update_post_content_mix", side_effect=reclass),
        patch(f"{_RCP}._select_story_for_post", return_value=_STORY),
        patch(f"{_RCP}.record_story_bank_use"),
        patch(f"{_RCP}.update_db_post_shape"),
        patch(f"{_RCP}._post_source_for_slot", return_value="thought_leadership"),
        patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=writer),
        patch(f"{_RCP}.get_ai_linked_post_refinement", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}.optimize_post_hook", side_effect=lambda c, **kw: c),
        patch(f"{_RCP}._apply_once_per_post_gates", side_effect=lambda ctx, c, *a, **kw: c),
        patch(f"{_RCP}._affiliate_promo_content", return_value=None),
        patch(f"{_RCP}._curated_source_content", return_value=None),
        patch(f"{_RCP}._generate_text_post_image"),
        patch(f"{_RCP}._generate_video_src", return_value=None),
        patch(f"{_RCP}._score_and_persist_authenticity"),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        for pid, _when, mix, post_type in _SLOTS:
            content, _ = rcp.create_content(1, post_type, "awareness", post_id=pid,
                                            content_mix=mix)
            store.rows[pid]["content"] = content
    return store


class TestKeywordCtaPlanReplay:
    @pytest.fixture
    def store(self):
        # Function-scoped on purpose: the replay runs under the per-test hermetic guards.
        return _replay_plan()

    def _keyword_posts(self, store):
        return [r for r in sorted(store.rows.values(), key=lambda r: r["scheduled_time"])
                if has_lead_magnet_cta_mechanic(r["content"], "AUDIT")]

    def test_the_keyword_ask_lands_only_on_promo_slots(self, store):
        promo = {pid for pid, _w, mix, _t in _SLOTS if mix == "promo"}
        assert {r["id"] for r in self._keyword_posts(store)} <= promo

    def test_never_on_the_video_captions_the_legacy_cadence_selected(self, store):
        for pid in (123, 135, 141):
            assert not has_lead_magnet_cta_mechanic(store.rows[pid]["content"], "AUDIT")

    def test_at_most_once_in_any_seven_days_of_slots(self, store):
        times = [r["scheduled_time"] for r in self._keyword_posts(store)]
        assert len(times) == 2  # promo 126 and promo 144; promo 129 is three days after 126
        assert all(b - a > timedelta(days=7) for a, b in zip(times, times[1:]))

    def test_the_promo_inside_the_cooldown_is_written_and_reclassed_as_value(self, store):
        assert store.reclassed == {129: "value"}

    def test_a_copied_close_on_a_non_promo_post_is_removed(self, store):
        for pid in (128, 141):
            assert "AUDIT" not in store.rows[pid]["content"]
            assert store.rows[pid]["content"].endswith("The last line lands the point.")


class TestKeywordCooldownHelpers:
    def test_capped_when_a_nearby_slot_already_asks(self):
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM), \
             patch(f"{_RCP}.get_post_texts_near_slot",
                   return_value=["Plain.", "Comment AUDIT and I'll DM it."]) as near:
            assert rcp._keyword_cta_capped(1, 9) is True
        near.assert_called_once_with(1, 9, cf.ARTIFACT_CTA_COOLDOWN_DAYS)

    def test_not_capped_without_a_nearby_ask_or_without_a_lead_magnet(self):
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM), \
             patch(f"{_RCP}.get_post_texts_near_slot", return_value=["Plain."]):
            assert rcp._keyword_cta_capped(1, 9) is False
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RCP}.get_post_texts_near_slot") as near:
            assert rcp._keyword_cta_capped(1, 9) is False
        near.assert_not_called()
        with patch(f"{_RCP}.get_lead_magnet_settings", side_effect=RuntimeError("db")):
            assert rcp._keyword_cta_capped(1, 9) is False

    @pytest.mark.parametrize("failure", [{"return_value": None},
                                         {"side_effect": RuntimeError("db")}])
    def test_an_unreadable_window_fails_closed(self, failure):
        with patch(f"{_RCP}.get_lead_magnet_settings", return_value=_LM), \
             patch(f"{_RCP}.get_post_texts_near_slot", **failure), \
             patch(f"{_RCP}.log_warning") as warn:
            assert rcp._keyword_cta_capped(1, 9) is True
        warn.assert_called_once()

    def test_only_a_promo_slot_is_demoted(self):
        with patch(f"{_RCP}._keyword_cta_capped", return_value=True), \
             patch(f"{_RCP}.update_post_content_mix") as reclass:
            assert rcp._cap_promo_keyword(1, 9, "promo") == "value"
            assert rcp._cap_promo_keyword(1, 9, "authority") == "authority"
            assert rcp._cap_promo_keyword(1, None, "promo") == "value"
        reclass.assert_called_once_with(9, "value")

    def test_the_strip_leaves_a_sanctioned_ask_alone(self):
        post = "Body.\n\nComment AUDIT and I'll DM it."
        assert rcp._strip_unsanctioned_keyword_cta(_ctx(), post, _LM, True) == post
        assert rcp._strip_unsanctioned_keyword_cta(_ctx(), post, _LM, False) == "Body."
        assert rcp._strip_unsanctioned_keyword_cta(_ctx(), post, {}, False) == post
        assert rcp._strip_unsanctioned_keyword_cta(_ctx(), "Comment AUDIT now.", _LM,
                                                   False) == "Comment AUDIT now."


# --- Recorded story cooldown: two uses inside a ten-post window -----------------------------------

FAB_ROI = {"id": 7, "kind": "mistake", "title": "The $49 fabricated ROI",
           "body": "My agent team wrote a business case claiming $49 saved per run in May 2026.",
           "active": True, "used_count": 0}
OTHER = {"id": 8, "kind": "number", "title": "160 releases",
         "body": "Between June 25 and July 27 my system shipped 160 releases.", "active": True,
         "used_count": 5}
PARAPHRASE = "I once watched an AI-written business case invent a return nobody could source."


class TestRecordedStoryCooldown:
    def _window(self, story_id):
        rows = [{"id": 100 + i, "content": f"Unrelated post {i}.", "story_id": None}
                for i in range(8)]
        rows.insert(3, {"id": 123, "content": PARAPHRASE, "story_id": story_id})
        return rows

    def test_a_recorded_use_cools_the_story_the_echo_could_not_see(self):
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[FAB_ROI, OTHER]), \
             patch(f"{_RCP}.get_recent_post_records", return_value=self._window(7)):
            assert rcp._select_story_for_post(1, {}, post_id=133)["id"] == 8

    def test_without_the_record_the_paraphrase_would_retell_it(self):
        # The round-4 failure: echo detection alone lets the "$49" story anchor a second post.
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[FAB_ROI, OTHER]), \
             patch(f"{_RCP}.get_recent_post_records", return_value=self._window(None)):
            assert rcp._select_story_for_post(1, {}, post_id=133)["id"] == 7

    def test_every_entry_recorded_is_a_cooldown(self):
        rows = self._window(7) + [{"id": 90, "content": "x", "story_id": 8}]
        outcome: dict = {}
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[FAB_ROI, OTHER]), \
             patch(f"{_RCP}.get_recent_post_records", return_value=rows):
            assert rcp._select_story_for_post(1, {}, outcome=outcome) is None
        assert outcome == {"cooled": True}

    def test_the_writer_records_the_story_it_was_anchored_to(self):
        ctx = _ctx(post_id=133, blueprint={"format": "personal_lesson", "audience": "secondary"})
        with patch(f"{_RCP}.record_story_bank_use"), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}._score_and_persist_authenticity"), \
             patch(f"{_RCP}.update_post_generation_record") as rec:
            rcp._persist_draft_outcome(ctx, "A post.", FAB_ROI)
            rcp._persist_draft_outcome(ctx, None, FAB_ROI)  # a failed draft records nothing
        rec.assert_called_once_with(133, "secondary", 7)

    def test_a_record_failure_is_a_warning_and_no_post_id_is_a_no_op(self):
        with patch(f"{_RCP}.update_post_generation_record", side_effect=RuntimeError("db")), \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._record_generation(1, 5, None, FAB_ROI)
            rcp._record_generation(1, None, None, FAB_ROI)
        warn.assert_called_once()


# --- Dual audience --------------------------------------------------------------------------------

MIX = {"primary_audience": "ops and engineering leaders already running AI",
       "secondary_audience": "small-business owners", "secondary_share": 0.6,
       "secondary_focus_topics": ["cash flow", "customer response time"]}
PREFS = {"focus_topics": ["LLM cost"], "business_goals": "Calls with ops leaders.",
         "audience_mix": MIX}


class TestDualAudienceWiring:
    def test_single_audience_users_are_untouched(self):
        prefs = {"focus_topics": ["LLM cost"]}
        assert rcp._resolve_post_audience(1, 5, prefs, []) == (prefs, None)
        assert rcp._resolve_post_audience(1, 5, None, []) == (None, None)

    def test_the_pick_reads_the_recorded_audiences(self):
        history = [{"audience": "secondary"}, {"audience": "primary"}, {"audience": "secondary"}]
        prefs, audience = rcp._resolve_post_audience(1, 5, PREFS, history)
        assert audience["audience"] == "primary" and audience["smb"] is False
        assert prefs["focus_topics"] == ["LLM cost"]
        prefs, audience = rcp._resolve_post_audience(1, 5, PREFS, [])
        assert audience["audience"] == "secondary" and audience["smb"] is True
        assert prefs["focus_topics"] == ["cash flow", "customer response time"]

    def test_an_owner_post_gets_the_fold_its_menu_and_its_directive(self):
        audience = {"audience": "secondary", "description": "small-business owners", "smb": True}
        steered = rcp._steer_post_blueprint(1, {"format": "personal_lesson"}, 4, "value", {},
                                            "voice", [], audience=audience)
        assert steered["plain_fold"] is True and steered["audience"] == "secondary"
        assert "small-business owner" in steered["audience_directive"]
        # Post 4 draws the question close; an owner gets the plain sub-style, never a debate.
        assert steered["cta_type"] == cf.CTA_TYPE_QUESTION
        assert steered["cta_style"] == "reply_question"

    def test_a_practitioner_post_of_an_owner_facing_author_skips_the_fold(self):
        audience = {"audience": "primary", "description": "ops leaders", "smb": False}
        steered = rcp._steer_post_blueprint(1, {"format": "personal_lesson"}, 6, "value",
                                            {"business_goals": "small-business owners"}, "voice",
                                            [], audience=audience)
        assert "plain_fold" not in steered and "practitioner" in steered["audience_directive"]

    def test_the_story_pick_prefers_an_owner_story_for_an_owner(self):
        owner = {"id": 9, "kind": "client_win", "title": "Invoices",
                 "body": "A bakery owner got invoices out in two hours so customers paid faster.",
                 "active": True, "used_count": 9}
        with patch(f"{_RCP}.get_story_bank_entries", return_value=[FAB_ROI, owner]), \
             patch(f"{_RCP}.get_recent_post_records", return_value=[]):
            assert rcp._select_story_for_post(1, {}, audience={"smb": True})["id"] == 9
            assert rcp._select_story_for_post(1, {}, audience={"smb": False})["id"] == 7

    def test_create_text_post_writes_for_the_picked_audience(self):
        captured = {}

        def writer(user_profile, stage, prefs=None, blueprint=None, **kwargs):
            captured.update(prefs=prefs, blueprint=blueprint)
            return "Plain point.\n\nDetail.\n\nEnd."

        with patch(f"{_RCP}.get_engagement_preferences", return_value=dict(PREFS)), \
             patch(f"{_RCP}.get_or_create_profile_synthesis", return_value="voice"), \
             patch(f"{_RCP}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RCP}.get_recent_post_texts", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_shape_history", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_records", return_value=[]), \
             patch(f"{_RCP}.get_recent_post_topics", return_value=[]), \
             patch(f"{_RCP}.get_story_bank_entries", return_value=[]), \
             patch(f"{_RCP}.update_db_post_shape"), \
             patch(f"{_RCP}.update_post_generation_record") as rec, \
             patch(f"{_RCP}.get_thought_leadership_post_from_ai", side_effect=writer):
            rcp.create_text_post(1, "awareness", post_type="thought_leadership",
                                 user_profile=MagicMock(), refine_final_post=False, post_id=5,
                                 content_mix="value")
        assert captured["prefs"]["focus_topics"] == ["cash flow", "customer response time"]
        assert captured["blueprint"]["audience"] == "secondary"
        assert captured["blueprint"]["plain_fold"] is True
        rec.assert_called_once_with(5, "secondary", None)


# --- Opening-shape rotation -----------------------------------------------------------------------

class TestHookShapeWiring:
    def test_the_blueprint_gets_a_rotated_shape_and_its_hook_style(self):
        steered = rcp._steer_post_blueprint(1, {"format": "personal_lesson"}, 6, "value", {},
                                            "voice", ["What if it leaks?\n\nbody"],
                                            hook_shape=True)
        assert steered["hook_shape"] != cf.HOOK_SHAPE_QUESTION
        assert steered["what_if_banned"] is True
        assert steered["hook_style"] == cf.hook_style_for_shape(steered["hook_shape"],
                                                                "personal_lesson")

    def test_the_shape_never_brings_back_a_recent_hook_style(self):
        # A number-led archetype with "surprising_stat" just used: the shape rotation must not
        # re-assign it (the V51 no-repeat rule the blueprint selection already honoured).
        for recent in ([], ["Plain point."], ["Last week it broke."], ["How to fix it."]):
            steered = rcp._steer_post_blueprint(
                1, {"format": "build_receipt", "recent_hook_styles": ["surprising_stat"]}, 6,
                "value", {}, "voice", recent, hook_shape=True)
            assert steered["hook_style"] != "surprising_stat"

    def test_the_post_blueprint_carries_the_recent_hook_window(self):
        history = [{"archetype": "listicle", "hook_style": h}
                   for h in ("question", "bold_claim", "micro_story", "surprising_stat")]
        with patch(f"{_RCP}.get_recent_post_shape_history", return_value=history), \
             patch(f"{_RCP}.get_shape_performance", return_value=None):
            bp = rcp._select_post_blueprint(1)
        assert bp["recent_hook_styles"] == ["question", "bold_claim", "micro_story"]

    def test_no_history_means_no_shape(self):
        steered = rcp._steer_post_blueprint(1, {"format": "personal_lesson"}, 6, "value", {},
                                            "voice", None, hook_shape=True)
        assert "hook_shape" not in steered

    def test_a_repeat_gets_one_more_hook_pass(self):
        ctx = _ctx()
        with patch(f"{_RCP}.optimize_post_hook",
                   return_value="Most teams are wrong about this.\n\nBody.") as hook, \
             patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c: c):
            out = rcp._enforce_hook_shape(ctx, "What if it leaks?\n\nBody.\n\nEnd.",
                                          ["What if X?"], "AUDIT")
        assert out.startswith("Most teams")
        assert hook.call_args.kwargs["ban_what_if"] is True
        assert hook.call_args.kwargs["preserve_cta_keyword"] == "AUDIT"

    def test_a_stubborn_question_is_cut_when_the_post_stands_without_it(self):
        ctx = _ctx()
        stubborn = "What if it leaks?\n\nThe bill doubled in March.\n\nHere is the fix."
        with patch(f"{_RCP}.optimize_post_hook", return_value=stubborn), \
             patch(f"{_RCP}.sanitize_for_linkedin", side_effect=lambda c: c):
            out = rcp._enforce_hook_shape(ctx, stubborn, ["What if X?"], None)
        assert out == "The bill doubled in March.\n\nHere is the fix."

    def test_a_fine_opening_or_an_unshaped_post_is_left_alone(self):
        with patch(f"{_RCP}.optimize_post_hook") as hook:
            assert rcp._enforce_hook_shape(_ctx(), "Most are wrong.", ["Plain."], None) == \
                "Most are wrong."
            assert rcp._enforce_hook_shape(_ctx(blueprint={}), "What if?", ["What if?"],
                                           None) == "What if?"
        hook.assert_not_called()

    def test_a_statement_that_still_repeats_ships_as_is(self):
        with patch(f"{_RCP}.log_info") as info:
            out = rcp._drop_repeated_question(_ctx(), "Plain point.\n\nTwo.\n\nThree.",
                                              ["Another plain point."])
        assert out == "Plain point.\n\nTwo.\n\nThree."
        assert "shipped as is" in info.call_args[0][0]


# --- Topic-cluster cap ----------------------------------------------------------------------------

COST = "AI spend keeps rising. Routing cut our token bill and the cost per call."
FRESH = "A bakery owner asked how to answer customers faster on a Saturday."


class TestTopicCapWiring:
    def _rows(self, n_cost):
        return ([{"topic": f"cost {i}", "content": COST} for i in range(n_cost)]
                + [{"topic": "hiring", "content": "We hired a candidate."}])

    def test_a_fourth_post_in_a_cluster_is_regenerated_with_the_cap_named(self):
        ctx = _ctx()
        with patch(f"{_RCP}.get_recent_post_topics", return_value=self._rows(3)), \
             patch(f"{_RCP}._compose_draft", side_effect=lambda c, **kw: (FRESH, c)) as compose:
            out, used = rcp._diversify_topic(ctx, COST + " Another angle.", None, None)
        assert out == FRESH and compose.call_count == 1
        assert "TOPIC CAP" in used.history_directive

    def test_two_in_the_window_is_still_allowed(self):
        ctx = _ctx()
        with patch(f"{_RCP}.get_recent_post_topics",
                   return_value=[{"topic": "a", "content": COST}, {"topic": "b", "content": FRESH},
                                 {"topic": "c", "content": "Gardens."},
                                 {"topic": "d", "content": COST}]), \
             patch(f"{_RCP}._compose_draft") as compose:
            assert rcp._diversify_topic(ctx, "Cutting the AI bill: routing, tokens, cost.",
                                        None, None)[0].startswith("Cutting")
        compose.assert_not_called()

    def test_the_research_fallback_drops_capped_focus_topics(self):
        prefs = rcp._without_repeated_focus_topics({"focus_topics": ["LLM cost", "Hiring"]}, [],
                                                   {"ai_cost"})
        assert prefs["focus_topics"] == ["Hiring"]

    def test_crowded_focus_topics_are_steered_off_before_drafting(self):
        prefs = {"focus_topics": ["LLM cost efficiency", "AI governance"]}
        assert rcp._without_crowded_focus_topics(prefs, [COST] * 3)["focus_topics"] == \
            ["AI governance"]
        assert rcp._without_crowded_focus_topics(prefs, [COST]) is prefs
        only = {"focus_topics": ["LLM cost"]}
        assert rcp._without_crowded_focus_topics(only, [COST] * 3) is only
        assert rcp._without_crowded_focus_topics(None, [COST] * 3) is None


class TestHistoryRead:
    def test_records_and_texts(self):
        rows = [{"content": "a"}, {"content": ""}, {"content": "b"}]
        with patch(f"{_RCP}.get_recent_post_records", return_value=rows):
            assert rcp._recent_post_records(1, 5) == rows
        assert rcp._record_texts(rows) == ["a", "b"]
        assert rcp._record_texts(None) is None

    @pytest.mark.parametrize("failure", [{"side_effect": RuntimeError("db")},
                                         {"return_value": None}])
    def test_unreadable_history_is_none(self, failure):
        with patch(f"{_RCP}.get_recent_post_records", **failure):
            assert rcp._recent_post_records(1, 5) is None
