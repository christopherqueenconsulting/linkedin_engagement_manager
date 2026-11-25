"""Agent-added story-bank entries are UNVERIFIED and the content engine never reads them (#2804).

A row is verified when `source IS NULL OR verified_at IS NOT NULL`, so every pre-existing row (source
NULL) is verified with no backfill. `active` is the user's own switch and is never involved.
"""

import ast
import pathlib
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import feedback as repo

pytestmark = pytest.mark.unit

_CUR = "cqc_lem.platform.db.repositories.feedback.db_cursor"
SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "cqc_lem"
VERIFIED = "(source IS NULL OR verified_at IS NOT NULL)"


def _cursor(rowcount=1, rows=None, one=None, lastrowid=9):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return cur, ctx


def _sql(cur, index=0):
    return cur.execute.call_args_list[index][0][0]


def test_the_verified_definition_is_the_documented_one():
    assert repo.STORY_VERIFIED_SQL == VERIFIED


class TestReadersExcludeUnverifiedRows:
    def test_the_generation_reader_filters_unverified(self):
        cur, ctx = _cursor(rows=[])
        with patch(_CUR, return_value=ctx):
            repo.get_story_bank_entries(3, active_only=True)
        sql = _sql(cur)
        assert VERIFIED in sql and "active=1" in sql and "review_status='accepted'" in sql

    def test_the_spa_reader_sees_every_row_and_gets_a_verified_flag(self):
        cur, ctx = _cursor(rows=[{"id": 1, "active": 1, "used_count": None, "verified": 0,
                                  "source": "mcp"}])
        with patch(_CUR, return_value=ctx):
            rows = repo.get_story_bank_entries(3)
        sql = _sql(cur)
        assert f"{VERIFIED} AS verified" in sql and "source" in sql
        assert "WHERE user_id=%s ORDER BY" in sql  # no verified filter on the SPA list
        assert rows[0]["verified"] is False and rows[0]["source"] == "mcp"

    def test_a_user_row_is_verified(self):
        cur, ctx = _cursor(rows=[{"id": 2, "active": 1, "used_count": 1, "verified": 1,
                                  "source": None}])
        with patch(_CUR, return_value=ctx):
            assert repo.get_story_bank_entries(3)[0]["verified"] is True

    def test_the_count_that_drives_the_has_stories_nudge_counts_verified_only(self):
        cur, ctx = _cursor(one=(4,))
        with patch(_CUR, return_value=ctx):
            assert repo.count_story_bank_entries(3) == 4
        assert VERIFIED in _sql(cur)

    def test_every_content_engine_caller_asks_for_active_only(self):
        """Every caller outside the SPA's own endpoint passes `active_only=True`, the verified branch."""
        offenders = []
        for path in SRC.rglob("*.py"):
            if path.name == "feedback.py" or path.as_posix().endswith("api/routers/user.py"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name != "get_story_bank_entries":
                    continue
                ok = any(k.arg == "active_only" and isinstance(k.value, ast.Constant)
                         and k.value.value is True for k in node.keywords)
                if not ok:
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        assert not offenders, f"story-bank readers that would read unverified rows: {offenders}"

    def test_nothing_else_selects_from_the_story_bank(self):
        """The only SELECTs on `story_bank` live in the repository that owns the filter."""
        offenders = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                     if p.name != "feedback.py" and "FROM story_bank" in p.read_text()
                     and "SELECT" in p.read_text().split("FROM story_bank")[0].split("\n")[-1]]
        assert offenders == []


class TestInsertDraft:
    def test_it_lands_unverified_from_the_agent(self):
        cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx):
            assert repo.insert_story_draft(3, "number", "", "Cut churn by 12%", "2026-03-01") == 9
        sql, params = cur.execute.call_args[0]
        assert "source" in sql and "verified_at" not in sql  # verified_at stays NULL
        assert params == (3, "number", "Cut churn by 12%", "Cut churn by 12%", "2026-03-01", "mcp")

    def test_an_unknown_kind_falls_back_like_the_spa_writer(self):
        cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx):
            repo.insert_story_draft(3, "bogus", "T", "body", None)
        assert cur.execute.call_args[0][1][1] == "anecdote"

    def test_a_db_fault_is_none(self):
        with patch(_CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.insert_story_draft(3, "anecdote", "t", "b", None) is None


class TestCountDrafts:
    def test_it_counts_the_unverified(self):
        cur, ctx = _cursor(one=(3,))
        with patch(_CUR, return_value=ctx):
            assert repo.count_unverified_story_drafts(3) == 3
        assert "NOT " + VERIFIED in _sql(cur)

    def test_unreadable_is_none_never_zero(self):
        with patch(_CUR, side_effect=mysql.connector.Error("boom")):
            assert repo.count_unverified_story_drafts(3) is None


class TestConfirm:
    def test_it_sets_verified_at_once_and_scopes_to_the_owner(self):
        cur, ctx = _cursor(rowcount=1)
        with patch(_CUR, return_value=ctx):
            assert repo.confirm_story_bank_entry(3, 5) is True
        sql, params = cur.execute.call_args[0]
        assert "COALESCE(verified_at, NOW())" in sql and params == (3, 5)

    def test_an_already_confirmed_row_is_still_found(self):
        cur, ctx = _cursor(rowcount=0, one=(1,))
        with patch(_CUR, return_value=ctx):
            assert repo.confirm_story_bank_entry(3, 5) is True

    def test_another_users_id_matches_nothing(self):
        cur, ctx = _cursor(rowcount=0, one=None)
        with patch(_CUR, return_value=ctx):
            assert repo.confirm_story_bank_entry(3, 5) is False


class TestEditingAnAgentDraftConfirmsIt:
    def test_the_update_confirms_only_on_a_content_change_and_reads_before_it_writes(self):
        cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx):
            assert repo.upsert_story_bank_entries(3, [
                {"id": 5, "kind": "number", "title": "T", "body": "B", "happened_at": None,
                 "active": False}]) is True
        sql = cur.executemany.call_args[0][0]
        # verified_at is assigned FIRST: MySQL SET is left to right, so it must compare against the
        # stored text before this same statement overwrites it.
        assert sql.index("verified_at =") < sql.index("kind=%s")
        assert "source IS NOT NULL AND verified_at IS NULL" in sql
        for column in ("kind <> %s", "CAST(title AS BINARY)", "CAST(body AS BINARY)",
                       "NOT (happened_at <=> %s)"):
            assert column in sql
        assert "active <>" not in sql  # the on/off switch never confirms a fact
        params = cur.executemany.call_args[0][1][0]
        assert params == ("number", "T", "B", None, "number", "T", "B", None, 0, 5, 3)

    def test_a_new_row_from_the_spa_is_the_users_own(self):
        cur, ctx = _cursor()
        with patch(_CUR, return_value=ctx):
            repo.upsert_story_bank_entries(3, [{"kind": "opinion", "body": "B"}])
        assert "source" not in cur.executemany.call_args[0][0]  # NULL source = user = verified
