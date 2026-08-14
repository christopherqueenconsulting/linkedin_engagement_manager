"""Grade ``probe_avatar_likeness`` against human-labelled frames without publishing a likeness.

Issue #1430 (Phase 2 of #1279). ``AVATAR_LIKENESS_VIDEO_HOLD_ENABLED`` may not default on until the
probe's error rates are MEASURED, and measuring them needs real LoRA-rendered frames of a real
person. This repository is PUBLIC, so those frames must never be committed. This module is the
seam that makes the measurement publishable anyway: a frame is identified by the SHA-256 of its
bytes, and the record that leaves here carries that digest, the human's label and the probe's
verdict — never a path, never pixels.

The polarity is the thing that gets misread, so it is fixed here once and everything downstream
reads it from this docstring:

  * The **positive class is "the declared likeness is present"**.
  * A **false positive** is the probe saying ``present=True`` on a frame a human labelled
    ``absent`` — the hold would let a frame through that lost the likeness.
  * A **false negative** is the probe saying ``present=False`` on a frame a human labelled
    ``present`` — with the hold ON this costs the user their AI video, so it is the rate that
    decides the default.

Only ``checked`` verdicts are graded. The probe fails OPEN by design (a vision outage, an
unreadable image, or an empty declared likeness all return ``checked=False``), and an unchecked
verdict can never hold a video, so counting one as an error would grade the wrong thing. Unchecked
rows are reported on their own line instead.

A rate with an empty denominator is ``None``, never ``0.0`` — an unmeasured class is not a perfect
one.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping, Optional

from cqc_lem.utilities.avatar.attributes import subject_clause

#: Human ground-truth labels. Anything else in a manifest is a typo, not a third class.
LABEL_PRESENT = "present"
LABEL_ABSENT = "absent"
LABELS = (LABEL_PRESENT, LABEL_ABSENT)

#: Below these counts the rates are a curiosity, not a finding — `grade()` reports `sufficient`
#: False and the doc records the reading as provisional. Deliberately small: the issue asks for a
#: SMALL labelled set, and each frame costs a real LoRA render.
MIN_GRADED = 10
MIN_PER_CLASS = 4

#: Fields a verdict record may carry. `frame` (the on-disk path) is NOT one of them — a path names
#: the person's file and is exactly what must not reach a public repo.
RECORD_FIELDS = (
    "frame_id", "label", "subject_clause", "checked", "present", "used_avatar", "reason",
)


def frame_id(path: str) -> str:
    """Stable identity for a frame file: the first 16 hex chars of the SHA-256 of its bytes.

    Digesting the CONTENT rather than the filename is what lets a verdict be re-attributed to a
    frame the owner still holds locally while carrying nothing about who is in it.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def normalize_label(value: Optional[str]) -> Optional[str]:
    """Canonical ground-truth label, or None when it is not one of the two the grader knows."""
    label = (value or "").strip().lower()
    return label if label in LABELS else None


def normalize_used_avatar(value: Any) -> str:
    """Canonical ``"true"`` / ``"false"`` / ``"unknown"`` for a manifest's ``used_avatar``.

    `grade()` groups on this value verbatim, so an entry written as the JSON boolean ``true``
    would otherwise bucket as ``"True"`` beside a string ``"true"`` — silently splitting the one
    split the harness exists to make. Anything the grader cannot read is ``"unknown"``, never
    ``"false"``: an unattributed frame is not a fallback render.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value or "").strip().lower()
    if text in ("true", "false"):
        return text
    return "unknown"


def verdict_record(
    entry: Mapping[str, Any],
    verdict: Mapping[str, Any],
    *,
    fid: str,
    include_reason: bool = False,
) -> dict:
    """One publishable row: the human label, the declared clause, and the probe's verdict.

    Args:
        entry: The manifest entry — its ``label`` and the declared attributes it was probed under.
        verdict: What ``probe_avatar_likeness`` returned.
        fid: The frame's content digest from :func:`frame_id`.
        include_reason: Carry the vision model's one-line reason. Off by default: the reason is
            free text a model wrote while looking at a real person, and nothing in the grading
            reads it.

    Returns:
        A dict whose keys are exactly the subset of :data:`RECORD_FIELDS` that applies.
    """
    record = {
        "frame_id": fid,
        "label": normalize_label(entry.get("label")),
        "subject_clause": subject_clause(entry),
        "checked": bool(verdict.get("checked")),
        "present": verdict.get("present"),
        "used_avatar": normalize_used_avatar(entry.get("used_avatar", "true")),
    }
    if include_reason:
        record["reason"] = str(verdict.get("reason") or "")
    return record


def run_eval(
    entries: Iterable[Mapping[str, Any]],
    probe: Callable[..., dict],
    *,
    include_reasons: bool = False,
) -> list[dict]:
    """Probe every manifest entry and return the publishable verdict records.

    Args:
        entries: Manifest entries — each needs a ``frame`` path and a ``label``, plus the
            ``gender_presentation`` / ``age_band`` the frame is to be judged against.
        probe: The probe to call; injected so a test never needs a vision model.
        include_reasons: Passed through to :func:`verdict_record`.

    Returns:
        One record per entry, in manifest order. An entry whose frame cannot be read is skipped
        with a record carrying ``checked=False`` — the same shape the live probe reports for an
        unreadable image, so the unchecked line stays honest.
    """
    records = []
    for entry in entries:
        path = str(entry.get("frame") or "")
        try:
            fid = frame_id(path)
        except OSError:
            records.append({
                "frame_id": "",
                "label": normalize_label(entry.get("label")),
                "subject_clause": subject_clause(entry),
                "checked": False,
                "present": None,
                "used_avatar": normalize_used_avatar(entry.get("used_avatar", "true")),
            })
            continue
        verdict = probe(path, dict(entry))
        records.append(verdict_record(entry, verdict, fid=fid, include_reason=include_reasons))
    return records


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """``numerator / denominator``, or None when the denominator is empty."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _counts(records: Iterable[Mapping[str, Any]]) -> dict:
    """Confusion-matrix counts over one group of records."""
    tally = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0,
             "unchecked": 0, "unlabelled": 0, "total": 0}
    for record in records:
        tally["total"] += 1
        label = normalize_label(record.get("label"))
        if label is None:
            tally["unlabelled"] += 1
            continue
        if not record.get("checked"):
            tally["unchecked"] += 1
            continue
        present = bool(record.get("present"))
        if label == LABEL_PRESENT:
            tally["true_positive" if present else "false_negative"] += 1
        else:
            tally["false_positive" if present else "true_negative"] += 1
    return tally


def _summarize(tally: Mapping[str, int]) -> dict:
    """Counts plus the two rates the hold decision turns on."""
    graded = (tally["true_positive"] + tally["false_positive"]
              + tally["true_negative"] + tally["false_negative"])
    positives = tally["true_positive"] + tally["false_negative"]
    negatives = tally["true_negative"] + tally["false_positive"]
    return {
        **dict(tally),
        "graded": graded,
        # FN / (all frames a human says DO carry the likeness) — the rate the hold flag costs a user.
        "false_negative_rate": _rate(tally["false_negative"], positives),
        # FP / (all frames a human says do NOT) — the rate the hold flag fails to catch.
        "false_positive_rate": _rate(tally["false_positive"], negatives),
        "unchecked_rate": _rate(tally["unchecked"], tally["total"]),
        "sufficient": (graded >= MIN_GRADED and positives >= MIN_PER_CLASS
                       and negatives >= MIN_PER_CLASS),
    }


def _group(records: Iterable[Mapping[str, Any]], key: str) -> dict:
    """Per-value summaries for one record field, ordered by value."""
    buckets: dict[str, list] = defaultdict(list)
    for record in records:
        buckets[str(record.get(key) or "")].append(record)
    return {value: _summarize(_counts(rows)) for value, rows in sorted(buckets.items())}


def grade(records: Iterable[Mapping[str, Any]]) -> dict:
    """Score a set of verdict records into the rates issue #1430 asks the doc to carry.

    Args:
        records: Verdict records as produced by :func:`run_eval` (or replayed from a committed
            fixture — the grader never touches a frame, so a fixture grades identically).

    Returns:
        ``overall`` plus ``by_subject_clause`` and ``by_used_avatar`` breakdowns. The
        ``by_used_avatar`` split is the one the #1431 review demanded: a checked-negative on a
        ``"false"`` frame is the base-Flux fallback carrying no likeness, which is working
        behaviour, so it must never be summed into the LoRA render's error rate.
    """
    rows = list(records)
    return {
        "overall": _summarize(_counts(rows)),
        "by_subject_clause": _group(rows, "subject_clause"),
        "by_used_avatar": _group(rows, "used_avatar"),
    }


def leaks_a_frame_path(records: Iterable[Mapping[str, Any]]) -> bool:
    """True when any record carries a field outside :data:`RECORD_FIELDS`.

    The publish guard. A verdict file is committed to a PUBLIC repo, so anything the grader did not
    put there — a ``frame`` path, a user's name, a source directory — is a leak, and the caller
    refuses to write rather than trimming it silently.
    """
    return any(set(record) - set(RECORD_FIELDS) for record in records)
