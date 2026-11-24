"""Comment outcome reader v2 Phase 2 (#2663): the disagreement tripwire and the v2 backfill."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import engagement as repo

pytestmark = pytest.mark.unit

POST = "cqc_lem.app.engagement.posting"
CUR = "cqc_lem.platform.db.repositories.engagement.db_cursor"


def _ctx(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx, cur


def _row(log_id, version, reply_count, status="checked", at=1):
    return {"log_id": log_id, "reader_version": version, "checked_at": at,
            "status": status, "reply_count": reply_count}


class TestReaderDisagreements:
    def _run(self, rows):
        ctx, _ = _ctx(rows)
        with patch(CUR, return_value=ctx):
            return repo.get_reader_disagreements(1)

    def test_fires_when_followups_saw_replies_and_v2_read_none(self):
        assert self._run([_row(1, 2, 0)]) == {"followup_posts": 1, "disagreeing_posts": 1}

    def test_silent_when_they_agree(self):
        assert self._run([_row(1, 2, 3)])["disagreeing_posts"] == 0

    def test_silent_when_latest_outcome_is_unscored_v1(self):
        assert self._run([_row(1, 1, 0)])["disagreeing_posts"] == 0

    def test_latest_row_wins_over_older_v1(self):
        out = self._run([_row(1, 1, 0, at=1), _row(1, 2, 2, at=2)])
        assert out == {"followup_posts": 1, "disagreeing_posts": 0}

    def test_skipped_v2_is_not_a_disagreement(self):
        assert self._run([_row(1, 2, 0, status="skipped")])["disagreeing_posts"] == 0

    def test_followup_after_the_reading_is_not_evidence(self):
        ctx, cur = _ctx([])
        with patch(CUR, return_value=ctx):
            repo.get_reader_disagreements(1)
        assert "cf.created_at <= co.checked_at" in cur.execute.call_args[0][0]

    def test_db_fault_reads_as_no_disagreement(self):
        with patch(CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.get_reader_disagreements(1) == {"followup_posts": 0, "disagreeing_posts": 0}


class TestTripwire:
    def _fn(self):
        from cqc_lem.app.engagement.posting import check_outcome_reader_disagreement
        return check_outcome_reader_disagreement

    def test_flag_off_does_nothing(self):
        with ExitStack() as es:
            es.enter_context(patch(f"{POST}.comment_outcome_reader_v2", return_value=False))
            read = es.enter_context(patch(f"{POST}.get_reader_disagreements"))
            assert self._fn()(1) == 0
        assert not read.called

    def test_disagreement_warns_once_and_emits(self):
        with ExitStack() as es:
            es.enter_context(patch(f"{POST}.comment_outcome_reader_v2", return_value=True))
            es.enter_context(patch(f"{POST}.get_reader_disagreements",
                                   return_value={"followup_posts": 4, "disagreeing_posts": 2}))
            warn = es.enter_context(patch(f"{POST}.log_warning"))
            emit = es.enter_context(patch(f"{POST}.track_outcome_reader_disagreement"))
            assert self._fn()(7) == 2
        assert warn.call_count == 1
        emit.assert_called_once_with(7, 2, 4)

    def test_agreement_is_silent(self):
        with ExitStack() as es:
            es.enter_context(patch(f"{POST}.comment_outcome_reader_v2", return_value=True))
            es.enter_context(patch(f"{POST}.get_reader_disagreements",
                                   return_value={"followup_posts": 4, "disagreeing_posts": 0}))
            warn = es.enter_context(patch(f"{POST}.log_warning"))
            emit = es.enter_context(patch(f"{POST}.track_outcome_reader_disagreement"))
            assert self._fn()(7) == 0
        assert not warn.called and not emit.called


def test_event_is_registered_with_string_label():
    from cqc_lem.utilities import observability as obs
    spec = obs.EVENTS["outcome_reader_disagreement"]
    with patch.object(obs, "_emit") as emit:
        obs.track_outcome_reader_disagreement(3, 2, 5)
    emit.assert_called_once()
    assert emit.call_args.args[0] is spec
    assert emit.call_args.args[1]["status"] == "disagreement"


class TestBackfillTargets:
    def test_query_selects_v1_only_known_positives_first_and_caps(self):
        ctx, cur = _ctx([{"log_id": 1}])
        with patch(CUR, return_value=ctx):
            out = repo.get_comment_outcome_backfill_targets(1, limit=5)
        sql, params = cur.execute.call_args.args
        assert out == [{"log_id": 1}]
        assert "reader_version = 1" in sql and "reader_version >= 2" in sql
        assert "NOT EXISTS" in sql
        assert sql.index("known_positive DESC") < sql.index("l.created_at ASC")
        assert params[-1] == 5

    def test_db_fault_is_an_empty_list(self):
        with patch(CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.get_comment_outcome_backfill_targets(1) == []


class TestSweepBackfill:
    def _sweep(self, es, v2, fresh, backfill):
        profile = MagicMock()
        profile.profile_url = "https://www.linkedin.com/in/me/"
        es.enter_context(patch(f"{POST}.get_current_profile",
                               return_value=(MagicMock(), MagicMock(), "e", profile)))
        for name in ("quit_gracefully", "release_run_lock", "track_comment_outcome", "time"):
            es.enter_context(patch(f"{POST}.{name}"))
        es.enter_context(patch(f"{POST}.acquire_run_lock", return_value="tok"))
        es.enter_context(patch(f"{POST}.comment_outcome_reader_v2", return_value=v2))
        es.enter_context(patch(f"{POST}.get_comment_outcome_targets", return_value=fresh))
        bf = es.enter_context(patch(f"{POST}.get_comment_outcome_backfill_targets",
                                    return_value=backfill))
        read = es.enter_context(patch(f"{POST}._read_comment_outcome", return_value={
            "status": "checked", "skip_reason": None, "author_replied": False, "reply_count": 1,
            "like_count": 0, "visible_most_relevant": True, "our_reply_sent": False,
            "reader_version": 2}))
        es.enter_context(patch(f"{POST}.record_comment_outcome", return_value=True))
        return bf, read

    @staticmethod
    def _t(n):
        return [{"log_id": i, "post_url": f"feedurn://urn:li:activity:{i}", "message": "m"}
                for i in range(n)]

    def test_backfill_fills_only_the_spare_capacity(self):
        from cqc_lem.app.engagement.posting import _MAX_OUTCOME_CHECKS_PER_RUN, _run_comment_outcomes_sweep
        with ExitStack() as es:
            bf, read = self._sweep(es, True, self._t(10), self._t(100)[:5])
            _run_comment_outcomes_sweep(1)
        assert bf.call_args.kwargs["limit"] == _MAX_OUTCOME_CHECKS_PER_RUN - 10
        assert read.call_count == 15

    def test_no_backfill_while_v2_is_off(self):
        from cqc_lem.app.engagement.posting import _run_comment_outcomes_sweep
        with ExitStack() as es:
            bf, _read = self._sweep(es, False, self._t(2), self._t(3))
            _run_comment_outcomes_sweep(1)
        assert not bf.called

    def test_no_backfill_when_the_run_is_already_full(self):
        from cqc_lem.app.engagement.posting import _MAX_OUTCOME_CHECKS_PER_RUN, _run_comment_outcomes_sweep
        with ExitStack() as es:
            bf, _read = self._sweep(es, True, self._t(_MAX_OUTCOME_CHECKS_PER_RUN), [])
            _run_comment_outcomes_sweep(1)
        assert not bf.called


class TestSkippedV2ReadIsV2:
    def test_skip_carries_reader_version_2(self):
        from cqc_lem.app.engagement import posting as P
        driver = MagicMock()
        with ExitStack() as es:
            es.enter_context(patch(f"{POST}.comment_outcome_reader_v2", return_value=True))
            es.enter_context(patch(f"{POST}.time"))
            es.enter_context(patch(f"{POST}._load_comment_thread"))
            es.enter_context(patch(f"{POST}._comment_items", return_value=[]))
            es.enter_context(patch(f"{POST}._comment_sort_label", return_value=None))
            es.enter_context(patch(f"{POST}._find_our_comment", return_value=None))
            es.enter_context(patch(f"{POST}._comment_outcome_skip_reason",
                                   return_value="post-unavailable"))
            es.enter_context(patch(f"{POST}.log_debug"))
            out = P._read_comment_outcome(driver, MagicMock(), 1, "https://p", "me", "t")
        assert out["status"] == "skipped" and out["skip_reason"] == "post-unavailable"
        assert out["reader_version"] == 2

    def test_v1_skip_stays_unversioned(self):
        from cqc_lem.app.engagement import posting as P
        with ExitStack() as es:
            es.enter_context(patch(f"{POST}.comment_outcome_reader_v2", return_value=False))
            es.enter_context(patch(f"{POST}.time"))
            es.enter_context(patch(f"{POST}._load_comment_thread"))
            es.enter_context(patch(f"{POST}._comment_items", return_value=[]))
            es.enter_context(patch(f"{POST}._comment_sort_label", return_value=None))
            es.enter_context(patch(f"{POST}._find_our_comment", return_value=None))
            es.enter_context(patch(f"{POST}._comment_outcome_skip_reason", return_value="x"))
            es.enter_context(patch(f"{POST}.log_debug"))
            out = P._read_comment_outcome(MagicMock(), MagicMock(), 1, "https://p", "me", "t")
        assert "reader_version" not in out
