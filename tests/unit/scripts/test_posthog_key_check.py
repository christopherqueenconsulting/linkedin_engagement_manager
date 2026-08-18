"""Unit tests for scripts/posthog_key_check.py — the key-split preflight (issue #1453).

Two properties carry the whole script. It must never WRITE to PostHog (it is run against the live
project, repeatedly, mid-rollout), and it must never report success for a surface it did not
actually read — the silent-degradation failure it exists to replace.
"""

import importlib.util
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PATH = _ROOT / "scripts" / "posthog_key_check.py"
_spec = importlib.util.spec_from_file_location("posthog_key_check", _PATH)
kc = importlib.util.module_from_spec(_spec)
sys.modules["posthog_key_check"] = kc
_spec.loader.exec_module(kc)

PROJECT = "475262"
HOST = "https://us.posthog.com"


class _FakeReader:
    """Records what it was asked for and replays canned (status, body) answers."""

    def __init__(self, answers=None, raises=None):
        self.answers = list(answers or [])
        self.raises = raises
        self.calls = []

    def read(self, spec, api_key):
        self.calls.append({**spec, "api_key": api_key})
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0) if self.answers else (200, "{}")


class TestSurfaceTable:
    def test_every_surface_is_structurally_read_only(self):
        # The one property that makes this safe to run against the live project on demand.
        for surface in kc.SURFACES:
            assert kc.is_read_only(surface), f"{surface['name']} can write to PostHog"

    def test_a_write_surface_would_be_rejected_by_the_guard(self):
        # Proves the guard above has teeth rather than passing everything.
        assert not kc.is_read_only({"method": "POST", "path": "/api/projects/{project_id}/annotations/"})
        assert not kc.is_read_only({"method": "PATCH", "path": "/api/projects/{project_id}/query/"})

    def test_every_known_purpose_has_at_least_one_surface(self):
        # A purpose with no surface would pass silently — exactly the hole this script closes.
        from cqc_lem.utilities.posthog_keys import PURPOSE_ENV_VARS
        assert set(kc.known_purposes()) == set(PURPOSE_ENV_VARS)

    def test_the_runtime_key_is_checked_on_both_of_its_jobs(self):
        runtime = [s["name"] for s in kc.SURFACES if s["purpose"] == "runtime"]
        assert len(runtime) == 2

    def test_the_stats_endpoint_name_matches_the_app_s_own(self):
        # The script keeps its own copy so it stays stdlib+requests; this is the anti-drift check.
        from cqc_lem.utilities.posthog_endpoints import ENDPOINT_POSTS_ENGAGEMENT
        assert kc.STATS_ENDPOINT_NAME == ENDPOINT_POSTS_ENGAGEMENT


class TestPlanChecks:
    def test_no_filter_plans_every_surface(self):
        assert kc.plan_checks() == list(kc.SURFACES)
        assert kc.plan_checks([]) == list(kc.SURFACES)

    def test_a_filter_keeps_only_that_purpose(self):
        planned = kc.plan_checks(["query"])
        assert [s["purpose"] for s in planned] == ["query"]

    def test_an_unknown_purpose_raises_rather_than_planning_nothing(self):
        with pytest.raises(ValueError, match="Unknown purpose"):
            kc.plan_checks(["runtme"])


class TestRequestSpec:
    def test_a_get_surface_sends_no_body(self):
        surface = next(s for s in kc.SURFACES if s["purpose"] == "annotation")
        spec = kc.request_spec(surface, PROJECT, HOST)
        assert spec["method"] == "GET"
        assert spec["json"] is None
        assert spec["url"] == f"{HOST}/api/projects/{PROJECT}/annotations/?limit=1"

    def test_the_stats_endpoint_is_run_for_the_requested_distinct_id(self):
        surface = next(s for s in kc.SURFACES if s["name"] == "SPA stats endpoint")
        spec = kc.request_spec(surface, PROJECT, HOST, distinct_id=41)
        assert spec["json"] == {"variables": {"distinct_id": "41"}}
        assert spec["url"].endswith(f"/endpoints/{kc.STATS_ENDPOINT_NAME}/run/")

    def test_the_hogql_surface_carries_the_window(self):
        surface = next(s for s in kc.SURFACES if s["purpose"] == "query")
        spec = kc.request_spec(surface, PROJECT, HOST, hours=6)
        assert spec["json"]["query"]["kind"] == "HogQLQuery"
        assert "INTERVAL 6 HOUR" in spec["json"]["query"]["query"]

    def test_a_trailing_slash_on_the_host_is_not_doubled(self):
        surface = kc.SURFACES[0]
        assert "//api/" not in kc.request_spec(surface, PROJECT, HOST + "/")["url"]


class TestErrorHogql:
    def test_it_reads_the_same_event_the_cron_does(self):
        assert "$exception" in kc.error_hogql()

    @pytest.mark.parametrize("hours", [0, None])
    def test_an_unset_window_falls_back_to_the_default(self, hours):
        assert f"INTERVAL {kc.DEFAULT_HOURS} HOUR" in kc.error_hogql(hours)

    def test_a_negative_window_is_floored_to_one_hour_like_the_cron(self):
        # Same flooring as posthog_error_issues.build_query — never an invalid INTERVAL.
        assert "INTERVAL 1 HOUR" in kc.error_hogql(-5)


class TestDescribeKey:
    def test_it_names_the_scoped_var_when_that_is_what_answered(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_query")
        described = kc.describe_key("query")
        assert described["key"] == "phx_query"
        assert described["source"] == "POSTHOG_QUERY_API_KEY"

    def test_it_names_the_fallback_when_the_scoped_var_is_still_empty(self, monkeypatch):
        # Mid-rollout this is the difference between "populated" and "the old key is still doing it".
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        described = kc.describe_key("benchmark")
        assert described["source"] == "POSTHOG_PERSONAL_API_KEY"

    def test_no_key_at_all_carries_the_message_naming_both_vars(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        described = kc.describe_key("runtime")
        assert described["key"] == ""
        assert "POSTHOG_RUNTIME_API_KEY" in described["message"]
        assert "POSTHOG_PERSONAL_API_KEY" in described["message"]


class TestClassifyResponse:
    @pytest.mark.parametrize("status", [200, 201, 204])
    def test_a_2xx_passes(self, status):
        assert kc.classify_response(status)["ok"] is True

    @pytest.mark.parametrize("status,hint", [
        (401, "revoked"),
        (403, "scope"),
        (404, "not provisioned"),
        (500, "unexpected"),
    ])
    def test_a_failure_names_the_likely_cause(self, status, hint):
        result = kc.classify_response(status, '{"detail": "nope"}')
        assert result["ok"] is False
        assert hint in result["detail"]
        assert str(status) in result["detail"]


class TestRunCheck:
    def test_a_surface_with_no_key_fails_without_making_a_request(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        reader = _FakeReader()
        result = kc.run_check(kc.SURFACES[0], reader, PROJECT, HOST)
        assert result["ok"] is False
        assert reader.calls == []

    def test_a_successful_read_passes_and_used_the_resolved_key(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ANNOTATION_API_KEY", "phx_annotation")
        reader = _FakeReader([(200, "{}")])
        result = kc.run_check(kc.SURFACES[0], reader, PROJECT, HOST)
        assert result["ok"] is True
        assert reader.calls[0]["api_key"] == "phx_annotation"
        assert result["source"] == "POSTHOG_ANNOTATION_API_KEY"

    def test_a_transport_failure_is_a_fail_line_not_a_traceback(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        reader = _FakeReader(raises=OSError("name resolution failed"))
        result = kc.run_check(kc.SURFACES[0], reader, PROJECT, HOST)
        assert result["ok"] is False
        assert "request failed" in result["detail"]


class TestReporting:
    def test_a_line_names_the_purpose_surface_and_env_var(self):
        line = kc.format_result({"purpose": "runtime", "name": "feature-flag definitions",
                                 "source": "POSTHOG_RUNTIME_API_KEY", "ok": True,
                                 "detail": "HTTP 200"})
        assert line.startswith("PASS")
        assert "runtime" in line and "POSTHOG_RUNTIME_API_KEY" in line

    def test_a_line_never_prints_the_key_itself(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_secret_value")
        result = kc.run_check(next(s for s in kc.SURFACES if s["purpose"] == "query"),
                              _FakeReader([(200, "{}")]), PROJECT, HOST)
        assert "phx_secret_value" not in kc.format_result(result)

    def test_summary_counts_both_sides(self):
        results = [{"ok": True}, {"ok": False}, {"ok": True}]
        assert kc.summarize(results) == "2 passed, 1 failed"

    def test_exit_code_is_zero_only_when_everything_passed(self):
        assert kc.exit_code([{"ok": True}, {"ok": True}]) == 0
        assert kc.exit_code([{"ok": True}, {"ok": False}]) == 1

    def test_checking_nothing_is_not_success(self):
        # An empty plan must never read as "the rollout is fine".
        assert kc.exit_code([]) == 1


class TestMain:
    def test_list_prints_the_plan_and_makes_no_request(self, monkeypatch, capsys):
        monkeypatch.setattr(kc, "PostHogReader",
                            lambda *a, **k: pytest.fail("--list must not perform I/O"))
        assert kc.main(["--list"]) == 0
        out = capsys.readouterr().out
        for surface in kc.SURFACES:
            assert surface["name"] in out

    def test_an_unknown_purpose_exits_two(self, capsys):
        assert kc.main(["--purpose", "provisioning"]) == 2
        assert "Unknown purpose" in capsys.readouterr().err

    def test_a_full_pass_exits_zero_and_prints_one_line_per_surface(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.setattr(kc, "PostHogReader", lambda **k: _FakeReader([(200, "{}")] * 10))
        assert kc.main([]) == 0
        out = capsys.readouterr().out
        assert out.count("PASS") == len(kc.SURFACES)
        assert "0 failed" in out

    def test_one_bad_surface_fails_the_whole_run(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.setattr(kc, "PostHogReader",
                            lambda **k: _FakeReader([(200, "{}"), (403, "no scope")] + [(200, "{}")] * 8))
        assert kc.main([]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_a_purpose_filter_only_checks_that_purpose(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        reader = _FakeReader([(200, "{}")] * 10)
        monkeypatch.setattr(kc, "PostHogReader", lambda **k: reader)
        assert kc.main(["--purpose", "benchmark"]) == 0
        assert len(reader.calls) == 1
        assert "llm_analytics" in reader.calls[0]["url"]
