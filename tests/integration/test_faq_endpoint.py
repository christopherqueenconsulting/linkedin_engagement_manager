"""Integration test for the public front-page FAQ (issue #506).

Drives the real GET /api/faq handler through the real db.py SQL into a stateful fake cursor that
models `faq_entries` — so it is provable, without a MySQL container, that only published entries
reach the landing page, that they arrive in display order, and that the migration seeds the topics
the front page promises (pricing/trial, ToS, data/privacy, voice, what the automation does,
cancellation).
"""
import re
from datetime import datetime
from pathlib import Path

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"
_MIGRATION = (Path(__file__).resolve().parents[2]
              / "compose/local/database/migrations/V20260725155210__add_faq_entries.sql")


class _FakeCursor:
    """Minimal MySQL-ish cursor over an in-memory faq_entries store."""

    def __init__(self, store, dictionary=False):
        self.store = store
        self.dictionary = dictionary
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT id, question, answer, cluster_id, updated_at FROM faq_entries"):
            rows = [r for r in self.store if r["status"] == params[0]]
            rows.sort(key=lambda r: (r["sort_order"], r["id"]))
            self._result = [{k: r[k] for k in ("id", "question", "answer", "cluster_id", "updated_at")}
                            for r in rows[:params[1]]]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self, *a, **k):
        return _FakeCursor(self.store, dictionary=k.get("dictionary", False))

    def close(self):
        pass


def _entry(entry_id, question, sort_order, status="published", cluster_id=None):
    return {"id": entry_id, "question": question, "answer": f"answer {entry_id}",
            "cluster_id": cluster_id, "status": status, "sort_order": sort_order,
            "updated_at": datetime(2026, 7, 25, 12, 0)}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def store():
    return [
        _entry(2, "Can I cancel anytime?", 60),
        _entry(1, "What does the automation actually do?", 10),
        _entry(3, "Why is my post stuck?", 20, status="draft", cluster_id=12),
        _entry(4, "Old pricing?", 30, status="archived"),
    ]


def _get_faq(client, store):
    with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)), \
         patch("cqc_lem.utilities.observability.posthog"):
        return client.get("/api/faq")


class TestPublicFaq:
    def test_serves_only_published_entries_in_display_order(self, client, store):
        response = _get_faq(client, store)

        assert response.status_code == 200
        entries = response.json()["detail"]["entries"]
        assert [e["question"] for e in entries] == [
            "What does the automation actually do?", "Can I cancel anytime?"]
        # An auto-generated draft awaiting review must never leak onto the public page.
        assert all("stuck" not in e["question"] for e in entries)
        assert entries[0]["updated_at"] == "2026-07-25T12:00:00Z"
        assert "cluster_id" not in entries[0]

    def test_an_unpublished_faq_returns_an_empty_list_not_an_error(self, client):
        """The SPA falls back to its seeded copy — the front page must not break on an empty FAQ."""
        response = _get_faq(client, [_entry(9, "Draft only", 10, status="draft")])

        assert response.status_code == 200
        assert response.json()["detail"]["entries"] == []


class TestSeedEntries:
    def test_the_migration_seeds_every_promised_topic(self):
        sql = _MIGRATION.read_text(encoding="utf-8")
        assert "INSERT IGNORE INTO faq_entries" in sql  # re-running never duplicates a question
        for topic in ("free trial", "Terms of Service", "credentials", "voice",
                      "automation actually do", "cancel anytime"):
            assert topic in sql
        questions = re.findall(r"^\('(.+?)',$", sql, flags=re.MULTILINE)
        assert len(questions) == 6
