"""Integration test for the content-attribution feedback loop (#386).

Exercises the db.py helpers together (update_db_post_shape → record_post_stats →
get_post_engagement_rows) through a stateful fake cursor that models the `posts` and `post_stats`
tables, so a captured stat row provably resolves back to its post's hook/format/topic — the
acceptance criterion — without needing a live MySQL container.
"""
import re
import pytest
from datetime import datetime
from unittest.mock import patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"


class _FakeCursor:
    """Minimal MySQL-ish cursor over in-memory `posts` and `post_stats` stores."""

    def __init__(self, posts, stats):
        self.posts = posts
        self.stats = stats
        self._result = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("UPDATE posts SET archetype"):
            archetype, hook_style, topic, post_id = params
            self.posts[post_id].update(archetype=archetype, hook_style=hook_style, topic=topic)
            self.rowcount = 1
        elif s.startswith("SELECT archetype, hook_style, post_type, topic, buyer_stage FROM posts"):
            p = self.posts.get(params[0])
            self._result = [(p["archetype"], p["hook_style"], p["post_type"],
                             p["topic"], p["buyer_stage"])] if p else []
        elif s.startswith("INSERT INTO post_stats"):
            (user_id, post_id, reactions, comments, reposts, impressions,
             archetype, hook_style, fmt, topic, buyer_stage) = params
            self.lastrowid = len(self.stats) + 1
            self.stats.append({
                "id": self.lastrowid, "user_id": user_id, "post_id": post_id,
                "reactions": reactions, "comments": comments, "reposts": reposts,
                "impressions": impressions, "archetype": archetype, "hook_style": hook_style,
                "format": fmt, "topic": topic, "buyer_stage": buyer_stage,
            })
            self.rowcount = 1
        elif re.search(r"SELECT p\.scheduled_time, s\.reactions", s):
            user_id = params[0]
            latest = {}
            for row in self.stats:
                if row["user_id"] == user_id:
                    latest[row["post_id"]] = row  # append order → last wins = MAX(id)
            self._result = [
                (self.posts[r["post_id"]]["scheduled_time"], r["reactions"], r["comments"],
                 r["reposts"], r["archetype"], r["hook_style"], r["format"], r["topic"],
                 r["buyer_stage"])
                for r in latest.values() if r["post_id"] in self.posts
            ]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, posts, stats):
        self._cur = _FakeCursor(posts, stats)

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


class TestPostAttributionFeedbackLoop:
    def test_captured_stat_resolves_to_post_hook_format_topic(self):
        from cqc_lem.utilities.db import (update_db_post_shape, record_post_stats,
                                          get_post_engagement_rows)

        posts = {9: {"user_id": 1, "post_type": "carousel", "buyer_stage": "consideration",
                     "scheduled_time": datetime(2026, 7, 20, 14, 0),
                     "archetype": None, "hook_style": None, "topic": None}}
        stats = []

        def _conn(*a, **k):
            return _FakeConn(posts, stats)

        with patch(f"{_DB}.get_db_connection", side_effect=_conn):
            # 1) Generation assigns the post's shape + topic.
            assert update_db_post_shape(9, "case_study", "bold_claim", topic="AI onboarding") is True
            # 2) The stat scraper captures engagement and snapshots attribution off the post.
            assert record_post_stats(1, 9, reactions=42, comments=7, reposts=3, impressions=900) is True
            # 3) The feedback loop reads stats back WITH attribution resolved.
            rows = get_post_engagement_rows(1)

        assert len(rows) == 1
        scheduled_time, reactions, comments, reposts, archetype, hook_style, fmt, topic, stage = rows[0]
        assert (reactions, comments, reposts) == (42, 7, 3)
        assert archetype == "case_study"
        assert hook_style == "bold_claim"
        assert fmt == "carousel"          # from posts.post_type
        assert topic == "AI onboarding"
        assert stage == "consideration"

    def test_stat_snapshot_survives_later_post_edit(self):
        """The snapshot is taken at capture time, so re-shaping the post afterward does NOT
        rewrite the historical stat's attribution."""
        from cqc_lem.utilities.db import (update_db_post_shape, record_post_stats,
                                          get_post_engagement_rows)

        posts = {5: {"user_id": 1, "post_type": "text", "buyer_stage": "awareness",
                     "scheduled_time": datetime(2026, 7, 21, 9, 0),
                     "archetype": "myth_bust", "hook_style": "question", "topic": "hiring"}}
        stats = []

        def _conn(*a, **k):
            return _FakeConn(posts, stats)

        with patch(f"{_DB}.get_db_connection", side_effect=_conn):
            record_post_stats(1, 5, reactions=10, comments=1)
            update_db_post_shape(5, "tactical_list", "story", topic="retention")
            rows = get_post_engagement_rows(1)

        _, _, _, _, archetype, hook_style, _, topic, _ = rows[0]
        assert (archetype, hook_style, topic) == ("myth_bust", "question", "hiring")
