"""Unit tests for best-effort C2PA provenance helper."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestFormatFor:
    def test_known_and_unknown(self):
        from cqc_lem.utilities.c2pa_helper import _format_for
        assert _format_for("a.png") == "image/png"
        assert _format_for("a.MP4") == "video/mp4"
        assert _format_for("a.txt") is None


class TestAddCredentials:
    def test_disabled_is_noop(self):
        with patch("cqc_lem.utilities.c2pa_helper.C2PA_ENABLED", False):
            from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
            assert add_ai_content_credentials("/tmp/whatever.png") is False

    def test_missing_cert_returns_false(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        with patch("cqc_lem.utilities.c2pa_helper.C2PA_ENABLED", True), \
             patch("cqc_lem.utilities.c2pa_helper.C2PA_CERT_PATH", ""), \
             patch("cqc_lem.utilities.c2pa_helper.C2PA_KEY_PATH", ""):
            from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
            assert add_ai_content_credentials(str(f)) is False

    def test_sign_failure_swallowed(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        cert = tmp_path / "c.pem"
        cert.write_text("not a real cert")
        key = tmp_path / "k.pem"
        key.write_text("not a real key")
        with patch("cqc_lem.utilities.c2pa_helper.C2PA_ENABLED", True), \
             patch("cqc_lem.utilities.c2pa_helper.C2PA_CERT_PATH", str(cert)), \
             patch("cqc_lem.utilities.c2pa_helper.C2PA_KEY_PATH", str(key)):
            from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
            # Garbage cert/key (or missing c2pa) -> internal error -> swallowed -> False
            assert add_ai_content_credentials(str(f)) is False

    def test_successful_sign_signs_in_place(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"original")
        cert = tmp_path / "c.pem"
        cert.write_text("cert")
        key = tmp_path / "k.pem"
        key.write_text("key")

        def _cm(value):
            cm = MagicMock()
            cm.__enter__.return_value = value
            cm.__exit__.return_value = False
            return cm

        fake_c2pa = MagicMock()
        fake_c2pa.Signer.from_callback.return_value = _cm(MagicMock())
        builder = MagicMock()
        def _sign_file(src, out, signer):
            with open(out, "wb") as fh:
                fh.write(b"signed")
        builder.sign_file.side_effect = _sign_file
        fake_c2pa.Builder.return_value = _cm(builder)

        with patch("cqc_lem.utilities.c2pa_helper.C2PA_ENABLED", True), \
             patch("cqc_lem.utilities.c2pa_helper.C2PA_CERT_PATH", str(cert)), \
             patch("cqc_lem.utilities.c2pa_helper.C2PA_KEY_PATH", str(key)), \
             patch.dict("sys.modules", {"c2pa": fake_c2pa}):
            from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
            assert add_ai_content_credentials(str(f)) is True
        assert f.read_bytes() == b"signed"  # signed temp replaced the original in place
