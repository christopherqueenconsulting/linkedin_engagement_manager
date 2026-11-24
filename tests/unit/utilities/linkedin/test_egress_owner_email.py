"""#2620: the owner is emailed when an egress streak escalates, re-notified daily, told on recovery."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MODULE = "cqc_lem.utilities.linkedin.helper"
_RL = "cqc_lem.utilities.linkedin.rate_limit"
_DAY = 24 * 3600


class _FakeHashRedis:
    """Enough Redis for the streak: HSETNX/HSET/HINCRBY/HGETALL/EXPIRE/DELETE/SCAN_ITER."""

    def __init__(self):
        self.store: dict = {}

    def hsetnx(self, key, field, value):
        h = self.store.setdefault(key, {})
        if field in h:
            return 0
        h[field] = str(value).encode()
        return 1

    def hset(self, key, field, value):
        self.store.setdefault(key, {})[field] = str(value).encode()

    def hincrby(self, key, field, amount):
        h = self.store.setdefault(key, {})
        h[field] = str(int(h.get(field, b"0")) + amount).encode()
        return int(h[field])

    def expire(self, key, seconds):
        return True

    def hgetall(self, key):
        return {k.encode(): v for k, v in self.store.get(key, {}).items()}

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def scan_iter(self, match=None, count=None):
        prefix = (match or "*").rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]


@pytest.fixture
def redis(monkeypatch):
    monkeypatch.delenv("LINKEDIN_EGRESS_ESCALATE_AFTER_SECONDS", raising=False)
    monkeypatch.delenv("LINKEDIN_EGRESS_ESCALATE_MIN_FAILURES", raising=False)
    client = _FakeHashRedis()
    with patch(f"{_RL}._redis_client", return_value=client):
        yield client


def _notifies(now_values, kind="base_page_not_origin"):
    from cqc_lem.utilities.linkedin.rate_limit import record_egress_failure
    return [bool(record_egress_failure(7, now=t, kind=kind, host_class="other")["notify_now"])
            for t in now_values]


class TestNotifyCadence:
    def test_exactly_one_then_none_for_24h_then_one_more(self, redis):
        # Sessions every 30 min: escalation at the 5th (2h), then a day of silence, then day two.
        times = [i * 1800 for i in range(0, 120)]
        flags = _notifies(times)
        sent_at = [t for t, f in zip(times, flags) if f]
        assert sent_at == [4 * 1800, 4 * 1800 + _DAY, 4 * 1800 + 2 * _DAY]

    def test_blip_or_burst_sends_nothing(self, redis):
        assert not any(_notifies([0]))
        assert not any(_notifies([10, 20, 30, 40, 50, 60]))

    def test_streak_keeps_kind_and_host_class_words_only(self, redis):
        from cqc_lem.utilities.linkedin.rate_limit import egress_streak_state
        _notifies([0], kind="feed_transport_error")
        state = egress_streak_state(7)
        assert state["kind"] == "feed_transport_error"
        assert state["host_class"] == "other"

    def test_clear_reports_how_many_emails_went_out(self, redis):
        from cqc_lem.utilities.linkedin.rate_limit import clear_egress_failures
        _notifies([i * 1800 for i in range(6)])
        cleared = clear_egress_failures(7)
        assert cleared["notified"] == 1
        assert cleared["escalated_at"] is not None
        assert clear_egress_failures(7) is None


class TestSummary:
    def test_redis_down_is_none(self):
        from cqc_lem.utilities.linkedin.rate_limit import egress_streak_summary
        with patch(f"{_RL}._redis_client", return_value=None):
            assert egress_streak_summary() is None

    def test_scan_error_is_none(self):
        from cqc_lem.utilities.linkedin.rate_limit import egress_streak_summary
        client = MagicMock()
        client.scan_iter.side_effect = RuntimeError("down")
        with patch(f"{_RL}._redis_client", return_value=client):
            assert egress_streak_summary() is None

    def test_no_escalated_streak_reads_zero(self, redis):
        from cqc_lem.utilities.linkedin.rate_limit import egress_streak_summary
        _notifies([0])  # a blip, not escalated
        assert egress_streak_summary() == {"down_since": None, "failures": 0, "kind": None}

    def test_escalated_streak_is_reported(self, redis):
        from cqc_lem.utilities.linkedin.rate_limit import egress_streak_summary
        _notifies([i * 1800 for i in range(6)])
        out = egress_streak_summary()
        assert out == {"down_since": 0, "failures": 6, "kind": "base_page_not_origin"}


class TestHelperWiring:
    def _state(self, **over):
        base = {"first_at": 0, "count": 4, "elapsed_seconds": 7200, "escalate_now": False,
                "notify_now": True}
        base.update(over)
        return base

    def test_notify_now_sends_one_email_with_kind_and_host_class(self, monkeypatch):
        from cqc_lem.utilities.linkedin.helper import _note_egress_failure
        monkeypatch.delenv("EGRESS_DOWN_EMAIL_ENABLED", raising=False)
        with patch(f"{_MODULE}.record_egress_failure", return_value=self._state()), \
             patch("cqc_lem.utilities.db.get_user_email", return_value="o@e.com"), \
             patch("cqc_lem.utilities.email.send_egress_down_email") as send:
            _note_egress_failure(7, "base_page_not_origin", "browser_error_page")
        send.assert_called_once_with("o@e.com", hours=2.0, failures=4,
                                     failure_kind="base_page_not_origin",
                                     host_class="browser_error_page")

    def test_error_line_carries_kind_and_host_class(self):
        from cqc_lem.utilities.linkedin.helper import _note_egress_failure
        with patch(f"{_MODULE}.record_egress_failure",
                   return_value=self._state(escalate_now=True, notify_now=False)), \
             patch(f"{_MODULE}._egress_label_for_user", return_value="proxy.example:8080"), \
             patch(f"{_MODULE}.log_error") as err, \
             patch("cqc_lem.utilities.observability.capture_exception"):
            _note_egress_failure(7, "feed_transport_error", "linkedin")
        message = err.call_args.args[0]
        assert "kind=feed_transport_error" in message and "host_class=linkedin" in message

    def test_no_notify_no_email(self):
        from cqc_lem.utilities.linkedin.helper import _note_egress_failure
        with patch(f"{_MODULE}.record_egress_failure",
                   return_value=self._state(notify_now=False)), \
             patch("cqc_lem.utilities.email.send_egress_down_email") as send:
            _note_egress_failure(7)
        send.assert_not_called()

    def test_redis_down_sends_nothing(self):
        from cqc_lem.utilities.linkedin.helper import _note_egress_failure
        with patch(f"{_MODULE}.record_egress_failure", return_value=None), \
             patch("cqc_lem.utilities.email.send_egress_down_email") as send:
            _note_egress_failure(7)
        send.assert_not_called()

    def test_flag_off_sends_no_email(self, monkeypatch):
        from cqc_lem.utilities.linkedin.helper import _note_egress_failure
        monkeypatch.setenv("EGRESS_DOWN_EMAIL_ENABLED", "false")
        with patch(f"{_MODULE}.record_egress_failure", return_value=self._state()), \
             patch("cqc_lem.utilities.email.send_egress_down_email") as send:
            _note_egress_failure(7)
        send.assert_not_called()

    def test_email_failure_never_raises(self):
        from cqc_lem.utilities.linkedin.helper import _note_egress_failure
        with patch(f"{_MODULE}.record_egress_failure", return_value=self._state()), \
             patch("cqc_lem.utilities.db.get_user_email", side_effect=RuntimeError("db")):
            _note_egress_failure(7)

    def test_recovery_notice_only_when_an_email_went_out(self):
        from cqc_lem.utilities.linkedin.helper import _clear_egress_streak
        with patch(f"{_MODULE}.clear_egress_failures",
                   return_value={"count": 9, "escalated_at": 5, "notified": 1}), \
             patch("cqc_lem.utilities.db.get_user_email", return_value="o@e.com"), \
             patch("cqc_lem.utilities.email.send_egress_down_email") as send:
            _clear_egress_streak(7)
        send.assert_called_once_with("o@e.com", hours=0, failures=9, recovered=True)
        with patch(f"{_MODULE}.clear_egress_failures",
                   return_value={"count": 9, "escalated_at": 5, "notified": 0}), \
             patch("cqc_lem.utilities.email.send_egress_down_email") as send:
            _clear_egress_streak(7)
        send.assert_not_called()

    @pytest.mark.parametrize("url,expected", [
        ("https://www.linkedin.com/feed/", "linkedin"),
        ("chrome-error://chromewebdata/", "browser_error_page"),
        ("https://interstitial.example/x", "other"),
        ("", "unknown"),
    ])
    def test_host_class_is_a_word(self, url, expected):
        from cqc_lem.utilities.linkedin.helper import _egress_host_class
        driver = MagicMock()
        driver.current_url = url
        assert _egress_host_class(driver) == expected

    def test_host_class_unreadable_driver(self):
        from cqc_lem.utilities.linkedin.helper import _egress_host_class
        bad = MagicMock()
        type(bad).current_url = property(lambda self: (_ for _ in ()).throw(RuntimeError()))
        assert _egress_host_class(bad) == "unknown"


class TestEmailText:
    def test_down_email_carries_words_only(self):
        from cqc_lem.utilities import email as mod
        with patch.object(mod, "_dispatch_email", return_value=True) as dispatch:
            assert mod.send_egress_down_email("o@e.com", 2.5, 7, "base_page_not_origin",
                                              "browser_error_page")
        _to, subject, html = dispatch.call_args.args[:3]
        assert "cannot reach LinkedIn" in subject
        assert "base_page_not_origin" in html and "browser_error_page" in html
        assert "2.5 hours" in html and "7 sessions" in html
        assert dispatch.call_args.kwargs["high_priority"] is True

    def test_failure_kind_is_escaped(self):
        from cqc_lem.utilities import email as mod
        with patch.object(mod, "_dispatch_email", return_value=True) as dispatch:
            mod.send_egress_down_email("o@e.com", 1, 3, "<script>", None)
        assert "<script>" not in dispatch.call_args.args[2]

    def test_recovery_email(self):
        from cqc_lem.utilities import email as mod
        with patch.object(mod, "_dispatch_email", return_value=True) as dispatch:
            mod.send_egress_down_email("o@e.com", 0, 9, recovered=True)
        assert "back online" in dispatch.call_args.args[1]
        assert dispatch.call_args.kwargs["high_priority"] is False
