"""Guards for Celery node-name stability and uniqueness (issue #1869).

A Celery node name is `<prefix>@<host>`. The prefix comes from `--hostname=` in the worker start
script; the host half is `%h`, which Celery expands with `socket.gethostname()`. Docker sets that
hostname to the random container ID unless compose pins it — so before #1869 every deploy minted a
fresh set of node names, Flower's worker list grew without bound, and no lane could be followed
across releases. `docker-compose.yml` now pins `hostname:` on each worker service.

Two things have to stay true, and nothing in the app fails when they stop being true:

1. **Stable.** The host half is a LITERAL in compose. `${CELERY_WORKER_HOST}` was not good enough:
   an unset var interpolates to empty and puts Docker straight back on the container ID.
2. **Unique cluster-wide.** Two workers sharing a node name is a worse outage than the churn this
   fixed — control messages and revokes are addressed by node name, so they would cross-talk.

The healthchecks are the third leg: they ping `…-worker@$(hostname)`, so the container hostname is
the ONE source of truth for both halves of the agreement. If the two ever disagree, every Selenium
lane reports unhealthy while running perfectly. That agreement is asserted here directly.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
PROD_OVERLAY = (REPO_ROOT / "docker-compose.prod.yml").read_text()
GRID_OVERLAY = (REPO_ROOT / "docker-compose.grid.yml").read_text()
DOCKERFILE = (REPO_ROOT / "compose" / "local" / "Dockerfile").read_text()

# In-image path -> the script the Dockerfile COPYs there. Only WORKER scripts: beat and flower
# register no node name (asserted below), so they cannot participate in a collision.
WORKER_START_SCRIPTS = {
    "/start-celeryworker": Path("compose/local/celery/worker/start"),
    "/start-celeryworker-selenium": Path("compose/local/celery/worker/start-selenium"),
    "/start-celeryworker-solo": Path("compose/local/celery/worker/start-solo-pool"),
}

# The node names an operator should see in Flower, before AND after any deploy. Pinned so a rename
# is a deliberate edit with a changelog line, never a side effect of touching a queue name.
EXPECTED_NODE_NAMES = {
    "celery_worker": "main-worker@celery-worker",
    "celery_worker_selenium": "selenium-se_engage-worker@celery-worker-selenium",
    "celery_worker_selenium_prepost": "selenium-se_prepost-worker@celery-worker-selenium-prepost",
    "celery_worker_selenium_outreach": "selenium-se_outreach-worker@celery-worker-selenium-outreach",
    "celery_worker_selenium_content": "selenium-se_content-worker@celery-worker-selenium-content",
}


def _service_block(compose: str, name: str) -> str:
    return re.split(r"\n  (?=\w)", compose.split(f"\n  {name}:\n")[1])[0]


def _config_only(block: str) -> str:
    """Comments explain the invariant and NAME the keys, so they must not read as a redefinition."""
    return "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))


def _services(compose: str) -> list[str]:
    return re.findall(r"\n  ([\w-]+):\n", compose)


def _scalar(block: str, key: str) -> str | None:
    match = re.search(rf"^    {key}: (.+)$", _config_only(block), re.MULTILINE)
    return match.group(1).strip() if match else None


def _environment(block: str) -> dict[str, str]:
    return dict(re.findall(r"^      - ([A-Z_]+)=(.*)$", _config_only(block), re.MULTILINE))


def _worker_services(compose: str) -> dict[str, str]:
    """{service name: the in-image start script it runs} for every celery WORKER service."""
    found = {}
    for name in _services(compose):
        command = _scalar(_service_block(compose, name), "command")
        if command in WORKER_START_SCRIPTS:
            found[name] = command
    return found


def _hostname_template(script: str) -> str | None:
    """The `--hostname=` argument as written in a start script, quotes stripped."""
    match = re.search(r"--hostname=(\"[^\"]+\"|\S+?)\s*\\", script)
    return match.group(1).strip('"') if match else None


def _expand(template: str, env: dict[str, str], hostname: str) -> str:
    """Resolve the shell/Celery substitutions the container would perform at start-up."""
    # `${VAR//,/-}` (comma-joined queue lists become one token) then plain `${VAR}`.
    template = re.sub(r"\$\{(\w+)//,/-}", lambda m: env[m.group(1)].replace(",", "-"), template)
    template = re.sub(r"\$\{(\w+)}", lambda m: env[m.group(1)], template)
    return template.replace("%h", hostname)


def _node_names(compose: str) -> dict[str, str]:
    names = {}
    for service, command in _worker_services(compose).items():
        block = _service_block(compose, service)
        script = (REPO_ROOT / WORKER_START_SCRIPTS[command]).read_text()
        names[service] = _expand(_hostname_template(script), _environment(block), _scalar(block, "hostname"))
    return names


class TestWorkerServicesAreDiscoverable:
    """Guards the parsing every assertion below depends on — a silently empty sweep proves nothing."""

    def test_every_celery_worker_service_is_found(self):
        assert set(_worker_services(COMPOSE)) == set(EXPECTED_NODE_NAMES)

    def test_the_dockerfile_still_installs_the_scripts_those_commands_name(self):
        # The service `command:` is an in-image path; it only means what this test thinks it means
        # while the Dockerfile keeps COPYing that source script there.
        for image_path, source in WORKER_START_SCRIPTS.items():
            assert f"COPY ./{source.as_posix()} {image_path}\n" in DOCKERFILE, image_path

    def test_every_worker_script_pins_a_hostname(self):
        # A script with no `--hostname` falls back to Celery's default `celery@<host>` — one shared
        # prefix for every worker that omits it, which is the collision this file exists to refuse.
        for source in WORKER_START_SCRIPTS.values():
            assert _hostname_template((REPO_ROOT / source).read_text()), source


class TestNodeNamesAreStableAcrossDeploys:
    def test_every_worker_service_pins_a_literal_hostname(self):
        for service in _worker_services(COMPOSE):
            hostname = _scalar(_service_block(COMPOSE, service), "hostname")
            assert hostname, f"{service} has no hostname: — Docker will use the container ID"
            # An interpolated value is not stability: an unset var expands to empty, and an empty
            # hostname puts Docker back on the container ID with no error anywhere.
            assert "$" not in hostname, f"{service} hostname is interpolated: {hostname}"

    def test_node_names_are_the_expected_set(self):
        assert _node_names(COMPOSE) == EXPECTED_NODE_NAMES

    def test_no_placeholder_survives_into_a_node_name(self):
        # Belt and braces on _expand: an unresolved `%h`/`${…}` would make the pinned names above
        # agree with each other and with nothing the container actually registers.
        for service, node in _node_names(COMPOSE).items():
            assert "%" not in node and "$" not in node, (service, node)


class TestNoTwoWorkersShareANodeName:
    def test_node_names_are_unique(self):
        names = _node_names(COMPOSE)
        assert len(set(names.values())) == len(names), names

    def test_both_halves_are_independently_unique(self):
        # The prefixes alone would do it today, but they are DERIVED from SELENIUM_QUEUES: copying a
        # lane service and forgetting to change the queue makes two prefixes identical. Distinct
        # hostnames are the second, independent guarantee that such a slip cannot collide.
        names = _node_names(COMPOSE)
        prefixes = [node.split("@")[0] for node in names.values()]
        hosts = [node.split("@")[1] for node in names.values()]
        assert len(set(prefixes)) == len(prefixes), prefixes
        assert len(set(hosts)) == len(hosts), hosts

    def test_beat_and_flower_register_no_node_name(self):
        # They are not workers, so they never appear in Flower's worker list and are outside the
        # uniqueness argument — but only for as long as their commands stay off the worker scripts.
        for service in ("celery_beat", "flower"):
            command = _scalar(_service_block(COMPOSE, service), "command")
            assert command is not None, service
            assert command not in WORKER_START_SCRIPTS, (service, command)

    def test_beat_and_flower_still_pin_a_stable_hostname(self):
        # Not for a node name — for $HOSTNAME, which logger.py sends as the OTel
        # service.instance.id. Left unpinned, every deploy files their logs under a fresh instance,
        # which is the same churn one layer down. It also means a `--hostname` added to either
        # command later inherits a stable host instead of the container ID.
        for service in ("celery_beat", "flower"):
            hostname = _scalar(_service_block(COMPOSE, service), "hostname")
            assert hostname and "$" not in hostname, (service, hostname)


class TestHealthchecksAgreeWithTheNodeName:
    """The `--hostname` flag and the healthcheck's `-d` target must resolve to the same string.

    They are written in two different files and expanded by two different mechanisms (`%h` in
    Celery, `$(hostname)` in the container shell), which is exactly why they can drift. When they
    do, the lane runs perfectly and reports unhealthy forever.
    """

    @staticmethod
    def _selenium_services() -> list[str]:
        return [name for name, command in _worker_services(COMPOSE).items()
                if command == "/start-celeryworker-selenium"]

    def test_every_selenium_lane_pings_a_specific_node(self):
        # Without `-d` the probe passes when ANY worker answers, so a dead lane reads healthy —
        # and the agreement asserted below would be vacuous.
        services = self._selenium_services()
        assert services
        for service in services:
            healthcheck = _config_only(_service_block(COMPOSE, service)).split("healthcheck:")[1]
            assert "inspect ping -d " in healthcheck, service

    def test_the_healthcheck_target_resolves_to_this_services_node_name(self):
        for service in self._selenium_services():
            block = _service_block(COMPOSE, service)
            healthcheck = _config_only(block).split("healthcheck:")[1]
            target = re.search(r"inspect ping -d (\S+)", healthcheck).group(1)
            # `$$VAR` is compose's escape for a runtime `$VAR`; `$(hostname)` is the shell reading
            # the hostname compose pinned. Resolve both the way the container would.
            resolved = target.replace("$${SELENIUM_QUEUES}", _environment(block)["SELENIUM_QUEUES"])
            resolved = resolved.replace("$(hostname)", _scalar(block, "hostname"))
            assert resolved == _node_names(COMPOSE)[service], service

    def test_the_healthcheck_reads_the_hostname_rather_than_hard_coding_it(self):
        # One source of truth: `hostname:` in compose. A literal host in the healthcheck would keep
        # passing while `--hostname` moved, which is the drift this class is here to prevent.
        for service in self._selenium_services():
            healthcheck = _config_only(_service_block(COMPOSE, service)).split("healthcheck:")[1]
            assert "@$(hostname)" in healthcheck, service


class TestOverlaysInheritRatherThanRestateTheHostnames:
    """A base-file `hostname:` reaches production only if no overlay resets it.

    The stack always composes base + prod (+ grid), so that inheritance is asserted here rather
    than assumed.
    """

    @pytest.mark.parametrize("overlay", ["prod", "grid"])
    def test_no_overlay_redefines_a_worker_hostname(self, overlay: str):
        text = PROD_OVERLAY if overlay == "prod" else GRID_OVERLAY
        covered = [s for s in _worker_services(COMPOSE) if f"\n  {s}:\n" in text]
        # The overlays patch every worker (images, volumes, hub wiring); an empty list means the
        # service names moved and this test stopped looking at anything.
        assert covered, overlay
        for service in covered:
            block = _config_only(_service_block(text, service))
            assert "hostname:" not in block, (overlay, service)

    def test_the_prod_overlay_adds_no_extra_celery_worker_service(self):
        # Only web_api is duplicated blue/green there. A second copy of a worker service would be a
        # second container running the SAME start script — and, with a pinned hostname, the same
        # node name, which is the collision this whole file refuses.
        assert not set(_worker_services(PROD_OVERLAY)) - set(_worker_services(COMPOSE))
        assert not set(_worker_services(GRID_OVERLAY)) - set(_worker_services(COMPOSE))
