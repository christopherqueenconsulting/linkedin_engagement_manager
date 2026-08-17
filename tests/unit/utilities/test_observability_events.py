"""The contracts the declarative EVENTS spec exists to make testable (issue #1218).

Before the spec, every event's property shape was a hand-written literal inside its own `track_*`
wrapper, so a rule that had to hold across ALL of them could only be prose in a docstring. The one
that matters most is the string rule: PostHog matches a property filter against the INGESTED type,
so an alert tile filtering `status = "paused"` matches nothing the moment some rows carry booleans,
and the alert then silently never fires (docs/kpi-dashboards.md). That is worse than having no
alert at all, and nothing in the tree could catch it.
"""

import inspect
import re
from unittest.mock import patch

import pytest

from cqc_lem.utilities import observability
from cqc_lem.utilities.observability import (
    DISTINCT_ANONYMOUS,
    DISTINCT_REQUIRED,
    DISTINCT_SYSTEM,
    DISTINCT_USER,
    DISTINCT_USER_STRICT,
    EVENTS,
    EventSpec,
    _emit,
    count,
    label,
    prop,
)

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.observability"

# Every (event, property) a provisioned PostHog dashboard tile or ALERT filters on, read off
# scripts/posthog_provision.py and docs/kpi-dashboards.md. Each MUST stay a `label()` in EVENTS:
# demoting one to `prop()` is exactly the silent-alert regression this file guards, and nothing
# else in the tree would notice.
ALERT_FILTERED = (
    # The ONE native property filter a provisioned tile actually carries today
    # (`_event_series("celery_task", properties=[{"key": "state", "value": ["FAILURE"], ...}])`),
    # and the alert behind it — "Celery failure spike" in docs/kpi-dashboards.md.
    ("celery_task", "state"),
    # The two the code itself already documented as load-bearing strings.
    ("feed_scan", "feed_sort"),                    # #817 — an unsorted scan must be distinguishable
    ("inbound_parse_email", "verdict"),            # the webhook drops most mail BY DESIGN
    # Lane health: "ran and did nothing" vs "silently broken" is read off `status`.
    ("company_page_invite_run", "status"),
    ("stale_invite_run", "status"),
    ("catchup_run", "status"),
    ("catchup_run", "phase"),
    ("suppression_check", "status"),
    ("suppression_check", "reach_status"),
    ("suppression_check", "comment_status"),
    ("comment_outcome", "status"),
    ("comment_outcome", "skip_reason"),
    ("comment_quality", "verdict"),
    ("golden_hour_report", "status"),
    ("golden_hour_report", "phase"),
    ("pre_post_engagement", "status"),
    ("youtube_token_check", "status"),
    ("rate_limit_trip", "reason"),
    # Content gates: the verdict IS the metric, and the surface is how it is broken down.
    ("image_gate_verdict", "verdict"),
    ("image_gate_verdict", "surface"),
    ("motion_prompt_check", "verdict"),
    ("motion_prompt_check", "surface"),
    ("content_quality", "surface"),
    ("video_asset_probe", "reason"),
    ("sdui_selector_evidence", "surface"),
    # Cost + traffic breakdowns.
    ("llm_call", "model"),
    ("llm_call", "feature"),
    ("media_cost", "kind"),
    ("media_cost", "provider"),
    ("api_call", "route"),
    ("api_call", "method"),
    # Activation and experiments.
    ("onboarding_step", "step"),
    ("$feature_flag_called", "experiment"),
    ("$feature_flag_called", "variant"),
)


def _fields(event: str) -> dict:
    return {field.name: field for field in EVENTS[event].fields}


def _payload(spec: EventSpec, value):
    """A reading that puts `value` at the read path of every dashboard-filtered field."""
    source: dict = {}
    for field in spec.fields:
        if not field.filtered:
            continue
        parts = (field.key or field.name).split(".")
        cursor = source
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return source


def _capture(spec: EventSpec, source=None, **kwargs) -> dict:
    with patch(f"{_MOD}.posthog") as mock_ph:
        _emit(spec, source, **kwargs)
    mock_ph.capture.assert_called_once()
    return mock_ph.capture.call_args.kwargs


class TestDashboardFilteredFieldsAreStrings:
    """The rule the whole spec exists to express — see this module's docstring."""

    @pytest.mark.parametrize("value", [True, False, 0, 1, 3.5, "recent"])
    def test_every_filtered_field_lands_as_a_string(self, value):
        checked = 0
        for name, spec in EVENTS.items():
            filtered = [field.name for field in spec.fields if field.filtered]
            if not filtered:
                continue
            props = _capture(spec, _payload(spec, value))["properties"]
            for field_name in filtered:
                assert isinstance(props[field_name], str), (
                    f"{name}.{field_name} ingested {type(props[field_name]).__name__} for "
                    f"{value!r}; a PostHog alert filtering it would match nothing and never fire"
                )
                checked += 1
        assert checked > 30, "the filtered-field census collapsed — did EVENTS lose its labels?"

    def test_an_unread_filtered_field_stays_none_rather_than_the_word_none(self):
        # "we could not read it" and the literal string "None" are different readings, and only one
        # of them is excluded from a breakdown.
        props = _capture(EVENTS["feed_scan"], {})["properties"]
        assert props["feed_sort"] is None

    @pytest.mark.parametrize("event,field_name", ALERT_FILTERED)
    def test_every_provisioned_alert_filter_is_declared_a_label(self, event, field_name):
        field = _fields(event).get(field_name)
        assert field is not None, f"{event} no longer carries {field_name}"
        assert field.filtered, (
            f"{event}.{field_name} is filtered by a provisioned PostHog tile/alert but is no "
            f"longer a label() — a non-string row would silence that alert"
        )

    def test_a_numeric_property_an_alert_compares_is_not_stringified(self):
        # The opposite failure: `status_code >= 500` needs a number. Forcing it to a string would
        # break the comparison instead of fixing anything.
        assert _fields("api_call")["status_code"].filtered is False
        props = _capture(EVENTS["api_call"], {"status_code": 503})["properties"]
        assert props["status_code"] == 503


class TestEmit:
    def test_extra_is_applied_last_so_a_call_site_can_override_a_declared_property(self):
        spec = EventSpec("t", (prop("a"), prop("b")))
        props = _capture(spec, {"a": 1, "b": 2}, extra={"b": 9, "c": 3})["properties"]
        assert props == {"a": 1, "b": 9, "c": 3}

    def test_a_dotted_key_reads_a_nested_value(self):
        spec = EventSpec("t", (prop("verdict_reason", "verdict.reason"),))
        assert _capture(spec, {"verdict": {"reason": "thin"}})["properties"] == {
            "verdict_reason": "thin"}

    def test_a_dotted_key_whose_path_breaks_reads_none_rather_than_raising(self):
        spec = EventSpec("t", (prop("x", "a.b.c"),))
        assert _capture(spec, {"a": "not a dict"})["properties"] == {"x": None}

    def test_only_declared_fields_become_properties(self):
        # A report dict is the lane's own working state; the event schema is this module's.
        spec = EventSpec("t", (prop("kept"),))
        props = _capture(spec, {"kept": 1, "internal_cursor": "leaky"})["properties"]
        assert props == {"kept": 1}

    def test_a_missing_count_is_zero_not_none(self):
        assert _capture(EventSpec("t", (count("n"),)), {})["properties"] == {"n": 0}

    def test_a_non_dict_source_emits_the_declared_shape_rather_than_raising(self):
        assert _capture(EventSpec("t", (count("n"),)), None)["properties"] == {"n": 0}

    @pytest.mark.parametrize("rule,user_id,expected", [
        (DISTINCT_USER, 7, "7"),
        (DISTINCT_USER, None, "system"),
        (DISTINCT_USER_STRICT, 0, "0"),
        (DISTINCT_USER_STRICT, None, "system"),
        (DISTINCT_REQUIRED, 7, "7"),
        (DISTINCT_ANONYMOUS, None, "anonymous"),
        (DISTINCT_ANONYMOUS, 7, "7"),
        (DISTINCT_SYSTEM, 7, "system"),
    ])
    def test_distinct_id_rules(self, rule, user_id, expected):
        spec = EventSpec("t", (), rule)
        assert _capture(spec, {"user_id": user_id})["distinct_id"] == expected

    def test_event_name_and_distinct_id_can_be_decided_per_call(self):
        # `track_affiliate_event` / `track_funnel_event` choose both at the call site.
        kwargs = _capture(EVENTS["affiliate_event"], {"user_id": 3},
                          event="affiliate_enrolled", distinct_id="anon_abc")
        assert kwargs["event"] == "affiliate_enrolled"
        assert kwargs["distinct_id"] == "anon_abc"


class TestOneEmitter:
    def test_the_module_has_exactly_one_posthog_capture_call(self):
        source = inspect.getsource(observability)
        sites = re.findall(r"posthog\.capture\(", source)
        assert len(sites) == 1, (
            "a capture written outside _emit() skips the EVENTS spec, so its properties are "
            "uncoerced and invisible to the dashboard-filtered-string contract"
        )

    def test_no_tracker_captures_directly(self):
        offenders = [
            name for name, fn in vars(observability).items()
            if name.startswith("track_") and inspect.isfunction(fn)
            and "posthog.capture(" in inspect.getsource(fn)
        ]
        assert offenders == []

    def test_every_registry_key_is_its_own_event_name(self):
        assert all(key == spec.event for key, spec in EVENTS.items())

    def test_every_public_tracker_still_exists(self):
        # The 36 public names are the API 74 call sites import; the refactor moved their BODIES,
        # never their names.
        trackers = {name for name, fn in vars(observability).items()
                    if name.startswith("track_") and inspect.isfunction(fn)}
        assert len(trackers) >= 36
        assert {"track_llm_call", "track_task", "track_api_call", "track_funnel_event",
                "track_feed_scan", "track_media_cost"} <= trackers


class TestLabelledEventsEndToEnd:
    """The two the code called out by name — proved through the public tracker, not just _emit."""

    def test_feed_scan_sort_state_is_a_string(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_feed_scan(1, {"feed_sort": "recent", "examined": 4})
        props = mock_ph.capture.call_args.kwargs["properties"]
        assert props["feed_sort"] == "recent"
        assert props["examined"] == 4

    def test_inbound_email_verdict_is_a_string(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_inbound_email("comment_accepted", user_id=2)
        kwargs = mock_ph.capture.call_args.kwargs
        assert kwargs["properties"] == {"verdict": "comment_accepted"}
        assert kwargs["distinct_id"] == "2"

    def test_celery_task_state_is_a_string_the_failure_alert_can_match(self):
        # A declared field, NOT one of track_task's `**extra`: extra is applied after the coercions,
        # so a `state` arriving that way would reach PostHog in whatever shape the caller sent and
        # the `state = "FAILURE"` filter would quietly stop matching.
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_task("cqc_lem.some_task", 12, success=False, state=False)
        props = mock_ph.capture.call_args.kwargs["properties"]
        assert props["state"] == "False"
        assert props["success"] is False

    def test_the_real_postrun_reading_lands_as_the_alert_expects(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_task("cqc_lem.some_task", 12, success=False, state="FAILURE")
        assert mock_ph.capture.call_args.kwargs["properties"]["state"] == "FAILURE"

    def test_a_label_declared_on_a_tracker_survives_a_boolean_reading(self):
        # The regression in miniature: a lane hands back a bool where the funnel promised a string.
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_feed_scan(1, {"feed_sort": False})
        assert mock_ph.capture.call_args.kwargs["properties"]["feed_sort"] == "False"


class TestAvatarLikenessProbeCause:
    """The unchecked CAUSE has to be countable, not free text (issue #1598).

    #1430 read 152 `avatar_likeness_probe` events, every one `checked=false`, and learning why took
    a scan of the distinct `reason` strings — which is whatever the vision model wrote.
    """

    def test_the_cause_is_declared_a_label_so_a_breakdown_can_count_it(self):
        field = _fields("avatar_likeness_probe").get("unchecked_cause")
        assert field is not None
        assert field.filtered, "a prop() cause is exactly the un-countable state #1598 removes"

    def test_the_probes_cause_reaches_the_event(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_avatar_likeness_probe(
                1, 9, {"present": None, "checked": False,
                       "reason": "No declared likeness attributes to verify",
                       "unchecked_cause": "no_declared_attributes"},
                used_avatar="true")
        assert mock_ph.capture.call_args.kwargs["properties"]["unchecked_cause"] == \
            "no_declared_attributes"

    @pytest.mark.parametrize("verdict", [
        {"present": True, "checked": True, "reason": "ok"},
        {"present": None, "checked": False, "reason": "no idea"},
        {},
        None,
    ])
    def test_every_path_lands_a_string_never_a_null_no_filter_matches(self, verdict):
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_avatar_likeness_probe(1, 9, verdict)
        assert isinstance(
            mock_ph.capture.call_args.kwargs["properties"]["unchecked_cause"], str)

    def test_a_checked_row_says_none_rather_than_dropping_out_of_the_breakdown(self):
        with patch(f"{_MOD}.posthog") as mock_ph:
            observability.track_avatar_likeness_probe(
                1, 9, {"present": True, "checked": True, "reason": "ok",
                       "unchecked_cause": "none"})
        assert mock_ph.capture.call_args.kwargs["properties"]["unchecked_cause"] == "none"


class TestSpecConstructors:
    def test_label_marks_the_field_filtered_and_stringifies(self):
        field = label("status")
        assert field.filtered is True
        assert field.coerce(True) == "True"

    def test_prop_is_verbatim_and_not_filtered(self):
        field = prop("raw")
        assert field.filtered is False
        assert field.coerce({"a": 1}) == {"a": 1}
