"""Regression guards for the LinkedIn 429 root causes: unproxied Selenium sessions
and a pinned User-Agent that contradicts Chrome's User-Agent Client Hints."""

import ast
import pathlib

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.selenium_util"
_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "cqc_lem"


def _proxy_url(user: str, password: str, host: str, port: int) -> str:
    """Assemble a credentialed proxy URL from parts so no literal user:pass@host
    string lives in the source — secret scanners flag that as a Basic Auth String."""
    return f"http://{user}:{password}@{host}:{port}"


class TestNoUserAgentOverride:
    """A hardcoded UA cannot be kept truthful: --user-agent does not update
    navigator.userAgentData / Sec-CH-UA, so any pinned string eventually contradicts the
    real Chrome build and OS. Chrome's own UA is the only self-consistent option."""

    def test_base_options_set_no_user_agent(self):
        from cqc_lem.utilities.selenium_util import getBaseOptions
        args = getBaseOptions().arguments
        assert not any("user-agent" in a.lower() for a in args), (
            f"a --user-agent override was reintroduced: {args}")

    def test_docker_driver_sets_no_user_agent(self):
        args = _capture_options(user_id=None).arguments
        assert not any("user-agent" in a.lower() for a in args)


class TestProxyLabel:
    def test_direct_when_no_proxy(self):
        from cqc_lem.utilities.selenium_util import _proxy_label
        assert _proxy_label(None) == "DIRECT"
        assert _proxy_label("") == "DIRECT"

    def test_host_and_port_without_credentials(self):
        from cqc_lem.utilities.selenium_util import _proxy_label
        label = _proxy_label(_proxy_url("someuser", "s3cret", "host.example", 12323))
        assert label == "host.example:12323"
        assert "s3cret" not in label and "someuser" not in label

    def test_host_only_when_no_port(self):
        from cqc_lem.utilities.selenium_util import _proxy_label
        assert _proxy_label("http://proxy.internal") == "proxy.internal"

    def test_invalid_url_does_not_raise(self):
        from cqc_lem.utilities.selenium_util import _proxy_label
        assert _proxy_label("not a url") == "invalid"


class TestMissingUserIdIsLoud:
    """A session without user_id gets no proxy, timezone, locale or geo — it egresses from
    the host's datacenter IP. That must never happen silently again."""

    def test_warns_when_user_id_missing(self):
        with patch(f"{_MOD}.log_warning") as warn:
            _capture_options(user_id=None)
        messages = " ".join(str(c.args[0]) for c in warn.call_args_list)
        assert "WITHOUT user_id" in messages

    def test_no_missing_user_id_warning_when_provided(self):
        geo = {"latitude": 28.5, "longitude": -81.4, "timezone": "America/New_York",
               "locale": "en-US", "country": "US"}
        with patch(f"{_MOD}.log_warning") as warn:
            _capture_options(user_id=1, geo=geo, proxy=_proxy_url("u", "p", "host.example", 9000))
        messages = " ".join(str(c.args[0]) for c in warn.call_args_list)
        assert "WITHOUT user_id" not in messages

    def test_warns_when_falling_back_to_default_coordinates(self):
        with patch(f"{_MOD}.log_warning") as warn:
            _capture_options(user_id=None)
        messages = " ".join(str(c.args[0]) for c in warn.call_args_list)
        assert "no resolved geolocation" in messages


class TestEgressLogging:
    def test_logs_proxy_host_without_credentials(self):
        geo = {"latitude": 28.5, "longitude": -81.4, "timezone": "America/New_York",
               "locale": "en-US", "country": "US"}
        with patch(f"{_MOD}.log_info") as info:
            _capture_options(user_id=1, geo=geo, proxy=_proxy_url("user", "s3cret", "host.example", 9000))
        messages = " ".join(str(c.args[0]) for c in info.call_args_list)
        assert "egress=host.example:9000" in messages
        assert "s3cret" not in messages

    def test_logs_direct_when_unproxied(self):
        with patch(f"{_MOD}.log_info") as info:
            _capture_options(user_id=None)
        messages = " ".join(str(c.args[0]) for c in info.call_args_list)
        assert "egress=DIRECT" in messages


class TestEverySessionPassesUserId:
    """AST guard: every get_driver_wait_pair() call in the app must pass user_id, or that
    task silently loses its proxy and geo. This is the bug that caused the 429s."""

    def test_all_call_sites_pass_user_id(self):
        offenders = []
        for path in _SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name != "get_driver_wait_pair":
                    continue
                if not any(kw.arg == "user_id" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
        assert not offenders, (
            "get_driver_wait_pair() called without user_id — these sessions egress from the "
            f"host IP with no proxy/geo: {offenders}")


def _capture_options(user_id=None, geo=None, proxy=None):
    """Build a driver with all I/O mocked and return the ChromeOptions actually used."""
    captured = {}

    def _remote(command_executor=None, options=None):
        captured["options"] = options
        return MagicMock()

    with patch(f"{_MOD}._wait_for_selenium_ready"), \
         patch(f"{_MOD}.DEVICE_FARM_PROJECT_ARN", None), \
         patch(f"{_MOD}.TEST_GRID_PROJECT_ARN", None), \
         patch(f"{_MOD}.webdriver.Remote", side_effect=_remote), \
         patch("cqc_lem.utilities.db.get_user_geo", return_value=geo), \
         patch("cqc_lem.utilities.db.get_user_proxy", return_value=proxy):
        from cqc_lem.utilities.selenium_util import get_docker_driver
        get_docker_driver(headless=False, user_id=user_id)
    return captured["options"]
