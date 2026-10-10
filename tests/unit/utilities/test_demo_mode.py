"""DEMO_MODE refuses every LinkedIn client before any network call (#2372).

Three layers are pinned here:

* the switch itself — off by default, read at call time, the same truthy spellings as the env helpers;
* each transport's choke point — ``requests.Session.send`` for HTTP (which the ``linkedin_api`` SDK
  also rides) and ``get_docker_driver`` for Selenium — refusing with the socket boundary asserted
  untouched;
* a source scan that enumerates every LinkedIn client in ``src/`` so a new one cannot quietly
  bypass both.
"""

import ast
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import cqc_lem  # noqa: F401  (installs the HTTP guard, exactly as every process does)
from cqc_lem.utilities import demo_mode
from cqc_lem.utilities.demo_mode import DemoModeError, guard_linkedin, is_demo_mode, is_linkedin_url

SRC = Path(__file__).resolve().parents[3] / "src" / "cqc_lem"


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")


@pytest.fixture
def demo_off(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)


@pytest.fixture
def no_socket():
    """The adapter below `Session.send` is where a connection would open; it must never be reached."""
    def _ok(request, **_kwargs):
        response = requests.Response()
        response.status_code, response._content, response.url = 200, b"{}", request.url
        response.request = request
        return response

    with patch("requests.adapters.HTTPAdapter.send", side_effect=_ok) as adapter_send:
        yield adapter_send


# --------------------------------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------------------------------

class TestSwitch:
    def test_off_by_default(self, demo_off):
        assert is_demo_mode() is False
        guard_linkedin("anything")  # no raise

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "t", "y", "yes", " Yes "])
    def test_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("DEMO_MODE", value)
        assert is_demo_mode() is True

    @pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "demo"])
    def test_anything_else_is_off(self, monkeypatch, value):
        monkeypatch.setenv("DEMO_MODE", value)
        assert is_demo_mode() is False

    def test_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "1")
        assert is_demo_mode() is True
        monkeypatch.setenv("DEMO_MODE", "0")
        assert is_demo_mode() is False

    def test_guard_raises_naming_the_surface(self, demo_on):
        with pytest.raises(DemoModeError, match="poster.share_on_linkedin") as info:
            guard_linkedin("poster.share_on_linkedin")
        assert info.value.surface == "poster.share_on_linkedin"

    @pytest.mark.parametrize("url,expected", [
        ("https://api.linkedin.com/rest/posts", True),
        ("https://www.linkedin.com/oauth/v2/accessToken", True),
        ("https://linkedin.com/", True),
        ("https://media.licdn.com/dms/image/x.jpg", True),
        ("https://lnkd.in/abc", True),
        ("https://evil.com/?next=https://www.linkedin.com/", False),
        ("https://notlinkedin.com/", False),
        ("https://linkedin.com.evil.com/", False),
        ("https://api.bls.gov/publicAPI/v2/", False),
        ("", False),
        (None, False),
    ])
    def test_linkedin_host_match_is_parsed_not_substring(self, url, expected):
        assert is_linkedin_url(url) is expected


# --------------------------------------------------------------------------------------------------
# HTTP transport choke point
# --------------------------------------------------------------------------------------------------

class TestHttpTransportGuard:
    def test_installed_by_package_import(self):
        assert getattr(requests.Session.send, "_lem_demo_guard", False) is True

    def test_install_is_idempotent(self):
        before = requests.Session.send
        demo_mode.install_requests_guard()
        assert requests.Session.send is before

    @pytest.mark.parametrize("method,url", [
        ("post", "https://api.linkedin.com/rest/posts"),
        ("put", "https://www.linkedin.com/dms-uploads/abc"),
        ("get", "https://api.linkedin.com/v2/userinfo"),
        ("post", "https://www.linkedin.com/oauth/v2/accessToken"),
    ])
    def test_linkedin_request_refused_before_the_socket(self, demo_on, no_socket, method, url):
        with pytest.raises(DemoModeError):
            getattr(requests, method)(url, timeout=1)
        no_socket.assert_not_called()

    def test_other_hosts_still_go_out_in_demo_mode(self, demo_on, no_socket):
        requests.get("https://api.bls.gov/publicAPI/v2/timeseries/data/x", timeout=1)
        no_socket.assert_called_once()

    def test_linkedin_request_passes_when_off(self, demo_off, no_socket):
        requests.post("https://api.linkedin.com/rest/posts", timeout=1)
        no_socket.assert_called_once()

    def test_linkedin_sdk_restli_client_refused(self, demo_on, no_socket):
        # The `linkedin_api` SDK opens its own `requests.Session`; the guard sits below it too.
        from linkedin_api.clients.restli.client import RestliClient
        with pytest.raises(DemoModeError):
            RestliClient().get(resource_path="/userinfo", access_token="tok")
        no_socket.assert_not_called()

    def test_linkedin_sdk_auth_client_refused(self, demo_on, no_socket):
        from linkedin_api.clients.auth.client import AuthClient
        with pytest.raises(DemoModeError):
            AuthClient("id", "secret", "https://example.com/cb").exchange_auth_code_for_access_token("c")
        no_socket.assert_not_called()


# --------------------------------------------------------------------------------------------------
# Publishing (API path): refused at entry, before credentials, media or any request
# --------------------------------------------------------------------------------------------------

def _poster_calls():
    from cqc_lem.utilities.linkedin import poster
    return [
        ("share_on_linkedin", lambda: poster.share_on_linkedin(1, "hello", media_path="/tmp/x.png")),
        ("share_carousel_on_linkedin", lambda: poster.share_carousel_on_linkedin(1, "hi", ["a", "b"])),
        ("share_document_on_linkedin", lambda: poster.share_document_on_linkedin(1, "hi", ["a.png"])),
        ("share_animated_image_on_linkedin",
         lambda: poster.share_animated_image_on_linkedin(1, "hi", "/tmp/a.gif", "/tmp/a.png")),
        ("comment_on_linkedin_post",
         lambda: poster.comment_on_linkedin_post(1, "urn:li:share:1", "nice")),
        ("delete_linkedin_comment",
         lambda: poster.delete_linkedin_comment(1, "urn:li:share:1", "urn:li:comment:1")),
        ("upload_media", lambda: poster.upload_media("tok", "sub", "/tmp/x.png")),
        ("upload_document", lambda: poster.upload_document("tok", "sub", "/tmp/x.pdf")),
        ("upload_image_versioned", lambda: poster.upload_image_versioned("tok", "sub", "/tmp/x.gif")),
    ]


def _reshare_calls():
    from cqc_lem.utilities.linkedin import reshare
    return [
        ("share_reshare_on_linkedin",
         lambda: reshare.share_reshare_on_linkedin(1, "worth a read", "urn:li:share:1")),
        ("share_curated_image_on_linkedin",
         lambda: reshare.share_curated_image_on_linkedin(1, "chart", "/tmp/c.png")),
        ("share_article_on_linkedin",
         lambda: reshare.share_article_on_linkedin(1, "read", "https://example.com/a", "Title",
                                                   thumbnail_url="https://example.com/t.png")),
    ]


@pytest.fixture
def no_linkedin_io():
    """Every way a publish helper could reach the network or the DB, mocked so a call is visible."""
    with patch("requests.adapters.HTTPAdapter.send") as adapter_send, \
         patch("cqc_lem.utilities.linkedin.poster.requests") as poster_requests, \
         patch("cqc_lem.utilities.linkedin.poster.RestliClient") as restli, \
         patch("cqc_lem.utilities.linkedin.poster.get_user_access_token") as poster_token, \
         patch("cqc_lem.utilities.linkedin.poster.get_user_linked_sub_id") as poster_sub, \
         patch("cqc_lem.utilities.linkedin.reshare.requests") as reshare_requests, \
         patch("cqc_lem.utilities.linkedin.reshare.get_user_access_token") as reshare_token, \
         patch("cqc_lem.utilities.linkedin.reshare.get_user_linked_sub_id") as reshare_sub:
        mocks = [adapter_send, poster_requests, restli, poster_token, poster_sub, reshare_requests,
                 reshare_token, reshare_sub]
        yield mocks


class TestPublishingApiPath:
    @pytest.mark.parametrize("name,call", _poster_calls() + _reshare_calls(),
                             ids=[n for n, _ in _poster_calls() + _reshare_calls()])
    def test_refused_before_any_network_call(self, demo_on, no_linkedin_io, name, call):
        with pytest.raises(DemoModeError, match=name):
            call()
        for mock in no_linkedin_io:
            assert mock.mock_calls == [], f"{name} touched {mock} in demo mode"

    def test_publish_still_reaches_linkedin_when_off(self, demo_off, no_linkedin_io):
        # The control for the test above: with demo mode off the same mocks DO see the call.
        from cqc_lem.utilities.linkedin import poster
        _, _, restli, token, sub, *_ = no_linkedin_io
        token.return_value, sub.return_value = "tok", "sub"
        restli.return_value.create.return_value = MagicMock(entity_id="urn:li:share:9")
        poster.share_on_linkedin(1, "hello")
        restli.return_value.create.assert_called_once()


# --------------------------------------------------------------------------------------------------
# Token refresh
# --------------------------------------------------------------------------------------------------

class TestTokenRefresh:
    _INFO = {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
             "refresh_token_expires_in": 10 ** 9}

    def test_attempt_refresh_raises_before_post(self, demo_on):
        from cqc_lem.utilities.linkedin import token_refresh
        with patch.object(token_refresh, "requests") as req, \
             patch("cqc_lem.utilities.db.get_user_token_info") as info:
            with pytest.raises(DemoModeError):
                token_refresh.attempt_token_refresh(1)
        req.post.assert_not_called()
        info.assert_not_called()

    def test_status_read_still_answers_without_refreshing(self, demo_on):
        from cqc_lem.utilities.linkedin import token_refresh
        with patch.object(token_refresh, "requests") as req, \
             patch("cqc_lem.utilities.db.get_user_token_info", return_value=dict(self._INFO)), \
             patch.object(token_refresh, "is_token_expiring_soon", return_value=True), \
             patch.object(token_refresh, "refresh_token_usable", return_value=True):
            status = token_refresh.resolve_token_status(1, auto_refresh=True)
        assert status["connected"] is True
        assert status["refresh_attempted"] is False
        req.post.assert_not_called()


# --------------------------------------------------------------------------------------------------
# Selenium path
# --------------------------------------------------------------------------------------------------

class TestSeleniumPath:
    def test_get_docker_driver_refused_before_the_grid(self, demo_on):
        from cqc_lem.utilities import selenium_util
        with patch.object(selenium_util, "_wait_for_selenium_ready") as ready, \
             patch.object(selenium_util.webdriver, "Remote") as remote, \
             patch("cqc_lem.utilities.db.get_user_geo") as geo:
            with pytest.raises(DemoModeError, match="get_docker_driver"):
                selenium_util.get_docker_driver(user_id=1, session_name="Feed")
        ready.assert_not_called()
        remote.assert_not_called()
        geo.assert_not_called()

    def test_driver_wait_pair_is_refused_too(self, demo_on):
        # Every lane acquires its browser through this wrapper; it must not retry past the refusal.
        from cqc_lem.utilities import selenium_util
        with patch.object(selenium_util, "_wait_for_selenium_ready") as ready, \
             patch.object(selenium_util.webdriver, "Remote") as remote:
            with pytest.raises(DemoModeError):
                selenium_util.get_driver_wait_pair(user_id=1, session_name="Feed")
        ready.assert_not_called()
        remote.assert_not_called()

    def test_demo_safe_session_proceeds(self, demo_on):
        from cqc_lem.utilities import selenium_util

        class _Stop(Exception):
            pass

        with patch.object(selenium_util, "_wait_for_selenium_ready", side_effect=_Stop) as ready, \
             patch.object(selenium_util, "DEVICE_FARM_PROJECT_ARN", None):
            with pytest.raises(_Stop):
                selenium_util.get_docker_driver(session_name="TutorialCapture", demo_safe=True)
        ready.assert_called_once()

    def test_off_by_default_reaches_the_grid(self, demo_off):
        from cqc_lem.utilities import selenium_util

        class _Stop(Exception):
            pass

        with patch.object(selenium_util, "_wait_for_selenium_ready", side_effect=_Stop) as ready, \
             patch.object(selenium_util, "DEVICE_FARM_PROJECT_ARN", None):
            with pytest.raises(_Stop):
                selenium_util.get_docker_driver(user_id=1, session_name="Feed")
        ready.assert_called_once()


class TestEgressProbe:
    def test_probe_opens_no_socket_in_demo_mode(self, demo_on):
        from cqc_lem.utilities import egress_probe
        with patch.object(egress_probe.socket, "create_connection") as connect:
            assert egress_probe.probe_proxy("http://u:p@proxy.example:8080") == egress_probe.EGRESS_UNKNOWN
        connect.assert_not_called()


# --------------------------------------------------------------------------------------------------
# Enumeration: no LinkedIn client in src/ may bypass both choke points
# --------------------------------------------------------------------------------------------------

def _src_files():
    # The guard module names every client it guards; it is the one file that is not a client.
    return sorted(p for p in SRC.rglob("*.py")
                  if "/ui/" not in p.as_posix() and p.name != "demo_mode.py")


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


# Files that talk to a LinkedIn HTTP API. Each rides `requests` (the guarded transport) AND calls
# `guard_linkedin` at its entry points. Adding a file here is a review decision, not a formality.
_LINKEDIN_HTTP_CLIENTS = {
    "api/main.py",
    "utilities/linkedin/poster.py",
    "utilities/linkedin/reshare.py",
    "utilities/linkedin/token_refresh.py",
}
_LINKEDIN_API_MARKERS = re.compile(
    r"api\.linkedin\.com|linkedin\.com/oauth|linkedin\.com/v2|linkedin\.com/rest|"
    r"\bRestliClient\b|\bAuthClient\b")
# Any HTTP stack other than `requests` would go around the `Session.send` guard.
_UNGUARDED_HTTP = re.compile(
    r"^\s*(import|from)\s+(httpx|aiohttp|urllib3|pycurl|http\.client|urllib\.request)\b", re.M)
_LINKEDIN_HOST = re.compile(r"linkedin\.com|licdn\.com|lnkd\.in")


class TestEveryLinkedInClientIsGuarded:
    def test_linkedin_http_clients_are_exactly_the_guarded_set(self):
        found = {_rel(p) for p in _src_files() if _LINKEDIN_API_MARKERS.search(p.read_text())}
        assert found == _LINKEDIN_HTTP_CLIENTS, (
            "A file now talks to a LinkedIn API. Call `guard_linkedin(...)` at its entry points, "
            "use `requests` (the guarded transport), and add it to _LINKEDIN_HTTP_CLIENTS. "
            f"New: {sorted(found - _LINKEDIN_HTTP_CLIENTS)}; gone: {sorted(_LINKEDIN_HTTP_CLIENTS - found)}")

    @pytest.mark.parametrize("rel", sorted(_LINKEDIN_HTTP_CLIENTS))
    def test_each_client_calls_the_guard(self, rel):
        assert "guard_linkedin(" in (SRC / rel).read_text(), f"{rel} never calls guard_linkedin"

    def test_publish_entry_points_guard_first(self):
        # Several publish helpers swallow `Exception` around their LinkedIn calls, so the
        # transport's refusal alone would come back as a quiet None. Their FIRST statement after
        # the docstring must be the guard.
        wanted = {
            "utilities/linkedin/poster.py": [n for n, _ in _poster_calls()],
            "utilities/linkedin/reshare.py": [n for n, _ in _reshare_calls()],
            "utilities/linkedin/token_refresh.py": ["attempt_token_refresh"],
        }
        for rel, names in wanted.items():
            tree = ast.parse((SRC / rel).read_text())
            funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            for name in names:
                body = funcs[name].body
                stmt = body[1] if isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) else body[0]
                call = getattr(stmt, "value", None)
                assert isinstance(call, ast.Call) and getattr(call.func, "id", "") == "guard_linkedin", (
                    f"{rel}:{name} must open with guard_linkedin(...)")

    def test_no_linkedin_file_uses_an_unguarded_http_stack(self):
        offenders = [_rel(p) for p in _src_files()
                     if _LINKEDIN_HOST.search(p.read_text()) and _UNGUARDED_HTTP.search(p.read_text())]
        assert offenders == [], f"LinkedIn-referencing files using a non-requests HTTP stack: {offenders}"

    def test_raw_sockets_to_linkedin_check_demo_mode(self):
        users = [_rel(p) for p in _src_files()
                 if _LINKEDIN_HOST.search(p.read_text())
                 and re.search(r"socket\.create_connection|socket\.socket\(", p.read_text())]
        assert users == ["utilities/egress_probe.py"], users
        assert "is_demo_mode()" in (SRC / "utilities/egress_probe.py").read_text()

    def test_only_selenium_util_constructs_a_browser(self):
        pattern = re.compile(r"webdriver\.(Remote|Chrome|Firefox|Edge|Safari)\(")
        offenders = [_rel(p) for p in _src_files() if pattern.search(p.read_text())]
        assert offenders == ["utilities/selenium_util.py"], offenders

    def test_demo_safe_sessions_are_allowlisted(self):
        # `demo_safe=True` opens a browser in demo mode; only the SPA tutorial recorder may.
        users = []
        for p in _src_files():
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.Call) and any(
                        k.arg == "demo_safe" and not (isinstance(k.value, ast.Constant)
                                                      and k.value.value is False)
                        for k in node.keywords):
                    users.append(_rel(p))
        assert users == ["utilities/marketing/video_tutorials.py"], users
