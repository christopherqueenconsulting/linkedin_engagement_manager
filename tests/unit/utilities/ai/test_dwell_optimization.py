"""Unit tests for the dwell-time optimization core (issue #391 — C2): the writer-side directives
that shape generation for hold time, the deterministic dwell-proxy score, and the no-LLM structural
repair. Dwell is the dominant 2026 ranking signal, so the measuring side must stay deterministic —
no LLM, no I/O — and the score must never gate a post.
"""

import pytest

from cqc_lem.utilities.ai import content_framework as fw

pytestmark = pytest.mark.unit

# A well-shaped post: hook inside the fold, ~200 words, short paragraphs, a numbered block.
_GOOD_POST = """Most teams measure LinkedIn the wrong way, and it costs them reach every week.

Likes are the cheapest signal on the platform. A reader who taps like in half a second tells the
feed almost nothing about whether your post was worth reading.

Hold time is the signal that actually moves distribution now. Last quarter I rewrote 12 of our
posts to open a loop in the first line instead of answering it, and average read time went from
14 seconds to 71 seconds on the same topics.

Here is the shape that worked:

1. Open a gap in the first line and leave it open until after the fold.
2. Keep every paragraph under three lines so the eye keeps descending.
3. Put one number, one named example, or one concrete step in every section.
4. Close with a question narrow enough that answering takes ten seconds.

None of that is clever writing. It is structure, and structure is the part you can actually
control before you hit post. The clever line either lands or it does not, but a reader who can
scan your post will always give you more of their attention than one who cannot.

What is the longest a post has ever held you, and what did it do differently?
"""

# One giant paragraph: hook runs past the fold, no white space, nothing to scroll back to.
_WALL_POST = (
    "There is a lot to say about how professionals should think about measuring engagement on "
    "social platforms in the current year, and the truth is that most of the advice circulating "
    "is either outdated or was never true in the first place, which makes it hard to know where "
    "to start. Likes are cheap. Hold time is what matters, and the way you earn hold time is by "
    "opening a gap early and refusing to close it until the reader has committed to the post."
)


class TestDwellMetrics:
    def test_measures_length_read_time_and_structure(self):
        m = fw.dwell_metrics(_GOOD_POST)
        assert m["words"] > 100
        assert m["read_seconds"] == pytest.approx(m["words"] / fw.DWELL_READ_WPM * 60, abs=0.1)
        assert m["hook_within_fold"] is True
        assert m["hook_chars"] <= fw.LINKEDIN_FOLD_CHARS
        assert m["paragraphs"] >= fw.DWELL_MIN_PARAGRAPHS
        assert m["wall_of_text"] is False
        assert m["has_list"] is True
        assert m["fold_text"] == _GOOD_POST.strip()[:fw.LINKEDIN_FOLD_CHARS]

    def test_flags_wall_of_text_and_missing_fold_hook(self):
        m = fw.dwell_metrics(_WALL_POST)
        assert m["hook_within_fold"] is False
        assert m["wall_of_text"] is True
        assert m["paragraphs"] == 1
        assert m["has_list"] is False

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_content_is_measurable_not_explosive(self, text):
        m = fw.dwell_metrics(text)
        assert m["chars"] == 0 and m["words"] == 0 and m["paragraphs"] == 0
        assert m["hook_within_fold"] is False
        assert fw.dwell_score(text) == 0

    @pytest.mark.parametrize("line", ["1. Do the thing", "- Do the thing", "• Do the thing",
                                      "Step 2 is where it breaks"])
    def test_list_shapes_detected(self, line):
        assert fw.dwell_metrics(f"Hook line.\n\n{line}\n\nClose.")["has_list"] is True

    def test_prose_with_a_period_is_not_a_list(self):
        assert fw.dwell_metrics("Hook line.\n\nWe shipped it. It worked.")["has_list"] is False


class TestDwellScore:
    def test_well_shaped_post_scores_high(self):
        report = fw.dwell_report(_GOOD_POST)
        assert report["score"] >= 90
        assert report["issues"] == []
        assert report["components"]["hook_fold"] == 30.0
        assert report["components"]["read_time"] == 30.0
        assert report["components"]["structure"] == 15.0

    def test_wall_of_text_scores_low_with_reasons(self):
        report = fw.dwell_report(_WALL_POST)
        assert report["score"] < fw.DWELL_SCORE_MIN_DEFAULT
        assert report["components"]["hook_fold"] < 30.0
        assert any("wall-of-text" in i for i in report["issues"])
        assert any("fold" in i for i in report["issues"])

    def test_score_is_bounded_and_components_sum(self):
        for text in (_GOOD_POST, _WALL_POST, "Short.", "Hook.\n\nBody.\n\nMore.\n\nClose."):
            report = fw.dwell_report(text)
            assert 0 <= report["score"] <= 100
            assert report["score"] == int(round(sum(report["components"].values())))

    def test_too_short_and_too_long_both_lose_read_time_points(self):
        short = fw.dwell_report("A tight hook line.\n\nOne.\n\nTwo.\n\nThree.")
        long_words = " ".join(["word"] * 1200)
        long_post = f"A tight hook line.\n\n{long_words}"
        assert short["components"]["read_time"] < 30.0
        assert any("read time" in i for i in short["issues"])
        assert fw.dwell_report(long_post)["components"]["read_time"] < 30.0

    def test_score_is_deterministic(self):
        assert fw.dwell_score(_GOOD_POST) == fw.dwell_score(_GOOD_POST)

    def test_dwell_score_min_reads_env_at_call_time(self, monkeypatch):
        assert fw.dwell_score_min() == fw.DWELL_SCORE_MIN_DEFAULT
        monkeypatch.setenv("DWELL_SCORE_MIN", "75")
        assert fw.dwell_score_min() == 75
        monkeypatch.setenv("DWELL_SCORE_MIN", "not-a-number")
        assert fw.dwell_score_min() == fw.DWELL_SCORE_MIN_DEFAULT
        monkeypatch.setenv("DWELL_SCORE_MIN", "500")
        assert fw.dwell_score_min() == 100

    def test_read_seconds_tracks_word_count(self):
        assert fw.read_seconds(" ".join(["word"] * fw.DWELL_READ_WPM)) == 60.0
        assert fw.read_seconds("") == 0.0


class TestFoldAndStructureHeuristics:
    """The acceptance contract: generated text meets the fold/structure heuristics."""

    def test_well_shaped_post_passes(self):
        assert fw.meets_dwell_heuristics(_GOOD_POST) is True

    def test_wall_of_text_fails(self):
        assert fw.meets_dwell_heuristics(_WALL_POST) is False

    def test_hook_past_the_fold_fails(self):
        long_hook = "x" * (fw.LINKEDIN_FOLD_CHARS + 1)
        text = f"{long_hook}\n\nTwo.\n\nThree.\n\nFour."
        assert fw.meets_dwell_heuristics(text) is False

    def test_too_few_paragraphs_fails(self):
        assert fw.meets_dwell_heuristics("Hook line.\n\nOnly one more paragraph here.") is False

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_fails(self, text):
        assert fw.meets_dwell_heuristics(text) is False


class TestShapeForDwell:
    def test_reflows_a_wall_into_scannable_paragraphs(self):
        shaped = fw.shape_for_dwell(_WALL_POST)
        assert shaped != _WALL_POST
        m = fw.dwell_metrics(shaped)
        assert m["wall_of_text"] is False
        assert m["paragraphs"] > 1
        # Repair only reflows — no sentence may be lost.
        assert "Hold time is what matters" in shaped
        assert fw.dwell_score(shaped) > fw.dwell_score(_WALL_POST)

    def test_already_scannable_text_untouched(self):
        assert fw.shape_for_dwell(_GOOD_POST) == _GOOD_POST

    def test_never_truncates_a_long_post(self):
        tail = "This closing line must survive the reflow."
        wall = ("A sentence that carries some real weight and detail. " * 40) + tail
        shaped = fw.shape_for_dwell(wall)
        assert shaped.endswith(tail)

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_passthrough(self, text):
        assert fw.shape_for_dwell(text) == text


class TestDwellDirectives:
    def test_dwell_directive_carries_the_fold_readtime_and_structure_rules(self):
        text = fw.dwell_directive()
        assert str(fw.LINKEDIN_FOLD_CHARS) in text
        assert f"{fw.DWELL_TARGET_WORDS_MIN}-{fw.DWELL_TARGET_WORDS_MAX} words" in text
        assert str(fw.DWELL_PARAGRAPH_MAX_CHARS) in text
        assert "'...more'" in text
        assert "checklist" in text.lower()

    def test_post_writing_directive_includes_the_dwell_rules(self):
        text = fw.post_writing_directive()
        assert "DWELL TIME" in text
        assert f"{fw.DWELL_TARGET_WORDS_MIN}-{fw.DWELL_TARGET_WORDS_MAX} words" in text
        # The craft rules it already carried must survive.
        assert "210 characters" in text
        assert "1300-2000" in text
        assert text.rstrip().endswith("no quotes, no labels, no explanation.")

    def test_save_worthy_directive_frames_a_carousel_as_a_reference(self):
        text = fw.save_worthy_directive("carousel")
        assert "CAROUSEL" in text
        for token in ("checklist", "playbook", "cover slide", "final slide", "save this"):
            assert token in text.lower()
        # Save-worthy framing must never become engagement bait.
        assert "follow for more" in text.lower() and "never" in text.lower()

    def test_save_worthy_directive_supports_documents(self):
        assert "DOCUMENT" in fw.save_worthy_directive("document")
