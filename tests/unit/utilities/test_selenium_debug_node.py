"""Unit tests for the Grid debug-node opt-in pin (issue #753)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from selenium.webdriver.chrome.options import Options

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.selenium_util"


def _status_response(debug_host: str, slots: list[dict]):
    """Build a mock Grid /status response with one debug node."""
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "value": {
            "nodes": [
                {"uri": f"http://{debug_host}:5555", "slots": slots},
                {"uri": "http://selenium-node-chrome-1:5555", "slots": [{"session": None}]},
            ]
        }
    }
    return response


class TestApplyDebugNode:
    def test_adds_capability_when_debug_requested_and_node_free(self):
        options = Options()
        response = _status_response("selenium-node-debug", [{"session": None}])
        with patch(f"{_MOD}.requests.get", return_value=response), \
             patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", "selenium-node-debug"), \
             patch(f"{_MOD}.SELENIUM_HUB_HOST", "selenium-hub"), \
             patch(f"{_MOD}.SELENIUM_HUB_PORT", "4444"), \
             patch(f"{_MOD}.log_info"):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is True
            assert options.to_capabilities().get(SELENIUM_DEBUG_CAPABILITY) is True

    def test_no_capability_when_debug_not_requested(self):
        options = Options()
        response = _status_response("selenium-node-debug", [{"session": None}])
        with patch(f"{_MOD}.requests.get", return_value=response), \
             patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", "selenium-node-debug"), \
             patch(f"{_MOD}.SELENIUM_HUB_HOST", "selenium-hub"), \
             patch(f"{_MOD}.SELENIUM_HUB_PORT", "4444"):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=False) is False
            assert SELENIUM_DEBUG_CAPABILITY not in options.to_capabilities()

    def test_falls_back_when_debug_node_is_busy(self):
        options = Options()
        response = _status_response("selenium-node-debug", [{"session": {"sessionId": "x"}}])
        with patch(f"{_MOD}.requests.get", return_value=response), \
             patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", "selenium-node-debug"), \
             patch(f"{_MOD}.SELENIUM_HUB_HOST", "selenium-hub"), \
             patch(f"{_MOD}.SELENIUM_HUB_PORT", "4444"), \
             patch(f"{_MOD}.log_warning") as warn:
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is False
            assert SELENIUM_DEBUG_CAPABILITY not in options.to_capabilities()
            warn.assert_called_once()

    def test_falls_back_when_debug_node_not_registered(self):
        options = Options()
        response = MagicMock(status_code=200)
        response.json.return_value = {"value": {"nodes": [
            {"uri": "http://selenium-node-chrome-1:5555", "slots": [{"session": None}]},
        ]}}
        with patch(f"{_MOD}.requests.get", return_value=response), \
             patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", "selenium-node-debug"), \
             patch(f"{_MOD}.SELENIUM_HUB_HOST", "selenium-hub"), \
             patch(f"{_MOD}.SELENIUM_HUB_PORT", "4444"), \
             patch(f"{_MOD}.log_warning"):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is False
            assert SELENIUM_DEBUG_CAPABILITY not in options.to_capabilities()

    def test_falls_back_when_status_check_fails(self):
        options = Options()
        with patch(f"{_MOD}.requests.get", side_effect=Exception("hub down")), \
             patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", "selenium-node-debug"), \
             patch(f"{_MOD}.SELENIUM_HUB_HOST", "selenium-hub"), \
             patch(f"{_MOD}.SELENIUM_HUB_PORT", "4444"), \
             patch(f"{_MOD}.log_warning"):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is False
            assert SELENIUM_DEBUG_CAPABILITY not in options.to_capabilities()

    def test_falls_back_when_debug_host_not_configured(self):
        options = Options()
        with patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", ""), \
             patch(f"{_MOD}.log_warning"):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is False
            assert SELENIUM_DEBUG_CAPABILITY not in options.to_capabilities()


class TestGetDockerDriverDebug:
    def _run(self, *, env_value, explicit_debug):
        fake_driver = MagicMock()
        with patch(f"{_MOD}.os.getenv", return_value=env_value), \
             patch(f"{_MOD}.apply_debug_node") as apply, \
             patch(f"{_MOD}._wait_for_selenium_ready"), \
             patch(f"{_MOD}.DEVICE_FARM_PROJECT_ARN", None), \
             patch(f"{_MOD}.TEST_GRID_PROJECT_ARN", None), \
             patch(f"{_MOD}.getBaseOptions", return_value=Options()), \
             patch(f"{_MOD}.webdriver.Remote", return_value=fake_driver), \
             patch(f"{_MOD}._record_session_wait"), \
             patch("cqc_lem.utilities.db.get_user_geo", return_value=None), \
             patch("cqc_lem.utilities.db.get_user_proxy", return_value=None):
            from cqc_lem.utilities.selenium_util import get_docker_driver
            get_docker_driver(user_id=1, debug=explicit_debug)
            apply.assert_called_once()
            return apply.call_args[0][1]

    def test_env_var_requests_debug_when_no_explicit_flag(self):
        """When debug=None, get_docker_driver reads SELENIUM_DEBUG_NODE env var."""
        assert self._run(env_value="true", explicit_debug=None) is True

    def test_explicit_debug_false_overrides_env(self):
        assert self._run(env_value="true", explicit_debug=False) is False


class TestComposeDebugStereotype:
    def test_debug_node_advertises_lem_debug_stereotype(self):
        grid = (Path(__file__).resolve().parents[3] / "docker-compose.grid.yml").read_text()
        debug_block = grid.split("\n  selenium-node-debug:\n")[1].split("\n  selenium-chrome:")[0]
        assert "SE_NODE_STEREOTYPE_EXTRA" in debug_block
        assert '"lem:debug":true' in debug_block
        # The pool nodes must NOT advertise the same capability.
        pool_block = grid.split("\n  selenium-node-chrome:\n")[1].split("\n  selenium-node-debug:")[0]
        assert "lem:debug" not in pool_block
