"""Unit tests for the FastAPI unhandled-exception capture in the observability middleware
(issue #648)."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def app_with_probe_routes():
    """The real app plus two probe routes: one that raises an unhandled error, one that raises the
    HTTPException a route uses for a normal 4xx."""
    with patch("cqc_lem.utilities.observability.track_api_call"):
        from cqc_lem.api.main import app

        @app.get("/__probe_boom")
        async def _boom():
            raise RuntimeError("probe boom")

        @app.get("/__probe_http_error")
        async def _http_error():
            raise HTTPException(status_code=404, detail="Not found")

        yield app


@pytest.fixture(scope="module")
def client(app_with_probe_routes):
    from fastapi.testclient import TestClient
    with TestClient(app_with_probe_routes, raise_server_exceptions=False) as tc:
        yield tc


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
