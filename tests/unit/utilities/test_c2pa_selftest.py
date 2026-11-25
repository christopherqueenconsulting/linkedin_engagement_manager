"""C2PA signing readiness (#2794): cert generator, self-test, shared signer, census read."""
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "c2pa_make_cert.sh"
HELPER = "cqc_lem.utilities.c2pa_helper"

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _make_chain(out_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), str(out_dir), *extra],
                          capture_output=True, text=True, check=False)


@pytest.fixture
def chain(tmp_path):
    out = tmp_path / "c2pa"
    result = _make_chain(out)
    assert result.returncode == 0, result.stderr
    return out / "chain.pem", out / "signer.key"


@contextmanager
def _configured(cert: Path, key: Path):
    with patch(f"{HELPER}.C2PA_CERT_PATH", str(cert)), patch(f"{HELPER}.C2PA_KEY_PATH", str(key)):
        yield


def _run_selftest(capsys) -> tuple[int, str]:
    from cqc_lem.utilities import c2pa_selftest
    with patch("cqc_lem.utilities.c2pa_selftest._census_line", return_value="census"):
        code = c2pa_selftest.run_selftest()
    return code, capsys.readouterr().out


@needs_openssl
class TestCertScript:
    def test_writes_chain_and_private_key(self, chain):
        cert, key = chain
        assert oct(key.stat().st_mode & 0o777) == "0o600"
        assert cert.read_text().count("BEGIN CERTIFICATE") == 2  # leaf, then root
        assert "PRIVATE KEY" in key.read_text()

    def test_leaf_meets_the_profile(self, chain):
        from cqc_lem.utilities.c2pa_selftest import _profile_problems
        assert _profile_problems(str(chain[0])) == []

    def test_refuses_to_overwrite_and_leaves_files_alone(self, chain):
        cert, key = chain
        before = (cert.read_bytes(), key.read_bytes())
        result = _make_chain(cert.parent)
        assert result.returncode != 0
        assert "refusing to overwrite" in result.stderr
        assert (cert.read_bytes(), key.read_bytes()) == before

    def test_prints_no_key_material_and_leaves_no_scratch_files(self, tmp_path):
        out = tmp_path / "c2pa"
        result = _make_chain(out)
        body = [ln for ln in (out / "signer.key").read_text().splitlines() if not ln.startswith("-----")]
        assert not any(ln in result.stdout + result.stderr for ln in body)
        assert sorted(p.name for p in out.iterdir()) == ["chain.pem", "signer.key"]

    def test_without_owner_uid_prints_the_chown_line(self, tmp_path):
        result = _make_chain(tmp_path / "c2pa")
        assert "sudo chown <uid>" in result.stdout
        assert "id -u" in result.stdout

    def test_bad_owner_uid_is_rejected_before_writing(self, tmp_path):
        out = tmp_path / "c2pa"
        result = _make_chain(out, "--owner-uid", "root; id")
        assert result.returncode == 2
        assert not out.exists()


@needs_openssl
class TestSharedSigner:
    def test_generated_cert_signs_a_png_through_add_ai_content_credentials(self, chain, tmp_path):
        c2pa = pytest.importorskip("c2pa")
        from PIL import Image

        from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
        from cqc_lem.utilities.c2pa_selftest import _source_type_and_status
        png = tmp_path / "a.png"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(png)
        with _configured(*chain), patch(f"{HELPER}.C2PA_ENABLED", True):
            assert add_ai_content_credentials(str(png)) is True
        import json
        source, _ = _source_type_and_status(json.loads(c2pa.Reader(str(png)).json()))
        assert source.endswith("trainedAlgorithmicMedia")

    def test_bypass_parameter_signs_while_the_flag_is_off(self, chain, tmp_path):
        pytest.importorskip("c2pa")
        from PIL import Image

        from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
        png = tmp_path / "a.png"
        Image.new("RGB", (32, 32)).save(png)
        with _configured(*chain), patch(f"{HELPER}.C2PA_ENABLED", False):
            assert add_ai_content_credentials(str(png)) is False
            assert add_ai_content_credentials(str(png), require_enabled=False) is True


@needs_openssl
class TestSelftest:
    @needs_ffmpeg
    def test_good_chain_passes_every_format_and_prints_no_key(self, chain, capsys):
        pytest.importorskip("c2pa")
        with _configured(*chain):
            code, out = _run_selftest(capsys)
        assert code == 0, out
        for fmt in ("png", "jpg", "webp", "mp4"):
            assert f"ok    {fmt}" in out
        assert "RESULT: PASS" in out
        body = [ln for ln in chain[1].read_text().splitlines() if not ln.startswith("-----")]
        assert not any(ln in out for ln in body)

    def test_missing_cert_fails(self, chain, capsys):
        cert, key = chain
        cert.unlink()
        with _configured(cert, key):
            code, out = _run_selftest(capsys)
        assert code == 1
        assert "cert missing" in out

    def test_missing_key_fails(self, chain, capsys):
        cert, key = chain
        key.unlink()
        with _configured(cert, key):
            code, out = _run_selftest(capsys)
        assert code == 1
        assert "key missing" in out

    def test_unset_paths_fail(self, capsys):
        with patch(f"{HELPER}.C2PA_CERT_PATH", ""), patch(f"{HELPER}.C2PA_KEY_PATH", ""):
            code, out = _run_selftest(capsys)
        assert code == 1
        assert "not configured" in out

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads mode 000")
    def test_unreadable_key_is_named_with_the_uid(self, chain, capsys):
        cert, key = chain
        key.chmod(0o000)
        try:
            with _configured(cert, key):
                code, out = _run_selftest(capsys)
        finally:
            key.chmod(0o600)
        assert code == 1
        assert f"key unreadable by uid {os.getuid()}" in out

    def test_ca_certificate_fails_the_profile(self, tmp_path, capsys):
        cert, key = tmp_path / "ca.pem", tmp_path / "ca.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=bare",
             "-addext", "basicConstraints=critical,CA:TRUE"],
            capture_output=True, check=True)
        with _configured(cert, key):
            code, out = _run_selftest(capsys)
        assert code == 1
        assert "C2PA profile" in out
        assert "CA:TRUE" in out


class TestFormatCheck:
    def test_signing_error_is_reported_and_key_lines_are_scrubbed(self, tmp_path):
        from cqc_lem.utilities import c2pa_selftest
        key_bytes = b"-----BEGIN PRIVATE KEY-----\nSECRETKEYBODYLINE1234\n-----END PRIVATE KEY-----\n"
        boom = RuntimeError("signer exploded near SECRETKEYBODYLINE1234")
        with patch("cqc_lem.utilities.c2pa_selftest.sign_file_in_place", side_effect=boom):
            row = c2pa_selftest.check_format("png", str(tmp_path), "c", "k", key_bytes)
        assert row["ok"] is False and row["signed"] is False
        assert "SECRETKEYBODYLINE1234" not in row["problem"]
        assert "[redacted]" in row["problem"]

    def _row(self, tmp_path, store):
        from cqc_lem.utilities import c2pa_selftest
        with patch("cqc_lem.utilities.c2pa_selftest.sign_file_in_place"), \
             patch("cqc_lem.utilities.c2pa_selftest._read_manifest", return_value=store), \
             patch("cqc_lem.utilities.c2pa_selftest.read_ai_source_type",
                   return_value="http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"):
            return c2pa_selftest.check_format("png", str(tmp_path), "c", "k", b"")

    @staticmethod
    def _store(source="http://x/trainedAlgorithmicMedia", statuses=()):
        return {"active_manifest": "m", "manifests": {"m": {"assertions": [
            {"label": "c2pa.actions.v2", "data": {"actions": [{"digitalSourceType": source}]}}]}},
            "validation_status": [{"code": c} for c in statuses]}

    def test_untrusted_signer_is_expected_and_passes(self, tmp_path):
        assert self._row(tmp_path, self._store(statuses=["signingCredential.untrusted"]))["ok"] is True

    def test_other_validation_status_fails(self, tmp_path):
        row = self._row(tmp_path, self._store(statuses=["assertion.hashedURI.mismatch"]))
        assert row["ok"] is False and "mismatch" in row["problem"]

    def test_wrong_source_type_fails(self, tmp_path):
        row = self._row(tmp_path, self._store(source="http://x/digitalCapture"))
        assert row["ok"] is False and "trainedAlgorithmicMedia" in row["problem"]


class TestCensus:
    def test_line_reports_counts(self):
        from cqc_lem.utilities import c2pa_selftest
        with patch("cqc_lem.utilities.db.count_posts_by_c2pa_signed",
                   return_value={"signed": 2, "unsigned": 1, "unknown": 5}):
            line = c2pa_selftest._census_line(7)
        assert "signed(1)=2" in line and "unsigned(0)=1" in line and "unknown(NULL)=5" in line

    def test_unreadable_db_never_reads_as_zero(self):
        from cqc_lem.utilities import c2pa_selftest
        with patch("cqc_lem.utilities.db.count_posts_by_c2pa_signed", return_value=None):
            assert "unavailable" in c2pa_selftest._census_line(7)

    def test_repository_query_is_read_only_and_windowed(self):
        from cqc_lem.platform.db.repositories import posts
        cursor = MagicMock()
        cursor.fetchone.return_value = (3, 1, 4)

        @contextmanager
        def _cursor(**kwargs):
            assert not kwargs.get("commit")
            yield cursor

        with patch.object(posts, "db_cursor", _cursor):
            assert posts.count_posts_by_c2pa_signed(14) == {"signed": 3, "unsigned": 1, "unknown": 4}
        sql, params = cursor.execute.call_args[0]
        assert sql.lstrip().upper().startswith("SELECT") and params == (14,)

    def test_repository_returns_none_on_db_error(self):
        import mysql.connector

        from cqc_lem.platform.db.repositories import posts

        @contextmanager
        def _boom(**kwargs):
            raise mysql.connector.Error("down")
            yield

        with patch.object(posts, "db_cursor", _boom), patch.object(posts, "log_error"):
            assert posts.count_posts_by_c2pa_signed() is None

    def test_facade_re_exports_it(self):
        from cqc_lem.utilities import db
        assert "count_posts_by_c2pa_signed" in db.__all__
