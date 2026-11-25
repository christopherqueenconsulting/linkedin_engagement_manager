"""Render ONE fixed tutorial narration through tts-1 and the realtime model, side by side (#2258).

`lem-tts` (tts-1) sunsets 2027-01-06 and its successor is realtime-only, so the swap needs a human
listen first. This writes `tts-1.mp3` and `realtime.mp3` into one directory and prints each file's
path, duration, character count and billed USD.

Read-only towards LEM state: no DB, no publish, no upload. It calls `_tts_openai` and
`_tts_realtime` directly, so `TUTORIAL_TTS_PROVIDER` is irrelevant.

    poetry run python scripts/tts_side_by_side.py <out_dir>

Exit codes: 0 both rendered, 1 the realtime render failed (the tts-1 file is kept), 2 the tts-1
render failed, 64 bad usage.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cqc_lem.utilities.env_constants import TUTORIAL_MAX_NARRATION_CHARS  # noqa: E402
from cqc_lem.utilities.marketing import video_tutorials as vt  # noqa: E402

# Fixed on purpose: both engines read the same words, so any difference is the voice.
SAMPLE_NARRATION = (
    "Welcome to LinkedIn Engagement Manager. In this short tutorial we open the content plan, "
    "review the posts scheduled for this week, and approve one for publishing. First, open the "
    "Content tab. Each card shows the post type, the buyer-journey stage and the scheduled time. "
    "Select the first draft, read it through, and edit any line you want to change. When you are "
    "happy with it, press Approve. The post is now queued, and it will go out at the time shown. "
    "That is all it takes to keep a steady cadence without writing every post by hand."
)


def _line(label: str, path: str, chars: int, usd: float) -> str:
    try:
        duration = f"{vt.audio_duration(path):.1f}s"
    except Exception as exc:  # noqa: BLE001 - a missing ffprobe must not hide the render
        duration = f"unreadable ({exc})"
    return f"{label}: path={path} duration={duration} chars={chars} usd={usd:.6f}\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Render both voices and report them.

    Args:
        argv: Arguments after the program name; `<out_dir>` is the only one.

    Returns:
        The process exit code (see the module docstring).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: tts_side_by_side.py <out_dir>\n")
        return 64
    if len(SAMPLE_NARRATION) > TUTORIAL_MAX_NARRATION_CHARS:
        sys.stderr.write(f"sample is {len(SAMPLE_NARRATION)} chars, over the "
                         f"{TUTORIAL_MAX_NARRATION_CHARS} cap\n")
        return 64
    out_dir = args[0]
    os.makedirs(out_dir, exist_ok=True)
    chars = len(SAMPLE_NARRATION)

    tts1_path = os.path.join(out_dir, "tts-1.mp3")
    try:
        vt._tts_openai(SAMPLE_NARRATION, tts1_path)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"tts-1 render failed: {exc}\n")
        return 2
    sys.stdout.write(_line("tts-1", tts1_path, chars, vt.tts_cost_usd(chars, "openai")))

    realtime_path = os.path.join(out_dir, "realtime.mp3")
    try:
        usd = vt._tts_realtime(SAMPLE_NARRATION, realtime_path)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"realtime render failed: {exc}\n")
        return 1
    sys.stdout.write(_line("realtime", realtime_path, chars, usd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
