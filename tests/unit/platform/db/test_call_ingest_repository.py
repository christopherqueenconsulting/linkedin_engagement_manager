"""The `call_ingest` repository: SQL shape, encryption, review gating, fail-soft (issue #2629)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import call_ingest as repo, feedback

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.call_ingest.db_cursor"
_FB_CUR = "cqc_lem.platform.db.repositories.feedback.db_cursor"
_ERR = mysql.connector.Error("boom")


def _cursor(rowcount=1, rows=None, one=None, lastrowid=7):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


def _sql(cur):
    return " ".join(c[0][0] for c in cur.execute.call_args_list)


def test_facade_re_exports_every_name():
    from cqc_lem.utilities import db
    for name in ("insert_call_ingest", "review_candidate", "purge_all_call_data",
                 "purge_call_transcripts", "get_pending_candidates", "create_call_source"):
        assert getattr(db, name) is getattr(repo, name)
        assert name in db.__all__


def test_aad_constant_is_pinned():
    assert repo.CALL_TRANSCRIPT_FIELD == "call_ingest.transcript"


def test_insert_encrypts_transcript_and_sets_purge_cap():
    cur, ctx = _cursor(one=None)
    now = datetime(2026, 1, 1)
    with patch(_CUR, return_value=ctx), \
            patch.object(repo, "encrypt_secret", return_value="lemv1:sealed") as enc:
        assert repo.insert_call_ingest(3, "redacted text", now=now) == 7
    enc.assert_called_once_with("redacted text", 3, "call_ingest.transcript")
    params = cur.execute.call_args_list[-1][0][1]
    assert "lemv1:sealed" in params and "redacted text" not in params
    assert params[-1] == datetime(2026, 1, 8)


def test_insert_duplicate_returns_existing_id_without_second_insert():
    cur, ctx = _cursor(one=(42,))
    with patch(_CUR, return_value=ctx), patch.object(repo, "encrypt_secret", return_value="x"):
        assert repo.insert_call_ingest(3, "t") == 42
    assert "INSERT INTO call_ingest" not in _sql(cur)


def test_insert_fault_returns_none():
    with patch(_CUR, side_effect=_ERR), patch.object(repo, "encrypt_secret", return_value="x"):
        assert repo.insert_call_ingest(3, "t") is None


def test_get_transcript_decrypts_and_purged_is_none():
    cur, ctx = _cursor(one=("lemv1:sealed",))
    with patch(_CUR, return_value=ctx), \
            patch.object(repo, "decrypt_secret", return_value="plain") as dec:
        assert repo.get_call_transcript(3, 9) == "plain"
    dec.assert_called_once_with("lemv1:sealed", 3, "call_ingest.transcript")
    cur, ctx = _cursor(one=(None,))
    with patch(_CUR, return_value=ctx):
        assert repo.get_call_transcript(3, 9) is None
    with patch(_CUR, side_effect=_ERR):
        assert repo.get_call_transcript(3, 9) is None


def test_source_requires_attestation_to_be_enabled():
    cur, ctx = _cursor()
    with patch(_CUR, return_value=ctx):
        assert repo.create_call_source(1, "otter", consent_attested=False) == 7
    params = cur.execute.call_args[0][1]
    assert params[3] == 0 and params[5] == 0
    assert repo.create_call_source(1, "zoom") is None
    with patch(_CUR, side_effect=_ERR):
        assert repo.create_call_source(1, "otter") is None


def test_source_enabled_needs_both_flags():
    for row, expected in (((1, 1), True), ((1, 0), False), ((0, 1), False), (None, False)):
        cur, ctx = _cursor(one=row)
        with patch(_CUR, return_value=ctx):
            assert repo.is_call_source_enabled(1, 2) is expected
    with patch(_CUR, side_effect=_ERR):
        assert repo.is_call_source_enabled(1, 2) is False


def test_candidates_insert_skips_unquoted_and_inserts_pending():
    cur, ctx = _cursor()
    items = [{"title": "t", "body": "b", "source_quote": "q", "kind": "bogus", "confidence": 0.9},
             {"title": "no quote", "body": "b", "source_quote": ""}]
    with patch(_CUR, return_value=ctx):
        assert repo.insert_extract_candidates(1, 5, items) == 1
    assert repo.insert_extract_candidates(1, 5, []) == 0
    rows = cur.executemany.call_args[0][1]
    assert rows[0][2] == "anecdote" and len(rows) == 1
    with patch(_CUR, side_effect=_ERR):
        assert repo.insert_extract_candidates(1, 5, items) == 0


def test_pending_queue_reads_only_pending():
    cur, ctx = _cursor(rows=[{"id": 1}])
    with patch(_CUR, return_value=ctx):
        assert repo.get_pending_candidates(1) == [{"id": 1}]
    assert "status='pending'" in _sql(cur)
    with patch(_CUR, side_effect=_ERR):
        assert repo.get_pending_candidates(1) == []


def test_accept_promotes_to_story_bank_as_accepted():
    cand = {"id": 4, "ingest_id": 5, "kind": "number", "title": "t", "body": "b",
            "source_quote": "q", "t0": None}
    cur, ctx = _cursor(one=cand, lastrowid=99)
    with patch(_CUR, return_value=ctx):
        assert repo.review_candidate(1, 4, "accepted") == 99
    sql = _sql(cur)
    assert "INSERT INTO story_bank" in sql and "'accepted'" in sql
    assert "status='pending'" in sql  # replay/foreign id matches nothing
    insert_params = cur.execute.call_args_list[1][0][1]
    assert insert_params[:2] == (1, "number") and "q" in insert_params


def test_edit_uses_the_edited_body():
    cand = {"id": 4, "ingest_id": 5, "kind": "number", "title": "t", "body": "orig",
            "source_quote": "q", "t0": None}
    cur, ctx = _cursor(one=cand, lastrowid=11)
    with patch(_CUR, return_value=ctx):
        assert repo.review_candidate(1, 4, "edited", edited_body=" better ") == 11
    assert "better" in cur.execute.call_args_list[1][0][1]


def test_reject_never_touches_story_bank():
    cand = {"id": 4, "ingest_id": 5, "kind": "number", "title": "t", "body": "b",
            "source_quote": "q", "t0": None}
    cur, ctx = _cursor(one=cand)
    with patch(_CUR, return_value=ctx):
        assert repo.review_candidate(1, 4, "rejected") is None
    assert "story_bank" not in _sql(cur) and "status='rejected'" in _sql(cur)


def test_review_missing_candidate_bad_decision_and_fault():
    cur, ctx = _cursor(one=None)
    with patch(_CUR, return_value=ctx):
        assert repo.review_candidate(1, 4, "accepted") is None
    assert repo.review_candidate(1, 4, "pending") is None
    with patch(_CUR, side_effect=_ERR):
        assert repo.review_candidate(1, 4, "accepted") is None


def test_purge_nulls_transcripts_past_cap():
    cur, ctx = _cursor(rowcount=3)
    with patch(_CUR, return_value=ctx):
        assert repo.purge_call_transcripts() == 3
    assert "transcript_enc=NULL" in _sql(cur) and "purge_after <=" in _sql(cur)
    with patch(_CUR, side_effect=_ERR):
        assert repo.purge_call_transcripts() == 0


def test_purge_reviewed_only_when_no_pending_candidate():
    cur, ctx = _cursor(rowcount=2)
    with patch(_CUR, return_value=ctx):
        assert repo.purge_reviewed_call_transcripts(1) == 2
    assert "NOT EXISTS" in _sql(cur) and "'pending'" in _sql(cur)
    assert "'extracted'" in _sql(cur)  # never purge a transcript still awaiting extraction
    with patch(_CUR, side_effect=_ERR):
        assert repo.purge_reviewed_call_transcripts(1) == 0


def test_purge_all_removes_every_table_and_promoted_rows():
    cur, ctx = _cursor()
    with patch(_CUR, return_value=ctx):
        assert repo.purge_all_call_data(1) is True
    sql = _sql(cur)
    for table in ("story_bank", "extract_candidate", "call_ingest", "call_source"):
        assert f"DELETE FROM {table}" in sql
    with patch(_CUR, side_effect=_ERR):
        assert repo.purge_all_call_data(1) is False


def test_selectors_read_only_accepted_active_story_bank_rows():
    """Hazard: a non-accepted row must never reach live comments or DMs."""
    cur, ctx = _cursor(rows=[])
    with patch(_FB_CUR, return_value=ctx):
        feedback.get_story_bank_entries(1, active_only=True)
    sql = cur.execute.call_args[0][0]
    assert "active=1" in sql and "review_status='accepted'" in sql
    cur, ctx = _cursor(one=(0,))
    with patch(_FB_CUR, return_value=ctx):
        feedback.count_story_bank_entries(1, active_only=True)
    assert "review_status='accepted'" in cur.execute.call_args[0][0]


def test_set_status_only_extracted_or_failed_and_never_resurrects_purged():
    assert repo.set_call_ingest_status(1, 2, "purged") is False
    cur, ctx = _cursor(rowcount=1)
    with patch(_CUR, return_value=ctx):
        assert repo.set_call_ingest_status(1, 2, "extracted") is True
    assert "status<>'purged'" in _sql(cur)
    with patch(_CUR, side_effect=_ERR):
        assert repo.set_call_ingest_status(1, 2, "failed") is False


def test_list_calls_and_candidates_scope_to_the_user_and_fail_soft():
    cur, ctx = _cursor(rows=[{"id": 1}])
    with patch(_CUR, return_value=ctx):
        assert repo.list_call_ingests(5) == [{"id": 1}]
        assert repo.get_candidates_for_call(5, 3) == [{"id": 1}]
    assert "user_id=%s" in _sql(cur) and "status='pending'" in _sql(cur)
    with patch(_CUR, side_effect=_ERR):
        assert repo.list_call_ingests(5) == []
        assert repo.get_candidates_for_call(5, 3) == []


def test_bulk_accept_promotes_each_pending_candidate_above_threshold():
    cur, ctx = _cursor(rows=[(4,), (9,)])
    with patch(_CUR, return_value=ctx), \
         patch.object(repo, "review_candidate", side_effect=[11, None]) as rv:
        assert repo.accept_candidates_above(5, 0.8, ingest_id=3) == 1
    assert "confidence>=%s" in _sql(cur) and "ingest_id=%s" in _sql(cur)
    assert [c.args for c in rv.call_args_list] == [(5, 4, "accepted"), (5, 9, "accepted")]


def test_bulk_accept_fault_accepts_nothing():
    with patch(_CUR, side_effect=_ERR):
        assert repo.accept_candidates_above(5, 0.5) == 0
