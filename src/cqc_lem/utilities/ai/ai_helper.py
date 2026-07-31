import os
import random
import time
from datetime import datetime

import openai
import replicate
from cqc_lem import assets_dir
from cqc_lem.utilities.ai.client import client, attribution_metadata
from cqc_lem.utilities.ai import content_framework as _framework
# The ONE alignment core (voice + prefs + LEM purpose + promo policy) shared by newsletters,
# posts, and comments. The underscore aliases keep this module's long-standing internal API
# (and the tests that import it) stable while the definitions live in exactly one place.
from cqc_lem.utilities.ai.content_alignment import (
    COMMENT_LENGTH_CHARS as _COMMENT_LENGTH_CHARS,  # lgtm[py/unused-global-variable]
    NO_SELF_PROMO_GUARDRAIL as _NO_SELF_PROMO_GUARDRAIL,
    NEWSLETTER_SOFT_PROMO_NOTE as _NEWSLETTER_SOFT_PROMO_NOTE,
    DEFAULT_ENGAGEMENT_INTENTION as _DEFAULT_ENGAGEMENT_INTENTION,  # lgtm[py/unused-global-variable]
    style_directive as _style_directive,
    focus_directive as _focus_directive,
    intention_directive as _intention_directive,
    alignment_directive as _alignment_directive,
    voice_reference as _voice_reference,
    select_focus_topic as _select_focus_topic,
    lead_magnet_preserve_note as _lead_magnet_preserve_note,
    humanize_text as _humanize_text,
    humanize_title as _humanize_title,
)
from cqc_lem.utilities.ai import slop_lint as _slop
from cqc_lem.utilities.ai.content_research import research_topic
from cqc_lem.utilities.ai.tools import search_recent_news
from cqc_lem.utilities.avatar.guardrails import AVATAR_SURFACE_POST_IMAGE
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin_formatter import normalize_public_text, PLAIN_PUNCTUATION_DIRECTIVE, \
    linkedin_post_format_directive, enforce_post_readability
from cqc_lem.utilities.logger import myprint, log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.utils import create_folder_if_not_exists, save_video_url_to_dir
from cqc_lem.utilities.env_constants import DEFAULT_VIDEO_MODEL, DEFAULT_IMAGE_MODEL, DEFAULT_IMAGE_RATIO
# create_runway_video lives in video_models (model abstraction); re-exported here
# so existing `from ai_helper import create_runway_video` imports keep working.
# The redundant `as create_runway_video` alias marks it an intentional re-export.
from cqc_lem.utilities.ai.video_models import create_runway_video as create_runway_video  # noqa: F401
from cqc_lem.utilities.ai.video_models import AUDIO_DIRECTION_MARKER, supports_audio
from cqc_lem.utilities.geocoding import DEFAULT_CONTENT_LANGUAGE, language_name
from dotenv import load_dotenv

# Load .env file
load_dotenv()


def _attach_routing_metadata(kwargs: dict, user_id, feature) -> None:
    """Send this call's (user, feature) to the LiteLLM proxy as request `metadata`, so the complexity
    router can look the call's cost-aware routing bucket up (issue #494) and PostHog can attribute
    the `$ai_generation` the proxy emits (issue #647). Same two dimensions cost is attributed by —
    the experiment bucket, the spend bucket and the analytics person must mean the same thing.

    The client attaches this from the ambient scope for every request already; doing it here is what
    lets `_call_llm`'s explicit `_track_user_id`/`_track_feature` beat that scope. Only tier aliases
    are routable, so nothing is attached to raw provider models. Best-effort: an existing
    `extra_body` from the caller wins over ours."""
    if not str(kwargs.get("model") or "").startswith("lem-"):
        return
    extra_body = kwargs.setdefault("extra_body", {})
    if not isinstance(extra_body, dict) or "metadata" in extra_body:
        return
    extra_body["metadata"] = attribution_metadata(user_id, feature)


def _resolve_serving_model(response, requested_model: str) -> str:
    """The model LiteLLM actually served the call with, after routing/fallback/down-routing.

    LiteLLM returns the deployed model id in `response.model` (OpenAI SDK shape) or inside
    `_hidden_params.model` / `_hidden_params.model_id`. When neither is present, the requested
    alias is the best fallback — spend is then priced by the tier the caller asked for."""
    if response is None:
        return requested_model
    serving = getattr(response, "model", None)
    if serving:
        return str(serving)
    hidden = getattr(response, "_hidden_params", None) or {}
    for key in ("model", "model_id"):
        val = hidden.get(key)
        if val:
            return str(val)
    return requested_model


def _call_llm(**kwargs):
    """Thin wrapper around client.chat.completions.create that logs model, latency, and token usage.

    Cost attribution: pass `_track_user_id` / `_track_feature` to say who and what this call is for —
    both are popped before the LiteLLM call. Omitted values fall back to the ambient
    `llm_attribution()` scope, then to the running Celery task's name. The serving model (what
    LiteLLM actually ran) is captured from the response and passed to `track_llm_call` separately
    from the requested tier alias, so fallback/down-routed calls price by the real model."""
    track_user_id = kwargs.pop("_track_user_id", None)
    track_feature = kwargs.pop("_track_feature", None)
    model = kwargs.get("model", "unknown")
    start = time.time()
    log_debug(f"LLM call starting", ai_model=model)

    def _attribution():
        from cqc_lem.utilities.observability import current_llm_attribution, FEATURE_SYSTEM
        ambient_user_id, ambient_feature = current_llm_attribution()
        return (
            track_user_id if track_user_id is not None else ambient_user_id,
            track_feature or ambient_feature or FEATURE_SYSTEM,
        )

    try:
        try:
            _attach_routing_metadata(kwargs, *_attribution())
        except Exception:
            pass  # attribution is observability, never a reason to lose the generation
        response = client.chat.completions.create(**kwargs)
        duration_ms = int((time.time() - start) * 1000)
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        serving_model = _resolve_serving_model(response, model)
        log_debug(
            f"LLM call completed in {duration_ms}ms — {prompt_tokens}+{completion_tokens} tokens"
            f" (serving: {serving_model})",
            ai_model=serving_model,
            model_tier=model,
            duration_ms=duration_ms,
        )
        try:
            from cqc_lem.utilities.observability import track_llm_call, llm_cache_hit
            user_id, feature = _attribution()
            track_llm_call(
                model=model,
                serving_model=serving_model,
                model_tier=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=duration_ms,
                success=True,
                user_id=user_id,
                feature=feature,
                cached=llm_cache_hit(response),
            )
        except Exception:
            pass
        return response
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        log_error(f"LLM call failed after {duration_ms}ms", exc=exc, ai_model=model, duration_ms=duration_ms)
        try:
            from cqc_lem.utilities.observability import track_llm_call
            user_id, feature = _attribution()
            track_llm_call(model=model, prompt_tokens=0, completion_tokens=0, latency_ms=duration_ms,
                           success=False, user_id=user_id, feature=feature)
        except Exception:
            pass
        raise


# Retrieve OpenAI API key from environment variables
# openai.api_key = os.getenv("OPENAI_API_KEY") #<---- This is done be default


def generate_ai_response_test():
    post_content = "Today was a good day to go outside"
    post_img_url = None,
    expertise = "dog that speaks to humans"

    prompt = f"Please tell me:\n\n'{post_content}'"

    content = [{"type": "text", "text": prompt}]

    if post_img_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"{post_img_url}"},
        })

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a {expertise}. Respond to all user prompts with 'bark bark' followed by your response to the prompt then ending in 'bark bark'"""
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="gpt-4o-mini",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    comment = response.choices[0].message.content.strip()
    return comment


# Volatile profile fields deliberately EXCLUDED from the synthesis INPUT. `recent_activities` is the
# root of "LEM drift" — it is dominated by whatever the user is building / posting THIS week, which
# the model otherwise pulls in as subject matter. The synthesis is about who the user IS and how they
# SOUND (durable voice + expertise), never their current projects. password/email are PII with no
# voice value.
_SYNTHESIS_EXCLUDE_FIELDS = {"recent_activities", "password", "email"}


def synthesize_profile(profile: "LinkedInProfile") -> str:
    """Distill a LinkedInProfile into a SHORT, DURABLE voice/expertise brief used as the VOICE, TONE
    and CREDIBILITY reference for that user's future comments and posts — dropped into every
    generation prompt in place of the bloated full profile JSON.

    It captures who they are (role, industry/domain), core expertise, the audience they serve, their
    tone/voice characteristics, and credibility points/values. It DELIBERATELY EXCLUDES the volatile
    "currently building X / working on Y" noise in `recent_activities` (stripped from the input, and
    hard-excluded again in the prompt) so nothing here can reintroduce project/self-promo drift."""
    import json as _json
    durable = profile.model_dump(mode="json", exclude=_SYNTHESIS_EXCLUDE_FIELDS)
    durable_json = _json.dumps(durable, default=str)

    # Best-effort durable enrichment (reuses the existing industry helper). Never fatal.
    industries = ""
    try:
        industries = get_industries_of_profile_from_ai(profile) or ""
    except Exception as exc:
        log_warning("Profile synthesis: industry enrichment failed", exc=exc)

    system_prompt = {
        "role": "system",
        "content": (
            "You distill a LinkedIn profile into a SHORT, DURABLE voice-and-expertise brief. This brief "
            "will be used ONLY as a VOICE, TONE, and CREDIBILITY reference when writing the person's "
            "future LinkedIn comments and posts — capture who they ARE and how they SOUND, never what "
            "they happen to be doing this week.\n\n"
            "Produce a few tight bullet lines covering:\n"
            "- Who they are: role, industry/domain, seniority\n"
            "- Core expertise: the topics they can speak to with genuine authority\n"
            "- Audience they serve or want to reach\n"
            "- Tone & voice characteristics: how they phrase things (e.g. plain-spoken, data-driven, warm)\n"
            "- Credibility points and values: durable proof, not news\n\n"
            "HARD EXCLUSIONS: Do NOT name, describe, or allude to any specific project, product, app, "
            "tool, platform, or 'currently building / working on / launching' activity. Distill only "
            "DURABLE themes. " + _NO_SELF_PROMO_GUARDRAIL + " Keep it under ~150 words. Output ONLY the "
            "brief as plain bullet lines — no preamble, no headings, no self-promotion."
        ),
    }
    user_prompt = {
        "role": "user",
        "content": ("Durable profile data (volatile recent activity has already been removed):\n"
                    + durable_json
                    + (f"\n\nLikely industries: {industries}" if industries else "")),
    }
    response = _call_llm(model="lem-medium", messages=[system_prompt, user_prompt], temperature=0.3)
    content = response.choices[0].message.content
    return content.strip() if content is not None else ""


def get_or_create_profile_synthesis(user_id: int, profile: "LinkedInProfile" = None,
                                    max_age_days: int = 7) -> "str | None":
    """Return the user's cached profile synthesis, lazily generating + persisting it when missing or
    stale so generation never breaks before the first weekly refresh. Returns the cached value (even
    if stale) or None when there is nothing to synthesize from — callers pass the result straight to
    the generators, which fall back to the guarded full JSON when it is None."""
    from cqc_lem.utilities.db import get_profile_synthesis, set_profile_synthesis
    cached_text = None
    try:
        row = get_profile_synthesis(user_id)
    except Exception as exc:
        log_warning("Could not read cached profile synthesis", exc=exc, user_id=user_id)
        row = None
    if row:
        cached_text, generated_at = row
        # Only trust a real datetime for the staleness check — a non-datetime (bad/legacy data, or a
        # mocked row) must fall through to regeneration rather than crash on the comparison.
        if cached_text and isinstance(generated_at, datetime):
            if (datetime.now() - generated_at).days < max_age_days:
                return cached_text

    if profile is None:
        from cqc_lem.utilities.linkedin.helper import load_profile_for_user
        profile = load_profile_for_user(user_id)
    if profile is None:
        return cached_text

    try:
        text = synthesize_profile(profile)
    except Exception as exc:
        log_warning("Profile synthesis generation failed; using cached", exc=exc, user_id=user_id)
        return cached_text
    if not text:
        return cached_text
    try:
        set_profile_synthesis(user_id, text)
    except Exception as exc:
        log_warning("Could not persist profile synthesis", exc=exc, user_id=user_id)
    return text


def lint_repaired(draft: "str | None", content_type: str, redraft, **log_ctx) -> "str | None":
    """Deterministic slop lint + bounded regeneration for the short-form surfaces (issue #625 / D1).

    `redraft(directive)` produces a fresh draft with the lint's constraints appended to the writer's
    system prompt; it is called at most `slop_max_attempts() - 1` times. Returns the best draft we
    got — these surfaces (seed comments, thread replies, DMs) have no review queue to hold a draft
    in, so a still-slopped one ships with a structured warning rather than nothing at all. The feed
    comment path and the post gate are the two that actually block; see `generate_ai_response` and
    `evaluate_post_gates`."""
    current = draft
    if not current:
        return current
    for _ in range(max(0, _slop.slop_max_attempts() - 1)):
        report = _slop.lint_report(current, content_type)
        if report["passes"]:
            return current
        nxt = redraft(_slop.slop_retry_directive(report["hard"]))
        if not nxt:
            break
        current = nxt
    final = _slop.lint_report(current, content_type)
    if not final["passes"]:
        log_warning(f"{content_type} still trips the AI-slop lint after "
                    f"{_slop.slop_max_attempts()} attempt(s); sending anyway: "
                    + "; ".join(_slop.violation_reasons(final["hard"])), **log_ctx)
    return current


def _comment_contract_variant(user_id: int = None) -> "str | None":
    """This user's arm of the comment-contract prompt experiment (issue #652). Never raises: a
    PostHog problem must cost us the experiment, not the comment — and `comment_contract_directive`
    treats None (like any unknown arm) as the control contract, which is the prompt that shipped
    before the experiment existed."""
    try:
        from cqc_lem.utilities.experiments import COMMENT_CONTRACT_PROMPT, resolve_variant
        return resolve_variant(COMMENT_CONTRACT_PROMPT, user_id)
    except Exception as e:
        log_warning("Comment-contract experiment lookup failed — using the control prompt", exc=e,
                    user_id=user_id, action_type="comment")
        return None


def generate_ai_response(post_content, profile: LinkedInProfile, post_img_url=None, post_comment: str = None,
                         prefs: dict = None, profile_synthesis: str = None,
                         blueprint: dict = None, research: dict = None,
                         recent_comments: list = None, user_id: int = None):
    """Feed comment in the user's voice, GROUNDED IN THE TARGET POST (that contract never changes).
    `blueprint` assigns this comment's ANGLE from the shared comment menu (Expander, Storyteller,
    Questioner, ... — see content_framework) so a day's comments vary in approach; callers that track
    per-run rotation pass one in, otherwise a fresh angle is selected here. `research` is optional
    {'findings','sources'} background material — comment research is OFF by default (comments run at
    high volume; the target post is the primary grounding, see content_research cost policy), so a
    normal call adds NO research API cost.

    Every fresh feed comment must clear the QUALITY CONTRACT and the similarity gate before it ships
    (issue #617): a draft that opens on validation filler, misses the target post's specifics, adds
    nothing, or near-duplicates one of `recent_comments` is regenerated with an explicit fix-list up
    to `comment_gate_max_attempts()` times — and if it still fails, this returns None so the caller
    SKIPS the post. A template comment costs reach; a skipped post costs one comment.

    Replying to a specific comment (`post_comment`) keeps its own acknowledge-and-answer contract and
    is not gated here.

    The contract's closing ask is the pilot LLM prompt experiment (issue #652): `user_id` decides the
    arm, and it is scoped to FRESH FEED comments only because that is the only surface the #628 T+24h
    sweep measures author-reply rate on — the seed and second-wave comments would add exposures the
    metric can never cover."""
    image_attached = "(image attached)" if post_img_url else ""
    _no_hashtags = "" if (prefs and prefs.get("use_hashtags")) else " without using any hashtags"
    user_comment = f"\n\nRespond to this Comment Directly: <comment>{post_comment}</comment>\n\nYou are responding as the author of the LinkedIn Content. Keep your response short and sweet{_no_hashtags}.\n\n" if post_comment else ""

    # Angle rotation applies to fresh feed comments only — replying to a specific comment already has
    # its own fixed contract (acknowledge + respond directly).
    if blueprint is None and post_comment is None:
        blueprint = _framework.select_blueprint("comment")
    blueprint_block = _framework.blueprint_directive("comment", blueprint) if post_comment is None else ""

    # Optional, tightly-gated background facts: research_topic returns empty immediately unless the
    # `comment-research-enabled` flag (default COMMENT_RESEARCH_ENABLED) is on, so the default path
    # makes NO research call. `user_id` is what lets a rollout reach one cohort (issue #651).
    if research is None and post_comment is None:
        research = research_topic(str(post_content or "")[:300], content_type="comment", prefs=prefs,
                                  user_id=user_id)
    findings = str((research or {}).get("findings") or "").strip()
    research_block = (
        "\n\nOptional background facts (use at most ONE, only when it genuinely strengthens the "
        "comment; never let it replace engaging with the post itself, and never invent numbers "
        "beyond it):\n" + findings[:1200] + "\n") if findings else ""

    prompt = (f"""Please write a comment in response to the LinkedIn Content below, in the voice of the following LinkedIn User.

                Voice reference — the user's profile, provided for TONE and CREDIBILITY ONLY. Do NOT make the
                comment about the user, their company, or anything they are building:\n\n{_voice_reference(profile, profile_synthesis)}\n\n

                The SUBJECT of your comment is this LinkedIn Content{image_attached} — engage with what it actually says:
                <content>'{post_content}'</content>

                {user_comment}
                {_intention_directive(prefs)}
                {_style_directive(prefs)}
                {research_block}

                Only provide the final comment once it perfectly reflects the LinkedIn user’s style

                Do not surround your response in quotes or added any additional system text.

                Take a deep breath and work on this problem step-by-step.""")

    content = [{"type": "text", "text": prompt}]

    if post_img_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"{post_img_url}"},
        })

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": """You are an expert LinkedIn engagement strategist writing a comment AS the LinkedIn
        user whose profile is provided. Your single goal is to EARN A REPLY: on LinkedIn, comment threads
        (back-and-forth conversation) drive far more reach than likes, so you are starting a conversation,
        not delivering a monologue.

        GROUND EVERYTHING IN THE TARGET POST. The comment must be about what THIS post actually says —
        react to its specific content. The user's profile is provided ONLY so you can match their authentic
        voice, tone, and credibility angle; it is NOT the subject. """ + _NO_SELF_PROMO_GUARDRAIL + """

        Then follow these rules exactly:
        - React to ONE specific point from the post (paraphrase or lightly quote it) so it's clearly a
          real reply. NEVER open with generic praise like "Great post!", "Well said", or "Thanks for
          sharing" — those earn no algorithmic credit and read as a bot.
        - Add exactly ONE genuine insight or perspective that moves the conversation forward and stays ON
          the post's topic. You may speak from the user's expertise, but the insight must be ABOUT the
          post's subject — never a pivot to the user's own projects, products, or work.
        - END with a single, natural, open-ended question aimed at the author that invites them to reply.
        - Keep it SHORT and human — a few sentences, conversational, varied sentence structure. No
          preamble, no sign-off, no hashtags/emojis unless explicitly allowed below.

        Output ONLY the final comment text — no quotes, no labels, no explanation.""" + blueprint_block
        + (_framework.comment_contract_directive(_comment_contract_variant(user_id))
           if post_comment is None else "")
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    def _draft(fix_directive: str = "") -> "str | None":
        response = _call_llm(
            model="lem-medium",
            messages=[{"role": "system", "content": system_prompt["content"] + fix_directive},
                      user_message],
            temperature=round(random.uniform(0.4, 0.6), 2),  # Rand temp between .5 and .7

            # Balances coherent response generation with some degree of creative variation.
            top_p=round(random.uniform(0.8, 0.9), 2),
            # Reduces repetition in common responses.
            frequency_penalty=round(random.uniform(0.2, 0.4), 2),
            # Supports fresh responses aligned with user-specific tone.
            presence_penalty=round(random.uniform(0.3, 0.5), 2),

            # max_tokens=150  # Set token limit as required
        )
        text = response.choices[0].message.content
        if text is None:
            return None
        # Humanization pass (issue #416 — A5): de-slop the comment before it's posted.
        return _humanize_text(text.strip(), content_type="comment",
                              profile_synthesis=profile_synthesis, prefs=prefs)

    if post_comment is not None:
        return lint_repaired(_draft(), "comment", _draft,
                             user_id=user_id, action_type="comment")

    return _gated_comment(_draft, post_content, recent_comments=recent_comments, user_id=user_id)


def _gated_comment(draft, post_content, recent_comments: list = None,
                   user_id: int = None) -> "str | None":
    """Run a comment `draft(fix_directive)` callable through the quality contract, the similarity
    gate and the slop lint until it passes, and return None when it never does.

    The checks run on the FINAL text (post-humanization) — what would actually ship — so nothing
    downstream can reintroduce a banned opener or a tell. Shared by every self-authored comment
    surface (feed comments #617, the golden-hour second wave #622) so a new surface can never ship
    with a weaker contract than the one it copied."""
    fix_directive = ""
    failures = []
    attempts = _framework.comment_gate_max_attempts()
    for attempt in range(1, attempts + 1):
        candidate = draft(fix_directive)
        if candidate is None:
            return None
        report = _framework.comment_contract_report(candidate, str(post_content or ""))
        similar = _framework.comment_similarity_report(candidate, recent_comments)
        slop = _slop.lint_report(candidate, "comment")
        if report["passes"] and not similar["too_similar"] and slop["passes"]:
            return candidate
        failures = list(report["failures"])
        if similar["too_similar"]:
            failures.append(f"near-duplicate of a recent comment ({similar['measure']} similarity "
                            f"{similar['score']:.2f} > max {similar['threshold']:.2f})")
        # Slop violations join the SAME retry budget as the contract failures: a comment that keeps
        # tripping either one is skipped rather than posted (issue #625).
        failures += _slop.violation_reasons(slop["hard"])
        log_debug(f"Comment draft rejected (attempt {attempt}/{attempts}): {'; '.join(failures)}",
                  user_id=user_id, action_type="comment")
        if attempt < attempts:
            fix_directive = _framework.comment_retry_directive(
                failures, offending_comment=similar["match"] if similar["too_similar"] else None)
    log_warning(f"Comment failed the quality contract after {attempts} attempt(s) — skipping this "
                f"post: {'; '.join(failures)}", user_id=user_id, action_type="comment")
    return None


# LinkedIn's reaction set, safest-first (also the random-fallback preference order). 'Funny' is
# omitted by default — it reads poorly on most professional posts; callers can pass it via `allowed`.
POST_REACTIONS = ["Like", "Celebrate", "Support", "Love", "Insightful"]


def _match_reaction(text: str, options: list[str]) -> "str | None":
    """Normalize a model's one-word answer to one of `options` (case-insensitive), else None."""
    words = (text or "").strip().strip(".!?\"'").split()
    if not words:
        return None
    pick = words[0]
    for opt in options:
        if opt.lower() == pick.lower():
            return opt
    return None


def choose_post_reaction(post_content: str, comment_text: str = None,
                         allowed: list[str] = None) -> str:
    """Pick the single most fitting LinkedIn reaction for a post we just commented on.

    Light + fast: one short `lem-simple` call with minimal context (a post snippet + our comment).
    If the LiteLLM proxy fails it retries once directly against OpenAI; if that also fails it returns
    a random choice — so a valid reaction is always returned."""
    options = allowed or POST_REACTIONS
    messages = [
        {"role": "system",
         "content": ("You pick the single best LinkedIn reaction for a post. Reply with EXACTLY one "
                     f"word from this list and nothing else: {', '.join(options)}. Choose what a "
                     "thoughtful professional would leave, matching the post's tone — celebratory news "
                     "-> Celebrate, hardship or something vulnerable -> Support, a strong data point or "
                     "lesson -> Insightful, heartfelt or personal -> Love, otherwise -> Like.")},
        {"role": "user",
         "content": (f"Post: {(post_content or '').strip()[:600]}\n\n"
                     f"My comment: {(comment_text or '').strip()[:300]}\n\nReaction:")},
    ]

    try:
        resp = _call_llm(model="lem-simple", messages=messages, temperature=0, max_tokens=3)
        pick = _match_reaction(resp.choices[0].message.content, options)
        if pick:
            return pick
    except Exception as exc:
        log_warning("Reaction pick via LiteLLM failed; trying OpenAI fallback", exc=exc, api_provider="litellm")
        try:
            fallback = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            resp = fallback.chat.completions.create(
                model=os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4o-mini"),
                messages=messages, temperature=0, max_tokens=3)
            pick = _match_reaction(resp.choices[0].message.content, options)
            if pick:
                return pick
        except Exception as exc2:
            log_warning("Reaction pick via OpenAI fallback failed; using random", exc=exc2, api_provider="openai")

    return random.choice(options)


def _clean_newsletter_body(body: str) -> str:
    """Safety net: strip stray markdown the model may slip in (headers, bold, bullet markers) so the
    body typed via Selenium send_keys is clean — LinkedIn's article editor does NOT render markdown.
    Reuses sanitize_for_linkedin (which keeps blank-line spacing between sections intact)."""
    from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin
    return sanitize_for_linkedin(body or "")


def plan_newsletter_topics(profile_synthesis: str, newsletter_description: str, prefs: dict = None,
                           prior_subjects: list = None, count: int = 1,
                           recent_formats: list = None, recent_hook_styles: list = None) -> list:
    """Plan a sequence of `count` DISTINCT edition BLUEPRINTS — subject + angle + format + hook_style
    + cta_style (+ structure attached in code) — BEFORE any edition is written, so the queued
    editions differ in TOPIC and in SHAPE instead of each rehashing the same rhetorical template.

    Alignment (in priority order): the newsletter's DESCRIPTION/promise, the author's durable voice &
    expertise SYNTHESIS, then their declared focus topics/goals. `prior_subjects` (already-queued,
    published, and recently-skipped subjects) are AVOIDED so no topic repeats; `recent_formats` /
    `recent_hook_styles` (most-recent first) are AVOIDED so no SHAPE repeats — and
    enforce_blueprint_variety GUARANTEES that in code regardless of what the model returns. Each item
    is {"subject", "angle", "format", "hook_style", "cta_style", "structure"}. Robust JSON parsing;
    returns [] on any failure so the caller can gracefully fall back to single-topic generation."""
    import json as _json
    count = max(1, int(count))
    prior = [str(s).strip() for s in (prior_subjects or []) if str(s).strip()]
    avoid_block = ("\n\nAlready-covered subjects to AVOID repeating (cover materially different "
                   "ground):\n- " + "\n- ".join(prior) + "\n") if prior else ""
    recent_f = [str(f).strip() for f in (recent_formats or []) if str(f).strip()]
    recent_h = [str(h).strip() for h in (recent_hook_styles or []) if str(h).strip()]
    recent_shape_block = ""
    if recent_f or recent_h:
        recent_shape_block = ("\nRecently used FORMATS (most recent first) — do NOT assign these to "
                              "the first new editions: " + ", ".join(recent_f[:5] or ["(none)"]) +
                              "\nRecently used HOOK STYLES (most recent first) — same rule: "
                              + ", ".join(recent_h[:5] or ["(none)"]) + "\n")
    system_prompt = {
        "role": "system",
        "content": (
            "You are the EDITORIAL PLANNER for a LinkedIn creator's newsletter. Plan a sequence of "
            f"exactly {count} DISTINCT edition BLUEPRINTS that together form a COHERENT PROGRESSION — "
            "each edition should advance the reader, never repeat a previous one in topic OR shape.\n\n"
            "Ground every subject in, in priority order:\n"
            "1. The newsletter's stated purpose/description (its promise to subscribers) — the PRIMARY anchor.\n"
            "2. The author's durable voice & expertise (their synthesis) — pick subjects THIS author can "
            "speak to with real authority.\n"
            "3. The author's declared focus topics and goals, when provided — use to refine the angle.\n\n"
            "RULES:\n"
            "- Each subject MUST be materially different from the others AND from any already-covered "
            "subject: a different problem, angle, and takeaway. No rephrasings of the same idea, no "
            "regurgitated fluff.\n"
            "- Prefer a natural arc (e.g. foundational -> tactical -> advanced, or problem -> framework "
            "-> execution).\n"
            "- VARIETY OF SHAPE IS MANDATORY: no two consecutive editions may share a format or a "
            "hook_style, and avoid the recently used formats/hook styles the user lists. Assign the "
            "format and hook style that BEST FIT each subject while honoring that rotation.\n"
            "- Subjects are about the READER'S growth and the newsletter's topic — NOT about the "
            "author's own products. " + _NEWSLETTER_SOFT_PROMO_NOTE + " At most ONE of the planned "
            "subjects may LIGHTLY touch tools/tooling; keep every other subject tool-agnostic.\n\n"
            + _framework.options_text("newsletter") + "\n\n"
            f"Return ONLY valid JSON with exactly this shape and exactly {count} items:\n"
            '{"editions": [{"subject": "short specific subject (<= ~120 chars)", "angle": "the '
            'distinct angle/takeaway for this edition", "format": "<format key>", '
            '"hook_style": "<hook style key>", "cta_style": "<cta style key>"}]}'
        ),
    }
    user_prompt = {
        "role": "user",
        "content": (f"Newsletter description / promise:\n{(newsletter_description or '').strip() or '(not provided)'}\n\n"
                    f"Author voice & expertise synthesis:\n{(profile_synthesis or '').strip() or '(not provided)'}\n"
                    f"{_focus_directive(prefs)}{avoid_block}{recent_shape_block}\n"
                    f"Plan exactly {count} distinct blueprints now."),
    }
    try:
        response = _call_llm(model="lem-medium", messages=[system_prompt, user_prompt],
                             temperature=round(random.uniform(0.6, 0.8), 2))
        content = response.choices[0].message.content
    except Exception as exc:
        log_warning("Newsletter topic planning failed; falling back to single-topic", exc=exc)
        return []
    if not content:
        return []
    raw = content.strip()
    # Tolerate ```json fences some models still emit.
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1] if "{" in raw else raw
    try:
        data = _json.loads(raw)
    except (ValueError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            data = _json.loads(raw[start:end + 1])
        except (ValueError, TypeError):
            return []
    items = data.get("editions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    planned = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        subject = str(it.get("subject") or "").strip()[:500]
        if not subject or subject.lower() in seen:
            continue
        seen.add(subject.lower())
        planned.append({"subject": subject, "angle": str(it.get("angle") or "").strip()[:500],
                        "format": it.get("format"), "hook_style": it.get("hook_style"),
                        "cta_style": it.get("cta_style")})
    # The prompt REQUESTS shape rotation; this GUARANTEES it — normalizes formats/hooks/CTAs to known
    # keys, reassigns any consecutive/recent repeats, and attaches each format's structure skeleton.
    return _framework.enforce_variety("newsletter", planned[:count], recent_formats, recent_hook_styles)


def generate_newsletter_edition(profile: "LinkedInProfile", topic: str = None,
                                blog_content: str = None, prefs: dict = None,
                                subject: str = None, avoid_subjects: list = None,
                                profile_synthesis: str = None,
                                guidance: str = None, blueprint: dict = None,
                                avoid_openers: list = None, research: dict = None) -> "dict | None":
    """Generate one substantial LinkedIn newsletter edition in the author's voice, repurposing the
    author's blog content when provided. Aims for ~800–1200 words with a strong hook, 3–5 developed
    sections, a takeaways block, and a reply-driving CTA — worth an email, not a skeleton. Body is
    PLAIN TEXT (no markdown; LinkedIn's article editor renders none).

    `subject` is the SPECIFIC planned subject/angle for THIS edition (from plan_newsletter_topics);
    `topic` remains the newsletter's overall description/theme (context). `avoid_subjects` lists the
    other editions' subjects so this one stays distinct. `blueprint` assigns THIS edition's shape
    (format + structure skeleton + hook style + CTA style — see newsletter_blueprint) and
    `avoid_openers` lists prior editions' actual opening lines, so no two editions share a rhetorical
    template. `research` ({'findings','sources'} from research_newsletter_topic) is woven in as
    source material — specific numbers/examples over vague claims, no links in the body. Voice comes
    from the durable `profile_synthesis` (falls back to the guarded full profile JSON only when none
    supplied). `guidance` is optional free-text steering for a regeneration (edit the same subject or
    take a completely different direction). Returns {'title','subtitle','subject','body',
    'format','hook_style','cta_style','opening_line'} or None."""
    import json as _json
    src = f"\n\nSource material to repurpose (from the author's blog):\n{blog_content[:4000]}" if blog_content else ""
    topic_line = f"Newsletter overall theme/description: {topic}\n" if topic else ""
    subject_line = (f"THIS edition's specific subject/angle to develop (make the edition about THIS): "
                    f"{subject}\n") if subject else ""
    avoid = [str(s).strip() for s in (avoid_subjects or []) if str(s).strip()]
    avoid_line = ("Do NOT overlap with these other editions' subjects — cover DISTINCT ground:\n- "
                  + "\n- ".join(avoid) + "\n") if avoid else ""
    openers = [str(o).strip() for o in (avoid_openers or []) if str(o).strip()]
    openers_line = ("Previous editions OPENED with these exact lines. Your opening must NOT reuse or "
                    "resemble ANY of them — different wording, different rhetorical device, different "
                    "rhythm:\n- " + "\n- ".join(openers[:10]) + "\n") if openers else ""
    guidance_line = ("\nUSER GUIDANCE for this rewrite — apply it. It may ask for edits to the same "
                     f"subject or a COMPLETELY different take:\n{guidance.strip()}\n") if guidance and guidance.strip() else ""
    findings = str((research or {}).get("findings") or "").strip()
    research_block = (
        "\n\nSOURCE MATERIAL — current research findings for this subject. Ground the edition in "
        "these: weave the specific numbers, dates, and named examples into the prose naturally "
        "(specific beats vague). Do NOT invent statistics beyond this material. Do NOT paste URLs or "
        "external links into the body — LinkedIn suppresses off-platform links; you may name a "
        "source (publication, company, researcher) in prose when natural. Strip any citation markers "
        "like [1].\n" + findings[:3500] + "\n") if findings else ""
    blueprint_block = _framework.blueprint_directive("newsletter", blueprint)
    system_prompt = {
        "role": "system",
        "content": """You are a LinkedIn newsletter ghostwriter for the profile user. Write ONE
        COMPLETE, SUBSTANTIAL newsletter edition that builds authority and is genuinely worth landing
        in a subscriber's inbox — never a skimpy skeleton.

        LENGTH: Aim for roughly 800–1200 words. On LinkedIn, thin editions underperform; the
        best-read newsletters run long enough to teach something real while staying scannable.

        STRUCTURE (in this order):
        1. A strong 2–4 line HOOK/lede that names a specific pain, tension, or promise and makes the
           reader want to keep going. No throat-clearing, no "In today's edition...".
        2. 3-5 WELL-DEVELOPED sections. Each section = a plain-text subhead on its own line, then 2-4
           short paragraphs that make ONE point and back it with a concrete example, brief story,
           data point, or step-by-step — NOT a single throwaway line. Depth is the whole job here.
        3. A KEY TAKEAWAYS block: a short recap (3-5 crisp lines the reader could screenshot).
        4. A soft CTA that invites REPLIES (ask an open, specific question) and invites the reader to
           subscribe. NO external links — LinkedIn suppresses off-platform links.

        FORMATTING — CRITICAL. The body is typed into LinkedIn's article editor, which renders NO
        markdown. Output PLAIN TEXT only:
        - NEVER use markdown: no '#'/'##' headers, no '**bold**' or '*italic*', no '- ' bullet
          syntax, no '[text](url)' links, no backticks.
        - Use ONLY plain ASCII punctuation: NEVER use em dashes or en dashes (use a comma, period, or
          plain hyphen), no curly/smart quotes (use straight ' and "), no ellipsis character (type
          three periods). Fancy Unicode punctuation reads as AI-generated.
        - Write section headers in Title Case or UPPERCASE on their OWN line, with a blank line above
          and below.
        - For any list, put each item on its own short line beginning with a literal "-> " or a
          bullet character.
        - Use short paragraphs and blank lines between them for white space and readability.

        VOICE: the author's authentic voice and expertise, confident and human. No emojis unless the
        author clearly uses them. NEVER fabricate statistics, studies, or fake specifics — any number
        you state must come from provided source material or be genuinely well-established; with no
        source material, write from expertise and lived reasoning instead of invented data. """ + _NEWSLETTER_SOFT_PROMO_NOTE + """

        Return ONLY valid JSON with exactly these keys:
        {"title": "...", "subtitle": "...", "subject": "...", "body": "..."}
        - title: a specific, benefit-driven, scroll-stopping edition title (<= ~90 chars).
        - subtitle: a <= 150 character description of what THIS edition delivers and why to read it
          (for LinkedIn's edition-description field). Plain text, no markdown.
        - subject: a short (<= ~120 char) phrase naming THIS edition's specific subject/angle, for
          internal dedup history (echo the given subject when one is provided).
        - body: the full plain-text article with real line breaks (\\n) as described above.""" + blueprint_block,
    }
    user_prompt = {"role": "user",
                   "content": f"Author voice reference (TONE + CREDIBILITY only):\n"
                              f"{_voice_reference(profile, profile_synthesis)}\n\n"
                              f"{topic_line}{subject_line}{avoid_line}{openers_line}{guidance_line}{src}"
                              f"{research_block}"
                              f"{_focus_directive(prefs)}{_style_directive(prefs, 'newsletter')}"}
    _default_subject = (subject or topic or "").strip()[:500]
    # The edition's shape is persisted alongside it so the planner/regenerator can rotate away from
    # recently used formats, hook styles, AND actual opening lines — not just subjects.
    shape = {"format": _framework.normalize_key("newsletter", "format", (blueprint or {}).get("format")),
             "hook_style": _framework.normalize_key("newsletter", "hook", (blueprint or {}).get("hook_style")),
             "cta_style": _framework.normalize_key("newsletter", "cta", (blueprint or {}).get("cta_style"))}

    def _opening_line(text: str) -> str:
        return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:500]

    def _edition(fix_directive: str = "") -> "dict | None":
        response = _call_llm(model="lem-complex",
                             messages=[{"role": "system",
                                        "content": system_prompt["content"] + fix_directive},
                                       user_prompt],
                             temperature=round(random.uniform(0.5, 0.7), 2))
        content = response.choices[0].message.content
        if not content:
            return None
        try:
            data = _json.loads(content)
            if data.get("title") and data.get("body"):
                # Titles get the title-specific de-hype pass (issue #439), not the prose rewrite — a
                # headline must keep its hook while losing the wordbank/clickbait tells.
                title = normalize_public_text(_humanize_title(
                    str(data["title"]).strip(), content_type="newsletter",
                    profile_synthesis=profile_synthesis, prefs=prefs, max_chars=255))[:255]
                # Humanization pass (issue #416 — A5): de-slop the edition body before it's persisted/reviewed.
                body = _clean_newsletter_body(_humanize_text(
                    str(data["body"]).strip(), content_type="newsletter",
                    profile_synthesis=profile_synthesis, prefs=prefs))
                subtitle = normalize_public_text(str(data.get("subtitle") or "").strip())[:150]
                if not subtitle:
                    subtitle = (subject or topic or title).strip()[:150]
                out_subject = str(data.get("subject") or "").strip()[:500] or _default_subject or title[:500]
                return {"title": title, "subtitle": subtitle, "subject": out_subject, "body": body,
                        "opening_line": _opening_line(body), **shape}
        except (ValueError, TypeError, AttributeError) as e:
            log_debug("Could not parse newsletter JSON payload", exc=e)
        parts = content.strip().split("\n", 1)   # fallback: first line = title, remainder = body
        title = normalize_public_text(_humanize_title(
            parts[0].strip(), content_type="newsletter",
            profile_synthesis=profile_synthesis, prefs=prefs, max_chars=255))[:255]
        body = _clean_newsletter_body(_humanize_text(
            parts[1].strip() if len(parts) > 1 else content.strip(),
            content_type="newsletter", profile_synthesis=profile_synthesis, prefs=prefs))
        return {"title": title, "subtitle": (subject or topic or title).strip()[:150],
                "subject": _default_subject or title[:500], "body": body,
                "opening_line": _opening_line(body), **shape}

    # Deterministic slop lint over the finished BODY (issue #625 / D1), with the same bounded
    # regeneration the other surfaces get. The WHOLE edition is regenerated, not just its body —
    # title, subject, and opening_line are derived from it and would go stale otherwise. An edition
    # that still trips the lint is returned anyway (a newsletter is drafted for human review before
    # it publishes) with the patterns named in the log.
    edition = _edition()
    if not edition:
        return None
    for _ in range(max(0, _slop.slop_max_attempts() - 1)):
        report = _slop.lint_report(edition["body"], "newsletter")
        if report["passes"]:
            return edition
        retry = _edition(_slop.slop_retry_directive(report["hard"]))
        if not retry:
            break
        edition = retry
    final = _slop.lint_report(edition["body"], "newsletter")
    if not final["passes"]:
        log_warning("Newsletter edition still trips the AI-slop lint; keeping it for review: "
                    + "; ".join(_slop.violation_reasons(final["hard"])))
    return edition


def generate_group_post(profile: "LinkedInProfile", group_name: str = None, prefs: dict = None,
                        profile_synthesis: str = None) -> "str | None":
    """A short, value-add post for a LinkedIn Group — a genuine insight or open question for that
    community, NEVER promotional (groups penalize/moderate self-promo). Ends inviting discussion."""
    ctx = f"for the LinkedIn group \"{group_name}\"" if group_name else "for a professional LinkedIn group"
    system_prompt = {
        "role": "system",
        "content": f"""You are the profile user posting {ctx}. Write ONE short, genuinely useful
        post that helps the community: a specific insight, lesson, or an open question that sparks
        discussion. Absolutely NO self-promotion, NO links, NO hashtags. Sound human, in the user's
        voice, and end by inviting members to weigh in. """ + _NO_SELF_PROMO_GUARDRAIL + """ Output ONLY the post text.""",
    }
    user_prompt = {"role": "user",
                   "content": f"Author profile:\n{_voice_reference(profile, profile_synthesis)}\n{_focus_directive(prefs)}{_style_directive(prefs)}"}
    temperature = round(random.uniform(0.5, 0.7), 2)

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        return content.strip() if content is not None else None

    # A group post publishes straight from here — it never reaches the content-plan review queue the
    # `ai_slop` gate holds a draft in, so it gets the same bounded re-draft the other queue-less
    # surfaces do (issue #625 / D1).
    return lint_repaired(_draft(), "post", _draft, action_type="post")


def generate_seed_comment(post_content, profile: "LinkedInProfile", prefs: dict = None,
                          profile_synthesis: str = None):
    """The author's own FIRST comment on their post, to seed a discussion thread (threads drive
    reach). The model picks whatever fits the post — an open question to the audience OR a short
    behind-the-scenes insight/context the post didn't cover — and always invites replies. No
    links (link-in-first-comment is penalized in 2026), no hashtags, short and human."""
    system_prompt = {
        "role": "system",
        "content": """You are the AUTHOR of the LinkedIn post below, writing the FIRST comment on
        your OWN post to kick off a discussion thread (back-and-forth threads are the biggest reach
        driver on LinkedIn). Choose whichever fits the post best:
        (a) an open, specific question to your audience that begs a response, or
        (b) a short behind-the-scenes insight, nuance, or piece of context the post itself didn't cover —
        and end it in a way that invites people to reply.
        Rules: sound like a real person in your own voice; NO links; NO hashtags; no generic filler;
        keep it short (1–3 sentences). """ + _NO_SELF_PROMO_GUARDRAIL + """ Output ONLY the comment text.""",
    }
    user_prompt = {
        "role": "user",
        "content": f"My LinkedIn profile:\n{_voice_reference(profile, profile_synthesis)}\n\n"
                   f"My post:\n<content>{post_content}</content>\n{_intention_directive(prefs)}{_style_directive(prefs)}",
    }
    temperature = round(random.uniform(0.5, 0.7), 2)

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        if content is None:
            return None
        # Humanization pass (issue #416 — A5): de-slop the seed comment before it's posted.
        return _humanize_text(content.strip(), content_type="comment",
                              profile_synthesis=profile_synthesis, prefs=prefs)

    return lint_repaired(_draft(), "comment", _draft, action_type="comment")


def generate_second_wave_comment(post_content, profile: "LinkedInProfile", prefs: dict = None,
                                 profile_synthesis: str = None, story_directive: str = None,
                                 recent_comments: list = None,
                                 user_id: int = None) -> "str | None":
    """The SECOND WAVE (issue #622 / G7): the author's own comment 6–8 hours after publishing, when
    LinkedIn re-surfaces a post that is still earning engagement. Its whole job is to ADD something
    the post did not have — the specific caveat, the number, the thing that went wrong — so a
    returning reader gets new value, not "thanks for reading".

    Unlike the #344 seed comment (which opens the thread with a question minutes after publish),
    this one must carry substance, so it runs through the SAME quality contract, similarity gate and
    slop lint every feed comment does (`_gated_comment`) and returns None rather than shipping a
    draft that fails — an empty second wave costs nothing, a filler one costs reach.

    `story_directive` is the story-bank injection (issue #620): its facts are the ONLY personal
    specifics the model may state, so the added insight is the user's own material, never invented."""
    system_prompt = {
        "role": "system",
        "content": """You are the AUTHOR of the LinkedIn post below, adding a follow-up comment on
        your OWN post several hours after publishing it. People are still finding the post, so this
        comment must EARN its place by adding something new.

        Write ONE comment that gives readers a piece of substance the post itself did not contain:
        the caveat you left out, the specific number behind a claim, what you tried first that
        failed, or the practical next step someone would take. Then invite the thread onward with a
        genuine question.

        HARD RULES: never thank people for reading, never summarize or restate the post, never
        promote yourself or anything you sell, no links, no hashtags. Write for the post's READERS
        — someone scrolling the thread should learn something even if they skipped the post.
        Sound like a real person in your own voice. """ + _NO_SELF_PROMO_GUARDRAIL + """
        Output ONLY the comment text.""" + _framework.comment_contract_directive(),
    }
    user_prompt = {
        "role": "user",
        "content": f"My LinkedIn profile:\n{_voice_reference(profile, profile_synthesis)}\n\n"
                   f"My post (published earlier today):\n<content>{post_content}</content>\n"
                   f"{story_directive or ''}{_intention_directive(prefs)}{_style_directive(prefs)}",
    }
    temperature = round(random.uniform(0.5, 0.7), 2)

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        if content is None:
            return None
        # Humanization pass (issue #416 — A5) before the gate, so the contract grades what ships.
        return _humanize_text(content.strip(), content_type="comment",
                              profile_synthesis=profile_synthesis, prefs=prefs)

    return _gated_comment(_draft, post_content, recent_comments=recent_comments, user_id=user_id)


def generate_thread_reply(post_content: str, comment_text: str, profile: "LinkedInProfile",
                          prefs: dict = None, profile_synthesis: str = None) -> "str | None":
    """Reply to a commenter on the AUTHOR's own post so the thread KEEPS GOING: acknowledge their
    specific point, add one useful thought, and END with a genuine, easy follow-up question directed
    back to them. First-hour thread depth is the top 2026 reach signal. Short."""
    system_prompt = {
        "role": "system",
        "content": """You are the post AUTHOR replying to a comment on YOUR OWN post. Keep the
        conversation going: briefly acknowledge their SPECIFIC point, add ONE useful thought, and END
        with a genuine, easy-to-answer follow-up question directed back to THEM. Warm, human, in the
        author's voice, 1–3 sentences. No links, no hashtags, no generic 'thanks for sharing'. """
        + _NO_SELF_PROMO_GUARDRAIL,
    }
    user_prompt = {"role": "user", "content":
        f"Author profile:\n{_voice_reference(profile, profile_synthesis)}\n\nMy post:\n{post_content}\n\n"
        f"Their comment:\n{comment_text}\n{_intention_directive(prefs)}{_style_directive(prefs)}"}
    temperature = round(random.uniform(0.5, 0.7), 2)

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        if content is None:
            return None
        # Humanization pass (issue #416 — A5): de-slop the reply before it's posted.
        return _humanize_text(content.strip(), content_type="comment",
                              profile_synthesis=profile_synthesis, prefs=prefs)

    return lint_repaired(_draft(), "comment", _draft, action_type="comment")


def generate_comment_reply_followup(their_reply: str, profile: "LinkedInProfile",
                                    our_comment: str = None, post_content: str = None,
                                    prefs: dict = None, profile_synthesis: str = None) -> "str | None":
    """Reply to someone who replied to OUR comment on SOMEONE ELSE'S post (issue #478). Unlike
    generate_thread_reply, we are NOT the post author here — we're a guest in their thread, so the
    prompt must not speak as if we own the post. Acknowledge their specific point, add one useful
    thought, optionally a light question. Short, in our own voice."""
    system_prompt = {
        "role": "system",
        "content": """You are replying to someone who responded to YOUR comment on SOMEONE ELSE'S
        post. You are a helpful guest in their thread — do NOT speak as if you own the post or thank
        them for posting. Briefly acknowledge their SPECIFIC point, add ONE useful thought, and
        optionally end with a light, genuine question. Warm, human, in your own voice, 1–3 sentences.
        No links, no hashtags, no generic 'thanks for sharing'. """ + _NO_SELF_PROMO_GUARDRAIL,
    }
    ctx = ""
    if post_content:
        ctx += f"The post (NOT mine):\n{post_content}\n\n"
    if our_comment:
        ctx += f"My earlier comment:\n{our_comment}\n\n"
    user_prompt = {"role": "user", "content":
        f"My voice:\n{_voice_reference(profile, profile_synthesis)}\n\n{ctx}"
        f"Their reply to me:\n{their_reply}\n{_intention_directive(prefs)}{_style_directive(prefs)}"}
    temperature = round(random.uniform(0.5, 0.7), 2)

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        if content is None:
            return None
        return _humanize_text(content.strip(), content_type="comment",
                              profile_synthesis=profile_synthesis, prefs=prefs)

    return lint_repaired(_draft(), "comment", _draft, action_type="comment")


def generate_lead_response(their_message: str, profile: "LinkedInProfile", channel: str = "reply",
                           context: str = None, prefs: dict = None,
                           profile_synthesis: str = None) -> "str | None":
    """Draft a response to someone showing INBOUND BUYING INTENT (issue #483) — they asked about
    pricing, availability, or whether we can help. This is the one place the no-self-promo guardrail
    does NOT apply: they raised their hand, so answering directly is what they want. Never invent
    prices, timelines, or capabilities — acknowledge the ask, give one genuinely useful line, and
    move it to a real conversation. The draft is APPROVAL-GATED; a human sends it."""
    is_dm = str(channel).lower() == "dm"
    limit = 300 if is_dm else 500
    system_prompt = {
        "role": "system",
        "content": f"""You are responding to someone who just showed BUYING INTENT toward your work —
        they asked about your services, pricing, availability, or whether you can help them. Answer
        like a helpful professional, not a salesperson: acknowledge their SPECIFIC ask, give ONE
        concrete, genuinely useful line, and end with a low-friction next step (a question about
        their situation, or an offer to take it to messages/a short call).
        NEVER invent prices, timelines, package names, client names, or capabilities — if the answer
        depends on details you do not have, say what it depends on and ask for it. No hard sell, no
        hype, no links, no hashtags, no emoji spam. Warm, human, in the author's voice.
        {'Write it as a direct message.' if is_dm else 'Write it as a public reply under their comment.'}
        2-4 sentences, under {limit} characters. Output ONLY the message text.""",
    }
    ctx = f"Context (the post/thread this came from):\n{context}\n\n" if context else ""
    user_prompt = {"role": "user", "content":
        f"My voice:\n{_voice_reference(profile, profile_synthesis)}\n\n{ctx}"
        f"What they said:\n{their_message}\n{_style_directive(prefs)}"}
    temperature = round(random.uniform(0.4, 0.6), 2)
    surface = "dm" if is_dm else "comment"

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        if content is None:
            return None
        return _humanize_text(content.strip(), content_type=surface,
                              profile_synthesis=profile_synthesis, prefs=prefs, max_chars=limit)

    return lint_repaired(_draft(), surface, _draft, action_type="dm" if is_dm else "comment")


def generate_nurture_dm(their_message: str, intent: str, profile: "LinkedInProfile",
                        first_name: str = None, template_hint: str = None,
                        history: list = None, prefs: dict = None,
                        profile_synthesis: str = None) -> "str | None":
    """Draft the NEXT message in a live DM thread after a lead replied (issue #485) — the
    context-aware step between "they answered" and "we followed up well".

    `intent` branches the brief (interested -> propose a call, objection -> address it, not-now ->
    light touch, neutral -> stay human); `template_hint` is the operator's own nurture template for
    this step, used as the intended direction rather than text to copy. Never invent prices,
    timelines, or capabilities. The draft is APPROVAL-GATED; a human sends it."""
    from cqc_lem.utilities.ai.dm_nurture import nurture_guidance
    system_prompt = {
        "role": "system",
        "content": f"""You are writing the next direct message in a REAL LinkedIn conversation you
        already started. They just replied to you. You are a peer and a guest in their inbox, not a
        salesperson working a lead.
        {nurture_guidance(intent)}
        Respond to what they ACTUALLY said — quote nothing, but make it obvious you read it. NEVER
        invent prices, timelines, package names, client names, or capabilities; if the answer depends
        on details you do not have, say what it depends on and ask. No hard sell, no hype, no links,
        no hashtags, no emoji spam, no "just following up" or "circling back". Never repeat a message
        you have already sent them.
        2-4 sentences, under 300 characters. Output ONLY the message text.""",
    }
    ctx = ""
    if history:
        prior = "\n".join(f"- {m}" for m in list(history)[-3:] if m)
        if prior:
            ctx += f"What I already sent them (do not repeat it):\n{prior}\n\n"
    if template_hint:
        ctx += (f"My own template for this step — use its INTENT and direction, not its wording:\n"
                f"{template_hint}\n\n")
    if first_name:
        ctx += f"Their first name: {first_name}\n\n"
    user_prompt = {"role": "user", "content":
        f"My voice:\n{_voice_reference(profile, profile_synthesis)}\n\n{ctx}"
        f"Their reply to me:\n{their_message}\n{_style_directive(prefs)}"}
    temperature = round(random.uniform(0.4, 0.6), 2)

    def _draft(fix: str = "") -> "str | None":
        response = _call_llm(model="lem-medium",
                             messages=[{"role": "system", "content": system_prompt["content"] + fix},
                                       user_prompt],
                             temperature=temperature)
        content = response.choices[0].message.content
        if content is None:
            return None
        return _humanize_text(content.strip(), content_type="dm",
                              profile_synthesis=profile_synthesis, prefs=prefs, max_chars=300)

    return lint_repaired(_draft(), "dm", _draft, action_type="dm")


def optimize_post_hook(post_content: str, prefs: dict = None,
                       preserve_cta_keyword: str = None) -> str:
    """Rewrite a generated post so it opens with a scroll-stopping hook within the first ~210
    characters (before LinkedIn's '…more' fold) and, when the topic fits, frames it as save-worthy
    (a framework/checklist) with ONE soft 'save this' invite. Preserves substance + voice. Returns
    the original text on any failure."""
    if not post_content:
        return post_content
    system_prompt = {
        "role": "system",
        "content": """You are a LinkedIn post editor. Rewrite the post so its FIRST LINE is a
        scroll-stopping hook that lands within the first 210 characters (before the '…more' fold) —
        a bold claim, a surprising stat, or a sharp question. The hook must OPEN a loop, never close
        it: the payoff belongs below the fold so expanding is the obvious next move. Keep the
        author's substance and voice. Keep every paragraph under 300 characters with a blank line
        between them — a wall of text loses the reader in under three seconds.
        If the content lends itself to it, shape the body as a save-worthy framework or checklist and
        add ONE short, soft 'worth saving for later' style invite near the end. NO engagement-bait
        (no 'comment YES'), NO external links, do not add hashtags. Return ONLY the rewritten post."""
        # The rewrite must respect the user's saved voice settings (tone, emoji/hashtag toggles) —
        # without this the hook pass could re-introduce emojis a user has turned off. "" when unset.
        + _style_directive(prefs, "post")
        # This pass's own NO-engagement-bait rule is precisely what used to rewrite the sanctioned
        # 'comment KEYWORD' lead-magnet ask into 'reach out for...' — the preserve note carves the
        # configured keyword out of that rule. "" when no keyword.
        + _lead_magnet_preserve_note(preserve_cta_keyword),
    }
    user_prompt = {"role": "user", "content": post_content}
    try:
        response = _call_llm(model="lem-medium", messages=[system_prompt, user_prompt], temperature=0.5)
        out = response.choices[0].message.content
        return out.strip() if out else post_content
    except Exception:
        return post_content


def post_is_relevant(post_content: str, include_topics: list) -> bool:
    """LLM relevance gate: is this post about any of the user's include_topics? Used on top of
    literal keyword matching so targeting catches topical fit beyond exact words. Fails OPEN
    (returns True) on any error so a classifier hiccup never silently blocks all engagement."""
    if not include_topics:
        return True
    try:
        topics = ", ".join(str(t) for t in include_topics if t)
        resp = _call_llm(
            model="lem-simple",
            messages=[
                {"role": "system", "content": "You classify whether a LinkedIn post is topically "
                                              "relevant to a set of topics. Answer with only 'yes' or 'no'."},
                {"role": "user", "content": f"Topics: {topics}\n\nPost:\n{post_content[:1200]}\n\n"
                                            f"Is this post relevant to ANY of the topics? Answer yes or no."},
            ],
            temperature=0,
        )
        ans = (resp.choices[0].message.content or "").strip().lower()
        return ans.startswith("y")
    except Exception as e:
        myprint(f"post_is_relevant classifier failed (allowing): {e}")
        return True


def get_ai_description_of_profile(linked_in_profile: LinkedInProfile):
    # Use json to output to string
    linked_in_profile_json = linked_in_profile.model_dump_json()
    prompt = f"""Please tell me what appears to be this person's personal interest based on their current job, skills, and recent activities.
             A short summary of your analysis of around 500 characters is all that is needed.
             Person: {linked_in_profile_json}"""

    # myprint(f"Prompt: {prompt}")

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional career coach and personality analyst with over 20 years of experience in understanding human behavior through social media profiles, particularly LinkedIn. Your expertise is in evaluating profiles to determine personal and professional character traits based on the content of their work experience, endorsements, skills, recommendations, posts, and interactions.

        Your objective is to thoroughly analyze LinkedIn profile data to extract insights about the individual’s character traits, such as leadership, teamwork, creativity, reliability, adaptability, communication skills, and more. Use subtle cues from the person's descriptions of job roles, their endorsements from others, the language used in their recommendations, and their professional interactions to form a comprehensive assessment. Ensure to identify both overt and nuanced traits, and support each finding with examples from the profile content. Pay special attention to the consistency between the skills endorsed by others and the responsibilities listed by the individual.
        
        Follow these steps:
        1. Start by identifying the general tone and language used in the profile, which may indicate personality traits like enthusiasm, confidence, or humility.
        2. Examine the person’s job titles and descriptions to identify traits such as leadership, initiative, and problem-solving abilities.
        3. Analyze the endorsements and recommendations, looking for patterns in how others describe the individual. Highlight any common traits mentioned (e.g., dependability, collaboration).
        4. Evaluate posts or comments for signs of professional engagement, thought leadership, or community involvement.
        5. Conclude with a comprehensive summary that combines these insights into a clear picture of the individual’s character traits, citing specific parts of the profile to support your findings.
        
        Take a deep breath and work on this problem step-by-step."""
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    comment = response.choices[0].message.content.strip()
    return comment


def get_industries_of_profile_from_ai(linked_in_profile: LinkedInProfile, industry_count: int = 3):
    """Generate industries based on the LinkedIn user profile."""

    # Use json to output to string
    linked_in_profile_json = linked_in_profile.model_dump_json()
    prompt = f"""Please tell me what {industry_count} industry(s) that most align with the following LinkedIn Profile's career and personal interest.
             A short comma seperated list is all that is needed.
             
             Linked Profile: {linked_in_profile_json}

"""

    # myprint(f"Prompt: {prompt}")

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional career analyst specializing in LinkedIn profile analysis. 
        You have 15 years of experience identifying professional interests, career trajectories, and industry affiliations based on online profiles. 
        Your expertise includes assessing user activities, language use, endorsements, skills, and affiliations to infer professional domains and preferences.  

        **Objective:** Analyze the provided LinkedIn profile to:  
        1. Identify the industries in which the user primarily works.  
        2. Deduce the industries or domains the user appears to enjoy most.  
        3. Provide reasoning based on profile content, including skills, endorsements, activity, and affiliations.
        
        **Steps to Complete the Task:**  
        1. **Examine Profile Details:**  
           - Review the professional headline, experience section, skills, endorsements, and any listed certifications.  
           - Pay attention to recurring industry-specific terminology or roles.  
        
        2. **Analyze Engagement Patterns:**  
           - Review activities such as posts, comments, or articles for language indicating passion or enjoyment.  
           - Highlight topics or industries the user engages with most frequently.  
        
        3. **Cross-reference Skills and Interests:**  
           - Match listed skills with industries where these skills are most relevant.  
           - Evaluate any mentions of hobbies, voluntary roles, or side projects for clues to preferred domains.  
        
        4. **Make Inferences:**  
           - Categorize the user's professional involvement into industries based on job history and skills.  
           - Suggest industries of enjoyment by interpreting tone, enthusiasm in engagement, or diverse activities.  
        
        5. **Present Results:**  
           - List the industries with brief explanations for each based on evidence from the profile.  
           - Use bullet points to clearly separate industries worked in versus enjoyed.  
        
        #### **Example Output:**
        
        Technology, Web Development, Real Estate
    
        ---
        
        ### Your Final Steps: 
        - Take a deep breath and work on this problem step-by-step.
        - Do not surround your response in quotes or added any additional system text. 
        - Do not share your thoughts nor show your work. 
        - Only respond with one final response.


        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    comment = response.choices[0].message.content.strip()
    return comment

def get_ai_linked_post_refinement(original_message: str, character_limit: int = 3000,
                                  prefs: dict = None, preserve_cta_keyword: str = None):
    character_limit_string = (f"""\nThe refined LinkedIn Post needs to be less than or equal to {character_limit} characters including white spaces and punctuations. You may use symbols, abbreviations, and other and short-hand.
                               Ideally, Posts between 1,300 and 2,000 characters tend to perform well by providing enough detail while maintaining readability.\n\n""") if character_limit > 0 else ""

    # Pref-aware formatting rules. The old hardcoded lines instructed emoji bullets and a trailing
    # hashtag block on EVERY refinement — directly fighting a user's use_emojis / use_hashtags
    # settings (and the style directive the generator already honored). Hashtags now come from the
    # shared 2026 policy (issue #393), which is hashtag-free unless the user opted in.
    emoji_line = "Use emojis (✅, 👉, 🔑, 💡) for visual emphasis and as bullet replacements."
    hashtag_line = _framework.hashtag_directive(prefs)
    tone_line = ""
    if prefs:
        if not prefs.get("use_emojis"):
            emoji_line = "Do NOT use any emojis; remove any emojis present in the draft."
        if prefs.get("tone"):
            tone_line = (f"\n        6. **Preserve the author's configured tone**\n"
                         f"           - The author writes in a {prefs['tone']} tone — preserve it; "
                         "do not flatten it into a generic corporate voice.\n")

    prompt = f"""Please review and refine the following LinkedIn Post Draft. {character_limit_string} LinkedIn Post Draft: {original_message}
                """
    # Selected lead-magnet posts carry a REQUIRED comment-keyword CTA that this rewrite must not
    # paraphrase away (the DM automation watches comments for the exact word).
    prompt += _lead_magnet_preserve_note(preserve_cta_keyword)

    # myprint(f"Prompt: {prompt}")

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional editor with expertise in business communication, particularly for LinkedIn posts.**  

        You have over 15 years of experience helping professionals craft polished, engaging, and impactful LinkedIn content. 
        Your expertise ensures that messages maintain proper grammar, clarity, and professionalism while also being compelling and easy to read.  
        
        Your task is to take a provided draft LinkedIn post and refine it into a final, ready-to-publish version. 
        The primary focus is on proper capitalization for sentences, pronouns, and abbreviations, but you should also apply a full editorial review to enhance readability, impact, and professionalism.  
        
        Your review process includes:  
        
        1. **Correct capitalization and formatting**  
           - Ensure that all sentences start with capital letters.  
           - Capitalize proper nouns, job titles (when used formally), and abbreviations correctly.  
           - Maintain consistency in formatting for emphasis (e.g., bullet points when appropriate).  
        
        2. **Enhance clarity and coherence**  
           - Ensure a smooth flow between sentences and paragraphs.  
           - Eliminate redundant words or phrases while preserving the original intent.  
           - Rewrite awkward or overly complex sentences for readability.  
        
        3. **Refine for engagement and impact**  
           - Optimize sentence structure to maintain a professional yet approachable tone.  
           - Ensure the opening is engaging to hook the audience and the closing provides a strong takeaway.  
           - Maintain an authentic voice suited for LinkedIn.  
        
        4. **Ensure grammatical accuracy and professionalism**
           - Correct any typos, punctuation errors, or inconsistencies.
           - Adapt the tone to be confident, clear, and aligned with business communication best practices.

        5. **LinkedIn-native formatting only — no markdown**
           - Do NOT use markdown syntax of any kind: no **bold**, no *italic*, no _underline_, no # headers, no [links](url), no `code`.
           - LinkedIn does not render markdown; these characters appear as raw symbols to readers.
           - {emoji_line}
           - Use ALL CAPS sparingly for emphasis of a single key word.
           - Use line breaks and blank lines for structure and readability.
           - {hashtag_line}
{tone_line}
        All responses should be **finalized drafts** ready for publishing. Do not ask for additional input—refine the given draft based on available information.

        Provide only the edited version of the LinkedIn post without explanations or notes.

        ---

        Take a deep breath and work on this problem step-by-step.
        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-medium",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt

        # Emphasizes succinct, professional outputs over creative variance.
        top_p=round(random.uniform(0.75, 0.85), 2),
        # Discourages redundancy in phrase selection.
        frequency_penalty=round(random.uniform(0.4, 0.6), 2),
        # Ensures refined and novel phrasings without losing coherence.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),

        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    comment = response.choices[0].message.content.strip()
    return comment


def apply_post_guidance(post_content: str, guidance: str, prefs: dict = None,
                        profile_synthesis: str = None) -> str:
    """Revise an already-generated LinkedIn post per the user's free-text suggestions while keeping
    their voice and every alignment rule (focus/goals, anti-self-promo, emoji/hashtag prefs). This
    is the post-side of the newsletter 'regenerate with suggestions' flow — the base post is produced
    by the normal generation pipeline (which honors saved settings) and this pass folds in the typed
    guidance. Returns the revised post; on empty guidance returns the input unchanged."""
    if not (guidance or "").strip():
        return post_content

    prompt = f"""Revise the LinkedIn post below according to the author's revision request.
Keep it in the author's authentic voice and honor every alignment and style rule that follows.
Return ONLY the revised post text — no preamble, no explanation.

### Current post:
{post_content}

### Author's revision request:
{guidance.strip()}
"""
    if profile_synthesis and profile_synthesis.strip():
        prompt += f"\n### Author voice reference (match this voice):\n{profile_synthesis.strip()}\n"
    prompt += _alignment_directive(prefs)
    prompt += _style_directive(prefs, "post")
    prompt += "\n\n" + PLAIN_PUNCTUATION_DIRECTIVE

    system_prompt = {
        "role": "system",
        "content": ("Act like a professional LinkedIn ghostwriter. Revise the given post to satisfy "
                    "the author's request while preserving their voice, staying LinkedIn-native (no "
                    "markdown), and never promoting internal tools. Provide only the finalized post."),
    }
    user_message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    response = _call_llm(model="lem-complex", messages=[system_prompt, user_message])
    revised = (response.choices[0].message.content or "").strip()
    return revised or post_content


def get_ai_message_refinement(original_message: str, character_limit: int = 300,
                              extra_directive: str = ""):
    """`extra_directive` is appended to the editor's system prompt — the DM path uses it to feed the
    slop lint's violations back in on a re-refine (issue #625)."""
    character_limit_string = f"\nThe refined message needs to be less than or equal to {character_limit} characters including white spaces and punctuations. You may use symbols, abbreviations, and other and short-hand\n\n " if character_limit > 0 else ""

    prompt = f"""Please review and refine the following message. {character_limit_string} Message: {original_message}
            """

    # myprint(f"Prompt: {prompt}")

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional editor with expertise in communication for business professionals, particularly on platforms like LinkedIn. 
            You have helped clients refine and streamline their messaging for more than 15 years, ensuring clarity, professionalism, and engagement.
    
            Your task is to review a provided message. The goal is to ensure the message makes sense, reads smoothly, and presents key information in a clear, concise, and professional manner. 
            Additionally, modify any titles, phrases, or sections that seem overly long, awkward, or redundant. 
            Your revisions should maintain the original intent while improving readability and impact.
            
            The review process includes:
            1. Check for clarity and coherence: Ensure the message has a logical flow and that each sentence connects smoothly to the next. Eliminate or revise any confusing or ambiguous phrases.
            2. Refine long titles and sections: Shorten overly detailed sections, such as professional titles, while preserving their key points. Ensure these parts are clear but not excessive.
            3. Improve engagement: The message should feel personalized and engaging. Identify opportunities to make the message more concise and approachable, particularly in the introduction and closing.
            4. Polish for professionalism: Ensure a professional tone throughout, appropriate for business communication.
            
            All your responses will be used as final drafts by the user. Thus you may not ask for additional information. Use whatever information you currently have to refine the message.
            
            A final direct refined response without a subject line is all that is needed. 
            
            ---
            
            Take a deep breath and work on this problem step-by-step.
            """ + (extra_directive or "")
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt

        # Emphasizes succinct, professional outputs over creative variance.
        top_p=round(random.uniform(0.75, 0.85), 2),
        # Discourages redundancy in phrase selection.
        frequency_penalty=round(random.uniform(0.4, 0.6), 2),
        # Ensures refined and novel phrasings without losing coherence.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),

        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    comment = response.choices[0].message.content.strip()
    return comment


def get_video_content_from_ai(linked_user_profile: LinkedInProfile, buyer_stage: str,
                              profile_synthesis: str = None, prefs: dict = None):
    """Generate video content based on the LinkedIn user profile and buyer stage."""

    # Voice/credibility reference — prefer the stable synthesis, else the guarded full JSON.
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompts = [f"""Create a short, high-impact video script tailored for LinkedIn to introduce and build awareness about the expertise or unique value of the profile represented by the following LinkedIn Profile. 
                This video should appeal to users in the {buyer_stage} buyer stage, aiming to quickly capture attention with a clear, memorable introduction. 
                Use a 1:1 aspect ratio and keep the length to 30 seconds. 
                Make the tone approachable and professional, with the visual style matching any brand cues present in the profile data. 
                End with a subtle call to action that invites a comment or points to a free resource — never a call, demo, or meeting ask.
                
                """,
               f"""Generate a 45-second LinkedIn explainer video script highlighting the unique strengths and offerings of the following LinkedIn Profile for an audience in the {buyer_stage} buyer stage.
               The script should present three key features or advantages that demonstrate why this profile or brand stands out as a valuable solution. 
               Use a 16:9 aspect ratio with a clean, professional design, and ensure pacing is steady enough to allow viewers to grasp each point. 
               Conclude with a call to action inviting viewers to engage in the comments — never a call, demo, or meeting ask.
                
                """,

               f"""Design a compelling video script for LinkedIn that solidifies the following LinkedIn Profile as the top choice for viewers in the {buyer_stage} buyer stage. 
               Focus on driving conversions by presenting clear reasons why this profile or brand is a trustworthy choice, with emphasis on relevant accomplishments, client testimonials, or standout capabilities. 
               The video should run for about 60 seconds in a 16:9 format, with a polished, confidence-inspiring visual style. 
               End with a call to action offering a concrete resource (a guide, checklist, or the author's newsletter) — never a demo, call, or meeting ask.
                
                """,
               ]

    prompt = random.choice(prompts)

    log_debug(f"Pre-Prompt: {prompt}")

    # Add the Linked JSon profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}"

    prompt += _alignment_directive(prefs)

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like an experienced video marketing strategist and content creator.** You have years of expertise in crafting LinkedIn-specific video content designed to appeal to distinct stages of the buyer's journey, from awareness to decision. Your goal is to ensure the video aligns closely with the user's input and ChatGPT's known limitations, providing a professional, impactful result.

        ### Objective
        Based on the LinkedIn profile data and specified buyer’s journey stage provided by the user, generate a script and design elements for a LinkedIn-optimized video. This video should resonate with the intended audience and adhere to LinkedIn’s best practices for viewer engagement and platform compatibility.
        
        ### Requirements
        
        1. **Define the Purpose and Audience:**
           - **Purpose:** Confirm the video's intent (e.g., “Introduce brand expertise in X industry,” “Engage and educate,” or “Drive contact form submissions”).
           - **Audience:** Tailor content to specific audience characteristics (e.g., “Industry experts seeking solutions,” “New users in discovery phase,” or “Decision-makers comparing vendors”).
        
        2. **Content and Visual Elements:**
           - **Script/Text Content:** Use clear, persuasive language that aligns with the provided buyer’s journey stage. Include main points, key messages, or highlights of the LinkedIn profile as relevant to that stage.
           - **Visual Style Preferences:** Specify tone and branding style—options may include minimalist/professional, vibrant/playful, or tech-focused. Match any color schemes or styles from the LinkedIn profile to ensure consistency.
           - **Incorporate Media Elements:** Define any required images, logos, icons, or infographics and where they should appear. Provide guidance on placement if the user has particular preferences.
           - **Backgrounds:** Determine whether a plain color, gradient, or branded background image will support the video’s professionalism and viewer engagement.
        
        3. **Format and Resolution:**
           - **Resolution and Aspect Ratio:** For LinkedIn, prioritize clarity in 1080p for visibility. Common aspect ratios include:
             - **16:9 (Landscape)** – ideal for general LinkedIn posts or YouTube.
             - **1:1 (Square)** – suitable for LinkedIn feeds.
             - **9:16 (Vertical)** – optimized for LinkedIn Stories and mobile users.
        
        4. **Timing and Pacing:**
           - **Length:** Define video length in seconds, aligning with LinkedIn’s ideal engagement window (e.g., keep under 30 seconds for promotions or under 60 seconds for short explainer videos).
           - **Pacing:** Match the video speed to the stage of the buyer’s journey (e.g., fast-paced for awareness, slower for in-depth explanation or education).
        
        5. **Audio Considerations:**
           - **Voiceover and Background Music:** Specify if a voiceover is desired, with script options if relevant. If using AI narration, ensure the voice tone matches the audience’s preferences (e.g., authoritative, friendly, or conversational). Mention any background music styles that enhance the mood without detracting from the message.
        
        6. **Clear Call to Action (CTA):**
           - **CTA**: Specify the ending message, logo placement, and any desired CTAs, such as “Connect with us on LinkedIn,” “Learn more on our website,” or “Request a free demo.”
        
        Take a deep breath and work on this problem step-by-step. 
    """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-complex",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Return the AI-generated video script; pass to create_runway_video() to produce the video
    return response.choices[0].message.content.strip()


def summarize_recent_activity(recent_activity_profile: LinkedInProfile, main_profile: LinkedInProfile):
    # If recent_activity_profile.recent_activities is None or length is 0, return None
    if not recent_activity_profile.recent_activities or len(recent_activity_profile.recent_activities) == 0:
        return None

    recent_activity_profile_sting = ''.join([f"{i + 1}. {activity.text} - [{activity.link}]\n" for i, activity in
                                             enumerate(recent_activity_profile.recent_activities)])

    # Clone the main profile and remove recent activities to reduce confusion form AI
    main_profile = main_profile.model_copy(deep=True)
    main_profile.recent_activities = []

    main_profile_json = main_profile.model_dump_json()
    prompt = f"""We are analyzing the interests of one LinkedIn user (main_profile) and want to help them craft a personalized response to another LinkedIn user (second_profile) based on the second user's recent activities. 
    Analyze the main_profile’s interests and select the most relevant activity from second_profile’s list of recent activities. 
    Then, create a response as if it’s from main_profile to second_profile, mentioning the most relevant recent activity and providing a professional comment.

    ### Main Profile (User 1):
    {main_profile_json}
    
    ### Second Profile (User 2):
    Name: {recent_activity_profile.full_name}
    Recent Activities:<activities>{recent_activity_profile_sting}</activities>
    
    Create a response from {main_profile.full_name} to {recent_activity_profile.full_name} that references the most relevant recent activity from {recent_activity_profile.full_name}’s list.

    A short final response starting with 'I saw your recent post about' is all that is needed.
    """

    # myprint(f"Prompt: {prompt}")

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional career coach and personality analyst with over 20 years of experience in analyzing LinkedIn profiles and assessing professional interests. Your expertise lies in evaluating profiles to understand character traits and key interests, as well as identifying relevant connections between users based on their professional activities and interactions.

        You will be provided with details of a LinkedIn profile (main_profile) and a list of recent activities from another profile (recent_activities). Your objective is twofold:
        
        1. Analyze the main_profile to understand their core professional interests, focus areas, and potential motivations.
        2. Review the recent_activities and select the one that is most relevant to the interests of the main_profile. This may include shared professional fields, emerging trends that match their focus, or topics that directly address the needs of the main_profile.
        
        Once you identify the most relevant recent activity, compose a professional response. The response should acknowledge the activity and link it to the main_profile’s interests using a thoughtful and engaging tone. Use the following structure:
        
        - Begin by referencing the recent activity in a polite and personalized manner.
        - Summarize the most relevant recent activity.
        - Include the link for reference.
        - Provide a brief, positive analysis using a professional adjective to describe the relevance of the activity to the main_profile.
        
        Follow these steps:
        1. Analyze the provided main_profile data to understand their professional interests and areas of focus.
        2. Review the list of recent_activities, including the text and links, and identify the one that most closely aligns with the main_profile’s interests.
        3. Craft a response using the format: 'I also saw your recent post about [most relevant activity text summary] [insert_link_recent_activity] and found it [insert_professional_adjective]'. Ensure the adjective aligns with the nature of the content (e.g., insightful, innovative, thought-provoking, etc.).
        
        Take a deep breath and work on this problem step-by-step."""
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        # temperature=0.3,  # Adjust this parameter as per your needs
        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    result = response.choices[0].message.content.strip()
    return result


def _subject_anchor_line(trends: dict) -> str:
    """SUBJECT anchor injected into trend-based post prompts when the trend analysis was anchored to
    one of the user's focus topics — states the assigned subject explicitly so the writer model
    builds the post around it instead of picking any random thread from the industry analysis."""
    focus_topic = (trends or {}).get("focus_topic")
    if not focus_topic:
        return ""
    industry = (trends or {}).get("industry", "")
    return (f"\n ### SUBJECT ANCHOR: build this post around \"{focus_topic}\" (a topic the user "
            f"wants to be known for), at its intersection with the {industry} industry when the "
            f"trends support it. The trend analysis was gathered for that subject — do NOT drift "
            f"into unrelated {industry} news.\n")


def get_thought_leadership_post_from_ai(linked_user_profile: LinkedInProfile, buyer_stage: str,
                                        prefs: dict = None, profile_synthesis: str = None,
                                        blueprint: dict = None, lead_magnet_cta: str = None,
                                        post_id: int = None, history_directive: str = None,
                                        story_directive: str = None, content_mix: str = None):
    """
        Generate a thought leadership post based on user's expertise and industry.
        Uses the user's profile (e.g., job title, industry) and intended buyer_stage to form an insightful post.
    """

    trends = get_industry_trend_analysis_based_on_user_profile(linked_user_profile, limit_to=10,
                                                               prefs=prefs, sequence_index=post_id)
    # Fall back to the user's OWN profile industry before the generic "Technology" — a fixed
    # industry fallback misaligns the whole post for non-tech users.
    industry = trends.get("industry") or getattr(linked_user_profile, "industry", None) or "Technology"
    analysis = trends.get("analysis", "")

    myprint(
        f'Generating Thought Leadership AI Response for {buyer_stage} buyer stage about the {industry} industry.\n\nAnalysis: {analysis} ')

    # Use json to output to string
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompt = f"""Please create a thought leadership post for me based on my LinkedIn Profile information and the current trends in the {industry} industry.

        Craft the post to appeal to readers who are currently in the {buyer_stage} buyer stage of their journey.
        
        # Buyer Stages:
        - Awareness: Introduce key industry challenges and trends that my expertise addresses.
        - Consideration: Highlight unique solutions, strategies, or frameworks that showcase my approach to common industry problems.
        - Decision: Provide insight into how my experience and skills make me a strong partner for organizations seeking expertise in relevant industries or skills areas.
        
        Conclude with the call to action assigned below — invite a real reply or point to a resource; NEVER ask the reader for a call, demo, or meeting.
        
                
        """

    # Add the Linked JSON profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}\n\n"

    # Add the industry trend analysis to the prompt
    prompt += f"\n ### Current {industry} Trends: <analysis>{analysis}</analysis>"
    prompt += _subject_anchor_line(trends)

    prompt += _alignment_directive(prefs, lead_magnet_cta, content_mix)
    # Shared framework core: this post's assigned archetype/hook/CTA blueprint (rotated across
    # the user's recent posts by the caller) + the invariant short-form craft rules + prefs.
    prompt += _framework.blueprint_directive("post", blueprint)
    prompt += _framework.post_writing_directive()
    prompt += _style_directive(prefs, "post")
    prompt += history_directive or ""
    # Story bank (issue #620): the author's OWN anecdote/number/win this post is anchored to — the
    # only personal specifics the writer may state. Empty when the caller has no bank entry, in
    # which case run_content_plan passes the explicit no-fabrication fallback instead.
    prompt += story_directive or ""

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like an experienced thought leadership content creator. You have years of expertise crafting high-impact insights tailored to an executive audience across various industries. Your goal is to develop a compelling, informative, and engaging thought leadership post that reflects the user’s unique perspective and experience. Follow the steps carefully to ensure the content is insightful and relevant.

        ### Objective
        Create a thought leadership post based on the user’s expertise and current industry trends. This post should:
        - Position the user as an authority in their field.
        - Offer unique insights or innovative solutions to challenges in their industry.
        - Encourage engagement by inspiring readers to reflect, comment, or share.
        
        ### Instructions
        1. **Analyze User Profile**:
           Use the following details provided by the user:
           - Job Title (exactly as stated in their profile)
           - Industry (exactly as stated in — or inferred from — their profile; never substitute a different industry)
           - Years of Experience and Key Skills, if available.

        2. **Identify Key Industry Trends**:
           Based on the user’s industry, identify one or two current challenges, emerging trends, or transformations affecting the field, drawing them from the trend analysis provided — never from an unrelated example industry.
        
        3. **Develop Core Insight**:  
           Draw from the user's job title and experience to present an insight or perspective that:
           - Tackles a common pain point or goal in the user’s industry.
           - Reflects forward-thinking or innovative approaches.
           - Incorporates specific, actionable advice when possible.
        
        4. **Create Engaging Introduction**:  
           Start the post with a hook to capture reader interest, such as:
           - A bold statement, question, or statistic that underscores the importance of the issue.
           - A relatable scenario in which many readers in the field might find themselves.
        
        5. **Expand with Depth and Expertise**:  
           In the main content, build upon the user’s insight with examples, strategies, or industry-specific approaches. Use phrases like:
           - “In my experience as a [Job Title]…”
           - “One of the biggest challenges in [Industry] today is…”
           - “A strategy I’ve found effective involves…”
        
        6. **Close with a Call to Action**:  
           End with a thought-provoking question or prompt that encourages engagement, such as:
           - “How is your organization addressing [trend or challenge]?”
           - “What strategies have you found successful in navigating [relevant issue]?”
        
        **Final Reminder**: Focus on clarity, avoid jargon, and write in a tone that is both authoritative and accessible.
        
        Take a deep breath and work on this problem step-by-step.
    """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-complex",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.5, 0.7), 2),  # Rand temp between .5 and .7

        top_p=round(random.uniform(0.85, 0.95), 2),
        # Encourages diversity in word choice while focusing on high-probability responses for coherent professional content.
        frequency_penalty=round(random.uniform(0.3, 0.5), 2),
        # Minimizes repetitive patterns to ensure unique and varied phrasing.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),
        # Boosts exploration of new ideas while keeping content relevant to the LinkedIn tone.

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def get_industry_trend_analysis_based_on_user_profile(linked_in_profile: LinkedInProfile, limit_to=None,
                                                      randomize=True, prefs: dict = None,
                                                      sequence_index: int = None):
    my_industries = get_industries_of_profile_from_ai(linked_in_profile, 3)
    myprint(f"Likely Industries: {my_industries}")

    # Convert the industries into a list by splitting on comma
    my_industries_list = my_industries.split(', ')

    # Get one of the industries by random choice
    industry = random.choice(my_industries_list)

    myprint(f"Chosen Industry: {industry}")

    # SUBJECT anchoring (the topic-drift fix): trends used to come ONLY from the profile's industry,
    # so by the time the alignment directive steered the ANGLE the subject was already off the
    # user's declared focus. When focus_topics exist, anchor the trend subject to the INTERSECTION
    # of the industry and ONE focus topic (rotated deterministically per post via sequence_index so
    # anchoring never collapses onto a single topic); with no intersection the focus topic itself
    # wins. No focus topics → fall back to on-niche anchors derived from the profile (Topic-DNA
    # steering, issue #384); with no usable profile anchors either, the original industry-only
    # behavior is unchanged.
    focus_topic = _select_focus_topic(prefs, sequence_index, profile=linked_in_profile)
    if focus_topic:
        myprint(f"Anchoring trends to focus topic: {focus_topic}")
        subject = (f"{focus_topic} in the {industry} industry: recent trends, developments, and "
                   f"news at that intersection. If little exists at the intersection, cover recent "
                   f"trends and news about {focus_topic} itself instead of generic {industry} news")
    else:
        subject = f"Recent trends and news in the {industry} industry"

    # SHARED research core (same lem-research-preferred / direct-Perplexity-fallback layer the
    # newsletter uses; POST_RESEARCH_ENABLED toggles it). When it returns findings they ARE the
    # analysis — dated stats + named examples — so no second summarization LLM call is needed.
    research = research_topic(subject,
                              content_type="post",
                              blueprint={"format": "industry_observation"},
                              prefs=prefs)
    if research.get("findings"):
        return {
            'industry': industry.strip(),
            'analysis': research["findings"],
            'sources': research.get("sources") or [],
            'focus_topic': focus_topic,
        }

    # Research disabled/unavailable → the free GoogleNews path + trend summarization below.
    # The news query prefers the anchored focus topic for the same drift reason as above.
    articles_dict = search_recent_news(focus_topic or industry, 7)
    articles = articles_dict.get('articles', [])

    myprint(f"Articles Found: {len(articles)}")

    if randomize:
        random.shuffle(articles)
        myprint(f"Articles Shuffled")

    if limit_to and len(articles) > limit_to:
        articles = articles[:limit_to]
        myprint(f"Limited to {limit_to} articles")

    myprint(f"Articles: {articles}")

    # Get the trend analysis of the industry (scoped to the anchored focus topic when present)
    trend_analysis = get_industry_trend_from_ai(focus_topic or industry, articles)

    return {
        'industry': industry.strip(),
        'analysis': trend_analysis,
        'sources': [],
        'focus_topic': focus_topic,
    }


def get_industry_news_post_from_ai(linked_user_profile: LinkedInProfile, buyer_stage: str,
                                   prefs: dict = None, profile_synthesis: str = None,
                                   blueprint: dict = None, lead_magnet_cta: str = None,
                                   post_id: int = None, history_directive: str = None,
                                   story_directive: str = None, content_mix: str = None):
    """
       Generate a post sharing industry news based on the LinkedIn user's profile and the intended buyer stage, along with the user's commentary.
    """

    trends = get_industry_trend_analysis_based_on_user_profile(linked_user_profile, limit_to=3,
                                                               prefs=prefs, sequence_index=post_id)
    # Profile industry beats the generic "Technology" fallback (misalignment guard).
    industry = trends.get("industry") or getattr(linked_user_profile, "industry", None) or "Technology"
    analysis = trends.get("analysis", "")

    # Use json to output to string
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompt = f"""Please create a post sharing recent {industry} industry news based on my LinkedIn Profile information provided below. 
    Tailor the post to readers in the {buyer_stage} buyer stage of their journey and include my own commentary to add perspective.
            
    # Buyer Stages to Consider:
    - Awareness: Introduce the news topic with broad insights on its relevance to the industry.
    - Consideration: Frame the topic in a way that helps readers think strategically about addressing this development.
    - Decision: Emphasize the importance of expert insights and how my expertise can be valuable in navigating this trend.
    
    """

    # Add the Linked JSON profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}"

    # Add the industry trend analysis to the prompt
    prompt += f"\n ### Current {industry} Trends: <analysis>{analysis}</analysis>"
    prompt += _subject_anchor_line(trends)

    prompt += f"""\n\n
    ---
    \n
    Make the post insightful and end with a question or prompt that invites engagement from readers.


    """

    prompt += _alignment_directive(prefs, lead_magnet_cta, content_mix)
    # Shared framework core: this post's assigned archetype/hook/CTA blueprint (rotated across
    # the user's recent posts by the caller) + the invariant short-form craft rules + prefs.
    prompt += _framework.blueprint_directive("post", blueprint)
    prompt += _framework.post_writing_directive()
    prompt += _style_directive(prefs, "post")
    prompt += history_directive or ""
    # Story bank (issue #620): the author's OWN anecdote/number/win this post is anchored to — the
    # only personal specifics the writer may state. Empty when the caller has no bank entry, in
    # which case run_content_plan passes the explicit no-fabrication fallback instead.
    prompt += story_directive or ""

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a seasoned industry analyst and content strategist. You specialize in creating timely, relevant posts that share current industry news while showcasing the user's unique insights and expertise. Your goal is to craft a post based on trending topics or news in the user's industry, as inferred from their LinkedIn profile. Tailor the post to align with the buyer’s current stage in their journey—whether they are at the Awareness, Consideration, or Decision stage.
 
        ### Instructions:
        1. **Analyze the User’s Profile**:  
           Use information about the user’s role, industry, and expertise from their LinkedIn profile.
         
        2. **Identify Relevant Industry News**:  
           Identify a recent trend or piece of news in the user’s industry. Ensure the topic is significant, relevant, and likely to catch the attention of readers in the intended buyer stage.
         
        3. **Compose the Post**:
            - **For Awareness Stage**: Introduce the news in a way that highlights broad industry implications, focusing on why this development matters and its potential impact on the field. Example: “With recent changes in [industry], we’re seeing a shift toward…”
            - **For Consideration Stage**: Provide context on the topic’s importance and suggest how readers might think strategically about addressing the issue. Example: “As organizations face [issue], it’s crucial to consider approaches like…”
            - **For Decision Stage**: Focus on the practical impact of this news for decision-makers and highlight the user's expertise or offerings as a valuable resource. Example: “Given this development, partnering with an expert in [user’s specialty] can ensure…”
         
        4. **Add User Commentary**:  
           Write a thoughtful commentary that reflects the user’s experience and perspective. Use phrases like:
           - “In my experience as a [Job Title]…”
           - “One key takeaway I see here is…”
         
        5. **Close with Engagement**:  
           Encourage readers to engage by asking a relevant question or prompting them to share their own experiences.
         
        Take a deep breath and work on this problem step-by-step.
        
        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-complex",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.3, 0.5), 2),  # Rand temp between .3 and .5

        top_p=round(random.uniform(0.8, 0.9), 2),
        # Ensures focus on high-quality, relevant insights while allowing some variation in tone.
        frequency_penalty=round(random.uniform(0.2, 0.4), 2),
        # Helps maintain consistency while reducing overuse of standard expressions.
        presence_penalty=round(random.uniform(0.3, 0.5), 2),  # Allows new perspectives and commentary to emerge.

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def get_industry_trend_from_ai(industry: str, articles: list):
    """Generate industry trend based on the LinkedIn user profile."""

    articles_text = "\n".join([
        f"- {article['title']} ({article['date']}): {article['link']}"
        for article in articles
    ])

    prompt = (
            f"Here are recent news articles related to the {industry} industry:\n\n"
            f"{articles_text}\n\n" +
            "Analyze these articles and provide:\n" +
            "- A list of key topics or keywords that are trending in this industry.\n" +
            "- Categories these articles belong to (e.g., Innovation, Finance, Policy).\n" +
            "- A Summary of the industry and suggestions for further exploration based on the news trends."
    )

    # myprint(f"Prompt: {prompt}")

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional market analyst with over 15 years of experience in identifying industry trends and providing actionable insights. Your role is to analyze and interpret data from news articles to uncover patterns, emerging trends, and key industry dynamics.*

        When analyzing news articles:
        
        1. **Identify Trends:** Extract and summarize key industry trends, focusing on the specific sectors or industries mentioned in the articles.
        2. **Provide Context:** Offer detailed explanations of the factors driving these trends, referencing specific information from the articles provided.
        3. **Highlight Opportunities and Risks:** Identify opportunities and risks for businesses or stakeholders associated with these trends.
        4. **Synthesize Data:** If multiple articles are provided, synthesize their content to create a cohesive overview of the industry landscape.
        5. **Support with Evidence:** Base all analysis on the information provided in the articles. Clearly cite data or events from the articles to support your insights.
        
        Your outputs should be structured as follows:
        
        1. **Summary of Trends:** List the major industry trends identified.
        2. **Driving Factors:** Provide an analysis of what is causing these trends.
        3. **Opportunities and Risks:** Detail the potential impacts on businesses and stakeholders, including both positive and negative outcomes.
        4. **Recommendations:** Suggest strategic actions or considerations for stakeholders to adapt or respond effectively.
        5. **References:** Include references to specific articles and sections to validate your findings.

        ### Your Final Steps: 
        - Take a deep breath and work on this problem step-by-step.
        - Do not surround your response in quotes or added any additional system text. 
        - Do not share your thoughts nor show your work. 
        - Only respond with one final response.

            """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-medium",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt

        temperature=round(random.uniform(0.6, 0.8), 2),  # Rand temp between .6 and .8

        # promoting nuanced and diverse analyses.
        top_p=round(random.uniform(0.5, 0.7), 2),

        # Ensuring unique phrasing and varied insights across outputs.
        frequency_penalty=round(random.uniform(0.5, 0.7), 2),

        # Encourages exploration of new ideas while maintaining relevance to the content of the provided articles.
        presence_penalty=round(random.uniform(0.5, 0.7), 2),

        # max_tokens=150  # Set token limit as required
    )

    # Extract and return the model's response
    comment = response.choices[0].message.content.strip()
    return comment


def get_personal_story_post_from_ai(linked_user_profile: LinkedInProfile, stage: str,
                                    prefs: dict = None, profile_synthesis: str = None,
                                    blueprint: dict = None, lead_magnet_cta: str = None,
                                    post_id: int = None, history_directive: str = None,
                                    story_directive: str = None, content_mix: str = None):
    """
    Generate a post sharing a personal or professional story, based on the user's profile.
    """
    # Pull from the user's recent milestones, achievements, or challenges
    # Example content: "Reflecting on my journey as a [job title], I’ve learned that..."

    # Use json to output to string
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompt = f"""Please create a story-based post for me, reflecting on a personal or professional milestone, achievement, or challenge, using the information from my LinkedIn Profile provided below. 
    Tailor the story to connect with readers in the {stage} buyer stage of their journey. 
    Do not repeat content that I have already shared in my recent activity. 
    Do your best to relate the story to the current industry trends if possible.
    
    # Buyer Stages to Consider:
    - Awareness: Share a story that introduces me as a thoughtful leader, highlighting a key career insight or turning point.
    - Consideration: Emphasize lessons learned from a specific challenge or achievement, showing how my experience can guide or inspire similar efforts.
    - Decision: Position my expertise as a valuable resource for those facing similar challenges, demonstrating the depth of my skills and experience.
    
    """

    # Add the Linked JSON profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}"

    trends = get_industry_trend_analysis_based_on_user_profile(linked_user_profile, limit_to=5,
                                                               prefs=prefs, sequence_index=post_id)
    # Profile industry beats the generic "Technology" fallback (misalignment guard).
    industry = trends.get("industry") or getattr(linked_user_profile, "industry", None) or "Technology"
    analysis = trends.get("analysis", "")

    # Add the industry trend analysis to the prompt
    prompt += f"\n ### Current {industry} Trends: <analysis>{analysis}</analysis>"
    prompt += _subject_anchor_line(trends)

    prompt += f"""\n\n
        ---
        \n
        Conclude with an engaging question or prompt that encourages readers to reflect on similar experiences.


        """

    prompt += _alignment_directive(prefs, lead_magnet_cta, content_mix)
    # Shared framework core: this post's assigned archetype/hook/CTA blueprint (rotated across
    # the user's recent posts by the caller) + the invariant short-form craft rules + prefs.
    prompt += _framework.blueprint_directive("post", blueprint)
    prompt += _framework.post_writing_directive()
    prompt += _style_directive(prefs, "post")
    prompt += history_directive or ""
    # Story bank (issue #620): the author's OWN anecdote/number/win this post is anchored to — the
    # only personal specifics the writer may state. Empty when the caller has no bank entry, in
    # which case run_content_plan passes the explicit no-fabrication fallback instead.
    prompt += story_directive or ""

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a professional storyteller and content strategist. Your goal is to create a meaningful post that shares a personal or professional story from the user’s career journey, highlighting key milestones, achievements, or challenges. Craft a narrative that resonates with readers, giving them insights into the user’s experiences and growth within their field.
 
        ### Instructions:
        1. **Analyze the User’s Profile**:  
           Use information provided from the user’s LinkedIn profile, such as job title, years of experience, industry, key skills, recent achievements, or challenges.
         
        2. **Identify a Story Theme**:  
           Select a relevant theme for the story, based on milestones or lessons learned in the user’s career. Consider:
            - **Milestones**: A promotion, award, or significant project completion.
            - **Achievements**: Professional accomplishments, certifications, or goals reached.
            - **Challenges**: Professional hurdles, difficult projects, or industry shifts the user had to navigate.
         
        3. **Craft the Story**:
            - Begin with a relatable opening, such as: “Reflecting on my journey as a [job title]…” or “One of the most challenging moments in my career came when…”
            - Describe the situation briefly but vividly, focusing on what the user faced and how they approached it.
            - Include key learnings or insights that readers in the user’s industry might find valuable or inspiring.
         
        4. **Add a Personal Touch**:  
           Include the user’s reflections on how this experience shaped them professionally or personally. Use phrases like:
           - “This experience taught me that…”
           - “One key takeaway for me was…”
         
        5. **Close with a Call to Engage**:  
           Encourage readers to reflect on their own journeys by ending with a question or prompt, such as:
           - “What experiences have shaped your professional growth?”
           - “I’d love to hear how others in [industry] have handled similar challenges.”
         
        Take a deep breath and work on this problem step-by-step.

        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-complex",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.6, 0.8), 2),  # Rand temp between .6 and .8

        top_p=round(random.uniform(0.75, 0.85), 2),
        # Prioritizes more creative storytelling approaches for personal anecdotes.
        frequency_penalty=round(random.uniform(0.4, 0.6), 2),
        # Reduces redundancy in narrative details to make the story unique.
        presence_penalty=round(random.uniform(0.6, 0.8), 2),
        # Encourages creative content generation that resonates emotionally.

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def generate_engagement_prompt_post(linked_user_profile: LinkedInProfile, stage: str,
                                    prefs: dict = None, profile_synthesis: str = None,
                                    blueprint: dict = None, lead_magnet_cta: str = None,
                                    post_id: int = None, history_directive: str = None,
                                    story_directive: str = None, content_mix: str = None):
    """
    Generate a question or prompt that encourages engagement from followers.
    """
    # Create a question or engagement prompt related to the user's field
    # Example content: "As a [job title], I’m curious to hear how others are handling [challenge]..."

    # Use json to output to string
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompt = f"""Please generate a question or prompt to encourage engagement from my followers based on the information in my LinkedIn Profile below and related it to current industry trends. 
    Tailor the question to resonate with readers in the {stage} buyer stage of their journey.

    # Buyer Stages to Consider:
    - Awareness: Ask a thought-provoking question to spark curiosity about industry challenges or trends.
    - Consideration: Pose a question that invites followers to share strategies or insights on common challenges.
    - Decision: Encourage a deeper conversation around specific pain points or decision-making criteria, drawing on my expertise.
    
    """

    # Add the Linked JSON profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}"

    trends = get_industry_trend_analysis_based_on_user_profile(linked_user_profile,
                                                               prefs=prefs, sequence_index=post_id)
    # Profile industry beats the generic "Technology" fallback (misalignment guard).
    industry = trends.get("industry") or getattr(linked_user_profile, "industry", None) or "Technology"
    analysis = trends.get("analysis", "")

    # Add the industry trend analysis to the prompt
    prompt += f"\n ### Current {industry} Trends: <analysis>{analysis}</analysis>"
    prompt += _subject_anchor_line(trends)

    prompt += f"""\n\n
            ---
            \n
            Make the question open-ended and relatable to create meaningful engagement.


            """

    prompt += _alignment_directive(prefs, lead_magnet_cta, content_mix)
    # Shared framework core: this post's assigned archetype/hook/CTA blueprint (rotated across
    # the user's recent posts by the caller) + the invariant short-form craft rules + prefs.
    prompt += _framework.blueprint_directive("post", blueprint)
    prompt += _framework.post_writing_directive()
    prompt += _style_directive(prefs, "post")
    prompt += history_directive or ""
    # Story bank (issue #620): the author's OWN anecdote/number/win this post is anchored to — the
    # only personal specifics the writer may state. Empty when the caller has no bank entry, in
    # which case run_content_plan passes the explicit no-fabrication fallback instead.
    prompt += story_directive or ""

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a social media engagement strategist with expertise in crafting questions that spark meaningful conversations among professionals. Your task is to generate an engaging question or prompt that encourages the user’s followers to share their insights, experiences, or thoughts on a relevant industry topic.
 
    ### Instructions:
    1. **Analyze the User’s Profile**:  
       Use information from the user’s LinkedIn profile, including their job title, industry, key skills, and recent professional topics or challenges.
     
    2. **Identify a Relevant Topic for Engagement**:  
       Select a topic relevant to the user’s field that aligns with current trends, challenges, or frequent discussions. Examples include:
       - **Emerging Trends**: Innovations, new technologies, or industry shifts.
       - **Challenges**: Common obstacles or pain points within the user’s role or industry.
       - **Best Practices**: Insights or advice on strategies or approaches in the user’s field.
     
    3. **Craft an Engaging Question or Prompt**:
        - Formulate a question or prompt that invites followers to share their own experiences or perspectives. Use phrases like:
          - “As a [job title] in [industry], I’m curious to hear…”
          - “How are others in [industry] addressing…?”
          - “What strategies have you found effective for…?”
        - Ensure the question is open-ended to encourage detailed responses rather than simple yes/no answers.
     
    4. **Make it Relatable**:  
       Use language that resonates with followers in the user’s industry or role. The question should feel authentic, reflecting the user’s voice and curiosity as an industry professional.
     
    5. **Close with a Call to Action**:  
       Prompt followers to respond directly by saying, for example:
       - “I’d love to hear your thoughts!”
       - “Share your experiences below!”
     
    Take a deep breath and work on this problem step-by-step.

           """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-medium",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.6, 0.9), 2),  # Rand temp between .6 and .9

        top_p=round(random.uniform(0.7, 0.85), 2),  # Balances creativity and relevance in open-ended prompts.
        frequency_penalty=round(random.uniform(0.5, 0.7), 2),
        # Prevents repetitive patterns, especially in prompts or questions.
        presence_penalty=round(random.uniform(0.6, 0.7), 2),  # Promotes original and thought-provoking prompts.

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def get_blog_summary_post_from_ai(blog_post_url: str, blog_post_content: str, linked_user_profile: LinkedInProfile,
                                  stage: str, prefs: dict = None, profile_synthesis: str = None,
                                  blueprint: dict = None, lead_magnet_cta: str = None,
                                  history_directive: str = None,
                                  story_directive: str = None, content_mix: str = None):
    """
    Generate a summary post for a blog article using the provide post url and post content from user to create interest using relevance to the provided LinkedIn Profile.
    """
    # create a LinkedIn-friendly summary

    # Use json to output to string
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompt = f"""Please generate a LinkedIn-friendly summary post for the blog article provided below. 
    Tailor the post to appeal to readers in the {stage} buyer stage of their journey, using my LinkedIn profile details to make the summary relevant to my role and industry.

    """

    # Add the Linked JSON profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}"

    prompt += f"""\n\n Buyer Stages to Consider:
    - Awareness: Summarize the article with broad insights into industry trends and challenges.
    - Consideration: Frame the post to highlight actionable strategies or best practices discussed in the article.
    - Decision: Emphasize the practical value of the insights for decision-makers and align the tone to demonstrate my expertise in the area.
    
    ### Blog Post URL: {blog_post_url}
    
    --- 
    
    ### Blog Post Content: <blog_content>{blog_post_content}</blog_content>
    
    ---
    
    Ensure the post is engaging, includes a clear call to action, and ends with a link inviting readers to read the full article.

    
    """

    prompt += _alignment_directive(prefs, lead_magnet_cta, content_mix)
    # Shared framework core: this post's assigned archetype/hook/CTA blueprint (rotated across
    # the user's recent posts by the caller) + the invariant short-form craft rules + prefs.
    prompt += _framework.blueprint_directive("post", blueprint)
    prompt += _framework.post_writing_directive()
    prompt += _style_directive(prefs, "post")
    prompt += history_directive or ""
    # Story bank (issue #620): the author's OWN anecdote/number/win this post is anchored to — the
    # only personal specifics the writer may state. Empty when the caller has no bank entry, in
    # which case run_content_plan passes the explicit no-fabrication fallback instead.
    prompt += story_directive or ""

    content = [{"type": "text", "text": prompt}]

    # Pref-aware emoji/hashtag instructions — the hardcoded versions told the model to use emojis
    # and add hashtags even when the user's settings (already in the style directive above) said
    # not to, and a system-prompt instruction usually wins that fight. Hashtags follow the shared
    # 2026 policy (issue #393): none unless the user opted in.
    _emoji_instr = ("You can use emojis (such as 📊, 🌟, or ❓) to add personality, but only if it aligns with the user’s tone and industry norms."
                    if (not prefs or prefs.get("use_emojis"))
                    else "Do NOT use any emojis in the post.")
    _hashtag_instr = _framework.hashtag_directive(prefs)

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act as an informed LinkedIn content strategist with expertise in the user’s industry. You will be provided with a blog article URL, the article content, and LinkedIn profile information from the user. Create an engaging LinkedIn-friendly summary post that highlights the relevance of the article to the user’s industry and expertise.

        ### Instructions:
        1. **Summarize the Main Idea**:
           Begin with a clear, concise summary of the article's main message or insight, focusing on how it relates to the user’s industry. Avoid using complex terminology to keep the content accessible and engaging.

        2. **Personalize with Relatable Elements**:
           Incorporate a relatable comment or anecdote that connects the article’s content to the user’s role or experience. Use phrases like:
           - “As a [Job Title], I often see…”
           - “In the world of [Industry], this trend is particularly relevant because…”

        3. **Add Engaging Elements**:
           Include a question, a call to action, or a compelling statistic from the article to prompt followers to engage with the post. {_emoji_instr}

        4. **Hashtags**:
           {_hashtag_instr}

        5. **Tone Adaptation**:
           Adjust the tone to match the article’s content and the LinkedIn user’s profile. Whether the tone is formal, casual, motivational, or insightful, ensure it feels authentic to the user's voice.
         
        6. **Encourage Readers to Read the Full Article**:  
           Conclude with an invitation for readers to explore the topic further by including the article link with a phrase like:
           - “Read the full article here: [insert URL]”
           - “Explore more insights in the full piece: [insert URL]”
        
        Take a deep breath and work on this problem step-by-step.

        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-medium",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.5, 0.7), 2),  # Rand temp between .5 and .7

        # Focuses on concise and accurate summaries while retaining flexibility for phrasing.
        top_p=round(random.uniform(0.8, 0.9), 2),
        # Ensures variety in how summaries are structured.
        frequency_penalty=round(random.uniform(0.3, 0.5), 2),
        # Encourages fresh perspectives in the summarization process.
        presence_penalty=round(random.uniform(0.3, 0.5), 2),

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def get_website_content_post_from_ai(content: str, url: str, linked_user_profile: LinkedInProfile, stage: str,
                                     prefs: dict = None, profile_synthesis: str = None,
                                     blueprint: dict = None, lead_magnet_cta: str = None,
                                     history_directive: str = None,
                                     story_directive: str = None, content_mix: str = None):
    """
        Generate a summary post for a blog article using the provide post url and post content from user to create interest using relevance to the provided LinkedIn Profile.
        """
    # create a LinkedIn-friendly summary

    # Use json to output to string
    linked_in_profile_json = _voice_reference(linked_user_profile, profile_synthesis)

    prompt = f"""Please generate a LinkedIn-friendly summary post for the website content provided below. 
        Tailor the post to appeal to readers in the {stage} buyer stage of their journey, using my LinkedIn profile details to make the summary relevant to my role and industry.

               """

    # Add the Linked JSON profile to end of prompt
    prompt += f"\n ### LinkedIn Profile: {linked_in_profile_json}"

    prompt += f"""---\n\n Buyer Stages to Consider:
        - Awareness: Summarize the website content with broad insights into industry trends and challenges.
        - Consideration: Frame the post to highlight actionable strategies or best practices discussed in the website content.
        - Decision: Emphasize the practical value of the insights for decision-makers and align the tone to demonstrate my expertise in the area.

        ### Website URL: {url}

        --- 

        ### Website Content: <website_content>{content}</<website_content>

        ---

        Ensure the post is engaging, includes a clear call to action, and ends with a link to the website url.

                """

    prompt += _alignment_directive(prefs, lead_magnet_cta, content_mix)
    # Shared framework core: this post's assigned archetype/hook/CTA blueprint (rotated across
    # the user's recent posts by the caller) + the invariant short-form craft rules + prefs.
    prompt += _framework.blueprint_directive("post", blueprint)
    prompt += _framework.post_writing_directive()
    prompt += _style_directive(prefs, "post")
    prompt += history_directive or ""
    # Story bank (issue #620): the author's OWN anecdote/number/win this post is anchored to — the
    # only personal specifics the writer may state. Empty when the caller has no bank entry, in
    # which case run_content_plan passes the explicit no-fabrication fallback instead.
    prompt += story_directive or ""

    content = [{"type": "text", "text": prompt}]

    # Pref-aware emoji/hashtag instructions (same fix as get_blog_summary_post_from_ai): honor the
    # user's use_emojis/use_hashtags settings instead of hardcoding both ON. Hashtags follow the
    # shared 2026 policy (issue #393): none unless the user opted in.
    _emoji_instr = ("You can use emojis (such as 📊, 🌟, or ❓) to add personality, but only if it aligns with the user’s tone and industry norms."
                    if (not prefs or prefs.get("use_emojis"))
                    else "Do NOT use any emojis in the post.")
    _hashtag_instr = _framework.hashtag_directive(prefs)

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act as an informed LinkedIn content strategist with expertise in the user’s industry. You will be provided with a website URL, the website content, and LinkedIn profile information from the user.
        Create an engaging LinkedIn-friendly summary post that highlights the relevance of the website content to the user’s industry and expertise.

            ### Instructions:
            1. **Summarize the Main Idea**:
               Begin with a clear, concise summary of the website content's main message or insight, focusing on how it relates to the user’s industry. Avoid using complex terminology to keep the content accessible and engaging.

            2. **Personalize with Relatable Elements**:
               Incorporate a relatable comment or anecdote that connects the website’s content to the user’s role or experience. Use phrases like:
               - “As a [Job Title], I often see…”
               - “In the world of [Industry], this trend is particularly relevant because…”

            3. **Add Engaging Elements**:
               Include a question, a call to action, or a compelling statistic from the website content to prompt followers to engage with the post. {_emoji_instr}

            4. **Hashtags**:
               {_hashtag_instr}

            5. **Tone Adaptation**:
               Adjust the tone to match the website’s content and the LinkedIn user’s profile. Whether the tone is formal, casual, motivational, or insightful, ensure it feels authentic to the user's voice.

            6. **Encourage Readers to visit the website url**:  
               Conclude with an invitation for readers to explore further by including the website url link with a phrase like:
               - “Read the more from here: [insert URL]”
               - “Explore more here: [insert URL]”

            Take a deep breath and work on this problem step-by-step.

            """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-medium",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.5, 0.7), 2),  # Rand temp between .5 and .7

        # Focuses on generating insightful and engaging website-related content.
        top_p=round(random.uniform(0.85, 0.95), 2),
        # Helps avoid overuse of common phrases while summarizing website content.
        frequency_penalty=round(random.uniform(0.3, 0.5), 2),
        # Encourages originality and exploration of unique aspects of website content.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def get_dall_e_image_prompt_from_ai(post_content: str):
    """
       Generate a Dalle-3 image prompt from the provided post content
       """

    prompt = f"""Please generate a DALL-E-3 image prompt based on the following:
    
    Post: <content>{post_content}</content>
    
    """

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act as an expert image generator and social media content strategist. 
        Your task is to analyze the provided LinkedIn post text and craft a detailed prompt for DALL-E 3 
        to create an image that visually encapsulates the post’s theme, message, and tone.

         ### Desired Image Characteristics:
        - **Clean and Aesthetic**: The image should not feel overcrowded but should clearly depict the content of the post.
        - **Professional and Polished**: The image should look modern and inspiring, suitable for LinkedIn.
        - **Balanced**: Avoid excessive elements; instead, highlight key themes in a visually appealing way.
        - **Focused**: The image should have its focal point specifically described
        
        ### Step-by-Step Instructions:
        1. **Analyze the Post Content:**
           - Identify the main topic, message, or story conveyed by the LinkedIn post.
           - Determine the tone (e.g., professional, motivational, celebratory, reflective) and emotional undertone.
           - Extract key visual elements or themes mentioned or implied in the text (e.g., "growth," "collaboration," "achievement," "innovation").
        
        2. **Specify Visual Representation:**
           - Translate the key message into a visual scene or concept. Describe the primary subject of the image (e.g., "a team brainstorming in a futuristic office" or "a rising sun symbolizing new beginnings").
           - Ensure the image aligns with LinkedIn's professional and motivational tone while reflecting the mood of the post.
        
        3. **Choose Art Style and Composition:**
           - Select an art style that matches the post’s tone: photorealistic, professional illustration, minimalistic, vibrant, or corporate-inspired.
           - Define the composition and perspective, such as close-up, wide-angle, or eye-level.
        
        4. **Detail the Setting, Colors, and Mood:**
           - Specify the setting (e.g., urban office, natural landscape, futuristic environment).
           - Suggest colors and lighting that enhance the mood (e.g., "warm golden light for optimism" or "sleek blue tones for professionalism").
        
        5. **Generate a Complete DALL-E Prompt:**
           - Combine all the above elements into a concise, richly detailed instruction for DALL-E. Ensure clarity, specificity, and imaginative detail.
           - Review the Desired Image Characteristics and make sure your prompt incorporates each aspect but does not directly state it in your generated response.
        
        ### Output Format:
        - **DALL-E Prompt:** A fully detailed and descriptive image-generation prompt.
        
        
        ---
        
        ### Your Final Steps: 
        - Take a deep breath and work on this problem step-by-step.
        - Do not surround your response in quotes or added any additional system text. 
        - Do not share your thoughts nor show your work. 
        - Only respond with one final response without the prefix "DALL-E Prompt:".
            
        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.4, 0.6), 2),
        # Ensure logical and structured prompts but allow some creativity for DALL-E descriptions. Slightly tighter control avoids over-creativity that might make outputs unfocused.

        # Focuses on high-probability tokens while leaving room for variation in descriptions.
        top_p=round(random.uniform(0.85, 0.95), 2),
        # Ensures the generation focuses on high-probability tokens while leaving room for variation in descriptions.
        frequency_penalty=round(random.uniform(0.5, 0.7), 2),
        # Ensures new elements are explored in prompts without becoming overly imaginative or irrelevant.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def generate_dall_e_image_from_prompt(prompt: str, size: str = "1024x1024") -> list:
    """
    Generate an image from the provided prompt using the DALL-E-3 model.
    """
    # Call the DALL-E-3 model to generate an image based on the prompt
    response = client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size=size,
        quality="hd",  # Standard or hd
        n=1,  # Only 1 allowed for Dalle-3
        # style="natural", #Style can be vivid or natural
        response_format='url'
    )

    generated = len(response.data or [])
    if generated:
        from cqc_lem.utilities.observability import track_media_cost, image_cost_usd
        track_media_cost("image", "openai", image_cost_usd(generated), qty=generated,
                         model="dall-e-3", meta={"size": size})

    # Always the image list: the old empty-data branch indexed data[0] and raised IndexError,
    # and returning a URL string there made the return type depend on the failure mode.
    return response.data or []


def _profile_visual_context(profile: "LinkedInProfile | None",
                            subject_directive: str = "") -> str:
    """Short brand/context line for image prompts, built from a user's profile.

    ``subject_directive`` carries the user's SELF-DECLARED likeness (issue #744). It leads the
    context because the system prompt below actively pushes the model to invent a person: with
    nothing stating who that person is, the LLM picked a gender freely and FLUX then rendered
    that invented gender over the avatar LoRA's identity.
    """
    bits = []
    if profile is not None:
        if profile.job_title:
            bits.append(f"a {profile.job_title}")
        if profile.industry:
            bits.append(f"in the {profile.industry} industry")
        if profile.company_name:
            bits.append(f"at {profile.company_name}")
    if not bits:
        return subject_directive
    return subject_directive + (
        "The author is " + " ".join(bits) + ". "
        "Make the visual feel on-brand, credible, and relevant to this professional "
        "context.\n\n"
    )


def get_flux_image_prompt_from_ai(post_content: str, *, profile: "LinkedInProfile | None" = None,
                                  ratio: str = "1:1", avatar: "dict | None" = None) -> str:
    """Generate a Flux.1 image prompt from the post content and optional profile.

    The prompt is engineered for a single attention-drawing focal subject and brand
    alignment — the image must stop a prospect mid-scroll, not be abstract art.
    """
    from cqc_lem.utilities.avatar.attributes import subject_directive

    profile_context = _profile_visual_context(profile, subject_directive(avatar))

    prompt = f"""{profile_context}Here is a LinkedIn post: <post_content>{post_content}</post_content>.

    Pick ONE clear focal point that best represents this post and describe a single,
    photorealistic, professional image built around it. Compose it for a {ratio}
    aspect ratio. Keep it specific and grounded — not abstract or surreal.

    Respond with a single detailed paragraph describing the scene, subject, setting,
    lighting, and color — no preamble.
    """

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": """Act as a world-class commercial visual director creating
        scroll-stopping LinkedIn imagery. Your image descriptions must read like a
        brief for a professional photoshoot, optimized to draw the attention of
        business prospects.

        ### Required qualities
        - **One clear focal subject** in the foreground — ideally a real person or a
          tangible object central to the post's message. When a person is present,
          they make confident eye contact with the camera.
        - **Attention-drawing composition:** strong foreground/background separation,
          shallow depth of field, and a bold, high-contrast color accent that makes
          the subject pop in a busy feed.
        - **Professional & on-brand:** modern, clean, and credible for the author's
          stated industry. Photorealistic by default; tasteful editorial illustration
          only when it clearly fits.
        - **Good lighting:** natural or studio lighting that flatters the subject.

        ### Hard constraints
        - **NO text, letters, words, numbers, logos, watermarks, captions, charts, or
          UI** anywhere in the image — generators render these as garbled artifacts.
        - No collages, no split screens, no busy montages — one cohesive scene.
        - Avoid surreal, steampunk, glitch, or abstract treatments.

        ### Output
        One richly descriptive paragraph, no prefixes, no explanation — just the prompt.
        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.7, 1), 2),
        # Ensure logical and structured prompts but allow some creativity for Flux1 descriptions. Slightly tighter control avoids over-creativity that might make outputs unfocused.

        # Focuses on high-probability tokens while leaving room for variation in descriptions.
        top_p=round(random.uniform(0.85, 0.95), 2),
        # Ensures the generation focuses on high-probability tokens while leaving room for variation in descriptions.
        frequency_penalty=round(random.uniform(0.5, 0.7), 2),
        # Ensures new elements are explored in prompts without becoming overly imaginative or irrelevant.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


def get_flux_image_via_replicate(prompt: str, ref: str = DEFAULT_IMAGE_MODEL, *,
                                 aspect_ratio: str = "1:1"):
    if "1.1-pro" in ref:
        # flux-1.1-pro uses a different (smaller) input schema than flux-dev.
        input_params = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "output_format": "png",
            "output_quality": 100,
            "prompt_upsampling": True,
            "safety_tolerance": 2,
        }
    else:
        input_params = {
            "prompt": prompt,
            "go_fast": True,
            "guidance": 3.5,
            "megapixels": "1",
            "num_outputs": 1,
            "aspect_ratio": aspect_ratio,
            "output_format": "webp",
            "output_quality": 100,
            "prompt_strength": 0.8,
            "num_inference_steps": 28,
        }

    output = replicate.run(ref, input=input_params)

    print("Flux Image via Replicate:")
    # flux-dev returns a list; flux-1.1-pro returns a single output object.
    url = str(output[0]) if isinstance(output, (list, tuple)) else str(output)
    print(url)

    # Get the folder name of the parent of the url image
    url_image_folder = os.path.basename(os.path.dirname(url))

    # Save the file to assets/videos/replicate folder
    save_dir = os.path.join(assets_dir, "images", 'replicate', url_image_folder)

    print(f"Save to Folder: {save_dir}")

    create_folder_if_not_exists(save_dir)

    # Save the generated image
    video_file_path = save_video_url_to_dir(url, save_dir)

    return video_file_path


def generate_flux1_image_from_prompt(prompt: str, *, ratio: str = DEFAULT_IMAGE_RATIO,
                                     image_model: str = DEFAULT_IMAGE_MODEL):
    """
    video_file_path = get_flux_image_via_huggingface(prompt)

    # Move the video file path to the assets/gradio dir
    gradio_dir = os.path.join(assets_dir, "gradio")
    print(f"Gradio Dir: {gradio_dir}")
    create_folder_if_not_exists(gradio_dir)
    video_file_name = os.path.basename(video_file_path)
    # Get the parent folder name of the video file
    video_parent_dir = os.path.basename(os.path.dirname(video_file_path))
    print(f"Video Parent Folder: {video_parent_dir}")
    # Create dest folder
    file_dest_folder = os.path.join(gradio_dir, video_parent_dir)
    create_folder_if_not_exists(file_dest_folder)
    # Create final file destination
    video_file_dest = os.path.join(file_dest_folder, video_file_name)
    print(f"Video File Dest: {video_file_dest}")
    # Move the video file to the gradio dir
    shutil.move(video_file_path, video_file_dest)

    return video_file_dest
    """

    return get_flux_image_via_replicate(prompt, ref=image_model, aspect_ratio=ratio)


def generate_post_image(prompt: str, user_id: int, *, ratio: str = DEFAULT_IMAGE_RATIO,
                        image_model: str = DEFAULT_IMAGE_MODEL,
                        surface: str = AVATAR_SURFACE_POST_IMAGE,
                        post_id: "int | None" = None,
                        depicts_person: bool = True) -> str:
    """Generate a LinkedIn post image, using the user's avatar LoRA when the guardrails allow it.

    Falls back to the base Flux.1 model whenever ``resolve_avatar_for`` declines (issue #744):
    no approved avatar, the surface's opt-in is off, this post opted out, or the user switched
    their avatar off entirely.

    ``depicts_person=False`` is the object/concept case — a slide about a dashboard is not a
    scene the author appears in, and prepending the trigger word there asked the LoRA to insert
    the user's face into a scene never written to contain a person.
    """
    from cqc_lem.utilities.avatar.attributes import apply_subject_clause
    from cqc_lem.utilities.avatar.guardrails import resolve_avatar_for
    from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

    avatar = resolve_avatar_for(user_id, surface=surface, post_id=post_id) if depicts_person else None
    if avatar:
        full_prompt = apply_subject_clause(prompt, avatar)
        path, used_avatar = generate_image_with_avatar(
            full_prompt, avatar["model_ref"], ratio=ratio, fallback_prompt=prompt)
        if used_avatar:
            _record_avatar_media(path, post_id, user_id)
        return path
    return generate_flux1_image_from_prompt(prompt, ratio=ratio, image_model=image_model)


def _record_avatar_media(image_path: str, post_id: "int | None", user_id: "int | None") -> None:
    """Provenance for a synthetic likeness of a real person: C2PA on the file, a durable flag on
    the post so the caption disclosure covers avatar images and not just video (issue #744).

    Best-effort on both halves — provenance must never break media generation.
    """
    try:
        from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
        add_ai_content_credentials(image_path)
    except Exception as e:
        log_warning("C2PA signing skipped for avatar image", exc=e, user_id=user_id,
                    post_id=post_id)
    if post_id:
        try:
            from cqc_lem.utilities.db import mark_post_avatar_media
            mark_post_avatar_media(post_id)
        except Exception as e:
            log_warning("Could not flag avatar media on post", exc=e, user_id=user_id,
                        post_id=post_id)


MAX_MOTION_PROMPT_CHARS = 512


def _audio_direction(model: str, language: str = DEFAULT_CONTENT_LANGUAGE) -> str:
    """The audio clause appended to the motion prompt of every audio-capable video model.

    Veo exposes no language/voice API parameter, so audio is prompt-controlled only: with native
    audio ON and no audio direction, it invents a voiceover and picks its language freely (issue
    #548, posts #34/#36). Ambience-only removes the failure class outright, and the language is
    still stated so any incidental speech lands in the user's language. Empty for models without
    native audio (gen4_turbo/gen4.5/seedance) — they ignore audio cues.
    """
    if not supports_audio(model):
        return ""
    return (f"{AUDIO_DIRECTION_MARKER} natural ambient sound only, at low volume. No spoken "
            f"dialogue, no voiceover, no narration, no singing, no lyrics. Any incidental "
            f"speech must be in {language_name(language)}.")


def get_runway_ml_video_prompt_from_ai(post_content: str, image_prompt: str, *,
                                       model: str = DEFAULT_VIDEO_MODEL,
                                       language: str = DEFAULT_CONTENT_LANGUAGE) -> str:
    """Generate a motion-first Runway Gen-4 video prompt.

    Gen-4 image-to-video uses the IMAGE to define the scene; the text prompt should
    describe ONLY camera and subject motion, in plain concrete terms. Keyword-stuffed
    cinematic prompts (the old Gen-3 style) degrade Gen-4 output.

    For audio-capable models the deterministic `_audio_direction()` clause is appended to the
    model's output — the LLM is told to stay off audio entirely, because the clause it would
    have to write is made of negatives this system prompt otherwise forbids.
    """

    audio_direction = _audio_direction(model, language)
    audio_note = ("\n        - Say NOTHING about audio, speech, dialogue or narration — an audio "
                  "direction is appended automatically." if audio_direction else "")

    prompt = f"""Describe the motion for a short video built from this still image.

    Post (for context only): <content>{post_content}</content>

    Image already generated (the scene — do NOT re-describe it):
    <image_prompt>{image_prompt}</image_prompt>
    """

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""You write motion prompts for Runway Gen-4 image-to-video. The
        input image already defines the subject, composition, colors, and style — your
        job is to describe ONLY what moves.

        ### Rules
        - Describe camera movement and subject motion in simple, concrete, physical
          terms (e.g. "slow push-in toward the subject; she turns her head and smiles;
          gentle hair movement; subtle background blur shift").
        - Lead with the camera move, then the subject's motion.
        - Keep motion natural and subtle — this is a professional clip, not a music video.
        - NO negatives ("no X"), NO style/lighting/aesthetic adjectives, NO film-stock
          or text-effect keywords, NO scene re-description.
        - 1–3 short sentences, well under 480 characters.{audio_note}

        ### Example
        Image: a founder at a standing desk in a bright office.
        Motion prompt: "Slow push-in toward the founder. She looks up from her laptop
        and smiles at the camera. Soft papers flutter in the background."

        Output only the motion prompt — no quotes, no prefix, no explanation.
        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.4, 0.6), 2),  # Concrete, low-embellishment motion
        top_p=round(random.uniform(0.8, 0.9), 2),  # Prioritize high-probability tokens
        frequency_penalty=round(random.uniform(0.6, 0.8), 2),  # Discourage repetition
        presence_penalty=round(random.uniform(0.5, 0.7), 2),  # Encourage exploration of new ideas

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    if audio_direction:
        # Trim the motion half, never the audio direction — callers cap the prompt at
        # MAX_MOTION_PROMPT_CHARS and a truncated clause would re-open issue #548.
        motion = content[:MAX_MOTION_PROMPT_CHARS - len(audio_direction) - 1].rstrip()
        content = f"{motion} {audio_direction}"
    return content


def ai_check_message_history(message_history_json: str, main_focus: str, message: str, user_name: str = "the recipient"):
    """Check if the message history contains sentiments of the message already. It will return it or try to generate a seamless new message that is tied to the main_focus"""

    """Here is the fully optimized and structured prompt based on your one-liner. This prompt includes a clearly defined identity, objective, and step-by-step logic tailored to maximize performance in ChatGPT:

    

"""

    myprint(
        f'Generating message to the recipient based on the given message history.')



    prompt = f"""Please create an initial message, continued message response, or empty response to {user_name} based on our message history based on the given message only if it makes contextual sense: 
    
    Message History JSON:
    ```json
    <insert message history here>
    ````
    """

    # Add the Message History JSON to end of prompt
    prompt += f"\n ### Message History JSON: ```text{message_history_json}```\n\n"

    # Add the New Message to end of prompt
    prompt += f"\n ### New Message: ```text{message}```\n\n"

    # Add the Main Focus to the prompt
    prompt += f"\n ### Main Focus: ```text{main_focus}```"

    content = [{"type": "text", "text": prompt}]

    # System prompt to be included in every request
    system_prompt = {
        "role": "system",
        "content": f"""Act like a conversational continuity assistant and dialogue analyzer. You specialize in evaluating JSON-formatted message histories between two users to determine logical continuity and sentiment consistency. Your goal is to either repeat a given message or generate a seamless continuation based on conversational context.
    
    Objective:
    You are provided with:
    1. A `json` string representing the chronological message history between two users.
    2. A `new_message` which contains the content we want to evaluate for repetition or potential continuation.
    3. A `main_focus`, which is the thematic anchor or subject that should guide any new content generation.
    
    Your task:
    Determine whether the `new_message` already reflects sentiments, expressions, or thematic presence in the message history. If so, avoid redundancy. If not, generate a new message that:
    - Logically continues the conversation.
    - Naturally connects to the `main_focus`.
    - Flows seamlessly in tone and topic with the message history.
    
    Instructions:
    Step 1: Parse the JSON string of message history and summarize the conversation's tone, key sentiments, and focal points so far.
    Step 2: Compare this summary with the `new_message` to check if the same intent or sentiment is already expressed. Be strict about avoiding redundancy in intent, sentiment, or meaning—even if the wording differs.
    Step 3: If the history is empty or does not reflect the `new_message`, return the `new_message` as is.
    Step 4: If the `new_message` would be redundant, then generate a fresh, original message that naturally follows the last few entries, maintains thematic alignment with `main_focus`, and enhances the dialogue flow.
    Step 5: Ensure the tone, style, and user perspective are consistent with prior entries.
    
    Take a deep breath and work on this problem step-by-step.
        """
    }

    # User prompt to be sent with each API call
    user_message = {
        "role": "user",
        "content": content
    }

    # Call the API with the system and user prompt only (no memory of past prompts)
    response = _call_llm(
        model="lem-simple",  # Specify the model you want to use
        messages=[system_prompt, user_message],  # System prompt + current user prompt
        temperature=round(random.uniform(0.5, 0.7), 2),  # Rand temp between .5 and .7

        top_p=round(random.uniform(0.85, 0.95), 2),
        # Encourages diversity in word choice while focusing on high-probability responses for coherent professional content.
        frequency_penalty=round(random.uniform(0.3, 0.5), 2),
        # Minimizes repetitive patterns to ensure unique and varied phrasing.
        presence_penalty=round(random.uniform(0.4, 0.6), 2),
        # Boosts exploration of new ideas while keeping content relevant to the LinkedIn tone.

        # max_tokens=150  # Set token limit as required
        # response_format={"type": "json_object"},
    )

    # Extract and return the model's response
    content = response.choices[0].message.content.strip()
    return content


# Carousel captions are an INTRO to the slides, so keep them well under LinkedIn's 3000-char hard
# limit; this is both the prompt target and the deterministic post-generation cap.
_CAROUSEL_CAPTION_MAX_CHARS = 2200


def generate_carousel_content(user_id: int, stage: str, prefs: dict = None,
                              profile_synthesis: str = None, blueprint: dict = None,
                              fact_anchors: list = None,
                              story_directive: str = None) -> tuple[str, dict]:
    """Generate structured carousel content using AI and return (post_text, carousel_dict).

    An assigned `blueprint` (issue #619 / G4) maps a SHORT-FORM POST ARCHETYPE onto the slides — a
    build receipt renders naturally as a document post, LinkedIn's highest-multiplier format — so
    carousels rotate through the same shared menu as text posts instead of a carousel-only prompt.

    `fact_anchors` is the WRITER's allow-list — the ONE story-bank entry this deck is anchored to
    (issue #728), never the whole bank; `story_directive` carries that same entry's material. The
    full bank stays with the CHECKERS in `run_content_plan`.

    The carousel_dict matches the schema of one of the carousel models in carousel_creator.py.
    The carousel type is chosen by buyer journey stage:
      awareness       → EducationalContentCarousel
      consideration   → CaseStudyCarousel
      decision        → ProductDemoCarousel
      (anything else) → IndustryInsightsCarousel
    """
    from cqc_lem.utilities.db import get_user_password_pair_by_id
    from cqc_lem.utilities.linkedin.helper import get_my_profile
    from cqc_lem.utilities.selenium_util import get_driver_wait_pair, quit_gracefully
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile as _Profile

    stage_lower = (stage or "").lower()
    if "awareness" in stage_lower:
        schema_hint = (
            "EducationalContentCarousel with fields: "
            "cover (title, content), "
            "contents (list of 2-4 slides each with title and content), "
            "call_to_action (title, content)"
        )
    elif "consideration" in stage_lower:
        schema_hint = (
            "CaseStudyCarousel with fields: "
            "cover (title, content), challenge (title, content), "
            "solution (title, content), results (title, content), "
            "testimonial (title, content) [optional], "
            "call_to_action (title, content)"
        )
    elif "decision" in stage_lower:
        schema_hint = (
            "ProductDemoCarousel with fields: "
            "cover (title, content), main_feature (title, content), "
            "additional_features (list of 1-2 slides each with title and content), "
            "call_to_action (title, content)"
        )
    else:
        schema_hint = (
            "IndustryInsightsCarousel with fields: "
            "cover (title, content), "
            "insights (list of 2-4 slides each with title and content), "
            "call_to_action (title, content)"
        )

    # Attempt to load user profile for personalisation; fall back gracefully
    try:
        user_email, user_password = get_user_password_pair_by_id(user_id)
        driver, wait = get_driver_wait_pair(session_name="Carousel AI", user_id=user_id)
        try:
            profile = get_my_profile(driver, wait, user_email, user_password, user_id=user_id)
        finally:
            quit_gracefully(driver)
    except Exception as exc:
        log_warning("Could not load user profile for carousel generation; trying cached profile", exc=exc)
        # Prefer the user's cached DB profile (no Selenium) over a generic persona — a hardcoded
        # persona misaligns the carousel's industry/role framing.
        profile = None
        try:
            from cqc_lem.utilities.linkedin.helper import load_profile_for_user
            profile = load_profile_for_user(user_id)
        except Exception as exc2:
            log_warning("Cached profile unavailable for carousel generation", exc=exc2, user_id=user_id)
        if profile is None:
            profile = _Profile(full_name="Professional", job_title="Expert", company_name="Your Company")

    industry = getattr(profile, "industry", "Business") or "Business"
    job_title = getattr(profile, "job_title", "Professional") or "Professional"

    # Honor the user's hashtag setting — the old prompt hardcoded "End with 5-10 hashtags" for
    # every carousel regardless of use_hashtags. Hashtags now follow the shared 2026 policy
    # (issue #393): none unless the user opted in.
    _hashtag_rule = _framework.hashtag_directive(prefs)

    # Shared LinkedIn formatting/QA best practices + a hard length cap for the caption (carousels
    # previously had NO length or formatting enforcement — one post came back as a 3400-char wall).
    _CAROUSEL_FORMAT_DIRECTIVE = linkedin_post_format_directive(_CAROUSEL_CAPTION_MAX_CHARS)

    prompt = f"""You are a LinkedIn content strategist creating a visual carousel post for a {job_title} in the {industry} industry at the {stage} stage of the buyer journey.

Create two things and return them as a single JSON object with these top-level keys:
1. "post_text": A compelling LinkedIn post (about 1300-2000 characters) that introduces the carousel. {_hashtag_rule}
   In the JSON string, separate paragraphs with a real "\\n\\n" so it is scannable, not one block. {_CAROUSEL_FORMAT_DIRECTIVE}
2. "carousel": A JSON object matching the {schema_hint}. Each slide's "title" should be 3-8 words. Each slide's "content" should be 1-3 engaging sentences (max 200 chars).

Return ONLY valid JSON. No explanation, no markdown fences."""

    # Save-worthy / reference framing (issue #391 — C2): a carousel earns its reach from saves and
    # slide-by-slide dwell, so it must read as a reusable checklist/playbook, not an announcement.
    # Shared directive from the framework core — never a parallel per-content-type prompt helper.
    prompt += _framework.save_worthy_directive("carousel")

    # The assigned post archetype's beats, mapped onto the slides (issue #619 / G4). Carries the
    # number-led hook constraints and the no-fabrication rules with it for a fact-anchored
    # archetype, so a build-receipt carousel can no more invent a metric than a text one can.
    prompt += _framework.carousel_blueprint_directive(blueprint, fact_anchors)

    # The ONE story-bank entry this deck is anchored to (issue #620, wired for carousels in #728) —
    # the author's own material, and the only personal specifics the writer may state. Carousels
    # previously got the fact allow-list with no story behind it, so they had numbers to quote and
    # nothing to say with them.
    if story_directive:
        prompt += story_directive

    # Same alignment core as text posts: voice synthesis when available, prefs steering, and the
    # hard anti-self-promo guardrail — carousels must not drift while text posts stay aligned.
    if profile_synthesis:
        prompt += (f"\n\nAuthor voice reference (TONE + CREDIBILITY only — never the subject):\n"
                   f"{profile_synthesis}")
    prompt += _alignment_directive(prefs)

    system_prompt = {
        "role": "system",
        "content": (
            "You are an expert LinkedIn content creator who produces high-engagement carousel posts. "
            "You always return well-structured JSON with no markdown formatting. "
            "All text in the JSON is concise, professional, and written for a LinkedIn audience. "
            + _NO_SELF_PROMO_GUARDRAIL + " "
            + PLAIN_PUNCTUATION_DIRECTIVE
        ),
    }
    import json as _json

    def _draft(extra_directive: str = "") -> tuple[str, dict]:
        user_message = {"role": "user",
                        "content": [{"type": "text", "text": prompt + extra_directive}]}
        response = _call_llm(
            model="lem-complex",
            messages=[system_prompt, user_message],
            response_format={"type": "json_object"},
            temperature=round(random.uniform(0.6, 0.8), 2),
            top_p=round(random.uniform(0.85, 0.95), 2),
        )
        raw = response.choices[0].message.content.strip()
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            log_error("generate_carousel_content: LLM returned invalid JSON", exc=exc)
            parsed = {}
        # QA guard: reflow a wall-of-text caption into scannable paragraphs and hard-cap the length,
        # even when the model ignored the format directive (JSON mode often drops line breaks).
        text = enforce_post_readability(
            normalize_public_text(parsed.get("post_text",
                                             f"Explore our latest insights on {industry}.")),
            max_chars=_CAROUSEL_CAPTION_MAX_CHARS)
        return text, _normalize_carousel_strings(parsed.get("carousel", {}))

    post_text, carousel_dict = _draft()

    # Reference-value gate (issue #728): a save-targeted deck — or any deck whose caption promises a
    # checklist / the exact stack / the numbers — must carry something reusable on every body slide.
    # Deterministic, so a failure names the exact slides and gets ONE bounded regeneration with that
    # list attached, exactly like the slop lint's retry. A deck that still fails ships with the
    # reason logged: the slides are baked into images with no review queue behind them, and a
    # narrative deck is worth more than no post at all.
    save_targeted = _framework.is_save_targeted("post", (blueprint or {}).get("format"))
    report = _framework.deck_reference_report(carousel_dict, post_text, save_targeted=save_targeted)
    attempts = _framework.deck_reference_max_attempts()
    attempt = 1
    while report["checked"] and report["required"] and not report["passes"] and attempt < attempts:
        log_info("Carousel deck carries no reusable reference value — regenerating: "
                 + "; ".join(report["reasons"]), user_id=user_id, task_name="create_carousel_content")
        attempt += 1
        # The retry must never cost us the draft we already have. A gate whose whole posture is
        # "a narrative deck beats no post" cannot fail the task on a second-call error, and an
        # unparseable retry grades as no-gradeable-slide (checked=False) — shipping THAT would
        # trade a narrative deck for an empty one plus the generic fallback caption.
        try:
            retry_text, retry_deck = _draft(_framework.deck_retry_directive(report))
        except Exception as exc:
            log_warning("Carousel reference-gate retry failed — keeping the previous deck",
                        exc=exc, user_id=user_id, task_name="create_carousel_content")
            break
        retry_report = _framework.deck_reference_report(retry_deck, retry_text,
                                                        save_targeted=save_targeted)
        if not retry_report["checked"]:
            log_warning("Carousel reference-gate retry came back with no gradeable slide — keeping "
                        "the previous deck", user_id=user_id, task_name="create_carousel_content")
            break
        post_text, carousel_dict, report = retry_text, retry_deck, retry_report
    if report["checked"] and report["required"] and not report["passes"]:
        log_warning(f"Carousel deck still carries nothing worth saving after {attempt} attempt(s): "
                    + "; ".join(report["reasons"]),
                    user_id=user_id, task_name="create_carousel_content")
    return post_text, carousel_dict


def _normalize_carousel_strings(value):
    """Recursively normalize every string in the carousel data (slide titles/content) so rogue
    typographic characters never get rendered onto the slide images."""
    if isinstance(value, str):
        return normalize_public_text(value)
    if isinstance(value, dict):
        return {k: _normalize_carousel_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_carousel_strings(v) for v in value]
    return value

# LEM's OWN brand voice for product emails — plain, direct, useful. Deliberately separate from the
# user's LinkedIn voice: these emails come from us, not from them.
_LEM_BRAND_VOICE = (
    "You write product emails for LinkedIn Engagement Manager (LEM), a tool that runs a busy "
    "professional's LinkedIn presence for them — posting in their voice and engaging genuinely on "
    "their behalf. Brand voice: plain, direct, warm, and specific. No hype, no exclamation marks, no "
    "marketing cliches, no emoji, no greeting or sign-off. Respect the reader's time."
)


def generate_onboarding_nudge_copy(user_id: int, nudge: dict) -> str:
    """Rewrite a stalled-user activation nudge (issue #500) in LEM's brand voice. Short, one call to
    `lem-simple`. Returns the default body on any failure — the nudge must never depend on the LLM."""
    default_body = (nudge or {}).get("body") or ""
    try:
        response = _call_llm(
            model="lem-simple",
            messages=[
                {"role": "system", "content": _LEM_BRAND_VOICE + PLAIN_PUNCTUATION_DIRECTIVE},
                {"role": "user", "content":
                    f"Rewrite this onboarding reminder as 2 sentences (max 45 words) of email "
                    f"body copy. Keep the same ask and the same facts — do not invent features, "
                    f"numbers, or deadlines. Return only the body text.\n\n"
                    f"Headline: {(nudge or {}).get('headline', '')}\n"
                    f"Body: {default_body}\n"
                    f"Button: {(nudge or {}).get('cta_label', '')}"},
            ],
            temperature=0.4,
            max_tokens=120,
            _track_user_id=user_id,
        )
        text = (response.choices[0].message.content or "").strip()
        return normalize_public_text(text) if text else default_body
    except Exception as e:
        log_warning("Could not generate onboarding nudge copy — using default", exc=e)
        return default_body
