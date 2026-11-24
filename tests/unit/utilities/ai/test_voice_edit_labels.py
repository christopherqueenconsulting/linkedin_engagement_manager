"""Edited-draft labels: original + final stored on approval, never for an untouched draft (#2627 1b)."""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import voice_exemplars as repo
from cqc_lem.utilities.ai import voice_exemplars as ve

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_FLAG = "cqc_lem.utilities.ai.voice_exemplars.flag_enabled"
_CUR = "cqc_lem.platform.db.repositories.voice_exemplars.db_cursor"
_FINAL = "Thanks for the intro, Dana. I will send the two-page summary over by Friday morning."
_DRAFT = "Thank you so much for the wonderful introduction, Dana! Summary coming Friday."


def _cursor(rowcount=1, one=None):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


# --- note_draft_edit -------------------------------------------------------------------------

def test_edit_that_changes_text_remembers_the_original():
    with patch(_FLAG, return_value=True), patch(f"{_DB}.remember_draft_original") as keep:
        ve.note_draft_edit(7, "scheduled_dm", 3, _DRAFT, _FINAL)
    keep.assert_called_once_with(7, "scheduled_dm", 3, _DRAFT)


@pytest.mark.parametrize("stored,submitted", [
    (_DRAFT, None),                    # field not sent
    (_DRAFT, "  " + _DRAFT.upper()[:0] + _DRAFT + " "),  # whitespace-only difference
    (None, _FINAL),                    # nothing on file to be the original
    ("", _FINAL),
])
def test_non_edits_remember_nothing(stored, submitted):
    with patch(_FLAG, return_value=True), patch(f"{_DB}.remember_draft_original") as keep:
        ve.note_draft_edit(7, "post", 3, stored, submitted)
    keep.assert_not_called()


def test_flag_off_remembers_nothing():
    with patch(_FLAG, return_value=False), patch(f"{_DB}.remember_draft_original") as keep:
        ve.note_draft_edit(7, "post", 3, _DRAFT, _FINAL)
    keep.assert_not_called()


# --- record_approved_edit --------------------------------------------------------------------

def test_approved_edit_stores_both_texts_and_feeds_the_corpus():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_draft_original", return_value=_DRAFT), \
         patch(f"{_DB}.finalize_edit_label", return_value=True) as fin, \
         patch.object(ve, "ingest_exemplars") as ingest:
        assert ve.record_approved_edit(7, "scheduled_dm", 3, _FINAL) is True
    fin.assert_called_once_with(7, "scheduled_dm", 3, _FINAL)
    ingest.assert_called_once_with(7, "edited", [_FINAL])


def test_unedited_draft_stores_nothing():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_draft_original", return_value=None), \
         patch(f"{_DB}.finalize_edit_label") as fin, \
         patch.object(ve, "ingest_exemplars") as ingest:
        assert ve.record_approved_edit(7, "post", 3, _FINAL) is False
    fin.assert_not_called()
    ingest.assert_not_called()


def test_edited_back_to_the_original_is_not_a_label():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_draft_original", return_value=_FINAL), \
         patch(f"{_DB}.finalize_edit_label") as fin:
        assert ve.record_approved_edit(7, "post", 3, "  " + _FINAL) is False
    fin.assert_not_called()


def test_second_approval_does_not_ingest_twice():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_draft_original", return_value=_DRAFT), \
         patch(f"{_DB}.finalize_edit_label", return_value=False), \
         patch.object(ve, "ingest_exemplars") as ingest:
        assert ve.record_approved_edit(7, "post", 3, _FINAL) is False
    ingest.assert_not_called()


def test_flag_off_and_blank_final_do_nothing():
    with patch(_FLAG, return_value=False), patch(f"{_DB}.get_draft_original") as read:
        assert ve.record_approved_edit(7, "post", 3, _FINAL) is False
    with patch(_FLAG, return_value=True), patch(f"{_DB}.get_draft_original") as read2:
        assert ve.record_approved_edit(7, "post", 3, "  ") is False
    read.assert_not_called()
    read2.assert_not_called()


def test_a_fault_never_breaks_the_approval():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_draft_original", side_effect=RuntimeError("db down")), \
         patch("cqc_lem.utilities.ai.voice_exemplars.log_warning") as warn:
        assert ve.record_approved_edit(7, "post", 3, _FINAL) is False
    warn.assert_called_once()


def test_edited_final_shares_the_upload_length_floor():
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value=set()), \
         patch(f"{_DB}.count_voice_exemplars", return_value=0), \
         patch("cqc_lem.utilities.ai.voice_exemplars.embed_comments") as embed, \
         patch(f"{_DB}.insert_voice_exemplar") as ins:
        out = ve.ingest_exemplars(7, "edited", ["too short"])
    assert out["too_short_or_long"] == 1 and out["stored"] == 0
    embed.assert_not_called()
    ins.assert_not_called()


# --- repository ------------------------------------------------------------------------------

def test_remember_original_is_insert_ignore_and_reports_new():
    cur, ctx = _cursor(rowcount=1)
    with patch(_CUR, return_value=ctx):
        assert repo.remember_draft_original(7, "post", 3, _DRAFT) is True
    assert "INSERT IGNORE INTO voice_edit_labels" in cur.execute.call_args.args[0]
    cur2, ctx2 = _cursor(rowcount=0)
    with patch(_CUR, return_value=ctx2):
        assert repo.remember_draft_original(7, "post", 3, _DRAFT) is False


def test_remember_original_blank_never_writes():
    with patch(_CUR) as cur:
        assert repo.remember_draft_original(7, "post", 3, "  ") is False
    cur.assert_not_called()


def test_get_original_reads_row_or_none():
    _, ctx = _cursor(one=(_DRAFT,))
    with patch(_CUR, return_value=ctx):
        assert repo.get_draft_original(7, "post", 3) == _DRAFT
    _, ctx = _cursor(one=None)
    with patch(_CUR, return_value=ctx):
        assert repo.get_draft_original(7, "post", 3) is None


def test_finalize_only_fills_an_unfinalized_label():
    cur, ctx = _cursor(rowcount=1)
    with patch(_CUR, return_value=ctx):
        assert repo.finalize_edit_label(7, "post", 3, _FINAL) is True
    assert "final_text IS NULL" in cur.execute.call_args.args[0]


@pytest.mark.parametrize("call", [
    lambda: repo.remember_draft_original(7, "post", 3, _DRAFT),
    lambda: repo.get_draft_original(7, "post", 3),
    lambda: repo.finalize_edit_label(7, "post", 3, _FINAL),
])
def test_db_faults_fail_soft(call):
    with patch(_CUR, side_effect=mysql.connector.Error("boom")):
        assert not call()


def test_edited_is_an_allowed_source_and_facade_exports():
    from cqc_lem.utilities import db
    assert "edited" in repo.VOICE_EXEMPLAR_SOURCES
    for name in ("remember_draft_original", "get_draft_original", "finalize_edit_label"):
        assert getattr(db, name) is getattr(repo, name) and name in db.__all__


# --- review_actions bulk approve ---------------------------------------------------------------

def test_bulk_approve_dm_labels_an_edited_draft():
    from cqc_lem.utilities import review_actions as ra
    with patch.object(ra, "get_scheduled_dm_user_id", return_value=7), \
         patch.object(ra, "get_scheduled_dm", return_value={"message": _FINAL, "scheduled_time": None}), \
         patch.object(ra, "update_scheduled_dm", return_value=True), \
         patch.object(ve, "record_approved_edit") as rec, \
         patch("cqc_lem.app.engagement.outreach.scheduled_dm_is_stale", return_value=False):
        assert ra.approve_scheduled_dm(7, 3) is None
    rec.assert_called_once_with(7, "scheduled_dm", 3, _FINAL)


def test_bulk_approve_catchup_labels_an_edited_draft():
    from cqc_lem.utilities import review_actions as ra
    with patch.object(ra, "get_catchup_touch", return_value={"user_id": 7, "message": _FINAL}), \
         patch.object(ra, "update_catchup_touch", return_value=True), \
         patch.object(ve, "record_approved_edit") as rec:
        assert ra.approve_catchup_touch(7, 4) is None
    rec.assert_called_once_with(7, "catchup_touch", 4, _FINAL)
