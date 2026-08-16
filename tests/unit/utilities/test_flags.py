"""Unit tests for the runtime feature-flag wrapper (issue #651).

The whole point of this module is what happens when PostHog does NOT answer, so most of these tests
are fallback tests: no key, key but no definitions, undefined flag, SDK raising. Every one of them
must land on the flag's env var, because that is the guarantee that lets a safety-critical
automation depend on this at all.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import flags

pytestmark = pytest.mark.unit

_PH = "cqc_lem.utilities.flags.posthog"


@contextmanager
def _sdk():
    """A stand-in posthog module that is CONFIGURED (not disabled) — a bare MagicMock's truthy
    `.disabled` would read as "PostHog turned off" and quietly take every test down the fallback
    path this file is trying to distinguish from.
    """
    with patch(_PH) as ph:
        ph.disabled = False
        ph.setup.return_value = MagicMock(personal_api_key=None, poll_interval=None)
        yield ph


@pytest.fixture(autouse=True)
def _clean_flag_state(monkeypatch):
    for name in ("POSTHOG_FLAGS_ENABLED", "POSTHOG_API_KEY", "POSTHOG_PERSONAL_API_KEY",
                 "POSTHOG_FLAG_POLL_SECONDS",
                 "COMMENT_RESEARCH_ENABLED", "TUTORIAL_VIDEOS_ENABLED",
                 "FEED_FALLBACK_WHEN_EMPTY_DEFAULT", "COST_ROUTING_ENABLED",
                 "NEWSLETTER_EDITOR_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    flags.reset_flag_state()
    yield
    flags.reset_flag_state()


def _with_backend(monkeypatch):
    """The env a deployment that CAN evaluate locally has."""
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")


class TestRegistry:
    def test_every_spec_is_keyed_by_its_own_key(self):
        for key, spec in flags.FLAGS.items():
            assert spec.key == key

    def test_env_vars_are_unique_per_flag(self):
        env_vars = [spec.env_var for spec in flags.FLAGS.values()]
        assert len(env_vars) == len(set(env_vars))

    def test_exported_constants_are_all_registered(self):
        for key in (flags.COMMENT_RESEARCH, flags.TUTORIAL_VIDEOS,
                    flags.FEED_FALLBACK_DEFAULT, flags.COST_ROUTING,
                    flags.POSTHOG_SURVEYS, flags.NEWSLETTER_EDITOR,
                    flags.VIDEO_CAPTIONS):
            assert key in flags.FLAGS

    def test_safety_controls_are_not_flags(self):
        """The 429 breaker, the automation pause and the per-day caps stay in Redis/env.

        Matched on whole hyphen-separated WORDS, not substrings: `video-captions-enabled` is a
        render toggle, and a substring check reads the 'cap' inside 'captions' as a per-day cap.
        """
        forbidden = {"pause", "paused", "breaker", "rate", "limit", "cap", "caps", "suppression"}
        for key in flags.FLAGS:
            assert not forbidden & set(key.split("-")), key

    def test_unregistered_flag_raises_rather_than_silently_reading_false(self):
        with pytest.raises(KeyError):
            flags.flag_enabled("not-a-real-flag")

    def test_registry_rows_expose_the_documented_columns(self):
        row = next(r for r in flags.registry_rows() if r["key"] == flags.COMMENT_RESEARCH)
        assert row["env_var"] == "COMMENT_RESEARCH_ENABLED"
        assert row["default"] is False
        assert row["owner"] and row["description"]


class TestEnvDefault:
    def test_unset_env_uses_the_spec_default(self):
        assert flags.env_default(flags.COMMENT_RESEARCH) is False
        assert flags.env_default(flags.FEED_FALLBACK_DEFAULT) is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " off "])
    def test_off_values(self, monkeypatch, raw):
        monkeypatch.setenv("FEED_FALLBACK_WHEN_EMPTY_DEFAULT", raw)
        assert flags.env_default(flags.FEED_FALLBACK_DEFAULT) is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_on_values(self, monkeypatch, raw):
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", raw)
        assert flags.env_default(flags.COMMENT_RESEARCH) is True

    def test_blank_env_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("FEED_FALLBACK_WHEN_EMPTY_DEFAULT", "   ")
        assert flags.env_default(flags.FEED_FALLBACK_DEFAULT) is True


class TestFallsBackToEnvWhenPostHogCannotAnswer:
    def test_no_keys_at_all_never_touches_the_sdk(self, monkeypatch):
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with patch(_PH) as ph:
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is True
        ph.get_feature_flag.assert_not_called()
        ph.load_feature_flags.assert_not_called()

    def test_project_key_without_a_personal_key_is_not_local_evaluation(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "true")
        with patch(_PH) as ph:
            assert flags.flag_enabled(flags.TUTORIAL_VIDEOS) is True
        ph.get_feature_flag.assert_not_called()

    def test_master_kill_switch_disables_lookups_entirely(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("POSTHOG_FLAGS_ENABLED", "false")
        monkeypatch.setenv("COST_ROUTING_ENABLED", "true")
        with patch(_PH) as ph:
            assert flags.flag_enabled(flags.COST_ROUTING) is True
        ph.get_feature_flag.assert_not_called()

    def test_undefined_flag_in_posthog_falls_back(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with _sdk() as ph:
            ph.get_feature_flag.return_value = None  # not defined / not decidable locally
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is True

    def test_sdk_raising_falls_back(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with _sdk() as ph:
            ph.get_feature_flag.side_effect = RuntimeError("posthog exploded")
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is True

    def test_definition_load_failure_falls_back(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with _sdk() as ph:
            ph.load_feature_flags.side_effect = RuntimeError("unreachable")
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is True
            ph.get_feature_flag.assert_not_called()

    def test_a_failing_load_retries_at_most_once_per_poll_interval(self, monkeypatch):
        """Without the cooldown a PostHog outage would make every flag check in a feed loop attempt
        its own definition fetch.
        """
        _with_backend(monkeypatch)
        monkeypatch.setenv("POSTHOG_FLAG_POLL_SECONDS", "30")
        clock = [1000.0]
        monkeypatch.setattr(flags, "_now", lambda: clock[0])
        with _sdk() as ph:
            ph.load_feature_flags.side_effect = RuntimeError("unreachable")
            flags.flag_enabled(flags.COMMENT_RESEARCH)
            flags.flag_enabled(flags.COMMENT_RESEARCH)
            assert ph.load_feature_flags.call_count == 1

            clock[0] += 31
            flags.flag_enabled(flags.COMMENT_RESEARCH)
            assert ph.load_feature_flags.call_count == 2


class TestPostHogDecides:
    def test_flag_off_overrides_an_env_var_that_is_on(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        with _sdk() as ph:
            ph.get_feature_flag.return_value = False
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is False

    def test_flag_on_overrides_an_env_var_that_is_off(self, monkeypatch):
        _with_backend(monkeypatch)
        with _sdk() as ph:
            ph.get_feature_flag.return_value = True
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is True

    @pytest.mark.parametrize("variant,expected", [
        ("control", False), ("off", False), ("disabled", False),
        ("treatment", True), ("cheap-tier", True),
    ])
    def test_multivariate_values_coerce(self, monkeypatch, variant, expected):
        _with_backend(monkeypatch)
        with _sdk() as ph:
            ph.get_feature_flag.return_value = variant
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is expected


class TestLocalEvaluation:
    def test_lookups_never_make_a_network_request(self, monkeypatch):
        _with_backend(monkeypatch)
        with _sdk() as ph:
            ph.get_feature_flag.return_value = True
            flags.flag_enabled(flags.COMMENT_RESEARCH, user_id=7)
        kwargs = ph.get_feature_flag.call_args.kwargs
        assert kwargs["only_evaluate_locally"] is True
        assert kwargs["send_feature_flag_events"] is False

    def test_definitions_load_once_and_the_poller_keeps_them_fresh(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("POSTHOG_FLAG_POLL_SECONDS", "15")
        with _sdk() as ph:
            ph.get_feature_flag.return_value = False
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is False
            # A flip lands in the poller's refreshed definitions; the next in-process lookup sees it
            # without any further load call.
            ph.get_feature_flag.return_value = True
            assert flags.flag_enabled(flags.COMMENT_RESEARCH) is True
            assert ph.load_feature_flags.call_count == 1
            assert ph.setup.return_value.poll_interval == 15

    def test_settings_are_pushed_onto_an_already_built_client(self, monkeypatch):
        """setup() reuses a client that a prior capture() may have built with no personal key —
        that client would never poll.
        """
        _with_backend(monkeypatch)
        with _sdk() as ph:
            ph.get_feature_flag.return_value = True
            flags.flag_enabled(flags.COMMENT_RESEARCH)
        assert ph.setup.return_value.personal_api_key == "phx_test"

    def test_the_runtime_scoped_key_alone_enables_local_evaluation(self, monkeypatch):
        """The runtime-scoped key is sufficient on its own (issue #1453).

        Once POSTHOG_RUNTIME_API_KEY carries it, the shared POSTHOG_PERSONAL_API_KEY no longer has
        to be present in the app containers at all.
        """
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("POSTHOG_RUNTIME_API_KEY", "phx_runtime")
        with _sdk() as ph:
            assert flags.local_evaluation_available() is True
            ph.get_feature_flag.return_value = True
            flags.flag_enabled(flags.COMMENT_RESEARCH)
        assert ph.setup.return_value.personal_api_key == "phx_runtime"

    def test_the_runtime_scoped_key_outranks_the_shared_one(self, monkeypatch):
        _with_backend(monkeypatch)
        monkeypatch.setenv("POSTHOG_RUNTIME_API_KEY", "phx_runtime")
        with _sdk() as ph:
            ph.get_feature_flag.return_value = True
            flags.flag_enabled(flags.COMMENT_RESEARCH)
        assert ph.setup.return_value.personal_api_key == "phx_runtime"

    def test_another_purpose_s_key_does_not_enable_local_evaluation(self, monkeypatch):
        # The cron's query key reaching the app containers must not read as a runtime key.
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_query")
        with _sdk():
            assert flags.local_evaluation_available() is False

    def test_distinct_id_matches_the_observability_convention(self):
        assert flags.distinct_id(42) == "42"
        assert flags.distinct_id(None) == "system"

    def test_user_id_scopes_the_lookup(self, monkeypatch):
        _with_backend(monkeypatch)
        with _sdk() as ph:
            ph.get_feature_flag.return_value = True
            flags.flag_enabled(flags.COMMENT_RESEARCH, user_id=7)
        assert ph.get_feature_flag.call_args.args[1] == "7"


class TestBootstrapPayload:
    def test_every_registered_flag_is_present(self, monkeypatch):
        payload = flags.bootstrap_payload(3)
        assert set(payload["flags"]) == set(flags.FLAGS)
        assert payload["distinct_id"] == "3"

    def test_local_evaluation_is_false_without_a_backend(self):
        assert flags.bootstrap_payload()["local_evaluation"] is False

    def test_values_are_the_env_defaults_without_a_backend(self, monkeypatch):
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "true")
        payload = flags.bootstrap_payload()
        assert payload["flags"][flags.TUTORIAL_VIDEOS] is True
        assert payload["flags"][flags.COMMENT_RESEARCH] is False

    def test_local_evaluation_is_true_once_definitions_load(self, monkeypatch):
        _with_backend(monkeypatch)
        with _sdk() as ph:
            ph.get_feature_flag.return_value = True
            payload = flags.bootstrap_payload(1)
        assert payload["local_evaluation"] is True
        assert all(payload["flags"].values())
