"""Regression coverage for the single timezone contract (docs/timezone-contract.md).

The chain under test is the one a user actually experiences:

    wall clock picked in the user's tz
      -> UTC ISO sent by the SPA           (zonedInputToUtcIso, mirrored here)
      -> naive UTC stored in the DB        (db.to_naive_utc)
      -> explicit-'Z' ISO served back      (api.main._utc_iso)
      -> wall clock displayed in the tz    (formatInTimezone, mirrored here)
      -> aware instant handed to Celery    (run_scheduler's naive->utc attach)

A break anywhere in that chain shifts a post AND its pre-post engagement window by the user's
offset, so every hop is asserted for zero drift — including across both DST transitions.
"""

import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.unit

_POSTS = "cqc_lem.platform.db.repositories.posts"


# One zone per interesting offset shape: DST + negative, DST + positive, half-hour, no DST at all.
ZONES = ["America/New_York", "Europe/London", "Asia/Kolkata", "Australia/Sydney", "UTC"]

# Wall clocks the user might pick, including both US DST boundaries (spring-forward Mar 8 2026,
# fall-back Nov 1 2026) and the ambiguous hour right after fall-back.
WALL_CLOCKS = [
    dt.datetime(2026, 1, 15, 16, 0),   # winter
    dt.datetime(2026, 3, 7, 16, 0),    # day before spring-forward
    dt.datetime(2026, 3, 9, 16, 0),    # day after spring-forward
    dt.datetime(2026, 7, 4, 9, 30),    # summer
    dt.datetime(2026, 10, 31, 16, 0),  # day before fall-back
    dt.datetime(2026, 11, 1, 1, 30),   # ambiguous hour on fall-back day
    dt.datetime(2026, 11, 2, 16, 0),   # day after fall-back
]


def _spa_submits(wall_clock: dt.datetime, tz: str) -> dt.datetime:
    """What the SPA sends: the picked wall clock read in the user's tz, as an aware UTC instant.
    Mirrors `zonedInputToUtcIso` in ui/src/utils/datetime.ts.
    """
    return wall_clock.replace(tzinfo=ZoneInfo(tz)).astimezone(dt.timezone.utc)


def _spa_displays(iso: str, tz: str) -> dt.datetime:
    """What the SPA renders: the served ISO string localized to the user's tz. Mirrors
    `formatInTimezone` / `toZonedInputValue`, including their defensive naive-means-UTC parse.
    """
    parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


class TestToNaiveUtc:
    def test_aware_is_converted_and_stripped(self):
        from cqc_lem.utilities.db import to_naive_utc
        aware = dt.datetime(2026, 7, 4, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        assert to_naive_utc(aware) == dt.datetime(2026, 7, 4, 18, 0)

    def test_naive_is_assumed_utc_and_left_alone(self):
        from cqc_lem.utilities.db import to_naive_utc
        naive = dt.datetime(2026, 7, 4, 18, 0)
        assert to_naive_utc(naive) == naive
        assert to_naive_utc(naive).tzinfo is None

    def test_none_passes_through(self):
        """update_newsletter_edition uses None to mean 'leave this column alone' (COALESCE)."""
        from cqc_lem.utilities.db import to_naive_utc
        assert to_naive_utc(None) is None

    def test_utc_aware_is_unchanged_apart_from_tzinfo(self):
        from cqc_lem.utilities.db import to_naive_utc
        aware = dt.datetime(2026, 7, 4, 18, 0, tzinfo=dt.timezone.utc)
        assert to_naive_utc(aware) == dt.datetime(2026, 7, 4, 18, 0)


class TestUtcIsoSerialization:
    def test_naive_gets_explicit_z(self):
        from cqc_lem.api.main import _utc_iso
        assert _utc_iso(dt.datetime(2026, 7, 4, 18, 0)) == "2026-07-04T18:00:00Z"

    def test_aware_is_converted_to_utc(self):
        from cqc_lem.api.main import _utc_iso
        aware = dt.datetime(2026, 7, 4, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _utc_iso(aware) == "2026-07-04T18:00:00Z"

    def test_none_passes_through(self):
        from cqc_lem.api.main import _utc_iso
        assert _utc_iso(None) is None


class TestRoundTrip:
    @pytest.mark.parametrize("tz", ZONES)
    @pytest.mark.parametrize("wall_clock", WALL_CLOCKS)
    def test_picked_time_survives_the_full_chain(self, tz, wall_clock):
        """UI tz -> stored UTC -> served ISO -> displayed tz, with zero drift."""
        from cqc_lem.api.main import _utc_iso
        from cqc_lem.utilities.db import to_naive_utc

        stored = to_naive_utc(_spa_submits(wall_clock, tz))
        assert stored.tzinfo is None, "scheduling columns hold NAIVE UTC"

        displayed = _spa_displays(_utc_iso(stored), tz)
        assert displayed == wall_clock

    @pytest.mark.parametrize("tz", ZONES)
    @pytest.mark.parametrize("wall_clock", WALL_CLOCKS)
    def test_executed_instant_matches_the_displayed_time(self, tz, wall_clock):
        """The scheduler re-attaches UTC to the naive column and uses that as the Celery eta
        (run_scheduler.auto_check_scheduled_posts). That instant must be the one the user saw.
        """
        from cqc_lem.utilities.db import to_naive_utc

        intended = _spa_submits(wall_clock, tz)
        stored = to_naive_utc(intended)
        eta = stored.replace(tzinfo=dt.timezone.utc)

        assert eta == intended
        assert eta.astimezone(ZoneInfo(tz)).replace(tzinfo=None) == wall_clock

    @pytest.mark.parametrize("tz", ZONES)
    def test_pre_post_engagement_window_tracks_the_same_instant(self, tz):
        """Feed commenting fires at eta - 15min and profile-viewer DMs at eta - 10min. Both are
        derived from the stored instant, so they must land 15/10 minutes before the DISPLAYED
        time — the whole point of the golden-hour window.
        """
        from cqc_lem.utilities.db import to_naive_utc

        wall_clock = dt.datetime(2026, 7, 4, 16, 0)
        eta = to_naive_utc(_spa_submits(wall_clock, tz)).replace(tzinfo=dt.timezone.utc)

        commenting_local = (eta - dt.timedelta(minutes=15)).astimezone(ZoneInfo(tz))
        dm_local = (eta - dt.timedelta(minutes=10)).astimezone(ZoneInfo(tz))

        assert commenting_local.replace(tzinfo=None) == dt.datetime(2026, 7, 4, 15, 45)
        assert dm_local.replace(tzinfo=None) == dt.datetime(2026, 7, 4, 15, 50)

    def test_dst_transition_shifts_the_utc_instant_not_the_wall_clock(self):
        """4pm New York is 20:00 UTC in winter and 20:00-1h in summer. The user always sees 4pm;
        the stored UTC is what moves. A fixed-offset conversion would get one of these wrong.
        """
        from cqc_lem.utilities.db import to_naive_utc

        winter = to_naive_utc(_spa_submits(dt.datetime(2026, 1, 15, 16, 0), "America/New_York"))
        summer = to_naive_utc(_spa_submits(dt.datetime(2026, 7, 15, 16, 0), "America/New_York"))

        assert winter.hour == 21  # EST, UTC-5
        assert summer.hour == 20  # EDT, UTC-4


class TestSchedulingWritesNormalizeToUtc:
    """mysql-connector formats a datetime from its wall-clock fields and DROPS tzinfo, so an aware
    non-UTC value reaches the column shifted by the sender's offset with no error raised. Every
    scheduling write must therefore convert BEFORE it hands the value to the cursor.
    """

    # 14:00 in New York on July 4 2026 (EDT, UTC-4) is 18:00 UTC.
    AWARE = dt.datetime(2026, 7, 4, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    EXPECTED = dt.datetime(2026, 7, 4, 18, 0)

    def test_insert_scheduled_dm(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_scheduled_dm
            insert_scheduled_dm(1, "https://x/in/jane", "hi", self.AWARE)
        stored = cur.execute.call_args[0][1][5]
        assert stored == self.EXPECTED and stored.tzinfo is None

    def test_update_scheduled_dm(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_scheduled_dm
            update_scheduled_dm(7, scheduled_time=self.AWARE)
        stored = cur.execute.call_args[0][1][0]
        assert stored == self.EXPECTED and stored.tzinfo is None

    def test_insert_post(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch(f"{_POSTS}.get_user_id", return_value=1):
            from cqc_lem.utilities.db import PostType, insert_post
            insert_post("a@b.co", "body", self.AWARE, PostType.TEXT)
        stored = cur.execute.call_args[0][1][1]
        assert stored == self.EXPECTED and stored.tzinfo is None

    def test_bulk_update_posts(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import bulk_update_posts
            bulk_update_posts([1, 2], scheduled_time=self.AWARE)
        stored = cur.execute.call_args[0][1][0]
        assert stored == self.EXPECTED and stored.tzinfo is None

    def test_create_newsletter_edition(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            create_newsletter_edition(1, "T", "S", "B", self.AWARE)
        stored = cur.execute.call_args[0][1][-1]
        assert stored == self.EXPECTED and stored.tzinfo is None

    def test_update_newsletter_edition_keeps_none_as_leave_alone(self, fake_cursor):
        """scheduled_for=None means COALESCE-to-existing, not 'clear the column'."""
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            update_newsletter_edition(3, 1, title="T")
        assert cur.execute.call_args[0][1][-3] is None


class TestRecommendPostTimesTimezone:
    """recommend_post_times buckets stored UTC times; get_post_time hands the result straight back
    to the scheduler as a LOCAL time. Bucketing in UTC would shift it by the offset a second time.
    """

    def _row(self, scheduled, reactions=10):
        return (scheduled, reactions, 0, 0, None, None, None, None, None, None)

    def test_buckets_use_the_users_wall_clock(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        # 20:00, 21:00, 22:00 UTC == 16:00, 17:00, 18:00 EDT on a Saturday in July.
        rows = [self._row(dt.datetime(2026, 7, 4, 20, 0), 100),
                self._row(dt.datetime(2026, 7, 4, 21, 0), 1),
                self._row(dt.datetime(2026, 7, 4, 22, 0), 1)]
        top = recommend_post_times(rows, top_n=1, min_posts=3, tz="America/New_York")[0]
        assert top["hour"] == 16
        assert top["weekday"] == "Saturday"

    def test_weekday_can_roll_back_a_day(self):
        """01:00 UTC Monday is 21:00 Sunday in New York — the weekday flips, not just the hour."""
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [self._row(dt.datetime(2026, 7, 6, 1, 0), 100),
                self._row(dt.datetime(2026, 7, 13, 1, 0), 100),
                self._row(dt.datetime(2026, 7, 20, 1, 0), 100)]
        top = recommend_post_times(rows, top_n=1, min_posts=3, tz="America/New_York")[0]
        assert top["weekday"] == "Sunday"
        assert top["hour"] == 21

    def test_defaults_to_utc_when_tz_omitted(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [self._row(dt.datetime(2026, 7, 4, 20, 0), 100),
                self._row(dt.datetime(2026, 7, 4, 21, 0), 1),
                self._row(dt.datetime(2026, 7, 4, 22, 0), 1)]
        assert recommend_post_times(rows, top_n=1, min_posts=3)[0]["hour"] == 20

    def test_unknown_tz_falls_back_to_utc_instead_of_raising(self):
        from cqc_lem.utilities.post_stats import recommend_post_times
        rows = [self._row(dt.datetime(2026, 7, 4, 20, 0), 100),
                self._row(dt.datetime(2026, 7, 4, 21, 0), 1),
                self._row(dt.datetime(2026, 7, 4, 22, 0), 1)]
        assert recommend_post_times(rows, top_n=1, min_posts=3, tz="Mars/Olympus")[0]["hour"] == 20

    def test_get_post_time_asks_for_the_users_zone(self):
        """The learned hour must come back as a LOCAL time, because plan_content_for_user converts
        it local->UTC before storing.
        """
        from cqc_lem.utilities.utils import get_post_time
        recs = [{"weekday_num": 5, "hour": 16, "sample": 3}]
        with patch("cqc_lem.utilities.db.get_post_engagement_rows", return_value=[object()]), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch("cqc_lem.utilities.post_stats.recommend_post_times", return_value=recs) as rec:
            result = get_post_time(dt.date(2026, 7, 4), user_id=1)
        assert result.hour == 16
        assert rec.call_args.kwargs["tz"] == "America/New_York"
