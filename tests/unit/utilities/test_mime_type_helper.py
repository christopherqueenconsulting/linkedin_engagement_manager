"""Unit tests for cqc_lem.utilities.mime_type_helper."""

import pytest

pytestmark = pytest.mark.unit


class TestChoosePreferredMime:
    def test_octet_stream_wins_over_zip(self):
        from cqc_lem.utilities.mime_type_helper import choose_preferred_mime
        result = choose_preferred_mime(["application/octet-stream", "application/zip"])
        assert result == "application/octet-stream"

    def test_zip_returned_when_no_octet_stream(self):
        from cqc_lem.utilities.mime_type_helper import choose_preferred_mime
        result = choose_preferred_mime(["application/zip", "other/type"])
        assert result == "application/zip"

    def test_first_item_returned_when_no_priority_match(self):
        from cqc_lem.utilities.mime_type_helper import choose_preferred_mime
        result = choose_preferred_mime(["image/jpeg", "image/pjpeg"])
        assert result == "image/jpeg"

    def test_single_item_list_returns_that_item(self):
        from cqc_lem.utilities.mime_type_helper import choose_preferred_mime
        assert choose_preferred_mime(["text/plain"]) == "text/plain"


class TestGetFileMimeType:
    def test_jpg_with_leading_dot(self):
        from cqc_lem.utilities.mime_type_helper import get_file_mime_type
        result = get_file_mime_type(".jpg")
        # Multiple MIME types exist for .jpg; we accept either common variant
        assert result in ("image/jpeg", "image/pjpeg", "application/octet-stream")

    def test_mp4_without_leading_dot(self):
        from cqc_lem.utilities.mime_type_helper import get_file_mime_type
        result = get_file_mime_type("mp4")
        assert result == "video/mp4"

    def test_mp4_with_leading_dot(self):
        from cqc_lem.utilities.mime_type_helper import get_file_mime_type
        result = get_file_mime_type(".mp4")
        assert result == "video/mp4"

    def test_zip_returns_acceptable_mime(self):
        from cqc_lem.utilities.mime_type_helper import get_file_mime_type
        result = get_file_mime_type(".zip")
        # Multiple MIME types exist; octet-stream wins priority, but zip variants are valid
        assert "zip" in result or result == "application/octet-stream"

    def test_unknown_extension_returns_octet_stream(self):
        from cqc_lem.utilities.mime_type_helper import get_file_mime_type
        result = get_file_mime_type(".unknown_xyz_ext_abc")
        assert result == "application/octet-stream"

    def test_png_returns_image_png(self):
        from cqc_lem.utilities.mime_type_helper import get_file_mime_type
        result = get_file_mime_type(".png")
        assert result == "image/png"

    def test_malformed_table_line_logs_instead_of_printing(self, monkeypatch):
        """The parse error path used to `print()`; it goes through the structured logger now."""
        from unittest.mock import patch

        from cqc_lem.utilities import mime_type_helper

        monkeypatch.setattr(mime_type_helper, "mime_types_str", ".png image/png\nnot-a-pair-line x y")
        with patch.object(mime_type_helper, "log_warning") as mock_warn:
            assert mime_type_helper.get_file_mime_type(".png") == "image/png"
        mock_warn.assert_called_once()
        assert mock_warn.call_args.kwargs["mime_line"] == "not-a-pair-line x y"

    def test_table_has_no_malformed_lines(self):
        """Every row is `<ext> <mime>`, so the warning above is a real defect signal, not noise."""
        from cqc_lem.utilities.mime_type_helper import mime_types_str
        assert [ln for ln in mime_types_str.strip().split("\n") if len(ln.split()) != 2] == []
