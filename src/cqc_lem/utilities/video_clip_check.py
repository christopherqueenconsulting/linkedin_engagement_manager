"""Post-render clip check: did Runway hallucinate between frames? (PR #2249 video gauntlet).

The source frame is graded before a credit is spent; the CLIP never was. The gauntlet's clips
showed what image-to-video does unattended: a striped mug popping into frame in the last second,
an arm swinging in from nowhere, a dark monitor growing bright green UI bars. This samples three
frames of the finished clip (ffmpeg at 1s, 2.5s and 4.5s), sends them to ``lem-vision`` in ONE
call, and asks three yes/no questions. Any yes buys the caller ONE re-render with a repair clause
naming the defect; otherwise the clip ships.

Bounded and fail-OPEN: no ffmpeg, an undownloadable clip, a vision outage or an unreadable reply
all return an unchecked verdict and the clip ships as it would have without this module. Off with
``VIDEO_CLIP_CHECK_ENABLED=false`` (read at call time).
"""

import base64
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Optional

from cqc_lem.utilities.logger import log_debug, log_info, log_warning

SAMPLE_SECONDS = (1.0, 2.5, 4.5)
_FFMPEG_TIMEOUT_SECONDS = 20
_CHECK_MAX_TOKENS = 400

# (reply key, the question, the defect's name in a repair clause)
QUESTIONS = (
    ("objects_appear_or_disappear",
     "Do any objects or people appear or disappear between the frames?",
     "an object or person appeared or disappeared mid-shot"),
    ("limbs_or_faces_distorted",
     "Are any limbs or faces distorted in any frame?",
     "a limb or face distorted"),
    ("text_or_ui_appears",
     "Does any on-screen text or UI appear in any frame?",
     "on-screen text or UI appeared"),
)

_PROMPT = ("These are three frames, in order, from ONE continuous 5-second AI-generated video "
           "clip (at 1s, 2.5s and 4.5s). Compare them and answer:\n"
           + "\n".join(f"- {key}: {question}" for key, question, _ in QUESTIONS)
           + "\nRespond with ONLY a JSON object: "
           + "{" + ", ".join(f'"{key}": true|false' for key, _, _ in QUESTIONS)
           + ', "details": "<one short sentence naming what changed, or empty>"}')


@dataclass
class ClipVerdict:
    """One look at a finished clip.

    Attributes:
        checked: False when the check could not run (it then fails open).
        defects: The defect names (``QUESTIONS``' third column) the judge answered yes to.
        details: The judge's own one-sentence account of what changed.
        reason: Why the check could not run, when ``checked`` is False.
    """

    checked: bool
    defects: list = field(default_factory=list)
    details: str = ""
    reason: str = ""

    def to_receipt(self) -> dict:
        """The verdict as a JSON-safe dict for the video's brief receipt."""
        return asdict(self)


def clip_check_enabled() -> bool:
    """``VIDEO_CLIP_CHECK_ENABLED``, read at call time; ON unless set to a false-y value."""
    return os.getenv("VIDEO_CLIP_CHECK_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off")


def sample_frames(video_path: str, out_dir: str) -> list[str]:
    """Extract one JPEG per ``SAMPLE_SECONDS`` with ffmpeg. Raises when any frame is missing.

    Args:
        video_path: The local clip.
        out_dir: Where to write the frames.

    Returns:
        The frame paths, in time order.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    frames = []
    for index, second in enumerate(SAMPLE_SECONDS):
        out = os.path.join(out_dir, f"frame{index}.jpg")
        subprocess.run([ffmpeg, "-y", "-ss", f"{second}", "-i", video_path, "-frames:v", "1",
                        "-q:v", "3", out], capture_output=True, text=True,
                       timeout=_FFMPEG_TIMEOUT_SECONDS, check=False)
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError(f"ffmpeg produced no frame at {second}s")
        frames.append(out)
    return frames


def _frame_part(path: str) -> dict:
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"}}


def inspect_frames(frames: list[str]) -> ClipVerdict:
    """ONE ``lem-vision`` call over the sampled frames. Raises on an unusable reply.

    Args:
        frames: The sampled frame paths, in time order.

    Returns:
        The checked verdict.
    """
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    response = client.chat.completions.create(
        model="lem-vision",
        messages=[{"role": "user", "content": [{"type": "text", "text": _PROMPT}]
                   + [_frame_part(f) for f in frames]}],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=_CHECK_MAX_TOKENS,
    )
    answer = _loads_json_object(response.choices[0].message.content or "")
    if not isinstance(answer, dict) or not all(key in answer for key, _, _ in QUESTIONS):
        raise ValueError("clip check reply was not the expected JSON object")
    defects = [name for key, _, name in QUESTIONS if answer.get(key) is True]
    details = " ".join(str(answer.get("details") or "").split())[:240]
    return ClipVerdict(checked=True, defects=defects, details=details)


def check_clip_url(url: str, *, user_id: Optional[int] = None,
                   post_id: Optional[int] = None) -> ClipVerdict:
    """Download a finished clip to a temp dir, sample it, and judge it. Never raises.

    Args:
        url: The clip — the renderer's remote URL, or a local path.
        user_id: For log context.
        post_id: For log context.

    Returns:
        The verdict; ``checked=False`` (with ``reason``) whenever the check could not run.
    """
    workdir = tempfile.mkdtemp(prefix="clipcheck_")
    try:
        if str(url).startswith("http"):
            from cqc_lem.utilities.utils import save_video_url_to_dir
            local = save_video_url_to_dir(url, workdir)
        else:
            local = str(url)
        verdict = inspect_frames(sample_frames(local, workdir))
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"[:240]
        # The clip ships as it would have without this module; a broken checker is still a defect
        # someone should see, so it is a warning rather than a silent pass.
        log_warning("Clip check could not run — shipping the clip unchecked", exc=e,
                    user_id=user_id, post_id=post_id, action_type="video_clip_check")
        return ClipVerdict(checked=False, reason=reason)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if verdict.defects:
        log_info("Clip check found defects", user_id=user_id, post_id=post_id,
                 action_type="video_clip_check", defects="; ".join(verdict.defects),
                 details=verdict.details)
    else:
        log_debug("Clip check passed", user_id=user_id, post_id=post_id,
                  action_type="video_clip_check")
    return verdict


def repair_clause(verdict: ClipVerdict) -> str:
    """The clause appended to the motion prompt for the ONE re-render, naming the defect.

    Args:
        verdict: A checked verdict with at least one defect.

    Returns:
        A short clause: what went wrong last time, then what the shot must hold instead.
    """
    named = "; ".join(verdict.defects)
    detail = f" ({verdict.details})" if verdict.details else ""
    return (f"Correct the last render, where {named}{detail}: only what is already in the image "
            f"stays in frame for the whole shot, limbs and faces keep natural proportions, and "
            f"screens stay dark and unchanged.")
