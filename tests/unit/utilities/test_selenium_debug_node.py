"""Unit tests for the Grid debug-node pin (issue #753, made two-way and enforceable in #1301)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from selenium.webdriver.chrome.options import Options

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.selenium_util"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _slot(session=None, advertised: bool = True) -> dict:
    stereotype = {"browserName": "chrome"}
    if advertised:
        stereotype["lem:debug"] = True
    return {"session": session, "stereotype": stereotype}


def _status_response(debug_host: str, slots: list[dict]):
    """Build a mock Grid /status response with one debug node and one pool node."""
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "value": {
            "nodes": [
                {"uri": f"http://{debug_host}:5555", "slots": slots},
                {"uri": "http://selenium-node-chrome-1:5555",
                 "slots": [{"session": None, "stereotype": {"lem:debug": False}}]},
            ]
        }
    }
    return response


def _patched_grid(response=None, **kwargs):
    """The hub-address patches every case needs, with `requests.get` wired to `response`."""
    get_kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
    return [
        patch(f"{_MOD}.requests.get", **get_kwargs),
        patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", kwargs.get("debug_host", "selenium-node-debug")),
        patch(f"{_MOD}.SELENIUM_HUB_HOST", "selenium-hub"),
        patch(f"{_MOD}.SELENIUM_HUB_PORT", "4444"),
    ]


class _Patches:
    """Tiny context helper so each case reads as one `with`."""

    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestApplyDebugNode:
    def test_adds_capability_when_debug_requested_and_node_free(self):
        options = Options()
        with _Patches(_patched_grid(_status_response("selenium-node-debug", [_slot()]))
                      + [patch(f"{_MOD}.log_info")]):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is True
            assert options.to_capabilities().get(SELENIUM_DEBUG_CAPABILITY) is True

    def test_a_production_session_declares_false_rather_than_omitting_the_capability(self):
        # The whole point of #1301: an ABSENT capability still matches the debug node (Grid only
        # compares extension capabilities a stereotype declares), so omitting it is not exclusion.
        options = Options()
        with _Patches(_patched_grid(_status_response("selenium-node-debug", [_slot()]))):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=False) is False
            assert options.to_capabilities()[SELENIUM_DEBUG_CAPABILITY] is False

    def test_falls_back_when_debug_node_is_busy(self):
        options = Options()
        busy = _status_response("selenium-node-debug", [_slot(session={"sessionId": "x"})])
        with _Patches(_patched_grid(busy) + [patch(f"{_MOD}.log_warning")]):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is False
            # Falling back means joining the pool, which is exactly the request the pool answers.
            assert options.to_capabilities()[SELENIUM_DEBUG_CAPABILITY] is False

    def test_falls_back_when_debug_node_not_registered(self):
        options = Options()
        response = MagicMock(status_code=200)
        response.json.return_value = {"value": {"nodes": [
            {"uri": "http://selenium-node-chrome-1:5555", "slots": [{"session": None}]},
        ]}}
        with _Patches(_patched_grid(response) + [patch(f"{_MOD}.log_warning")]):
            from cqc_lem.utilities.selenium_util import apply_debug_node
            assert apply_debug_node(options, debug=True) is False

    def test_falls_back_when_status_check_fails(self):
        options = Options()
        with _Patches(_patched_grid(Exception("hub down")) + [patch(f"{_MOD}.log_warning")]):
            from cqc_lem.utilities.selenium_util import apply_debug_node
            assert apply_debug_node(options, debug=True) is False

    def test_falls_back_when_debug_host_not_configured(self):
        options = Options()
        with patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", ""), patch(f"{_MOD}.log_warning"):
            from cqc_lem.utilities.selenium_util import apply_debug_node
            assert apply_debug_node(options, debug=True) is False

    def test_a_node_that_does_not_advertise_the_capability_is_not_a_pin(self):
        # The #753→#1301 live failure: the compose value carried literal quotes, the node's merge
        # failed, and `--watch` matched every node. A stereotype without the key must read as
        # "cannot be pinned", not as "the debug node".
        options = Options()
        unadvertised = _status_response("selenium-node-debug", [_slot(advertised=False)])
        with _Patches(_patched_grid(unadvertised) + [patch(f"{_MOD}.log_warning")]):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True) is False
            assert options.to_capabilities()[SELENIUM_DEBUG_CAPABILITY] is False


class TestRequiredDebugNode:
    """`required=True` is the agent's guarantee it never spends a lane slot (#1301)."""

    def _raises(self, response, **kwargs):
        from cqc_lem.utilities.selenium_util import DebugNodeUnavailable, apply_debug_node
        with _Patches(_patched_grid(response, **kwargs) + [patch(f"{_MOD}.log_warning")]):
            with pytest.raises(DebugNodeUnavailable):
                apply_debug_node(Options(), debug=True, required=True)

    def test_busy_node_refuses_instead_of_borrowing_a_pool_slot(self):
        self._raises(_status_response("selenium-node-debug", [_slot(session={"sessionId": "x"})]))

    def test_unreachable_hub_refuses(self):
        self._raises(Exception("hub down"))

    def test_unadvertised_capability_refuses(self):
        self._raises(_status_response("selenium-node-debug", [_slot(advertised=False)]))

    def test_unconfigured_host_refuses(self):
        from cqc_lem.utilities.selenium_util import DebugNodeUnavailable, apply_debug_node
        with patch(f"{_MOD}.SELENIUM_DEBUG_NODE_HOST", ""):
            with pytest.raises(DebugNodeUnavailable):
                apply_debug_node(Options(), debug=True, required=True)

    def test_a_free_advertised_node_is_pinned(self):
        options = Options()
        with _Patches(_patched_grid(_status_response("selenium-node-debug", [_slot()]))
                      + [patch(f"{_MOD}.log_info")]):
            from cqc_lem.utilities.selenium_util import SELENIUM_DEBUG_CAPABILITY, apply_debug_node
            assert apply_debug_node(options, debug=True, required=True) is True
            assert options.to_capabilities()[SELENIUM_DEBUG_CAPABILITY] is True


class TestGetDockerDriverDebug:
    def _run(self, *, env_value, explicit_debug, required=False):
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
            get_docker_driver(user_id=1, debug=explicit_debug, debug_required=required)
            apply.assert_called_once()
            return apply.call_args

    def test_env_var_requests_debug_when_no_explicit_flag(self):
        """When debug=None, get_docker_driver reads SELENIUM_DEBUG_NODE env var."""
        assert self._run(env_value="true", explicit_debug=None).args[1] is True

    def test_explicit_debug_false_overrides_env(self):
        assert self._run(env_value="true", explicit_debug=False).args[1] is False

    def test_debug_required_implies_debug_even_against_an_explicit_false(self):
        # Otherwise a caller could ask for "required" and still be handed a pool session.
        call = self._run(env_value="false", explicit_debug=False, required=True)
        assert call.args[1] is True
        assert call.kwargs["required"] is True

    def test_the_refusal_is_raised_before_a_session_is_requested(self):
        # A refusal that costs a Chrome slot is not a refusal.
        from cqc_lem.utilities.selenium_util import DebugNodeUnavailable, get_docker_driver
        with patch(f"{_MOD}.apply_debug_node", side_effect=DebugNodeUnavailable("busy")), \
             patch(f"{_MOD}._wait_for_selenium_ready"), \
             patch(f"{_MOD}.DEVICE_FARM_PROJECT_ARN", None), \
             patch(f"{_MOD}.TEST_GRID_PROJECT_ARN", None), \
             patch(f"{_MOD}.getBaseOptions", return_value=Options()), \
             patch(f"{_MOD}.webdriver.Remote") as remote, \
             patch("cqc_lem.utilities.db.get_user_geo", return_value=None), \
             patch("cqc_lem.utilities.db.get_user_proxy", return_value=None):
            with pytest.raises(DebugNodeUnavailable):
                get_docker_driver(user_id=1, debug_required=True)
            remote.assert_not_called()


class TestComposeDebugStereotype:
    """The compose half. Every assertion here failed to catch the live bug before #1301: the value
    was PRESENT and still never reached the node, because it carried literal quotes.
    """

    @staticmethod
    def _services(filename: str) -> dict:
        # Parsed as YAML, not grepped, because the bug WAS the quoting: the node receives whatever
        # a YAML parser resolves this to, and `KEY='{...}'` resolves to a value with literal
        # apostrophes in it. A substring assertion cannot see the difference; this can.
        import yaml

        class _Loader(yaml.SafeLoader):
            """SafeLoader that tolerates compose's `!override` / `!reset` merge tags."""

        _Loader.add_multi_constructor(
            "!", lambda loader, suffix, node: loader.construct_mapping(node, deep=True)
            if isinstance(node, yaml.MappingNode) else None)
        return yaml.load((REPO_ROOT / filename).read_text(), Loader=_Loader)["services"]

    def _stereotype_extra(self, filename: str, service: str):
        env = self._services(filename)[service]["environment"]
        values = [entry.split("=", 1)[1] for entry in env
                  if entry.split("=", 1)[0] == "SE_NODE_STEREOTYPE_EXTRA"]
        assert values, f"{service} declares no SE_NODE_STEREOTYPE_EXTRA"
        return json.loads(values[0])

    def test_the_debug_node_declares_lem_debug_true_as_PARSEABLE_json(self):
        # json.loads is the regression guard. `SE_NODE_STEREOTYPE_EXTRA='{"lem:debug":true}'` (the
        # shipped form until #1301) passes a substring check and fails here, exactly as the node's
        # own json_merge.py failed on it: "Failed to merge ... Keep using main stereotype".
        assert self._stereotype_extra("docker-compose.grid.yml",
                                      "selenium-node-debug") == {"lem:debug": True}

    def test_the_pool_declares_lem_debug_false_so_the_pin_is_exclusive(self):
        # Without this the debug capability is decoration: Grid ignores an extension capability a
        # stereotype does not declare, so a `lem:debug=true` request matches all 8 pool nodes too.
        assert self._stereotype_extra("docker-compose.grid.yml",
                                      "selenium-node-chrome") == {"lem:debug": False}

    def test_the_standalone_topology_declares_the_pool_side_too(self):
        assert self._stereotype_extra("docker-compose.yml",
                                      "selenium-chrome") == {"lem:debug": False}
