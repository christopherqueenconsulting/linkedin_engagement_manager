"""Content-quality telemetry (issue #630 / D6) — the TREND LINE under every quality gate LEM already
has.

Quality was a one-time fix everywhere else: #625's slop lint blocks a bad draft, #617's contract
throws away a bad comment, #382 scores authenticity once. None of them can answer "is the writing
getting worse?" — and it silently can, because the weekly model-retirement cron SWAPS the model
underneath the same prompts. B2B benchmarks put engagement-by-impression at ~3.6% (documents ~6.6%,
plain text ~2%); user 1 sits near 0.5%. A regression that costs half a percent of reach is invisible
in any single draft and obvious in a week of them.

This module is the arithmetic half. Everything here is pure except two clearly-marked I/O helpers
(`similarity_reports`, which spends ONE `lem-embedding` call per batch, and `detector_score`, which is
OFF by default) — the beat tasks own the DB writes and the PostHog emission, exactly as
`comment_outcomes` (#628) and `suppression` (#629) are split.

Two rules run through all of it:

* **Unscored is never zero.** A post with no impressions yet has no engagement rate; a draft the lint
  was disabled for has no slop score; an account with no history has no self-similarity. Each of
  those is None and is excluded from its own denominator. Charting them as 0 would invent a collapse
  (or, for slop, invent a clean week).
* **A regression is measured against the account's OWN prior period**, never an absolute target. The
  floor alert is the one exception, and it is the only one with a benchmark behind it.

The external AI-detector hook is a REGRESSION SIGNAL ONLY, per the #416 policy: nothing here is an
evasion target, no score ever rewrites text, and with no API key configured it is a silent no-op.
"""

import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timedelta
from math import gcd
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from cqc_lem import assets_dir
from cqc_lem.utilities.ai import slop_lint
from cqc_lem.utilities.ai.content_framework import (
    COMMENT_HISTORY_LIMIT,
    MOBILE_HOOK_MAX_CHARS,
    SIMILARITY_MEASURE_EMBEDDING,
    SIMILARITY_MEASURE_LEXICAL,
    SIMILARITY_MEASURE_NONE,
    cosine_similarity,
    embed_comments,
    hook_report,
    text_similarity,
)

SURFACE_POST = "post"
SURFACE_COMMENT = "comment"
SURFACE_NEWSLETTER = "newsletter"
SURFACES: tuple = (SURFACE_POST, SURFACE_COMMENT, SURFACE_NEWSLETTER)

# Aliases, not copies (issue #1265): the generation-time gates grade the SAME post on the same two
# measures, and the trend line is only readable against a hold if both call the measure by one name.
MEASURE_EMBEDDING = SIMILARITY_MEASURE_EMBEDDING
MEASURE_LEXICAL = SIMILARITY_MEASURE_LEXICAL
MEASURE_NONE = SIMILARITY_MEASURE_NONE

ALERT_SLOP_REGRESSION = "slop_regression"
ALERT_ENGAGEMENT_FLOOR = "engagement_floor"
ALERT_SIMILARITY_CREEP = "similarity_creep"

# Video asset vocabulary. Keep these constants in ONE place so the scorer, the DB writer, the
# nightly beat and the weekly rollup all name the same states.
VIDEO_MODEL_PEXELS = "pexels"
# Coarse on purpose: the stored URL proves the asset came out of the Runway path, not WHICH model
# rendered it — nothing persists that per post (issue #1410).
VIDEO_MODEL_RUNWAY = "runway"
VIDEO_PROBE_OK = "ok"
VIDEO_PROBE_MISSING = "missing"
VIDEO_PROBE_EMPTY = "empty"
VIDEO_PROBE_UNREADABLE = "unreadable"

# A HARD violation is what actually blocks a post or drops a comment, so it carries most of the
# weight; WARN checks are advisory (a genuine list of three tools reads like a rule-of-three) and only
# move the score enough to show a drift.
SLOP_HARD_WEIGHT = 3.0
SLOP_WARN_WEIGHT = 1.0

# How much the mean weighted slop score may rise week-over-week before it reads as a regression
# rather than noise. 1.0 is one extra HARD violation every three pieces of content.
DEFAULT_SLOP_REGRESSION_DELTA = 1.0
# Engagement-by-impression floor. Deliberately BELOW the ~3.6% B2B benchmark: this alert exists to
# catch a collapse, and a threshold set at the benchmark would fire permanently for an account that
# is merely below average.
DEFAULT_ENGAGEMENT_FLOOR = 0.02
# Mean self-similarity rise that reads as the writer settling into one template.
DEFAULT_SIMILARITY_REGRESSION_DELTA = 0.05
# No alert fires off a handful of pieces — a week with two posts in it has no trend.
DEFAULT_MIN_ALERT_SAMPLE = 5
# The ENGAGEMENT floor needs its own, smaller minimum, and the reason is arithmetic rather than
# taste: only POSTS carry impressions, and the default cadence is DEFAULT_POSTS_PER_WEEK = 3 a week.
# Requiring 5 posts-with-impressions in a 7-day period would make the floor alert unreachable for
# every account on the default plan — an alert that can never fire is worse than no alert, because it
# reads as "engagement is fine". Three is the default cadence, i.e. a full week of posting.
DEFAULT_MIN_ENGAGEMENT_SAMPLE = 3
DEFAULT_ROLLUP_DAYS = 7
# The nightly pass looks back TWO days, not one. Consecutive nightly runs would tile a 24h window
# perfectly, but two days buys both things a 24h window cannot: a missed night self-heals, and a post
# scored the night it shipped (no stats captured yet, so no engagement rate) is re-scored once the
# 23:00 scrape has run and finally gets its ER. The write is an upsert, so the overlap costs one extra
# reading, never a double count.
DEFAULT_WINDOW_DAYS = 2
# Items scored per user per nightly run. A cap, not a filter: whatever is dropped is reported.
DEFAULT_MAX_ITEMS = 60

# External detector defaults — every one of them exists to stop this from becoming a cost surprise.
DEFAULT_DETECTOR_SAMPLE_RATE = 0.1
DEFAULT_DETECTOR_DAILY_MAX = 5
DEFAULT_DETECTOR_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Config. Read at CALL time (the POST_SIMILARITY_MAX live-env pattern) so ops can retune a threshold
# without a restart.
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def content_quality_enabled() -> bool:
    """CONTENT_QUALITY_TELEMETRY_ENABLED=false restores the exact pre-#630 behaviour (no scoring, no
    events, no rows).
    """
    return _env_flag("CONTENT_QUALITY_TELEMETRY_ENABLED", True)


def rollup_days() -> int:
    """Length of ONE reporting period.

    The window a rollup averages over, and the same width the prior period is measured across when
    `evaluate_alerts` looks for a regression — callers wanting both periods read
    `rollup_days() * 2` of scores.
    """
    return max(1, _env_int("CONTENT_QUALITY_ROLLUP_DAYS", DEFAULT_ROLLUP_DAYS))


def window_days() -> int:
    """How far back the NIGHTLY scoring pass reads shipped content."""
    return max(1, _env_int("CONTENT_QUALITY_WINDOW_DAYS", DEFAULT_WINDOW_DAYS))


def max_items_per_run() -> int:
    """Ceiling on items scored per user per nightly run.

    A cap, not a filter: what it drops is reported rather than silently skipped, and the next run's
    overlapping window picks it up.
    """
    return max(1, _env_int("CONTENT_QUALITY_MAX_ITEMS", DEFAULT_MAX_ITEMS))


def slop_regression_delta() -> float:
    """How far the mean weighted slop score may RISE against the account's own prior period.

    `ALERT_SLOP_REGRESSION` fires AT this delta, not past it — the comparison is `>=`. Never
    negative, so a mistyped threshold cannot make every period look like a regression.
    """
    return max(0.0, _env_float("CONTENT_QUALITY_SLOP_REGRESSION_DELTA",
                               DEFAULT_SLOP_REGRESSION_DELTA))


def engagement_floor() -> float:
    """The configurable ER-per-impression floor. Clamped to a real 0-1 share so a misconfigured `2`
    (meaning percent) can't make every account look collapsed.
    """
    return min(1.0, max(0.0, _env_float("CONTENT_QUALITY_ENGAGEMENT_FLOOR",
                                        DEFAULT_ENGAGEMENT_FLOOR)))


def similarity_regression_delta() -> float:
    """Rise in mean self-similarity that reads as the writer settling into one template.

    Fires `ALERT_SIMILARITY_CREEP`, and only between periods graded on the SAME measure — the
    embedding and lexical scales are not interchangeable.
    """
    return max(0.0, _env_float("CONTENT_QUALITY_SIMILARITY_DELTA",
                               DEFAULT_SIMILARITY_REGRESSION_DELTA))


def min_alert_sample() -> int:
    """Scored PIECES required on BOTH sides of a comparison before a regression alert may fire.

    Counts posts + comments + editions together. A false alert nobody can act on trains the owner to
    ignore the next one, so a thin period reports its numbers and raises nothing.
    """
    return max(1, _env_int("CONTENT_QUALITY_MIN_SAMPLE", DEFAULT_MIN_ALERT_SAMPLE))


def min_engagement_sample() -> int:
    """Minimum posts-with-impressions before the ER floor may fire. Defaults to the weekly posting
    cadence rather than to `min_alert_sample()`, which counts every scored PIECE (posts + comments +
    editions) and is therefore unreachable for a dimension only posts can contribute to. Never
    higher than the general minimum, so lowering `CONTENT_QUALITY_MIN_SAMPLE` still lowers this.
    """
    raw = (os.environ.get("CONTENT_QUALITY_MIN_ER_SAMPLE") or "").strip()
    if raw:
        return max(1, _env_int("CONTENT_QUALITY_MIN_ER_SAMPLE", DEFAULT_MIN_ENGAGEMENT_SAMPLE))
    return max(1, min(DEFAULT_MIN_ENGAGEMENT_SAMPLE, min_alert_sample()))


def detector_enabled() -> bool:
    """The external AI-detector pass is OFF unless BOTH the flag and an API key are set. A missing key
    is a silent no-op, never a warning loop — this is an optional regression signal, so its absence is
    the normal state.
    """
    return _env_flag("AI_DETECTOR_ENABLED", False) and bool(
        (os.environ.get("AI_DETECTOR_API_KEY") or "").strip())


def detector_provider() -> str:
    """Name recorded alongside a detector reading, so a score is attributable to what produced it.

    Swapping detector services must not read as a quality move. Clamped to 32 chars to match the
    `content_quality_scores.detector_provider` column, and never empty.
    """
    return (os.environ.get("AI_DETECTOR_PROVIDER") or "generic").strip()[:32] or "generic"


def detector_sample_rate() -> float:
    """Share of items (0-1) that get a paid external detector reading.

    Clamped into that range, so a misconfigured `10` (meaning percent) cannot bill for every piece
    of content.
    """
    return min(1.0, max(0.0, _env_float("AI_DETECTOR_SAMPLE_RATE", DEFAULT_DETECTOR_SAMPLE_RATE)))


def detector_daily_max() -> int:
    """Hard cap on detector calls per user per run — the cost ceiling. 0 disables the pass."""
    return max(0, _env_int("AI_DETECTOR_DAILY_MAX", DEFAULT_DETECTOR_DAILY_MAX))


# ---------------------------------------------------------------------------
# Per-item scoring
# ---------------------------------------------------------------------------

def slop_severity_score(report: Optional[Mapping[str, Any]]) -> Optional[float]:
    """One weighted number for a lint report: HARD violations count triple. None when the lint did not
    run for this surface (`checked` False) — a disabled lint has no score, and recording 0.0 would
    read as a clean week.
    """
    report = dict(report or {})
    if not report.get("checked"):
        return None
    hard = len(report.get("hard") or [])
    warn = len(report.get("warnings") or [])
    return round(hard * SLOP_HARD_WEIGHT + warn * SLOP_WARN_WEIGHT, 3)


def _norm(text: Optional[str]) -> str:
    return " ".join(str(text or "").split()).lower()


def similarity_reports(texts: Sequence[str], history: Optional[Iterable[str]] = None) -> list:
    """Self-similarity of each text against the account's recent history — ONE `lem-embedding` call
    for the whole batch (draft texts + history together), because a call per item is what makes this
    kind of telemetry too expensive to keep running.

    Prefers embedding cosine (it catches the same piece reworded, which is what a template produces)
    and degrades to deterministic token overlap when the embedding endpoint is unavailable — the
    measure is recorded alongside the score so an embedding week and a lexical week are never averaged
    together as if they were the same scale.

    An item's OWN text is dropped from its history (a shipped comment is already in the log it is
    being compared against, and grading it against itself would score every item 1.0). With nothing
    left to compare against, the score is None: not measured, not "unique".
    """
    items = [str(t or "") for t in (texts or [])]
    pool: list = []
    seen: set = set()
    for entry in (history or []):
        flat = str(entry or "").strip()
        key = _norm(flat)
        if not flat or key in seen:
            continue
        seen.add(key)
        pool.append(flat)
        if len(pool) >= COMMENT_HISTORY_LIMIT:
            break

    empty = {"score": None, "measure": MEASURE_NONE, "match": None}
    if not items:
        return []
    if not pool or not all(item.strip() for item in items):
        # Any empty body would make the batch embedding call fail anyway; grade lexically below.
        vectors = None
    else:
        vectors = embed_comments(items + pool)

    reports = []
    for index, item in enumerate(items):
        # `own` is by text, not by id: the same comment can appear in the history under a different
        # log row, and a self-match would silently report a perfect duplicate.
        own = _norm(item)
        candidates = [(pos, cand) for pos, cand in enumerate(pool) if _norm(cand) != own]
        if not item.strip() or not candidates:
            reports.append(dict(empty))
            continue
        if vectors:
            scores = [(cosine_similarity(vectors[index], vectors[len(items) + pos]), cand)
                      for pos, cand in candidates]
            measure = MEASURE_EMBEDDING
        else:
            scores = [(text_similarity(item, cand), cand) for _, cand in candidates]
            measure = MEASURE_LEXICAL
        best_score, best_match = max(scores, key=lambda pair: pair[0])
        reports.append({"score": round(best_score, 4), "measure": measure, "match": best_match})
    return reports


def stable_fraction(key: str) -> float:
    """A stable 0-1 draw for a string key. Used for detector sampling so a retried nightly run picks
    the SAME items and never re-bills for a second reading of the same piece of content.
    """
    digest = hashlib.sha1(str(key or "").encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) / float(0xFFFFFFFF)


def detector_sampled(surface: str, ref_id: Any) -> bool:
    """Whether this item is in the detector sample. False whenever the pass is off, so callers need no
    second guard.
    """
    if not detector_enabled() or detector_daily_max() <= 0:
        return False
    rate = detector_sample_rate()
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return stable_fraction(f"{surface}:{ref_id}") < rate


def detector_score(text: Optional[str]) -> Optional[float]:
    """Optional external AI-detector reading, 0-1 (higher = more machine-like), or None.

    A REGRESSION SIGNAL ONLY (#416): the number is recorded next to the deterministic scores so a
    prompt/model change that makes the writing more machine-like shows up, and it NEVER holds, edits
    or steers content. Any failure — no key, no endpoint, timeout, unparseable body, a missing
    `requests` install — returns None quietly, because an optional signal must never be able to break
    the nightly job.
    """
    body = str(text or "").strip()
    if not body or not detector_enabled():
        return None
    url = (os.environ.get("AI_DETECTOR_URL") or "").strip()
    if not url:
        return None
    try:
        import requests  # local: the detector is optional, so nothing imports it at module load

        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {(os.environ.get('AI_DETECTOR_API_KEY') or '').strip()}",
                     "Content-Type": "application/json"},
            json={"text": body[:5000]},
            timeout=_env_int("AI_DETECTOR_TIMEOUT", DEFAULT_DETECTOR_TIMEOUT),
        )
        response.raise_for_status()
        payload = response.json() or {}
    except Exception as exc:
        from cqc_lem.utilities.logger import log_warning
        log_warning("AI-detector scoring unavailable — quality telemetry continues without it",
                    exc=exc, api_provider=detector_provider())
        return None
    for field in ("ai_score", "score", "fake_probability", "ai_probability"):
        value = payload.get(field) if isinstance(payload, Mapping) else None
        if value is None:
            continue
        try:
            return round(min(1.0, max(0.0, float(value))), 4)
        except (TypeError, ValueError):
            return None
    return None


def _asset_file_name(video_url: Optional[str]) -> Optional[str]:
    """The `file_name=` value of a LEM `/api/assets?file_name=...` URL, or None.

    Both the path resolver and the model-tier reader need it, and they must agree: a URL one of
    them treats as ours while the other does not would score an asset it cannot find.
    """
    url = str(video_url or "").strip()
    if not url:
        return None
    query = parse_qs(urlparse(url).query)
    return (query.get("file_name") or [None])[0] or None


def _aspect_ratio_from_dimensions(width: Any, height: Any) -> Optional[str]:
    """Reduce probed pixel dimensions to the project's friendly ratio vocabulary.

    Renders do not come back at exactly the nominal ratio (Runway's 9:16 lands as 1088x1920, whose
    exact reduction is 17:30), so a probed ratio is snapped to the nearest `RATIO_ALIASES` key
    within 2% and only falls back to the exact reduction when nothing matches — otherwise the
    dimension would report a different string every render and trend nothing.
    """
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    # Imported here, not at module scope: video_models pulls in the RunwayML SDK, and this module is
    # loaded by the nightly beat for text surfaces that never touch video.
    from cqc_lem.utilities.ai.video_models import RATIO_ALIASES

    measured = w / h
    for alias in RATIO_ALIASES:
        aw, ah = alias.split(":")
        if abs(measured - (int(aw) / int(ah))) <= 0.02 * measured:
            return alias
    divisor = gcd(w, h) or 1
    return f"{w // divisor}:{h // divisor}"[:16]


def resolve_local_video_path(video_url: Optional[str]) -> Optional[str]:
    """Turn a LEM `/api/assets?file_name=...` URL into an on-disk path under `assets_dir`.

    Only URLs whose query path lives under `videos/` are resolved; anything else (external URL,
    missing query, path escape) returns None so the probe reports "missing" rather than touching
    arbitrary files. This is a read-only lookup: the file may or may not exist.
    """
    file_name = _asset_file_name(video_url)
    if not file_name:
        return None
    # Containment: the stored asset root is `assets_dir`; anything outside it is not ours. Both
    # sides are absolute and case-preserving on purpose — comparing an absolute path against a
    # possibly-relative `assets_dir`, or lowercasing only one side, rejects every real asset and
    # the probe then reports a healthy video as "missing".
    root = os.path.abspath(assets_dir)
    local_path = os.path.abspath(os.path.join(root, file_name))
    if not local_path.startswith(root + os.sep):
        return None
    if not local_path.startswith(os.path.join(root, "videos") + os.sep):
        return None
    return local_path


def probe_video_asset(path: Optional[str]) -> dict:
    """Probe a local video file for duration and a readable video stream.

    Returns a dict with `duration_seconds`, `aspect_ratio`, `asset_probe` and `has_video_stream`.
    The ratio is read from the file rather than from the render request: what LinkedIn autoplays is
    the asset on disk, and a render that came back at a different ratio than it was asked for is
    exactly the regression this dimension exists to catch. Missing, empty or unreadable files
    report None for duration and a named probe state rather than raising, because a nightly
    telemetry pass must not break on one bad file.
    """
    result = {"duration_seconds": None, "aspect_ratio": None,
              "asset_probe": VIDEO_PROBE_MISSING, "has_video_stream": False}
    file_path = str(path or "").strip()
    if not file_path:
        return result
    if not os.path.exists(file_path):
        return result
    if os.path.getsize(file_path) == 0:
        result["asset_probe"] = VIDEO_PROBE_EMPTY
        return result
    try:
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-show_entries", "stream=codec_type,width,height", "-of", "json", file_path],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if output.returncode != 0:
            result["asset_probe"] = VIDEO_PROBE_UNREADABLE
            return result
        payload = output.stdout or "{}"
        data = json.loads(payload)
        streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        result["has_video_stream"] = bool(video_streams)
        if video_streams:
            result["aspect_ratio"] = _aspect_ratio_from_dimensions(
                video_streams[0].get("width"), video_streams[0].get("height"))
        duration = data.get("format", {}).get("duration")
        if duration is not None:
            try:
                result["duration_seconds"] = round(float(duration))
            except (TypeError, ValueError):
                # ffprobe reports "N/A" for a container with no readable duration; leave
                # duration_seconds None so an unmeasured video is never recorded as zero.
                pass
        result["asset_probe"] = VIDEO_PROBE_OK if result["has_video_stream"] else VIDEO_PROBE_UNREADABLE
    except Exception:
        result["asset_probe"] = VIDEO_PROBE_UNREADABLE
    return result


def video_model_tier(model: Optional[str], video_url: Optional[str] = None) -> Optional[str]:
    """Normalize a model identifier to the tier recorded in telemetry.

    Known Runway keys from `video_models.VIDEO_MODELS` are passed through; Pexels stock is
    named explicitly; an empty/unknown model with no URL is None; an unrecognized model is
    preserved as-is so it is not silently rewritten.

    With no `model` in hand the tier is read off the STORED asset, and the file NAME decides it
    before the directory does: every stored `posts.video_url` is written under `videos/runwayml/`
    whatever produced it (`_store_video_asset`), so a stock clip landing there would otherwise be
    recorded as a Runway render. Only `pexels_*` proves stock, so that check comes first.
    `VIDEO_MODEL_RUNWAY` is deliberately coarse — which Runway model rendered a post is not
    persisted anywhere (issue #1410).
    """
    model = str(model or "").strip()
    if model:
        return model
    file_name = _asset_file_name(video_url) or ""
    if os.path.basename(file_name).startswith("pexels_"):
        return VIDEO_MODEL_PEXELS
    url = str(video_url or "").strip()
    if "videos/pexels" in url:
        return VIDEO_MODEL_PEXELS
    if "videos/runwayml" in url:
        return VIDEO_MODEL_RUNWAY
    return None


def score_video_asset(*, video_url: Optional[str], model: Optional[str] = None,
                      ratio: Optional[str] = None) -> dict:
    """Score the video-specific dimensions of ONE shipped video post.

    Pure except for the local file probe, which is bounded by a timeout and never raises.
    `render_ok` is True only when the post has a reachable video asset with a readable video
    stream; `model_tier`, `duration_seconds`, `aspect_ratio` and `asset_probe` are recorded
    alongside it so a regression in any one of them can be trended.

    `ratio` is the ratio the render was ASKED for, which only a caller holding the render request
    has; the nightly beat scores a post that shipped days ago and passes none. So the probed ratio
    is the default and an explicit `ratio` overrides it — without that the dimension would be NULL
    on every row the beat writes.
    """
    path = resolve_local_video_path(video_url)
    probe = probe_video_asset(path)
    render_ok = bool(path and os.path.exists(path) and probe["has_video_stream"])
    aspect = (str(ratio or "").strip()[:16]) or probe["aspect_ratio"]
    return {
        "video_render_ok": render_ok,
        "video_model_tier": video_model_tier(model, video_url),
        "video_duration_seconds": probe["duration_seconds"],
        "video_aspect_ratio": aspect,
        "video_asset_probe": probe["asset_probe"],
    }


def score_item(*, surface: str, ref_id: Any, text: Optional[str], shipped_on: Any,
               format_key: Optional[str] = None, authenticity: Optional[int] = None,
               similarity: Optional[Mapping[str, Any]] = None,
               engagement_rate: Optional[float] = None, impressions: Optional[int] = None,
               exempt_keyword: Optional[str] = None,
               detector: Optional[float] = None,
               video: Optional[Mapping[str, Any]] = None) -> dict:
    """Score ONE shipped piece of content. Pure — the lint and the hook grader are both deterministic,
    and every network-backed input (similarity, authenticity, ER, detector) is passed in by the
    caller so this stays unit-testable and always agrees with itself.

    `authenticity` is #382's STORED score (`posts.authenticity_score`), not a fresh judge call: the
    gate already paid for it at generation time, and re-judging every shipped piece nightly would add
    an LLM call per item for a number that cannot have changed. Surfaces with no stored score report
    None rather than a guess.
    """
    body = str(text or "")
    lint = slop_lint.lint_report(body, surface, exempt_keyword=exempt_keyword)
    hook = hook_report(body, surface, format_key=format_key)
    sim = dict(similarity or {})
    checked = bool(lint.get("checked"))
    return {
        "surface": surface,
        "ref_id": str(ref_id),
        "shipped_on": shipped_on,
        "chars": len(body),
        "slop_checked": checked,
        "slop_hard": len(lint.get("hard") or []) if checked else None,
        "slop_warn": len(lint.get("warnings") or []) if checked else None,
        "slop_score": slop_severity_score(lint),
        "slop_checks": [v.get("check") for v in (lint.get("violations") or [])
                        if isinstance(v, dict) and v.get("check")],
        "slop_reasons": lint.get("reasons") or [],
        "similarity": sim.get("score"),
        "similarity_measure": sim.get("measure") or MEASURE_NONE,
        "authenticity_score": int(authenticity) if authenticity is not None else None,
        "hook_chars": hook.get("chars"),
        "hook_within_budget": hook.get("within_mobile_budget"),
        "hook_budget": MOBILE_HOOK_MAX_CHARS,
        "engagement_rate": engagement_rate,
        "impressions": impressions,
        "detector_score": detector,
        "detector_provider": detector_provider() if detector is not None else None,
        **dict(video or {}),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _floats(rows: Iterable[Mapping[str, Any]], field: str) -> list:
    values = []
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _mean(values: Sequence[float], digits: int = 4) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), digits)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def summarize_scores(rows: Optional[Iterable[Mapping[str, Any]]]) -> dict:
    """Aggregate scored rows into the quality picture for one period.

    Every dimension carries its OWN sample size because they are measured on different subsets: slop
    is scored for every piece the lint ran on, self-similarity only where a history existed,
    authenticity only for posts, and engagement rate only where impressions were captured. One shared
    denominator would let a dimension nobody could measure drag the others down.

    `engagement_rate` is impression-WEIGHTED (total engagement / total impressions), not a mean of
    per-post rates: a post seen by 50 people must not count the same as one seen by 5,000.
    """
    rows = [dict(row) for row in (rows or []) if row]
    by_surface: dict = {}
    for row in rows:
        surface = str(row.get("surface") or "")
        by_surface[surface] = by_surface.get(surface, 0) + 1

    slop_rows = [row for row in rows if row.get("slop_score") is not None]
    slop_scores = _floats(slop_rows, "slop_score")
    hard_hits = sum(1 for row in slop_rows if float(row.get("slop_hard") or 0) > 0)
    warn_hits = sum(1 for row in slop_rows if float(row.get("slop_warn") or 0) > 0)

    # Which measure produced each score, because the two are NOT the same scale — cosine and
    # token-overlap ceilings differ by a wide margin (0.82 vs 0.55), so a week that fell back to
    # lexical is not comparable to an embedding week and must not be read as a move.
    measures: dict = {}
    for row in rows:
        if row.get("similarity") is None:
            continue
        measure = str(row.get("similarity_measure") or MEASURE_NONE)
        measures[measure] = measures.get(measure, 0) + 1
    dominant = max(measures, key=lambda key: measures[key]) if measures else None
    # The mean is taken over the dominant measure ONLY. Mixing is not just a cross-period risk: each
    # surface embeds in its own batch, so one failed `lem-embedding` call drops that surface to
    # lexical while the rest of the period stays cosine. Averaging both would move `similarity_avg`
    # by the gap between the scales — a swing the cross-period guard would then wave through, because
    # both periods still report the same dominant label.
    sim_scores = _floats([row for row in rows
                          if str(row.get("similarity_measure") or MEASURE_NONE) == dominant],
                         "similarity") if dominant else []

    auth_scores = _floats(rows, "authenticity_score")
    detector_scores = _floats(rows, "detector_score")

    hook_rows = [row for row in rows if row.get("hook_within_budget") is not None]
    hook_ok = sum(1 for row in hook_rows if bool(row.get("hook_within_budget")))

    engagement = 0.0
    impressions = 0
    er_sample = 0
    for row in rows:
        rate, views = row.get("engagement_rate"), row.get("impressions")
        if rate is None or views is None:
            continue
        try:
            rate, views = float(rate), int(views)
        except (TypeError, ValueError):
            continue
        if views <= 0:
            continue
        engagement += rate * views
        impressions += views
        er_sample += 1

    return {
        "items": len(rows),
        "by_surface": by_surface,
        "slop_sample": len(slop_scores),
        "slop_score_avg": _mean(slop_scores, 3),
        "slop_hard_rate": _rate(hard_hits, len(slop_rows)),
        "slop_warn_rate": _rate(warn_hits, len(slop_rows)),
        "slop_hard_total": int(sum(_floats(slop_rows, "slop_hard"))),
        "similarity_sample": len(sim_scores),
        "similarity_avg": _mean(sim_scores),
        "similarity_max": round(max(sim_scores), 4) if sim_scores else None,
        "similarity_measure": dominant,
        "similarity_measures": measures,
        "authenticity_sample": len(auth_scores),
        "authenticity_avg": _mean(auth_scores, 1),
        "hook_sample": len(hook_rows),
        "hook_chars_avg": _mean(_floats(hook_rows, "hook_chars"), 1),
        "hook_budget_rate": _rate(hook_ok, len(hook_rows)),
        "hook_budget": MOBILE_HOOK_MAX_CHARS,
        "engagement_rate_sample": er_sample,
        "engagement_rate": round(engagement / impressions, 6) if impressions > 0 else None,
        "impressions": impressions if er_sample else None,
        "detector_sample": len(detector_scores),
        "detector_avg": _mean(detector_scores),
    }


def _day(value: Any) -> Optional[date]:
    # datetime BEFORE date: datetime is a subclass of date, and returning one unchanged would make the
    # window comparison below raise on `date <= datetime`.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def split_periods(rows: Optional[Iterable[Mapping[str, Any]]], days: int,
                  today: Optional[date] = None) -> tuple:
    """Split scored rows into (current, prior) windows of `days` each, ending today. Rows outside both
    windows — or with an unreadable `shipped_on` — are dropped rather than folded into the nearest
    period, so a stale row can never move a regression verdict.
    """
    days = max(1, int(days))
    today = today or date.today()
    current_start = today - timedelta(days=days - 1)
    prior_start = current_start - timedelta(days=days)
    current, prior = [], []
    for row in rows or []:
        when = _day((row or {}).get("shipped_on"))
        if when is None:
            continue
        if current_start <= when <= today:
            current.append(row)
        elif prior_start <= when < current_start:
            prior.append(row)
    return current, prior


def _delta(current: Optional[float], prior: Optional[float], digits: int = 4) -> Optional[float]:
    """Current - prior, or None when either side was never measured. None is load-bearing: 'we have
    no baseline' must not read as 'no change'.
    """
    if current is None or prior is None:
        return None
    return round(float(current) - float(prior), digits)


def evaluate_alerts(current: Mapping[str, Any], prior: Mapping[str, Any]) -> list:
    """The three regression conditions, in the order they cost the user money.

    Each needs a real sample on BOTH sides before it can fire (`min_alert_sample`) — a week with two
    posts in it has no trend, and a false alert that pauses nobody still trains the owner to ignore
    the next one. The engagement floor is the exception twice over: it needs no prior period (an
    account below the floor is below it regardless of last week) and it counts against
    `min_engagement_sample`, because impressions come from posts alone.
    """
    current, prior = dict(current or {}), dict(prior or {})
    minimum = min_alert_sample()
    alerts = []

    slop_delta = _delta(current.get("slop_score_avg"), prior.get("slop_score_avg"), 3)
    slop_threshold = slop_regression_delta()
    if (slop_delta is not None and slop_delta >= slop_threshold
            and int(current.get("slop_sample") or 0) >= minimum
            and int(prior.get("slop_sample") or 0) >= minimum):
        alerts.append({
            "name": ALERT_SLOP_REGRESSION,
            "metric": "slop_score_avg",
            "current": current.get("slop_score_avg"),
            "prior": prior.get("slop_score_avg"),
            "delta": slop_delta,
            "threshold": slop_threshold,
            "sample": int(current.get("slop_sample") or 0),
            "reason": (f"AI-slop score rose {slop_delta:+.2f} to "
                       f"{current.get('slop_score_avg')} across "
                       f"{current.get('slop_sample')} pieces (max rise {slop_threshold:.2f}) — "
                       f"check for a model or prompt change"),
        })

    floor = engagement_floor()
    rate = current.get("engagement_rate")
    # Its OWN minimum: `minimum` counts scored PIECES, and only posts carry impressions, so a
    # piece-count threshold would gate a post-only dimension on comment volume it can never reach.
    er_minimum = min_engagement_sample()
    if (rate is not None and rate < floor
            and int(current.get("engagement_rate_sample") or 0) >= er_minimum):
        alerts.append({
            "name": ALERT_ENGAGEMENT_FLOOR,
            "metric": "engagement_rate",
            "current": rate,
            "prior": prior.get("engagement_rate"),
            "delta": _delta(rate, prior.get("engagement_rate"), 6),
            "threshold": floor,
            "sample": int(current.get("engagement_rate_sample") or 0),
            "reason": (f"Engagement per impression is {rate:.2%} across "
                       f"{current.get('engagement_rate_sample')} post(s) with impressions — "
                       f"below the {floor:.2%} floor"),
        })

    sim_delta = _delta(current.get("similarity_avg"), prior.get("similarity_avg"))
    sim_threshold = similarity_regression_delta()
    # Both periods must have been graded by the SAME measure. A week the embedding endpoint was down
    # scores on the token-overlap scale, and comparing that against a cosine week would read as a
    # large move in whichever direction the scales happen to differ.
    same_measure = (current.get("similarity_measure") is not None
                    and current.get("similarity_measure") == prior.get("similarity_measure"))
    if (sim_delta is not None and sim_delta >= sim_threshold and same_measure
            and int(current.get("similarity_sample") or 0) >= minimum
            and int(prior.get("similarity_sample") or 0) >= minimum):
        alerts.append({
            "name": ALERT_SIMILARITY_CREEP,
            "metric": "similarity_avg",
            "current": current.get("similarity_avg"),
            "prior": prior.get("similarity_avg"),
            "delta": sim_delta,
            "threshold": sim_threshold,
            "sample": int(current.get("similarity_sample") or 0),
            "reason": (f"Self-similarity rose {sim_delta:+.3f} to "
                       f"{current.get('similarity_avg')} across "
                       f"{current.get('similarity_sample')} pieces (max rise {sim_threshold:.3f}) — "
                       f"the writing is converging on one template"),
        })
    return alerts


def quality_rollup(rows: Optional[Iterable[Mapping[str, Any]]], days: Optional[int] = None,
                   today: Optional[date] = None) -> dict:
    """The ONE shape shared by the weekly PostHog event, the analytics endpoint and the dashboard
    panel — so the number the user reads and the number that raised the alert can never diverge.
    """
    days = rollup_days() if days is None else max(1, int(days))
    current_rows, prior_rows = split_periods(rows, days, today=today)
    current = summarize_scores(current_rows)
    prior = summarize_scores(prior_rows)
    alerts = evaluate_alerts(current, prior)
    return {
        "days": days,
        "current": current,
        "prior": prior,
        "deltas": {
            "slop_score_avg": _delta(current.get("slop_score_avg"), prior.get("slop_score_avg"), 3),
            "similarity_avg": _delta(current.get("similarity_avg"), prior.get("similarity_avg")),
            "authenticity_avg": _delta(current.get("authenticity_avg"), prior.get("authenticity_avg"), 1),
            "engagement_rate": _delta(current.get("engagement_rate"), prior.get("engagement_rate"), 6),
            "hook_budget_rate": _delta(current.get("hook_budget_rate"), prior.get("hook_budget_rate")),
        },
        "alerts": alerts,
        "config": {
            "engagement_floor": engagement_floor(),
            "slop_regression_delta": slop_regression_delta(),
            "similarity_delta": similarity_regression_delta(),
            "min_sample": min_alert_sample(),
            "min_engagement_sample": min_engagement_sample(),
        },
    }
