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
