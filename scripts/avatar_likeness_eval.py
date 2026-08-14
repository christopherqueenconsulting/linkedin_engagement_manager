"""Measure `probe_avatar_likeness` against human-labelled avatar frames (issue #1430).

`AVATAR_LIKENESS_VIDEO_HOLD_ENABLED` may not default on until the probe's false-positive and
false-negative rates are measured on REAL rendered frames. This repository is PUBLIC, so a real
person's likeness must never be committed — this script is how the measurement is produced without
publishing one. It reads a manifest of frames that stay OUTSIDE the checkout, runs the live probe
over them, and writes a verdict file that carries only a content digest per frame, the human's
label and the probe's verdict.

Manifest format (JSON, a list of entries, kept wherever the frames live — NOT in the repo):

    [
      {
        "frame": "/home/you/avatar-eval/lora-01.png",
        "label": "present",              // the HUMAN's verdict: "present" or "absent"
        "gender_presentation": "man",    // the declared attributes to judge the frame AGAINST
        "age_band": "40s",
        "used_avatar": "true",           // "false" for a base-Flux fallback frame
        "note": "clean LoRA render"      // free text, never read by the grader
      }
    ]

Cover both classes deliberately — the grader needs at least 4 of each: frames a human agrees carry
the likeness, and frames a human says do not (a bad LoRA render, a frame whose focal person is
someone else, a frame with no person at all, and at least one base-Flux fallback frame marked
`"used_avatar": "false"`).

Run it where the LiteLLM proxy is reachable (the probe is a real `lem-vision` call, so this costs
money — one call per frame):

    poetry run python scripts/avatar_likeness_eval.py --manifest ~/avatar-eval/manifest.json
    poetry run python scripts/avatar_likeness_eval.py --manifest ~/avatar-eval/manifest.json \
        --out tests/fixtures/avatar_likeness_verdicts.json

The scorecard goes to stdout; `--out` writes the committable verdict file. Writing REFUSES if any
record carries a field the grader did not put there, so a frame path can never reach the repo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional, Sequence

# Runnable from anywhere (the checkout's src/ is not on sys.path for a standalone script).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cqc_lem.utilities.avatar.likeness_eval import (  # noqa: E402
    MIN_GRADED,
    MIN_PER_CLASS,
    grade,
    leaks_a_frame_path,
    normalize_label,
    run_eval,
)


def _load_manifest(path: str) -> list[dict]:
    """Read and sanity-check the labelled manifest."""
    with open(path, "r", encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, list):
        raise SystemExit(f"{path}: expected a JSON list of entries")
    bad = [i for i, e in enumerate(entries) if normalize_label(e.get("label")) is None]
    if bad:
        raise SystemExit(f"{path}: entries {bad} have no usable 'label' (present|absent)")
    return entries


def _probe(image_path: str, avatar: dict) -> dict:
    """The live probe, imported lazily so `--help` needs no LiteLLM config."""
    from cqc_lem.utilities.avatar.likeness_probe import probe_avatar_likeness

    return probe_avatar_likeness(image_path, avatar)


def _render_block(title: str, summary: dict) -> list[str]:
    """One scorecard block: counts, then the two rates, then whether it is enough to act on."""
    def pct(value: Optional[float]) -> str:
        return "n/a (no frames in this class)" if value is None else f"{value * 100:.1f}%"

    return [
        f"{title}",
        f"  graded {summary['graded']}  (TP {summary['true_positive']}  FN {summary['false_negative']}"
        f"  TN {summary['true_negative']}  FP {summary['false_positive']})",
        f"  unchecked {summary['unchecked']} of {summary['total']}  ({pct(summary['unchecked_rate'])})",
        f"  false-negative rate (good frame wrongly declined): {pct(summary['false_negative_rate'])}",
        f"  false-positive rate (bad frame wrongly passed):    {pct(summary['false_positive_rate'])}",
        f"  sufficient to decide the hold default: {'yes' if summary['sufficient'] else 'no'}"
        f"  (needs >= {MIN_GRADED} graded, >= {MIN_PER_CLASS} per class)",
        "",
    ]


def _render(scores: dict) -> str:
    """The human-readable scorecard — this IS the product of the script."""
    lines = ["Avatar likeness probe — measured against human-labelled frames (issue #1430)", ""]
    lines += _render_block("OVERALL", scores["overall"])
    lines.append("BY DECLARED SUBJECT CLAUSE")
    for clause, summary in scores["by_subject_clause"].items():
        lines += _render_block(f"  clause: {clause or '(none declared)'}", summary)
    lines.append("BY used_avatar (posts.avatar_media — a real LoRA render, or the base-Flux fallback)")
    for value, summary in scores["by_used_avatar"].items():
        lines += _render_block(f"  used_avatar={value or '(unset)'}", summary)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns 2 when the verdict file would leak more than a verdict."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True,
                        help="path to the labelled manifest (keep it outside the repo)")
    parser.add_argument("--out", help="write the committable verdict file here")
    parser.add_argument("--include-reasons", action="store_true",
                        help="carry the vision model's one-line reason into the verdict file "
                             "(off by default — it is free text written about a real person)")
    parser.add_argument("--json", action="store_true", help="emit the scorecard as JSON")
    args = parser.parse_args(argv)

    entries = _load_manifest(args.manifest)
    records = run_eval(entries, _probe, include_reasons=args.include_reasons)
    scores = grade(records)

    if args.out:
        if leaks_a_frame_path(records):
            print("REFUSING to write: a verdict record carries a field outside the grader's own.",
                  file=sys.stderr)
            return 2
        payload: dict[str, Any] = {
            # A real run declares itself "measured": the committed placeholder is a schema example
            # and must never be read as a finding, so the two say which they are in the file.
            "source": "measured",
            "measured_frames": len(records),
            "records": records,
            "scores": scores,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"Wrote {len(records)} verdicts to {args.out}", file=sys.stderr)

    print(json.dumps(scores, indent=2) if args.json else _render(scores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
