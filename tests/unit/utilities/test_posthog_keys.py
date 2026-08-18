"""Unit tests for utilities/posthog_keys.py — purpose-scoped PostHog key resolution (issue #1453).

The property that matters is the ADDITIVE rollout: a scoped key wins where it is set, and an
environment that has none of them behaves exactly as it did when one shared key did everything.
"""

import pytest

from cqc_lem.utilities.posthog_keys import (
    ANNOTATION_ENV_VAR,
    BENCHMARK_ENV_VAR,
    FALLBACK_ENV_VAR,
    PURPOSE_ENV_VARS,
    QUERY_ENV_VAR,
    RUNTIME_ENV_VAR,
    annotation_api_key,
    benchmark_api_key,
    key_env_vars,
    missing_key_message,
    query_api_key,
    resolve_posthog_key,
    resolve_posthog_key_source,
    runtime_api_key,
)

pytestmark = pytest.mark.unit

_PURPOSES = ("annotation", "runtime", "query", "benchmark")


class TestResolvePosthogKey:
    @pytest.mark.parametrize("purpose", _PURPOSES)
    def test_falls_back_to_the_shared_key_when_the_scoped_one_is_unset(self, monkeypatch, purpose):
        # The whole rollout rests on this: nothing changes until a scoped key actually exists.
        monkeypatch.setenv(FALLBACK_ENV_VAR, "phx_shared")
        assert resolve_posthog_key(purpose) == "phx_shared"

    @pytest.mark.parametrize("purpose", _PURPOSES)
    def test_the_scoped_key_outranks_the_shared_one(self, monkeypatch, purpose):
        monkeypatch.setenv(FALLBACK_ENV_VAR, "phx_shared")
        monkeypatch.setenv(PURPOSE_ENV_VARS[purpose], "phx_scoped")
        assert resolve_posthog_key(purpose) == "phx_scoped"

    @pytest.mark.parametrize("purpose", _PURPOSES)
    def test_neither_set_is_an_empty_string_not_an_error(self, monkeypatch, purpose):
        monkeypatch.delenv(FALLBACK_ENV_VAR, raising=False)
        assert resolve_posthog_key(purpose) == ""

    @pytest.mark.parametrize("purpose", _PURPOSES)
    def test_a_blank_scoped_key_is_not_a_key(self, monkeypatch, purpose):
        # A key var left in place but emptied (mid-rotation) must not shadow the working one.
        monkeypatch.setenv(FALLBACK_ENV_VAR, "phx_shared")
        monkeypatch.setenv(PURPOSE_ENV_VARS[purpose], "   ")
        assert resolve_posthog_key(purpose) == "phx_shared"

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv(RUNTIME_ENV_VAR, "  phx_runtime\n")
        assert resolve_posthog_key("runtime") == "phx_runtime"

    def test_one_purpose_s_key_never_answers_another_s(self, monkeypatch):
        monkeypatch.delenv(FALLBACK_ENV_VAR, raising=False)
        monkeypatch.setenv(QUERY_ENV_VAR, "phx_query")
        assert resolve_posthog_key("query") == "phx_query"
        assert resolve_posthog_key("runtime") == ""
        assert resolve_posthog_key("annotation") == ""
        assert resolve_posthog_key("benchmark") == ""

    def test_the_benchmark_lane_does_not_ride_the_runtime_key(self, monkeypatch):
        # Owner decision 1A: the weekly cron gets its own key so the app containers' runtime key
        # never has to carry the LLM-evaluation scope.
        monkeypatch.delenv(FALLBACK_ENV_VAR, raising=False)
        monkeypatch.setenv(RUNTIME_ENV_VAR, "phx_runtime")
        assert resolve_posthog_key("benchmark") == ""

    def test_an_unknown_purpose_raises_rather_than_resolving_the_shared_key(self):
        with pytest.raises(ValueError, match="Unknown PostHog key purpose"):
            resolve_posthog_key("provisioning")


class TestKeyEnvVars:
    @pytest.mark.parametrize("purpose,scoped", [
        ("annotation", ANNOTATION_ENV_VAR),
        ("runtime", RUNTIME_ENV_VAR),
        ("query", QUERY_ENV_VAR),
        ("benchmark", BENCHMARK_ENV_VAR),
    ])
    def test_precedence_order_is_scoped_then_shared(self, purpose, scoped):
        assert key_env_vars(purpose) == (scoped, FALLBACK_ENV_VAR)

    def test_message_names_both_vars_so_a_reader_knows_what_to_set(self):
        message = missing_key_message("query")
        assert QUERY_ENV_VAR in message and FALLBACK_ENV_VAR in message

    def test_env_var_names_are_distinct(self):
        names = set(PURPOSE_ENV_VARS.values()) | {FALLBACK_ENV_VAR}
        assert len(names) == len(PURPOSE_ENV_VARS) + 1


class TestResolvePosthogKeySource:
    """Which VAR answered is the rollout's audit trail.

    A scoped key and the shared fallback can hold the same value while only one of them means the
    rollout step is done.
    """

    def test_it_names_the_scoped_var_when_that_one_answers(self, monkeypatch):
        monkeypatch.setenv(FALLBACK_ENV_VAR, "phx_same")
        monkeypatch.setenv(RUNTIME_ENV_VAR, "phx_same")
        assert resolve_posthog_key_source("runtime") == ("phx_same", RUNTIME_ENV_VAR)

    def test_it_names_the_fallback_when_the_scoped_var_is_unset(self, monkeypatch):
        monkeypatch.setenv(FALLBACK_ENV_VAR, "phx_shared")
        assert resolve_posthog_key_source("query") == ("phx_shared", FALLBACK_ENV_VAR)

    def test_neither_set_names_no_var(self, monkeypatch):
        monkeypatch.delenv(FALLBACK_ENV_VAR, raising=False)
        assert resolve_posthog_key_source("benchmark") == ("", "")

    @pytest.mark.parametrize("purpose", _PURPOSES)
    def test_it_agrees_with_the_value_resolve_returns(self, monkeypatch, purpose):
        monkeypatch.setenv(PURPOSE_ENV_VARS[purpose], "phx_scoped")
        assert resolve_posthog_key_source(purpose)[0] == resolve_posthog_key(purpose)


class TestNamedAccessors:
    def test_each_accessor_reads_its_own_purpose(self, monkeypatch):
        monkeypatch.setenv(ANNOTATION_ENV_VAR, "phx_annotation")
        monkeypatch.setenv(RUNTIME_ENV_VAR, "phx_runtime")
        monkeypatch.setenv(QUERY_ENV_VAR, "phx_query")
        monkeypatch.setenv(BENCHMARK_ENV_VAR, "phx_benchmark")
        assert annotation_api_key() == "phx_annotation"
        assert runtime_api_key() == "phx_runtime"
        assert query_api_key() == "phx_query"
        assert benchmark_api_key() == "phx_benchmark"


class TestStdlibOnly:
    def test_module_imports_nothing_from_cqc_lem(self):
        # scripts/posthog_annotate.py imports this on a bare CI runner and
        # scripts/posthog_error_issues.py from a cron clone — a cqc_lem import here breaks both.
        import pathlib

        import cqc_lem.utilities.posthog_keys as keys
        source = pathlib.Path(keys.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        body = code.split('"""', 2)[-1]
        assert "import cqc_lem" not in body
        assert "from cqc_lem" not in body
