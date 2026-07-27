"""Unit tests for scripts/posthog_ops_destination.py — the realtime 429-breaker-trip CDP
destination (issue #655, PH10 spike)."""

import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pod = _load("posthog_ops_destination")


class TestDestinationPayload:
    def test_filters_on_the_rate_limit_trip_event(self):
        assert pod.destination_filters() == {
            "events": [{"id": "rate_limit_trip", "type": "events"}]}

    def test_payload_shape(self):
        payload = pod.destination_payload("https://hooks.example.com/x")
        assert payload["type"] == "internal_destination"
        assert payload["template_id"] == "template-webhook"
        assert payload["name"] == pod.DESTINATION_NAME
        assert payload["enabled"] is True
        assert payload["inputs"] == {"url": {"value": "https://hooks.example.com/x"}}
        assert payload["filters"] == pod.destination_filters()


class TestPlanDestination:
    def test_no_webhook_url_is_blocked(self):
        action = pod.plan_destination(None, "")
        assert action["action"] == "blocked"
        assert "POSTHOG_OPS_WEBHOOK_URL" in action["reason"]

    def test_blocked_even_if_destination_already_exists(self):
        # An empty URL is never actionable, regardless of what's already live.
        existing = {"id": 1, "filters": pod.destination_filters(), "enabled": True,
                    "inputs": {"url": {"value": "https://old.example.com"}}}
        action = pod.plan_destination(existing, "")
        assert action["action"] == "blocked"

    def test_create_when_missing(self):
        action = pod.plan_destination(None, "https://hooks.example.com/x")
        assert action["action"] == "create"
        assert action["payload"]["inputs"]["url"]["value"] == "https://hooks.example.com/x"

    def test_unchanged_when_identical(self):
        existing = {"id": 7, "filters": pod.destination_filters(), "enabled": True,
                    "inputs": {"url": {"value": "https://hooks.example.com/x"}}}
        action = pod.plan_destination(existing, "https://hooks.example.com/x")
        assert action == {"action": "unchanged", "id": 7}

    def test_update_when_url_drifted(self):
        existing = {"id": 7, "filters": pod.destination_filters(), "enabled": True,
                    "inputs": {"url": {"value": "https://old.example.com"}}}
        action = pod.plan_destination(existing, "https://new.example.com")
        assert action["action"] == "update"
        assert action["id"] == 7
        assert action["payload"]["inputs"]["url"]["value"] == "https://new.example.com"

    def test_update_when_disabled(self):
        existing = {"id": 7, "filters": pod.destination_filters(), "enabled": False,
                    "inputs": {"url": {"value": "https://hooks.example.com/x"}}}
        action = pod.plan_destination(existing, "https://hooks.example.com/x")
        assert action["action"] == "update"

    def test_update_when_filters_drifted(self):
        existing = {"id": 7, "filters": {"events": []}, "enabled": True,
                    "inputs": {"url": {"value": "https://hooks.example.com/x"}}}
        action = pod.plan_destination(existing, "https://hooks.example.com/x")
        assert action["action"] == "update"


class TestApplyPlan:
    def test_blocked_logs_and_never_touches_the_client(self):
        client = MagicMock()
        line = pod.apply_plan(client, {"action": "blocked", "reason": "no url"}, dry_run=False)
        assert "skipped" in line and "no url" in line
        client.create.assert_not_called()
        client.update.assert_not_called()

    def test_unchanged_logs_and_never_touches_the_client(self):
        client = MagicMock()
        line = pod.apply_plan(client, {"action": "unchanged", "id": 3}, dry_run=False)
        assert "unchanged" in line
        client.create.assert_not_called()

    def test_dry_run_create_never_touches_the_client(self):
        client = MagicMock()
        line = pod.apply_plan(client, {"action": "create", "payload": {}}, dry_run=True)
        assert "[dry-run] create" in line
        client.create.assert_not_called()

    def test_apply_create_calls_the_client(self):
        client = MagicMock()
        client.create.return_value = {"id": 42}
        line = pod.apply_plan(client, {"action": "create", "payload": {"x": 1}}, dry_run=False)
        client.create.assert_called_once_with({"x": 1})
        assert "created destination" in line and "42" in line

    def test_apply_update_calls_the_client(self):
        client = MagicMock()
        line = pod.apply_plan(
            client, {"action": "update", "id": 9, "payload": {"x": 1}}, dry_run=False)
        client.update.assert_called_once_with(9, {"x": 1})
        assert "updated destination" in line


class TestMain:
    def test_print_payload_needs_no_network(self, capsys):
        assert pod.main(["--print-payload"]) == 0
        out = capsys.readouterr().out
        assert pod.PLACEHOLDER_URL in out
        assert "template-webhook" in out

    def test_missing_api_key_is_an_error(self, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert pod.main([]) == 1
        assert "POSTHOG_PERSONAL_API_KEY" in capsys.readouterr().err

    def test_no_webhook_url_exits_zero_with_instructions(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.delenv("POSTHOG_OPS_WEBHOOK_URL", raising=False)
        client = MagicMock()
        client.find_destination.return_value = None
        monkeypatch.setattr(pod, "PostHogFunctionsClient", lambda *a, **k: client)
        assert pod.main(["--apply"]) == 0
        err = capsys.readouterr().err
        assert "POSTHOG_OPS_WEBHOOK_URL" in err
        client.create.assert_not_called()

    def test_dry_run_with_url_reports_pending(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_OPS_WEBHOOK_URL", "https://hooks.example.com/x")
        client = MagicMock()
        client.find_destination.return_value = None
        monkeypatch.setattr(pod, "PostHogFunctionsClient", lambda *a, **k: client)
        assert pod.main(["--dry-run"]) == 2
        assert "[dry-run] create" in capsys.readouterr().out
        client.create.assert_not_called()

    def test_apply_with_url_creates_the_destination(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_OPS_WEBHOOK_URL", "https://hooks.example.com/x")
        client = MagicMock()
        client.find_destination.return_value = None
        client.create.return_value = {"id": 42}
        monkeypatch.setattr(pod, "PostHogFunctionsClient", lambda *a, **k: client)
        assert pod.main(["--apply"]) == 0
        client.create.assert_called_once()
        assert client.create.call_args.args[0]["inputs"]["url"]["value"] == \
            "https://hooks.example.com/x"
        assert "created destination" in capsys.readouterr().out

    def test_apply_unchanged_is_a_noop(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_OPS_WEBHOOK_URL", "https://hooks.example.com/x")
        client = MagicMock()
        client.find_destination.return_value = {
            "id": 7, "filters": pod.destination_filters(), "enabled": True,
            "inputs": {"url": {"value": "https://hooks.example.com/x"}}}
        monkeypatch.setattr(pod, "PostHogFunctionsClient", lambda *a, **k: client)
        assert pod.main(["--apply"]) == 0
        client.create.assert_not_called()
        client.update.assert_not_called()
        assert "unchanged" in capsys.readouterr().out

    def test_read_failure_is_an_error(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        client = MagicMock()
        client.find_destination.side_effect = RuntimeError("boom")
        monkeypatch.setattr(pod, "PostHogFunctionsClient", lambda *a, **k: client)
        assert pod.main([]) == 1
        assert "Failed to read PostHog state" in capsys.readouterr().err


class TestPostHogFunctionsClient:
    def test_create_posts_to_the_right_url(self):
        from unittest.mock import patch
        client = pod.PostHogFunctionsClient("phx_test", "475262", "https://us.posthog.com")
        response = MagicMock()
        response.content = b'{"id": 1}'
        response.json.return_value = {"id": 1}
        with patch("requests.post", return_value=response) as post:
            client.create({"name": "x"})
        assert post.call_args.args[0] == \
            "https://us.posthog.com/api/projects/475262/hog_functions/"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer phx_test"

    def test_update_patches_the_function_id(self):
        from unittest.mock import patch
        client = pod.PostHogFunctionsClient("phx_test", "475262", "https://us.posthog.com")
        response = MagicMock()
        response.content = b""
        with patch("requests.patch", return_value=response) as patch_call:
            client.update(9, {"name": "x"})
        assert patch_call.call_args.args[0] == \
            "https://us.posthog.com/api/projects/475262/hog_functions/9/"

    def test_find_destination_filters_by_name_and_skips_deleted(self):
        from unittest.mock import patch
        client = pod.PostHogFunctionsClient("phx_test", "475262", "https://us.posthog.com")
        response = MagicMock()
        response.json.return_value = {
            "results": [
                {"name": "other", "id": 1},
                {"name": pod.DESTINATION_NAME, "id": 2, "deleted": True},
                {"name": pod.DESTINATION_NAME, "id": 3},
            ],
            "next": None,
        }
        response.raise_for_status = MagicMock()
        with patch("requests.get", return_value=response):
            found = client.find_destination(pod.DESTINATION_NAME)
        assert found["id"] == 3
