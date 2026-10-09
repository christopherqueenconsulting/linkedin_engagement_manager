"""Unit tests for `utilities/egress_probe.py` — the `/health/deep` egress reading (#2346).

The probe is exercised against a throwaway proxy on the loopback interface, so the classification
of each real proxy answer (200 / 407 / other / hang-up / nothing listening) is tested over a real
socket without leaving the machine.
"""

import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.egress_probe"


# Every credential-shaped string in this file (u:p, us%40er:p%3Ass, ...) is a fake placeholder sent
# only to a throwaway proxy on 127.0.0.1.


class _FakeProxy:
    """A one-connection-at-a-time loopback 'proxy' that answers CONNECT with a canned reply."""

    def __init__(self, reply: bytes | None):
        self.reply = reply
        self.requests: list = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(2)
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                self.requests.append(data)
                if self.reply is not None:
                    conn.sendall(self.reply)

    def close(self):
        self.sock.close()


@pytest.fixture
def fake_proxy():
    made = []

    def _make(reply):
        p = _FakeProxy(reply)
        made.append(p)
        return p

    yield _make
    for p in made:
        p.close()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    for name in ("EGRESS_PROBE_CACHE_SECONDS", "EGRESS_DEGRADE_AFTER_SECONDS",
                 "HEALTH_DEEP_EGRESS_DEGRADES", "EGRESS_PROBE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    from cqc_lem.utilities.egress_probe import reset_cache
    reset_cache()
    # Hermetic: no Redis unless a test hands one in. The in-process clock is the fallback.
    with patch(f"{_MOD}._redis", return_value=None):
        yield
    reset_cache()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestProbeProxy:
    def test_a_200_to_connect_is_ok_and_targets_linkedin(self, fake_proxy):
        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(b"HTTP/1.1 200 Connection established\r\n\r\n")
        assert probe_proxy(f"http://127.0.0.1:{proxy.port}", timeout=2) == "ok"
        assert proxy.requests[0].startswith(b"CONNECT www.linkedin.com:443 HTTP/1.1\r\n")
        assert b"Proxy-Authorization" not in proxy.requests[0]

    def test_credentials_are_sent_as_proxy_authorization(self, fake_proxy):
        import base64

        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(b"HTTP/1.1 200 OK\r\n\r\n")
        assert probe_proxy(f"http://us%40er:p%3Ass@127.0.0.1:{proxy.port}", timeout=2) == "ok"
        expected = base64.b64encode(b"us@er:p:ss")
        assert b"Proxy-Authorization: Basic " + expected in proxy.requests[0]

    def test_a_407_is_auth_failed(self, fake_proxy):
        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        assert probe_proxy(f"http://u:p@127.0.0.1:{proxy.port}", timeout=2) == "auth_failed"

    def test_any_other_status_is_refused(self, fake_proxy):
        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        assert probe_proxy(f"http://127.0.0.1:{proxy.port}", timeout=2) == "refused"

    def test_a_proxy_that_hangs_up_is_unreachable(self, fake_proxy):
        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(b"")
        assert probe_proxy(f"http://127.0.0.1:{proxy.port}", timeout=2) == "unreachable"

    def test_a_proxy_that_never_answers_times_out_as_unreachable(self):
        from cqc_lem.utilities.egress_probe import probe_proxy
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)  # accepts the TCP connection, never replies
        try:
            assert probe_proxy(f"http://127.0.0.1:{silent.getsockname()[1]}",
                               timeout=0.3) == "unreachable"
        finally:
            silent.close()

    def test_nothing_listening_is_unreachable(self):
        from cqc_lem.utilities.egress_probe import probe_proxy
        assert probe_proxy(f"http://127.0.0.1:{_free_port()}", timeout=1) == "unreachable"

    def test_socks_is_a_tcp_check_only(self, fake_proxy):
        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(None)
        assert probe_proxy(f"socks5://127.0.0.1:{proxy.port}", timeout=1) == "ok"

    def test_an_https_proxy_that_cannot_complete_tls_is_unreachable(self, fake_proxy):
        """An `https://` proxy is reached over TLS (1.2 minimum); a failed handshake is unreachable."""
        from cqc_lem.utilities.egress_probe import probe_proxy
        proxy = fake_proxy(b"HTTP/1.1 200 OK\r\n\r\n")  # plain text, not a TLS server hello
        assert probe_proxy(f"https://127.0.0.1:{proxy.port}", timeout=2) == "unreachable"

    def test_a_proxy_that_trickles_bytes_is_cut_off_at_the_deadline(self):
        """Each recv has its own timeout; the overall deadline stops a slow drip from stalling."""
        from cqc_lem.utilities.egress_probe import probe_proxy
        drip = socket.socket()
        drip.bind(("127.0.0.1", 0))
        drip.listen(1)
        stop = threading.Event()

        def serve():
            conn, _ = drip.accept()
            with conn:
                while not stop.is_set():
                    try:
                        conn.sendall(b"H")
                    except OSError:
                        return
                    stop.wait(0.1)

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            assert probe_proxy(f"http://127.0.0.1:{drip.getsockname()[1]}",
                               timeout=0.5) == "unreachable"
        finally:
            stop.set()
            drip.close()

    def test_a_url_without_a_host_is_unknown(self):
        from cqc_lem.utilities.egress_probe import probe_proxy
        assert probe_proxy("not a url", timeout=1) == "unknown"


class TestSummarize:
    def test_no_proxies_is_direct(self):
        from cqc_lem.utilities.egress_probe import summarize
        assert summarize([]) == "direct"

    def test_the_worst_state_wins(self):
        from cqc_lem.utilities.egress_probe import summarize
        assert summarize(["ok", "auth_failed", "ok"]) == "auth_failed"
        assert summarize(["refused", "unreachable"]) == "unreachable"
        assert summarize(["ok", "unknown"]) == "unknown"
        assert summarize(["ok", "ok"]) == "ok"


class TestEgressHealth:
    def test_counts_distinct_proxies_and_failures_without_naming_them(self):
        from cqc_lem.utilities.egress_probe import egress_health
        outcomes = {"http://a:1": "ok", "http://b:2": "unreachable"}
        with patch(f"{_MOD}.configured_egress_proxies", return_value=list(outcomes)), \
             patch(f"{_MOD}.probe_proxy", side_effect=lambda url: outcomes[url]):
            out = egress_health()
        assert out == {"egress": "unreachable", "egress_checked": 2, "egress_failing": 1}

    def test_unreadable_configuration_is_unknown(self):
        from cqc_lem.utilities.egress_probe import egress_health
        with patch(f"{_MOD}.configured_egress_proxies", return_value=None):
            assert egress_health() == {"egress": "unknown", "egress_checked": 0,
                                       "egress_failing": 0}

    def test_nobody_proxied_is_direct(self):
        from cqc_lem.utilities.egress_probe import egress_health
        with patch(f"{_MOD}.configured_egress_proxies", return_value=[]):
            assert egress_health()["egress"] == "direct"

    def test_a_reading_is_cached_so_a_burst_probes_once(self):
        from cqc_lem.utilities.egress_probe import egress_health
        with patch(f"{_MOD}.configured_egress_proxies", return_value=["http://a:1"]), \
             patch(f"{_MOD}.probe_proxy", return_value="ok") as probe:
            for _ in range(20):
                egress_health()
        probe.assert_called_once()

    def test_use_cache_false_probes_again(self):
        from cqc_lem.utilities.egress_probe import egress_health
        with patch(f"{_MOD}.configured_egress_proxies", return_value=["http://a:1"]), \
             patch(f"{_MOD}.probe_proxy", return_value="ok") as probe:
            egress_health()
            egress_health(use_cache=False)
        assert probe.call_count == 2

    def test_probes_are_bounded(self):
        from cqc_lem.utilities.egress_probe import egress_health
        urls = [f"http://p{i}:1" for i in range(25)]
        with patch(f"{_MOD}.configured_egress_proxies", return_value=urls), \
             patch(f"{_MOD}.probe_proxy", return_value="ok") as probe:
            out = egress_health()
        assert probe.call_count == 10
        assert out["egress_checked"] == 10


class _FakeRedis:
    def __init__(self):
        self.data: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = str(value).encode()
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0


class TestShouldDegrade:
    def _reading(self, state):
        from cqc_lem.utilities.egress_probe import egress_health
        with patch(f"{_MOD}.configured_egress_proxies", return_value=["http://a:1"]), \
             patch(f"{_MOD}.probe_proxy", return_value=state):
            return egress_health(use_cache=False)

    def test_a_fresh_failure_does_not_degrade_a_lasting_one_does(self):
        from cqc_lem.utilities.egress_probe import should_degrade
        with patch(f"{_MOD}.time.time", return_value=1000.0):
            self._reading("unreachable")
            assert should_degrade() is False
        with patch(f"{_MOD}.time.time", return_value=1000.0 + 299):
            assert should_degrade() is False
        with patch(f"{_MOD}.time.time", return_value=1000.0 + 300):
            assert should_degrade() is True

    def test_a_good_reading_resets_the_clock(self):
        from cqc_lem.utilities.egress_probe import should_degrade
        with patch(f"{_MOD}.time.time", return_value=1000.0):
            self._reading("unreachable")
        with patch(f"{_MOD}.time.time", return_value=2000.0):
            self._reading("ok")
            self._reading("unreachable")
            assert should_degrade() is False

    def test_the_clock_is_shared_across_workers_through_redis(self):
        """Prod runs several uvicorn workers; the first one to see the failure starts ONE clock."""
        from cqc_lem.utilities.egress_probe import reset_cache, should_degrade
        shared = _FakeRedis()
        with patch(f"{_MOD}._redis", return_value=shared):
            with patch(f"{_MOD}.time.time", return_value=1000.0):
                self._reading("unreachable")          # worker 1 starts the clock
            reset_cache()                              # worker 2: a fresh process, no local state
            with patch(f"{_MOD}.time.time", return_value=1400.0):
                self._reading("unreachable")          # NX: does not restart the clock
                assert should_degrade() is True
            with patch(f"{_MOD}.time.time", return_value=1500.0):
                self._reading("ok")                   # any worker's good reading clears it
                assert "health:egress_failing_since" not in shared.data
                assert should_degrade() is False

    def test_a_non_failing_reading_never_degrades(self):
        from cqc_lem.utilities.egress_probe import should_degrade
        with patch(f"{_MOD}.time.time", return_value=0.0):
            self._reading("unknown")
        with patch(f"{_MOD}.time.time", return_value=99999.0):
            assert should_degrade() is False

    def test_the_switch_turns_degrading_off(self, monkeypatch):
        from cqc_lem.utilities.egress_probe import should_degrade
        monkeypatch.setenv("HEALTH_DEEP_EGRESS_DEGRADES", "false")
        with patch(f"{_MOD}.time.time", return_value=0.0):
            self._reading("unreachable")
        with patch(f"{_MOD}.time.time", return_value=99999.0):
            assert should_degrade() is False

    def test_threshold_is_configurable(self, monkeypatch):
        from cqc_lem.utilities.egress_probe import should_degrade
        monkeypatch.setenv("EGRESS_DEGRADE_AFTER_SECONDS", "0")
        self._reading("auth_failed")
        assert should_degrade() is True

    def test_an_unreadable_redis_falls_back_to_the_local_clock(self):
        from cqc_lem.utilities.egress_probe import should_degrade
        broken = MagicMock()
        broken.set.side_effect = RuntimeError("down")
        broken.get.side_effect = RuntimeError("down")
        broken.delete.side_effect = RuntimeError("down")
        with patch(f"{_MOD}._redis", return_value=broken):
            with patch(f"{_MOD}.time.time", return_value=0.0):
                self._reading("unreachable")
            with patch(f"{_MOD}.time.time", return_value=301.0):
                assert should_degrade() is True
            self._reading("ok")
            assert should_degrade() is False


class TestConfiguredEgressProxies:
    def test_resolves_each_active_user_and_dedupes(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "http://global:3128")
        monkeypatch.delenv("REGION_PROXIES", raising=False)
        from cqc_lem.utilities.egress_probe import configured_egress_proxies
        rows = [{"user_id": 1, "proxy_url": "http://own:1", "country": "US"},
                {"user_id": 2, "proxy_url": None, "country": "US"},
                {"user_id": 3, "proxy_url": None, "country": "GB"},
                {"user_id": 4, "proxy_url": "http://own:1", "country": "US"}]
        with patch("cqc_lem.utilities.db.get_active_user_ids", return_value=[1, 2, 3, 4]), \
             patch("cqc_lem.utilities.db.get_users_proxy_config", return_value=rows):
            assert configured_egress_proxies() == ["http://own:1", "http://global:3128"]

    def test_no_active_users_is_an_empty_list(self):
        from cqc_lem.utilities.egress_probe import configured_egress_proxies
        with patch("cqc_lem.utilities.db.get_active_user_ids", return_value=[]):
            assert configured_egress_proxies() == []

    def test_a_resolution_error_is_none_not_a_raise(self):
        from cqc_lem.utilities.egress_probe import configured_egress_proxies
        with patch("cqc_lem.utilities.db.get_active_user_ids", side_effect=RuntimeError("db")):
            assert configured_egress_proxies() is None

    def test_a_database_outage_reads_as_no_proxies(self):
        """The DB helpers swallow a MySQL error and return no rows, so an outage reads `direct`.

        Documented in docs/stack-watchdog.md; the Celery fields and the app's own DB errors are
        what report a database outage, not the egress reading.
        """
        from cqc_lem.utilities.egress_probe import configured_egress_proxies
        with patch("cqc_lem.utilities.db.get_active_user_ids", return_value=[]):
            assert configured_egress_proxies() == []
