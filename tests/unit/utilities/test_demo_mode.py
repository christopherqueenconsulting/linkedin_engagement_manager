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
        # Raw, unprepared URL: the stdlib reads evil.com, urllib3 reads api.linkedin.com. Either
        # parser seeing LinkedIn is enough.
        ("https://api.linkedin.com\\@evil.com/", True),
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
# A refusal is expected under demo mode: it never reaches error tracking
# --------------------------------------------------------------------------------------------------

class TestRefusalIsNotADefect:
    @pytest.mark.parametrize("level", ["log_warning", "log_error", "log_critical"])
    def test_logger_demotes_a_refusal_to_info(self, level):
        from cqc_lem.utilities import logger as lg
        with patch.object(lg, "logger") as py_logger, patch.object(lg, "_capture") as capture, \
             patch.object(lg.log_escalation, "note") as note:
            getattr(lg, level)("Could not publish", exc=DemoModeError("poster.share_on_linkedin"),
                               user_id=1)
        py_logger.info.assert_called_once()
        assert "poster.share_on_linkedin" in py_logger.info.call_args.args[0]
        py_logger.warning.assert_not_called()
        py_logger.error.assert_not_called()
        py_logger.critical.assert_not_called()
        capture.assert_not_called()
        note.assert_not_called()

    def test_control_a_real_error_is_still_captured(self):
        from cqc_lem.utilities import logger as lg
        with patch.object(lg, "logger") as py_logger, patch.object(lg, "_capture") as capture:
            lg.log_error("Could not publish", exc=RuntimeError("boom"))
        py_logger.error.assert_called_once()
        capture.assert_called_once()

    def test_celery_failure_and_retry_hooks_do_not_file_a_refusal(self):
        from types import SimpleNamespace

        from cqc_lem.app import my_celery
        sender = SimpleNamespace(name="cqc_lem.app.run_automation.post_to_linkedin")
        with patch.object(my_celery, "capture_exception") as capture:
            my_celery.on_task_failure(task_id="t", exception=DemoModeError("x"), sender=sender,
                                      kwargs={"user_id": 1})
            my_celery.on_task_retry(request=SimpleNamespace(kwargs={}, id="t", retries=0),
                                    reason=DemoModeError("x"), sender=sender)
            capture.assert_not_called()
            # Control: an ordinary failure is still filed by the same hooks.
            my_celery.on_task_failure(task_id="t", exception=RuntimeError("boom"), sender=sender,
                                      kwargs={})
            my_celery.on_task_retry(request=SimpleNamespace(kwargs={}, id="t", retries=0),
                                    reason=RuntimeError("boom"), sender=sender)
        assert capture.call_count == 2


class TestParserDifferential:
    @pytest.mark.parametrize("url", [
        "https://evil\\@api.linkedin.com/",
        "https://api.linkedin.com\\@evil.com/",
        "https://evil.com\\@api.linkedin.com/x",
        "https://a@b@api.linkedin.com/",
        "https://api.linkedin.com:443/rest/me",
        "https://API.LinkedIn.com./rest/me",
    ])
    def test_whatever_actually_connects_to_linkedin_is_refused(self, demo_on, no_socket, url):
        # The stdlib and urllib3 disagree about backslash-before-@ hosts (on the second URL the
        # stdlib reads evil.com while urllib3 — what requests connects with — reads
        # api.linkedin.com). The contract is about the CONNECTION: a request that would reach
        # LinkedIn is refused, and one that reaches the adapter is bound for somewhere else.
        try:
            requests.get(url, timeout=1)
        except DemoModeError:
            no_socket.assert_not_called()
            return
        sent = no_socket.call_args.args[0].url
        from urllib3.util import parse_url
        assert not is_linkedin_url(sent), sent
        assert not (parse_url(sent).host or "").lower().endswith("linkedin.com"), sent

    @pytest.mark.parametrize("url", ["https://api.linkedin.com\\@evil.com/",
                                     "https://a@b@api.linkedin.com/"])
    def test_the_differential_cases_that_reach_linkedin_are_refused(self, demo_on, no_socket, url):
        with pytest.raises(DemoModeError):
            requests.get(url, timeout=1)
        no_socket.assert_not_called()

    def test_ip_literals_are_not_matched(self):
        # Documented gap: the guard matches names, not addresses (docs/demo-mode.md).
        assert is_linkedin_url("https://13.107.42.14/") is False


# --------------------------------------------------------------------------------------------------
# Enumeration: no LinkedIn client in src/cqc_lem, scripts/ or tools/ may bypass the guards
# --------------------------------------------------------------------------------------------------

ROOT = SRC.parents[1]
SCAN_ROOTS = ("src/cqc_lem", "scripts", "tools")
# Not scanned, on purpose (docs/demo-mode.md "Keeping it complete"): `ui/` is the SPA, and
# `browser_extension/` runs in the operator's own Chrome, not on the stack.
_SKIP_PARTS = ("/ui/", "/browser_extension/", "/__pycache__/")


def _scan_files():
    out = []
    for root in SCAN_ROOTS:
        for p in (ROOT / root).rglob("*.py"):
            posix = p.as_posix()
            # The guard module names every client it guards; it is the one file that is not one.
            if any(s in posix for s in _SKIP_PARTS) or p.name == "demo_mode.py":
                continue
            out.append(p)
    return sorted(out)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _unguarded_calls(path: Path, matches) -> list:
    """Calls matching `matches(dotted_name)` with no guard call before them in their function.

    A guard is any call whose name ends in `guard_linkedin`. A matching call at module level is
    always unguarded.
    """
    tree = ast.parse(path.read_text())
    found, inside = [], set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        guards = [c.lineno for c in calls if _dotted(c.func).split(".")[-1].endswith("guard_linkedin")]
        for c in calls:
            if matches(_dotted(c.func)):
                inside.add(id(c))
                if not any(g < c.lineno for g in guards):
                    found.append(f"{_rel(path)}:{c.lineno} {_dotted(c.func)}")
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and matches(_dotted(c.func)) and id(c) not in inside:
            found.append(f"{_rel(path)}:{c.lineno} {_dotted(c.func)} (module level)")
    return found


# Files that talk to a LinkedIn HTTP API. Each calls `guard_linkedin` at its entry points; the src/
# ones ride `requests` (the guarded transport), the two scripts use urllib and guard every urlopen.
# Adding a file here is a review decision, not a formality.
_LINKEDIN_HTTP_CLIENTS = {
    "src/cqc_lem/api/main.py",
    "src/cqc_lem/utilities/linkedin/poster.py",
    "src/cqc_lem/utilities/linkedin/reshare.py",
    "src/cqc_lem/utilities/linkedin/token_refresh.py",
    "scripts/linkedin_version_check.py",
    "scripts/linkedin_post_stats_api_probe.py",
}
_LINKEDIN_API_MARKERS = re.compile(
    r"api\.linkedin\.com|linkedin\.com/oauth|linkedin\.com/v2|linkedin\.com/rest|"
    r"\bRestliClient\b|\bAuthClient\b")
_LINKEDIN_HOST = re.compile(r"linkedin\.com|licdn\.com|lnkd\.in")


def _non_requests_http(name: str) -> bool:
    """An HTTP call the `Session.send` guard cannot see."""
    return (name.split(".")[-1] == "urlopen"
            or name.startswith(("httpx.", "aiohttp.", "urllib3.", "http.client.", "pycurl.")))


def _requests_call(name: str) -> bool:
    return name.startswith("requests.") and name.split(".")[-1] in {
        "get", "post", "put", "patch", "delete", "head", "request", "Session"}


_BROWSER = re.compile(r"^(webdriver\.)?(Remote|Chrome|Firefox|Edge|Safari|ChromiumEdge)$")


class TestEveryLinkedInClientIsGuarded:
    def test_scan_roots_exist(self):
        for root in SCAN_ROOTS:
            assert any((ROOT / root).rglob("*.py")), f"scan root {root} has no python files"

    def test_linkedin_http_clients_are_exactly_the_guarded_set(self):
        found = {_rel(p) for p in _scan_files() if _LINKEDIN_API_MARKERS.search(p.read_text())}
        assert found == _LINKEDIN_HTTP_CLIENTS, (
            "A file now talks to a LinkedIn API. Call `guard_linkedin(...)` at its entry points "
            "and add it to _LINKEDIN_HTTP_CLIENTS. "
            f"New: {sorted(found - _LINKEDIN_HTTP_CLIENTS)}; gone: {sorted(_LINKEDIN_HTTP_CLIENTS - found)}")

    @pytest.mark.parametrize("rel", sorted(_LINKEDIN_HTTP_CLIENTS))
    def test_each_client_calls_the_guard(self, rel):
        assert "guard_linkedin(" in (ROOT / rel).read_text(), f"{rel} never calls guard_linkedin"

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

    def test_no_unguarded_non_requests_http_in_a_linkedin_file(self):
        # urllib / httpx / aiohttp / urllib3 / http.client go around `Session.send`, so in any
        # file that names a LinkedIn host every such call needs a guard before it in its function.
        offenders = []
        for p in _scan_files():
            if _LINKEDIN_HOST.search(p.read_text()):
                offenders += _unguarded_calls(p, _non_requests_http)
        assert offenders == [], offenders

    def test_scripts_and_tools_guard_their_own_requests_calls(self):
        # Under src/ the package import installs the `requests` guard. A script or tool may call
        # `requests` before anything imports `cqc_lem`, so there the call itself must be guarded.
        offenders = []
        for p in _scan_files():
            if not _rel(p).startswith("src/") and _LINKEDIN_HOST.search(p.read_text()):
                offenders += _unguarded_calls(p, _requests_call)
        assert offenders == [], offenders

    def test_raw_sockets_to_linkedin_check_demo_mode(self):
        users = [_rel(p) for p in _scan_files()
                 if _LINKEDIN_HOST.search(p.read_text())
                 and re.search(r"socket\.create_connection|socket\.socket\(", p.read_text())]
        assert users == ["src/cqc_lem/utilities/egress_probe.py"], users
        assert "is_demo_mode()" in (ROOT / users[0]).read_text()

    def test_every_browser_construction_is_guarded(self):
        builders = {_rel(p) for p in _scan_files()
                    if re.search(r"webdriver\.(Remote|Chrome|Firefox|Edge|Safari)\(", p.read_text())}
        assert builders == {"src/cqc_lem/utilities/selenium_util.py",
                            "tools/selenium_mcp_server.py"}, builders
        offenders = []
        for rel in sorted(builders):
            offenders += _unguarded_calls(ROOT / rel, lambda n: bool(_BROWSER.match(n)))
        assert offenders == [], offenders

    def test_demo_safe_sessions_are_allowlisted(self):
        # `demo_safe=True` opens a browser in demo mode; only the SPA tutorial recorder may.
        users = []
        for p in _scan_files():
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.Call) and any(
                        k.arg == "demo_safe" and not (isinstance(k.value, ast.Constant)
                                                      and k.value.value is False)
                        for k in node.keywords):
                    users.append(_rel(p))
        assert users == ["src/cqc_lem/utilities/marketing/video_tutorials.py"], users

    def test_the_scan_catches_an_unguarded_urlopen(self, tmp_path):
        # The scanner itself, against a planted offender, so a green run is not a vacuous one.
        bad = tmp_path / "bad.py"
        bad.write_text("import urllib.request\nURL = 'https://api.linkedin.com/rest/me'\n"
                       "def probe():\n    return urllib.request.urlopen(URL)\n")
        good = tmp_path / "good.py"
        good.write_text("import urllib.request\nURL = 'https://api.linkedin.com/rest/me'\n"
                        "def probe():\n    _guard_linkedin()\n    return urllib.request.urlopen(URL)\n")
        global ROOT
        saved, ROOT = ROOT, tmp_path
        try:
            assert _unguarded_calls(bad, _non_requests_http)
            assert _unguarded_calls(good, _non_requests_http) == []
        finally:
            ROOT = saved
