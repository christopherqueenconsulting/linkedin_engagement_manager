"""API routes that generate inline answer the free-trial daily AI cap with a 429 (issue #2378).

`POST /api/generate-carousel` called the carousel generator with no attribution scope at all, so its
spend landed on no user — the cap never saw it, and never stopped it. These drive the real route and
the real shared client (over a recording transport), so a regression in either half shows up here:
a capped trial user is refused before anything is sent, and an uncapped one's call is counted.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from cqc_lem.utilities import ai_spend_cap as cap

pytestmark = pytest.mark.unit

_CC = "cqc_lem.utilities.carousel_creator"
_AI = "cqc_lem.utilities.ai.ai_helper"

_DECK = {
    "cover": {"title": "The 3 checks I run", "content": "The exact stack."},
    "contents": [{"title": "1. Pin the tag", "content": "Set IMAGE_TAG to the release tag."}],
    "call_to_action": {"title": "Save this", "content": "Save it for your next deploy."},
}
_CHAT = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "lem-complex",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
}


class _Recorder(httpx.BaseTransport):
    def __init__(self):
        self.bodies = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_CHAT, headers={"x-litellm-response-cost": "0.12"})


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    def incrbyfloat(self, key, amount):
        self.kv[key] = float(self.kv.get(key, 0.0)) + float(amount)

    def hincrbyfloat(self, key, field, amount):
        return None

    def expire(self, key, ttl):
        return True

    def get(self, key):
        return None if key not in self.kv else str(self.kv[key]).encode()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True


@pytest.fixture
def harness():
    """A free-trial session user, fake Redis, and the shared client on a recording transport."""
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    from tests.unit.api.conftest import SESSION_USER_ID

    recorder = _Recorder()
    client = AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                              http_client=httpx.Client(transport=recorder))
    redis = _FakeRedis()
    cap._TIER_CACHE.clear()

    def generator(user_id, stage):
        from cqc_lem.utilities.ai import ai_helper
        ai_helper._call_llm(model="lem-complex", messages=[{"role": "user", "content": "deck"}])
        return "caption", _DECK

    with patch(f"{_AI}.client", client), \
            patch(f"{_AI}.generate_carousel_content", side_effect=generator) as generate, \
            patch(f"{_CC}.create_carousel_slide_images", return_value=["/tmp/slide_1.png"]), \
            patch("cqc_lem.utilities.ai_spend_cap._redis", return_value=redis), \
            patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=redis), \
            patch("cqc_lem.utilities.db.get_user_subscription_info",
                  return_value={"subscription_tier": "free_trial"}), \
            patch("cqc_lem.utilities.observability.track_ai_daily_cap_reached"):
        yield {"user_id": SESSION_USER_ID, "recorder": recorder, "redis": redis,
               "generate": generate}
    cap._TIER_CACHE.clear()


def _post(api_client):
    from tests.unit.api.conftest import SESSION_TOKEN
    return api_client.post("/api/generate-carousel",
                           json={"session_token": SESSION_TOKEN, "stage": "awareness"})


def test_a_capped_trial_user_gets_a_429_and_nothing_is_sent(api_client, signed_in, harness):
    cap.record_user_spend(harness["user_id"], 0.75, client=harness["redis"])
    resp = _post(api_client)
    assert resp.status_code == 429
    assert resp.json()["reason"] == "daily_ai_limit"
    assert resp.headers["Retry-After"]
    assert harness["recorder"].bodies == []
    harness["generate"].assert_not_called()


def test_an_under_cap_call_is_attributed_and_counted(api_client, signed_in, harness):
    resp = _post(api_client)
    assert resp.status_code == 200
    assert len(harness["recorder"].bodies) == 1
    sent = harness["recorder"].bodies[0]["metadata"]
    assert sent["user_id"] == str(harness["user_id"])
    key = f"lem:ai_spend:user:{cap.utc_day()}:{harness['user_id']}"
    assert harness["redis"].kv[key] == pytest.approx(0.12)


@pytest.fixture
def capped():
    """The session user is a free-trial user already past today's cap."""
    from tests.unit.api.conftest import SESSION_USER_ID
    redis = _FakeRedis()
    cap._TIER_CACHE.clear()
    cap.record_user_spend(SESSION_USER_ID, 0.75, client=redis)
    with patch("cqc_lem.utilities.ai_spend_cap._redis", return_value=redis), \
            patch("cqc_lem.utilities.db.get_user_subscription_info",
                  return_value={"subscription_tier": "free_trial"}), \
            patch("cqc_lem.utilities.observability.track_ai_daily_cap_reached"):
        yield SESSION_USER_ID
    cap._TIER_CACHE.clear()


_USER = "cqc_lem.api.routers.user"


def test_rescore_is_refused_with_a_429_before_any_judge_call(api_client, signed_in, capped):
    from tests.unit.api.conftest import SESSION_TOKEN
    with patch(f"{_USER}.get_post_user_id", return_value=capped), \
            patch(f"{_USER}.get_post_status", return_value="pending"), \
            patch("cqc_lem.app.run_content_plan.rescore_post") as rescore:
        resp = api_client.post("/api/user/post/rescore",
                               json={"session_token": SESSION_TOKEN, "post_id": 5})
    assert resp.status_code == 429
    assert resp.json()["reason"] == "daily_ai_limit"
    rescore.assert_not_called()


def test_rescore_attributes_its_calls_to_the_session_user(api_client, signed_in):
    from cqc_lem.utilities.observability import current_llm_attribution
    from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID
    seen = []
    with patch(f"{_USER}.get_post_user_id", return_value=SESSION_USER_ID), \
            patch(f"{_USER}.get_post_status", return_value="pending"), \
            patch("cqc_lem.utilities.db.get_user_subscription_info", return_value=None), \
            patch("cqc_lem.app.run_content_plan.rescore_post",
                  side_effect=lambda post_id: seen.append(current_llm_attribution()) or {}):
        resp = api_client.post("/api/user/post/rescore",
                               json={"session_token": SESSION_TOKEN, "post_id": 5})
    assert resp.status_code == 200
    assert seen == [(SESSION_USER_ID, "content")]


def test_image_generation_is_refused_before_the_hourly_slot_is_spent(api_client, signed_in, capped):
    from tests.unit.api.conftest import SESSION_TOKEN
    with patch(f"{_USER}.claim_manual_generation", return_value=True) as claim, \
            patch(f"{_USER}.generate_image_for_post") as generate:
        resp = api_client.post("/api/user/post/image/generate",
                               json={"session_token": SESSION_TOKEN, "content": "A post"})
    assert resp.status_code == 429
    assert resp.json()["reason"] == "daily_ai_limit"
    claim.assert_not_called()
    generate.assert_not_called()


def test_image_generation_attributes_its_calls_to_the_session_user(api_client, signed_in):
    from cqc_lem.utilities.observability import current_llm_attribution
    from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID
    seen = []

    def generate(user_id, text, post_id=None):
        seen.append(current_llm_attribution())
        return "https://x/img.png", None

    with patch(f"{_USER}.claim_manual_generation", return_value=True), \
            patch("cqc_lem.utilities.db.get_user_subscription_info", return_value=None), \
            patch(f"{_USER}.generate_image_for_post", side_effect=generate):
        resp = api_client.post("/api/user/post/image/generate",
                               json={"session_token": SESSION_TOKEN, "content": "A post"})
    assert resp.status_code == 200
    assert seen == [(SESSION_USER_ID, "content")]


def test_an_admin_action_for_a_capped_user_is_attributed_but_never_refused(api_client, capped):
    from cqc_lem.utilities.observability import current_llm_attribution
    seen = []

    def generate(**kwargs):
        user_id, _feature = current_llm_attribution()
        cap.check_request(user_id, "lem-complex")  # would raise outside the operator scope
        seen.append(user_id)
        return {"batch_id": "1_abc", "variants": [], "total_estimated_cost_usd": 0.0,
                "metadata_url": "u"}

    with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
            patch("cqc_lem.app.generate_variants.generate_media_variants", side_effect=generate):
        resp = api_client.post("/api/admin/generate-media-variants",
                               json={"text": "hi", "user_id": capped},
                               headers={"x-admin-secret": "s3cret"})
    assert resp.status_code == 200
    assert seen == [capped]


# --- Every API route that generates inline is scoped (issue #2378) -------------------------------
# A route that reaches an LLM or a render inside the request must name the user it is for and let
# the cap's refusal reach the 429 handler. Found by a static call-graph audit of api/; listed here so
# a NEW route calling one of these entry points fails until it is classified.
INLINE_AI_ROUTES = {
    ("POST", "/api/generate-carousel"): "llm_trace(",
    ("POST", "/api/user/post/rescore"): "llm_trace(",
    ("POST", "/api/user/post/image/generate"): "llm_trace(",
    ("POST", "/api/admin/regenerate-carousel"): "operator_action(",
    ("POST", "/api/admin/regenerate-video"): "operator_action(",
    ("POST", "/api/admin/generate-media-variants"): "operator_action(",
    ("POST", "/api/admin/feedback/{feedback_id}/review"): "operator_action(",
}
# Reach a provider but are not LLM generation the cap governs, with the reason.
NOT_CAPPED = {
    # Replicate LoRA TRAINING: paid for with avatar credits the route deducts, not per-call spend.
    ("POST", "/api/avatar/training"),
}
# Names whose presence in a handler means it generates inline.
AI_ENTRY_POINTS = (
    "generate_carousel_content", "rescore_post", "generate_image_for_post",
    "create_carousel_content", "regenerate_video_for_post", "generate_media_variants",
    "file_feedback_issue", "start_avatar_training", "_call_llm", "chat.completions",
    "images.generate", "generate_ai_response", "create_text_post",
)


def _api_routes():
    import inspect

    from cqc_lem.api.main import _walk_routes, app
    # `_walk_routes`, not `app.routes`: FastAPI keeps an included router as ONE opaque node.
    for route in _walk_routes(app.routes):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(route, "path", "").startswith("/api"):
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        for method in getattr(route, "methods", None) or ():
            yield (method, route.path), source


def test_every_inline_ai_route_is_classified():
    found = {key for key, source in _api_routes()
             if any(name in source for name in AI_ENTRY_POINTS)}
    assert found, "matched no route — the scan would pass vacuously"
    assert found - set(INLINE_AI_ROUTES) - NOT_CAPPED == set()


def test_every_classified_route_still_exists_and_carries_its_scope():
    routes = dict(_api_routes())
    for key, marker in INLINE_AI_ROUTES.items():
        assert key in routes, f"{key} is gone — drop it from INLINE_AI_ROUTES"
        assert marker in routes[key], f"{key} lost its {marker} scope"


def test_the_generator_is_a_pipeline_for_every_caller():
    """The decorator, not just the route: the background path is attributed the same way."""
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    assert getattr(generate_carousel_content, "__llm_trace_name__", None) == "carousel_generation"
