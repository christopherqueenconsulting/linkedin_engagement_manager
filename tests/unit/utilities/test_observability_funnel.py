"""Unit tests for the launch-funnel tracker in cqc_lem.utilities.observability (issue #503)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.observability"


class TestResolveChannel:
    def test_explicit_channel_wins_and_is_lowercased(self):
        from cqc_lem.utilities.observability import resolve_channel
        assert resolve_channel({"channel": "Affiliate", "utm_source": "linkedin"}) == "affiliate"

    def test_paid_medium_beats_a_search_engine_source(self):
        from cqc_lem.utilities.observability import resolve_channel, CHANNEL_PAID
        assert resolve_channel({"utm_source": "google", "utm_medium": "cpc"}) == CHANNEL_PAID

    @pytest.mark.parametrize("source,expected", [
        ("linkedin", "linkedin"),
        ("lem-newsletter", "newsletter"),
        ("substack", "newsletter"),
        ("google", "seo"),
        ("bing", "seo"),
        ("partner-xyz", "affiliate"),
    ])
    def test_source_rules(self, source, expected):
        from cqc_lem.utilities.observability import resolve_channel
        assert resolve_channel({"utm_source": source}) == expected

    @pytest.mark.parametrize("medium,expected", [
        ("email", "email"),
        ("newsletter", "newsletter"),
        ("referral", "referral"),
        ("organic", "seo"),
        ("social", "linkedin"),
        ("affiliate", "affiliate"),
    ])
    def test_medium_rules(self, medium, expected):
        from cqc_lem.utilities.observability import resolve_channel
        assert resolve_channel({"utm_medium": medium}) == expected

    def test_unrecognised_utms_are_other_not_direct(self):
        from cqc_lem.utilities.observability import resolve_channel, CHANNEL_OTHER
        assert resolve_channel({"utm_source": "some-podcast"}) == CHANNEL_OTHER

    def test_referrer_host_is_the_fallback(self):
        from cqc_lem.utilities.observability import resolve_channel
        assert resolve_channel({"referrer": "https://www.linkedin.com/feed/"}) == "linkedin"

    def test_unknown_referrer_is_a_referral(self):
        from cqc_lem.utilities.observability import resolve_channel, CHANNEL_REFERRAL
        assert resolve_channel({"referrer": "https://news.ycombinator.com/"}) == CHANNEL_REFERRAL

    def test_unparseable_referrer_still_resolves(self):
        from cqc_lem.utilities.observability import resolve_channel, CHANNEL_REFERRAL
        # urlparse raises on a malformed IPv6 URL — the channel must not blow up with it.
        assert resolve_channel({"referrer": "http://["}) == CHANNEL_REFERRAL

    def test_no_signal_is_direct(self):
        from cqc_lem.utilities.observability import resolve_channel, CHANNEL_DIRECT
        assert resolve_channel({}) == CHANNEL_DIRECT
        assert resolve_channel(None) == CHANNEL_DIRECT
        assert resolve_channel("not-a-dict") == CHANNEL_DIRECT


class TestNormalizeAttribution:
    def test_keeps_allowlisted_keys_and_derives_channel(self):
        from cqc_lem.utilities.observability import normalize_attribution
        props = normalize_attribution({
            "utm_source": " linkedin ", "utm_campaign": "launch",
            "landing_page": "/", "evil": "drop me", "utm_medium": "",
        })
        assert props == {"utm_source": "linkedin", "utm_campaign": "launch",
                         "landing_page": "/", "channel": "linkedin"}

    def test_channel_is_always_present(self):
        from cqc_lem.utilities.observability import normalize_attribution, CHANNEL_DIRECT
        assert normalize_attribution(None) == {"channel": CHANNEL_DIRECT}

    def test_long_values_are_truncated(self):
        from cqc_lem.utilities.observability import normalize_attribution, _ATTRIBUTION_MAX_LEN
        props = normalize_attribution({"utm_campaign": "x" * 500})
        assert len(props["utm_campaign"]) == _ATTRIBUTION_MAX_LEN


class TestAnonymousDistinctId:
    def test_is_stable_and_case_insensitive(self):
        from cqc_lem.utilities.observability import anonymous_distinct_id
        assert anonymous_distinct_id("A@b.com") == anonymous_distinct_id(" a@b.com ")

    def test_does_not_contain_the_email(self):
        from cqc_lem.utilities.observability import anonymous_distinct_id
        anon = anonymous_distinct_id("someone@example.com")
        assert anon.startswith("anon_") and "someone" not in anon

    def test_blank_email_falls_back_to_anonymous(self):
        from cqc_lem.utilities.observability import anonymous_distinct_id
        assert anonymous_distinct_id("") == "anonymous"


class TestTrackFunnelEvent:
    def test_captures_event_with_channel_and_extras(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_TRIAL_STARTED
        with patch(f"{_MOD}.posthog") as mock_ph:
            track_funnel_event(FUNNEL_TRIAL_STARTED, user_id=7,
                               attribution={"utm_source": "linkedin", "utm_medium": "social"},
                               trial_days=14)

        _, kwargs = mock_ph.capture.call_args
        assert kwargs["distinct_id"] == "7"
        assert kwargs["event"] == "trial_started"
        props = kwargs["properties"]
        assert props["channel"] == "linkedin"
        assert props["utm_source"] == "linkedin"
        assert props["user_id"] == 7
        assert props["trial_days"] == 14

    def test_sets_first_touch_person_properties_once(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_SIGNUP_COMPLETED
        with patch(f"{_MOD}.posthog") as mock_ph:
            track_funnel_event(FUNNEL_SIGNUP_COMPLETED, user_id=3,
                               attribution={"utm_campaign": "beta"})

        set_once = mock_ph.capture.call_args.kwargs["properties"]["$set_once"]
        assert set_once == {"initial_utm_campaign": "beta", "initial_channel": "other"}

    def test_anonymous_distinct_id_is_used_when_there_is_no_user(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_SIGNUP_STARTED
        with patch(f"{_MOD}.posthog") as mock_ph:
            track_funnel_event(FUNNEL_SIGNUP_STARTED, distinct_id="anon_abc")

        assert mock_ph.capture.call_args.kwargs["distinct_id"] == "anon_abc"
        assert mock_ph.alias.called is False

    def test_falls_back_to_the_anonymous_bucket(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_SIGNUP_STARTED
        with patch(f"{_MOD}.posthog") as mock_ph:
            track_funnel_event(FUNNEL_SIGNUP_STARTED)

        assert mock_ph.capture.call_args.kwargs["distinct_id"] == "anonymous"

    def test_alias_merges_the_pre_signup_person(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_SIGNUP_COMPLETED
        with patch(f"{_MOD}.posthog") as mock_ph:
            track_funnel_event(FUNNEL_SIGNUP_COMPLETED, user_id=9, alias_from="anon_abc")

        mock_ph.alias.assert_called_once_with(previous_id="anon_abc", distinct_id="9")

    def test_alias_is_skipped_when_it_would_be_a_self_alias(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_SIGNUP_STARTED
        with patch(f"{_MOD}.posthog") as mock_ph:
            track_funnel_event(FUNNEL_SIGNUP_STARTED, distinct_id="anon_abc",
                               alias_from="anon_abc")

        assert mock_ph.alias.called is False

    def test_unknown_event_warns_but_still_emits(self):
        from cqc_lem.utilities.observability import track_funnel_event
        with patch(f"{_MOD}.posthog") as mock_ph, \
             patch("cqc_lem.utilities.logger.log_warning") as warn:
            track_funnel_event("signup_startd", user_id=1)

        assert warn.called
        assert mock_ph.capture.call_args.kwargs["event"] == "signup_startd"

    def test_never_raises_when_posthog_fails(self):
        from cqc_lem.utilities.observability import track_funnel_event, FUNNEL_CHURNED
        with patch(f"{_MOD}.posthog") as mock_ph, \
             patch("cqc_lem.utilities.logger.log_warning") as warn:
            mock_ph.capture.side_effect = RuntimeError("posthog down")
            track_funnel_event(FUNNEL_CHURNED, user_id=1)

        assert warn.called
