"""Unit tests for the FastAPI unhandled-exception capture in the observability middleware
(issue #648)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def app_with_probe_routes():
    """The real app plus two probe routes: one that raises an unhandled error, one that raises the
    HTTPException a route uses for a normal 4xx."""
    from cqc_lem.api.main import app

    original_routes = list(app.router.routes)

    # main.py registers the SPA fallback (`GET /{full_path:path}`) LAST, and only when ui/dist was
    # built. Starlette matches in registration order, so probe routes appended after it never run —
    # they serve index.html with a 200, and every assertion below passes against the wrong response
    # (issue #820). Register an equivalent catch-all unconditionally so the hazard is present here
    # whether or not the UI was built, then put the probes in FRONT of it.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        return {"spa": full_path}

    routes_before_probes = len(app.router.routes)

    @app.get("/__probe_boom")
    async def _boom():
        raise RuntimeError("probe boom")

    @app.get("/__probe_http_error")
    async def _http_error():
        raise HTTPException(status_code=404, detail="Not found")

    probe_routes = app.router.routes[routes_before_probes:]
    del app.router.routes[routes_before_probes:]
    app.router.routes[:0] = probe_routes

    # `app` is a module-level singleton shared with every other test module, and main.py binds
    # track_api_call into its own namespace at import time — patching it here (rather than on
    # observability) holds regardless of who imported main first.
    with patch("cqc_lem.api.main.track_api_call"):
        yield app

    app.router.routes[:] = original_routes


@pytest.fixture(scope="module")
def client(app_with_probe_routes):
    from fastapi.testclient import TestClient
    with TestClient(app_with_probe_routes, raise_server_exceptions=False) as tc:
        yield tc


def _capturing_posthog() -> MagicMock:
    """A stand-in for the posthog module that reports itself enabled, so capture_exception runs its
    real body instead of returning early on the keyless test env."""
    fake = MagicMock()
    fake.disabled = False
    return fake


class TestProbeRoutesAreReachable:
    def test_catch_all_is_registered_and_would_shadow_appended_routes(self, client):
        """Guards the fixture itself: an unmatched path resolves to a catch-all and answers 200, so
        the 500/404 the probes return below can only mean they were matched ahead of it. If the
        probes stop being registered first they go back to answering 200 too, and the tests below
        stop proving anything."""
        response = client.get("/__no_such_route")

        assert response.status_code == 200


class TestUnhandledExceptionCapture:
    def test_unhandled_route_error_is_captured_with_route_context(self, client):
        with patch("cqc_lem.api.main.capture_exception") as mock_capture:
            response = client.get("/__probe_boom")

        assert response.status_code == 500
        mock_capture.assert_called_once()
        args, kwargs = mock_capture.call_args
        assert isinstance(args[0], RuntimeError)
        assert kwargs["route"] == "/__probe_boom"
        assert kwargs["method"] == "GET"
        assert kwargs["source"] == "fastapi.middleware"

    def test_http_exception_is_not_captured(self, client):
        """A 404/401 is a normal response, not an error-tracking issue — capturing it would drown
        real crashes in expected 4xx noise."""
        with patch("cqc_lem.api.main.capture_exception") as mock_capture:
            response = client.get("/__probe_http_error")

        assert response.status_code == 404
        mock_capture.assert_not_called()

    def test_healthy_request_is_not_captured(self, client):
        with patch("cqc_lem.api.main.capture_exception") as mock_capture:
            client.get("/health")

        mock_capture.assert_not_called()


class TestExceptionReachesPostHog:
    """End to end through the real capture_exception — the middleware calling a mock proves the
    wiring, not that an `$exception` is actually emitted with route context."""

    def test_unhandled_route_error_emits_exception_event(self, client):
        fake_posthog = _capturing_posthog()

        with patch("cqc_lem.utilities.observability.posthog", fake_posthog):
            response = client.get("/__probe_boom")

        assert response.status_code == 500
        fake_posthog.capture_exception.assert_called_once()
        args, kwargs = fake_posthog.capture_exception.call_args
        assert isinstance(args[0], RuntimeError)
        assert kwargs["distinct_id"] == "system"
        properties = kwargs["properties"]
        assert properties["route"] == "/__probe_boom"
        assert properties["method"] == "GET"
        assert properties["source"] == "fastapi.middleware"

    def test_http_exception_emits_no_exception_event(self, client):
        fake_posthog = _capturing_posthog()

        with patch("cqc_lem.utilities.observability.posthog", fake_posthog):
            response = client.get("/__probe_http_error")

        assert response.status_code == 404
        fake_posthog.capture_exception.assert_not_called()
