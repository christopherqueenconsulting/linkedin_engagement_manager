"""Deterministic avatar-likeness probe for generated source frames and rendered video first frames.

Issue #1279. The user self-declares gender presentation and age band (issue #744); this probe asks
a cheap vision model whether the rendered frame still depicts that declared likeness. It never
infers attributes from the image itself — an empty declaration means there is nothing deterministic
to verify, and the probe reports unchecked rather than guessing.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from PIL import Image

from cqc_lem.utilities.ai.client import client
from cqc_lem.utilities.avatar.attributes import subject_clause
from cqc_lem.utilities.env_constants import (
    AVATAR_LIKENESS_PROBE_MODEL,
)
from cqc_lem.utilities.logger import log_debug, log_warning

_LIKENESS_PROMPT = """You are verifying that an AI-generated image still carries the user's declared
likeness after rendering.

The user has declared their likeness as: {subject_clause}.

Look at the image. Is a person matching this description clearly the central focal subject of the
frame? Ignore incidental people in the background. Answer ONLY with a JSON object:
{{"present": true|false, "reason": "<one-sentence explanation>"}}"""


def _encode_image(image_path: str) -> str:
    """Return a base64 PNG data URI for the image at ``image_path``."""
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def probe_avatar_likeness(
    image_path: str,
    avatar: dict,
    user_id: Optional[int] = None,
    post_id: Optional[int] = None,
) -> dict:
    """Check whether ``image_path`` depicts the user's declared likeness.

    Uses only the self-declared attributes stored on the avatar row (gender_presentation + age_band),
    turned into the canonical subject clause by ``attributes.subject_clause``. An empty clause means
    there is nothing to verify, so the probe returns ``checked=False``. Any vision outage, bad
    response, or unreadable image also returns ``checked=False`` — the probe fails open because a
    false positive would block a user's own video posts.

    Returns:
        ``{"present": bool|None, "checked": bool, "reason": str}``.
    """
    clause = subject_clause(avatar)
    if not clause:
        return {
            "present": None,
            "checked": False,
            "reason": "No declared likeness attributes to verify",
        }
    if not image_path or not os.path.exists(image_path):
        return {
            "present": None,
            "checked": False,
            "reason": "No image file to check",
        }

    try:
        encoded = _encode_image(image_path)
        response = client.chat.completions.create(
            model=AVATAR_LIKENESS_PROBE_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _LIKENESS_PROMPT.format(subject_clause=clause),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{encoded}",
                            "detail": "low",
                        },
                    },
                ],
            }],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=80,
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            return {
                "present": None,
                "checked": False,
                "reason": "Empty vision response",
            }
        parsed = json.loads(raw)
        present = bool(parsed.get("present"))
        reason = str(parsed.get("reason") or "").strip()
        return {
            "present": present,
            "checked": True,
            "reason": reason or ("Likeness confirmed" if present else "Likeness not confirmed"),
        }
    except Exception as e:
        log_debug(
            "Avatar likeness probe failed — treating frame as unchecked",
            error=str(e),
            user_id=user_id,
            post_id=post_id,
            action_type="avatar_likeness_probe",
        )
        return {
            "present": None,
            "checked": False,
            "reason": f"Probe error: {e}",
        }


def _ffmpeg_bin(name: str = "ffmpeg") -> Optional[str]:
    """Return the path to an ffmpeg binary, or None if it is not on PATH."""
    return shutil.which(name)


def extract_first_frame(
    video_path: str,
    out_dir: Optional[str] = None,
) -> Optional[str]:
    """Extract one frame from the start of ``video_path`` to a PNG file.

    Returns the extracted PNG path, or None when ffmpeg is unavailable or the extraction fails. This is
    a best-effort helper for inspecting already-rendered videos; the live probe runs on the stored
    source frame before video generation, so it does not depend on ffmpeg.
    """
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        log_warning("ffmpeg not found — cannot extract video first frame",
                    action_type="avatar_likeness_probe")
        return None

    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, f"{base}_first_frame.png")

    cmd = [
        ffmpeg, "-y", "-i", video_path,
        "-ss", "00:00:00.001",
        "-vframes", "1",
        "-vf", "scale='min(iw,1920)':-1",
        out_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out_path
    except subprocess.CalledProcessError as e:
        log_warning("ffmpeg first-frame extraction failed",
                    action_type="avatar_likeness_probe",
                    error=(e.stderr or str(e))[-200:])
        return None
