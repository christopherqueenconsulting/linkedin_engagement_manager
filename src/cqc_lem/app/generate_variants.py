"""Generate a few image/video variants for human review — without mutating any post.

Backs the `POST /api/admin/generate-media-variants` endpoint and a local CLI. Each
variant runs the same source text through the (profile-aligned) image prompt → image
→ motion prompt → video pipeline, saves the assets under
``assets/variants/<batch_id>/``, and returns public ``/api/assets`` URLs plus a cost
estimate so the user can approve/reject the look before it goes to production.
"""
import argparse
import json
import os
import shutil
import time
from typing import Optional
from uuid import uuid4

from cqc_lem import assets_dir
from cqc_lem.utilities.ai.ai_helper import (
    create_runway_video,
    generate_flux1_image_from_prompt,
    generate_post_image,
    get_flux_image_prompt_from_ai,
    get_runway_ml_video_prompt_from_ai,
)
from cqc_lem.utilities.ai.video_models import RATIO_ALIASES, estimate_video_cost, supports_audio
from cqc_lem.utilities.db import (
    get_post_content,
    get_user_content_language,
    record_shipped_variant as _db_record_shipped_variant,
)
from cqc_lem.utilities.env_constants import (
    API_URL_FINAL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_VIDEO_MODEL,
    DEFAULT_VIDEO_RATIO,
)
from cqc_lem.utilities.geocoding import DEFAULT_CONTENT_LANGUAGE
from cqc_lem.utilities.linkedin.helper import load_profile_for_user
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.utils import create_folder_if_not_exists, save_video_url_to_dir

# Default comparison matrix: all Gen-4 Turbo, square 1:1, 3 variants
# (flux-dev baseline, flux-1.1-pro, flux-1.1-pro w/ alt seed for variety).
DEFAULT_COMBOS = [
    {"image_model": "black-forest-labs/flux-dev", "video_model": "gen4_turbo", "ratio": "1:1"},
    {"image_model": "black-forest-labs/flux-1.1-pro", "video_model": "gen4_turbo", "ratio": "1:1"},
    {"image_model": "black-forest-labs/flux-1.1-pro", "video_model": "gen4_turbo", "ratio": "1:1", "seed": 42},
]

# Rough per-image cost estimates (USD) for the cost preview.
_IMAGE_COST = {
    "black-forest-labs/flux-dev": 0.025,
    "black-forest-labs/flux-1.1-pro": 0.04,
}

_RES_TO_ASPECT = {v: k for k, v in RATIO_ALIASES.items()}


def _image_aspect_ratio(ratio: str) -> str:
    """Map a combo ratio to a Replicate aspect-ratio string.

    Accepts either the friendly form ("1:1") or a Runway resolution ("960:960").
    """
    if ratio in RATIO_ALIASES:
        return ratio
    return _RES_TO_ASPECT.get(ratio, "1:1")


def _image_cost(image_model: str) -> float:
    return _IMAGE_COST.get(image_model, 0.03)


def combo_key(combo: dict) -> str:
    """Stable A/B identity for a variant combo — the key realized `post_stats` are attributed to
    (issue #396). Combos that render the same look collapse to the same key; ``seed`` and the
    video-off case are only encoded when they actually differ the output.
    """
    image = combo.get("image_model", DEFAULT_IMAGE_MODEL)
    include_video = combo.get("include_video", True)
    video = combo.get("video_model", DEFAULT_VIDEO_MODEL) if include_video else "none"
    # Collapse a Runway resolution alias (e.g. "960:960") to its friendly aspect ("1:1")
    # so equivalent ratios don't fragment outcome attribution into separate keys.
    ratio = combo.get("ratio", DEFAULT_VIDEO_RATIO)
    ratio = _RES_TO_ASPECT.get(ratio, ratio)
    key = f"{image}|{video}|{ratio}"
    seed = combo.get("seed")
    if seed is not None:
        key += f"|seed={seed}"
    return key


def record_shipped_variant(user_id: int, post_id: int, combo: dict, *,
                           batch_id: Optional[str] = None, variant_index: Optional[int] = None) -> bool:
    """Record which variant `combo` actually shipped for `post_id` so its outcome can be attributed
    later (issue #396 / D2). Derives the stable `combo_key` and persists via db; returns False on
    any DB error rather than raising, so a shipping path is never broken by tracking.

    This is also the exposure moment for the PostHog side of the same experiment (issue #652): the
    combo that shipped IS the arm, so it is reported as one rather than looked up from a flag, and the
    post's `post_outcome` event carries the same variant. The homegrown ranking
    (`post_stats.select_variant_winners`) is untouched — PostHog gets the stats engine, the ranking
    keeps its recency weighting. The exposure follows the DB write, not the other way round: the arm
    a `post_outcome` event carries is read back out of `post_variants`, so an exposure for a row that
    never landed would be an enrolment the metric can never cover.
    """
    key = combo_key(combo)
    recorded = _db_record_shipped_variant(
        user_id, post_id, key, combo=combo,
        batch_id=batch_id, variant_index=variant_index)
    if recorded:
        _track_shipped_variant_exposure(user_id, post_id, key)
    return recorded


def _track_shipped_variant_exposure(user_id: Optional[int], post_id: Optional[int],
                                    key: str) -> None:
    """Best-effort PostHog exposure for the shipped variant. Never raises: analytics can lose an
    exposure, a post cannot lose its variant record.
    """
    try:
        from cqc_lem.utilities.experiments import POST_MEDIA_VARIANT, track_shipped_variant
        track_shipped_variant(POST_MEDIA_VARIANT, key, user_id=user_id, post_id=post_id)
    except Exception as e:
        log_warning("Shipped-variant experiment exposure skipped", exc=e)


def _public_url(batch_id: str, file_name: str) -> str:
    return f"{API_URL_FINAL}/api/assets?file_name=variants/{batch_id}/{file_name}"


def _sign_best_effort(file_path: str) -> None:
    try:
        from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
        add_ai_content_credentials(file_path)
    except Exception as e:  # never let provenance break variant generation
        log_warning("C2PA signing skipped for variant asset", exc=e)


def _generate_one_variant(idx: int, combo: dict, source_text: str, profile, user_id: Optional[int],
                          batch_dir: str, batch_id: str, post_id: Optional[int] = None) -> dict:
    image_model = combo.get("image_model", DEFAULT_IMAGE_MODEL)
    video_model = combo.get("video_model", DEFAULT_VIDEO_MODEL)
    ratio = combo.get("ratio", DEFAULT_VIDEO_RATIO)
    duration = int(combo.get("duration", 5))
    seed = combo.get("seed")
    include_video = combo.get("include_video", True)
    img_ratio = _image_aspect_ratio(ratio)

    normalized_combo = {
        "image_model": image_model, "video_model": video_model, "ratio": ratio,
        "duration": duration, "seed": seed, "include_video": include_video,
    }
    result = {
        "combo": normalized_combo, "variant_key": combo_key(normalized_combo),
        "image_url": None, "video_url": None,
        "image_prompt": "", "video_prompt": None,
        "estimated_cost_usd": 0.0, "error": None,
    }

    # 1. Image prompt + image
    # The declared subject clause has to reach the prompt author, not just the Replicate call —
    # otherwise the LLM invents a person for the LoRA to contradict (issue #744).
    from cqc_lem.utilities.avatar.guardrails import AVATAR_SURFACE_POST_IMAGE, resolve_avatar_for
    avatar = resolve_avatar_for(user_id, surface=AVATAR_SURFACE_POST_IMAGE, post_id=post_id)
    image_prompt = get_flux_image_prompt_from_ai(source_text, profile=profile, ratio=img_ratio,
                                                 avatar=avatar)
    result["image_prompt"] = image_prompt
    if user_id:
        image_path = generate_post_image(image_prompt, user_id, ratio=img_ratio,
                                         image_model=image_model,
                                         surface=AVATAR_SURFACE_POST_IMAGE, post_id=post_id)
    else:
        image_path = generate_flux1_image_from_prompt(image_prompt, ratio=img_ratio,
                                                       image_model=image_model,
                                                       surface=AVATAR_SURFACE_POST_IMAGE)

    img_ext = os.path.splitext(image_path)[1] or ".webp"
    img_name = f"variant_{idx}_image{img_ext}"
    dest_img = os.path.join(batch_dir, img_name)
    shutil.copy2(image_path, dest_img)
    _sign_best_effort(dest_img)
    result["image_url"] = _public_url(batch_id, img_name)
    result["estimated_cost_usd"] = _image_cost(image_model)

    # 2. Motion prompt + video (from the local base image)
    if include_video:
        # Only audio-capable models need the user's language (issue #548) — skip the lookup otherwise.
        language = (get_user_content_language(user_id) if supports_audio(video_model)
                    else DEFAULT_CONTENT_LANGUAGE)
        video_prompt = get_runway_ml_video_prompt_from_ai(
            source_text, image_prompt, model=video_model, language=language, user_id=user_id,
            post_id=post_id)[:512]
        result["video_prompt"] = video_prompt
        video_src_url = create_runway_video(
            dest_img, video_prompt, model=video_model, ratio=ratio, duration=duration, seed=seed,
            user_id=user_id, post_id=post_id)
        if video_src_url:
            saved = save_video_url_to_dir(video_src_url, batch_dir)
            vid_name = f"variant_{idx}_video.mp4"
            final_path = os.path.join(batch_dir, vid_name)
            if os.path.abspath(saved) != os.path.abspath(final_path):
                shutil.move(saved, final_path)
            _sign_best_effort(final_path)
            result["video_url"] = _public_url(batch_id, vid_name)
            result["estimated_cost_usd"] += estimate_video_cost(video_model, duration)

    return result


def generate_media_variants(*, post_id: Optional[int] = None, text: Optional[str] = None,
                            topic: Optional[str] = None, user_id: Optional[int] = None,
                            combos: Optional[list] = None, timestamp: Optional[int] = None) -> dict:
    """Generate variant media and return a payload of public URLs + cost estimate.

    Provide either ``post_id`` (uses its content) or ``text``/``topic``. Never mutates
    the post. Per-variant failures are isolated and reported in each variant's ``error``.
    """
    source_text = get_post_content(post_id) if post_id is not None else None
    if not source_text:
        source_text = text or topic
    if not source_text:
        raise ValueError("Provide post_id (with content) or text/topic")

    profile = load_profile_for_user(user_id) if user_id else None
    ts = int(timestamp if timestamp is not None else time.time())
    batch_id = f"{ts}_{uuid4().hex[:8]}"
    batch_dir = os.path.join(assets_dir, "variants", batch_id)
    create_folder_if_not_exists(batch_dir)

    use_combos = combos if combos else DEFAULT_COMBOS
    log_info(f"Generating {len(use_combos)} media variant(s) into batch {batch_id}")

    variants = []
    for idx, combo in enumerate(use_combos):
        try:
            variants.append(_generate_one_variant(idx, combo, source_text, profile, user_id, batch_dir,
                                                  batch_id, post_id=post_id))
        except Exception as e:
            log_warning(f"Variant {idx} failed", exc=e)
            variants.append({
                "combo": combo, "variant_key": combo_key(combo),
                "image_url": None, "video_url": None,
                "image_prompt": "", "video_prompt": None,
                "estimated_cost_usd": 0.0, "error": f"{type(e).__name__}: {e}",
            })

    total = round(sum(v.get("estimated_cost_usd") or 0.0 for v in variants), 3)
    payload = {
        "batch_id": batch_id,
        "variants": variants,
        "total_estimated_cost_usd": total,
        "metadata_url": _public_url(batch_id, "metadata.json"),
    }

    metadata = {
        "request": {"post_id": post_id, "text": text, "topic": topic,
                    "user_id": user_id, "combos": use_combos},
        "generated_at": ts,
        "result": payload,
    }
    with open(os.path.join(batch_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2, default=str)

    return payload


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate AI media variants for review")
    parser.add_argument("--post-id", type=int, default=None)
    parser.add_argument("--text", default=None)
    parser.add_argument("--topic", default=None)
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args()
    result = generate_media_variants(
        post_id=args.post_id, text=args.text, topic=args.topic, user_id=args.user_id)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _main()
