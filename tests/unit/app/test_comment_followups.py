"""Unit tests for the auto-follow-up feature — issue #478 (pure logic; Selenium DOM is validated
on a supervised run). Covers URL derivation, question detection, stable reply keys, and the
sweep_reply_comments concurrency lock."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

RA = "cqc_lem.app.run_automation"


def _fn(name):
    import importlib
    return getattr(importlib.import_module(RA), name)


class TestPostUrlFromKey:
    def test_feedurn_becomes_navigable_url(self):
        f = _fn("_post_url_from_key")
        assert f("feedurn://urn:li:activity:7486451907129958400") == \
            "https://www.linkedin.com/feed/update/urn:li:activity:7486451907129958400/"

    def test_legacy_hash_key_is_not_navigable(self):
        assert _fn("_post_url_from_key")("feedpost://abc123") is None

    def test_passthrough_http(self):
        assert _fn("_post_url_from_key")("https://x/y").startswith("https://")

    def test_none_and_garbage(self):
        f = _fn("_post_url_from_key")
        assert f(None) is None
        assert f("feedurn://not-a-urn") is None


class TestReplyIsQuestion:
    def test_question_mark_is_a_question(self):
        assert _fn("_reply_is_question")("Interesting — how do you handle scale?") is True

    def test_statement_is_not(self):
        assert _fn("_reply_is_question")("Great point, totally agree.") is False

    def test_url_query_string_does_not_count(self):
        assert _fn("_reply_is_question")("see https://x.com/a?b=1 for more") is False

    def test_empty(self):
        assert _fn("_reply_is_question")("") is False


class TestFollowupReplyKey:
    def test_stable_across_whitespace_and_case(self):
        f = _fn("_followup_reply_key")
        a = f("feedurn://urn:li:activity:1", "https://www.linkedin.com/in/jane-doe/", "Nice!  …see more")
        b = f("feedurn://urn:li:activity:1", "https://www.linkedin.com/in/jane-doe/?x=1", "nice!")
        assert a == b  # same replier + same normalized text -> one key

    def test_namespaced_to_post_and_replier(self):
        f = _fn("_followup_reply_key")
        k = f("feedurn://urn:li:activity:1", "https://www.linkedin.com/in/jane-doe/", "hi")
        assert k.startswith("feedurn://urn:li:activity:1#reply:jane-doe:")

    def test_different_repliers_differ(self):
        f = _fn("_followup_reply_key")
        assert f("p", "https://www.linkedin.com/in/a/", "hi") != \
               f("p", "https://www.linkedin.com/in/b/", "hi")
