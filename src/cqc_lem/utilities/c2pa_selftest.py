"""In-container proof that C2PA signing works, BEFORE ``C2PA_ENABLED`` is flipped (#2794).

    python -m cqc_lem.utilities.c2pa_selftest [--days N]

Signs a generated PNG, JPEG, WEBP and short MP4 in a temp dir with the configured cert and key,
through ``c2pa_helper.sign_file_in_place`` (the SAME code ``add_ai_content_credentials`` runs), then
re-reads each with ``c2pa.Reader`` and reports per format: signed, ``digitalSourceType``, IPTC
(images) and validation status. ``signingCredential.untrusted`` is EXPECTED for a self-signed chain
and passes; any other status fails. Exit 0 only when every format passes.

Read-only apart from its own temp dir: it never prints key material, never touches LinkedIn, and
the optional ``posts.c2pa_signed`` census is a SELECT. Owner runbook: ``docs/content-quality-audits/video.md``.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional

from cqc_lem.utilities import c2pa_helper
from cqc_lem.utilities.c2pa_helper import (
    _AI_SOURCE_TYPE,
    read_ai_source_type,
    sign_file_in_place,
    write_ai_source_metadata,
)

FORMATS = ("png", "jpg", "webp", "mp4")
_EXPECTED_STATUSES = frozenset({"signingCredential.untrusted"})
_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "mp4": "video/mp4"}


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def _scrub(text: str, key_bytes: bytes) -> str:
    """Remove any run of the private key from an error string before it can be printed."""
    for line in key_bytes.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if len(line) >= 8 and not line.startswith("-----"):
            text = text.replace(line, "[redacted]")
    return text[:300]


def check_credentials(cert_path: str, key_path: str) -> list[str]:
    """Problems with the cert/key on disk, by name. An empty list means they are usable."""
    uid = os.getuid() if hasattr(os, "getuid") else -1
    problems: list[str] = []
    for label, path in (("cert", cert_path), ("key", key_path)):
        if not path:
            problems.append(f"{label} path is not configured")
        elif not os.path.exists(path):
            problems.append(f"{label} missing: {path}")
        else:
            try:
                with open(path, "rb"):
                    pass
            except OSError:
                problems.append(f"{label} unreadable by uid {uid}: {path}")
    if problems:
        return problems
    return _profile_problems(cert_path)


def _profile_problems(cert_path: str) -> list[str]:
    """The signer-certificate profile C2PA validators enforce, checked on the LEAF (first cert)."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        with open(cert_path, "rb") as fh:
            certs = x509.load_pem_x509_certificates(fh.read())
    except Exception as e:
        return [f"cert is not a readable PEM chain ({type(e).__name__})"]
    if not certs:
        return ["cert file holds no certificate"]
    leaf = certs[0]
    problems: list[str] = []
    try:
        bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        if bc.ca:
            problems.append("cert fails the C2PA profile: leaf is a CA (CA:TRUE)")
    except x509.ExtensionNotFound:
        problems.append("cert fails the C2PA profile: no basicConstraints on the leaf")
    try:
        ku = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        if not ku.digital_signature:
            problems.append("cert fails the C2PA profile: digitalSignature key usage missing")
    except x509.ExtensionNotFound:
        problems.append("cert fails the C2PA profile: no keyUsage on the leaf")
    try:
        leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        problems.append("cert fails the C2PA profile: no extendedKeyUsage on the leaf")
    pub = leaf.public_key()
    if not (isinstance(pub, ec.EllipticCurvePublicKey) and pub.curve.name == "secp256r1"):
        problems.append("cert is not an ES256 (P-256) key; the signer is configured for ES256")
    return problems


def _make_sample(fmt: str, directory: str) -> str:
    path = os.path.join(directory, f"selftest.{fmt}")
    if fmt == "mp4":
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found; cannot build the sample MP4")
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=1",
             "-pix_fmt", "yuv420p", path],
            capture_output=True, timeout=60, check=False)
        if result.returncode != 0 or not os.path.exists(path):
            raise RuntimeError("ffmpeg could not build the sample MP4")
        return path
    from PIL import Image
    img = Image.new("RGB", (64, 64), (200, 120, 40))
    img.save(path, {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[fmt])
    return path


def _read_manifest(path: str) -> dict[str, Any]:
    import c2pa
    reader = c2pa.Reader(path)
    try:
        return json.loads(reader.json())
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


def _source_type_and_status(store: dict[str, Any]) -> tuple[Optional[str], list[str]]:
    active = (store.get("manifests") or {}).get(store.get("active_manifest") or "") or {}
    source = None
    for assertion in active.get("assertions") or []:
        if str(assertion.get("label", "")).startswith("c2pa.actions"):
            for action in (assertion.get("data") or {}).get("actions") or []:
                if action.get("digitalSourceType"):
                    source = action["digitalSourceType"]
    statuses = [str(s.get("code")) for s in store.get("validation_status") or [] if s.get("code")]
    return source, statuses


def check_format(fmt: str, directory: str, cert_path: str, key_path: str, key_bytes: bytes) -> dict[str, Any]:
    """Sign and re-read one sample. ``ok`` is the verdict; ``problem`` names why it is False."""
    row: dict[str, Any] = {"format": fmt, "signed": False, "source_type": None,
                           "iptc": None, "validation": [], "ok": False, "problem": None}
    try:
        path = _make_sample(fmt, directory)
        if fmt != "mp4":
            write_ai_source_metadata(path)
        sign_file_in_place(path, _MIME[fmt], cert_path, key_path)
        row["signed"] = True
        source, statuses = _source_type_and_status(_read_manifest(path))
        row["source_type"] = source
        row["validation"] = statuses
        if fmt != "mp4":
            row["iptc"] = read_ai_source_type(path) == _AI_SOURCE_TYPE
    except Exception as e:
        row["problem"] = _scrub(f"{type(e).__name__}: {e}", key_bytes)
        return row
    unexpected = [s for s in row["validation"] if s not in _EXPECTED_STATUSES]
    if not (source or "").endswith("trainedAlgorithmicMedia"):
        row["problem"] = f"digitalSourceType is {source!r}, expected trainedAlgorithmicMedia"
    elif row["iptc"] is False:
        row["problem"] = "IPTC DigitalSourceType missing from the signed image"
    elif unexpected:
        row["problem"] = "validation failed: " + ", ".join(unexpected)
    row["ok"] = row["problem"] is None
    return row


def run_selftest(days: int = 7) -> int:
    """Run every check, print the report, and return the process exit code."""
    cert_path = c2pa_helper.C2PA_CERT_PATH
    key_path = c2pa_helper.C2PA_KEY_PATH
    _out(f"C2PA self-test  cert={cert_path or '(unset)'}  key={key_path or '(unset)'}")
    problems = check_credentials(cert_path, key_path)
    for problem in problems:
        _out(f"FAIL  {problem}")
    ok = not problems
    if ok:
        with open(key_path, "rb") as fh:
            key_bytes = fh.read()
        with tempfile.TemporaryDirectory(prefix="c2pa-selftest-") as tmp:
            for fmt in FORMATS:
                row = check_format(fmt, tmp, cert_path, key_path, key_bytes)
                status = "ok  " if row["ok"] else "FAIL"
                _out(f"{status}  {fmt:<5} signed={row['signed']} "
                     f"digitalSourceType={row['source_type']} iptc={row['iptc']} "
                     f"validation={','.join(row['validation']) or 'clean'}"
                     + (f"  -- {row['problem']}" if row["problem"] else ""))
                ok = ok and row["ok"]
    _out(_census_line(days))
    _out("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _census_line(days: int) -> str:
    """Read-only `posts.c2pa_signed` count over the last ``days``. Informational; never fails the run."""
    try:
        from cqc_lem.utilities.db import count_posts_by_c2pa_signed
        counts = count_posts_by_c2pa_signed(days)
    except Exception as e:
        return f"posts.c2pa_signed last {days}d: unavailable ({type(e).__name__})"
    if counts is None:
        return f"posts.c2pa_signed last {days}d: unavailable (DB read failed)"
    return (f"posts.c2pa_signed last {days}d: signed(1)={counts['signed']} "
            f"unsigned(0)={counts['unsigned']} unknown(NULL)={counts['unknown']}")


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: parse ``--days`` and return the self-test's exit code."""
    parser = argparse.ArgumentParser(description="Prove C2PA signing works with the configured cert.")
    parser.add_argument("--days", type=int, default=7, help="window for the posts.c2pa_signed count")
    return run_selftest(parser.parse_args(argv).days)


if __name__ == "__main__":
    sys.exit(main())
