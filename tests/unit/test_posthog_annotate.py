"""Unit tests for scripts/posthog_annotate.py — the release-deploy PostHog annotation (issue
#654).
"""

import importlib.util
import pathlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pha = _load("posthog_annotate")


class TestAnnotationPayload:
    def test_default_content_names_the_tag(self):
        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        payload = pha.annotation_payload("v1.2.3", now)
        assert payload == {"content": "v1.2.3 deployed", "date_marker": "2026-07-27T12:00:00Z",
                           "scope": "project"}

    def test_content_override(self):
        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        payload = pha.annotation_payload("v1.2.3", now, content="custom note")
        assert payload["content"] == "custom note"

    def test_date_marker_normalizes_to_utc(self):
        from datetime import timedelta
        est = timezone(timedelta(hours=-5))
        now = datetime(2026, 7, 27, 7, 0, 0, tzinfo=est)
        payload = pha.annotation_payload("v1.2.3", now)
        assert payload["date_marker"] == "2026-07-27T12:00:00Z"

    def test_scope_is_project_wide(self):
        # A release affects everything, not one dashboard's tiles.
        now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        assert pha.annotation_payload("v1.2.3", now)["scope"] == "project"


class TestMain:
    def test_dry_run_needs_no_network(self, capsys, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert pha.main(["--tag", "v1.2.3", "--dry-run"]) == 0
        assert "v1.2.3 deployed" in capsys.readouterr().out

    def test_missing_api_key_skips_without_failing(self, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert pha.main(["--tag", "v1.2.3"]) == 0
        assert "skipping the release annotation" in capsys.readouterr().err

    def test_posts_the_annotation(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        client = MagicMock()
        client.create_annotation.return_value = {"id": 42}
        monkeypatch.setattr(pha, "PostHogAnnotationsClient", lambda *a, **k: client)
        assert pha.main(["--tag", "v1.2.3"]) == 0
        client.create_annotation.assert_called_once()
        assert client.create_annotation.call_args.args[0]["content"] == "v1.2.3 deployed"
        assert "Posted release annotation 42" in capsys.readouterr().out

    def test_content_override_reaches_the_payload(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        client = MagicMock()
        client.create_annotation.return_value = {}
        monkeypatch.setattr(pha, "PostHogAnnotationsClient", lambda *a, **k: client)
        pha.main(["--tag", "v1.2.3", "--content", "custom note"])
        assert client.create_annotation.call_args.args[0]["content"] == "custom note"

    def test_api_error_is_reported_but_not_a_dry_run_crash(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        client = MagicMock()
        client.create_annotation.side_effect = RuntimeError("boom")
        monkeypatch.setattr(pha, "PostHogAnnotationsClient", lambda *a, **k: client)
        assert pha.main(["--tag", "v1.2.3"]) == 1
        assert "Could not post the release annotation" in capsys.readouterr().err

    def test_reads_project_and_host_from_env(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_PROJECT_ID", "999")
        monkeypatch.setenv("POSTHOG_APP_HOST", "https://eu.posthog.com")
        captured = {}

        def _client(api_key, project_id, app_host):
            captured["project_id"] = project_id
            captured["app_host"] = app_host
            client = MagicMock()
            client.create_annotation.return_value = {}
            return client

        monkeypatch.setattr(pha, "PostHogAnnotationsClient", _client)
        pha.main(["--tag", "v1.2.3"])
        assert captured == {"project_id": "999", "app_host": "https://eu.posthog.com"}


class TestPostHogAnnotationsClient:
    def test_posts_to_the_right_url_with_bearer_auth(self):
        client = pha.PostHogAnnotationsClient("phx_test", "475262", "https://us.posthog.com")
        response = MagicMock()
        response.content = b'{"id": 1}'
        response.json.return_value = {"id": 1}
        with patch("requests.post", return_value=response) as post:
            client.create_annotation({"content": "x"})
        assert post.call_args.args[0] == "https://us.posthog.com/api/projects/475262/annotations/"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer phx_test"
        assert post.call_args.kwargs["json"] == {"content": "x"}
