"""Best-effort C2PA Content Credentials for AI-generated assets.

Embeds a signed manifest declaring ``digitalSourceType = trainedAlgorithmicMedia``
so platforms that read C2PA (LinkedIn's "CR" badge) can attribute the media to AI.

Honest limitation: a self-signed cert produces a valid manifest but validators flag
it as an untrusted signer, and LinkedIn's reader is inconsistent. So this is
technically-correct provenance, NOT a guaranteed visible badge — the caption-line
disclosure (AI_DISCLOSURE_*) remains the reliable, human-visible fallback. Swapping in
a CA-issued cert on the C2PA Trust List upgrades trust with no code change.

Everything here is best-effort: it never raises into the generation pipeline.
"""
import os
import re
import struct
import zlib

from cqc_lem.utilities.env_constants import C2PA_CERT_PATH, C2PA_ENABLED, C2PA_KEY_PATH
from cqc_lem.utilities.logger import log_debug, log_warning

_AI_SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"

_FORMATS = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


def _format_for(file_path: str) -> "str | None":
    return _FORMATS.get(os.path.splitext(file_path)[1].lower())


_XMP_PACKET = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
    'Iptc4xmpExt:DigitalSourceType="' + _AI_SOURCE_TYPE + '"/>'
    '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
).encode("utf-8")
_XMP_JPEG_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_XMP_PNG_KEYWORD = b"XML:com.adobe.xmp"
_IPTC_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_SOURCE_TYPE_RE = re.compile(rb'DigitalSourceType\s*=\s*"([^"]+)"')


def _xmp_into_png(data: bytes) -> bytes:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    body = _XMP_PNG_KEYWORD + b"\x00\x00\x00\x00\x00" + _XMP_PACKET
    chunk = (struct.pack(">I", len(body)) + b"iTXt" + body
             + struct.pack(">I", zlib.crc32(b"iTXt" + body) & 0xFFFFFFFF))
    ihdr_end = 8 + 8 + struct.unpack(">I", data[8:12])[0] + 4
    return data[:ihdr_end] + chunk + data[ihdr_end:]


def _xmp_into_jpeg(data: bytes) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    payload = _XMP_JPEG_HEADER + _XMP_PACKET
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    at = 2
    if data[2:4] == b"\xff\xe0":  # JFIF APP0 must stay first
        at = 4 + struct.unpack(">H", data[4:6])[0]
    return data[:at] + segment + data[at:]


def _xmp_into_webp(file_path: str, out_path: str) -> None:
    from PIL import Image
    with Image.open(file_path) as img:
        img.load()
        img.save(out_path, "WEBP", lossless=True, xmp=_XMP_PACKET)


def write_ai_source_metadata(file_path: str) -> bool:
    """Embed the IPTC ``DigitalSourceType = trainedAlgorithmicMedia`` XMP property in an image.

    Needs no certificate. Runs BEFORE C2PA so the signed manifest hashes the metadata too.
    Supports PNG, JPEG and WEBP; returns True only when the property was written. Never raises.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _IPTC_IMAGE_EXTS or not os.path.exists(file_path):
        log_debug(f"IPTC metadata skipped (unsupported or missing): {file_path}")
        return False
    tmp_out = file_path + ".iptc.tmp"
    try:
        if ext == ".webp":
            _xmp_into_webp(file_path, tmp_out)
        else:
            with open(file_path, "rb") as fh:
                data = fh.read()
            out = _xmp_into_png(data) if ext == ".png" else _xmp_into_jpeg(data)
            with open(tmp_out, "wb") as fh:
                fh.write(out)
        os.replace(tmp_out, file_path)
        return True
    except Exception as e:
        log_warning("IPTC source-type metadata failed", exc=e)
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass  # best-effort temp-file cleanup
        return False


def read_ai_source_type(file_path: str) -> "str | None":
    """The IPTC digital source type embedded in ``file_path``, or None when absent/unreadable."""
    try:
        with open(file_path, "rb") as fh:
            match = _SOURCE_TYPE_RE.search(fh.read())
    except OSError:
        return None
    return match.group(1).decode("utf-8", "replace") if match else None


def sign_ai_image(file_path: str, *, post_id: "int | None" = None) -> bool:
    """Final-write provenance for a non-avatar AI image: IPTC metadata, then C2PA.

    Call it on the file that ships, after the last re-save (a Pillow re-save strips both layers).
    Returns the C2PA result, so a caller deciding the visible disclosure reads the same boolean
    as before. When ``post_id`` is given and C2PA is on, the outcome is persisted to
    ``posts.c2pa_signed``. Never raises.
    """
    try:
        write_ai_source_metadata(file_path)
        signed = add_ai_content_credentials(file_path)
        if post_id and C2PA_ENABLED:
            from cqc_lem.utilities.db import set_post_c2pa_signed
            set_post_c2pa_signed(post_id, signed)
        return signed
    except Exception as e:
        log_warning("AI image provenance signing failed", exc=e, post_id=post_id)
        return False


def add_ai_content_credentials(file_path: str) -> bool:
    """Sign ``file_path`` in place with an AI Content Credentials manifest.

    Returns True only when a signed manifest was written. No-ops (returns False)
    when disabled, when cert/key/file are missing, or on any signing error.
    """
    if not C2PA_ENABLED:
        return False
    if not (C2PA_CERT_PATH and C2PA_KEY_PATH
            and os.path.exists(C2PA_CERT_PATH) and os.path.exists(C2PA_KEY_PATH)
            and os.path.exists(file_path)):
        log_debug("C2PA signing skipped (missing cert/key/file)")
        return False

    fmt = _format_for(file_path)
    if not fmt:
        log_debug(f"C2PA signing skipped (unsupported format): {file_path}")
        return False

    tmp_out = file_path + ".c2pa.tmp"
    try:
        import c2pa
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        with open(C2PA_CERT_PATH, "rb") as f:
            certs = f.read()
        with open(C2PA_KEY_PATH, "rb") as f:
            key = f.read()

        def _sign(data: bytes) -> bytes:
            private_key = serialization.load_pem_private_key(key, password=None, backend=default_backend())
            return private_key.sign(data, ec.ECDSA(hashes.SHA256()))

        manifest = {
            "claim_generator_info": [{"name": "LinkedIn Engagement Manager", "version": "1.0"}],
            "format": fmt,
            "title": os.path.basename(file_path),
            "assertions": [{
                "label": "c2pa.actions",
                "data": {"actions": [{"action": "c2pa.created", "digitalSourceType": _AI_SOURCE_TYPE}]},
            }],
        }

        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        with c2pa.Signer.from_callback(_sign, c2pa.C2paSigningAlg.ES256, certs.decode("utf-8")) as signer:
            with c2pa.Builder(manifest) as builder:
                builder.sign_file(file_path, tmp_out, signer)
        os.replace(tmp_out, file_path)
        log_debug(f"C2PA credentials added: {os.path.basename(file_path)}")
        return True
    except Exception as e:
        log_warning("C2PA signing failed; relying on caption disclosure", exc=e)
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass  # best-effort temp-file cleanup; ignore if it can't be removed
        return False
