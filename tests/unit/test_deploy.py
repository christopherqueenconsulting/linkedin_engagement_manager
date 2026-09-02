"""Regression tests for scripts/deploy.sh converge/verify logic (issue #831)."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"


_FAKE_COMPOSE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os
    import sys

    args = sys.argv[1:]
    state_file = os.environ.get("DEPLOY_FAKE_STATE")
    mode = os.environ.get("DEPLOY_FAKE_MODE", "pass")
    fail_count = int(os.environ.get("DEPLOY_FAKE_UP_FAIL_COUNT", "0"))

    # `config --services` is profile-filtered by compose, so the parked standalone
    # `selenium-chrome` never appears here even when a stopped container of it still exists.
    _EXPECTED = ["web_app", "web_api_blue", "web_api_green", "celery_worker", "celery_beat"]
    _ALL_RUNNING = [svc + " running" for svc in _EXPECTED]

    _ALL_PS_A = {
        "pass": _ALL_RUNNING,
        "created": ["web_app running", "celery_worker created"],
        "exited": ["web_app running", "celery_worker exited"],
        "restarting": ["web_app running", "celery_worker restarting"],
        "missing": _ALL_RUNNING,
        # A container of a profile-disabled service the deploy never touches. `ps` labels by
        # PROJECT, not by profile, so it shows up Exited forever and must NOT fail the deploy.
        "profile_leftover": _ALL_RUNNING + ["selenium-chrome exited"],
    }

    _ALL_PS = {
        "pass": _EXPECTED,
        "created": _EXPECTED,
        "exited": _EXPECTED,
        "restarting": _EXPECTED,
        "missing": ["web_app", "web_api_blue", "web_api_green", "celery_worker"],
        "profile_leftover": _EXPECTED,
    }

    def inc_state() -> int:
        if not state_file:
            return 0
        n = 0
        if os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                n = int((f.read().strip() or "0"))
        n += 1
        os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(str(n))
        return n

    if args[:2] == ["config", "--services"]:
        for svc in _EXPECTED + ["flyway"]:
            print(svc)
    elif "ps" in args and "-a" in args:
        for line in _ALL_PS_A.get(mode, _ALL_PS_A["pass"]):
            print(line)
    elif "ps" in args:
        for svc in _ALL_PS.get(mode, _ALL_PS["pass"]):
            print(svc)
    elif args[:3] == ["up", "-d", "--remove-orphans"]:
        n = inc_state()
        if n <= fail_count:
            msg = os.environ.get(
                "DEPLOY_FAKE_UP_ERROR",
                "Error response from daemon: No such container: 5edffe0695",
            )
            print(msg, file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    sys.exit(0)
    """
)


# A `docker` stub for the probe/sweep tests. `_run` puts tmp_path first on PATH, so writing this
# as tmp_path/"docker" shadows the real binary. Driven entirely by env vars so one stub covers
# "the container is missing", "the app is sick" and "there is a renamed shadow" without branching
# on anything the script itself controls.
_FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os
    import sys
    import time

    args = sys.argv[1:]

    def containers():
        # DOCKER_FAKE_CONTAINERS: comma-separated "<id>:<name>:<true|false>"
        out = []
        for item in os.environ.get("DOCKER_FAKE_CONTAINERS", "").split(","):
            if item.strip():
                cid, name, running = item.strip().split(":")
                out.append((cid, name, running))
        return out

    def bump(path):
        n = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                n = int(fh.read().strip() or "0")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(n + 1))

    if args[:1] == ["exec"]:
        counter = os.environ.get("DOCKER_FAKE_EXEC_COUNT")
        if counter:
            bump(counter)
        mode = os.environ.get("DOCKER_FAKE_EXEC_MODE", "ok")
        if mode == "ok":
            sys.exit(0)
        if mode == "nosuch":
            target = args[1] if len(args) > 1 else "?"
            print("Error response from daemon: No such container: " + target, file=sys.stderr)
            sys.exit(1)
        # The container resolves; the app inside it is not answering yet. The small real sleep
        # bounds how many times a sub-second timeout can spin this stub up.
        time.sleep(0.05)
        print("curl: (7) Failed to connect to localhost port 8000: Connection refused",
              file=sys.stderr)
        sys.exit(1)

    if args[:2] == ["ps", "-aq"]:
        for cid, _name, _running in containers():
            print(cid)
        sys.exit(0)

    if args[:1] == ["ps"]:
        for name in os.environ.get("DOCKER_FAKE_PS_NAMES", "").split(","):
            if name.strip():
                print(name.strip())
        sys.exit(0)

    if args[:1] == ["inspect"]:
        fmt = args[args.index("-f") + 1]
        wanted = args[-1]
        for cid, name, running in containers():
            if cid == wanted:
                print(running if "State.Running" in fmt else "/" + name)
                sys.exit(0)
        sys.exit(1)

    if args[:1] == ["rm"]:
        removed = os.environ.get("DOCKER_FAKE_REMOVED")
        if removed:
            with open(removed, "a", encoding="utf-8") as fh:
                fh.write(" ".join(args[1:]) + "\\n")
        sys.exit(0)

    sys.exit(0)
    """
)


def _write_docker_stub(tmp_path: Path) -> None:
    """Install the fake `docker` binary into tmp_path, which `_run` puts first on PATH."""
    stub = tmp_path / "docker"
    stub.write_text(_FAKE_DOCKER, encoding="utf-8")
    stub.chmod(0o755)


def _run(tmp_path: Path, env_overrides: dict[str, str], bash_code: str) -> subprocess.CompletedProcess[str]:
    """Source deploy.sh in a subshell, override COMPOSE to a fake, and run bash_code."""
    fake = tmp_path / "docker-compose-fake.py"
    fake.write_text(_FAKE_COMPOSE, encoding="utf-8")
    fake.chmod(0o755)

    # Avoid real sleeps in converge retry path.
    sleep_bin = tmp_path / "sleep"
    sleep_bin.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    sleep_bin.chmod(0o755)

    env = os.environ.copy()
    env.update(env_overrides)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    cmd = f'source "{DEPLOY_SH}"; COMPOSE="{fake}"; {bash_code}'
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        cwd=str(DEPLOY_SH.parent.parent),
        env=env,
    )


class TestConvergeStack:
    def test_retries_once_on_no_such_container(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        result = _run(
            tmp_path,
            {
                "DEPLOY_FAKE_UP_FAIL_COUNT": "1",
                "DEPLOY_FAKE_STATE": str(state),
            },
            "converge_stack",
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert state.read_text(encoding="utf-8") == "2"
        assert "retrying converge" in (result.stdout + result.stderr).lower()

    def test_fails_without_retry_on_other_error(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        result = _run(
            tmp_path,
            {
                "DEPLOY_FAKE_UP_FAIL_COUNT": "1",
                "DEPLOY_FAKE_STATE": str(state),
                "DEPLOY_FAKE_UP_ERROR": "Error: some unrelated compose failure",
            },
            "converge_stack",
        )
        assert result.returncode != 0
        assert state.read_text(encoding="utf-8") == "1"
        assert "No such container" not in result.stdout + result.stderr

    def test_retries_once_on_a_name_collision(self) -> None:
        """An orphaned `<hex>_<name>` container makes the next converge fail with
        'is already in use', not 'No such container'. Retrying only on the latter is why the
        v0.118.0 converge gave up after one attempt and left the worker tier in Created.
        """
        body = DEPLOY_SH.read_text(encoding="utf-8")
        retry_test = body.split("if [[ ${attempts} -lt ${max_attempts} ]]", 1)[1].split("then", 1)[0]
        assert "is already in use" in retry_test
        assert "no such container" in retry_test.lower()

    def test_orphan_sweep_survives_a_failing_docker(self, tmp_path: Path) -> None:
        """The sweep is a diagnostic, so it must degrade to 'sweep nothing' rather than abort the
        deploy. Under `set -e` an unguarded `x="$(docker ...)"` assignment exits the shell the
        moment docker errors — which took the whole converge down with it, `up` never running.

        `_run` puts tmp_path first on PATH, so this docker stub shadows the real binary.
        """
        docker_stub = tmp_path / "docker"
        docker_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        docker_stub.chmod(0o755)
        state = tmp_path / "state"
        result = _run(tmp_path, {"DEPLOY_FAKE_STATE": str(state)}, "converge_stack")
        assert result.returncode == 0, result.stderr + result.stdout
        assert state.read_text(encoding="utf-8") == "1"  # the `up` still ran exactly once


class TestVerifyStackRunning:
    def test_passes_when_all_services_running(self, tmp_path: Path) -> None:
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": "pass"}, "verify_stack_running")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "Verified all expected services are running" in result.stdout

    def test_ignores_a_stopped_container_of_a_profile_disabled_service(self, tmp_path: Path) -> None:
        # `compose ps` labels by project, not by profile: the standalone selenium-chrome the Grid
        # overlay parks for rollback sits Exited indefinitely. Failing on it would break every
        # deploy over a service the deploy never touches.
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": "profile_leftover"}, "verify_stack_running")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "selenium-chrome" not in result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("mode", "needle"),
        [
            ("created", "created"),
            ("exited", "exited"),
            ("restarting", "restarting"),
            ("missing", "not running"),
        ],
    )
    def test_fails_on_unhealthy_service(self, tmp_path: Path, mode: str, needle: str) -> None:
        result = _run(tmp_path, {"DEPLOY_FAKE_MODE": mode}, "verify_stack_running")
        assert result.returncode != 0, result.stdout
        combined = result.stdout + result.stderr
        if mode == "missing":
            assert "celery_beat" in combined
        else:
            assert "celery_worker" in combined
        assert needle in combined


class TestDeployScriptWiring:
    """The functions above are only worth anything if the deploy path actually calls them.

    Unit-testing them in isolation cannot catch a converge/verify call being dropped from `main`,
    which is the exact regression this issue is about (#831).
    """

    BODY = DEPLOY_SH.read_text(encoding="utf-8")

    def test_main_converges_through_the_retrying_helper(self) -> None:
        assert "if ! converge_stack; then" in self.BODY
        # No bare bulk converge left anywhere — including the rollback path, which recreates the
        # same worker tier and runs when something has already gone wrong.
        assert "${COMPOSE} up -d --remove-orphans" not in self.BODY.replace(
            '${COMPOSE} up -d --remove-orphans >"${logfile}" 2>&1', ""
        )

    def test_main_aborts_when_verification_fails(self) -> None:
        block = self.BODY.split("if ! verify_stack_running; then", 1)
        assert len(block) == 2, "main() no longer verifies the converged stack"
        body_lines = block[1].splitlines()
        end = next(i for i, line in enumerate(body_lines) if line.strip() == "fi")
        assert any(line.strip() == "exit 1" for line in body_lines[:end])

    def test_orphan_sweep_runs_before_the_blue_green_flip(self) -> None:
        """The sweep must clear a shadow before anything addresses a colour by name.

        It used to be called only from `converge_stack`, which is step 7 — AFTER the flip — so a
        running `<hex>_web_api_blue` shadow could never be cleared in time to save the deploy that
        tripped over it (issue #1897): every v0.172.8 attempt swept too late.
        """
        section_6 = self.BODY.index('STATE_FILE="${ROOT_DIR}/.active_color"')
        sweep = self.BODY.index("sweep_rename_orphans", section_6)
        active_resolution = self.BODY.index('ACTIVE="$(cat "${STATE_FILE}"')
        assert sweep < active_resolution

    def test_health_probe_call_site_keeps_the_return_code(self) -> None:
        """`if ! color_healthy ...` collapses rc=2 (no such container) into rc=1 (unhealthy).

        The distinction is the whole point of the three-valued probe, so the call site has to
        capture `$?` rather than test the command.
        """
        assert 'color_healthy "${TARGET}" "${HEALTH_TIMEOUT}" || health_rc=$?' in self.BODY
        assert "if ! color_healthy" not in self.BODY
        assert "(( health_rc == 2 ))" in self.BODY

    def test_tag_baseline_is_persisted_before_the_worker_converge(self) -> None:
        # A partial deploy must still leave IMAGE_TAG/.last_good_tag matching what is serving.
        persist = self.BODY.index('persist_image_tag "${TAG}"')
        converge = self.BODY.index("if ! converge_stack; then")
        assert persist < converge


class TestColorHealthy:
    """The blue/green health probe (issue #1897).

    A compose converge left the standby renamed to `<12hex>_web_api_blue`. It was running and
    healthy, but `docker exec web_api_blue` cannot resolve that name — and the probe discarded
    stderr, so a name that does not exist was reported exactly like an app that starts slowly,
    after burning the whole 180s HEALTH_TIMEOUT.
    """

    def test_returns_2_at_once_when_the_container_name_does_not_resolve(self, tmp_path: Path) -> None:
        """`No such container` is terminal: it will not resolve in 180 more seconds.

        Pins both halves — the distinct exit code AND that the probe stops immediately instead of
        spinning out the timeout, which is what made a naming fault read as a slow boot.
        """
        _write_docker_stub(tmp_path)
        calls = tmp_path / "exec-count"
        result = _run(
            tmp_path,
            {
                "DOCKER_FAKE_EXEC_MODE": "nosuch",
                "DOCKER_FAKE_EXEC_COUNT": str(calls),
                "DOCKER_FAKE_PS_NAMES": "web_api_green,1db3f8dadf4d_web_api_blue",
                "API_PORT": "8000",
            },
            "color_healthy blue 180",
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert calls.read_text(encoding="utf-8") == "1", "the probe spun instead of returning"
        combined = result.stdout + result.stderr
        assert "web_api_blue" in combined
        # The shadow it actually found is named, so the log says what to fix.
        assert "1db3f8dadf4d_web_api_blue" in combined

    def test_returns_0_when_the_exec_probe_succeeds(self, tmp_path: Path) -> None:
        """The healthy path is unchanged by the three-valued rewrite."""
        _write_docker_stub(tmp_path)
        result = _run(
            tmp_path,
            {"DOCKER_FAKE_EXEC_MODE": "ok", "API_PORT": "8000"},
            "color_healthy blue 180",
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_returns_1_not_2_when_the_app_is_merely_unhealthy(self, tmp_path: Path) -> None:
        """A container that exists but does not answer must still be the retried timeout path.

        If any exec failure returned 2, the deploy would abort on the first probe of a container
        that is simply still booting — the opposite regression.
        """
        _write_docker_stub(tmp_path)
        calls = tmp_path / "exec-count"
        result = _run(
            tmp_path,
            {
                "DOCKER_FAKE_EXEC_MODE": "refused",
                "DOCKER_FAKE_EXEC_COUNT": str(calls),
                "API_PORT": "8000",
            },
            "color_healthy blue 1",
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert int(calls.read_text(encoding="utf-8")) >= 1
        assert "No such container" not in result.stdout + result.stderr


class TestSweepRunningRenameOrphans:
    """`sweep_rename_orphans` skipped RUNNING orphans, which is why #1897 survived every converge.

    It was written for stopped debris from an interrupted converge. The shadow that broke deploys
    was running, so the sweep declined it every single time it ran.
    """

    SHADOW = "1db3f8dadf4d:1db3f8dadf4d_web_api_blue:true"

    def _sweep(self, tmp_path: Path, env: dict[str, str], active: str) -> subprocess.CompletedProcess[str]:
        """Run the sweep with ROOT_DIR pointed at tmp_path and `.active_color` set to `active`."""
        _write_docker_stub(tmp_path)
        (tmp_path / ".active_color").write_text(f"{active}\n", encoding="utf-8")
        return _run(tmp_path, env, f'ROOT_DIR="{tmp_path}"; sweep_rename_orphans')

    def test_removes_a_running_shadow_of_the_standby_color(self, tmp_path: Path) -> None:
        """The exact #1897 shape: blue is shadowed, nothing holds `web_api_blue`, green is active.

        nginx's upstream addresses the bare name, so a shadow of the standby serves no traffic and
        the `up -d --no-deps web_api_blue` that follows recreates it properly.
        """
        removed = tmp_path / "removed"
        result = self._sweep(
            tmp_path,
            {
                "DOCKER_FAKE_CONTAINERS": self.SHADOW,
                "DOCKER_FAKE_PS_NAMES": "web_api_green",
                "DOCKER_FAKE_REMOVED": str(removed),
            },
            active="green",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert removed.exists(), "the running shadow was skipped again"
        assert "1db3f8dadf4d" in removed.read_text(encoding="utf-8")
        assert "-f" in removed.read_text(encoding="utf-8"), "a running container needs rm -f"
        assert "Removing RUNNING rename-orphan" in result.stdout

    def test_leaves_a_running_shadow_of_the_active_color_alone(self, tmp_path: Path) -> None:
        """Refusing to remove a shadowed ACTIVE colour is a precaution, not a measured fact.

        A compose network alias comes from the SERVICE name at connect time and is not necessarily
        dropped by a rename, so the shadow may still be answering edge traffic. Warn and continue
        rather than risk dropping live requests.
        """
        removed = tmp_path / "removed"
        result = self._sweep(
            tmp_path,
            {
                "DOCKER_FAKE_CONTAINERS": self.SHADOW,
                "DOCKER_FAKE_PS_NAMES": "web_api_green",
                "DOCKER_FAKE_REMOVED": str(removed),
            },
            active="blue",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not removed.exists(), "removed a shadow of the colour that may be serving"
        combined = result.stdout + result.stderr
        assert "WARN" in combined
        assert "1db3f8dadf4d_web_api_blue" in combined

    def test_leaves_a_running_shadow_when_the_bare_name_also_exists(self, tmp_path: Path) -> None:
        """Two containers, one canonical name: nothing here can tell which one is serving."""
        removed = tmp_path / "removed"
        result = self._sweep(
            tmp_path,
            {
                "DOCKER_FAKE_CONTAINERS": self.SHADOW,
                "DOCKER_FAKE_PS_NAMES": "web_api_green,web_api_blue",
                "DOCKER_FAKE_REMOVED": str(removed),
            },
            active="green",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not removed.exists()
        assert "leaving both alone" in result.stdout

    def test_still_removes_a_stopped_orphan(self, tmp_path: Path) -> None:
        """The original issue-#831 behaviour is untouched by widening the sweep."""
        removed = tmp_path / "removed"
        result = self._sweep(
            tmp_path,
            {
                "DOCKER_FAKE_CONTAINERS": "1db3f8dadf4d:1db3f8dadf4d_celery_worker:false",
                "DOCKER_FAKE_PS_NAMES": "web_api_green,celery_worker",
                "DOCKER_FAKE_REMOVED": str(removed),
            },
            active="green",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert removed.read_text(encoding="utf-8").strip() == "1db3f8dadf4d"
        assert "interrupted prior converge" in result.stdout

    def test_ignores_a_container_whose_state_cannot_be_read(self, tmp_path: Path) -> None:
        """An unreadable state is never acted on — the sweep only removes what it can identify."""
        removed = tmp_path / "removed"
        result = self._sweep(
            tmp_path,
            {
                # `docker inspect` on an id the stub does not know exits non-zero, so both the name
                # and the running state come back empty.
                "DOCKER_FAKE_CONTAINERS": "1db3f8dadf4d:1db3f8dadf4d_web_api_blue:unknown",
                "DOCKER_FAKE_PS_NAMES": "web_api_green",
                "DOCKER_FAKE_REMOVED": str(removed),
            },
            active="green",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not removed.exists()
