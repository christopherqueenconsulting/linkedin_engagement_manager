"""Voice exemplars: parse, ingest caps/dedupe, retrieval fallbacks, copy gate (issue #2627)."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.ai import voice_exemplars as ve

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_EMBED = "cqc_lem.utilities.ai.voice_exemplars.embed_comments"
_FLAG = "cqc_lem.utilities.ai.voice_exemplars.flag_enabled"


def _long(n):
    return f"This is my own comment number {n} about shipping boring software reliably."


def test_parse_comments_csv_reads_message_column_with_bom():
    raw = "﻿Date,Link,Message\n2026-01-01,u,hello there\n2026-01-02,u,second\n".encode()
    assert ve.parse_export_csv("comments", raw) == ["hello there", "second"]


def test_parse_shares_uses_sharecommentary():
    raw = b"Date,ShareLink,ShareCommentary\n2026,x,my post text\n"
    assert ve.parse_export_csv("shares", raw) == ["my post text"]


@pytest.mark.parametrize("kind,raw", [("bogus", b"a,b\n1,2"), ("comments", b"a,b\n1,2"),
                                       ("comments", b"\xff\xfe\x00bad")])
def test_parse_unusable_is_empty(kind, raw):
    assert ve.parse_export_csv(kind, raw) == []


def test_ingest_dedupes_filters_and_stores():
    texts = [_long(1), _long(1), "short", "x" * 2000, _long(2)]
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value=set()), \
         patch(f"{_DB}.count_voice_exemplars", return_value=0), \
         patch(_EMBED, side_effect=lambda b: [[1.0]] * len(b)), \
         patch(f"{_DB}.insert_voice_exemplar", return_value=True) as ins:
        out = ve.ingest_exemplars(1, "comments", texts)
    assert out["stored"] == 2 and out["duplicate"] == 1 and out["too_short_or_long"] == 2
    assert ins.call_count == 2


def test_ingest_skips_already_stored_hash():
    from cqc_lem.platform.db.repositories.voice_exemplars import exemplar_hash
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value={exemplar_hash(_long(1))}), \
         patch(f"{_DB}.count_voice_exemplars", return_value=0), \
         patch(_EMBED, side_effect=lambda b: [[1.0]] * len(b)), \
         patch(f"{_DB}.insert_voice_exemplar", return_value=True) as ins:
        out = ve.ingest_exemplars(1, "comments", [_long(1)])
    assert out["duplicate"] == 1 and ins.call_count == 0


def test_ingest_row_caps_per_upload_and_per_user():
    texts = [_long(i) for i in range(30)]
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value=set()), \
         patch(f"{_DB}.count_voice_exemplars", return_value=_cap() - 3), \
         patch(_EMBED, side_effect=lambda b: [[1.0]] * len(b)), \
         patch(f"{_DB}.insert_voice_exemplar", return_value=True) as ins:
        out = ve.ingest_exemplars(1, "comments", texts)
    assert ins.call_count == 3 and out["over_cap"] == 27


def _cap():
    from cqc_lem.platform.db.repositories.voice_exemplars import MAX_EXEMPLARS_PER_USER
    return MAX_EXEMPLARS_PER_USER


def test_ingest_per_upload_cap():
    texts = [_long(i) for i in range(ve.MAX_PER_UPLOAD + 5)]
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value=set()), \
         patch(f"{_DB}.count_voice_exemplars", return_value=0), \
         patch(_EMBED, side_effect=lambda b: [[1.0]] * len(b)), \
         patch(f"{_DB}.insert_voice_exemplar", return_value=True):
        out = ve.ingest_exemplars(1, "comments", texts)
    assert out["stored"] == ve.MAX_PER_UPLOAD and out["over_cap"] == 5


def test_ingest_embed_failure_stores_nothing():
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value=set()), \
         patch(f"{_DB}.count_voice_exemplars", return_value=0), \
         patch(_EMBED, return_value=None), \
         patch(f"{_DB}.insert_voice_exemplar") as ins:
        out = ve.ingest_exemplars(1, "comments", [_long(1)])
    assert out["embed_failed"] == 1 and ins.call_count == 0


def test_ingest_db_unreadable_reports_unavailable():
    with patch(f"{_DB}.get_existing_exemplar_hashes", return_value=None), \
         patch(f"{_DB}.count_voice_exemplars", return_value=0):
        assert ve.ingest_exemplars(1, "comments", [_long(1)])["unavailable"] is True


def _rows(n):
    return [{"body": f"ex{i}", "embedding": [1.0, float(i)]} for i in range(n)]


def test_nearest_flag_off_is_empty_and_reads_nothing():
    with patch(_FLAG, return_value=False), patch(f"{_DB}.get_embedded_voice_exemplars") as g:
        assert ve.nearest_voice_exemplars(1, "post") == []
    g.assert_not_called()


def test_nearest_below_minimum_falls_back():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_embedded_voice_exemplars", return_value=_rows(3)):
        assert ve.nearest_voice_exemplars(1, "post") == []


def test_nearest_unreadable_or_unembeddable_is_empty():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_embedded_voice_exemplars", return_value=None):
        assert ve.nearest_voice_exemplars(1, "post") == []
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_embedded_voice_exemplars", return_value=_rows(6)), \
         patch(_EMBED, return_value=None):
        assert ve.nearest_voice_exemplars(1, "post") == []


def test_nearest_ranks_by_cosine_and_takes_k():
    with patch(_FLAG, return_value=True), \
         patch(f"{_DB}.get_embedded_voice_exemplars", return_value=_rows(8)), \
         patch(_EMBED, return_value=[[0.0, 1.0]]):
        out = ve.nearest_voice_exemplars(1, "post")
    assert len(out) == ve.NEAREST_K and out[0] == "ex7"


def test_nearest_blank_query_is_empty():
    assert ve.nearest_voice_exemplars(1, "  ") == []


def test_block_empty_is_empty_string_and_filled_lists_each():
    assert ve.exemplar_block([]) == ""
    block = ve.exemplar_block(["one", "two"])
    assert "- one" in block and "- two" in block and "Do NOT copy" in block


def test_too_close_flags_copy_and_passes_fresh():
    with patch(_EMBED, return_value=[[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]):
        assert ve.too_close_to_exemplar("d", ["a", "b"])["score"] == 1.0
    with patch(_EMBED, return_value=[[1.0, 0.0], [0.0, 1.0]]):
        assert ve.too_close_to_exemplar("d", ["a"]) is None


def test_too_close_fails_open():
    assert ve.too_close_to_exemplar("d", []) is None
    assert ve.too_close_to_exemplar("", ["a"]) is None
    with patch(_EMBED, return_value=None):
        assert ve.too_close_to_exemplar("d", ["a"]) is None


def test_enabled_delegates_to_flag():
    with patch(_FLAG, return_value=True) as f:
        assert ve.voice_exemplars_enabled(5) is True
    f.assert_called_once_with("voice-exemplars-enabled", 5)
