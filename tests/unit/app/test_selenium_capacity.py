"""Guards for the Selenium capacity invariant (issue #552).

The browser pool is a fixed number of Chrome session slots shared by every Selenium lane
worker. If the lanes collectively request MORE concurrency than `SE_NODE_MAX_SESSIONS`,
tasks block on session creation and time-sensitive work (pre-post commenting, golden-hour
loops) fires late; if they request LESS, paid-for slots sit idle. Nothing in the app code
fails when the two drift apart — only these assertions do. See docs/scaling-plan.md §5a.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
PROD_OVERLAY = (REPO_ROOT / "docker-compose.prod.yml").read_text()
GRID_OVERLAY = (REPO_ROOT / "docker-compose.grid.yml").read_text()
DEPLOY_SH = (REPO_ROOT / "scripts" / "deploy.sh").read_text()


def _service_block(compose: str, name: str) -> str:
    return re.split(r"\n  (?=\w)", compose.split(f"\n  {name}:\n")[1])[0]


def _overlay_chrome() -> str:
    # An overlay that drops selenium-chrome entirely can't redefine anything.
    return _service_block(PROD_OVERLAY, "selenium-chrome") if "\n  selenium-chrome:\n" in PROD_OVERLAY else ""


def _config_only(block: str) -> str:
    # Knob names are matched as substrings, so prose in comments must not count as a redefinition.
    return "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))


def _max_sessions(compose: str) -> int:
    return int(re.search(r"SE_NODE_MAX_SESSIONS=(\d+)", _service_block(compose, "selenium-chrome")).group(1))


def _lane_concurrencies(compose: str) -> dict[str, int]:
    return {
        queue: int(concurrency)
        for queue, concurrency in re.findall(
            r"SELENIUM_QUEUES=(\w+)\n(?:\s*#.*\n)*\s*- SELENIUM_CONCURRENCY=(\d+)", compose
        )
    }


class TestSessionCapMatchesLaneConcurrency:
    def test_every_lane_declares_a_concurrency(self):
        # Guards the regexes above: a renamed/reformatted env line must fail loudly here
        # rather than silently shrink the sum and make the invariant test pass by accident.
        assert set(_lane_concurrencies(COMPOSE)) == {"se_engage", "se_prepost", "se_outreach", "se_content"}

    def test_summed_lane_concurrency_equals_the_session_cap(self):
        lanes = _lane_concurrencies(COMPOSE)
        assert sum(lanes.values()) == _max_sessions(COMPOSE), lanes

    def test_the_engagement_lane_carries_the_most_slots(self):
        # se_engage runs the 15-minute commenting loops that dominate Selenium-minutes/user/day.
        lanes = _lane_concurrencies(COMPOSE)
        assert lanes["se_engage"] == max(lanes.values())

    def test_the_prepost_lane_keeps_its_own_slots(self):
        # The whole point of the se_prepost split (issue #553) is that the eta-bound warm-up
        # never shares a slot with a golden-hour loop — a lane at 0/absent silently undoes it.
        assert _lane_concurrencies(COMPOSE)["se_prepost"] >= 2

    def test_prod_overlay_does_not_redefine_the_capacity_knobs(self):
        # The invariant is only enforceable in one place; prod inherits the base numbers.
        # The browser-pool knobs are checked inside the overlay's selenium-chrome block only —
        # deploy/cpus/memory legitimately appear on the other services there.
        for knob in ("SE_NODE_MAX_SESSIONS", "shm_size", "deploy", "cpus", "memory"):
            assert knob not in _config_only(_overlay_chrome()), knob
        # Lane concurrency lives on the worker services, so that one spans the whole overlay.
        assert "SELENIUM_CONCURRENCY" not in PROD_OVERLAY


class TestChromeResourceBudget:
    def test_shm_covers_every_concurrent_session(self):
        # Chrome renderers share /dev/shm; ~1g per session is the practical floor before
        # tabs start crashing with "session deleted because of page crash".
        shm = int(re.search(r"shm_size: (\d+)g", _service_block(COMPOSE, "selenium-chrome")).group(1))
        assert shm >= _max_sessions(COMPOSE)

    def test_cpu_limit_covers_every_concurrent_session(self):
        chrome = _service_block(COMPOSE, "selenium-chrome")
        cpus = float(re.search(r"cpus: '([\d.]+)'", chrome).group(1))
        # A session that can't get a core runs slow enough to look non-human to LinkedIn.
        assert cpus >= _max_sessions(COMPOSE)

    def test_memory_limit_stays_within_the_host_chrome_budget(self):
        chrome = _service_block(COMPOSE, "selenium-chrome")
        memory = int(re.search(r"memory: (\d+)g", chrome).group(1))
        # ~1-1.5g per LinkedIn session; 8g is the agreed ceiling on the 31 GiB box.
        assert _max_sessions(COMPOSE) <= memory <= 8


class TestGridOverlay:
    """docker-compose.grid.yml is the Phase-2 horizontal path (issue #556). It replaces the
    standalone with hub + N single-session nodes, so the SAME cap == Σ-lanes invariant has to hold
    there — only now the cap is the node count. See docs/SELENIUM_GRID.md."""

    @staticmethod
    def _node_block() -> str:
        return _service_block(GRID_OVERLAY, "selenium-node-chrome")

    def test_default_node_count_equals_the_summed_lane_concurrency(self):
        # One session per node, so replicas IS the session cap. A Grid that starts with fewer slots
        # than the lanes request would make the cutover itself a capacity regression.
        replicas = int(re.search(r"replicas: \$\{SELENIUM_GRID_NODES:-(\d+)\}", self._node_block()).group(1))
        sessions_per_node = int(re.search(r"SE_NODE_MAX_SESSIONS=(\d+)", self._node_block()).group(1))
        assert sessions_per_node == 1
        assert replicas * sessions_per_node == sum(_lane_concurrencies(COMPOSE).values())

    def test_the_overlay_does_not_restate_lane_concurrency(self):
        # The lanes stay defined in one place; the overlay only moves where the browsers live.
        # Comments are stripped first — the invariant is explained there, not redefined.
        assert "SELENIUM_CONCURRENCY" not in _config_only(GRID_OVERLAY)

    def test_node_resources_match_the_documented_per_session_budget(self):
        # scaling-plan.md §5b budgets ~1 vCPU / 1.5 GB + 2 GB shm per additional session. Nodes
        # sized below that are the "slow session looks non-human to LinkedIn" failure, one per node.
        node = self._node_block()
        assert re.search(r"cpus: '1\.0'", node)
        assert re.search(r"memory: 1536m", node)
        assert re.search(r"shm_size: 2g", node)

    def test_the_standalone_is_parked_behind_a_profile_not_deleted(self):
        # Keeping it defined is the rollback: --profile standalone brings the old browser pool back
        # without reverting the overlay.
        assert 'profiles: ["standalone"]' in _service_block(GRID_OVERLAY, "selenium-chrome")

    def test_every_service_that_waited_on_the_standalone_now_waits_on_the_hub(self):
        waiters = [name for name in re.findall(r"\n  ([\w-]+):\n", COMPOSE)
                   if "selenium-chrome:\n        condition" in _service_block(COMPOSE, name)]
        assert waiters, "no service depends on selenium-chrome — did the base compose change?"
        for name in waiters:
            block = _service_block(GRID_OVERLAY, name)
            # `!override` (not a merge) so the replaced map cannot still name the profiled-out
            # standalone, which would make the whole stack wait on a container that never starts.
            assert "depends_on: !override" in block, name
            assert "selenium-chrome" not in block, name
            assert "selenium-hub" in block, name
            assert "SELENIUM_HUB_HOST=selenium-hub" in block, name

    def test_the_hub_is_not_healthy_until_a_node_can_actually_serve(self):
        # A hub with zero registered nodes answers /status but has no capacity.
        hub = _config_only(_service_block(GRID_OVERLAY, "selenium-hub"))
        assert r'\"availability\":\"UP\"' in hub

    def test_the_hub_healthcheck_needs_no_interpreter_in_the_image(self):
        # selenium/hub is a JRE image with no Python — the standalone's `python3 -c` probe would
        # never pass there, leaving the hub permanently unhealthy and every dependant stranded.
        hub = _config_only(_service_block(GRID_OVERLAY, "selenium-hub"))
        healthcheck = hub.split("healthcheck:")[1]
        assert "python" not in healthcheck

    def test_nodes_wait_only_for_the_hub_to_start(self):
        # service_healthy here would deadlock: the hub is healthy only once a node registers.
        assert "condition: service_started" in self._node_block()

    def test_the_event_bus_is_not_published_to_the_world_by_default(self):
        # Registering a node needs no credentials, so a reachable bus is an open door into the Grid.
        hub = _service_block(GRID_OVERLAY, "selenium-hub")
        for port in ("4442", "4443"):
            assert f'"${{SELENIUM_GRID_BUS_BIND:-127.0.0.1}}:{port}:{port}"' in hub

    def test_the_hub_port_defaults_to_loopback_like_the_standalone_does_in_prod(self):
        # This overlay composes AFTER docker-compose.prod.yml, which binds the standalone's 4444 to
        # loopback on purpose. A hub published on all interfaces would undo that on the cutover.
        hub = _service_block(GRID_OVERLAY, "selenium-hub")
        assert '"${SELENIUM_GRID_HUB_BIND:-127.0.0.1}:${SELENIUM_HUB_PORT}:4444"' in hub


class TestLoadTestMirrorsTheDeployedTopology:
    """The load-test harness projects a VPS/second-box decision from these numbers, so they must be
    the numbers actually deployed — not a snapshot of them (issue #556)."""

    def test_default_lanes_match_compose(self):
        from cqc_lem.utilities.selenium_load_test import DEFAULT_LANES

        assert DEFAULT_LANES == _lane_concurrencies(COMPOSE)

    def test_default_session_cap_matches_compose(self):
        from cqc_lem.utilities.selenium_load_test import DEFAULT_SESSION_CAP

        assert DEFAULT_SESSION_CAP == _max_sessions(COMPOSE)


def _run_deploy_prefix(tmp_path: Path, env_file: str | None = None, **env: str) -> subprocess.CompletedProcess:
    """Execute deploy.sh up to (not including) the first side effect, then print what it resolved.

    Everything above the git-sync step is pure variable/function setup, so it can run for real —
    and it must, because the failure this guards is a `set -euo pipefail` abort, which no
    substring assertion on the source can see.
    """
    prefix = DEPLOY_SH.split("# 1. Sync compose files")[0]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "deploy.sh"
    script.write_text(prefix + '\necho "RESOLVED ${SELENIUM_TOPOLOGY} :: ${COMPOSE}"\n')
    if env_file is not None:
        (tmp_path / ".env").write_text(env_file)
    clean = {k: v for k, v in os.environ.items() if k != "SELENIUM_TOPOLOGY"}
    return subprocess.run(
        ["bash", str(script), "v1.2.3"], capture_output=True, text=True, env={**clean, **env}
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
class TestDeployComposesTheGridTopology:
    """The overlay only matters if the DEPLOY uses it. `docker-compose.grid.yml` shipped with #556
    but `deploy.sh` composed base+prod only, so every release silently reverted the box to the
    standalone — healthy, and quietly the wrong topology."""

    def test_grid_is_the_default_when_the_env_file_has_no_topology_key(self, tmp_path):
        # The state of every box today: the key is new, so no .env carries it. Reading it with a
        # bare `grep` exits 1 here and `set -euo pipefail` aborts the deploy before check_env.sh.
        result = _run_deploy_prefix(tmp_path, env_file="IMAGE_TAG=v1.2.3\nAPI_PORT=8000\n")
        assert result.returncode == 0, result.stderr
        assert "RESOLVED grid :: " in result.stdout
        assert "-f docker-compose.grid.yml" in result.stdout

    def test_grid_is_the_default_when_there_is_no_env_file_at_all(self, tmp_path):
        result = _run_deploy_prefix(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "-f docker-compose.grid.yml" in result.stdout

    def test_env_file_can_fall_back_to_the_standalone(self, tmp_path):
        result = _run_deploy_prefix(tmp_path, env_file="SELENIUM_TOPOLOGY=standalone\n")
        assert result.returncode == 0, result.stderr
        assert "RESOLVED standalone :: " in result.stdout
        assert "docker-compose.grid.yml" not in result.stdout

    def test_an_exported_value_beats_the_env_file(self, tmp_path):
        result = _run_deploy_prefix(
            tmp_path, env_file="SELENIUM_TOPOLOGY=standalone\n", SELENIUM_TOPOLOGY="grid"
        )
        assert result.returncode == 0, result.stderr
        assert "-f docker-compose.grid.yml" in result.stdout

    def test_an_unrecognized_value_falls_forward_to_grid_not_silently_back(self, tmp_path):
        # A typo must not revert the topology — that is the exact drift this change exists to stop.
        result = _run_deploy_prefix(tmp_path, env_file="SELENIUM_TOPOLOGY=Grid\n")
        assert result.returncode == 0, result.stderr
        assert "-f docker-compose.grid.yml" in result.stdout
        assert "WARN: unrecognized SELENIUM_TOPOLOGY" in result.stdout

    def test_the_stale_browser_is_evicted_after_the_drain_and_before_any_up(self):
        # Both topologies bind 4444 and a compose PROFILE does not stop an already-running
        # container, so the old one must go first. But evicting a browser kills every live session
        # in it — after the drain, or the deploy does the mid-flight kill #549 exists to prevent.
        evict = DEPLOY_SH.index("docker rm -f \"${STALE_BROWSER}\"")
        assert DEPLOY_SH.index("maint drain") < evict < DEPLOY_SH.index("${COMPOSE} up -d mysql")

    def test_the_rollback_direction_evicts_the_hub(self):
        # Rolling back to the standalone hits the SAME collision with the roles swapped.
        block = DEPLOY_SH.split("# 4c.")[1].split("# 5.")[0]
        assert 'STALE_BROWSER="selenium-chrome"' in block
        assert 'STALE_BROWSER="selenium-hub"' in block
