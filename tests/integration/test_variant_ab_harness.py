"""Integration test for the outcome-tracked A/B harness (#396 / D2).

Exercises the harness end to end through a stateful fake cursor that models the `posts`,
`post_stats` and `post_variants` tables: a shipped variant is recorded (`record_shipped_variant`),
its post's engagement is captured (`record_post_stats`), the two are joined back
(`get_variant_outcome_rows`), and winner selection (`select_variant_winners`) provably reflects the
seeded results — the acceptance criterion — without a live MySQL container.
"""
import json
from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


class _FakeCursor:
    """Minimal MySQL-ish cursor over in-memory `posts`, `post_stats`, `post_variants` stores."""

    def __init__(self, posts, stats, variants):
        self.posts = posts
        self.stats = stats
        self.variants = variants
        self._result = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO post_variants"):
            user_id, post_id, batch_id, variant_index, variant_key, combo = params
            self.variants[post_id] = {  # UNIQUE(post_id) → re-record overwrites
                "user_id": user_id, "post_id": post_id, "batch_id": batch_id,
                "variant_index": variant_index, "variant_key": variant_key, "combo": combo,
            }
            self.rowcount = 1
        elif s.startswith("SELECT archetype, hook_style, post_type, topic, buyer_stage FROM posts"):
            p = self.posts.get(params[0])
            self._result = [(p["archetype"], p["hook_style"], p["post_type"],
                             p["topic"], p["buyer_stage"])] if p else []
        elif s.startswith("INSERT INTO post_stats"):
            (user_id, post_id, reactions, comments, reposts, impressions, saves,
             archetype, hook_style, fmt, topic, buyer_stage) = params
            self.lastrowid = len(self.stats) + 1
            self.stats.append({
                "id": self.lastrowid, "user_id": user_id, "post_id": post_id,
                "reactions": reactions, "comments": comments, "reposts": reposts,
                "impressions": impressions, "saves": saves,
            })
            self.rowcount = 1
        elif s.startswith("SELECT v.variant_key, p.scheduled_time, s.reactions"):
            user_id = params[0]
            latest = {}
            for row in self.stats:  # append order → last wins = MAX(id)
                if row["user_id"] == user_id:
                    latest[row["post_id"]] = row
            self._result = []
            for v in self.variants.values():
                if v["user_id"] != user_id:
                    continue
                r = latest.get(v["post_id"])
                if r is None or v["post_id"] not in self.posts:
                    continue
                self._result.append((v["variant_key"], self.posts[v["post_id"]]["scheduled_time"],
                                     r["reactions"], r["comments"], r["reposts"], r["impressions"]))
        elif s.startswith("SELECT post_id, variant_key FROM post_variants"):
            user_id = params[0]
            self._result = [(v["post_id"], v["variant_key"]) for v in self.variants.values()
                            if v["user_id"] == user_id]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, posts, stats, variants):
        self._cur = _FakeCursor(posts, stats, variants)

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


class TestVariantABHarness:
    def test_shipped_variant_recorded_and_winner_reflects_seeded_results(self):
        from cqc_lem.utilities.db import get_variant_outcome_rows, record_post_stats, record_shipped_variant
        from cqc_lem.utilities.post_stats import select_variant_winners

        now = datetime(2026, 7, 24, 12, 0)
        # Two posts shipped variant A, two shipped variant B — B seeded as the strong performer.
        posts = {
            1: _post(datetime(2026, 7, 23, 9, 0)),
            2: _post(datetime(2026, 7, 22, 9, 0)),
            3: _post(datetime(2026, 7, 23, 9, 0)),
            4: _post(datetime(2026, 7, 22, 9, 0)),
        }
        stats, variants = [], {}

        def _conn(*a, **k):
            return _FakeConn(posts, stats, variants)

        combo_a = {"image_model": "flux-dev", "video_model": "gen4_turbo", "ratio": "1:1"}
        combo_b = {"image_model": "flux-1.1-pro", "video_model": "gen4_turbo", "ratio": "1:1"}
        with patch("cqc_lem.platform.db.connection.get_db_connection", side_effect=_conn):
            # 1) Persist which variant actually shipped for each post.
            assert record_shipped_variant(1, 1, "A", combo=combo_a, batch_id="b", variant_index=0)
            assert record_shipped_variant(1, 2, "A", combo=combo_a, batch_id="b", variant_index=0)
            assert record_shipped_variant(1, 3, "B", combo=combo_b, batch_id="b", variant_index=1)
            assert record_shipped_variant(1, 4, "B", combo=combo_b, batch_id="b", variant_index=1)
            # 2) Capture engagement outcomes — B far outperforms A.
            record_post_stats(1, 1, reactions=3, comments=0, impressions=500)
            record_post_stats(1, 2, reactions=4, comments=1, impressions=500)
            record_post_stats(1, 3, reactions=60, comments=15, impressions=500)
            record_post_stats(1, 4, reactions=55, comments=12, impressions=500)
            # 3) Read the shipped variants back joined to their outcomes, then pick a winner.
            rows = get_variant_outcome_rows(1)
            result = select_variant_winners(rows, now=now)

        assert len(rows) == 4
        # combo JSON round-trips as provenance on the recorded row.
        assert json.loads(variants[1]["combo"])["image_model"] == "flux-dev"
        assert result["winner"] == "B"
        assert [r["key"] for r in result["ranking"]] == ["B", "A"]
        assert result["ranking"][0]["samples"] == 2
        assert result["ranking"][0]["metric"] == "engagement_rate"  # every row has impressions

    def test_shipped_variant_lookup_feeds_the_posthog_experiment_label(self):
        """Issue #652: the stats sweep reads {post_id: variant_key} ONCE and puts each post's arm on
        its `post_outcome` event, so PostHog can compare variants beside select_variant_winners.
        """
        from cqc_lem.utilities.db import get_shipped_variant_keys, record_shipped_variant
        from cqc_lem.utilities.experiments import POST_MEDIA_VARIANT, experiment_properties

        posts = {1: _post(datetime(2026, 7, 23, 9, 0)), 2: _post(datetime(2026, 7, 22, 9, 0))}
        variants = {}

        def _conn(*a, **k):
            return _FakeConn(posts, [], variants)

        with patch("cqc_lem.platform.db.connection.get_db_connection", side_effect=_conn):
            record_shipped_variant(1, 1, "flux-dev|gen4_turbo|1:1")
            record_shipped_variant(1, 2, "flux-1.1-pro|gen4_turbo|1:1")
            record_shipped_variant(2, 3, "other-user")  # never leaks across users
            shipped = get_shipped_variant_keys(1)

        assert shipped == {1: "flux-dev|gen4_turbo|1:1", 2: "flux-1.1-pro|gen4_turbo|1:1"}
        props = experiment_properties(1, extra={POST_MEDIA_VARIANT: shipped[1]})
        assert props == {f"$feature/{POST_MEDIA_VARIANT}": "flux-dev-gen4-turbo-1-1"}

    def test_re_recording_overwrites_shipped_variant(self):
        from cqc_lem.utilities.db import get_variant_outcome_rows, record_shipped_variant
        posts = {1: _post(datetime(2026, 7, 23, 9, 0))}
        stats = [{"id": 1, "user_id": 1, "post_id": 1, "reactions": 10, "comments": 0,
                  "reposts": 0, "impressions": None}]
        variants = {}

        def _conn(*a, **k):
            return _FakeConn(posts, stats, variants)

        with patch("cqc_lem.platform.db.connection.get_db_connection", side_effect=_conn):
            record_shipped_variant(1, 1, "A")
            record_shipped_variant(1, 1, "B")  # same post → overwrite
            rows = get_variant_outcome_rows(1)

        assert len(rows) == 1 and rows[0]["variant_key"] == "B"


def _post(scheduled_time):
    return {"user_id": 1, "post_type": "text", "buyer_stage": "awareness",
            "scheduled_time": scheduled_time, "archetype": "a", "hook_style": "h", "topic": "t"}
