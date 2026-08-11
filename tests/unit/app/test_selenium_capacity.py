"""Guards for the Selenium capacity invariant (issue #552).

The browser pool is a fixed number of Chrome session slots shared by every Selenium lane
worker. If the lanes collectively request MORE concurrency than `SE_NODE_MAX_SESSIONS`,
tasks block on session creation and time-sensitive work (pre-post commenting, golden-hour
loops) fires late; if they request LESS, paid-for slots sit idle. Nothing in the app code
fails when the two drift apart — only these assertions do. See docs/scaling-plan.md §5a.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

# Sessions the watchable debug node offers, deliberately OUTSIDE the cap == Σ-lanes arithmetic
# (issue #1301). Two, because two consumers genuinely overlap: a probe run (the weekly drift sweep
# is minutes of Chrome) and an agent or the owner opening the Selenium MCP browser. A third is
# refused immediately rather than queued.
DEBUG_NODE_SESSIONS = 2
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
PROD_OVERLAY = (REPO_ROOT / "docker-compose.prod.yml").read_text()
GRID_OVERLAY = (REPO_ROOT / "docker-compose.grid.yml").read_text()


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
    """docker-compose.grid.yml is the Phase-2 horizontal path (issue #556).

    It replaces the standalone with hub + N single-session nodes, so the SAME cap == Σ-lanes
    invariant has to hold there — only now the cap is the node count. See docs/SELENIUM_GRID.md.
    """

    @staticmethod
    def _node_block() -> str:
        return _service_block(GRID_OVERLAY, "selenium-node-chrome")

    def test_default_node_count_equals_the_summed_lane_concurrency(self):
        # One session per node, so the POOL count IS the session cap the lanes may consume. The
        # debug node is deliberately excluded — it is a ninth node the lanes never size for.
        pool = int(re.search(r"replicas: \$\{SELENIUM_GRID_NODES:-(\d+)\}", self._node_block()).group(1))
        sessions_per_node = int(re.search(r"SE_NODE_MAX_SESSIONS=(\d+)", self._node_block()).group(1))
        assert sessions_per_node == 1
        assert pool * sessions_per_node == sum(_lane_concurrencies(COMPOSE).values())

    def test_the_debug_node_is_extra_capacity_not_a_borrowed_lane_slot(self):
        # Owner decision 2026-07-27: debugging must not cost a production slot. The debug node sits
        # ON TOP of the pool, so the lane invariant above stays keyed on the pool alone — the
        # assertion that matters is that its sessions are NOT in that arithmetic, which is exactly
        # what `test_default_node_count_equals_the_summed_lane_concurrency` measures (pool replicas,
        # never this block). If someone ever folds it into the pool, that test breaks.
        #
        # #1301 raised it from 1 to DEBUG_NODE_SESSIONS: the probe and the Selenium MCP browser are
        # now BOTH pinned here, and at one session the Monday drift sweep made every agent browser
        # request fail for its duration. The count is pinned here so raising it stays a decision
        # with a resource budget attached (below), not a knob someone nudges.
        # Comments stripped first: the reasoning above lives in that block and NAMES the pool's
        # `replicas`, which a raw substring check would read as a redefinition.
        debug = _config_only(_service_block(GRID_OVERLAY, "selenium-node-debug"))
        assert int(re.search(r"SE_NODE_MAX_SESSIONS=(\d+)", debug).group(1)) == DEBUG_NODE_SESSIONS
        assert "replicas" not in debug  # exactly one container, never scaled

    def test_the_debug_nodes_resources_cover_every_session_it_offers(self):
        # Same rule as the pool (§5b: ~1 vCPU / 1.5 GB + 2 GB shm per concurrent session). A debug
        # node with 2 sessions on a 1-session budget is the "slow session looks non-human to
        # LinkedIn" failure, and it would hit the read-only probe — the one session whose whole
        # job is to report what LinkedIn's DOM does.
        debug = _service_block(GRID_OVERLAY, "selenium-node-debug")
        assert float(re.search(r"cpus: '([\d.]+)'", debug).group(1)) >= DEBUG_NODE_SESSIONS
        assert int(re.search(r"memory: (\d+)m", debug).group(1)) >= 1536 * DEBUG_NODE_SESSIONS
        assert int(re.search(r"shm_size: (\d+)g", debug).group(1)) >= 2 * DEBUG_NODE_SESSIONS

    def test_the_debug_node_is_not_counted_in_the_lane_arithmetic(self):
        # The invariant CLAUDE.md states, checked from the other side: the summed lane concurrency
        # equals the POOL, and stays right even though the box now runs more Chrome slots than that.
        pool = int(re.search(r"replicas: \$\{SELENIUM_GRID_NODES:-(\d+)\}",
                             self._node_block()).group(1))
        assert pool == sum(_lane_concurrencies(COMPOSE).values())
        assert pool + DEBUG_NODE_SESSIONS > sum(_lane_concurrencies(COMPOSE).values())

    def test_the_debug_node_publishes_novnc(self):
        # The whole reason it exists: lemvnc needs a container that actually serves 7900. A debug
        # node without the publish is just a pool node with a name.
        assert "7900:7900" in _service_block(GRID_OVERLAY, "selenium-node-debug")

    def test_the_debug_node_registers_under_a_stable_name(self):
        # Its ninth slot has to be droppable from the capacity monitor's saturation denominator
        # (capacity_alerts._pool_slots matches the /status uri host). Without SE_NODE_HOST the node
        # registers under a per-restart container IP, nothing matches, and the pool silently reads
        # 9 slots — which is the difference between a breach and an "ok" at full lane load.
        from cqc_lem.utilities.env_constants import SELENIUM_DEBUG_NODE_HOST

        debug = _config_only(_service_block(GRID_OVERLAY, "selenium-node-debug"))
        assert f"SE_NODE_HOST={SELENIUM_DEBUG_NODE_HOST}" in debug

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


class TestDeployComposesTheDeployedTopology:
    """The overlay only matters if a deploy actually composes it.

    Before this, `deploy.sh` used base + prod only, so every release quietly put the box back on
    the standalone (issue: the 2026-07-27 cutover lasted until the next deploy).
    """

    DEPLOY = (REPO_ROOT / "scripts" / "deploy.sh").read_text()

    def test_the_grid_overlay_is_composed_by_default(self):
        assert "-f docker-compose.grid.yml" in self.DEPLOY
        assert "env_value SELENIUM_TOPOLOGY grid" in self.DEPLOY

    def test_standalone_is_the_one_flag_rollback(self):
        # Only the exact word falls back, and it must NOT drag the overlay in with it.
        block = self.DEPLOY.split("case \"${SELENIUM_TOPOLOGY}\" in")[1].split("esac")[0]
        standalone = block.split("standalone)")[1].split(";;")[0]
        assert "docker-compose.grid.yml" not in standalone

    def test_an_unrecognised_topology_is_named_not_silently_honoured(self):
        # A typo'd value used to mean "deploy the standalone" — the same invisible drift this whole
        # block exists to end. It must warn and take the documented default.
        assert "unrecognised SELENIUM_TOPOLOGY" in self.DEPLOY

    def test_the_topology_is_read_with_the_shared_env_parser(self):
        # env_value() strips quotes, inline comments and stray whitespace; a second hand-rolled
        # grep|cut did not, so `SELENIUM_TOPOLOGY=standalone # rollback` resolved to neither value.
        assert "grep -E '^SELENIUM_TOPOLOGY=' " not in self.DEPLOY
        assert self.DEPLOY.index("env_value() {") < self.DEPLOY.index("SELENIUM_TOPOLOGY:-$(env_value")

    def test_both_transition_directions_evict_the_other_topology_first(self):
        # A compose profile stops a service being STARTED, not one already RUNNING — so whichever
        # topology is up keeps holding 4444 and the incoming one fails to bind (leaving, in the grid
        # direction, a hub running with no network attached). The rollback direction is the one that
        # runs during an incident, and the hub answers to the `selenium-chrome` alias, so leaving it
        # up would also make that name resolve to two containers.
        assert "docker rm -f selenium-chrome" in self.DEPLOY
        assert "rm -sf selenium-hub selenium-node-chrome selenium-node-debug" in self.DEPLOY

    def test_the_eviction_happens_before_anything_is_brought_up(self):
        assert self.DEPLOY.index("docker rm -f selenium-chrome") < self.DEPLOY.index("${COMPOSE} pull")


class TestLoadTestMirrorsTheDeployedTopology:
    """The load-test harness projects a VPS/second-box decision from these numbers.

    They must be the numbers actually deployed — not a snapshot of them (issue #556).
    """

    def test_default_lanes_match_compose(self):
        from cqc_lem.utilities.selenium_load_test import DEFAULT_LANES

        assert DEFAULT_LANES == _lane_concurrencies(COMPOSE)

    def test_default_session_cap_matches_compose(self):
        from cqc_lem.utilities.selenium_load_test import DEFAULT_SESSION_CAP

        assert DEFAULT_SESSION_CAP == _max_sessions(COMPOSE)
