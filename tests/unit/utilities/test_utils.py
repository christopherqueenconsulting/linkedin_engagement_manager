"""Unit tests for cqc_lem.utilities.utils (non-AWS functions)."""

import datetime
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class TestGetTopLevelDomain:
    def test_standard_url(self):
        from cqc_lem.utilities.utils import get_top_level_domain
        assert get_top_level_domain("https://www.example.com/path?q=1") == "example.com"

    def test_subdomain_stripped(self):
        from cqc_lem.utilities.utils import get_top_level_domain
        assert get_top_level_domain("https://blog.linkedin.com/articles") == "linkedin.com"

    def test_multi_part_tld(self):
        from cqc_lem.utilities.utils import get_top_level_domain
        result = get_top_level_domain("https://www.bbc.co.uk/news")
        assert result == "bbc.co.uk"

    def test_no_www(self):
        from cqc_lem.utilities.utils import get_top_level_domain
        assert get_top_level_domain("https://github.com/user/repo") == "github.com"


class TestGetBestPostingTimes:
    def test_returns_dict_with_seven_keys(self):
        from cqc_lem.utilities.utils import get_best_posting_times
        times = get_best_posting_times()
        assert set(times.keys()) == {0, 1, 2, 3, 4, 5, 6}

    def test_all_values_are_time_objects(self):
        from cqc_lem.utilities.utils import get_best_posting_times
        for t in get_best_posting_times().values():
            assert isinstance(t, datetime.time)

    def test_monday_is_16_00(self):
        from cqc_lem.utilities.utils import get_best_posting_times
        assert get_best_posting_times()[0] == datetime.time(16, 0)  # 2026 afternoon-peak model


class TestGetBestPostingTime:
    def test_monday_returns_16_00(self):
        from cqc_lem.utilities.utils import get_best_posting_time
        monday = datetime.date(2024, 1, 1)  # weekday() == 0
        assert get_best_posting_time(monday) == datetime.time(16, 0)

    def test_wednesday_returns_16_00(self):
        from cqc_lem.utilities.utils import get_best_posting_time
        wednesday = datetime.date(2024, 1, 3)  # weekday() == 2 (peak day)
        assert get_best_posting_time(wednesday) == datetime.time(16, 0)

    def test_all_weekdays_return_time(self):
        from cqc_lem.utilities.utils import get_best_posting_time
        # 7-day week starting from Monday 2024-01-01
        for i in range(7):
            d = datetime.date(2024, 1, 1) + datetime.timedelta(days=i)
            result = get_best_posting_time(d)
            assert isinstance(result, datetime.time)


class TestGet12hFormatBestTime:
    def test_afternoon_time_shows_pm(self):
        from cqc_lem.utilities.utils import get_12h_format_best_time
        t = datetime.time(14, 0)
        assert get_12h_format_best_time(t) == "02:00 PM"

    def test_morning_time_shows_am(self):
        from cqc_lem.utilities.utils import get_12h_format_best_time
        t = datetime.time(9, 0)
        assert get_12h_format_best_time(t) == "09:00 AM"

    def test_midnight_shows_12_am(self):
        from cqc_lem.utilities.utils import get_12h_format_best_time
        t = datetime.time(0, 0)
        assert get_12h_format_best_time(t) == "12:00 AM"

    def test_noon_shows_12_pm(self):
        from cqc_lem.utilities.utils import get_12h_format_best_time
        t = datetime.time(12, 0)
        assert get_12h_format_best_time(t) == "12:00 PM"


class TestGetFileExtensionFromFilepath:
    def test_returns_lowercase_extension(self):
        from cqc_lem.utilities.utils import get_file_extension_from_filepath
        assert get_file_extension_from_filepath("/some/path/video.MP4") == ".mp4"

    def test_removes_leading_dot_when_requested(self):
        from cqc_lem.utilities.utils import get_file_extension_from_filepath
        assert get_file_extension_from_filepath("/path/to/file.jpg", remove_leading_dot=True) == "jpg"

    def test_keeps_leading_dot_by_default(self):
        from cqc_lem.utilities.utils import get_file_extension_from_filepath
        assert get_file_extension_from_filepath("/path/to/file.png") == ".png"

    def test_no_extension_returns_empty_string(self):
        from cqc_lem.utilities.utils import get_file_extension_from_filepath
        assert get_file_extension_from_filepath("/path/to/Makefile") == ""

    def test_nested_path_uses_basename_only(self):
        from cqc_lem.utilities.utils import get_file_extension_from_filepath
        assert get_file_extension_from_filepath("/a/b.c/file.csv") == ".csv"


class TestCreateFolderIfNotExists:
    def test_creates_folder_when_not_exists(self, tmp_path):
        from cqc_lem.utilities.utils import create_folder_if_not_exists
        new_dir = str(tmp_path / "new_subfolder")
        with patch("os.path.exists", return_value=False), \
             patch("os.makedirs") as mock_makedirs:
            create_folder_if_not_exists(new_dir)
        mock_makedirs.assert_called_once_with(new_dir)

    def test_does_not_create_when_already_exists(self, tmp_path):
        from cqc_lem.utilities.utils import create_folder_if_not_exists
        existing = str(tmp_path)
        with patch("os.makedirs") as mock_makedirs:
            create_folder_if_not_exists(existing)
        mock_makedirs.assert_not_called()
