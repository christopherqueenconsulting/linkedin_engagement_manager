"""The Selenium MCP browser may only ever run on the watchable debug node (issue #1301).

This server points at the SHARED hub, so before #1301 an agent (or the owner) opening a browser to
look at a page drew one of the eight Chrome slots the engagement lanes are sized for — competing
with the very commenting it was opened to debug. `.mcp.json` pre-enables `selenium-lem` for every
worktree-isolated agent, so that was one `start_browser` call away at all times.

The `mcp` package is an OPTIONAL poetry group and CI installs `--with test` only, so it is stubbed
here rather than imported. Skipping instead would leave this guard not running in the one place it
has to: the build.
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

SERVER = Path(__file__).resolve().parents[3] / "tools" / "selenium_mcp_server.py"


@pytest.fixture
def server(monkeypatch):
    """Load the MCP server module with `mcp` stubbed out."""
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    instance = MagicMock()
    # `@mcp.tool()` must hand the function back unchanged, or every tool becomes a MagicMock and
    # this test would assert against a mock instead of the code.
    instance.tool.return_value = lambda fn: fn
    fastmcp.FastMCP = MagicMock(return_value=instance)
    server_pkg = types.ModuleType("mcp.server")
    server_pkg.fastmcp = fastmcp
    root = types.ModuleType("mcp")
    root.server = server_pkg
    for name, module in (("mcp", root), ("mcp.server", server_pkg),
                         ("mcp.server.fastmcp", fastmcp)):
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("selenium_mcp_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStartBrowserRequiresTheDebugNode:
    def test_it_asks_for_the_debug_node_and_will_not_take_no_for_an_answer(self, server):
        with patch.object(server, "apply_debug_node", return_value=True) as pin, \
             patch.object(server.webdriver, "Remote", return_value=MagicMock()):
            server.start_browser()
        assert pin.call_args.kwargs == {"debug": True, "required": True}

    def test_no_debug_slot_means_no_session_at_all(self, server):
        from cqc_lem.utilities.selenium_util import DebugNodeUnavailable

        with patch.object(server, "apply_debug_node",
                          side_effect=DebugNodeUnavailable("every slot is claimed")), \
             patch.object(server.webdriver, "Remote") as remote:
            with pytest.raises(RuntimeError, match="No debug browser slot"):
                server.start_browser()
            # The point of the whole change: it does NOT fall back to the pool.
            remote.assert_not_called()

    def test_the_refusal_tells_the_caller_to_wait_rather_than_escalate(self, server):
        from cqc_lem.utilities.selenium_util import DebugNodeUnavailable

        with patch.object(server, "apply_debug_node",
                          side_effect=DebugNodeUnavailable("every slot is claimed")), \
             patch.object(server.webdriver, "Remote"):
            with pytest.raises(RuntimeError) as refused:
                server.start_browser()
        message = str(refused.value)
        assert "WAIT and retry" in message and "do not escalate" in message
        assert "every slot is claimed" in message  # the Grid's own reading, not a generic error

    def test_it_reads_no_env_var_to_decide_this(self, server, monkeypatch):
        # There is no opt-out, so `.mcp.json` needs no capability env var — and setting the old
        # SELENIUM_DEBUG_NODE=false cannot put an agent back on a production slot.
        monkeypatch.setenv("SELENIUM_DEBUG_NODE", "false")
        with patch.object(server, "apply_debug_node", return_value=True) as pin, \
             patch.object(server.webdriver, "Remote", return_value=MagicMock()):
            server.start_browser()
        assert pin.call_args.kwargs["required"] is True


class TestPosthogInitFailsOpen:
    """`_init_posthog` must never take the server down.

    A missing package, a missing or wrong-shaped key, or a client that won't construct are all
    disable-and-log, not raise.
    """

    def _clear_posthog_env(self, monkeypatch):
        for var in ("POSTHOG_API_KEY", "POSTHOG_PROJECT_TOKEN", "POSTHOG_HOST"):
            monkeypatch.delenv(var, raising=False)

    def test_missing_posthog_package_disables_analytics(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        with patch.object(server, "Posthog", None), patch.object(server, "instrument", None), \
             patch.object(server, "log_info") as log_info:
            result = server._init_posthog(MagicMock())
        assert result is None
        log_info.assert_called_once()

    def test_no_key_anywhere_disables_analytics(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        with patch.object(server, "log_info") as log_info:
            result = server._init_posthog(MagicMock())
        assert result is None
        log_info.assert_called_once()

    def test_posthog_api_key_env_var_instruments(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_from_env")
        fake_client = MagicMock()
        with patch.object(server, "Posthog", return_value=fake_client) as posthog_cls, \
             patch.object(server, "instrument") as instrument:
            result = server._init_posthog(MagicMock())
        assert result is fake_client
        posthog_cls.assert_called_once()
        assert posthog_cls.call_args.args[0] == "phc_from_env"
        instrument.assert_called_once()

    def test_posthog_project_token_env_var_is_the_fallback_name(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_from_dotenv")
        fake_client = MagicMock()
        with patch.object(server, "Posthog", return_value=fake_client), \
             patch.object(server, "instrument"):
            result = server._init_posthog(MagicMock())
        assert result is fake_client

    def test_a_key_shaped_like_a_personal_key_is_rejected(self, server, monkeypatch):
        # POSTHOG_API_KEY is documented repo-wide as the project token (`phc_…`); a personal key
        # (`phx_…`) would construct and instrument fine while silently dropping every event.
        self._clear_posthog_env(monkeypatch)
        monkeypatch.setenv("POSTHOG_API_KEY", "phx_looks_like_a_personal_key")
        with patch.object(server, "Posthog") as posthog_cls, patch.object(server, "log_warning") as log_warning:
            result = server._init_posthog(MagicMock())
        assert result is None
        posthog_cls.assert_not_called()
        log_warning.assert_called_once()

    def test_a_client_that_raises_disables_analytics_and_logs(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_valid_shape")
        with patch.object(server, "Posthog", side_effect=RuntimeError("boom")), \
             patch.object(server, "log_warning") as log_warning:
            result = server._init_posthog(MagicMock())
        assert result is None
        log_warning.assert_called_once()

    def test_debug_is_explicitly_off_so_stdout_stays_clean_for_the_stdio_transport(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_valid_shape")
        with patch.object(server, "Posthog") as posthog_cls, patch.object(server, "instrument"):
            server._init_posthog(MagicMock())
        assert posthog_cls.call_args.kwargs["debug"] is False

    def test_instrument_is_wired_with_the_argument_stripping_hook(self, server, monkeypatch):
        self._clear_posthog_env(monkeypatch)
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_valid_shape")
        with patch.object(server, "Posthog"), patch.object(server, "instrument") as instrument:
            server._init_posthog(MagicMock())
        assert instrument.call_args.kwargs["before_send"] is server._strip_tool_arguments


class TestPosthogShutdownNeverMasksTheRealException:
    def test_a_raising_shutdown_is_swallowed_and_logged(self, server, monkeypatch):
        fake_client = MagicMock()
        fake_client.shutdown.side_effect = RuntimeError("network flush failed")
        monkeypatch.setattr(server, "_posthog", fake_client)
        with patch.object(server, "log_warning") as log_warning:
            server._shutdown_posthog()
        fake_client.shutdown.assert_called_once()
        log_warning.assert_called_once()

    def test_no_client_is_a_no_op(self, server, monkeypatch):
        monkeypatch.setattr(server, "_posthog", None)
        server._shutdown_posthog()  # must not raise


class TestStripToolArguments:
    def test_it_drops_mcp_parameters_and_keeps_everything_else(self, server):
        event = {
            "event": "$mcp_tool_called",
            "properties": {
                "$mcp_tool_name": "type_into",
                "$mcp_parameters": {"request": {"params": {"arguments": {"text": "hunter2"}}}},
                "$mcp_duration": 12,
            },
        }
        result = server._strip_tool_arguments(event)
        assert "$mcp_parameters" not in result["properties"]
        assert result["properties"]["$mcp_tool_name"] == "type_into"
        assert result["properties"]["$mcp_duration"] == 12

    def test_it_tolerates_an_event_with_no_properties(self, server):
        assert server._strip_tool_arguments({"event": "$mcp_initialize"}) == {
            "event": "$mcp_initialize"
        }
