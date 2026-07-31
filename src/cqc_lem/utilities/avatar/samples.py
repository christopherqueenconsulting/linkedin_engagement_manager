"""Sample renders that let a user SEE their avatar before it is published on their behalf.

Issue #744 (Phase 2 of #548), decision 4A. Activation used to be reachable straight from
``succeeded``, so the first time anyone saw their avatar was on a live post — which is how a
male user's posts shipped with a female likeness and nobody could catch it first.

Three fixed scenes are rendered through the LoRA (headshot, at-desk, speaking-to-camera) using
the same subject clause production images use, so what the user approves is what they will get.
"""
import os
import shutil
from typing import Optional

from cqc_lem import assets_dir
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.utils import create_folder_if_not_exists

# (label, scene). No trigger word here — apply_subject_clause prepends it together with the
# user's declared attributes, exactly as generate_post_image does for a real post.
AVATAR_SAMPLE_SCENES: tuple[tuple[str, str], ...] = (
    ("headshot",
     "a professional headshot portrait, neutral studio background, soft natural lighting, "
     "looking directly at the camera, photorealistic"),
    ("at_desk",
     "seated at a modern desk with a laptop in a bright office, working, candid editorial "
     "photograph, shallow depth of field"),
    ("speaking",
     "speaking to the camera in a bright modern office, mid-sentence, confident and warm, "
     "editorial photograph"),
)

SAMPLE_RATIO = "1:1"
_SAMPLE_SUBDIR = os.path.join("images", "avatar_samples")


def sample_relative_dir(avatar_id: int) -> str:
    """Path of an avatar's sample folder RELATIVE to assets_dir (what /api/assets resolves)."""
    return f"{_SAMPLE_SUBDIR}/{avatar_id}".replace(os.sep, "/")


def render_avatar_samples(avatar: dict) -> list[dict]:
    """Render the fixed sample set for ``avatar``. Returns ``[{"label", "path"}]``.

    Never raises: one failed scene is skipped rather than losing the ones that worked, and an
    avatar with no renderable samples simply stays un-approvable (which is the safe state).
    """
    from cqc_lem.utilities.avatar.attributes import apply_subject_clause
    from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

    avatar_id = avatar.get("id")
    model_ref = avatar.get("model_ref")
    if not avatar_id or not model_ref:
        return []

    rel_dir = sample_relative_dir(int(avatar_id))
    out_dir = os.path.join(assets_dir, *rel_dir.split("/"))
    create_folder_if_not_exists(out_dir)

    rendered: list[dict] = []
    for label, scene in AVATAR_SAMPLE_SCENES:
        try:
            prompt = apply_subject_clause(scene, avatar)
            path, used_avatar = generate_image_with_avatar(prompt, model_ref, ratio=SAMPLE_RATIO)
            if not used_avatar:
                # A base-Flux fallback image is a picture of a stranger. Showing it as "your
                # avatar" is worse than showing nothing — the whole point is a truthful preview.
                log_warning("Avatar sample fell back to the base model — discarding it",
                            action_type="avatar_sample")
                continue
            ext = os.path.splitext(path)[1] or ".webp"
            dest = os.path.join(out_dir, f"{label}{ext}")
            if os.path.abspath(path) != os.path.abspath(dest):
                shutil.copy2(path, dest)
            _sign_best_effort(dest)
            rendered.append({"label": label, "path": f"{rel_dir}/{os.path.basename(dest)}"})
        except Exception as e:
            log_warning(f"Avatar sample '{label}' failed to render", exc=e,
                        action_type="avatar_sample", api_provider="replicate")

    log_info(f"Rendered {len(rendered)}/{len(AVATAR_SAMPLE_SCENES)} avatar samples",
             action_type="avatar_sample")
    return rendered


def _sign_best_effort(file_path: str) -> None:
    try:
        from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
        add_ai_content_credentials(file_path)
    except Exception as e:
        log_warning("C2PA signing skipped for avatar sample", exc=e, action_type="avatar_sample")


def sample_asset_url(relative_path: str, api_url: Optional[str] = None) -> str:
    from cqc_lem.utilities.env_constants import API_URL_FINAL
    base = api_url or API_URL_FINAL
    return f"{base}/api/assets?file_name={relative_path}"


def sample_payload(avatar: dict) -> list[dict]:
    """Serialize an avatar's stored samples for the SPA."""
    return [
        {"label": s.get("label"), "url": sample_asset_url(s.get("path", ""))}
        for s in (avatar.get("sample_paths") or [])
        if isinstance(s, dict) and s.get("path")
    ]
