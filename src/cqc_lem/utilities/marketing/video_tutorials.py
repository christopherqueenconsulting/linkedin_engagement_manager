"""Automated SPA video-tutorial production (issue #505).

One declarative flow per feature -> one finished tutorial:

  capture (real SPA, headless Chrome) -> grounded script (lem-medium) -> TTS voice-over
  -> ffmpeg MP4 (branded intro/outro + captions) -> vertical clip -> YouTube -> manifest

Everything is FAIL-CLOSED and ordered cheapest-first: capture runs before a single token is
spent, and a flow whose declared UI anchors no longer exist raises instead of narrating a
screen that has moved. Narration is grounded in the flow definition + the text actually read
off the captured screens, and audited for fabricated specifics before any TTS spend.

Cost per video is attributed in three parts: script tokens (via `_call_llm` -> track_llm_call),
TTS characters and local render minutes (both via `track_media_cost`), and the total is stored
on the manifest record so a tutorial's true cost is answerable per video.

The no-self-promo guardrail from `content_alignment` is deliberately NOT applied here: this is
LEM's own product marketing, the one content type where naming the product IS the point.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem import assets_dir
from cqc_lem.utilities.ai.content_alignment import apply_contractions, cap_em_dashes, find_ai_tell_words, humanize_text
from cqc_lem.utilities.env_constants import (
    API_URL_FINAL,
    ELEVENLABS_API_KEY,
    ELEVENLABS_COST_PER_1K_CHARS,
    ELEVENLABS_MODEL,
    ELEVENLABS_VOICE_ID,
    TUTORIAL_BURN_CAPTIONS,
    TUTORIAL_DEMO_EMAIL,
    TUTORIAL_DEMO_SESSION_TOKEN,
    TUTORIAL_MAX_NARRATION_CHARS,
    TUTORIAL_REFRESH_DAYS,
    TUTORIAL_RENDER_COST_PER_MINUTE,
    TUTORIAL_SPA_BASE_URL,
    TUTORIAL_THUMBNAIL_ENABLED,
    TUTORIAL_TTS_COST_PER_1K_CHARS,
    TUTORIAL_TTS_MODEL,
    TUTORIAL_TTS_PROVIDER,
    TUTORIAL_TTS_VOICE,
    WAIT_DEFAULT_TIMEOUT,
    YOUTUBE_PRIVACY_STATUS,
)
from cqc_lem.utilities.flags import TUTORIAL_VIDEOS, flag_enabled
from cqc_lem.utilities.linkedin_formatter import normalize_public_text
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.marketing.attribution import (
    MEDIUM_VIDEO,
    PLACEMENT_VIDEO_DESCRIPTION,
    SOURCE_YOUTUBE,
    campaign_for_tutorial,
    signup_url,
    tag_links_in_text,
)
from cqc_lem.utilities.marketing.youtube_auth import mint_access_token, preflight, youtube_configured
from cqc_lem.utilities.observability import FEATURE_MARKETING, track_media_cost
from cqc_lem.utilities.utils import create_folder_if_not_exists

TASK_NAME = "produce_feature_tutorial"

# 1920x1080 landscape master; the dogfooding clip is 1080x1920 of the same render.
FRAME_WIDTH, FRAME_HEIGHT = 1920, 1080
VERTICAL_WIDTH, VERTICAL_HEIGHT = 1080, 1920
CARD_SECONDS = 3.0          # branded intro/outro card hold
SEGMENT_PAD_SECONDS = 0.5   # breathing room after each narrated segment
FPS = 30


class TutorialCaptureError(RuntimeError):
    """The SPA did not render the flow we declared — never narrate a screen we could not verify."""


class TutorialGuardrailError(RuntimeError):
    """The generated script failed a publish guardrail (length, profanity, ungrounded claim)."""


class TutorialRenderError(RuntimeError):
    """ffmpeg is unavailable or failed — no half-rendered MP4 is ever published."""


@dataclass(frozen=True)
class TutorialStep:
    """One captured beat of a tutorial: a route, proof it rendered, and how long to hold the frame.

    `wait_for` is where fail-closed lives — `capture_flow` raises `TutorialCaptureError` when the
    declared anchor is gone, so a moved UI stops the run before any token is spent instead of
    being narrated over a screen nobody verified.
    """

    caption: str                      # on-screen caption AND what this beat of the script covers
    path: str                         # SPA route, relative to the tutorial base URL
    wait_for: Optional[str] = None    # CSS selector that MUST exist, or the flow is stale
    click: Optional[str] = None       # CSS selector clicked after the frame is captured
    seconds: float = 4.0              # minimum hold for this frame


@dataclass(frozen=True)
class TutorialFlow:
    """One feature's declarative tutorial — the entire input to a production run.

    `feature` is the only feature claim the script is allowed to make: it goes into the grounding
    corpus (`grounding_text`) alongside the text actually read off the captured screens, and
    anything the model asserts outside that corpus is an ungrounded claim that aborts the run
    before TTS spend. A `requires_auth` flow is skipped entirely with no demo session token
    configured (`_flow_available`), rather than filmed against a login modal.
    """

    key: str
    title: str
    feature: str                      # grounded one-liner: the only feature claim the script may make
    steps: tuple = ()
    requires_auth: bool = False       # capture needs the demo account's session seeded
    tags: tuple = ("linkedin", "automation", "tutorial")


# The declarative flow registry. Routes/selectors mirror the real SPA (`ui/src/App.tsx`); a
# selector that disappears fails the capture, which is exactly how a UI change gets noticed.
TUTORIAL_FLOWS: dict[str, TutorialFlow] = {
    "getting-started": TutorialFlow(
        key="getting-started",
        title="Getting started with LinkedIn Engagement Manager",
        feature=("LEM automates LinkedIn engagement end to end: it plans and schedules a month of "
                 "posts, comments on your feed, and replies to your own posts, all in your own voice."),
        steps=(
            TutorialStep(caption="What LEM does", path="/", wait_for="h1", seconds=5.0),
            TutorialStep(caption="The feature set", path="/#features", wait_for="#features",
                         seconds=6.0),
            TutorialStep(caption="Pick a plan and sign in", path="/#pricing", wait_for="#pricing",
                         seconds=5.0),
        ),
    ),
    "content-studio": TutorialFlow(
        key="content-studio",
        title="Reviewing and approving posts in the Content Studio",
        feature=("The Content Studio is where every generated post, newsletter and DM waits for "
                 "your approval before it is published or sent."),
        steps=(
            TutorialStep(caption="Open the Content Studio", path="/content", wait_for="main",
                         seconds=5.0),
            TutorialStep(caption="Review what is queued", path="/content?tab=review",
                         wait_for="main", seconds=6.0),
            TutorialStep(caption="Newsletter editions", path="/content?tab=newsletters",
                         wait_for="main", seconds=5.0),
        ),
        requires_auth=True,
    ),
    "engagement-preferences": TutorialFlow(
        key="engagement-preferences",
        title="Setting your engagement preferences",
        feature=("Account preferences control who LEM engages with, the voice it writes in, and how "
                 "many comments and DMs it may send per day."),
        steps=(
            TutorialStep(caption="Open your settings", path="/account", wait_for="main",
                         seconds=5.0),
            TutorialStep(caption="Who you engage with", path="/account?section=targeting",
                         wait_for="main", seconds=6.0),
            TutorialStep(caption="How much and how often", path="/account?section=volume",
                         wait_for="main", seconds=6.0),
        ),
        requires_auth=True,
    ),
}


# --- paths + manifest ------------------------------------------------------------------------
# The manifest lives in the shared assets volume (survives deploys) and doubles as the SPA embed
# source, so shipping this feature needs no schema change.

def tutorials_dir() -> str:
    """Root under the SHARED assets volume where every tutorial's frames, audio and MP4 live.

    Created on demand. It is a volume rather than image content, so the catalogue and the rendered
    files survive a deploy — which is why this feature needed no schema change to ship.
    """
    path = os.path.join(assets_dir, "videos", "tutorials")
    create_folder_if_not_exists(path)
    return path


def manifest_path() -> str:
    """Path of `manifest.json` — the produced-tutorial catalogue the SPA embeds from."""
    return os.path.join(tutorials_dir(), "manifest.json")


def load_manifest() -> dict:
    """The tutorial catalogue, or an empty `{"videos": {}}` when there is no usable one.

    Fails open on every failure mode — no file, unreadable file, or JSON that parses to the wrong
    shape — because a beat that cannot read the catalogue should re-film rather than crash. That
    is not free: a caller who saves over an empty read loses the RECORDS of previously produced
    videos, though the rendered files themselves stay on disk.
    """
    try:
        with open(manifest_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"videos": {}}
    if not isinstance(data, dict) or not isinstance(data.get("videos"), dict):
        return {"videos": {}}
    return data


def save_manifest(manifest: dict) -> None:
    """Write the catalogue back, sorted and indented so a change to it reads as a sane diff.

    A whole-file overwrite with no merge, so callers must start from `load_manifest` and add to
    what it returned.
    """
    with open(manifest_path(), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def asset_url(relative_path: str) -> str:
    """Public /api/assets URL for a file under assets_dir — what the SPA embeds."""
    return f"{API_URL_FINAL}/api/assets?file_name={relative_path.lstrip('/')}"


def spa_base_url() -> str:
    """Origin the headless capture browser drives — `TUTORIAL_SPA_BASE_URL`, else the API's own URL.

    Kept separate from `API_URL_FINAL` because the SPA being filmed and the API serving the
    `asset_url` embeds do not have to be the same origin.
    """
    return (TUTORIAL_SPA_BASE_URL or API_URL_FINAL).rstrip("/")


def ui_fingerprint(markers: list) -> str:
    """Hash of the text read off each captured screen. A changed fingerprint means the UI moved,
    which is the trigger to re-film — the "refresh on UI-change" half of the cadence.
    """
    joined = "\n".join(re.sub(r"\s+", " ", str(m or "")).strip()[:400] for m in markers)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _flow_available(flow: TutorialFlow) -> bool:
    return bool(not flow.requires_auth or TUTORIAL_DEMO_SESSION_TOKEN)


def next_flow(manifest: Optional[dict] = None) -> Optional[TutorialFlow]:
    """The one flow worth producing this run: an uncovered feature first (the weekly cadence runs
    until every top-level feature has a tutorial), then the stalest covered one.
    """
    manifest = manifest if manifest is not None else load_manifest()
    videos = manifest.get("videos") or {}
    candidates = [f for f in TUTORIAL_FLOWS.values() if _flow_available(f)]
    uncovered = [f for f in candidates if f.key not in videos]
    if uncovered:
        return uncovered[0]
    cutoff = datetime.now(timezone.utc) - timedelta(days=TUTORIAL_REFRESH_DAYS)
    stale = [f for f in candidates if _produced_before(videos.get(f.key), cutoff)]
    if stale:
        return sorted(stale, key=lambda f: str((videos.get(f.key) or {}).get("produced_at") or ""))[0]
    return None


def is_current(record: Optional[dict], fingerprint: str) -> bool:
    """True when an existing tutorial still shows today's UI and is inside the refresh window —
    the two conditions that make re-filming pure spend.
    """
    if not record:
        return False
    if record.get("fingerprint") != fingerprint:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=TUTORIAL_REFRESH_DAYS)
    return not _produced_before(record, cutoff)


def _produced_before(record: Optional[dict], cutoff: datetime) -> bool:
    raw = (record or {}).get("produced_at")
    if not raw:
        return True
    try:
        produced = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if produced.tzinfo is None:
        produced = produced.replace(tzinfo=timezone.utc)
    return produced < cutoff


# --- capture ---------------------------------------------------------------------------------

def _seed_demo_session(driver, base_url: str) -> None:
    """Put the demo account's session token where the SPA's AuthContext reads it, so protected
    routes render the real product instead of the login modal.
    """
    driver.get(f"{base_url}/")
    driver.execute_script(
        "window.localStorage.setItem('lem_session', arguments[0]);"
        "window.localStorage.setItem('lem_email', arguments[1]);",
        TUTORIAL_DEMO_SESSION_TOKEN, TUTORIAL_DEMO_EMAIL or "demo@example.com",
    )


def capture_flow(flow: TutorialFlow, driver=None) -> dict:
    """Screenshot every step of `flow` against the running SPA. Raises TutorialCaptureError the
    moment a declared anchor is missing or a screenshot fails — a tutorial is never built on a
    screen we could not verify.
    """
    from cqc_lem.utilities.selenium_util import click_first, find_first, get_docker_driver

    if flow.requires_auth and not TUTORIAL_DEMO_SESSION_TOKEN:
        raise TutorialCaptureError(
            f"Flow '{flow.key}' needs an authenticated SPA session but TUTORIAL_DEMO_SESSION_TOKEN is unset")

    base_url = spa_base_url()
    frames_dir = os.path.join(tutorials_dir(), flow.key, "frames")
    create_folder_if_not_exists(frames_dir)
    owns_driver = driver is None
    # No user_id on purpose: this session hits OUR OWN SPA, never LinkedIn, so it wants neither a
    # residential proxy nor a user's geo profile.
    driver = driver or get_docker_driver(headless=True, session_name="TutorialCapture")
    wait = WebDriverWait(driver, WAIT_DEFAULT_TIMEOUT)
    frames, markers = [], []
    try:
        if flow.requires_auth:
            _seed_demo_session(driver, base_url)
        for index, step in enumerate(flow.steps):
            driver.get(f"{base_url}{step.path}")
            if step.wait_for:
                element = find_first(driver, wait, [(By.CSS_SELECTOR, step.wait_for)],
                                     f"tutorial:{flow.key}:{index}", required=False)
                if element is None:
                    raise TutorialCaptureError(
                        f"Flow '{flow.key}' step {index} anchor '{step.wait_for}' not found at "
                        f"{step.path} — the UI changed, re-declare the flow before publishing")
                markers.append(_element_text(element))
            frame_path = os.path.join(frames_dir, f"{index:02d}.png")
            if not driver.save_screenshot(frame_path):
                raise TutorialCaptureError(f"Flow '{flow.key}' step {index} screenshot failed")
            frames.append(frame_path)
            if step.click:
                click_first(driver, wait, [(By.CSS_SELECTOR, step.click)],
                            f"tutorial:{flow.key}:{index}:click", required=False)
    finally:
        if owns_driver:
            try:
                driver.quit()
            except Exception as e:
                log_warning("Tutorial capture driver quit failed", exc=e, task_name=TASK_NAME)

    log_info(f"Tutorial capture: {len(frames)} frame(s) for '{flow.key}'", task_name=TASK_NAME)
    return {"frames": frames, "markers": markers, "fingerprint": ui_fingerprint(markers)}


def _element_text(element) -> str:
    try:
        return str(element.text or "")
    except Exception:
        return ""


# --- script ----------------------------------------------------------------------------------

_SCRIPT_SYSTEM = (
    "You write the voice-over script for a short screen-recorded product tutorial. You will be given "
    "the feature description, the ordered on-screen steps, and the text actually visible on each "
    "captured screen.\n\n"
    "HARD RULES:\n"
    "- Narrate ONLY what the steps and the captured screen text support. Never invent a feature, a "
    "menu, a number, a price, a statistic, a customer, or a result.\n"
    "- One short spoken paragraph per step, 2 sentences max, written the way a person talks.\n"
    "- Second person ('you'), present tense, no marketing hype, no AI cliches (no seamless, robust, "
    "leverage, unlock, elevate, journey, game-changing).\n"
    "- Naming the product is expected; this is its own tutorial.\n\n"
    "Return ONLY a JSON object, no prose or code fences:\n"
    '{"intro": "...", "segments": ["...", "..."], "outro": "...", "youtube_title": "...", '
    '"youtube_description": "..."}\n'
    "`segments` MUST have exactly one entry per step, in order. `intro` says what the viewer will "
    "learn, `outro` is one plain next-step line."
)

# Numbers/prices are the fabrication that costs credibility, so they must be traceable to the
# flow definition or the captured screens.
_NUMERIC_CLAIM_RE = re.compile(r"(?:\$\s?\d[\d,.]*|\d[\d,.]*\s?%|\b\d[\d,.]*\b)")
_PROFANITY = frozenset({
    "shit", "shitty", "fuck", "fucking", "fucked", "bullshit", "asshole", "bastard", "bitch",
    "dick", "damn", "goddamn", "crap", "piss", "cunt", "twat", "wanker",
})


def _script_prompt(flow: TutorialFlow, capture: dict) -> str:
    lines = [f"Feature: {flow.feature}", f"Tutorial title: {flow.title}", "", "Steps:"]
    markers = capture.get("markers") or []
    for index, step in enumerate(flow.steps):
        seen = re.sub(r"\s+", " ", str(markers[index] if index < len(markers) else "")).strip()
        lines.append(f"{index + 1}. {step.caption} (route {step.path})")
        if seen:
            lines.append(f"   Text visible on this screen: {seen[:400]}")
    return "\n".join(lines)


def grounding_text(flow: TutorialFlow, capture: dict) -> str:
    """Everything the narration is allowed to be traceable to, as one blob.

    The flow's title, feature one-liner, step captions and routes, plus the text actually read off
    each captured screen. `ungrounded_claims` checks the script's numbers against this and nothing
    else, which is what turns "we never narrate a specific the product did not show" into
    something checkable before any TTS is bought.
    """
    parts = [flow.title, flow.feature] + [s.caption for s in flow.steps] + [s.path for s in flow.steps]
    parts += [str(m) for m in (capture.get("markers") or [])]
    return "\n".join(parts)


def ungrounded_claims(narration: str, allowed: str) -> list:
    """Numeric/price claims in the narration that appear nowhere in the flow definition or the
    captured screen text. Deterministic — no LLM, so the gate itself can never hallucinate.
    """
    allowed_numbers = set(_NUMERIC_CLAIM_RE.findall(allowed or ""))
    allowed_norm = {n.replace(" ", "") for n in allowed_numbers}
    out = []
    for hit in _NUMERIC_CLAIM_RE.findall(narration or ""):
        if hit.replace(" ", "") not in allowed_norm and hit not in out:
            out.append(hit)
    return out


def check_narration(narration: str, allowed: str) -> None:
    """Publish guardrails, in fail-closed order: something to say, inside the TTS budget, clean,
    and free of invented specifics.
    """
    text = (narration or "").strip()
    if not text:
        raise TutorialGuardrailError("Script produced no narration")
    if len(text) > TUTORIAL_MAX_NARRATION_CHARS:
        raise TutorialGuardrailError(
            f"Narration is {len(text)} chars, over the {TUTORIAL_MAX_NARRATION_CHARS} cap")
    words = {w.lower().strip(".,!?;:") for w in text.split()}
    profane = sorted(words & _PROFANITY)
    if profane:
        raise TutorialGuardrailError(f"Narration contains disallowed language: {', '.join(profane)}")
    invented = ungrounded_claims(text, allowed)
    if invented:
        raise TutorialGuardrailError(
            f"Narration contains ungrounded specifics: {', '.join(invented[:5])}")


def _spoken(text: str) -> str:
    """Plain, TTS-safe prose: at most one em-dash (capped BEFORE normalization folds them into
    hyphens), no smart punctuation to mispronounce, and the contractions a person actually says.
    """
    return apply_contractions(normalize_public_text(cap_em_dashes(str(text or ""), 1)).strip())


def generate_script(flow: TutorialFlow, capture: dict) -> dict:
    """Brand-voice, grounded narration for a captured flow. Raises TutorialGuardrailError before
    any TTS/publish spend if the result is unusable.
    """
    from cqc_lem.utilities.ai.ai_helper import _call_llm

    response = _call_llm(
        model="lem-medium",
        messages=[{"role": "system", "content": _SCRIPT_SYSTEM},
                  {"role": "user", "content": _script_prompt(flow, capture)}],
        temperature=0.5,
        _track_feature=FEATURE_MARKETING,
    )
    raw = (response.choices[0].message.content or "").strip()
    data = _coerce_script(raw, len(flow.steps))
    if data is None:
        raise TutorialGuardrailError(f"Could not parse a script for flow '{flow.key}'")

    intro = _spoken(data["intro"]) or f"Here is how {flow.title.lower()} works."
    outro = _spoken(data["outro"]) or "That is the whole flow. Try it on your own account."
    segments = [{"caption": step.caption, "narration": _spoken(narration), "seconds": step.seconds}
                for step, narration in zip(flow.steps, data["segments"])]
    segments = ([{"caption": flow.title, "narration": intro, "seconds": CARD_SECONDS}] + segments
                + [{"caption": "Next step", "narration": outro, "seconds": CARD_SECONDS}])

    narration = " ".join(s["narration"] for s in segments).strip()
    check_narration(narration, grounding_text(flow, capture))
    tells = find_ai_tell_words(narration)
    if tells:
        log_warning(f"Tutorial narration still carries AI-tell words: {', '.join(tells[:5])}",
                    task_name=TASK_NAME)

    title = _spoken(data["youtube_title"]) or flow.title
    # The description is read, not spoken, so it gets the full READER-mode de-slop pass.
    description = humanize_text(data["youtube_description"], content_type="post") or flow.feature
    return {"segments": segments, "narration": narration, "title": title[:100],
            "description": description_with_cta(description, flow)}


DESCRIPTION_MAX_CHARS = 4500
_DESCRIPTION_CTA_PREFIX = "\n\nTry it on your own account: "


def description_with_cta(description: str, flow: TutorialFlow,
                         max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    """The YouTube description plus ONE tagged trial link (issue #658).

    A tutorial is an acquisition surface, and YouTube passes no referrer we can read — without the
    UTMs on this link every signup a video drives is indistinguishable from direct traffic. Any
    owned link the model already wrote into the description is tagged in place under the same
    campaign; the CTA is only appended when there is a signup URL configured AND the description
    does not already carry it, so re-running never stacks two.

    The cap is applied to the PROSE, never to the finished string: truncating afterwards would chop
    a long description's CTA mid-URL and publish a broken link, which is worse than shipping none.
    """
    campaign = campaign_for_tutorial(flow.key)
    body = tag_links_in_text(description or "", SOURCE_YOUTUBE, MEDIUM_VIDEO, campaign,
                             content=PLACEMENT_VIDEO_DESCRIPTION)
    link = signup_url(SOURCE_YOUTUBE, MEDIUM_VIDEO, campaign,
                      content=PLACEMENT_VIDEO_DESCRIPTION)
    if not link or link.split("?")[0] in body:
        return body[:max_chars]
    cta = f"{_DESCRIPTION_CTA_PREFIX}{link}"
    # A CTA that cannot fit at all is dropped whole rather than published half-written.
    if len(cta) > max_chars:
        return body[:max_chars]
    return f"{body[:max_chars - len(cta)].rstrip()}{cta}".strip()


def _coerce_script(raw: str, step_count: int) -> Optional[dict]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    segments = data.get("segments")
    if isinstance(segments, str):
        segments = [segments]
    if not isinstance(segments, list) or len(segments) < step_count:
        return None
    return {
        "intro": str(data.get("intro") or ""),
        "outro": str(data.get("outro") or ""),
        "segments": [str(s or "") for s in segments[:step_count]],
        "youtube_title": str(data.get("youtube_title") or ""),
        "youtube_description": str(data.get("youtube_description") or ""),
    }


# --- voice-over ------------------------------------------------------------------------------

def tts_cost_usd(chars: int, provider: Optional[str] = None) -> float:
    """Estimated USD for synthesizing `chars` of speech, at the configured per-1K-character rate.

    An app-side estimate off a constant, never a figure the provider returned — it is what gets
    attributed via `track_media_cost`. `provider` defaults to the configured one, and anything
    that is not `elevenlabs` is priced at the OpenAI rate.
    """
    provider = (provider or TUTORIAL_TTS_PROVIDER or "openai").lower()
    rate = ELEVENLABS_COST_PER_1K_CHARS if provider == "elevenlabs" else TUTORIAL_TTS_COST_PER_1K_CHARS
    return round(rate * max(0, int(chars or 0)) / 1000.0, 6)


def _tts_openai(text: str, out_path: str) -> None:
    from cqc_lem.utilities.ai.client import client
    response = client.audio.speech.create(model=TUTORIAL_TTS_MODEL, voice=TUTORIAL_TTS_VOICE,
                                          input=text)
    # openai>=1.x exposes both write_to_file and raw .content depending on the response wrapper.
    if hasattr(response, "write_to_file"):
        response.write_to_file(out_path)
        return
    with open(out_path, "wb") as f:
        f.write(response.content)


def _tts_elevenlabs(text: str, out_path: str) -> None:
    import requests
    if not (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID):
        raise TutorialRenderError("TUTORIAL_TTS_PROVIDER=elevenlabs but no API key / voice id is set")
    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "accept": "audio/mpeg"},
        json={"text": text, "model_id": ELEVENLABS_MODEL},
        timeout=120,
    )
    response.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(response.content)


def synthesize_segment(text: str, out_path: str) -> str:
    """One narrated segment to an mp3, with its characters billed to the marketing feature."""
    provider = (TUTORIAL_TTS_PROVIDER or "openai").lower()
    if provider == "elevenlabs":
        _tts_elevenlabs(text, out_path)
    else:
        _tts_openai(text, out_path)
    track_media_cost("audio", provider, tts_cost_usd(len(text), provider),
                     feature=FEATURE_MARKETING, qty=len(text),
                     model=ELEVENLABS_MODEL if provider == "elevenlabs" else TUTORIAL_TTS_MODEL)
    return out_path


# --- render ----------------------------------------------------------------------------------

def _ffmpeg_bin(name: str = "ffmpeg") -> str:
    path = shutil.which(name)
    if not path:
        raise TutorialRenderError(f"{name} is not installed — cannot render a tutorial video")
    return path


def _run(cmd: list) -> str:
    log_debug(f"ffmpeg: {' '.join(cmd[:6])}...", task_name=TASK_NAME)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TutorialRenderError(f"{os.path.basename(cmd[0])} failed: {(result.stderr or '')[-500:]}")
    return result.stdout or ""


def audio_duration(path: str) -> float:
    """Length of an audio file in seconds, read with ffprobe.

    Returns:
        0.0 when the output cannot be parsed as a number. That zero is load-bearing rather than an
        error: caption timings and frame holds are both built from these durations, and
        `assemble_video` floors a hold at 0.5s — so an unreadable duration shortens one beat
        instead of failing a render that has already been paid for in tokens and TTS.

    Raises:
        TutorialRenderError: ffprobe is not installed, or exited non-zero.
    """
    out = _run([_ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path])
    try:
        return float((out or "").strip())
    except ValueError:
        return 0.0


def brand_card(text: str, out_path: str, subtitle: str = "") -> str:
    """Branded intro/outro frame. PIL only — no design service, no network."""
    import textwrap

    from PIL import Image, ImageDraw
    image = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), (11, 34, 64))  # LEM deep blue
    draw = ImageDraw.Draw(image)
    title_font, sub_font = _card_fonts()
    lines = textwrap.wrap(str(text or ""), width=28) or [""]
    line_height = 92
    top = FRAME_HEIGHT // 2 - 40 - (len(lines) - 1) * line_height // 2
    for index, line in enumerate(lines):
        _centered(draw, line, top + index * line_height, title_font, (255, 255, 255))
    if subtitle:
        _centered(draw, subtitle, top + len(lines) * line_height + 40, sub_font, (146, 180, 219))
    image.save(out_path)
    return out_path


def _card_fonts():
    from PIL import ImageFont
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, 72), ImageFont.truetype(candidate, 40)
    return ImageFont.load_default(size=72), ImageFont.load_default(size=40)


def _centered(draw, text: str, y: int, font, fill: tuple) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((FRAME_WIDTH - (right - left)) / 2, y - (bottom - top) / 2), text, font=font, fill=fill)


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_captions(segments: list, out_path: str) -> str:
    """SRT sidecar built from the REAL per-segment audio durations, so captions match the voice."""
    blocks, clock = [], 0.0
    for index, segment in enumerate(segments, start=1):
        end = clock + float(segment.get("duration") or 0.0)
        blocks.append(f"{index}\n{_srt_timestamp(clock)} --> {_srt_timestamp(end)}\n"
                      f"{segment.get('narration') or segment.get('caption') or ''}\n")
        clock = end
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return out_path


def concat_audio(paths: list, out_path: str) -> str:
    """Join the per-segment narration files into one voice-over track, in the order given.

    Stream copy through ffmpeg's concat demuxer (`-c copy`) — no re-encode, so it neither costs
    render time nor degrades the audio, but it requires every input to share a codec and format.
    That holds because they all come from `synthesize_segment`. The list file written beside
    `out_path` is left on disk.

    Raises:
        TutorialRenderError: ffmpeg is not installed, or exited non-zero.
    """
    list_file = f"{out_path}.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for path in paths:
            f.write(f"file '{os.path.abspath(path)}'\n")
    _run([_ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_path])
    return out_path


def assemble_video(segments: list, audio_path: str, out_path: str,
                   captions_path: Optional[str] = None) -> str:
    """Frames (held for their segment's real audio duration) + voice-over -> H.264 MP4."""
    list_file = f"{out_path}.frames.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for segment in segments:
            f.write(f"file '{os.path.abspath(segment['frame'])}'\n")
            f.write(f"duration {max(0.5, float(segment['duration'])):.3f}\n")
        # The concat demuxer drops the final entry's duration, so the last frame is repeated.
        f.write(f"file '{os.path.abspath(segments[-1]['frame'])}'\n")

    video_filter = (f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:force_original_aspect_ratio=decrease,"
                    f"pad={FRAME_WIDTH}:{FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if captions_path and TUTORIAL_BURN_CAPTIONS:
        video_filter += f",subtitles={captions_path}"
    _run([_ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-i", audio_path,
          "-vf", video_filter, "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "128k", "-shortest", out_path])
    return out_path


def render_vertical_clip(mp4_path: str, out_path: str) -> str:
    """9:16 cut of the same render — the clip the dogfooding agents post."""
    _run([_ffmpeg_bin(), "-y", "-i", mp4_path, "-vf",
          f"scale={VERTICAL_WIDTH}:{VERTICAL_HEIGHT}:force_original_aspect_ratio=decrease,"
          f"pad={VERTICAL_WIDTH}:{VERTICAL_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", out_path])
    return out_path


def generate_thumbnail(flow: TutorialFlow, out_dir: str) -> Optional[str]:
    """Optional lem-image thumbnail (off by default — it costs money per render)."""
    if not TUTORIAL_THUMBNAIL_ENABLED:
        return None
    try:
        import shutil

        from cqc_lem.utilities.ai.image_brief import build_image_brief
        from cqc_lem.utilities.ai.image_gen import render_image_gated
        # Through the ONE brief engine and its `thumbnail` preset, not a hand-written prompt: the
        # inline one said "No text, no logos", and negation is what SUMMONS both on the FLUX
        # fallback backend (issue #1141). 16:9 to match the YouTube player; quality low — a
        # thumbnail draft tier is plenty. The render is graded by the bounded vision gate with
        # the brief's focal_concept; because `thumbnail` is outside IMAGE_QUALITY_GATE_SURFACES
        # by default, the verdict is advisory-only and a vision outage fails open (issue #1290).
        brief = build_image_brief(flow.title, surface="thumbnail", ratio="16:9")
        path = render_image_gated(
            brief.prompt, surface="thumbnail", ratio="16:9", quality="low",
            focal_concept=brief.focal_concept)
        if not path:
            return None
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, os.path.basename(path))
        shutil.copyfile(path, dest)
        return dest
    except Exception as e:
        log_warning("Tutorial thumbnail generation failed", exc=e, task_name=TASK_NAME)
        return None


# --- publish ---------------------------------------------------------------------------------

def publish_to_youtube(mp4_path: str, title: str, description: str,
                       tags: Optional[list] = None) -> Optional[str]:
    """Resumable upload via the YouTube Data API v3 (plain requests — no extra SDK). Returns the
    watch URL, or None when YouTube is not configured or the upload fails: an unpublished MP4 is
    still a usable asset, so this never fails the whole production.
    """
    if not youtube_configured():
        # DEBUG: the whole feature is off by default, so "not configured" is the resting state,
        # not an incident. The docstring already says an unpublished MP4 is a usable asset.
        log_debug("YouTube publish skipped — no OAuth credentials configured", task_name=TASK_NAME)
        return None
    import requests
    try:
        token = mint_access_token()
        metadata = {
            "snippet": {"title": title[:100], "description": description[:5000],
                        "tags": list(tags or [])[:15], "categoryId": "28"},
            "status": {"privacyStatus": YOUTUBE_PRIVACY_STATUS, "selfDeclaredMadeForKids": False},
        }
        start = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=metadata, timeout=60)
        start.raise_for_status()
        upload_url = start.headers.get("Location")
        if not upload_url:
            raise RuntimeError("YouTube resumable session returned no upload URL")
        with open(mp4_path, "rb") as f:
            upload = requests.put(upload_url, data=f,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "video/mp4"}, timeout=900)
        upload.raise_for_status()
        video_id = (upload.json() or {}).get("id")
        if not video_id:
            raise RuntimeError("YouTube upload returned no video id")
        log_info(f"Published tutorial to YouTube: {video_id}", task_name=TASK_NAME)
        return f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        log_error("YouTube tutorial upload failed", exc=e, task_name=TASK_NAME)
        return None


# --- orchestration ---------------------------------------------------------------------------

def produce_tutorial(flow_key: Optional[str] = None, driver=None) -> Optional[dict]:
    """Produce (and publish) ONE tutorial end to end. Returns the manifest record, or None when
    the pipeline is disabled or there is nothing due. Raises on capture/guardrail/render failure —
    a broken tutorial must never reach YouTube.
    """
    # Runtime toggle (issue #651): the `tutorial-videos-enabled` flag, falling back to
    # TUTORIAL_VIDEOS_ENABLED whenever PostHog has no answer. Checked HERE rather than read at
    # import so a flip lands on the next beat instead of the next deploy.
    if not flag_enabled(TUTORIAL_VIDEOS):
        log_info("Tutorial production skipped — the tutorial-videos-enabled flag is off",
                 task_name=TASK_NAME)
        return None

    # Publishing preflight (issue #742), BEFORE any Selenium capture, TTS or render spend. Only a
    # PROVEN-dead grant aborts: an install with no OAuth credentials at all still produces a usable
    # MP4 (publishing simply no-ops), and an undecidable probe is not evidence of anything.
    gate = preflight()
    if gate.get("should_abort"):
        log_error(f"Tutorial production aborted before spend — YouTube publishing needs re-auth: "
                  f"{gate.get('reason')}", task_name=TASK_NAME)
        return None

    manifest = load_manifest()
    if flow_key:
        flow = TUTORIAL_FLOWS.get(flow_key)
        if flow is None:
            raise ValueError(f"Unknown tutorial flow {flow_key!r}. Known: {sorted(TUTORIAL_FLOWS)}")
        if not _flow_available(flow):
            log_warning(f"Tutorial flow '{flow_key}' needs a demo session — skipped",
                        task_name=TASK_NAME)
            return None
    else:
        flow = next_flow(manifest)
    if flow is None:
        log_info("Tutorial production: every feature flow is covered and current", task_name=TASK_NAME)
        return None

    work_dir = os.path.join(tutorials_dir(), flow.key)
    create_folder_if_not_exists(work_dir)

    capture = capture_flow(flow, driver=driver)
    existing = (manifest.get("videos") or {}).get(flow.key)
    # An explicit flow_key is a deliberate re-film request; the scheduled pass stops here when the
    # captured UI still matches what we already published.
    if not flow_key and is_current(existing, capture["fingerprint"]):
        log_info(f"Tutorial '{flow.key}' is current (UI unchanged) — nothing to re-film",
                 task_name=TASK_NAME)
        return None

    script = generate_script(flow, capture)
    segments = script["segments"]

    # Frames: branded intro card, one captured screen per step, branded outro card.
    frames = ([brand_card(flow.title[:90], os.path.join(work_dir, "intro.png"),
                          subtitle="LinkedIn Engagement Manager")]
              + capture["frames"]
              + [brand_card("Try it on your account", os.path.join(work_dir, "outro.png"),
                            subtitle=spa_base_url())])
    if len(frames) != len(segments):
        raise TutorialRenderError(
            f"Frame/segment mismatch for '{flow.key}': {len(frames)} frames vs {len(segments)} segments")

    audio_dir = os.path.join(work_dir, "audio")
    create_folder_if_not_exists(audio_dir)
    audio_paths = []
    for index, segment in enumerate(segments):
        segment["frame"] = frames[index]
        audio_path = synthesize_segment(segment["narration"], os.path.join(audio_dir, f"{index:02d}.mp3"))
        audio_paths.append(audio_path)
        segment["duration"] = max(float(segment.get("seconds") or 0.0),
                                  audio_duration(audio_path) + SEGMENT_PAD_SECONDS)

    voiceover = concat_audio(audio_paths, os.path.join(work_dir, "voiceover.mp3"))
    captions = write_captions(segments, os.path.join(work_dir, f"{flow.key}.srt"))
    mp4 = assemble_video(segments, voiceover, os.path.join(work_dir, f"{flow.key}.mp4"), captions)
    vertical = render_vertical_clip(mp4, os.path.join(work_dir, f"{flow.key}-vertical.mp4"))
    thumbnail = generate_thumbnail(flow, work_dir)

    duration_seconds = round(sum(float(s["duration"]) for s in segments), 2)
    render_usd = round(TUTORIAL_RENDER_COST_PER_MINUTE * duration_seconds / 60.0, 6)
    track_media_cost("video", "local-ffmpeg", render_usd, feature=FEATURE_MARKETING,
                     qty=duration_seconds, model="ffmpeg-libx264",
                     meta={"flow": flow.key, "kind": "tutorial"})
    narration_chars = len(script["narration"])
    total_usd = round(tts_cost_usd(narration_chars) + render_usd, 6)

    youtube_url = publish_to_youtube(mp4, script["title"], script["description"], list(flow.tags))

    record = {
        "flow": flow.key,
        "title": script["title"],
        "description": script["description"],
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": capture["fingerprint"],
        "duration_seconds": duration_seconds,
        "narration_chars": narration_chars,
        "tts_provider": (TUTORIAL_TTS_PROVIDER or "openai").lower(),
        "tts_usd": tts_cost_usd(narration_chars),
        "render_usd": render_usd,
        "total_usd": total_usd,
        "video_url": asset_url(_relative_asset(mp4)),
        "vertical_url": asset_url(_relative_asset(vertical)),
        "captions_url": asset_url(_relative_asset(captions)),
        "thumbnail_url": asset_url(_relative_asset(thumbnail)) if thumbnail else None,
        "youtube_url": youtube_url,
    }
    manifest.setdefault("videos", {})[flow.key] = record
    manifest["updated_at"] = record["produced_at"]
    save_manifest(manifest)
    log_info(f"Tutorial '{flow.key}' produced: {duration_seconds}s, ${total_usd} "
             f"({narration_chars} narration chars), youtube={'yes' if youtube_url else 'no'}",
             task_name=TASK_NAME)
    return record


def _relative_asset(path: str) -> str:
    return os.path.relpath(path, assets_dir).replace(os.sep, "/")
