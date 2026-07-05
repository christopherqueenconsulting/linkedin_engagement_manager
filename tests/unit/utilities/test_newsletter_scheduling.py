"""Unit tests for newsletter scheduling math."""

from datetime import datetime

import pytest
import pytz

pytestmark = pytest.mark.unit

_ET = pytz.timezone("America/New_York")  # July = EDT (UTC-4), so 9am local = 13:00 UTC


class TestNextPublishDatetime:
    def test_never_published_finds_next_tuesday_9am_local(self):
        from cqc_lem.utilities.newsletter import next_publish_datetime
        now = datetime(2026, 7, 5, 12, 0)  # Sunday, UTC
        nxt = next_publish_datetime(1, 9, "weekly", None, _ET, now)  # Tue 9am
        assert nxt == datetime(2026, 7, 7, 13, 0)  # Tue 2026-07-07 09:00 EDT == 13:00 UTC

    def test_respects_weekly_interval_after_last_edition(self):
        from cqc_lem.utilities.newsletter import next_publish_datetime
        last = datetime(2026, 7, 7, 13, 0)  # published Tue
        now = datetime(2026, 7, 8, 12, 0)
        nxt = next_publish_datetime(1, 9, "weekly", last, _ET, now)
        assert nxt == datetime(2026, 7, 14, 13, 0)  # the FOLLOWING Tuesday, not tomorrow

    def test_slot_already_passed_today_rolls_to_next_week(self):
        from cqc_lem.utilities.newsletter import next_publish_datetime
        now = datetime(2026, 7, 7, 15, 0)  # Tuesday 15:00 UTC — past the 13:00 slot
        nxt = next_publish_datetime(1, 9, "weekly", None, _ET, now)
        assert nxt == datetime(2026, 7, 14, 13, 0)

    def test_biweekly_interval(self):
        from cqc_lem.utilities.newsletter import next_publish_datetime
        last = datetime(2026, 7, 7, 13, 0)
        now = datetime(2026, 7, 8, 12, 0)
        nxt = next_publish_datetime(1, 9, "biweekly", last, _ET, now)
        assert nxt == datetime(2026, 7, 21, 13, 0)  # +14 days → Tue 2026-07-21


class TestShouldGenerateNow:
    def test_within_lead_window(self):
        from cqc_lem.utilities.newsletter import should_generate_now
        nxt = datetime(2026, 7, 14, 13, 0)
        assert should_generate_now(nxt, datetime(2026, 7, 12, 12, 0)) is True   # 2 days out
        assert should_generate_now(nxt, datetime(2026, 7, 10, 12, 0)) is False  # 4 days out
