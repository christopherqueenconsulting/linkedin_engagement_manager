"""RunwayML video-model abstraction.

Isolates the RunwayML SDK shape so model selection lives in one place. gen3a_turbo
(the previous default) is deliberately absent — Runway sunset it on 2026-07-30.

Two quality tiers:
- STANDARD (credits=0, free): gen4_turbo / gen4.5 image->video, no audio.
- PREMIUM (credits>0): Veo (veo3.1_fast=1 credit, veo3.1=3 credits) with native
  audio. Veo supports BOTH image->video (used to preserve an avatar's likeness)
  and text->video (when there's no base image / an abstract concept fits better).
"""
import base64
import time
from dataclasses import dataclass
from typing import Optional

from runwayml import RunwayML

from cqc_lem.utilities.env_constants import DEFAULT_VIDEO_MODEL, DEFAULT_VIDEO_RATIO
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.observability import track_media_cost


@dataclass(frozen=True)
class VideoModelSpec:
    """Everything about one Runway model that a caller must not hardcode.

    The SDK takes only a model string, so duration validity, audio capability and price are
    knowledge that would otherwise be scattered across call sites and drift when Runway retires a
    model (gen3a_turbo, 2026-07-30). Frozen because `VIDEO_MODELS` is module-level shared state.
    """

    sdk_model: str               # value passed to the SDK 'model' kwarg
    cost_per_second: float       # USD (premium values assume audio on)
    credits: int                 # video credits charged (0 = free/standard tier)
    supports_audio: bool
    valid_durations: tuple       # durations the API accepts for this model
    default_duration: int


# Runway API models reachable through the same RunwayML() client. Veo only accepts
# 4/6/8s durations; gen4/seedance accept 5/10.
VIDEO_MODELS: dict[str, VideoModelSpec] = {
    "gen4_turbo":     VideoModelSpec("gen4_turbo",     0.05, 0, False, (5, 10), 5),
    "gen4.5":         VideoModelSpec("gen4.5",         0.12, 0, False, (5, 10), 5),
    "veo3.1_fast":    VideoModelSpec("veo3.1_fast",    0.15, 1, True,  (4, 6, 8), 6),
    "veo3.1":         VideoModelSpec("veo3.1",         0.40, 3, True,  (4, 6, 8), 6),
    "seedance2_fast": VideoModelSpec("seedance2_fast", 0.29, 1, False, (5, 10), 5),
}

DEFAULT_VIDEO_DURATION = 5

# Veo has no language/voice API parameter — audio is steered ONLY by prompt text, so an
# audio-enabled render whose prompt says nothing about audio invents a voiceover and picks its
# own language (issue #548, posts #34/#36). ai_helper._audio_direction() writes a clause starting
# with this marker into every audio-capable motion prompt; create_runway_video refuses to enable
# audio without it, so the defect cannot silently return.
AUDIO_DIRECTION_MARKER = "Audio:"

# Friendly aspect-ratio aliases -> Runway resolution strings.
RATIO_ALIASES = {
    "1:1": "960:960",
    "16:9": "1280:720",
    "9:16": "720:1280",
    "4:5": "864:1080",
    "5:4": "1080:864",
    "4:3": "1104:832",
    "3:4": "832:1104",
}

_POLL_SECONDS = 10


def resolve_ratio(ratio: str) -> str:
    """Turn a friendly aspect ratio ("9:16") into the resolution string Runway wants ("720:1280").

    Anything not in `RATIO_ALIASES` passes through untouched, so a caller that already holds a raw
    Runway resolution — or a new one added before this map catches up — is never rewritten.
    """
    return RATIO_ALIASES.get(ratio, ratio)


def model_credits(model: str) -> int:
    """Video credits this model charges the user's balance; 0 for the free standard tier.

    An unknown model reads as 0 rather than raising: this is the number `run_content_plan` reserves
    up front, and refusing to bill for a model we cannot price beats over-charging a guess.
    """
    spec = VIDEO_MODELS.get(model)
    return spec.credits if spec else 0


def is_premium(model: str) -> bool:
    """Whether rendering this model spends credits — the switch between the Veo and standard paths.

    Cost is what defines the tier, so this is derived from `model_credits` rather than a separate
    flag that could disagree with the number actually deducted.
    """
    return model_credits(model) > 0


def supports_audio(model: str) -> bool:
    """True when the model generates native audio — i.e. when the prompt MUST carry an audio
    direction (issue #548). Unknown models are treated as silent.
    """
    spec = VIDEO_MODELS.get(model)
    return bool(spec and spec.supports_audio)


def resolve_duration(model: str, duration: Optional[int]) -> int:
    """Coerce a requested duration to one this model's API actually accepts.

    The tiers disagree — Veo takes 4/6/8s, gen4/seedance 5/10 — so a caller carrying one number
    across both would get a hard API rejection mid-render. A duration outside the model's set is
    replaced by that model's default rather than rounded, and an unknown model is left alone.
    """
    spec = VIDEO_MODELS.get(model)
    if not spec:
        return duration or DEFAULT_VIDEO_DURATION
    if duration in spec.valid_durations:
        return duration
    return spec.default_duration


def estimate_video_cost(model: str, duration: int = DEFAULT_VIDEO_DURATION) -> float:
    """Estimated USD for one render, from this module's own price table — not a provider-billed figure.

    Premium per-second rates assume audio is on, so a silent Veo render is over-estimated rather
    than under. An unknown model costs 0.0: the media ledger would rather miss a row than invent a
    price nobody can reconcile.
    """
    spec = VIDEO_MODELS.get(model)
    return round(spec.cost_per_second * duration, 3) if spec else 0.0


def _to_prompt_image(image_path_or_url: str) -> str:
    """Hosted URLs pass through; local files become a base64 PNG data URI."""
    if image_path_or_url.startswith(("http://", "https://")):
        return image_path_or_url
    with open(image_path_or_url, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _create_task(endpoint, create_kwargs: dict):
    """Call endpoint.create, retrying without optional kwargs if the pinned SDK
    version rejects them (resilience across runwayml versions).
    """
    try:
        return endpoint.create(**create_kwargs)
    except TypeError:
        keep = ("model", "prompt_image", "prompt_text", "ratio")
        return endpoint.create(**{k: v for k, v in create_kwargs.items() if k in keep})


def create_runway_video(
    image_path_or_url: Optional[str] = None,
    prompt: str = "",
    *,
    model: str = DEFAULT_VIDEO_MODEL,
    ratio: str = DEFAULT_VIDEO_RATIO,
    duration: Optional[int] = None,
    seed: Optional[int] = None,
    audio: bool = False,
    user_id: Optional[int] = None,
    post_id: Optional[int] = None,
) -> Optional[str]:
    """Create a video via the RunwayML API and return its URL.

    If ``image_path_or_url`` is provided -> image->video; if it's None -> text->video
    (the model must support it). ``audio`` is honored only for audio-capable models.
    ``user_id``/``post_id`` only attribute the render's cost (issue #490); when omitted the
    active llm_attribution scope supplies the user.
    Backwards compatible with the old positional ``(image_path, prompt)`` call.
    Raises on creation failure (so callers' fallback can trigger); returns None only
    when the task itself reports FAILED / produces no output.
    """
    spec = VIDEO_MODELS.get(model)
    if spec is None:
        raise ValueError(f"Unknown video model {model!r}. Known: {sorted(VIDEO_MODELS)}")

    runway_client = RunwayML()
    use_text = not image_path_or_url
    endpoint_name = "text_to_video" if use_text else "image_to_video"
    resolved_ratio = resolve_ratio(ratio)
    dur = resolve_duration(model, duration)

    create_kwargs = {
        "model": spec.sdk_model,
        "prompt_text": prompt,
        "ratio": resolved_ratio,
        "duration": dur,
    }
    if not use_text:
        create_kwargs["prompt_image"] = _to_prompt_image(image_path_or_url)
    enable_audio = bool(audio and spec.supports_audio)
    if enable_audio and AUDIO_DIRECTION_MARKER not in prompt:
        # Silent audio beats a hallucinated foreign-language voiceover (issue #548).
        log_warning("Audio requested without an audio direction in the prompt — rendering silent",
                    ai_model=spec.sdk_model)
        enable_audio = False
    if enable_audio:
        create_kwargs["audio"] = True
    if seed is not None:
        create_kwargs["seed"] = seed

    endpoint = getattr(runway_client, endpoint_name)
    log_debug(
        f"Runway {endpoint_name} model={spec.sdk_model} ratio={resolved_ratio} "
        f"duration={dur}s audio={enable_audio}",
        ai_model=spec.sdk_model,
    )
    try:
        task = _create_task(endpoint, create_kwargs)
    except Exception as e:
        log_warning("Runway video creation failed", exc=e, ai_model=spec.sdk_model)
        raise

    task_id = task.id
    time.sleep(_POLL_SECONDS)
    task = runway_client.tasks.retrieve(task_id)
    while task.status not in ("SUCCEEDED", "FAILED"):
        time.sleep(_POLL_SECONDS)
        task = runway_client.tasks.retrieve(task_id)

    if task.status == "SUCCEEDED" and getattr(task, "output", None):
        # Only a SUCCEEDED render is billed, so the ledger row goes here and not at creation time.
        track_media_cost("video", "runway", estimate_video_cost(model, dur), user_id=user_id,
                         post_id=post_id, qty=dur, model=spec.sdk_model,
                         meta={"ratio": resolved_ratio, "audio": enable_audio})
        return task.output[0]
    log_warning(f"Runway task {task_id} ended status={task.status}", ai_model=spec.sdk_model)
    return None
