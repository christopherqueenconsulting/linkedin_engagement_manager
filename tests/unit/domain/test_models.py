"""The three domain types the restructure audit warranted (issue #1220).

What is worth pinning here is not the field lists — it is the two properties the readers depend on:
a `PostEngagementRow` is STILL a tuple (so an index-based caller cannot be broken by the rename),
and a SHORT row pads instead of raising, which is how the documented four-column minimum keeps
working. Plus the row-layout contract itself: the SELECT in `get_post_engagement_rows` is the only
definition of that order, and the two readers now trust this type for it.
"""

import ast
import datetime as dt
import pathlib

import pytest

from cqc_lem.domain.models import FeedRunContext, PostDraftContext, PostEngagementRow

pytestmark = pytest.mark.unit


class TestPostEngagementRow:
    def test_names_the_columns_the_readers_used_to_index(self):
        row = PostEngagementRow(dt.datetime(2026, 8, 1), 10, 2, 1, "case_snapshot", "question",
                                "text", "pricing", "awareness", 1000)
        assert (row.scheduled_time, row.reactions, row.comments, row.reposts) == (
            dt.datetime(2026, 8, 1), 10, 2, 1)
        assert (row.archetype, row.hook_style, row.format, row.topic, row.buyer_stage) == (
            "case_snapshot", "question", "text", "pricing", "awareness")
        assert row.impressions == 1000

    def test_is_still_a_tuple_so_positional_callers_keep_working(self):
        row = PostEngagementRow.from_row((dt.datetime(2026, 8, 1), 10, 2, 1, None, None, None,
                                          None, None, 1000))
        assert isinstance(row, tuple)
        assert row[0] == dt.datetime(2026, 8, 1) and row[9] == 1000
        assert len(row) == 10

    def test_a_short_row_pads_rather_than_raising(self):
        """The four-column minimum both readers document — a missing column is UNKNOWN, not an error."""
        row = PostEngagementRow.from_row((dt.datetime(2026, 8, 1), 5, 1, 0))
        assert row.impressions is None and row.archetype is None
        assert row.reactions == 5

    def test_extra_trailing_columns_are_ignored(self):
        """A widened SELECT must not break scoring before its reader is taught the new column."""
        row = PostEngagementRow.from_row((None, 1, 2, 3, None, None, None, None, None, 9, "extra"))
        assert len(row) == 10 and row.impressions == 9

    def test_coercing_an_already_named_row_is_a_no_op(self):
        row = PostEngagementRow(reactions=3)
        assert PostEngagementRow.from_row(row) is row

    def test_the_field_order_matches_the_repository_select(self):
        """The ONE place the column order is defined is that SELECT; this fails if they drift."""
        import cqc_lem
        source = (pathlib.Path(cqc_lem.__file__).parent
                  / "platform" / "db" / "repositories" / "posts.py").read_text()
        tree = ast.parse(source)
        fn = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "get_post_engagement_rows")
        sql = " ".join(part.value for part in ast.walk(fn)
                       if isinstance(part, ast.Constant) and isinstance(part.value, str))
        selected = sql[sql.index("SELECT ") + 7:sql.index(" FROM ")]
        columns = [col.strip().split(".")[-1].strip("`") for col in selected.split(",")]
        assert columns == list(PostEngagementRow._fields)


class TestFeedRunContext:
    def test_the_run_accumulators_are_shared_not_copied(self):
        """The dedup set and the rotation/history lists are the RUN's — a pass appends to them."""
        seen, shapes, recent = set(), [], []
        ctx = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=1, prefs={},
                             seen=seen, used_comment_shapes=shapes, recent_comments=recent)
        ctx.seen.add("feedurn://x")
        ctx.used_comment_shapes.insert(0, "expander")
        ctx.recent_comments.insert(0, "a comment")
        assert seen == {"feedurn://x"} and shapes == ["expander"] and recent == ["a comment"]

    def test_it_is_frozen_so_a_callee_cannot_swap_a_gate_s_input(self):
        ctx = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=1, prefs={})
        with pytest.raises(Exception):
            ctx.recent_comments = []

    def test_two_contexts_do_not_share_default_accumulators(self):
        a = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=1, prefs={})
        b = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=2, prefs={})
        a.seen.add("x")
        assert b.seen == set()

    def test_no_deadline_never_runs_out_of_time(self):
        ctx = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=1, prefs={})
        assert ctx.out_of_time(1_000_000.0) is False

    def test_a_deadline_expires_exactly_at_it(self):
        ctx = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=1, prefs={},
                             deadline_ts=100.0)
        assert ctx.out_of_time(99.9) is False
        assert ctx.out_of_time(100.0) is True

    def test_the_group_flag_is_off_by_default(self):
        """A roster/home-feed run must never take the group lane's composer-first ordering."""
        ctx = FeedRunContext(driver=None, wait=None, my_profile=None, user_id=1, prefs={})
        assert ctx.is_group_feed is False


class TestPostDraftContext:
    def _draft(self, **kwargs):
        base = dict(user_id=1, stage="awareness", post_type="blog_summary", user_profile="profile",
                    prefs={"tone": "direct"}, profile_synthesis="synthesis",
                    blueprint={"format": "case_snapshot"}, post_id=7, lead_magnet_cta="comment GUIDE",
                    history_directive="avoid X", story_directive="anchor on Y", content_mix="value")
        base.update(kwargs)
        return PostDraftContext(**base)

    def test_a_retry_keeps_the_planned_post_and_only_changes_the_type(self):
        retry = self._draft().with_post_type("personal_story")
        assert retry.post_type == "personal_story"
        assert retry.blueprint == {"format": "case_snapshot"}
        assert retry.story_directive == "anchor on Y"
        assert retry.lead_magnet_cta == "comment GUIDE"
        assert retry.history_directive == "avoid X"
        assert retry.content_mix == "value"
        assert retry.post_id == 7

    def test_a_retry_stands_the_once_per_post_gates_down(self):
        """Refinement, authenticity, humanization and the review gate run in the OUTERMOST call."""
        retry = self._draft().with_post_type("industry_news")
        assert retry.refine_final_post is False and retry.similarity_check is False

    def test_the_original_is_untouched(self):
        draft = self._draft()
        draft.with_post_type("industry_news")
        assert draft.post_type == "blog_summary"
        assert draft.refine_final_post is True and draft.similarity_check is True
