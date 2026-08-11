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
