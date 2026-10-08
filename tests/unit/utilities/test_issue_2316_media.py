"""Issue #2316, media half: people on covers, the split budget, deck rotation, motion resets.

Pins (docs/newsletter-covers.md, docs/image-stack.md, #2316): the owner's binding rule for people
on a cover (the author's avatar, else no person; stock people only for a third party, and never a
render the judge called generic); no more than 2 split covers in any 5; decks open on at least 3
covers in any 5 and never repeat a recent deck's inside slides; a counter or checklist never seen
going backwards; a title card's caption band never empty.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from cqc_lem.utilities import carousel_creator as crc, motion_design as md, newsletter_cover as nc, video_captions as vc
from cqc_lem.utilities.ai import image_compose as icomp, image_concept as ic, image_gen
from cqc_lem.utilities.avatar import guardrails as gr

pytestmark = pytest.mark.unit

_AVATAR = {"status": "succeeded", "model_ref": "owner/lora:v1", "approval_status": "approved"}
_PROFILE = SimpleNamespace(full_name="Christopher Queen")


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="Buyers shortlist AI vendors before they call", audience="founders",
                specific_entities=("procurement shortlist",), emotional_beat="concern",
                hook_phrase="Buyers decide before the call", treatment="people_scene",
                treatment_rationale="x", archetype="people_scene",
                archetype_ranking=("people_scene",), human_moment=True,
                visual_ideas=("A founder frowns at a shortlist.",
                              "A printed vendor shortlist with one name circled in gold.",
                              "A mug on a desk."),
                layout="split_left")
    base.update(kw)
    return ic.ImageConcept(**base)


# --- Who a cover is about ---------------------------------------------------------------------------

class TestAuthorReference:
    def test_the_concept_speaking_of_the_author(self):
        assert gr.author_reference(_concept(thesis="I nearly lost a client"), "") == \
            gr.SELF_REFERENCE_CONCEPT
        assert gr.author_reference(_concept(thesis="Christopher Queen rebuilt it"), "",
                                   ["Christopher Queen"]) == gr.SELF_REFERENCE_CONCEPT

    def test_the_edition_naming_the_author_by_first_or_full_name(self):
        names = gr.author_names(_PROFILE)
        assert names == ["Christopher Queen", "Christopher"]
        assert gr.author_reference(_concept(), "Christopher took the call.", names) == \
            gr.SELF_REFERENCE_NAMED
        assert not gr.names_author("Christophers everywhere.", names)

    def test_a_first_person_edition_counts_the_firm_we(self):
        text = "We shipped it. Our client asked why. We told them. Our team fixed it in a day."
        assert gr.author_reference(_concept(), text) == gr.SELF_REFERENCE_FIRST_PERSON

    def test_a_third_party_piece(self):
        assert gr.author_reference(_concept(), "Buyers shortlist vendors before any call.") == ""
        assert gr.author_reference(None, "") == ""
        assert gr.author_names(None) == []
        assert gr.author_names(SimpleNamespace(full_name="Al")) == []


# --- The owner's rule, end to end in the resolver ----------------------------------------------------

class TestResolvePeopleCover:
    def _resolve(self, concept, use_avatar=None, avatar=_AVATAR, disabled=False,
                 text="I lost a client. My fault. I fixed it. My team learned."):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=avatar) as policy, \
                patch.object(nc, "_avatar_for_explicit_choice", return_value=avatar), \
                patch("cqc_lem.utilities.db.get_avatar_preferences",
                      return_value={"avatar_disabled": disabled}), \
                patch.object(nc, "_recent_cover_archetypes", return_value=[]), \
                patch.object(nc, "_recent_concept_field", return_value=[]):
            out = nc.resolve_people_cover(3, use_avatar, concept, "T", None, text, _PROFILE)
        return out, policy

    def test_about_the_author_with_an_avatar_renders_the_avatar(self):
        (concept, avatar, policy), guard = self._resolve(_concept())
        assert avatar == _AVATAR and policy == nc.PEOPLE_POLICY_AVATAR
        assert guard.call_args[1]["surface"] == "newsletter"

    def test_about_the_author_without_an_avatar_shows_no_person(self):
        (concept, avatar, policy), _ = self._resolve(_concept(), avatar=None)
        assert avatar is None and policy == nc.PEOPLE_POLICY_NO_AVATAR
        assert concept.treatment == ic.TREATMENT_EDITORIAL
        assert ic.ARCHETYPE_PEOPLE not in concept.archetype_ranking
        assert concept.chosen_idea.startswith("A printed vendor shortlist")
        assert concept.art_style in ic.ART_STYLES

    @pytest.mark.parametrize("use_avatar,disabled", [(False, False), (None, True)])
    def test_an_opt_out_plus_a_self_reference_is_a_non_people_cover(self, use_avatar, disabled):
        (concept, avatar, policy), _ = self._resolve(_concept(), use_avatar=use_avatar,
                                                     avatar=None, disabled=disabled)
        assert policy == nc.PEOPLE_POLICY_OPTED_OUT and avatar is None
        assert concept.treatment == ic.TREATMENT_EDITORIAL

    def test_a_third_party_people_scene_stays_stock(self):
        concept = _concept()
        (got, avatar, policy), guard = self._resolve(concept, text="Buyers decide early.")
        assert got is concept and avatar is None and policy == nc.PEOPLE_POLICY_STOCK
        guard.assert_not_called()

    def test_a_non_people_cover_and_an_explicit_with_me(self):
        editorial = _concept(treatment="editorial_concept", archetype="editorial_concept",
                             archetype_ranking=("editorial_concept",))
        (got, avatar, policy), _ = self._resolve(editorial)
        assert got is editorial and avatar is None and policy == nc.PEOPLE_POLICY_NONE
        (got, avatar, policy), _ = self._resolve(editorial, use_avatar=True)
        assert avatar == _AVATAR and policy == nc.PEOPLE_POLICY_NONE

    def test_no_concept_keeps_the_legacy_decision(self):
        with patch.object(nc, "_resolve_cover_avatar", return_value=None) as legacy:
            assert nc.resolve_people_cover(3, None, None, "T", "S", "B") == \
                (None, None, nc.PEOPLE_POLICY_NONE)
        legacy.assert_called_once()

    def test_unreadable_preferences_read_as_opted_out(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences", side_effect=RuntimeError):
            assert nc._avatar_opted_out(3, None) is True


class TestWithoutPeople:
    def test_a_post_surface_is_forced_off_people_too(self):
        got = ic.without_people(_concept(), "video")
        assert got.treatment == ic.TREATMENT_EDITORIAL and not got.human_moment

    def test_none_and_a_non_people_concept_pass_through(self):
        editorial = _concept(treatment="editorial_concept", archetype="editorial_concept",
                             archetype_ranking=("editorial_concept",))
        assert ic.without_people(None) is None
        assert ic.without_people(editorial) is editorial


# --- A stock render the judge called generic never ships as people ---------------------------------

class TestGenericPeopleFallsBack:
    @pytest.mark.parametrize("info,expected", [
        ({"archetype_rendered": "people_scene", "gate_verdict": "accepted",
          "gate_issues": ["scene is generic, not AI-specific"]}, True),
        ({"archetype_rendered": "people_scene", "gate_verdict": "rejected",
          "gate_failing": ["specificity"], "gate_issues": []}, True),
        ({"archetype_rendered": "people_scene", "gate_verdict": "accepted",
          "gate_issues": ["pop 12/14"]}, False),
        ({"archetype_rendered": "people_scene", "used_avatar": True,
          "gate_issues": ["generic"]}, False),
        ({"archetype_rendered": "editorial_concept", "gate_issues": ["generic"]}, False),
        ({"gate_issues": ["stock photo look"]}, True),
    ])
    def test_the_rule(self, info, expected):
        assert nc.people_render_unrelated(info, _concept()) is expected

    def test_the_cover_becomes_a_typeset_card(self, tmp_path):
        info: dict = {}
        with patch.object(nc, "assets_dir", str(tmp_path)):
            path = nc.typeset_cover("53.7% of LinkedIn posts miss their mark", _concept(), "",
                                    "Christopher Queen", info, "people_generic", 3)
            plain = nc.typeset_cover("Buyers decide before the call", None, "", None, {},
                                     "x", None)
        assert nc.is_cover_ratio(path) and info["typeset_layout"] == "number_led"
        assert info["archetype_rendered"] == "typeset_card" and info["gate_verdict"] == "code_drawn"
        assert nc.shipped_cover_layout(info, _concept()) == nc.COVER_LAYOUT_TYPESET
        assert nc.is_cover_ratio(plain)
        assert nc.typeset_cover("", None, "", None, {}, "x") is None

    def test_a_card_that_cannot_draw_is_none(self, tmp_path):
        with patch.object(nc, "assets_dir", str(tmp_path)), \
                patch("cqc_lem.utilities.ai.image_graphics.render_typeset_card",
                      side_effect=RuntimeError("no font")):
            assert nc.typeset_cover("A hook", None, "", None, {}, "x") is None


def _brief(concept=None, prompt="a prompt", hook=None):
    from cqc_lem.utilities.ai.image_brief import ImageBrief

    return ImageBrief(prompt=prompt, ratio=nc.COVER_IMAGE_RATIO, surface="newsletter",
                      style_preset="newsletter", focal_concept="f", concept=concept,
                      hook_text=hook)


def _render(tmp_path, size=(1920, 1080)):
    path = tmp_path / "render.png"
    Image.new("RGB", size, (30, 40, 50)).save(path)
    return str(path)


class TestCoverGenerationWiring:
    def _generate(self, tmp_path, concept, brief, gate, recent=()):
        assets = tmp_path / "assets"
        assets.mkdir(exist_ok=True)
        with patch.object(nc, "assets_dir", str(assets)), \
                patch("cqc_lem.assets_dir", str(assets)), \
                patch.object(nc, "resolve_people_cover",
                             return_value=(concept, None, nc.PEOPLE_POLICY_STOCK)), \
                patch.object(nc, "recent_cover_layouts", return_value=list(recent)), \
                patch("cqc_lem.utilities.ai.image_concept.analyze_content_for_image",
                      return_value=concept), \
                patch("cqc_lem.utilities.ai.image_brief.build_image_brief", return_value=brief), \
                patch("cqc_lem.utilities.ai.image_gen.render_image_gated",
                      side_effect=gate) as render:
            rel, reason = nc.generate_cover_for_edition(3, 9, "The Title", None, "Body.")
            receipt = None
            if rel:
                from cqc_lem.utilities.media_provenance import read_brief_receipt

                receipt = read_brief_receipt(nc.cover_public_url(rel))
        return rel, reason, receipt, render

    def test_no_concept_and_a_people_prompt_never_renders(self, tmp_path):
        brief = _brief(prompt="A candid hallway conversation between two colleagues.")
        rel, reason, receipt, render = self._generate(tmp_path, None, brief, None)
        assert reason is None and not render.called
        assert receipt["archetype_rendered"] == "typeset_card"
        assert receipt["cover_composed"] == "people_without_concept"

    def test_a_generic_stock_render_is_re_set(self, tmp_path):
        concept = _concept(layout="full_bleed")
        raw = _render(tmp_path)

        def gate(*_a, **kwargs):
            kwargs["render_info"].update({"gate_verdict": "accepted",
                                          "archetype_rendered": "people_scene",
                                          "gate_issues": ["scene is generic"]})
            return raw

        rel, reason, receipt, _ = self._generate(tmp_path, concept, _brief(concept, hook="Hook"),
                                                 gate)
        assert reason is None and receipt["cover_composed"] == "people_generic"
        assert receipt["people_policy"] == nc.PEOPLE_POLICY_STOCK

    def test_a_split_past_the_budget_is_re_set_as_a_card(self, tmp_path):
        concept = _concept(treatment="editorial_concept", archetype="stat_card",
                           archetype_ranking=("stat_card", "editorial_concept"),
                           human_moment=False)
        raw = _render(tmp_path)

        def gate(*_a, **kwargs):
            kwargs["render_info"].update({"gate_verdict": "code_drawn",
                                          "archetype_rendered": "stat_card"})
            return raw

        rel, reason, receipt, _ = self._generate(tmp_path, concept, _brief(concept, hook="Hook"),
                                                 gate, recent=["split_left", "split_right"])
        assert reason is None and receipt["cover_composed"] == "split_budget"
        assert receipt["cover_layout"] == nc.COVER_LAYOUT_TYPESET


# --- No more than 2 splits in any 5 covers ---------------------------------------------------------

class TestSplitBudget:
    def test_the_budget(self):
        assert nc.split_budget_spent(["split_left", "full_bleed", "split_right"])
        assert not nc.split_budget_spent(["split_left", "full_bleed", "typeset_card"])
        assert not nc.split_budget_spent(None)
        # Only the last four count: with the new one, that is five.
        assert not nc.split_budget_spent(["full_bleed", "typeset_card", "full_bleed",
                                          "split_left", "split_right"])

    def test_any_five_covers_hold_at_most_two_splits(self):
        shipped: list = []
        for _ in range(12):
            concept = nc.fresh_cover_layout(_concept(layout="split_left"), [], shipped)
            shipped.insert(0, concept.layout)
        assert all(sum(nc.is_split_layout(x) for x in shipped[i:i + 5]) <= 2
                   for i in range(len(shipped) - 4))

    def test_recent_layouts_read_what_shipped(self):
        receipts = [{"archetype_rendered": "typeset_card", "concept": {"layout": "split_left"}},
                    {"cover_layout": "full_bleed", "concept": {"layout": "split_left"}},
                    {"concept": {"layout": "split_right"}}, {"concept": {}}]
        with patch.object(nc, "_recent_cover_receipts", return_value=receipts):
            assert nc.recent_cover_layouts(1) == ["typeset_card", "full_bleed", "split_right"]

    def test_shipped_layout(self):
        assert nc.shipped_cover_layout({"cover_layout": "split_left"}, None) == "split_left"
        drawn = {"archetype_rendered": "stat_card"}
        assert nc.shipped_cover_layout(drawn, _concept(layout="full_bleed")) == "split_left"
        assert nc.shipped_cover_layout(drawn, _concept(layout="split_right")) == "split_right"
        assert nc.shipped_cover_layout({}, _concept(layout="full_bleed")) == "full_bleed"


class TestFullBleedReachesTheCover:
    def test_a_full_bleed_cover_renders_at_the_cover_ratio(self):
        assert image_gen._scene_ratio("16:9", "Hook", "newsletter", "full_bleed") == "16:9"
        assert image_gen._scene_ratio("16:9", "Hook", "newsletter", "split_left") == "1:1"
        assert image_gen._scene_ratio("16:9", "Hook", "newsletter") == "1:1"
        assert image_gen._scene_ratio("16:9", None, "newsletter", "full_bleed") == "16:9"

    def test_a_landscape_full_bleed_render_is_fitted_not_split(self, tmp_path):
        raw = _render(tmp_path, (1536, 1024))
        info = {"raw_render_path": raw}
        out = nc.ensure_cover_ratio(raw, "Buyers decide before the call", info,
                                    concept=_concept(layout="full_bleed", kicker="AI"),
                                    brand="", byline="Christopher Queen")
        assert nc.is_cover_ratio(out) and info["cover_composed"] == "ratio_fit_full_bleed"
        assert info["cover_layout"] == "full_bleed"

    def test_the_compositor_reports_the_layout_it_used(self, tmp_path):
        dark, light = _render(tmp_path), str(tmp_path / "light.png")
        Image.new("RGB", (1920, 1080), (250, 250, 250)).save(light)
        report: dict = {}
        icomp.compose_headline(dark, "A hook here", layout="full_bleed", report=report)
        assert report["layout"] == "full_bleed"
        report = {}
        icomp.compose_headline(light, "A hook here", layout="full_bleed", report=report)
        assert report["layout"] == icomp.OVERLAY_FALLBACK_LAYOUT

    def test_the_gate_loop_records_the_composited_layout(self, tmp_path):
        raw = _render(tmp_path)
        info: dict = {}
        verdict = image_gen.QualityVerdict(acceptable=True, checked=True)
        with patch.object(image_gen, "_render_with_backend", return_value=(raw, "gpt-image")), \
                patch.object(image_gen, "inspect_render_quality", return_value=verdict), \
                patch("cqc_lem.utilities.observability.track_image_gate_verdict"):
            image_gen.render_image_gated("p", surface="newsletter", ratio="16:9",
                                         concept=_concept(layout="full_bleed",
                                                          archetype="editorial_concept",
                                                          archetype_ranking=("editorial_concept",)),
                                         hook_text="A hook here", render_info=info)
        assert info["cover_layout"] == "full_bleed"


# --- Decks: three covers in any five, never the same inside slides --------------------------------

class TestDeckCovers:
    def test_a_cover_two_of_the_last_four_decks_used_goes_behind_the_template(self):
        chain = crc.deck_cover_chain("Route by cost", False, False, ["poster", "x", "poster"])
        assert chain == [crc.DECK_COVER_TEMPLATE, crc.DECK_COVER_POSTER]
        assert crc.deck_cover_capped("poster", ["poster", "a", "poster", "b"])
        assert not crc.deck_cover_capped("poster", ["a", "b", "c", "d", "poster", "poster"])

    def test_a_capped_template_goes_last(self):
        recent = ["template", "poster", "template", "poster"]
        chain = crc.deck_cover_chain("Route by cost", False, False, recent)
        assert chain == [crc.DECK_COVER_POSTER, crc.DECK_COVER_TEMPLATE]

    def test_any_five_decks_open_on_three_covers_when_the_ai_cover_keeps_failing(self):
        # Round 10: the concept cover was picked, refused by its judge, and the chain fell back to
        # the poster four times. Simulate a judge that refuses every concept cover.
        shipped: list = []
        for n in range(10):
            chain = crc.deck_cover_chain("Route by cost", True, True, shipped, seed=str(n))
            shipped.insert(0, next(t for t in chain if t != crc.DECK_COVER_CONCEPT))
        assert all(len(set(shipped[i:i + 5])) >= 3 for i in range(len(shipped) - 4))


class TestDeckInsideSlides:
    def test_a_recent_template_motif_and_badge_are_refused(self):
        # 128 and 144: stat_reveal / dots / rule, four decks apart.
        recent = ["bracket", "arc", "stripes", "dots"]
        templates = ["minimal_dark", "step_framework", "bold_listicle", "stat_reveal"]
        badges = ["rule", "rule", "numbered_circle", "rule"]
        assert crc.deck_motif(recent, "s") == "dots"
        got = crc.deck_motif(recent, "s", "stat_reveal", templates, "rule", badges)
        assert got != "dots"
        history = {"motif": recent, "template": templates, "badge": badges}
        assert crc.internal_set_repeats("stat_reveal", "dots", "rule", history)
        assert not crc.internal_set_repeats("stat_reveal", got, "rule", history)

    def test_any_five_decks_use_three_templates(self):
        recent: list = []
        for _ in range(10):
            recent.insert(0, crc.rotate_deck_template("stat_reveal", recent))
        assert all(len(set(recent[i:i + 5])) >= 3 for i in range(len(recent) - 4))

    def test_the_renderer_logs_a_set_with_no_free_motif(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        root = tmp_path / "carousel"
        for n, motif in enumerate(crc.DECK_MOTIFS, start=1):
            old = root / str(n)
            old.mkdir(parents=True)
            with open(crc.deck_render_receipt_path(str(old)), "w") as fh:
                json.dump({"user_id": 7, "template": "stat_reveal", "motif": motif,
                           "badge": "rule"}, fh)
        deck = crc.EducationalContentCarousel(**{
            "cover": {"title": "Three checks before you pay", "content": "From one mistake."},
            "contents": [{"title": "Ask for a date", "content": "Tie it to something usable."}],
            "call_to_action": {"title": "Your turn", "content": "Which check is hardest?"}})
        with patch.object(crc, "retain_carousel_keyframes"), \
                patch("cqc_lem.utilities.logger.log_info") as info:
            crc.create_carousel_slide_images(deck, post_id=9, output_dir=str(root / "9"),
                                             template="stat_reveal", user_id=7)
        assert any("repeat a recent deck" in str(c[0][0]) for c in info.call_args_list)
        assert os.path.isdir(root / "9")


# --- Motion: a reset is a cut, never a count down -------------------------------------------------

class TestMotionReset:
    def test_the_data_layer_sits_blank_before_it_builds(self):
        blank = md.MOTION_HOLD_SECONDS + md._RESET_SECONDS + 0.01
        assert md._data_clock(blank) == (md._DONE, 0.0)
        assert md._data_clock(md._BUILD_AT + 0.5)[1] == 1.0
        assert md._data_clock(0.1) == (md._DONE, 1.0)
        assert md.RESET_BLANK_SECONDS >= 0.25

    def test_a_visible_counter_never_goes_down(self):
        shown = []
        t = 0.0
        while t < 6.0:
            build, alpha = md._data_clock(t)
            progress = md._progress(build, 0.0, md._COUNT_SECONDS) if build < md._DONE else 1.0
            shown.append((alpha, float(md.counter_text("55", progress))))
            t += 1 / 24
        visible_runs, run = [], []
        for alpha, value in shown:
            if alpha > 0:
                run.append(value)
            elif run:
                visible_runs.append(run)
                run = []
        visible_runs.append(run)
        # Within every visible stretch the count only climbs; between stretches the layer is gone.
        assert all(a <= b for r in visible_runs for a, b in zip(r, r[1:]))
        blank_frames = sum(1 for alpha, _ in shown if alpha == 0)
        assert blank_frames >= int(md.RESET_BLANK_SECONDS * 24) - 1


# --- A title card's caption band holds to the end -------------------------------------------------

class TestTitleCardCaptionHold:
    def test_a_title_card_cue_runs_the_whole_video(self):
        with patch.object(vc, "video_duration_seconds", return_value=7.0):
            assert vc.caption_hold_seconds("title_card_1.mp4", "Can AI draft?") == 7.0
            assert vc.caption_hold_seconds("clip.mp4", None) == vc.VIDEO_CAPTION_HOLD_SECONDS
        with patch.object(vc, "video_duration_seconds", return_value=1.0):
            assert vc.caption_hold_seconds("t.mp4", "h") == vc.VIDEO_CAPTION_HOLD_SECONDS

    def test_the_cue_is_written_with_that_hold(self, tmp_path):
        video = tmp_path / "title_card_5.mp4"
        video.write_bytes(b"x")
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True), \
                patch("cqc_lem.utilities.video_title_card.card_headline", return_value="Head"), \
                patch.object(vc, "caption_lines", return_value=["Line one"]), \
                patch.object(vc, "caption_srt_dir", return_value=str(tmp_path)), \
                patch.object(vc, "video_duration_seconds", return_value=8.0), \
                patch.object(vc, "build_caption_srt", return_value=None) as srt, \
                patch.object(vc, "burn_captions", return_value=False):
            vc.apply_captions_to_video(str(video), "Post text")
        assert srt.call_args[1]["hold_seconds"] == 8.0
