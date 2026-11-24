"""The MCP lifespan fails OPEN and can start twice in one process (epic #2682, plan §2).

SDK 2.3.0's `StreamableHTTPSessionManager.run()` may be entered once per instance, so every app
startup must build a fresh manager. A failure anywhere in that startup must leave the rest of the
app serving while `/api/mcp` answers 503.
"""

import asyncio
import logging
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import lifespan as lifespan_mod
from cqc_lem.api.mcp.lifespan import (
    REASON_BASE_URL_EMPTY,
    REASON_BASE_URL_INVALID,
    REASON_STARTUP_FAILED,
    mcp_lifespan,
    mcp_state,
)
from cqc_lem.api.mcp.server import mcp_server

pytestmark = pytest.mark.unit

_JSON = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _fresh_log_latch() -> Iterator[None]:
    """Each test sees the once-per-process ERROR latch empty, and leaves it as it found it."""
    saved = set(mcp_state.logged_reasons)
    mcp_state.logged_reasons.clear()
    yield
    mcp_state.logged_reasons.clear()
    mcp_state.logged_reasons.update(saved)


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.ERROR and "MCP" in r.getMessage()]


class TestStartsCleanly:
    def test_api_client_startup_brings_the_endpoint_up(self, api_client: Any) -> None:
        assert mcp_state.unavailable is False
        assert mcp_state.host == "testserver"
        assert mcp_state.origin == "http://testserver"
        assert mcp_state.dev_loopback is False

    def test_two_startups_in_one_process_each_get_a_fresh_manager(self) -> None:
        """`run()` is once-per-instance; entering the lifespan twice must not reuse one."""
        managers: list[object] = []

        async def _start_twice() -> None:
            for _ in range(2):
                async with mcp_lifespan(None):
                    assert mcp_state.unavailable is False
                    managers.append(mcp_server.session_manager)
                assert mcp_state.unavailable is True

        asyncio.run(_start_twice())
        assert len(managers) == 2 and managers[0] is not managers[1]

    def test_the_sdk_transport_security_agrees_with_the_gate(self) -> None:
        async def _start() -> Any:
            async with mcp_lifespan(None):
                return mcp_server.session_manager.security_settings

        settings = asyncio.run(_start())
        assert settings.enable_dns_rebinding_protection is True
        assert settings.allowed_hosts == ["testserver"]
        assert settings.allowed_origins == ["http://testserver"]

    def test_a_loopback_base_url_also_allows_loopback_hosts(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")

        async def _start() -> Any:
            async with mcp_lifespan(None):
                return mcp_server.session_manager.security_settings, mcp_state.dev_loopback

        settings, dev_loopback = asyncio.run(_start())
        assert dev_loopback is True
        assert "localhost:8000" in settings.allowed_hosts
        for pattern in ("127.0.0.1:*", "[::1]", "[::1]:*"):
            assert pattern in settings.allowed_hosts
        for pattern in ("http://[::1]", "http://[::1]:*", "http://127.0.0.1:*"):
            assert pattern in settings.allowed_origins

    def test_only_the_sdk_server_loggers_are_pinned(self) -> None:
        """`mcp.client` belongs to content research's MCP calls and must keep its own level."""
        assert logging.getLogger("mcp.server").level == logging.WARNING
        assert logging.getLogger("mcp").level == logging.NOTSET

    def test_a_default_port_is_dropped_from_the_expected_host(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://LEM.example.com:443/")

        async def _start() -> tuple[str, str]:
            async with mcp_lifespan(None):
                return mcp_state.host, mcp_state.origin

        assert asyncio.run(_start()) == ("lem.example.com", "https://lem.example.com")


class TestFailsOpen:
    @pytest.fixture
    def broken_client(self, request: pytest.FixtureRequest) -> Iterator[Any]:
        """`api_client`, started while the session manager's `run()` raises."""
        broken = MagicMock()
        broken.session_manager.run.side_effect = RuntimeError("manager exploded")
        with patch.object(lifespan_mod, "mcp_server", broken):
            yield request.getfixturevalue("api_client")

    def test_a_raising_manager_leaves_health_up_and_the_gate_at_503(
            self, broken_client: Any, fake_redis: Any, caplog: pytest.LogCaptureFixture) -> None:
        assert mcp_state.unavailable is True
        assert mcp_state.reason == REASON_STARTUP_FAILED

        assert broken_client.get("/health").status_code == 200
        resp = broken_client.post("/api/mcp", json={}, headers=_JSON)
        assert resp.status_code == 503
        assert resp.json()["reason"] == "mcp_unavailable"

    def test_a_raising_manager_logs_one_error_with_the_exception(
            self, caplog: pytest.LogCaptureFixture) -> None:
        broken = MagicMock()
        broken.session_manager.run.side_effect = RuntimeError("manager exploded")

        async def _start_twice() -> None:
            for _ in range(2):
                async with mcp_lifespan(None):
                    pass

        with patch.object(lifespan_mod, "mcp_server", broken), \
                caplog.at_level(logging.ERROR):
            asyncio.run(_start_twice())
        errors = _error_records(caplog)
        assert len(errors) == 1
        assert errors[0].exc_info is not None

    def test_an_empty_base_url_with_the_flag_on_gives_503_and_exactly_one_error(
            self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
            fake_redis: Any, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setenv("PUBLIC_BASE_URL", "")
        monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
        with caplog.at_level(logging.ERROR):
            client = request.getfixturevalue("api_client")
            first = client.post("/api/mcp", json={}, headers=_JSON)
            second = client.post("/api/mcp", json={}, headers=_JSON)

        assert mcp_state.reason == REASON_BASE_URL_EMPTY
        assert (first.status_code, second.status_code) == (503, 503)
        assert first.json()["reason"] == "mcp_unavailable"
        assert client.get("/health").status_code == 200
        assert len(_error_records(caplog)) == 1

    def test_an_empty_base_url_with_the_flag_off_is_info_not_error(
            self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """A deploy that never meant to serve MCP is not a defect."""
        monkeypatch.setenv("PUBLIC_BASE_URL", "")
        monkeypatch.delenv("MCP_SERVER_ENABLED", raising=False)

        async def _start() -> str:
            async with mcp_lifespan(None):
                return mcp_state.reason

        with caplog.at_level(logging.INFO, logger="cqc-lem"):
            reason = asyncio.run(_start())
        lines = [r for r in caplog.records if "PUBLIC_BASE_URL is empty" in r.getMessage()]
        assert [r.levelno for r in lines] == [logging.INFO]
        assert reason == REASON_BASE_URL_EMPTY

    @pytest.mark.parametrize("base_url", ["not-a-url", "ftp://lem.example.com", "https://x]",
                                          "http://[::1", "http://lem.example.com:abc"])
    def test_a_malformed_base_url_is_unavailable_never_a_raise(
            self, monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
        monkeypatch.setenv("PUBLIC_BASE_URL", base_url)

        async def _start() -> str:
            async with mcp_lifespan(None):
                return mcp_state.reason

        assert asyncio.run(_start()) == REASON_BASE_URL_INVALID

    @pytest.mark.parametrize("base_url", ["https://x]", "http://[::1"])
    def test_a_malformed_ipv6_base_url_leaves_the_api_serving(
            self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
            fake_redis: Any, base_url: str) -> None:
        """`urlparse` raises on these; before the fix that stopped the whole API at startup."""
        monkeypatch.setenv("PUBLIC_BASE_URL", base_url)
        client = request.getfixturevalue("api_client")

        assert client.get("/health").status_code == 200
        resp = client.post("/api/mcp", json={}, headers=_JSON)
        assert resp.status_code == 503
        assert resp.json()["reason"] == "mcp_unavailable"
        assert mcp_state.reason == REASON_BASE_URL_INVALID

    def test_a_shutdown_failure_is_logged_not_raised(
            self, caplog: pytest.LogCaptureFixture) -> None:
        class _BadExit:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: object) -> None:
                raise RuntimeError("shutdown exploded")

        broken = MagicMock()
        broken.session_manager.run.return_value = _BadExit()

        async def _start() -> None:
            async with mcp_lifespan(None):
                assert mcp_state.unavailable is False

        with patch.object(lifespan_mod, "mcp_server", broken), caplog.at_level(logging.ERROR):
            asyncio.run(_start())
        assert any("shut down" in r.getMessage() for r in _error_records(caplog))
        assert mcp_state.unavailable is True
