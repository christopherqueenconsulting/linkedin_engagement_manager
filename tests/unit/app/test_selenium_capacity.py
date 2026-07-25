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
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
PROD_OVERLAY = (REPO_ROOT / "docker-compose.prod.yml").read_text()


def _service_block(compose: str, name: str) -> str:
    return re.split(r"\n  (?=\w)", compose.split(f"\n  {name}:\n")[1])[0]


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
        for knob in ("SE_NODE_MAX_SESSIONS", "SELENIUM_CONCURRENCY", "shm_size"):
            assert knob not in PROD_OVERLAY


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
