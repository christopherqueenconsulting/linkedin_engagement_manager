"""Showcase round 6: the title card's byline and its burned caption.

The byline is never under the caption band, and the caption on a title card carries the
post's payoff, never the headline again.
"""

from unittest.mock import patch

import pytest

pytest.importorskip("PIL")

from cqc_lem.utilities import video_captions as vc, video_title_card as tc  # noqa: E402

pytestmark = pytest.mark.unit

POST = ("I gave Claude a 55-minute assessment and walked away. I came back to 14 passing tests and "
        "a 90 GB script nobody had noticed.\n\nPick one workflow that moves the bottom line.")
HEADLINE = "I gave Claude a 55-minute assessment and walked away."
LONG = ("Small-business owners who wait for the perfect AI tool keep paying for the old one while "
        "the right one sits unused")


def _byline_bottom(layout) -> int:
    return layout.byline_xy[1] + round(layout.byline_font.size * 1.25)


class TestTheBylineClearsTheCaption:
    @pytest.mark.parametrize("size", [(1080, 1080), (1080, 1920)])
    @pytest.mark.parametrize("variant", tc.TITLE_CARD_VARIANTS)
    @pytest.mark.parametrize("hook", [HEADLINE, LONG, "3 of 5 deploys failed on Friday nights"])
    def test_the_byline_sits_above_the_worst_case_band(self, size, variant, hook):
        layout = tc.plan_title_card(hook, size=size, palette=tc.title_card_palette(),
                                    kicker="AI COSTS", byline="Christopher Queen",
                                    variant=variant)
        assert _byline_bottom(layout) <= vc.caption_band_top(size)
        for word in layout.words:
            assert word.y + layout.hook_size <= vc.caption_band_top(size)

    def test_with_no_room_beneath_the_hook_the_byline_moves_to_the_top_row(self):
        size = (1080, 1080)
        real_fit = tc._fit_lines
        calls = []

        def fit(draw, text, width, height, high, low, max_lines):
            calls.append(height)
            return None if len(calls) == 1 else real_fit(draw, text, width, height, high, low,
                                                         max_lines)

        with patch.object(tc, "_fit_lines", side_effect=fit):
            layout = tc.plan_title_card(HEADLINE, size=size, palette=tc.title_card_palette(),
                                        byline="Christopher Queen")
        assert layout.byline_xy[1] < round(size[1] * 0.09)
        assert calls[1] > calls[0]  # the refit got the byline's space back

    def test_a_hook_that_only_fit_before_round_6_still_sets(self):
        real_fit = tc._fit_lines
        calls = []

        def fit(draw, text, width, height, high, low, max_lines):
            calls.append(height)
            return None if len(calls) < 3 else real_fit(draw, text, width, height, high, low,
                                                        max_lines)

        with patch.object(tc, "_fit_lines", side_effect=fit):
            layout = tc.plan_title_card(HEADLINE, size=(1080, 1080),
                                        palette=tc.title_card_palette(),
                                        byline="Christopher Queen")
        assert len(calls) == 3 and layout.byline_xy[1] < round(1080 * 0.09)

    def test_the_band_top_tracks_the_line_budget(self):
        assert vc.caption_band_top((1080, 1080), max_lines=1) > vc.caption_band_top((1080, 1080),
                                                                                      max_lines=3)


class TestTheCaptionIsThePayoff:
    def test_the_caption_skips_the_sentence_the_headline_came_from(self):
        lines = vc.caption_lines(POST, avoid=HEADLINE)
        caption = " ".join(lines)
        assert caption.startswith("I came back to 14 passing tests")
        assert not vc.same_thought(caption, HEADLINE)

    def test_without_a_headline_the_opening_is_the_caption(self):
        assert " ".join(vc.caption_lines(POST)).startswith("I gave Claude")

    def test_a_headline_not_from_the_opening_still_never_repeats(self):
        candidates = vc.payoff_candidates(POST, 200, "Pick one workflow that moves the bottom line")
        assert candidates and not any("Pick one workflow" in c for c in candidates)

    def test_a_post_that_is_only_its_headline_has_no_caption(self):
        assert vc.caption_lines(HEADLINE, avoid=HEADLINE) == []

    def test_same_thought(self):
        assert vc.same_thought("A b c d", "a b c d e f")
        assert not vc.same_thought("cats sleep", "dogs bark loudly")
        assert not vc.same_thought("", "x")


class TestTheCardHeadline:
    def test_a_rendered_card_remembers_its_headline(self):
        tc.remember_headline("/x/title_card_9_abc.mp4", "The real headline")
        assert tc.card_headline("/assets/videos/runwayml/title_card_9_abc.mp4", POST) == \
            "The real headline"

    def test_an_unremembered_card_reads_the_hook_off_the_post(self):
        assert tc.card_headline("/v/title_card_1_zzz.mp4", POST) == tc.title_card_hook(POST)

    def test_any_other_video_has_none(self):
        assert tc.card_headline("/v/runway_clip.mp4", POST) is None
        assert tc.card_headline(None) is None

    def test_a_hook_fault_is_none(self):
        with patch.object(tc, "title_card_hook", side_effect=ValueError("x")):
            assert tc.card_headline("/v/title_card_2_q.mp4", POST) is None

    def test_the_registry_is_bounded(self):
        for i in range(tc._CARD_HEADLINES_MAX + 5):
            tc.remember_headline(f"title_card_{i}.mp4", "h")
        assert len(tc._CARD_HEADLINES) <= tc._CARD_HEADLINES_MAX
        tc.remember_headline("", "h")
        tc.remember_headline("title_card_x.mp4", "")
        assert "title_card_x.mp4" not in tc._CARD_HEADLINES

    def test_the_burn_is_handed_the_headline_to_avoid(self, tmp_path, monkeypatch):
        video = tmp_path / "title_card_7_abc.mp4"
        video.write_bytes(b"x")
        tc.remember_headline(str(video), HEADLINE)
        monkeypatch.setattr(vc, "caption_srt_dir", lambda: str(tmp_path))
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True), \
             patch.object(vc, "caption_lines", return_value=[]) as lines:
            result = vc.apply_captions_to_video(str(video), POST, post_id=7, user_id=1)
        assert result.skipped_reason == "no_caption_text"
        assert lines.call_args.kwargs["avoid"] == HEADLINE
