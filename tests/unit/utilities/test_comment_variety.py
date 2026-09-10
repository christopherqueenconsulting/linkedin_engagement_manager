"""Comment variety: a rotation that survives the run, and a shape the fact gate can accept (#2034).

Two causes of the same symptom — production comments that recycled the same three anecdotes and
ended on the same closing question, sampled across three days.

**The rotation reset every run.** `select_blueprint("comment", recent_formats=…)` was handed a list
built fresh at the top of each feed walk, and a walk lands one or two comments. So the rotation had
nothing to rotate away from.

**One archetype could not be satisfied at all.** `evidence_add` requires "ONE concrete number… a
real figure the commenter actually knows", while `fact_grounding_severity("comment")` is HARD and
`has_unsourced_specifics` rejects any number not in the author's own material. Handed to a writer
with an empty story bank the two contracts are mutually exclusive: the draft regenerates until
`COMMENT_GATE_MAX_ATTEMPTS` runs out and the post is skipped. 18 comments lost that way in three
days, each having spent its whole budget first.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.comment_rotation import (
    RECENT_SHAPE_MEMORY,
    recent_comment_shapes,
    record_comment_shape,
)

pytestmark = pytest.mark.unit

_ROT = "cqc_lem.utilities.comment_rotation"


class TestTheRotationSurvivesTheRun:
    def test_shapes_are_read_back_most_recent_first(self):
        client = MagicMock()
        client.lrange.return_value = [b"evidence_add", b"expander"]
        with patch(f"{_ROT}.shared_redis_client", return_value=client):
            assert recent_comment_shapes(1) == ["evidence_add", "expander"]

    def test_a_recorded_shape_is_capped_and_expires(self):
        client = MagicMock()
        with patch(f"{_ROT}.shared_redis_client", return_value=client):
            record_comment_shape(1, "expander")
        client.lpush.assert_called_once()
        client.ltrim.assert_called_once_with(client.ltrim.call_args.args[0], 0, RECENT_SHAPE_MEMORY - 1)
        client.expire.assert_called_once()

    def test_no_redis_degrades_to_the_per_run_rotation(self):
        """Exactly the behaviour this replaces — never to "no comment"."""
        with patch(f"{_ROT}.shared_redis_client", return_value=None):
            assert recent_comment_shapes(1) == []
            record_comment_shape(1, "expander")  # must not raise

    def test_a_redis_error_is_swallowed(self):
        client = MagicMock()
        client.lrange.side_effect = RuntimeError("connection reset")
        client.lpush.side_effect = RuntimeError("connection reset")
        with patch(f"{_ROT}.shared_redis_client", return_value=client):
            assert recent_comment_shapes(1) == []
            record_comment_shape(1, "expander")  # must not raise

    def test_an_empty_shape_records_nothing(self):
        client = MagicMock()
        with patch(f"{_ROT}.shared_redis_client", return_value=client):
            record_comment_shape(1, None)
            record_comment_shape(1, "")
        client.lpush.assert_not_called()


class TestTheFactAnchoredShapeIsKeptOffAnUnsourcedMenu:
    def test_evidence_add_is_declared_fact_anchored(self):
        from cqc_lem.utilities.ai.content_framework import fact_anchored_formats

        assert "evidence_add" in fact_anchored_formats("comment")

    def test_an_author_with_no_numbers_reads_as_unsourced(self):
        from cqc_lem.app.engagement.feed import _has_sourced_facts

        with patch("cqc_lem.app.engagement.feed.get_story_bank_entries",
                   return_value=[{"body": "We rebuilt the deploy pipeline after an outage."}]):
            assert _has_sourced_facts(1) is False

    def test_one_number_anywhere_in_the_bank_is_enough(self):
        from cqc_lem.app.engagement.feed import _has_sourced_facts

        with patch("cqc_lem.app.engagement.feed.get_story_bank_entries",
                   return_value=[{"body": "no figures here"},
                                 {"body": "deploys went from 22 minutes to 9"}]):
            assert _has_sourced_facts(1) is True

    def test_an_unreadable_bank_fails_closed(self):
        """Unreadable reads as "no facts", which keeps the fact-anchored shapes off the menu.

        Being wrong that way costs a slightly narrower rotation; being wrong the other way costs
        the skipped post this exists to prevent.
        """
        from cqc_lem.app.engagement.feed import _has_sourced_facts

        with patch("cqc_lem.app.engagement.feed.get_story_bank_entries",
                   side_effect=RuntimeError("db down")):
            assert _has_sourced_facts(1) is False

    def test_an_empty_bank_reads_as_unsourced(self):
        from cqc_lem.app.engagement.feed import _has_sourced_facts

        with patch("cqc_lem.app.engagement.feed.get_story_bank_entries", return_value=[]):
            assert _has_sourced_facts(1) is False

    def test_excluding_everything_still_yields_a_blueprint(self):
        """Excluding everything still yields a blueprint.

        `select_blueprint` falls back to the full menu rather than to nothing, so an author with no
        sourced facts still gets a comment — just never the shape that demands a number.
        """
        from cqc_lem.utilities.ai.content_framework import (
            fact_anchored_formats,
            select_blueprint,
        )

        blueprint = select_blueprint("comment", exclude_formats=fact_anchored_formats("comment"))
        assert blueprint and blueprint.get("format")
