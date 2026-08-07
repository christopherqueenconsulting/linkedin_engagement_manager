"""Unit tests for LinkedIn scraper pure functions (no Selenium required)."""

import pytest
from unittest.mock import MagicMock


@pytest.mark.unit
class TestSourceAsRow:
    def test_splits_text_on_newlines(self):
        from cqc_lem.utilities.linkedin.scrapper import source_as_row

        element = MagicMock()
        element.getText.return_value = "Line 1\nLine 2\nLine 3"

        result = source_as_row(element)

        assert result == ["Line 1", "Line 2", "Line 3"]

    def test_returns_single_item_list_for_no_newlines(self):
        from cqc_lem.utilities.linkedin.scrapper import source_as_row

        element = MagicMock()
        element.getText.return_value = "Single line"

        result = source_as_row(element)

        assert result == ["Single line"]

    def test_returns_empty_strings_for_multiple_newlines(self):
        from cqc_lem.utilities.linkedin.scrapper import source_as_row

        element = MagicMock()
        element.getText.return_value = "\n\nText\n"

        result = source_as_row(element)

        assert "" in result
        assert "Text" in result


@pytest.mark.unit
class TestGetStartIdentifier:
    def test_returns_negative_one_for_non_empty_list(self):
        from cqc_lem.utilities.linkedin.scrapper import get_start_identifier

        result = get_start_identifier(["Apple", "Banana"])

        assert result == -1

    def test_counts_leading_empty_strings(self):
        from cqc_lem.utilities.linkedin.scrapper import get_start_identifier

        result = get_start_identifier(["", "", "Content"])

        assert result == 1

    def test_counts_leading_spaces(self):
        from cqc_lem.utilities.linkedin.scrapper import get_start_identifier

        result = get_start_identifier([" ", " ", "Data"])

        assert result == 1

    def test_empty_list_returns_negative_one(self):
        from cqc_lem.utilities.linkedin.scrapper import get_start_identifier

        result = get_start_identifier([])

        assert result == -1

    def test_all_empty_strings_counts_all(self):
        from cqc_lem.utilities.linkedin.scrapper import get_start_identifier

        result = get_start_identifier(["", "", ""])

        assert result == 2
