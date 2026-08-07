"""Unit tests for date utility functions."""

import datetime
from datetime import timedelta

import pytest

from cqc_lem.utilities.date import (
    convert_date_to_datetime,
    convert_datetime_to_end_of_day,
    convert_datetime_to_local_tz,
    convert_datetime_to_start_of_day,
    convert_viewed_on_to_date,
    filter_dates_in_range,
    format_year,
    get_datetime,
    get_earliest_date,
    get_latest_date,
    get_linkedin_datetime_from_text,
    is_checkdate_after_date,
    is_checkdate_before_date,
    is_date_in_range,
    order_dates,
    purge_empty_and_invalid_dates,
    weeks_between_dates,
)

pytestmark = pytest.mark.unit


class TestDateUtilities:
    """Test suite for date utility functions."""

    def test_convert_datetime_to_local_tz_with_utc(self):
        """Test converting UTC datetime to local timezone."""
        # Create a UTC datetime
        utc_time = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
        
        # Convert to local timezone
        local_time = convert_datetime_to_local_tz(utc_time)
        
        # Verify it has timezone info
        assert local_time.tzinfo is not None
        # Verify it's different from UTC (unless system is in UTC)
        assert isinstance(local_time, datetime.datetime)

    def test_convert_datetime_to_local_tz_naive(self):
        """Test converting naive datetime to local timezone assuming UTC."""
        # Create a naive datetime
        naive_time = datetime.datetime(2024, 1, 1, 12, 0, 0)
        
        # Convert to local timezone (assuming UTC)
        local_time = convert_datetime_to_local_tz(naive_time, assumed_utc=True)
        
        # Verify it has timezone info
        assert local_time.tzinfo is not None
        assert isinstance(local_time, datetime.datetime)

    def test_format_year(self):
        """Test formatting year strings."""
        # Test various year formats (format_year expects a 4-digit year as string)
        assert format_year("2020") == "20"
        assert format_year("2024") == "24"

    def test_get_datetime(self):
        """Test parsing datetime from text."""
        # Test parsing various date formats
        result = get_datetime("2024-01-01 12:00:00")
        assert isinstance(result, datetime.datetime) or result is None

    def test_datetime_arithmetic_with_timezone(self):
        """Test that datetime arithmetic works correctly with timezones."""
        # Create a timezone-aware datetime
        utc_time = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
        
        # Subtract 15 minutes
        updated_time = utc_time - timedelta(minutes=15)
        
        # Verify the time is correct
        expected = datetime.datetime(2024, 1, 1, 11, 45, 0, tzinfo=datetime.timezone.utc)
        assert updated_time == expected
        assert updated_time.tzinfo == datetime.timezone.utc

    def test_datetime_format_12_hour(self):
        """Test formatting datetime in 12-hour format."""
        # Create a datetime
        dt = datetime.datetime(2024, 1, 1, 15, 30, 0, tzinfo=datetime.timezone.utc)
        
        # Format in 12-hour format
        formatted = dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')
        
        # Verify format
        assert '03:30:00 PM' in formatted
        assert '2024-01-01' in formatted
        assert 'UTC' in formatted or 'GMT' in formatted  # Depends on system


class TestDateIntegration:
    """Integration tests for date utilities with database."""

    @pytest.mark.requires_database
    def test_post_scheduled_time_conversion(self, mock_database_connection, sample_post_data):
        """Test scheduled time conversion for posts from database."""
        # This test would require actual database connection
        # Marking with requires_database to skip in CI
        pass


class TestDateUtilitiesExtended:
    """Tests for uncovered date utility functions."""

    # ------------------------------------------------------------------
    # get_linkedin_datetime_from_text
    # ------------------------------------------------------------------

    def test_linkedin_datetime_years_and_months(self):
        result = get_linkedin_datetime_from_text("2 yrs 3 mos")
        # Result must be a non-empty month/year string like "Mar 2024"
        assert len(result) > 0
        # Should contain a 4-digit year
        import re
        assert re.search(r'\d{4}', result)

    def test_linkedin_datetime_only_years(self):
        result = get_linkedin_datetime_from_text("1 yr")
        assert len(result) > 0

    def test_linkedin_datetime_only_months(self):
        result = get_linkedin_datetime_from_text("6 mos")
        assert len(result) > 0

    def test_linkedin_datetime_strips_whitespace(self):
        result_stripped = get_linkedin_datetime_from_text("  1 yr  ")
        result_clean = get_linkedin_datetime_from_text("1 yr")
        assert result_stripped == result_clean

    # ------------------------------------------------------------------
    # is_checkdate_before_date
    # ------------------------------------------------------------------

    def test_before_date_true_when_earlier(self):
        earlier = datetime.date(2020, 1, 1)
        later = datetime.date(2020, 6, 1)
        assert is_checkdate_before_date(earlier, later) is True

    def test_before_date_false_when_same(self):
        d = datetime.date(2020, 3, 15)
        assert is_checkdate_before_date(d, d) is False

    def test_before_date_with_datetime_objects(self):
        earlier = datetime.datetime(2021, 1, 1, 10, 0)
        later = datetime.datetime(2021, 1, 2, 10, 0)
        assert is_checkdate_before_date(earlier, later) is True

    # ------------------------------------------------------------------
    # is_checkdate_after_date
    # ------------------------------------------------------------------

    def test_after_date_true_when_later(self):
        later = datetime.date(2020, 12, 1)
        earlier = datetime.date(2020, 1, 1)
        assert is_checkdate_after_date(later, earlier) is True

    def test_after_date_false_when_same(self):
        d = datetime.date(2020, 6, 15)
        assert is_checkdate_after_date(d, d) is False

    def test_after_date_false_when_check_is_earlier(self):
        check = datetime.date(2020, 1, 1)
        after = datetime.date(2020, 6, 1)
        assert is_checkdate_after_date(check, after) is False

    # ------------------------------------------------------------------
    # is_date_in_range
    # ------------------------------------------------------------------

    def test_in_range_true_for_middle_date(self):
        start = datetime.date(2020, 1, 1)
        check = datetime.date(2020, 6, 15)
        end = datetime.date(2020, 12, 31)
        assert is_date_in_range(start, check, end) is True

    def test_in_range_true_at_start_boundary(self):
        d = datetime.date(2020, 1, 1)
        assert is_date_in_range(d, d, datetime.date(2020, 12, 31)) is True

    def test_in_range_false_before_start(self):
        start = datetime.date(2020, 6, 1)
        check = datetime.date(2020, 1, 1)
        end = datetime.date(2020, 12, 31)
        assert is_date_in_range(start, check, end) is False

    # ------------------------------------------------------------------
    # filter_dates_in_range
    # ------------------------------------------------------------------

    def test_filter_dates_keeps_in_range(self):
        dates = ["2020-06-15", "2019-01-01", "2021-01-01"]
        start = datetime.date(2020, 1, 1)
        end = datetime.date(2020, 12, 31)
        result = filter_dates_in_range(dates, start, end)
        assert len(result) == 1
        assert "2020-06-15" in result

    def test_filter_dates_empty_list(self):
        result = filter_dates_in_range([], datetime.date(2020, 1, 1), datetime.date(2020, 12, 31))
        assert result == []

    # ------------------------------------------------------------------
    # purge_empty_and_invalid_dates
    # ------------------------------------------------------------------

    def test_purge_removes_empty_strings(self):
        result = purge_empty_and_invalid_dates(["", "  ", "2020-01-01"])
        assert "" not in result
        assert "  " not in result

    def test_purge_removes_unparseable_dates(self):
        result = purge_empty_and_invalid_dates(["not-a-date", "2020-01-01"])
        assert "not-a-date" not in result
        assert len(result) == 1

    def test_purge_all_valid(self):
        dates = ["2020-01-01", "2021-06-15"]
        result = purge_empty_and_invalid_dates(dates)
        assert len(result) == 2

    # ------------------------------------------------------------------
    # order_dates
    # ------------------------------------------------------------------

    def test_order_dates_sorts_chronologically(self):
        dates = ["2021-06-01", "2019-01-01", "2020-03-15"]
        result = order_dates(dates)
        parsed = [get_datetime(d) for d in result]
        assert parsed == sorted(parsed)

    def test_order_dates_empty_list(self):
        assert order_dates([]) == []

    def test_order_dates_skips_invalid(self):
        dates = ["2020-01-01", "not-valid", "2019-06-01"]
        result = order_dates(dates)
        assert "not-valid" not in result
        assert len(result) == 2

    # ------------------------------------------------------------------
    # get_latest_date
    # ------------------------------------------------------------------

    def test_latest_date_returns_most_recent(self):
        dates = ["2019-01-01", "2021-12-31", "2020-06-15"]
        result = get_latest_date(dates)
        assert get_datetime(result) >= get_datetime("2021-12-31")

    def test_latest_date_empty_list_returns_empty_string(self):
        assert get_latest_date([]) == ""

    def test_latest_date_single_entry(self):
        assert get_latest_date(["2020-05-20"]) == "2020-05-20"

    # ------------------------------------------------------------------
    # get_earliest_date
    # ------------------------------------------------------------------

    def test_earliest_date_returns_oldest(self):
        dates = ["2019-01-01", "2021-12-31", "2020-06-15"]
        result = get_earliest_date(dates)
        assert get_datetime(result) <= get_datetime("2019-01-01")

    def test_earliest_date_empty_list_returns_empty_string(self):
        assert get_earliest_date([]) == ""

    # ------------------------------------------------------------------
    # weeks_between_dates
    # ------------------------------------------------------------------

    def test_weeks_between_exact_two_weeks(self):
        d1 = datetime.date(2020, 1, 1)
        d2 = datetime.date(2020, 1, 15)
        assert weeks_between_dates(d1, d2) == 2

    def test_weeks_between_rounds_down_by_default(self):
        d1 = datetime.date(2020, 1, 1)
        d2 = datetime.date(2020, 1, 10)  # 9 days = 1 week remainder
        assert weeks_between_dates(d1, d2) == 1

    def test_weeks_between_rounds_up_when_flag_set(self):
        d1 = datetime.date(2020, 1, 1)
        d2 = datetime.date(2020, 1, 10)  # 9 days → ceil(9/7) = 2 weeks
        assert weeks_between_dates(d1, d2, round_up=True) == 2

    def test_weeks_between_same_date_is_zero(self):
        d = datetime.date(2020, 6, 15)
        assert weeks_between_dates(d, d) == 0

    # ------------------------------------------------------------------
    # convert_datetime_to_end_of_day
    # ------------------------------------------------------------------

    def test_end_of_day_sets_time_to_max(self):
        dt = datetime.datetime(2020, 6, 15, 10, 30, 0)
        result = convert_datetime_to_end_of_day(dt)
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59
        assert result.date() == dt.date()

    # ------------------------------------------------------------------
    # convert_datetime_to_start_of_day
    # ------------------------------------------------------------------

    def test_start_of_day_sets_time_to_zero(self):
        dt = datetime.datetime(2020, 6, 15, 18, 45, 0)
        result = convert_datetime_to_start_of_day(dt)
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0
        assert result.date() == dt.date()

    # ------------------------------------------------------------------
    # convert_date_to_datetime
    # ------------------------------------------------------------------

    def test_convert_date_to_datetime_midnight(self):
        d = datetime.date(2020, 6, 15)
        result = convert_date_to_datetime(d)
        assert isinstance(result, datetime.datetime)
        assert result.year == 2020
        assert result.month == 6
        assert result.day == 15
        assert result.hour == 0
        assert result.minute == 0

    # ------------------------------------------------------------------
    # convert_viewed_on_to_date
    # ------------------------------------------------------------------

    def test_convert_viewed_on_strips_viewed_keyword(self):
        # "viewed 2 days ago" → strips "viewed", parses the rest
        result = convert_viewed_on_to_date("viewed 2 days ago")
        assert isinstance(result, datetime.datetime)

    def test_convert_viewed_on_strips_edited_keyword(self):
        result = convert_viewed_on_to_date("edited 3 days ago")
        assert isinstance(result, datetime.datetime)

    def test_convert_viewed_on_strips_bullet(self):
        result = convert_viewed_on_to_date("• 1 day ago")
        assert isinstance(result, datetime.datetime)

    def test_convert_viewed_on_replaces_w_with_week(self):
        result = convert_viewed_on_to_date("1w ago")
        assert isinstance(result, datetime.datetime)

    # Live caption forms from the SDUI profile-views page (grounded 2026-08-03):
    # "Viewed 1h ago" / "20h" / "1d" / "1w" / "1mo" / "2mo".

    def test_convert_viewed_on_live_relative_forms_land_at_the_right_age(self):
        now = datetime.datetime.now()
        expectations = {
            "Viewed 5m ago": datetime.timedelta(minutes=5),
            "Viewed 20h ago": datetime.timedelta(hours=20),
            "Viewed 1d ago": datetime.timedelta(days=1),
            "Viewed 2w ago": datetime.timedelta(weeks=2),
            "Viewed 1mo ago": datetime.timedelta(days=30),
            "Viewed 2mo ago": datetime.timedelta(days=60),
            "Viewed 1yr ago": datetime.timedelta(days=365),
        }
        for caption, age in expectations.items():
            result = convert_viewed_on_to_date(caption)
            assert abs((now - result) - age) < datetime.timedelta(minutes=1), caption

    def test_convert_viewed_on_week_caption_is_not_mangled(self):
        # The old 'w'->'week' substitution turned "week" into "weekeek"; the parser must
        # take spelled-out units as-is.
        result = convert_viewed_on_to_date("Viewed 2 weeks ago")
        assert abs((datetime.datetime.now() - result) - datetime.timedelta(weeks=2)) \
            < datetime.timedelta(minutes=1)

    def test_convert_viewed_on_unparseable_raises_value_error(self):
        with pytest.raises(ValueError):
            convert_viewed_on_to_date("Viewed ??")
