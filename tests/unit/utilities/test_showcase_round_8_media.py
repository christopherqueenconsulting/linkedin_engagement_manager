"""Showcase round 8: batch layout variety, moving GIFs, covers and the deck cover element.

Each test names the round-8 sample it was written from (docs/image-stack.md and
docs/motion-design.md, "Showcase round 8").
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageChops

from cqc_lem.utilities import (
    animated_loop as al,
    carousel_creator as crc,
    motion_design as md,
    newsletter_cover as nc,
    video_captions as vc,
    video_title_card as tc,
)
from cqc_lem.utilities.ai import (
    image_brief as ib,
    image_compose as icp,
    image_concept as ic,
    image_graphics as ig,
    post_treatment as pt,
)

pytestmark = pytest.mark.unit


def _concept(**kw) -> ic.ImageConcept:
    base = dict(thesis="AI spend hides in seat licences", audience="ops", specific_entities=(),
                emotional_beat="calm", hook_phrase="", treatment="editorial_concept",
                treatment_rationale="x")
    base.update(kw)
    return ic.ImageConcept(**base)


# --- Quote cards and typeset cards -----------------------------------------------------------------

class TestQuoteCapBinds:
    def test_a_typeset_card_never_sets_quote_marks(self):
        # rhythm_6: meta said typeset_card, the render was a quote card.
        assert ig.CARD_QUOTE_MARKS not in ig.typeset_layouts_for("Exact words.", verbatim=True)

    def test_a_quote_marks_receipt_counts_as_a_quote_card(self):
        receipt = {"rhythm": {"treatment": "typeset_card", "style": pt.STYLE_CODE_DRAWN,
                              "card_layout": "quote_marks"}}
        history = pt.rhythm_history([receipt])
        assert history["style"] == [pt.STYLE_QUOTE_CARD]
        # …so the next post may not be a quote card (slot_125 followed rhythm_6).
        assert pt.TREATMENT_QUOTE_CARD in pt.style_blocks(history["style"])

    def test_one_quote_card_in_six(self):
        assert pt.QUOTE_CAP_WINDOW == 6
        five_back = [None, None, None, None, pt.STYLE_QUOTE_CARD]
        assert pt.TREATMENT_QUOTE_CARD in pt.style_blocks(five_back)


class TestTitleCardGround:
    def test_the_ground_alternates_card_to_card(self):
        # 123, 141, 135: three layouts, one charcoal ground — one template to a reader.
        recent = [(tc.VARIANT_POSTER, tc.GROUND_CHARCOAL)]
        with patch.object(tc, "pick_ground", return_value=tc.GROUND_CHARCOAL):
            _variant, ground = tc.pick_card_style("Routing is a core part of ops", recent, "s")
        assert ground == tc.GROUND_OFF_WHITE

    def test_no_history_keeps_the_seeded_ground(self):
        with patch.object(tc, "pick_ground", return_value=tc.GROUND_CHARCOAL):
            assert tc.pick_card_style("Routing is a core part of ops", [], "s")[1] \
                == tc.GROUND_CHARCOAL


class TestDeckWindow:
    def test_a_pair_five_decks_back_still_moves_back(self):
        # slot_144 repeated slot_128's (stat_reveal, poster) four decks later.
        chain = crc.deck_cover_chain("A title", True, True, ["a", "b", "c", "poster"], "s",
                                     template="stat_reveal",
                                     recent_templates=["x", "y", "z", "stat_reveal"])
        assert chain.index(crc.DECK_COVER_POSTER) > chain.index(crc.DECK_COVER_DRAWN)
        assert crc.DECK_PAIR_WINDOW == 6


_DECK_POST = ("I sent 51 cold emails, and not a single reply.\n\n"
              "Additionally, 18 emails were dispatched in just 23 seconds.")


def _render_deck(tmp_path, **kw):
    import json

    deck = crc.EducationalContentCarousel(**{
        "cover": {"title": "Cold outreach, taken apart", "content": "What went wrong."},
        "contents": [
            {"title": "Too Fast", "content": "18 emails were dispatched in just 23 seconds."},
            {"title": "Wrong People", "content": "Every recipient was an executive."}],
        "call_to_action": {"title": "Your turn", "content": "Which would you fix first?"}})
    out = tmp_path / "deck"
    with patch.object(crc, "retain_carousel_keyframes"):
        crc.create_carousel_slide_images(deck, post_id=143, output_dir=str(out),
                                         template="bold_listicle", user_id=1,
                                         evidence=_DECK_POST, **kw)
    with open(crc.deck_render_receipt_path(str(out))) as fh:
        return json.load(fh)


class TestOneStatPanelPerDeck:
    def test_a_cover_that_borrows_a_slides_element_leaves_that_slide_typographic(
            self, tmp_path, monkeypatch):
        # slot_143: the "18" stat panel drawn on the cover AND on slide 3.
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        receipt = _render_deck(tmp_path, cover_treatment=crc.DECK_COVER_DRAWN)
        assert receipt["cover_treatment"] == crc.DECK_COVER_DRAWN
        assert receipt["slides"][1]["element"] is None

    def test_without_a_drawn_cover_the_slide_keeps_its_element(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_AI_COVER_ENABLED", "false")
        receipt = _render_deck(tmp_path, cover_treatment=crc.DECK_COVER_POSTER)
        assert receipt["slides"][1]["element"] is not None


# --- List markers and kickers ----------------------------------------------------------------------

class TestListMarkers:
    @pytest.mark.parametrize("raw,want", [("- Automate ticket routing.", "Automate ticket routing."),
                                          ("• Pick one task", "Pick one task"),
                                          ("2) Route by complexity", "Route by complexity"),
                                          ("5.6 hours a week", "5.6 hours a week"),
                                          ("-40% churn", "-40% churn")])
    def test_a_leading_marker_is_stripped(self, raw, want):
        assert vc.strip_list_marker(raw) == want

    def test_a_caption_never_sets_the_marker(self):
        # slot_141 / gif_141: the burned line began with a literal "- ".
        post = "Pull the right levers.\n\n- Automate ticket routing: an LLM sends each ticket."
        assert not any(c.startswith("-") for c in vc.payoff_candidates(post, 120,
                                                                      "Pull the right levers."))

    def test_a_title_card_hook_never_sets_it(self):
        assert tc.title_card_hook("x", _concept(hook_phrase="- Route every ticket")) \
            == "Route every ticket"


class TestKickerNeverRepeatsTheHeadline:
    def test_a_repeating_kicker_is_dropped(self):
        # cover_20: "OBSERVE-LOOP" over "OBSERVE-Loop: A Practical Guide…".
        parts = icp.headline_parts("OBSERVE-Loop: A Practical Guide", "OBSERVE-LOOP")
        assert parts.kicker == ""
        assert icp.headline_parts("Flexible infrastructure beats policy", "LLM COSTS").kicker \
            == "LLM COSTS"

    def test_the_typeset_card_drops_it_too(self, tmp_path):
        draw = MagicMock()
        with patch.dict(ig._CARD_DRAWERS, {ig.CARD_POSTER: draw}):
            ig.render_typeset_card("- AI spend cut", kicker="AI SPEND", card_layout="poster",
                                   out_path=str(tmp_path / "c.png"))
        _canvas, hook, kicker, _sig, _pal = draw.call_args.args
        assert hook == "AI spend cut" and kicker == ""


class TestSignatureFits:
    def test_a_long_byline_wraps_inside_the_panel(self):
        # cover_19: "…B2B Tho" ran off the panel.
        sig = ("Christopher Queen · Source: Edelman-LinkedIn B2B Thought Leadership Impact "
               "Report 2025")
        font, _size, lines = icp.fit_signature(sig, 520, 1080)
        assert 1 < len(lines) <= icp.SIGNATURE_MAX_LINES
        draw = __import__("PIL.ImageDraw", fromlist=["Draw"]).Draw(Image.new("RGB", (1, 1)))
        assert all(icp._ink_width(draw, ln, font) <= 520 for ln in lines)

    def test_a_short_byline_is_one_line(self):
        assert icp.fit_signature("Christopher Queen", 900, 1080)[2] == ["Christopher Queen"]

    def test_a_composed_cover_draws_it(self, tmp_path):
        render = tmp_path / "r.png"
        Image.new("RGB", (800, 800), (200, 200, 200)).save(render)
        out = icp.compose_headline(str(render), "Thought leadership wins the 95%",
                                   layout="split_left", surface="newsletter",
                                   signature="Christopher Queen · Source: Edelman-LinkedIn B2B "
                                             "Thought Leadership Impact Report 2025",
                                   out_path=str(tmp_path / "o.png"))
        assert Image.open(out).size == (1920, 1080)


class TestStatPanelContext:
    def test_the_panel_keeps_the_hook_without_its_figure(self):
        # cover_16: "$30K" big on the left, ~70% empty panel on the right.
        assert ig.hook_without_figures("Audit cut $30K AI spend") == "Audit cut AI spend"
        assert ig.hook_without_figures("Saved $12,000") == ""


# --- GIFs ------------------------------------------------------------------------------------------

def _gif(path, frames):
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
    return str(path)


class TestMotionFloor:
    def test_a_static_loop_scores_zero_and_ships_the_still(self, tmp_path):
        # gif_125 / gif_130: no visible motion at 4 MB.
        still = [Image.new("RGB", (160, 160), (30, 30, 30)) for _ in range(12)]
        path = _gif(tmp_path / "s.gif", still)
        assert al.loop_motion_score(path) == 0.0
        with patch.object(al, "log_info") as info:
            assert not al.moves_visibly(path, user_id=1, post_id=2)
        assert "static image ships" in info.call_args.args[0]

    def test_a_moving_loop_clears_it(self, tmp_path):
        frames = []
        for n in range(12):
            img = Image.new("RGB", (160, 160), (30, 30, 30))
            img.paste((230, 200, 40), (n * 10, 40, n * 10 + 40, 120))
            frames.append(img)
        path = _gif(tmp_path / "m.gif", frames)
        assert al.loop_motion_score(path) >= al.MOTION_FLOOR
        assert al.moves_visibly(path)

    def test_an_unreadable_file_never_blocks(self, tmp_path):
        assert al.loop_motion_score(str(tmp_path / "missing.gif")) is None
        assert al.moves_visibly(str(tmp_path / "missing.gif"))


class TestCardsAreAnimatedInCode:
    @pytest.mark.parametrize("receipt", [{"archetype_rendered": "quote_card"},
                                         {"archetype_rendered": "typeset_card"},
                                         {"gate_verdict": "last_resort"},
                                         {"render_path": "code_drawn_last_resort"}])
    def test_a_card_never_goes_to_runway(self, receipt):
        assert al._code_drawn(receipt)

    def test_an_ai_render_still_does(self):
        assert not al._code_drawn({"archetype_rendered": "editorial_concept",
                                   "gate_verdict": "accepted"})

    def test_a_quote_card_loops_its_own_quote_in_kinetic_type(self):
        receipt = {"archetype_rendered": "quote_card", "quote": "Those teams stayed stable.",
                   "concept": {"hook_phrase": "No ops needed", "graphic": {"steps": [1]}}}
        with patch.object(md, "create_motion_gif", return_value="g.gif") as make:
            md.loop_from_receipt(receipt, user_id=1, post_id=2)
        assert make.call_args.args[0] == "Those teams stayed stable."
        assert make.call_args.kwargs["graphic"] is None

    def test_a_static_code_drawn_loop_ships_the_still(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANIMATED_POST_ENABLED", "true")
        assets = tmp_path / "assets"
        (assets / "images" / "posts" / "42").mkdir(parents=True)
        (assets / "images" / "posts" / "42" / "img_abc.png").write_bytes(b"png")
        url = "https://api.example.com/api/assets?file_name=images/posts/42/img_abc.png"
        gif = _gif(tmp_path / "made.gif", [Image.new("RGB", (40, 40)) for _ in range(6)])
        with patch("cqc_lem.utilities.post_image.assets_dir", str(assets)), \
                patch("cqc_lem.utilities.media_provenance.read_brief_receipt",
                      return_value={"archetype_rendered": "quote_card", "quote": "q"}), \
                patch("cqc_lem.utilities.motion_design.loop_from_receipt", return_value=gif), \
                patch("cqc_lem.utilities.ai.video_models.create_runway_video") as runway:
            assert al.produce_post_loop(7, 42, "text", url) is None
        runway.assert_not_called()
        assert not os.path.exists(gif)


class TestStatLabelStaysUp:
    def test_mid_count_frames_keep_the_label(self):
        # gif_123: a bare "7" mid-loop, "tests passed" gone.
        graphic = ig.validate_graphic_facts({"thesis_stat": {
            "label": "tests passed", "value": "14", "unit": "",
            "source_sentence": "All 14 tests passed."}}, "All 14 tests passed.")
        plan = md.plan_data(md.STYLE_STAT_COUNTER, graphic, "Fourteen tests, one hour",
                            size=(540, 675), palette=tc.title_card_palette(None, "charcoal"),
                            kicker="AI", byline="Jane", seconds=6.0, mode=md.MODE_GIF)
        plan.background, plan.drift = None, 0
        d = plan.data
        box = (plan.region[0], d["label_y"], plan.region[2],
               d["label_y"] + d["label_h"] * len(d["label_lines"]))
        mid = md.render_motion_frame(plan, md._BUILD_AT + 0.6).crop(box)
        done = md.render_motion_frame(plan, 0.0).crop(box)
        assert ImageChops.difference(mid, done).getbbox() is None

    def test_a_slide_travels_far_enough_to_see(self):
        assert md._SLIDE_EXIT >= 0.12 and md._SLIDE_ENTER >= 0.16


# --- Covers ----------------------------------------------------------------------------------------

class TestCoverRatio:
    def test_a_square_render_is_re_composed_at_16_9(self, tmp_path):
        # cover_18: the avatar LoRA rendered 1:1 and the cover shipped square.
        raw = tmp_path / "raw.png"
        Image.new("RGB", (1024, 1024), (120, 130, 140)).save(raw)
        composed = tmp_path / "composed.png"
        Image.new("RGB", (1024, 1024)).save(composed)
        info = {"raw_render_path": str(raw)}
        out = nc.ensure_cover_ratio(str(composed), "AI posts lose to human ones", info,
                                    concept=_concept(layout="full_bleed", kicker="AI CONTENT"),
                                    brand="", byline="Christopher Queen")
        assert nc.is_cover_ratio(out) and info["cover_composed"] == "ratio_recompose"

    def test_without_a_raw_render_the_composite_is_fitted(self, tmp_path):
        composed = tmp_path / "composed.png"
        Image.new("RGB", (900, 900)).save(composed)
        info: dict = {}
        out = nc.ensure_cover_ratio(str(composed), None, info, concept=None, brand="", byline=None)
        assert Image.open(out).size == nc.COVER_SIZE and info["cover_composed"] == "ratio_fit"

    def test_a_16_9_cover_is_untouched(self, tmp_path):
        path = tmp_path / "ok.png"
        Image.new("RGB", (1920, 1080)).save(path)
        assert nc.ensure_cover_ratio(str(path), "h", {}, concept=None, brand="",
                                     byline=None) == str(path)

    def test_an_unreadable_cover_is_refused(self, tmp_path):
        assert not nc.is_cover_ratio(str(tmp_path / "missing.png"))
        with patch.object(nc, "log_warning") as warn:
            assert nc.ensure_cover_ratio(str(tmp_path / "missing.png"), None, {}, concept=None,
                                         brand="", byline=None) is None
        warn.assert_called_once()


class TestCoverLayoutPairs:
    def test_a_render_on_a_recent_split_moves_side(self):
        # cover_17 and cover_20: two AI renders on split_left, three covers apart. (Round 9: the
        # previous cover's layout is never reused, whatever its family.)
        recent = [("full_bleed", "drawn"), ("split_right", "drawn"), ("split_left", "render")]
        got = nc.fresh_cover_layout(_concept(layout="split_left"), recent)
        assert got.layout == "split_right"

    def test_both_sides_shown_breaks_to_full_bleed(self):
        recent = [("split_right", "render"), ("split_left", "render")]
        assert nc.fresh_cover_layout(_concept(layout="split_left"), recent).layout == "full_bleed"

    def test_a_fresh_pair_or_a_non_cover_layout_is_kept(self):
        concept = _concept(layout="split_left", archetype="stat_card")
        assert nc.fresh_cover_layout(concept, [("split_right", "render")]) is concept
        assert nc.fresh_cover_layout(None, []) is None
        band = _concept(layout="band_top")
        assert nc.fresh_cover_layout(band, [("band_top", "render")]) is band

    def test_pairs_are_read_off_receipts(self):
        receipts = [{"concept": {"layout": "split_left", "archetype": "editorial_concept"}},
                    {"archetype_rendered": "checklist", "concept": {"layout": "split_right"}},
                    {"concept": {}}]
        with patch.object(nc, "_recent_cover_receipts", return_value=receipts):
            assert nc.recent_cover_pairs(1, 4) == [("split_left", "render"),
                                                   ("split_right", "drawn")]


class TestCoverScenes:
    @pytest.mark.parametrize("phrase", ["an engine block full of paper", "a single piston",
                                        "brass clockwork", "a conveyor belt of letters"])
    def test_machinery_is_a_cliche(self, phrase):
        assert ib.cliche_hit(f"A photo of {phrase} on a desk.")

    def test_a_backdrop_is_named_in_the_brand_palette(self):
        out = ib.palette_backdrop_backstop("An engine of paper on a table.")
        assert out.endswith(ib.BRAND_BACKDROP_SENTENCE)
        named = "A ledger on an oak table against a warm off-white wall."
        assert ib.palette_backdrop_backstop(named) == named
        assert ib.palette_backdrop_backstop("") == ""
