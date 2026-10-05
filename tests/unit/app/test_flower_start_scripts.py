"""Guard the Flower start scripts against CLI options that crash-loop the container.

flower 2.2.0 (#2227) started exiting 1 on any ``--broker-api`` whose scheme is not http(s). The
scripts passed the ``redis://`` broker URL there, so ``celery_flower`` restarted every minute on
0.181.x while every other service stayed green. ``--broker-api`` is the RabbitMQ management-API
URL and does nothing on Redis, so neither script may pass it a non-http value.
"""

import re
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FLOWER_DIR = REPO_ROOT / "compose" / "local" / "celery" / "flower"
SCRIPTS = ("start", "start-no-wait")


def _celery_argv(script: str) -> list[str]:
    """Return the argv of the script's ``celery ... flower ...`` command, comments dropped."""
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    command = next(
        line for line in joined.splitlines()
        if line.lstrip().startswith("celery ") and " flower " in line
    )
    return shlex.split(command, comments=True)


@pytest.mark.parametrize("name", SCRIPTS)
class TestFlowerStartScripts:
    def test_broker_api_is_absent_or_http(self, name: str) -> None:
        argv = _celery_argv((FLOWER_DIR / name).read_text())
        for arg in argv:
            if arg.startswith("--broker-api") or arg.startswith("--broker_api"):
                value = arg.partition("=")[2]
                assert value.startswith(("http://", "https://")), arg

    def test_broker_is_a_celery_option_before_flower(self, name: str) -> None:
        argv = _celery_argv((FLOWER_DIR / name).read_text())
        flower_at = argv.index("flower")
        broker_at = [i for i, arg in enumerate(argv) if arg.startswith("--broker=")]
        assert broker_at, argv
        assert all(i < flower_at for i in broker_at), argv
