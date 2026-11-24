"""Daily review read side (#2625 Phase 1): lane adapters, summary, routes, digest beat."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.utilities import review_queue as rq

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
DB = "cqc_lem.utilities.db."


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).replace(tzinfo=None).isoformat()


@pytest.fixture
def lanes():
    """Patch every facade reader the adapters import, one row per lane."""
    with patch(DB + "get_scheduled_dms", return_value={"dms": [
            {"id": 1, "recipient_name": "Ann", "message": "hi  there", "created_at": _ago(72)}]}), \
         patch(DB + "get_connection_requests", return_value={"requests": [
             {"id": 2, "recipient_profile_url": "u", "message": "m", "created_at": _ago(5)}]}), \
         patch(DB + "get_outreach_targets", return_value={"targets": [
             {"id": 3, "stage": "comment", "target_name": "Bo", "draft_text": "d",
              "created_at": _ago(10)}]}), \
         patch(DB + "get_catchup_touches", return_value={"touches": [
             {"id": 4, "event_type": "promotion", "person_name": "Cy", "message": "gz",
              "created_at": _ago(1)}]}), \
         patch(DB + "get_held_pending_posts", return_value=[
             {"id": 5, "content": "post", "post_type": "text", "updated_at": NOW,
              "gate_reason": '[{"gate":"slop","label":"AI slop","demoted":true}]',
              "ever_gate_demoted": 0},
             {"id": 6, "content": "p2", "post_type": "text", "updated_at": NOW,
              "gate_reason": None, "ever_gate_demoted": 1}]), \
         patch(DB + "get_pending_newsletter_editions", return_value=[
             {"id": 7, "title": "T", "body": "b", "status": "draft", "fact_hold": None},
             {"id": 8, "title": "A", "body": "b", "status": "approved"}]), \
         patch(DB + "get_recent_unreadable_stopped_followups", return_value=[
             {"id": 10, "profile_url": "p", "first_name": "Di", "event_type": "manual",
              "next_step": 1, "updated_at": _ago(4)}]), \
         patch(DB + "get_open_group_post_draft", return_value={
             "id": 9, "group_name": "G", "content": "c", "created_at": _ago(30)}), \
         patch("cqc_lem.utilities.comment_candidate_drafts.get_drafted_comment_candidates",
               return_value=[{"id": 10, "author": "Di", "score": 0.8, "draft_text": "nice",
                              "scanned_at": NOW}]):
        yield


def test_unreadable_followup_lane_is_a_notice_with_no_actions(lanes):
    (item,) = rq.get_review_queue(1, lane="stopped_followups", now=NOW)
    assert item["summary"] == "Follow-up to Di stopped"
    assert item["allowed_actions"] == [] and item["age_hours"] == 4.0
    assert "never became readable" in item["hold_reason"]


def test_unreadable_followup_reader_failure_skips_only_that_lane(lanes):
    with patch(DB + "get_recent_unreadable_stopped_followups", side_effect=RuntimeError("x")):
        lanes_seen = {i["lane"] for i in rq.get_review_queue(1, now=NOW)}
    assert "stopped_followups" not in lanes_seen and "posts" in lanes_seen


def test_every_lane_returns_items_oldest_first_with_the_item_shape(lanes):
    items = rq.get_review_queue(1, now=NOW)
    assert {i["lane"] for i in items} == set(rq.LANE_IDS)
    assert items[0]["id"] == 1 and items[0]["age_hours"] == 72.0
    assert items[-1]["age_hours"] is None  # unmeasured sorts last
    assert set(items[0]) == {"lane", "id", "age_hours", "summary", "draft_text", "hold_reason",
                             "risk_flags", "confidence", "allowed_actions", "edit_href",
                             "agent_created"}
    assert items[0]["summary"] == "DM to Ann"


def test_held_post_reasons(lanes):
    posts = {i["id"]: i for i in rq.get_review_queue(1, lane="posts", now=NOW)}
    assert posts[5]["hold_reason"] == "AI slop"
    assert "Repaired" in posts[6]["hold_reason"]


def test_only_a_draft_edition_is_a_review_item(lanes):
    ids = [i["id"] for i in rq.get_review_queue(1, lane="newsletter_editions", now=NOW)]
    assert ids == [7]


def test_lane_filter_and_a_failing_lane_does_not_sink_the_rest(lanes):
    with patch(DB + "get_scheduled_dms", side_effect=RuntimeError("boom")):
        items = rq.get_review_queue(1, now=NOW)
    assert "scheduled_dms" not in {i["lane"] for i in items} and len(items) > 1


def test_empty_group_draft_is_no_item():
    with patch(DB + "get_open_group_post_draft", return_value=None):
        assert rq._read_group_post_draft(1, NOW) == []


def test_summary_counts_stale_and_ignores_unmeasured(lanes):
    s = rq.summarize(rq.get_review_queue(1, now=NOW))
    assert s["total"] == 10 and s["stale_48h"] == 1 and s["oldest_age_hours"] == 72.0
    assert s["by_lane"]["posts"] == 2 and s["by_lane"]["comment_candidates"] == 1
    assert rq.summarize([])["oldest_age_hours"] is None


@pytest.mark.parametrize("value,expected", [
    ("garbage", None), (None, None), (NOW - timedelta(hours=2), 2.0),
    (NOW + timedelta(hours=2), 0.0)])
def test_age_hours(value, expected):
    assert rq._age_hours(value, NOW) == expected


def test_summary_text_is_clipped():
    assert len(rq._clip("x" * 500)) == 140


# --- routes -----------------------------------------------------------------------------------

@pytest.fixture
def client(api_client):
    return api_client


def test_queue_route_scopes_to_the_caller(client):
    with patch("cqc_lem.api.main.get_session_user_id", return_value=7), \
         patch("cqc_lem.api.routers.review.get_review_queue", return_value=[{"id": 1}]) as q:
        r = client.get("/api/review/queue", params={"session_token": "t", "lane": "posts"})
    assert r.status_code == 200 and r.json()["detail"] == [{"id": 1}]
    q.assert_called_once_with(7, lane="posts")


def test_queue_route_rejects_unknown_lane_and_bad_session(client):
    with patch("cqc_lem.api.main.get_session_user_id", return_value=7):
        assert client.get("/api/review/queue",
                          params={"session_token": "t", "lane": "nope"}).status_code == 422
    with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
        assert client.get("/api/review/queue", params={"session_token": "t"}).status_code == 401
        assert client.get("/api/review/summary", params={"session_token": "t"}).status_code == 401


def test_summary_route(client):
    with patch("cqc_lem.api.main.get_session_user_id", return_value=7), \
         patch("cqc_lem.api.routers.review.get_review_queue", return_value=[]):
        r = client.get("/api/review/summary", params={"session_token": "t"})
    assert r.json()["detail"]["total"] == 0


def test_review_paths_are_off_every_scoped_surface():
    """No scoped session (agent, operator, observer, ...) may reach /review/*.

    One exception, owner-decided in #2635/#2647: the Assist scope posts the done receipt to
    /review/comment-posted (POST only). It is the only /review path on any scoped surface.
    """
    from cqc_lem.api import main
    for scope, surface in main._SCOPE_SURFACES.items():
        allowed = {"/review/comment-posted"} if scope == main.SESSION_SCOPE_ASSIST else set()
        assert {p for p in surface if p.startswith("/review")} <= allowed, scope


# --- digest beat ------------------------------------------------------------------------------

_SUMMARY_STALE = {"total": 3, "stale_48h": 2, "oldest_age_hours": 80.0, "by_lane": {"posts": 3}}
_SUMMARY_FRESH = {"total": 1, "stale_48h": 0, "oldest_age_hours": 3.0, "by_lane": {"posts": 1}}


def _beat(summaries, enabled=True):
    from cqc_lem.app.run_scheduler import auto_notify_stale_approvals
    with patch(DB + "get_user_ids_with_pending_review", return_value=list(summaries)), \
         patch(DB + "get_review_digest_enabled", return_value=enabled), \
         patch("cqc_lem.utilities.review_queue.get_review_queue", return_value=[]), \
         patch("cqc_lem.utilities.review_queue.summarize", side_effect=list(summaries.values())), \
         patch("cqc_lem.utilities.notifications.notify_stale_approvals",
               return_value=True) as notify:
        return auto_notify_stale_approvals(), notify


def test_beat_emails_only_users_with_stale_items():
    out, notify = _beat({1: _SUMMARY_STALE, 2: _SUMMARY_FRESH})
    assert out == "Sent 1 stale-approvals digest(s)"
    notify.assert_called_once_with(1, _SUMMARY_STALE, rq.REVIEW_STALE_HOURS)


def test_beat_honours_opt_out_and_empty_sweep():
    out, notify = _beat({1: _SUMMARY_STALE}, enabled=False)
    assert out == "Sent 0 stale-approvals digest(s)" and not notify.called
    assert _beat({})[0] == "Sent 0 stale-approvals digest(s)"


def test_beat_survives_a_failing_user():
    from cqc_lem.app.run_scheduler import auto_notify_stale_approvals
    with patch(DB + "get_user_ids_with_pending_review", return_value=[1]), \
         patch(DB + "get_review_digest_enabled", side_effect=RuntimeError("x")):
        assert auto_notify_stale_approvals() == "Sent 0 stale-approvals digest(s)"


def test_beat_is_scheduled_daily_at_13_utc():
    from cqc_lem.app.my_celery import app
    entry = app.conf.beat_schedule["notify-stale-approvals"]
    assert entry["task"] == "cqc_lem.app.run_scheduler.auto_notify_stale_approvals"
    assert (entry["schedule"]._orig_hour, entry["schedule"]._orig_minute) == ("13", "0")


# --- notification + email -----------------------------------------------------------------------

def _notify(redis_client, email="a@b.c", sent=True):
    from cqc_lem.utilities import notifications as n
    with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
               return_value=redis_client), \
         patch.object(n, "get_user_email", return_value=email), \
         patch("cqc_lem.utilities.email.send_stale_approvals_email", return_value=sent) as send:
        return n.notify_stale_approvals(1, _SUMMARY_STALE, 48), send


def test_notify_sends_once_a_day():
    redis = MagicMock()
    redis.set.return_value = True
    ok, send = _notify(redis)
    assert ok and send.call_args.args[4] == {"Held posts": 3}
    redis.set.return_value = None
    ok, send = _notify(redis)
    assert not ok and not send.called


def test_notify_releases_the_claim_when_the_send_fails():
    redis = MagicMock()
    redis.set.return_value = True
    ok, _ = _notify(redis, sent=False)
    assert not ok and redis.delete.called


def test_notify_fails_open_without_redis_and_skips_no_email():
    assert _notify(None)[0] is True
    ok, send = _notify(None, email=None)
    assert not ok and not send.called


def test_notify_redis_errors_do_not_silence():
    redis = MagicMock()
    redis.set.side_effect = RuntimeError("down")
    assert _notify(redis)[0] is True


def test_email_renders_counts_and_escapes():
    from cqc_lem.utilities import email as em
    with patch.object(em, "_dispatch_email", return_value=True) as d:
        assert em.send_stale_approvals_email("a@b.c", 3, 2, 72.0, {"<b>": 3, "Zero": 0}, 48)
    html = d.call_args.args[2]
    assert "&lt;b&gt;" in html and "Zero" not in html and "3.0 days" in html
    assert "2 approvals waiting over 48h" in d.call_args.args[1]


# --- repository reads ---------------------------------------------------------------------------

def _cursor(rows=None, row=None, err=False):
    cur = MagicMock()
    if err:
        cur.execute.side_effect = mysql.connector.Error("x")
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = row
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx


@pytest.mark.parametrize("err,expected", [(False, [{"id": 1}]), (True, [])])
def test_get_held_pending_posts(err, expected):
    from cqc_lem.platform.db.repositories import posts
    with patch.object(posts, "db_cursor", return_value=_cursor(rows=[{"id": 1}], err=err)):
        assert posts.get_held_pending_posts(1) == (expected)


@pytest.mark.parametrize("row,err,expected", [
    (None, False, True), ({"review_digest_enabled": None}, False, True),
    ({"review_digest_enabled": 1}, False, True), ({"review_digest_enabled": 0}, False, False),
    (None, True, True)])
def test_get_review_digest_enabled(row, err, expected):
    from cqc_lem.platform.db.repositories import users
    with patch.object(users, "db_cursor", return_value=_cursor(row=row, err=err)):
        assert users.get_review_digest_enabled(1) is expected


@pytest.mark.parametrize("err,expected", [(False, [1, 2]), (True, [])])
def test_get_user_ids_with_pending_review(err, expected):
    from cqc_lem.platform.db.repositories import dashboard
    with patch.object(dashboard._connection, "db_cursor",
                      return_value=_cursor(rows=[(1,), (2,)], err=err)):
        assert dashboard.get_user_ids_with_pending_review() == expected
