"""Impact report (issue #2643): window maths, exclusions, latest snapshot, admin gate, GitHub note."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import impact_report as ir, impact_report_job as job

pytestmark = pytest.mark.unit

# 2026-11-10 16:00 UTC = 11:00 ET, deploy day 2026-11-10 (ET).
CHANGE = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)


def _post(day: str, imp=100, reactions=5, comments=3, own=1, reposts=0):
    return {"scheduled_time": datetime.fromisoformat(day + "T15:00:00"), "impressions": imp,
            "reactions": reactions, "comments": comments, "own_comments": own, "reposts": reposts}


def _build(posts=(), logs=(), leads=(), followers=(), cost=(), days=14, exclude=()):
    return ir.build_impact_report(CHANGE, days, exclude, list(posts),
                                  {"logs": list(logs), "leads": list(leads)},
                                  list(followers), list(cost))


class TestWindowMaths:
    def test_deploy_day_in_neither_window(self):
        before, after = ir.window_days(CHANGE, 14)
        assert date(2026, 11, 10) not in before + after
        assert before[0] == date(2026, 10, 27) and before[-1] == date(2026, 11, 9)
        assert after[0] == date(2026, 11, 11) and after[-1] == date(2026, 11, 24)
        assert len(before) == len(after) == 14

    def test_et_day_boundary_uses_new_york_not_utc(self):
        # 03:00 UTC on the 11th is still the 10th in New York.
        assert ir.et_day(datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)) == date(2026, 11, 10)

    def test_utc_bounds_cover_et_midnights(self):
        start, end = ir.utc_bounds(CHANGE, 14)
        assert start == datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)  # EDT midnight
        assert end == datetime(2026, 11, 25, 5, 0, tzinfo=timezone.utc)    # EST midnight

    def test_outage_days_dropped_from_both_windows(self):
        out = [date(2026, 11, 8), date(2026, 11, 12)]
        before, after = ir.window_days(CHANGE, 14, out)
        assert date(2026, 11, 8) not in before and date(2026, 11, 12) not in after
        assert len(before) == 13 and len(after) == 13

    def test_outage_exclusion_changes_the_per_day_rate(self):
        # 10 comments on one outage-adjacent day inside "before": excluded day's events vanish.
        logs = [{"hour": "2026-11-08 15", "action_type": "comment", "result": "success", "n": 10}]
        kept = _build(logs=logs)["metrics"]["comments_per_day"]
        dropped = _build(logs=logs, exclude=[date(2026, 11, 8)])["metrics"]["comments_per_day"]
        assert kept["n_before"] == 10 and dropped["n_before"] == 0


class TestVerdict:
    def test_small_n_is_inconclusive(self):
        assert ir.verdict(1.0, 5.0, 2, 3, ir.MIN_EVENTS) == "inconclusive"

    def test_unread_side_is_inconclusive(self):
        assert ir.verdict(None, 5.0, 50, 50, ir.MIN_EVENTS) == "inconclusive"

    @pytest.mark.parametrize("before,after,expected", [(10, 10.2, "flat"), (10, 15, "up"),
                                                       (10, 5, "down"), (0, 0, "flat"), (0, 3, "up")])
    def test_direction(self, before, after, expected):
        assert ir.verdict(before, after, 50, 50, ir.MIN_EVENTS) == expected


class TestMetrics:
    def test_counts_by_et_day_and_result(self):
        logs = [
            {"hour": "2026-11-12 15", "action_type": "comment", "result": "success", "n": 28},
            {"hour": "2026-11-12 15", "action_type": "group_comment", "result": "success", "n": 14},
            {"hour": "2026-11-12 15", "action_type": "comment", "result": "failure", "n": 99},
            {"hour": "2026-11-12 15", "action_type": "dm", "result": "failure", "n": 4},
            {"hour": "2026-11-12 15", "action_type": "invite", "result": "success", "n": 2},
            {"hour": "2026-11-12 15", "action_type": "engaged", "result": "success", "n": 1},
        ]
        m = _build(logs=logs)["metrics"]
        assert m["comments_per_day"]["n_after"] == 42  # group comments count, failures do not
        assert m["comments_per_day"]["after"] == 3.0
        assert m["dm_failures_per_day"]["n_after"] == 4
        assert m["invites_per_day"]["n_after"] == 3
        assert m["comments_per_day"]["verdict"] == "inconclusive"  # nothing before

    def test_deploy_day_events_never_counted(self):
        logs = [{"hour": "2026-11-10 15", "action_type": "comment", "result": "success", "n": 50}]
        m = _build(logs=logs)["metrics"]["comments_per_day"]
        assert m["n_before"] == 0 and m["n_after"] == 0

    def test_leads_and_in_conversation(self):
        leads = [{"hour": "2026-11-12 15", "stage": "cold", "n": 8},
                 {"hour": "2026-11-12 15", "stage": "in_conversation", "n": 3}]
        m = _build(leads=leads)["metrics"]
        assert m["new_leads_per_day"]["n_after"] == 11
        assert m["new_in_conversation_per_day"]["n_after"] == 3

    def test_organic_rate_excludes_own_comments_and_post_windows(self):
        posts = [_post("2026-11-05", imp=100, reactions=4, comments=3, own=3)] * 5 + \
                [_post("2026-11-15", imp=100, reactions=10, comments=3, own=3)] * 5
        m = _build(posts=posts)["metrics"]
        assert m["organic_engagement_rate"]["before"] == pytest.approx(0.04)
        assert m["organic_engagement_rate"]["after"] == pytest.approx(0.10)
        assert m["organic_engagement_rate"]["verdict"] == "up"
        assert m["posts_published_per_day"]["n_before"] == 5

    def test_unknown_own_comments_is_not_measured_not_zero(self):
        posts = [_post("2026-11-05", own=None)] * 6 + [_post("2026-11-15", own=None)] * 6
        report = _build(posts=posts)
        assert report["metrics"]["organic_engagement_rate"]["before"] is None
        assert "organic_engagement_rate" in report["not_measured"]

    def test_follower_net_needs_two_snapshots(self):
        snaps = [{"captured_at": datetime(2026, 11, 12, 12), "follower_count": 120},
                 {"captured_at": datetime(2026, 11, 20, 12), "follower_count": 150},
                 {"captured_at": datetime(2026, 11, 1, 12), "follower_count": 100}]
        m = _build(followers=snaps)["metrics"]["net_followers"]
        assert m["after"] == 30 and m["before"] is None

    def test_cost_per_day(self):
        cost = [{"day": date(2026, 11, 12), "usd": 14}, {"day": date(2026, 11, 13), "usd": 14}]
        m = _build(cost=cost)["metrics"]["llm_cost_usd_per_day"]
        assert m["after"] == 2.0 and m["n_after"] == 2

    def test_always_unmeasured_named(self):
        nm = _build()["not_measured"]
        assert {"author_replies", "saves", "meetings_won"} <= set(nm)


class TestLatestSnapshot:
    def test_query_picks_max_post_stats_id_per_post(self):
        from cqc_lem.platform.db.repositories import posts as repo
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        with patch.object(repo, "db_cursor", return_value=ctx):
            repo.get_impact_post_rows(1, CHANGE, CHANGE + timedelta(days=1))
        sql = cursor.execute.call_args[0][0]
        assert "PARTITION BY post_id" in sql and "(source='api') DESC" in sql


class TestLoad:
    def test_failed_read_raises_never_empty(self):
        with patch("cqc_lem.utilities.db.get_impact_post_rows", return_value=None), \
             patch("cqc_lem.utilities.db.get_impact_hourly_counts", return_value={"logs": [], "leads": []}), \
             patch("cqc_lem.utilities.db.get_impact_llm_cost_by_day", return_value=[]), \
             patch("cqc_lem.utilities.db.get_follower_stats", return_value=[]):
            with pytest.raises(ir.ImpactReadError):
                ir.load_impact_report(1, CHANGE)

    def test_days_clamped(self):
        with patch("cqc_lem.utilities.db.get_impact_post_rows", return_value=[]), \
             patch("cqc_lem.utilities.db.get_impact_hourly_counts", return_value={"logs": [], "leads": []}), \
             patch("cqc_lem.utilities.db.get_impact_llm_cost_by_day", return_value=[]), \
             patch("cqc_lem.utilities.db.get_follower_stats", return_value=[]):
            assert ir.load_impact_report(1, CHANGE, days=500)["days"] == ir.MAX_DAYS


class TestAdminGate:
    URL = "/api/admin/impact-report"
    PARAMS = {"user_id": 1, "change_at": "2026-11-10T16:00:00Z"}

    def test_forbidden_without_secret(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            assert api_client.get(self.URL, params=self.PARAMS).status_code == 403

    def test_forbidden_wrong_secret(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            r = api_client.get(self.URL, params=self.PARAMS, headers={"x-admin-secret": "no"})
        assert r.status_code == 403

    def test_ok_with_secret(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.utilities.impact_report.load_impact_report",
                   return_value={"metrics": {}}) as load:
            r = api_client.get(self.URL, params={**self.PARAMS, "exclude_days": "2026-11-08"},
                               headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 200
        assert load.call_args[0][3] == [date(2026, 11, 8)]

    def test_failed_read_is_503(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.utilities.impact_report.load_impact_report",
                   side_effect=ir.ImpactReadError("x")):
            r = api_client.get(self.URL, params=self.PARAMS, headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 503

    def test_bad_exclude_day_is_422(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            r = api_client.get(self.URL, params={**self.PARAMS, "exclude_days": "nope"},
                               headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 422

    def test_route_hidden_from_published_schema(self, api_client):
        assert "/api/admin/impact-report" not in api_client.app.openapi()["paths"]


class TestGithubNote:
    def test_note_has_no_metrics_names_or_urls(self):
        note = ir.release_note("v0.209.0")
        assert note == "Impact report ready for v0.209.0."
        assert "http" not in note and not any(ch in note for ch in "%$")

    @pytest.mark.parametrize("tag", ["latest", "v1.2.3 see https://x.y", "", None, "v1.2"])
    def test_non_version_tag_posts_nothing(self, tag):
        assert ir.release_note(tag) is None


class TestJob:
    NOW = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)

    def test_due_releases_window(self):
        rels = [{"tag_name": "v1.0.0", "published_at": "2026-11-09T10:00:00Z"},   # 15d: due
                {"tag_name": "v0.9.0", "published_at": "2026-11-10T10:00:00Z"},   # 14d: last day partial
                {"tag_name": "v0.8.0", "published_at": "2026-11-01T10:00:00Z"},   # too old
                {"tag_name": "v1.1.0", "published_at": "2026-11-09T10:00:00Z", "draft": True}]
        assert [t for t, _ in job.due_releases(rels, self.NOW)] == ["v1.0.0"]
        assert job.due_releases(None, self.NOW) == []

    def _run(self, flag=True, redis=None, email=True, releases=None, now=None, recipient="o@x.test"):
        redis = redis if redis is not None else MagicMock(**{"set.return_value": True})
        releases = releases if releases is not None else [
            {"tag_name": "v1.0.0", "published_at": "2026-11-09T10:00:00Z"}]
        comment = MagicMock(return_value=True)
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=flag), \
             patch.object(job, "_recipient", return_value=recipient), \
             patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=redis), \
             patch("cqc_lem.utilities.feedback.issue_service.github_request", return_value=releases), \
             patch("cqc_lem.utilities.feedback.issue_service.comment_on_issue", comment), \
             patch.object(job, "load_impact_report", return_value={
                 "change_at": "x", "windows": {"deploy_day": "d", "before_days": [], "after_days": []},
                 "metrics": {"a": {"before": 1, "after": 2, "delta": 1, "n_before": 20,
                                   "n_after": 20, "verdict": "up"}}, "not_measured": {}}), \
             patch("cqc_lem.utilities.email.send_impact_report_email", return_value=email) as send:
            return job.run_impact_report_sweep(now or self.NOW), send, comment, redis

    def test_flag_off_does_nothing(self):
        result, send, comment, _ = self._run(flag=False)
        assert result["action"] == "disabled" and not send.called and not comment.called

    def test_sends_email_and_metric_free_note(self):
        result, send, comment, _ = self._run()
        assert result["results"]["v1.0.0"] == "sent"
        assert "up" in send.call_args[0][2]  # metrics live in the EMAIL
        comment.assert_called_once_with(2643, "Impact report ready for v1.0.0.")

    def test_already_claimed_sends_nothing(self):
        result, send, comment, _ = self._run(redis=MagicMock(**{"set.return_value": None}))
        assert result["results"]["v1.0.0"] == "already"
        assert not send.called and not comment.called

    def test_failed_email_releases_claim_and_posts_no_note(self):
        result, _, comment, redis = self._run(email=False)
        assert result["results"]["v1.0.0"] == "email_failed"
        redis.delete.assert_called_once()
        assert not comment.called

    def test_unreadable_inputs_release_claim(self):
        redis = MagicMock(**{"set.return_value": True})
        with patch.object(job, "load_impact_report", side_effect=ir.ImpactReadError("x")), \
             patch("cqc_lem.utilities.email.send_impact_report_email") as send:
            assert job._send(1, "k", "l", self.NOW, 14, "o@x.test", redis) == "unread"
        redis.delete.assert_called_once()
        assert not send.called

    def test_no_redis_sends_nothing(self):
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True), \
             patch.object(job, "_recipient", return_value="o@x.test"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=None):
            assert job.run_impact_report_sweep(self.NOW)["reason"] == "no redis"

    def test_no_recipient_skips(self):
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True), \
             patch.object(job, "_recipient", return_value=""):
            assert job.run_impact_report_sweep(self.NOW)["reason"] == "no recipient"

    def test_baseline_on_first_of_month_emailed_without_github_note(self):
        first = datetime(2026, 12, 1, 14, 30, tzinfo=timezone.utc)
        result, send, comment, _ = self._run(releases=[], now=first)
        assert result["results"]["baseline:2026-12"] == "sent"
        assert send.called and not comment.called

    def test_redis_fault_claims_nothing(self):
        redis = MagicMock()
        redis.set.side_effect = RuntimeError("down")
        assert job._claim(redis, "k") is False
