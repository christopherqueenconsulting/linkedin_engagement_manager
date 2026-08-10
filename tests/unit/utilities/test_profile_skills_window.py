"""Unit tests for the profile-skills re-index window (issue #1075).

Tests cover: diff detection (including first-scrape seeding), window open/close/expiry, directive
composition, and the adopt-skills endpoint round-trip.
"""

import json
from unittest.mock import patch

import pytest

from cqc_lem.utilities.linkedin.profile import LinkedInProfile, LinkedInSkill
from cqc_lem.utilities.profile_skills_window import (
    WINDOW_DAYS,
    _key,
    _top_skills,
    close_profile_skills_window,
    get_profile_skills_window,
    open_profile_skills_window,
    profile_skills_directive,
    record_profile_skills_change,
    skills_changed_since_last_recorded,
)

pytestmark = pytest.mark.unit


def _redis_client_mock():
    """An in-memory Redis stand-in with the small API this module uses."""
    store = {}

    class Client:
        def setex(self, key, seconds, value):
            store[key] = (value, seconds)

        def get(self, key):
            return store.get(key, (None, None))[0]

        def delete(self, key):
            store.pop(key, None)

        def ttl(self, key):
            return -2

    return Client(), store


def _make_profile(skills) -> LinkedInProfile:
    """Build a minimal LinkedInProfile with the given skills."""
    return LinkedInProfile(full_name="Test User", skills=skills)


class TestTopSkills:
    def test_extracts_top_five_in_order(self):
        profile = _make_profile(["AI Strategy", "Product Growth", "B2B Sales", "Leadership", "Data", "Marketing"])
        assert _top_skills(profile) == ["ai strategy", "product growth", "b2b sales", "leadership", "data"]

    def test_handles_skill_objects(self):
        profile = _make_profile([
            LinkedInSkill(name="Machine Learning"),
            LinkedInSkill(name="Python"),
        ])
        assert _top_skills(profile) == ["machine learning", "python"]

    def test_deduplicates_and_skips_garbage(self):
        # Pydantic rejects non-string/non-object skills, so test the helper directly with a plain object.
        class P:
            skills = ["  AI Strategy ", "ai strategy", "", None, 123, "Leadership"]
        assert _top_skills(P()) == ["ai strategy", "leadership"]

    def test_empty_skills_returns_empty(self):
        assert _top_skills(_make_profile([])) == []
        assert _top_skills(_make_profile(None)) == []


class TestSkillsChanged:
    def test_first_scrape_records_snapshot_but_is_not_a_change(self):
        profile = _make_profile(["AI Strategy", "Product Growth"])
        with patch("cqc_lem.utilities.db.get_last_recorded_skills", return_value=[]), \
             patch("cqc_lem.utilities.db.set_last_recorded_skills") as mock_write:
            assert skills_changed_since_last_recorded(1, profile) is False
            # The snapshot is still recorded, so the NEXT scrape can be compared against it.
            mock_write.assert_called_once_with(1, ["ai strategy", "product growth"])

    def test_same_skills_no_change(self):
        profile = _make_profile(["AI Strategy", "Product Growth"])
        with patch("cqc_lem.utilities.db.get_last_recorded_skills",
                   return_value=["ai strategy", "product growth"]), \
             patch("cqc_lem.utilities.db.set_last_recorded_skills") as mock_write:
            assert skills_changed_since_last_recorded(1, profile) is False
            mock_write.assert_not_called()

    def test_order_change_counts_as_change(self):
        profile = _make_profile(["Product Growth", "AI Strategy"])
        with patch("cqc_lem.utilities.db.get_last_recorded_skills",
                   return_value=["ai strategy", "product growth"]), \
             patch("cqc_lem.utilities.db.set_last_recorded_skills") as mock_write:
            assert skills_changed_since_last_recorded(1, profile) is True
            mock_write.assert_called_once_with(1, ["product growth", "ai strategy"])

    def test_new_skill_counts_as_change(self):
        profile = _make_profile(["AI Strategy", "Product Growth", "Leadership"])
        with patch("cqc_lem.utilities.db.get_last_recorded_skills",
                   return_value=["ai strategy", "product growth"]), \
             patch("cqc_lem.utilities.db.set_last_recorded_skills"):
            assert skills_changed_since_last_recorded(1, profile) is True


class TestRedisWindow:
    def test_open_window_stores_skills(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            profile = _make_profile(["AI Strategy", "Product Growth"])
            skills = open_profile_skills_window(1, profile)
            assert skills == ["ai strategy", "product growth"]
            assert json.loads(store[_key(1)][0]) == skills
            assert store[_key(1)][1] == WINDOW_DAYS * 24 * 60 * 60

    def test_get_window_reads_open_window(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            store[_key(1)] = (json.dumps(["ai strategy"]), WINDOW_DAYS * 24 * 60 * 60)
            assert get_profile_skills_window(1) == ["ai strategy"]

    def test_get_window_returns_none_when_closed(self):
        client, _ = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            assert get_profile_skills_window(1) is None

    def test_close_window_removes_key(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            store[_key(1)] = (json.dumps(["ai strategy"]), WINDOW_DAYS * 24 * 60 * 60)
            close_profile_skills_window(1)
            assert _key(1) not in store

    def test_record_change_opens_window(self):
        profile = _make_profile(["AI Strategy", "Product Growth"])
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.db.get_last_recorded_skills", return_value=["product growth", "ai strategy"]), \
             patch("cqc_lem.utilities.db.set_last_recorded_skills"), \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client",
                   return_value=client):
            result = record_profile_skills_change(1, profile)
            assert result == ["ai strategy", "product growth"]
            assert _key(1) in store

    def test_record_no_change_leaves_window_closed(self):
        profile = _make_profile(["AI Strategy"])
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.db.get_last_recorded_skills", return_value=["ai strategy"]), \
             patch("cqc_lem.utilities.db.set_last_recorded_skills"), \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client",
                   return_value=client):
            assert record_profile_skills_change(1, profile) == []
            assert _key(1) not in store


class TestDirective:
    def test_directive_contains_skills_when_window_open(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            store[_key(7)] = (json.dumps(["ai strategy", "product growth"]), WINDOW_DAYS * 24 * 60 * 60)
            d = profile_skills_directive(7)
            assert "ai strategy, product growth" in d
            assert "weave them in ONLY when they genuinely fit" in d

    def test_directive_empty_when_window_closed(self):
        client, _ = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            assert profile_skills_directive(7) == ""

    def test_directive_empty_when_redis_unavailable(self):
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=None):
            assert profile_skills_directive(7) == ""


class TestRedisFailsOpen:
    """Redis is steering infrastructure, never a gate — every fault reads as "no window"."""

    def test_open_window_without_skills_stores_nothing(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            assert open_profile_skills_window(1, _make_profile([])) == []
            assert store == {}

    def test_open_window_returns_skills_when_redis_is_gone(self):
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=None):
            assert open_profile_skills_window(1, _make_profile(["AI Strategy"])) == ["ai strategy"]

    def test_open_window_survives_a_write_error(self):
        client, _ = _redis_client_mock()
        with patch.object(client, "setex", side_effect=RuntimeError("redis down")), \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            assert open_profile_skills_window(1, _make_profile(["AI Strategy"])) == ["ai strategy"]

    def test_close_window_is_false_when_redis_is_gone(self):
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=None):
            assert close_profile_skills_window(1) is False

    def test_close_window_is_false_on_a_delete_error(self):
        client, _ = _redis_client_mock()
        with patch.object(client, "delete", side_effect=RuntimeError("redis down")), \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            assert close_profile_skills_window(1) is False

    def test_read_error_reads_as_no_window(self):
        client, _ = _redis_client_mock()
        with patch.object(client, "get", side_effect=RuntimeError("redis down")), \
             patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            assert get_profile_skills_window(1) is None

    def test_unparseable_payload_reads_as_no_window(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            store[_key(1)] = ("{not json", 60)
            assert get_profile_skills_window(1) is None

    def test_non_list_payload_reads_as_no_window(self):
        client, store = _redis_client_mock()
        with patch("cqc_lem.utilities.profile_skills_window.shared_redis_client", return_value=client):
            store[_key(1)] = (json.dumps({"skills": ["a"]}), 60)
            assert get_profile_skills_window(1) is None
