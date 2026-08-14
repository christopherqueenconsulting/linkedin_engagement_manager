"""Sample SHIPPED native video posts and measure them against the #1282 video rubric (issue #1363).

`docs/content-quality-audits/video.md` grades the machinery that produces a video; it could not
grade a corpus, because the agent worktree has no production MySQL credentials and no stored assets.
This is the sampler that closes that gap the moment it is run somewhere the database and the
`assets_dir` volume are both reachable: it pulls 6-10 published video posts, probes each stored MP4,
extracts representative frames, and prints the scorecard the audit doc is missing.

Read-only: it opens no browser, writes nothing to the database and calls no LLM. It re-uses the
existing readers (`db.get_posted_posts`, `db.get_post_video_url`, `db.get_post_captions`) and the
existing scorer (`content_quality.score_item` / `score_video_asset`) rather than issuing SQL or
re-implementing a probe, so the numbers it reports are the same numbers the nightly telemetry
records. The only thing it writes is image files, under `--frames-dir`.

Run it where the database and the asset volume are reachable:

    poetry run python scripts/sample_shipped_videos.py
    poetry run python scripts/sample_shipped_videos.py --users 1 --limit 10 --json > corpus.json

Output goes to stdout because the scorecard IS the product of this script; the frames land on disk
and their paths are named in the report so they can be referenced from the audit doc.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from typing import Any, Iterable, Mapping, Optional, Sequence

# Runnable from anywhere (the checkout's src/ is not on sys.path for a standalone script).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cqc_lem.platform.db.enums import PostType  # noqa: E402
from cqc_lem.utilities.content_quality import (  # noqa: E402
    SURFACE_POST,
    VIDEO_PROBE_MISSING,
    VIDEO_PROBE_OK,
    resolve_local_video_path,
    score_item,
    score_video_asset,
)

# The corpus band the issue asks for: fewer than MIN_CORPUS cannot support a scorecard, and more
# than MAX_SAMPLES is not a bigger answer — it is a slower one, over older renders.
MIN_CORPUS = 6
MAX_SAMPLES = 10
# Rubric row R4: LinkedIn feed video is rewarded at 5-10 seconds, and every model default sits
# inside that band. A clip outside it is the regression this measure exists to catch.
DURATION_BAND = (5, 10)
# Where the frames go by default — beside the audit doc that references them.
DEFAULT_FRAMES_DIR = os.path.join("docs", "content-quality-audits", "assets", "1363")
# Rubric row R1 grades the first 2-3 seconds, so the opening frame is sampled EARLY rather than at
# t=0: frame zero of a Runway render is routinely a near-black fade-in and says nothing about the
# hook. The closing frame backs off the end for the same reason (R8).
OPEN_FRAME_SECONDS = 0.5
CLOSE_FRAME_BACKOFF_SECONDS = 0.3
FRAME_TIMEOUT_SECONDS = 30
# The 2026-08-14 production run sampled 10 shipped video posts and graded none of them: every
# stored MP4 was gone. That is the DESIGNED behaviour, not a broken mount — `purge_post_assets`
# (#148) deletes the local copy the moment LinkedIn re-hosts the media — and a report that prints
# "10 missing" without saying so sends the next reader to check volume permissions for an afternoon.
PURGE_HINT = (
    "NOTE: nothing was gradable and every sampled asset is missing on disk. Expected, not a mount\n"
    "  fault: purge_post_assets (#148, 2026-06-25) deletes a post's stored MP4 as soon as it\n"
    "  publishes, so a SHIPPED post's asset only survives on renders that predate that purge —\n"
    "  which also predate the #1293 aspect fix and the #1278 caption burn. Grading current-pipeline\n"
    "  video needs the asset measures recorded at STORE time: issue #1517."
)


def video_posts(posts: Optional[Iterable[Mapping[str, Any]]], limit: int = MAX_SAMPLES) -> list:
    """The most recently published video posts, newest first.

    `get_posted_posts` answers oldest-first across every post type and None when the read failed;
    both are normalized here so a failed read samples nothing rather than raising. Rows with no
    body are dropped at the source: the issue asks for posts whose body AND asset are available, and
    a bodiless row cannot be graded on either the hook or the caption it should have burned.
    """
    rows = [p for p in (posts or [])
            if str(p.get("post_type") or "") == PostType.VIDEO.value
            and str(p.get("content") or "").strip()]
    rows.sort(key=lambda p: str(p.get("scheduled_time") or ""), reverse=True)
    return rows[:max(0, limit)]


def frame_timestamps(duration: Optional[float]) -> list:
    """`(label, seconds)` pairs to grab from a clip of `duration` seconds.

    An unknown or implausibly short duration yields the opening frame only — inventing a midpoint
    for a clip whose length was never read is how a scorecard ends up citing a frame that does not
    depict what it claims.
    """
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return [("open", OPEN_FRAME_SECONDS)]
    if seconds <= OPEN_FRAME_SECONDS + CLOSE_FRAME_BACKOFF_SECONDS:
        return [("open", OPEN_FRAME_SECONDS)]
    return [("open", OPEN_FRAME_SECONDS),
            ("mid", round(seconds / 2, 2)),
            ("close", round(seconds - CLOSE_FRAME_BACKOFF_SECONDS, 2))]


def extract_frames(video_path: Optional[str], out_dir: str, prefix: str,
                   duration: Optional[float]) -> list:
    """Write one JPEG per `frame_timestamps` entry and return the paths that actually landed.

    Best-effort in every direction — no ffmpeg, an unreadable clip, a non-zero exit or a zero-byte
    output simply contributes no frame. The scorecard reports the frames it has; a sampler that
    raised here would lose the measured rows it already collected.
    """
    path = str(video_path or "").strip()
    if not path or not os.path.exists(path):
        return []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    os.makedirs(out_dir, exist_ok=True)
    written: list = []
    for label, seconds in frame_timestamps(duration):
        out_path = os.path.join(out_dir, f"{prefix}_{label}.jpg")
        try:
            # -ss BEFORE -i seeks by keyframe, which is what a 5-10s render wants: it is fast and
            # the frame it lands on is the one a scrolling viewer actually sees.
            subprocess.run([ffmpeg, "-y", "-ss", str(seconds), "-i", path,
                            "-frames:v", "1", "-q:v", "2", out_path],
                           capture_output=True, check=False, timeout=FRAME_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            written.append(out_path)
    return written


def sample_report(post: Mapping[str, Any], video_url: Optional[str],
                  captions: Optional[Mapping[str, Any]] = None,
                  frames: Optional[Sequence[str]] = None,
                  user_id: Optional[int] = None) -> dict:
    """Score ONE shipped video post — body measures and asset measures in the same row.

    Everything scored here comes from `content_quality`, the module the nightly beat scores with, so
    a row in this report and a row in `content_quality_scores` cannot disagree about the same post.
    """
    body = str(post.get("content") or "")
    video = score_video_asset(video_url=video_url)
    row = score_item(surface=SURFACE_POST, ref_id=post.get("id"), text=body,
                     shipped_on=post.get("scheduled_time"), video=video)
    caption_text = str((captions or {}).get("caption_text") or "").strip()
    duration = video.get("video_duration_seconds")
    low, high = DURATION_BAND
    return {
        **row,
        "post_id": post.get("id"),
        "user_id": user_id,
        "video_url": video_url,
        "local_path": resolve_local_video_path(video_url),
        "body_available": bool(body.strip()),
        "asset_available": video.get("video_asset_probe") == VIDEO_PROBE_OK,
        "duration_in_band": (low <= duration <= high) if duration is not None else None,
        "captioned": bool(caption_text),
        "caption_srt": bool((captions or {}).get("caption_srt_url")),
        "frames": list(frames or []),
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Aggregate the per-post rows into the scorecard the audit doc is missing.

    `sufficient_corpus` is reported next to every rate for the same reason the newsletter sampler
    reports it: a hit rate over three videos is an anecdote, and publishing it as a measurement is
    how an audit invents a calibration. `duration_in_band` is counted over the clips whose duration
    was actually READ — an unprobed clip is excluded from the denominator, never scored as a pass
    and never as a failure.
    """
    graded = [r for r in rows if r.get("body_available") and r.get("asset_available")]
    banded = [r for r in graded if r.get("duration_in_band") is not None]
    in_band = [r for r in banded if r.get("duration_in_band")]
    return {
        "sampled": len(rows),
        "gradable": len(graded),
        "sufficient_corpus": len(graded) >= MIN_CORPUS,
        "min_corpus": MIN_CORPUS,
        "duration_band": list(DURATION_BAND),
        "duration_measured": len(banded),
        "duration_in_band": len(in_band),
        "duration_in_band_rate": (len(in_band) / len(banded)) if banded else None,
        "captioned": sum(1 for r in graded if r.get("captioned")),
        "aspect_ratios": dict(Counter(str(r.get("video_aspect_ratio") or "unknown")
                                      for r in graded).most_common()),
        "model_tiers": dict(Counter(str(r.get("video_model_tier") or "unknown")
                                    for r in graded).most_common()),
        "asset_probes": dict(Counter(str(r.get("video_asset_probe") or "unknown")
                                     for r in rows).most_common()),
        "hook_within_budget": sum(1 for r in graded if r.get("hook_within_budget")),
        "slop_hard": sum(int(r.get("slop_hard") or 0) for r in graded),
        "frames": [f for r in rows for f in (r.get("frames") or [])],
        "per_post": list(rows),
    }


def collect(user_ids: Sequence[int], limit: int, frames_dir: str,
            with_frames: bool = True) -> dict:
    """Read the corpus through the db facade and probe each stored asset."""
    from cqc_lem.utilities.db import get_post_captions, get_post_video_url, get_posted_posts

    rows: list = []
    for user_id in user_ids:
        for post in video_posts(get_posted_posts(user_id), limit=limit):
            video_url = get_post_video_url(post.get("id"))
            probe = score_video_asset(video_url=video_url)
            frames = extract_frames(resolve_local_video_path(video_url), frames_dir,
                                    f"post{post.get('id')}",
                                    probe.get("video_duration_seconds")) if with_frames else []
            rows.append(sample_report(post, video_url,
                                      captions=get_post_captions(post.get("id")),
                                      frames=frames, user_id=user_id))
    rows.sort(key=lambda r: str(r.get("shipped_on") or ""), reverse=True)
    return summarize(rows[:max(0, limit)])


def purge_hint(summary: Mapping[str, Any]) -> Optional[str]:
    """`PURGE_HINT` when an empty scorecard is explained by the publish-time asset purge, else None.

    Only fires when NOTHING was gradable and at least one asset probed `missing`: a corpus that
    graded something has a scorecard to read, and a run that failed for another reason (empty probe,
    unreadable file) must not be handed this explanation.
    """
    if summary.get("gradable"):
        return None
    if not int((summary.get("asset_probes") or {}).get(VIDEO_PROBE_MISSING, 0) or 0):
        return None
    return PURGE_HINT


def _render(summary: Mapping[str, Any]) -> str:
    lines = ["Shipped native-video corpus sample (issue #1363)", ""]
    gradable = summary["gradable"]
    lines.append(f"Video posts sampled       : {summary['sampled']}")
    lines.append(f"Gradable (body + asset)   : {gradable}"
                 + ("" if summary["sufficient_corpus"]
                    else f"  (NOT ENOUGH — {summary['min_corpus']}+ needed for a scorecard)"))
    low, high = summary["duration_band"]
    rate = summary["duration_in_band_rate"]
    lines.append(f"Duration in {low}-{high}s band     : {summary['duration_in_band']}"
                 f"/{summary['duration_measured']} measured"
                 + (f"  ({rate:.0%})" if rate is not None else "  (none probed)"))
    lines.append(f"Captioned (burned text)   : {summary['captioned']}/{gradable}")
    lines.append(f"Hook within mobile budget : {summary['hook_within_budget']}/{gradable}")
    lines.append(f"Hard slop violations      : {summary['slop_hard']}")
    lines.append("")
    for title, key in (("Aspect ratios", "aspect_ratios"), ("Model tiers", "model_tiers"),
                       ("Asset probe states", "asset_probes")):
        lines.append(f"{title}:")
        for name, count in (summary[key] or {"— none —": 0}).items():
            lines.append(f"  {count:>4}  {name}")
    lines.append("")
    lines.append("Per post (id | duration | ratio | captioned | probe)")
    for row in summary["per_post"]:
        lines.append(f"  {row.get('post_id')} | {row.get('video_duration_seconds')}s | "
                     f"{row.get('video_aspect_ratio')} | "
                     f"{'yes' if row.get('captioned') else 'no'} | "
                     f"{row.get('video_asset_probe')}")
    lines.append("")
    lines.append("Frames written:")
    for frame in summary["frames"] or []:
        lines.append(f"  {frame}")
    if not summary["frames"]:
        lines.append("  — none — (no ffmpeg, or no readable local asset)")
    hint = purge_hint(summary)
    if hint:
        lines.extend(["", hint])
    return "\n".join(lines)


def _user_ids(raw: Optional[str]) -> list:
    if raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    from cqc_lem.utilities.db import get_active_user_ids

    return list(get_active_user_ids() or [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns 0 on an empty corpus — "nothing shipped yet" is an answer."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", help="comma-separated user ids (default: all active users)")
    parser.add_argument("--limit", type=int, default=MAX_SAMPLES,
                        help=f"how many video posts to sample (default: {MAX_SAMPLES})")
    parser.add_argument("--frames-dir", default=DEFAULT_FRAMES_DIR,
                        help=f"where representative frames are written (default: {DEFAULT_FRAMES_DIR})")
    parser.add_argument("--no-frames", action="store_true",
                        help="probe and score only; write no image files")
    parser.add_argument("--json", action="store_true", help="emit the raw summary as JSON")
    args = parser.parse_args(argv)

    summary = collect(_user_ids(args.users), max(1, args.limit), args.frames_dir,
                      with_frames=not args.no_frames)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(_render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
