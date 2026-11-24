"""The `voice_exemplars` repository: SQL shape, dedupe key, fail-soft (issue #2627)."""

import json
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import voice_exemplars as repo

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.voice_exemplars.db_cursor"
_ERR = mysql.connector.Error("boom")


def _cursor(rowcount=1, rows=None, one=None):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


def test_facade_re_exports_every_name():
    from cqc_lem.utilities import db
    for name in ("count_voice_exemplars", "insert_voice_exemplar", "exemplar_hash",
                 "get_embedded_voice_exemplars", "get_existing_exemplar_hashes"):
        assert getattr(db, name) is getattr(repo, name)
        assert name in db.__all__


def test_hash_ignores_case_and_whitespace():
    assert repo.exemplar_hash("Hello  World\n") == repo.exemplar_hash("hello world")
    assert repo.exemplar_hash("a") != repo.exemplar_hash("b")


def test_insert_stores_vector_as_json_and_reports_new_row():
    cur, ctx = _cursor(rowcount=1)
    with patch(_CUR, return_value=ctx):
        assert repo.insert_voice_exemplar(1, "comments", "some text", [0.1, 0.2]) is True
    args = cur.execute.call_args[0][1]
    assert json.loads(args[4]) == [0.1, 0.2]
    assert "INSERT IGNORE" in cur.execute.call_args[0][0]


def test_insert_duplicate_is_false():
    cur, ctx = _cursor(rowcount=0)
    with patch(_CUR, return_value=ctx):
        assert repo.insert_voice_exemplar(1, "shares", "text", None) is False


@pytest.mark.parametrize("source,text", [("bogus", "x"), ("comments", "  ")])
def test_insert_rejects_bad_input_without_touching_db(source, text):
    with patch(_CUR) as db:
        assert repo.insert_voice_exemplar(1, source, text, None) is False
    db.assert_not_called()


def test_insert_fault_is_false():
    with patch(_CUR, side_effect=_ERR):
        assert repo.insert_voice_exemplar(1, "comments", "x", None) is False


def test_count_and_fault():
    _, ctx = _cursor(one=(4,))
    with patch(_CUR, return_value=ctx):
        assert repo.count_voice_exemplars(1) == 4
    with patch(_CUR, side_effect=_ERR):
        assert repo.count_voice_exemplars(1) is None


def test_hashes_and_fault():
    _, ctx = _cursor(rows=[("a",), ("b",)])
    with patch(_CUR, return_value=ctx):
        assert repo.get_existing_exemplar_hashes(1) == {"a", "b"}
    with patch(_CUR, side_effect=_ERR):
        assert repo.get_existing_exemplar_hashes(1) is None


def test_embedded_rows_parse_json_and_skip_empty():
    _, ctx = _cursor(rows=[{"body": "a", "embedding": "[1.0, 0.0]"},
                           {"body": "b", "embedding": [0.0, 1.0]},
                           {"body": "c", "embedding": None}])
    with patch(_CUR, return_value=ctx):
        rows = repo.get_embedded_voice_exemplars(1)
    assert [r["body"] for r in rows] == ["a", "b"]
    assert rows[0]["embedding"] == [1.0, 0.0]


def test_embedded_rows_fault_and_bad_json_are_none():
    with patch(_CUR, side_effect=_ERR):
        assert repo.get_embedded_voice_exemplars(1) is None
    _, ctx = _cursor(rows=[{"body": "a", "embedding": "{not json"}])
    with patch(_CUR, return_value=ctx):
        assert repo.get_embedded_voice_exemplars(1) is None
