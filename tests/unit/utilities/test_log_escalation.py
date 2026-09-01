from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import log_escalation as esc


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("LOG_ESCALATION_ENABLED", raising=False)
    monkeypatch.delenv("LOG_ESCALATE_THRESHOLD", raising=False)
    monkeypatch.delenv("LOG_ESCALATE_REPEAT_EVERY", raising=False)
    monkeypatch.delenv("LOG_ESCALATE_MAX_PER_WINDOW", raising=False)
    monkeypatch.delenv("LOG_ESCALATE_EXCLUDE", raising=False)
    monkeypatch.delenv("LOG_ESCALATE_LOCAL_CAP", raising=False)
    esc.reset_state()
    yield
    esc.reset_state()


def _fake_redis(counts, budget=1):
    """A pipeline whose execute() returns [count, True, budget, True] like the real one."""
    client = MagicMock()
    pipe = MagicMock()
    state = {"i": 0}

    def _execute():
        value = counts[min(state["i"], len(counts) - 1)]
        state["i"] += 1
        return [value, True, budget, True]

    pipe.execute.side_effect = _execute
    client.pipeline.return_value = pipe
    return client


def _note(message, counts, budget=1, origin="mod.func"):
    with patch.object(esc, "_redis", return_value=_fake_redis(counts, budget)):
        return esc.note(message, "WARNING", origin)


# ── normalization / fingerprinting (pure) ─────────────────────────────────────

def test_distinct_selector_labels_do_not_collapse():
    """The headline requirement: two different broken selectors are two different problems."""
    _, a = esc.normalize_message("Selector miss: Feed sort control")
    _, b = esc.normalize_message("Selector miss: Reaction state")
    assert esc.fingerprint("WARNING", "m.f", a) != esc.fingerprint("WARNING", "m.f", b)


def test_same_message_same_fingerprint():
    _, a = esc.normalize_message("Selector miss: Feed sort control")
    _, b = esc.normalize_message("Selector miss: Feed sort control")
    assert esc.fingerprint("WARNING", "m.f", a) == esc.fingerprint("WARNING", "m.f", b)


@pytest.mark.parametrize("first,second", [
    ("Re-queueing orphaned connection request 41", "Re-queueing orphaned connection request 42"),
    ("Posts ready to post: [41, 42]", "Posts ready to post: []"),
    ("Unknown timezone 'America/New_York'", "Unknown timezone 'Europe/Berlin'"),
    ("Failed for https://a.example/x", "Failed for https://b.example/y"),
    ("User bob@example.com failed", "User sue@example.org failed"),
])
def test_volatile_tokens_collapse(first, second):
    _, a = esc.normalize_message(first)
    _, b = esc.normalize_message(second)
    assert a == b


def test_log_recipient_masks_a_name_and_collapses_per_contact():
    # A bare first name reaches the grouped $exception value and forks one defect into an issue per
    # contact. Quoting it lets normalize_message collapse it through the quoted-value mask, so every
    # contact shares one fingerprint and no name survives in the display.
    msg = "DM nurture: a reply from {} was detected but its text could not be read"
    _, key_a = esc.normalize_message(msg.format(esc.log_recipient("Asbjørn", "https://x/in/a")))
    display_b, key_b = esc.normalize_message(msg.format(esc.log_recipient("Bianca", "https://x/in/b")))
    assert key_a == key_b
    assert "Asbjørn" not in display_b and "Bianca" not in display_b


def test_log_recipient_masks_a_name_with_an_apostrophe():
    # The quoted-value mask stops at the first inner single quote, so O'Brien would leak its tail
    # and fork the fingerprint unless the apostrophe is dropped first.
    display, _ = esc.normalize_message(
        "reply from {} was detected".format(esc.log_recipient("O'Brien", "https://x/in/o")))
    assert display == "reply from <str> was detected"


def test_log_recipient_falls_back_to_the_masked_url():
    display, _ = esc.normalize_message(
        "reply from {} was detected".format(esc.log_recipient("", "https://x/in/c")))
    assert display == "reply from <url> was detected"


def test_call_site_is_part_of_the_key():
    _, key = esc.normalize_message("Could not fetch profile")
    assert esc.fingerprint("WARNING", "mod_a.f", key) != esc.fingerprint("WARNING", "mod_b.f", key)


def test_display_keeps_casing_key_is_folded():
    display, key = esc.normalize_message("Selector Miss: Feed Sort Control")
    assert display == "Selector Miss: Feed Sort Control"
    assert key == "selector miss: feed sort control"


def test_long_message_truncated_without_error():
    display, key = esc.normalize_message("x" * 10_000)
    assert len(display) <= 300 and esc.fingerprint("WARNING", "m.f", key)


def test_fingerprint_is_stable_across_releases():
    """Regression lock: changing normalization resets every counter and forks every PostHog issue,
    so it must be a deliberate act, not a side effect.
    """
    _, key = esc.normalize_message("Selector miss: Feed sort control")
    assert esc.fingerprint("WARNING", "cqc_lem.utilities.selenium_util.find_first", key) == \
        esc.fingerprint("WARNING", "cqc_lem.utilities.selenium_util.find_first", key)
    assert len(esc.fingerprint("WARNING", "m.f", key)) == 12


# ── counting / thresholds ─────────────────────────────────────────────────────

def test_below_threshold_returns_none():
    assert _note("boom", [1]) is None
    assert _note("boom", [2]) is None


def test_escalates_exactly_at_threshold():
    result = _note("boom", [3])
    assert result is not None and result["count"] == 3


def test_does_not_re_escalate_immediately_after_threshold():
    for count in (4, 5, 12):
        assert _note("boom", [count]) is None


def test_re_escalates_on_the_repeat_interval():
    assert _note("boom", [13]) is not None  # threshold 3 + repeat 10


def test_repeat_every_zero_escalates_once_only(monkeypatch):
    monkeypatch.setenv("LOG_ESCALATE_REPEAT_EVERY", "0")
    assert _note("boom", [13]) is None


def test_global_budget_caps_escalations(monkeypatch):
    monkeypatch.setenv("LOG_ESCALATE_MAX_PER_WINDOW", "5")
    assert _note("boom", [3], budget=6) is None
    esc.reset_state()
    assert _note("boom", [3], budget=2) is not None


# The counts below are the THRESHOLD (3), never an arbitrary large one: `_should_escalate` only
# fires at the threshold and every `LOG_ESCALATE_REPEAT_EVERY`th past it, so e.g. 99 returns None on
# its own arithmetic and an exclusion test written against it would pass with the exclusion deleted.
def test_excluded_prefix_never_escalates():
    assert _note("Could not capture exception in PostHog: boom", [3]) is None


def test_cost_alert_breaches_never_escalate():
    """A budget breach is a measurement the alerter was asked to report, and it repeats for as many
    days as the spend does — escalating it filed a code defect for working tooling (#1071).
    """
    from cqc_lem.utilities.cost_alerts import ALERT_LOG_PREFIX

    assert ALERT_LOG_PREFIX in esc.BUILTIN_EXCLUDED_PREFIXES
    breach = (f"{ALERT_LOG_PREFIX}user_cost_ceiling]: User #1 variable cost is 120.0% of tier MRR")
    assert _note(breach, [3]) is None
    # …and the exclusion is what did it: the identical line escalates once the prefix is gone.
    esc.reset_state()
    with patch.object(esc, "BUILTIN_EXCLUDED_PREFIXES", ()):
        assert _note(breach, [3]) is not None


def test_cost_alert_delivery_failures_still_escalate():
    """The exclusion is scoped to the breach digest — an alerter that cannot deliver IS a defect."""
    assert _note("Cost alert email failed", [3]) is not None


def test_env_exclusions_add_to_the_builtins(monkeypatch):
    """An override must not be able to drop the capture_exception re-entrancy guard."""
    monkeypatch.setenv("LOG_ESCALATE_EXCLUDE", "Custom noise")
    assert _note("Custom noise: boom", [3]) is None
    esc.reset_state()
    assert _note("Could not capture exception in PostHog: boom", [3]) is None


def test_disabled_switch_skips_redis(monkeypatch):
    monkeypatch.setenv("LOG_ESCALATION_ENABLED", "false")
    client = _fake_redis([3])
    with patch.object(esc, "_redis", return_value=client):
        assert esc.note("boom", "WARNING", "m.f") is None
    client.pipeline.assert_not_called()


# ── fail-open behaviour ───────────────────────────────────────────────────────

def test_no_redis_fails_open():
    with patch.object(esc, "_redis", return_value=None):
        assert esc.note("boom", "WARNING", "m.f") is None


def test_redis_error_fails_open():
    client = MagicMock()
    client.pipeline.side_effect = RuntimeError("redis down")
    with patch.object(esc, "_redis", return_value=client):
        assert esc.note("boom", "WARNING", "m.f") is None


def test_redis_circuit_opens_after_repeated_failures(monkeypatch):
    """A dead Redis costs a 2s connect timeout per call; after N failures we stop calling it, or
    every one of ~400 warning call sites would stall for 2s while Redis is down.
    """
    monkeypatch.setenv("LOG_ESCALATE_REDIS_FAILURES", "3")
    monkeypatch.setenv("LOG_ESCALATE_LOCAL_CAP", "1000")
    client = MagicMock()
    client.pipeline.side_effect = RuntimeError("redis down")
    with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=client):
        for _ in range(3):
            assert esc.note("boom", "WARNING", "m.f") is None
        calls_before = client.pipeline.call_count
        assert esc.note("boom", "WARNING", "m.f") is None
        assert client.pipeline.call_count == calls_before  # circuit open, no further Redis calls


def test_local_cap_stops_calling_redis(monkeypatch):
    monkeypatch.setenv("LOG_ESCALATE_LOCAL_CAP", "2")
    client = _fake_redis([1])
    with patch.object(esc, "_redis", return_value=client):
        esc.note("boom", "WARNING", "m.f")
        esc.note("boom", "WARNING", "m.f")
        calls = client.pipeline.call_count
        esc.note("boom", "WARNING", "m.f")
        assert client.pipeline.call_count == calls


# ── emission ──────────────────────────────────────────────────────────────────

def _record(count=3, fp="abc123abc123"):
    return {"count": count, "fingerprint": fp, "display": "Selector miss: Feed sort control",
            "window": 86400, "origin": "m.f", "level": "WARNING"}


def test_escalate_captures_recurring_warning_with_fingerprint():
    with patch("cqc_lem.utilities.observability.capture_exception") as cap:
        esc.escalate(_record(), {"user_id": 7})
    exc = cap.call_args.args[0]
    kwargs = cap.call_args.kwargs
    assert isinstance(exc, esc.RecurringWarning)
    assert str(exc) == "Selector miss: Feed sort control"
    assert kwargs["fingerprint"] == "lem-log:abc123abc123"
    assert kwargs["escalation_count"] == 3
    assert kwargs["escalated_from"] == "WARNING"
    assert kwargs["user_id"] == 7


def test_escalate_reuses_the_real_exception_when_present():
    original = ValueError("kaboom")
    with patch("cqc_lem.utilities.observability.capture_exception") as cap:
        esc.escalate(_record(), {}, exc=original)
    assert cap.call_args.args[0] is original


def test_escalate_is_not_reentrant():
    """capture_exception's own failure handler calls log_warning; that must not recurse."""
    calls = []

    def _capture(*a, **k):
        calls.append(1)
        esc.escalate(_record(), {})

    with patch("cqc_lem.utilities.observability.capture_exception", side_effect=_capture):
        esc.escalate(_record(), {})
    assert len(calls) == 1


def test_escalate_swallows_capture_failure():
    with patch("cqc_lem.utilities.observability.capture_exception",
               side_effect=RuntimeError("posthog down")):
        esc.escalate(_record(), {})  # must not raise
