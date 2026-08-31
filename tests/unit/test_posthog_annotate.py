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
        err = capsys.readouterr().err
        assert "skipping the release annotation" in err
        # Names BOTH vars — the reader has to know which one to set (issue #1453).
        assert "POSTHOG_ANNOTATION_API_KEY" in err and "POSTHOG_PERSONAL_API_KEY" in err

    def test_the_annotation_scoped_key_outranks_the_shared_one(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.setenv("POSTHOG_ANNOTATION_API_KEY", "phx_annotation")
        captured = {}

        def _client(api_key, project_id, app_host):
            captured["api_key"] = api_key
            client = MagicMock()
            client.create_annotation.return_value = {}
            return client

        monkeypatch.setattr(pha, "PostHogAnnotationsClient", _client)
        assert pha.main(["--tag", "v1.2.3"]) == 0
        assert captured["api_key"] == "phx_annotation"

    def test_the_shared_key_still_works_alone(self, monkeypatch):
        # The rollout is additive: CI keeps working before any scoped key exists.
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.delenv("POSTHOG_ANNOTATION_API_KEY", raising=False)
        captured = {}

        def _client(api_key, project_id, app_host):
            captured["api_key"] = api_key
            client = MagicMock()
            client.create_annotation.return_value = {}
            return client

        monkeypatch.setattr(pha, "PostHogAnnotationsClient", _client)
        assert pha.main(["--tag", "v1.2.3"]) == 0
        assert captured["api_key"] == "phx_shared"

    def test_another_purpose_s_key_does_not_post_an_annotation(self, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_query")
        monkeypatch.setattr(pha, "PostHogAnnotationsClient", MagicMock())
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


class TestTheDeployJobActuallyPassesTheKey:
    """The gap that let this lane die silently for 11 days (issue #1453).

    `POSTHOG_ANNOTATION_API_KEY` existed as a repository secret and `posthog_annotate.py` resolved
    it correctly — but the workflow never put it in the job's environment, so the script saw only
    the shared fallback, which was then revoked. Every release kept going green, because the step
    is `continue-on-error` and the script exits 0 on a missing key by design. A unit test of the
    script alone can never catch that: the defect lives in the WIRING between them.
    """

    WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/build-and-push.yml"

    def _annotating_job(self) -> dict:
        yaml = pytest.importorskip("yaml")
        workflow = yaml.safe_load(self.WORKFLOW.read_text())
        jobs = [job for job in workflow["jobs"].values()
                if any("posthog_annotate.py" in str(step.get("run", ""))
                       for step in job.get("steps") or [])]
        assert len(jobs) == 1, "posthog_annotate.py moved jobs — update this test with it"
        return jobs[0]

    def test_the_job_running_the_script_exports_the_scoped_annotation_key(self):
        env = self._annotating_job().get("env") or {}
        assert "POSTHOG_ANNOTATION_API_KEY" in env
        assert "secrets.POSTHOG_ANNOTATION_API_KEY" in env["POSTHOG_ANNOTATION_API_KEY"]

    def test_the_job_does_not_export_the_revoked_shared_key(self):
        # Not tidiness: `posthog_keys.py` prefers any non-empty value it finds, so re-exporting the
        # revoked key would make the annotation 401 rather than fall back to nothing.
        assert "POSTHOG_PERSONAL_API_KEY" not in (self._annotating_job().get("env") or {})

    def test_the_job_exports_the_project_and_host_the_script_reads(self):
        env = self._annotating_job().get("env") or {}
        assert {"POSTHOG_PROJECT_ID", "POSTHOG_APP_HOST"} <= set(env)
